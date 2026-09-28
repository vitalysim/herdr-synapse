"""Ordering inside ranks for the layered layout: fewer crossings, groups kept together (a port of dagre's ``order``, MIT).

The layered graph is proper (every edge joins neighbouring ranks) and each
item names its innermost group. ``initial`` gives a first order: a depth-first
walk from the sources (dagre's ``initOrder``), or, for an incremental run, the
items' previous coordinates. ``improve`` then sweeps down and up, sorting each
rank by the weighted barycentre of its neighbours in the rank before (dagre's
``sortSubgraph``: a group is sorted as one entry, then its inside, so a group
stays contiguous; sibling groups keep one order across ranks through the
constraint graph of ``resolveConflicts``), followed by a transpose pass that
swaps neighbours while that removes crossings. It keeps the best order by
crossing count (the Barth-Mutzel accumulator tree) and stops after four
sweeps without improvement.

Pure. Keys are strings; ``group_of`` maps a key to its innermost group (None:
top level), ``parent`` maps a group to its parent group, ``borders`` maps a
group to its left and right border key per rank.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from herdr_team.canvas_layouts import _budget

Adj = Mapping[str, Sequence[Tuple[str, float]]]
Layers = List[List[str]]

#: At most this many sweeps (the spec's 24), and stop after this many without improvement.
MAX_SWEEPS = 24
PATIENCE = 12
#: A bigger graph gives up sooner: (most items, patience) steps, the first that holds.
PATIENCE_BY_SIZE = ((400, 12), (1500, 6), (10 ** 9, 3))
#: Transpose passes per sweep, and the most items a rank may have for transpose to run (it is quadratic per rank).
TRANSPOSE_PASSES = 4
TRANSPOSE_MAX_RANK = 400


class Model:
    """What ordering needs to know about the proper layered graph."""

    def __init__(self, layers: Layers, up: Adj, down: Adj, group_of: Mapping[str, Optional[str]], parent: Mapping[str, Optional[str]],
                 borders: Mapping[str, Mapping[int, Tuple[str, str]]], tie: Mapping[str, int],
                 order_lists: Sequence[Sequence[str]] = ()) -> None:
        self.layers = [list(layer) for layer in layers]
        self.up = up
        self.down = down
        self.group_of = group_of
        self.parent = parent
        self.borders = borders
        self.tie = tie
        self.order_lists = [list(members) for members in order_lists if len(members) > 1]
        self.border_keys: Set[str] = {k for per in borders.values() for pair in per.values() for k in pair}
        self.rank_of: Dict[str, int] = {k: r for r, layer in enumerate(self.layers) for k in layer}

    # -- groups ---------------------------------------------------------------------------------------------
    def chain(self, key: str) -> List[Optional[str]]:
        """The item's groups, outermost first, ending with its innermost (``[None]`` at the top level)."""
        out: List[Optional[str]] = []
        g = self.group_of.get(key)
        while g is not None and g not in out:
            out.append(g)
            g = self.parent.get(g)
        return [None] + out[::-1]


def cross_count(layers: Layers, down: Adj) -> float:
    """Weighted crossings between every pair of neighbouring ranks (the Barth-Mutzel accumulator tree)."""
    total = 0.0
    for r in range(len(layers) - 1):
        north, south = layers[r], layers[r + 1]
        if not north or not south:
            continue
        pos = {k: i for i, k in enumerate(south)}
        entries = []
        for k in north:
            row = sorted((pos[w], weight) for w, weight in down.get(k, ()) if w in pos)
            entries.extend(row)
        first = 1
        while first < len(south):
            first <<= 1
        tree = [0.0] * (2 * first - 1)
        first -= 1
        for p, weight in entries:
            index = p + first
            tree[index] += weight
            acc = 0.0
            while index > 0:
                if index % 2:
                    acc += tree[index + 1]
                index = (index - 1) >> 1
                tree[index] += weight
            total += weight * acc
    return total


# --------------------------------------------------------------------------
# initial order


