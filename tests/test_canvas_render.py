"""Canvas rendering, sanitising, layout and the Mermaid subset (``canvas_render``, ``canvas_layout``, ``canvas_mermaid``, 0.21)."""
from __future__ import annotations

import math
import os
import stat
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from test_canvas import OPERATOR, PNG_MAGIC, CanvasRig, fake_resvg, png

from herdr_team import canvas as C
from herdr_team import canvas_layout as L
from herdr_team import canvas_mermaid as M
from herdr_team import canvas_render as R
from herdr_team import canvas_theme as T
from herdr_team.errors import HerdrTeamError

SVG = "http://www.w3.org/2000/svg"


def refused_reason(markup: str) -> str:
    try:
        R.sanitize_svg(markup)
    except HerdrTeamError as err:
        assert err.code == "svg_refused", err.code
        return str(err.details["reason"])
    raise AssertionError("not refused: {}".format(markup))


def tags(root):
    return [node.tag.split("}")[-1] for node in root.iter()]


class SanitizeSvg(unittest.TestCase):
    def test_attacks_refuse_the_whole_block(self):
        cases = {
            "<svg xmlns='{}'><script>alert(1)</script></svg>": "element:script",
            "<svg xmlns='{}'><g><foreignObject><div/></foreignObject></g></svg>": "element:foreignObject",
            "<svg xmlns='{}'><rect onload='x()'/></svg>": "event_handler",
            "<svg xmlns='{}' ONMOUSEOVER='x()'/>": "event_handler",
            "<svg xmlns='{}' xmlns:xlink='http://www.w3.org/1999/xlink'><use xlink:href='https://evil/x.svg#a'/></svg>": "external_href",
            "<svg xmlns='{}'><use href='data:image/svg+xml;base64,AAAA'/></svg>": "external_href",
            "<svg xmlns='{}'><rect style='fill:url(https://evil/x)'/></svg>": "css_url",
            "<svg xmlns='{}'><rect fill='url( \"//evil/x\" )'/></svg>": "css_url",
            "<svg xmlns='{}'><rect fill='u\\72l(https://evil)'/></svg>": "css_escape",
            "<svg xmlns='{}'><rect fill='javascript:alert(1)'/></svg>": "script_url",
            "<svg xmlns='{}'><image href='https://evil/x.png'/></svg>": "element:image",
            "<svg xmlns='{}'><a href='https://evil'><rect/></a></svg>": "element:a",
            "<svg xmlns='{}'><rect><animate attributeName='x'/></rect></svg>": "element:animate",
            "<svg xmlns='{}'><set attributeName='fill' to='red'/></svg>": "element:set",
            "<svg xmlns='{}'><metadata><script/></metadata></svg>": "element:script",
            "<svg xmlns='{}'><iframe/></svg>": "element:iframe",
            '<!DOCTYPE svg [<!ENTITY lol "lol">]><svg xmlns="{}">&lol;</svg>': "doctype",
            '<?xml-stylesheet href="https://evil/x.css"?><svg xmlns="{}"/>': "stylesheet",
            "<html xmlns='{}'/>": "not_svg",
            "<div/>": "not_svg",
            "<svg": "not_xml",
            "": "empty",
        }
        for markup, reason in cases.items():
            with self.subTest(markup=markup):
                self.assertEqual(refused_reason(markup.replace("{}", SVG)), reason)

    def test_size_and_node_limits(self):
        self.assertEqual(refused_reason("<svg xmlns='{}'>{}</svg>".format(SVG, "<g/>" * 30000)), "too_large")
        with mock.patch.object(R, "MAX_SVG_NODES", 5):
            self.assertEqual(refused_reason("<svg xmlns='{}'>{}</svg>".format(SVG, "<g/>" * 5)), "too_many_nodes")

    def test_safe_content_is_rebuilt_from_the_allow_list(self):
        clean = R.sanitize_svg(
            "<?xml version='1.0'?><svg xmlns='{}' xmlns:xlink='http://www.w3.org/1999/xlink' xmlns:inkscape='x' viewBox='0 0 10 10' "
            "inkscape:version='1'><defs><linearGradient id='g'><stop offset='0' stop-color='red'/></linearGradient></defs>"
            "<style>@import url(https://evil)</style><metadata><rdf/></metadata>"
            "<g fill='url(#g)' style='stroke: blue; behaviour-ish: x; stroke-width: 2' data-x='1'>"
            "<text x='1'>a &lt;b&gt; <tspan>c</tspan> d</text><use xlink:href='#g'/><path d='M0 0L1 1'/></g></svg>".format(SVG))
        root = ET.fromstring(clean)
        self.assertEqual(tags(root), ["svg", "defs", "linearGradient", "stop", "g", "text", "tspan", "use", "path"])
        group = root.find("{%s}g" % SVG)
        self.assertEqual(group.get("style"), "stroke:blue;stroke-width:2")
        self.assertIsNone(group.get("data-x"))
        self.assertEqual(root.find(".//{%s}use" % SVG).get("href"), "#g")
        self.assertEqual("".join(root.find(".//{%s}text" % SVG).itertext()), "a <b> c d")
        self.assertNotIn("inkscape", clean)
        self.assertEqual(R.sanitize_svg(clean), clean, "sanitising is idempotent")
        self.assertEqual(R.svg_size(clean), (10.0, 10.0))
        self.assertEqual(R.svg_size("<svg xmlns='{}' width='30px' height='20'/>".format(SVG)), (30.0, 20.0))
        self.assertIsNone(R.svg_size("<svg xmlns='{}'/>".format(SVG)))


