"""Exact quota-constrained selection.

Two exact methods are provided behind one entry point, never an approximation:

1. :func:`enumerate_solve` exhaustively lists every N-subset and independently
   checks both quota sides, the global cost and ties. It is the reference
   oracle used whenever ``choose(m, n)`` fits the declared enumeration limit.

2. :func:`min_cost_flow_solve` builds a bipartite lower-bound flow network.

Why per-category inventory comparison is not enough
---------------------------------------------------
A subject is *one* node attached to one category on each side. Demanding two
east members and one old member can be individually satisfiable while no joint
set exists (east/young, east/young, west/old with N=2). The flow network makes
the coupling explicit::

    S -> region category c        lower/upper = region quota [lo, hi]
    c -> subject i                 [0, 1]     (i carries c on the region side)
    subject i -> age category d    [0, 1]     cost = deterministic weight of i
    d -> T                         [lo, hi] = age quota
    T -> S                         [N, N] fixed return edge

Every unit of flow is a subject selected exactly once: it enters through the
subject's region node and leaves through its age node. Lower bounds are handled
by the standard circulation transformation (each edge keeps its lower bound;
node balances are fed by a super source/sink). Phase 1 routes every forced unit
via SPFA to prove feasibility - failure yields a min-cut contention
certificate. Phase 2 cancels exact Bellman-Ford negative cycles over the core
network: the residual reverse of the saturated [N,N] return edge (an S -> T
edge of capacity N) closes each improving swap while keeping the circulating
total pinned at N, so a cycle always deselects as many subjects as it selects.
The minimum total subject cost results, with the lexicographic tie break
encoded in the integer weights.

All limits are declared up front. Exceeding them raises
:class:`PlanCapacityError`, which is distinct from :class:`PlanInfeasible` and
never causes silent approximation.
"""

from collections import deque
from itertools import combinations

from .quota import PlanCapacityError, PlanInfeasible, inventory


# ---------------------------------------------------------------------------
# Residual network primitives
# ---------------------------------------------------------------------------

class _Edge:
    __slots__ = ("tail", "head", "cap", "cost", "lower", "role", "rev")

    def __init__(self, tail, head, cap, cost, lower, role):
        self.tail = tail
        self.head = head
        self.cap = cap
        self.cost = cost
        self.lower = lower
        self.role = role  # ("src", label) | ("pair", sid) | ("sink", label) | ("return",) | ("aux",)
        self.rev = -1


class _Network:
    def __init__(self):
        self.nodes = []
        self.edges = []
        self.original_edge_count = 0
        self.adjacency = []

    def add_node(self, role):
        idx = len(self.nodes)
        self.nodes.append(role)
        self.adjacency.append([])
        return idx

    def add_edge(self, tail, head, lower, upper, cost, role):
        if lower > upper:
            raise PlanInfeasible("edge lower bound above upper bound", {"reason": "contradictory_bounds"})
        forward = _Edge(tail, head, upper - lower, cost, lower, role)
        backward = _Edge(head, tail, 0, -cost, 0, ("reverse",))
        forward_index = len(self.edges)
        forward.rev = forward_index + 1
        backward.rev = forward_index
        self.edges.append(forward)
        self.edges.append(backward)
        self.adjacency[tail].append(forward_index)
        self.adjacency[head].append(forward_index + 1)
        if role[0] not in ("reverse", "aux"):
            self.original_edge_count += 1
        return forward


def _spfa_shortest_paths(net, source, relax_budget, budget_holder):
    """SPFA distances/predecessors over adjacency lists. Negative costs OK."""
    n = len(net.nodes)
    inf = float("inf")
    dist = [inf] * n
    in_queue = [False] * n
    dist[source] = 0
    queue = deque([source])
    in_queue[source] = True
    while queue:
        u = queue.popleft()
        in_queue[u] = False
        for ei in net.adjacency[u]:
            edge = net.edges[ei]
            if edge.cap <= 0:
                continue
            budget_holder["relax"] += 1
            if budget_holder["relax"] > relax_budget:
                raise PlanCapacityError("flow computation exceeded relaxation budget", budget_holder["limits"])
            v = edge.head
            nd = dist[u] + edge.cost
            if nd < dist[v]:
                dist[v] = nd
                budget_holder["pred"][v] = ei
                if not in_queue[v]:
                    queue.append(v)
                    in_queue[v] = True
    return dist


