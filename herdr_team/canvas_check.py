"""The canvas layout check (0.21): what reads badly, with element ids and a fix to apply.

A picture of the canvas helps an agent that can see images, but vision models
judge overlaps and alignment poorly, and many agents cannot read images at
all. So the checks are geometry over the scene, reported as text every agent
can act on (``canvas check``, the MCP ``canvas_check`` tool, and the
``problems`` section of ``look``):

* ``overlap``: two marks partly on top of each other (one wholly inside the
  other is a grouping, not a problem, unless it is text on a labelled shape);
* ``text_on_label``: a text lying on a shape that has its own label;
* ``label_overflow``: a shape's label needs more room than the shape has,
  measured with the bundled font's metrics by the element's kind
  (``canvas_kinds``, ``canvas_text``); since 0.22 shapes grow to fit, so
  this reports elements drawn before that, or a bug;
* ``label_truncated``: a clamped label shows only part of its text (a
  kind's own check);
* ``frame_edge``: a mark half inside a frame;
* ``arrow_through``: an arrow crossing a mark it does not connect;
* ``stray``: a mark far from everything else;
* ``claim_edge``: an active claim's boundary drawn through a mark, so the mark reads as cut off.

Every problem carries ``ids``, a one-line ``message``, and ``fix``: a ready
``move`` operation when there is an obvious one (``None`` otherwise, the
message says what to do). Pure: reads element dicts, writes nothing.

The checks are a registry (``CHECKS``, ``register_check``): a new check is
one function and one line, and a kind adds its own through ``Kind.checks``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from herdr_team import canvas_kinds as _kinds

_KIND_SETS: Dict[str, FrozenSet[str]] = {}
_kinds.on_change(_KIND_SETS.clear)


def solid_kinds() -> FrozenSet[str]:
    """Marks that take room on the canvas and so can collide (``Kind.solid``). Arrows, pens and comments are drawn over things on purpose."""
    found = _KIND_SETS.get("solid")
    if found is None:
        found = _KIND_SETS["solid"] = frozenset(kind.name for kind in _kinds.kinds() if kind.solid)
    return found


def labelled_kinds() -> FrozenSet[str]:
    """Shapes with a fitted label inside (``Kind.labelled``): a text on one covers its label."""
    found = _KIND_SETS.get("labelled")
    if found is None:
        found = _KIND_SETS["labelled"] = frozenset(kind.name for kind in _kinds.kinds() if kind.labelled)
    return found


def __getattr__(name: str) -> Any:
    # ``SOLID`` and ``LABELLED_SHAPES`` stay importable (PEP 562), derived from the registry (canvas v2 phase 1, 2.4).
    if name == "SOLID":
        return solid_kinds()
    if name == "LABELLED_SHAPES":
        return labelled_kinds()
    raise AttributeError("module {!r} has no attribute {!r}".format(__name__, name))

#: Overlaps thinner than this are edges touching, not marks on top of each other.
TOUCH = 4.0
#: A mark this far from its nearest neighbour is reported as stray (with at least ``STRAY_MIN_MARKS`` marks).
STRAY_GAP = 1500.0
STRAY_MIN_MARKS = 4
FRAME_PAD = 20
#: Problems a ``look`` shows; ``canvas check`` lists every one.
LOOK_MAX = 8
#: The order ``problems`` reports in, most serious first. The codes a kind of element contributes sort here too
#: (canvas v2 layout clarity, 6): a tangle a reader cannot follow is as serious as an arrow through a mark
#: (``arrow_through``, 3), a label or a claim edge on the wrong thing as serious as a stray (4), and a badly
#: shaped drawing after both (5).
SEVERITY = {"overlap": 0, "text_on_label": 0, "label_overflow": 1, "label_truncated": 1, "frame_edge": 2,
            "arrow_through": 3, "crossings_high": 3, "routes_tangled": 3, "stray": 4, "labels_adrift": 4,
            "claim_edge": 4, "graph_thin": 5}


def box_of(el: Dict[str, Any]) -> Tuple[float, float, float, float]:
    x, y = float(el.get("x") or 0), float(el.get("y") or 0)
    return x, y, x + max(1.0, float(el.get("w") or 1)), y + max(1.0, float(el.get("h") or 1))


def _overlap(a: Sequence[float], b: Sequence[float]) -> Tuple[float, float]:
    return min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])


def _intersects(a: Sequence[float], b: Sequence[float], margin: float = 0.0) -> bool:
    w, h = _overlap(a, b)
    return w > margin and h > margin


def _contains(outer: Sequence[float], inner: Sequence[float]) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _label(el: Dict[str, Any]) -> str:
    text = " ".join(str(el.get("text") or "").split())
    return ' "{}"'.format(text if len(text) <= 30 else text[:29] + "…") if text else ""


def _name(el: Dict[str, Any]) -> str:
    return "{} {}{}".format(el.get("id"), el.get("type"), _label(el))


def _problem(code: str, ids: List[str], message: str, fix: Optional[Dict[str, Any]], reader: Optional[str], by_id: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    if fix is not None:
        # Every operation carries an intent, so the fix applies as it stands (canvas draw --op, canvas_draw).
        fix = dict(fix, intent=fix.get("intent") or "fix the layout: {} ({})".format(code.replace("_", " "), ", ".join(ids)))
    return {"code": code, "ids": ids, "message": message, "fix": fix,
            "yours": bool(reader) and any((by_id.get(i) or {}).get("author") == reader for i in ids)}


# --------------------------------------------------------------------------
# the checks


GAP = 20
GRID = 20
#: How far (in grid steps) ``free_spot`` searches around the other mark before giving up.
SEARCH_RINGS = 12


def _snap(value: float) -> int:
    return int(round(value / GRID) * GRID)


def free_spot(el: Dict[str, Any], other: Dict[str, Any], boxes: List[Tuple[str, Tuple[float, float, float, float]]],
              frame: Optional[Tuple[float, float, float, float]], near: Optional[Tuple[float, float]] = None,
              frame_grows: bool = False, clearance: float = 0.0) -> Optional[Tuple[int, int]]:
    """A top-left corner for ``el`` beside ``other`` that touches no other mark and stays inside ``frame`` (when given).

    ``near`` tries the spots nearest that point first (where the author put it) rather than the sides
    of ``other`` in a fixed order; ``frame_grows`` lets the spot pass the frame's right and bottom edges,
    which the frame then grows over (QA F-2: a sun that grew into the roof went below the roof, onto the
    spot the next op put the tree on, because the free spot beside the roof was past the frame's edge);
    ``clearance`` keeps that much room to every other mark (room for an arrow between them).
    """
    x0, y0, x1, y1 = box_of(el)
    w, h = x1 - x0, y1 - y0
    ox0, oy0, ox1, oy1 = box_of(other)

    def free(x: float, y: float) -> bool:
        box = (x, y, x + w, y + h)
        if frame is not None and not _contains(frame, box):
            if not frame_grows or box[0] < frame[0] or box[1] < frame[1]:
                return False
        room = (x - clearance, y - clearance, x + w + clearance, y + h + clearance)
        return not any(_intersects(room, b, TOUCH) for eid, b in boxes if eid != el["id"])

    candidates = [(ox1 + GAP, y0), (x0, oy1 + GAP), (ox0 - w - GAP, y0), (x0, oy0 - h - GAP)]
    for ring in range(1, SEARCH_RINGS + 1):
        step = ring * GRID
        candidates += [(x0 + dx, y0 + dy) for dx in (-step, 0, step) for dy in (-step, 0, step) if dx or dy]
    if near is not None:
        candidates.sort(key=lambda c: math.hypot(_snap(c[0]) - near[0], _snap(c[1]) - near[1]))
    for x, y in candidates:
        x, y = _snap(x), _snap(y)
        if free(x, y):
            return x, y
    return None


def _move_away(el: Dict[str, Any], other: Dict[str, Any], boxes: List[Tuple[str, Tuple[float, float, float, float]]],
               by_id: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    frame_el = by_id.get(str(el.get("frame") or ""))
    spot = free_spot(el, other, boxes, box_of(frame_el) if frame_el else None)
    if spot is None:
        return {"op": "move", "id": el["id"], "right_of": other["id"]}
    return {"op": "move", "id": el["id"], "to": [spot[0], spot[1]]}


def _overlaps(solid: List[Dict[str, Any]], reader: Optional[str], by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    boxes = [(str(el["id"]), box_of(el)) for el in solid]
    ordered = sorted(solid, key=lambda el: box_of(el)[0])
    active: List[Tuple[Dict[str, Any], Tuple[float, float, float, float]]] = []
    for el in ordered:
        box = box_of(el)
        active = [(o, ob) for o, ob in active if ob[2] > box[0] + TOUCH]
        for other, other_box in active:
            w, h = _overlap(box, other_box)
            if w <= TOUCH or h <= TOUCH:
                continue
            outer, inner = (other, el) if _contains(other_box, box) else (el, other) if _contains(box, other_box) else (None, None)
            if outer is not None and inner is not None:
                # A mark wholly inside another is a grouping, except text on a shape that has its own label.
                if inner.get("type") == "text" and outer.get("type") in labelled_kinds() and outer.get("text"):
                    out.append(_problem("text_on_label", [inner["id"], outer["id"]],
                                        "{} lies on {}, whose own label is there; move it off the shape or put the words in the label".format(_name(inner), _name(outer)),
                                        _move_away(inner, outer, boxes, by_id), reader, by_id))
                continue
            later, earlier = (el, other) if int(el.get("created_seq") or 0) >= int(other.get("created_seq") or 0) else (other, el)
            out.append(_problem("overlap", [later["id"], earlier["id"]],
                                "{} overlaps {} by {}x{}; move {} to a free spot".format(_name(later), _name(earlier), int(w), int(h), later["id"]),
                                _move_away(later, earlier, boxes, by_id), reader, by_id))
        active.append((el, box))
    return out


def label_room(el: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """``(w, h)`` a labelled element needs for its label at the size it is drawn, or None when it fits.

    Measured by the element's kind with the bundled font's metrics: a shape
    at its drawn size (``fit.size``, else ``style.size``) with no further
    shrinking, a text as it wraps. A kind without a ``measure`` never overflows.
    """
    if not str(el.get("text") or "").strip() or el.get("role") == "portrait":
        return None
    result = _kinds.drawn(el)
    if result is None:
        return None
    x0, y0, x1, y1 = box_of(el)
    w, h = x1 - x0, y1 - y0
    need_w, need_h = int(math.ceil(result.w - 1e-6)), int(math.ceil(result.h - 1e-6))
    if need_w <= w + 0.5 and need_h <= h + 0.5:
        return None
    return max(need_w, int(math.ceil(w))), max(need_h, int(math.ceil(h)))


def _label_overflows(solid: List[Dict[str, Any]], reader: Optional[str], by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for el in solid:
        need = label_room(el)
        if need is None:
            continue
        x0, y0, x1, y1 = box_of(el)
        # A text's height follows its lines, so its fix is the width it wraps at.
        fix = {"op": "move", "id": el["id"], "w": need[0]} if el.get("type") == "text" else {"op": "move", "id": el["id"], "w": need[0], "h": need[1]}
        out.append(_problem("label_overflow", [el["id"]],
                            "{}'s label needs {}x{} but the {} is {}x{}; resize it (or shorten the label)".format(
                                _name(el), need[0], need[1], "text" if el.get("type") == "text" else "shape", int(x1 - x0), int(y1 - y0)),
                            fix, reader, by_id))
    return out


def _frame_edges(marks: List[Dict[str, Any]], frames: List[Dict[str, Any]], reader: Optional[str], by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for el in marks:
        box = box_of(el)
        for frame in frames:
            if frame["id"] == el["id"]:
                continue
            outer = box_of(frame)
            if not _intersects(outer, box, TOUCH) or _contains(outer, box) or _contains(box, outer):
                continue
            sides = [side for side, past in (("left", box[0] < outer[0]), ("top", box[1] < outer[1]),
                                             ("right", box[2] > outer[2]), ("bottom", box[3] > outer[3])) if past]
            w, h = _overlap(box, outer)
            mostly_in = w * h >= 0.5 * (box[2] - box[0]) * (box[3] - box[1])
            if _joined_only(frame):
                # A stack or a positional block takes members only on purpose (phase 2): move the mark off its zone.
                boxes = [(str(o["id"]), box_of(o)) for o in marks + frames if o["id"] != el["id"]]
                spot = free_spot(el, frame, boxes, None)
                fix = {"op": "move", "id": el["id"], "to": [spot[0], spot[1]]} if spot else None
                out.append(_problem("frame_edge", [el["id"], frame["id"]],
                                    "{} lies on {} past its {} edge; move it off the {} (or put it in with place in)".format(
                                        _name(el), _name(frame), " and ".join(sides), frame.get("block")), fix, reader, by_id))
                break
            if mostly_in and "left" not in sides and "top" not in sides:
                grow_w = int(math.ceil(max(outer[2], box[2] + FRAME_PAD) - outer[0]))
                grow_h = int(math.ceil(max(outer[3], box[3] + FRAME_PAD) - outer[1]))
                fix: Optional[Dict[str, Any]] = {"op": "move", "id": frame["id"], "w": grow_w, "h": grow_h}
                advice = "grow the frame to {}x{}".format(grow_w, grow_h)
            else:
                fix = {"op": "move", "id": el["id"], "inside": frame["id"]}
                advice = "move it inside"
            out.append(_problem("frame_edge", [el["id"], frame["id"]],
                                "{} sticks out of frame {} past its {} edge; {}".format(_name(el), _name(frame), " and ".join(sides), advice),
                                fix, reader, by_id))
            break
    # A frame inside a frame (its ``frame`` names the parent) that grew past it: the parent should grow (QA F-3).
    ids = {frame["id"] for frame in frames}
    for frame in frames:
        parent = by_id.get(str(frame.get("frame"))) if frame.get("frame") in ids else None
        if parent is None:
            continue
        box, outer = box_of(frame), box_of(parent)
        if _contains(outer, box):
            continue
        sides = [side for side, past in (("left", box[0] < outer[0]), ("top", box[1] < outer[1]),
                                         ("right", box[2] > outer[2]), ("bottom", box[3] > outer[3])) if past]
        grow_w = int(math.ceil(max(outer[2], box[2] + FRAME_PAD) - outer[0]))
        grow_h = int(math.ceil(max(outer[3], box[3] + FRAME_PAD) - outer[1]))
        out.append(_problem("frame_edge", [frame["id"], parent["id"]],
                            "{} sticks out of its frame {} past its {} edge; grow the frame to {}x{}".format(
                                _name(frame), _name(parent), " and ".join(sides), grow_w, grow_h),
                            {"op": "move", "id": parent["id"], "w": grow_w, "h": grow_h}, reader, by_id))
    return out


def _joined_only(frame: Dict[str, Any]) -> bool:
    """A block frame that takes members only on purpose: a stack (``row``/``column``/``grid``) or a positional block."""
    if not frame.get("block"):
        return False
    settings = frame.get("settings") if isinstance(frame.get("settings"), dict) else {}
    if settings.get("layout") in ("row", "column", "grid"):
        return True
    kind = _kinds.kind_of(frame)
    return bool(kind is not None and kind.block is not None and kind.block.positional and kind.name != "section")


def _segment_hits_box(p: Sequence[float], q: Sequence[float], box: Sequence[float]) -> bool:
    """Whether the segment p-q passes through the box (Liang-Barsky clipping)."""
    x0, y0, x1, y1 = box
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for edge_p, edge_q in ((-dx, p[0] - x0), (dx, x1 - p[0]), (-dy, p[1] - y0), (dy, y1 - p[1])):
        if edge_p == 0:
            if edge_q < 0:
                return False
            continue
        t = edge_q / edge_p
        if edge_p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _drawn(arrow: Dict[str, Any], points: List[Any]) -> List[Any]:
    """The line an arrow is drawn along: its points, or a curved one's curve sampled (a curve's points are only its
    control points; QA phase 2, R3)."""
    if not (arrow.get("curve") or (arrow.get("style") or {}).get("route") in ("curved", "curve")):
        return points
    from herdr_team import canvas_geometry

    return canvas_geometry.arrow_route(dict(arrow, curve=True), per_piece=8)


#: The cell of the coarse grid ``_arrows_through`` buckets marks in, so each arrow meets only the marks near it.
THROUGH_CELL = 400.0


def _arrows_through(arrows: List[Dict[str, Any]], solid: List[Dict[str, Any]], reader: Optional[str], by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    # Each mark's box once, bucketed by a coarse grid: a board of 400 arrows and 200 nodes tests each arrow against
    # the marks near it, not all of them (QA phase 2, R2).
    marks: List[Tuple[Dict[str, Any], Tuple[float, ...], Tuple[float, ...]]] = []
    cells: Dict[Tuple[int, int], List[int]] = {}
    for el in solid:
        box = box_of(el)
        inner = (box[0] + TOUCH, box[1] + TOUCH, box[2] - TOUCH, box[3] - TOUCH)
        if inner[0] >= inner[2] or inner[1] >= inner[3]:
            continue
        index = len(marks)
        marks.append((el, tuple(box), inner))
        for cx in range(int(math.floor(box[0] / THROUGH_CELL)), int(math.floor(box[2] / THROUGH_CELL)) + 1):
            for cy in range(int(math.floor(box[1] / THROUGH_CELL)), int(math.floor(box[3] / THROUGH_CELL)) + 1):
                cells.setdefault((cx, cy), []).append(index)
    for arrow in arrows:
        points = [p for p in (arrow.get("points") or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(points) < 2:
            continue
        points = _drawn(arrow, points)
        ends = {arrow.get("from"), arrow.get("to")}
        arrow_box = box_of(arrow)
        xs, ys = [float(p[0]) for p in points], [float(p[1]) for p in points]
        reach = (min(xs + [arrow_box[0]]), min(ys + [arrow_box[1]]), max(xs + [arrow_box[2]]), max(ys + [arrow_box[3]]))
        pieces = [(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]), a, b) for a, b in zip(points, points[1:])]
        # The marks each piece comes near (the cells it crosses), so a long route meets each mark with its own pieces only.
        near: Dict[int, List[int]] = {}
        for number, (sx0, sy0, sx1, sy1, _a, _b) in enumerate(pieces):
            for cx in range(int(math.floor(sx0 / THROUGH_CELL)), int(math.floor(sx1 / THROUGH_CELL)) + 1):
                for cy in range(int(math.floor(sy0 / THROUGH_CELL)), int(math.floor(sy1 / THROUGH_CELL)) + 1):
                    for index in cells.get((cx, cy), ()):
                        found = near.setdefault(index, [])
                        if not found or found[-1] != number:
                            found.append(number)
        for index in sorted(near):
            el, box, inner = marks[index]
            if el["id"] in ends or not _intersects(reach, box):
                continue
            # An end drawn from a point inside a shape starts there on purpose; only the path through counts.
            if any(inner[0] < p[0] < inner[2] and inner[1] < p[1] < inner[3] for p in (points[0], points[-1])):
                continue
            if any(sx0 <= inner[2] and inner[0] <= sx1 and sy0 <= inner[3] and inner[1] <= sy1 and _segment_hits_box(a, b, inner)
                   for sx0, sy0, sx1, sy1, a, b in (pieces[number] for number in near[index])):
                # A bound arrow routes around what is in its way (phase 2, 3.4); a free one is moved by hand. One that
                # already routes orthogonal found no clear way: offering the route it has would change nothing, and an
                # agent that follows the fix would loop (QA phase 2, R1).
                bound = bool(arrow.get("from") or arrow.get("to"))
                routed = (arrow.get("style") or {}).get("route") in ("orthogonal", "elbow")
                fix = {"op": "restyle", "ids": [arrow["id"]], "route": "orthogonal", "intent": "route around {}".format(el["id"])} \
                    if bound and not routed else None
                if fix is not None:
                    advice = "route it around it (the fix: route orthogonal)"
                elif bound:
                    advice = "there is no clear way around it: move {} or an end of the arrow to open a gap".format(el["id"])
                else:
                    advice = "move it, or move {} out of its way".format(el["id"])
                out.append(_problem("arrow_through", [arrow["id"], el["id"]],
                                    "arrow {}{} passes through {}; {}".format(arrow["id"], _label(arrow), _name(el), advice),
                                    fix, reader, by_id))
    return out


#: How far a claim's boundary is grown, at most, to take in the marks it cuts: a multiple of the area asked for.
#: A claim carries authority (what it refuses others), so it may tidy its edge and never annex a neighbourhood.
CLAIM_SNAP_MAX_AREA = 3.0
#: How many times the snap looks again after growing (growing can reach a mark the first pass did not touch).
CLAIM_SNAP_PASSES = 4


#: How much of a mark a claim has to hold before its boundary counts as drawn *through* the mark rather than beside
#: it. A claim's edge always ends somewhere, and ending in the margin of the next mark along is not a problem worth
#: an agent's turn; holding most of a chart and cutting its axis labels off is (V2). It is also what keeps the snap
#: honest: a claim never grows over a mark it holds less than half of, because that mark is its neighbour's.
CLAIM_CUT_SHARE = 0.5


def claim_cuts(region: Sequence[float], marks: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The marks this region's boundary is drawn through: most of the mark inside it, and the rest outside.

    A mark wholly inside a claim is claimed and a mark mostly outside is its neighbour's; one the claim holds most of
    is drawn half in one authority and half in another, which is what a reader sees as a line through their words.
    """
    box = (float(region[0]), float(region[1]), float(region[2]), float(region[3]))
    out = []
    for el in marks:
        other = box_of(el)
        if _contains(box, other) or not _intersects(box, other, TOUCH):
            continue
        width, height = _overlap(box, other)
        area = max(1.0, (other[2] - other[0]) * (other[3] - other[1]))
        if width * height >= CLAIM_CUT_SHARE * area:
            out.append(el)
    return out


