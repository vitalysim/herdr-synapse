"""Ranking for the layered layout: cycle breaking and network simplex (a port of dagre's, MIT).

``acyclic`` picks the edges to reverse: the lighter of the depth-first walk's
back edges in input order (dagre's default ``dfsFAS``) and the greedy feedback
arc set of Eades, Lin and Smyth (``greedyFAS``), the walk's on a tie.
``rank`` gives every node a rank so that each edge ``a -> b`` has
``rank(b) - rank(a) >= minlen``, first by longest path, then tightened by
network simplex (Gansner et al., as dagre's ``networkSimplex``: a feasible
tight tree, cut values, and edge exchanges until no cut value is negative),
which minimises the total weighted edge length. Each connected component is
ranked on its own and starts at rank 0.

Pure; nodes are hashable keys, edges ``(a, b, minlen, weight)``.
"""
from __future__ import annotations

import math

from typing import Dict, Hashable, List, Sequence, Set, Tuple

Key = Hashable
REdge = Tuple[Key, Key, int, float]  # (tail, head, minlen, weight)

#: Network simplex stops after this many exchanges (a safety net; real graphs need far fewer).
MAX_EXCHANGES = 5000


def acyclic(nodes: Sequence[Key], edges: Sequence[Tuple[Key, Key, float]]) -> Set[int]:
    """The indexes of the edges to reverse so the graph has no cycle (self-loops are never reversed).

    Two candidates: the back edges of a depth-first walk in input order (dagre's default ``dfsFAS``: an agent lists
    a flow in its order, so a retry edge back to the start is the one that turns), and the greedy feedback arc set
    (``greedy``). The lighter set wins; a tie keeps the walk's, which follows the input."""
    walk = dfs_fas(nodes, edges)
    greedy_set = greedy(nodes, edges)

    def weight(found: Set[int]) -> float:
        return math.fsum(max(float(edges[i][2]), 1e-6) for i in found)

    return greedy_set if weight(greedy_set) < weight(walk) - 1e-9 else walk


def dfs_fas(nodes: Sequence[Key], edges: Sequence[Tuple[Key, Key, float]]) -> Set[int]:
    """The back edges of a depth-first walk from each node in input order, children in edge order."""
    out_edges: Dict[Key, List[int]] = {n: [] for n in nodes}
    for i, (a, b, _w) in enumerate(edges):
        if a != b:
            out_edges[a].append(i)
    state: Dict[Key, int] = {}
    back: Set[int] = set()
    for root in nodes:
        if root in state:
            continue
        state[root] = 1
        stack: List[Tuple[Key, int]] = [(root, 0)]
        while stack:
            node, k = stack[-1]
            if k >= len(out_edges[node]):
                state[node] = 2
                stack.pop()
                continue
            stack[-1] = (node, k + 1)
            i = out_edges[node][k]
            child = edges[i][1]
            seen = state.get(child)
            if seen == 1:
                back.add(i)
            elif seen is None:
                state[child] = 1
                stack.append((child, 0))
    return back


def greedy(nodes: Sequence[Key], edges: Sequence[Tuple[Key, Key, float]]) -> Set[int]:
    """The greedy feedback arc set of Eades, Lin and Smyth (dagre's ``greedyFAS``), ties broken by input order."""
    index = {n: i for i, n in enumerate(nodes)}
    out_w: Dict[Key, float] = {n: 0.0 for n in nodes}
    in_w: Dict[Key, float] = {n: 0.0 for n in nodes}
    succ: Dict[Key, List[Tuple[Key, float]]] = {n: [] for n in nodes}
    pred: Dict[Key, List[Tuple[Key, float]]] = {n: [] for n in nodes}
    for a, b, w in edges:
        if a == b:
            continue
        weight = max(float(w), 1e-6)
        out_w[a] += weight
        in_w[b] += weight
        succ[a].append((b, weight))
        pred[b].append((a, weight))
    alive = set(nodes)
    left: List[Key] = []
    right: List[Key] = []

    def remove(n: Key) -> None:
        alive.discard(n)
        for b, w in succ[n]:
            in_w[b] -= w
        for a, w in pred[n]:
            out_w[a] -= w

    eps = 1e-9
    while alive:
        progressed = True
        while progressed:
            progressed = False
            sinks = sorted((n for n in alive if out_w[n] <= eps), key=lambda n: index[n])
            for n in sinks:
                if n in alive and out_w[n] <= eps:
                    right.append(n)
                    remove(n)
                    progressed = True
            sources = sorted((n for n in alive if in_w[n] <= eps), key=lambda n: index[n])
            for n in sources:
                if n in alive and in_w[n] <= eps:
                    left.append(n)
                    remove(n)
                    progressed = True
        if alive:
            best = max(alive, key=lambda n: (out_w[n] - in_w[n], -index[n]))
            left.append(best)
            remove(best)
    sequence = left + right[::-1]
    place = {n: i for i, n in enumerate(sequence)}
    return {i for i, (a, b, _w) in enumerate(edges) if a != b and place[a] > place[b]}


