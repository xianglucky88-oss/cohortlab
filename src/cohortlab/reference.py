"""Reference exhaustive interpreter for temporal cohort expressions.

This module deliberately implements the semantics independently from the
bounded state machine in :mod:`cohortlab.query`: it enumerates every
strictly-ordered selection of consumed events and checks all constraints
directly. On small event streams it serves as an oracle in property tests.

Sharing with the production executor is limited to the normalized AST shape
and :func:`cohortlab.query.matches`, the leaf predicate evaluator, so a bug in
the frontier bookkeeping cannot be mirrored here.

Events are tuples ``(index, type, at_us, attributes)`` ordered by
``(at, event id)``; the index gives a total order for same-instant events.
"""

from .query import matches


def _step_ok(step, event):
    return event[1] == step["type"] and (step["where"] is None or matches(step["where"], event[3]))


def _negative_ok(negatives, left_event, right_event, events):
    """No negative event may fall in the open interval (left, right).

    Both boundaries are strictly exclusive; same-instant ordering is decided by
    the total event index, so an event at the same instant but a higher id does
    invalidate an unclosed interval.
    """
    left_i, right_i = left_event[0], right_event[0]
    for event in events:
        if not (left_i < event[0] < right_i): continue
        for node in negatives:
            if event[1] == node["type"] and (node["where"] is None or matches(node["where"], event[3])):
                return False
    return True


def witnesses(events, normalized):
    """Return every valid consumed-event index list (empty list when the
    expression has no positive steps). A non-empty result means a match."""
    seq = normalized["sequence"]
    steps, negatives = seq["steps"], seq["not"]
    gap_us, total_us = seq["max_gap_us"], seq["max_total_us"]
    n = len(steps)
    if n == 0:
        return [[]]

    found = []

    def extend(step_index, chosen, start_at):
        if step_index == n:
            found.append([event[0] for event in chosen])
            return
        step = steps[step_index]
        if step["optional"]:
            # Skipping an optional step invents no event and does not touch timers.
            extend(step_index + 1, chosen, start_at)
        lower = chosen[-1][0] if chosen else -1
        for event in events:
            if event[0] <= lower or not _step_ok(step, event): continue
            if chosen and (event[2] - chosen[-1][2] > gap_us): continue
            if start_at is not None and total_us is not None and event[2] - start_at > total_us: continue
            if chosen and negatives and not _negative_ok(negatives, chosen[-1], event, events): continue
            extend(step_index + 1, chosen + [event], start_at if start_at is not None else event[2])

    extend(0, [], None)
    return found


def reference_matches(events, normalized):
    return bool(witnesses(events, normalized))
