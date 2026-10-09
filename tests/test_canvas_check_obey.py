"""Ownership-aware repair advice converges: barred readers get no op, and current exact proposals are acknowledged.

Three loops predated the layout findings round (all identical on ``d13e58c5``), each the same failure: check printed a
fix the reader may not make, the review gate turned the attempt into a proposal for the operator, the drawing did not
change, and check printed it again, so an obedient agent filed P-1, P-2, P-3 ... forever:

- (a) an agent told to reroute the *operator's* arrow (``arrow_through``, ``restyle ... route: orthogonal``);
- (b) a peer told ``routes_tangled`` / ``crossings_high`` / ``labels_adrift`` with ``relayout: "full"`` on another
  agent's graph;
- (c) the operator printed ``{op: claim, region}`` for an agent's claim, which made a claim of the operator's inside
  the agent's (K-2, K-3 ...) while the agent's edge went on cutting.

The owner's rule (2026-10-09): when the reader cannot apply the fix, check prints the finding without an op and says
who can, or names the reader's own open proposal that already makes the change. Every test here obeys check as a real
agent would - runs each printed fix verbatim through the CLI, as the reader, round after round - and fails on a fix
printed again after its own op left the drawing as it was. Separate freezes, locks and pins are not bypassed: a
frozen own graph may file one proposal, then check names that current exact proposal without printing another op.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Tuple

from collab_support import LEAD, MANAGER, MEMBER, PEER
from test_canvas_legacy_boards import LegacyRig, ROSTER, _owner
from support import fake_agent
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

from herdr_team import canvas as C

#: An incident response flow, the verifier's board: the operator then drags two boxes and pins them, adds a card of
#: their own and an arrow from it into the graph that runs straight through "Mitigate".
INCIDENT = {
    "op": "graph", "id": "incident", "title": "Incident response", "layout": "flow", "direction": "right",
    "route": "orthogonal", "at": [0, 0], "intent": "an incident board",
    "nodes": [{"id": n, "text": t} for n, t in (
        ("alert", "Pager alert"), ("ack", "On-call acknowledges"), ("triage", "Triage severity"), ("mitig", "Mitigate"),
        ("comms", "Status page update"), ("fix", "Ship the fix"), ("verify", "Verify recovery"),
        ("pm", "Write the postmortem"))],
    "edges": ["alert -> ack: page", "ack -> triage: assess", "triage -> mitig: sev1", "triage -> comms: customers hit",
              "mitig -> fix: root cause", "fix -> verify: deployed", "verify -> mitig: still failing",
              "verify -> pm: resolved", "comms -> verify: next update"]}
READERS = (("author", MEMBER), ("manager", MANAGER), ("peer", PEER), ("operator", LEAD))
#: Rounds an obedient reader is given. Every loop seen took one round to show (the fix printed again after its op
#: changed nothing), so four is plenty, and a reader still being printed fixes after four is a loop by itself.
ROUNDS = 4


class ObeyRig(LegacyRig):
    """Boards with operator-placed elements, and a reader who runs whatever check prints."""

    def cli(self, who, *argv):
        """The real argument/identity/author path, with only the Herdr API replaced by the isolated test server."""
        agents = [fake_agent(m["pane_id"], m["terminal_id"], m["kind"], m["name"])
                  for m in ROSTER if m.get("pane_id") and m.get("terminal_id")]
        pane = next((m["pane_id"] for m in ROSTER if m["name"] == who.name and m.get("pane_id")), None)
        code, payload, error = json_out(run_cli(["--json", "canvas"] + list(argv),
                                               env_no_daemon(self.ts, HERDR_PANE_ID=pane), live_api(agents)))
        self.assertEqual(code, 0, error)
        return payload

    def incident(self) -> None:
        """The incident board, drawn by its author, then touched by the operator."""
        self.apply(INCIDENT)
        scene = self.scene()
        root = next(el for el in scene["elements"] if el.get("alias") == "incident")
        boxes = {el.get("part"): el for el in scene["elements"] if el.get("group") == root["id"] and el.get("type") == "box"}
        for part, by in (("triage", [-30, 70]), ("fix", [40, -55])):
            self.apply({"op": "move", "id": boxes[part]["id"], "by": by, "intent": "the operator places it"}, LEAD)
            self.apply({"op": "pin", "id": boxes[part]["id"], "intent": "keep it there"}, LEAD)
        note = self.apply({"op": "card", "id": "op_note", "title": "Operator note: on-call rota",
                           "body": "page the secondary after 10 min", "at": [root["x"] + root["w"] + 80, root["y"]],
                           "intent": "operator note"}, LEAD)["aliases"]["op_note"]
        self.apply({"op": "arrow", "from": note, "to": boxes["ack"]["id"], "label": "escalation", "intent": "link it"}, LEAD)

    def operator_arrow(self) -> str:
        return next(el["id"] for el in self.scene()["elements"] if el.get("type") == "arrow" and el.get("author_kind") == "human")

    def found(self, who: Any) -> List[Dict[str, Any]]:
        return self.cli(who, "check").get("problems") or []

    def obey(self, who: Any) -> Tuple[List[str], List[Tuple[int, str]], int]:
        """Run every printed fix as ``who`` until check prints none: ``(proposals filed, loops, rounds)``.

        A loop is a finding printed with its fix again after running that very fix changed nothing on the board."""
        filed: List[str] = []
        loops: List[Tuple[int, str]] = []
        idle: set = set()
        for number in range(ROUNDS):
            fixes = [p for p in self.found(who) if isinstance(p.get("fix"), dict)]
            loops.extend((number, p["code"]) for p in fixes if (p["code"], tuple(p["ids"])) in idle)
            if not fixes:
                return filed, loops, number
            idle = set()
            for problem in fixes:
                before = self.board()
                result = self.cli(who, "draw", "--op", json.dumps(problem["fix"]))
                filed.extend(str(p.get("proposal")) for p in result["proposed"])
                if self.board() == before:
                    idle.add((problem["code"], tuple(problem["ids"])))
        return filed, loops + [(ROUNDS, "still printing fixes")], ROUNDS

    def open_proposals(self) -> List[Dict[str, Any]]:
        return [p for p in self.scene().get("proposals") or [] if p.get("status") == "open"]


class OperatorsArrow(ObeyRig):
    """(a) The operator's arrow through a box: agents are told it is the operator's, the operator gets the fix."""

    def test_no_agent_is_printed_a_reroute_of_the_operators_arrow(self):
        self.incident()
        arrow = self.operator_arrow()
        for name, who in READERS[:3]:
            with self.subTest(reader=name):
                told = [p for p in self.found(who) if p["code"] == "arrow_through" and arrow in p["ids"]]
                self.assertTrue(told, "the operator's arrow runs through a box")
                for problem in told:
                    self.assertIsNone(problem["fix"], problem)
                    self.assertEqual(problem["fix_by"], ["the operator"])
                    self.assertIn("only the operator can", problem["message"])

    def test_every_reader_who_obeys_check_on_the_board_gets_out_without_filing_a_proposal(self):
        for name, who in READERS:
            with self.subTest(reader=name):
                self.new_team()
                self.incident()
                filed, loops, _rounds = self.obey(who)
                self.assertEqual(loops, [], name)
                self.assertEqual(filed, [], "{} filed proposals by obeying check".format(name))
                self.assertEqual(self.open_proposals(), [])

    def test_the_operator_is_printed_the_reroute_and_it_clears(self):
        self.incident()
        arrow = self.operator_arrow()
        printed = [p for p in self.found(LEAD) if p["code"] == "arrow_through" and arrow in p["ids"]]
        self.assertTrue(printed and all(isinstance(p["fix"], dict) for p in printed), printed)
        self.apply(printed[0]["fix"], LEAD)
        self.assertEqual([p for p in self.found(LEAD) if p["code"] == "arrow_through" and arrow in p["ids"]], [])

    def test_under_human_edits_live_an_agent_may_reroute_it_and_is_printed_the_fix(self):
        # The operator's setting makes an agent's change to the operator's marks go live, so the fix works for them.
        self.incident()
        self.apply({"op": "settings", "human_edits": "live", "intent": "agents may tidy my marks"}, LEAD)
        arrow = self.operator_arrow()
        printed = [p for p in self.found(MEMBER) if p["code"] == "arrow_through" and arrow in p["ids"]]
        self.assertTrue(printed and all(isinstance(p["fix"], dict) for p in printed), printed)
        filed, loops, _rounds = self.obey(MEMBER)
        self.assertEqual((filed, loops), ([], []))


class AnotherAgentsGraph(ObeyRig):
    """(b) A peer is told what is wrong with another agent's graph, and who may redraw it, never the redraw."""

    READABILITY = ("routes_tangled", "crossings_high", "labels_adrift", "graph_thin", "bands_apart")

    def test_a_peer_is_told_the_finding_and_whose_graph_it_is(self):
        self.plant(_owner()["scene"])
        told = [p for p in self.found(PEER) if p["code"] in self.READABILITY]
        self.assertTrue(told, "the owner's board as it stands reads badly to anyone")
        for problem in told:
            self.assertIsNone(problem["fix"], problem)
            self.assertEqual(problem["fix_by"], ["alpha-member", "the manager", "the operator"])
            self.assertIn("link_shortener_flow is alpha-member's", problem["message"])

    def test_a_peer_who_obeys_check_on_the_owners_board_gets_out_without_filing_a_proposal(self):
        for start in ("as it is", "after the author's repair"):
            with self.subTest(start=start):
                self.new_team()
                self.plant(_owner()["scene"])
                if start != "as it is":
                    repair = [p for p in self.found(MEMBER) if isinstance(p.get("fix"), dict) and p["fix"].get("relayout") == "full"]
                    self.assertTrue(repair)
                    self.apply(repair[0]["fix"])
                filed, loops, _rounds = self.obey(PEER)
                self.assertEqual(loops, [], start)
                self.assertEqual(filed, [], "{}: the peer filed proposals by obeying check".format(start))

    def test_every_reader_who_obeys_check_on_the_owners_board_gets_out(self):
        for name, who in READERS:
            with self.subTest(reader=name):
                self.new_team()
                self.plant(_owner()["scene"])
                superseded = {p["id"] for p in self.scene().get("proposals") or [] if p.get("status") == "superseded"}
                filed, loops, _rounds = self.obey(who)
                self.assertEqual(loops, [], name)
                self.assertEqual(len(filed), len(set(filed)))
                self.assertFalse([p["id"] for p in self.scene().get("proposals") or []
                                  if p.get("status") == "superseded" and p["id"] not in superseded],
                                 "{} superseded its own proposal by obeying check".format(name))

    def test_the_author_and_the_manager_are_still_printed_the_redraw(self):
        self.plant(_owner()["scene"])
        for who in (MEMBER, MANAGER, LEAD):
            with self.subTest(reader=who.name):
                printed = [p for p in self.found(who) if p["code"] in self.READABILITY]
                fixes = [p for p in printed if isinstance(p.get("fix"), dict)]
                self.assertEqual(len(fixes), 1, printed)
                self.assertTrue(all(p.get("fix") or "fix above also repairs this" in p["message"] for p in printed), printed)


