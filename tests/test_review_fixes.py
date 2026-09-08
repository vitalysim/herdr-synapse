"""The 2026-09-08 review, fixed (0.13.0). Every test here fails against 0.12.1.

Numbered as the review was:

1. any agent could become the operator by setting ``HERDR_PLUGIN_ENTRYPOINT_ID``
2. a Codex/OpenCode member compacted by its own harness was never re-briefed,
   and ``context_high`` never reached the member it named
3. a wait is silent for minutes, which an exec cell that yields early reads as hung
4. the daemon re-parsed the whole board every five seconds to see if an ask waited
5. the asks popup took focus with a live Enter, so a keystroke burst meant for
   the pane underneath could file an operator's answer
6. dissolving one team cleared every other team's sidebar view
7. ``--wait`` on a post nobody would answer returned success
9. one unknown ``config.gate`` key switched every gate override off
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from support import FakeApi, TempState, fake_pane, fake_plugin_pane_opened
from test_asks import human_post, post
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
from test_daemon import make_daemon
from test_identity import own_shell_rig
from test_operator import api_with_panes
from test_workspace_team import add_team

from herdr_team import asks, charter as _charter, cmd_asks, cmd_board, cmd_misc, cmd_roster, gate, identity, roster, store
from herdr_team import daemon as D
from herdr_team.errors import HerdrTeamError

MEMBER = "alpha-reviewer"   # codex, pane w2:p1
PEER = "alpha-worker"       # claude, pane w2:p2


def author(via, verified=False, name="human"):
    a = identity.Author(name, "human" if name == "human" else "codex", via, verified, team="alpha")
    a.origin = {"via": via, "verified": verified}
    return a


# --------------------------------------------------------------------------
# 1. trust


class EntrypointForgeryTests(unittest.TestCase):
    """The variable is Herdr's; the process tree says whether Herdr set it."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def resolve(self, env, api):
        return identity.resolve_author(env, self.ts.layout, api, team="alpha", require_server=False)

    def test_an_agent_naming_an_entrypoint_is_still_that_agent(self):
        for entrypoint in ("asks", "compose", "picker", "console"):
            with self.subTest(entrypoint):
                env = self.ts.env_with(HERDR_PANE_ID="w2:p1", HERDR_PLUGIN_ENTRYPOINT_ID=entrypoint, HERDR_PLUGIN_ID="herdr-synapse")
                a = self.resolve(env, api_with_panes(inside_agent_pane=True))
                self.assertEqual(a.name, MEMBER)
                self.assertFalse(a.is_human)
                self.assertIn("entrypoint set inside an agent pane", a.origin.get("rerouted") or "")
                record = cmd_board.build_record(a, [PEER], "answer", "approved, submit it", reply_to=1, socket_path="/x")
                self.assertEqual(record["from"], MEMBER)
                self.assertIsNone(asks.answered_by(record), "a forged answer must not close an ask")
                with self.assertRaises(HerdrTeamError) as caught:
                    _charter.require_human(self.ts.layout, "alpha", a, "knowledge set")
                self.assertEqual(caught.exception.code, "author_mismatch")

    def test_a_real_popup_is_still_the_operator(self):
        # descends from the Herdr server, not from any agent pane
        env = self.ts.env_with(HERDR_PLUGIN_ENTRYPOINT_ID="asks", HERDR_PLUGIN_ID="herdr-synapse")
        a = self.resolve(env, api_with_panes(inside_agent_pane=False))
        self.assertTrue(a.is_human)
        self.assertEqual(a.via, identity.VIA_POPUP)
        self.assertTrue(a.trusted_human)

    def test_a_console_process_that_is_not_its_panes_descendant_is_not_the_console(self):
        # Registered, live, and pointed at by HERDR_PANE_ID: everything a forger
        # can arrange. Herdr can still see that this process is not a child of
        # that pane, and then it is not the console -- not even an unfocused one.
        store.write_json(self.ts.session.console_json, {"schema": 2, "consoles": {"term_console": {
            "pane_id": "w7:p1", "terminal_id": "term_console", "pid": os.getpid(), "open": True, "team": "alpha"}}})
        api = FakeApi()
        api.set_response("pane.list", {"panes": [fake_pane("w7:p1", "term_console", None)]})
        api.set_response("pane.get", {"pane": fake_pane("w7:p1", "term_console", None)})
        api.set_response("pane.process_info", {"process_info": {"pane_id": "w7:p1", "shell_pid": 1, "foreground_process_group_id": 1, "foreground_processes": [{"pid": 2, "name": "sh"}]}})
        env = self.ts.env_with(HERDR_PANE_ID="w7:p1", HERDR_PLUGIN_ENTRYPOINT_ID="console", HERDR_PLUGIN_ID="herdr-synapse")
        with mock.patch.object(identity, "ps_table", return_value={os.getpid(): os.getppid(), os.getppid(): 1}):
            a = self.resolve(env, api)
        self.assertTrue(a.is_human)
        self.assertEqual(a.via, identity.VIA_CLI_UNVERIFIED)
        self.assertFalse(a.trusted_human)


