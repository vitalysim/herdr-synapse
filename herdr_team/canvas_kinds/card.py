"""Cards (canvas v2 phase 2, 4.2): a titled surface with a body, an icon, badges, an owner and a status.

A card hugs its content up to its width class (``size`` s, m or l: 240, 320 or
440 wide): the title (20/600) wraps first, then the body (16/400, muted; lines
that start with ``- `` or ``* `` are bullets with a hanging indent), then the
badges in a row that wraps, then the owner's chip and the status. Inside a
kanban its title keeps 2 lines and its body 3 (``clamp``); a clamped card keeps
its whole text and says so (``label_truncated``). A tone adds a bar on its left
edge; ``solid`` fills it. ``detail`` is not drawn: it is the tooltip and
``look --full``.

Kanban cards and timeline events are cards too (``card_fields`` reads the same
fields for them), and a card may be a graph node (``node``).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_icons, canvas_text, canvas_theme
from herdr_team.canvas_kinds import Kind, OpSpec, Tool
from herdr_team.canvas_kinds import _text as TX
from herdr_team.canvas_kinds._common import Element, quote, style_of, truncated_label

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 131

_TOKENS = canvas_theme.section("card", {}) or {}
WIDTHS = dict(_TOKENS.get("widths") or {"s": 240, "m": 320, "l": 440})
MIN = tuple(_TOKENS.get("min") or (240, 80))
PAD = 16.0
TITLE_SIZE, TITLE_WEIGHT = 20.0, 600
BODY_SIZE, BODY_WEIGHT = 16.0, 400
CAPTION_SIZE, CAPTION_WEIGHT = 12.0, 500
TITLE_GAP = 4.0
ROW_GAP = float(_TOKENS.get("row_gap", 8))
ICON = float(_TOKENS.get("icon", 20))
INDENT = float(_TOKENS.get("icon_indent", 28))
BAR = float(_TOKENS.get("bar", 4))
BADGE_H, BADGE_PAD, BADGE_GAP, BADGE_SIZE = 20.0, 8.0, float(_TOKENS.get("badge_gap", 8)), 12.0
OWNER_R = float(_TOKENS.get("owner_r", 9))
FOOTER_H = 20.0
STATUSES = ("todo", "doing", "review", "blocked", "done")
STATUS_TONES = {"todo": "neutral", "doing": "info", "review": "accent", "blocked": "danger", "done": "success"}
MAX_BADGES = 8
MAX_BADGE_CHARS = 40
MAX_STATUS_CHARS = 20
MAX_OWNER_CHARS = 64
MAX_DETAIL_CHARS = 2000
VARIANTS = ("soft", "outline", "solid")


# --------------------------------------------------------------------------
# fields


def _badges(ctx: Any, raw: Any, field: str) -> List[Dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ctx.invalid(field, "{} is a list of words or {{text, tone}}".format(field))
    if len(raw) > MAX_BADGES:
        raise ctx.too_big(field, "MAX_BADGES", MAX_BADGES, "{} badges; a card shows at most {}".format(len(raw), MAX_BADGES))
    out = []
    for index, item in enumerate(raw):
        here = "{}[{}]".format(field, index)
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            raise ctx.invalid(here, "a badge is a word or {text, tone}")
        for key in item:
            if key not in ("text", "tone"):
                raise ctx.invalid("{}.{}".format(here, key), "a badge takes text and tone")
        text = ctx.text(item, "text", limit="label", one_line=True, required=True, label=here + ".text")
        if len(text) > MAX_BADGE_CHARS:
            raise ctx.invalid(here + ".text", "a badge is at most {} characters".format(MAX_BADGE_CHARS))
        badge = {"text": text}
        if item.get("tone") is not None:
            badge["tone"] = ctx.choice(item, "tone", canvas_theme.TONES, "neutral", label=here + ".tone")
        out.append(badge)
    return out


def icon_name(ctx: Any, value: Any, field: str) -> Optional[str]:
    """An icon field: a Lucide name (resolved), or ``icon_unknown`` with the nearest names."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ctx.invalid(field, "{} is an icon name (canvas icons --search <word>)".format(field))
    found = canvas_icons.resolve(value)
    if found is None:
        near = canvas_icons.suggest(value)
        raise ctx.error("icon_unknown", "{} is not an icon{}; canvas icons --search <word> lists them".format(
            value[:40], "; did you mean {}".format(", ".join(near)) if near else ""), field=field, did_you_mean=near)
    return found


