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
  measured the way the renderer wraps it (``canvas_render.wrap_text``);
* ``frame_edge``: a mark half inside a frame;
* ``arrow_through``: an arrow crossing a mark it does not connect;
* ``stray``: a mark far from everything else.

Every problem carries ``ids``, a one-line ``message``, and ``fix``: a ready
``move`` operation when there is an obvious one (``None`` otherwise, the
message says what to do). Pure: reads element dicts, writes nothing.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from herdr_team import canvas_render as _render

#: Marks that take room on the canvas and so can collide. Arrows, pens and comments are drawn over things on purpose.
SOLID = frozenset(("box", "ellipse", "diamond", "note", "text", "path", "svg", "mermaid", "chart", "viz", "image"))
LABELLED_SHAPES = frozenset(("box", "ellipse", "diamond", "note"))
#: How much of a shape's box its label may use (an ellipse's and a diamond's inner boxes are smaller).
LABEL_ROOM = {"box": 1.0, "note": 1.0, "ellipse": math.sqrt(0.5), "diamond": 0.5}
LABEL_PAD = 16
#: Overlaps thinner than this are edges touching, not marks on top of each other.
TOUCH = 4.0
#: A mark this far from its nearest neighbour is reported as stray (with at least ``STRAY_MIN_MARKS`` marks).
STRAY_GAP = 1500.0
STRAY_MIN_MARKS = 4
FRAME_PAD = 20
#: Problems a ``look`` shows; ``canvas check`` lists every one.
LOOK_MAX = 8
SEVERITY = {"overlap": 0, "text_on_label": 0, "label_overflow": 1, "frame_edge": 2, "arrow_through": 3, "stray": 4}


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
        fix = dict(fix, intent="fix the layout: {} ({})".format(code.replace("_", " "), ", ".join(ids)))
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
              frame: Optional[Tuple[float, float, float, float]]) -> Optional[Tuple[int, int]]:
    """A top-left corner for ``el`` beside ``other`` that touches no other mark and stays inside ``frame`` (when given)."""
    x0, y0, x1, y1 = box_of(el)
    w, h = x1 - x0, y1 - y0
    ox0, oy0, ox1, oy1 = box_of(other)

    def free(x: float, y: float) -> bool:
        box = (x, y, x + w, y + h)
        if frame is not None and not _contains(frame, box):
            return False
        return not any(_intersects(box, b, TOUCH) for eid, b in boxes if eid != el["id"])

    candidates = [(ox1 + GAP, y0), (x0, oy1 + GAP), (ox0 - w - GAP, y0), (x0, oy0 - h - GAP)]
    for ring in range(1, SEARCH_RINGS + 1):
        step = ring * GRID
        candidates += [(x0 + dx, y0 + dy) for dx in (-step, 0, step) for dy in (-step, 0, step) if dx or dy]
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
                if inner.get("type") == "text" and outer.get("type") in LABELLED_SHAPES and outer.get("text"):
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
    """``(w, h)`` a labelled shape needs for its label as the renderer wraps it, or None when it fits."""
    text = str(el.get("text") or "")
    if el.get("type") not in LABELLED_SHAPES or not text.strip():
        return None
    style = el.get("style") if isinstance(el.get("style"), dict) else {}
    size = float(style.get("size") or 20)
    room = LABEL_ROOM.get(str(el.get("type")), 1.0)
    x0, y0, x1, y1 = box_of(el)
    w, h = x1 - x0, y1 - y0
    longest = max((len(word) for line in text.split("\n") for word in line.split()), default=0)
    need_w = w
    if longest > _render.chars_per_line(w * room - LABEL_PAD, size):
        need_w = math.ceil((longest * _render.CHAR_W * size + LABEL_PAD) / room)
    lines = len(_render.wrap_text(text, _render.chars_per_line(need_w * room - LABEL_PAD, size)))
    need_h = max(h, math.ceil((lines * 1.25 * size + LABEL_PAD) / room))
    if need_w <= w and need_h <= h:
        return None
    return int(math.ceil(need_w)), int(math.ceil(need_h))


def _label_overflows(solid: List[Dict[str, Any]], reader: Optional[str], by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for el in solid:
        need = label_room(el)
        if need is None:
            continue
        x0, y0, x1, y1 = box_of(el)
        out.append(_problem("label_overflow", [el["id"]],
                            "{}'s label needs {}x{} but the shape is {}x{}; resize it (or shorten the label)".format(
                                _name(el), need[0], need[1], int(x1 - x0), int(y1 - y0)),
                            {"op": "move", "id": el["id"], "w": need[0], "h": need[1]}, reader, by_id))
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
    return out


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


def _arrows_through(arrows: List[Dict[str, Any]], solid: List[Dict[str, Any]], reader: Optional[str], by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for arrow in arrows:
        points = [p for p in (arrow.get("points") or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(points) < 2:
            continue
        ends = {arrow.get("from"), arrow.get("to")}
        arrow_box = box_of(arrow)
        for el in solid:
            if el["id"] in ends:
                continue
            box = box_of(el)
            inner = (box[0] + TOUCH, box[1] + TOUCH, box[2] - TOUCH, box[3] - TOUCH)
            if inner[0] >= inner[2] or inner[1] >= inner[3] or not _intersects(arrow_box, box):
                continue
            # An end drawn from a point inside a shape starts there on purpose; only the path through counts.
            if any(inner[0] < p[0] < inner[2] and inner[1] < p[1] < inner[3] for p in (points[0], points[-1])):
                continue
            if any(_segment_hits_box(points[i], points[i + 1], inner) for i in range(len(points) - 1)):
                out.append(_problem("arrow_through", [arrow["id"], el["id"]],
                                    "arrow {}{} passes through {}; route it around (points, or curve) or move {} out of its way".format(
                                        arrow["id"], _label(arrow), _name(el), el["id"]),
                                    None, reader, by_id))
    return out


def _strays(marks: List[Dict[str, Any]], reader: Optional[str], by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(marks) < STRAY_MIN_MARKS:
        return []
    boxes = [(el, box_of(el)) for el in marks]
    out = []
    for el, box in boxes:
        nearest = min((max(0.0, max(other[0] - box[2], box[0] - other[2])) + max(0.0, max(other[1] - box[3], box[1] - other[3]))
                       for o, other in boxes if o is not el), default=0.0)
        if nearest > STRAY_GAP:
            out.append(_problem("stray", [el["id"]],
                                "{} is {} units from everything else; move it next to what it belongs with".format(_name(el), int(nearest)),
                                None, reader, by_id))
    return out


def problems(elements: Iterable[Dict[str, Any]], reader: Optional[str] = None, region: Optional[Sequence[float]] = None) -> List[Dict[str, Any]]:
    """Every layout problem on these elements, most serious first and the reader's own first within each kind.

    ``region`` keeps the problems that touch it (either mark in view).
    """
    live = [el for el in elements if isinstance(el, dict) and el.get("id") and not el.get("deleted")]
    by_id = {str(el["id"]): el for el in live}
    solid = [el for el in live if el.get("type") in SOLID]
    frames = [el for el in live if el.get("type") == "frame" and el.get("role") != "portrait"]
    arrows = [el for el in live if el.get("type") == "arrow"]
    marks = [el for el in live if el.get("type") not in ("frame", "comment", "arrow")]
    found = (_overlaps(solid, reader, by_id) + _label_overflows(solid, reader, by_id) + _frame_edges(marks, frames, reader, by_id)
             + _arrows_through(arrows, solid, reader, by_id) + _strays(marks, reader, by_id))
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
