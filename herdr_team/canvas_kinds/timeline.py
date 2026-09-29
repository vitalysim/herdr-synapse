"""Timelines (canvas v2 phase 2, 4.6): events on a horizontal axis, dated or in order.

A timeline is a frame (``block: "timeline"``) whose events are ``card`` members
(size s, 200 wide, title and body 2 lines each). When every ``at`` is a date
(``YYYY-MM-DD`` or ``YYYY-MM``) the axis is time, mapped linearly, with ticks
at a scale that suits the span (days, weeks, months, quarters, years); otherwise
it is in input order, evenly spaced (and mixing the two warns
``date_unparsed``). Cards pack greedily into levels that alternate above and
below the axis, as close to it as they fit; each has a stem to a dot on its
date; an event with an ``end`` is a bar on the axis; a ``milestone`` a diamond.

The arrangement is the kind's own geometry, not a general layout: a person's
drag pins an event where it was put (phase 2, D5), and the others pack around it.
"""
from __future__ import annotations

import datetime
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_theme
from herdr_team.canvas_kinds import Arrangement, Kind, OpSpec
from herdr_team.canvas_kinds import _zone
from herdr_team.canvas_kinds import card as _card
from herdr_team.canvas_kinds._common import Element
from herdr_team.canvas_kinds.sdk import Block, Collection

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 139

_TOKENS = canvas_theme.section("timeline", {}) or {}
CARD_W = float(_TOKENS.get("card_w", 200))
MIN_W = float(_TOKENS.get("min_w", 800))
GAP = float(_TOKENS.get("gap", 20))
AXIS_W = float(_TOKENS.get("axis", 2))
SPAN_H = float(_TOKENS.get("span", 6))
LEVEL_GAP = float(_TOKENS.get("level_gap", 24))
STEM_GAP = float(_TOKENS.get("stem_gap", 16))
CLAMP = dict(((canvas_theme.section("card", {}) or {}).get("timeline_clamp") or {"title": 2, "body": 2}))
MAX_EVENTS = 100
SCALES = ("auto", "day", "week", "month", "quarter", "year", "ordinal")
TICK_SIZE, TICK_WEIGHT = 12.0, 500
#: Room under the axis for its tick labels, and the dot on an event's date.
TICK_ROOM = 28.0
DOT_R = 5.0
_DATE = re.compile(r"^\s*(\d{4})-(\d{2})(?:-(\d{2}))?\s*\Z")
EVENT_KEYS = ("id", "at", "end", "title", "body", "tone", "icon", "milestone", "detail", "_gen")


def parse_date(value: Any) -> Optional[datetime.date]:
    """``YYYY-MM-DD`` or ``YYYY-MM`` (the first of the month) as a date, else None."""
    match = _DATE.match(value) if isinstance(value, str) else None
    if not match:
        return None
    try:
        return datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3) or 1))
    except ValueError:
        return None


def event_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} is {{at, title, end, body, tone, icon, milestone, detail}}".format(field))
    for key in raw:
        if key not in EVENT_KEYS:
            raise ctx.invalid("{}.{}".format(field, key), "an event takes {}".format(", ".join(k for k in EVENT_KEYS if not k.startswith("_"))))
    out = _card.card_fields(ctx, raw, field)
    at = ctx.text(raw, "at", limit="label", one_line=True, required=True, label=field + ".at")
    out["at"] = at
    if raw.get("end") is not None:
        out["end"] = ctx.text(raw, "end", limit="label", one_line=True, label=field + ".end")
    if raw.get("milestone"):
        out["milestone"] = ctx.boolean(raw, "milestone", False)
    if raw.get("id") is not None:
        if not isinstance(raw["id"], str) or not re.match(r"^[A-Za-z][A-Za-z0-9_-]{0,31}\Z", raw["id"]):
            raise ctx.invalid(field + ".id", "an event id is a letter then up to 31 letters, digits, _ or -")
        out["id"] = raw["id"]
    if raw.get("_gen"):
        out["_gen"] = True
    return out