class PathData(unittest.TestCase):
    def test_commands_and_numbers_only(self):
        self.assertEqual(R.sanitize_path_data("M10,10 l 20 0 a5 5 0 1010 10 z m1-2e1 .5.5"), "M10 10 l20 0 a5 5 0 1 0 10 10 z m1 -20 l0.5 0.5")
        for bad in ("L 10 10", "M 1 2 <", "M 1", "M 1 2 A 1 1 0 2 1 3 3", "M1 2 z 3 4", "M 1 2 url(x)", "", "M 1e 2"):
            with self.subTest(bad=bad):
                with self.assertRaises(HerdrTeamError) as ctx:
                    R.sanitize_path_data(bad)
                self.assertEqual(ctx.exception.code, "op_invalid")

    def test_normalised_to_the_origin_with_its_size(self):
        self.assertEqual(R.normalize_path("M100 100 L 200 150 C 250 150 250 250 200 250 Z"), ("M0 0 L100 50 C150 50 150 150 100 150 Z", 150.0, 150.0))
        self.assertEqual(R.normalize_path("m 10 10 h 50 v 30 H 5 V 1 z"), ("M5 9 H55 V39 H0 V0 Z", 55.0, 39.0))
        d, w, h = R.normalize_path("M0 0 A 50 50 0 0 1 100 0")
        self.assertEqual((w, h), (100.0, 50.0), "an arc's bulge counts toward the bounds")
        self.assertTrue(d.startswith("M0 50 A50 50 0 0 1 100 50"))
        _segments, points = R.absolute_path(R.parse_path("M0 0 C10 10 20 10 30 0 S 50 -10 60 0 Q 70 10 80 0 T 100 0"))
        self.assertIn((50.0, -10.0), points)
        self.assertIn((90.0, -10.0), points, "T reflects the last quadratic control point")


class Freehand(unittest.TestCase):
    def test_outline_is_a_closed_polygon_around_the_stroke(self):
        points = [[0, 0], [10, 5], [20, 10], [40, 10], [60, 0]]
        outline = R.freehand_outline(points, 2)
        self.assertGreater(len(outline), 2 * len(points))
        self.assertTrue(all(math.isfinite(x) and math.isfinite(y) for x, y in outline))
        xs = [p[0] for p in outline]
        self.assertLess(min(xs), 0)
        self.assertGreater(max(xs), 60)
        straight = R.freehand_outline([[0, 0], [50, 0], [100, 0]], 2, smooth=False)
        widths = {round(abs(y), 2) for x, y in straight if 1 < x < 99}
        self.assertEqual(widths, {4.25}, "no thinning when not smooth: a constant half-width")
        pressured = R.freehand_outline([[0, 0, 0.1], [50, 0, 0.1], [100, 0, 1.0]], 4)
        self.assertEqual(len(R.freehand_outline([[5, 5]], 2)), 16, "a dot is a small circle")
        self.assertEqual(R.freehand_outline([], 2), [])
        self.assertTrue(pressured)


