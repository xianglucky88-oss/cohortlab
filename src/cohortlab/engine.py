import hashlib
import json
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from .policy import QueryPolicy, canonical
from .query import aggregate, instant, matches, sequence_matches, validate


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
              revision INTEGER NOT NULL, policy TEXT NOT NULL, members TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
              id INTEGER PRIMARY KEY, tenant TEXT, principal TEXT, at TEXT,
              fingerprint TEXT, policy TEXT, status TEXT, cached INTEGER);
        """)

    @staticmethod
    def _identity(value):
        if not isinstance(value, str) or not value.strip(): raise ValueError("identifiers must be nonempty strings")
        return value

    def ingest(self, tenant, subjects, events=()):
        tenant = self._identity(tenant)
        with self.db:
            for subject in subjects:
                sid = self._identity(subject["id"])
                attrs = subject["attributes"]
                if not isinstance(attrs, dict) or any(not isinstance(k, str) or isinstance(v, (list, dict)) for k, v in attrs.items()):
                    raise ValueError("attributes must contain scalar values")
                self.db.execute("INSERT INTO subjects VALUES (?,?,?) ON CONFLICT(tenant,id) DO UPDATE SET attributes=excluded.attributes",
                                (tenant, sid, canonical(attrs)))
            for event in events:
                eid, subject, kind = map(self._identity, (event["id"], event["subject"], event["type"]))
                row = (tenant, eid, subject, instant(event["at"]), kind)
                previous = self.db.execute("SELECT tenant,id,subject,at,kind FROM events WHERE tenant=? AND id=?", (tenant, eid)).fetchone()
                if previous is not None and previous != row: raise ValueError("event id already has different content")
                self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)", row)
            self.db.execute("INSERT INTO revisions VALUES (?,1) ON CONFLICT(tenant) DO UPDATE SET revision=revision+1", (tenant,))
        return self._revision(tenant)

    def _revision(self, tenant):
        row = self.db.execute("SELECT revision FROM revisions WHERE tenant=?", (tenant,)).fetchone()
        return row[0] if row else 0

    def _evaluate(self, tenant, principal, definition):
        if definition.get("snapshot"):
            row = self.db.execute("SELECT members,revision FROM snapshots WHERE id=? AND tenant=? AND principal=?",
                                  (definition["snapshot"], tenant, principal)).fetchone()
            if row is None: raise ValueError("snapshot is unavailable to this principal")
            members = json.loads(row[0])
            if len(members) > self.policy.max_candidates: raise ValueError("snapshot exceeds current candidate limit")
            return members, row[1]
        candidates = self.db.execute("SELECT id,attributes FROM subjects WHERE tenant=? ORDER BY id LIMIT ?",
                                     (tenant, self.policy.max_candidates + 1)).fetchall()
        if len(candidates) > self.policy.max_candidates: raise ValueError("candidate scan exceeds policy")
        selected = [{"id": sid, "attributes": json.loads(attrs)} for sid, attrs in candidates
                    if matches(definition.get("where"), json.loads(attrs))]
        sequence = definition.get("sequence", [])
        if not sequence: return selected, self._revision(tenant)
        rows = self.db.execute("SELECT subject,at,kind FROM events WHERE tenant=? AND at>=? AND at<? ORDER BY at,id LIMIT ?",
                               (tenant, instant(definition["start"]), instant(definition["end"]), self.policy.max_events + 1)).fetchall()
        if len(rows) > self.policy.max_events: raise ValueError("event scan exceeds policy")
        by_subject = defaultdict(list)
        for subject, at, kind in rows: by_subject[subject].append((at, kind))
        gap = definition.get("max_gap_seconds", self.policy.max_window_days * 86400) * 1000000
        return [s for s in selected if sequence_matches(by_subject[s["id"]], sequence, gap)], self._revision(tenant)

    def query(self, tenant, principal, definition):
        tenant, principal = self._identity(tenant), self._identity(principal)
        definition = json.loads(canonical(definition))
        fingerprint = hashlib.sha256(canonical(definition).encode()).hexdigest()
        now = self.clock()
        if now.tzinfo is None: raise ValueError("clock must be timezone aware")
        day = now.astimezone(timezone.utc).date().isoformat()
        error, result, cached = None, None, False
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
                    validate(definition, self.policy)
                    revision = self._revision(tenant)
                    key = hashlib.sha256(canonical([tenant, principal, self.policy.digest, revision, definition]).encode()).hexdigest()
                    hit = self.db.execute("SELECT result FROM cache WHERE key=?", (key,)).fetchone()
                    if hit:
                        result, cached = json.loads(hit[0]), True
                    else:
                        members, source_revision = self._evaluate(tenant, principal, definition)
                        result = aggregate(members, definition.get("group_by", []), self.policy)
                        snapshot_id = definition.get("snapshot") or uuid.uuid4().hex
                        if not definition.get("snapshot"):
                            self.db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                                            (snapshot_id, tenant, principal, revision, self.policy.digest, canonical(members)))
                        result.update(snapshot=snapshot_id, revision=source_revision)
                        self.db.execute("INSERT INTO cache VALUES (?,?)", (key, canonical(result)))
                except (ValueError, KeyError, TypeError) as exc:
                    error = ValueError(str(exc))
            self.db.execute("INSERT INTO audit(tenant,principal,at,fingerprint,policy,status,cached) VALUES (?,?,?,?,?,?,?)",
                            (tenant, principal, now.isoformat(), fingerprint, self.policy.digest,
                             "rejected" if error else "ok", int(cached)))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        if error: raise error
        return {**result, "cache_hit": cached}

    def close(self):
        self.db.close()
