"""What every registered chart type must do (canvas v2 phase 3, 8.1): run by ``test_charts_registry`` over the registry.

For one ``ChartType`` and its example op: the pure hooks are deterministic
(twice, and over shuffled input rows when the spec does not depend on row
order), the model and option are 16 KB or less, the drawing is 1,500 primitives
or fewer inside the slot and validates, every text keeps the fit invariant,
every token reference resolves in both palettes, every ``$doc`` reference names
something the doc holds, the gist is not empty, the declared ECharts modules
are bundled (``web/dist/charts.json``, once built), and a fuzz of random
tables never raises anything but a refusal.
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from herdr_team import canvas_charts as CC
from herdr_team import canvas_display as D
from herdr_team import canvas_theme
from herdr_team.canvas_charts import _data
from herdr_team.errors import HerdrTeamError

REPO = Path(__file__).resolve().parent.parent
GUIDES = REPO / "tests" / "fixtures" / "canvas_artifacts" / "guides"
CHARTS_JSON = REPO / "web" / "dist" / "charts.json"
_REF = re.compile(r"^(chart|tone|base|mat)\.[a-z0-9_.]+$")
BOX = (536.0, 330.0)


def example_spec(chart: CC.ChartType) -> Dict[str, Any]:
    spec, _warnings = CC.normalize_spec(json.loads(chart.example))
    return spec


def table_of(spec: Dict[str, Any], shuffle: Optional[random.Random] = None) -> Tuple[_data.Table, Optional[str]]:
    """The example's table (its guide file, or its inline rows), rows shuffled when asked."""
    if spec.get("data"):
        path = GUIDES / spec["data"]
        raw = path.read_bytes()
        if shuffle is not None and path.suffix in (".csv", ".tsv"):
            lines = raw.decode("utf-8").splitlines()
            body = lines[1:]
            shuffle.shuffle(body)
            raw = ("\n".join(lines[:1] + body) + "\n").encode("utf-8")
        elif shuffle is not None:
            rows = json.loads(raw.decode("utf-8"))
            shuffle.shuffle(rows)
            raw = json.dumps(rows).encode("utf-8")
        return _data.parse(raw, path.suffix, spec["data"]), spec["data"]
    rows = list(spec.get("rows") or [])
    if shuffle is not None:
        shuffle.shuffle(rows)
    return _data.inline(rows, spec.get("columns")), None


def order_free(spec: Dict[str, Any]) -> bool:
    """Whether the spec's drawing may not depend on row order (no ordinal column, no ``sort: none``)."""
    return "ordinal" not in (spec.get("types") or {}).values() and spec.get("sort") != "none"


def walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)


def doc_refs(value: Any) -> List[str]:
    out: List[str] = []
    if isinstance(value, dict):
        if set(value) == {"$doc"}:
            out.append(value["$doc"])
        for item in value.values():
            out += doc_refs(item)
    elif isinstance(value, list):
        for item in value:
            out += doc_refs(item)
    return out


