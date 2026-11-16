import json
import random
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cohortlab import CohortEngine, QueryPolicy
from cohortlab.query import (LANGUAGE_VERSION, LEGACY_LANGUAGE_VERSION,
                             instant, prepare, temporal_matches)
from cohortlab.reference import reference_matches, witnesses

# Event attribute fields used by these tests; extends the policy whitelist.
TEST_POLICY = QueryPolicy(event_filter_fields=("channel", "status", "method", "ok", "n"))

START = "2026-11-01T00:00:00Z"
WINDOW = {"start": START, "end": "2026-11-03T00:00:00Z"}


def ev(eid, subject, kind, at, **attributes):
    return {"id": eid, "subject": subject, "type": kind,
            "at": f"2026-11-01T00:00:{at:02d}Z", "attributes": dict(attributes)}


def seq(steps, negatives=(), gap=3600, total=None):
    body = {"steps": list(steps), "not": list(negatives)}
    query = {**WINDOW, "sequence": body, "max_gap_seconds": gap}
    if total is not None: query["max_total_seconds"] = total
    return query


def step(kind, optional=False, where=None):
    node = {"type": kind, "optional": optional}
    if where is not None: node["where"] = where
    return node


def neg(kind, where=None):
    return {"type": kind, "where": where}


def tuples(raw):
    """Build ordered (index, type, at, attrs) tuples from (at, type, attrs, id)."""
    ordered = sorted(raw, key=lambda r: (r[0], r[3]))
    return [(i, t, at, attrs) for i, (at, t, attrs, _eid) in enumerate(ordered)]


def ast(steps, negatives=(), gap=1000, total=None):
    return prepare(seq(steps, negatives, gap=gap, total=total), TEST_POLICY)


def micro_ast(steps, negatives=(), gap_us=1000, total_us=None):
    """Build a normalized AST directly with microsecond-scale limits, matching
    the integer timestamps produced by the tuples() helper."""
    normalized = prepare(seq(steps, negatives, gap=3600), TEST_POLICY)
    normalized["sequence"]["max_gap_us"] = gap_us
    normalized["sequence"]["max_total_us"] = total_us
    return normalized


class NormalizationTests(unittest.TestCase):
    def setUp(self):
        self.policy = TEST_POLICY

    def test_language_versions_and_canonical_identity(self):
        # A bare list of event types is the legacy v1 form.
        legacy = {**WINDOW, "sequence": ["signup", "activate"], "max_gap_seconds": 3600}
        self.assertEqual(prepare(legacy, self.policy)["sequence"]["language"], LEGACY_LANGUAGE_VERSION)
        v2 = seq([step("signup"), step("activate", optional=True), step("activate")])
        self.assertEqual(prepare(v2, self.policy)["sequence"]["language"], LANGUAGE_VERSION)
        # JSON key reordering normalizes to the same AST.
        reordered = json.loads(json.dumps(v2))
        reordered["sequence"] = {"not": [], "steps": reordered["sequence"]["steps"]}
        self.assertEqual(prepare(v2, self.policy), prepare(reordered, self.policy))

    def test_all_optional_and_unbounded_negatives_are_rejected(self):
        for bad in (
            seq([step("signup", optional=True), step("activate", optional=True)]),
            seq([step("signup")], [neg("refund")]),
        ):
            with self.assertRaisesRegex(ValueError, "negative|required"):
                prepare(bad, self.policy)

    def test_event_predicate_field_whitelist_and_bounds(self):
        good = seq([step("signup", where={"field": "channel", "op": "eq", "value": "web"}),
                    step("activate")], [neg("refund", where={"field": "status", "op": "eq", "value": "ok"})])
        prepare(good, self.policy)
        bad_field = seq([step("signup", where={"field": "region", "op": "eq", "value": "x"}),
                         step("activate")])
        with self.assertRaises(ValueError): prepare(bad_field, self.policy)
        leaf = {"field": "channel", "op": "eq", "value": "x"}
        deep = leaf
        for _ in range(12):
            deep = {"all": [deep]}
        with self.assertRaises(ValueError):
            prepare(seq([step("signup", where=deep), step("activate")]), self.policy)
        with self.assertRaises(ValueError):
            prepare(seq([step("signup"), step("activate")], total=0), self.policy)
        with self.assertRaises(ValueError):
            prepare({**seq([step("signup"), step("activate")]), "max_total_seconds": 1.5}, self.policy)

    def test_unknown_fields_and_negative_shape_rejected(self):
        with self.assertRaises(ValueError):
            prepare({**seq([step("signup"), step("activate")]), "sql": "SELECT 1"}, self.policy)
        with self.assertRaises(ValueError):
            prepare(seq([step("signup"), step("activate")],
                        [{"type": "refund", "where": None, "extra": 1}]), self.policy)


