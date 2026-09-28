"""The ``icon`` kind (canvas v2 phase 2, 4.2): a Lucide icon by name, sizes, a label under it, unknown names refused."""
from __future__ import annotations

from test_canvas import CanvasRig

from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R


class Icon(CanvasRig):
    def test_sizes_and_label(self):
        for size, side in (("s", 24), ("m", 32), ("l", 48), ("xl", 64)):
            el = self.el(self.ok({"op": "icon", "name": "database", "size": size, "at": [side * 4, 0], "intent": "t"})["ids"][0])
            self.assertEqual((el["w"], el["h"]), (side, side))
        el = self.el(self.ok({"op": "icon", "name": "db", "label": "Orders database", "at": [0, 200], "intent": "t"})["ids"][0])
        self.assertEqual(el["icon"], "database", "our alias resolves")
        self.assertGreater(el["h"], 32)
        self.assertEqual(R.get("icon").readback(el, False), '{} icon database "Orders database"'.format(el["id"]))
        entry = D.entry(el, D.environment(self.scene()))
        self.assertEqual(entry["items"][0]["k"], "group")
        self.assertTrue(all(p["k"] == "path" for p in entry["items"][0]["items"]))

    def test_an_unknown_name_is_refused_with_suggestions(self):
        refused = self.refused({"op": "icon", "name": "rockett", "at": [0, 0], "intent": "t"})
        self.assertEqual(refused["code"], "icon_unknown")
        self.assertIn("rocket", refused["details"]["did_you_mean"])
        self.assertEqual(self.refused({"op": "icon", "at": [0, 0], "intent": "t"})["details"]["field"], "name")

    def test_icons_on_shapes(self):
        plain = self.el(self.ok({"op": "shape", "text": "Checkout API", "at": [0, 0], "intent": "t"})["ids"][0])
        with_icon = self.el(self.ok({"op": "shape", "text": "Checkout API", "icon": "server", "at": [0, 200], "intent": "t"})["ids"][0])
        self.assertEqual(with_icon["icon"], "server")
        self.assertGreaterEqual(with_icon["w"], plain["w"])
        entry = D.entry(with_icon, D.environment(self.scene()))
        icon = next(p for p in entry["items"] if p["k"] == "group")
        text = next(p for p in entry["items"] if p["k"] == "text")
        self.assertLess(icon["t"][4] + 20, text["box"][0] + 1, "the icon sits before the label")
        self.assertEqual(self.refused({"op": "shape", "kind": "text", "text": "x", "icon": "server", "at": [0, 0], "intent": "t"})["details"]["field"],
                         "icon")
