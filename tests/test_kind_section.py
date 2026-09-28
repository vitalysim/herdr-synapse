"""The ``section`` kind (canvas v2 phase 2, 4.2 and 4.3): a frame with a layout, its minimum, grid cells and title rule."""
from __future__ import annotations

from test_canvas import CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R


def by_alias(scene, alias):
    return next(e for e in scene["elements"] if e.get("alias") == alias)


class Section(CanvasRig):
    def test_a_section_is_a_frame_with_a_layout(self):
        self.ok({"op": "section", "id": "s", "title": "Section", "at": [0, 0], "intent": "t"})
        el = by_alias(self.scene(), "s")
        self.assertEqual((el["type"], el["block"], el["settings"]["layout"], el["w"], el["h"]), ("frame", "section", "free", 320, 200))
        self.assertIs(R.kind_of(el), R.get("section"))

    def test_settings_are_checked(self):
        for op, field in (({"layout": "diagonal"}, "layout"), ({"cols": 3}, "cols"), ({"layout": "row", "grid": "3x3"}, "grid"),
                          ({"grid": "0x3"}, "grid"), ({"padding": "xl"}, "padding"), ({"gap": "huge"}, "gap")):
            with self.subTest(op=op):
                refused = self.refused(dict({"op": "section", "at": [0, 0], "intent": "t"}, **op))
                self.assertEqual(refused["details"]["field"], field)

    def test_a_grid_section_places_children_row_major(self):
        self.ok({"op": "section", "id": "g", "title": "Grid", "layout": "grid", "cols": 2, "at": [0, 0], "intent": "t"})
        ids = [self.ok({"op": "sticky", "text": str(i), "in": "g", "intent": "t"})["ids"][0] for i in range(3)]
        els = [self.el(i) for i in ids]
        self.assertEqual(els[0]["y"], els[1]["y"])
        self.assertLess(els[0]["x"], els[1]["x"])
        self.assertEqual(els[2]["x"], els[0]["x"])
        self.assertGreater(els[2]["y"], els[0]["y"])

    def test_a_free_section_with_a_grid_resolves_cells_locally(self):
        self.ok({"op": "section", "id": "w", "title": "Wire", "grid": "4x3", "at": [100, 100], "intent": "t"})
        root = by_alias(self.scene(), "w")
        self.assertEqual(root["settings"]["cell"], [80, 80])
        eid = self.ok({"op": "sticky", "text": "q", "in": "w", "at": "c1r1", "size": "s", "intent": "t"})["ids"][0]
        x0, y0 = root["x"] + 32, root["y"] + 60 + 32
        self.assertEqual((self.el(eid)["x"], self.el(eid)["y"]), (x0 + 80, y0 + 80))
        self.assertEqual(self.el(eid)["frame"], root["id"])
        self.ok({"op": "section", "id": "r", "layout": "row", "at": [2000, 0], "intent": "t"})
        self.assertEqual(self.refused({"op": "sticky", "text": "q", "in": "r", "at": "c1r1", "intent": "t"})["details"]["field"], "at")

    def test_children_join_on_create_and_readback(self):
        a = self.ok({"op": "card", "id": "a", "title": "A", "at": [0, 0], "intent": "t"})["ids"][0]
        b = self.ok({"op": "card", "id": "b", "title": "B", "at": [300, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "section", "id": "s", "title": "Both", "layout": "row", "gap": "m", "children": ["a", "b"], "intent": "t"})
        root = by_alias(self.scene(), "s")
        self.assertEqual((self.el(a)["frame"], self.el(b)["frame"]), (root["id"], root["id"]))
        self.assertEqual(root["order"], [a, b])
        line = C.look(self.layout, self.team, "alpha-worker")["text"]
        self.assertIn('section s "Both" row gap m', line)
        self.assertIn(": a b", line)

    def test_the_title_rule(self):
        self.ok({"op": "section", "id": "s", "title": "A long section title", "at": [0, 0], "intent": "t"})
        entry = next(e for e in D.display_list(self.scene())["entries"] if e["kind"] == "section")
        titles = [p for p in entry["items"] if p.get("k") == "text" and p["lines"] and p["lines"][0]["t"].startswith("A long")]
        self.assertEqual(len(titles), 2, "in the band, and above it zoomed out")
        band, above = titles
        self.assertEqual(band["lod"], [0.6, None])
        self.assertEqual((above["lod"], above["zoom"]["min_px"]), ([None, 0.6], 12))
        self.assertIn("Empty section", [p["lines"][0]["t"] for p in entry["items"] if p.get("k") == "text"])

    def test_upsert_changes_the_settings(self):
        self.ok({"op": "section", "id": "s", "title": "S", "layout": "row", "at": [0, 0], "intent": "t"})
        applied = self.ok({"op": "section", "id": "s", "title": "S2", "layout": "column", "intent": "t"})
        self.assertTrue(applied["block"]["upsert"])
        root = by_alias(self.scene(), "s")
        self.assertEqual((root["text"], root["settings"]["layout"]), ("S2", "column"))
        self.ok({"op": "patch", "id": "s", "set": {"layout": "grid", "cols": 2}, "intent": "t"})
        self.assertEqual(by_alias(self.scene(), "s")["settings"]["cols"], 2)
