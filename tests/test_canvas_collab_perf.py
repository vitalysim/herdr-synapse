"""Canvas v2 phase 5 performance, as 5x bounds (G9): the numbers themselves come from ``tools/canvas_qa.py --perf collab``."""
from __future__ import annotations

import time
from unittest import mock

from collab_support import LEAD, MEMBER, PEER, CollabRig

from herdr_team import canvas as C
from herdr_team import canvas_collab as K
from herdr_team import canvas_presence as P
from herdr_team import sketch as S


class Bounds(CollabRig):
    def test_the_gate_the_presence_write_and_look(self):
        self.apply([{"op": "shape", "kind": "box", "text": "Hers {}".format(n), "at": [n * 200, 0]} for n in range(30)], LEAD)
        mine = self.ok({"op": "shape", "kind": "box", "text": "Mine", "at": [0, 400], "intent": "t"})["ids"][0]
        self.ok({"op": "freeze", "region": [0, 800, 400, 1000]}, LEAD)
        spent = []
        real = K.review_op

        def timed(ctx, name, raised):
            started = time.perf_counter()
            try:
                real(ctx, name, raised)
            finally:
                spent.append(time.perf_counter() - started)

        with mock.patch.object(K, "review_op", timed):
            for n in range(10):
                self.ok({"op": "restyle", "id": mine, "tone": ("info", "success")[n % 2], "intent": "t"})
        self.assertLess(sorted(spent)[len(spent) // 2], 0.015, "the gate: 3 ms p50 budget, 5x")
        P.write_member(self.team, MEMBER, status="drawing", intent="warm")
        started = time.perf_counter()
        for _ in range(20):
            P.write_member(self.team, MEMBER, status="drawing", region=[0, 0, 100, 100], intent="perf")
        self.assertLess((time.perf_counter() - started) / 20, 0.010, "a presence write: 2 ms budget, 5x")

    def test_sketch_sends_a_base(self):
        sketch = S.Sketch(default_intent="t")
        sketch.shape("box", "x")
        self.assertNotIn("base", sketch.batch())
        sketch.base = "last"
        self.assertEqual(sketch.batch()["base"], "last")
        self.assertEqual(C.parse_envelope(sketch.to_json()).base, "last")