def card_fields(ctx: Any, raw: Dict[str, Any], field: str = "", title_key: str = "title", required: bool = True) -> Dict[str, Any]:
    """A card's fields from an op or an item, validated: ``title``, ``body``, ``icon``, ``badges``, ``owner``, ``status``,
    ``tone``, ``detail`` (``field`` prefixes refusals: ``columns[1].cards[3].owner``)."""
    def name(key: str) -> str:
        return "{}.{}".format(field, key) if field else key

    title = ctx.text(raw, title_key, limit="label", one_line=True, label=name(title_key))
    body = ctx.text(raw, "body", limit="text", label=name("body"))
    if required and not title and not body:
        raise ctx.invalid(name(title_key), "a card needs a title or a body")
    out: Dict[str, Any] = {"title": title}
    if body:
        out["body"] = body
    icon = icon_name(ctx, raw.get("icon"), name("icon"))
    if icon:
        out["icon"] = icon
    badges = _badges(ctx, raw.get("badges"), name("badges"))
    if badges:
        out["badges"] = badges
    if raw.get("owner") is not None:
        owner = ctx.text(raw, "owner", limit="label", one_line=True, label=name("owner"))
        if len(owner) > MAX_OWNER_CHARS:
            raise ctx.invalid(name("owner"), "an owner is a member name or human")
        if owner:
            out["owner"] = owner
    if raw.get("status") is not None:
        status = ctx.text(raw, "status", limit="label", one_line=True, label=name("status"))
        if len(status) > MAX_STATUS_CHARS:
            raise ctx.invalid(name("status"), "status is todo, doing, review, blocked, done, or a word of at most {} characters".format(MAX_STATUS_CHARS))
        if status:
            out["status"] = status.lower() if status.lower() in STATUSES else status
    if raw.get("tone") is not None:
        out["tone"] = ctx.choice(raw, "tone", canvas_theme.TONES, "neutral", label=name("tone"))
    if raw.get("detail") is not None:
        detail = ctx.text(raw, "detail", limit="text", label=name("detail"))
        if detail:
            out["detail"] = detail
    return out


def owner_known(ctx: Any, owner: Optional[str]) -> bool:
    if not owner:
        return True
    try:
        return bool(ctx.mentions([owner], ""))
    except Exception:  # noqa: BLE001 - an unknown owner is reported by the owner_unknown check, never refused
        return False


def element_fields(ctx: Any, fields: Dict[str, Any]) -> Dict[str, Any]:
    """The stored element fields of a card from ``card_fields`` (the title is the element's ``text``)."""
    out = {key: fields[key] for key in ("body", "icon", "badges", "owner", "status", "detail") if fields.get(key) is not None}
    if "owner" in out:
        out["owner_known"] = owner_known(ctx, out["owner"])
    return out


# --------------------------------------------------------------------------
# layout


def _clamp(el: Element, key: str) -> int:
    clamp = el.get("clamp") if isinstance(el.get("clamp"), dict) else {}
    value = clamp.get(key)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _badge_w(text: str) -> float:
    return TX.width(text, BADGE_SIZE, 500) + 2 * BADGE_PAD


def _badge_list(el: Element) -> List[Dict[str, str]]:
    found = el.get("badges") if isinstance(el.get("badges"), list) else []
    return [b for b in found if isinstance(b, dict) and isinstance(b.get("text"), str) and b["text"]]


def _footer(el: Element) -> List[Tuple[str, str]]:
    out = []
    if isinstance(el.get("owner"), str) and el["owner"]:
        out.append(("owner", el["owner"]))
    if isinstance(el.get("status"), str) and el["status"]:
        out.append(("status", el["status"]))
    return out


def _footer_w(el: Element) -> float:
    total = 0.0
    for kind, text in _footer(el):
        total += (2 * OWNER_R + 6 if kind == "owner" else 8 + 6) + TX.width(text, CAPTION_SIZE, CAPTION_WEIGHT) + 12
    return total


