"""Kanban boards (canvas v2 phase 2, 4.5): columns of cards, laid out as stacks.

A kanban is a frame (``block: "kanban"``) holding a row of columns; each column
is a section (``layout: column``, ``align: stretch``) holding its cards, which
are ``card`` elements. All of them are members of the board (``group`` and
``part``): columns by their id (``todo``), cards by ``c<n>`` (``work.c2``, an
alias that survives a move between columns, phase 2 D7).

On create and in readback the cards nest under their column; in a ``patch``
they are flat with ``in``. A card moves with ``place {id: "work.c2", in:
"work.done"}``, with ``patch update cards [{id: "c2", in: "done"}]``, or by a
person dragging it (a ``move`` into another column, which reorders it by where
it was dropped). A column's title counts its cards (``Todo · 2``), and a column
with a ``limit`` shows ``1/3``, in the danger colour when it is over.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from herdr_team import canvas_theme
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds import _zone
from herdr_team.canvas_kinds import card as _card
from herdr_team.canvas_kinds._common import Element
from herdr_team.canvas_kinds.sdk import Block, Collection

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 138

MAX_COLUMNS, MAX_CARDS = 12, 200
COLUMN_MIN = tuple((canvas_theme.section("kanban", {}) or {}).get("column_min") or (280, 200))
ROOT_MIN = (320, 200)
CLAMP = dict(((canvas_theme.section("card", {}) or {}).get("kanban_clamp") or {"title": 2, "body": 3}))
_SLUG = re.compile(r"[^a-z0-9]+")
CARD_KEYS = ("id", "in", "title", "body", "owner", "badges", "status", "tone", "icon", "detail", "_gen")


def slug(title: str) -> str:
    found = _SLUG.sub("-", title.lower()).strip("-")[:32].strip("-")
    return found or "column"


def column_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    """``{id, title, tone, limit, cards}`` (``"Todo"`` alone is a title); the nested cards are read by ``normalize``."""
    if isinstance(raw, str):
        raw = {"title": raw}
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} is a title or {{id, title, tone, limit, cards}}".format(field))
    for key in raw:
        if key not in ("id", "title", "tone", "limit", "cards", "_cards"):
            raise ctx.invalid("{}.{}".format(field, key), "a column takes id, title, tone, limit, cards")
    out: Dict[str, Any] = {"title": ctx.text(raw, "title", limit="label", one_line=True, label=field + ".title")}
    if raw.get("id") is not None:
        ident = raw["id"]
        if not isinstance(ident, str) or not re.match(r"^[A-Za-z][A-Za-z0-9_-]{0,31}\Z", ident):
            raise ctx.invalid(field + ".id", "a column id is a letter then up to 31 letters, digits, _ or -")
        out["id"] = ident
    if raw.get("tone") is not None:
        out["tone"] = ctx.choice(raw, "tone", canvas_theme.TONES, "neutral", label=field + ".tone")
    if raw.get("limit") is not None:
        limit = raw["limit"]
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_CARDS:
            raise ctx.invalid(field + ".limit", "limit is a whole number of cards from 1 to {}".format(MAX_CARDS))
        out["limit"] = limit
    cards = raw.get("cards", raw.get("_cards"))
    if cards is not None:
        if not isinstance(cards, list):
            raise ctx.invalid(field + ".cards", "cards is a list")
        out["_cards"] = cards
        out["_field"] = field
    return out


def card_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    """A card: its title alone, or ``{id, in, title, body, owner, badges, status, tone, icon, detail}``."""
    if isinstance(raw, str):
        raw = {"title": raw}
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} is a title or {{id, in, title, body, owner, badges, status, tone, icon, detail}}".format(field))
    for key in raw:
        if key not in CARD_KEYS:
            raise ctx.invalid("{}.{}".format(field, key), "a card takes {}".format(", ".join(k for k in CARD_KEYS if not k.startswith("_"))))
    out = _card.card_fields(ctx, raw, field)
    if raw.get("id") is not None:
        if not isinstance(raw["id"], str) or not re.match(r"^[A-Za-z][A-Za-z0-9_-]{0,31}\Z", raw["id"]):
            raise ctx.invalid(field + ".id", "a card id is a letter then up to 31 letters, digits, _ or -")
        out["id"] = raw["id"]
    if raw.get("in") is not None:
        if not isinstance(raw["in"], str):
            raise ctx.invalid(field + ".in", "in is a column id")
        out["in"] = raw["in"]
    if raw.get("_gen"):
        out["_gen"] = True
    return out


def normalize(ctx: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Column ids (a slug of the title, made unique), nested cards flattened with ``in``, and every ``in`` a column."""
    spec["title"] = ctx.text(spec, "title", limit="label", one_line=True)
    columns = spec.get("columns") or []
    if not columns:
        raise ctx.invalid("columns", 'a kanban needs columns: [{"title": "Todo", "cards": ["..."]}]')
    if len(columns) > MAX_COLUMNS:
        raise ctx.too_big("columns", "MAX_COLUMNS", MAX_COLUMNS, "{} columns; the limit is {}".format(len(columns), MAX_COLUMNS))
    taken = {c["id"] for c in columns if c.get("id")}
    for column in columns:
        if column.get("id"):
            continue
        base = slug(column.get("title") or "column")
        ident, n = base, 1
        while ident in taken:
            n += 1
            ident = "{}-{}".format(base, n)
        column["id"] = ident
        taken.add(ident)
    ids = [c["id"] for c in columns]
    cards: List[Dict[str, Any]] = []
    for index, column in enumerate(columns):
        nested = column.pop("_cards", None)
        field = column.pop("_field", "columns[{}]".format(index))
        for n, raw in enumerate(nested or []):
            item = card_item(ctx, raw, "{}.cards[{}]".format(field, n))
            item["in"] = column["id"]
            cards.append(item)
    for index, item in enumerate(spec.get("cards") or []):
        if item.get("in") is None:
            item["in"] = ids[0]
        elif item["in"] not in ids:
            raise ctx.invalid("cards[{}].in".format(index), "{} is not a column; the columns are {}".format(item["in"], ", ".join(ids)))
        cards.append(item)
    if len(cards) > MAX_CARDS:
        raise ctx.too_big("cards", "MAX_CARDS", MAX_CARDS, "{} cards; the limit is {}".format(len(cards), MAX_CARDS))
    # Cards read column by column, so an op and its readback list them in the same order.
    rank = {cid: i for i, cid in enumerate(ids)}
    spec["cards"] = sorted(cards, key=lambda item: rank[item["in"]])
    return spec


