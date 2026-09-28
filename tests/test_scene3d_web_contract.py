"""What the page can draw in 3D against what Python registers (canvas v2 phase 4, D19, interface I-12).

``web/dist/charts.json`` (written by ``web/scripts/postbuild.mjs`` from
``web/src/v2/scene3d/manifest.js``) lists the scene3d primitives and loaders
the page bundles and the echarts-gl modules of its GL chart chunk. Every
primitive Python registers must have its page builder, and every GL chart
type's modules must be bundled. The source manifest is held here too, so a
drift shows before the next build.
"""
from __future__ import annotations

import json
import re
import unittest

from support import PLUGIN_ROOT

from herdr_team import canvas_charts as CC
from herdr_team import canvas_scene3d as S

MANIFEST = PLUGIN_ROOT / "web" / "src" / "v2" / "scene3d" / "manifest.js"
CHARTS_JSON = PLUGIN_ROOT / "web" / "dist" / "charts.json"


def js_list(source: str, name: str) -> list:
    """The string array ``export const <name> = [...]`` in a data-only module."""
    match = re.search(r"export const " + name + r"\s*=\s*(\[[^\]]*\])", source)
    assert match is not None, name
    return json.loads(match.group(1))


class SourceManifest(unittest.TestCase):
    def setUp(self):
        self.source = MANIFEST.read_text(encoding="utf-8")

    def test_primitives_and_loaders(self):
        self.assertEqual(sorted(js_list(self.source, "PRIMITIVES")), sorted(S.names()))
        self.assertEqual(js_list(self.source, "LOADERS"), S.loaders())

    def test_gl_chart_modules(self):
        bundled = set(js_list(self.source, "GL_MODULES"))
        for chart in CC.types():
            if chart.gl:
                self.assertLessEqual(set(chart.echarts), bundled, chart.name)


class Built(unittest.TestCase):
    def setUp(self):
        if not CHARTS_JSON.is_file():
            self.skipTest("web/dist/charts.json is written by the first npm run build of phases 3 and 4 (interface I-12)")
        self.doc = json.loads(CHARTS_JSON.read_text(encoding="utf-8"))

    def test_primitives_and_loaders(self):
        self.assertEqual(self.doc["v"], 1)
        self.assertEqual(sorted(self.doc["scene3d"]["primitives"]), sorted(S.names()))
        self.assertEqual(self.doc["scene3d"]["loaders"], ["gltf"])

    def test_gl_chart_modules(self):
        for chart in CC.types():
            if chart.gl:
                self.assertLessEqual(set(chart.echarts), set(self.doc["gl"]), chart.name)


if __name__ == "__main__":
    unittest.main()
