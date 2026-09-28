"""Coordinates for the layered layout: Brandes-Koepf along the ranks (a port of dagre's ``positionX``, MIT), then the
group and separation constraints, and the ranks' own coordinates.

Everything here works in the ``down`` frame: ``x`` runs along a rank, ``y``
from rank to rank, and every item has an along-size ``w`` and a rank-size ``h``.

``brandes_koepf`` gives the centre of every item: four alignments (up or
down, leftmost or rightmost median), each compacted, aligned to the narrowest,
and balanced by the average of the two middle values. Long edges (chains of
dummies) stay straight because their inner segments win every conflict.

``constrain`` turns wanted left edges into final ones in one pass over a
constraint graph: neighbours in a rank keep their separation, and every group
gets a shared left and right marker, so a group's box (its members plus pad)
never holds another rank's item. It only pushes to the right, so wanted
positions that already satisfy everything are kept exactly (the fixed point).

Pure.
"""
from __future__ import annotations

import heapq
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

Adj = Mapping[str, Sequence[Tuple[str, float]]]
Layers = Sequence[Sequence[str]]
Sep = Callable[[str, str], float]  # (left item, right item) -> least distance between their centres


def brandes_koepf(layers: Layers, up: Adj, down: Adj, width: Mapping[str, float], dummy: Mapping[str, bool], sep: Sep,
                  border_side: Mapping[str, str]) -> Dict[str, float]:
    """The centre of every item along its rank."""
    conflicts = _type1_conflicts(layers, up, dummy)
    xss: Dict[str, Dict[str, float]] = {}
    for vert in ("u", "d"):
        base = [list(layer) for layer in layers] if vert == "u" else [list(layer) for layer in reversed(layers)]
        adj = up if vert == "u" else down
        for horiz in ("l", "r"):
            layering = base if horiz == "l" else [list(reversed(layer)) for layer in base]
            root, align = _vertical_alignment(layering, conflicts, adj)
            xs = _compaction(layering, root, align, sep, horiz == "r", border_side)
            if horiz == "r":
                xs = {v: -x for v, x in xs.items()}
            xss[vert + horiz] = xs
    smallest = min(("ul", "ur", "dl", "dr"), key=lambda name: _width_of(xss[name], width))
    _align_to(xss, smallest)
    out: Dict[str, float] = {}
    for v in xss["ul"]:
        values = sorted(xss[name][v] for name in ("ul", "ur", "dl", "dr"))
        out[v] = (values[1] + values[2]) / 2.0
    return out


def _type1_conflicts(layers: Layers, up: Adj, dummy: Mapping[str, bool]) -> Set[Tuple[str, str]]:
    """Non-inner segments that cross an inner segment (between two dummies): they lose alignment."""
    conflicts: Set[Tuple[str, str]] = set()
    for r in range(1, len(layers)):
        prev, layer = layers[r - 1], layers[r]
        pos_prev = {k: i for i, k in enumerate(prev)}
        k0, scan = 0, 0
        last = layer[-1] if layer else None
        for i, v in enumerate(layer):
            w = None
            if dummy.get(v):
                w = next((u for u, _ in up.get(v, ()) if dummy.get(u) and u in pos_prev), None)
            k1 = pos_prev[w] if w is not None else len(prev)
            if w is not None or v == last:
                for scan_node in layer[scan:i + 1]:
                    for u, _wt in up.get(scan_node, ()):
                        if u not in pos_prev:
                            continue
                        u_pos = pos_prev[u]
                        if (u_pos < k0 or k1 < u_pos) and not (dummy.get(u) and dummy.get(scan_node)):
                            conflicts.add((min(u, scan_node), max(u, scan_node)))
                scan = i + 1
                k0 = k1
    return conflicts


