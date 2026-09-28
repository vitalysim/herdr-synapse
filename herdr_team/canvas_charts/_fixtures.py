"""The shared chart fixtures the page is held to (canvas v2 phase 3, interface I-9): ``--write`` or ``--check`` them.

* ``tests/fixtures/charts/options/<type>.json``: every registered chart type's
  example op compiled as the chart kind stores it, ``{"element": {...}, "doc":
  {...}}``: the page's ``resolveOption`` and static render run over these.
* ``tests/fixtures/charts/format-vectors.json``: inputs and the exact strings
  ``_format.fmt_number`` gives, which ``web/src/v2/charts/format.js`` must give too.

The examples' data files come from ``tests/fixtures/canvas_artifacts/guides``.
``python3 -m herdr_team.canvas_charts._fixtures --write`` rewrites them;
``--check`` exits 1 when one is stale (a test runs it).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Sequence

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "charts"
GUIDES = REPO / "tests" / "fixtures" / "canvas_artifacts" / "guides"


def _dump(doc: Any) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def option_fixture(name: str) -> Dict[str, Any]:
    """One chart type's example, compiled and framed as the chart kind stores it (at the type's default size)."""
    from herdr_team import canvas_charts as CC
    from herdr_team.canvas_charts import _data
    from herdr_team.canvas_kinds import chart as K

    chart = CC.get(name)
    assert chart is not None and chart.example, name
    op = json.loads(chart.example)
    spec = K.normalize(K.PURE, dict(op))
    settings = K.settings_of(spec)
    if settings.get("data") is not None:
        path = GUIDES / settings["data"]
        table = _data.parse(path.read_bytes(), path.suffix, settings["data"])
        rel = settings["data"]
    else:
        table, rel = _data.inline(settings.get("rows"), settings.get("columns")), None
    part = CC.compile_data(settings, table, rel)
    title = str(spec.get("title") or "")
    w, h = (float(v) for v in chart.default_box)
    w, h, _grew = K._fit_size(settings, part, w, h, title, str(settings.get("caption") or ""))
    shown = K.view_fields(settings, part, 0.0, 0.0, w, h, title)
    doc_text = json.dumps(part.doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    element = {"id": "E-1", "type": "chart", "kv": K.KIND_VERSION, "alias": op.get("id"), "text": title, "x": 0, "y": 0, "w": w, "h": h,
               "updated_seq": 1, "settings": settings, "engine": "echarts", "chart": {"type": chart.name, "gl": bool(chart.gl)},
               "source": part.source, "model": part.model, "stats": part.stats,
               "resolved": {k: v for k, v in part.spec.items() if k not in ("rows", "columns")},
               "doc_asset": "{}.json".format(hashlib.sha256(doc_text.encode("utf-8")).hexdigest()[:32])}
    element.update(shown)
    box = K.slot_box(0.0, 0.0, w, h, title, str(settings.get("caption") or ""))
    # ``box`` is the slot's size (what the page renders the option at); ``slot`` its place in the element's box.
    return {"type": chart.name, "gl": bool(chart.gl), "box": {"w": box[2], "h": box[3]}, "slot": list(box), "element": element, "doc": part.doc}


def files() -> Dict[Path, str]:
    from herdr_team import canvas_charts as CC
    from herdr_team.canvas_charts import _format

    out = {FIXTURES / "format-vectors.json": _dump(_format.vectors())}
    for chart in CC.types():
        if chart.example:
            out[FIXTURES / "options" / "{}.json".format(chart.name)] = _dump(option_fixture(chart.name))
    return out


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m herdr_team.canvas_charts._fixtures",
                                     description="Write (or check) the chart fixtures the page's tests read.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(list(argv))
    stale = []
    wanted = files()
    for path, text in sorted(wanted.items()):
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current != text:
            stale.append(path)
            if args.write:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
    if args.write:
        known = {p.name for p in wanted}
        for path in sorted((FIXTURES / "options").glob("*.json")):
            if path.name not in known:
                path.unlink()
                stale.append(path)
    if args.check and stale:
        sys.stderr.write("stale chart fixtures: {}; run python3 -m herdr_team.canvas_charts._fixtures --write\n".format(
            ", ".join(str(p.relative_to(REPO)) for p in stale)))
        return 1
    for path in stale if args.write else ():
        sys.stdout.write("wrote {}\n".format(path.relative_to(REPO)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
