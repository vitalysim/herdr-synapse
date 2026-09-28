"""Server projections of a scene (canvas v2 phase 4, 3.8 and 8.2): the agent's picture of a scene with no page open.

Views ``iso``, ``front`` and ``top`` against hand-computed corner points,
back-face culling, the painter's order, shading by face, labels and leaders,
links, the primitive budget and the cache.
"""
from __future__ import annotations

import math
import sys
import time
import unittest
from unittest import mock

from support import PLUGIN_ROOT

from herdr_team import canvas_display as D
from herdr_team.canvas_scene3d import _project

sys.path.insert(0, str(PLUGIN_ROOT / "tests" / "fixtures" / "scene3d"))
import make_fixtures as M  # noqa: E402 - the fixture builders live beside the fixtures

BOX = (0.0, 0.0, 400.0, 300.0)


def element(*objects, links=(), **settings):
    op = {"op": "scene3d", "id": "s", "objects": list(objects), "links": list(links)}
    op.update(settings)
    return M.solved_element(op)


def prims(result):
    """The drawing's primitives: its clipped geometry, then its labels."""
    group = result["items"][0]
    return group["items"] + result["items"][1:]


def polys(result, fill_prefix):
    return [p for p in prims(result) if p.get("k") == "poly" and str(p.get("fill")).startswith(fill_prefix)]


def rounded(points):
    return sorted((round(x, 2), round(y, 2)) for x, y in points)


class Views(unittest.TestCase):
    """A unit box standing at the origin (x and z from 0 to 1, y from 0 to 1), fitted into 400 × 300 with a 6 % margin."""

    UNIT = {"id": "u", "shape": "box", "tone": "info", "pos": [0.5, 0, 0.5]}

    def test_iso_corners(self):
        # iso: az 45°, el 35.264°. The picture's x is (x − z)·√½ and its y (up) is −(x + z)·√⅙ + y·√⅔: the box spans
        # ±√½ across and ±√⅔ up, so the 264 units of height (300 less 6 % a side) set the scale.
        result = _project.project(element(self.UNIT, ground=False), "iso", BOX)
        scale = 264.0 / (2 * math.sqrt(2.0 / 3.0))
        self.assertAlmostEqual(result["scale"], scale, 3)

        def p(x, y, z):
            return (200 + (x - z) * math.sqrt(0.5) * scale, 150 - (-(x + z) / math.sqrt(6) + y * math.sqrt(2.0 / 3.0)) * scale)

        top = polys(result, "mat.info.top")
        self.assertEqual(len(top), 1)
        self.assertEqual(rounded(top[0]["points"]), rounded([p(0, 1, 0), p(1, 1, 0), p(1, 1, 1), p(0, 1, 1)]))
        right = polys(result, "mat.info.right")[0]
        self.assertEqual(rounded(right["points"]), rounded([p(1, 0, 0), p(1, 1, 0), p(1, 1, 1), p(1, 0, 1)]), "the +x face")
        left = polys(result, "mat.info.left")[0]
        self.assertEqual(rounded(left["points"]), rounded([p(0, 0, 1), p(1, 0, 1), p(1, 1, 1), p(0, 1, 1)]), "the +z face")

    def test_front_and_top(self):
        front = _project.project(element(self.UNIT, ground=False), "front", BOX)
        faces = polys(front, "mat.info")
        self.assertEqual(len(faces), 1, "seen from the front, only the +z face")
        # front: the picture's x is x and its y is y; the unit square fills 264 of 300 units.
        self.assertEqual(rounded(faces[0]["points"]), rounded([(68, 282), (332, 282), (332, 18), (68, 18)]))
        self.assertEqual(faces[0]["fill"], "mat.info.left")
        top = _project.project(element(self.UNIT, ground=False), "top", BOX)
        faces = polys(top, "mat.info")
        self.assertEqual([f["fill"] for f in faces], ["mat.info.top"])
        # top: x across and z down the picture (toward the viewer is down).
        self.assertEqual(rounded(faces[0]["points"]), rounded([(68, 18), (332, 18), (332, 282), (68, 282)]))

    def test_back_faces_are_culled(self):
        result = _project.project(element(self.UNIT, ground=False), "iso", BOX)
        self.assertEqual(len(polys(result, "mat.info")), 3, "top, +x and +z: the three faces the camera sees")
        for poly in polys(result, "mat.info"):
            self.assertEqual((poly["stroke"], poly["sw_px"]), ("mat.info.edge", 1))

    def test_a_zoomed_camera_is_clipped_to_the_box(self):
        plain = _project.project(element(self.UNIT), "iso", BOX)
        self.assertNotIn("clip", plain["items"][0])
        zoomed = _project.project(element(self.UNIT, camera={"preset": "iso", "zoom": 2}), "iso", BOX)
        self.assertEqual(zoomed["items"][0]["clip"], [0.0, 0.0, 400.0, 300.0])
        self.assertAlmostEqual(zoomed["scale"], 2 * plain["scale"], 6)

    def test_the_three_views_differ(self):
        el = element(self.UNIT, {"id": "b", "shape": "cylinder", "right_of": "u"})
        drawn = [D.dumps(_project.project(el, view, BOX)["items"]) for view in ("iso", "front", "top")]
        self.assertEqual(len(set(drawn)), 3)


