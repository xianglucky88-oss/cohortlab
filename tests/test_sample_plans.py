"""Tests for the internal reproducible quota sample-plan capability.

Covers normalization semantics, the contention diagnosis, enumeration/flow
agreement against brute force, deterministic restarts, frozen snapshot
immunity, capacity refusal and the read-only trusted-job boundary.
"""

import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from itertools import combinations
from pathlib import Path

from cohortlab import (
    CohortEngine,
    PlanCapacityError,
    PlanInfeasible,
    PlanRequestError,
    QueryPolicy,
    build_plan,
)
from cohortlab.planning.planner import SOLVER_PROTOCOL_VERSION, stable_subject_cost
from cohortlab.planning.quota import MISSING_CATEGORY, normalize_request, precheck
from cohortlab.planning.solver import _combination_count

FLOW_POLICY = replace(QueryPolicy(), enumeration_limit=0)
REGIONS = ("east", "west", "north")
AGES = ("young", "mid", "old")


def member(sid, region, age):
    return {"id": sid, "attributes": {"region": region, "age_band": age}}


CONTENTION = [
    member("s1", "east", "young"),
    member("s2", "east", "young"),
    member("s3", "west", "old"),
]


class NormalizationTests(unittest.TestCase):
    def test_n_zero_negative_and_non_integer_are_rejected(self):
        for bad_n in (0, -1, "2", 1.5, True):
            with self.subTest(bad_n=bad_n):
                with self.assertRaises(PlanRequestError):
                    build_plan(
                        {"n": bad_n, "seed": "x",
                         "region": {"east": {}}, "age_band": {"young": {}}},
                        CONTENTION, QueryPolicy())

    def test_empty_population_is_infeasible_not_request_error(self):
        with self.assertRaises(PlanInfeasible) as ctx:
            build_plan({"n": 1, "seed": "x", "region": {"east": {}},
                        "age_band": {"young": {}}}, [], QueryPolicy())
        self.assertEqual(ctx.exception.evidence["reason"], "empty_population")
        self.assertNotIn("selected_ids", ctx.exception.evidence)

    def test_duplicate_subject_ids_reject_the_plan(self):
        dup = [member("s1", "east", "young"), member("s1", "west", "old")]
        with self.assertRaisesRegex(PlanRequestError, "duplicate subject id"):
            build_plan({"n": 1, "seed": "x", "region": {"east": {}, "west": {}},
                        "age_band": {"young": {}, "old": {}}}, dup, QueryPolicy())

    def test_contradictory_bounds_and_bound_above_n_rejected(self):
        req_bad = {"n": 2, "seed": "x",
                   "region": {"east": {"min": 2, "max": 1}},
                   "age_band": {"young": {}}}
        with self.assertRaisesRegex(PlanRequestError, "lower bound above"):
            build_plan(req_bad, CONTENTION, QueryPolicy())
        req_hi = {"n": 1, "seed": "x",
                  "region": {"east": {"min": 0, "max": 2}},
                  "age_band": {"young": {}}}
        with self.assertRaisesRegex(PlanRequestError, "upper bound exceeds n"):
            build_plan(req_hi, CONTENTION, QueryPolicy())

    def test_unknown_quota_category_rejects_whole_plan(self):
        with self.assertRaisesRegex(PlanRequestError, "absent from frozen"):
            build_plan({"n": 1, "seed": "x", "region": {"antarctica": {}},
                        "age_band": {"young": {}}}, CONTENTION, QueryPolicy())

    def test_missing_attribute_becomes_explicit_category(self):
        people = [
            {"id": "a", "attributes": {"age_band": "young"}},
            member("b", "east", "old"),
        ]
        request = {"n": 1, "seed": "x",
                   "region": {MISSING_CATEGORY: {"min": 1, "max": 1}, "east": {}},
                   "age_band": {"young": {}, "old": {}}}
        plan = build_plan(request, people, QueryPolicy())
        self.assertEqual(plan["selected_ids"], ["a"])
        # Diagnostics expose the missing category as JSON null, never ids.
        normalized = normalize_request(request, people,
                                       QueryPolicy().plan_dimensions,
                                       SOLVER_PROTOCOL_VERSION)
        from cohortlab.planning.quota import inventory
        region_inv = {row["category"]: row for row in inventory(normalized)["region"]}
        self.assertIn(None, region_inv)
        self.assertEqual(region_inv[None]["available"], 1)
        self.assertEqual(region_inv[None]["min"], 1)
        # Diagnostics carry only aggregate categories, no subject id field.
        self.assertNotIn("subjects", inventory(normalized))
        self.assertNotIn("id", json.dumps(inventory(normalized)))

    def test_legacy_missing_marker_is_reserved_and_rejected(self):
        people = [{"id": "a", "attributes": {"region": "__missing__", "age_band": "young"}}]
        with self.assertRaises(PlanRequestError):
            build_plan({"n": 1, "seed": "x", "region": {"east": {}},
                        "age_band": {"young": {}}}, people, QueryPolicy())

    def test_multi_category_string_is_one_opaque_label(self):
        people = [member("a", "east,west", "young")]
        plan = build_plan(
            {"n": 1, "seed": "x", "region": {"east,west": {"min": 1}},
             "age_band": {"young": {}}}, people, QueryPolicy())
        self.assertEqual(plan["selected_ids"], ["a"])
        # Declaring the split labels must fail: they are not real categories.
        with self.assertRaises(PlanRequestError):
            build_plan({"n": 1, "seed": "x", "region": {"east": {}, "west": {}},
                        "age_band": {"young": {}}}, people, QueryPolicy())

    def test_numeric_and_string_labels_are_distinct_categories(self):
        people = [
            {"id": "a", "attributes": {"region": 1, "age_band": "young"}},
            {"id": "b", "attributes": {"region": "1", "age_band": "young"}},
        ]
        plan = build_plan(
            {"n": 1, "seed": "x", "region": {1: {"min": 1}},
             "age_band": {"young": {}}}, people, QueryPolicy())
        self.assertEqual(plan["selected_ids"], ["a"])

    def test_boolean_and_structured_attribute_values_rejected(self):
        for bad_value in (True, ["east"], {"k": "v"}):
            people = [{"id": "a", "attributes": {"region": bad_value, "age_band": "young"}}]
            with self.subTest(bad_value=bad_value):
                with self.assertRaises(PlanRequestError):
                    build_plan({"n": 1, "seed": "x", "region": {"east": {}},
                                "age_band": {"young": {}}}, people, QueryPolicy())

    def test_unknown_request_fields_rejected_no_events_or_filters(self):
        for extra in ({"events": []}, {"where": {}}, {"sequence": ["x"]},
                      {"start": "2026-01-01T00:00:00Z"}, {"sql": "SELECT 1"}):
            with self.subTest(extra=extra):
                with self.assertRaises(PlanRequestError):
                    build_plan({"n": 1, "seed": "x", "region": {"east": {}},
                                "age_band": {"young": {}}, **extra},
                               CONTENTION, QueryPolicy())

    def test_seed_must_be_nonempty_string(self):
        for bad_seed in ("", None, 42):
            with self.subTest(bad_seed=bad_seed):
                with self.assertRaises(PlanRequestError):
                    build_plan({"n": 1, "seed": bad_seed, "region": {"east": {}},
                                "age_band": {"young": {}}}, CONTENTION, QueryPolicy())

    def test_list_style_bounds_and_defaults(self):
        plan = build_plan(
            {"n": 2, "seed": "x", "region": {"east": [2, 2]},
             "age_band": {"young": [0, 2]}}, CONTENTION, QueryPolicy())
        self.assertEqual(len(plan["selected_ids"]), 2)
        with self.assertRaises(PlanRequestError):
            build_plan({"n": 2, "seed": "x", "region": {"east": [1]},
                        "age_band": {"young": {}}}, CONTENTION, QueryPolicy())


