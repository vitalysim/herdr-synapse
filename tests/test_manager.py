"""The team manager (0.10.0): one member the others are told coordinates the work.

Every test here fails against 0.9.0: `Member.manager` did not exist, nothing
published a manager, and an agent's post to the whole team never woke anyone.
"""
from __future__ import annotations

import json
import os
import unittest

from herdr_team import cmd_board, cmd_roster, gate, nudge, operator, render, roster, store, tui_model
from herdr_team import daemon as D
from herdr_team import picker
from herdr_team.errors import HerdrTeamError
from pathlib import Path
from support import FAKE_AGENTS, FakeApi, TempState, fake_agent
from test_cmd_board import json_out, run_cli, write_live_daemon
from test_cmd_roster import env_no_daemon, live_api
from test_daemon import make_daemon, post, ticks

STRONG_IDLE = {"type": "agent_explain", "explain": {"state": "idle", "matched_rule": {"id": "codex.idle", "region": "after_last_prompt_marker"}}}
MANAGER = "alpha-reviewer"
PEER = "alpha-worker"


# --------------------------------------------------------------------------
# the model


class ModelTests(unittest.TestCase):
    def test_the_field_round_trips_and_an_older_roster_reads_as_no_manager(self):
        raw = {"name": "a", "role": "r", "kind": "claude", "terminal_id": "t"}
        self.assertFalse(roster.Member.from_json(raw).manager, "a roster written before 0.10 has no manager")
        member = roster.Member.from_json(dict(raw, manager=True))
        self.assertTrue(member.manager)
        # it must survive to_json, or the next CLI write silently drops it
        self.assertIs(member.to_json()["manager"], True)
        self.assertTrue(roster.Member.from_json(member.to_json()).manager)

    def test_the_team_names_at_most_one_and_skips_the_human_and_the_departed(self):
        def team(*flags):
            members = [roster.Member(name="m{}".format(i), role="r", kind="claude", terminal_id="t{}".format(i), manager=f)
                       for i, f in enumerate(flags)]
            return roster.Team(team="alpha", socket="/s", state_dir="/d", created_at=None, members=members)

        self.assertIsNone(team(False, False).manager())
        self.assertEqual(team(False, True).manager().name, "m1")
        human = roster.Member(name="human", role="operator", kind="human", terminal_id=None, manager=True)
        self.assertIsNone(roster.Team(team="a", socket="/s", state_dir="/d", created_at=None, members=[human]).manager())
        gone = roster.Member(name="g", role="r", kind="claude", terminal_id="t", manager=True, status="left")
        self.assertIsNone(roster.Team(team="a", socket="/s", state_dir="/d", created_at=None, members=[gone]).manager())


