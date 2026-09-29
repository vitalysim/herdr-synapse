"""The chart-type registry (canvas v2 phase 3, 2.3): every chart type is one module of this package.

A chart is compiled in Python (D1): the flat spec an agent writes is
validated against its data (``_data``), shaped into a compact ``model`` (what
Python draws, gists and checks), a ``doc`` (the datasets the page reads), a
``Frame`` (where the plot, axes and legend go: ``_frame``), a theme-neutral
ECharts ``option`` (``_option``), a Python drawing (``_draw``) and a ``gist`` in
words (``_gist``). Each chart type contributes one ``ChartType`` of hooks; the
helpers are shared.

Discovery follows ``canvas_kinds``: every public module of this package that
exports ``CHARTS`` is registered, in the order its ``ORDER`` says; modules named
``_*`` are helpers. ``_load_extra`` and ``_unload`` are the test hooks of the
one-module proof (a ``lollipop`` type dropped in with no other file changed),
and ``on_change`` lets the chart kind refresh the op fields it takes.

The hooks (``spec`` is the flat spec with its type's defaults, ``table`` the
filtered ``_data.Table``):

* ``normalize(spec, table) -> spec``: defaults that depend on the data (sort,
  aggregate) and the type's own checks;
* ``model(spec, table) -> model``: aggregated and capped, JSON of 16 KB or less;
  a ``"_full"`` key may carry what ``doc`` needs, and is never stored;
* ``stats(spec, table, model) -> stats``: exact numbers over the whole table for
  the gist (JSON, stored);
* ``doc(spec, table, model) -> doc``: the page's datasets and ``refs`` (2.7);
* ``frame(spec, model, box) -> Frame`` and ``option(spec, model, frame, gist)``;
* ``draw(spec, model, frame, (x, y, w, h)) -> [primitive]``;
* ``gist(spec, model, stats) -> [line]``; ``vegalite(spec, model)`` (v1, D18);
* ``checks``: each ``(spec, model, frame) -> [problem]``;
* ``min_box(spec, model) -> (w, h)``: the slot size its labels need.

Pure: no I/O and no import of ``canvas``.
"""
from __future__ import annotations

import copy
import importlib
import json
import math
import pkgutil
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team.canvas_charts import _data
from herdr_team.canvas_charts import _format as F
from herdr_team.canvas_charts._data import Table, invalid, refuse
from herdr_team.canvas_charts._frame import Frame

DEFAULT_ORDER = 1000
ALL_TYPES = _data.TYPES
#: Every op field a flat chart takes whatever its type (the type's channels and options join them).
COMMON = ("type", "data", "rows", "columns", "aggregate", "filter", "sort", "top", "other", "labels", "legend", "format", "units", "types",
          "highlight", "annotations", "caption")
#: ``kind`` is read as ``type``: every other op in the language names its variant ``kind``, and a chart is the one that
#: names it ``type``, which cost a live agent its first try (QA phase 6, F8). The accepted name is still ``type``, and
#: a chart drawn with ``kind`` says so in a ``field_alias`` warning.
FIELD_ALIASES = {"kind": "type"}
#: Fields that change only how a chart looks, never its model: a patch of them needs no data (the stored model is re-framed).
VISUAL = ("title", "caption", "labels", "legend", "highlight", "annotations", "horizontal", "smooth", "points", "palette", "orient", "inner",
          "shading", "wireframe")
LABELS = ("auto", "values", "none")
LEGENDS = ("auto", "right", "bottom", "none")
SORTS = ("x", "-x", "y", "-y", "none")
MAX_MODEL_BYTES = 16 * 1024
MAX_OPTION_BYTES = 16 * 1024
MAX_TOP = 500
MAX_HIGHLIGHT = 20
MAX_ANNOTATIONS = 10


@dataclass(frozen=True)
class Channel:
    """One encoding channel of a chart type (``x``, ``y``, ``color``, ``size``, ``category``, ``value``, ``source``,
    ``target``, ``path``, ``z``)."""

    name: str
    required: bool = False
    types: Tuple[str, ...] = ALL_TYPES
    #: A list of fields (a treemap's ``path``).
    many: bool = False
    #: The channel ``aggregate`` applies to (it may then be left out for ``aggregate: "count"``).
    aggregate: bool = False
    doc: str = ""