def build(bctx: Any, spec: Dict[str, Any]) -> None:
    ctx = bctx.ctx
    op = bctx.op
    style = None
    if bctx.root is None or op.get("tone") is not None:
        base = bctx.root.get("style") if bctx.root is not None and isinstance(bctx.root.get("style"), dict) else None
        style = ctx.style({"tone": op["tone"]} if op.get("tone") is not None else {}, "frame", base)
    fields: Dict[str, Any] = {}
    if bctx.root is None:
        fields.update(fit={"policy": "hug", "min": list(ROOT_MIN)}, w=ROOT_MIN[0], h=ROOT_MIN[1])
    root = bctx.root_fields(text=spec.get("title") or "", style=style, settings={"layout": "row", "gap": "s", "padding": "m"}, **fields)
    columns: Dict[str, Element] = {}
    for column in spec["columns"]:
        settings = {"layout": "column", "align": "stretch", "gap": "s", "padding": "s", "counter": True}
        if column.get("limit"):
            settings["limit"] = column["limit"]
        col_style = ctx.style({"tone": column.get("tone") or "neutral"}, "frame")
        columns[column["id"]] = bctx.member(column["id"], "section", text=column.get("title") or "", style=col_style, frame=root["id"],
                                            minimum=COLUMN_MIN, settings=settings, fit={"policy": "hug", "min": list(COLUMN_MIN)})
    placed: Dict[str, List[str]] = {cid: [] for cid in columns}
    for item in spec.get("cards") or []:
        column = columns[item["in"]]
        stored = _card.element_fields(ctx, item)
        card_style = ctx.style({"tone": item.get("tone") or "neutral", "variant": "outline"}, "card")
        el = bctx.member(item["id"], "card", text=item.get("title") or "", style=card_style, frame=column["id"], clamp=dict(CLAMP), size="m",
                         **stored)
        placed[item["in"]].append(el["id"])
    bctx.keep_only(list(columns) + [item["id"] for item in spec.get("cards") or []])
    current = ctx.el(root["id"]) or root
    bctx.set_order(current, [columns[c["id"]]["id"] for c in spec["columns"]])
    for cid, ids in placed.items():
        bctx.set_order(ctx.el(columns[cid]["id"]) or columns[cid], ids)


def _card_readback(el: Element, generated: bool) -> Any:
    item: Dict[str, Any] = {}
    if not generated:
        item["id"] = el["part"]
    item["title"] = str(el.get("text") or "")
    for key in ("body", "owner", "status", "icon", "detail"):
        if isinstance(el.get(key), str) and el[key]:
            item[key] = el[key]
    badges = el.get("badges") if isinstance(el.get("badges"), list) else []
    if badges:
        item["badges"] = [b["text"] if not b.get("tone") else {"text": b["text"], "tone": b["tone"]} for b in badges if isinstance(b, dict)]
    tone = (el.get("style") or {}).get("tone") if isinstance(el.get("style"), dict) else None
    if tone and tone != "neutral":
        item["tone"] = tone
    if list(item) == ["title"]:
        return item["title"]
    return item