def initial(model: Model, sources_first: Sequence[str], coords: Optional[Mapping[str, float]] = None) -> Layers:
    """The first order: a depth-first walk from ``sources_first`` (dagre's ``initOrder``); with ``coords`` (incremental),
    items sorted by coordinate, the walk's order breaking ties. Groups are then made contiguous (``normalize``)."""
    visited: Set[str] = set()
    walk: Dict[int, List[str]] = {r: [] for r in range(len(model.layers))}
    everything = [k for layer in model.layers for k in layer]
    start = [k for k in sources_first if k in model.rank_of] + sorted(everything, key=lambda k: (model.rank_of[k], model.tie.get(k, 0), k))
    for root in start:
        if root in visited:
            continue
        stack = [root]
        while stack:
            k = stack.pop()
            if k in visited:
                continue
            visited.add(k)
            walk[model.rank_of[k]].append(k)
            children = sorted((w for w, _ in model.down.get(k, ())), key=lambda w: (model.tie.get(w, 0), w))
            for w in reversed(children):
                if w not in visited:
                    stack.append(w)
    layers = [walk[r] for r in range(len(model.layers))]
    if coords:
        index = {k: i for layer in layers for i, k in enumerate(layer)}
        layers = [sorted(layer, key=lambda k: (coords.get(k, float("inf")) if coords.get(k) is not None else float("inf"), index[k]))
                  for layer in layers]
    return reconcile(model, [normalize(model, r, layer) for r, layer in enumerate(layers)])


def reconcile(model: Model, layers: Layers) -> Layers:
    """``layers`` with sibling groups in one order on every rank.

    A group's left borders share one coordinate across its ranks (and so do
    its right borders), so two groups that swap sides between ranks make the
    separation constraints unsatisfiable. Each set of sibling groups gets one
    order (most pairwise wins over the ranks first, then the mean position),
    and every rank takes it in the slots its groups already hold; items keep
    their slots. Layers already consistent come back unchanged.
    """
    wins: Dict[Tuple[str, str], int] = {}
    spot: Dict[str, List[float]] = {}
    for layer in layers:
        seen: Dict[Optional[str], List[str]] = {}
        for i, k in enumerate(layer):
            chain = model.chain(k)
            for parent, child in zip(chain, chain[1:]):
                row = seen.setdefault(parent, [])
                if child not in row:  # type: ignore[operator]
                    row.append(child)  # type: ignore[arg-type]
                    spot.setdefault(child, []).append(i / float(max(1, len(layer))))  # type: ignore[arg-type]
        for row in seen.values():
            for a_i, a in enumerate(row):
                for b in row[a_i + 1:]:
                    wins[(a, b)] = wins.get((a, b), 0) + 1
    if not any((b, a) in wins for (a, b) in wins):
        return layers
    score: Dict[str, float] = {}
    for (a, b), count in wins.items():
        score[a] = score.get(a, 0.0) + count
        score[b] = score.get(b, 0.0) - count
    mean = {g: sum(v) / len(v) for g, v in spot.items()}

    def rank_of(g: str) -> Tuple[float, float, str]:
        return (-score.get(g, 0.0), mean.get(g, 0.0), g)

    out: Layers = []
    for layer in layers:
        def build(level: Optional[str], keys: Sequence[str]) -> List[str]:
            # Entries of this level in their current order: ("item", key) or ("group", g, its keys).
            entries: List[Tuple[str, str]] = []
            inside: Dict[str, List[str]] = {}
            head: List[str] = []
            tail: List[str] = []
            for k in keys:
                chain = model.chain(k)
                depth = chain.index(level) if level in chain else 0
                if depth + 1 >= len(chain):
                    if k in model.border_keys and level is not None and model.group_of.get(k) == level:
                        (head if not entries and not inside else tail).append(k)
                        continue
                    entries.append(("item", k))
                    continue
                child = chain[depth + 1]
                if child not in inside:
                    inside[child] = []  # type: ignore[index]
                    entries.append(("group", child))  # type: ignore[arg-type]
                inside[child].append(k)  # type: ignore[index]
            groups = sorted((v for kind, v in entries if kind == "group"), key=rank_of)
            order = iter(groups)
            result: List[str] = list(head)
            for kind, v in entries:
                if kind == "item":
                    result.append(v)
                else:
                    g = next(order)
                    result.extend(build(g, inside[g]))
            return result + tail

        out.append(build(None, layer))
    return out


