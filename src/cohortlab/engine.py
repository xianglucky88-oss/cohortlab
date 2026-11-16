import hashlib
import json
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from .policy import QueryPolicy, canonical
from .query import (LANGUAGE_VERSION, LEGACY_LANGUAGE_VERSION, aggregate,
                    instant, matches, prepare, temporal_matches)


class CohortEngine:
    def __init__(self, path, policy=None, clock=None):
        self.policy = policy or QueryPolicy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS subjects (
              tenant TEXT NOT NULL, id TEXT NOT NULL, attributes TEXT NOT NULL, PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS events (
              tenant TEXT NOT NULL, id TEXT NOT NULL, subject TEXT NOT NULL, at INTEGER NOT NULL, kind TEXT NOT NULL,
              PRIMARY KEY(tenant,id), FOREIGN KEY(tenant,subject) REFERENCES subjects(tenant,id));
            CREATE INDEX IF NOT EXISTS events_time ON events(tenant,at,id);
            CREATE TABLE IF NOT EXISTS revisions (tenant TEXT PRIMARY KEY, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS budgets (
              tenant TEXT, principal TEXT, day TEXT, used INTEGER NOT NULL, PRIMARY KEY(tenant,principal,day));
            CREATE TABLE IF NOT EXISTS snapshots (
              id TEXT PRIMARY KEY, tenant TEXT NOT NULL, principal TEXT NOT NULL,
              revision INTEGER NOT NULL, policy TEXT NOT NULL, members TEXT NOT NULL,
              language TEXT NOT NULL DEFAULT '2026-11-v1');
            CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
              id INTEGER PRIMARY KEY, tenant TEXT, principal TEXT, at TEXT,
              fingerprint TEXT, policy TEXT, status TEXT, cached INTEGER,
              language TEXT NOT NULL DEFAULT '2026-11-v1');
        """)
        self._migrate()

    def _migrate(self):
        """Upgrade pre-attribute databases in place. Legacy events carry empty
        attributes, which every v2 predicate treats as a missing field."""
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
        if "attributes" not in columns:
            self.db.execute("ALTER TABLE events ADD COLUMN attributes TEXT NOT NULL DEFAULT '{}'")
        for table, default in (("snapshots", "'2026-11-v1'"), ("audit", "'2026-11-v1'")):
            columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if "language" not in columns:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN language TEXT NOT NULL DEFAULT {default}")
        self.db.commit()

    @staticmethod
    def _identity(value):
        if not isinstance(value, str) or not value.strip(): raise ValueError("identifiers must be nonempty strings")
        return value

    @staticmethod
    def _attributes(value):
        if not isinstance(value, dict) or any(not isinstance(k, str) or isinstance(v, (list, dict)) for k, v in value.items()):
            raise ValueError("attributes must contain scalar values")
        return canonical(value)

    def ingest(self, tenant, subjects, events=()):
        tenant = self._identity(tenant)
        with self.db:
            for subject in subjects:
                sid = self._identity(subject["id"])
                attrs_json = self._attributes(subject["attributes"])
                self.db.execute("INSERT INTO subjects VALUES (?,?,?) ON CONFLICT(tenant,id) DO UPDATE SET attributes=excluded.attributes",
                                (tenant, sid, attrs_json))
            for event in events:
                eid, subject, kind = map(self._identity, (event["id"], event["subject"], event["type"]))
                attrs_json = self._attributes(event.get("attributes", {}))
                row = (tenant, eid, subject, instant(event["at"]), kind, attrs_json)
                # Same tenant + event id is idempotent only when the full
                # content, including attributes, is identical.
                previous = self.db.execute(
                    "SELECT tenant,id,subject,at,kind,attributes FROM events WHERE tenant=? AND id=?",
                    (tenant, eid)).fetchone()
                if previous is not None and previous != row: raise ValueError("event id already has different content")
                self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)", row)
            self.db.execute("INSERT INTO revisions VALUES (?,1) ON CONFLICT(tenant) DO UPDATE SET revision=revision+1", (tenant,))
        return self._revision(tenant)

    def _revision(self, tenant):
        row = self.db.execute("SELECT revision FROM revisions WHERE tenant=?", (tenant,)).fetchone()
        return row[0] if row else 0

    def _evaluate(self, tenant, principal, normalized):
        if "snapshot" in normalized:
            row = self.db.execute(
                "SELECT members,revision,language FROM snapshots WHERE id=? AND tenant=? AND principal=?",
                (normalized["snapshot"], tenant, principal)).fetchone()
            if row is None: raise ValueError("snapshot is unavailable to this principal")
            members = json.loads(row[0])
            if len(members) > self.policy.max_candidates: raise ValueError("snapshot exceeds current candidate limit")
            # Frozen membership is never re-filtered through temporal logic.
            return members, row[1], row[2]
        candidates = self.db.execute("SELECT id,attributes FROM subjects WHERE tenant=? ORDER BY id LIMIT ?",
                                     (tenant, self.policy.max_candidates + 1)).fetchall()
        if len(candidates) > self.policy.max_candidates: raise ValueError("candidate scan exceeds policy")
        selected = [{"id": sid, "attributes": json.loads(attrs)} for sid, attrs in candidates
                    if matches(normalized.get("where"), json.loads(attrs))]
        seq = normalized["sequence"]
        if not seq["steps"]: return selected, self._revision(tenant), seq["language"]
        rows = self.db.execute(
            "SELECT subject,at,id,kind,attributes FROM events WHERE tenant=? AND at>=? AND at<? ORDER BY at,id LIMIT ?",
            (tenant, normalized["start"], normalized["end"], self.policy.max_events + 1)).fetchall()
        if len(rows) > self.policy.max_events: raise ValueError("event scan exceeds policy")
        by_subject = defaultdict(list)
        for position, (subject, at, eid, kind, attrs_json) in enumerate(rows):
            by_subject[subject].append((position, kind, at, json.loads(attrs_json)))
        matched = []
        for subject in selected:
            if temporal_matches(by_subject[subject["id"]], normalized, self.policy.max_states):
                matched.append(subject)
        return matched, self._revision(tenant), seq["language"]

    def query(self, tenant, principal, definition):
        tenant, principal = self._identity(tenant), self._identity(principal)
        # canonical() first makes JSON key order irrelevant to identity while
        # preserving the declared values; prepare() then normalizes semantics.
        raw = json.loads(canonical(definition))
        now = self.clock()
        if now.tzinfo is None: raise ValueError("clock must be timezone aware")
        day = now.astimezone(timezone.utc).date().isoformat()
        error, result, cached = None, None, False
        normalized, language, fingerprint = None, LEGACY_LANGUAGE_VERSION, None
        self.db.execute("BEGIN IMMEDIATE")
        try:
            used = self.db.execute("SELECT used FROM budgets WHERE tenant=? AND principal=? AND day=?",
                                   (tenant, principal, day)).fetchone()
            if used and used[0] >= self.policy.daily_queries:
                error = ValueError("daily query budget exhausted")
            else:
                self.db.execute("INSERT INTO budgets VALUES (?,?,?,1) ON CONFLICT(tenant,principal,day) DO UPDATE SET used=used+1",
                                (tenant, principal, day))
                try:
                    # Validation and normalization happen after the budget is
                    # reserved, so rejected requests are still charged.
                    normalized = prepare(raw, self.policy)
                    language = normalized.get("sequence", {}).get("language", LEGACY_LANGUAGE_VERSION)
                    fingerprint = hashlib.sha256(canonical([LANGUAGE_VERSION, normalized]).encode()).hexdigest()
                    revision = self._revision(tenant)
                    # Cache identity binds tenant, principal, full policy, data
                    # revision, language version and the normalized definition:
                    # changed event conditions or a language bump miss the cache;
                    # equivalent JSON reorderings still hit it.
                    key = hashlib.sha256(canonical([tenant, principal, self.policy.digest, revision,
                                                    language, normalized]).encode()).hexdigest()
                    hit = self.db.execute("SELECT result FROM cache WHERE key=?", (key,)).fetchone()
                    if hit:
                        result, cached = json.loads(hit[0]), True
                    else:
                        members, source_revision, source_language = self._evaluate(tenant, principal, normalized)
                        result = aggregate(members, normalized.get("group_by", []), self.policy)
                        snapshot_id = normalized.get("snapshot") or uuid.uuid4().hex
                        if "snapshot" not in normalized:
                            self.db.execute(
                                "INSERT INTO snapshots(id,tenant,principal,revision,policy,members,language) VALUES (?,?,?,?,?,?,?)",
                                (snapshot_id, tenant, principal, revision, self.policy.digest,
                                 canonical(members), source_language))
                        result.update(snapshot=snapshot_id, revision=source_revision, language=source_language)
                        self.db.execute("INSERT INTO cache VALUES (?,?)", (key, canonical(result)))
                except (ValueError, KeyError, TypeError) as exc:
                    error = ValueError(str(exc))
            if fingerprint is None:
                fingerprint = hashlib.sha256(canonical([LANGUAGE_VERSION, raw]).encode()).hexdigest()
            self.db.execute(
                "INSERT INTO audit(tenant,principal,at,fingerprint,policy,status,cached,language) VALUES (?,?,?,?,?,?,?,?)",
                (tenant, principal, now.isoformat(), fingerprint, self.policy.digest,
                 "rejected" if error else "ok", int(cached), language))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        if error: raise error
        return {**result, "cache_hit": cached}

    def close(self):
        self.db.close()
