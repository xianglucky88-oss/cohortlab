"""Quota normalization and feasibility diagnostics.

No storage access happens here. The functions take the frozen member copies
(``[{"id": str, "attributes": dict}, ...]``) produced by the storage adapter
and return deterministic, JSON-safe structures.

Category semantics
------------------
Each subject carries exactly one category value per dimension. The two
dimensions are coupled *through the subject*: a subject in region ``east`` and
age band ``young`` contributes to both categories at once, so per-category
inventory comparison can never decide feasibility by itself. The exact joint
diagnosis is computed in :mod:`cohortlab.planning.solver`; this module handles
the univariate contradictions and the inventory on which every diagnosis is
based.

* A dimension value that is missing or ``None`` becomes :data:`MISSING_CATEGORY`
  (the reserved string ``"__none__"``). Missing data is therefore an explicit,
  selectable category rather than a silent drop.
* Booleans, lists and dicts are not attribute values: their presence rejects
  the whole plan. Scalars are keyed by their canonical JSON encoding so that a
  numeric ``1`` and a string ``"1"`` never collide; quotas may use either the
  raw scalar or the canonical JSON string.
* Quotas are *exact* declarations of the only category labels that exist for
  the plan. A label declared in a quota but absent from the frozen population
  rejects the whole plan (unknown category); labels present in the data but
  absent from a quota are implicitly bounded by ``[0, N]``.
* Multi-category string encodings are never parsed: ``"east,west"`` is one
  opaque label, not two categories.

All iteration uses sorted lists / insertion-ordered dicts, never sets, so
Python hash randomization cannot change any output.
"""

from dataclasses import dataclass

from ..policy import canonical

#: Sentinel label for subjects whose dimension attribute is missing/None.
MISSING_CATEGORY = "__none__"
#: Label that looks like the old ad-hoc missing marker; explicit use is rejected
#: so callers cannot confuse it with the documented sentinel.
_RESERVED_AMBIGUOUS = "__missing__"


class PlanRequestError(ValueError):
    """The plan request itself is malformed. No selection is possible."""


class PlanInfeasible(ValueError):
    """The request is well formed but no N-set satisfies both quota sides.

    ``evidence`` is JSON-safe and contains only aggregate quota/inventory
    facts and security summaries - never subject ids.
    """

    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = evidence


class PlanCapacityError(Exception):
    """Declared node/edge/computation limits would be exceeded.

    This is deliberately *not* a :class:`PlanInfeasible`: capacity refusal must
    never be misread as a quota contradiction and never triggers approximation.
    It does not subclass :class:`ValueError` so callers cannot conflate it with
    validation failures.
    """

    def __init__(self, message, limits):
        super().__init__(message)
        self.limits = limits


def label_key(value):
    """Canonical category key for an attribute value or a quota label.

    Raw scalars and canonical JSON strings map to the same key; two values with
    the same key are the same category. The sentinel :data:`MISSING_CATEGORY`
    is reachable on the quota side (that is how a caller bounds subjects with a
    missing attribute) but a subject may never literally carry it as a string.
    """
    if value is None:
        return MISSING_CATEGORY
    if isinstance(value, bool) or isinstance(value, (list, dict)):
        raise PlanRequestError(f"unsupported category value: {value!r}")
    if isinstance(value, str):
        if value == _RESERVED_AMBIGUOUS:
            raise PlanRequestError(f"reserved category label is not permitted: {value!r}")
        return canonical(value)
    return canonical(value)


def _coerce_bound(value, name):
    if type(value) is not int:
        raise PlanRequestError(f"quota bound {name} must be a non-negative integer")
    if value < 0:
        raise PlanRequestError(f"quota bound {name} must be non-negative")
    return value


