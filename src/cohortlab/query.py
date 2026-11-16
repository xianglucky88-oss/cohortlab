from datetime import datetime, timezone
from .policy import canonical

# Language version of the declarative temporal query definition. Bumped with
# every semantic/normalization change; it is part of cache identity, audit rows
# and snapshots so older results can never be reused under new semantics.
LANGUAGE_VERSION = "2026-11-v2"
LEGACY_LANGUAGE_VERSION = "2026-11-v1"

# Filter tree bounds shared by subject predicates and event predicates.
MAX_FILTER_NODES = 80
MAX_FILTER_DEPTH = 10
MAX_IN_VALUES = 100

_COMPARISON_OPS = ("eq", "in", "gte", "lt")


def instant(value):
    if not isinstance(value, str): raise ValueError("ISO8601 timestamp is required")
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None: raise ValueError("timestamps require timezone")
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def _scalar(value):
    return isinstance(value, (str, int, float, bool)) or value is None


def _check_predicate(node, whitelist, depth=0, visited=0):
    """Walk and return a normalized copy of a bounded predicate tree.

    Only whitelisted fields and fixed operators are accepted, so no user SQL or
    expression is ever evaluated.
    """
    visited += 1
    if visited > MAX_FILTER_NODES or depth > MAX_FILTER_DEPTH or not isinstance(node, dict):
        raise ValueError("filter complexity limit")
    branches = set(node) & {"all", "any", "not"}
    if branches:
        if len(branches) != 1 or len(node) != 1: raise ValueError("ambiguous filter")
        branch = next(iter(branches))
        children = [node[branch]] if branch == "not" else node[branch]
        if not isinstance(children, list) or not children: raise ValueError("empty filter")
        normalized_children, seen = [], visited
        for child in children:
            copy, seen = _check_predicate(child, whitelist, depth + 1, seen)
            normalized_children.append(copy)
        return {branch: normalized_children}, seen
    if set(node) != {"field", "op", "value"} or node["field"] not in whitelist or node["op"] not in _COMPARISON_OPS:
        raise ValueError("filter is not permitted")
    if node["op"] == "in":
        if not isinstance(node["value"], list) or not node["value"] or len(node["value"]) > MAX_IN_VALUES:
            raise ValueError("membership filter limit")
        if not all(_scalar(v) for v in node["value"]): raise ValueError("filter values must be scalars")
    elif not _scalar(node["value"]):
        raise ValueError("filter value must be a scalar")
    return {"field": node["field"], "op": node["op"], "value": node["value"]}, visited


def _bounded_seconds(value, window_seconds, name):
    if type(value) is not int or value < 1 or value > window_seconds:
        raise ValueError(f"invalid {name}")
    return value


def _normalize_step(raw, index, policy):
    if isinstance(raw, str):
        if not raw: raise ValueError("invalid event sequence")
        return {"type": raw, "optional": False, "where": None}
    if not isinstance(raw, dict) or set(raw) - {"type", "optional", "where"}:
        raise ValueError("invalid sequence step")
    if not isinstance(raw.get("type"), str) or not raw["type"]: raise ValueError("invalid sequence step")
    if "optional" in raw and not isinstance(raw["optional"], bool): raise ValueError("optional must be a boolean")
    where = None
    if "where" in raw and raw["where"] is not None:
        where, _ = _check_predicate(raw["where"], policy.event_filter_fields)
    return {"type": raw["type"], "optional": bool(raw.get("optional", False)), "where": where}


def _normalize_negative(raw, policy):
    if not isinstance(raw, dict) or set(raw) != {"type", "where"}:
        raise ValueError("negative condition must declare a type and a where predicate (null allowed)")
    if not isinstance(raw["type"], str) or not raw["type"]: raise ValueError("invalid negative condition")
    where = None
    if raw["where"] is not None:
        where, _ = _check_predicate(raw["where"], policy.event_filter_fields)
    return {"type": raw["type"], "where": where}