class FeasibilityDiagnosisTests(unittest.TestCase):
    def test_canonical_contention_returns_evidence_via_both_solvers(self):
        req = {"n": 2, "seed": "run-42",
               "region": {"east": {"min": 2, "max": 2}},
               "age_band": {"old": {"min": 1}}}
        for policy, solver_name in ((QueryPolicy(), "enumeration"),
                                    (FLOW_POLICY, "min_cost_flow")):
            with self.subTest(solver=solver_name):
                with self.assertRaises(PlanInfeasible) as ctx:
                    build_plan(req, CONTENTION, policy)
                evidence = ctx.exception.evidence
                self.assertEqual(evidence["reason"], "joint_quota_contention")
                self.assertIn("inventory", evidence)
                # Diagnostics stay aggregate: no subject identifier leaks.
                blob = json.dumps(evidence)
                for sid in ("s1", "s2", "s3"):
                    self.assertNotIn(sid, blob)

    def test_flow_cut_certificate_is_structured_and_limited(self):
        req = {"n": 2, "seed": "run-42",
               "region": {"east": {"min": 2, "max": 2}},
               "age_band": {"old": {"min": 1}}}
        with self.assertRaises(PlanInfeasible) as ctx:
            build_plan(req, CONTENTION, FLOW_POLICY)
        evidence = ctx.exception.evidence
        self.assertLess(evidence["routed"], evidence["forced_supply"])
        self.assertEqual(evidence["cut_capacity"], 0)
        self.assertIn("reachable_side", evidence)

    def test_univariate_shortfall_diagnosis(self):
        # min=3 with default max=n=2 is contradictory, so give an explicit max.
        with self.assertRaises(PlanInfeasible) as ctx:
            build_plan({"n": 3, "seed": "x",
                        "region": {"east": {"min": 3, "max": 3}},
                        "age_band": {"young": {}, "old": {}}},
                       CONTENTION + [member("s4", "west", "old")], QueryPolicy())
        self.assertEqual(ctx.exception.evidence["reason"], "category_inventory_shortfall")
        self.assertEqual(ctx.exception.evidence["required"], 3)
        self.assertEqual(ctx.exception.evidence["available"], 2)

    def test_lower_sum_above_n_and_upper_sum_below_n(self):
        with self.assertRaises(PlanInfeasible) as ctx:
            build_plan({"n": 1, "seed": "x",
                        "region": {"east": {"min": 1}, "west": {"min": 1}},
                        "age_band": {"young": {}, "old": {}}},
                       CONTENTION, QueryPolicy())
        self.assertEqual(ctx.exception.evidence["reason"], "lower_bounds_exceed_n")
        with self.assertRaises(PlanInfeasible) as ctx:
            build_plan({"n": 3, "seed": "x",
                        "region": {"east": {"max": 1}, "west": {"max": 1}},
                        "age_band": {"young": {}, "old": {}}},
                       CONTENTION, QueryPolicy())
        self.assertEqual(ctx.exception.evidence["reason"], "upper_bounds_below_n")


