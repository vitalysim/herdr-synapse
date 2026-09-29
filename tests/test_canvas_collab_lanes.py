"""Lanes (canvas v2 phase 5, 4.1): sliding claim renewals, automatic claims made and grown, the release order, and the
manager and delegates exempt from other lanes."""
from __future__ import annotations

import time

from collab_support import DEPUTY, LEAD, MANAGER, MEMBER, PEER, CollabRig

from herdr_team import canvas as C


class Renewals(CollabRig):
    def test_a_live_op_in_a_claim_renews_it_once_half_its_time_is_gone(self):
        now = time.time()
        claim = self.ok({"op": "claim", "region": [0, 0, 800, 400], "label": "mine", "intent": "t"}, now=now)["ids"][0]
        expires = self.scene()["claims"][0]["expires_at"]
        self.ok({"op": "shape", "text": "early", "at": [20, 20], "intent": "t"}, now=now + 60)
        self.assertEqual(self.scene()["claims"][0]["expires_at"], expires, "more than half is left: no renewal")
        later = now + C.CLAIM_TTL_S / 2 + 10
        result = self.apply([{"op": "shape", "text": "late", "at": [300, 20], "intent": "t"}, {"op": "shape", "text": "later", "at": [500, 20], "intent": "t"}],
                            now=later)
        self.assertEqual(len(result["applied"]), 2)
        found = next(c for c in C._load_state(self.team).claims.values() if c["id"] == claim)
        self.assertEqual(found["expires_at"], C._iso(later + C.CLAIM_TTL_S))
        events = C._read_events(self.team)[-2:]
        self.assertEqual(sum(1 for e in events for c in e["changes"] if c["target"] == "claim"), 1, "one renewal per batch")

    def test_an_op_outside_its_claims_renews_nothing(self):
        now = time.time()
        self.ok({"op": "claim", "region": [0, 0, 400, 400], "label": "mine", "intent": "t"}, now=now)
        expires = self.scene()["claims"][0]["expires_at"]
        self.ok({"op": "shape", "text": "far", "at": [2000, 2000], "intent": "t"}, now=now + 200)
        self.assertIn(expires, [c["expires_at"] for c in C._load_state(self.team).claims.values()])