def prepare(definition, policy):
    """Validate and return the bounded, normalized query AST.

    The returned object is JSON-canonical (deterministic key order via
    ``canonical`` callers) and carries its language version. Two requests that
    differ only in JSON key order normalize to the same object.
    """
    if not isinstance(definition, dict) or set(definition) - {
            "where", "sequence", "start", "end", "max_gap_seconds",
            "max_total_seconds", "group_by", "snapshot"}:
        raise ValueError("unknown query fields")
    groups = definition.get("group_by", [])
    if not isinstance(groups, list) or len(groups) > policy.max_group_dimensions or \
            len(set(groups)) != len(groups) or set(groups) - set(policy.group_fields):
        raise ValueError("grouping exceeds policy or uses undeclared fields")
    if definition.get("snapshot"):
        if set(definition) - {"snapshot", "group_by"}: raise ValueError("snapshot queries may only change grouping")
        return {"snapshot": definition["snapshot"], "group_by": list(groups)}

    start, end = instant(definition.get("start")), instant(definition.get("end"))
    if not 0 < end - start <= policy.max_window_days * 86400 * 1000000:
        raise ValueError("query time window exceeds policy")
    window_seconds = policy.max_window_days * 86400

    where = None
    if "where" in definition and definition["where"] is not None:
        where, _ = _check_predicate(definition["where"], policy.filter_fields)

    gap_seconds = _bounded_seconds(
        definition.get("max_gap_seconds", window_seconds), window_seconds, "sequence gap")
    total_seconds = None
    if "max_total_seconds" in definition and definition["max_total_seconds"] is not None:
        total_seconds = _bounded_seconds(definition["max_total_seconds"], window_seconds, "sequence total duration")

    sequence = definition.get("sequence", [])
    if isinstance(sequence, dict):
        if set(sequence) - {"steps", "not"}: raise ValueError("unknown sequence fields")
        raw_steps = sequence.get("steps", [])
        if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > policy.max_sequence:
            raise ValueError("invalid event sequence")
        steps = [_normalize_step(raw, i, policy) for i, raw in enumerate(raw_steps)]
        if not any(step["optional"] is False for step in steps):
            raise ValueError("a sequence must contain at least one required step")
        raw_not = sequence.get("not", [])
        if not isinstance(raw_not, list) or len(raw_not) > policy.max_sequence: raise ValueError("invalid negative conditions")
        negatives = [_normalize_negative(raw, policy) for raw in raw_not]
        required = [i for i, step in enumerate(steps) if not step["optional"]]
        if negatives and len(required) < 2:
            raise ValueError("negative conditions require boundaries on both sides: at least two required steps")
        normalized = {"language": LANGUAGE_VERSION, "steps": steps, "not": negatives,
                      "max_gap_us": gap_seconds * 1000000,
                      "max_total_us": None if total_seconds is None else total_seconds * 1000000}
    elif sequence in (None, []):
        normalized = {"language": LEGACY_LANGUAGE_VERSION, "steps": [], "not": [],
                      "max_gap_us": gap_seconds * 1000000, "max_total_us": None}
    elif isinstance(sequence, list) and all(isinstance(e, str) for e in sequence):
        if len(sequence) > policy.max_sequence or any(not e for e in sequence):
            raise ValueError("invalid event sequence")
        steps = [{"type": e, "optional": False, "where": None} for e in sequence]
        normalized = {"language": LEGACY_LANGUAGE_VERSION, "steps": steps, "not": [],
                      "max_gap_us": gap_seconds * 1000000, "max_total_us": None}
    else:
        raise ValueError("invalid event sequence")

    return {"start": start, "end": end, "where": where, "group_by": list(groups), "sequence": normalized}


def validate(definition, policy):
    """Backward-compatible validation entry point; the normalized AST is the
    single source of truth used by the engine."""
    prepare(definition, policy)


def matches(node, attributes):
    if node is None: return True
    if "all" in node: return all(matches(n, attributes) for n in node["all"])
    if "any" in node: return any(matches(n, attributes) for n in node["any"])
    if "not" in node: return not matches(node["not"], attributes)
    if node["field"] not in attributes: return False
    a, b = attributes[node["field"]], node["value"]
    try:
        if node["op"] == "eq": return a == b
        if node["op"] == "in": return a in b
        if node["op"] == "gte": return a >= b
        return a < b
    except TypeError: return False


def _reachable_steps(steps, index):
    """Indices reachable from ``index`` by skipping optional steps.

    Reaching ``len(steps)`` means every required step has been consumed and the
    expression is satisfied. Skipping consumes no event, so it neither invents
    an event nor resets timing.
    """
    reachable, current = set(), index
    while True:
        reachable.add(current)
        if current == len(steps) or not steps[current]["optional"]: break
        current += 1
    return reachable


def _step_matches(step, event):
    return event[1] == step["type"] and (step["where"] is None or matches(step["where"], event[3]))


def _is_negative(negatives, event):
    return any(event[1] == node["type"] and (node["where"] is None or matches(node["where"], event[3]))
               for node in negatives)


