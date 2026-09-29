"""Sequence diagrams (canvas v2 phase 2, 4.7.3): participants, messages, notes and groups, held in one element.

A sequence is an inline block (``Block.parts == "inline"``): one element of
type ``sequence`` holds its items, and draws them from a pure ``geometry``
of itself, so a 50-message diagram costs one element, not a hundred. Every
item is still addressable: ``p.<id>`` (a participant), ``m<n>`` (a message's
label) and ``n<n>`` (a note) are its display-list ``parts``, and editing one
is a ``patch``.

* ``participants`` ``[{id, text, icon, tone}]`` (a bare string is an id);
* ``messages`` as shorthand: ``a -> b: text`` a call (solid, triangle head),
  ``a --> b: text`` a reply or async message (dashed, chevron); objects carry
  ``tone``; a message from a participant to itself is a small loop;
* ``notes`` ``[{over: [ids] | left_of | right_of, text, after}]`` (``after``: the
  message number it follows; the end by default);
* ``groups`` ``[{kind: loop|alt|opt|par, label, from, to}]`` (message numbers,
  1-based).

Columns sit as far apart as the widest label between neighbours needs (at
least 160); a row is its label's lines plus room for the arrow; with more
than eight messages the participant boxes repeat at the bottom.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_geometry as G
from herdr_team import canvas_icons, canvas_text, canvas_theme
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds import _text as TX
from herdr_team.canvas_kinds._common import Element, quote
from herdr_team.canvas_kinds.sdk import Block, Collection

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 77

MAX_PARTICIPANTS = 20
MAX_MESSAGES = 200
MAX_NOTES = 50
MAX_GROUPS = 20
GROUP_KINDS = ("loop", "alt", "opt", "par")
PID = re.compile(r"^[A-Za-z0-9_-]{1,32}\Z")
MESSAGE_RE = re.compile(r"^\s*(\w[\w-]{0,31}?)\s*(-->|->)\s*(\w[\w-]{0,31})\s*(?::\s*(.*?))?\s*\Z")
MESSAGE_GRAMMAR = "a message is \"a -> b: text\" (a call) or \"a --> b: text\" (a reply)"

# Geometry (design units).
MARGIN = 16
BAND = 40
TITLE_SIZE = 16
TITLE_WEIGHT = 600
BOX_SIZE, BOX_WEIGHT = 16.0, 500
BOX_MIN = (120.0, 48.0)
BOX_MAX_W = 200.0
BOX_PAD = (16.0, 12.0)
ICON, ICON_ROOM = 20.0, 28.0
COL_MIN = 160.0
COL_ROOM = 40.0
LABEL_SIZE, LABEL_WEIGHT = 16.0, 500
LABEL_MAX_W = 280.0
LABEL_LINES = 2
ROW_EXTRA = 24.0
LOOP_W, LOOP_H = 40.0, 20.0
NOTE_SIZE, NOTE_W, NOTE_PAD = 14.0, 160.0, 8.0
TAB_H, TAB_SIZE, TAB_WEIGHT = 20.0, 12.0, 600
FOOTER_AFTER = 8
LIFELINE_W = 1.5
ARROW_W = 1.5


# --------------------------------------------------------------------------
# items


def participant_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    if isinstance(raw, str):
        raw = {"id": raw}
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} must be an id or {{\"id\", \"text\"}}".format(field))
    for key in raw:
        if key not in ("id", "text", "icon", "tone"):
            raise ctx.invalid("{}.{}".format(field, key), "a participant takes id, text, icon, tone")
    pid = raw.get("id")
    if not isinstance(pid, str) or not PID.match(pid):
        raise ctx.invalid(field + ".id", "a participant id is 1 to 32 letters, digits, _ or -")
    out: Dict[str, Any] = {"id": pid, "text": ctx.text(raw, "text", limit="label", one_line=True, value=raw.get("text", pid),
                                                       label=field + ".text") or pid}
    if raw.get("icon") is not None:
        found = canvas_icons.resolve(raw["icon"]) if isinstance(raw["icon"], str) else None
        if found is None:
            raise ctx.error("icon_unknown", "{} is not an icon; try {}".format(raw["icon"], ", ".join(canvas_icons.suggest(raw["icon"])) or
                                                                               "canvas icons --search"),
                            field=field + ".icon", did_you_mean=canvas_icons.suggest(raw["icon"]))
        out["icon"] = found
    if raw.get("tone") is not None:
        out["tone"] = ctx.choice(raw, "tone", canvas_theme.TONES, "neutral", label=field + ".tone")
    return out


def message_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    if isinstance(raw, str):
        match = MESSAGE_RE.match(raw)
        if match is None:
            raise ctx.invalid(field, "{} does not parse: {}".format(raw[:80], MESSAGE_GRAMMAR))
        a, arrow, b, text = match.groups()
        raw = {"from": a, "to": b, "text": text or ""}
        if arrow == "-->":
            raw["reply"] = True
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} must be \"a -> b: text\" or {{\"from\", \"to\", \"text\"}}".format(field))
    for key in raw:
        if key not in ("id", "from", "to", "text", "reply", "tone"):
            raise ctx.invalid("{}.{}".format(field, key), "a message takes id, from, to, text, reply, tone")
    out: Dict[str, Any] = {}
    if raw.get("id") is not None:
        if not isinstance(raw["id"], str) or not re.match(r"^m[0-9]{1,4}\Z", raw["id"]):
            raise ctx.invalid(field + ".id", "a message id is m<number>")
        out["id"] = raw["id"]
    for end in ("from", "to"):
        if not isinstance(raw.get(end), str) or not PID.match(raw[end]):
            raise ctx.invalid("{}.{}".format(field, end), "{} is not one of the participants".format(raw.get(end)))
        out[end] = raw[end]
    text = ctx.text(raw, "text", limit="label", one_line=True, label=field + ".text")
    if text:
        out["text"] = text
    if raw.get("reply"):
        out["reply"] = True
    if raw.get("tone") is not None:
        out["tone"] = ctx.choice(raw, "tone", canvas_theme.TONES, "neutral", label=field + ".tone")
    return out


def note_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} must be {{\"over\": [ids], \"text\"}}".format(field))
    for key in raw:
        if key not in ("id", "over", "left_of", "right_of", "text", "after"):
            raise ctx.invalid("{}.{}".format(field, key), "a note takes over, left_of, right_of, text, after")
    out: Dict[str, Any] = {}
    if raw.get("id") is not None:
        if not isinstance(raw["id"], str) or not re.match(r"^n[0-9]{1,4}\Z", raw["id"]):
            raise ctx.invalid(field + ".id", "a note id is n<number>")
        out["id"] = raw["id"]
    places = [key for key in ("over", "left_of", "right_of") if raw.get(key) is not None]
    if len(places) != 1:
        raise ctx.invalid(field, "a note is over [ids], left_of an id or right_of an id")
    where = places[0]
    value = raw[where]
    if where == "over":
        value = [value] if isinstance(value, str) else value
        if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
            raise ctx.invalid(field + ".over", "over is a list of participant ids")
    elif not isinstance(value, str):
        raise ctx.invalid("{}.{}".format(field, where), "{} is a participant id".format(where))
    out[where] = value
    out["text"] = ctx.text(raw, "text", limit="label", one_line=True, required=True, label=field + ".text")
    if raw.get("after") is not None:
        out["after"] = int(ctx.number(raw, "after", 0, MAX_MESSAGES))
    return out


def group_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} must be {{\"kind\", \"label\", \"from\", \"to\"}}".format(field))
    for key in raw:
        if key not in ("id", "kind", "label", "from", "to"):
            raise ctx.invalid("{}.{}".format(field, key), "a group takes kind, label, from, to")
    out: Dict[str, Any] = {}
    if raw.get("id") is not None:
        if not isinstance(raw["id"], str) or not re.match(r"^g[0-9]{1,4}\Z", raw["id"]):
            raise ctx.invalid(field + ".id", "a group id is g<number>")
        out["id"] = raw["id"]
    out["kind"] = ctx.choice(raw, "kind", GROUP_KINDS, "loop", label=field + ".kind")
    label = ctx.text(raw, "label", limit="label", one_line=True, label=field + ".label")
    if label:
        out["label"] = label
    for end in ("from", "to"):
        if raw.get(end) is None:
            raise ctx.invalid("{}.{}".format(field, end), "a group spans messages from and to (numbers, 1-based)")
        out[end] = int(ctx.number(raw, end, 1, MAX_MESSAGES))
    return out


def normalize(ctx: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    title = ctx.text(spec, "title", limit="label", one_line=True)
    if title:
        spec["title"] = title
    else:
        spec.pop("title", None)
    participants = spec.get("participants") or []
    if not participants:
        raise ctx.invalid("participants", "a sequence needs participants: [{\"id\": \"u\", \"text\": \"User\"}, ...]")
    for name, limit, maximum in (("participants", "MAX_PARTICIPANTS", MAX_PARTICIPANTS), ("messages", "MAX_MESSAGES", MAX_MESSAGES),
                                 ("notes", "MAX_NOTES", MAX_NOTES), ("groups", "MAX_GROUPS", MAX_GROUPS)):
        if len(spec.get(name) or []) > maximum:
            raise ctx.too_big(name, limit, maximum, "{} {}; the limit is {}".format(len(spec[name]), name, maximum))
    ids = [p["id"] for p in participants]
    for index, pid in enumerate(ids):
        if pid in ids[:index]:
            raise ctx.invalid("participants[{}].id".format(index), "participant id {} appears twice".format(pid))
    for index, m in enumerate(spec.get("messages") or []):
        for end in ("from", "to"):
            if m[end] not in ids:
                raise ctx.invalid("messages[{}].{}".format(index, end), "{} is not one of the participants".format(m[end]))
    count = len(spec.get("messages") or [])
    for index, n in enumerate(spec.get("notes") or []):
        for key in ("over", "left_of", "right_of"):
            for pid in (n.get(key) if isinstance(n.get(key), list) else [n[key]] if n.get(key) else []):
                if pid not in ids:
                    raise ctx.invalid("notes[{}].{}".format(index, key), "{} is not one of the participants".format(pid))
        if n.get("after") is not None and n["after"] > count:
            raise ctx.invalid("notes[{}].after".format(index), "after is a message number up to {}".format(count))
    for index, g in enumerate(spec.get("groups") or []):
        if g["from"] > g["to"] or g["to"] > count:
            raise ctx.invalid("groups[{}]".format(index), "groups[{}] spans messages {} to {}, but from and to are message numbers (1-based) "
                              "with from <= to <= {} (the sequence has {} message{})".format(
                                  index, g["from"], g["to"], count, count, "" if count == 1 else "s"))
    return spec


# --------------------------------------------------------------------------
# geometry (a pure function of the element)


def _items(el: Element, name: str) -> List[Dict[str, Any]]:
    return [item for item in el.get(name) or [] if isinstance(item, dict)]


def _box_layout(p: Mapping[str, Any]) -> Tuple[float, float, TX.Laid]:
    room = BOX_MAX_W - 2 * BOX_PAD[0] - (ICON_ROOM if p.get("icon") else 0.0)
    laid = TX.lay(str(p.get("text") or p.get("id") or ""), room, BOX_SIZE, BOX_WEIGHT, max_lines=2, bullets=False)
    w = min(BOX_MAX_W, max(BOX_MIN[0], laid.width + 2 * BOX_PAD[0] + (ICON_ROOM if p.get("icon") else 0.0)))
    h = max(BOX_MIN[1], laid.height + 2 * BOX_PAD[1])
    return w, h, laid


def geometry(el: Element) -> Dict[str, Any]:
    """Every participant box, lifeline, message, note and group, relative to the element's top-left corner."""
    participants = _items(el, "participants")
    messages = _items(el, "messages")
    notes = _items(el, "notes")
    groups = _items(el, "groups")
    band = float(BAND) if str(el.get("text") or "").strip() else 0.0
    boxes = [_box_layout(p) for p in participants]
    index = {p["id"]: i for i, p in enumerate(participants)}
    head_h = max((b[1] for b in boxes), default=BOX_MIN[1])
    label_lh = canvas_text.line_height(LABEL_SIZE)
    natural = [min(LABEL_MAX_W, TX.natural(str(m.get("text") or ""), LABEL_SIZE, LABEL_WEIGHT, bullets=False)) for m in messages]
    gaps = [max(COL_MIN, (boxes[i][0] + boxes[i + 1][0]) / 2.0 + COL_ROOM) for i in range(len(boxes) - 1)]
    for m, width in zip(messages, natural):
        a, b = index.get(m.get("from"), 0), index.get(m.get("to"), 0)
        lo, hi = min(a, b), max(a, b)
        if hi == lo:
            if hi < len(gaps):
                gaps[hi] = max(gaps[hi], LOOP_W + width + COL_ROOM)
            continue
        span = math.fsum(gaps[lo:hi])
        need = width + COL_ROOM
        if span < need:
            gaps[hi - 1] += need - span
    xs: List[float] = []
    for i, box in enumerate(boxes):
        xs.append(box[0] / 2.0 if i == 0 else xs[-1] + gaps[i - 1])
    # Rows: messages in order, notes after the message they follow, room for group tabs.
    starts = {}
    for g in groups:
        starts[g["from"]] = starts.get(g["from"], 0) + 1
    ends = {}
    for g in groups:
        ends[g["to"]] = ends.get(g["to"], 0) + 1
    notes_after: Dict[int, List[int]] = {}
    for i, n in enumerate(notes):
        notes_after.setdefault(int(n.get("after", len(messages))), []).append(i)
    y = band + head_h + ROW_EXTRA
    rows: List[Dict[str, Any]] = []
    note_rows: List[Dict[str, Any]] = []
    tops: Dict[int, float] = {}
    bottoms: Dict[int, float] = {}

    def place_notes(after: int) -> None:
        nonlocal y
        for i in notes_after.get(after, []):
            n = notes[i]
            if n.get("over"):
                cols = [xs[index[p]] for p in n["over"] if p in index] or [xs[0]]
                x0, x1 = min(cols) - NOTE_W / 2.0, max(cols) + NOTE_W / 2.0
            elif n.get("left_of"):
                cx = xs[index.get(n["left_of"], 0)]
                x0, x1 = cx - 12 - NOTE_W, cx - 12
            else:
                cx = xs[index.get(n.get("right_of"), 0)]
                x0, x1 = cx + 12, cx + 12 + NOTE_W
            laid = TX.lay(str(n.get("text") or ""), (x1 - x0) - 2 * NOTE_PAD, NOTE_SIZE, 400, max_lines=4, bullets=False)
            h = laid.height + 2 * NOTE_PAD
            note_rows.append({"index": i, "id": str(n.get("id") or "n{}".format(i + 1)), "box": [x0, y, x1 - x0, h], "laid": laid})
            y += h + 12

    place_notes(0)
    for number, (m, width) in enumerate(zip(messages, natural), 1):
        y += TAB_H * starts.get(number, 0)
        a, b = index.get(m.get("from"), 0), index.get(m.get("to"), 0)
        self_loop = a == b
        room = (LABEL_MAX_W if self_loop else max(80.0, abs(xs[b] - xs[a]) - COL_ROOM + 16.0))
        laid = TX.lay(str(m.get("text") or ""), max(room, width if width <= room else room), LABEL_SIZE, LABEL_WEIGHT, max_lines=LABEL_LINES,
                      bullets=False)
        lines = len(laid.lines()) if str(m.get("text") or "") else 0
        tops[number] = y
        arrow_y = y + lines * label_lh + 8.0
        if self_loop:
            x = xs[a]
            points = [[x, arrow_y], [x + LOOP_W, arrow_y], [x + LOOP_W, arrow_y + LOOP_H], [x, arrow_y + LOOP_H]]
            label_x = x + LOOP_W + 8.0
            label_box = [label_x, y, room, lines * label_lh]
        else:
            points = [[xs[a], arrow_y], [xs[b], arrow_y]]
            left, right = min(xs[a], xs[b]), max(xs[a], xs[b])
            label_box = [left + (right - left - min(room, right - left)) / 2.0, y, min(room, right - left), lines * label_lh]
        height = lines * label_lh + ROW_EXTRA + (LOOP_H if self_loop else 0.0)
        rows.append({"number": number, "id": str(m.get("id") or "m{}".format(number)), "points": points, "reply": bool(m.get("reply")),
                     "tone": m.get("tone"), "laid": laid, "label": label_box, "self": self_loop})
        y += height
        bottoms[number] = y
        y += 8.0 * ends.get(number, 0)
        place_notes(number)
    group_boxes = []
    for i, g in enumerate(groups):
        involved = set()
        for m in messages[g["from"] - 1:g["to"]]:
            involved.update((index.get(m.get("from"), 0), index.get(m.get("to"), 0)))
        cols = [xs[c] for c in involved] or [xs[0]]
        x0 = min(cols) - 60.0
        x1 = max(cols) + 60.0 + (LOOP_W if len(involved) == 1 else 0.0)
        top = tops.get(g["from"], y) - TAB_H - 4.0
        bottom = bottoms.get(g["to"], y) + 4.0
        group_boxes.append({"index": i, "box": [x0, top, x1 - x0, bottom - top], "kind": g.get("kind") or "loop", "label": g.get("label") or ""})
    footer = len(messages) > FOOTER_AFTER
    life_end = y + (0.0 if footer else ROW_EXTRA)
    foot_top = y + 8.0
    total_h = foot_top + head_h if footer else life_end
    # Everything sits right of MARGIN: a note left of the first participant shifts the whole drawing.
    lefts = [xs[i] - boxes[i][0] / 2.0 for i in range(len(boxes))] + [n["box"][0] for n in note_rows] + [g["box"][0] for g in group_boxes]
    rights = [xs[i] + boxes[i][0] / 2.0 for i in range(len(boxes))] + [n["box"][0] + n["box"][2] for n in note_rows] + \
        [g["box"][0] + g["box"][2] for g in group_boxes] + [r["label"][0] + r["label"][2] for r in rows]
    shift = MARGIN - min(lefts) if lefts else float(MARGIN)
    rights = rights or [0.0]
    xs = [x + shift for x in xs]
    for r in rows:
        r["points"] = [[p[0] + shift, p[1]] for p in r["points"]]
        r["label"][0] += shift
    for n in note_rows:
        n["box"][0] += shift
    for g in group_boxes:
        g["box"][0] += shift
    width = max(rights) + shift + MARGIN
    heads = [{"id": p["id"], "box": [xs[i] - boxes[i][0] / 2.0, band + (head_h - boxes[i][1]) / 2.0, boxes[i][0], boxes[i][1]], "laid": boxes[i][2],
              "icon": p.get("icon"), "tone": p.get("tone"), "text": p.get("text")} for i, p in enumerate(participants)]
    feet = [dict(h, box=[h["box"][0], foot_top + (head_h - h["box"][3]) / 2.0, h["box"][2], h["box"][3]]) for h in heads] if footer else []
    lifelines = [[[xs[i], band + head_h], [xs[i], (foot_top if footer else life_end)]] for i in range(len(participants))]
    return {"band": band, "w": max(width, 2 * MARGIN + BOX_MIN[0]), "h": total_h + MARGIN, "heads": heads, "feet": feet, "lifelines": lifelines,
            "rows": rows, "notes": note_rows, "groups": group_boxes, "truncated": [r["id"] for r in rows if r["laid"].truncated]}