def spec(root: Element, members: List[Element], full: bool) -> Dict[str, Any]:
    """The op, with each column's cards nested under it, in their order."""
    from herdr_team.canvas_blocks import generated_ids

    out: Dict[str, Any] = {"op": "kanban"}
    if root.get("alias"):
        out["id"] = root["alias"]
    if root.get("text"):
        out["title"] = root["text"]
    by_id = {el["id"]: el for el in members}
    column_ids = [i for i in root.get("order") or [] if i in by_id]
    column_ids += [el["id"] for el in members if el.get("frame") == root["id"] and el.get("type") == "frame" and el["id"] not in column_ids]
    flat: List[Element] = []
    columns: List[Dict[str, Any]] = []
    for cid in column_ids:
        column = by_id[cid]
        cards = [by_id[i] for i in column.get("order") or [] if i in by_id and isinstance(by_id[i].get("part"), str)]
        cards += [el for el in members if el.get("frame") == cid and el.get("type") == "card" and el not in cards and isinstance(el.get("part"), str)]
        flat += cards
        columns.append({"column": column, "cards": cards})
    generated = dict(zip([el["part"] for el in flat], generated_ids("c", [el["part"] for el in flat])))
    out["columns"] = []
    for entry in columns:
        column = entry["column"]
        item: Dict[str, Any] = {}
        title = str(column.get("text") or "")
        if column["part"] != slug(title or "column"):
            item["id"] = column["part"]
        item["title"] = title
        tone = (column.get("style") or {}).get("tone") if isinstance(column.get("style"), dict) else None
        if tone and tone != "neutral":
            item["tone"] = tone
        limit = _zone.settings_of(column).get("limit")
        if limit:
            item["limit"] = limit
        item["cards"] = [_card_readback(card, generated.get(card["part"], False)) for card in entry["cards"]]
        out["columns"].append(item)
    return out


def adopt(root: Element, el: Element) -> Optional[str]:
    """A card or a sticky dropped into a column joins the board as a card (a sticky becomes one, its text the title)."""
    if el.get("type") not in ("card", "sticky"):
        return None
    seq = root.get("seq") if isinstance(root.get("seq"), dict) else {}
    return "c{}".format(int(seq.get("c") or 0) + 1)


def _convert(root: Element, el: Element) -> Dict[str, Any]:
    """What an adopted element becomes: a sticky turns into a card (the kanban's clamps apply to every card)."""
    fields: Dict[str, Any] = {"clamp": dict(CLAMP), "size": el.get("size") if el.get("type") == "card" else "m"}
    if el.get("type") == "sticky":
        tone = (el.get("style") or {}).get("tone") if isinstance(el.get("style"), dict) else None
        fields.update(type="card", style=dict(el.get("style") or {}, tone=tone or "neutral", variant="outline"))
    return fields


adopt.convert = _convert  # type: ignore[attr-defined]


def wip(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``wip_exceeded``: a column holds more cards than its limit; the fix moves its newest card to the column before."""
    if el.get("block") != "kanban":
        return []
    by_id = env.get("by_id") or {}
    order = [i for i in el.get("order") or [] if i in by_id]
    out = []
    for index, cid in enumerate(order):
        column = by_id[cid]
        limit = _zone.settings_of(column).get("limit")
        cards = [o for o in by_id.values() if o.get("frame") == cid and o.get("type") == "card"]
        if not isinstance(limit, int) or len(cards) <= limit:
            continue
        newest = max(cards, key=lambda c: (int(c.get("created_seq") or 0), c["id"]))
        fix = None
        if index > 0:
            before = by_id[order[index - 1]]
            fix = {"op": "place", "id": newest.get("alias") or newest["id"], "in": before.get("alias") or before["id"]}
        out.append({"code": "wip_exceeded", "ids": [cid, newest["id"]],
                    "message": "{} ({}) holds {} cards over its limit of {}; move one back".format(
                        cid, column.get("text") or column.get("part"), len(cards), limit), "fix": fix})
    return out


def create(ctx: Any, op: Dict[str, Any]) -> None:
    ctx.block("kanban", op)


OPS = (
    OpSpec(name="kanban", family="block", fields=("title", "columns", "tone", "id", "client_id"), create=create, place=True, order=44,
           doc="a kanban board: columns of cards (move a card with place in, or patch)",
           mcp="kanban {title, columns [{id, title, tone, limit, cards [title | {id, title, body, owner, badges, status, tone, icon}]}]}"),
)

KINDS = (
    Kind(name="kanban", stored_as="frame", role="composite", ops=("kanban",), cell=True, tone_group="frame", layer="zones",
         block=Block(collections=(Collection(name="columns", item=column_item, maximum=MAX_COLUMNS, label="title", member="section"),
                                  Collection(name="cards", item=card_item, prefix="c", maximum=MAX_CARDS, label="title", member="card",
                                             refs=(("in", "columns", "clear"),))),
                     parts="members", positional=False, normalize=normalize, build=build, spec=spec, adopt=adopt,
                     max_members=MAX_COLUMNS + MAX_CARDS),
         arrange=_zone.stack_arrange, emit=lambda el, env: _zone.emit(el, env), hit=_zone.hit, text_edit=_zone.title_edit, checks=(wip,),
         noun=("kanban", "kanbans"), doc="columns of cards, laid out as stacks"),
)