def _coerce_label(value, dimension):
    if value is None:
        raise PlanRequestError(f"quota label for {dimension} cannot be null; use {MISSING_CATEGORY!r}")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise PlanRequestError(f"unsupported quota label for {dimension}: {value!r}")
    if isinstance(value, str) and value == _RESERVED_AMBIGUOUS:
        raise PlanRequestError(f"reserved category label is not permitted: {value!r}")
    if isinstance(value, str) and value == MISSING_CATEGORY:
        return MISSING_CATEGORY
    return label_key(value)


@dataclass(frozen=True)
class NormalizedPlan:
    """Validated plan request, independent of storage and solver."""

    n: int
    seed: str
    dimensions: tuple
    # dimension -> sorted list of (label_key, lower, upper)
    quotas: dict
    # dimension -> label_key -> number of frozen members carrying that label
    counts: dict
    # subject id -> (label_key dim 0, label_key dim 1)
    member_categories: dict
    member_ids: tuple
    protocol_version: str


def normalize_request(request, members, dimensions, protocol_version):
    """Validate and normalize a plan request against frozen member copies.

    ``request`` accepts only ``n``, ``seed`` and per-dimension quotas; any
    other field (filters, events, windows, ...) is rejected, because planning
    never re-runs temporal queries or accepts new selection conditions.
    """
    if not isinstance(request, dict):
        raise PlanRequestError("plan request must be an object")
    allowed = {"n", "seed"} | set(dimensions)
    unknown = set(request) - allowed
    if unknown:
        raise PlanRequestError(f"unknown plan fields: {sorted(unknown)}")
    if type(request.get("n")) is not int or request["n"] < 1:
        raise PlanRequestError("n must be a positive integer (n=0 plans are not supported)")
    n = request["n"]
    seed = request.get("seed")
    if not isinstance(seed, str) or not seed:
        raise PlanRequestError("seed must be a non-empty string")
    if not isinstance(protocol_version, str) or not protocol_version:
        raise PlanRequestError("solver protocol version is required")

    seen_ids = set()
    ordered_ids = []
    member_categories = {}
    counts = {dim: {} for dim in dimensions}
    for member in members:
        if not isinstance(member, dict) or set(member) != {"id", "attributes"}:
            raise PlanRequestError("frozen members must contain only id and attributes")
        sid = member["id"]
        if not isinstance(sid, str) or not sid:
            raise PlanRequestError("subject id must be a non-empty string")
        if sid in seen_ids:
            raise PlanRequestError(f"duplicate subject id in frozen snapshot: {sid!r}")
        seen_ids.add(sid)
        attrs = member["attributes"]
        if not isinstance(attrs, dict):
            raise PlanRequestError("subject attributes must be an object")
        keys = []
        for dim in dimensions:
            key = label_key(attrs.get(dim))
            keys.append(key)
            counts[dim][key] = counts[dim].get(key, 0) + 1
        member_categories[sid] = tuple(keys)
        ordered_ids.append(sid)
    ordered_ids.sort()
    plan_members = bool(ordered_ids)

    quotas = {}
    for dim in dimensions:
        raw = request.get(dim, {})
        if not isinstance(raw, dict) or not raw:
            raise PlanRequestError(f"quota for {dim} must be a non-empty object of category bounds")
        entries = []
        declared = set()
        for label in sorted(raw):  # sorted -> hash-randomization independent
            spec = raw[label]
            if isinstance(spec, (list, tuple)):
                if len(spec) != 2:
                    raise PlanRequestError(f"quota {dim}.{label} must be [lower, upper]")
                lower, upper = _coerce_bound(spec[0], "lower"), _coerce_bound(spec[1], "upper")
            elif isinstance(spec, dict):
                if set(spec) - {"min", "max"}:
                    raise PlanRequestError(f"quota {dim}.{label} accepts only min and max")
                lower = _coerce_bound(spec.get("min", 0), "min")
                upper = _coerce_bound(spec.get("max", n), "max")
            else:
                raise PlanRequestError(f"quota {dim}.{label} must be an object or [lower, upper]")
            if lower > upper:
                raise PlanRequestError(f"quota {dim}.{label} has lower bound above upper bound")
            if upper > n:
                raise PlanRequestError(f"quota {dim}.{label} upper bound exceeds n={n}")
            key = _coerce_label(label, dim)
            if key in declared:
                raise PlanRequestError(f"duplicate normalized quota label in {dim}: {label!r}")
            declared.add(key)
            entries.append((key, lower, upper))
        # Unknown category: a declared label with no frozen carrier can never be
        # filled. An entirely empty population gets its own feasibility
        # diagnosis in precheck instead of a per-label request rejection.
        if plan_members:
            unknown_labels = sorted(key for key, _, _ in entries
                                    if key not in counts[dim])
            if unknown_labels:
                raise PlanRequestError(
                    f"quota labels absent from frozen population in {dim}: {unknown_labels}")
        quotas[dim] = tuple(sorted(entries))

    return NormalizedPlan(
        n=n, seed=seed, dimensions=tuple(dimensions), quotas=quotas,
        counts=counts, member_categories=member_categories,
        member_ids=tuple(ordered_ids), protocol_version=protocol_version)


