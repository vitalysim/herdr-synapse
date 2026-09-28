"""The ``sticky`` kind (canvas v2 phase 2, 4.2): paper sizes, shrink then grow taller, the N tool's blank sticky."""
from __future__ import annotations

from test_canvas import OPERATOR, CanvasRig

from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R


class Sticky(CanvasRig):
    def test_paper_sizes_and_the_idea_tone(self):
        for size, side in (("s", 160), ("m", 200), ("l", 240)):
            el = self.el(self.ok({"op": "sticky", "text": "hi", "size": size, "at": [side * 2, 0], "intent": "t"})["ids"][0])
            self.assertEqual((el["w"], el["h"]), (side, side))
            self.assertEqual(el["style"]["tone"], "idea")

    def test_text_shrinks_to_14_before_the_paper_grows_taller(self):
        el = self.el(self.ok({"op": "sticky", "text": "word " * 30, "size": "s", "at": [0, 0], "intent": "t"})["ids"][0])
        self.assertEqual(el["fit"]["size"], 14)
        self.assertEqual(el["w"], 160)
        self.assertGreater(el["h"], 160)

    def test_the_page_makes_a_blank_one_an_agent_may_not(self):
        self.ok({"op": "sticky", "text": "", "at": [0, 0], "intent": "t"}, author=OPERATOR)
        self.assertEqual(self.refused({"op": "sticky", "at": [0, 0], "intent": "t"})["details"]["field"], "text")

    def test_emit_has_no_outline_and_a_shadow(self):
        eid = self.ok({"op": "sticky", "text": "hi", "tone": "warning", "at": [0, 0], "intent": "t"})["ids"][0]
        entry = D.entry(self.el(eid), D.environment(self.scene()))
        paper = entry["items"][0]
        self.assertEqual((paper["fill"], paper["stroke"], paper["elev"]), ("tone.warning.sticky", None, 1))
        self.assertEqual(R.get("sticky").tool.key, "n")