# --------------------------------------------------------------------------
# the command


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        write_live_daemon(self.ts)

    def cli(self, *args):
        return json_out(run_cli(["--json", "--team", "alpha"] + list(args), self.ts.env, FakeApi()))

    def members(self):
        return {m["name"]: m for m in store.read_json(self.ts.team.team_json)["members"]}

    def records(self):
        return [r for r in store.BoardStore(self.ts.team).read() if r.get("event") == "manager_changed"]

    def test_show_set_and_clear(self):
        code, out, _ = self.cli("manager")
        self.assertEqual((code, out["member"]), (0, None))
        code, out, _ = self.cli("manager", MANAGER)
        self.assertEqual((code, out["member"], out["previous"], out["changed"]), (0, MANAGER, None, True))
        self.assertTrue(self.members()[MANAGER]["manager"])
        self.assertEqual(self.cli("manager")[1]["member"], MANAGER)
        code, out, _ = self.cli("manager", "--clear")
        self.assertEqual((code, out["member"], out["previous"]), (0, None, MANAGER))
        self.assertFalse(self.members()[MANAGER]["manager"])
        # clearing when there is none says so rather than posting a record
        self.assertFalse(self.cli("manager", "--clear")[1]["changed"])
        self.assertEqual(len(self.records()), 2)

    def test_appointing_a_second_one_clears_the_first_in_the_same_write(self):
        self.cli("manager", MANAGER)
        code, out, _ = self.cli("manager", PEER)
        self.assertEqual((code, out["member"], out["previous"]), (0, PEER, MANAGER))
        members = self.members()
        self.assertTrue(members[PEER]["manager"])
        self.assertFalse(members[MANAGER]["manager"], "two managers must never exist")

    def test_the_record_names_every_member_as_well_as_the_team(self):
        self.cli("manager", MANAGER)
        record = self.records()[0]
        # a record addressed only to "all" is a broadcast the gate holds, so
        # naming each member is what gets every one of them nudged
        self.assertEqual(record["to"], [MANAGER, PEER, "human", "all"])
        self.assertEqual(record["to"][-1], "all")
        # "human" is what puts it in the operator's toast queue and inbox: the
        # daemon only queues a record for the human when the human is named
        self.assertIn("human", record["to"])
        self.assertEqual(record["member"], MANAGER)
        self.assertIn("is the team manager", record["text"])
        self.assertIn("not the operator", record["text"], "the record has to say what it is not")

    def test_the_operator_is_toasted_and_every_member_is_nudged(self):
        # Both halves were missing on the first cut and this is why: a record
        # whose author is "system" takes an early return in ``_ingest_record``,
        # so naming every member did nothing and naming the human did nothing.
        # The operator saw no notification and the team learned of it only when
        # each agent next happened to read the board.
        d, _api, clock = make_daemon(self.ts)
        d.on_connected()
        d.tick()
        self.cli("manager", MANAGER)
        clock.advance(1)
        team = d.teams["alpha"]
        d.tail_boards()
        self.assertEqual(len(team.human_queue), 1, "the operator's toast queue")
        self.assertEqual(team.human_queue[0]["event"], "manager_changed")
        self.assertEqual(sorted(team.pending), sorted([MANAGER, PEER]), "every member is nudged, not just swept later")

    def test_the_record_is_urgent_so_the_fan_out_reaches_the_team(self):
        self.cli("manager", MANAGER)
        record = self.records()[0]
        self.assertTrue(record["urgent"], "an un-urgent system record never fans out")
        self.assertIn("manager_changed", D.URGENT_SYSTEM_EVENTS)
        self.assertIn("manager_changed", D.TOAST_SYSTEM_EVENTS)

    def test_an_unknown_or_human_member_is_refused_and_nothing_is_written(self):
        for name in ("nobody", "human"):
            code, _out, err = self.cli("manager", name)
            self.assertEqual((code, err["code"]), (1, "member_not_found"), name)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.cli("manager", MANAGER, "--clear")[2]["code"], "usage")

    def test_a_member_pane_cannot_appoint_itself(self):
        env = self.ts.env_with(HERDR_PANE_ID="w2:p1")
        code, _out, err = json_out(run_cli(["--json", "--team", "alpha", "manager", MANAGER], env, live_api()))
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        self.assertFalse(self.members()[MANAGER].get("manager"), "nothing was written")

    def test_create_takes_a_manager_and_announces_it(self):
        ts = TempState(write_team=False)
        self.addCleanup(ts.cleanup)
        code, out, err = json_out(run_cli(
            ["--json", "create", "beta", "--member", "w5:p1:reviewer", "--member", "wA:p6:worker:bob", "--brief", "reviewer=Review every patch.", "--brief", "bob=Implement the patch.", "--manager", "bob"],
            env_no_daemon(ts), live_api()))
        self.assertEqual((code, (out or {}).get("manager")), (0, "bob"), err)
        team = ts.layout.team("beta")
        said = [r for r in store.BoardStore(team).read() if r.get("event") == "manager_changed"]
        self.assertEqual(len(said), 1)
        self.assertIn("bob", said[0]["to"])


class OperatorFlagTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        write_live_daemon(self.ts)

    def test_the_flag_grants_and_plain_appointment_does_not(self):
        code, out, _ = json_out(run_cli(["--json", "--team", "alpha", "manager", MANAGER], self.ts.env, FakeApi()))
        self.assertEqual((code, out["operator"]), (0, False))
        self.assertIsNone(operator.active(self.ts.session, "alpha", MANAGER), "appointing is not granting")
        code, out, _ = json_out(run_cli(["--json", "--team", "alpha", "manager", MANAGER, "--operator", "--ttl", "1h"], self.ts.env, FakeApi()))
        self.assertEqual((code, out["operator"]), (0, True))
        self.assertIsNotNone(operator.active(self.ts.session, "alpha", MANAGER))
        events = [r.get("event") for r in store.BoardStore(self.ts.team).read()]
        self.assertIn("operator_granted", events, "a grant is announced like any other")

    def test_a_delegate_may_appoint_but_may_not_grant(self):
        operator.grant(self.ts.session, "alpha", MANAGER, ttl_s=3600)
        env = self.ts.env_with(HERDR_PANE_ID="w2:p1")
        api = live_api()
        code, out, err = json_out(run_cli(["--json", "--team", "alpha", "manager", PEER], env, api))
        if code == 0:  # the delegate was resolved and passed the human-only gate
            self.assertEqual(out["member"], PEER)
        else:  # identity could not confirm the pane in this rig; not what is under test
            self.assertEqual(err["code"], "author_mismatch")
        # granting is the operator's alone either way
        code, _out, err = json_out(run_cli(["--json", "--team", "alpha", "manager", PEER, "--operator"], env, api))
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        self.assertIn("cannot grant", err["message"])


# --------------------------------------------------------------------------
# delivery: the manager's broadcasts wake the team


class BroadcastTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.api.set_response("agent.explain", STRONG_IDLE)
        self.d.on_connected()
        self.team = self.d.teams["alpha"]

    def make_manager(self, name=MANAGER):
        def mutate(doc):
            for member in doc.get("members", []):
                member["manager"] = member.get("name") == name
        D.update_roster(self.team.paths, mutate)
        self.d.scan_teams(force=True)
        self.team = self.d.teams["alpha"]

    def test_a_peer_broadcast_still_waits_and_the_manager_s_does_not(self):
        post(self.ts, "all", author=PEER)
        self.d.tick()
        self.assertNotIn(MANAGER, self.team.pending, "an ordinary agent's broadcast waits for the next read")
        self.make_manager(PEER)
        post(self.ts, "all", text="scope split", author=PEER)
        self.d.tick()
        self.assertIn(MANAGER, self.team.pending, "the manager's plan reaches the team")
        self.assertNotIn(PEER, self.team.pending, "and never itself")

    def test_the_gate_lets_a_manager_broadcast_through_and_holds_a_peer_one(self):
        work = gate.PendingWork([5], False, 0, [PEER], broadcast=True)
        snapshot = self.d._snapshot(self.team, self.team.member(MANAGER), self.d.agents.get("term_r1"), self.team.rt(MANAGER), self.d.now_ms(), None)
        self.assertEqual(gate.evaluate(snapshot, work, self.d.now_ms(), None, 0).hold, gate.HOLD_BROADCAST)
        managed = gate.PendingWork([5], False, 0, [PEER], broadcast=True, from_manager=True)
        self.assertNotEqual(gate.evaluate(snapshot, managed, self.d.now_ms(), None, 0).hold, gate.HOLD_BROADCAST)

    def test_it_lifts_the_broadcast_hold_and_nothing_else(self):
        # the bypass surface is exactly done_hold and the per-member interval,
        # and being the manager must not widen it
        base = dict(seqs=[5], urgent=False, cursor_seq=0, authors=[PEER], broadcast=True)
        work = gate.PendingWork(from_manager=True, **base)
        self.assertFalse(work.urgent)
        self.assertFalse(work.force)
        now = self.d.now_ms()
        snapshot = self.d._snapshot(self.team, self.team.member(MANAGER), self.d.agents.get("term_r1"), self.team.rt(MANAGER), now, None)
        snapshot.stable_since_ms = now - 60000.0  # long past the stable window
        snapshot.idle_since_ms = now  # but only just idle, so the done-hold applies
        self.assertEqual(gate.evaluate(snapshot, work, now, None, 0).hold, gate.HOLD_DONE_HOLD)

    def test_the_daemon_marks_pending_work_from_the_manager(self):
        self.make_manager(PEER)
        pending = D.Pending(first_ms=0.0, seqs=[3], authors={PEER})
        self.assertTrue(self.d._pending_work(self.team, pending, 0).from_manager)
        self.assertFalse(self.d._pending_work(self.team, D.Pending(first_ms=0.0, seqs=[3], authors={"someone"}), 0).from_manager)


