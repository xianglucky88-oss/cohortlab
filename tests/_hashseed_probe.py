"""Subprocess probe: emit a plan selection under a caller-chosen hash seed.

The cross-process test runs this script with several PYTHONHASHSEED values;
identical stdout proves the plan does not depend on process hash randomization.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cohortlab import QueryPolicy, build_plan  # noqa: E402

MEMBERS = [
    {"id": f"m{i:02d}",
     "attributes": {"region": ("east" if i % 2 else "west"),
                    "age_band": ("young" if i % 3 else "old")}}
    for i in range(12)
]
REQUEST = {
    "n": 5,
    "seed": "hashseed-probe",
    "region": {"east": {"min": 2, "max": 4}, "west": {"max": 4}},
    "age_band": {"young": {"min": 1}, "old": {"max": 4}},
}

if __name__ == "__main__":
    plan = build_plan(REQUEST, MEMBERS, QueryPolicy())
    print(json.dumps({"plan_id": plan["plan_id"],
                      "selected_ids": plan["selected_ids"],
                      "total_cost": plan["total_cost"]},
                     sort_keys=True))