class ExecutorSemanticsTests(unittest.TestCase):
    def test_alternative_start_path_is_retained(self):
        # Registration at second 0 and 90, activation at 100, total cap 30s:
        # only the second registration can anchor a valid path.
        events = tuples([(0, "signup", {}, "e1"), (90, "signup", {}, "e2"), (100, "activate", {}, "e3")])
        self.assertTrue(temporal_matches(
            events, micro_ast([step("signup"), step("activate")], total_us=30, gap_us=30), 100))
        # A refund at 95 sits in the open interval of the only viable path.
        blocked = tuples([(0, "signup", {}, "e1"), (90, "signup", {}, "e2"),
                          (95, "refund", {}, "e3"), (100, "activate", {}, "e4")])
        self.assertFalse(temporal_matches(
            blocked, micro_ast([step("signup"), step("activate")], [neg("refund")], total_us=30, gap_us=30), 100))

    def test_same_instant_uses_event_id_order_for_negatives(self):
        # Same instant as the right boundary. The refund id sorts BEFORE the
        # activation id, so in total (at, id) order it lies inside the open
        # interval (signup, activation) and must invalidate the path.
        inside = tuples([(10, "signup", {}, "a1"),
                         (20, "refund", {}, "a2"),
                         (20, "activate", {}, "a3")])
        self.assertFalse(temporal_matches(
            inside, micro_ast([step("signup"), step("activate")], [neg("refund")]), 100))
        # Refund id sorts AFTER the activation id: it is beyond the right
        # boundary, so the already-closed interval stays valid.
        outside = tuples([(10, "signup", {}, "b1"),
                          (20, "activate", {}, "b2"),
                          (20, "refund", {}, "b3")])
        self.assertTrue(temporal_matches(
            outside, micro_ast([step("signup"), step("activate")], [neg("refund")]), 100))

    def test_negative_interval_requires_both_boundaries(self):
        # A negative event before any consumed positive event never poisons;
        # there is no left boundary, and validation already forbids single-step
        # negatives, so here it simply precedes the first consumed event.
        events = tuples([(0, "refund", {}, "e0"), (5, "signup", {}, "e1"), (10, "activate", {}, "e2")])
        self.assertTrue(temporal_matches(
            events, micro_ast([step("signup"), step("activate")], [neg("refund")]), 100))

    def test_negative_typed_event_can_close_an_interval(self):
        # The refund is itself the required second step's matching event type
        # only when predicates distinguish roles; here refund also appears as a
        # positive type and must be allowed to close its own interval.
        events = tuples([(0, "signup", {}, "e1"), (10, "refund", {"ok": True}, "e2"),
                         (20, "activate", {}, "e3")])
        definition = seq([step("signup"), step("refund", where={"field": "ok", "op": "eq", "value": True}),
                          step("activate")], [neg("refund", where={"field": "ok", "op": "eq", "value": False})])
        self.assertTrue(temporal_matches(events, prepare(definition, TEST_POLICY), 100))

    def test_optional_step_skip_invents_nothing_and_keeps_timing(self):
        # Skip verify: signup@0 -> activate@60 must still obey the 30s gap.
        events = tuples([(0, "signup", {}, "e1"), (60, "activate", {}, "e2")])
        self.assertFalse(temporal_matches(
            events, micro_ast([step("signup"), step("verify", optional=True), step("activate")], gap_us=30), 100))
        tight = tuples([(0, "signup", {}, "e1"), (20, "activate", {}, "e2")])
        self.assertTrue(temporal_matches(
            tight, micro_ast([step("signup"), step("verify", optional=True), step("activate")], gap_us=30), 100))
        # Consuming the optional step is also a valid alternative path.
        with_verify = tuples([(0, "signup", {}, "e1"), (10, "verify", {}, "e2"), (20, "activate", {}, "e3")])
        self.assertTrue(temporal_matches(
            with_verify, micro_ast([step("signup"), step("verify", optional=True), step("activate")], gap_us=30), 100))

    def test_no_event_is_consumed_twice(self):
        # A single signup cannot satisfy two required signup steps.
        events = tuples([(0, "signup", {}, "e1")])
        self.assertFalse(temporal_matches(
            events, micro_ast([step("signup"), step("signup")], gap_us=30), 100))
        # Two same-instant signups ordered by id can.
        pair = tuples([(0, "signup", {}, "e1"), (0, "signup", {}, "e2")])
        self.assertTrue(temporal_matches(
            pair, micro_ast([step("signup"), step("signup")], gap_us=30), 100))

    def test_total_duration_spans_first_to_last_consumed_event(self):
        events = tuples([(0, "signup", {}, "e1"), (40, "activate", {}, "e2")])
        self.assertFalse(temporal_matches(
            events, micro_ast([step("signup"), step("activate")], gap_us=100, total_us=30), 100))
        self.assertTrue(temporal_matches(
            events, micro_ast([step("signup"), step("activate")], gap_us=100, total_us=40), 100))
        # A single consumed required event (leading optional skipped) has span zero.
        single = tuples([(0, "verify", {}, "e0"), (50, "activate", {}, "e1")])
        self.assertTrue(temporal_matches(
            single, micro_ast([step("verify", optional=True), step("activate")], gap_us=100, total_us=10), 100))

    def test_attribute_predicates_filter_positive_and_negative_events(self):
        events = tuples([(0, "signup", {"channel": "web"}, "e1"),
                         (10, "refund", {"status": "pending"}, "e2"),
                         (20, "activate", {}, "e3")])
        definition = seq([step("signup", where={"field": "channel", "op": "eq", "value": "web"}),
                          step("activate")],
                         [neg("refund", where={"field": "status", "op": "eq", "value": "ok"})])
        self.assertTrue(temporal_matches(events, prepare(definition, TEST_POLICY), 100))
        ok_refund = tuples([(0, "signup", {"channel": "web"}, "e1"),
                            (10, "refund", {"status": "ok"}, "e2"),
                            (20, "activate", {}, "e3")])
        self.assertFalse(temporal_matches(ok_refund, prepare(definition, TEST_POLICY), 100))

    def test_state_limit_rejects_without_silently_dropping_paths(self):
        events = [(i, "a" if i % 2 == 0 else "b", i, {}) for i in range(20)]
        definition = micro_ast([step("a"), step("b"), step("c")], gap_us=10**9)
        with self.assertRaisesRegex(ValueError, "state limit"):
            temporal_matches(events, definition, 1)
        # A generous cap computes the same answer as the exhaustive oracle.
        self.assertEqual(temporal_matches(events, definition, 10000),
                         reference_matches(events, definition))