class AliasedGraphs(ObeyRig):
    """The same alias belongs to two authors. A printed repair must name the right graph in every reader's hands."""

    def test_shared_alias_repairs_the_canonical_target_without_editing_the_other_graph(self):
        for name, who in READERS:
            with self.subTest(reader=name):
                self.new_team()
                self.plant(_owner()["scene"])
                rid = self.root("link_shortener_flow")["id"]
                self.apply(dict(INCIDENT, id="link_shortener_flow", at=[4000, 0]), MANAGER)
                other = next(e for e in self.scene()["elements"]
                             if e.get("alias") == "link_shortener_flow" and e.get("author") == MANAGER.name)
                unchanged = {eid: value for eid, value in self.board().items() if eid != rid and
                             (next(e for e in self.scene()["elements"] if e["id"] == eid).get("group") == other["id"] or eid == other["id"])}
                fixes = [p["fix"] for p in self.found(who) if p["ids"] == [rid] and p.get("fix")]
                if who is PEER:
                    self.assertEqual(fixes, [])
                    continue
                self.assertEqual(len(fixes), 1, self.found(who))
                result = self.cli(who, "draw", "--op", json.dumps(fixes[0]))
                self.assertEqual((result["refused"], result["proposed"]), ([], []), result)
                self.assertFalse([p for p in self.found(who) if p["ids"] == [rid] and p.get("fix")], self.found(who))
                self.assertEqual({eid: self.board()[eid] for eid in unchanged}, unchanged)