def inventory(plan):
    """JSON-safe per-dimension inventory merging quotas and observed counts."""
    result = {}
    for dim in plan.dimensions:
        declared = {key: (lower, upper) for key, lower, upper in plan.quotas[dim]}
        labels = sorted(set(declared) | set(plan.counts[dim]))
        result[dim] = [{
            "category": None if key == MISSING_CATEGORY else _decode_label(key),
            "available": plan.counts[dim].get(key, 0),
            "min": declared.get(key, (0, plan.n))[0],
            "max": declared.get(key, (0, plan.n))[1],
        } for key in labels]
    return result


def _decode_label(key):
    # label_key uses canonical JSON; decode it for readable diagnostics.
    import json
    try:
        return json.loads(key)
    except ValueError:
        return key


def precheck(plan):
    """Univariate feasibility checks that do not require flow analysis.

    The coupled contention case (each side feasible alone, jointly impossible)
    is left to the min-cut certificate in the solver; raising here keeps the
    solver focused on genuinely joint structure.
    """
    if not plan.member_ids:
        raise PlanInfeasible("frozen population is empty", {
            "reason": "empty_population", "n": plan.n, "inventory": inventory(plan)})
    if plan.n > len(plan.member_ids):
        raise PlanInfeasible(f"population of {len(plan.member_ids)} cannot fill n={plan.n}", {
            "reason": "population_smaller_than_n", "n": plan.n,
            "population_size": len(plan.member_ids), "inventory": inventory(plan)})
    for dim in plan.dimensions:
        total_lower = sum(lower for _, lower, _ in plan.quotas[dim])
        total_upper = sum(upper for _, _, upper in plan.quotas[dim])
        if total_lower > plan.n:
            raise PlanInfeasible(f"lower bounds on {dim} sum to {total_lower} > n={plan.n}", {
                "reason": "lower_bounds_exceed_n", "dimension": dim,
                "lower_sum": total_lower, "n": plan.n, "inventory": inventory(plan)})
        if total_upper < plan.n:
            raise PlanInfeasible(f"upper bounds on {dim} sum to {total_upper} < n={plan.n}", {
                "reason": "upper_bounds_below_n", "dimension": dim,
                "upper_sum": total_upper, "n": plan.n, "inventory": inventory(plan)})
        for key, lower, upper in plan.quotas[dim]:
            available = plan.counts[dim].get(key, 0)
            if lower > available:
                raise PlanInfeasible(
                    f"category {key!r} on {dim} requires {lower} but only {available} exist", {
                        "reason": "category_inventory_shortfall", "dimension": dim,
                        "category": None if key == MISSING_CATEGORY else _decode_label(key),
                        "required": lower, "available": available,
                        "inventory": inventory(plan)})