class SolverAgreementTests(unittest.TestCase):
    def test_enumeration_and_flow_agree_on_canonical_case(self):
        req = {"n": 2, "seed": "run-42",
               "region": {"east": {"min": 2}},
               "age_band": {"young": {"min": 1}}}
        enum = build_plan(req, CONTENTION, QueryPolicy())
        flow = build_plan(req, CONTENTION, FLOW_POLICY)
        self.assertEqual(enum["solver"], "enumeration")
        self.assertEqual(flow["solver"], "min_cost_flow")
        self.assertEqual(enum["selected_ids"], flow["selected_ids"])
        self.assertEqual(enum["plan_id"], flow["plan_id"])
        self.assertEqual(enum["total_cost"], flow["total_cost"])

    def test_randomized_oracle_cross_check(self):
        rng = random.Random(20261116)
        outcomes = {"agreed": 0, "infeasible": 0, "rejected": 0}
        for trial in range(300):
            m = rng.randint(1, 11)
            people = [member(f"sub{i:03d}", rng.choice(REGIONS), rng.choice(AGES))
                      for i in range(m)]
            if rng.random() < 0.2:
                people[rng.randrange(m)]["attributes"].pop("region", None)
            n = rng.randint(1, m + 1)
            req = {"n": n, "seed": f"seed-{trial}"}
            for dim, vocab in (("region", REGIONS), ("age_band", AGES)):
                quota = {}
                for label in vocab:
                    if rng.random() < 0.5:
                        lo = rng.randint(0, 2)
                        quota[label] = {"min": lo, "max": rng.randint(lo, max(lo, n))}
                if not quota:
                    quota[vocab[0]] = {"max": n}
                req[dim] = quota

            def run(policy):
                try:
                    return "ok", build_plan(req, people, policy)
                except PlanInfeasible as exc:
                    return "infeasible", exc.evidence["reason"]
                except PlanRequestError as exc:
                    return "rejected", str(exc)

            kind_a, a = run(FLOW_POLICY)
            kind_b, b = run(QueryPolicy())
            self.assertEqual(kind_a, kind_b, (trial, req))
            if kind_a == "ok":
                self.assertEqual(a["selected_ids"], b["selected_ids"])
                self.assertEqual(a["plan_id"], b["plan_id"])
                self.assertEqual(a["total_cost"], b["total_cost"])
                outcomes["agreed"] += 1
            else:
                outcomes[kind_a] += 1
        # Sanity: the random harness exercises all three outcomes.
        self.assertTrue(all(value > 0 for value in outcomes.values()), outcomes)

    def test_brute_force_cost_and_lexicographic_tie_break(self):
        people = [member(f"id{i}", "east" if i % 2 else "west",
                         "young" if i < 4 else "old") for i in range(8)]
        costs = {p["id"]: stable_subject_cost(SOLVER_PROTOCOL_VERSION, "tie", p["id"])
                 for p in people}
        for n in (1, 3, 5, 8):
            req = {"n": n, "seed": "tie",
                   "region": {"east": {"max": n}, "west": {"max": n}},
                   "age_band": {"young": {"max": n}, "old": {"max": n}}}
            best = min(
                ((sum(costs[s] for s in subset), subset)
                 for subset in combinations(sorted(costs), n)),
                key=lambda item: item)
            for policy in (QueryPolicy(), FLOW_POLICY):
                plan = build_plan(req, people, policy)
                self.assertEqual(plan["selected_ids"], list(best[1]))
                self.assertEqual(plan["total_cost"], best[0])

    def test_combination_count_helper(self):
        self.assertEqual(_combination_count(5, 2), 10)
        self.assertEqual(_combination_count(5, 3), 10)
        self.assertEqual(_combination_count(3, 5), 0)
        self.assertEqual(_combination_count(10, 0), 1)