def _components(nodes: Sequence[Key], edges: Sequence[REdge]) -> List[List[Key]]:
    parent = {n: n for n in nodes}

    def find(n: Key) -> Key:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for a, b, _m, _w in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    groups: Dict[Key, List[Key]] = {}
    for n in nodes:
        groups.setdefault(find(n), []).append(n)
    return list(groups.values())


def rank(nodes: Sequence[Key], edges: Sequence[REdge], simplex: bool = True) -> Dict[Key, int]:
    """Ranks for an acyclic graph (edges point from lower to higher rank), each component from 0."""
    merged: Dict[Tuple[Key, Key], List[float]] = {}
    for a, b, minlen, weight in edges:
        if a == b:
            continue
        found = merged.get((a, b))
        if found is None:
            merged[(a, b)] = [float(minlen), float(weight)]
        else:
            found[0] = max(found[0], float(minlen))
            found[1] += float(weight)
    simple = [(a, b, int(v[0]), v[1]) for (a, b), v in merged.items()]
    out: Dict[Key, int] = {}
    for component in _components(nodes, simple):
        members = set(component)
        local = [e for e in simple if e[0] in members]
        ranks = _longest_path(component, local)
        if simplex and len(component) > 2 and local:
            ranks = _network_simplex(component, local, ranks)
        low = min(ranks.values())
        for n in component:
            out[n] = ranks[n] - low
    return out


def _longest_path(nodes: Sequence[Key], edges: Sequence[REdge]) -> Dict[Key, int]:
    """dagre's ``longestPath``: sinks at 0, every other node as low as its successors allow (ranks <= 0)."""
    succ: Dict[Key, List[Tuple[Key, int]]] = {n: [] for n in nodes}
    for a, b, minlen, _w in edges:
        succ[a].append((b, minlen))
    ranks: Dict[Key, int] = {}
    for start in nodes:
        if start in ranks:
            continue
        stack: List[Tuple[Key, int]] = [(start, 0)]
        visiting = {start}
        while stack:
            node, i = stack[-1]
            children = succ[node]
            if i < len(children):
                stack[-1] = (node, i + 1)
                child = children[i][0]
                if child not in ranks and child not in visiting:
                    visiting.add(child)
                    stack.append((child, 0))
                continue
            stack.pop()
            ranks[node] = min((ranks[c] - m for c, m in children if c in ranks), default=0)
    return ranks