def layout(el: Element, w: float) -> Dict[str, Any]:
    """Where everything goes in a card ``w`` wide: the title, body, badge rows and footer, relative to its corner."""
    inner = max(1.0, w - 2 * PAD)
    indent = INDENT if el.get("icon") else 0.0
    title_text = str(el.get("text") or "")
    body_text = str(el.get("body") or "") if isinstance(el.get("body"), str) else ""
    title = TX.lay(title_text, inner - indent, TITLE_SIZE, TITLE_WEIGHT, max_lines=_clamp(el, "title"), bullets=False)
    body = TX.lay(body_text, inner, BODY_SIZE, BODY_WEIGHT, max_lines=_clamp(el, "body")) if body_text.strip() else None
    y = PAD
    title_h = max(title.height, canvas_text.line_height(TITLE_SIZE)) if (title_text or not body) else 0.0
    title_top = y
    y += title_h
    body_top = y + (TITLE_GAP if title_h else 0.0)
    if body is not None:
        y = body_top + body.height
    rows: List[List[Tuple[float, Dict[str, str]]]] = []
    badges = _badge_list(el)
    if badges:
        row: List[Tuple[float, Dict[str, str]]] = []
        used = 0.0
        for badge in badges:
            bw = min(_badge_w(badge["text"]), inner)
            if row and used + BADGE_GAP + bw > inner:
                rows.append(row)
                row, used = [], 0.0
            row.append((used + (BADGE_GAP if row else 0.0), badge))
            used += (BADGE_GAP if len(row) > 1 else 0.0) + bw
        rows.append(row)
    badges_top = y + ROW_GAP
    if rows:
        y = badges_top + len(rows) * BADGE_H + (len(rows) - 1) * 6
    footer_top = y + ROW_GAP
    if _footer(el):
        y = footer_top + FOOTER_H
    return {"inner": inner, "indent": indent, "title": title, "title_top": title_top, "title_h": title_h, "body": body, "body_top": body_top,
            "rows": rows, "badges_top": badges_top, "footer_top": footer_top, "h": y + PAD,
            "truncated": title.truncated or bool(body and body.truncated)}


def _width_class(el: Element) -> float:
    size = el.get("size")
    return float(WIDTHS.get(size, WIDTHS["m"])) if isinstance(size, str) else float(WIDTHS["m"])


def natural_width(el: Element) -> float:
    indent = INDENT if el.get("icon") else 0.0
    parts = [TX.natural(str(el.get("text") or ""), TITLE_SIZE, TITLE_WEIGHT, bullets=False) + indent,
             TX.natural(str(el.get("body") or "") if isinstance(el.get("body"), str) else "", BODY_SIZE, BODY_WEIGHT)]
    badges = _badge_list(el)
    if badges:
        parts.append(sum(_badge_w(b["text"]) for b in badges) + BADGE_GAP * (len(badges) - 1))
    parts.append(_footer_w(el))
    return max(parts) + 2 * PAD


def measure(el: Element, minimum: Tuple[float, float]) -> canvas_text.FitResult:
    """The card's size from its content, never below ``minimum`` (at least 240x80), hugging up to its width class; its
    title's lines and room are the fit's (``fit.lines``)."""
    fixed = el.get("fixed_w")
    min_w = max(float(minimum[0]), float(MIN[0]))
    min_h = max(float(minimum[1]), float(MIN[1]))
    if isinstance(fixed, (int, float)) and not isinstance(fixed, bool) and fixed > 0:
        w = max(float(fixed), float(minimum[0]))
        min_h = max(float(minimum[1]), 40.0)
    else:
        top = max(_width_class(el), min_w)
        w = TX.snap(min(max(min_w, natural_width(el)), top), min_w)
    laid = layout(el, w)
    h = TX.snap(max(min_h, laid["h"]), min_h)
    title = laid["title"]
    inner = (PAD + laid["indent"], laid["title_top"], max(0.0, w - 2 * PAD - laid["indent"]), max(laid["title_h"], title.height))
    return TX.fit_result(w, h, title.lines(), inner, TITLE_SIZE, TITLE_WEIGHT, truncated=laid["truncated"], grew=w > min_w or h > min_h)


# --------------------------------------------------------------------------
# drawing