# --------------------------------------------------------------------------
# drawing


def _participant(head: Mapping[str, Any], x0: float, y0: float) -> List[Dict[str, Any]]:
    bx, by, bw, bh = head["box"]
    tone = head.get("tone") if head.get("tone") in canvas_theme.TONES else "neutral"
    refs = canvas_theme.resolve_ref(tone, "soft", "box")
    out: List[Dict[str, Any]] = [{"k": "rect", "x": x0 + bx, "y": y0 + by, "w": bw, "h": bh, "r": 8, "fill": refs["fill"], "stroke": refs["stroke"],
                                  "sw": 1.5}]
    laid: TX.Laid = head["laid"]
    icon_room = ICON_ROOM if head.get("icon") else 0.0
    content_w = laid.width + icon_room
    left = x0 + bx + (bw - content_w) / 2.0
    top = y0 + by + (bh - laid.height) / 2.0
    if head.get("icon"):
        icon = canvas_icons.emit(str(head["icon"]), left, y0 + by + (bh - ICON) / 2.0, ICON, refs["text"] or D.INK)
        if icon is not None:
            icon["lod"] = list(D.LOD_LABEL)
            out.append(icon)
    out += TX.emit_lines(laid, left + icon_room, top, laid.width, refs["text"] or D.INK, lod="label", anchor="middle")
    return out


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    x0, y0, x1, y1 = D.box_of(el)
    geo = geometry(el)
    items: List[Dict[str, Any]] = [{"k": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "r": 8, "fill": "base.surface",
                                    "stroke": "tone.neutral.zone_stroke", "sw": 1}]
    title = str(el.get("text") or "")
    if geo["band"]:
        lh = canvas_text.line_height(TITLE_SIZE)
        line = D.text_prim([G.fit_line(title, max(0.0, (x1 - x0) - 2 * MARGIN), TITLE_SIZE, TITLE_WEIGHT)], x0 + MARGIN, y0 + (BAND - lh) / 2.0,
                           TITLE_SIZE, {}, D.INK, "start", (x0 + MARGIN, y0, max(0.0, (x1 - x0) - 2 * MARGIN), BAND), TITLE_WEIGHT)
        line["lod"] = list(D.LOD_LABEL)
        items.append(line)
    for g in geo["groups"]:
        gx, gy, gw, gh = g["box"]
        items.append({"k": "rect", "x": x0 + gx, "y": y0 + gy, "w": gw, "h": gh, "r": 4, "fill": None, "stroke": "base.line", "sw": 1})
        text = g["kind"] + (" · " + g["label"] if g["label"] else "")
        tab_w = min(gw, TX.width(text, TAB_SIZE, TAB_WEIGHT) + 16.0)
        items.append({"k": "rect", "x": x0 + gx, "y": y0 + gy, "w": tab_w, "h": TAB_H, "r": 4, "fill": "base.surface", "stroke": "base.line", "sw": 1})
        tab = D.text_prim([G.fit_line(text, tab_w - 16.0, TAB_SIZE, TAB_WEIGHT)], x0 + gx + 8.0, y0 + gy + (TAB_H - canvas_text.line_height(TAB_SIZE)) / 2.0,
                          TAB_SIZE, {}, D.MUTED, "start", (x0 + gx + 8.0, y0 + gy, tab_w - 16.0, TAB_H), TAB_WEIGHT)
        tab["lod"] = list(D.LOD_LABEL)
        items.append(tab)
    for (ax, ay), (bx, by) in geo["lifelines"]:
        items.append({"k": "line", "points": [[x0 + ax, y0 + ay], [x0 + bx, y0 + by]], "stroke": "base.line", "sw": LIFELINE_W,
                      "dash": [LIFELINE_W * 4, LIFELINE_W * 3]})
    for head in geo["heads"] + geo["feet"]:
        items += _participant(head, x0, y0)
    for row in geo["rows"]:
        points = [[x0 + p[0], y0 + p[1]] for p in row["points"]]
        stroke = "tone.{}.stroke".format(row["tone"]) if row.get("tone") in canvas_theme.TONES and row["tone"] != "neutral" else D.INK
        if row["self"]:
            d = " ".join(["M{} {}".format(D.fmt(points[0][0]), D.fmt(points[0][1]))] +
                         ["L{} {}".format(D.fmt(p[0]), D.fmt(p[1])) for p in points[1:]])
        else:
            d = "M{} {} L{} {}".format(D.fmt(points[0][0]), D.fmt(points[0][1]), D.fmt(points[1][0]), D.fmt(points[1][1]))
        head = G.head_points("arrow" if row["reply"] else "triangle", (points[-1][0], points[-1][1]), (points[-2][0], points[-2][1]), ARROW_W)
        prim: Dict[str, Any] = {"k": "arrow", "d": d, "stroke": stroke, "sw": ARROW_W, "heads": [dict(head, at="end")] if head else []}
        if row["reply"]:
            prim["dash"] = [ARROW_W * 4, ARROW_W * 3]
        items.append(prim)
        laid: TX.Laid = row["laid"]
        if laid.lines() and any(laid.lines()):
            lx, ly, lw, _lh = row["label"]
            anchor = "start" if row["self"] else "middle"
            items += TX.emit_lines(laid, x0 + lx, y0 + ly, lw, D.MUTED, lod="body", anchor=anchor)
    for note in geo["notes"]:
        nx, ny, nw, nh = note["box"]
        refs = canvas_theme.resolve_ref("idea", "soft", "note")
        items.append({"k": "rect", "x": x0 + nx, "y": y0 + ny, "w": nw, "h": nh, "r": 4, "fill": refs["fill"], "stroke": None})
        items += TX.emit_lines(note["laid"], x0 + nx + NOTE_PAD, y0 + ny + NOTE_PAD, nw - 2 * NOTE_PAD, refs["text"] or D.INK, lod="body",
                               anchor="middle")
    return items


