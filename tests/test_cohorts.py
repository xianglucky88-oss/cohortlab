import copy
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cohortlab import CohortEngine, QueryPolicy
from cohortlab.query import sequence_matches


class CohortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cohort.sqlite"
        self.now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        self.engine = CohortEngine(self.path, clock=lambda: self.now)
        fixtures = Path(__file__).resolve().parents[1] / "examples"
        self.population = json.loads((fixtures / "population.json").read_text())
        self.query = json.loads((fixtures / "query.json").read_text())
        self.engine.ingest("a", self.population["subjects"], self.population["events"])
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.engine.close)

    def test_filter_sequence_and_cached_result(self):
        first = self.engine.query("a", "alice", self.query)
        second = self.engine.query("a", "alice", self.query)
        self.assertEqual(first["total"], 3)
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["snapshot"], second["snapshot"])

    def test_cache_isolation_by_principal_policy_and_revision(self):
        first = self.engine.query("a", "alice", self.query)
        other = self.engine.query("a", "bob", self.query)
        self.assertNotEqual(first["snapshot"], other["snapshot"])
        self.engine.policy = replace(self.engine.policy, version="v2", k=4)
        self.assertIsNone(self.engine.query("a", "alice", self.query)["total"])
        self.engine.ingest("a", self.population["subjects"])
        self.assertFalse(self.engine.query("a", "alice", self.query)["cache_hit"])

    def test_tenant_isolation(self):
        result = self.engine.query("empty", "alice", self.query)
        self.assertIsNone(result["total"])
        self.assertTrue(all(cell["count"] is None for cell in result["cells"]))
        self.assertEqual(self.engine.query("a", "alice", self.query)["total"], 3)

    def test_complementary_suppression_and_total_withheld(self):
        query = {k: v for k, v in self.query.items() if k not in ("sequence", "where", "max_gap_seconds")}
        result = self.engine.query("a", "alice", query)
        self.assertEqual(len(result["cells"]), 2)
        self.assertTrue(all(c["suppressed"] for c in result["cells"]))
        self.assertIsNone(result["total"])

    def test_daily_budget_counts_cache_and_rejections(self):
        self.engine.policy = replace(self.engine.policy, daily_queries=2)
        self.engine.query("a", "alice", self.query)
        self.engine.query("a", "alice", self.query)
        with self.assertRaisesRegex(ValueError, "budget"): self.engine.query("a", "alice", self.query)
        self.now += timedelta(days=1)
        self.assertTrue(self.engine.query("a", "alice", self.query)["cache_hit"])
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM audit").fetchone()[0], 4)

    def test_snapshot_repeats_membership_after_updates(self):
        first = self.engine.query("a", "alice", self.query)
        updated = copy.deepcopy(self.population["subjects"])
        for row in updated: row["attributes"]["plan"] = "free"
        self.engine.ingest("a", updated)
        self.assertIsNone(self.engine.query("a", "alice", self.query)["total"])
        frozen = self.engine.query("a", "alice", {"snapshot": first["snapshot"], "group_by": ["region"]})
        self.assertEqual(frozen["total"], 3)
        self.assertEqual(frozen["revision"], first["revision"])
        with self.assertRaises(ValueError):
            self.engine.query("a", "bob", {"snapshot": first["snapshot"]})

    def test_sequence_alternative_starts_and_no_event_reuse(self):
        self.assertTrue(sequence_matches([(0, "a"), (100, "a"), (105, "b")], ["a", "b"], 10))
        self.assertFalse(sequence_matches([(0, "a")], ["a", "a"], 10))
        self.assertTrue(sequence_matches([(0, "a"), (0, "a")], ["a", "a"], 10))
        self.assertFalse(sequence_matches([(0, "b"), (1, "a")], ["a", "b"], 10))

    def test_sequence_against_exhaustive_oracle(self):
        from itertools import combinations, product
        for types in product("ab", repeat=5):
            events = [(i * 2, t) for i, t in enumerate(types)]
            for sequence in (["a", "b"], ["a", "a", "b"], ["b", "a", "b"]):
                for gap in (1, 2, 4, 10):
                    expected = any([events[i][1] for i in indices] == sequence and
                                   all(events[b][0] - events[a][0] <= gap for a, b in zip(indices, indices[1:]))
                                   for indices in combinations(range(5), len(sequence)))
                    self.assertEqual(sequence_matches(events, sequence, gap), expected, (events, sequence, gap))

    def test_scan_limits_and_rejected_query_audit(self):
        self.engine.policy = replace(self.engine.policy, max_candidates=2)
        with self.assertRaisesRegex(ValueError, "scan"): self.engine.query("a", "alice", self.query)
        self.assertEqual(self.engine.db.execute("SELECT status FROM audit").fetchone()[0], "rejected")
        self.engine.policy = replace(self.engine.policy, max_candidates=20, max_events=2)
        with self.assertRaisesRegex(ValueError, "event scan"): self.engine.query("a", "alice", self.query)

    def test_unsafe_queries_are_rejected_and_charged(self):
        for change in ({"group_by": ["id"]}, {"sql": "DROP TABLE subjects"}, {"sequence": ["a"] * 20}):
            with self.subTest(change=change):
                query = {**self.query, **change}
                with self.assertRaises(ValueError): self.engine.query("a", "alice", query)
        self.assertEqual(self.engine.db.execute("SELECT used FROM budgets").fetchone()[0], 3)

    def test_bad_ingestion_rolls_back_subjects_and_revision(self):
        bad_event = {"id": "bad", "subject": "missing", "type": "x", "at": "2026-11-01T00:00:00Z"}
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.ingest("a", [{"id": "new", "attributes": {}}], [bad_event])
        self.assertIsNone(self.engine.db.execute("SELECT id FROM subjects WHERE id='new'").fetchone())
        self.assertEqual(self.engine._revision("a"), 1)

    def test_event_idempotency_and_conflicts(self):
        self.engine.ingest("a", [], self.population["events"])
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM events").fetchone()[0], 10)
        event = {**self.population["events"][0], "type": "changed"}
        with self.assertRaises(ValueError): self.engine.ingest("a", [], [event])
        # A same-id event whose only difference is an attribute value also conflicts.
        attr_conflict = {**self.population["events"][0],
                         "attributes": {**self.population["events"][0]["attributes"], "channel": "branch"}}
        with self.assertRaises(ValueError): self.engine.ingest("a", [], [attr_conflict])
        # Re-ingesting byte-identical events stays idempotent, attributes included.
        self.engine.ingest("a", [], self.population["events"])
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM events").fetchone()[0], 10)

    def test_group_and_window_limits(self):
        self.engine.policy = replace(self.engine.policy, max_groups=1)
        query = {k: v for k, v in self.query.items() if k not in ("where", "sequence", "max_gap_seconds")}
        with self.assertRaises(ValueError): self.engine.query("a", "alice", query)
        query = {**self.query, "start": "2020-01-01T00:00:00Z"}
        with self.assertRaises(ValueError): self.engine.query("a", "alice", query)


if __name__ == "__main__": unittest.main()