class RenderSvg(CanvasRig):
    def scene_with_everything(self):
        self.drivers()
        self.apply([
            {"op": "pen", "points": ["c11r5", "c19r4", "c22r9", "c15r12"], "color": "red", "intent": "t"},
            {"op": "shape", "kind": "text", "text": "<script>alert(1)</script>", "at": "c0r30", "intent": "t"},
            {"op": "claim", "region": "c10r4:c40r22", "label": "mapping", "intent": "t"},
            {"op": "comment", "at": "price", "text": "why?", "intent": "t"},
            {"op": "chart", "spec": {"mark": "bar", "data": {"values": [{"a": 1}]}}, "title": "Churn", "at": "c50r0", "intent": "t"},
            {"op": "svg", "svg": "<svg xmlns='{}' viewBox='0 0 10 10'><circle cx='5' cy='5' r='4'/></svg>".format(SVG), "at": "c0r40", "intent": "t"},
            {"op": "path", "d": "M0 0 L10 10", "at": "c20r40", "intent": "t"},
        ])
        self.apply([{"op": "lock", "region": "c0r60:c10r70", "label": "hands off"}], OPERATOR)
        return C.load_scene(self.team)

    def test_structure(self):
        scene = self.scene_with_everything()
        svg = R.render_svg(scene, team=self.team, grid=True)
        root = ET.fromstring(svg)
        self.assertEqual(root.tag, "{%s}svg" % SVG)
        box = R.view_box(scene)
        self.assertEqual(root.get("viewBox"), " ".join(R._n(v) for v in (box[0], box[1], box[2] - box[0], box[3] - box[1])))
        self.assertEqual(max(int(root.get("width")), int(root.get("height"))), 1024)
        texts = ["".join(t.itertext()) for t in root.iter("{%s}text" % SVG)]
        for mark in ("E-1", "E-2", "E-4", "C-1"):
            self.assertIn(mark, texts, "every element carries its id badge")
        self.assertIn("<script>alert(1)</script>", texts, "text is text, never markup")
        self.assertNotIn("script", tags(root))
        self.assertIn("c0r0", texts, "grid labels")
        self.assertIn("rendered on the page", " ".join(texts))
        sticky = T.resolve("idea", "soft", "note")["fill"]
        self.assertTrue(any(r.get("fill") == sticky for r in root.iter("{%s}rect" % SVG)), "notes are sticky paper in their tone")
        self.assertTrue(any(r.get("stroke-dasharray") for r in root.iter("{%s}rect" % SVG)), "claims are dashed")
        # Phase 1: hatch patterns are numbered by first use (docs/display-list.md), synapse-hatch-0 here.
        self.assertTrue(any(r.get("fill") == "url(#synapse-hatch-0)" for r in root.iter("{%s}rect" % SVG)), "locks are hatched")
        red = T.resolve("danger", "soft", "pen")["stroke"]
        self.assertTrue(any(p.get("fill") == red for p in root.iter("{%s}polygon" % SVG)), "the pen is a filled outline")
        nested = [n for n in root.iter("{%s}svg" % SVG) if n is not root]
        self.assertEqual(len(nested), 1, "the svg block is inlined")
        self.assertEqual(nested[0].get("viewBox"), "0 0 10 10")
        # Phase 1: a path is placed and scaled by its group's matrix (the display list's group primitive).
        self.assertTrue(any(g.get("transform", "") == "matrix(1 0 0 1 400 800)" for g in root.iter("{%s}g" % SVG)))
        no_marks = ET.fromstring(R.render_svg(scene, team=self.team, marks=False))
        self.assertNotIn("E-1", ["".join(t.itertext()) for t in no_marks.iter("{%s}text" % SVG)])

    def test_region_and_stills(self):
        scene = self.scene_with_everything()
        chart = next(e for e in scene["elements"] if e["type"] == "chart")
        region = [chart["x"], chart["y"], chart["x"] + chart["w"], chart["y"] + chart["h"]]
        root = ET.fromstring(R.render_svg(scene, region, team=self.team, max_px=500))
        self.assertEqual((root.get("width"), root.get("height")), ("500", "333"))
        self.assertEqual(root.findall(".//{%s}image" % SVG), [])
        C.store_still(self.team, chart["id"], chart["updated_seq"], png())
        root = ET.fromstring(R.render_svg(scene, region, team=self.team))
        [image] = root.findall(".//{%s}image" % SVG)
        self.assertTrue(image.get("href").startswith("data:image/png;base64,"))

    def test_images_are_data_uris(self):
        asset = C.store_asset(self.team, png(8, 8), "image")["asset"]
        self.apply([{"op": "image", "asset": asset, "at": "c0r0"}], OPERATOR)
        root = ET.fromstring(R.render_svg(C.load_scene(self.team), team=self.team))
        [image] = root.findall(".//{%s}image" % SVG)
        self.assertTrue(image.get("href").startswith("data:image/png;base64,"))