def _todo(*_args: Any) -> Any:
    raise NotImplementedError("a chart type needs this hook")


def _no_stats(spec: Dict[str, Any], table: Table, model: Dict[str, Any]) -> Dict[str, Any]:
    return {}


def _no_min(spec: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, float]:
    return 240.0, 160.0


def _keep(spec: Dict[str, Any], table: Table) -> Dict[str, Any]:
    return spec


@dataclass(frozen=True)
class ChartType:
    """One chart type: its channels, options and hooks (see the module doc)."""

    name: str
    aliases: Tuple[str, ...] = ()
    channels: Tuple[Channel, ...] = ()
    #: Type-specific op fields (``bins``, ``inner``, ``smooth``); a field no chosen type takes is refused.
    options: Tuple[str, ...] = ()
    #: ``option -> (value, field) -> checked value``: how each of ``options`` is checked (the shared rules cover the
    #: built-in ones; a new type brings its own).
    option_rules: Mapping[str, Callable[[Any, str], Any]] = field(default_factory=dict)
    #: Which ``_frame`` helper fits it: ``cartesian``, ``radial``, ``none`` or ``gl``.
    frame_kind: str = "cartesian"
    normalize: Callable[[Dict[str, Any], Table], Dict[str, Any]] = _keep
    model: Callable[[Dict[str, Any], Table], Dict[str, Any]] = _todo
    stats: Callable[[Dict[str, Any], Table, Dict[str, Any]], Dict[str, Any]] = _no_stats
    doc: Callable[[Dict[str, Any], Table, Dict[str, Any]], Dict[str, Any]] = _todo
    frame: Callable[[Dict[str, Any], Dict[str, Any], Tuple[float, float]], Frame] = _todo
    option: Callable[[Dict[str, Any], Dict[str, Any], Frame, Sequence[str]], Dict[str, Any]] = _todo
    draw: Optional[Callable[[Dict[str, Any], Dict[str, Any], Frame, Tuple[float, float, float, float]], List[Dict[str, Any]]]] = None
    gist: Callable[[Dict[str, Any], Dict[str, Any], Mapping[str, Any]], List[str]] = _todo
    vegalite: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Optional[Dict[str, Any]]]] = None
    checks: Tuple[Callable[[Dict[str, Any], Dict[str, Any], Frame], List[Dict[str, Any]]], ...] = ()
    #: ECharts modules it needs (``BarChart``, ``GridComponent``); echarts-gl names when ``gl``.
    echarts: Tuple[str, ...] = ()
    gl: bool = False
    min_box: Callable[[Dict[str, Any], Dict[str, Any]], Tuple[float, float]] = _no_min
    #: The element's size when the op gives none (it still grows to ``min_box``).
    default_box: Tuple[float, float] = (560, 360)
    doc_line: str = ""
    #: What it draws at most, in words (``60 categories × 12 series``), for the catalog.
    caps: str = ""
    #: One JSON op (``catalog``, the guides' test, conformance).
    example: str = ""
    order: int = 100

    def channel(self, name: str) -> Optional[Channel]:
        return next((c for c in self.channels if c.name == name), None)


@dataclass
class Compiled:
    """What a flat chart compiles to (2.3): the resolved spec, model, doc, frame, option, gist, v1 spec, stats and
    warnings (``chart_capped``, ``channel_alias`` ...)."""

    spec: Dict[str, Any]
    model: Dict[str, Any]
    doc: Dict[str, Any]
    frame: Frame
    option: Dict[str, Any]
    gist: List[str]
    vegalite: Dict[str, Any]
    stats: Dict[str, Any]
    warnings: List[Dict[str, Any]]
    source: Dict[str, Any]


# --------------------------------------------------------------------------
# the registry

_REGISTRY: Dict[str, ChartType] = {}
_ALIASES: Dict[str, str] = {}
_OWNER: Dict[str, str] = {}
_LOADED = False
_CHANGED: List[Callable[[], None]] = []


