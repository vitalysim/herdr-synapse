"""Re-cutting a path of ranks into lanes, for the layered layout: which nodes to put in one rank, in what order.

A graph whose ranks hold one node each is drawn as a line, and a line is the one shape a screen cannot fit. The
nine-step build pipeline of the readability corpus came out 16:1 and covered 11 % of a view; the forty-step one
26:1 and 7 %. At those shapes the page's semantic zoom drops every node's text, so the drawing says nothing - which
is the owner's "the aspect is wrong and the drawing is small" in its worst form.

The fold answers it by cutting the path into columns and putting each column's nodes **in one rank**, side by side
across the flow, in serpentine order (odd columns reversed). Two properties make that worth doing here rather than
as a layout of its own:

- Every step inside a column is a same-rank edge - short, local, no dummy chain, no direction to get wrong, because
  a rank's lane order is an order and not a flow. Every step between columns joins the last lane of one column to
  the last lane of the next, which is one rank forward in the same lane: straight.
- The result is an ordinary layered drawing. Its ranks are in order along the flow and each rank's nodes sit on one
  line, so ``layers._seeds_hold`` recognises a folded board as its own fixed point and an incremental redraw keeps
  every box where it is. That is what a fold along the *flow* axis - a true boustrophedon, with alternate rows
  running backwards - could never be, and why the first attempt at this was built, measured and abandoned.

Nothing here lays anything out or measures anything: it says which sets to fold into, and ``layers`` lays each
candidate out and keeps the one whose boxes render biggest.
"""
from __future__ import annotations

import math

from typing import Dict, List, Mapping, Optional, Sequence, Tuple

Sets = Tuple[Tuple[str, ...], ...]
#: ``(the ranks to merge, the lane order inside each of them)``, both as ``LayoutRequest`` accepts them.
Fold = Tuple[Sets, Sets]

#: A path shorter than this is left alone: five ranks of boxes is about 3:1, which a view fits.
FROM_RANKS = 6
#: Past this many nodes no fold is tried: each candidate is a whole layout pass and a graph that big is already
#: against its time budget.
MAX_NODES = 200
#: The most lanes a fold will use, and how many counts around the estimate are laid out and compared.
#:
#: The estimate is closed form and usually right, so only a window around it is tried: each candidate is a whole
#: layout pass, and a two-hundred-step path wants sixteen lanes, which is not a number to find by trying every one.
MAX_LANES = 24
WINDOW = 2


def path_of_ranks(rank: Mapping[str, int], order: Mapping[str, int]) -> Optional[List[str]]:
    """The nodes in rank order when every rank holds exactly one of them, else None.

    This is the shape the fold is for, and it is asked of the *ranks* rather than of the edges on purpose: the
    corpus's long chain is a cycle (its last step loops back to its first) and its forty-step cousin carries six
    edges that skip five ranks each. Neither is a simple path, and both are drawn as one line.
    """
    if len(rank) < FROM_RANKS or len(rank) > MAX_NODES:
        return None
    by_rank: Dict[int, List[str]] = {}
    for node, r in rank.items():
        by_rank.setdefault(r, []).append(node)
    if any(len(members) != 1 for members in by_rank.values()):
        return None
    return [by_rank[r][0] for r in sorted(by_rank)]


#: How many nodes may stand off the spine and the drawing still be folded (``spine_of_ranks``).
#:
#: One, because one is what the shapes that were left thin need: a retry loop is a line with its "done" step off to
#: one side, and an editorial pipeline a line with its "revise" step. Two were tried in thought and not built: each
#: node off the spine is laid out wherever the ordering pass puts it in the rank after its own column, and a second
#: one can land in the middle of the next column's lanes, which is the snake the fold exists to avoid. A candidate
#: with a branch is also held to the unfolded drawing's crossings (``layers._folded``), so a branch that does land
#: badly costs the fold, not the reader.
BRANCH_MAX = 1