class Order(unittest.TestCase):
    def ids_in_order(self, result, tones):
        seen = []
        for prim in prims(result):
            fill = str(prim.get("fill") or "")
            for tone, ident in tones.items():
                if fill.startswith("mat.{}.".format(tone)) and ident not in seen:
                    seen.append(ident)
        return seen

    def test_far_is_drawn_before_near(self):
        el = element({"id": "near", "shape": "box", "tone": "info"}, {"id": "far", "shape": "box", "tone": "danger", "behind": "near"},
                     {"id": "side", "shape": "box", "tone": "success", "right_of": "near"})
        order = self.ids_in_order(_project.project(el, "iso", BOX), {"info": "near", "danger": "far", "success": "side"})
        self.assertLess(order.index("far"), order.index("near"))
        self.assertLess(order.index("near"), order.index("side"), "+x is nearer the iso camera")

    def test_what_stands_on_a_floor_is_drawn_after_it(self):
        el = element({"id": "floor", "shape": "plane", "size": [6, 6], "tone": "neutral"},
                     {"id": "b", "shape": "box", "tone": "accent", "on": "floor", "at": [-2.5, -2.5]})
        order = self.ids_in_order(_project.project(el, "iso", BOX), {"neutral": "floor", "accent": "b"})
        self.assertEqual(order, ["floor", "b"])

    def test_the_ground_grid_comes_first(self):
        result = _project.project(element({"id": "b", "shape": "box", "size": [4, 1, 4]}), "iso", BOX)
        first = prims(result)[0]
        self.assertEqual((first["k"], first["stroke"]), ("line", "base.grid"))
        none = _project.project(element({"id": "b", "shape": "box", "size": [4, 1, 4]}, ground=False), "iso", BOX)
        self.assertFalse([p for p in prims(none) if p.get("stroke") == "base.grid"])


class Shapes(unittest.TestCase):
    def test_round_shapes_are_outlines(self):
        el = element({"id": "c", "shape": "cylinder", "tone": "success"}, {"id": "s", "shape": "sphere", "tone": "warning", "right_of": "c"},
                     {"id": "k", "shape": "cone", "tone": "danger", "right_of": "s"}, ground=False)
        result = _project.project(el, "iso", BOX)
        paths = [p for p in prims(result) if p.get("k") == "path"]
        self.assertEqual(sorted(p["fill"] for p in paths), ["mat.danger.left", "mat.success.left", "mat.success.top", "mat.warning.left"],
                         "a cylinder's body and its cap, a sphere's circle, a cone's body")
        for path in paths:
            self.assertRegex(path["d"], r"^M [-0-9.]+ [-0-9.]+ ")
            self.assertTrue(path["d"].endswith("Z"))
        top = _project.project(el, "top", BOX)
        caps = [p for p in prims(top) if p.get("fill") == "mat.success.top"]
        self.assertEqual(len(caps), 1, "from the top, a cylinder shows its cap")

    def test_glass_opacity_and_models(self):
        el = element({"id": "g", "shape": "box", "finish": "glass"}, {"id": "o", "shape": "box", "opacity": 0.5, "right_of": "g"}, ground=False)
        result = _project.project(el, "iso", BOX)
        self.assertEqual({p.get("op") for p in polys(result, "mat.neutral")}, {0.35, 0.5})
        model = M.solved_element({"op": "scene3d", "id": "m", "objects": [{"id": "arm", "shape": "gltf", "src": "models/arm-v3.glb"}],
                                  "ground": False})
        dashed = polys(_project.project(model, "iso", BOX), "mat.neutral")
        self.assertTrue(dashed and all(p.get("dash") == [4, 3] for p in dashed), "a model draws as its dashed proxy box")

    def test_text3d_is_text(self):
        el = element({"id": "p", "shape": "plane", "size": [3, 2]}, {"id": "t", "shape": "text3d", "text": "loading dock", "above": "p"})
        texts = [p for p in prims(_project.project(el, "iso", BOX)) if p.get("k") == "text"]
        self.assertIn(["loading dock"], [[line["t"] for line in t["lines"]] for t in texts])

    def test_links_and_arrows_are_arrows_with_heads(self):
        el = element({"id": "a", "shape": "box"}, {"id": "b", "shape": "box", "right_of": "a", "gap": 2},
                     {"id": "ab", "shape": "arrow3d", "from": "a", "to": "b", "tone": "accent"},
                     links=[{"from": "a", "to": "b", "tone": "warning"}], ground=False)
        arrows = [p for p in prims(_project.project(el, "iso", BOX)) if p.get("k") == "arrow"]
        self.assertEqual(sorted(a["stroke"] for a in arrows), ["tone.accent.stroke", "tone.warning.stroke"])
        for arrow in arrows:
            self.assertEqual(arrow["heads"][0]["shape"], "triangle")
            self.assertEqual(len(arrow["heads"][0]["points"]), 3)