def normalize(model: Model, rank: int, layer: Sequence[str]) -> List[str]:
    """``layer`` with every group contiguous (a group where its first item stands), its borders at its ends,
    and each ``order`` list in its given order."""
    position = {k: i for i, k in enumerate(layer)}

    def key_of(entry: Tuple[str, str]) -> float:
        kind, value = entry
        if kind == "item":
            return position[value]
        return min(position[k] for k in members.get(value, ()) or [value]) if members.get(value) else 0

    # The tree of groups present on this rank.
    members: Dict[str, List[str]] = {}
    for k in layer:
        if k in model.border_keys:
            continue
        for g in model.chain(k)[1:]:
            members.setdefault(g, []).append(k)
    for g, per in model.borders.items():
        if rank in per:
            members.setdefault(g, [])

    def build(level: Optional[str]) -> List[str]:
        entries: List[Tuple[str, str]] = []
        for k in layer:
            if k in model.border_keys:
                continue
            if model.group_of.get(k) == level:
                entries.append(("item", k))
        for g in members:
            if model.parent.get(g) == level:
                entries.append(("group", g))

        def sort_key(entry: Tuple[str, str]) -> Tuple[float, str]:
            kind, value = entry
            if kind == "item":
                return position[value], value
            inside = members.get(value) or []
            return (min(position[k] for k in inside) if inside else float("inf"), value)

        out: List[str] = []
        for kind, value in sorted(entries, key=sort_key):
            out.extend([value] if kind == "item" else build(value))
        if level is not None and rank in model.borders.get(level, {}):
            left, right = model.borders[level][rank]
            out = [left] + out + [right]
        return out

    return _apply_order_lists(model, build(None))


def _apply_order_lists(model: Model, layer: List[str]) -> List[str]:
    """Each ``order`` list's members that share a rank and a group take their slots in the list's order."""
    if not model.order_lists:
        return layer
    out = list(layer)
    for members in model.order_lists:
        by_group: Dict[Optional[str], List[str]] = {}
        present = set(out)
        for m in members:
            if m in present:
                by_group.setdefault(model.group_of.get(m), []).append(m)
        for wanted in by_group.values():
            if len(wanted) < 2:
                continue
            slots = sorted(out.index(m) for m in wanted)
            for slot, m in zip(slots, wanted):
                out[slot] = m
    return out


# --------------------------------------------------------------------------
# sweeps


class _Entry:
    __slots__ = ("vs", "i", "bc", "weight", "indegree", "ins", "outs", "merged", "v")

    def __init__(self, v: str, vs: List[str], i: int, bc: Optional[float], weight: float) -> None:
        self.v = v
        self.vs = vs
        self.i = i
        self.bc = bc
        self.weight = weight
        self.indegree = 0
        self.ins: List["_Entry"] = []
        self.outs: List["_Entry"] = []
        self.merged = False


def _merge(target: _Entry, source: _Entry) -> None:
    total, weight = 0.0, 0.0
    if target.weight and target.bc is not None:
        total += target.bc * target.weight
        weight += target.weight
    if source.weight and source.bc is not None:
        total += source.bc * source.weight
        weight += source.weight
    target.vs = source.vs + target.vs
    target.bc = total / weight if weight else None
    target.weight = weight
    target.i = min(source.i, target.i)
    source.merged = True


def _resolve(entries: List[_Entry], cg: Mapping[str, Set[str]]) -> List[_Entry]:
    """dagre's ``resolveConflicts``: an entry that must come before another (constraint graph) but sorts after
    it is merged into it, so sorting keeps the constraint."""
    by_v = {e.v: e for e in entries}
    for e in entries:
        for w in sorted(cg.get(e.v, ())):
            target = by_v.get(w)
            if target is not None:
                target.indegree += 1
                e.outs.append(target)
    sources = [e for e in entries if not e.indegree]
    out: List[_Entry] = []
    while sources:
        entry = sources.pop()
        out.append(entry)
        for u in reversed(entry.ins):
            if u.merged:
                continue
            if u.bc is None or entry.bc is None or u.bc >= entry.bc:
                _merge(entry, u)
        for w in entry.outs:
            w.ins.append(entry)
            w.indegree -= 1
            if w.indegree == 0:
                sources.append(w)
    kept = [e for e in out if not e.merged]
    seen = {id(e) for e in kept} | {id(e) for e in entries if e.merged}
    # Entries on a constraint cycle never become sources: keep them, unsorted, rather than lose them.
    kept += [e for e in entries if id(e) not in seen]
    return kept