class HumanGraphs(ObeyRig):
    """Live operator edits use the handler's raised authority, without granting it to an ordinary own-root draw."""

    def human_graph(self, whole):
        scene = copy.deepcopy(_owner()["scene"])
        root = next(e for e in scene["elements"] if e.get("alias") == "link_shortener_flow")
        for el in scene["elements"]:
            if el["id"] == root["id"] or (whole and el.get("group") == root["id"]):
                el["author"], el["author_kind"] = C.HUMAN, C.KIND_HUMAN
        self.plant(scene)
        self.apply({"op": "settings", "human_edits": "live", "intent": "agents may repair my graph"}, LEAD)
        return root["id"]

    def test_a_wholly_operator_owned_graph_live_repair_settles_without_a_noop_loop(self):
        rid = self.human_graph(True)
        self.assertTrue([p for p in self.found(MEMBER) if p["ids"] == [rid] and p.get("fix")])
        filed, loops, _rounds = self.obey(MEMBER)
        self.assertEqual((filed, loops), ([], []))

    def test_an_operator_root_does_not_authorize_editing_a_peers_members(self):
        rid = self.human_graph(False)
        findings = [p for p in self.found(MEMBER) if p["ids"] == [rid]]
        self.assertTrue(findings)
        self.assertTrue(all(p.get("fix") is None for p in findings), findings)
        self.assertTrue(any(PEER.name in (p.get("fix_by") or []) for p in findings), findings)
        filed, loops, _rounds = self.obey(MEMBER)
        self.assertEqual((filed, loops), ([], []))


