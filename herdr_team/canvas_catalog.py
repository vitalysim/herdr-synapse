"""``canvas catalog charts|scene3d`` (canvas v2 phases 3 and 4, 1.6): what the chart types and the 3D primitives take.

Generated from the registries (``canvas_charts.catalog()`` and
``canvas_scene3d.catalog()``), so a new chart type or primitive module shows up
here with no edit: its channels or parameters, options, caps and one example op.
It is the discovery door the skill guides point to, like ``canvas icons
--search``; it needs no team.

Pure: reads the registries only.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from herdr_team.errors import HerdrTeamError, exit_code_for


def catalog(what: str, name: Optional[str] = None) -> Dict[str, Any]:
    """``{"what", ...}``: the chart types (``charts``) or the 3D primitives, relations and layouts (``scene3d``); ``name``
    narrows it to one type or primitive (``usage`` naming the known ones otherwise)."""
    if what == "charts":
        from herdr_team import canvas_charts

        try:
            return dict(canvas_charts.catalog(name), what="charts")
        except HerdrTeamError as err:
            raise HerdrTeamError("usage", err.message, exit_code_for("usage"), dict(err.details))
    if what == "scene3d":
        from herdr_team import canvas_scene3d

        found = canvas_scene3d.catalog(name)
        if name is not None and not any(found.get(key) for key in ("primitives", "relations", "layouts")):
            known = [p.get("name") for p in canvas_scene3d.catalog().get("primitives") or []]
            raise HerdrTeamError("usage", "no primitive, relation or layout {}; primitives: {}".format(json.dumps(name), ", ".join(known)),
                                 exit_code_for("usage"), {"name": name})
        return dict(found, what="scene3d")
    raise HerdrTeamError("usage", "catalog is charts or scene3d", exit_code_for("usage"), {"what": what})


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


def text(doc: Dict[str, Any]) -> str:
    """The catalog as the lines ``canvas catalog`` prints."""
    lines: List[str] = []
    if doc.get("what") == "charts":
        for chart in doc.get("charts") or []:
            head = "{}{}{} — {}".format(chart["name"], " (also {})".format(", ".join(chart["aliases"])) if chart.get("aliases") else "",
                                        " [3D, WebGL]" if chart.get("gl") else "", chart.get("doc") or "")
            lines.append(head)
            channels = ", ".join("{}{} ({})".format(c["name"], "*" if c.get("required") else "", "/".join(c.get("types") or []))
                                 for c in chart.get("channels") or [])
            lines.append("  channels: {}".format(channels or "none"))
            if chart.get("options"):
                lines.append("  options: {}".format(", ".join(chart["options"])))
            if chart.get("caps"):
                lines.append("  caps: {}".format(chart["caps"]))
            if chart.get("example"):
                lines.append("  example: {}".format(_compact(chart["example"])))
        lines.append("* required. Every type also takes: {}".format(", ".join(doc.get("common") or [])))
        lines.append("aggregate: {} · filter ops: {} · format: {}".format(", ".join(doc.get("aggregates") or []), ", ".join(doc.get("filters") or []),
                                                                        ", ".join(doc.get("formats") or [])))
        lines.append("escape hatches: " + "; ".join(doc.get("escape") or []))
        return "\n".join(lines)
    for prim in doc.get("primitives") or []:
        lines.append("{} — {}".format(prim["name"], prim.get("doc") or ""))
        lines.append("  params: {}".format(", ".join(prim.get("params") or []) or "none"))
        if prim.get("example"):
            lines.append("  example: {}".format(prim["example"] if isinstance(prim["example"], str) else _compact(prim["example"])))
    if doc.get("relations"):
        lines.append("relations: " + ", ".join("{} ({})".format(r["name"], r.get("doc") or r.get("group") or "") for r in doc["relations"]))
    if doc.get("layouts"):
        lines.append("group layouts: " + ", ".join("{} ({})".format(l["name"], l.get("doc") or "") for l in doc["layouts"]))
    if doc.get("common"):
        lines.append("every object: {}".format(", ".join(doc["common"])))
    return "\n".join(lines)
