"""The ``kanban`` kind (canvas v2 phase 2, 4.5): columns as stack sections, cards as members, counters and limits."""
from __future__ import annotations

from test_canvas import CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_blocks as B
from herdr_team import canvas_display as D

WORK = {"op": "kanban", "id": "work", "title": "Launch work", "at": [0, 0], "intent": "t",
        "columns": [{"title": "To do", "cards": ["A", "B"]}, {"title": "Doing", "limit": 1, "tone": "warning", "cards": ["C", "D"]},
                    {"title": "To do", "cards": []}]}


class Kanban(CanvasRig):
    def elements(self):
        return self.scene()["elements"]

    def test_column_ids_are_slugs_made_unique(self):
        self.ok(WORK)
        parts = sorted(e["part"] for e in self.elements() if e.get("type") == "frame" and e.get("part"))
        self.assertEqual(parts, ["doing", "to-do", "to-do-2"])

    def test_counters_limits_and_the_wip_check(self):
        self.ok(WORK)
        doing = next(e for e in self.elements() if e.get("part") == "doing")
        self.assertEqual(doing["count"], 2)
        entry = next(e for e in D.display_list(self.scene())["entries"] if e["id"] == doing["id"])
        counter = [p for p in entry["items"] if p.get("k") == "text" and p["lines"][0]["t"] == " · 2/1"]
        self.assertEqual(counter[0]["fill"], "tone.danger.text", "over its limit")
        problem = next(p for p in C.check(self.layout, self.team, "alpha-worker")["problems"] if p["code"] == "wip_exceeded")
        self.assertEqual(problem["fix"], {"op": "place", "id": "work.c4", "in": "work.to-do", "intent": problem["fix"]["intent"]})
        self.ok(problem["fix"])
        self.assertNotIn("wip_exceeded", [p["code"] for p in C.check(self.layout, self.team, "alpha-worker")["problems"]])

    def test_cards_are_clamped_and_readback_nests_them(self):
        self.ok(dict(WORK, columns=[{"id": "todo", "title": "Todo", "cards": [{"title": "t " * 60, "body": "b " * 200}]}]))
        card = next(e for e in self.elements() if e.get("type") == "card")
        self.assertEqual(card["clamp"], {"title": 2, "body": 3})
        self.assertTrue(card["fit"]["truncated"])
        root = next(e for e in self.elements() if e.get("alias") == "work")
        spec = B.spec_of(self.elements(), root)
        self.assertNotIn("id", spec["columns"][0], "an id that is its title's slug is left out")
        self.assertEqual(spec["columns"][0]["title"], "Todo")
        self.assertEqual(spec["columns"][0]["cards"][0]["title"], ("t " * 60).strip())

    def test_limits(self):
        self.assertEqual(self.refused(dict(WORK, columns=[{"title": str(i)} for i in range(13)]))["code"], "canvas_limit")
        refused = self.refused({"op": "patch", "id": "work", "add": {"cards": [{"title": "x", "in": "nowhere"}]}, "intent": "t"}) \
            if self.ok(WORK) else None
        self.assertEqual(refused["details"]["field"], "cards[4].in")