class DeterminismTests(unittest.TestCase):
    REQ = {"n": 2, "seed": "restart",
           "region": {"east": {"min": 1}, "west": {}},
           "age_band": {"young": {}, "old": {}}}

    def test_same_seed_restart_is_identical(self):
        first = build_plan(self.REQ, CONTENTION, QueryPolicy())
        second = build_plan(self.REQ, CONTENTION, QueryPolicy())
        self.assertEqual(first["selected_ids"], second["selected_ids"])
        self.assertEqual(first["plan_id"], second["plan_id"])

    def test_stable_cost_is_hash_seed_independent(self):
        cost = stable_subject_cost(SOLVER_PROTOCOL_VERSION, "seed", "subject-9")
        self.assertEqual(cost, stable_subject_cost(SOLVER_PROTOCOL_VERSION, "seed", "subject-9"))
        self.assertIsInstance(cost, int)
        self.assertLess(cost, 1 << 64)

    def test_different_seeds_change_selection_or_cost_but_not_protocol(self):
        plans = []
        for seed in ("alpha", "beta", "gamma"):
            req = {**self.REQ, "seed": seed}
            plans.append(build_plan(req, CONTENTION, FLOW_POLICY))
        self.assertEqual(len({p["plan_id"] for p in plans}), 3)
        self.assertTrue(all(p["protocol_version"] == SOLVER_PROTOCOL_VERSION for p in plans))