def _refs(el: Element) -> Dict[str, Optional[str]]:
    style = style_of(el)
    tone = style.get("tone") if style.get("tone") in canvas_theme.TONES else "neutral"
    variant = style.get("variant") if style.get("variant") in VARIANTS else "outline"
    refs = canvas_theme.resolve_ref(tone, variant, "card")
    refs = dict(refs, tone=tone, variant=variant)
    return refs


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The surface (with its tone bar), the icon and title, the body with its skeleton, the badges and the footer."""
    x0, y0, x1, y1 = D.box_of(el)
    w, h = x1 - x0, y1 - y0
    refs = _refs(el)
    solid = refs["variant"] == "solid"
    items: List[Dict[str, Any]] = [{"k": "rect", "x": x0, "y": y0, "w": w, "h": h, "r": 12, "fill": refs["fill"], "stroke": refs["stroke"], "sw": 1,
                                    "elev": 1}]
    if refs["tone"] != "neutral" and not solid:
        items.append({"k": "rect", "x": x0 + 6, "y": y0 + 10, "w": BAR, "h": max(1.0, h - 20), "r": BAR / 2.0,
                      "fill": "tone.{}.solid".format(refs["tone"])})
    laid = layout(el, w)
    ink = refs["text"] or D.INK
    muted = "tone.{}.on_solid".format(refs["tone"]) if solid else D.MUTED
    title = laid["title"]
    left = x0 + PAD + laid["indent"]
    lh = canvas_text.line_height(TITLE_SIZE)
    if el.get("icon"):
        icon = canvas_icons.emit(str(el["icon"]), x0 + PAD, y0 + laid["title_top"] + (lh - ICON) / 2.0, ICON,
                                 ink if solid or refs["tone"] == "neutral" else "tone.{}.text".format(refs["tone"]), D.LOD_LABEL)
        if icon is not None:
            items.append(icon)
    items += TX.emit_lines(title, left, y0 + laid["title_top"], laid["inner"] - laid["indent"], ink, lod="label")
    if laid["body"] is not None:
        items += TX.emit_lines(laid["body"], x0 + PAD, y0 + laid["body_top"], laid["inner"], muted, lod="body")
    for row_index, row in enumerate(laid["rows"]):
        top = y0 + laid["badges_top"] + row_index * (BADGE_H + 6)
        for offset, badge in row:
            items += badge_prims(badge, x0 + PAD + offset, top, min(_badge_w(badge["text"]), laid["inner"]))
    footer = _footer(el)
    if footer:
        items += _footer_prims(el, footer, x0 + PAD, y0 + laid["footer_top"], env)
    return items


def badge_prims(badge: Dict[str, str], x: float, y: float, w: float) -> List[Dict[str, Any]]:
    """A small pill (height 20, 12/500) in its tone's soft colours."""
    tone = badge.get("tone") if badge.get("tone") in canvas_theme.TONES else "neutral"
    refs = canvas_theme.resolve_ref(tone, "soft", "badge")
    pill = {"k": "rect", "x": x, "y": y, "w": w, "h": BADGE_H, "r": BADGE_H / 2.0, "fill": refs["fill"], "lod": list(D.LOD_LABEL)}
    if tone == "neutral":
        pill.update(stroke="tone.neutral.zone_stroke", sw=1)
    text = D.text_prim([TX.ellipsize(badge["text"], w - 2 * BADGE_PAD, BADGE_SIZE, 500) if _badge_w(badge["text"]) > w else badge["text"]],
                       x + w / 2.0, y + (BADGE_H - canvas_text.line_height(BADGE_SIZE)) / 2.0, BADGE_SIZE, {}, refs["text"] or D.INK, "middle",
                       (x + BADGE_PAD, y, max(0.0, w - 2 * BADGE_PAD), BADGE_H), 500)
    text["lod"] = list(D.LOD_LABEL)
    return [pill, text]


