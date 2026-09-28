"""A test-only browser-drawn kind: ``gauge``, a dial reading one number from a file (canvas v2 phases 3 and 4, slot contract
v2, section 1). ``tests/test_slot_contract.py`` loads it with ``canvas_kinds._load_extra`` and proves the contract with no
chart or 3D name in it: ``Block.load`` reads ``artifacts/`` outside the lock, ``bctx.store_asset`` keeps a document only
when the op applies, two still views (``dial`` and ``side``) with a drawing each, a gist, and ``look --image --view``."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

from herdr_team import canvas_display as D
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds import _slot
from herdr_team.canvas_kinds._common import Element
from herdr_team.canvas_kinds.sdk import Block

ORDER = 5100
VIEWS = ("dial", "side")


def load(op: Mapping[str, Any], io: Any) -> Optional[Dict[str, Any]]:
    source = op.get("data")
    if op.get("op") == "patch" and isinstance(op.get("_spec"), dict):
        source = dict(op["_spec"], **(op.get("set") or {})).get("data")
    if not isinstance(source, str):
        return None
    fetched = io.read_artifact(source, (".json",), 4096, field="data")
    value = json.loads(fetched.data.decode("utf-8")).get("value")
    return {"value": float(value), "sha256": fetched.sha256, "rel": fetched.rel}


def normalize(ctx: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(spec.get("data"), str):
        raise ctx.invalid("data", "a gauge reads data: a .json file under artifacts/ holding {\"value\": n}")
    return spec


def build(bctx: Any, spec: Dict[str, Any]) -> None:
    loaded = bctx.loaded
    if loaded is None:
        loaded = bctx.fetch(dict(spec, op="gauge"))
    doc = bctx.store_asset(json.dumps({"v": 1, "value": loaded["value"]}).encode("utf-8"), "doc")
    if loaded["value"] > float(spec.get("max") or 100):
        raise bctx.ctx.invalid("max", "the value is over max")
    bctx.root_fields(text=str(spec.get("title") or ""), w=200, h=120, settings={"data": spec["data"], "max": spec.get("max")}, value=loaded["value"],
                     doc_asset=doc, source=loaded["rel"])


def spec(root: Element, members: List[Element], full: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {"op": "gauge", "data": (root.get("settings") or {}).get("data")}
    if root.get("alias"):
        out["id"] = root["alias"]
    if (root.get("settings") or {}).get("max") is not None:
        out["max"] = root["settings"]["max"]
    return out


def gist(el: Element, full: bool = False) -> List[str]:
    lines = ["gauge at {:g} of {:g}".format(float(el.get("value") or 0), float((el.get("settings") or {}).get("max") or 100))]
    return lines + (["from {}".format(el.get("source"))] if full else [])


def draw_view(el: Element, view: str) -> Optional[List[Dict[str, Any]]]:
    if view not in VIEWS:
        return None
    x0, y0, x1, y1 = D.box_of(el)
    share = float(el.get("value") or 0) / float((el.get("settings") or {}).get("max") or 100)
    if view == "dial":
        return [{"k": "rect", "x": x0, "y": y0, "w": (x1 - x0) * share, "h": y1 - y0, "fill": "chart.cat.0"}]
    return [{"k": "rect", "x": x0, "y": y1 - (y1 - y0) * share, "w": x1 - x0, "h": (y1 - y0) * share, "fill": "chart.cat.1"}]


def create(ctx: Any, op: Dict[str, Any]) -> None:
    ctx.block("gauge", op)


_EMIT = _slot.slot_emit(lambda el: "gauge", fallback=lambda el, env: draw_view(el, VIEWS[0]), views=VIEWS, doc=lambda el: el.get("doc_asset"))

OPS = (OpSpec(name="gauge", fields=("title", "data", "max", "id", "client_id"), create=create, place=True, order=5100,
              doc="a test gauge reading one number", mcp="gauge {data, max}"),)
KINDS = (Kind(name="gauge", role="composite", ops=("gauge",), solid=True, cell=True, slot="gauge", handles="box",
              block=Block(settings=("data", "max"), parts="inline", positional=False, normalize=normalize, build=build, spec=spec, load=load),
              emit=_EMIT, readback=lambda el, full: "{} gauge {:g}".format(el.get("id"), float(el.get("value") or 0)), still_views=VIEWS, gist=gist,
              gist_first=True, draw_view=draw_view, look_json=("gauges", lambda el: {"value": el.get("value")}), doc="a test gauge"),)