class AgentsClaim(ObeyRig):
    """(c) The operator is told an agent's claim cuts a mark, and is not printed a claim of their own for it."""

    def plant_claim(self) -> Dict[str, Any]:
        """The owner's board with an agent's claim whose edge runs through a box of the graph."""
        scene = copy.deepcopy(_owner()["scene"])
        box = next(el for el in scene["elements"] if el.get("type") == "box" and el.get("author") == MEMBER.name)
        region = [box["x"] - 200, box["y"] - 200, box["x"] + box["w"] * 0.7, box["y"] + box["h"] + 200]
        claim = {"id": "K-1", "author": MEMBER.name, "region": [int(v) for v in region], "label": "working here",
                 "intent": "working here", "at": "2026-10-09T10:00:00.000Z", "expires_at": "2099-01-01T00:00:00.000Z"}
        scene["claims"] = [claim]
        scene.setdefault("counters", {})["K"] = 1
        self.plant(scene)
        return box

    def test_the_operator_is_told_whose_claim_it_is_and_gets_no_op(self):
        box = self.plant_claim()
        told = [p for p in self.found(LEAD) if p["code"] == "claim_edge"]
        self.assertTrue(told and box["id"] in told[0]["ids"], told)
        self.assertIsNone(told[0]["fix"])
        self.assertEqual(told[0]["fix_by"], [MEMBER.name])
        self.assertIn("only alpha-member can redraw this claim", told[0]["message"])

    def test_the_operator_who_obeys_check_piles_up_no_claims(self):
        self.plant_claim()
        filed, loops, _rounds = self.obey(LEAD)
        self.assertEqual(loops, [])
        self.assertEqual(sorted(c["id"] for c in self.scene()["claims"] if c.get("author") == C.HUMAN), [])

    def test_the_claims_author_is_printed_the_claim_and_it_clears(self):
        self.plant_claim()
        printed = [p for p in self.found(MEMBER) if p["code"] == "claim_edge"]
        self.assertTrue(printed and isinstance(printed[0]["fix"], dict), printed)
        filed, loops, _rounds = self.obey(MEMBER)
        self.assertEqual(loops, [])
        self.assertEqual([p for p in self.found(MEMBER) if p["code"] == "claim_edge"], [])
        self.assertEqual(len([c for c in self.scene()["claims"] if c.get("author") == MEMBER.name]), 1, "replaced, not added")