class CapacityTests(unittest.TestCase):
    def setUp(self):
        rng = random.Random(11)
        self.people = [
            member(f"s{i:04d}", rng.choice(REGIONS), rng.choice(AGES))
            for i in range(60)]

    def _req(self, n=20):
        return {"n": n, "seed": "cap",
                "region": {"east": {"min": 5}, "west": {"max": 15}},
                "age_band": {"young": {"min": 3}, "old": {"max": 18}}}

    def test_member_limit_refusal_is_distinct_from_infeasibility(self):
        policy = replace(QueryPolicy(), max_plan_members=10)
        with self.assertRaises(PlanCapacityError) as ctx:
            build_plan(self._req(), self.people, policy)
        self.assertNotIsInstance(ctx.exception, PlanInfeasible)
        self.assertIn("max_plan_members", ctx.exception.limits)

    def test_node_and_edge_limits_refuse_without_approximation(self):
        policy = replace(QueryPolicy(), max_plan_nodes=30)
        with self.assertRaises(PlanCapacityError):
            build_plan(self._req(), self.people, policy)
        policy = replace(QueryPolicy(), max_plan_edges=30)
        with self.assertRaises(PlanCapacityError):
            build_plan(self._req(), self.people, policy)

    def test_enumeration_limit_overflow_falls_back_to_exact_flow(self):
        # Default enumeration_limit=200000; choose(m,n) overflows it but flow
        # still returns an exact, independently verified result.
        policy = replace(QueryPolicy(), enumeration_limit=100)
        plan = build_plan(self._req(), self.people, policy)
        self.assertEqual(plan["solver"], "min_cost_flow")
        self._assert_quota_satisfaction(plan)

    def test_computation_budget_refusal(self):
        policy = replace(QueryPolicy(), enumeration_limit=0, flow_steps_budget=3)
        with self.assertRaises(PlanCapacityError):
            build_plan(self._req(), self.people, policy)

    def _assert_quota_satisfaction(self, plan):
        chosen = set(plan["selected_ids"])
        self.assertEqual(len(chosen), plan["n"])
        by_id = {p["id"]: p for p in self.people}
        for dim_index, dim in enumerate(plan["dimensions"]):
            used = {}
            for sid in plan["selected_ids"]:
                value = by_id[sid]["attributes"].get(dim)
                used[value] = used.get(value, 0) + 1
            self.assertEqual(sum(used.values()), plan["n"])