def _negative_cycle_bellman_ford(net, limits, budget_holder):
    """Exact residual negative-cycle detection by Bellman-Ford.

    A virtual zero-cost source reaches every node (all distances start at 0).
    After ``V`` relaxation passes any edge that can still be tightened reaches
    a node on a negative cycle; the predecessor chain is walked back onto that
    cycle. The returned edges form a validated closed directed walk, all
    residual, with strictly negative total cost. Only list/dict bookkeeping is
    used, never hash-seeded containers.
    """
    n = len(net.nodes)
    dist = [0] * n
    pred = [-1] * n
    relaxed_any = False
    for _ in range(n):
        relaxed_any = False
        for edge_index, edge in enumerate(net.edges):
            if edge.cap <= 0:
                continue
            budget_holder["relax"] += 1
            if budget_holder["relax"] > limits["flow_steps_budget"]:
                raise PlanCapacityError("flow computation exceeded relaxation budget",
                                        limits)
            head = edge.head
            candidate = dist[edge.tail] + edge.cost
            if candidate < dist[head]:
                dist[head] = candidate
                pred[head] = edge_index
                relaxed_any = True
        if not relaxed_any:
            return None
    # A tighten-able edge remains; find a node guaranteed to sit on a cycle.
    on_cycle = -1
    for edge_index, edge in enumerate(net.edges):
        if edge.cap > 0 and dist[edge.tail] + edge.cost < dist[edge.head]:
            on_cycle = edge.head
            break
    if on_cycle < 0:
        return None
    for _ in range(n):
        on_cycle = net.edges[pred[on_cycle]].tail
    # Walk predecessors from the on-cycle node: pred[v] is the edge entering v,
    # so the collected edges run in reverse traversal order. Walking once around
    # returns to the start; reverse the list to get forward directed order.
    reverse_order = []
    node = on_cycle
    while True:
        edge_index = pred[node]
        reverse_order.append(edge_index)
        node = net.edges[edge_index].tail
        if node == on_cycle:
            break
    cycle = list(reversed(reverse_order))
    # Defence in depth: validate closure, residual capacity and negative cost.
    if not all(net.edges[i].cap > 0 for i in cycle):
        raise RuntimeError("internal flow error: cycle uses a saturated edge")
    if sum(net.edges[i].cost for i in cycle) >= 0:
        raise RuntimeError("internal flow error: detected cycle is not negative")
    for i, edge_index in enumerate(cycle):
        edge = net.edges[edge_index]
        nxt = net.edges[cycle[(i + 1) % len(cycle)]]
        if edge.head != nxt.tail:
            raise RuntimeError("internal flow error: cycle is not a closed walk")
    return cycle


def _augment_along_edges(net, edge_indices):
    pushed = min(net.edges[i].cap for i in edge_indices)
    if pushed <= 0:
        raise RuntimeError("internal flow error: augmenting saturated edge")
    for i in edge_indices:
        edge = net.edges[i]
        edge.cap -= pushed
        net.edges[edge.rev].cap += pushed
    return pushed


# ---------------------------------------------------------------------------
# Min-cost lower-bound circulation
# ---------------------------------------------------------------------------

def _build_network(plan, weights):
    dim0, dim1 = plan.dimensions
    net = _Network()
    s = net.add_node(("terminal", "S"))
    t = net.add_node(("terminal", "T"))
    cat0, cat1 = {}, {}
    for label in sorted(plan.counts[dim0]):
        cat0[label] = net.add_node(("category", dim0, label))
    for label in sorted(plan.counts[dim1]):
        cat1[label] = net.add_node(("category", dim1, label))
    subject_node = {}
    for sid in plan.member_ids:
        subject_node[sid] = net.add_node(("subject", sid))

    for label, lower, upper in plan.quotas[dim0]:
        net.add_edge(s, cat0[label], lower, upper, 0, ("src", dim0, label))
    # Implicit categories (present in data, absent from quota): [0, N].
    for label, node in sorted(cat0.items()):
        if label not in {k for k, _, _ in plan.quotas[dim0]}:
            net.add_edge(s, node, 0, plan.n, 0, ("src", dim0, label))
    for label, node in sorted(cat1.items()):
        if label not in {k for k, _, _ in plan.quotas[dim1]}:
            net.add_edge(node, t, 0, plan.n, 0, ("sink", dim1, label))
    for label, lower, upper in plan.quotas[dim1]:
        net.add_edge(cat1[label], t, lower, upper, 0, ("sink", dim1, label))

    for sid in plan.member_ids:
        label0, label1 = plan.member_categories[sid]
        c0, node, c1 = cat0[label0], subject_node[sid], cat1[label1]
        net.add_edge(c0, node, 0, 1, 0, ("pair0", sid))
        net.add_edge(node, c1, 0, 1, weights[sid], ("pair1", sid))
    # The [N, N] return edge pins total circulation at N. Its lower bound makes
    # S a deficit node and T a surplus node, so the super-source/super-sink
    # balancing flow has no bypass: it must cross real category and subject
    # edges while honouring every fixed lower bound.
    net.add_edge(t, s, plan.n, plan.n, 0, ("return",))
    return net, s, t, subject_node