class AutoClaims(CollabRig):
    def test_drawing_in_free_space_claims_it(self):
        entry = self.ok({"op": "shape", "text": "idea", "at": [100, 100], "intent": "sketch the idea"})
        claim = self.scene()["claims"][0]
        self.assertEqual(entry["auto_claim"], claim["id"])
        self.assertEqual((claim["author"], claim["label"], claim["auto"]), ("alpha-member", "sketch the idea", True))
        self.assertEqual(claim["region"], [80, 80, 280, 200], "the box padded a grid step, on the grid")
        self.assertNotIn(claim["id"], entry["ids"], "the claim is named apart, not among the op's ids")

    def test_a_second_op_in_the_batch_grows_the_same_claim(self):
        result = self.apply([{"op": "shape", "text": "a", "at": [0, 0], "intent": "t"}, {"op": "shape", "text": "b", "at": [600, 0], "intent": "t"}])
        self.assertEqual([e["auto_claim"] for e in result["applied"]], ["K-1", "K-1"])
        claims = self.scene()["claims"]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["region"], [-20, -20, 780, 100])

    def test_one_mark_per_batch_next_to_each_other_is_one_claim(self):
        # QA phase 5 L8: twenty boxes in a row, one per batch, used to make K-1 to K-31 and drop the older ones.
        ids = {self.ok({"op": "shape", "text": "n{}".format(i), "at": [i * 200, 0], "intent": "t"})["auto_claim"] for i in range(8)}
        self.assertEqual(len(ids), 1)
        claim = self.scene()["claims"][0]
        self.assertEqual(claim["region"], [-20, -20, 1580, 100])
        far = self.ok({"op": "shape", "text": "far", "at": [6000, 0], "intent": "t"})["auto_claim"]
        self.assertNotIn(far, ids, "far away is a lane of its own")
        wide = [self.ok({"op": "shape", "text": "w{}".format(i), "at": [1600 + i * 200, 0], "intent": "t"})["auto_claim"] for i in range(8)]
        self.assertGreater(len(set(wide)), 1, "a claim stops growing at AUTO_CLAIM_MAX a side")

    def test_drawing_in_its_own_lane_or_home_claims_nothing(self):
        self.ok({"op": "claim", "region": [0, 0, 800, 400], "label": "mine", "intent": "t"})
        self.assertNotIn("auto_claim", self.ok({"op": "shape", "text": "in", "at": [100, 100], "intent": "t"}))
        home = C._load_state(self.team).homes["alpha-member"]
        self.assertNotIn("auto_claim", self.ok({"op": "shape", "text": "home", "at": [home[0] + 40, home[1] + 40], "intent": "t"}))
        self.assertNotIn("auto_claim", self.ok({"op": "comment", "at": [3000, 3000], "text": "a note", "intent": "t"}))
        self.assertNotIn("auto_claim", self.ok({"op": "arrow", "from": [3000, 0], "to": [3400, 0], "intent": "t"}))

    def test_the_oldest_automatic_claim_is_released_first(self):
        now = time.time()
        explicit = self.ok({"op": "claim", "region": [0, 0, 200, 200], "label": "mine", "intent": "t"}, now=now)["ids"][0]
        first = self.ok({"op": "shape", "text": "a", "at": [1000, 0], "intent": "t"}, now=now + 1)["auto_claim"]
        second = self.ok({"op": "shape", "text": "b", "at": [2000, 0], "intent": "t"}, now=now + 2)["auto_claim"]
        third = self.ok({"op": "shape", "text": "c", "at": [3000, 0], "intent": "t"}, now=now + 3)["auto_claim"]
        ids = [c["id"] for c in C._load_state(self.team).active_claims(now + 4)]
        self.assertEqual(ids, [explicit, second, third], "the oldest automatic claim went, the explicit one stayed")
        self.ok({"op": "claim", "region": [4000, 0, 4200, 200], "label": "another", "intent": "t"}, now=now + 5)
        ids = [c["id"] for c in C._load_state(self.team).active_claims(now + 6)]
        self.assertNotIn(second, ids)
        self.assertIn(explicit, ids)
        del first

    def test_the_manager_delegates_and_the_lead_draw_without_claims(self):
        for author in (MANAGER, DEPUTY, LEAD):
            self.assertNotIn("auto_claim", self.ok({"op": "shape", "text": author.name, "at": [5000, 0], "intent": "t"}, author))


class OtherLanes(CollabRig):
    def setUp(self):
        super().setUp()
        self.ok({"op": "claim", "region": [0, 0, 800, 400], "label": "peer's", "intent": "t"}, PEER)

    def test_a_plain_members_new_mark_in_another_lane_is_a_proposal(self):
        found = self.proposed({"op": "shape", "text": "mine", "at": [100, 100], "intent": "t"})
        self.assertEqual((found["reason"], found["message"]), ("foreign_lane", "it is in alpha-peer's lane (K-1)"))
        home = C._load_state(self.team).homes.get("alpha-peer")
        self.assertEqual(self.proposed({"op": "shape", "text": "in their home", "at": [home[0] + 40, home[1] + 40], "intent": "t"})["reason"],
                         "foreign_lane")

    def test_the_manager_and_delegates_are_exempt(self):
        for author in (MANAGER, DEPUTY):
            result = self.apply([{"op": "shape", "text": author.name, "at": [100, 100], "intent": "t"}], author)
            self.assertEqual((len(result["applied"]), result["proposed"]), (1, []))
            self.assertEqual(result["warnings"][0]["code"], "inside_claim")

    def test_comments_and_arrows_are_never_judged_by_lane(self):
        self.ok({"op": "comment", "at": [100, 100], "text": "a question", "intent": "t"})
        self.ok({"op": "arrow", "from": [100, 100], "to": [200, 100], "intent": "t"})

    def test_a_member_with_an_overlapping_claim_of_its_own_draws_live(self):
        self.ok({"op": "claim", "region": [0, 0, 400, 400], "label": "shared", "intent": "t"})
        self.ok({"op": "shape", "text": "ours", "at": [100, 100], "intent": "t"})


