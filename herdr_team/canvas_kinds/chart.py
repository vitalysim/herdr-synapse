"""Charts (canvas v2 phase 3, ``.local/prd/canvas-v2-phase3-4.md`` 2): a chart an agent describes, Python compiles and draws.

A ``chart`` is an inline block (``Block.parts == "inline"``) whose settings are
its spec, in one of three engines:

* ``echarts`` (``type`` given): the flat spec (``type``, ``data`` or ``rows``,
  ``x``, ``y``, ``color`` ...). Python validates it against the data, snapshots
  what it read (``model``, ``stats``, a content-addressed ``doc`` asset with
  the page's datasets), fits it to its box (``chart_frame``), and writes a
  theme-neutral ECharts ``option`` and a ``gist`` in words
  (``canvas_charts``). The agent's picture is Python's own drawing of the chart
  (the slot's ``fallback``, ``drawn: true``); the page shows the same drawing
  until ECharts has drawn it.
* ``vega-lite`` (``spec`` given): today's raw Vega-Lite spec, stored as a
  ``spec_asset`` and rendered by the page through ``vega-interpreter``; it reads
  back as a gist, and a plain single view is also drawn by Python.
* ``echarts-raw`` (``echarts`` given): a raw ECharts option, sanitised against
  ``assets/canvas/echarts-allow.json`` and stored as a ``doc`` asset.

Data is snapshotted when the op runs (D6): a file under ``artifacts/`` is read
in ``Block.load``, outside the canvas lock, and a later edit of the file changes
nothing drawn until ``patch {relayout: "full"}`` or an upsert re-reads it. A
visual change (a title, a legend, a highlight, a resize) re-frames the stored
model with no I/O. Every flat chart also stores a small Vega-Lite spec over its
model (``spec_asset``), so the frozen v1 page still draws it (D18).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import OrderedDict
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team import canvas_charts as CC
from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R
from herdr_team import canvas_text
from herdr_team.canvas_charts import _data, _sanitize
from herdr_team.canvas_charts import _vegalite as VL
from herdr_team.canvas_charts._frame import Frame
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds import _slot
from herdr_team.canvas_kinds import _text as TX
from herdr_team.canvas_kinds._common import Element, quote
from herdr_team.canvas_kinds.sdk import Block
from herdr_team.errors import HerdrTeamError

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 90

KIND_VERSION = 2
#: Re-exported for ``canvas`` and its tests: the Vega-Lite escape hatch's limits (2.8).
MAX_CHART_SPEC_BYTES = VL.MAX_CHART_SPEC_BYTES
MAX_SPEC_DEPTH = VL.MAX_SPEC_DEPTH
MAX_SIDE = 4000.0
PAD = 12.0
TITLE_GAP = 8.0
CAPTION_H = 16.0
TITLE_SIZE, TITLE_WEIGHT = 16.0, 600
CAPTION_SIZE = 12.0
#: A Vega-Lite or raw ECharts chart's size when the op gives none (a flat chart takes its type's ``default_box``).
ESCAPE_BOX = (480.0, 320.0)
MIN_SIZE = (160.0, 100.0)
ENGINES = ("echarts", "vega-lite", "echarts-raw")
_VL_ASSET = re.compile(r"^[0-9a-f]{32}\.vl\.json\Z")
_DOC_ASSET = re.compile(r"^[0-9a-f]{32}\.json\Z")
#: Fields a chart's element keeps beside its settings (what the page, the drawing and the gist read).
SEVERITY = {"chart_labels": 1, "chart_crowded": 2, "chart_pie_slices": 2, "chart_contrast": 2, "chart_capped": 3, "chart_escape": 4}
#: Series past this many make a chart crowded (``chart_crowded``), as do legends of more rows.
CROWDED_SERIES = 10
CROWDED_LEGEND_ROWS = 8


# --------------------------------------------------------------------------
# fields (the chart types' channels and options join the common ones; a new type module adds its own)


def op_fields() -> Tuple[str, ...]:
    """Every field the ``chart`` op takes: the flat spec's (``canvas_charts.fields()``), the escape hatches' and the
    field aliases, each beside the field it stands for (``kind`` for ``type``, QA phase 6 F8). ``mcp`` still teaches
    ``type`` alone, and a chart drawn with an alias earns a ``field_alias`` warning."""
    out: List[str] = []
    for name in dict.fromkeys(("title",) + tuple(CC.fields()) + ("spec", "spec_asset", "echarts", "echarts_asset")):
        out.append(name)
        out.extend(key for key, target in CC.FIELD_ALIASES.items() if target == name)
    return tuple(out)


def settings_fields() -> Tuple[str, ...]:
    """The fields the block pipeline hands ``normalize``: the op's without the title. The aliases stay in (that is how
    ``kind`` reaches ``normalize``); nothing stores one, because ``normalize`` resolves it into ``type`` first."""
    return tuple(f for f in op_fields() if f != "title")


class _Pure:
    """The refusals and text cleaning ``normalize`` needs, for ``Block.load`` (outside the canvas; the op itself is
    normalized again under the lock with the canvas's own context, which also refuses secrets)."""

    def invalid(self, field: str, message: str, **details: Any) -> HerdrTeamError:
        return _data.invalid(field, message, **details)

    def error(self, code: str, message: str, **details: Any) -> HerdrTeamError:
        return _data.refuse(code, message, **details)

    def too_big(self, field: str, limit: str, maximum: int, message: str) -> HerdrTeamError:
        return _data.too_big(field, limit, maximum, message)

    def text(self, op: Mapping[str, Any], field: str, *, limit: str = "text", one_line: bool = False, required: bool = False,
             value: Any = ..., label: Optional[str] = None) -> str:
        raw = op.get(field) if value is ... else value
        return " ".join(str(raw or "").split()) if one_line else str(raw or "").strip()

    def guard_code(self, text: str, field: str) -> None:
        return None


PURE = _Pure()


# --------------------------------------------------------------------------
# normalize (pure: the same path for a create, an upsert, a patch and a readback)


def engine_of(spec: Mapping[str, Any]) -> str:
    """Which engine a spec (or a stored element's settings) is in."""
    if spec.get("spec") is not None or spec.get("spec_asset") is not None:
        return "vega-lite"
    if spec.get("echarts") is not None or spec.get("echarts_asset") is not None:
        return "echarts-raw"
    return "echarts"


def _asset_name(raw: bytes, ext: str) -> str:
    return "{}.{}".format(hashlib.sha256(raw).hexdigest()[:32], ext)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def normalize(ctx: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    """The op as a canonical spec: exactly one of ``type``, ``spec`` or ``echarts`` (2.1); a flat spec checked for shape and
    its aliases resolved; a Vega-Lite spec or raw ECharts option checked, and named by the asset it will be stored as."""
    out: Dict[str, Any] = {"op": "chart"}
    if isinstance(spec.get("id"), str):
        out["id"] = spec["id"]
    title = ctx.text(spec, "title", limit="label", one_line=True)
    if title:
        out["title"] = title
    # ``kind`` counts as ``type`` here too, so the alias reaches ``normalize_spec`` instead of being refused as no family.
    type_keys = ("type",) + tuple(key for key, target in CC.FIELD_ALIASES.items() if target == "type")
    families = [name for name, keys in (("type", type_keys), ("spec", ("spec", "spec_asset")), ("echarts", ("echarts", "echarts_asset")))
                if any(spec.get(k) is not None for k in keys)]
    if len(families) != 1:
        known = CC.names()
        raise ctx.invalid("type", "{}; a chart takes exactly one of: type (one of: {}), spec (Vega-Lite) or echarts (an ECharts option)".format(
            "invalid type" if not families else "give one of type, spec or echarts, not {}".format(" and ".join(families)), ", ".join(known)),
            types=known)
    family = families[0]
    raw = {k: v for k, v in spec.items() if k not in ("op", "id", "title") and not k.startswith("_")}
    if family == "type":
        flat, warnings = CC.normalize_spec(raw)
        if flat.get("caption"):
            flat["caption"] = ctx.text(flat, "caption", limit="label", one_line=True)
        for index, note in enumerate(flat.get("annotations") or []):
            if note.get("text"):
                note["text"] = ctx.text(note, "text", limit="label", one_line=True, label="annotations[{}].text".format(index))
        if flat.get("rows") is not None:
            ctx.guard_code(_dump(flat["rows"]), "rows")
        out.update(flat)
        if warnings:
            out["_warnings"] = warnings
        return out
    extra = [k for k in raw if k not in (("spec", "spec_asset", "data") if family == "spec" else ("echarts", "echarts_asset", "rows"))]
    if extra:
        raise ctx.invalid(extra[0], "a chart with {} takes {}; {} belongs to the flat spec (type)".format(
            family, "data" if family == "spec" else "rows", extra[0]))
    if family == "spec":
        if raw.get("spec") is not None:
            text = VL.check_spec(raw["spec"])
            ctx.guard_code(text, "spec")
            inline = raw["spec"].get("data") if isinstance(raw["spec"], dict) else None
            if raw.get("data") is not None and isinstance(inline, dict) and "values" in inline:
                raise ctx.invalid("data", "give the data as a file (data) or inline (spec.data.values), not both")
            out["spec_asset"] = _asset_name(text.encode("utf-8"), "vl.json")
            out["_vl"] = text
        else:
            if not isinstance(raw.get("spec_asset"), str) or not _VL_ASSET.match(raw["spec_asset"]):
                raise ctx.invalid("spec_asset", "spec_asset names a stored Vega-Lite spec (<32 hex>.vl.json); give spec to write a new one")
            out["spec_asset"] = raw["spec_asset"]
        if raw.get("data") is not None:
            out["data"] = CC._common("data", raw["data"])
        return out
    if raw.get("echarts") is not None:
        option = raw["echarts"]
        if raw.get("rows") is not None and isinstance(option, dict) and "dataset" not in option:
            rows = CC._common("rows", raw["rows"])
            if len(rows) > _data.MAX_INLINE_ROWS:
                raise ctx.too_big("rows", "MAX_INLINE_ROWS", _data.MAX_INLINE_ROWS, "{} inline rows; the limit is {}".format(len(rows), _data.MAX_INLINE_ROWS))
            option = dict(option, dataset={"source": rows})
        clean, stripped = _sanitize.sanitize(option)
        text = _dump({"v": 1, "option": clean})
        ctx.guard_code(text, "echarts")
        out["echarts_asset"] = _asset_name(text.encode("utf-8"), "json")
        out["_echarts"] = text
        out["_stripped"] = stripped
    else:
        if not isinstance(raw.get("echarts_asset"), str) or not _DOC_ASSET.match(raw["echarts_asset"]):
            raise ctx.invalid("echarts_asset", "echarts_asset names a stored ECharts option (<32 hex>.json); give echarts to write a new one")
        out["echarts_asset"] = raw["echarts_asset"]
    return out


def settings_of(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """What an element keeps as its settings: the spec without the op, id, title and private keys."""
    return {k: v for k, v in spec.items() if k not in ("op", "id", "title") and not k.startswith("_")}


# --------------------------------------------------------------------------
# loading (outside the canvas lock, D6)


def _prospective(op: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The spec an op will build, as far as it can be known without the lock: a chart op's own fields, or a patch's
    ``set`` over the block's spec as it stood (None when a patch changes nothing of the chart)."""
    if op.get("op") == "patch":
        base = op.get("_spec")
        if not isinstance(base, dict):
            return None
        out = dict(base)
        changes = op.get("set") if isinstance(op.get("set"), dict) else {}
        for key, value in changes.items():
            if value is None:
                out.pop(key, None)
            else:
                out[key] = value
        return out
    kept = set(op_fields()) | {"id"}
    return {k: v for k, v in op.items() if k in kept or k.startswith("_")}


def read_table(spec: Mapping[str, Any], io: Any) -> Tuple[_data.Table, Optional[str]]:
    """The flat spec's table: its file under ``artifacts/`` (through ``FetchIO``), or its inline rows."""
    if spec.get("data") is not None:
        fetched = io.read_artifact(spec["data"], _data.DATA_SUFFIXES, _data.MAX_CHART_DATA_BYTES, field="data")
        return _data.parse(fetched.data, fetched.suffix, fetched.rel), fetched.rel
    return _data.inline(spec.get("rows"), spec.get("columns")), None


def load(op: Mapping[str, Any], io: Any) -> Optional[Dict[str, Any]]:
    """``Block.load``: read the data and compile its half of the chart before the lock (a 5 MB file parses here, never
    under it). A patch that changes only how the chart looks reads nothing; ``relayout: "full"`` and an upsert re-read."""
    found = _prospective(op)
    if found is None:
        return None
    try:
        spec = normalize(PURE, dict(found, op="chart"))
    except HerdrTeamError:
        return None  # the op reports what is wrong itself, under the lock
    engine = engine_of(spec)
    root = op.get("_root") if isinstance(op.get("_root"), dict) else None
    if engine == "echarts":
        settings = settings_of(spec)
        key = CC.data_key(settings)
        if op.get("op") == "patch" and op.get("relayout") != "full" and root is not None and root.get("data_key") == key:
            return None
        table, rel = read_table(settings, io)
        return {"engine": engine, "key": key, "part": CC.compile_data(settings, table, rel)}
    if engine == "vega-lite" and spec.get("_vl"):
        return {"engine": engine, "key": spec["spec_asset"] + "|" + str(spec.get("data") or ""), "vl": vega_facts(spec, io)}
    return None


def _vl_table(raw: Mapping[str, Any], io: Any, rel: Optional[str]) -> Optional[_data.Table]:
    """The table a Vega-Lite spec draws, when Python can read it (a file it names, or its inline values); None when it
    cannot (nested JSON the spec flattens itself): the chart still applies, and reads back from its fields only."""
    try:
        if rel is not None and io is not None:
            fetched = io.read_artifact(rel, _data.DATA_SUFFIXES, _data.MAX_CHART_DATA_BYTES, field="data")
            return _data.parse(fetched.data, fetched.suffix, fetched.rel)
        values = VL.inline_values(raw)
        if values is not None:
            return _data.from_json(values, "inline values", _asset_name(_dump(values).encode("utf-8"), "json")[:32])
    except HerdrTeamError as err:
        if rel is not None and err.code in ("path_refused", "artifacts_unset", "canvas_limit"):
            raise
    return None


def vega_facts(spec: Mapping[str, Any], io: Any) -> Dict[str, Any]:
    """What a Vega-Lite chart reads back as: its gist, and when it is a plain single view the flat model Python draws."""
    raw = json.loads(spec["_vl"])
    table = _vl_table(raw, io, spec.get("data"))
    gist = VL.gist_from_spec(raw, table)
    out: Dict[str, Any] = {"gist": gist, "brief": {k: raw[k] for k in ("mark", "encoding") if k in raw}}
    flat = VL.to_flat(raw)
    if flat is not None and table is not None:
        try:
            normal, _warnings = CC.normalize_spec(dict(flat, rows=[{}]))
            normal.pop("rows", None)
            out["part"] = CC.compile_data(normal, table, spec.get("data"))
            out["flat"] = normal
        except HerdrTeamError:
            pass
    return out


def _loaded(bctx: Any) -> Optional[Dict[str, Any]]:
    try:
        found = bctx.loaded
    except AttributeError:
        return None
    return found if isinstance(found, dict) else None


# --------------------------------------------------------------------------
# geometry: the card, the title band, the slot


def title_band(title: str) -> float:
    return PAD + canvas_text.line_height(TITLE_SIZE) + TITLE_GAP if title.strip() else PAD


def slot_box(x0: float, y0: float, w: float, h: float, title: str, caption: str) -> Tuple[float, float, float, float]:
    band = title_band(title)
    return x0 + PAD, y0 + band, max(1.0, w - 2 * PAD), max(1.0, h - band - PAD - (CAPTION_H if caption else 0.0))


def _el_slot(el: Mapping[str, Any]) -> Tuple[float, float, float, float]:
    x0, y0, x1, y1 = D.box_of(el)
    return slot_box(x0, y0, x1 - x0, y1 - y0, str(el.get("text") or ""), str(_settings(el).get("caption") or ""))


def _settings(el: Mapping[str, Any]) -> Dict[str, Any]:
    found = el.get("settings")
    return dict(found) if isinstance(found, dict) else {}


def _part_of(el: Mapping[str, Any]) -> Optional[CC.DataPart]:
    """The data half a flat chart stored (no file read): what a visual change or a resize re-frames."""
    if not isinstance(el.get("model"), dict) or not isinstance(el.get("resolved"), dict):
        return None
    return CC.DataPart(spec=dict(el["resolved"]), model=dict(el["model"]), doc={}, stats=dict(el.get("stats") or {}), vegalite={}, warnings=[],
                       source=dict(el.get("source") or {}), key=str(el.get("data_key") or ""))


def _fit_size(spec: Mapping[str, Any], part: CC.DataPart, w: float, h: float, title: str, caption: str) -> Tuple[float, float, bool]:
    """``w``, ``h`` grown to the chart type's legible minimum (D20), and whether they grew."""
    chart = CC.get(part.spec.get("type"))
    if chart is None:
        return w, h, False
    try:
        mw, mh = chart.min_box(dict(part.spec), part.model)
    except (TypeError, ValueError, KeyError, IndexError, ZeroDivisionError):
        return w, h, False
    band = title_band(title)
    need_w = mw + 2 * PAD
    need_h = mh + band + PAD + (CAPTION_H if caption else 0.0)
    grown_w, grown_h = min(MAX_SIDE, max(w, need_w)), min(MAX_SIDE, max(h, need_h))
    return grown_w, grown_h, (grown_w > w + 0.5 or grown_h > h + 0.5)


def view_fields(settings: Mapping[str, Any], part: CC.DataPart, x0: float, y0: float, w: float, h: float, title: str) -> Dict[str, Any]:
    """``chart_frame`` (the ``canvas_charts`` ``Frame``; ``frame`` is an element's enclosing frame), ``option`` and ``gist`` of a
    flat chart at a size (pure; a resize runs only this)."""
    _sx, _sy, sw, sh = slot_box(x0, y0, w, h, title, str(settings.get("caption") or ""))
    frame, option, gist = CC.view(settings, part, (sw, sh))
    return {"chart_frame": frame.to_json(), "option": option, "gist": gist}


# --------------------------------------------------------------------------
# build


def _size_of(ctx: Any, op: Mapping[str, Any], field: str, default: float) -> float:
    value = op.get(field)
    if value is None:
        return default
    found = ctx.number(dict(op), field, 1, MAX_SIDE)
    return max(float(found), MIN_SIZE[0] if field == "w" else MIN_SIZE[1])


def build(bctx: Any, spec: Dict[str, Any]) -> None:
    ctx = bctx.ctx
    root = bctx.root
    op = bctx.op
    settings = settings_of(spec)
    engine = engine_of(settings)
    title = str(spec.get("title") or "")
    for warning in spec.get("_warnings") or []:
        bctx.warn(warning.get("code") or "chart_note", str(warning.get("message") or ""), [])
    style = None if root is not None else ctx.default_style("chart")
    if engine == "echarts":
        fields = _build_flat(bctx, settings, title)
    elif engine == "vega-lite":
        fields = _build_vega(bctx, spec, settings)
    else:
        fields = _build_raw(bctx, spec, settings)
    fields["kv"] = KIND_VERSION
    bctx.root_fields(text=title, style=style, settings=settings, replace_settings=True, **fields)


def _sizes(bctx: Any, default: Tuple[float, float]) -> Tuple[float, float]:
    root = bctx.root
    base = (float(root.get("w") or default[0]), float(root.get("h") or default[1])) if root is not None else default
    return _size_of(bctx.ctx, bctx.op, "w", base[0]), _size_of(bctx.ctx, bctx.op, "h", base[1])


def _build_flat(bctx: Any, settings: Dict[str, Any], title: str) -> Dict[str, Any]:
    root = bctx.root
    key = CC.data_key(settings)
    loaded = _loaded(bctx)
    part: Optional[CC.DataPart] = None
    fresh = False
    if loaded is not None and loaded.get("key") == key and isinstance(loaded.get("part"), CC.DataPart):
        part, fresh = loaded["part"], True
    elif root is not None and root.get("data_key") == key and root.get("engine") == "echarts":
        part = _part_of(root)
    if part is None:
        found = bctx.fetch(dict(settings, op="chart"))
        if not isinstance(found, dict) or not isinstance(found.get("part"), CC.DataPart):
            raise bctx.ctx.invalid("data", "the chart's data could not be read")
        part, fresh = found["part"], True
    chart = CC.get(part.spec.get("type"))
    assert chart is not None
    w, h = _sizes(bctx, tuple(float(v) for v in chart.default_box))  # type: ignore[arg-type]
    caption = str(settings.get("caption") or "")
    w, h, grew = _fit_size(settings, part, w, h, title, caption)
    if grew:
        bctx.warn("chart_grew", "the {} chart grew to {}x{} so its labels fit".format(chart.name, int(round(w)), int(round(h))), [])
    x0 = float(root.get("x") or 0) if root is not None else 0.0
    y0 = float(root.get("y") or 0) if root is not None else 0.0
    shown = view_fields(settings, part, x0, y0, w, h, title)
    for note in shown["chart_frame"].get("notes") or []:
        bctx.warn("layout_note", "frame: {}".format(note), [])
    if fresh:
        for warning in part.warnings:
            bctx.warn(warning.get("code") or "chart_capped", str(warning.get("message") or ""), [])
    fields: Dict[str, Any] = {"w": w, "h": h, "engine": "echarts", "chart": {"type": chart.name, "gl": bool(chart.gl)}, "data_key": key,
                              "source": part.source, "model": part.model, "stats": part.stats,
                              "resolved": {k: v for k, v in part.spec.items() if k not in ("rows", "columns")}, "data": None, "stripped": None,
                              "vl": None}
    fields.update(shown)
    if fresh:
        fields["doc_asset"] = bctx.store_asset(_dump(part.doc).encode("utf-8"), "doc")
        compat = part.vegalite or VL.gist_card(shown["gist"], title)
        fields["spec_asset"] = bctx.store_asset(_dump(compat).encode("utf-8"), "json")
    else:
        fields["doc_asset"] = root.get("doc_asset") if root is not None else None
        fields["spec_asset"] = root.get("spec_asset") if root is not None else None
        if not part.vegalite and root is not None and root.get("gist") != shown["gist"]:
            fields["spec_asset"] = bctx.store_asset(_dump(VL.gist_card(shown["gist"], title)).encode("utf-8"), "json")
    return fields


def _build_vega(bctx: Any, spec: Mapping[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    root = bctx.root
    w, h = _sizes(bctx, ESCAPE_BOX)
    fields: Dict[str, Any] = {"w": w, "h": h, "engine": "vega-lite", "chart": {"type": "vega-lite", "gl": False}, "spec_asset": settings["spec_asset"],
                              "data": settings.get("data"), "doc_asset": None, "option": None, "data_key": None, "resolved": None, "stripped": None}
    facts: Optional[Dict[str, Any]] = None
    if spec.get("_vl"):
        loaded = _loaded(bctx)
        if loaded is not None and loaded.get("engine") == "vega-lite" and loaded.get("key") == settings["spec_asset"] + "|" + str(settings.get("data") or ""):
            facts = loaded.get("vl")
        elif settings.get("data") is not None:
            # Made earlier in this batch (``Block.load`` could not see it): read its file now, under the lock.
            found = bctx.fetch({"op": "chart", "spec": json.loads(str(spec["_vl"])), "data": settings["data"]})
            facts = found.get("vl") if isinstance(found, dict) else None
        else:
            facts = vega_facts(spec, None)
        stored = bctx.store_asset(str(spec["_vl"]).encode("utf-8"), "json")
        if stored != settings["spec_asset"]:
            raise bctx.ctx.error("chart_refused", "the stored spec's name {} is not the spec's own ({})".format(stored, settings["spec_asset"]))
    elif root is not None and root.get("spec_asset") == settings["spec_asset"]:
        for key in ("gist", "model", "stats", "source", "chart_frame", "vl", "resolved", "flat"):
            fields[key] = root.get(key)
        return fields
    if facts is None:
        fields.update(gist=["Vega-Lite chart · drawn on the page"], model=None, stats=None, source=None, chart_frame=None, vl=None, flat=None)
        return fields
    fields["gist"] = facts["gist"]
    fields["vl"] = facts.get("brief")
    part = facts.get("part")
    if isinstance(part, CC.DataPart):
        shown = view_fields(dict(part.spec), part, 0.0, 0.0, w, h, str(spec.get("title") or ""))
        fields.update(model=part.model, stats=part.stats, source=part.source, chart_frame=shown["chart_frame"], flat=facts.get("flat"),
                      resolved={k: v for k, v in part.spec.items() if k not in ("rows", "columns")})
    else:
        fields.update(model=None, stats=None, source=None, chart_frame=None, flat=None)
    return fields


def _build_raw(bctx: Any, spec: Mapping[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    root = bctx.root
    w, h = _sizes(bctx, ESCAPE_BOX)
    fields: Dict[str, Any] = {"w": w, "h": h, "engine": "echarts-raw", "chart": {"type": "echarts", "gl": False}, "option": None, "data": None,
                              "spec_asset": None, "model": None, "stats": None, "source": None, "chart_frame": None, "data_key": None, "resolved": None,
                              "vl": None, "doc_asset": settings["echarts_asset"]}
    if spec.get("_echarts"):
        stored = bctx.store_asset(str(spec["_echarts"]).encode("utf-8"), "doc")
        if stored != settings["echarts_asset"]:
            raise bctx.ctx.error("chart_refused", "the stored option's name {} is not the option's own ({})".format(stored, settings["echarts_asset"]))
        option = json.loads(spec["_echarts"])["option"]
        fields["gist"] = raw_gist(option)
        fields["stripped"] = list(spec.get("_stripped") or [])
        fields["colors"] = raw_colors(option)
        if fields["stripped"]:
            bctx.warn("chart_escape", "stripped from the ECharts option: {}".format(", ".join(fields["stripped"][:12])), [])
    elif root is not None and root.get("doc_asset") == settings["echarts_asset"]:
        for key in ("gist", "stripped", "colors"):
            fields[key] = root.get(key)
    else:
        fields.update(gist=["ECharts option · drawn on the page"], stripped=[], colors=[])
    return fields


def raw_gist(option: Mapping[str, Any]) -> List[str]:
    """A raw option in words: its series (type, name, how many values, the numeric extremes with their category)."""
    series = option.get("series") or []
    if isinstance(series, dict):
        series = [series]
    categories: List[Any] = []
    for key in ("xAxis", "yAxis"):
        axis = option.get(key)
        axis = axis[0] if isinstance(axis, list) and axis else axis
        if isinstance(axis, dict) and isinstance(axis.get("data"), list):
            categories = axis["data"]
    types = sorted({str(s.get("type")) for s in series if isinstance(s, dict)})
    lines = ["ECharts {} · {} series".format(", ".join(types) or "option", len(series))]
    for s in series[:5]:
        if not isinstance(s, dict):
            continue
        if s.get("type") == "radar":
            lines += _radar_lines(option, s)
            continue
        if s.get("type") == "gauge":
            lines += _gauge_lines(s)
            continue
        values: List[Tuple[Any, float]] = []
        for index, item in enumerate(s.get("data") or [] if isinstance(s.get("data"), list) else []):
            name = categories[index] if index < len(categories) else None
            value = item.get("value") if isinstance(item, dict) else item
            if isinstance(item, dict) and item.get("name") is not None:
                name = item["name"]
            if isinstance(value, list):
                numbers = [float(v) for v in value if isinstance(v, (int, float)) and not isinstance(v, bool)]
                if numbers and s.get("type") in ("radar", "parallel", "boxplot", "candlestick"):
                    values += [(name, max(numbers)), (name, min(numbers))]  # every number is a value
                elif numbers:
                    values.append((name, numbers[-1]))  # [x, y]: the last is the value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                values.append((name, float(value)))
        head = "{}{} · {} value{}".format(s.get("type"), " " + quote(str(s["name"]), 40) if s.get("name") else "", len(s.get("data") or []),
                                          "" if len(s.get("data") or []) == 1 else "s")
        if values:
            hi = max(values, key=lambda kv: kv[1])
            lo = min(values, key=lambda kv: kv[1])
            head += " · max {}{:g} · min {}{:g}".format(str(hi[0]) + " " if hi[0] is not None else "", hi[1],
                                                       str(lo[0]) + " " if lo[0] is not None else "", lo[1])
        lines.append(head)
    return lines


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _radar_lines(option: Mapping[str, Any], s: Mapping[str, Any]) -> List[str]:
    """A radar series in words (QA phase34 L3): one line per entity with each indicator's value out of its max, and
    which indicator is its highest and lowest share."""
    radar = option.get("radar")
    radar = radar[0] if isinstance(radar, list) and radar else radar
    indicators = [i for i in (radar.get("indicator") if isinstance(radar, dict) else None) or [] if isinstance(i, dict)]
    items = s.get("data") if isinstance(s.get("data"), list) else []
    lines = []
    for n, item in enumerate(items[:5]):
        value = item.get("value") if isinstance(item, dict) else item
        if not isinstance(value, list):
            continue
        name = str(item.get("name")) if isinstance(item, dict) and item.get("name") is not None else "#{}".format(n + 1)
        parts, shares = [], []
        for k, v in enumerate(value[:12]):
            number = _number(v)
            if number is None:
                continue
            ind = indicators[k] if k < len(indicators) else {}
            label = str(ind.get("name") or "axis {}".format(k + 1))
            top = _number(ind.get("max"))
            parts.append("{} {:g}{}".format(label, number, "/{:g}".format(top) if top else ""))
            shares.append((number / top if top else number, label))
        line = "{} {}".format(quote(name, 40), " · ".join(parts))
        if len(shares) > 1:
            # Ties go to the first indicator.
            best = max(range(len(shares)), key=lambda i: (shares[i][0], -i))
            weakest = min(range(len(shares)), key=lambda i: (shares[i][0], i))
            line += " (best {}, weakest {})".format(shares[best][1], shares[weakest][1])
        lines.append(line)
    if len(items) > 5:
        lines.append("… +{} more".format(len(items) - 5))
    return lines or ["radar · no values"]


def _gauge_lines(s: Mapping[str, Any]) -> List[str]:
    """A gauge in words (QA phase34 L3): each needle's value on the dial's range and its share of it."""
    lo = _number(s.get("min"))
    hi = _number(s.get("max"))
    lo, hi = (0.0 if lo is None else lo), (100.0 if hi is None else hi)
    lines = []
    for item in (s.get("data") if isinstance(s.get("data"), list) else [])[:5]:
        value = _number(item.get("value") if isinstance(item, dict) else item)
        if value is None:
            continue
        name = item.get("name") if isinstance(item, dict) else None
        share = (value - lo) / (hi - lo) * 100.0 if hi > lo else 0.0
        lines.append("gauge{} {:g} on {:g}…{:g} ({:.0f}% of the dial)".format(" " + quote(str(name), 40) if name else "", value, lo, hi, share))
    return lines or ["gauge · no value"]


def raw_colors(option: Mapping[str, Any]) -> List[str]:
    """The explicit hex colours a raw option paints with (``chart_contrast`` checks them)."""
    found: List[str] = []

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif isinstance(node, str) and key == "color" and re.match(r"^#[0-9a-fA-F]{6}\Z", node) and node.lower() not in found:
            found.append(node.lower())

    walk(option)
    return found[:40]


# --------------------------------------------------------------------------
# readback


def spec(root: Element, members: List[Element], full: bool) -> Dict[str, Any]:
    """The op that would build the chart: its alias, title and settings (the flat spec, or the escape hatch's asset)."""
    out: Dict[str, Any] = {"op": "chart"}
    if root.get("alias"):
        out["id"] = root["alias"]
    if root.get("text"):
        out["title"] = root["text"]
    out.update(_settings(root))
    return out


def readback(el: Element, full: bool) -> str:
    el = R.upgraded(el)
    settings = _settings(el)
    engine = str(el.get("engine") or "vega-lite")
    kind = (el.get("chart") or {}).get("type") if isinstance(el.get("chart"), dict) else None
    what = kind if engine == "echarts" and kind else ("Vega-Lite" if engine == "vega-lite" else "ECharts option")
    if settings.get("data") or el.get("data"):
        source = "data {}".format(settings.get("data") or el.get("data"))
    elif settings.get("rows") is not None:
        source = "{} inline rows".format(len(settings["rows"]))
    else:
        source = "inline data"
    return "{} chart{}{} {} [{},{} {}x{}] {}".format(el.get("id"), " " + str(el["alias"]) if el.get("alias") else "",
                                                   " " + quote(el.get("text"), 0 if full else 60) if el.get("text") else "", what,
                                                   el.get("x"), el.get("y"), el.get("w"), el.get("h"), source)


def gist(el: Element, full: bool = False) -> List[str]:
    """What the chart shows, in words (5): the stored gist; with ``full`` also the frame notes, the source columns and the
    model's first rows."""
    el = R.upgraded(el)
    lines = [str(line) for line in el.get("gist") or []]
    if not full:
        return lines
    frame = el.get("chart_frame") if isinstance(el.get("chart_frame"), dict) else {}
    if frame.get("notes"):
        lines.append("frame: " + "; ".join(str(n) for n in frame["notes"]))
    source = el.get("source") if isinstance(el.get("source"), dict) else {}
    if source.get("columns"):
        lines.append("columns: " + ", ".join("{} ({})".format(c[0], c[1]) for c in source["columns"] if isinstance(c, list) and len(c) == 2))
    if source.get("sha256"):
        lines.append("source: {} · {} rows · sha256 {}".format(source.get("data") or "inline rows", source.get("rows"), str(source["sha256"])[:12]))
    lines += _model_rows(el)
    if el.get("stripped"):
        lines.append("stripped: " + ", ".join(str(s) for s in el["stripped"][:20]))
    return lines


def _model_rows(el: Mapping[str, Any]) -> List[str]:
    """Up to 12 rows of the model as a small table (categories by series), for ``look --full``."""
    model = el.get("model") if isinstance(el.get("model"), dict) else {}
    if isinstance(model.get("cats"), list) and isinstance(model.get("series"), list) and model["series"] and \
            isinstance(model["series"][0].get("values"), list):
        names = [str(s.get("name")) for s in model["series"]][:6]
        out = ["rows: {} | {}".format("x", " | ".join(names))]
        for index, cat in enumerate(model["cats"][:12]):
            out.append("  {} | {}".format(cat, " | ".join(D.fmt(s["values"][index]) if s["values"][index] is not None else "-"
                                                        for s in model["series"][:6])))
        return out
    for key in ("slices", "stages"):
        if isinstance(model.get(key), list):
            return ["rows: " + " · ".join("{} {}".format(k, D.fmt(v)) for k, v in model[key][:12])]
    return []


def facts(el: Element) -> Dict[str, Any]:
    """``look --json`` ``charts``: the chart's type, engine, source and frame notes."""
    el = R.upgraded(el)
    frame = el.get("chart_frame") if isinstance(el.get("chart_frame"), dict) else {}
    source = el.get("source") if isinstance(el.get("source"), dict) else {}
    return {"type": (el.get("chart") or {}).get("type") if isinstance(el.get("chart"), dict) else None, "engine": el.get("engine") or "vega-lite",
            "source": {k: source.get(k) for k in ("data", "rows", "sha256", "columns") if k in source} or None,
            "frame_notes": list(frame.get("notes") or []), "legible": frame.get("legible", True)}


# --------------------------------------------------------------------------
# drawing

_DRAWINGS: "OrderedDict[Tuple[Any, ...], Optional[List[Dict[str, Any]]]]" = OrderedDict()
_DRAWING_CACHE = 128


def drawing(el: Mapping[str, Any], box: Tuple[float, float, float, float]) -> Optional[List[Dict[str, Any]]]:
    """Python's own drawing of the chart in ``box`` (the slot's fallback), or None when there is none (a raw option, a
    Vega-Lite spec that is not a plain single view). Cached by what it draws and where."""
    # By what the drawing reads (never by id and version alone: two canvases, or two tests, share this process).
    content = hashlib.sha256(_dump([el.get(k) for k in ("engine", "settings", "flat", "resolved", "model", "chart_frame")]).encode("utf-8")).hexdigest()
    key = (content, tuple(round(v, 2) for v in box))
    if key in _DRAWINGS:
        _DRAWINGS.move_to_end(key)
        return _DRAWINGS[key]
    found = _draw(el, box)
    _DRAWINGS[key] = found
    if len(_DRAWINGS) > _DRAWING_CACHE:
        _DRAWINGS.popitem(last=False)
    return found


def _draw(el: Mapping[str, Any], box: Tuple[float, float, float, float]) -> Optional[List[Dict[str, Any]]]:
    model = el.get("model")
    resolved = el.get("resolved")
    if not isinstance(model, dict) or not isinstance(resolved, dict):
        return None
    chart = CC.get(resolved.get("type"))
    if chart is None or chart.draw is None:
        return None
    settings = dict(el.get("flat") or {}) if el.get("engine") == "vega-lite" else _settings(el)
    work = dict(resolved)
    for key in CC.VISUAL:
        if key in settings:
            work[key] = settings[key]
        else:
            work.pop(key, None)
    try:
        frame = Frame.from_json(el["chart_frame"]) if isinstance(el.get("chart_frame"), dict) else None
        if frame is None or abs(frame.box[0] - box[2]) > 1 or abs(frame.box[1] - box[3]) > 1:
            frame = chart.frame(work, model, (box[2], box[3]))
        return chart.draw(work, model, frame, box)
    except (TypeError, ValueError, KeyError, IndexError, AttributeError, ZeroDivisionError):
        return None


def _subtitle(el: Mapping[str, Any]) -> str:
    engine = el.get("engine") or "vega-lite"
    if engine == "echarts-raw":
        return "ECharts option · drawn on the page"
    if engine == "vega-lite":
        return "Vega-Lite chart{} · drawn on the page".format(" of {}".format(el.get("data")) if el.get("data") else "")
    return "chart · drawn on the page"


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The card, the fitted title, the caption, and one ``chart`` slot holding the page's picture of the plot, with
    Python's drawing (or the gist card) as its fallback (D16)."""
    el = R.upgraded(el)
    x0, y0, x1, y1 = D.box_of(el)
    w = x1 - x0
    items: List[Dict[str, Any]] = [{"k": "rect", "x": x0, "y": y0, "w": w, "h": y1 - y0, "r": D.CARD_RADIUS, "fill": "chart.paper",
                                    "stroke": "base.line", "sw": 1, "elev": 1}]
    title = str(el.get("text") or "")
    room = max(0.0, w - 2 * PAD)
    if title.strip():
        from herdr_team import canvas_geometry

        lh = canvas_text.line_height(TITLE_SIZE)
        line = D.text_prim([canvas_geometry.fit_line(title, room, TITLE_SIZE, TITLE_WEIGHT)], x0 + PAD, y0 + PAD, TITLE_SIZE, {}, "chart.ink", "start",
                           (x0 + PAD, y0 + PAD, room, lh), TITLE_WEIGHT)
        line["lod"] = list(D.LOD_LABEL)
        items.append(line)
    caption = str(_settings(el).get("caption") or "")
    if caption:
        from herdr_team import canvas_geometry

        lh = canvas_text.line_height(CAPTION_SIZE)
        top = y1 - PAD - CAPTION_H + (CAPTION_H - lh) / 2.0
        prim = D.text_prim([canvas_geometry.fit_line(caption, room, CAPTION_SIZE, 400)], x0 + PAD, top, CAPTION_SIZE, {}, "chart.muted", "start",
                           (x0 + PAD, top, room, lh), 400)
        prim["lod"] = list(D.LOD_BODY)
        items.append(prim)
    box = _el_slot(el)
    found = drawing(el, box)
    gl = bool((el.get("chart") or {}).get("gl")) if isinstance(el.get("chart"), dict) else False
    doc = el.get("doc_asset") if el.get("engine") in ("echarts", "echarts-raw") and isinstance(el.get("doc_asset"), str) else None
    items += _slot.slot_emit(_subtitle, fallback=lambda _el, _env: found, doc=lambda _el: doc, gl=lambda _el: gl, box=lambda _el: box)(el, env)
    return items


def hit(el: Element) -> Dict[str, Any]:
    return {"shape": "rect", "box": D.xywh(D.box_of(el))}


def text_edit(el: Element) -> Optional[Dict[str, Any]]:
    """The title, in its band."""
    x0, y0, x1, _y1 = D.box_of(el)
    lh = canvas_text.line_height(TITLE_SIZE)
    return TX.edit("text", str(el.get("text") or ""), [x0 + PAD, y0 + PAD, max(1.0, (x1 - x0) - 2 * PAD), lh], TITLE_SIZE, TITLE_WEIGHT,
                   "chart.ink", wrap="line")


def resize(el: Element, w: Optional[float], h: Optional[float], ctx: Any) -> Dict[str, Any]:
    """A new size (the chart type's legible minimum at least, D20), re-framed from the stored model with no file read."""
    el = R.upgraded(el)
    new_w = max(MIN_SIZE[0], min(MAX_SIDE, float(w))) if w is not None else float(el.get("w") or ESCAPE_BOX[0])
    new_h = max(MIN_SIZE[1], min(MAX_SIDE, float(h))) if h is not None else float(el.get("h") or ESCAPE_BOX[1])
    asked = (float(w) if w is not None else new_w, float(h) if h is not None else new_h)
    out: Dict[str, Any] = {}
    part = _part_of(el)
    title = str(el.get("text") or "")
    if part is not None and el.get("engine") == "echarts":
        settings = _settings(el)
        new_w, new_h, _grew = _fit_size(settings, part, new_w, new_h, title, str(settings.get("caption") or ""))
        out.update(view_fields(settings, part, float(el.get("x") or 0), float(el.get("y") or 0), new_w, new_h, title))
    if (asked[0] < new_w - 0.5 or asked[1] < new_h - 0.5) and hasattr(ctx, "warn"):
        # Kept at its legible minimum: said, as a create that grew says it (QA phase34 L6).
        kind = (el.get("chart") or {}).get("type") if isinstance(el.get("chart"), dict) else None
        ctx.warn("chart_grew", "the {}chart stays {}x{} (asked {}x{}) so its labels fit".format(
            "{} ".format(kind) if kind else "", int(round(new_w)), int(round(new_h)), int(round(asked[0])), int(round(asked[1]))),
            [str(el.get("id"))])
    if part is not None and el.get("engine") == "vega-lite":
        out["chart_frame"] = view_fields(dict(el.get("flat") or {}), part, 0.0, 0.0, new_w, new_h, title)["chart_frame"]
    out["w"], out["h"] = round(new_w), round(new_h)
    return out


def upgrade(el: Element, stored: int) -> Element:
    """A v1 chart (a Vega-Lite spec with ``spec_asset`` and an optional ``data`` file) in the v2 shape: the
    ``vega-lite`` engine with those as its settings. Only what is read changes; the stored events never do."""
    if stored >= 2:
        return el
    out = dict(el)
    settings: Dict[str, Any] = {}
    if isinstance(el.get("spec_asset"), str):
        settings["spec_asset"] = el["spec_asset"]
    if isinstance(el.get("data"), str):
        settings["data"] = el["data"]
    out.update(settings=settings, engine="vega-lite", chart={"type": "vega-lite", "gl": False})
    out.setdefault("gist", ["Vega-Lite chart{} · drawn on the page".format(" of {}".format(el["data"]) if el.get("data") else "")])
    return out


# --------------------------------------------------------------------------
# checks (4.1)


def _ref(el: Mapping[str, Any]) -> Any:
    return el.get("alias") or el.get("id")


def _problem(el: Mapping[str, Any], code: str, message: str, fix: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {"code": code, "ids": [str(el.get("id"))], "message": message, "fix": fix, "severity": SEVERITY[code]}


def _patch(el: Mapping[str, Any], body: Mapping[str, Any]) -> Dict[str, Any]:
    fix: Dict[str, Any] = {"op": "patch", "id": _ref(el)}
    fix.update(body)
    return fix


def labels_check(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``chart_labels``: the frame could not make the labels readable (font under 11 px, more than half the category labels
    hidden, labels cut below 48 px, a plot too small)."""
    el = R.upgraded(el)
    frame = el.get("chart_frame") if isinstance(el.get("chart_frame"), dict) else None
    if not frame or frame.get("legible", True) or el.get("engine") != "echarts":
        return []
    model = el.get("model") if isinstance(el.get("model"), dict) else {}
    settings = _settings(el)
    notes = "; ".join(str(n) for n in frame.get("notes") or []) or "labels do not fit"
    cats = len(model.get("cats") or [])
    fix: Optional[Dict[str, Any]]
    part = _part_of(el)
    grown = None
    if part is not None:
        chart = CC.get(part.spec.get("type"))
        if chart is not None:
            for factor in (1.25, 1.5, 2.0, 2.5, 3.0):
                w, h = float(el.get("w") or 0) * factor, float(el.get("h") or 0) * factor
                if w > MAX_SIDE or h > MAX_SIDE:
                    break
                try:
                    shown = view_fields(settings, part, 0.0, 0.0, w, h, str(el.get("text") or ""))
                except (TypeError, ValueError, KeyError, IndexError, ZeroDivisionError, HerdrTeamError):
                    break
                if shown["chart_frame"].get("legible", True):
                    grown = (int(math.ceil(w / 10.0) * 10), int(math.ceil(h / 10.0) * 10))
                    break
    if cats > 30:
        fix = _patch(el, {"set": {"top": 20}})
    elif grown is not None:
        fix = {"op": "move", "id": _ref(el), "w": grown[0], "h": grown[1]}
    elif settings.get("type") == "bar" and not settings.get("horizontal"):
        fix = _patch(el, {"set": {"horizontal": True}})
    else:
        fix = None
    return [_problem(el, "chart_labels", "{}'s labels are not readable ({}); {}".format(
        _ref(el), notes, "keep fewer categories" if cats > 30 else "make it bigger" if grown else "lay the bars on their side"), fix)]


def crowded_check(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``chart_crowded``: more than 10 series, or a legend of more than 8 rows."""
    el = R.upgraded(el)
    model = el.get("model") if isinstance(el.get("model"), dict) else {}
    frame = el.get("chart_frame") if isinstance(el.get("chart_frame"), dict) else {}
    series = len(model.get("series") or [])
    rows = len(((frame.get("legend") or {}).get("rows")) or []) if isinstance(frame.get("legend"), dict) else 0
    if el.get("engine") != "echarts" or (series <= CROWDED_SERIES and rows <= CROWDED_LEGEND_ROWS):
        return []
    return [_problem(el, "chart_crowded", "{} draws {} series{}; nobody tells that many colours apart: keep the top 8".format(
        _ref(el), series, " in a legend of {} rows".format(rows) if rows > CROWDED_LEGEND_ROWS else ""), _patch(el, {"set": {"top": 8}}))]


def type_checks(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The chart type's own checks (``ChartType.checks``: a pie's slices ...), with the element's ids and fix ops."""
    el = R.upgraded(el)
    part = _part_of(el)
    if part is None or el.get("engine") != "echarts":
        return []
    chart = CC.get(part.spec.get("type"))
    if chart is None or not chart.checks:
        return []
    settings = _settings(el)
    work = dict(part.spec)
    for key in CC.VISUAL:
        if key in settings:
            work[key] = settings[key]
        else:
            work.pop(key, None)
    try:
        frame = Frame.from_json(el["chart_frame"]) if isinstance(el.get("chart_frame"), dict) else chart.frame(work, part.model, (400.0, 300.0))
    except (TypeError, ValueError, KeyError, IndexError):
        return []
    out = []
    for check in chart.checks:
        try:
            found = check(work, part.model, frame) or []
        except (TypeError, ValueError, KeyError, IndexError, ZeroDivisionError):
            continue
        for problem in found:
            code = str(problem.get("code") or "chart_note")
            fix = problem.get("fix")
            if isinstance(fix, dict) and "op" not in fix:
                fix = _patch(el, fix)
            SEVERITY.setdefault(code, int(problem.get("severity", 2)))
            out.append({"code": code, "ids": [str(el.get("id"))], "message": "{}: {}".format(_ref(el), problem.get("message") or ""), "fix": fix,
                        "severity": SEVERITY[code]})
    return out


def contrast_check(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``chart_contrast``: an explicit colour (a raw option's hex, the highlight paint) under 3:1 on the chart's paper in
    either theme."""
    from herdr_team import canvas_theme

    el = R.upgraded(el)
    settings = _settings(el)
    colors: List[str] = []
    if el.get("engine") == "echarts-raw":
        colors = [c for c in el.get("colors") or [] if isinstance(c, str)]
    elif settings.get("highlight"):
        colors = ["chart.highlight"]
    bad = []
    for theme in ("light", "dark"):
        palette = canvas_theme.palette(theme)
        paper = palette.get("chart.paper", "#ffffff")
        for color in colors:
            value = palette.get(color, color)
            if isinstance(value, str) and value.startswith("#") and canvas_theme.contrast(value, paper) < 3.0 and color not in bad:
                bad.append(color)
    if not bad:
        return []
    fix = _patch(el, {"set": {"highlight": None}}) if el.get("engine") == "echarts" else None
    return [_problem(el, "chart_contrast", "{} paints with {} under 3:1 on the chart's paper; pick a stronger colour".format(
        _ref(el), ", ".join(bad[:5])), fix)]


def capped_check(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``chart_capped`` (a note): the data was capped or sampled to draw."""
    el = R.upgraded(el)
    model = el.get("model") if isinstance(el.get("model"), dict) else {}
    capped = [str(c) for c in model.get("capped") or []]
    if not capped or el.get("engine") != "echarts":
        return []
    return [_problem(el, "chart_capped", "{} draws less than its data: {}".format(_ref(el), "; ".join(capped[:3])), None)]


def escape_check(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``chart_escape`` (a note): keys stripped from a raw ECharts option."""
    stripped = [str(s) for s in el.get("stripped") or []]
    if not stripped:
        return []
    return [_problem(el, "chart_escape", "{}'s ECharts option lost {} key{} the page never draws: {}".format(
        _ref(el), len(stripped), "" if len(stripped) == 1 else "s", ", ".join(stripped[:8])), None)]


# --------------------------------------------------------------------------
# registration


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``chart`` op (2.1): the block pipeline builds it, or updates the author's chart the op's ``id`` names."""
    ctx.block("chart", op)


MCP = ("chart {type bar|line|area|scatter|pie|donut|heatmap|histogram|box|funnel|treemap|sankey|…, data: a file under artifacts/ | rows, "
       "x, y, color, category, value, …; or spec (Vega-Lite) or echarts (an ECharts option)}")


def _mcp() -> str:
    shown = "|".join(CC.names())
    return ("chart {{type {}, data: a file under artifacts/ | rows, x, y, color, category, value, …; or spec (Vega-Lite) or echarts "
            "(an ECharts option)}}".format(shown))


def _ops() -> Tuple[OpSpec, ...]:
    return (OpSpec(name="chart", family="data", fields=op_fields() + ("w", "h", "id", "client_id"), create=create, place=True, order=90,
                   doc="a chart: a type over data from artifacts/ or inline rows (Python validates and draws it), or Vega-Lite, or ECharts",
                   mcp=_mcp()),)


def _kinds() -> Tuple[Kind, ...]:
    return (Kind(name="chart", version=KIND_VERSION, role="composite", ops=("chart",), page=True, solid=True, cell=True, slot="chart",
                 connectable=True, handles="box", edit_limit="label", noun=("chart", "charts"),
                 block=Block(settings=settings_fields(), parts="inline", positional=False, normalize=normalize, build=build, spec=spec,
                             load=load, fields=("title",)),
                 emit=emit, hit=hit, text_edit=text_edit, resize=resize, readback=readback, upgrade=upgrade,
                 checks=(labels_check, crowded_check, type_checks, contrast_check, capped_check, escape_check),
                 still_views=("",), gist=gist, gist_first=True, look_json=("charts", facts),
                 doc="a chart the page renders with ECharts (or Vega-Lite); Python validates, draws and reads it back"),)


OPS = _ops()
KINDS = _kinds()


def _refresh() -> None:
    """The chart types changed (a test's extra type module): the op takes their fields, so register it again."""
    global OPS, KINDS
    OPS = _ops()
    KINDS = _kinds()
    R.refresh(__name__)


CC.on_change(_refresh)
