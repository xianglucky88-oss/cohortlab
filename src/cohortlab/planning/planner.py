"""Plan identity, stable costs and end-to-end orchestration.

The module is storage agnostic. It receives already-read frozen member copies
from an adapter and returns a plan whose identity and selection are fully
determined by:

* the frozen content digest (canonical member ids and attribute copies),
* both normalized quota tables,
* N,
* the plan seed,
* the solver protocol version.

Re-ingesting current population attributes therefore cannot change a plan: the
adapter only reads the frozen snapshot rows, and the digest binds the plan id
to the exact bytes used.

Stable integer costs
--------------------
The primary cost of subject ``i`` is the first 8 bytes of
``sha256(canonical([protocol_version, seed, subject_id]))`` interpreted
big-endian. The hash input never includes a Python set, a dict iteration or a
hash-based container, so process ``PYTHONHASHSEED`` randomization cannot affect
it. This is a deterministic keyed ranking - it is explicitly *not* uniform
random sampling and not differential privacy.

Tie handling
------------
Among equal primary-cost sums the result must be the lexicographically
smallest sorted list of selected ids. With ids ranked ``r = 0..m-1`` and
``SCALE = 2**m`` each subject gets the integer weight::

    weight = primary * SCALE - 2 ** (m - 1 - r)

For fixed-size N-subsets the subtracted tie term is maximized (sum minimized)
exactly by the lexicographically smallest sorted id tuple, while
``0 <= sum(tie terms) < SCALE`` guarantees primary cost differences of one
unit always dominate every tie rearrangement. Minimizing the total weight in
the flow network therefore yields the unique documented result. Python's
arbitrary-precision integers make the encoding exact.
"""

import hashlib

from ..policy import canonical
from .quota import PlanRequestError, inventory, normalize_request, precheck
from .solver import solve

#: Bumped whenever the network construction, cost encoding or tie semantics
#: change. Old plan ids remain bound to their own version via the digest input.
SOLVER_PROTOCOL_VERSION = "cohortlab-sample-plan/v1"

#: Fields a plan request may carry at the orchestration boundary.
_PLAN_FIELDS = ("n", "seed")


def stable_subject_cost(protocol_version, seed, subject_id):
    digest = hashlib.sha256(
        canonical([protocol_version, seed, subject_id]).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def frozen_digest(members):
    """Content digest of the frozen member copies, order independent."""
    payload = canonical(sorted(
        ({"id": m["id"], "attributes": m["attributes"]} for m in members),
        key=lambda row: row["id"]))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_plan(request, members, policy, dimensions=None):
    """Validate, diagnose and solve an internal sample plan.

    Parameters
    ----------
    request:
        ``{"n": int, "seed": str, dim0: {label: {"min","max"}}, dim1: ...}``.
    members:
        Frozen copies already read through a tenant/principal-checked adapter.
    policy:
        :class:`~cohortlab.policy.QueryPolicy` carrying the declared node, edge
        and computation limits.
    dimensions:
        The two quota attribute names; defaults to
        ``policy.plan_dimensions``.
    """
    dimensions = tuple(dimensions or policy.plan_dimensions)
    if len(dimensions) != 2 or len(set(dimensions)) != 2:
        raise ValueError("sample plans require exactly two distinct quota dimensions")
    if any(d not in policy.group_fields for d in dimensions):
        raise ValueError("plan dimensions must be declared in policy group_fields")
    unknown = set(request) - set(_PLAN_FIELDS) - set(dimensions)
    if unknown:
        raise PlanRequestError(f"unknown plan fields: {sorted(unknown)}")

    plan = normalize_request(request, members, dimensions, SOLVER_PROTOCOL_VERSION)
    precheck(plan)

    primary_costs = {
        sid: stable_subject_cost(SOLVER_PROTOCOL_VERSION, plan.seed, sid)
        for sid in plan.member_ids}
    m = len(plan.member_ids)
    scale = 1 << m
    rank = {sid: r for r, sid in enumerate(plan.member_ids)}  # sorted ids
    weights = {
        sid: primary_costs[sid] * scale - (1 << (m - 1 - rank[sid]))
        for sid in plan.member_ids}

    limits = {
        "max_plan_members": policy.max_plan_members,
        "max_plan_nodes": policy.max_plan_nodes,
        "max_plan_edges": policy.max_plan_edges,
        "flow_steps_budget": policy.flow_steps_budget,
        "enumeration_limit": policy.enumeration_limit,
        "max_cycle_rounds": policy.max_cycle_rounds,
    }
    selected, method = solve(plan, primary_costs, weights, limits)
    total_primary_cost = sum(primary_costs[sid] for sid in selected)

    identity_input = {
        "protocol": SOLVER_PROTOCOL_VERSION,
        "frozen_digest": frozen_digest(members),
        "n": plan.n,
        "seed": plan.seed,
        "dimensions": list(plan.dimensions),
        "quotas": {dim: [list(entry) for entry in plan.quotas[dim]]
                   for dim in plan.dimensions},
    }
    plan_id = hashlib.sha256(
        canonical(identity_input).encode("utf-8")).hexdigest()

    return {
        "plan_id": plan_id,
        "protocol_version": SOLVER_PROTOCOL_VERSION,
        "solver": method,
        "n": plan.n,
        "seed": plan.seed,
        "dimensions": list(plan.dimensions),
        "frozen_digest": identity_input["frozen_digest"],
        "selected_ids": selected,
        "total_cost": total_primary_cost,
        "limits": {key: limits[key] for key in sorted(limits)},
        # Aggregate diagnostics only: quota inventory carries category counts,
        # never subject ids. The ids remain solely in selected_ids above.
        "inventory": inventory(plan),
    }