class ExhaustiveOraclePropertyTests(unittest.TestCase):
    KINDS = ("a", "b", "c", "x")

    def _predicate(self, rng):
        choice = rng.random()
        if choice < 0.4: return None
        if choice < 0.75:
            return {"field": "channel", "op": "eq", "value": rng.choice(("w", "m"))}
        return {"all": [{"field": "channel", "op": "in", "value": ["w", "m"]},
                        {"field": "n", "op": "gte", "value": 1}]}

    def test_executor_matches_exhaustive_oracle_on_small_streams(self):
        rng = random.Random(20261116)
        for _ in range(3000):
            size = rng.randint(1, 6)
            raw = [(rng.randint(0, 4) * 10, f"e{i}", rng.choice(self.KINDS),
                    {"channel": rng.choice(("w", "m")), "n": rng.randint(0, 3)})
                   for i in range(size)]
            raw.sort(key=lambda r: (r[0], r[1]))
            events = [(i, kind, at, attrs) for i, (at, _id, kind, attrs) in enumerate(raw)]
            step_count = rng.randint(1, 3)
            steps = [step(rng.choice(self.KINDS[:3]),
                          optional=rng.random() < 0.35, where=self._predicate(rng))
                     for _ in range(step_count)]
            if not any(not s["optional"] for s in steps):
                steps[rng.randrange(step_count)]["optional"] = False
            negatives = []
            if rng.random() < 0.6 and sum(not s["optional"] for s in steps) >= 2:
                negatives = [neg("x", self._predicate(rng))]
            gap = rng.choice((5, 10, 20, 40, 100))
            total = rng.choice((None, 10, 20, 40, 100))
            definition = micro_ast(steps, negatives, gap_us=gap,
                                   total_us=None if total is None else total)
            self.assertEqual(temporal_matches(events, definition, 1000),
                             reference_matches(events, definition),
                             (events, definition))

    def test_oracle_witnesses_respect_open_intervals_and_no_reuse(self):
        events = tuples([(0, "a", {}, "e1"), (5, "x", {}, "e2"), (10, "b", {}, "e3")])
        definition = micro_ast([step("a"), step("b")], [neg("x")])
        self.assertEqual(witnesses(events, definition), [])
        clean = tuples([(0, "a", {}, "e1"), (10, "b", {}, "e3")])
        self.assertEqual([w[0] for w in witnesses(clean, micro_ast([step("a"), step("b")]))], [0])