def _network_simplex(nodes: Sequence[Key], edges: Sequence[REdge], ranks: Dict[Key, int]) -> Dict[Key, int]:
    ranks = dict(ranks)
    index = {n: i for i, n in enumerate(nodes)}
    incident: Dict[Key, List[int]] = {n: [] for n in nodes}
    for i, (a, b, _m, _w) in enumerate(edges):
        incident[a].append(i)
        incident[b].append(i)

    def slack(i: int) -> int:
        a, b, minlen, _w = edges[i]
        return ranks[b] - ranks[a] - minlen

    # A feasible tight tree (dagre's feasibleTree).
    in_tree: Set[Key] = {nodes[0]}
    tree_edges: Set[int] = set()
    while True:
        stack = sorted(in_tree, key=lambda n: index[n])
        while stack:
            v = stack.pop()
            for i in incident[v]:
                a, b = edges[i][0], edges[i][1]
                w = b if a == v else a
                if w not in in_tree and slack(i) == 0:
                    in_tree.add(w)
                    tree_edges.add(i)
                    stack.append(w)
        if len(in_tree) == len(nodes):
            break
        best, best_slack = -1, None
        for i, (a, b, _m, _w) in enumerate(edges):
            if (a in in_tree) != (b in in_tree):
                s = slack(i)
                if best_slack is None or s < best_slack:
                    best, best_slack = i, s
        a, b = edges[best][0], edges[best][1]
        delta = best_slack if a in in_tree else -best_slack  # type: ignore[operator]
        for n in in_tree:
            ranks[n] += delta
    tree_adj: Dict[Key, List[Tuple[Key, int]]] = {}
    low: Dict[Key, int] = {}
    lim: Dict[Key, int] = {}
    parent: Dict[Key, Tuple[Key, int]] = {}
    cut: Dict[int, float] = {}
    root = nodes[0]

    def build_adj() -> None:
        tree_adj.clear()
        for n in nodes:
            tree_adj[n] = []
        for i in sorted(tree_edges):
            a, b = edges[i][0], edges[i][1]
            tree_adj[a].append((b, i))
            tree_adj[b].append((a, i))

    def low_lim() -> List[Key]:
        """Postorder numbers (dagre's ``initLowLimValues``); returns the postorder."""
        low.clear()
        lim.clear()
        parent.clear()
        order: List[Key] = []
        next_lim = 1
        stack: List[Tuple[Key, int, int]] = [(root, 0, next_lim)]
        seen = {root}
        while stack:
            v, i, start = stack[-1]
            nbrs = tree_adj[v]
            if i < len(nbrs):
                stack[-1] = (v, i + 1, start)
                w, e = nbrs[i]
                if w not in seen:
                    seen.add(w)
                    parent[w] = (v, e)
                    stack.append((w, 0, next_lim))
                continue
            stack.pop()
            low[v] = start
            lim[v] = next_lim
            next_lim += 1
            order.append(v)
        return order

    def cut_values(post: List[Key]) -> None:
        cut.clear()
        for child in post[:-1]:
            par, tree_edge = parent[child]
            a, _b, _m, weight = edges[tree_edge]
            child_is_tail = a == child
            value = weight
            for i in incident[child]:
                ea, eb, _em, ew = edges[i]
                if i == tree_edge:
                    continue
                is_out = ea == child
                other = eb if is_out else ea
                points_to_head = is_out == child_is_tail
                value += ew if points_to_head else -ew
                if i in tree_edges:
                    value += -cut[i] if points_to_head else cut[i]
            cut[tree_edge] = value

    def descendant(v: Key, of: Key) -> bool:
        return low[of] <= lim[v] <= lim[of]

    def update_ranks() -> None:
        stack = [root]
        seen = {root}
        while stack:
            v = stack.pop()
            for w, i in tree_adj[v]:
                if w in seen:
                    continue
                seen.add(w)
                a, _b, minlen, _w = edges[i]
                ranks[w] = ranks[v] - minlen if a == w else ranks[v] + minlen
                stack.append(w)

    build_adj()
    post = low_lim()
    cut_values(post)
    for _round in range(MAX_EXCHANGES):
        leaving = next((i for i in sorted(tree_edges) if cut[i] < 0), None)
        if leaving is None:
            break
        a, b = edges[leaving][0], edges[leaving][1]
        # The subtree side: the end deeper in the tree (the child of the tree edge).
        tail_side, flip = (a, False) if lim[a] < lim[b] else (b, True)
        best, best_slack = -1, None
        for i, (ea, eb, _m, _w) in enumerate(edges):
            if flip == descendant(ea, tail_side) and flip != descendant(eb, tail_side):
                s = slack(i)
                if best_slack is None or s < best_slack:
                    best, best_slack = i, s
        if best < 0:
            break
        tree_edges.discard(leaving)
        tree_edges.add(best)
        build_adj()
        post = low_lim()
        cut_values(post)
        update_ranks()
    return ranks