class AlreadyProposed(ObeyRig):
    """A fix the reader may make but that the gate still turns into a proposal (here: the operator froze the graph)
    is named by its proposal while that proposal is open, so obeying check files it once."""

    def test_a_frozen_graphs_redraw_is_proposed_once_and_then_named(self):
        self.plant(_owner()["scene"])
        root = next(el for el in self.scene()["elements"] if el.get("alias") == "link_shortener_flow")
        self.apply({"op": "freeze", "ids": [root["id"]], "label": "final layout", "intent": "hold it"}, LEAD)
        filed, loops, _rounds = self.obey(MEMBER)
        self.assertEqual(loops, [])
        # Several findings describe the same repair, but obeying one check files it only once. Later checks name
        # that exact current proposal, without superseding it or hiding a different repair for the same target.
        self.assertEqual(len(filed), 1, filed)
        mine = [p["id"] for p in self.open_proposals() if p.get("author") == MEMBER.name and p["id"] in filed]
        self.assertEqual(mine, [filed[-1]])
        named = [p for p in self.found(MEMBER) if p.get("proposal") == filed[-1]]
        self.assertTrue(named, "check names the open proposal")
        self.assertIn("your proposal {} already makes this change".format(filed[-1]), named[0]["message"])
        self.assertTrue(all(p["fix"] is None for p in named))

    def _frozen_repair(self):
        self.plant(_owner()["scene"])
        self.apply({"op": "freeze", "ids": [self.root("link_shortener_flow")["id"]], "intent": "review changes"}, LEAD)
        return next(p["fix"] for p in self.found(MEMBER) if isinstance(p.get("fix"), dict)
                    and p["fix"].get("relayout") == "full")

    def test_a_different_payload_for_the_same_target_does_not_hide_the_repair(self):
        repair = self._frozen_repair()
        # Even the repair's intent is not evidence of what the op does: this only proposes changing its title.
        result = C.apply_ops(self.ts.layout, self.ts.team,
                             [{"op": "graph", "id": repair["id"], "title": "A proposed title", "intent": repair["intent"]}], MEMBER)
        self.assertEqual(len(result["proposed"]), 1, result)
        self.assertTrue(any(p.get("fix") == repair for p in self.found(MEMBER)), self.found(MEMBER))

    def test_an_outdated_repair_proposal_does_not_hide_the_current_repair(self):
        repair = self._frozen_repair()
        result = C.apply_ops(self.ts.layout, self.ts.team, [repair], MEMBER)
        self.assertEqual(len(result["proposed"]), 1, result)
        self.apply({"op": "move", "id": self.root("link_shortener_flow")["id"], "by": [20, 0],
                    "intent": "the operator moves the graph"}, LEAD)
        self.assertTrue(any(p.get("fix") == repair for p in self.found(MEMBER)), self.found(MEMBER))

    def test_an_old_record_without_exact_repair_identity_does_not_hide_the_repair(self):
        repair = self._frozen_repair()
        result = C.apply_ops(self.ts.layout, self.ts.team, [repair], MEMBER)
        self.assertEqual(len(result["proposed"]), 1, result)
        scene = self.scene()
        for proposal in scene["proposals"]:
            proposal.pop("repair", None)
        # No current-format proposal event may replay over the old record planted in this fresh team.
        self.new_team()
        self.plant(scene)
        self.assertTrue(any(p.get("fix") == repair for p in self.found(MEMBER)), self.found(MEMBER))


if __name__ == "__main__":
    import unittest

    unittest.main()