class RenderPng(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cr-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_resvg_through_the_hook(self):
        calls = []

        def run(argv, timeout):
            calls.append((argv, timeout))
            return fake_resvg(argv, timeout)

        with mock.patch.object(R, "find_resvg", return_value="/fake/resvg"), mock.patch.object(R, "RUN", run):
            out = R.render_png("<svg xmlns='{}' width='10' height='10'/>".format(SVG), self.tmp / "a.png", width_px=64)
        self.assertEqual(out, self.tmp / "a.png")
        argv, timeout = calls[0]
        self.assertEqual(argv[:3], ["/fake/resvg", "-w", "64"])
        self.assertTrue(argv[-2].endswith(".svg") and argv[-1].endswith("a.png"))
        self.assertEqual(timeout, R.RENDER_TIMEOUT_S)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["a.png"], "the temporary svg is removed")

    def test_missing_or_failing_resvg_is_none_never_an_exception(self):
        self.assertIsNone(R.render_png("<svg/>", self.tmp / "b.png", env={"PATH": ""}))
        self.assertIsNone(R.find_resvg({"PATH": "", R.RESVG_ENV: os.fspath(self.tmp / "nope")}))
        with mock.patch.object(R, "find_resvg", return_value="/fake/resvg"):
            with mock.patch.object(R, "RUN", lambda argv, timeout: (1, "boom")):
                self.assertIsNone(R.render_png("<svg/>", self.tmp / "c.png"))
            with mock.patch.object(R, "RUN", lambda argv, timeout: (0, "")):
                self.assertIsNone(R.render_png("<svg/>", self.tmp / "d.png"), "exit 0 without a PNG is a failure")

            def explode(argv, timeout):
                raise RuntimeError("hook broke")

            with mock.patch.object(R, "RUN", explode):
                self.assertIsNone(R.render_png("<svg/>", self.tmp / "e.png"))

    def test_find_resvg_honours_the_override(self):
        binary = self.tmp / "resvg"
        binary.write_text("#!/bin/sh\n")
        os.chmod(binary, 0o700)
        self.assertEqual(R.find_resvg({R.RESVG_ENV: os.fspath(binary), "PATH": ""}), os.fspath(binary))
        self.assertEqual(R.find_resvg({"PATH": os.fspath(self.tmp)}), os.fspath(binary))

    def test_prune_keeps_the_newest_renders(self):
        for index in range(5):
            for ext in ("svg", "png"):
                path = self.tmp / "look-{}.{}".format(index, ext)
                path.write_text("x")
                os.utime(path, (1000 + index, 1000 + index))
        self.assertEqual(R.prune_renders(self.tmp, keep=2), 6)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["look-3.png", "look-3.svg", "look-4.png", "look-4.svg"])

    def test_sniff_image(self):
        self.assertEqual(R.sniff_image(png(640, 480)), ("image/png", 640, 480))
        jpeg = b"\xff\xd8\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00" + b"\x00" * 9 + b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + (300).to_bytes(2, "big") + (400).to_bytes(2, "big") + b"\x03"
        self.assertEqual(R.sniff_image(jpeg), ("image/jpeg", 400, 300))
        self.assertIsNone(R.sniff_image(b"GIF89a"))
        self.assertTrue(PNG_MAGIC)