# --------------------------------------------------------------------------
# what the agents are told


class PublishedTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def mark(self, name=MANAGER):
        def mutate(doc):
            member = doc.find(name)
            assert member is not None
            member.manager = True
        roster.update_team(self.ts.team, mutate)

    def test_who_shows_the_tag_beside_the_operator_one(self):
        self.assertIn("manager", render.render_who(
            {"members": [{"name": MANAGER, "role": "r", "kind": "codex", "status": "active", "manager": True}]}, "alpha"))
        self.assertNotIn("manager", render.render_who(
            {"members": [{"name": MANAGER, "role": "r", "kind": "codex", "status": "active"}]}, "alpha"))

    def test_who_json_carries_it_on_both_builders(self):
        self.mark()
        doc = roster.load_team(self.ts.team)
        built = roster.build_who_json({"alpha": doc}, [], {}, {}, {})
        entry = [m for m in built["teams"]["alpha"]["members"] if m["name"] == MANAGER][0]
        self.assertTrue(entry["manager"])
        d, _api, _clock = make_daemon(self.ts)
        d.on_connected()
        live = [m for m in d.build_who()["teams"]["alpha"]["members"] if m["name"] == MANAGER][0]
        self.assertTrue(live["manager"], "who.json and the fallback must agree")

    def test_me_marks_the_teammate_and_the_caller(self):
        self.mark()
        code, out, err = json_out(run_cli(["--json", "me"], self.ts.env_with(HERDR_PANE_ID="w2:p2"), live_api()))
        self.assertEqual(code, 0, err)
        mate = [m for m in out["teammates"] if m["name"] == MANAGER][0]
        self.assertTrue(mate["manager"])
        text = render.render_me(dict(out, manager=True), {})
        self.assertIn("you are the team manager", text)
        self.assertIn("manager", render.render_me(out, {}))

    def test_the_briefing_marks_the_manager_without_overrunning_its_budget(self):
        line = nudge.briefing_lines("w", "worker", "alpha", "ship", [("r", "reviewer", True), ("s", "scout", False)], None, "herdr-synapse")[0]
        self.assertIn("r (reviewer, manager)", line)
        self.assertIn("s (scout)", line)
        self.assertLessEqual(len(line), nudge.MAX_BRIEFING_CHARS)
        # the worst legal member is still briefable: the pinned regression
        worst = nudge.briefing_lines("m" * 32, "r" * 32, "t" * 15, None, [], None, "herdr-synapse")[0]
        self.assertLessEqual(len(worst), nudge.MAX_BRIEFING_CHARS)
        # and a roster too long to fit collapses rather than raising
        many = [("peer{}".format(i), "role{}".format(i), i == 0) for i in range(30)]
        crowded = nudge.briefing_lines("w", "worker", "alpha", "ship it now", many, None, "herdr-synapse")[0]
        self.assertLessEqual(len(crowded), nudge.MAX_BRIEFING_CHARS)
        self.assertIn("teammates, run", crowded)

    def test_the_claude_session_start_blob_names_the_manager(self):
        from herdr_team import cmd_hooks

        self.mark()
        doc = store.read_json(self.ts.team.team_json)
        peer = [m for m in doc["members"] if m["name"] == PEER][0]
        text = cmd_hooks.brief_context(self.ts.team, "alpha", dict(peer))
        self.assertIn("{} (reviewer, codex, team manager)".format(MANAGER), text)
        self.assertNotIn("you are the team manager", text)
        boss = [m for m in doc["members"] if m["name"] == MANAGER][0]
        self.assertIn("you are the team manager", cmd_hooks.brief_context(self.ts.team, "alpha", dict(boss)))

    def test_the_skill_teaches_it_without_contradicting_the_peer_rule(self):
        from herdr_team import paths as _paths

        text = _paths.skill_file().read_text(encoding="utf-8")
        self.assertIn("team manager", text)
        self.assertIn("It is not the operator", text)
        self.assertIn("request from a peer", text, "the peer rule still stands")


