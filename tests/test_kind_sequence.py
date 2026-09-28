"""Sequence diagrams (canvas v2 phase 2, 4.7.3): one element holding participants, messages, notes and groups; geometry,
drawing, parts and part edits, patches, and the round trip (T-B1)."""
from __future__ import annotations

import unittest

from test_kind_graph import GraphRig

from herdr_team import canvas as C
from herdr_team import canvas_blocks as B
from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R
from herdr_team.canvas_kinds import sequence as S

LOGIN = {"op": "sequence", "id": "login", "title": "Login", "at": [0, 0], "intent": "how login works",
         "participants": [{"id": "u", "text": "User", "icon": "user"}, {"id": "api", "text": "API"}, {"id": "db", "text": "DB", "icon": "database"}],
         "messages": ["u -> api: POST /login", "api -> db: find user", "db --> api: row", "api -> api: check hash", "api --> u: 200 + token"],
         "notes": [{"over": ["api"], "text": "rate limited"}],
         "groups": [{"kind": "alt", "label": "password ok", "from": 3, "to": 5}]}


class Sequence(GraphRig):
    def test_one_element_with_its_items_sized_from_its_geometry(self):
        applied = self.ok(LOGIN)
        [eid] = applied["ids"]
        el = self.el(eid)
        self.assertEqual(el["type"], "sequence")
        self.assertEqual([m["id"] for m in el["messages"]], ["m1", "m2", "m3", "m4", "m5"])
        self.assertTrue(el["messages"][2]["reply"])
        geo = S.geometry(el)
        self.assertEqual((el["w"], el["h"]), (int(round(geo["w"] + 0.49)), int(round(geo["h"] + 0.49))))
        xs = [(h["box"][0] + h["box"][2] / 2.0) for h in geo["heads"]]
        self.assertTrue(all(b - a >= S.COL_MIN for a, b in zip(xs, xs[1:])))
        self.assertTrue(geo["rows"][3]["self"])
        self.assertEqual(len(geo["rows"][3]["points"]), 4)
        self.assertEqual(geo["feet"], [], "five messages: no footer")

    def test_drawing_and_parts(self):
        self.ok(LOGIN)
        entry = next(e for e in D.display_list(self.scene())["entries"] if e["kind"] == "sequence")
        kinds = [item["k"] for item in entry["items"]]
        self.assertEqual(kinds.count("arrow"), 5)
        self.assertIn("group", kinds, "icons")
        parts = [p["part"] for p in entry["parts"]]
        self.assertEqual(parts, ["p.u", "p.api", "p.db", "m1", "m2", "m3", "m4", "m5", "n1"])
        self.assertTrue(all(p["edit"]["part"] == p["part"] for p in entry["parts"]))
        self.assertEqual(D.validate(D.display_list(self.scene())), [])

    def test_part_edits_and_patches(self):
        [eid] = self.ok(LOGIN)["ids"]
        self.assertEqual(S.part_edit(self.el(eid), "m2", "look up"), {"update": {"messages": [{"id": "m2", "text": "look up"}]}})
        self.ok({"op": "edit", "id": "login", "part": "p.db", "text": "Postgres", "intent": "t"})
        self.assertEqual(self.el(eid)["participants"][2]["text"], "Postgres")
        self.ok({"op": "patch", "id": "login", "add": {"messages": ["u -> api: logout"]}, "remove": {"notes": ["n1"]}, "intent": "t"})
        el = self.el(eid)
        self.assertEqual([m["id"] for m in el["messages"]][-1], "m6")
        self.assertEqual(el["notes"], [])
        refused = self.refused({"op": "sequence", "participants": ["a"], "messages": ["a -> z: hi"], "intent": "t"})
        self.assertEqual(refused["details"]["field"], "messages[0].to")
        self.assertEqual(self.refused({"op": "sequence", "participants": ["a"], "messages": ["a => a"], "intent": "t"})["details"]["field"],
                         "messages[0]")

    def test_many_messages_repeat_the_participants_and_the_round_trip_holds(self):
        long = {"op": "sequence", "id": "deploy", "at": [2000, 0], "intent": "t", "participants": ["ci", "cluster"],
                "messages": ["ci -> cluster: step {}".format(i) for i in range(10)]}
        [eid] = self.ok(long)["ids"]
        self.assertEqual(len(S.geometry(self.el(eid))["feet"]), 2)
        kctx = C._KindCtx(None)
        kind = R.get("sequence")
        for op in (LOGIN, long):
            self.ok(op) if op is LOGIN else None
            self.assertEqual(B.comparable(B.normalized(kctx, kind, self.spec(op["id"]))), B.comparable(B.normalized(kctx, kind, op)))
        self.assertIn("u -> api: POST /login", self.spec("login")["messages"])


if __name__ == "__main__":
    unittest.main()
