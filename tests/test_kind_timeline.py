"""The ``timeline`` kind (canvas v2 phase 2, 4.6): a time or ordinal axis, packed levels, ticks, spans and milestones."""
from __future__ import annotations

from test_canvas import CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_display as D
from herdr_team.canvas_kinds import timeline as K

PLAN = {"op": "timeline", "id": "plan", "title": "Launch plan", "at": [0, 0], "intent": "t",
        "events": [{"at": "2026-10-05", "title": "Beta"}, {"at": "2026-10-20", "title": "Docs freeze", "tone": "warning"},
                   {"at": "2026-11-03", "title": "Launch", "tone": "success", "icon": "rocket", "milestone": True},
                   {"at": "2026-10-06", "end": "2026-10-24", "title": "Private beta"}]}


class Timeline(CanvasRig):
    def root(self):
        return next(e for e in self.scene()["elements"] if e.get("alias") == "plan")

    def events(self):
        root = self.root()
        return sorted((e for e in self.scene()["elements"] if e.get("group") == root["id"]), key=lambda e: e["part"])

    def test_events_pack_above_and_below_a_time_axis(self):
        self.ok(PLAN)
        root = self.root()
        axis = root["axis"]
        self.assertEqual((axis["mode"], axis["scale"]), ("time", "day"))
        events = self.events()
        above = [e for e in events if e["y"] + e["h"] <= axis["y"]]
        below = [e for e in events if e["y"] >= axis["y"]]
        self.assertTrue(above and below, "levels alternate sides")
        for a in events:
            for b in events:
                if a is not b:
                    self.assertFalse(C._intersects(C.bounds(a), C.bounds(b)), (a["part"], b["part"]))
        beta, freeze = events[0], events[1]
        self.assertLess(beta["x"], freeze["x"], "time maps along the axis")
        self.assertEqual(events[0]["w"], 200)

    def test_marks_spans_and_milestones_are_drawn(self):
        self.ok(PLAN)
        entry = next(e for e in D.display_list(self.scene())["entries"] if e["id"] == self.root()["id"])
        kinds = [p["k"] for p in entry["items"]]
        self.assertIn("poly", kinds, "the milestone diamond")
        spans = [p for p in entry["items"] if p.get("k") == "rect" and p.get("h") == K.SPAN_H]
        self.assertEqual(len(spans), 1)
        labels = [p["lines"][0]["t"] for p in entry["items"] if p.get("k") == "text"]
        self.assertTrue(any(t.startswith("Oct") for t in labels), labels)

    def test_undated_events_run_in_order_and_mixing_warns(self):
        self.ok(dict(PLAN, events=[{"at": "Kickoff", "title": "A"}, {"at": "Later", "title": "B"}]))
        self.assertEqual(self.root()["axis"]["mode"], "ordinal")
        result = self.apply([dict(PLAN, events=[{"at": "2026-10-01", "title": "A"}, {"at": "soon", "title": "B"}])])
        self.assertIn("date_unparsed", [w["code"] for w in result["warnings"]])

    def test_moving_the_timeline_moves_its_axis(self):
        self.ok(PLAN)
        before = self.root()["axis"]
        self.ok({"op": "move", "id": "plan", "by": [100, 40], "intent": "t"})
        after = self.root()["axis"]
        self.assertEqual((after["x0"], after["y"]), (before["x0"] + 100, before["y"] + 40))
        self.assertEqual(after["marks"][0]["x"], before["marks"][0]["x"] + 100)

    def test_parse_date(self):
        self.assertEqual(str(K.parse_date("2026-10")), "2026-10-01")
        self.assertIsNone(K.parse_date("2026-13-01"))
        self.assertIsNone(K.parse_date("next week"))
