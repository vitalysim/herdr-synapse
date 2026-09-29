"""Canvas v2 phase 0 on the canvas: labels size their shapes before placement (``w``/``h`` are minimums),
graph nodes are sized before layout, colour comes from tones, the apply result reports geometry, a grown
shape keeps its frame and makes way for its neighbours, and the checks run as a registry."""
from __future__ import annotations

import unittest

from test_canvas import REVIEWER, CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_check as K
from herdr_team import canvas_layout as L
from herdr_team import canvas_text as X
from herdr_team import canvas_theme as T

LONG = "Internationalization and localization pipeline"


class ShapesFitTheirLabels(CanvasRig):
    def test_a_long_label_grows_the_shape_and_the_fit_is_stored(self):
        result = self.ok({"op": "shape", "text": LONG, "at": [0, 0], "intent": "t"})
        box = self.el(result["ids"][0])
        self.assertGreater(box["w"], 160)
        self.assertEqual((box["w"] % 20, box["h"] % 20), (0, 0), "grown sides land on the grid")
        self.assertEqual(box["fit"]["policy"], "hug")
        self.assertEqual(box["fit"]["min"], [160, 80])
        self.assertEqual(" ".join(box["fit"]["lines"]), LONG)
        self.assertIsNone(K.label_room(box))

    def test_an_explicit_size_larger_than_the_text_needs_is_kept_exactly(self):
        box = self.el(self.ok({"op": "shape", "text": "API", "at": [0, 0], "w": 333, "h": 97, "intent": "t"})["ids"][0])
        self.assertEqual((box["w"], box["h"], box["fit"]["min"]), (333, 97, [333, 97]))

    def test_every_kind_uses_its_policy(self):
        text = "Ask the designer whether the roof should be flat or pitched"
        for kind, policy in (("note", "shrink"), ("ellipse", "scale_shape"), ("diamond", "scale_shape"), ("text", "hug")):
            el = self.el(self.ok({"op": "shape", "kind": kind, "text": text, "at": [0, 1000 * len(policy)], "intent": "t"})["ids"][0])
            with self.subTest(kind=kind):
                self.assertEqual(el["fit"]["policy"], policy)
                self.assertIsNone(K.label_room(el), "it fits as drawn")
        note = self.el(self.ok({"op": "shape", "kind": "note", "text": text, "at": [0, 2000], "intent": "t"})["ids"][0])
        self.assertEqual((note["w"], note["h"]), C.SHAPE_SIZES["note"], "the paper keeps its size")
        self.assertLess(note["fit"]["size"], note["style"]["size"], "the text shrank instead")
        self.assertIn("(shrunk to {}px)".format(note["fit"]["size"]), C.describe(note))

    def test_the_apply_result_reports_the_geometry(self):
        result = self.apply([{"op": "shape", "text": LONG, "at": [0, 0], "intent": "t"},
                             {"op": "shape", "kind": "text", "text": "hello", "at": [0, 300], "intent": "t"}])
        first, second = result["applied"]
        box = self.el(first["ids"][0])
        self.assertEqual(first["geometry"], [{"id": box["id"], "x": 0, "y": 0, "w": box["w"], "h": box["h"],
                                              "fit": {"policy": "hug", "size": 20, "lines": 2}}])
        self.assertEqual(second["geometry"][0]["fit"]["lines"], 1)
        self.assertIn("#0 shape {} → {}x{} (hug, 2 lines)".format(box["id"], box["w"], box["h"]), C.apply_text(result))
        moved = self.ok({"op": "move", "id": box["id"], "by": [20, 0], "intent": "t"})
        self.assertNotIn("geometry", moved, "a plain move sizes nothing")

    def test_an_edit_refits_from_the_minimum_so_a_box_shrinks_back(self):
        eid = self.ok({"op": "shape", "text": LONG, "at": [0, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "edit", "id": eid, "text": "API", "intent": "t"})
        self.assertEqual((self.el(eid)["w"], self.el(eid)["h"]), (160, 80))
        grown = self.ok({"op": "edit", "id": eid, "text": LONG, "intent": "t"})
        self.assertEqual(grown["geometry"][0]["id"], eid)
        self.assertGreater(self.el(eid)["w"], 160)

    def test_a_resize_sets_a_new_minimum_but_never_hides_the_text(self):
        eid = self.ok({"op": "shape", "text": LONG, "at": [0, 0], "intent": "t"})["ids"][0]
        fitted = (self.el(eid)["w"], self.el(eid)["h"])
        self.ok({"op": "move", "id": eid, "w": 40, "h": 40, "intent": "too small"})
        self.assertEqual((self.el(eid)["w"], self.el(eid)["h"]), fitted, "the label still needs its room")
        self.assertEqual(self.el(eid)["fit"]["min"], [40, 40])
        self.ok({"op": "move", "id": eid, "w": 600, "h": 200, "intent": "bigger"})
        self.assertEqual((self.el(eid)["w"], self.el(eid)["h"]), (600, 200))
        self.assertEqual(len(self.el(eid)["fit"]["lines"]), 1, "a wider box holds the label on one line")

    def test_a_bigger_size_refits_and_bound_arrows_follow(self):
        a = self.ok({"op": "shape", "text": "Checkout", "at": [0, 0], "intent": "t"})["ids"][0]
        b = self.ok({"op": "shape", "text": "Payments", "at": [600, 0], "intent": "t"})["ids"][0]
        arrow = self.ok({"op": "arrow", "from": a, "to": b, "intent": "t"})["ids"][0]
        before = self.el(arrow)["points"]
        restyled = self.ok({"op": "restyle", "id": a, "size": "xl", "intent": "t"})
        self.assertIn(arrow, restyled["ids"], "the arrow re-routes in the same event")
        self.assertNotEqual(self.el(arrow)["points"], before)
        self.assertEqual(self.el(a)["fit"]["size"], 36)
        self.assertIsNone(K.label_room(self.el(a)))

    def test_an_element_from_before_0_22_only_grows(self):
        eid = self.ok({"op": "shape", "text": "old", "at": [0, 0], "w": 300, "h": 90, "intent": "t"})["ids"][0]
        el = self.el(eid)
        legacy = dict(el)
        legacy.pop("fit")
        self.assertEqual(C._minimum(legacy), (300.0, 90.0), "its authored size is its minimum")


class GraphsSizeNodesBeforeLayout(CanvasRig):
    def test_long_labels_never_run_into_their_neighbours(self):
        nodes = [{"id": "n{}".format(i), "text": ("step " * (1 + i * 2)).strip(), "kind": ("box", "diamond", "note", "ellipse")[i % 4]}
                 for i in range(12)]
        edges = [["n{}".format(i), "n{}".format(i + 1)] for i in range(11)] + [["n0", "n5"], ["n2", "n9"]]
        result = self.ok({"op": "graph", "nodes": nodes, "edges": edges, "at": [0, 0], "intent": "t"})
        scene = self.scene()
        shapes = [e for e in scene["elements"] if e.get("group") and e["type"] in C.TEXT_TYPES]
        self.assertEqual(len(shapes), 12)
        for el in shapes:
            self.assertIsNone(K.label_room(el), el["id"])
        found = K.problems(scene["elements"])
        self.assertEqual([p for p in found if p["code"] in ("overlap", "label_overflow", "frame_edge")], [])
        self.assertEqual(len(result["geometry"]), 12)
        sizes = {(el["w"], el["h"]) for el in shapes}
        self.assertGreater(len(sizes), 1, "nodes keep their own sizes")

    def test_a_node_tone_colours_that_node(self):
        self.ok({"op": "graph", "nodes": [{"id": "a", "text": "ok", "tone": "success"}, {"id": "b", "text": "broken", "tone": "danger"}],
                 "edges": [["a", "b"]], "at": [0, 0], "intent": "t"})
        nodes = {e["text"]: e for e in self.scene()["elements"] if e.get("group") and e["type"] == "box"}
        self.assertEqual(nodes["ok"]["style"]["fill"], T.resolve("success")["fill"])
        self.assertEqual(nodes["broken"]["style"]["tone"], "danger")
        self.assertEqual(self.refused({"op": "graph", "nodes": [{"id": "a", "tone": "mauve"}], "intent": "t"})["details"]["field"], "nodes[0].tone")

    def test_a_mermaid_flowchart_is_sized_the_same_way(self):
        source = "flowchart LR\n A[Start the long checkout process] --> B{Is the payment approved by the bank?}\n B -->|yes| C((Done))"
        self.ok({"op": "mermaid", "source": source, "at": [0, 0], "intent": "t"})
        scene = self.scene()
        for el in scene["elements"]:
            if el["type"] in C.TEXT_TYPES:
                self.assertIsNone(K.label_room(el), el["id"])
        self.assertEqual([p for p in K.problems(scene["elements"]) if p["code"] in ("overlap", "label_overflow")], [])


class Layout(unittest.TestCase):
    SIZES = {"a": (160.0, 60.0), "b": (320.0, 140.0), "c": (200.0, 60.0), "d": (160.0, 200.0)}
    EDGES = [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]

    def boxes(self, positions):
        return {n: (x, y, x + self.SIZES[n][0], y + self.SIZES[n][1]) for n, (x, y) in positions.items()}

    def assert_apart(self, boxes):
        items = list(boxes.items())
        for i, (a, ba) in enumerate(items):
            for b, bb in items[i + 1:]:
                self.assertFalse(ba[0] < bb[2] and bb[0] < ba[2] and ba[1] < bb[3] and bb[1] < ba[3], (a, b, ba, bb))

    def test_every_algorithm_places_sized_nodes_apart(self):
        for algorithm in L.LAYOUTS:
            for direction in ("down", "right", "up", "left"):
                with self.subTest(algorithm=algorithm, direction=direction):
                    positions = L.layout(list(self.SIZES), self.EDGES, algorithm, direction, sizes=self.SIZES)
                    self.assert_apart(self.boxes(positions))
                    self.assertEqual(min(x for x, _ in positions.values()), 0)

    def test_a_layered_layer_is_as_thick_as_its_largest_node_and_centres_the_rest(self):
        boxes = self.boxes(L.layout(list(self.SIZES), self.EDGES, "layered", "down", sizes=self.SIZES))
        layer_one = [boxes["b"], boxes["c"]]
        self.assertEqual(boxes["b"][3] - boxes["b"][1], 140)
        self.assertAlmostEqual((boxes["c"][1] + boxes["c"][3]) / 2, (boxes["b"][1] + boxes["b"][3]) / 2, msg="centred in the layer")
        self.assertGreaterEqual(boxes["d"][1], max(b[3] for b in layer_one) + L.LAYER_GAP)

    def test_equal_sizes_lay_out_exactly_as_before(self):
        ids, edges = ["a", "b", "c", "d"], self.EDGES
        uniform = {n: (L.NODE_W, L.NODE_H) for n in ids}
        for algorithm in L.LAYOUTS:
            self.assertEqual(L.layout(ids, edges, algorithm, sizes=uniform), L.layout(ids, edges, algorithm))


class Tones(CanvasRig):
    def test_authorship_is_not_a_colour(self):
        mine = self.el(self.ok({"op": "shape", "text": "a", "at": [0, 0], "intent": "t"})["ids"][0])
        theirs = self.el(self.ok({"op": "shape", "text": "b", "at": [400, 0], "intent": "t"}, REVIEWER)["ids"][0])
        self.assertEqual(mine["style"]["stroke"], theirs["style"]["stroke"])
        self.assertNotEqual(self.scene()["authors"]["alpha-worker"]["color"], self.scene()["authors"]["alpha-reviewer"]["color"],
                            "authors keep their colours for claims, chips and portraits")

    def test_tone_variant_and_the_hex_escape_hatch(self):
        eid = self.ok({"op": "shape", "text": "risk", "at": [0, 0], "tone": "warning", "intent": "t"})["ids"][0]
        self.assertEqual({k: self.el(eid)["style"][k] for k in ("stroke", "fill", "text", "tone", "variant")},
                         dict(T.resolve("warning", "soft", "box"), tone="warning", variant="soft"))
        self.ok({"op": "restyle", "id": eid, "variant": "solid", "intent": "t"})
        self.assertEqual(self.el(eid)["style"]["fill"], T.resolve("warning", "solid", "box")["fill"], "a variant alone keeps the tone")
        self.ok({"op": "restyle", "id": eid, "tone": "info", "fill": "#ff00ff", "intent": "t"})
        self.assertEqual((self.el(eid)["style"]["stroke"], self.el(eid)["style"]["fill"]), (T.resolve("info", "solid")["stroke"], "#ff00ff"),
                         "an explicit fill in the same op wins")
        text = self.el(self.ok({"op": "shape", "kind": "text", "text": "hex", "color": "#123456", "at": [0, 300], "intent": "t"})["ids"][0])
        self.assertEqual((text["style"]["stroke"], text["style"]["text"], text["style"]["tone"]), ("#123456", "#123456", None))
        self.assertEqual(self.refused({"op": "shape", "at": [0, 0], "variant": "loud", "intent": "t"})["details"]["field"], "variant")

    def test_a_portrait_keeps_its_authors_colour(self):
        self.ok({"op": "portrait", "steps": ["one", "two"], "current": 1, "intent": "plan"})
        steps = [e for e in self.scene()["elements"] if e.get("role") == "portrait" and e["type"] == "box"]
        self.assertTrue(steps and all(s["style"]["stroke"] == C.AUTHOR_PALETTE[0] for s in steps))


class GrowthAndNeighbours(CanvasRig):
    def test_a_shape_that_grows_past_its_frame_grows_the_frame(self):
        frame = self.ok({"op": "frame", "title": "Home", "at": [0, 0], "w": 300, "h": 200, "intent": "t"})["ids"][0]
        eid = self.ok({"op": "shape", "kind": "ellipse", "text": "the warm afternoon sun", "at": [200, 100], "w": 60, "h": 60, "intent": "t"})["ids"][0]
        self.assertEqual(self.el(eid)["frame"], frame, "its asked-for box was in the frame, so it joins it")
        self.assertTrue(C._contains(C.bounds(self.el(frame)), C.bounds(self.el(eid))))

    def test_a_later_label_makes_way_when_growth_made_them_collide(self):
        first = self.ok({"op": "shape", "text": LONG, "at": [0, 0], "intent": "t"})["ids"][0]
        result = self.apply([{"op": "shape", "text": "second", "at": [200, 0], "intent": "t"}])
        [warning] = [w for w in result["warnings"] if w["code"] == "moved_to_fit"]
        second = self.el(result["applied"][0]["ids"][0])
        self.assertEqual(warning["ids"], [second["id"], first])
        self.assertFalse(C._intersects(C.bounds(second), C.bounds(self.el(first))))
        self.assertEqual([p for p in K.problems(self.scene()["elements"]) if p["code"] == "overlap"], [])

    def test_a_collision_the_op_asked_for_is_left_to_check(self):
        self.ok({"op": "shape", "text": "walls", "at": [0, 0], "w": 300, "h": 200, "intent": "t"})
        result = self.apply([{"op": "shape", "text": "door", "at": [20, 20], "intent": "t"}])
        self.assertEqual([w["code"] for w in result["warnings"] if w["code"] == "moved_to_fit"], [])


class CheckRegistry(unittest.TestCase):
    def test_the_checks_are_registered_in_severity_order(self):
        self.assertEqual([c.code for c in K.CHECKS], ["overlap", "label_overflow", "frame_edge", "arrow_through", "stray", "claim_edge"])
        self.assertEqual([c.severity for c in K.CHECKS], sorted(c.severity for c in K.CHECKS))

    def test_a_new_check_is_one_function_and_one_line(self):
        def shouting(live, env):
            return [K._problem("shouting", [el["id"]], "{} is all capitals".format(el["id"]), None, env["reader"], env["by_id"])
                    for el in live if str(el.get("text") or "").isupper()]

        K.register_check(K.Check("shouting", 5, shouting))
        self.addCleanup(lambda: (K.CHECKS.pop(), K.SEVERITY.pop("shouting")))
        found = K.problems([{"id": "E-1", "type": "box", "x": 0, "y": 0, "w": 400, "h": 80, "text": "LOUD", "style": {"size": 20}}])
        self.assertEqual([p["code"] for p in found], ["shouting"])
        with self.assertRaises(ValueError):
            K.register_check(K.Check("shouting", 5, shouting))

    def test_a_kind_check_reports_a_clamped_label(self):
        text = "one two three four five six seven eight nine ten"
        result = X.fit("clamp", X.FitRequest(text, min_w=120, min_h=40, pad_x=16, pad_y=12, max_lines=1))
        box = {"id": "E-1", "type": "box", "x": 0, "y": 0, "w": result.w, "h": result.h, "text": text, "style": {"size": 20},
               "fit": X.fit_record(result, (120, 40))}
        self.assertEqual([(p["code"], p["ids"]) for p in K.problems([box])], [("label_truncated", ["E-1"])])


if __name__ == "__main__":
    unittest.main()