class Labels(unittest.TestCase):
    def labels(self, result):
        return {" ".join(line["t"] for line in p["lines"]): p for p in prims(result) if p.get("k") == "text"}

    def test_a_label_sits_on_a_pill_above_its_object(self):
        el = element({"id": "b", "shape": "box", "label": "Load balancer"}, ground=False)
        result = _project.project(el, "iso", BOX)
        label = self.labels(result)["Load balancer"]
        self.assertEqual(label["size"], 12)
        self.assertEqual(label["lod"], list(D.LOD_LABEL))
        pill = next(p for p in prims(result) if p.get("k") == "rect" and p.get("fill") == "base.surface")
        self.assertLessEqual(pill["y"] + pill["h"], min(y for poly in polys(result, "mat") for _x, y in poly["points"]) + 0.01,
                             "above the object's top")
        self.assertEqual(result["labels"], 1)
        self.assertEqual(result["dropped"], [])

    def test_labels_move_out_of_each_others_way_with_leaders(self):
        el = element({"id": "a", "shape": "box", "label": "first long label"}, {"id": "b", "shape": "box", "label": "second long label",
                                                                                   "right_of": "a", "gap": 0.05}, ground=False)
        result = _project.project(el, "iso", BOX)
        found = self.labels(result)
        self.assertEqual(len(found), 2)
        a, b = (found["first long label"]["box"], found["second long label"]["box"])
        self.assertFalse(a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3], "they do not overlap")

    def test_labels_that_cannot_fit_are_dropped_and_reported(self):
        many = [{"id": "o{}".format(i), "shape": "box", "size": [0.2, 0.2, 0.2], "label": "a rather long label {}".format(i)} for i in range(12)]
        el = element(*many, ground=False)
        small = _project.project(el, "iso", (0, 0, 120, 90))
        self.assertTrue(small["dropped"])
        self.assertEqual(len(self.labels(small)), 12 - len(small["dropped"]))
        everything = _project.project(element(*many, ground=False, labels="all"), "iso", (0, 0, 120, 90))
        self.assertEqual(everything["dropped"], [])
        self.assertTrue(everything["crowded"], "labels: all keeps them, and says they are crowded")
        none = _project.project(element(*many, ground=False, labels="none"), "iso", BOX)
        self.assertEqual(self.labels(none), {})

    def test_a_label_under_a_wide_object_sits_just_under_its_outline(self):
        # QA phase34 L11: the table's label, pushed under it by the servers standing on it, sat under its bounding box, far
        # below the table where the label was (the lowest corner is off to one side).
        servers = [{"id": "s{}".format(i), "shape": "box", "size": [0.5, 0.6, 0.6], "tone": "info", "in": "row", "label": "s{}".format(i)}
                   for i in (1, 2, 3)]
        el = element({"id": "table", "shape": "box", "size": [3, 0.8, 1], "label": "table", "tone": "neutral"},
                     {"id": "row", "shape": "group", "layout": "row", "gap": 0.2, "on": "table"}, *servers,
                     {"id": "mon", "shape": "box", "size": [0.6, 0.4, 0.05], "tone": "accent", "label": "monitor", "above": "s2", "gap": 0.1},
                     ground=False)
        result = _project.project(el, "iso", BOX)
        label = self.labels(result)["table"]
        x0, x1 = label["box"][0], label["box"][0] + label["box"][2]
        table = [pt for poly in polys(result, "mat.neutral") for pt in poly["points"]]
        self.assertGreater(label["box"][1], min(y for _x, y in table), "the servers and the monitor push it under the table")
        lowest = max(y for _x, y in table)
        edge = _project._bottom_under([tuple(pt) for pt in table], x0, x1)
        self.assertIsNotNone(edge)
        self.assertLess(edge, lowest - 20, "the outline under the label is well above the table's lowest corner")
        self.assertGreaterEqual(label["box"][1], edge, "the label is under the outline")
        self.assertLess(label["box"][1], edge + 12, "and just under it")
        self.assertEqual(_project._bottom_under([(0, 0), (10, 10)], 20, 30), None, "no outline there")
        self.assertEqual(_project._bottom_under([(0, 0), (10, 10), (10, 0)], 2, 4), 4.0, "the lower edge at the span's far end")

    def test_a_link_label_sits_at_its_middle(self):
        el = element({"id": "a", "shape": "box"}, {"id": "b", "shape": "box", "right_of": "a", "gap": 3}, links=["a -> b: SQL"], ground=False)
        self.assertIn("SQL", self.labels(_project.project(el, "front", BOX)))