def _sort(entries: List[_Entry], bias_right: bool) -> Tuple[List[str], Optional[float], float]:
    """dagre's ``sort``: entries with a barycentre by it, the others kept at their index."""
    sortable = [e for e in entries if e.bc is not None]
    unsortable = sorted((e for e in entries if e.bc is None), key=lambda e: -e.i)
    sortable.sort(key=lambda e: (e.bc, -e.i if bias_right else e.i))
    vs: List[str] = []
    total, weight = 0.0, 0.0
    index = 0

    def consume(index: int) -> int:
        while unsortable and unsortable[-1].i <= index:
            last = unsortable.pop()
            vs.extend(last.vs)
            index += 1
        return index

    index = consume(index)
    for e in sortable:
        index += len(e.vs)
        vs.extend(e.vs)
        total += e.bc * e.weight  # type: ignore[operator]
        weight += e.weight
        index = consume(index)
    while unsortable:
        vs.extend(unsortable.pop().vs)
    return vs, (total / weight if weight else None), weight


def _sort_level(model: Model, rank: int, level: Optional[str], layer_members: Mapping[Optional[str], List[str]],
                child_groups: Mapping[Optional[str], List[str]], bary: Mapping[str, Tuple[Optional[float], float]],
                cg: Dict[str, Set[str]], bias_right: bool, where: Mapping[str, int]) -> Tuple[List[str], Optional[float], float]:
    entries: List[_Entry] = []
    movable = sorted(list(layer_members.get(level, [])) + list(child_groups.get(level, [])), key=lambda v: (where.get(v, 0), v))
    subs: Dict[str, List[str]] = {}
    for i, v in enumerate(movable):
        if v in bary:
            bc, weight = bary[v]
            entries.append(_Entry(v, [v], i, bc, weight))
        else:
            vs, bc, weight = _sort_level(model, rank, v, layer_members, child_groups, bary, cg, bias_right, where)
            subs[v] = vs
            entries.append(_Entry(v, [v], i, bc, weight))
    resolved = _resolve(entries, cg)
    for e in resolved:
        e.vs = [x for v in e.vs for x in (subs[v] if v in subs else [v])]
    vs, bc, weight = _sort(resolved, bias_right)
    if level is not None and rank in model.borders.get(level, {}):
        left, right = model.borders[level][rank]
        vs = [left] + vs + [right]
    return vs, bc, weight


def _add_constraints(model: Model, layer: Sequence[str], cg: Dict[str, Set[str]]) -> None:
    """dagre's ``addSubgraphConstraints``: sibling groups keep the order they have in this rank."""
    prev: Dict[Optional[str], str] = {}
    for k in layer:
        if k in model.border_keys:
            continue
        child = model.group_of.get(k)
        while child is not None:
            parent = model.parent.get(child)
            before = prev.get(parent)
            prev[parent] = child
            if before is not None and before != child:
                cg.setdefault(before, set()).add(child)
                break
            child = parent


def _sweep(model: Model, layers: Layers, downward: bool, bias_right: bool) -> Layers:
    layers = [list(layer) for layer in layers]
    ranks = range(1, len(layers)) if downward else range(len(layers) - 2, -1, -1)
    fixed_adj = model.up if downward else model.down
    cg: Dict[str, Set[str]] = {}
    for r in ranks:
        neighbour = layers[r - 1] if downward else layers[r + 1]
        pos = {k: i for i, k in enumerate(neighbour)}
        bary: Dict[str, Tuple[Optional[float], float]] = {}
        layer_members: Dict[Optional[str], List[str]] = {}
        child_groups: Dict[Optional[str], List[str]] = {}
        present_groups: Set[str] = set()
        for k in layers[r]:
            if k in model.border_keys:
                continue
            layer_members.setdefault(model.group_of.get(k), []).append(k)
            total, weight = 0.0, 0.0
            for w, wt in fixed_adj.get(k, ()):
                if w in pos:
                    total += wt * pos[w]
                    weight += wt
            bary[k] = (total / weight if weight else None, weight)
            for g in model.chain(k)[1:]:
                present_groups.add(g)
        for g, per in model.borders.items():
            if r in per:
                present_groups.add(g)
        where: Dict[str, int] = {k: i for i, k in enumerate(layers[r])}
        for g in present_groups:
            where[g] = _first_index(layers[r], model, g)
        for g in sorted(present_groups, key=lambda g: (where[g], g)):
            child_groups.setdefault(model.parent.get(g), []).append(g)
        vs, _bc, _w = _sort_level(model, r, None, layer_members, child_groups, bary, cg, bias_right, where)
        layers[r] = _apply_order_lists(model, vs)
        _add_constraints(model, layers[r], cg)
    return layers


