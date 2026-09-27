"""The element kinds that have no hooks yet (canvas v2 phase 0): name, role and the ops that create them.

A transitional bucket: a kind moves into a module of its own the day it gets
its first hook (``measure``, ``readback``, ``checks`` ...). Until then every
stage treats it generically, exactly as before the registry existed.
"""
from __future__ import annotations

from herdr_team.canvas_kinds import Kind

KINDS = (
    Kind(name="arrow", role="connector", ops=("arrow", "graph", "mermaid"), doc="a line or connector between two elements or points"),
    Kind(name="frame", role="container", ops=("frame", "graph", "mermaid", "portrait"), doc="a titled area that owns its children"),
    Kind(name="pen", role="leaf", ops=("pen",), doc="a freehand stroke"),
    Kind(name="path", role="leaf", ops=("path",), doc="SVG path data drawn as one shape"),
    Kind(name="svg", role="leaf", ops=("svg",), doc="a sanitised SVG block"),
    Kind(name="mermaid", role="leaf", ops=("mermaid",), doc="a Mermaid diagram the page renders (flowcharts become native shapes)"),
    Kind(name="chart", role="leaf", ops=("chart",), doc="a Vega-Lite chart the page renders"),
    Kind(name="viz", role="leaf", ops=("viz",), doc="a sealed live visual (d3, three, p5) the page runs"),
    Kind(name="image", role="leaf", ops=("image",), doc="a stored PNG, JPEG or SVG image"),
    Kind(name="comment", role="overlay", ops=("comment",), doc="a comment pin on a point or an element"),
)
