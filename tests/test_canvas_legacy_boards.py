"""Boards already on disk, and a check an obedient agent could not get out of (layout findings N3-LEGACY, CHECK-LOOP).

The arrangement fingerprint (``canvas_blocks.arranged_digest``) made re-sending an unchanged ``graph`` op a no-op for
every board drawn after it. Every board the owner has was drawn before it, so its first re-issue still ran the old
quality cut: of 17 boards drawn by ``d13e58c5``, 10 changed and 7 gained crossings, and the two-batch shortener got a
wire through "Counter". ``fixtures/legacy/d13e58c5-boards.jsonl`` is those 17 boards exactly as that commit drew them
(each with the op that drew it), and ``owner-l6.json`` is the owner's own board as it stands on disk, authors renamed
to this roster; ``.local/qa/layout2/legacy/tools/build_fixture.py`` wrote both.

The check half: after the author ran the repair ``canvas check`` printed on the owner's board, the manager and the
operator were still told ``routes_tangled``, and the ``relayout:"full"`` it printed changed nothing when the manager
ran it - a manager agent that obeys check would have run it forever.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest import mock

import collab_support
from collab_support import LEAD, MANAGER, MEMBER
from support import TempState, whiteboard_on

from herdr_team import canvas as C
from herdr_team import canvas_blocks as B
from herdr_team import canvas_readability as RD
from herdr_team import canvas_render as R
from herdr_team import features as F
from herdr_team import store
from herdr_team.canvas_kinds import graph as G

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "legacy"
#: The collaboration roster plus the owner's third agent, who owns only a card outside the graph: kept distinct, so
#: the board's authorship maps one to one.
ROSTER = list(collab_support.MEMBERS) + [collab_support._row("alpha-drawer", "drawer", "claude", "term_r1", "w3:p1")]
_BOOKKEEPING = {"seq", "updated_seq", "updated_at", "created_at", "batch", "intent", "client_id"}


def _boards() -> List[Dict[str, Any]]:
    return [json.loads(line) for line in (FIXTURES / "d13e58c5-boards.jsonl").read_text().splitlines() if line.strip()]


def _owner() -> Dict[str, Any]:
    return json.loads((FIXTURES / "owner-l6.json").read_text())


class LegacyRig(unittest.TestCase):
    """A team with the whiteboard on and a board planted as it was stored."""

    def setUp(self) -> None:
        self.ts: Any = None
        self.addCleanup(lambda: self.ts.cleanup() if self.ts is not None else None)
        patcher = mock.patch.object(R, "find_resvg", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.new_team()

    def new_team(self) -> None:
        """A fresh team, the last one removed: one per board in the tests that walk the corpus."""
        if self.ts is not None:
            self.ts.cleanup()
        self.ts = TempState(members=ROSTER)
        whiteboard_on(self.ts.session, self.ts.team, via="cli")

    def plant(self, scene: Dict[str, Any]) -> None:
        scene = copy.deepcopy(scene)
        scene["team"] = self.ts.team.name
        wb = F.whiteboard_dir(self.ts.team)
        wb.mkdir(parents=True, exist_ok=True)
        store.write_json(wb / C.SCENE_FILE, scene)

    def apply(self, op: Dict[str, Any], author: Any = MEMBER) -> Dict[str, Any]:
        result = C.apply_ops(self.ts.layout, self.ts.team, [copy.deepcopy(op)], author)
        self.assertEqual((result["refused"], result["proposed"]), ([], []), result)
        return result

    def scene(self) -> Dict[str, Any]:
        return C.load_scene(self.ts.team)

    def board(self) -> Dict[str, str]:
        """Every element and every stored field but bookkeeping and the fingerprint stamp."""
        return {el["id"]: json.dumps({k: v for k, v in el.items() if k not in _BOOKKEEPING and k != B.ARRANGED},
                                     sort_keys=True, default=str) for el in self.scene()["elements"]}

    def root(self, alias: str) -> Dict[str, Any]:
        return next(el for el in self.scene()["elements"] if el.get("alias") == alias)

    def measure(self, alias: str) -> Dict[str, Any]:
        scene = self.scene()
        return RD.measure(RD.from_scene(scene, self.root(alias)["id"]))

    def codes(self, reader: str) -> List[str]:
        return sorted(p["code"] for p in C.check(self.ts.layout, self.ts.team, reader).get("problems") or [])

    def own_spec(self, alias: str) -> Dict[str, Any]:
        """The author re-sending its own graph as the board reads it back, as an agent working on l6 would."""
        scene = self.scene()
        return dict(B.spec_of(scene["elements"], self.root(alias)), op="graph", id=alias, intent="the same graph again")


def _changed(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    return sorted(eid for eid in set(before) | set(after) if before.get(eid) != after.get(eid))


def _redrawn(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    """``_changed`` less read-back repairs: an element whose only change is its stored ``item.id`` taking its own
    ``part``. The owner's board stores "Paste long URL" as part ``n326`` with item id ``paste`` (an old adoption);
    any re-issue writes the item back as ``spec_of`` reads it, which is no part of how the board is drawn."""
    out = []
    for eid in _changed(before, after):
        was, now = json.loads(before.get(eid) or "null"), json.loads(after.get(eid) or "null")
        if isinstance(was, dict) and isinstance(now, dict) and isinstance(now.get("item"), dict) and \
                now["item"].get("id") == now.get("part") and dict(was, item=None) == dict(now, item=None) and \
                dict(was["item"] or {}, id=None) == dict(now["item"], id=None):
            continue
        out.append(eid)
    return out


def _hard(m: Dict[str, Any]) -> Dict[str, float]:
    return {k: float(m[k]) for k in G.REDRAW_HARD}


class OldBoards(LegacyRig):
    """The first re-issue of a board drawn before the fingerprint keeps it, unless the redraw reads strictly better."""

    def _reissue(self, label: str, alias: str, op: Dict[str, Any], scene: Dict[str, Any]) -> Dict[str, Any]:
        self.plant(scene)
        b0, m0, c0 = self.board(), self.measure(alias), self.codes(MEMBER.name)
        self.assertNotIsInstance(self.root(alias).get(B.ARRANGED), int, "{}: the fixture is an old board".format(label))
        op = op if op is not None else self.own_spec(alias)
        self.apply(dict(op, intent="the same op again"))
        b1, m1, c1 = self.board(), self.measure(alias), self.codes(MEMBER.name)
        self.assertIsInstance(self.root(alias).get(B.ARRANGED), int, "{}: stamped on its first re-issue".format(label))
        self.apply(dict(op, intent="and once more"))
        b2 = self.board()
        return {"changed": _redrawn(b0, b1), "second": _changed(b1, b2), "before": _hard(m0), "after": _hard(m1),
                "new_codes": sorted(set(c1) - set(c0))}

    def test_no_old_board_reads_worse_after_its_first_re_issue_and_the_second_changes_nothing(self):
        for item in _boards() + [dict(_owner(), op=None)]:
            with self.subTest(board=item["label"]):
                self.new_team()
                got = self._reissue(item["label"], item["alias"], item["op"], item["scene"])
                worse = [k for k in G.REDRAW_HARD if got["after"][k] > got["before"][k]]
                self.assertEqual(worse, [], "{}: {} -> {}".format(item["label"], got["before"], got["after"]))
                if got["changed"]:
                    self.assertTrue(any(got["after"][k] < got["before"][k] for k in G.REDRAW_HARD),
                                    "{} changed without reading better: {}".format(item["label"], got["changed"][:6]))
                self.assertEqual(got["second"], [], "{}: the second re-issue changed {}".format(item["label"], got["second"][:6]))
                self.assertNotIn("arrow_through", got["new_codes"], item["label"])

    def test_the_seventeen_boards_d13e58c5_drew_keep_their_drawings(self):
        # Before: 10 of these 17 changed on their first re-issue and 7 gained crossings. Not one redraw of them reads
        # strictly better on crossings, wires through boxes and misattributed labels, so every one keeps its drawing.
        changed = []
        for item in _boards():
            with self.subTest(board=item["label"]):
                self.new_team()
                got = self._reissue(item["label"], item["alias"], item["op"], item["scene"])
                if got["changed"]:
                    changed.append(item["label"])
        self.assertEqual(changed, [])

    def _geometry(self, alias: str) -> Dict[str, Any]:
        """Where the block and everything in it is drawn: boxes, routes and label spots."""
        rid = self.root(alias)["id"]
        return {el["id"]: (el.get("x"), el.get("y"), el.get("w"), el.get("h"), el.get("points"), el.get("label_at"))
                for el in self.scene()["elements"] if el["id"] == rid or el.get("group") == rid or el.get("frame") == rid}

    def _owner_moved(self, by: float, proposal_stays: bool = False) -> Dict[str, Any]:
        """The owner's board with the operator's expiry note ``by`` units further down, and P-4 (an agent's edit of
        that note) following it unless ``proposal_stays``: then P-4 proposes moving the note back where it was, so
        its banner is drawn where the frame would grow and nothing else is."""
        scene = copy.deepcopy(_owner()["scene"])
        note = next(el for el in scene["elements"] if el["id"] == "E-344")
        was = dict(note)
        note["y"] = float(note["y"]) + by
        for record in scene.get("proposals") or []:
            if record.get("id") == "P-4":
                record["changes"] = [{"target": "element", "action": "update", "id": "E-344", "was": None,
                                      "value": dict(was, text=str(was.get("text") or "") + " (edited)") if proposal_stays
                                      else dict(note, text=str(note.get("text") or "") + " (edited)")}]
        return scene

    def test_the_owners_board_keeps_its_drawing_rather_than_grow_over_the_operators_note(self):
        # The owner's l6 board as it stands has a redraw that reads strictly better (8 crossings -> 6), and it grew
        # the frame 120 units down, to 2 units above the operator's "Operator note: expiry" card and over the banner
        # of the open proposal P-4. The owner's rule (decision 3): a one-time redraw nobody asked for must not grow
        # over someone else's content, so the stored drawing is kept, and stamped, so it settles.
        item = _owner()
        self.plant(item["scene"])
        before, note = self._geometry(item["alias"]), next(el for el in self.scene()["elements"] if el["id"] == "E-344")
        result = C.apply_ops(self.ts.layout, self.ts.team, [dict(self.own_spec(item["alias"]), intent="the same graph")], MEMBER)
        self.assertEqual((result["refused"], result["proposed"]), ([], []))
        self.assertEqual(self._geometry(item["alias"]), before, "the frame and its drawing stay as stored")
        self.assertEqual(next(el for el in self.scene()["elements"] if el["id"] == "E-344"), note)
        self.assertIsInstance(self.root(item["alias"]).get(B.ARRANGED), int, "stamped, so it settles")
        said = [w["message"] for w in result.get("warnings") or [] if "kept as it is" in w["message"]]
        self.assertTrue(said and "E-344" in said[0] and "P-4" in said[0], result.get("warnings"))
        board = self.board()
        self.apply(dict(self.own_spec(item["alias"]), intent="and again"))
        self.assertEqual(_changed(board, self.board()), [])

    def test_with_room_below_the_owners_board_still_takes_its_better_redraw(self):
        # Not frozen: moved out of the way, the note no longer stands where the frame grows, and the redraw that
        # reads better is taken (8 crossings -> 6) - a rule that only ever kept would have thrown that away.
        got = self._reissue("owner-l6, note moved", _owner()["alias"], None, self._owner_moved(2000.0))
        self.assertTrue(got["changed"])
        self.assertLess(got["after"]["crossings_seen"], got["before"]["crossings_seen"])

    def test_an_open_proposals_banner_alone_keeps_the_drawing(self):
        # The note is far away, but P-4 proposes putting it back below the frame: its banner is drawn there, and a
        # frame grown over it would cover the operator's pending decision.
        item = _owner()
        self.plant(self._owner_moved(2000.0, proposal_stays=True))
        before = self._geometry(item["alias"])
        result = C.apply_ops(self.ts.layout, self.ts.team, [dict(self.own_spec(item["alias"]), intent="the same graph")], MEMBER)
        self.assertEqual(self._geometry(item["alias"]), before)
        said = [w["message"] for w in result.get("warnings") or [] if "kept as it is" in w["message"]]
        self.assertTrue(said and "proposal P-4" in said[0] and "E-344" not in said[0], result.get("warnings"))

    def test_an_old_board_whose_spec_changed_is_arranged_for_the_change(self):
        # The stand-in fingerprint is taken of the board as the op found it: an op that adds an edge is not settled.
        item = next(b for b in _boards() if b["label"] == "flow")
        self.plant(item["scene"])
        op = dict(item["op"], edges=list(item["op"]["edges"]) + ["redirect -> counter: counted"], intent="one more edge")
        self.apply(op)
        root = self.root(item["alias"])
        arrows = [el for el in self.scene()["elements"] if el.get("group") == root["id"] and el.get("type") == "arrow"]
        new = next(el for el in arrows if el.get("text") == "counted")
        self.assertGreaterEqual(len(new.get("points") or []), 2)
        self.assertIsInstance(root.get(B.ARRANGED), int)


class RedrawClearance(unittest.TestCase):
    """Clearance is about newly covered ground, not net overlap area or a proposal's ownership."""

    @staticmethod
    def box(eid, x, y, w, h, **extra):
        return dict({"id": eid, "type": "box", "x": x, "y": y, "w": w, "h": h}, **extra)

    def crowds(self, was, now, marks=(), proposals=()):
        live = [now] + list(marks)
        state = SimpleNamespace(proposals={p["id"]: p for p in proposals})
        ctx = SimpleNamespace(live=lambda: iter(live), state=state)
        return B._crowds(ctx, was["id"], was, now)

    def test_growth_that_also_shrinks_still_preserves_clearance(self):
        was = self.box("root", 0, 0, 100, 100)
        now = self.box("root", 0, 0, 90, 110)
        other = self.box("operator-note", 80, 80, 20, 30)
        self.assertEqual(self.crowds(was, now, [other]), [other["id"]])

    def test_already_covered_ground_and_exact_clearance_are_not_new_intrusions(self):
        was = self.box("root", 0, 0, 100, 100)
        now = self.box("root", 0, 0, 90, 110)
        inside = self.box("inside", 20, 20, 10, 10)
        clear = self.box("clear", 0, 110 + B.REDRAW_CLEARANCE, 100, 10)
        self.assertEqual(self.crowds(was, now, [inside, clear]), [])
        clear["y"] -= 1
        self.assertEqual(self.crowds(was, now, [inside, clear]), [clear["id"]])

    def test_a_proposal_about_the_block_is_not_exempt_from_clearance(self):
        was = self.box("root", 0, 0, 100, 100)
        now = self.box("root", 0, 0, 100, 180)
        proposed = self.box("root", 0, 200, 100, 100)
        proposal = {"id": "P-1", "author": "someone", "status": "open", "targets": ["root"], "created": [],
                    "intent": "move it", "changes": [{"id": "root", "action": "update", "value": proposed}]}
        self.assertEqual(self.crowds(was, now, proposals=[proposal]), ["the overlay of proposal P-1"])

    def test_nonmember_arrows_and_comments_are_content_too(self):
        was = self.box("root", 0, 0, 100, 100)
        now = self.box("root", 0, 0, 100, 180)
        for kind in ("arrow", "comment"):
            with self.subTest(kind=kind):
                other = self.box("outside", 0, 160, 100, 20, type=kind)
                self.assertEqual(self.crowds(was, now, [other]), ["outside"])

    def test_the_banner_at_its_smallest_visible_zoom_preserves_clearance(self):
        was = self.box("root", 0, 0, 100, 100)
        now = self.box("root", 0, 0, 100, 180)
        other = self.box("outside", 0, 1000, 100, 60)
        proposed = dict(other, y=245)
        proposal = {"id": "P-1", "author": "someone", "status": "open", "targets": [other["id"]], "created": [],
                    "intent": "move it", "changes": [{"id": other["id"], "action": "update", "value": proposed}]}
        self.assertEqual(self.crowds(was, now, [other], [proposal]), ["the overlay of proposal P-1"])

    def test_clearance_bound_covers_the_display_lists_visible_banner_envelope(self):
        from herdr_team import canvas_collab, canvas_display

        other = self.box("outside", 0, 1000, 100, 60, author="someone", author_kind="member", text="A note")
        proposal = {"id": "P-1", "author": "someone", "status": "open", "targets": [other["id"]], "created": [],
                    "intent": "a proposal with a long banner", "changes": [{"id": other["id"], "action": "update",
                    "value": dict(other, y=245)}]}
        scene = {"elements": [other], "proposals": [proposal]}
        drawn = next(e for e in canvas_display.entries(scene) if e["id"] == proposal["id"])
        pill = next(i for i in drawn["items"] if i.get("k") == "group" and i.get("screen"))
        rect = next(i for i in pill["items"] if i.get("k") == "rect")
        zoom = pill["lod"][0]
        px, py = pill["screen"]
        visible = (px + rect["x"] / zoom, py + rect["y"] / zoom,
                   px + (rect["x"] + rect["w"]) / zoom, py + (rect["y"] + rect["h"]) / zoom)
        bound = canvas_collab.proposal_reach(proposal, {other["id"]: other})
        self.assertLessEqual(bound[0], visible[0])
        self.assertLessEqual(bound[1], visible[1])
        self.assertGreaterEqual(bound[2], visible[2])
        self.assertGreaterEqual(bound[3], visible[3])

    def test_a_moved_members_proposal_leader_alone_keeps_clearance(self):
        was = self.box("root", 0, 0, 100, 100)
        now = self.box("root", 0, 0, 120, 100)
        child = self.box("child", 10, 50, 20, 20, group="root")
        proposal = {"id": "P-1", "author": "someone", "status": "open", "targets": ["child"], "created": [],
                    "intent": "move it", "changes": [{"id": "child", "action": "update", "value": dict(child, x=500)}]}
        # The ghost and banner start near x500, but the dashed leader runs [20,60] -> [510,60], through the newly
        # covered strip x100..120. Membership does not exempt the immutable proposal overlay.
        self.assertEqual(self.crowds(was, now, [child], [proposal]), ["the overlay of proposal P-1"])

    def test_clearance_bound_holds_actual_moving_and_aside_leader_endpoints(self):
        from herdr_team import canvas_collab, canvas_display

        child = self.box("child", 10, 50, 20, 20, author="someone", author_kind="member", text="A note")
        for mode, value in (("move", dict(child, x=500)), ("aside", dict(child, text="A different note"))):
            with self.subTest(mode=mode):
                proposal = {"id": "P-1", "author": "someone", "status": "open", "targets": ["child"], "created": [],
                            "intent": "change it", "changes": [{"id": "child", "action": "update", "value": value}]}
                drawn = next(e for e in canvas_display.entries({"elements": [child], "proposals": [proposal]})
                             if e["id"] == "P-1")
                leaders = [i for i in drawn["items"] if i.get("k") == "line" and i.get("dash") == [6, 4]]
                self.assertTrue(leaders, drawn)
                bound = canvas_collab.proposal_reach(proposal, {"child": child})
                for line in leaders:
                    for x, y in line["points"]:
                        self.assertLessEqual(bound[0], x)
                        self.assertLessEqual(bound[1], y)
                        self.assertGreaterEqual(bound[2], x)
                        self.assertGreaterEqual(bound[3], y)


