"""The ``refit`` op and the page's text corrections (canvas v2 phase 1, 2.6 and 3.3; T-M1 to T-M5 of
``.local/prd/canvas-v2-phase1.md`` 5.1): old boards are sized again (QA R-9), authority holds, arrow labels are placed
again, a refit only ever grows a box, and ``look`` and ``check`` measure under the same corrections."""
from __future__ import annotations

import json
import random

from test_canvas import OPERATOR, REVIEWER, WORKER, CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_display as D
from herdr_team import canvas_text as X
from herdr_team import store

LEGACY_STYLE = {"stroke": "#1e1e1e", "fill": None, "width": 2, "dash": "solid", "opacity": 100, "font": "normal", "size": 20, "rough": 1}


def legacy(eid, kind="box", **fields):
    """An element as a board from before 0.22 stored it: no tone, no fit, its size as drawn then."""
    el = {"id": eid, "type": kind, "alias": None, "client_id": None, "x": 0, "y": 0, "w": 80, "h": 40, "text": "", "style": dict(LEGACY_STYLE),
          "frame": None, "group": None, "role": None, "z": int(eid.split("-")[1]), "author": "alpha-worker", "author_kind": "member",
          "intent": "old", "batch": "B-1", "created_seq": 1, "updated_seq": 1, "created_at": "2026-01-01T00:00:00.000Z",
          "updated_at": "2026-01-01T00:00:00.000Z"}
    el.update(fields)
    return el