def texts(items: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for prim in items:
        if prim.get("k") == "text":
            yield prim
        yield from texts(prim.get("items") or [])


def compiled(chart: CC.ChartType, shuffle: Optional[random.Random] = None) -> Tuple[CC.Compiled, List[Dict[str, Any]]]:
    spec = example_spec(chart)
    table, rel = table_of(spec, shuffle)
    found = CC.compile(spec, table, BOX, rel)
    drawing = chart.draw(found.spec, found.model, found.frame, (100.0, 50.0, BOX[0], BOX[1])) if chart.draw else []
    return found, drawing


def _canon(found: CC.Compiled, drawing: List[Dict[str, Any]]) -> str:
    doc = {k: v for k, v in found.doc.items() if k != "source"}  # the source's sha256 is of the bytes, which a shuffle changes
    return json.dumps([found.model, found.option, found.frame.to_json(), found.gist, doc, drawing], sort_keys=True, default=str)


def check_chart(case: Any, chart: CC.ChartType) -> None:
    case.assertTrue(chart.example, "{} has an example op".format(chart.name))
    first, drawing = compiled(chart)
    again, drawing2 = compiled(chart)
    case.assertEqual(_canon(first, drawing), _canon(again, drawing2), "{} is deterministic".format(chart.name))
    if order_free(example_spec(chart)):
        for seed in (1, 2):
            shuffled, drawn = compiled(chart, random.Random(seed))
            case.assertEqual(_canon(first, drawing), _canon(shuffled, drawn), "{}: row order changes nothing".format(chart.name))
    case.assertLessEqual(len(json.dumps(first.model, separators=(",", ":")).encode()), CC.MAX_MODEL_BYTES, chart.name)
    case.assertLessEqual(len(json.dumps(first.option, separators=(",", ":")).encode()), CC.MAX_OPTION_BYTES, chart.name)
    case.assertTrue(first.gist and all(isinstance(line, str) and line for line in first.gist), chart.name)
    # the option contract (2.6)
    case.assertIs(first.option.get("animation"), False, chart.name)
    case.assertEqual(first.option["tooltip"].get("renderMode"), "richText", chart.name)
    for key in ("toolbox", "title", "graphic"):
        case.assertNotIn(key, first.option, chart.name)
    palettes = {theme: canvas_theme.palette(theme) for theme in ("light", "dark")}
    for text in walk_strings(first.option):
        if _REF.match(text):
            for theme, palette in palettes.items():
                case.assertIn(text, palette, "{}: {} resolves in {}".format(chart.name, text, theme))
        case.assertFalse(text.strip().lower().startswith(("http:", "https:", "//", "data:", "javascript:")), chart.name)
    refs = first.doc.get("refs") or {}
    for name in doc_refs(first.option):
        case.assertTrue(name == "datasets" or name in refs, "{}: $doc {} is in the doc".format(chart.name, name))
    case.assertLessEqual(len(json.dumps(first.doc).encode()), 2 * 1024 * 1024, chart.name)
    # the drawing (D2)
    if chart.draw is not None:
        case.assertGreaterEqual(len(drawing), 5, chart.name)
        case.assertLessEqual(len(drawing), 1500, chart.name)
        out: List[str] = []
        for index, prim in enumerate(drawing):
            D._check_prim(prim, "{}[{}]".format(chart.name, index), palettes, out)
        case.assertEqual(out, [], chart.name)
        x0, y0, x1, y1 = 100.0 - 1, 50.0 - 1, 100.0 + BOX[0] + 1, 50.0 + BOX[1] + 1
        for prim in drawing:
            box = D.prim_bounds(prim)
            if box is None:
                continue
            case.assertTrue(box[0] >= x0 and box[1] >= y0 and box[2] <= x1 and box[3] <= y1,
                            "{}: {} {} inside the slot".format(chart.name, prim.get("k"), box))
        for prim in texts(drawing):
            for line in prim.get("lines") or []:
                case.assertLessEqual(line["w"], prim["box"][2] + 0.5, "{}: {!r} fits its box".format(chart.name, line["t"]))
    # the bundle (D19): skipped until the first build writes charts.json
    if CHARTS_JSON.is_file():
        bundled = json.loads(CHARTS_JSON.read_text(encoding="utf-8"))
        have = set(bundled.get("gl" if chart.gl else "echarts") or [])
        case.assertLessEqual(set(chart.echarts), have, "{}'s ECharts modules are bundled".format(chart.name))
    fuzz(case, chart)


def fuzz(case: Any, chart: CC.ChartType, runs: int = 200) -> None:
    """Random small tables through the example's spec: a chart or a refusal (``HerdrTeamError``), never anything else."""
    spec = example_spec(chart)
    fields = [value for channel in chart.channels for value in (spec.get(channel.name) if isinstance(spec.get(channel.name), list)
                                                               else [spec.get(channel.name)]) if value]
    rng = random.Random(chart.name)
    pools: List[List[Any]] = [[0, 1, -3.5, 1e9, 12.25, None, "", "NA"], ["a", "b", "c", "Other", "", None, "日本"],
                              ["2026-01", "2026-02", "2026-03-04", None, "2026-Q1"], [1, 1, 1, 1], ["x" * 300, "y"]]
    for _run in range(runs):
        rows = []
        for _ in range(rng.randint(0, 12)):
            rows.append({name: rng.choice(rng.choice(pools)) for name in fields})
        try:
            table = _data.inline(rows) if rows else _data.inline([{}])
            found = CC.compile(spec, table, (rng.randint(80, 900), rng.randint(60, 700)))
            if chart.draw is not None:
                chart.draw(found.spec, found.model, found.frame, (0.0, 0.0, 300.0, 200.0))
        except HerdrTeamError:
            continue