class SettledBoards(LegacyRig):
    """The two leftovers of the fingerprint on boards it drew: a frame that grew once, and a drag that took two."""

    def _fresh(self, op: Dict[str, Any]) -> None:
        self.apply(dict(op, intent="drawn"))

    def test_a_board_with_labels_does_not_grow_its_frame_on_re_issue(self):
        # The verifier's hiring board: the hug after a settled arrangement counted label pills the label pass placed
        # after the hug that sized the frame, so the frame moved 12 up and grew 20 on the first re-issue.
        item = next(b for b in _boards() if b["label"] == "v-hiring")
        self._fresh(item["op"])
        before = self.board()
        for n in range(3):
            self.apply(dict(item["op"], intent="again %d" % n))
        self.assertEqual(_changed(before, self.board()), [])

    def test_one_re_issue_settles_a_board_after_the_operator_drags_a_box(self):
        for label in ("adversarial", "corpus:tree", "corpus:two-components", "flow"):
            with self.subTest(board=label):
                self.new_team()
                item = next(b for b in _boards() if b["label"] == label)
                self._fresh(item["op"])
                root = self.root(item["alias"])
                boxes = [el for el in self.scene()["elements"] if el.get("group") == root["id"] and el.get("type") == "box"]
                dragged = boxes[len(boxes) // 2]
                self.apply({"op": "move", "id": dragged["id"], "by": [37, 23], "intent": "the operator drags it"}, LEAD)
                self.apply(dict(item["op"], intent="the author re-sends"))
                after_one = self.board()
                self.apply(dict(item["op"], intent="and again"))
                self.assertEqual(_changed(after_one, self.board()), [], label)


class CheckLoop(LegacyRig):
    """The repair ``canvas check`` prints clears the finding for whoever runs it, so obeying check ends."""

    ALIAS = "link_shortener_flow"
    READERS = (("author", MEMBER), ("manager", MANAGER), ("operator", LEAD))

    def _repairs(self, reader: str) -> List[Dict[str, Any]]:
        found = C.check(self.ts.layout, self.ts.team, reader).get("problems") or []
        return [p for p in found if isinstance(p.get("fix"), dict) and p["fix"].get("relayout") == "full"]

    def test_every_reader_who_obeys_check_after_the_authors_repair_gets_out(self):
        for name, who in self.READERS:
            with self.subTest(reader=name):
                self.new_team()
                self.plant(_owner()["scene"])
                printed = self._repairs(MEMBER.name)
                self.assertTrue(printed, "the owner's board needs its repair")
                self.apply(printed[0]["fix"])
                for _round in range(3):
                    asked = self._repairs(who.name)
                    if not asked:
                        break
                    before = self.board()
                    self.apply(asked[0]["fix"], who)
                    # A printed repair that changes nothing and is printed again is the loop.
                    self.assertNotEqual(_changed(before, self.board()), [],
                                        "{} was told {} and running it changed nothing".format(name, asked[0]["code"]))
                self.assertEqual(self._repairs(who.name), [], name)

    def test_the_manager_is_told_nothing_on_a_board_the_author_just_repaired(self):
        self.plant(_owner()["scene"])
        self.apply(self._repairs(MEMBER.name)[0]["fix"])
        self.assertNotIn("routes_tangled", self.codes(MANAGER.name))
        self.assertNotIn("routes_tangled", self.codes(MEMBER.name))

    def test_what_the_check_holds_is_what_the_repair_holds(self):
        # One rule, ``canvas_blocks.held_by``: the manager may move an agent's boxes and holds the operator's; a peer
        # holds every mark but their own; the operator holds nothing; with no reader only the operator's pins hold.
        self.plant(_owner()["scene"])
        elements = self.scene()["elements"]
        by_id = {el["id"]: el for el in elements}
        root = next(el for el in elements if el.get("alias") == self.ALIAS)
        members = [el for el in elements if el.get("group") == root["id"]]
        human = {el["id"] for el in members if el.get("author_kind") == C.KIND_HUMAN}
        pinned = {el["id"] for el in members if (el.get("pin") or {}).get("by") == "human"}
        theirs = {el["id"] for el in members if el.get("author") != "alpha-peer"}
        self.assertEqual(set(G._reader_holds(members, "alpha-manager", True, by_id)), human | pinned)
        self.assertEqual(set(G._reader_holds(members, "alpha-peer", False, by_id)), theirs | pinned)
        self.assertEqual(G._reader_holds(members, C.HUMAN, False, by_id), [])
        self.assertEqual(set(G._reader_holds(members, None, False, by_id)), pinned)


if __name__ == "__main__":
    unittest.main()
