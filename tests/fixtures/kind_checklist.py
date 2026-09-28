"""A test-only composite block: ``checklist``, a titled column of items to tick (canvas v2 phase 2, 8.2).

``tests/test_canvas_block_one_module.py`` loads this module with ``canvas_kinds._load_extra`` and proves that one module
is all a new block needs: its op, collection, build, readback, patch, arrangement (a stack column), drawing and check,
with no other file changed. Members are free ``text`` elements; the root is stored as a frame (phase 2, D2).
"""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds import _zone
from herdr_team.canvas_kinds._common import Element
from herdr_team.canvas_kinds.sdk import Block, Collection

MAX_ITEMS = 40
TICK, BOX = "☑", "☐"


def item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} is a string or {{id, text, done}}".format(field))
    for key in raw:
        if key not in ("id", "text", "done", "_gen"):
            raise ctx.invalid("{}.{}".format(field, key), "an item takes id, text, done")
    out: Dict[str, Any] = {"text": ctx.text(raw, "text", limit="label", one_line=True, required=True, label=field + ".text"),
                           "done": ctx.boolean(raw, "done", False)}
    if raw.get("id") is not None:
        if not isinstance(raw["id"], str) or not raw["id"].isidentifier():
            raise ctx.invalid(field + ".id", "an item id is a short name")
        out["id"] = raw["id"]
    if raw.get("_gen"):
        out["_gen"] = True
    return out


def normalize(ctx: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    spec["title"] = ctx.text(spec, "title", limit="label", one_line=True) or "Checklist"
    spec.setdefault("items", [])
    return spec


def build(bctx: Any, spec: Dict[str, Any]) -> None:
    root = bctx.root_fields(text=spec["title"], settings={"layout": "column", "gap": "s", "padding": "s"},
                           style=bctx.ctx.default_style("frame"), fit={"policy": "hug", "min": [240, 120]})
    parts = []
    for entry in spec["items"]:
        text = "{} {}".format(TICK if entry["done"] else BOX, entry["text"])
        bctx.member(entry["id"], "text", text=text, style=bctx.ctx.default_style("text"), done=entry["done"])
        parts.append(entry["id"])
    bctx.keep_only(parts)
    current = {el.get("part"): el["id"] for el in bctx.ctx.live() if el.get("group") == root["id"]}
    bctx.set_order(root, [current[p] for p in parts if p in current])


def spec(root: Element, members: List[Element], full: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {"op": "checklist"}
    if root.get("alias"):
        out["id"] = root["alias"]
    out["title"] = root.get("text") or "Checklist"
    items = []
    for el in members:
        if not isinstance(el.get("part"), str):
            continue
        text = str(el.get("text") or "")[2:]
        items.append({"id": el["part"], "text": text, "done": bool(el.get("done"))})
    out["items"] = items
    return out


def unticked(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    if el.get("block") != "checklist":
        return []
    open_items = [o for o in (env.get("by_id") or {}).values() if o.get("group") == el.get("id") and not o.get("done")]
    if not open_items:
        return []
    return [{"code": "checklist_open", "ids": [str(el.get("id"))], "message": "{} has {} open items".format(el.get("id"), len(open_items)),
             "fix": None}]


def create(ctx: Any, op: Dict[str, Any]) -> None:
    ctx.block("checklist", op)


OPS = (
    OpSpec(name="checklist", fields=("title", "items", "id", "client_id"), create=create, place=True, order=140,
           doc="a titled column of items to tick", mcp="checklist {title, items [text | {id, text, done}]}"),
)

KINDS = (
    Kind(name="checklist", stored_as="frame", role="composite", ops=("checklist",), cell=True, tone_group="frame", layer="zones",
         block=Block(collections=(Collection(name="items", item=item, prefix="i", maximum=MAX_ITEMS, label="text", member="text"),),
                     parts="members", positional=False, normalize=normalize, build=build, spec=spec),
         arrange=_zone.stack_arrange, emit=lambda el, env: _zone.emit(el, env), hit=_zone.hit, text_edit=_zone.title_edit,
         checks=(unticked,), doc="a titled column of items to tick"),
)