def spine_of_ranks(rank: Mapping[str, int], edges: Sequence[Tuple[str, str]],
                   order: Mapping[str, int]) -> Optional[Tuple[List[str], List[str]]]:
    """``(the spine in rank order, the nodes off it)`` when every rank holds one node but for ``BRANCH_MAX`` extra
    ones, else None.

    ``path_of_ranks`` asked for exactly one node per rank, and that left the retry loop - the corpus's ``cycle``,
    8.9:1 and 20 % of a view - drawn as a line, because its check step has two successors ("ok" and "error") and so
    one rank holds two of them. The loop *is* a line with one step beside it, and folding the line is what draws it
    as a loop: queue, run and check down one column, retry beside check and back to queue in the next.

    In a rank that holds two nodes, the spine takes the one that carries the line on: linked to the spine node before
    it and to a node in the next rank, then linked to either, then declaration order (``order``, so a shuffled request
    picks the same spine). The other is left out of every fold set, which puts it in a rank of its own after the
    column of the node it hangs from, lined up with it. A node off the spine with no edge to the spine at all is a
    second drawing, not a branch, and nothing is folded.
    """
    if len(rank) > MAX_NODES:
        return None
    by_rank: Dict[int, List[str]] = {}
    for node in sorted(rank, key=lambda n: (order.get(n, 0), n)):
        by_rank.setdefault(rank[node], []).append(node)
    if len(by_rank) < FROM_RANKS or len(rank) - len(by_rank) > BRANCH_MAX:
        return None
    linked: Dict[str, set] = {node: set() for node in rank}
    for a, b in edges:
        if a in linked and b in linked and a != b:
            linked[a].add(b)
            linked[b].add(a)
    ranks = sorted(by_rank)
    spine: List[str] = []
    branches: List[str] = []
    for index, r in enumerate(ranks):
        members = by_rank[r]
        if len(members) == 1:
            spine.append(members[0])
            continue
        after = set(by_rank[ranks[index + 1]]) if index + 1 < len(ranks) else set()
        before = spine[-1] if spine else None

        def score(node: str) -> Tuple[int, int]:
            back = 1 if before is not None and before in linked[node] else 0
            on = 1 if linked[node] & after else 0
            return (-(back + on), -on)

        chosen = min(members, key=score)  # ``min`` keeps the first of equals: declaration order breaks the tie
        spine.append(chosen)
        branches.extend(m for m in members if m != chosen)
    on_spine = set(spine)
    if any(not (linked[b] & on_spine) for b in branches):
        return None
    return spine, branches


def _fold_at(path: Sequence[str], lanes: int) -> Fold:
    """The path cut into columns of ``lanes``, each column a rank, odd columns reversed."""
    columns = [list(path[at:at + lanes]) for at in range(0, len(path), lanes)]
    sets = tuple(tuple(column) for column in columns if len(column) > 1)
    ordered = tuple(tuple(column if index % 2 == 0 else column[::-1])
                    for index, column in enumerate(columns) if len(column) > 1)
    return sets, ordered