class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cohort.sqlite"
        self.now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        # Two-subject cohort: lower the disclosure threshold so totals are visible.
        self.engine = CohortEngine(self.path,
                                   policy=replace(QueryPolicy(), k=2),
                                   clock=lambda: self.now)
        self.engine.ingest("a", [
            {"id": "u1", "attributes": {"region": "east", "plan": "pro"}},
            {"id": "u2", "attributes": {"region": "east", "plan": "pro"}},
        ], [
            ev("s1", "u1", "signup", 0, channel="web"),
            ev("v1", "u1", "verify", 10, status="ok"),
            ev("a1", "u1", "activate", 20, method="email"),
            ev("s2", "u2", "signup", 0, channel="app"),
            ev("a2", "u2", "activate", 15, method="email"),
        ])
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.engine.close)
        self.definition = seq(
            [step("signup", where={"field": "channel", "op": "in", "value": ["web", "app"]}),
             step("verify", optional=True, where={"field": "status", "op": "eq", "value": "ok"}),
             step("activate", where={"field": "method", "op": "eq", "value": "email"})],
            [neg("refund", where={"field": "status", "op": "eq", "value": "ok"})],
            gap=3600, total=3600)

    def test_v2_query_cache_language_and_audit(self):
        first = self.engine.query("a", "analyst", self.definition)
        self.assertEqual(first["language"], LANGUAGE_VERSION)
        second = self.engine.query("a", "analyst", self.definition)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["snapshot"], second["snapshot"])
        row = self.engine.db.execute("SELECT language, status, cached FROM audit ORDER BY id").fetchall()
        self.assertEqual(row, [(LANGUAGE_VERSION, "ok", 0), (LANGUAGE_VERSION, "ok", 1)])

    def test_json_key_reorder_keeps_cache_but_condition_change_misses(self):
        self.engine.query("a", "analyst", self.definition)
        reordered = json.loads(json.dumps(self.definition))
        self.assertTrue(self.engine.query("a", "analyst", reordered)["cache_hit"])
        changed = json.loads(json.dumps(self.definition))
        changed["sequence"]["steps"][0]["where"]["value"] = ["web"]
        self.assertFalse(self.engine.query("a", "analyst", changed)["cache_hit"])

    def test_ingested_attribute_change_invalidates_cache_and_survives_restart(self):
        first = self.engine.query("a", "analyst", self.definition)
        self.assertEqual(first["total"], 2)
        # Re-ingesting the same event id with different attributes is a conflict,
        # including when only the attributes differ.
        with self.assertRaises(ValueError):
            self.engine.ingest("a", [], [ev("s2", "u2", "signup", 0, channel="branch")])
        # Any new ingestion bumps the data revision, so the cache misses; the
        # recomputed result is then cached under the new revision.
        self.engine.ingest("a", [], [ev("n1", "u1", "note", 25, status="ok")])
        refreshed = self.engine.query("a", "analyst", self.definition)
        self.assertFalse(refreshed["cache_hit"])
        # Persistent cache and language survive reopening the SQLite database;
        # the same policy must be supplied because its digest is cache identity.
        reopened = CohortEngine(self.path,
                                policy=replace(QueryPolicy(), k=2),
                                clock=lambda: self.now)
        try:
            self.assertTrue(reopened.query("a", "analyst", self.definition)["cache_hit"])
        finally:
            reopened.close()

    def test_legacy_database_migrates_with_empty_event_attributes(self):
        legacy = Path(self.tmp.name) / "legacy.sqlite"
        connection = sqlite3.connect(legacy)
        connection.executescript("""
            CREATE TABLE subjects (tenant TEXT, id TEXT, attributes TEXT, PRIMARY KEY(tenant,id));
            CREATE TABLE events (tenant TEXT, id TEXT, subject TEXT, at INTEGER, kind TEXT,
                                 PRIMARY KEY(tenant,id));
            CREATE TABLE revisions (tenant TEXT PRIMARY KEY, revision INTEGER);
            CREATE TABLE budgets (tenant TEXT, principal TEXT, day TEXT, used INTEGER, PRIMARY KEY(tenant,principal,day));
            CREATE TABLE snapshots (id TEXT PRIMARY KEY, tenant TEXT, principal TEXT, revision INTEGER,
                                    policy TEXT, members TEXT);
            CREATE TABLE cache (key TEXT PRIMARY KEY, result TEXT);
            CREATE TABLE audit (id INTEGER PRIMARY KEY, tenant TEXT, principal TEXT, at TEXT,
                                fingerprint TEXT, policy TEXT, status TEXT, cached INTEGER);
        """)
        connection.execute("INSERT INTO subjects VALUES ('a','u1','{}')")
        connection.execute("INSERT INTO events VALUES ('a','e1','u1',?,'signup')", (instant(START),))
        connection.execute("INSERT INTO revisions VALUES ('a',1)")
        connection.commit()
        connection.close()
        engine = CohortEngine(legacy, policy=replace(QueryPolicy(), k=2))
        try:
            # A bare list sequence is legacy v1 and works on the migrated rows.
            legacy_query = {**WINDOW, "sequence": ["signup"], "max_gap_seconds": 3600}
            result = engine.query("a", "analyst", legacy_query)
            self.assertEqual(result["language"], LEGACY_LANGUAGE_VERSION)
            # One matching subject, but a single-member cell is suppressed under
            # k=2, so the count is withheld while the snapshot still holds it.
            self.assertTrue(all(cell["suppressed"] for cell in result["cells"]))
            members = json.loads(engine.db.execute(
                "SELECT members FROM snapshots WHERE id=?", (result["snapshot"],)).fetchone()[0])
            self.assertEqual([m["id"] for m in members], ["u1"])
            # An attribute predicate on a legacy (empty-attribute) event never matches.
            filtered = engine.query("a", "analyst",
                                    seq([step("signup", where={"field": "channel", "op": "eq", "value": "web"})]))
            self.assertIsNone(filtered["total"])
            self.assertEqual(filtered["language"], LANGUAGE_VERSION)
        finally:
            engine.close()

    def test_frozen_snapshot_skips_temporal_filtering_and_keeps_language(self):
        first = self.engine.query("a", "analyst", self.definition)
        # New refund would disqualify u1 if temporal logic were re-run.
        self.engine.ingest("a", [], [ev("r1", "u1", "refund", 18, status="ok")])
        frozen = self.engine.query("a", "analyst",
                                   {"snapshot": first["snapshot"], "group_by": []})
        self.assertEqual(frozen["total"], 2)
        self.assertEqual(frozen["language"], LANGUAGE_VERSION)
        self.assertEqual(frozen["revision"], first["revision"])
        with self.assertRaises(ValueError):
            self.engine.query("a", "analyst",
                              {"snapshot": first["snapshot"], "sequence": {"steps": [step("signup")]}})

    def test_rejected_requests_are_charged_and_tenant_isolated(self):
        bad = seq([step("signup", where={"field": "secret", "op": "eq", "value": 1}),
                   step("activate")])
        with self.assertRaises(ValueError): self.engine.query("a", "analyst", bad)
        with self.assertRaises(ValueError): self.engine.query("a", "analyst", bad)
        self.assertEqual(self.engine.db.execute("SELECT used FROM budgets").fetchone()[0], 2)
        self.assertEqual(self.engine.db.execute("SELECT status FROM audit").fetchall(),
                         [("rejected",), ("rejected",)])
        other = self.engine.query("b", "analyst", self.definition)
        self.assertIsNone(other["total"])

    def test_policy_state_limit_is_enforced_end_to_end(self):
        self.engine.policy = replace(self.engine.policy, max_states=1)
        # u1 alone has an incomparable verify-consumed vs verify-skipped frontier.
        with self.assertRaisesRegex(ValueError, "state limit"):
            self.engine.query("a", "analyst", self.definition)
        self.assertEqual(self.engine.db.execute("SELECT status FROM audit").fetchone()[0], "rejected")


if __name__ == "__main__":
    unittest.main()
