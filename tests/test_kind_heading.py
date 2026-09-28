"""The ``heading`` kind (canvas v2 phase 2, 4.2): three levels; level 1 follows the title rule, 2 and 3 are body text."""
from __future__ import annotations

from test_canvas import CanvasRig

from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R


class Heading(CanvasRig):
    def test_levels(self):
        sizes = {}
        for level in (1, 2, 3):
            el = self.el(self.ok({"op": "heading", "text": "Q4 launch", "level": level, "at": [0, level * 100], "intent": "t"})["ids"][0])
            sizes[level] = el["h"]
            entry = D.entry(el, D.environment(self.scene()))
            text = [p for p in entry["items"] if p.get("k") == "text"]
            if level == 1:
                self.assertEqual(len(text), 2, "level 1: in place, and held at 12 px zoomed out")
                self.assertEqual(text[1]["zoom"]["min_px"], 12)
            else:
                self.assertEqual(text[0]["lod"], D.LOD_BODY)
        self.assertGreater(sizes[1], sizes[2])
        self.assertGreater(sizes[2], sizes[3])

    def test_wrap_width_readback_and_resize(self):
        eid = self.ok({"op": "heading", "text": "A heading long enough to wrap", "w": 200, "at": [0, 0], "intent": "t"})["ids"][0]
        el = self.el(eid)
        self.assertEqual(el["w"], 200)
        self.assertGreater(len(el["fit"]["lines"]), 1)
        self.assertEqual(R.get("heading").readback(el, True).split(" [")[0], '{} heading h2 "A heading long enough to wrap"'.format(eid))
        self.ok({"op": "move", "id": eid, "w": 800, "intent": "t"})
        self.assertEqual(len(self.el(eid)["fit"]["lines"]), 1)
        self.assertEqual(self.refused({"op": "heading", "text": "x", "level": 4, "at": [0, 0], "intent": "t"})["details"]["field"], "level")
