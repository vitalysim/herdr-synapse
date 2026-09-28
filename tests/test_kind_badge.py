"""The ``badge`` kind (canvas v2 phase 2, 4.2): one line clamped at 240, two sizes, three variants, an icon."""
from __future__ import annotations

from test_canvas import CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R


class Badge(CanvasRig):
    def test_sizes_variants_and_readback(self):
        small = self.el(self.ok({"op": "badge", "text": "P0", "size": "s", "tone": "danger", "at": [0, 0], "intent": "t"})["ids"][0])
        self.assertEqual(small["h"], 20)
        medium = self.el(self.ok({"op": "badge", "text": "P0", "tone": "danger", "variant": "solid", "icon": "flame", "at": [0, 100],
                                  "intent": "t"})["ids"][0])
        self.assertEqual(medium["h"], 24)
        self.assertGreater(medium["w"], small["w"], "the icon takes room")
        entry = D.entry(medium, D.environment(self.scene()))
        self.assertEqual(entry["items"][0]["fill"], "tone.danger.solid")
        self.assertEqual(R.get("badge").readback(small, False), '{} badge "P0" (danger)'.format(small["id"]))

    def test_a_long_word_is_clamped_and_reported(self):
        eid = self.ok({"op": "badge", "text": "an extremely long status word that cannot fit in a badge at all", "at": [0, 0], "intent": "t"})["ids"][0]
        el = self.el(eid)
        self.assertEqual(el["w"], 240)
        self.assertTrue(el["fit"]["truncated"])
        self.assertIn("label_truncated", [p["code"] for p in C.check(self.layout, self.team, "alpha-worker")["problems"]])
