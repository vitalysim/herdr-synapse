"""Pins and place (canvas v2 phase 2, D5, D6 and 5.1): a pin is never moved by a layout, by growth push-out or by
another author; ``place`` moves elements as one group; and the property T-P3: random agent batches never change the box
of an operator-authored element or of any element a person pinned."""
from __future__ import annotations

import random

from test_canvas import OPERATOR, WORKER, CanvasRig

from herdr_team import canvas as C


class Pins(CanvasRig):
    def test_pin_and_unpin_record_who(self):
        eid = self.ok({"op": "shape", "text": "a", "at": [0, 0], "intent": "t"})["ids"][0]
        applied = self.ok({"op": "pin", "id": eid, "intent": "hold"})
        self.assertEqual(self.el(eid)["pin"], {"by": "agent", "who": "alpha-worker"})
        self.assertEqual(applied["block"], {"pinned": [eid]})
        self.ok({"op": "unpin", "id": eid, "intent": "let go"})
        self.assertIsNone(self.el(eid).get("pin"))

    def test_an_agent_cannot_lift_or_take_over_a_persons_pin(self):
        eid = self.ok({"op": "shape", "text": "a", "at": [0, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "pin", "id": eid, "intent": "t"}, author=OPERATOR)
        self.assertEqual(self.el(eid)["pin"]["by"], "human")
        self.assertEqual(self.refused({"op": "unpin", "id": eid, "intent": "t"})["code"], "pin_held")
        result = self.apply([{"op": "pin", "id": eid, "intent": "t"}])
        self.assertEqual(self.el(eid)["pin"]["by"], "human", "an agent's pin never replaces a person's")
        self.assertIn("pin_held", [w["code"] for w in result["warnings"]])
        for op in ({"op": "move", "id": eid, "by": [20, 0]}, {"op": "place", "id": eid, "at": [400, 400]}, {"op": "delete", "id": eid},
                   {"op": "move", "id": eid, "w": 400}):
            with self.subTest(op=op["op"]):
                self.assertEqual(self.refused(dict(op, intent="t"))["code"], "pin_held")
        self.ok({"op": "move", "id": eid, "by": [20, 0], "intent": "t"}, author=OPERATOR)

    def test_a_moved_pin_stays_pinned_by_its_mover(self):
        eid = self.ok({"op": "shape", "text": "a", "at": [0, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "pin", "id": eid, "intent": "t"})
        self.ok({"op": "place", "id": eid, "at": [200, 200], "intent": "t"}, author=OPERATOR)
        self.assertEqual(self.el(eid)["pin"], {"by": "human", "who": "human"})
        self.assertEqual((self.el(eid)["x"], self.el(eid)["y"]), (200, 200))

    def test_a_moved_member_of_a_positional_block_is_pinned_by_the_mover(self):
        self.ok({"op": "timeline", "id": "plan", "at": [0, 0], "intent": "t",
                 "events": [{"at": "2026-10-05", "title": "Beta"}, {"at": "2026-10-20", "title": "Freeze"}]})
        root = next(e for e in self.scene()["elements"] if e.get("alias") == "plan")
        beta = next(e for e in self.scene()["elements"] if e.get("part") == "e1")
        self.ok({"op": "move", "id": beta["id"], "by": [0, -40], "intent": "t"}, author=OPERATOR)
        moved = self.el(beta["id"])
        self.assertEqual(moved["pin"]["by"], "human")
        self.assertEqual(moved["y"], beta["y"] - 40, "a drag pins it where it went; nothing re-lays it out")
        # A structural change re-lays out the others; the pinned event holds.
        self.ok({"op": "patch", "id": "plan", "add": {"events": [{"at": "2026-10-10", "title": "More"}]}, "intent": "t"})
        self.assertEqual((self.el(beta["id"])["x"], self.el(beta["id"])["y"]), (moved["x"], moved["y"]))
        self.assertGreaterEqual(self.el(root["id"])["updated_seq"], moved["updated_seq"])


class Place(CanvasRig):
    def test_several_ids_move_as_one_group_beside_a_reference(self):
        ref = self.ok({"op": "shape", "text": "reference", "at": [0, 0], "w": 200, "h": 200, "intent": "t"})["ids"][0]
        a = self.ok({"op": "shape", "text": "a", "at": [1000, 0], "intent": "t"})["ids"][0]
        b = self.ok({"op": "shape", "text": "b", "at": [1000, 200], "intent": "t"})["ids"][0]
        offset = self.el(b)["y"] - self.el(a)["y"]
        self.ok({"op": "place", "ids": [a, b], "right_of": ref, "gap": "l", "intent": "t"})
        self.assertEqual(self.el(a)["x"], self.el(ref)["x"] + self.el(ref)["w"] + 80)
        self.assertEqual(self.el(a)["y"], self.el(ref)["y"], "align start: the tops line up")
        self.assertEqual(self.el(b)["y"] - self.el(a)["y"], offset, "the group keeps its offsets")
        self.ok({"op": "place", "ids": [a, b], "below": ref, "align": "center", "intent": "t"})
        group_w = max(self.el(a)["x"] + self.el(a)["w"], self.el(b)["x"] + self.el(b)["w"]) - min(self.el(a)["x"], self.el(b)["x"])
        self.assertAlmostEqual(min(self.el(a)["x"], self.el(b)["x"]) + group_w / 2.0, self.el(ref)["x"] + 100, delta=1)

    def test_place_takes_exactly_one_placement(self):
        eid = self.ok({"op": "shape", "text": "a", "at": [0, 0], "intent": "t"})["ids"][0]
        self.assertEqual(self.refused({"op": "place", "id": eid, "intent": "t"})["details"]["field"], "right_of")
        self.assertEqual(self.refused({"op": "place", "id": eid, "at": [0, 0], "below": eid, "intent": "t"})["code"], "op_invalid")
        self.assertEqual(self.refused({"op": "place", "id": eid, "at": [0, 0], "index": 1, "intent": "t"})["details"]["field"], "index")

    def test_place_in_a_plain_frame_makes_it_a_child(self):
        frame = self.ok({"op": "frame", "title": "F", "at": [0, 0], "w": 600, "h": 400, "intent": "t"})["ids"][0]
        eid = self.ok({"op": "shape", "text": "a", "at": [2000, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "place", "id": eid, "in": frame, "intent": "t"})
        self.assertEqual(self.el(eid)["frame"], frame)


BOX_KEYS = ("x", "y", "w", "h")


class PinProperty(CanvasRig):
    """T-P3 (seeded, 500 batches): whatever an agent does, the operator's elements and every element a person pinned keep
    their boxes."""

    SEED = 2209
    BATCHES = 500

    def setUp(self):
        super().setUp()
        rng = random.Random(self.SEED)
        self.rng = rng
        # The operator's own elements, and an agent's block whose members the operator pinned.
        ops = [{"op": "shape", "text": "operator {}".format(i), "at": [i * 300, 1200], "intent": "t"} for i in range(4)]
        self.operator_ids = [i for entry in self.apply(ops, author=OPERATOR)["applied"] for i in entry["ids"]]
        self.ok({"op": "timeline", "id": "plan", "at": [0, 0], "intent": "t",
                 "events": [{"at": "2026-10-{:02d}".format(d), "title": "Event {}".format(d)} for d in (3, 8, 13, 18, 23)]})
        self.ok({"op": "kanban", "id": "work", "at": [0, 600], "intent": "t",
                 "columns": [{"id": "todo", "title": "Todo", "cards": ["A", "B"]}, {"id": "done", "title": "Done", "cards": ["C"]}]})
        self.ok({"op": "section", "id": "free", "title": "Free", "at": [1400, 0], "intent": "t"})
        events = [e for e in self.scene()["elements"] if e.get("type") == "card" and (e.get("alias") or "").startswith("plan.")]
        for event in events[:2]:
            self.ok({"op": "move", "id": event["id"], "by": [0, -20], "intent": "t"}, author=OPERATOR)
        loose = self.ok({"op": "shape", "text": "agent's but pinned", "at": [1400, 600], "intent": "t"})["ids"][0]
        self.ok({"op": "pin", "id": loose, "intent": "t"}, author=OPERATOR)
        self.pinned_ids = [e["id"] for e in events[:2]] + [loose]

    def boxes(self):
        by_id = {e["id"]: e for e in self.scene()["elements"]}
        return {eid: tuple(by_id[eid][k] for k in BOX_KEYS) for eid in self.operator_ids + self.pinned_ids if eid in by_id}

    def random_op(self):
        rng = self.rng
        scene = self.scene()["elements"]
        mine = [e for e in scene if e.get("author") == "alpha-worker" and e.get("type") != "comment"]
        target = rng.choice(mine) if mine else None
        choice = rng.randrange(12)
        if choice == 0 or target is None:
            return {"op": "shape", "text": "grow " * rng.randint(1, 30), "at": [rng.randint(-2, 60) * 20, rng.randint(-2, 60) * 20], "intent": "t"}
        if choice == 1:
            return {"op": "patch", "id": "plan", "add": {"events": [{"at": "2026-10-{:02d}".format(rng.randint(1, 28)), "title": "E" * rng.randint(1, 40)}]},
                    "intent": "t"}
        if choice == 2:
            return {"op": "patch", "id": "work", "add": {"cards": [{"title": "card " * rng.randint(1, 12), "in": rng.choice(["todo", "done"])}]},
                    "relayout": rng.choice(["incremental", "full"]), "intent": "t"}
        if choice == 3:
            return {"op": "move", "id": target["id"], "by": [rng.randint(-10, 10) * 20, rng.randint(-10, 10) * 20], "intent": "t"}
        if choice == 4:
            return {"op": "place", "id": target["id"], "right_of": rng.choice(self.operator_ids + self.pinned_ids), "intent": "t"}
        if choice == 5:
            return {"op": "edit", "id": target["id"], "text": "longer text " * rng.randint(1, 20), "intent": "t"}
        if choice == 6:
            return {"op": "move", "id": target["id"], "w": rng.randint(4, 40) * 20, "h": rng.randint(2, 30) * 20, "intent": "t"}
        if choice == 7:
            return {"op": "unpin", "ids": [rng.choice(self.pinned_ids)], "relayout": "full", "intent": "t"}
        if choice == 8:
            return {"op": "card", "in": "free", "title": "in free " * rng.randint(1, 10), "intent": "t"}
        if choice == 9:
            return {"op": "timeline", "id": "plan", "intent": "t",
                    "events": [{"at": "2026-10-{:02d}".format(d), "title": "Event {}".format(d)} for d in rng.sample(range(1, 28), rng.randint(1, 6))]}
        if choice == 10:
            return {"op": "move", "ids": [e["id"] for e in rng.sample(mine, min(len(mine), 3))], "by": [rng.randint(-5, 5) * 20, 0], "intent": "t"}
        return {"op": "delete", "id": target["id"], "with_children": rng.random() < 0.5, "intent": "t"}

    def test_agents_never_move_the_operators_marks_or_a_persons_pins(self):
        import time

        self.start = time.time() + 120
        before = self.boxes()
        self.assertEqual(len(before), len(self.operator_ids) + len(self.pinned_ids))
        for number in range(self.BATCHES):
            batch = [self.random_op() for _ in range(self.rng.randint(1, 3))]
            # One batch a second (the member's rate limit is per minute).
            C.apply_ops(self.layout, self.team, batch, WORKER, now=self.start + number)
            now = self.boxes()
            for eid, box in before.items():
                if eid in now:
                    self.assertEqual(now[eid], box, "batch {} {} moved {}".format(number, batch, eid))