class Refit(CanvasRig):
    def old_board(self, *elements):
        """Write a board the way an older release left it: one event adding the elements."""
        author = {"color": "#1971c2", "index": 0, "kind": "member", "agent": "claude"}
        changes = [{"target": "author", "action": "add", "id": "alpha-worker", "value": author}]
        changes += [{"target": "element", "action": "add", "id": el["id"], "value": el} for el in elements]
        event = {"v": 1, "seq": 1, "ts": "2026-01-01T00:00:00.000Z", "batch": "B-1", "op": "shape", "index": 0, "intent": "old",
                 "author": {"name": "alpha-worker", "kind": "member", "agent": "claude", "via": "cli", "verified": True},
                 "ids": [el["id"] for el in elements], "changes": changes}
        path = C._file(self.team, C.EVENTS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    def overflow(self):
        return [p for p in C.check(self.layout, self.team, "alpha-worker")["problems"] if p["code"] == "label_overflow"]

    def test_an_old_board_is_sized_again(self):
        # T-M1 (QA R-9): boards stored before 0.22 keep their old sizes until refit.
        self.old_board(legacy("E-1", text="A label far too long for its little box"), legacy("E-2", "note", x=300, w=180, h=120, text="short"))
        self.assertEqual([p["ids"][0] for p in self.overflow()], ["E-1"])
        applied = self.ok({"op": "refit", "intent": "size the old board again"})
        self.assertEqual(applied["ids"], ["E-1", "E-2"])
        box = self.el("E-1")
        self.assertGreater((box["w"], box["h"]), (80, 40))
        self.assertEqual(box["fit"]["min"], [80, 40], "the author's size stays the minimum")
        self.assertEqual(self.overflow(), [])
        self.assertEqual(applied["geometry"][0]["id"], "E-1", "the result says how big it grew, as shape does")

    def test_authority_and_if_version(self):
        # T-M2
        mine = self.ok({"op": "shape", "kind": "box", "text": "mine", "at": [0, 0], "intent": "t"})["ids"][0]
        # Phase 5: a peer's refit that would change the mark is a proposal; this one has nothing to grow, so it does nothing.
        self.assertEqual(self.apply([{"op": "refit", "id": mine, "intent": "t"}], REVIEWER)["applied"][0]["ids"], [])
        seq = self.el(mine)["updated_seq"]
        self.assertEqual(self.refused({"op": "refit", "id": mine, "if_version": seq + 1, "intent": "t"})["code"], "canvas_stale")
        self.ok({"op": "refit", "id": mine, "if_version": seq, "intent": "t"})
        theirs = self.ok({"op": "shape", "kind": "box", "text": "theirs", "at": [400, 0], "intent": "t"}, REVIEWER)["ids"][0]
        self.assertNotIn(theirs, self.ok({"op": "refit", "intent": "t"})["ids"], "a whole-canvas refit takes only what the author may edit")
        self.assertEqual(self.refused({"op": "refit", "ids": ["E-1"] * (C.MAX_REFIT + 1), "intent": "t"})["code"], "canvas_limit")
        self.ok({"op": "refit", "ids": [mine, theirs]}, OPERATOR)

    def test_an_arrow_label_is_placed_again(self):
        # T-M3
        arrow = legacy("E-3", "arrow", text="depends on", points=[[0, 0], [300, 0]], x=0, y=0, w=300, h=1, head="arrow", tail="none",
                       curve=False, **{"from": None, "to": None})
        self.old_board(arrow)
        self.assertIsNone(self.el("E-3").get("label_at"))
        self.ok({"op": "refit", "id": "E-3", "intent": "t"})
        placed = self.el("E-3")
        self.assertEqual(placed["fit"]["lines"], ["depends on"])
        self.assertEqual(len(placed["label_at"]), 2)

    def test_a_refit_only_ever_grows(self):
        # T-M4: random corrections, random labels: no element comes out smaller.
        rng = random.Random(1301)
        words = ("API", "checkout", "orders", "payments", "Internationalization", "中文", "p99", "service")
        ops = []
        for index in range(24):
            kind = rng.choice(("box", "note", "ellipse", "diamond", "text"))
            ops.append({"op": "shape", "kind": kind, "text": " ".join(rng.choice(words) for _ in range(rng.randint(1, 5))),
                        "at": [(index % 6) * 400, (index // 6) * 300], "intent": "t"})
        self.apply(ops)
        before = {el["id"]: (el["w"], el["h"]) for el in self.scene()["elements"]}
        doc = D.display_list(self.scene())
        table = {}
        for entry in doc["entries"]:
            for prim in entry["items"]:
                for line in prim.get("lines") or []:
                    if rng.random() < 0.6:
                        key = X.correction_key(prim["font"], prim["weight"], line["t"])
                        table[key] = line["w"] / prim["size"] * rng.uniform(1.0, 2.5)
        store.write_json(C._file(self.team, C.MEASURE_FILE), {"v": 1, "metrics": C.metrics_sha(), "em": table})
        self.ok({"op": "refit", "intent": "t"})
        for el in self.scene()["elements"]:
            with self.subTest(id=el["id"]):
                self.assertGreaterEqual(el["w"], before[el["id"]][0])
                self.assertGreaterEqual(el["h"], before[el["id"]][1])

    def test_look_and_check_measure_under_the_corrections(self):
        # T-M5
        eid = self.ok({"op": "shape", "kind": "box", "text": "Checkout API", "at": [0, 0], "h": 40, "intent": "t"})["ids"][0]
        self.assertEqual(self.overflow(), [])
        width = X.measure("Checkout API").width
        store.write_json(C._file(self.team, C.MEASURE_FILE),
                         {"v": 1, "metrics": C.metrics_sha(), "em": {X.correction_key("sans", 500, "Checkout API"): width * 2.2 / 20}})
        self.assertEqual([p["ids"][0] for p in self.overflow()], [eid], "check measures what the page measured")
        looked = C.look(self.layout, self.team, "alpha-worker")
        self.assertIn("label_overflow", [p["code"] for p in looked["problems"]], "and so does look")
        first = D.dumps(C.display(self.team))
        C._DISPLAY_CACHE.clear()
        self.assertEqual(first, D.dumps(C.display(self.team)), "the display list is a function of the scene and measure.json")
        self.ok({"op": "refit", "id": eid, "intent": "t"})
        self.assertEqual(self.overflow(), [])
        store.write_json(C._file(self.team, C.MEASURE_FILE), {"v": 1, "metrics": "0" * 64, "em": {}})
        self.assertEqual(self.overflow(), [], "without corrections the grown box still fits")