def register(chart: ChartType) -> ChartType:
    """Add a chart type; a second one with the same name or alias, a frame kind or channel type this module does not know,
    or an option without a rule is a programming error."""
    if chart.frame_kind not in ("cartesian", "radial", "none", "gl"):
        raise ValueError("chart {}: frame_kind {!r}".format(chart.name, chart.frame_kind))
    for channel in chart.channels:
        if any(t not in ALL_TYPES for t in channel.types):
            raise ValueError("chart {}: channel {} types {}".format(chart.name, channel.name, channel.types))
    for option in chart.options:
        if option not in chart.option_rules and option not in RULES:
            raise ValueError("chart {}: option {} has no rule".format(chart.name, option))
    names = (chart.name,) + tuple(chart.aliases)
    for name in names:
        owner = _ALIASES.get(name)
        if owner is not None and owner != chart.name:
            raise ValueError("chart {}: {} is already {}'s".format(chart.name, name, owner))
    if chart.name in _REGISTRY and _REGISTRY[chart.name] is not chart:
        raise ValueError("chart {} is registered twice".format(chart.name))
    _REGISTRY[chart.name] = chart
    for name in names:
        _ALIASES[name] = chart.name
    return chart


def _discover() -> List[Tuple[int, str, Any]]:
    found = []
    for info in pkgutil.iter_modules(__path__):  # type: ignore[name-defined]
        if info.name.startswith("_"):
            continue
        module = importlib.import_module("{}.{}".format(__name__, info.name))
        if not hasattr(module, "CHARTS"):
            continue
        order = getattr(module, "ORDER", DEFAULT_ORDER)
        found.append((int(order), info.name, module))
    return sorted(found, key=lambda item: (item[0], item[1]))