def _vertical_alignment(layering: Sequence[List[str]], conflicts: Set[Tuple[str, str]], adj: Adj) -> Tuple[Dict[str, str], Dict[str, str]]:
    root: Dict[str, str] = {}
    align: Dict[str, str] = {}
    pos: Dict[str, int] = {}
    for layer in layering:
        for order, v in enumerate(layer):
            root[v] = v
            align[v] = v
            pos[v] = order
    for layer in layering:
        prev_idx = -1
        for v in layer:
            ws = sorted({w for w, _ in adj.get(v, ()) if w in pos}, key=lambda w: pos[w])
            if not ws:
                continue
            mp = (len(ws) - 1) / 2.0
            for i in range(int(mp), int(-(-mp // 1)) + 1):
                w = ws[i]
                if align[v] == v and prev_idx < pos[w] and (min(v, w), max(v, w)) not in conflicts:
                    align[w] = v
                    align[v] = root[v] = root[w]
                    prev_idx = pos[w]
    return root, align


def _compaction(layering: Sequence[List[str]], root: Mapping[str, str], align: Mapping[str, str], sep: Sep, reverse: bool,
                border_side: Mapping[str, str]) -> Dict[str, float]:
    order: List[str] = []
    seen: Set[str] = set()
    edges: Dict[str, Dict[str, float]] = {}
    preds: Dict[str, Dict[str, float]] = {}
    for layer in layering:
        u: Optional[str] = None
        for v in layer:
            rv = root[v]
            if rv not in seen:
                seen.add(rv)
                order.append(rv)
            if u is not None:
                ru = root[u]
                distance = sep(v, u) if reverse else sep(u, v)
                if distance > edges.setdefault(ru, {}).get(rv, float("-inf")):
                    edges[ru][rv] = distance
                    preds.setdefault(rv, {})[ru] = distance
            u = v
    topo = _topological(order, edges)
    xs: Dict[str, float] = {}
    for b in topo:
        xs[b] = max((xs[a] + d for a, d in preds.get(b, {}).items()), default=0.0)
    skip = "left" if reverse else "right"
    for b in reversed(topo):
        outs = edges.get(b, {})
        if outs and border_side.get(b) != skip:
            xs[b] = max(xs[b], min(xs[c] - d for c, d in outs.items()))
    return {v: xs[root[v]] for v in align}


def _topological(order: Sequence[str], edges: Mapping[str, Mapping[str, float]]) -> List[str]:
    index = {v: i for i, v in enumerate(order)}
    indegree = {v: 0 for v in order}
    for a, outs in edges.items():
        for b in outs:
            indegree[b] += 1
    heap = [(index[v], v) for v in order if indegree[v] == 0]
    heapq.heapify(heap)
    out: List[str] = []
    while heap:
        _i, v = heapq.heappop(heap)
        out.append(v)
        for b in edges.get(v, {}):
            indegree[b] -= 1
            if indegree[b] == 0:
                heapq.heappush(heap, (index[b], b))
    if len(out) < len(order):  # a cycle cannot happen in a block graph built from consistent orders; stay total anyway
        out += [v for v in order if v not in set(out)]
    return out


def _width_of(xs: Mapping[str, float], width: Mapping[str, float]) -> float:
    lo = min(x - width[v] / 2.0 for v, x in xs.items())
    hi = max(x + width[v] / 2.0 for v, x in xs.items())
    return hi - lo


def _align_to(xss: Dict[str, Dict[str, float]], name: str) -> None:
    target = xss[name].values()
    lo, hi = min(target), max(target)
    for vert in ("u", "d"):
        for horiz in ("l", "r"):
            key = vert + horiz
            if key == name:
                continue
            values = xss[key].values()
            delta = lo - min(values) if horiz == "l" else hi - max(values)
            if delta:
                xss[key] = {v: x + delta for v, x in xss[key].items()}


# --------------------------------------------------------------------------
# separation and groups


def constrain(layers: Layers, width: Mapping[str, float], wanted: Mapping[str, float], offset: Callable[[str, str], float],
              unify: Mapping[str, str]) -> Dict[str, float]:
    """Final left edges: each item at its wanted left edge or further right, just as far as the separations need.

    ``offset(u, v)`` is the least room between the right edge of ``u`` and the left edge of ``v`` when ``v``
    follows ``u`` in a rank. ``unify`` maps items that must share one coordinate (a group's left borders on
    every rank, and its right borders) to one variable, so a group's sides are straight and its box never
    holds another rank's item. Only pushes right: wanted edges that satisfy everything are kept exactly.
    """
    order: List[str] = []
    seen: Set[str] = set()
    edges: Dict[str, Dict[str, float]] = {}

    def var(key: str) -> str:
        return unify.get(key, key)

    for layer in layers:
        previous: Optional[str] = None
        for v in layer:
            if var(v) not in seen:
                seen.add(var(v))
                order.append(var(v))
            if previous is not None:
                need = width[previous] + offset(previous, v)
                found = edges.setdefault(var(previous), {})
                if need > found.get(var(v), float("-inf")):
                    found[var(v)] = need
            previous = v
    topo = _topological(order, edges)
    preds: Dict[str, List[Tuple[str, float]]] = {}
    for a, outs in edges.items():
        for b, need in outs.items():
            preds.setdefault(b, []).append((a, need))
    wanted_var: Dict[str, float] = {}
    for layer in layers:
        for v in layer:
            if v in wanted:
                wanted_var[var(v)] = max(wanted_var.get(var(v), float("-inf")), wanted[v])
    x: Dict[str, float] = {}
    for v in topo:
        best = wanted_var.get(v, float("-inf"))
        for a, need in preds.get(v, ()):
            best = max(best, x[a] + need)
        x[v] = best
    # What nothing placed from the left (no wanted edge, nothing before it) sits just left of what follows it.
    for v in reversed(topo):
        if x[v] != float("-inf"):
            continue
        outs = [(b, need) for b, need in edges.get(v, {}).items() if x[b] != float("-inf")]
        x[v] = min(x[b] - need for b, need in outs) if outs else 0.0
    return {v: x[var(v)] for layer in layers for v in layer}


def rank_tops(thickness: Sequence[float], between: Sequence[float], wanted: Optional[Sequence[Optional[float]]] = None) -> List[float]:
    """The top of every rank: each ``between[r]`` below the rank before (``between[r]`` sits above rank ``r``),
    or lower when ``wanted`` (an incremental run's seeded tops) asks for it."""
    tops: List[float] = []
    for r, thick in enumerate(thickness):
        floor = tops[r - 1] + thickness[r - 1] + between[r] if r else float("-inf")
        want = wanted[r] if wanted is not None else None
        if want is None:
            tops.append(0.0 if r == 0 else floor)
        else:
            tops.append(max(want, floor))
    return tops