class Budget(unittest.TestCase):
    def test_past_the_budget_the_smallest_draw_as_outlines(self):
        many = [{"id": "o{}".format(i), "shape": "box", "size": [0.2 + 0.05 * i, 0.5, 0.5]} for i in range(20)]
        el = element(*many, ground=False)
        full = _project.project(el, "iso", BOX)
        self.assertEqual(len(polys(full, "mat")), 60)
        with mock.patch.object(_project, "MAX_PRIMS", 100):
            _project._clear_cache()
            capped = _project.project(el, "iso", BOX)
        _project._clear_cache()
        outlines = [p for p in prims(capped) if p.get("k") == "rect" and p.get("fill") is None]
        self.assertTrue(outlines, "some objects are outlines")
        self.assertLessEqual(len(prims(capped)), 100)

    def test_the_cache(self):
        el = element({"id": "b", "shape": "box", "label": "cached"})
        _project._clear_cache()
        first = _project.project(el, "iso", BOX)
        first["items"].clear()
        again = _project.project(el, "iso", BOX)
        self.assertTrue(again["items"], "a caller changing its copy does not change the cache")
        self.assertEqual(len(_project._CACHE), 1)
        _project.project(el, "iso", (0, 0, 401, 300))
        self.assertEqual(len(_project._CACHE), 2, "the box is part of the key")

    def test_150_objects_within_the_budget(self):
        items = [{"id": "floor", "shape": "plane", "size": [60, 60]}]
        for i in range(149):
            item = {"id": "o{}".format(i), "shape": ("box", "sphere", "cylinder")[i % 3], "on": "floor", "label": "obj {}".format(i)}
            if i % 10:
                item["right_of"] = "o{}".format(i - 1)
            elif i:
                item["behind"] = "o{}".format(i - 10)
            items.append(item)
        el = M.solved_element({"op": "scene3d", "id": "big", "objects": items})
        colds, warms = [], []
        for _ in range(5):
            _project._clear_cache()
            colds.append(_timed(lambda: _project.project(el, "iso", (0, 0, 1200, 800))))
            warms.append(_timed(lambda: _project.project(el, "iso", (0, 0, 1200, 800))))
        # Wall-clock bounds are regression guards, not the budget: the budget is about 50 ms cold and 2 ms warm (a cache hit is
        # one JSON parse), but a loaded machine ran cold at 86 ms on 3.9 (load average 48, 2026-09-28), so the bounds leave room
        # while still catching the uncached re-projection (hundreds of ms) and a cache that stops hitting (warm near cold).
        self.assertLess(min(colds), 0.15, "cold {:.1f} ms".format(min(colds) * 1000))
        self.assertLess(min(warms), 0.01, "warm {:.2f} ms".format(min(warms) * 1000))
        self.assertLess(min(warms) * 5, min(colds), "a warm projection is a cache hit, far cheaper than a cold one")


def _timed(fn):
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


if __name__ == "__main__":
    unittest.main()
