"""Anchored comments (canvas v2 phase 5, 8): a comment on an element follows it when it moves or resizes, replies too;
a comment stored before anchors derives one; an orphan keeps its point and says where it was; following never proposes."""
from __future__ import annotations

from collab_support import LEAD, MEMBER, PEER, CollabRig

from herdr_team import canvas as C


class Follow(CollabRig):
    def setUp(self):
        super().setUp()
        self.box = self.ok({"op": "shape", "kind": "box", "text": "Pricing", "at": [0, 0], "w": 200, "h": 100, "intent": "t"})["ids"][0]
        self.comment = self.ok({"op": "comment", "at": self.box, "text": "why this price?", "intent": "t"})["ids"][0]

    def test_a_new_comment_sits_on_the_top_right_corner(self):
        comment = self.el(self.comment)
        self.assertEqual((comment["anchor"], comment["point"], comment["on"]), ([1, 0], [200, 0], self.box))

    def test_it_follows_a_move_and_a_resize(self):
        result = self.apply([{"op": "move", "id": self.box, "by": [100, 40], "intent": "t"}])
        self.assertIn(self.comment, result["applied"][0]["ids"])
        self.assertEqual(self.el(self.comment)["point"], [300, 40])
        self.ok({"op": "move", "id": self.box, "w": 300, "intent": "t"})
        self.assertEqual(self.el(self.comment)["point"], [400, 40])

    def test_it_follows_a_frame_move(self):
        frame = self.ok({"op": "frame", "title": "Page", "children": [self.box], "intent": "t"})["ids"][0]
        self.ok({"op": "move", "id": frame, "by": [0, 200], "intent": "t"})
        self.assertEqual(self.el(self.comment)["point"], [200, 200])

    def test_replies_follow_with_their_offset(self):
        reply = self.ok({"op": "comment", "at": self.comment, "text": "because costs rose", "intent": "t"}, PEER)["ids"][0]
        found = self.el(reply)
        self.assertEqual((found["on"], found["point"], found["anchor"]), (self.box, [216, 16], [1, 0.16, 16, 0]))
        self.ok({"op": "move", "id": self.box, "by": [100, 0], "intent": "t"})
        self.assertEqual(self.el(reply)["point"], [316, 16])

    def test_a_comment_moved_on_purpose_keeps_its_new_place_on_the_element(self):
        self.ok({"op": "move", "id": self.comment, "by": [-100, 50], "intent": "t"})
        self.assertEqual(self.el(self.comment)["anchor"], [0.5, 0.5])
        self.ok({"op": "move", "id": self.box, "by": [40, 0], "intent": "t"})
        self.assertEqual(self.el(self.comment)["point"], [140, 50])

    def test_a_comment_stored_before_anchors_derives_one(self):
        state = C._load_state(self.team)
        legacy = dict(state.elements[self.comment], point=[100, 0])
        legacy.pop("anchor")
        store_scene = state.to_scene()
        store_scene["elements"] = [legacy if e["id"] == self.comment else e for e in store_scene["elements"]]
        # Write the legacy comment through the log so the next batch reads it.
        C._append_events(self.team, [{"v": 1, "seq": state.version + 1, "ts": C._iso(0), "batch": None, "author": {"name": "human", "kind": "human"},
                                      "op": "edit", "index": 0, "intent": "legacy", "ids": [self.comment],
                                      "changes": [{"target": "element", "action": "update", "id": self.comment, "value": legacy}]}])
        C._write_scene(self.team, C._load_state(self.team), 0)
        self.ok({"op": "move", "id": self.box, "by": [0, 100], "intent": "t"})
        found = self.el(self.comment)
        self.assertEqual((found["anchor"], found["point"]), ([0.5, 0], [100, 100]))

    def test_an_orphan_keeps_its_point_and_says_where_it_was(self):
        self.ok({"op": "delete", "id": self.box, "intent": "t"})
        comment = self.el(self.comment)
        self.assertEqual((comment["on"], comment["was_on"], comment["point"]), (None, self.box, [200, 0]))
        self.assertIn("(was on {}, deleted)".format(self.box), C.look(self.layout, self.team, "alpha-member")["text"])

    def test_following_the_operators_mark_never_proposes(self):
        hers = self.ok({"op": "shape", "kind": "box", "text": "Hers", "at": [600, 0]}, LEAD)["ids"][0]
        mine = self.ok({"op": "comment", "at": hers, "text": "a question", "intent": "t"})["ids"][0]
        theirs = self.ok({"op": "comment", "at": self.box, "text": "peer note", "intent": "t"}, PEER)["ids"][0]
        self.ok({"op": "move", "id": hers, "by": [0, 60]}, LEAD)
        self.assertEqual(self.el(mine)["point"], [760, 60])
        result = self.apply([{"op": "move", "id": self.box, "by": [0, 60], "intent": "t"}])
        self.assertEqual((len(result["applied"]), result["proposed"]), (1, []), "a peer's comment follows the member's own move")
        self.assertEqual(self.el(theirs)["point"], [200, 60])

    def test_answered_is_derived_in_look(self):
        asking = self.ok({"op": "comment", "at": self.box, "text": "@alpha-peer is this right?", "intent": "t"})["ids"][0]
        self.ok({"op": "comment", "at": asking, "text": "yes", "intent": "t"}, PEER)
        look = C.look(self.layout, self.team, "alpha-member")
        line = next(line for line in look["text"].splitlines() if line.strip().startswith(asking))
        self.assertTrue(line.endswith("(answered)"), line)
        self.assertTrue(next(e for e in look["elements"] if e["id"] == asking)["answered"])
        self.assertNotIn("answered", self.el(asking), "never stored")
