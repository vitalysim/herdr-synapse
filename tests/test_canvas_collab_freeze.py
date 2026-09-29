"""Freeze and thaw (canvas v2 phase 5, 5): region and id freezes, descendants, following an element, both settings, comments
allowed, delegates bound, the thaw forms, the settings op, and what the display list and ``look`` say."""
from __future__ import annotations

from collab_support import DEPUTY, LEAD, MANAGER, MEMBER, CollabRig

from herdr_team import canvas as C
from herdr_team import canvas_collab as K
from herdr_team import canvas_display as D


class Freeze(CollabRig):
    def setUp(self):
        super().setUp()
        self.frame = self.ok({"op": "frame", "title": "Mine", "at": [0, 0], "w": 400, "h": 300, "intent": "t"})["ids"][0]
        self.inner = self.ok({"op": "shape", "kind": "box", "text": "Inner", "inside": self.frame, "intent": "t"})["ids"][0]
        self.loose = self.ok({"op": "shape", "kind": "box", "text": "Loose", "at": [1000, 0], "intent": "t"})["ids"][0]

    def test_an_id_freeze_covers_the_element_and_what_it_holds(self):
        xid = self.ok({"op": "freeze", "ids": [self.frame], "label": "final layout"}, LEAD)["ids"][0]
        record = self.scene()["freezes"][0]
        self.assertEqual({k: record[k] for k in ("id", "kind", "region", "ids", "label", "by")},
                         {"id": xid, "kind": "freeze", "region": None, "ids": [self.frame], "label": "final layout", "by": "human"})
        found = self.proposed({"op": "edit", "id": self.inner, "text": "Changed", "intent": "t"})
        self.assertEqual((found["reason"], found["message"]), ("frozen", "{} is frozen ({}): the operator holds it as it is".format(self.inner, xid)))
        self.ok({"op": "edit", "id": self.loose, "text": "free", "intent": "t"})
        self.assertEqual(self.proposed({"op": "shape", "text": "new", "inside": self.frame, "intent": "t"})["reason"], "frozen")

    def test_a_region_freeze_covers_what_meets_it_and_what_is_added_in_it(self):
        self.ok({"op": "freeze", "region": [900, -100, 1400, 200], "label": "done"}, LEAD)
        self.assertEqual(self.proposed({"op": "move", "id": self.loose, "by": [0, 20], "intent": "t"})["reason"], "frozen")
        self.assertEqual(self.proposed({"op": "shape", "text": "in", "at": [1200, 100], "intent": "t"})["reason"], "frozen")
        self.assertEqual(self.proposed({"op": "move", "id": self.frame, "to": [960, 0], "intent": "t"})["reason"], "frozen",
                         "moving your mark into a frozen region")
        self.ok({"op": "shape", "text": "out", "at": [2000, 0], "intent": "t"})

    def test_an_id_freeze_follows_its_element(self):
        xid = self.ok({"op": "freeze", "ids": [self.loose], "label": "hold"}, LEAD)["ids"][0]
        entry = lambda: next(e for e in C.display(self.team)["entries"] if e["id"] == xid)  # noqa: E731
        before = entry()["bbox"]
        self.ok({"op": "move", "id": self.loose, "by": [200, 0]}, LEAD)
        after = entry()["bbox"]
        self.assertEqual(after[0] - before[0], 200)
        self.assertEqual(entry()["freeze"], {"region": None, "ids": [self.loose], "label": "hold", "mode": "propose"})

    def test_an_id_freeze_whose_marks_drift_apart_outlines_each(self):
        # QA phase 5 L9: not one huge dashed box between them; the label stays above the top-left corner.
        xid = self.ok({"op": "freeze", "ids": [self.frame, self.loose], "label": "hold"}, LEAD)["ids"][0]
        entry = lambda: next(e for e in C.display(self.team)["entries"] if e["id"] == xid)  # noqa: E731
        together = entry()
        self.assertEqual([i["k"] for i in together["items"]], ["rect", "group"], "close together: one outline")
        self.assertEqual(together["items"][-1]["screen"], [together["bbox"][2], together["bbox"][1]], "above the top-right corner")
        self.ok({"op": "move", "id": self.loose, "by": [3000, 3000]}, LEAD)
        apart = entry()
        self.assertEqual([i["k"] for i in apart["items"]], ["rect", "rect", "group"])
        self.assertEqual(apart["items"][-1]["items"][0]["lines"][0]["t"], "{} frozen: hold".format(xid))
        self.assertEqual(D.validate(C.display(self.team)), [])

    def test_refuse_mode_and_delegates_are_bound(self):
        self.ok({"op": "freeze", "ids": [self.frame]}, LEAD)
        self.ok({"op": "settings", "frozen": "refuse"}, LEAD)
        for author in (MEMBER, MANAGER, DEPUTY):
            refusal = self.refused({"op": "move", "id": self.inner, "by": [0, 20], "intent": "t"}, author)
            self.assertEqual((refusal["code"], refusal["details"]["freezes"]), ("frozen", ["X-1"]), author.name)
        self.ok({"op": "move", "id": self.inner, "by": [0, 20]}, LEAD)

    def test_comments_are_never_frozen(self):
        self.ok({"op": "freeze", "ids": [self.frame]}, LEAD)
        self.ok({"op": "settings", "frozen": "refuse"}, LEAD)
        self.ok({"op": "comment", "at": self.inner, "text": "why this size?", "intent": "t"})
        self.ok({"op": "freeze", "region": [3000, 3000, 3400, 3400]}, LEAD)
        self.ok({"op": "comment", "at": [3100, 3100], "text": "here?", "intent": "t"})

    def test_thaw_by_id_and_by_element(self):
        whole = self.ok({"op": "freeze", "ids": [self.frame, self.loose]}, LEAD)["ids"][0]
        region = self.ok({"op": "freeze", "region": [5000, 0, 5200, 200]}, LEAD)["ids"][0]
        self.ok({"op": "thaw", "ids": [self.loose]}, LEAD)
        self.assertEqual(next(f for f in self.scene()["freezes"] if f["id"] == whole)["ids"], [self.frame])
        self.ok({"op": "edit", "id": self.loose, "text": "free", "intent": "t"})
        self.ok({"op": "thaw", "ids": [self.frame]}, LEAD)
        self.assertEqual([f["id"] for f in self.scene()["freezes"]], [region], "a freeze left with no ids goes")
        self.ok({"op": "thaw", "id": region}, LEAD)
        self.assertEqual(self.scene()["freezes"], [])
        self.assertEqual(self.refused({"op": "thaw", "id": region}, LEAD)["code"], "element_unknown")
        self.assertEqual(self.refused({"op": "thaw", "ids": [self.loose]}, LEAD)["code"], "element_unknown")
        self.assertEqual(self.refused({"op": "freeze", "region": [0, 0, 1, 1], "ids": [self.loose]}, LEAD)["details"]["field"], "region")

    def test_freezes_and_locks_share_the_x_counter_and_an_undo_thaws(self):
        lock = self.ok({"op": "lock", "region": [4000, 0, 4100, 100]}, LEAD)["ids"][0]
        result = self.apply([{"op": "freeze", "ids": [self.loose]}], LEAD)
        self.assertEqual((lock, result["applied"][0]["ids"]), ("X-1", ["X-2"]))
        self.ok({"op": "undo", "batch": result["batch"]}, LEAD)
        self.assertEqual(self.scene()["freezes"], [])

    def test_the_display_list_and_look(self):
        self.ok({"op": "freeze", "ids": [self.frame], "label": "final layout"}, LEAD)
        self.ok({"op": "freeze", "region": [900, -100, 1400, 200], "label": "done"}, LEAD)
        doc = C.display(self.team)
        self.assertEqual(D.validate(doc), [])
        by_id = {e["id"]: e for e in doc["entries"]}
        self.assertTrue(by_id[self.frame]["frozen"] and by_id[self.inner]["frozen"] and by_id[self.loose]["frozen"])
        freeze = by_id["X-1"]
        self.assertEqual((freeze["kind"], freeze["layer"], freeze["z"], freeze["hit"]["shape"]), ("freeze", "overlays", -3, "frame"))
        self.assertEqual(freeze["items"][0]["stroke"], "tone.info.stroke")
        self.assertEqual(freeze["items"][1]["items"][0]["lines"][0]["t"], "X-1 frozen: final layout")
        look = C.look(self.layout, self.team, "alpha-member")
        self.assertIn('frozen: X-1 {} "final layout" (your changes become proposals); X-2 c45r-5:c70r10 "done" (your changes become proposals)'.format(
            self.frame), look["text"])
        self.assertIn(" [frozen]", look["text"])
        self.assertEqual(look["settings"], K.SETTINGS_DEFAULTS)