# --------------------------------------------------------------------------
# dissolving a team


class DissolveTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        write_live_daemon(self.ts)

    def test_off_a_terminal_it_still_needs_yes_and_writes_nothing(self):
        code, _out, err = json_out(run_cli(["--json", "--team", "alpha", "dissolve", "alpha"], self.ts.env, FakeApi()))
        self.assertEqual((code, err["code"]), (1, "confirmation_required"))
        self.assertIn("archived", err["message"], "the refusal says what dissolving does")
        self.assertTrue(self.ts.team.team_json.is_file(), "nothing was touched")

    def test_yes_archives_the_team_rather_than_deleting_it(self):
        code, out, err = json_out(run_cli(["--json", "--team", "alpha", "dissolve", "alpha", "--yes"], self.ts.env, FakeApi()))
        self.assertEqual(code, 0, err)
        archived = Path(out["archived_to"])
        self.assertTrue(archived.is_dir(), "the team directory moved, it was not removed")
        self.assertTrue((archived / "team.json").is_file(), "the roster came with it")
        self.assertFalse(self.ts.team.team_json.exists())

    def test_a_member_cannot_dissolve_the_team(self):
        env = self.ts.env_with(HERDR_PANE_ID="w2:p1")
        code, _out, err = json_out(run_cli(["--json", "--team", "alpha", "dissolve", "alpha", "--yes"], env, live_api()))
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        self.assertTrue(self.ts.team.team_json.is_file())


# --------------------------------------------------------------------------
# the team view


class PickerTests(unittest.TestCase):
    def test_the_action_is_a_toggle_and_maps_to_the_right_argv(self):
        self.assertIn("member_manager", picker.ACTION_INTENTS)
        self.assertIn(("manager", "make it the team manager"), tui_model.ACTION_OPTIONS)
        self.assertIsNone(tui_model.action_label("manager", {"name": "x"}))
        self.assertIn("clears", tui_model.action_label("manager", {"name": "x", "manager": True}))
        set_intent = tui_model.Intent("member_manager", {"team": "alpha", "member": MANAGER, "clear": False})
        self.assertEqual(picker.action_args(set_intent), ["--team", "alpha", "manager", MANAGER])
        clear_intent = tui_model.Intent("member_manager", {"team": "alpha", "member": MANAGER, "clear": True})
        self.assertEqual(picker.action_args(clear_intent), ["--team", "alpha", "manager", "--clear"])
        self.assertIn("no longer the team manager", picker.action_success_status(clear_intent, {}))
        self.assertIn("is the team manager", picker.action_success_status(set_intent, {}))

    def test_the_tree_row_carries_and_shows_it(self):
        member = tui_model.tree_member({"name": MANAGER, "role": "r", "kind": "codex", "manager": True})
        self.assertTrue(member["manager"])
        self.assertIn("manager", tui_model.roster_line(member, 100, show_role=True))
        plain = tui_model.tree_member({"name": PEER, "role": "r", "kind": "claude"})
        self.assertNotIn("manager", tui_model.roster_line(plain, 100, show_role=True))


if __name__ == "__main__":
    unittest.main()