def temporal_matches(events, normalized, max_states):
    """Bounded production executor for the normalized temporal AST.

    Events are tuples ``(index, type, at_us, attributes)`` already ordered by
    ``(at, event id)``; the index makes same-instant ordering total.

    Execution state: ``(next_step, first_index, last_index, first_at, last_at)``
    where ``next_step`` counts consumed steps (optional skips never consume an
    event and are explored lazily via :func:`_reachable_steps`). A state is
    retained only if no state at the same step dominates it: the last-consumed
    timestamp (always) and, when a total bound exists, the first-consumed
    timestamp define a Pareto frontier, so every still-relevant alternative
    path is kept. Exceeding ``max_states`` raises instead of silently dropping
    paths.
    """
    seq = normalized["sequence"]
    steps, negatives = seq["steps"], seq["not"]
    gap_us, total_us = seq["max_gap_us"], seq["max_total_us"]
    n = len(steps)
    if n == 0: return True

    # frontier stores only states that have consumed at least one event.
    frontier = []

    def dominates(a, b):
        # Same consumed step, distinct events. For the adjacent-gap constraints
        # a later last-consumed timestamp is always at least as good: the gap to
        # any future event is then no larger, and any open negative interval is
        # no wider. So a dominates b if a ends no earlier than b. When the
        # total-duration bound is active a must also start no earlier than b so
        # the span to a future event stays no longer. Without that bound the
        # starting timestamp is irrelevant (an earlier start can still catch up
        # over the remaining required steps) and must not prune.
        if a[0] != b[0] or a[4] < b[4] or (a[1], a[2]) == (b[1], b[2]):
            return False
        if total_us is not None and a[3] < b[3]:
            return False
        return (a[3], a[4]) != (b[3], b[4])

    def add_state(states, state):
        if any(s == state for s in states): return states
        if any(dominates(other, state) for other in states): return states
        kept = [s for s in states if not dominates(state, s)]
        kept.append(state)
        if len(kept) > max_states:
            raise ValueError(
                f"execution state limit exceeded: {len(kept)} live paths > policy.max_states={max_states}; "
                "narrow the window or request a higher bound rather than dropping legal paths")
        return kept

    # Steps at which a fresh path may start: step 0 plus every leading optional
    # step reachable by skipping. A restart is always available even after a
    # path has been started or poisoned, because skipping a leading optional
    # step invents no event. The empty prefix is never poisoned by negatives.
    start_targets = sorted(t for t in _reachable_steps(steps, 0) if t < n)

    for event in events:
        index, _, at, _attrs = event
        is_negative = bool(negatives) and _is_negative(negatives, event)

        # Positive matching runs first against the unpoisoned frontier. A
        # negative-typed event can simultaneously be the right boundary of an
        # open interval: consuming it as a positive step closes that interval,
        # so it must be allowed to extend the very state it would otherwise
        # poison. The open (last_consumed, next_consumed) interval is judged in
        # total (at, event id) order; same-instant ordering uses the event
        # index, and an interval is never declared clear before it is closed.
        additions = []
        # Fresh starts (including a start that skips leading optional steps).
        for target in start_targets:
            if _step_matches(steps[target], event):
                if n in _reachable_steps(steps, target + 1): return True
                additions.append((target + 1, index, index, at, at))
        # Extensions of already consumed paths.
        for state in frontier:
            next_step, first_i, last_i, first_at, last_at = state
            for target in _reachable_steps(steps, next_step):
                if target == n or not _step_matches(steps[target], event): continue
                if at - last_at > gap_us: continue
                # The total duration spans the first through the last consumed
                # event; it is only meaningful once a further event is appended
                # to an existing path. A single consumed event has span zero,
                # so fresh starts are never checked against it.
                if total_us is not None and at - first_at > total_us: continue
                if n in _reachable_steps(steps, target + 1): return True
                additions.append((target + 1, first_i, index, first_at, at))

        # Only after positive matching do negative conditions poison the
        # intervals that remain open: states whose last consumed event strictly
        # precedes the negative event and which were not advanced by it. A state
        # whose boundary equals this event (last_i == index) has just consumed
        # it, so its new interval opens after the event and survives.
        if is_negative:
            frontier = [s for s in frontier if not (s[2] < index)]
        for state in additions:
            frontier = add_state(frontier, state)
    return False


def sequence_matches(events, sequence, max_gap, max_states=1000):
    """Legacy entry point used by existing callers and tests.

    Accepts ``(at, type)`` tuples and a list of required event types; limits are
    interpreted in the same units as the supplied timestamps. Internally it
    builds a normalized v1 AST and runs the same bounded production executor.
    """
    if not sequence: return True
    normalized_events = [(i, kind, at, {}) for i, (at, kind) in enumerate(events)]
    normalized = {
        "sequence": {
            "language": LEGACY_LANGUAGE_VERSION,
            "steps": [{"type": t, "optional": False, "where": None} for t in sequence],
            "not": [], "max_gap_us": max_gap, "max_total_us": None,
        }
    }
    return temporal_matches(normalized_events, normalized, max_states)


def aggregate(subjects, dimensions, policy):
    groups = {}
    for subject in subjects:
        values = {d: subject["attributes"].get(d) for d in dimensions}
        key = canonical(values)
        if key not in groups:
            if len(groups) >= policy.max_groups: raise ValueError("group count exceeds policy")
            groups[key] = {"group": values, "count": 0, "suppressed": False}
        groups[key]["count"] += 1
    cells = [groups[k] for k in sorted(groups)]
    if not cells:
        cells = [{"group": {}, "count": 0, "suppressed": True}]
    small = [c for c in cells if c["count"] < policy.k]
    for cell in small: cell["suppressed"] = True
    # Suppress a complementary cell when exactly one cell is small.
    if len(small) == 1 and len(cells) > 1:
        min((c for c in cells if not c["suppressed"]), key=lambda c: c["count"])["suppressed"] = True
    total = sum(c["count"] for c in cells) if not any(c["suppressed"] for c in cells) else None
    for cell in cells:
        if cell["suppressed"]: cell["count"] = None
    return {"cells": cells, "total": total, "policy": policy.digest}