class Settings(CollabRig):
    def test_the_settings_op(self):
        self.assertEqual(self.scene()["settings"], {"collab": {"human_edits": "propose", "frozen": "propose"}})
        result = self.apply([{"op": "settings", "human_edits": "live"}], LEAD)
        self.assertEqual(self.scene()["settings"]["collab"], {"human_edits": "live", "frozen": "propose"})
        second = self.apply([{"op": "settings", "frozen": "refuse"}], LEAD)
        self.assertEqual(self.scene()["settings"]["collab"], {"human_edits": "live", "frozen": "refuse"})
        self.assertIn("#0 settings - · human edits: live · frozen: refuse", C.apply_text(second), "QA phase 5 L15: says what they are now")
        self.assertEqual(self.refused({"op": "settings"}, LEAD)["details"]["field"], "human_edits")
        self.assertEqual(self.refused({"op": "settings", "human_edits": "maybe"}, LEAD)["details"]["field"], "human_edits")
        # It is undoable like any batch; an older setting changed later since is skipped like any element (an undo that
        # would take nothing back is refused, saying so).
        self.ok({"op": "undo", "batch": second["batch"]}, LEAD)
        self.assertEqual(self.scene()["settings"]["collab"], {"human_edits": "live", "frozen": "propose"})
        self.apply([{"op": "settings", "frozen": "refuse"}], LEAD)
        refusal = self.refused({"op": "undo", "batch": result["batch"]}, LEAD)
        self.assertEqual(refusal["details"]["skipped"][0]["id"], "collab")
        look = C.look(self.layout, self.team, "human")
        self.assertIn("settings: your marks: live with revert · frozen: refused", look["text"])