def load() -> None:
    """Import every chart module of this package once; each registers its ``CHARTS``."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        for _order, short, module in _discover():
            for chart in module.CHARTS:
                register(chart)
                _OWNER[chart.name] = "{}.{}".format(__name__, short)
    except BaseException:
        _LOADED = False
        raise


def on_change(hook: Callable[[], None]) -> None:
    """Call ``hook`` whenever a test hook changes the registry (the chart kind's op fields follow it)."""
    if hook not in _CHANGED:
        _CHANGED.append(hook)


def _load_extra(module_name: str) -> None:
    """Test hook: register one more module's ``CHARTS`` (the one-module proof)."""
    load()
    module = importlib.import_module(module_name)
    for chart in module.CHARTS:
        register(chart)
        _OWNER[chart.name] = module_name
    for hook in _CHANGED:
        hook()


def _unload(module_name: str) -> None:
    for name, owner in list(_OWNER.items()):
        if owner != module_name:
            continue
        chart = _REGISTRY.pop(name, None)
        del _OWNER[name]
        for alias in ((chart.name,) + tuple(chart.aliases)) if chart else ():
            _ALIASES.pop(alias, None)
    sys.modules.pop(module_name, None)
    for hook in _CHANGED:
        hook()


def get(name: Any) -> Optional[ChartType]:
    """The chart type named ``name`` or one of its aliases, or None."""
    load()
    if not isinstance(name, str):
        return None
    found = _ALIASES.get(name)
    return _REGISTRY.get(found) if found else None


def types() -> List[ChartType]:
    load()
    return sorted(_REGISTRY.values(), key=lambda c: (c.order, c.name))


def names(gl: Optional[bool] = None) -> List[str]:
    return [c.name for c in types() if gl is None or c.gl == gl]


def channel_names() -> List[str]:
    """Every channel any type takes, in first-seen order."""
    out: List[str] = []
    for chart in types():
        for channel in chart.channels:
            if channel.name not in out:
                out.append(channel.name)
    return out


def option_names() -> List[str]:
    out: List[str] = []
    for chart in types():
        for option in chart.options:
            if option not in out:
                out.append(option)
    return out


def fields() -> List[str]:
    """Every flat-chart op field: the common ones, every channel and every option."""
    out = list(COMMON)
    for name in channel_names() + option_names():
        if name not in out:
            out.append(name)
    return out


def echarts_modules(gl: bool = False) -> List[str]:
    """The ECharts (or echarts-gl) modules every registered type needs, in first-seen order."""
    out: List[str] = []
    for chart in types():
        if chart.gl != gl:
            continue
        for name in chart.echarts:
            if name not in out:
                out.append(name)
    return out


# --------------------------------------------------------------------------
# option rules (the shared ones; a type may bring its own)


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise invalid(name, "{} is true or false".format(name))
    return value


def _between(low: float, high: float, integer: bool = False) -> Callable[[Any, str], Any]:
    def rule(value: Any, name: str) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or (integer and float(value) != int(value)):
            raise invalid(name, "{} is {} from {:g} to {:g}".format(name, "a whole number" if integer else "a number", low, high))
        return int(value) if integer else float(value)
    return rule


def _one_of(*choices: Any) -> Callable[[Any, str], Any]:
    def rule(value: Any, name: str) -> Any:
        if value not in choices:
            raise invalid(name, "{} is one of: {}{}".format(name, ", ".join(json.dumps(c) for c in choices),
                                                           _data.did_you_mean(str(value), [str(c) for c in choices])))
        return value
    return rule


RULES: Dict[str, Callable[[Any, str], Any]] = {
    "stack": _one_of(False, True, "percent"),
    "horizontal": _bool,
    "smooth": _bool,
    "points": _bool,
    "bins": _between(2, 60, True),
    "inner": _between(0.3, 0.8),
    "trend": _one_of("linear", "none"),
    "whiskers": _one_of("1.5iqr", "minmax"),
    "depth": _between(1, 3, True),
    "orient": _one_of("horizontal", "vertical"),
    "palette": _one_of("categorical", "sequential", "diverging"),
    "shading": _one_of("lambert", "color", "realistic"),
    "wireframe": _bool,
}


# --------------------------------------------------------------------------
# the flat spec (canvas-free normalization; the kind adds the title)

#: Pie takes ``x`` and ``y`` as ``category`` and ``value`` (with a ``channel_alias`` warning).
CHANNEL_ALIASES = {"pie": {"x": "category", "y": "value"}, "donut": {"x": "category", "y": "value"}, "funnel": {"x": "category", "y": "value"}}


#: Words that name a *shape* of a chart rather than a type, and the fields that draw it (QA phase 6, F8). A grouped bar
#: chart is the most ordinary business request there is, and ``invalid type "grouped"`` used to list the types without
#: saying how to get one.
NOT_TYPES = {
    "grouped": 'type "bar" with color: <the column that splits the bars>; side by side is the default',
    "grouped_bar": 'type "bar" with color: <the column that splits the bars>; side by side is the default',
    "stacked": 'type "bar" with color: <column> and stack: true (or stack: "percent")',
    "stacked_bar": 'type "bar" with color: <column> and stack: true (or stack: "percent")',
    "multi": 'type "line" (or "bar") with color: <the column that splits the series>',
    "multi_line": 'type "line" with color: <the column that splits the series>',
    "horizontal": 'type "bar" with horizontal: true',
    "horizontal_bar": 'type "bar" with horizontal: true',
    "combo": "two charts side by side; one element draws one type",
    "table": 'not a chart: the "table" op draws columns and rows',
    "gauge": 'not a chart type; a single number is a "badge" or a "card"',
}


def _not_a_type(word: str) -> str:
    """The hint for a word that describes a chart's shape, normalised over spaces, dashes and a trailing noun."""
    key = " ".join(word.strip().lower().split()).replace("-", " ").replace(" ", "_")
    for suffix in ("_chart", "_bars", "_bar", "_charts"):
        if key.endswith(suffix) and key[: -len(suffix)] in NOT_TYPES:
            key = key[: -len(suffix)]
            break
    found = NOT_TYPES.get(key)
    return "; {} is {}".format(word, found) if found else ""


def resolve_type(value: Any) -> Tuple[ChartType, Dict[str, Any]]:
    """The chart type an op names (an alias is normalised: ``column`` is ``bar``, ``bubble`` is ``scatter`` with ``size``
    required), and the fields the alias implies; ``invalid type`` with the list and the nearest name otherwise."""
    load()
    known = names()
    if not isinstance(value, str) or get(value) is None:
        word = str(value) if value is not None else ""
        raise invalid("type", "invalid type {}; one of: {}{}{}".format(json.dumps(value, ensure_ascii=False), ", ".join(known),
                                                                       _data.did_you_mean(word, known + sorted(_ALIASES)),
                                                                       _not_a_type(word)),
                      types=known, nearest=_data.nearest(word, known + sorted(_ALIASES)))
    chart = get(value)
    assert chart is not None
    implied: Dict[str, Any] = {}
    if value == "bubble":
        implied["_size_required"] = True
    return chart, implied


def _field_name(value: Any, name: str, many: bool) -> Any:
    if many:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not value or len(value) > 3 or not all(isinstance(v, str) and v for v in value):
            raise invalid(name, "{} is a column name or a list of up to 3".format(name))
        return list(value)
    if not isinstance(value, str) or not value or len(value) > 200:
        raise invalid(name, "{} is a column name (text)".format(name))
    return value


def _mapping(value: Any, name: str, rule: Callable[[Any, str], Any]) -> Dict[str, Any]:
    if not isinstance(value, dict) or len(value) > 16:
        raise invalid(name, "{} maps a channel to its value".format(name))
    return {str(k): rule(v, "{}.{}".format(name, k)) for k, v in value.items()}


def _fmt_rule(value: Any, name: str) -> str:
    if value not in F.FORMATS and value not in F.DATE_FORMATS:
        raise invalid(name, "{} is one of: {}".format(name, ", ".join(F.FORMATS + ("quarter",))))
    return str(value)


def _unit_rule(value: Any, name: str) -> str:
    if not F.valid_unit(value):
        raise invalid(name, "{} is a short unit: $, £, €, ¥ (a prefix) or up to 8 characters (ms, %, kg) after the number".format(name))
    return str(value)


def _type_rule(value: Any, name: str) -> str:
    if value not in ALL_TYPES:
        raise invalid(name, "{} is one of: {}".format(name, ", ".join(ALL_TYPES)))
    return str(value)


def normalize_spec(raw: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """A flat chart spec checked for shape (every field, no data needed) and canonical (aliases resolved), with the
    warnings it earned (``channel_alias``). Idempotent: its readback normalizes to itself."""
    aliased = [key for key, target in FIELD_ALIASES.items() if raw.get(key) is not None and raw.get(target) is None]
    chart, implied = resolve_type(raw.get("type") if raw.get("type") is not None else
                                  next((raw[key] for key in aliased if FIELD_ALIASES[key] == "type"), None))
    spec: Dict[str, Any] = {"type": chart.name}
    warnings: List[Dict[str, Any]] = [{"code": "field_alias", "message": "a chart's type is type, not {}; read {} as type".format(key, key)}
                                      for key in aliased]
    renames = CHANNEL_ALIASES.get(chart.name, {})
    taken = {c.name: c for c in chart.channels}
    all_channels = channel_names()
    all_options = option_names()
    for key, value in raw.items():
        if value is None or key in ("type", "op", "id", "title", "intent", "if_version", "client_id", "w", "h", "spec", "echarts",
                                    "spec_asset") or key in FIELD_ALIASES or key.startswith("_"):
            continue
        target = key
        if key in renames and renames[key] not in raw:
            target = renames[key]
            warnings.append({"code": "channel_alias", "message": "{} takes {} as {}; wrote {}".format(chart.name, key, target, target)})
        if target in taken:
            spec[target] = _field_name(value, target, taken[target].many)
            continue
        if target in all_channels:
            takers = [c.name for c in types() if c.channel(target) is not None]
            raise invalid(target, "{} does not take {}; its channels: {} ({} is taken by {})".format(
                chart.name, target, ", ".join(c.name for c in chart.channels), target, ", ".join(takers)))
        if target in chart.options:
            rule = chart.option_rules.get(target) or RULES[target]
            spec[target] = rule(value, target)
            continue
        if target in all_options:
            takers = [c.name for c in types() if target in c.options]
            raise invalid(target, "{} is an option of {}, not of {}".format(target, ", ".join(takers), chart.name))
        if target not in COMMON:
            raise invalid(target, "a chart takes: {}{}".format(", ".join(fields()), _data.did_you_mean(target, fields())))
        spec[target] = _common(target, value)
    if spec.get("data") is not None and spec.get("rows") is not None:
        raise invalid("data", "give the data as a file (data) or inline (rows), not both")
    if spec.get("data") is None and spec.get("rows") is None:
        raise invalid("data", "a chart needs data: a file under artifacts/ (data: \"rev.csv\") or inline rows")
    if spec.get("columns") is not None and spec.get("rows") is None:
        raise invalid("columns", "columns go with rows as arrays")
    for channel in chart.channels:
        required = channel.required or (channel.name == "size" and implied.get("_size_required"))
        if required and spec.get(channel.name) is None and not (channel.aggregate and spec.get("aggregate") == "count"):
            raise invalid(channel.name, "{} needs {}: {}{}".format(chart.name, channel.name, channel.doc or "a column",
                                                                 "; its channels: " + ", ".join(
                                                                     "{}{}".format(c.name, " (required)" if c.required else "") for c in chart.channels)))
    return spec, warnings


def _common(name: str, value: Any) -> Any:
    if name == "data":
        if not isinstance(value, str) or not value.strip():
            raise invalid("data", "data names a file under artifacts/ (rev.csv)")
        text = value.strip()
        while text.startswith("./"):
            text = text[2:]
        if text.startswith("artifacts/"):
            text = text[len("artifacts/"):]
        return text
    if name == "rows":
        if not isinstance(value, list) or not value:
            raise invalid("rows", "rows is a non-empty list")
        return value
    if name == "columns":
        if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
            raise invalid("columns", "columns is a list of column names")
        return value
    if name == "aggregate":
        if value not in _data.AGGREGATES:
            raise invalid("aggregate", "aggregate is one of: {}".format(", ".join(_data.AGGREGATES)))
        return value
    if name == "filter":
        return _data.normalize_filters(value)
    if name == "sort":
        if value not in SORTS and value not in ("value", "-value"):
            raise invalid("sort", "sort is one of: {}".format(", ".join(SORTS + ("value", "-value"))))
        return value
    if name == "top":
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_TOP:
            raise invalid("top", "top is a whole number from 1 to {}".format(MAX_TOP))
        return value
    if name == "other":
        return _bool(value, "other")
    if name == "labels":
        return _one_of(*LABELS)(value, "labels")
    if name == "legend":
        return _one_of(*LEGENDS)(value, "legend")
    if name == "format":
        return _mapping(value, "format", _fmt_rule)
    if name == "units":
        return _mapping(value, "units", _unit_rule)
    if name == "types":
        return _mapping(value, "types", _type_rule)
    if name == "highlight":
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            value = [value]
        if not isinstance(value, list) or len(value) > MAX_HIGHLIGHT:
            raise invalid("highlight", "highlight is a list of up to {} series or categories".format(MAX_HIGHLIGHT))
        return [_data.as_label(v) for v in value]
    if name == "annotations":
        if not isinstance(value, list) or len(value) > MAX_ANNOTATIONS:
            raise invalid("annotations", "annotations is a list of up to {} {{x or y, text}}".format(MAX_ANNOTATIONS))
        out = []
        for index, note in enumerate(value):
            where = "annotations[{}]".format(index)
            if not isinstance(note, dict) or (note.get("x") is None) == (note.get("y") is None) or \
                    any(k not in ("x", "y", "text") for k in note):
                raise invalid(where, "{} is {{\"x\": value}} or {{\"y\": value}}, with an optional text".format(where))
            text = note.get("text")
            if text is not None and (not isinstance(text, str) or len(text) > 80 or "\n" in text):
                raise invalid(where + ".text", "an annotation's text is one line of up to 80 characters")
            axis = "x" if note.get("x") is not None else "y"
            point = note[axis]
            if isinstance(point, bool) or not isinstance(point, (str, int, float)):
                raise invalid("{}.{}".format(where, axis), "an annotation's {} is a category, a date or a number".format(axis))
            item = {axis: point}
            if text:
                item["text"] = text
            out.append(item)
        return out
    if name == "caption":
        if not isinstance(value, str) or len(value) > 200:
            raise invalid("caption", "caption is one line of up to 200 characters")
        return " ".join(value.split())
    raise invalid(name, "unknown chart field {}".format(name))


def data_key(spec: Mapping[str, Any]) -> str:
    """What decides a chart's model: every field but the visual ones (``VISUAL``), as canonical JSON."""
    kept = {k: v for k, v in spec.items() if k not in VISUAL and not k.startswith("_")}
    return json.dumps(kept, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------
# compile


def check_channels(chart: ChartType, spec: Mapping[str, Any], table: Table) -> Dict[str, Any]:
    """Every channel's column exists and has a type the channel accepts (2.2 steps 2 and 3), with the repair message."""
    units: Dict[str, str] = {}
    for channel in chart.channels:
        value = spec.get(channel.name)
        if value is None:
            continue
        for name in (value if isinstance(value, list) else [value]):
            column = table.col(name, channel.name)
            if column.type not in channel.types:
                hint = ""
                if channel.aggregate and "quantitative" in channel.types:
                    hint = ' For counts use aggregate: "count".'
                elif column.type == "nominal" and "ordinal" in channel.types:
                    # A nominal axis would be ranked by value, and a line over it states a false trend (QA phase34 M1).
                    hint = ' If its values have an order, declare it: types: {{{}: "ordinal"}} (kept in data order); ' \
                           'to compare unordered categories use a bar.'.format(json.dumps(name, ensure_ascii=False))
                if column.type in ("nominal", "ordinal"):
                    detail = "{} ({} values: {})".format(column.type, column.distinct, ", ".join(column.sample(3)))
                else:
                    detail = column.type
                raise invalid(channel.name, '{} needs a {} field; {} is {}.{}'.format(
                    channel.name, " or ".join(channel.types), json.dumps(name, ensure_ascii=False), detail, hint),
                    column=name, column_type=column.type, accepts=list(channel.types))
            if column.unit and channel.name not in units:
                units[channel.name] = column.unit
    return units


def filtered(table: Table, rows: Sequence[int]) -> Table:
    """The table with only ``rows`` (every column cut the same way)."""
    if len(rows) == table.rows:
        return table
    columns = []
    for column in table.columns:
        values = [column.values[i] for i in rows]
        keys = [column.keys[i] for i in rows] if column.keys is not None else None
        columns.append(_data.Column(column.name, column.type, values, keys, column.unit, column.grain, column.declared))
    return Table(columns, len(rows), table.label, table.sha256, table.notes)


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))


@dataclass
class DataPart:
    """The data half of a compile (no size needed): what ``Block.load`` computes before the canvas lock."""

    spec: Dict[str, Any]
    model: Dict[str, Any]
    doc: Dict[str, Any]
    stats: Dict[str, Any]
    vegalite: Dict[str, Any]
    warnings: List[Dict[str, Any]]
    source: Dict[str, Any]
    key: str


def compile_data(spec: Mapping[str, Any], table: Table, source_rel: Optional[str] = None) -> DataPart:
    """The data half: types overridden, channels checked, filters applied, defaults resolved, model, doc and stats."""
    chart = get(spec.get("type"))
    if chart is None:
        chart, _implied = resolve_type(spec.get("type"))
    work = dict(copy.deepcopy(dict(spec)))
    typed = _data.retype(table, work.get("types") or {})
    units = check_channels(chart, work, typed)
    if units:
        work["_units"] = units
    rows = _data.apply_filters(typed, work.get("filter") or [])
    view = filtered(typed, rows)
    work["_source"] = "{} {} row{}{}".format(table.label, table.rows, "" if table.rows == 1 else "s",
                                             " ({} kept)".format(len(rows)) if len(rows) != table.rows else "")
    work = chart.normalize(work, view)
    model = chart.model(work, view)
    full = model.pop("_full", None)
    warnings = list(model.pop("_warnings", []) or [])
    if full is not None:
        model_for_doc = dict(model, _full=full)
    else:
        model_for_doc = model
    doc = chart.doc(work, view, model_for_doc)
    stats = chart.stats(work, view, model_for_doc)
    if _json_size(model) > MAX_MODEL_BYTES:
        raise refuse("chart_refused", "the {} chart's model is over {} KB after capping; filter or aggregate the data".format(
            chart.name, MAX_MODEL_BYTES // 1024), limit="MAX_MODEL_BYTES", max=MAX_MODEL_BYTES)
    source = {"data": source_rel, "sha256": table.sha256, "rows": table.rows, "kept": len(rows), "columns": typed.schema()}
    if table.notes:
        source["notes"] = list(table.notes)
    doc = dict({"v": 1, "chart": chart.name, "source": {"data": source_rel, "sha256": table.sha256, "rows": table.rows}}, **doc)
    try:
        vl = chart.vegalite(work, model) if chart.vegalite is not None else None
    except (TypeError, ValueError, KeyError, IndexError, ZeroDivisionError):
        vl = None
    return DataPart(spec=work, model=model, doc=doc, stats=stats, vegalite=vl or {}, warnings=warnings, source=source, key=data_key(spec))


def view(spec: Mapping[str, Any], part: DataPart, box: Tuple[float, float]) -> Tuple[Frame, Dict[str, Any], List[str]]:
    """The size half: ``(frame, option, gist)`` for a slot of ``box`` (pure, cheap: a resize runs only this)."""
    chart = get(part.spec.get("type"))
    assert chart is not None
    work = dict(part.spec)
    for key in VISUAL:
        if key in spec:
            work[key] = spec[key]
        else:
            work.pop(key, None)
    gist = chart.gist(work, part.model, part.stats)
    frame = chart.frame(work, part.model, box)
    option = chart.option(work, part.model, frame, gist)
    if _json_size(option) > MAX_OPTION_BYTES:
        raise refuse("chart_refused", "the {} chart's option is over {} KB".format(chart.name, MAX_OPTION_BYTES // 1024),
                     limit="MAX_OPTION_BYTES", max=MAX_OPTION_BYTES)
    return frame, option, gist


def compile(spec: Mapping[str, Any], table: Table, box: Tuple[float, float] = (536, 300), source_rel: Optional[str] = None) -> Compiled:
    """The whole compile (2.3) of a normalized flat spec over its table, for a slot of ``box``."""
    part = compile_data(spec, table, source_rel)
    frame, option, gist = view(spec, part, box)
    if part.vegalite:
        vegalite = part.vegalite
    else:
        from herdr_team.canvas_charts import _vegalite

        vegalite = _vegalite.gist_card(gist)
    return Compiled(spec=part.spec, model=part.model, doc=part.doc, frame=frame, option=option, gist=gist, vegalite=vegalite,
                    stats=part.stats, warnings=part.warnings, source=part.source)


def catalog(name: Optional[str] = None) -> Dict[str, Any]:
    """Every chart type (or one): its channels, options, frame, caps, ECharts modules and example op."""
    load()
    chosen = types()
    if name is not None:
        found = get(name)
        if found is None:
            raise invalid("type", "no chart type {}; one of: {}{}".format(json.dumps(name), ", ".join(names()), _data.did_you_mean(name, names())))
        chosen = [found]
    out = []
    for chart in chosen:
        out.append({"name": chart.name, "aliases": list(chart.aliases), "gl": chart.gl, "doc": chart.doc_line,
                    "channels": [{"name": c.name, "required": c.required, "types": list(c.types), "many": c.many, "doc": c.doc}
                                 for c in chart.channels],
                    "options": list(chart.options), "echarts": list(chart.echarts), "caps": chart.caps,
                    "example": json.loads(chart.example) if chart.example else None})
    return {"charts": out, "common": list(COMMON), "formats": list(F.FORMATS), "aggregates": list(_data.AGGREGATES),
            "filters": list(_data.FILTER_OPS), "escape": ["spec (a Vega-Lite spec, with data: a file under artifacts/)",
                                                          "echarts (an ECharts option, sanitised: see chart_escape)"],
            "limits": {"data_bytes": _data.MAX_CHART_DATA_BYTES, "data_rows": _data.MAX_DATA_ROWS, "inline_rows": _data.MAX_INLINE_ROWS,
                       "inline_bytes": _data.MAX_INLINE_BYTES}}