class Layout(unittest.TestCase):
    def test_layered_down_and_right(self):
        down = L.layout(["a", "b", "c", "d"], [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")])
        self.assertEqual(down, {"a": (110.0, 0), "b": (0.0, 160), "c": (220.0, 160), "d": (110.0, 320)})
        right = L.layout(["a", "b", "c"], [("a", "b"), ("b", "c")], direction="right")
        self.assertEqual(right, {"a": (0, 0.0), "b": (260, 0.0), "c": (520, 0.0)})
        up = L.layout(["a", "b"], [("a", "b")], direction="up")
        self.assertGreater(up["a"][1], up["b"][1])

    def test_cycles_and_self_loops_do_not_hang(self):
        positions = L.layout(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a"), ("a", "a")])
        self.assertEqual(sorted(positions), ["a", "b", "c"])
        self.assertEqual(L.layer_of(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")]), {"a": 0, "b": 1, "c": 2})

    def test_other_layouts_are_deterministic_and_do_not_overlap(self):
        nodes = ["n{}".format(i) for i in range(8)]
        edges = [("n0", "n{}".format(i)) for i in range(1, 8)]
        for algorithm in ("radial", "force", "grid"):
            first = L.layout(nodes, edges, algorithm)
            self.assertEqual(first, L.layout(nodes, edges, algorithm), algorithm)
            boxes = [(x, y, x + L.NODE_W, y + L.NODE_H) for x, y in first.values()]
            for i, a in enumerate(boxes):
                for b in boxes[i + 1:]:
                    self.assertFalse(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3], algorithm)
        self.assertEqual(L.layout([], []), {})


class Mermaid(unittest.TestCase):
    def test_flowchart_subset(self):
        parsed = M.parse_flowchart("flowchart LR\n %% note\n A[\"Start, now\"] --> B{Ok?} -->|Yes| C((Done)); B -- No --> D([Retry]) -.-> A\n"
                                   " E & F ==> G>flag]\n subgraph one [First]\n  H[[sub]] --- I\n  subgraph two\n   J\n  end\n end\n"
                                   " classDef x fill:#f00\n style A fill:#fff\n linkStyle 0 stroke:red\n")
        self.assertEqual(parsed["direction"], "right")
        nodes = {n["id"]: (n["text"], n["kind"], n["subgraph"]) for n in parsed["nodes"]}
        self.assertEqual(nodes["A"], ("Start, now", "box", None))
        self.assertEqual(nodes["B"], ("Ok?", "diamond", None))
        self.assertEqual(nodes["C"][1], "ellipse")
        self.assertEqual(nodes["J"], ("J", "box", "two"))
        edges = [(e["from"], e["to"], e["label"], e["dash"], e["head"], e["thick"]) for e in parsed["edges"]]
        self.assertIn(("B", "C", "Yes", False, "arrow", False), edges)
        self.assertIn(("B", "D", "No", False, "arrow", False), edges)
        self.assertIn(("D", "A", None, True, "arrow", False), edges)
        self.assertIn(("E", "G", None, False, "arrow", True), edges)
        self.assertIn(("H", "I", None, False, "none", False), edges)
        self.assertEqual(parsed["subgraphs"], [{"id": "one", "title": "First", "parent": None}, {"id": "two", "title": "two", "parent": "one"}])

    def test_kinds_and_refusals(self):
        self.assertEqual([M.diagram_kind(s) for s in ("graph TD\nA", "sequenceDiagram\nA->>B: x", "stateDiagram-v2\n[*] --> A", "pie title x",
                                                       "erDiagram", "quadrantChart", "journey", "")],
                         ["flowchart", "sequence", "state", "pie", "er", "quadrant", "other", "other"])
        with self.assertRaises(M.MermaidRefused):
            M.parse_flowchart("graph TD\nA --> B\nclick A callback")
        for bad in ("graph TD\nA -->", "graph TD\nsubgraph x\nA", "graph TD\nend", "graph TD\nA[open", "sequenceDiagram\nA", "graph TD\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(M.MermaidSyntax):
                    M.parse_flowchart(bad)


if __name__ == "__main__":
    unittest.main()