class EngineBoundaryTests(unittest.TestCase):
    BASE_QUERY = {
        "start": "2026-11-01T00:00:00Z", "end": "2026-11-03T00:00:00Z",
        "where": {"field": "plan", "op": "eq", "value": "pro"},
        "sequence": ["signup", "activate"], "max_gap_seconds": 86400,
        "group_by": ["region"]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cohort.sqlite"
        self.engine = CohortEngine(self.path)
        fixtures = Path(__file__).resolve().parents[1] / "examples"
        population = json.loads((fixtures / "population.json").read_text())
        self.engine.ingest("a", population["subjects"], population["events"])
        self.snapshot = self.engine.query("a", "alice", self.BASE_QUERY)["snapshot"]
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.engine.close)

    def _plan_request(self, **overrides):
        req = {"snapshot": self.snapshot, "n": 2, "seed": "job-1",
               "region": {"east": {"min": 2}},
               "age_band": {"30-39": {"min": 1}, "40-49": {}}}
        req.update(overrides)
        return req

    def test_plan_reads_frozen_snapshot_and_returns_ids_internally(self):
        result = self.engine.build_sample_plan("a", "alice", self._plan_request())
        self.assertEqual(len(result["selected_ids"]), 2)
        self.assertTrue(all(isinstance(sid, str) for sid in result["selected_ids"]))
        self.assertEqual(result["snapshot"], self.snapshot)
        self.assertIn("plan_id", result)
        self.assertIn("frozen_digest", result)

    def test_wrong_principal_and_tenant_cannot_read_members(self):
        with self.assertRaises(PlanRequestError):
            self.engine.build_sample_plan("a", "bob", self._plan_request())
        with self.assertRaises(PlanRequestError):
            self.engine.build_sample_plan("other", "alice", self._plan_request())

    def test_solving_failure_creates_no_snapshot_cache_audit_or_budget(self):
        before = self._table_counts()
        with self.assertRaises(PlanInfeasible):
            self.engine.build_sample_plan(
                "a", "alice",
                self._plan_request(region={"east": {"min": 2, "max": 2}},
                                   age_band={"40-49": {"min": 2}}))
        # Also a malformed request must be side-effect free.
        with self.assertRaises(PlanRequestError):
            self.engine.build_sample_plan("a", "alice", {"snapshot": self.snapshot, "n": 0})
        self.assertEqual(self._table_counts(), before)

    def test_successful_plan_is_still_read_only(self):
        before = self._table_counts()
        self.engine.build_sample_plan("a", "alice", self._plan_request())
        self.engine.build_sample_plan("a", "alice", self._plan_request())
        self.assertEqual(self._table_counts(), before)

    def _table_counts(self):
        db = self.engine.db
        return {
            name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in ("snapshots", "cache", "audit", "budgets")}

    def test_reingest_attribute_updates_do_not_change_existing_plan(self):
        first = self.engine.build_sample_plan("a", "alice", self._plan_request())
        fixtures = Path(__file__).resolve().parents[1] / "examples"
        population = json.loads((fixtures / "population.json").read_text())
        updated = [dict(subject, attributes={**subject["attributes"], "region": "north"})
                   for subject in population["subjects"]]
        self.engine.ingest("a", updated)
        second = self.engine.build_sample_plan("a", "alice", self._plan_request())
        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(first["selected_ids"], second["selected_ids"])
        self.assertEqual(first["frozen_digest"], second["frozen_digest"])

    def test_plan_identity_changes_with_quota_n_and_seed(self):
        baseline = self.engine.build_sample_plan("a", "alice", self._plan_request())
        changed_seed = self.engine.build_sample_plan(
            "a", "alice", self._plan_request(seed="job-2"))
        changed_n = self.engine.build_sample_plan(
            "a", "alice", self._plan_request(n=3, region={"east": {}},
                                             age_band={"30-39": {}, "40-49": {}}))
        self.assertNotEqual(baseline["plan_id"], changed_seed["plan_id"])
        self.assertNotEqual(baseline["plan_id"], changed_n["plan_id"])

    def test_plan_rejects_fields_that_would_requery_or_filter(self):
        for extra in ({"where": {}}, {"sequence": ["signup"]},
                      {"events": []}, {"start": "2026-11-01T00:00:00Z"}):
            with self.subTest(extra=extra):
                with self.assertRaises(PlanRequestError):
                    self.engine.build_sample_plan(
                        "a", "alice", self._plan_request(**extra))

    def test_plan_requires_existing_snapshot_reference(self):
        with self.assertRaises(PlanRequestError):
            self.engine.build_sample_plan(
                "a", "alice", self._plan_request(snapshot="nope"))


class CrossProcessDeterminismTests(unittest.TestCase):
    def test_selection_independent_of_python_hash_seed(self):
        project = Path(__file__).resolve().parents[1]
        script = project / "tests" / "_hashseed_probe.py"
        env = dict(os.environ, PYTHONPATH=str(project / "src"))
        outputs = []
        for seed_env in ("0", "1", "12345"):
            env["PYTHONHASHSEED"] = seed_env
            completed = subprocess.run(
                [sys.executable, "-I", str(script)], env=env, check=True,
                capture_output=True, text=True)
            outputs.append(completed.stdout.strip())
        self.assertEqual(len(set(outputs)), 1, outputs)


if __name__ == "__main__":
    unittest.main()
