"""Live visuals (canvas v2): a sealed page (d3, three, p5) the browser runs in a sandbox."""
from __future__ import annotations

import json
from typing import Any, Dict

from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds._common import Element, bounds_text, quote
from herdr_team.canvas_kinds._slot import slot_emit

VIZ_LIBS = ("d3", "three", "p5")
MAX_VIZ_BYTES = 200 * 1024
MAX_VIZ_DATA_BYTES = 64 * 1024


def _subtitle(el: Element) -> str:
    libs = [str(lib) for lib in el.get("libs") or []] if isinstance(el.get("libs"), list) else []
    return "live visual{} · runs on the page".format(" ({})".format(", ".join(libs)) if libs else "")


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``viz`` op: html that draws with the bundled libraries, run sealed on the page (the team must allow live visuals)."""
    ctx.require_viz()
    html = op.get("html")
    if not isinstance(html, str) or not html.strip():
        raise ctx.invalid("html", "viz needs html: the page body that draws it")
    if len(html.encode("utf-8")) > MAX_VIZ_BYTES:
        raise ctx.too_big("html", "MAX_VIZ_BYTES", MAX_VIZ_BYTES, "the viz html is over {} KB".format(MAX_VIZ_BYTES // 1024))
    ctx.guard_code(html, "html")
    libs_raw = op.get("libs") if op.get("libs") is not None else []
    if not isinstance(libs_raw, list) or any(lib not in VIZ_LIBS for lib in libs_raw):
        raise ctx.invalid("libs", "libs is a list of: {}".format(", ".join(VIZ_LIBS)))
    libs = list(dict.fromkeys(libs_raw))
    data, data_path = op.get("data"), op.get("data_path")
    if data is not None and data_path is not None:
        raise ctx.invalid("data", "give data inline or as data_path, not both")
    if data is not None:
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > MAX_VIZ_DATA_BYTES:
            raise ctx.too_big("data", "MAX_VIZ_DATA_BYTES", MAX_VIZ_DATA_BYTES, "inline viz data is over {} KB; use data_path".format(MAX_VIZ_DATA_BYTES // 1024))
        ctx.guard_code(raw, "data")
    rel = ctx.artifact(data_path) if data_path is not None else None
    title = ctx.text(op, "title", limit="label", one_line=True, required=True)
    w, h = ctx.size(op, 480, 360)
    x, y, frame = ctx.place(op, w, h)
    ctx.create("viz", x, y, w, h, op=op, text=title, style=ctx.default_style("viz"), frame=frame, store={"html_asset": (html.encode("utf-8"), "html")},
               libs=libs, data=data, data_path=rel, still=None)


def readback(el: Element, full: bool) -> str:
    libs = ", ".join(str(lib) for lib in el.get("libs") or []) if isinstance(el.get("libs"), list) else ""
    return "{} viz {} ({}) {}".format(el.get("id"), quote(el.get("text"), 0 if full else 80), "live: " + libs if libs else "live", bounds_text(el))


OPS = (
    OpSpec(name="viz", fields=("html", "libs", "data", "data_path", "title", "w", "h", "id", "client_id"), create=create, place=True, order=100,
           doc="a sealed live visual (d3, three, p5) the page runs",
           mcp="viz {html, libs [d3,three,p5], title; draw to synapse.width x synapse.height}"),
)

KINDS = (
    Kind(name="viz", role="leaf", ops=("viz",), page=True, noun=("live visual", "live visuals"), solid=True, cell=True, slot="viz",
         connectable=True, edit_limit="label", edit_required=True, text_edit=lambda el: None, readback=readback, emit=slot_emit(_subtitle),
         doc="a sealed live visual (d3, three, p5) the page runs"),
)