class AuthorityGateTests(unittest.TestCase):
    """The gates test the origin, not the name, and all three agree."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def gates(self, a):
        out = {}
        for label, fn in (("require_human", lambda: _charter.require_human(self.ts.layout, "alpha", a, "x")),
                          ("_human_only", lambda: cmd_roster._human_only(self.ts.layout, "alpha", a, "x")),
                          ("_strictly_human", lambda: cmd_roster._strictly_human(self.ts.layout, "alpha", a, "x"))):
            try:
                fn()
                out[label] = "ok"
            except HerdrTeamError as err:
                out[label] = err.code
        return out

    def test_trusted_origins_pass_and_an_unverified_shell_does_not(self):
        for via, verified in ((identity.VIA_CONSOLE, True), (identity.VIA_CONSOLE_UNFOCUSED, False),
                              (identity.VIA_POPUP, False), (identity.VIA_OUTSIDE, False), (identity.VIA_CLI, True)):
            with self.subTest(via):
                self.assertTrue(author(via, verified).trusted_human)
                self.assertEqual(set(self.gates(author(via, verified)).values()), {"ok"})
        shell = author(identity.VIA_CLI_UNVERIFIED, False)
        shell.reason = "pane process-info unavailable for w3:p1"
        self.assertFalse(shell.trusted_human)
        self.assertEqual(set(self.gates(shell).values()), {"author_mismatch"})

    def test_the_refusal_says_how_a_real_operator_gets_trusted(self):
        shell = author(identity.VIA_CLI_UNVERIFIED, False)
        with self.assertRaises(HerdrTeamError) as caught:
            _charter.require_human(self.ts.layout, "alpha", shell, "knowledge set")
        for hint in ("team console", "focused Herdr pane", "env -u HERDR_PANE_ID"):
            self.assertIn(hint, caught.exception.message)
        # a member's refusal is the plain one, not the operator's hint
        with self.assertRaises(HerdrTeamError) as caught:
            _charter.require_human(self.ts.layout, "alpha", author(identity.VIA_CLI, True, name=MEMBER), "knowledge set")
        self.assertIn("human only", caught.exception.message)

    def test_one_rule_for_records_and_authors(self):
        for via, verified in ((identity.VIA_CONSOLE, True), (identity.VIA_CONSOLE_UNFOCUSED, False), (identity.VIA_POPUP, False),
                              (identity.VIA_OUTSIDE, False), (identity.VIA_CLI, True), (identity.VIA_CLI_UNVERIFIED, False), (identity.VIA_CLI, False)):
            record = {"seq": 1, "from": "human", "kind": "note", "to": [MEMBER], "origin": {"via": via, "verified": verified}}
            self.assertEqual(D.Daemon._counts_for_nudges(record), author(via, verified).trusted_human, via)


class AskClosingTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_only_a_trusted_human_reply_closes_an_ask(self):
        seq = post(self.ts)
        unverified = {"from": "human", "from_kind": "human", "from_terminal": "term_x", "origin": {"via": "cli", "verified": False},
                      "to": [MEMBER], "kind": "answer", "text": "sure", "reply_to": seq}
        self.assertIsNone(asks.answered_by(dict(unverified, seq=seq + 1)))
        store.BoardStore(self.ts.team).append(unverified)
        self.assertEqual([r["seq"] for r in asks.pending(self.ts.team)], [seq], "an unverified reply leaves the ask waiting")
        human_post(self.ts, reply_to=seq)
        self.assertEqual(asks.pending(self.ts.team), [])

    def test_a_waiting_member_is_not_released_by_an_unverified_reply(self):
        seq = post(self.ts)
        store.BoardStore(self.ts.team).append({"from": "human", "from_kind": "human", "from_terminal": "term_x",
                                                "origin": {"via": "cli", "verified": False}, "to": [MEMBER], "kind": "answer", "text": "go", "reply_to": seq})
        self.assertIsNone(cmd_board.wait_for_answer(self.ts.team, seq, 0.05, poll_s=0.01))


# --------------------------------------------------------------------------
# 2. compaction and context_high delivery


class CompactionRebriefTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.on_connected()
        self.team = self.d.teams["alpha"]
        self.d._apply_changes(self.team, [(MEMBER, {"briefed_at": roster.now_iso()})])

    def briefs(self):
        return [store.read_json(p) for p in sorted(self.ts.team.jobs_dir.glob("*.json")) if store.read_json(p).get("kind") == "brief"]

    def test_a_compaction_nobody_asked_for_re_briefs_the_member(self):
        from herdr_team import context

        rt = self.team.rt(MEMBER)
        rt.context_session = roster.session_key(self.team.member(MEMBER).get("session"))
        previous = {"used": 180_000, "window": 200_000, "percent": 90.0}
        self.assertIsNone(rt.control_pending, "nobody typed /compact")
        self.d._check_context_drop(self.team, MEMBER, context.Reading(used=20_000, window=200_000, source="/t"), previous, self.clock() * 1000)
        self.assertEqual(len([r for r in store.BoardStore(self.ts.team).read() if r.get("event") == "context_compacted"]), 1)
        self.assertIsNone(self.team.member(MEMBER)["briefed_at"], "it must look unbriefed again")
        self.assertEqual([j["member"] for j in self.briefs()], [MEMBER])

    def test_context_high_nudges_the_member_it_names_and_nobody_else(self):
        rec = {"seq": 501, "from": "system", "from_kind": "system", "kind": "system", "event": "context_high",
               "to": [MEMBER, "all"], "text": "at 90%", "origin": {"via": "system", "verified": True}, "urgent": False}
        self.d._ingest_record(self.team, rec)
        self.assertIn(MEMBER, self.team.pending)
        self.assertEqual(self.team.pending[MEMBER].seqs, [501])
        self.assertFalse(self.team.pending[MEMBER].urgent, "an ordinary nudge: every gate still applies")
        self.assertNotIn(PEER, self.team.pending)

    def test_the_delivery_table_replaces_both_lists_without_changing_them(self):
        self.assertEqual(D.URGENT_SYSTEM_EVENTS, ("charter_updated", "member_joined", "knowledge_updated", "instructions_updated", "manager_changed"))
        self.assertEqual(D.TOAST_SYSTEM_EVENTS, ("manager_changed",))
        self.assertEqual(D.NAMED_SYSTEM_EVENTS, ("context_high", "model_changed", "link_established", "link_broken"))
        declared_without_wake = {e for e, d in D.SYSTEM_EVENT_DELIVERY.items() if not d.get("wake")}
        self.assertEqual(declared_without_wake, {"link_read", "board_cleared"}, "declared so it is deliberate; wakes nobody (the sweep or the console carries it)")
        self.assertEqual(set(D.URGENT_SYSTEM_EVENTS) | set(D.NAMED_SYSTEM_EVENTS) | declared_without_wake, set(D.SYSTEM_EVENT_DELIVERY))


# --------------------------------------------------------------------------
# 3 and 7. the wait


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_a_long_wait_says_it_is_still_waiting(self):
        seq = post(self.ts)
        beats = []
        self.assertIsNone(cmd_board.wait_for_answer(self.ts.team, seq, 0.12, poll_s=0.01, heartbeat=beats.append, heartbeat_s=0.03))
        self.assertGreaterEqual(len(beats), 2)
        self.assertTrue(all(isinstance(b, float) and b > 0 for b in beats))
        self.assertEqual(beats, sorted(beats))

    def test_the_heartbeat_goes_to_stderr_not_the_json(self):
        import io

        seq = post(self.ts)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cmd_board, "WAIT_HEARTBEAT_S", 0.02):
            import argparse

            args = argparse.Namespace(stderr=err, stdout=out)
            cmd_board.wait_for_answer(self.ts.team, seq, 0.08, poll_s=0.01,
                                      heartbeat=lambda e: cmd_board.warn(args, "still waiting for the operator ({:.0f}s of 0s)".format(e)),
                                      heartbeat_s=0.02)
        self.assertIn("still waiting for the operator", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_wait_on_a_post_nobody_would_answer_is_a_usage_error_and_posts_nothing(self):
        env = self.ts.env_with(HERDR_PANE_ID="w2:p1")
        code, _out, err = json_out(run_cli(["--json", "--team", "alpha", "post", "--to", PEER, "--kind", "question", "--wait", "hm?"], env, live_api()))
        self.assertEqual((code, err["code"]), (2, "usage"))
        self.assertIn("--to human", err["message"])
        self.assertEqual(store.BoardStore(self.ts.team).read(), [], "a refused wait must leave nothing on the board")
        # the operator waiting on themself
        code, _out, err = json_out(run_cli(["--json", "--team", "alpha", "post", "--to", "human", "--kind", "question", "--wait", "hm?"], env_no_daemon(self.ts), live_api()))
        self.assertEqual((code, err["code"]), (2, "usage"))
        self.assertIn("operator", err["message"])
        self.assertEqual(store.BoardStore(self.ts.team).read(), [])


# --------------------------------------------------------------------------
# 4. the tick reads nothing


class AskTrackingTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.on_connected()
        self.team = self.d.teams["alpha"]

    def wanted(self):
        with mock.patch.object(store.BoardStore, "read", side_effect=AssertionError("the ask scan read the board")):
            with mock.patch.object(store.BoardStore, "read_detailed", side_effect=AssertionError("the ask scan read the board")):
                return self.d._ask_popup_wanted(self.team)

    def test_the_scan_follows_the_records_it_already_ingests(self):
        self.assertFalse(self.wanted())
        seq = post(self.ts)
        self.d.tail_boards()
        self.assertTrue(self.wanted())
        self.assertEqual(list(self.team.open_asks), [seq])
        human_post(self.ts, reply_to=seq)
        self.d.tail_boards()
        self.assertFalse(self.wanted())

    def test_a_retraction_and_a_dismissal_both_clear_it(self):
        seq = post(self.ts)
        self.d.tail_boards()
        self.assertTrue(self.wanted())
        asks.dismiss(self.ts.team, [seq])
        self.assertFalse(self.wanted(), "dismissed: not now")
        seq2 = post(self.ts, text="another")
        self.d.tail_boards()
        self.assertTrue(self.wanted())
        store.BoardStore(self.ts.team).append({"from": MEMBER, "from_kind": "codex", "from_terminal": "term_r1", "origin": {"via": "cli", "verified": True},
                                                "to": ["human"], "kind": "retract", "text": "retracted", "retracts": seq2})
        self.d.tail_boards()
        self.assertFalse(self.wanted())

    def test_a_daemon_started_after_the_ask_still_knows_about_it(self):
        seq = post(self.ts)
        d, _api, _clock = make_daemon(self.ts)
        d.on_connected()
        self.assertEqual(list(d.teams["alpha"].open_asks), [seq], "seeded once at start")

    def test_the_set_is_bounded(self):
        for i in range(D.OPEN_ASKS_MAX + 5):
            self.d._track_ask(self.team, {"seq": i, "from": MEMBER, "kind": "question", "to": ["human"], "origin": {"via": "cli", "verified": True}})
        self.assertEqual(len(self.team.open_asks), D.OPEN_ASKS_MAX)
        self.assertEqual(min(self.team.open_asks), 5, "the oldest go first")


# --------------------------------------------------------------------------
# 5. the popup arms itself


class PopupArmingTests(unittest.TestCase):
    def model(self, armed):
        record = {"seq": 7, "from": MEMBER, "kind": "question", "text": "ship it?", "to": ["human"]}
        return cmd_asks.AskModel(team="alpha", asks=[record], armed=armed)

    def test_a_burst_before_arming_changes_nothing_and_files_nothing(self):
        m = self.model(armed=False)
        for key in ("y", "e", "s", "ENTER", "CTRL_A", "TAB", "BACKSPACE"):
            self.assertEqual(cmd_asks.ask_key(m, key).kind, "none", key)
        self.assertEqual(m.input, "")
        self.assertNotIn("> ", "\n".join(cmd_asks.ask_lines(m)))
        self.assertIn("…", "\n".join(cmd_asks.ask_lines(m)))

    def test_leaving_needs_no_arming(self):
        self.assertEqual(cmd_asks.ask_key(self.model(armed=False), "ESC").kind, "dismiss")
        self.assertEqual(cmd_asks.ask_key(self.model(armed=False), "q").kind, "quit")

    def test_the_same_keys_work_once_armed(self):
        m = self.model(armed=True)
        for key in "yes":
            cmd_asks.ask_key(m, key)
        self.assertEqual(m.input, "yes")
        intent = cmd_asks.ask_key(m, "ENTER")
        self.assertEqual((intent.kind, intent.args["text"]), ("reply", "yes"))
        self.assertIn("> yes", "\n".join(cmd_asks.ask_lines(self.model(armed=True)).__class__(cmd_asks.ask_lines(m))))

    def test_the_default_model_is_armed_for_every_other_caller(self):
        self.assertTrue(cmd_asks.AskModel(team="alpha").armed)
        self.assertGreater(cmd_asks.ARM_S, 0)


# --------------------------------------------------------------------------
# 6. dissolve keeps the other teams' view


class DissolveViewTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        add_team(self.ts, "beta", "w7")
        cmd_misc.write_view_state(self.ts.layout, ["alpha", "beta"])
        self.api = live_api()
        self.api.set_response("agent.view.set", {"type": "ok"})
        self.api.set_response("agent.view.clear", {"type": "ok"})

    def views(self, method):
        return [p for m, p in self.api.calls if m == method]

    def test_dissolving_one_team_rebuilds_the_view_for_the_rest(self):
        code, payload, err = json_out(run_cli(["--json", "dissolve", "beta", "--yes"], env_no_daemon(self.ts), self.api))
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["view_kept"], ["alpha"])
        self.assertFalse(payload["view_cleared"])
        self.assertEqual(self.views("agent.view.clear"), [])
        self.assertEqual([p["filter"] for p in self.views("agent.view.set")], [cmd_misc.view_request(["alpha"])["filter"]])
        self.assertEqual(store.read_json(self.ts.session.view_json)["teams"], ["alpha"])

    def test_the_last_team_clears_it_as_before(self):
        json_out(run_cli(["--json", "dissolve", "beta", "--yes"], env_no_daemon(self.ts), self.api))
        code, payload, err = json_out(run_cli(["--json", "dissolve", "alpha", "--yes"], env_no_daemon(self.ts), self.api))
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["view_cleared"])
        self.assertEqual(payload["view_kept"], [])
        self.assertEqual(len(self.views("agent.view.clear")), 1)
        self.assertFalse(self.ts.session.view_json.exists())


# --------------------------------------------------------------------------
# 9. one bad gate key


class GateLeniencyTests(unittest.TestCase):
    def test_an_unknown_key_is_skipped_and_named_the_rest_applies(self):
        cfg, unknown = gate.GateConfig.from_mapping_lenient({"done_hold_ms": 0, "typo": 1, "another": 2})
        self.assertEqual((cfg.done_hold_ms, unknown), (0, ["another", "typo"]))
        self.assertEqual(gate.GateConfig.from_mapping_lenient(None), (gate.GateConfig(), []))

    def test_the_strict_form_still_refuses_for_callers_that_typed_the_mapping(self):
        with self.assertRaises(ValueError):
            gate.GateConfig.from_mapping({"typo": 1})

    def test_the_daemon_applies_what_it_understood(self):
        cfg, applied, error, skipped = D.gate_config_from_roster_detailed({"config": {"gate": {"done_hold_ms": 0, "typo": 1}}})
        self.assertEqual((cfg.done_hold_ms, applied, error, skipped), (0, {"done_hold_ms": 0}, None, ["typo"]))
        # a bad value for a known key is still the whole default: that one is a mistake, not a typo
        cfg, applied, error, skipped = D.gate_config_from_roster_detailed({"config": {"gate": {"done_hold_ms": "soon"}}})
        self.assertIs(cfg, gate.DEFAULT_CONFIG)
        self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main()
