"""A tidy tree for boxes of any size (Reingold-Tilford with per-level contours, as refined by Walker and Buchheim et al.).

Works in the ``down`` frame: depth runs along ``y``, siblings along ``x``.
Each subtree is laid out once, bottom up; its contour is the leftmost and
rightmost extent it reaches at every depth below its root. Children sit side
by side, each pushed right just far enough that its contour clears the ones
before it (``gap`` between sibling boxes, ``subtree_gap`` between deeper
cousins); a parent is centred over its first and last child. Small subtrees
between two large ones are then spread evenly (Walker's apportioning), so the
gaps look even.

Pure; nodes are string keys.
"""
from __future__ import annotations

from typing import Dict, List, Mapping, Sequence, Tuple

Contour = Dict[int, Tuple[float, float]]  # depth below the subtree root -> (left, right), relative to the root's centre


def tidy(roots: Sequence[str], children: Mapping[str, Sequence[str]], width: Mapping[str, float], gap: float,
         subtree_gap: float) -> Dict[str, float]:
    """The centre of every node along its level, the first root's leftmost extent at 0; roots side by side."""
    offset: Dict[str, float] = {}  # a child's centre relative to its parent's centre
    contour: Dict[str, Contour] = {}
    for node in _postorder(roots, children):
        kids = list(children.get(node, ()))
        half = width[node] / 2.0
        if not kids:
            contour[node] = {0: (-half, half)}
            continue
        placed: List[float] = []
        merged: Contour = {}
        for index, kid in enumerate(kids):
            shape = contour[kid]
            if index == 0:
                at = 0.0
            else:
                at = float("-inf")
                for depth, (left, _right) in shape.items():
                    if depth in merged:
                        room = gap if depth == 0 else subtree_gap
                        at = max(at, merged[depth][1] + room - left)
                if at == float("-inf"):
                    at = placed[-1]
            placed.append(at)
            for depth, (left, right) in shape.items():
                lo, hi = merged.get(depth, (float("inf"), float("-inf")))
                merged[depth] = (min(lo, left + at), max(hi, right + at))
        placed = _spread(kids, placed, contour, gap, subtree_gap)
        merged = {}
        for kid, at in zip(kids, placed):
            for depth, (left, right) in contour[kid].items():
                lo, hi = merged.get(depth, (float("inf"), float("-inf")))
                merged[depth] = (min(lo, left + at), max(hi, right + at))
        middle = (placed[0] + placed[-1]) / 2.0
        for kid, at in zip(kids, placed):
            offset[kid] = at - middle
        shape = {0: (-half, half)}
        for depth, (left, right) in merged.items():
            shape[depth + 1] = (left - middle, right - middle)
        contour[node] = shape
    out: Dict[str, float] = {}
    cursor = 0.0
    for index, root in enumerate(roots):
        shape = contour[root]
        left = min(l for l, _r in shape.values())
        right = max(r for _l, r in shape.values())
        centre = cursor - left
        stack = [(root, centre)]
        while stack:
            node, at = stack.pop()
            out[node] = at
            for kid in children.get(node, ()):
                stack.append((kid, at + offset[kid]))
        cursor = centre + right + subtree_gap
    return out


def _postorder(roots: Sequence[str], children: Mapping[str, Sequence[str]]) -> List[str]:
    out: List[str] = []
    for root in roots:
        stack: List[Tuple[str, int]] = [(root, 0)]
        while stack:
            node, i = stack[-1]
            kids = children.get(node, ())
            if i < len(kids):
                stack[-1] = (node, i + 1)
                stack.append((kids[i], 0))
                continue
            stack.pop()
            out.append(node)
    return out


def _spread(kids: Sequence[str], placed: List[float], contour: Mapping[str, Contour], gap: float, subtree_gap: float) -> List[float]:
    """Walker's apportioning, simplified: a run of children with slack between the two it sits between moves to
    share the slack evenly, as long as no contour comes closer than it may."""
    if len(kids) < 3:
        return placed
    out = list(placed)
    for i in range(1, len(kids) - 1):
        lo_bound = max(_least(contour[kids[j]], out[j], contour[kids[i]], gap, subtree_gap) for j in range(i))
        hi_bound = min(-_least_rev(contour[kids[j]], out[j], contour[kids[i]], gap, subtree_gap) for j in range(i + 1, len(kids)))
        if hi_bound < lo_bound:
            continue
        target = (out[i - 1] + out[i + 1]) / 2.0
        out[i] = min(max(target, lo_bound), hi_bound)
    return out


def _least(left_shape: Contour, left_at: float, shape: Contour, gap: float, subtree_gap: float) -> float:
    """The least centre ``shape`` may take right of ``left_shape`` at ``left_at``."""
    best = float("-inf")
    for depth, (left, _right) in shape.items():
        if depth in left_shape:
            best = max(best, left_shape[depth][1] + left_at + (gap if depth == 0 else subtree_gap) - left)
    return best


def _least_rev(right_shape: Contour, right_at: float, shape: Contour, gap: float, subtree_gap: float) -> float:
    """Minus the greatest centre ``shape`` may take left of ``right_shape`` at ``right_at``."""
    best = float("-inf")
    for depth, (_left, right) in shape.items():
        if depth in right_shape:
            best = max(best, right + (gap if depth == 0 else subtree_gap) - (right_shape[depth][0] + right_at))
    return best
