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
* ``stray``: a mark far from everything else.

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
SEVERITY = {"overlap": 0, "text_on_label": 0, "label_overflow": 1, "label_truncated": 1, "frame_edge": 2, "arrow_through": 3, "stray": 4}


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
        kind = _kinds.get(el.get("type"))
        for check in kind.checks if kind is not None else ():
            for found in check(el, env) or []:
                ids = [str(i) for i in found.get("ids") or [el.get("id")]]
                out.append(_problem(str(found.get("code") or "kind"), ids, str(found.get("message") or ""), found.get("fix"),
                                    env["reader"], env["by_id"]))
    return out


def problems(elements: Iterable[Dict[str, Any]], reader: Optional[str] = None, region: Optional[Sequence[float]] = None) -> List[Dict[str, Any]]:
    """Every layout problem on these elements, most serious first and the reader's own first within each kind.

    ``region`` keeps the problems that touch it (either mark in view).
    """
    live = [el for el in elements if isinstance(el, dict) and el.get("id") and not el.get("deleted")]
    by_id = {str(el["id"]): el for el in live}
    env: Dict[str, Any] = {"reader": reader, "by_id": by_id, "groups": _groups(live)}
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