def parts(el: Element) -> List[Dict[str, Any]]:
    """``p.<id>`` for a participant's box, ``m<n>`` for a message's label, ``n<n>`` for a note (6.2)."""
    x0, y0, _x1, _y1 = D.box_of(el)
    geo = geometry(el)
    out = []
    for head in geo["heads"]:
        bx, by, bw, bh = head["box"]
        inner = [x0 + bx + BOX_PAD[0], y0 + by + BOX_PAD[1], max(1.0, bw - 2 * BOX_PAD[0]), max(1.0, bh - 2 * BOX_PAD[1])]
        out.append({"part": "p." + head["id"], "hit": {"shape": "rect", "box": [x0 + bx, y0 + by, bw, bh]},
                    "edit": TX.edit("text", str(head.get("text") or ""), inner, BOX_SIZE, BOX_WEIGHT, D.INK, part="p." + head["id"], align="center"),
                    "lod": list(D.LOD_LABEL)})
    messages = _items(el, "messages")
    for row in geo["rows"]:
        lx, ly, lw, lh = row["label"]
        box = [x0 + lx, y0 + ly, max(lw, 40.0), max(lh, canvas_text.line_height(LABEL_SIZE))]
        text = str(messages[row["number"] - 1].get("text") or "")
        out.append({"part": row["id"], "hit": {"shape": "rect", "box": box},
                    "edit": TX.edit("text", text, box, LABEL_SIZE, LABEL_WEIGHT, D.MUTED, part=row["id"], align="start" if row["self"] else "center"),
                    "lod": list(D.LOD_BODY)})
    notes = _items(el, "notes")
    for note in geo["notes"]:
        nx, ny, nw, nh = note["box"]
        box = [x0 + nx, y0 + ny, nw, nh]
        inner = [x0 + nx + NOTE_PAD, y0 + ny + NOTE_PAD, max(1.0, nw - 2 * NOTE_PAD), max(1.0, nh - 2 * NOTE_PAD)]
        out.append({"part": note["id"], "hit": {"shape": "rect", "box": box},
                    "edit": TX.edit("text", str(notes[note["index"]].get("text") or ""), inner, NOTE_SIZE, 400, D.INK, part=note["id"], align="center"),
                    "lod": list(D.LOD_BODY)})
    return out


