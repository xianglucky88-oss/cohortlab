"""In-process sample-plan example (trusted Python job boundary only).

This deliberately is not a CLI entry point: selected subject ids are returned
only to a trusted internal job that already authenticated the tenant and
principal. The plan reads an existing frozen snapshot, never re-runs a temporal
query, and never consumes query budget or mutates snapshots/cache/audit.

Run from the project root:

    PYTHONPATH=src python3 examples/sample_plan.py
"""

import json
import tempfile
from pathlib import Path

from cohortlab import CohortEngine, PlanInfeasible, QueryPolicy

project = Path(__file__).resolve().parents[1]

BASE_QUERY = {
    "start": "2026-11-01T00:00:00Z", "end": "2026-11-03T00:00:00Z",
    "where": {"field": "plan", "op": "eq", "value": "pro"},
    "sequence": ["signup", "activate"], "max_gap_seconds": 86400,
    "group_by": ["region"]}

# Two-sided quotas: pick 2 of the frozen pro-plan cohort, both from east, at
# least one aged 30-39. Bounds are non-negative integers per category.
PLAN_REQUEST = {
    "snapshot": None,  # filled in once the frozen snapshot exists
    "n": 2,
    "seed": "research-job-2026-11",
    "region": {"east": {"min": 2, "max": 2}},
    "age_band": {"30-39": {"min": 1}, "40-49": {"max": 1}},
}


def main():
    with tempfile.TemporaryDirectory() as directory:
        engine = CohortEngine(Path(directory) / "cohort.sqlite", QueryPolicy())
        try:
            population = json.loads((project / "examples" / "population.json").read_text())
            engine.ingest("demo", population["subjects"], population["events"])
            snapshot_id = engine.query("demo", "analyst", BASE_QUERY)["snapshot"]

            request = {**PLAN_REQUEST, "snapshot": snapshot_id}
            plan = engine.build_sample_plan("demo", "analyst", request)
            print(json.dumps({
                "plan_id": plan["plan_id"],
                "solver": plan["solver"],
                "selected_ids": plan["selected_ids"],
                "total_cost": plan["total_cost"],
                "frozen_digest": plan["frozen_digest"][:16] + "...",
            }, indent=2))

            # A jointly contradictory request is diagnosed, never relaxed.
            try:
                engine.build_sample_plan("demo", "analyst", {
                    **request,
                    "region": {"east": {"min": 2, "max": 2}},
                    "age_band": {"40-49": {"min": 2}}})
            except PlanInfeasible as exc:
                print("infeasible reason:", exc.evidence["reason"])
        finally:
            engine.close()


if __name__ == "__main__":
    main()