#: How far past a claim's region its border may run, in world units, and how far inside it: the dashed rectangle is
#: drawn ``canvas_display.CLAIM_BORDER_OUT`` outside the region with a 2 px stroke, and the widest that stroke is in
#: world units at the zooms these numbers stand for is one pixel either side at the closest zoom a top-level frame
#: still stands its title above itself (``canvas_geometry.FRAME_TITLE_LOD``). Deeper zooms are the display's to clear
#: (``canvas_display._claim_rects``), because there the title grows on screen and no region can hold it.
def _border_reach() -> Tuple[float, float]:
    from herdr_team import canvas_display as _display  # it imports this module's neighbours: late on purpose
    from herdr_team import canvas_geometry as _geo

    half = 1.0 / float(_geo.FRAME_TITLE_LOD)
    return float(_display.CLAIM_BORDER_OUT) - half, float(_display.CLAIM_BORDER_OUT) + half


#: How far from a claim's region a line of text can be and still meet its border: the border's reach and a pill's
#: own size. Text further out is never looked at, so a claim on a 2,000-mark board measures its own neighbourhood.
CLAIM_TEXT_NEAR = 400.0


def claim_texts(elements: Iterable[Dict[str, Any]], near: Optional[Sequence[float]] = None) -> List[Dict[str, Any]]:
    """The lines of text a claim's border must never be drawn through that are not marks, as ``{"id", "box", "what"}``.

    An arrow's label pill where the canvas placed it, and a frame's title: in its band, and for a top-level frame
    standing above it at the closest zoom that stands it there. A title and a pill are not marks - an arrow and a frame
    are not - which is exactly why ``claim_edge`` called a board clean with a dashed line through "Checkout flow" and
    through "cancels a job the user / no longer wants" at every zoom (layout findings N4). Taken from each element's
    own display entry, so a check can never disagree with the picture about where the words are. ``near`` limits it to
    the text within ``CLAIM_TEXT_NEAR`` of a box.
    """
    from herdr_team import canvas_display as _display  # late, as above
    from herdr_team import canvas_geometry as _geo
    from herdr_team import canvas_render as _render

    reach = None if near is None else (near[0] - CLAIM_TEXT_NEAR, near[1] - CLAIM_TEXT_NEAR,
                                       near[2] + CLAIM_TEXT_NEAR, near[3] + CLAIM_TEXT_NEAR)
    listed = [el for el in elements if isinstance(el, dict) and el.get("id")]
    out: List[Dict[str, Any]] = []
    env = None
    for el in listed:
        kind = el.get("type")
        if kind == "arrow" and str(el.get("text") or "").strip():
            pill = _geo.arrow_label_pill(el)
            if pill is None:
                continue
            x, y, w, h = pill[0]
            box = (x, y, x + w, y + h)
            if reach is None or _intersects(reach, box):
                out.append({"id": str(el["id"]), "box": box, "what": "label", "text": str(el.get("text") or "")})
        elif kind == "frame" and str(el.get("text") or "").strip():
            if reach is not None and not _intersects(reach, box_of(el)):
                continue
            if env is None:
                env = _display.environment({"elements": listed})
            entry = _display.entry(el, env)
            seen = set()
            for u in (1.0, 1.0 / float(_geo.FRAME_TITLE_LOD) + 1e-6):
                for box in _render.text_boxes({"entries": [entry]}, u):
                    key = tuple(round(v, 2) for v in box)
                    if key not in seen:
                        seen.add(key)
                        out.append({"id": str(el["id"]), "box": box, "what": "title", "text": str(el.get("text") or "")})
    return out