def _contention_evidence(net, ss, supply, routed, plan):
    """Min-cut witness from the residual reachable set (aggregate only)."""
    reachable = {ss}
    queue = deque([ss])
    while queue:
        u = queue.popleft()
        for edge in net.edges:
            if edge.tail == u and edge.cap > 0 and edge.head not in reachable:
                reachable.add(edge.head)
                queue.append(edge.head)
    cut_capacity = 0
    for edge in net.edges:
        if edge.role[0] == "aux" and edge.tail in reachable and edge.head not in reachable:
            cut_capacity += edge.cap
    reachable_roles = {"categories": {}, "subject_pairs": {}}
    for idx in reachable:
        role = net.nodes[idx]
        if role[0] == "category":
            reachable_roles["categories"].setdefault(role[1], []).append(role[2])
        elif role[0] == "subject":
            sid = role[1]
            pair = plan.member_categories[sid]
            key = f"{pair[0]}|{pair[1]}"
            reachable_roles["subject_pairs"][key] = reachable_roles["subject_pairs"].get(key, 0) + 1
    for dim in reachable_roles["categories"]:
        reachable_roles["categories"][dim].sort()
    reachable_roles["subject_pairs"] = dict(sorted(reachable_roles["subject_pairs"].items()))
    evidence = {
        "reason": "joint_quota_contention",
        "forced_supply": supply,
        "routed": routed,
        "cut_capacity": cut_capacity,
        "reachable_side": reachable_roles,
        "inventory": inventory(plan),
    }
    return evidence


def min_cost_flow_solve(plan, weights, limits):
    """Exact lower-bound min-cost circulation selection.

    ``weights[sid]`` is a deterministic integer (may be negative) combining the
    stable primary cost and the lexicographic tie break.
    """
    net, s, t, subject_node = _build_network(plan, weights)
    original_edges = net.original_edge_count
    node_count = len(net.nodes)
    if node_count > limits["max_plan_nodes"] or original_edges > limits["max_plan_edges"]:
        raise PlanCapacityError(
            f"flow network needs {node_count} nodes/{original_edges} edges", limits)

    # Lower-bound circulation transformation.
    balance = [0] * node_count
    for edge in net.edges:
        if edge.role[0] in ("reverse", "aux"):
            continue
        balance[edge.tail] -= edge.lower
        balance[edge.head] += edge.lower
    ss = net.add_node(("aux", "SS"))
    tt = net.add_node(("aux", "TT"))
    supply = 0
    for idx, value in enumerate(balance):
        if value > 0:
            net.add_edge(ss, idx, 0, value, 0, ("aux",))
            supply += value
        elif value < 0:
            net.add_edge(idx, tt, 0, -value, 0, ("aux",))

    budget = {"relax": 0, "pred": [-1] * len(net.nodes), "limits": limits}

    def spfa(source):
        budget["pred"] = [-1] * len(net.nodes)
        return _spfa_shortest_paths(net, source, limits["flow_steps_budget"], budget)

    # Phase 1: satisfy every lower bound (max flow SS -> TT).
    routed = 0
    while routed < supply:
        dist = spfa(ss)
        if dist[tt] == float("inf"):
            evidence = _contention_evidence(net, ss, supply, routed, plan)
            raise PlanInfeasible(
                f"joint quotas are infeasible: forced {supply} units but cut admits {evidence['cut_capacity']} more",
                evidence)
        path = []
        node = tt
        while node != ss:
            ei = budget["pred"][node]
            if ei < 0:
                raise RuntimeError("internal flow error: broken predecessor chain")
            path.append(ei)
            node = net.edges[ei].tail
        routed += _augment_along_edges(net, path)

    # Phase 2: min-cost circulation by exact negative-cycle cancellation over
    # the core network. The fixed [N,N] return edge is saturated, but its
    # residual reverse edge (S -> T, capacity N) lets a closed cycle route from
    # S back to T after swapping subjects. Because that reverse edge moves the
    # already-fixed N units backwards, every accepted cycle preserves node
    # conservation and the total selection size: each cycle deselects as many
    # subjects through reverse pair edges as it selects through forward ones.
    # No extra return edge is added, so total flow can never drift. Bellman-Ford
    # (not the SPFA enqueue heuristic) detects the cycles under a declared cap.
    for _ in range(limits["max_cycle_rounds"]):
        cycle = _negative_cycle_bellman_ford(net, limits, budget)
        if cycle is None:
            break
        _augment_along_edges(net, cycle)
    else:
        raise PlanCapacityError("negative-cycle cancellation exceeded round limit",
                                limits)

    selected = []
    for edge in net.edges:
        if edge.role[0] == "pair0":
            sid = edge.role[1]
            # A subject is selected iff its region -> subject entry edge is
            # saturated. The two per-subject edges are serial; tracking pair1
            # instead would misread an exchange that only reversed one edge.
            if edge.cap == 0:
                selected.append(sid)
    selected.sort()
    _verify_selection(plan, selected)
    return selected


