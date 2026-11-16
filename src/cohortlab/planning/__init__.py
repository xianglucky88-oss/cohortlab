"""Internal reproducible quota sampling plans.

This package is an internal, in-process Python job boundary. It never opens a
new public query or authentication surface; the enclosing engine still relies
on the trusted caller to supply tenant/principal.

Three separable concerns live in separate modules:

* ``quota``   - request normalization, deterministic category semantics and
                feasibility diagnostics (pure functions, no storage).
* ``solver``  - exact selection: exhaustive N-subset enumeration for small
                inputs and a lower-bound min-cost circulation for large ones,
                both bounded by declared node/edge/computation limits.
* ``planner`` - plan identity, stable per-subject integer costs from the plan
                seed/protocol version/subject id and orchestration.
"""

from .quota import (
    MISSING_CATEGORY,
    NormalizedPlan,
    PlanCapacityError,
    PlanInfeasible,
    PlanRequestError,
    inventory,
    normalize_request,
    precheck,
)
from .planner import build_plan

__all__ = [
    "MISSING_CATEGORY",
    "NormalizedPlan",
    "PlanCapacityError",
    "PlanInfeasible",
    "PlanRequestError",
    "build_plan",
    "inventory",
    "normalize_request",
    "precheck",
]