def claim_text_cuts(region: Sequence[float], texts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The lines of text (``claim_texts``) this region's dashed border is drawn through: neither wholly inside the
    stroke nor wholly outside it. Unlike a mark, a line of text has no share that makes a cut acceptable - a dashed
    line through the edge of a word is still a line through a word."""
    inward, outward = _border_reach()
    x0, y0, x1, y1 = (float(v) for v in region)
    inner = (x0 - inward, y0 - inward, x1 + inward, y1 + inward)
    outer = (x0 - outward, y0 - outward, x1 + outward, y1 + outward)
    found = []
    for text in texts:
        t = text["box"]
        if _contains(inner, t) or t[2] <= outer[0] or t[0] >= outer[2] or t[3] <= outer[1] or t[1] >= outer[3]:
            continue
        found.append(text)
    return found


def claim_snap(region: Sequence[float], marks: Iterable[Dict[str, Any]],
               max_area: float = CLAIM_SNAP_MAX_AREA, texts: Optional[Iterable[Dict[str, Any]]] = None) -> Tuple[List[int], List[str]]:
    """``(region, ids)``: the region grown outward to hold whole the marks its boundary cut, and which those were.

    Bounded by ``max_area`` times the area asked for, and it stops before the step that would pass it, so a claim
    beside one big diagram never swallows it. Pure, and the same answer whatever order the marks come in.

    ``texts`` is the board's elements (``ctx.live()``): the region then also grows to hold whole every frame title and
    arrow pill its border would be drawn through (``claim_texts``), whatever share of it is inside - a claim that
    stops its edge in the margin of the next mark is fine, one that stops it in the middle of a word is not. Growing,
    never shrinking, so the repair ``claim_edge`` prints holds the claim it corrects and replaces it (the ``claim`` op
    releases an own claim the new region holds whole).
    """
    listed = [el for el in marks if isinstance(el, dict) and el.get("id")]
    box = [float(region[0]), float(region[1]), float(region[2]), float(region[3])]
    room = max(1.0, (box[2] - box[0]) * (box[3] - box[1])) * max(1.0, float(max_area))
    board = [el for el in texts if isinstance(el, dict)] if texts is not None else []
    inward, _outward = _border_reach()
    taken: List[str] = []
    for _pass in range(CLAIM_SNAP_PASSES):
        cuts = sorted(claim_cuts(box, (el for el in listed if str(el["id"]) not in taken)), key=lambda el: str(el["id"]))
        words = sorted(claim_text_cuts(box, claim_texts(board, box)), key=lambda t: (t["id"], t["box"])) if board else []
        if not cuts and not words:
            break
        grown = list(box)
        for el in cuts:
            other = box_of(el)
            candidate = [min(grown[0], other[0]), min(grown[1], other[1]), max(grown[2], other[2]), max(grown[3], other[3])]
            if (candidate[2] - candidate[0]) * (candidate[3] - candidate[1]) > room:
                continue
            grown = candidate
            taken.append(str(el["id"]))
        for text in words:
            # Hold the line whole *inside* the stroke: the border runs ``inward`` past the region's edge at most.
            t = text["box"]
            candidate = [min(grown[0], t[0] + inward - 1.0), min(grown[1], t[1] + inward - 1.0),
                         max(grown[2], t[2] - inward + 1.0), max(grown[3], t[3] - inward + 1.0)]
            if (candidate[2] - candidate[0]) * (candidate[3] - candidate[1]) > room:
                continue
            grown = candidate
            if text["id"] not in taken:
                taken.append(text["id"])
        if grown == box:
            break
        box = grown
    return [int(math.floor(box[0])), int(math.floor(box[1])), int(math.ceil(box[2])), int(math.ceil(box[3]))], taken


def _text_name(text: Dict[str, Any]) -> str:
    words = " ".join(str(text.get("text") or "").split())
    words = words if len(words) <= 40 else words[:39] + "…"
    return '{}\'s {} "{}"'.format(text["id"], "label" if text["what"] == "label" else "title", words)


def _claim_edges(claims: Sequence[Dict[str, Any]], marks: List[Dict[str, Any]], reader: Optional[str],
                 by_id: Dict[str, Dict[str, Any]], live: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Every mark an active claim's boundary crosses (V2), and every frame title and arrow label it is drawn through
    (N4): the overlay class the agent's feedback loop could not see.

    A claim is drawn as a dashed rectangle over the board, so its edge through a chart's axis labels reads as the
    chart being cut off - which is how the operator found it, and check called the board clean. A title and a pill
    are not marks, so the first version of this check could not see the same line drawn through "Checkout flow".
    """
    out = []
    for claim in claims or ():
        region = claim.get("region")
        if not isinstance(region, (list, tuple)) or len(region) != 4:
            continue
        cuts = claim_cuts(region, marks)
        words = claim_text_cuts(region, claim_texts(live, region)) if live else []
        if not cuts and not words:
            continue
        author = str(claim.get("author") or "")
        snapped, took = claim_snap(region, marks, texts=live)
        # A snap that holds none of them whole (it would pass ``claim_snap``'s bound) has no operation to offer: say so.
        mine = bool(reader) and (author == reader or reader == "human") and bool(took)
        named = [_name(el) for el in cuts] + [_text_name(t) for t in words]
        ids = [str(el["id"]) for el in cuts] + [t["id"] for t in words if t["id"] not in {str(el["id"]) for el in cuts}]
        ids = list(dict.fromkeys(ids))
        fix = {"op": "claim", "region": snapped, "intent": "claim the region that does not cut {}".format(
            ", ".join(ids[:3]))} if mine else None
        if fix is not None and str(claim.get("label") or ""):
            fix["label"] = str(claim["label"])[:80]
        advice = ("claim {} instead, which holds them whole (this one expires on its own)".format(snapped) if mine
                  else "claim a region that holds them whole, or leave them to {}".format(author or "their author")
                  if author == reader or reader == "human" else "ask {} to claim a region that holds them whole".format(author or "its author"))
        found = _problem("claim_edge", [str(claim.get("id") or "")] + ids,
                         "claim {}'s edge cuts across {}; {}".format(claim.get("id"), ", ".join(named[:3]), advice),
                         fix, reader, by_id)
        out.append(found)
    return out


def _gap(box: Sequence[float], other: Sequence[float]) -> float:
    return max(0.0, max(other[0] - box[2], box[0] - other[2])) + max(0.0, max(other[1] - box[3], box[1] - other[3]))


def _strays(marks: List[Dict[str, Any]], reader: Optional[str], by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(marks) < STRAY_MIN_MARKS:
        return []
    boxes = [(el, box_of(el)) for el in marks]
    # A mark with another within STRAY_GAP is not a stray, and that other meets its box grown by STRAY_GAP: look only in the
    # grid cells that box covers (a 2,000-mark board was 4 million pairs; QA phase 5 L13). Marks too big for the grid are
    # always looked at. Only a stray pays for its exact nearest distance.
    cell = STRAY_GAP
    grid: Dict[Tuple[int, int], List[int]] = {}
    big: List[int] = []

    def cells(box: Sequence[float]) -> Optional[List[Tuple[int, int]]]:
        i0, i1 = int(math.floor(box[0] / cell)), int(math.floor(box[2] / cell))
        j0, j1 = int(math.floor(box[1] / cell)), int(math.floor(box[3] / cell))
        if (i1 - i0 + 1) * (j1 - j0 + 1) > 64:
            return None
        return [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]

    for index, (_el, box) in enumerate(boxes):
        found = cells(box)
        if found is None:
            big.append(index)
            continue
        for key in found:
            grid.setdefault(key, []).append(index)
    out = []
    for index, (el, box) in enumerate(boxes):
        reach = (box[0] - STRAY_GAP, box[1] - STRAY_GAP, box[2] + STRAY_GAP, box[3] + STRAY_GAP)
        keys = cells(reach)
        # Both branches must be lists: ``big + range(...)`` raises TypeError, which killed every draw on a board
        # holding one mark whose reach spans more than 64 grid cells (a wide label is enough).
        near = big + ([i for key in keys for i in grid.get(key, ())] if keys is not None else list(range(len(boxes))))
        if any(i != index and _gap(box, boxes[i][1]) <= STRAY_GAP for i in near):
            continue
        nearest = min((_gap(box, other) for o, other in boxes if o is not el), default=0.0)
        if nearest > STRAY_GAP:
            out.append(_problem("stray", [el["id"]],
                                "{} is {} units from everything else; move it next to what it belongs with".format(_name(el), int(nearest)),
                                None, reader, by_id))
    return out


# --------------------------------------------------------------------------
# the registry


Problem = Dict[str, Any]


@dataclass(frozen=True)
class Check:
    """One layout check: ``run(elements, env)`` returns problems (``_problem`` dicts)."""

    code: str
    #: Its place in the order ``problems`` sorts by (``SEVERITY``).
    severity: int
    run: Callable[[List[Dict[str, Any]], Dict[str, Any]], List[Problem]]


def _groups(live: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    return {"solid": [el for el in live if el.get("type") in solid_kinds()],
            "frames": [el for el in live if el.get("type") == "frame" and el.get("role") != "portrait"],
            "arrows": [el for el in live if el.get("type") == "arrow"],
            "marks": [el for el in live if el.get("type") not in ("frame", "comment", "arrow")]}


CHECKS: List[Check] = [
    Check("overlap", 0, lambda live, env: _overlaps(env["groups"]["solid"], env["reader"], env["by_id"])),
    Check("label_overflow", 1, lambda live, env: _label_overflows(env["groups"]["solid"], env["reader"], env["by_id"])),
    Check("frame_edge", 2, lambda live, env: _frame_edges(env["groups"]["marks"], env["groups"]["frames"], env["reader"], env["by_id"])),
    Check("arrow_through", 3, lambda live, env: _arrows_through(env["groups"]["arrows"], env["groups"]["solid"], env["reader"], env["by_id"])),
    Check("stray", 4, lambda live, env: _strays(env["groups"]["marks"], env["reader"], env["by_id"])),
    Check("claim_edge", 4, lambda live, env: _claim_edges(env["claims"], env["groups"]["marks"], env["reader"], env["by_id"], live)),
]


def register_check(check: Check) -> None:
    """Add a check (``text_on_label`` comes with ``overlap``); a second one with the same code is a programming error."""
    if any(existing.code == check.code for existing in CHECKS):
        raise ValueError("check {} is registered twice".format(check.code))
    CHECKS.append(check)
    SEVERITY.setdefault(check.code, check.severity)


def _kind_checks(live: List[Dict[str, Any]], env: Dict[str, Any]) -> List[Problem]:
    """Each element's own kind checks (``Kind.checks``), made into full problems."""
    out: List[Problem] = []
    for el in live:
        kind = _kinds.kind_of(el)
        for check in kind.checks if kind is not None else ():
            for found in check(el, env) or []:
                ids = [str(i) for i in found.get("ids") or [el.get("id")]]
                if isinstance(found.get("severity"), int) and not isinstance(found.get("severity"), bool):
                    SEVERITY.setdefault(str(found.get("code") or "kind"), int(found["severity"]))  # a kind's own code sorts where it says
                out.append(_problem(str(found.get("code") or "kind"), ids, str(found.get("message") or ""), found.get("fix"),
                                    env["reader"], env["by_id"]))
    return out


def problems(elements: Iterable[Dict[str, Any]], reader: Optional[str] = None, region: Optional[Sequence[float]] = None,
             claims: Optional[Sequence[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Every layout problem on these elements, most serious first and the reader's own first within each kind.

    ``region`` keeps the problems that touch it (either mark in view). ``claims`` are the scene's active claims,
    which are not elements but are drawn over them (``claim_edge``); without them that check finds nothing.
    """
    live = [el for el in elements if isinstance(el, dict) and el.get("id") and not el.get("deleted")]
    by_id = {str(el["id"]): el for el in live}
    env: Dict[str, Any] = {"reader": reader, "by_id": by_id, "groups": _groups(live),
                           "claims": [c for c in claims or () if isinstance(c, dict)]}
    found: List[Problem] = []
    for check in CHECKS:
        found.extend(check.run(live, env))
    found.extend(_kind_checks(live, env))
    if region is not None:
        found = [p for p in found if any(_intersects(box_of(by_id[i]), region) for i in p["ids"] if i in by_id)]
    found.sort(key=lambda p: (SEVERITY.get(p["code"], 9), not p["yours"]))
    return found


def problem_lines(found: List[Dict[str, Any]], limit: Optional[int] = None) -> List[str]:
    """Indented text lines: each problem, then its fix as operation JSON."""
    import json

    lines = []
    for problem in found[:limit] if limit else found:
        lines.append("  - {}: {}".format(problem["code"], problem["message"]))
        if problem.get("fix"):
            lines.append("    fix: {}".format(json.dumps(problem["fix"], separators=(",", ":"))))
    if limit and len(found) > limit:
        lines.append("  … {} more: herdr-synapse canvas check".format(len(found) - limit))
    return lines