class JoiningHerWork(CollabRig):
    def test_moving_ones_own_mark_into_her_frame_is_a_proposal(self):
        frame = self.ok({"op": "frame", "title": "Hers", "at": [0, 0], "w": 600, "h": 400}, LEAD)["ids"][0]
        mine = self.ok({"op": "shape", "text": "Mine", "at": [1000, 0], "intent": "t"})["ids"][0]
        found = self.proposed({"op": "move", "id": mine, "inside": frame, "intent": "t"})
        self.assertEqual((found["reason"], found["message"]), ("human_made", "it goes on the operator's {}".format(frame)))
        self.assertIsNone(self.el(mine)["frame"])


class ClaimEdges(CollabRig):
    """V2: a claim is drawn as a dashed rectangle over the board, so an edge through a mark reads as the mark being
    cut off. The region grows outward to hold whole what it holds most of - at the ``claim`` op and for the automatic
    claims drawing makes - and the answer says it grew, because a claim's region is also what it refuses others."""

    def test_the_claim_op_snaps_outward_and_says_so(self):
        chart = self.ok({"op": "shape", "kind": "box", "text": "Revenue", "at": [200, 200], "w": 400, "h": 300, "intent": "t"})["ids"][0]
        result = self.apply([{"op": "claim", "region": [100, 100, 500, 450], "label": "the chart", "intent": "t"}])
        claim = next(c for c in self.scene()["claims"] if c["id"] == result["applied"][0]["ids"][0])
        self.assertEqual(claim["region"], [100, 100, 600, 500], "grown to the chart's own edges")
        self.assertEqual([(w["code"], w["ids"]) for w in result["warnings"]],
                         [("claim_snapped", [claim["id"], chart])])
        self.assertEqual(C.check(self.layout, self.team, MEMBER.name)["problems"], [])

    def test_a_reclaim_that_holds_an_older_claim_of_ones_own_replaces_it(self):
        first = self.ok({"op": "claim", "region": [0, 0, 400, 400], "label": "mine", "intent": "t"})["ids"][0]
        result = self.apply([{"op": "claim", "region": [-100, -100, 500, 500], "label": "mine, wider", "intent": "t"}])
        held = [c["id"] for c in self.scene()["claims"] if c["author"] == MEMBER.name]
        self.assertNotIn(first, held, "two claims over one region say nothing new")
        self.assertIn(result["applied"][0]["ids"][0], held)

    def test_an_automatic_claim_grown_over_a_peers_mark_holds_it_whole(self):
        # A fresh automatic claim is the batch's marks padded a grid step, so its edge is never far from its own
        # marks. It is growing that reaches somebody else's: two batches a step apart make one lane over the ground
        # between them, and a mark of the peer's in that ground was drawn through (the operator's V2, one board up).
        note = self.ok({"op": "shape", "kind": "note", "text": "Theirs", "at": [900, 100], "intent": "t"}, PEER)["ids"][0]
        self.ok({"op": "release", "id": "all", "intent": "t"}, PEER)
        self.ok({"op": "shape", "kind": "box", "text": "One", "at": [0, 0], "w": 400, "h": 200, "intent": "t"})
        self.ok({"op": "shape", "kind": "box", "text": "Two", "at": [600, 0], "w": 400, "h": 200, "intent": "t"})
        claim = next(c for c in self.scene()["claims"] if c["author"] == MEMBER.name)
        self.assertEqual(claim["region"], [-20, -20, 1080, 220], "one lane over both, out to the note's own edge")
        self.assertEqual([p["code"] for p in C.check(self.layout, self.team, MEMBER.name)["problems"]], ["overlap"],
                         "the marks still overlap; no edge is drawn through one of them")
        self.assertEqual(self.el(note)["x"], 900, "nothing moved: only the claim grew")