def normalize(ctx: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    spec["title"] = ctx.text(spec, "title", limit="label", one_line=True)
    spec["scale"] = ctx.choice(spec, "scale", SCALES, "auto")
    events = spec.get("events") or []
    if not events:
        raise ctx.invalid("events", 'a timeline needs events: [{"at": "2026-10-05", "title": "Beta"}]')
    for index, event in enumerate(events):
        if event.get("end") is not None and parse_date(event["at"]) is not None and parse_date(event["end"]) is not None \
                and parse_date(event["end"]) < parse_date(event["at"]):
            raise ctx.invalid("events[{}].end".format(index), "end comes before at")
    return spec


# --------------------------------------------------------------------------
# the axis


def _events(members: Sequence[Element]) -> List[Element]:
    return [el for el in members if el.get("type") == "card" and isinstance(el.get("part"), str)]


def axis(root: Element, events: Sequence[Element]) -> Dict[str, Any]:
    """The axis's mode (``time`` or ``ordinal``), scale, extent in days (time) and length in units."""
    settings = _zone.settings_of(root)
    dates = [parse_date(e.get("at")) for e in events]
    ends = [parse_date(e.get("end")) for e in events if e.get("end")]
    timed = bool(events) and all(d is not None for d in dates) and settings.get("scale") != "ordinal"
    length = max(MIN_W, len(events) * (CARD_W + GAP))
    if not timed:
        return {"mode": "ordinal", "scale": "ordinal", "length": length, "count": len(events)}
    first = min(d for d in dates if d is not None)
    last = max([d for d in dates if d is not None] + [d for d in ends if d is not None])
    days = max(1, (last - first).days)
    scale = settings.get("scale") if settings.get("scale") not in (None, "auto") else (
        "day" if days <= 45 else "month" if days <= 548 else "quarter" if days <= 1460 else "year")
    return {"mode": "time", "scale": scale, "first": first, "days": days, "length": length}


def position(info: Dict[str, Any], event: Element, index: int) -> float:
    """Where an event's date falls along the axis, from its start (card centres in ordinal mode)."""
    if info["mode"] == "ordinal":
        step = info["length"] / max(1, info["count"])
        return step * index + step / 2.0
    date = parse_date(event.get("at"))
    inset = CARD_W / 2.0
    usable = info["length"] - 2 * inset
    return inset + usable * (((date - info["first"]).days if date else 0) / float(info["days"]))


def end_position(info: Dict[str, Any], event: Element) -> Optional[float]:
    date = parse_date(event.get("end"))
    if info["mode"] != "time" or date is None:
        return None
    inset = CARD_W / 2.0
    return inset + (info["length"] - 2 * inset) * ((date - info["first"]).days / float(info["days"]))


def ticks(info: Dict[str, Any]) -> List[Tuple[float, str]]:
    """Tick marks and labels along a time axis at its scale (weeks label days' ticks; months, quarters, years)."""
    if info["mode"] != "time":
        return []
    first, days = info["first"], info["days"]
    inset = CARD_W / 2.0
    usable = info["length"] - 2 * inset
    out: List[Tuple[float, str]] = []
    scale = info["scale"]
    cursor = first
    last = first + datetime.timedelta(days=days)
    guard = 0
    while cursor <= last and guard < 400:
        guard += 1
        label = ""
        if scale in ("day", "week"):
            label = cursor.strftime("%b %d") if cursor.weekday() == 0 or cursor == first else ""
            step = datetime.timedelta(days=1 if scale == "day" else 7)
            nxt = cursor + step
        elif scale == "month":
            label = cursor.strftime("%b %Y") if cursor.month == 1 or cursor == first else cursor.strftime("%b")
            nxt = datetime.date(cursor.year + (cursor.month // 12), cursor.month % 12 + 1, 1)
        elif scale == "quarter":
            label = "Q{} {}".format((cursor.month - 1) // 3 + 1, cursor.year)
            month = ((cursor.month - 1) // 3 + 1) * 3 + 1
            nxt = datetime.date(cursor.year + (month > 12), (month - 1) % 12 + 1, 1)
        else:
            label = str(cursor.year)
            nxt = datetime.date(cursor.year + 1, 1, 1)
        if cursor >= first:
            out.append((inset + usable * ((cursor - first).days / float(days)), label))
        if scale in ("month", "quarter", "year") and cursor == first:
            nxt = nxt if nxt > cursor else cursor + datetime.timedelta(days=1)
        cursor = nxt
    return out


def arrange(root: Element, members: List[Element], env: Dict[str, Any]) -> Arrangement:
    """Cards packed by interval into levels alternating above and below the axis, as close as they fit; a pinned one
    holds. The root's ``axis`` (its line, ticks and each event's stem, dot, bar or marker) comes with it."""
    events = sorted(_events(members), key=lambda e: _order_key(root, e))
    info = axis(root, events)
    cx0, cy0, _cx1, _cy1 = _zone.content_box(root)
    held = set(env.get("held") or [])
    tallest = max([float(e.get("h") or 80) for e in events] + [60.0])
    band = tallest + LEVEL_GAP
    levels: Dict[bool, List[List[Tuple[float, float]]]] = {True: [], False: []}
    slots: Dict[str, Tuple[float, int, bool]] = {}
    for index, event in enumerate(events):
        if event["id"] in held:
            continue
        centre = position(info, event, index)
        left, right = centre - CARD_W / 2.0, centre + CARD_W / 2.0
        level, above = _slot(levels[True], levels[False], left, right, index)
        levels[above][level].append((left, right))
        slots[event["id"]] = (left, level, above)
    n_above = len([lv for lv in levels[True] if lv]) or (1 if not levels[False] else 0)
    n_below = len([lv for lv in levels[False] if lv])
    axis_y = cy0 + max(1, n_above) * band - LEVEL_GAP + STEM_GAP
    boxes: Dict[str, Tuple[float, float, float, float]] = {}
    for event in events:
        if event["id"] not in slots:
            continue
        left, level, above = slots[event["id"]]
        w, h = float(event.get("w") or CARD_W), float(event.get("h") or 80)
        y = axis_y - STEM_GAP - level * band - h if above else axis_y + TICK_ROOM + STEM_GAP + level * band
        boxes[event["id"]] = (cx0 + left + (CARD_W - w) / 2.0, y, w, h)
    bottom = axis_y + TICK_ROOM + (STEM_GAP + (n_below - 1) * band + tallest if n_below else 0.0)
    pad = _zone.padding(root)
    x0, y0, _x1, _y1 = D.box_of(root)
    width, height = info["length"] + 2 * pad, bottom - y0 + pad
    final = {e["id"]: (boxes[e["id"]] if e["id"] in boxes else _xywh(D.box_of(e))) for e in events}
    for eid in held:
        if eid in final:
            bx, by, bw, bh = final[eid]
            width, height = max(width, bx + bw - x0 + pad), max(height, by + bh - y0 + pad)
    marks = []
    for index, event in enumerate(events):
        bx, by, bw, bh = final[event["id"]]
        mx = cx0 + position(info, event, index)
        stem = [by + bh, axis_y - DOT_R] if by + bh <= axis_y else [axis_y + DOT_R, by]
        tone = (event.get("style") or {}).get("tone") if isinstance(event.get("style"), dict) else None
        mark: Dict[str, Any] = {"x": _r(mx), "stem": [_r(stem[0]), _r(stem[1])], "tone": tone if tone in canvas_theme.TONES else "neutral"}
        end = end_position(info, event)
        if end is not None:
            mark["end"] = _r(cx0 + end)
        if event.get("milestone"):
            mark["milestone"] = True
        marks.append(mark)
    drawn = {"x0": _r(cx0), "x1": _r(cx0 + info["length"]), "y": _r(axis_y), "mode": info["mode"], "scale": info["scale"],
             "ticks": [[_r(cx0 + t), label] for t, label in ticks(info)], "marks": marks}
    if info["mode"] == "time":
        # What a card dropped on the axis reads its date from (``adopt``).
        drawn.update(first=info["first"].isoformat(), days=info["days"])
    return Arrangement(boxes=boxes, root=(x0, y0, _snap(width), _snap(height)), fields={"axis": drawn},
                       stats={"levels": float(len(levels[True]) + len(levels[False]))})


def _r(value: float) -> float:
    rounded = round(float(value), 2)
    return int(rounded) if rounded == int(rounded) else rounded  # type: ignore[return-value]


def _xywh(box: Sequence[float]) -> Tuple[float, float, float, float]:
    return box[0], box[1], box[2] - box[0], box[3] - box[1]


def _snap(value: float) -> float:
    import math

    return float(math.ceil(value / 20.0 - 1e-9) * 20)


def _order_key(root: Element, event: Element) -> Tuple[int, int]:
    order = [str(i) for i in root.get("order") or []]
    part = str(event.get("part") or "")
    number = int(part[1:]) if part[1:].isdigit() else 0
    return (order.index(event["id"]) if event["id"] in order else len(order), number)


def _slot(above: List[List[Tuple[float, float]]], below: List[List[Tuple[float, float]]], left: float, right: float, index: int) -> Tuple[int, bool]:
    """The nearest free level, alternating sides (above first for even events)."""
    def free(level: List[Tuple[float, float]]) -> bool:
        return all(right + GAP <= a or left >= b + GAP for a, b in level)

    first_above = index % 2 == 0
    for level in range(MAX_EVENTS):
        for side_above in ((True, False) if first_above else (False, True)):
            levels = above if side_above else below
            while len(levels) <= level:
                levels.append([])
            if free(levels[level]):
                return level, side_above
    return 0, True


# --------------------------------------------------------------------------
# drawing the root: the axis, ticks, stems, dots, spans and milestones


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The zone and title, then the axis with its ticks, and for each event its stem, dot, span bar or milestone marker."""
    items = _zone.emit(el, env)
    geo = el.get("axis") if isinstance(el.get("axis"), dict) else None
    if geo is None:
        return items
    try:
        left, right, y = float(geo["x0"]), float(geo["x1"]), float(geo["y"])
    except (KeyError, TypeError, ValueError):
        return items
    items.append({"k": "line", "points": [[left, y], [right, y]], "stroke": "base.line", "sw": AXIS_W})
    for tick in geo.get("ticks") or []:
        tx, label = float(tick[0]), str(tick[1])
        items.append({"k": "line", "points": [[tx, y], [tx, y + 6]], "stroke": "base.line", "sw": 1, "lod": list(D.LOD_BODY)})
        if label:
            items += D.body(D.text_prim([label], tx, y + 8, TICK_SIZE, {}, D.MUTED, "middle", None, TICK_WEIGHT))
    for mark in geo.get("marks") or []:
        tone = mark.get("tone") if mark.get("tone") in canvas_theme.TONES else "neutral"
        mx = float(mark.get("x", left))
        stem = mark.get("stem")
        if isinstance(stem, list) and len(stem) == 2:
            items.append({"k": "line", "points": [[mx, float(stem[0])], [mx, float(stem[1])]], "stroke": "base.line", "sw": 1})
        if mark.get("end") is not None:
            ex = float(mark["end"])
            items.append({"k": "rect", "x": min(mx, ex), "y": y - SPAN_H / 2.0, "w": max(SPAN_H, abs(ex - mx)), "h": SPAN_H, "r": SPAN_H / 2.0,
                          "fill": "tone.{}.solid".format(tone)})
        if mark.get("milestone"):
            r = DOT_R + 3
            items.append({"k": "poly", "points": [[mx, y - r], [mx + r, y], [mx, y + r], [mx - r, y]], "closed": True,
                          "fill": "tone.{}.solid".format(tone), "stroke": "base.surface", "sw": 1.5})
        else:
            items.append({"k": "ellipse", "cx": mx, "cy": y, "rx": DOT_R, "ry": DOT_R, "fill": "tone.{}.solid".format(tone),
                          "stroke": "base.surface", "sw": 1.5})
    return items


def translate(el: Element, dx: float, dy: float) -> Dict[str, Any]:
    """Its axis moves with it."""
    geo = el.get("axis") if isinstance(el.get("axis"), dict) else None
    if geo is None:
        return {}
    try:
        moved = dict(geo, x0=_r(float(geo["x0"]) + dx), x1=_r(float(geo["x1"]) + dx), y=_r(float(geo["y"]) + dy),
                     ticks=[[_r(float(t[0]) + dx), t[1]] for t in geo.get("ticks") or []],
                     marks=[dict(m, x=_r(float(m["x"]) + dx), stem=[_r(float(m["stem"][0]) + dy), _r(float(m["stem"][1]) + dy)],
                                 **({"end": _r(float(m["end"]) + dx)} if m.get("end") is not None else {}))
                            for m in geo.get("marks") or []])
    except (KeyError, TypeError, ValueError, IndexError):
        return {}
    return {"axis": moved}


# --------------------------------------------------------------------------
# build and readback


def build(bctx: Any, spec: Dict[str, Any]) -> None:
    ctx = bctx.ctx
    op = bctx.op
    style = None
    if bctx.root is None or op.get("tone") is not None:
        style = ctx.style({"tone": op["tone"]} if op.get("tone") is not None else {}, "frame")
    fields: Dict[str, Any] = {}
    if bctx.root is None:
        fields.update(fit={"policy": "hug", "min": [int(MIN_W), 200]}, w=int(MIN_W), h=200)
    root = bctx.root_fields(text=spec.get("title") or "", style=style, settings={"scale": spec.get("scale") or "auto", "padding": "m"}, **fields)
    ids = []
    dated = [parse_date(event["at"]) is not None for event in spec["events"]]
    if any(dated) and not all(dated):
        bad = [event["id"] for event, ok in zip(spec["events"], dated) if not ok]
        ctx.warn("date_unparsed", "{} has events with dates and without ({}): its axis is in order, not time".format(
            bctx.alias or root["id"], ", ".join(bad[:5])), [root["id"]])
    for event in spec["events"]:
        stored = _card.element_fields(ctx, event)
        stored["at"] = event["at"]
        if event.get("end"):
            stored["end"] = event["end"]
        if event.get("milestone"):
            stored["milestone"] = True
        card_style = ctx.style({"tone": event.get("tone") or "neutral", "variant": "outline"}, "card")
        el = bctx.member(event["id"], "card", text=event.get("title") or "", style=card_style, frame=root["id"], clamp=dict(CLAMP), size="s",
                         fixed_w=int(CARD_W), minimum=(CARD_W, 60.0), **stored)
        ids.append(el["id"])
    bctx.keep_only([event["id"] for event in spec["events"]])
    bctx.set_order(ctx.el(root["id"]) or root, ids)


def spec(root: Element, members: List[Element], full: bool) -> Dict[str, Any]:
    from herdr_team.canvas_blocks import generated_ids

    out: Dict[str, Any] = {"op": "timeline"}
    if root.get("alias"):
        out["id"] = root["alias"]
    if root.get("text"):
        out["title"] = root["text"]
    scale = _zone.settings_of(root).get("scale")
    if scale and scale != "auto":
        out["scale"] = scale
    events = sorted(_events(members), key=lambda e: _order_key(root, e))
    generated = generated_ids("e", [e["part"] for e in events])
    out["events"] = []
    for event, gen in zip(events, generated):
        item: Dict[str, Any] = {}
        if not gen:
            item["id"] = event["part"]
        item["at"] = event.get("at")
        if event.get("end"):
            item["end"] = event["end"]
        item["title"] = str(event.get("text") or "")
        for key in ("body", "icon", "owner", "status", "detail"):
            if isinstance(event.get(key), str) and event[key]:
                item[key] = event[key]
        tone = (event.get("style") or {}).get("tone") if isinstance(event.get("style"), dict) else None
        if tone and tone != "neutral":
            item["tone"] = tone
        if event.get("milestone"):
            item["milestone"] = True
        out["events"].append(item)
    return out


def adopt(root: Element, el: Element) -> Optional[str]:
    """A card dropped on a timeline joins it as an event (pinned where it landed; on a time axis dated by where it
    landed, so the axis stays in time)."""
    if el.get("type") != "card":
        return None
    seq = root.get("seq") if isinstance(root.get("seq"), dict) else {}
    return "e{}".format(int(seq.get("e") or 0) + 1)


def _convert(root: Element, el: Element) -> Dict[str, Any]:
    """An adopted card's date on a time axis: the day under its centre (QA phase 2, F17); none on an ordinal one."""
    geo = root.get("axis") if isinstance(root.get("axis"), dict) else {}
    first = parse_date(geo.get("first"))
    if geo.get("mode") != "time" or first is None or parse_date(el.get("at")) is not None:
        return {}
    x0, x1, days = float(geo.get("x0") or 0), float(geo.get("x1") or 0), int(geo.get("days") or 1)
    usable = max(1.0, x1 - x0 - CARD_W)
    centre = float(el.get("x") or 0) + float(el.get("w") or CARD_W) / 2.0
    share = min(1.0, max(0.0, (centre - x0 - CARD_W / 2.0) / usable))
    return {"at": (first + datetime.timedelta(days=int(round(share * days)))).isoformat()}


adopt.convert = _convert  # type: ignore[attr-defined]


def unparsed(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    if el.get("block") != "timeline":
        return []
    events = [o for o in (env.get("by_id") or {}).values() if o.get("group") == el.get("id") and o.get("type") == "card"]
    dated = [parse_date(e.get("at")) is not None for e in events]
    if not events or all(dated) or not any(dated):
        return []
    return [{"code": "date_unparsed", "ids": [str(el.get("id"))],
             "message": "{} mixes dated and undated events, so its axis is in order: give every event a YYYY-MM-DD date".format(el.get("id")),
             "fix": None}]


def create(ctx: Any, op: Dict[str, Any]) -> None:
    ctx.block("timeline", op)


OPS = (
    OpSpec(name="timeline", family="block", fields=("title", "events", "scale", "tone", "id", "client_id"), create=create, place=True, order=45,
           doc="events on a horizontal axis, dated (YYYY-MM-DD) or in order, with spans and milestones",
           mcp="timeline {title, events [{at YYYY-MM-DD, end, title, body, tone, icon, milestone}], scale auto|day|week|month|quarter|year|ordinal}"),
)

KINDS = (
    Kind(name="timeline", stored_as="frame", role="composite", ops=("timeline",), cell=True, tone_group="frame", layer="zones",
         block=Block(collections=(Collection(name="events", item=event_item, prefix="e", maximum=MAX_EVENTS, label="title", member="card"),),
                     settings=("scale",), parts="members", positional=True, normalize=normalize, build=build, spec=spec, adopt=adopt,
                     max_members=MAX_EVENTS),
         arrange=arrange, emit=emit, hit=_zone.hit, text_edit=_zone.title_edit, translate=translate, checks=(unparsed,),
         noun=("timeline", "timelines"),
         doc="events on a horizontal axis, dated or in order"),
)