def part_edit(root: Element, part: str, text: str) -> Dict[str, Any]:
    """An inline part's edit as a patch: a participant's name, a message's text, or a note's text."""
    part = str(part)
    if part.startswith("p."):
        return {"update": {"participants": [{"id": part[2:], "text": text}]}}
    if part.startswith("m"):
        return {"update": {"messages": [{"id": part, "text": text}]}}
    return {"update": {"notes": [{"id": part, "text": text}]}}


def text_edit(el: Element) -> Optional[Dict[str, Any]]:
    if not str(el.get("text") or "").strip():
        return None
    x0, y0, x1, _y1 = D.box_of(el)
    lh = canvas_text.line_height(TITLE_SIZE)
    return TX.edit("text", str(el.get("text") or ""), [x0 + MARGIN, y0 + (BAND - lh) / 2.0, max(1.0, (x1 - x0) - 2 * MARGIN), lh], TITLE_SIZE,
                   TITLE_WEIGHT, D.INK, wrap="line")


def hit(el: Element) -> Dict[str, Any]:
    return {"shape": "rect", "box": D.xywh(D.box_of(el))}


# --------------------------------------------------------------------------
# build and readback


def _clean(items: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [{k: v for k, v in item.items() if k != "_gen"} for item in items]


def build(bctx: Any, spec: Dict[str, Any]) -> None:
    probe = {"type": "sequence", "text": spec.get("title") or "", "participants": _clean(spec["participants"]),
             "messages": _clean(spec.get("messages") or []), "notes": _clean(spec.get("notes") or []), "groups": _clean(spec.get("groups") or [])}
    geo = geometry(probe)
    w, h = int(round(geo["w"] + 0.49)), int(round(geo["h"] + 0.49))
    fit = {"policy": "clamp", "min": [w, h], "truncated": bool(geo["truncated"]), "parts": geo["truncated"][:50]}
    style = None if bctx.root is not None else bctx.ctx.default_style("sequence")
    bctx.root_fields(text=spec.get("title") or "", style=style, w=w, h=h, participants=probe["participants"], messages=probe["messages"],
                     notes=probe["notes"], groups=probe["groups"], fit=fit)


def _message_text(m: Mapping[str, Any]) -> Optional[str]:
    if set(m) - {"id", "from", "to", "text", "reply"}:
        return None
    return "{} {} {}".format(m["from"], "-->" if m.get("reply") else "->", m["to"]) + (": " + m["text"] if m.get("text") else "")


def spec(root: Element, members: List[Element], full: bool) -> Dict[str, Any]:
    """The sequence as its op, messages in shorthand where they are plain."""
    out: Dict[str, Any] = {"op": "sequence"}
    if root.get("alias"):
        out["id"] = root["alias"]
    if root.get("text"):
        out["title"] = root["text"]
    out["participants"] = [p["id"] if p == {"id": p["id"], "text": p["id"]} else dict(p) for p in _items(root, "participants")]
    for name, prefix in (("messages", "m"), ("notes", "n"), ("groups", "g")):
        items = _items(root, name)
        if not items:
            continue
        shown: List[Any] = []
        for number, item in enumerate(items, 1):
            auto = item.get("id") == "{}{}".format(prefix, number)
            clean = {k: v for k, v in item.items() if not (k == "id" and auto)}
            short = _message_text(clean) if name == "messages" and auto else None
            shown.append(short if short is not None else clean)
        out[name] = shown
    return out


def readback(el: Element, full: bool) -> str:
    title = str(el.get("text") or "")
    return "{} sequence{}{} participants {}, messages {} [{},{} {}x{}]".format(
        el.get("id"), " " + str(el["alias"]) if el.get("alias") else "", " " + quote(title, 0 if full else 80) if title else "",
        len(_items(el, "participants")), len(_items(el, "messages")), el.get("x"), el.get("y"), el.get("w"), el.get("h"))


def clamped(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    fit = el.get("fit") if isinstance(el.get("fit"), dict) else {}
    if not fit.get("truncated"):
        return []
    parts_cut = [str(p) for p in fit.get("parts") or []]
    return [{"code": "label_truncated", "ids": [str(el.get("id"))], "parts": parts_cut,
             "message": "{} shows only the first {} lines of {}; shorten the text (the whole text is kept)".format(
                 el.get("id"), LABEL_LINES, ", ".join(parts_cut[:5])), "fix": None}]


def create(ctx: Any, op: Dict[str, Any]) -> None:
    ctx.block("sequence", op)


OPS = (
    OpSpec(name="sequence", family="diagram", fields=("title", "participants", "messages", "notes", "groups", "id", "client_id"), create=create, place=True,
           order=74, doc="a sequence diagram: participants, messages, notes and groups in one element",
           mcp="sequence {title, participants [{id, text, icon}], messages [\"a -> b: call\", \"b --> a: reply\"], "
               "notes [{over [ids] | left_of | right_of, text, after}], groups [{kind loop|alt|opt|par, label, from, to}]}"),
)

KINDS = (
    Kind(name="sequence", role="composite", ops=("sequence",), solid=True, cell=True, connectable=True, handles="none", edit_limit="label",
         block=Block(collections=(Collection(name="participants", item=participant_item, maximum=MAX_PARTICIPANTS, label="text"),
                                  Collection(name="messages", item=message_item, prefix="m", maximum=MAX_MESSAGES,
                                             refs=(("from", "participants", "drop"), ("to", "participants", "drop"))),
                                  Collection(name="notes", item=note_item, prefix="n", maximum=MAX_NOTES,
                                             refs=(("over", "participants", "drop"), ("left_of", "participants", "drop"),
                                                   ("right_of", "participants", "drop"), ("after", "messages", "end"))),
                                  Collection(name="groups", item=group_item, prefix="g", maximum=MAX_GROUPS,
                                             refs=(("from", "messages", "start"), ("to", "messages", "end")))),
                     settings=(), parts="inline", positional=False, normalize=normalize, build=build, spec=spec, part_edit=part_edit),
         emit=emit, hit=hit, text_edit=text_edit, parts=parts, readback=readback, checks=(clamped,),
         noun=("sequence diagram", "sequence diagrams"), doc="participants, messages, notes and groups in one element, every item addressable"),
)