def _verify_selection(plan, selected):
    """Defensive independent check of the flow result."""
    if len(selected) != plan.n or len(set(selected)) != plan.n:
        raise RuntimeError("internal flow error: selection size or uniqueness violated")
    allowed = set(plan.member_ids)
    if any(sid not in allowed for sid in selected):
        raise RuntimeError("internal flow error: unknown subject selected")
    chosen = set(selected)
    for dim in plan.dimensions:
        used = {}
        for sid in selected:
            label = plan.member_categories[sid][plan.dimensions.index(dim)]
            used[label] = used.get(label, 0) + 1
        bounds = {key: (lower, upper) for key, lower, upper in plan.quotas[dim]}
        for label, count in used.items():
            lower, upper = bounds.get(label, (0, plan.n))
            if not lower <= count <= upper:
                raise RuntimeError("internal flow error: quota bounds violated")
        for label, lower, _ in plan.quotas[dim]:
            if used.get(label, 0) < lower:
                raise RuntimeError("internal flow error: lower bound violated")


# ---------------------------------------------------------------------------
# Exhaustive reference oracle
# ---------------------------------------------------------------------------

def _feasible_subset(plan, subset):
    chosen = set(subset)
    for dim_index, dim in enumerate(plan.dimensions):
        used = {}
        for sid in subset:
            label = plan.member_categories[sid][dim_index]
            used[label] = used.get(label, 0) + 1
        bounds = {key: (lower, upper) for key, lower, upper in plan.quotas[dim]}
        for label, count in used.items():
            lower, upper = bounds.get(label, (0, plan.n))
            if not lower <= count <= upper:
                return False
        for label, lower, _ in plan.quotas[dim]:
            if used.get(label, 0) < lower:
                return False
    return True


def enumerate_solve(plan, primary_costs, limits):
    """Exhaustively evaluate every N-subset. Independent of the flow code."""
    combos = _combination_count(len(plan.member_ids), plan.n)
    if combos > limits["enumeration_limit"]:
        return None
    best = None  # (primary_sum, sorted_id_tuple)
    inspected = 0
    for subset in combinations(sorted(plan.member_ids), plan.n):
        inspected += 1
        if inspected > limits["enumeration_limit"]:
            raise PlanCapacityError("enumeration exceeded declared subset limit", limits)
        if not _feasible_subset(plan, subset):
            continue
        candidate = (sum(primary_costs[sid] for sid in subset), tuple(subset))
        if best is None or candidate < best:
            best = candidate
    if best is None:
        # Build the same rigorous certificate via the flow network rather than
        # a weaker "nothing matched" message.
        raise PlanInfeasible("joint quotas admit no N-subset", {
            "reason": "joint_quota_contention",
            "subsets_inspected": inspected,
            "inventory": inventory(plan)})
    return list(best[1])


def _combination_count(m, n):
    if n > m:
        return 0
    n = min(n, m - n)
    result = 1
    for i in range(1, n + 1):
        result = result * (m - n + i) // i
    return result


def solve(plan, primary_costs, weights, limits):
    """Dispatch to the exact method appropriate for the declared limits."""
    m = len(plan.member_ids)
    if m > limits["max_plan_members"]:
        raise PlanCapacityError(f"frozen population of {m} exceeds member limit", limits)
    if _combination_count(m, plan.n) <= limits["enumeration_limit"]:
        found = enumerate_solve(plan, primary_costs, limits)
        if found is not None:
            return found, "enumeration"
    return min_cost_flow_solve(plan, weights, limits), "min_cost_flow"