def _footer_prims(el: Element, footer: List[Tuple[str, str]], x: float, y: float, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cy = y + FOOTER_H / 2.0
    lh = canvas_text.line_height(CAPTION_SIZE)
    for kind, text in footer:
        if kind == "owner":
            chip = D.chip(text, env)
            out.append({"k": "ellipse", "cx": x + OWNER_R, "cy": cy, "rx": OWNER_R, "ry": OWNER_R, "fill": chip["bg"], "lod": list(D.LOD_LABEL)})
            initials = D.text_prim([chip["initials"]], x + OWNER_R, cy - canvas_text.line_height(8) / 2.0, 8, {}, chip["fg"], "middle", None, 600)
            initials["lod"] = list(D.LOD_BODY)
            out.append(initials)
            x += 2 * OWNER_R + 6
        else:
            tone = STATUS_TONES.get(text, "neutral")
            out.append({"k": "ellipse", "cx": x + 4, "cy": cy, "rx": 4, "ry": 4, "fill": "tone.{}.solid".format(tone), "lod": list(D.LOD_LABEL)})
            x += 14
        caption = D.text_prim([text], x, cy - lh / 2.0, CAPTION_SIZE, {}, D.MUTED, "start", None, CAPTION_WEIGHT)
        out += D.body(caption)
        x += TX.width(text, CAPTION_SIZE, CAPTION_WEIGHT) + 12
    return out


def parts(el: Element) -> List[Dict[str, Any]]:
    """The title and body, each hit and edited on its own (6.2)."""
    x0, y0, x1, _y1 = D.box_of(el)
    laid = layout(el, x1 - x0)
    refs = _refs(el)
    left = x0 + PAD + laid["indent"]
    title_box = [left, y0 + laid["title_top"], max(1.0, laid["inner"] - laid["indent"]), max(laid["title_h"], canvas_text.line_height(TITLE_SIZE))]
    out = [{"part": "title", "hit": {"shape": "rect", "box": title_box},
            "edit": TX.edit("text", str(el.get("text") or ""), title_box, TITLE_SIZE, TITLE_WEIGHT, refs["text"] or D.INK, part="title"),
            "lod": list(D.LOD_LABEL)}]
    body = laid["body"]
    body_box = [x0 + PAD, y0 + laid["body_top"], laid["inner"], max(body.height if body else 0.0, canvas_text.line_height(BODY_SIZE))]
    out.append({"part": "body", "hit": {"shape": "rect", "box": body_box},
                "edit": TX.edit("body", str(el.get("body") or "") if isinstance(el.get("body"), str) else "", body_box, BODY_SIZE, BODY_WEIGHT,
                                D.MUTED, part="body"),
                "lod": list(D.LOD_BODY)})
    return out


def text_edit(el: Element) -> Optional[Dict[str, Any]]:
    """A double-click on the card edits its title (the body is its own part)."""
    return parts(el)[0]["edit"]


def hit(el: Element) -> Dict[str, Any]:
    return {"shape": "rect", "box": D.xywh(D.box_of(el))}


def readback(el: Element, full: bool) -> str:
    """``E-7 card gw "API gateway" (P0) icon network · 320x112``; in full, the body, owner, status and detail."""
    title = str(el.get("text") or "")
    badges = ", ".join(b["text"] for b in _badge_list(el))
    out = "{} card{}{}{}{} · {}x{}".format(el.get("id"), " " + str(el["alias"]) if el.get("alias") else "",
                                          " " + quote(title, 0 if full else 60) if title else "", " ({})".format(badges) if badges else "",
                                          " icon {}".format(el["icon"]) if el.get("icon") else "", el.get("w"), el.get("h"))
    if full:
        if isinstance(el.get("body"), str) and el["body"]:
            out += " body " + quote(el["body"], 200)
        for key in ("owner", "status"):
            if isinstance(el.get(key), str) and el[key]:
                out += " {} {}".format(key, el[key])
        if isinstance(el.get("detail"), str) and el["detail"]:
            out += " detail " + quote(el["detail"], 300)
    fit = el.get("fit") if isinstance(el.get("fit"), dict) else {}
    if fit.get("truncated"):
        out += " (clamped)"
    return out


def owner_unknown(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    if el.get("owner_known") is not False:
        return []
    return [{"code": "owner_unknown", "ids": [str(el.get("id"))],
             "message": "{} is owned by {}, who is not a member of the team; name a member (or human)".format(el.get("id"), el.get("owner")),
             "fix": None}]


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``card`` op: sized from its content (``w``/``h`` are its minimum), then placed."""
    fields = card_fields(ctx, op, required=not ctx.author_is_human)
    size = ctx.choice(op, "size", ("s", "m", "l"), "m")
    variant = ctx.choice(op, "variant", VARIANTS, "outline")
    style = ctx.style({"tone": fields.get("tone") or "neutral", "variant": variant}, "card")
    minimum = (ctx.number(op, "w", 1, 20000, MIN[0]), ctx.number(op, "h", 1, 20000, MIN[1]))
    stored = element_fields(ctx, fields)
    probe = dict(stored, type="card", text=fields["title"], style=style, size=size)
    fitted = ctx.fit(probe, minimum)
    w, h = fitted.pop("w"), fitted.pop("h")
    x, y, frame = ctx.place(op, w, h, (float(minimum[0]), float(minimum[1])))
    ctx.create("card", x, y, w, h, op=op, text=fields["title"], style=style, frame=frame, size=size, **stored, **fitted)


OPS = (
    OpSpec(name="card", family="block", fields=("title", "body", "icon", "badges", "owner", "status", "detail", "size", "tone", "variant", "w", "h", "id", "client_id"),
           create=create, place=True, order=36, doc="a titled card with a body, an icon, badges, an owner and a status, sized to its content",
           mcp="card {title, body (- bullets), icon, badges [text|{text,tone}], owner, status todo|doing|review|blocked|done, detail, "
               "size s|m|l, tone, variant soft|outline|solid}"),
)

KINDS = (
    Kind(name="card", role="leaf", ops=("card", "kanban", "timeline", "graph"), fields=("title", "body"), fit="hug", solid=True, labelled=True, cell=True,
         tone_group="card", connectable=True, edit_limit="label", node=True, measure=measure, readback=readback,
         checks=(truncated_label, owner_unknown), emit=emit, hit=hit, text_edit=text_edit, parts=parts,
         doc="a titled card with a body, icon, badges, owner and status",
         tool=Tool(key="c", title="Card", glyph="▤", gesture="block", order=45, template='{"op": "card", "title": ""}')),
)

