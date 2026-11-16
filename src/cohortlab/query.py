from datetime import datetime, timezone
from .policy import canonical


def instant(value):
    if not isinstance(value, str): raise ValueError("ISO8601 timestamp is required")
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None: raise ValueError("timestamps require timezone")
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def validate(definition, policy):
    if not isinstance(definition, dict) or set(definition) - {"where", "sequence", "start", "end", "max_gap_seconds", "group_by", "snapshot"}:
        raise ValueError("unknown query fields")
    groups = definition.get("group_by", [])
    if not isinstance(groups, list) or len(groups) > policy.max_group_dimensions or len(set(groups)) != len(groups) or set(groups) - set(policy.group_fields):
        raise ValueError("grouping exceeds policy or uses undeclared fields")
    if definition.get("snapshot"):
        if set(definition) - {"snapshot", "group_by"}: raise ValueError("snapshot queries may only change grouping")
        return
    start, end = instant(definition.get("start")), instant(definition.get("end"))
    if not 0 < end - start <= policy.max_window_days * 86400 * 1000000:
        raise ValueError("query time window exceeds policy")
    sequence = definition.get("sequence", [])
    if not isinstance(sequence, list) or len(sequence) > policy.max_sequence or any(not isinstance(e, str) or not e for e in sequence):
        raise ValueError("invalid event sequence")
    gap = definition.get("max_gap_seconds", policy.max_window_days * 86400)
    if type(gap) is not int or gap < 1 or gap > policy.max_window_days * 86400:
        raise ValueError("invalid sequence gap")
    visited = 0
    def check(node, depth=0):
        nonlocal visited
        visited += 1
        if visited > 80 or depth > 10 or not isinstance(node, dict): raise ValueError("filter complexity limit")
        branches = set(node) & {"all", "any", "not"}
        if branches:
            if len(branches) != 1 or len(node) != 1: raise ValueError("ambiguous filter")
            branch = next(iter(branches))
            children = [node[branch]] if branch == "not" else node[branch]
            if not isinstance(children, list) or not children: raise ValueError("empty filter")
            for child in children: check(child, depth + 1)
        else:
            if set(node) != {"field", "op", "value"} or node["field"] not in policy.filter_fields or node["op"] not in ("eq", "in", "gte", "lt"):
                raise ValueError("filter is not permitted")
            if node["op"] == "in" and (not isinstance(node["value"], list) or len(node["value"]) > 100):
                raise ValueError("membership filter limit")
    if "where" in definition: check(definition["where"])


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


def sequence_matches(events, sequence, max_gap):
    """Reverse stage updates prohibit one event satisfying multiple stages.

    Latest previous-stage timestamps dominate older timestamps for a consecutive
    gap constraint. This retains later alternative starts after an early expiry.
    """
    if not sequence: return True
    latest = [None] * len(sequence)
    for at, event_type in events:
        for stage in range(len(sequence) - 1, -1, -1):
            if event_type != sequence[stage]: continue
            if stage == 0 or (latest[stage - 1] is not None and at - latest[stage - 1] <= max_gap):
                latest[stage] = at
                if stage == len(sequence) - 1: return True
    return False


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