def _first_index(layer: Sequence[str], model: Model, group: str) -> int:
    for i, k in enumerate(layer):
        if group in model.chain(k):
            return i
        per = model.borders.get(group, {})
        if any(k in pair for pair in per.values()):
            return i
    return len(layer)


def _transpose(model: Model, layers: Layers) -> Layers:
    """Swap neighbours in a rank while that strictly lowers the crossings with both neighbouring ranks
    (never across a group boundary, a border, or an ``order`` list)."""
    layers = [list(layer) for layer in layers]
    locked = {m for members in model.order_lists for m in members}
    dirty = set(range(len(layers)))
    for _pass in range(TRANSPOSE_PASSES):
        improved = False
        todo, dirty = sorted(dirty), set()
        for r in todo:
            layer = layers[r]
            if len(layer) < 2 or len(layer) > TRANSPOSE_MAX_RANK:
                continue
            above = {k: i for i, k in enumerate(layers[r - 1])} if r > 0 else {}
            below = {k: i for i, k in enumerate(layers[r + 1])} if r + 1 < len(layers) else {}
            ups = {k: sorted((above[x], wt) for x, wt in model.up.get(k, ()) if x in above) for k in layer}
            downs = {k: sorted((below[x], wt) for x, wt in model.down.get(k, ()) if x in below) for k in layer}
            for i in range(len(layer) - 1):
                v, w = layer[i], layer[i + 1]
                if v in model.border_keys or w in model.border_keys or model.group_of.get(v) != model.group_of.get(w):
                    continue
                if v in locked and w in locked:
                    continue
                before = _inversions(ups[v], ups[w]) + _inversions(downs[v], downs[w])
                after = _inversions(ups[w], ups[v]) + _inversions(downs[w], downs[v])
                if after < before:
                    layer[i], layer[i + 1] = w, v
                    improved = True
                    dirty.update((r - 1, r, r + 1))
        dirty = {r for r in dirty if 0 <= r < len(layers)}
        if not improved:
            break
    return layers


def _inversions(left: Sequence[Tuple[int, float]], right: Sequence[Tuple[int, float]]) -> float:
    """Weighted pairs ``(a, b)`` with ``a`` from ``left``, ``b`` from ``right`` and ``a > b``: the crossings between
    the edges of an item and those of the item to its right (both lists sorted by position)."""
    if not left or not right:
        return 0.0
    total = 0.0
    j = 0
    below = 0.0  # the weight of right's positions strictly less than the current a
    for a, wa in left:
        while j < len(right) and right[j][0] < a:
            below += right[j][1]
            j += 1
        total += wa * below
    return total


def improve(model: Model, layers: Layers, incremental: bool = False) -> Tuple[Layers, float, int]:
    """The best order found by the sweeps: ``(layers, crossings, sweeps run)``. An incremental run starts from
    ``layers`` and takes only a strict improvement."""
    best = [list(layer) for layer in layers]
    best_cc = cross_count(best, model.down)
    items = sum(len(layer) for layer in layers)
    patience = next(p for limit, p in PATIENCE_BY_SIZE if items <= limit)
    current = best
    last_best = 0
    sweeps = 0
    budget = _budget.active()
    for i in range(MAX_SWEEPS):
        if last_best >= patience or best_cc == 0 or budget.over():
            break  # out of time: the best order so far
        sweeps += 1
        current = _sweep(model, current, downward=bool(i % 2), bias_right=i % 4 >= 2)
        current = reconcile(model, _transpose(model, current))
        cc = cross_count(current, model.down)
        last_best += 1
        if cc < best_cc:
            best, best_cc, last_best = [list(layer) for layer in current], cc, 0
    return best, best_cc, sweeps