def _reaching(path: Sequence[str], lanes: int, edges: Sequence[Tuple[str, str]]) -> int:
    """How many edges this fold would turn into a step down a column past other boxes.

    A step between two lanes of one column is short and straight; a step across four of them has to be routed
    around the four boxes between, and the router draws it as a detour that doubles back. The forty-step board of
    the corpus carries six steps that skip five, and the column count that fits a view best puts every one of them
    inside a single column: four reversals each, and one route that crosses itself. Counted rather than weighed,
    because one such step is already a picture with a snake in it.
    """
    at = {node: index for index, node in enumerate(path)}
    return sum(1 for a, b in edges if a in at and b in at and at[a] != at[b]
               and at[a] // lanes == at[b] // lanes and abs(at[a] - at[b]) > 1)


def lanes_for(steps: int, across: float, along: float, gap: float, rank_gap: float, vertical: bool,
              screen_aspect: float) -> int:
    """How many lanes make a path of ``steps`` boxes closest to the screen's own shape.

    Closed form, from the two extents a fold has: ``L`` lanes are ``L x (across + gap)`` wide across the flow and
    ``ceil(steps / L) x (along + rank_gap)`` long along it. Setting the ratio of those two to the screen's aspect
    (the right way up for the drawing's direction) and solving for ``L`` gives this; the window around it is what
    gets laid out and measured, because the estimate knows nothing about labels or a box that is wider than its
    neighbours.
    """
    lane = max(across + gap, 1.0)
    column = max(along + rank_gap, 1.0)
    ratio = screen_aspect if vertical else 1.0 / screen_aspect
    found = math.sqrt(max(steps * column / lane * ratio, 1.0))
    return max(2, min(MAX_LANES, int(round(found))))


def candidates(path: Sequence[str], edges: Sequence[Tuple[str, str]] = (), hint: Optional[int] = None,
               branches: Sequence[str] = ()) -> List[Tuple[int, Fold]]:
    """The folds worth laying out: ``(lanes, the fold)``, narrowest first.

    A window around ``hint`` (every count from 2 up without one). A column count that leaves the last column with one
    node in it is skipped where a narrower one does not: the shape gain is the same and the drawing has a dangling
    box. Folds that would send a step down a column past other boxes are left out unless every candidate does it, in
    which case the least of them stay.

    With a node off the spine, every count is laid out twice where it can be: once with the branch left out of every
    set (it lands in the rank after its column) and once with it at the end of its own column (``_beside``). And no
    count is skipped for its dangling last box, because the branch is often what decides which count reads: the
    editorial pipeline's only clean fold has "publish" alone in its last column.
    """
    out: List[Tuple[int, int]] = []
    for lanes in range(2, min(MAX_LANES, len(path) // 2) + 1):
        if len(path) % lanes == 1 and lanes > 2 and not branches:
            continue
        out.append((_reaching(path, lanes, edges), lanes))
    if not out:
        return []
    # Which counts are *allowed* is decided first and costs nothing, and only then is the field narrowed to the ones
    # near the estimate. The other way round threw away the one count that drew the forty-step board's six skip
    # steps as single rank-to-rank hops, because it sat four outside the window.
    fewest = min(count for count, _lanes in out)
    kept = sorted(lanes for count, lanes in out if count == fewest)
    if hint is not None and len(kept) > 2 * WINDOW + 1:
        kept = sorted(sorted(kept, key=lambda lanes: (abs(lanes - hint), lanes))[:2 * WINDOW + 1])
    found: List[Tuple[int, Fold]] = []
    for lanes in kept:
        if any(_home(path, lanes, edges, branch) is None for branch in branches):
            continue
        fold = _fold_at(path, lanes)
        found.append((lanes, fold))
        beside = _beside(path, lanes, edges, branches)
        if beside is not None and beside != fold:
            found.append((lanes, beside))
    return found


def _home(path: Sequence[str], lanes: int, edges: Sequence[Tuple[str, str]], branch: str) -> Optional[int]:
    """The one column a branch hangs from at this lane count, or None when its steps reach more than one.

    A branch is a step *beside* the line, and that is the only kind the fold tolerates. The flowchart golden's
    "Show a retry banner" is fed from two decisions in different columns and loops back to the first step: folded,
    one of its feeds ran through the middle of the drawing, crossed the "approved" step and pulled two pills away
    from their own arrows, where the unfolded flowchart reads top to bottom with nothing crossing. A node that ties
    several columns together is part of the drawing's structure, and a drawing with that much structure is left as
    the layered layout draws it.
    """
    where = {node: at // lanes for at, node in enumerate(path)}
    homes = {where[b if a == branch else a] for a, b in edges
             if branch in (a, b) and a != b and (b if a == branch else a) in where}
    return homes.pop() if len(homes) == 1 else None


def _beside(path: Sequence[str], lanes: int, edges: Sequence[Tuple[str, str]],
            branches: Sequence[str]) -> Optional[Fold]:
    """The fold with each branch at the end of its own column, one lane beyond the spine node it hangs from.

    Left out of every set, a branch goes in the rank after its column - which is free when that column is the last
    one (the retry loop's "done" lands beside "check", in line with it) and is the next column's rank otherwise,
    where it can only land among that column's lanes (``layers._column_blocked`` then sinks the candidate). The
    other place a branch can go without a step running past a box is the far end of its own column: both ends of a
    column's order are where the spine *leaves* the column sideways, so one lane beyond the end is empty in every
    column, and the branch is one short step down from the node it hangs from. The editorial pipeline's "revise"
    step reads that way: under "editor review", with the review going on to "fact check" in the next column.

    None when a branch hangs from spine nodes in more than one column, or from one in the middle of its column.
    """
    if not branches:
        return None
    columns = [list(path[at:at + lanes]) for at in range(0, len(path), lanes)]
    ordered = [column if index % 2 == 0 else column[::-1] for index, column in enumerate(columns)]
    where = {node: index for index, column in enumerate(columns) for node in column}
    for branch in branches:
        near = {a if b == branch else b for a, b in edges if branch in (a, b) and a != b} - {branch}
        homes = {where[n] for n in near if n in where}
        if len(homes) != 1 or any(n not in where for n in near):
            return None
        home = homes.pop()
        lane_order = ordered[home]
        if lane_order[-1] in near:
            ordered[home] = lane_order + [branch]
        elif lane_order[0] in near:
            ordered[home] = [branch] + lane_order
        else:
            return None
        columns[home] = columns[home] + [branch]
    sets = tuple(tuple(column) for column in columns if len(column) > 1)
    lane_sets = tuple(tuple(column) for column in ordered if len(column) > 1)
    return sets, lane_sets


def from_seeds(path: Sequence[str], seeds: Mapping[str, Tuple[float, float]], heights: Mapping[str, float],
               tolerance: float = 1.0, branches: Sequence[str] = ()) -> Optional[Fold]:
    """The fold a drawn board already has, read off its own boxes, or None when it is not folded.

    This is what keeps a fold stable, and it is the whole reason the fold can ship. A folded board's ranks are its
    columns, so an incremental redraw has to be told which columns those were - the graph alone still says "one
    node per rank". The columns are recovered from the seeds' own lines across the flow, and a node with no seed
    (the step its author has just added) is left out of every set, which puts it in a rank of its own after the
    column it follows. One new box moves; nothing else does.

    ``seeds`` and ``heights`` are in the layout's own ``down`` frame, so a column is a set of nodes whose centres
    along the flow agree.
    """
    lines: List[Tuple[float, List[str]]] = []
    for node in path:
        seed = seeds.get(node)
        if seed is None:
            continue
        centre = seed[1] + heights.get(node, 0.0) / 2.0
        found = next((entry for entry in lines if abs(entry[0] - centre) <= tolerance), None)
        if found is None:
            lines.append((centre, [node]))
        else:
            found[1].append(node)
    if len(lines) < 2 or all(len(members) < 2 for _centre, members in lines):
        return None
    place = {node: index for index, node in enumerate(path)}
    columns = [sorted(members, key=lambda n: place[n]) for _centre, members in sorted(lines)]
    for members in columns:
        run = [place[n] for n in members]
        if run != list(range(run[0], run[0] + len(run))):
            return None  # the lines are not columns of this path: the board was not folded, it was arranged
    columns.sort(key=lambda members: place[members[0]])
    # A branch drawn on a column's own line is in that column (``_beside``), and has to be told so: left out, it went
    # to a rank of its own on the redraw and the folded editorial board moved its "revise" step on every re-issue.
    # A branch on no column's line is where the fold left it, in a rank of its own, and stays out.
    for branch in branches:
        seed = seeds.get(branch)
        if seed is None:
            continue
        centre = seed[1] + heights.get(branch, 0.0) / 2.0
        for members in columns:
            first = seeds[members[0]]
            if abs(first[1] + heights.get(members[0], 0.0) / 2.0 - centre) <= tolerance:
                members.append(branch)
                break
    sets = tuple(tuple(column) for column in columns if len(column) > 1)
    ordered = tuple(tuple(sorted(column, key=lambda n: seeds[n][0]))
                    for column in columns if len(column) > 1)
    return sets, ordered
