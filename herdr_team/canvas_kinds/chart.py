"""Charts (canvas v2): a Vega-Lite chart the page renders."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Sequence

from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds._common import Element, bounds_text, quote
from herdr_team.canvas_kinds._slot import slot_emit

MAX_CHART_SPEC_BYTES = 200 * 1024
#: How deep a chart spec may nest (Vega-Lite ``layer``/``concat``); deeper is refused, never skipped.
MAX_SPEC_DEPTH = 64


def find_key(ctx: Any, obj: Any, keys: Sequence[str], depth: int = 0) -> Optional[str]:
    """The first key named one of ``keys`` anywhere in ``obj``; a spec too deep to search is refused, never passed."""
    if depth > MAX_SPEC_DEPTH:
        raise ctx.error("chart_refused", "the chart spec nests deeper than {} levels; flatten it".format(MAX_SPEC_DEPTH),
                        limit="MAX_SPEC_DEPTH", max=MAX_SPEC_DEPTH)
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in keys:
                return key
            found = find_key(ctx, value, keys, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_key(ctx, value, keys, depth + 1)
            if found:
                return found
    return None


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``chart`` op: a Vega-Lite spec that loads nothing itself; its data inline or a file under artifacts/."""
    spec = op.get("spec")
    if not isinstance(spec, dict):
        raise ctx.invalid("spec", "chart needs spec: a Vega-Lite object")
    raw = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_CHART_SPEC_BYTES:
        raise ctx.error("chart_refused", "the chart spec is over {} KB; put the data in a file under artifacts/".format(MAX_CHART_SPEC_BYTES // 1024),
                        limit="MAX_CHART_SPEC_BYTES", max=MAX_CHART_SPEC_BYTES)
    found = find_key(ctx, spec, ("url", "href"))
    if found:
        raise ctx.error("chart_refused", "a chart spec may not load anything itself: remove {!r} and pass data (a file under artifacts/)".format(found), key=found)
    ctx.guard_code(raw, "spec")
    data_rel: Optional[str] = None
    if op.get("data") is not None:
        inline = spec.get("data")
        if isinstance(inline, dict) and "values" in inline:
            raise ctx.invalid("data", "give the data as a file (data) or inline (spec.data.values), not both")
        data_rel = ctx.artifact(op["data"])
    title = ctx.text(op, "title", limit="label", one_line=True)
    w, h = ctx.size(op, 480, 320)
    x, y, frame = ctx.place(op, w, h)
    ctx.create("chart", x, y, w, h, op=op, text=title, style=ctx.default_style("chart"), frame=frame, store={"spec_asset": (raw.encode("utf-8"), "json")},
               data=data_rel, still=None)


def readback(el: Element, full: bool) -> str:
    text = str(el.get("text") or "")
    return "{} chart{} {} {}".format(el.get("id"), " " + quote(text, 0 if full else 80) if text else "", bounds_text(el),
                                     "data {}".format(el["data"]) if el.get("data") else "inline data")


OPS = (
    OpSpec(name="chart", fields=("spec", "data", "title", "w", "h", "id", "client_id"), create=create, place=True, order=90,
           doc="a Vega-Lite chart the page renders", mcp="chart {spec (Vega-Lite), data: a file under artifacts/}"),
)

KINDS = (
    Kind(name="chart", role="leaf", ops=("chart",), page=True, solid=True, cell=True, slot="chart", connectable=True, text_edit=lambda el: None,
         readback=readback, emit=slot_emit(lambda el: "chart{} · rendered on the page".format(" of {}".format(el.get("data")) if el.get("data") else "")),
         doc="a Vega-Lite chart the page renders"),
)
