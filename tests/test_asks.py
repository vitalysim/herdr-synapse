"""Human in the loop (0.12.0): the operator is asked, and the agent waits.

Every test here fails against 0.11.0: nothing knew which posts were waiting on
the operator, `post` never blocked, there was no popup and no policy.
"""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from unittest import mock

from herdr_team import asks, cmd_asks, cmd_misc, store, tui_model
from herdr_team import daemon as D
from herdr_team.errors import EXIT_NO_ANSWER, HerdrTeamError
from support import FakeApi, TempState
from test_cmd_board import json_out, run_cli, write_live_daemon


def err_of(err):
    """The JSON error object, past any `warning:` lines argparse put first."""
    if isinstance(err, dict):
        return err
    for line in str(err or "").splitlines():
        if line.lstrip().startswith("{"):
            return json.loads(line)
    return {}
from test_cmd_roster import live_api
from test_daemon import make_daemon

MEMBER = "alpha-reviewer"
PEER = "alpha-worker"


def post(ts, text="need a call", kind="question", author=MEMBER, to=("human",), reply_to=None):
    rec = {"from": author, "from_kind": "codex", "from_terminal": "term_r1",
           "origin": {"via": "cli", "verified": True}, "to": list(to), "kind": kind, "text": text}
    if reply_to is not None:
        rec["reply_to"] = reply_to
    return store.BoardStore(ts.team).append(rec)


def human_post(ts, text="go ahead", reply_to=None, kind="answer"):
    rec = {"from": "human", "from_kind": "human", "from_terminal": "term_console",
           "origin": {"via": "console", "verified": True}, "to": [MEMBER], "kind": kind, "text": text}
    if reply_to is not None:
        rec["reply_to"] = reply_to
    return store.BoardStore(ts.team).append(rec)


# --------------------------------------------------------------------------
# which posts are waiting


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def pending(self):
        return [r["seq"] for r in asks.pending(self.ts.team, dismissed=asks.dismissed(self.ts.team))]

    def test_a_post_to_the_operator_waits_until_the_operator_answers(self):
        seq = post(self.ts)
        self.assertEqual(self.pending(), [seq])
        human_post(self.ts, reply_to=seq)
        self.assertEqual(self.pending(), [])

    def test_a_peer_reply_does_not_close_it(self):
        # This is the whole point. On the team this was built for, four of the
        # ten asks were "answered" by another agent, and one of those overrode
        # a genuine pre-submission halt.
        seq = post(self.ts)
        post(self.ts, "STOP ORDER IS STALE", kind="answer", author=PEER, to=(MEMBER,), reply_to=seq)
        self.assertEqual(self.pending(), [seq], "only the operator closes an ask")

    def test_only_posts_addressed_to_the_operator_count(self):
        post(self.ts, to=("all",))
        post(self.ts, to=(PEER,))
        self.assertEqual(self.pending(), [])
        seq = post(self.ts, to=(PEER, "human"))
        self.assertEqual(self.pending(), [seq], "naming the operator among others still asks")

    def test_the_operator_and_the_system_never_ask_themselves(self):
        human_post(self.ts, "a note to myself")
        store.BoardStore(self.ts.team).append({
            "from": "system", "kind": "system", "event": "toast", "to": ["human"], "text": "toast shown",
            "origin": {"via": "system", "verified": True}})
        self.assertEqual(self.pending(), [])

    def test_retracted_and_dismissed_asks_drop_out(self):
        seq = post(self.ts)
        keep = post(self.ts, "second")
        store.BoardStore(self.ts.team).append({
            "from": MEMBER, "from_kind": "codex", "origin": {"via": "cli", "verified": True},
            "to": ["human"], "kind": "retract", "text": "retracted", "retracts": seq})
        self.assertEqual(self.pending(), [keep])
        asks.dismiss(self.ts.team, [keep])
        self.assertEqual(self.pending(), [], "dismissed is 'not now', and the popup stops reopening")
        self.assertEqual(asks.dismissed(self.ts.team), [keep])

    def test_newest_first(self):
        first = post(self.ts, "one")
        second = post(self.ts, "two")
        self.assertEqual(self.pending(), [second, first])

    def test_which_kinds_block(self):
        policy = {"block": True, "block_kinds": ["question", "blocked", "request"]}
        for kind in ("question", "blocked", "request"):
            self.assertTrue(asks.blocks(kind, policy), kind)
        for kind in ("note", "done", "answer", "handoff"):
            self.assertFalse(asks.blocks(kind, policy), kind)
        self.assertFalse(asks.blocks("question", dict(policy, block=False)), "block:false wins")


# --------------------------------------------------------------------------
# the wait


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.env = self.ts.env_with(HERDR_PANE_ID="w2:p1")

    def test_it_returns_the_operators_answer(self):
        answered = {}

        def answer():
            for _ in range(100):
                rows = [r for r in store.BoardStore(self.ts.team).read() if r.get("kind") == "question"]
                if rows:
                    answered["seq"] = human_post(self.ts, "proceed", reply_to=rows[-1]["seq"])
                    return
                time.sleep(0.05)

        thread = threading.Thread(target=answer)
        thread.start()
        code, out, err = json_out(run_cli(
            ["--json", "post", "--to", "human", "--kind", "question", "--timeout", "20s", "need a call"],
            self.env, live_api()))
        thread.join()
        self.assertEqual(code, 0, err)
        self.assertTrue(out["waited"])
        self.assertEqual((out["answer"]["from"], out["answer"]["text"]), ("human", "proceed"))

    def test_nobody_answers(self):
        started = time.monotonic()
        code, _out, err = json_out(run_cli(
            ["--json", "post", "--to", "human", "--kind", "question", "--timeout", "1s", "need a call"],
            self.env, live_api()))
        self.assertEqual(code, EXIT_NO_ANSWER, "a distinct code, not 'refused'")
        self.assertEqual(err_of(err)["code"], "wait_no_answer")
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual(len([r for r in store.BoardStore(self.ts.team).read() if r.get("kind") == "question"]), 1,
                         "the ask stays on the board")

    def test_a_peer_reply_does_not_end_the_wait(self):
        def peer():
            time.sleep(0.4)
            rows = [r for r in store.BoardStore(self.ts.team).read() if r.get("kind") == "question"]
            if rows:
                post(self.ts, "I think you should proceed", kind="answer", author=PEER, to=(MEMBER,), reply_to=rows[-1]["seq"])

        thread = threading.Thread(target=peer)
        thread.start()
        code, _out, err = json_out(run_cli(
            ["--json", "post", "--to", "human", "--kind", "question", "--timeout", "2s", "need a call"],
            self.env, live_api()))
        thread.join()
        self.assertEqual((code, err_of(err)["code"]), (EXIT_NO_ANSWER, "wait_no_answer"))

    def test_no_wait_and_a_non_asking_kind_return_at_once(self):
        for extra in (["--kind", "question", "--no-wait"], ["--kind", "done"], ["--kind", "note"]):
            started = time.monotonic()
            code, out, err = json_out(run_cli(["--json", "post", "--to", "human"] + extra + ["x"], self.env, live_api()))
            self.assertEqual(code, 0, err)
            self.assertFalse(out["waited"], extra)
            self.assertLess(time.monotonic() - started, 5.0, extra)

    def test_the_operators_own_post_never_waits(self):
        code, out, err = json_out(run_cli(
            ["--json", "--team", "alpha", "post", "--to", "human", "--kind", "question", "x"], self.ts.env, FakeApi()))
        self.assertEqual(code, 0, err)
        self.assertFalse(out["waited"])

    def test_the_board_lock_is_free_while_one_member_waits(self):
        # Holding the team lock across a wait would fail every other member's
        # post with board_locked after five seconds.
        other = {}

        def second_poster():
            time.sleep(0.3)
            other["result"] = json_out(run_cli(
                ["--json", "--team", "alpha", "post", "--to", "all", "meanwhile"], self.ts.env, FakeApi()))[0]

        thread = threading.Thread(target=second_poster)
        thread.start()
        run_cli(["--json", "post", "--to", "human", "--kind", "question", "--timeout", "2s", "waiting"], self.env, live_api())
        thread.join()
        self.assertEqual(other.get("result"), 0, "another member could post while one waited")

    def test_it_tails_rather_than_re_reading_the_whole_board(self):
        # BoardStore.read re-parses the active file, up to 4 MB, on every call.
        with mock.patch.object(store.BoardStore, "read", side_effect=AssertionError("read() in the wait loop")):
            self.assertIsNone(cmd_asks._asks and None)  # keep the import used
            self.assertIsNone(__import__("herdr_team.cmd_board", fromlist=["x"]).wait_for_answer(
                self.ts.team, 1, 0.2, poll_s=0.05))

    def test_the_wait_is_capped_under_the_harness_limit(self):
        from herdr_team import cmd_board

        self.assertLessEqual(cmd_board.MAX_WAIT_S, 540.0, "Claude Code kills a shell command at ten minutes")
        self.assertEqual(cmd_misc.ASK_DEFAULTS["timeout_s"], 480)


# --------------------------------------------------------------------------
# the policy


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def cli(self, *args, env=None, api=None):
        return json_out(run_cli(["--json", "--team", "alpha"] + list(args), env or self.ts.env, api or FakeApi()))

    def config(self):
        return store.read_json(self.ts.team.team_json).get("config") or {}

    def test_defaults_and_a_read_needs_no_authority(self):
        code, out, err = self.cli("ask-policy", env=self.ts.env_with(HERDR_PANE_ID="w2:p1"), api=live_api())
        self.assertEqual(code, 0, err)
        self.assertEqual((out["block"], out["timeout_s"], out["popup"]), (True, 480, True))
        self.assertEqual(out["block_kinds"], ["question", "blocked", "request"])
        self.assertFalse(out["changed"])

    def test_a_member_cannot_change_it(self):
        code, _out, err = self.cli("ask-policy", "--no-block", env=self.ts.env_with(HERDR_PANE_ID="w2:p1"), api=live_api())
        self.assertEqual((code, err_of(err)["code"]), (1, "author_mismatch"))
        self.assertEqual(self.config().get("ask"), None)

    def test_it_writes_config_ask_and_leaves_the_gate_alone(self):
        # config.gate is whitelisted: one unknown key there throws the whole
        # gate config back to defaults, which is why this lives beside it.
        code, out, err = self.cli("ask-policy", "--no-block", "--timeout", "3m", "--kinds", "blocked", "--no-popup")
        self.assertEqual(code, 0, err)
        self.assertEqual((out["block"], out["timeout_s"], out["popup"], out["block_kinds"]), (False, 180, False, ["blocked"]))
        config = self.config()
        self.assertIn("ask", config)
        self.assertNotIn("ask", config.get("gate") or {})
        from herdr_team import gate

        parsed, overrides, error = D.gate_config_from_roster(store.read_json(self.ts.team.team_json))
        self.assertIsNone(error, "config.ask must not trip the gate whitelist")
        self.assertIsNone(overrides)
        self.assertIs(parsed, gate.DEFAULT_CONFIG)

    def test_kinds_none_and_a_bad_kind(self):
        self.assertEqual(self.cli("ask-policy", "--kinds", "none")[1]["block_kinds"], [])
        code, _out, err = self.cli("ask-policy", "--kinds", "nonsense")
        self.assertEqual((code, err_of(err)["code"]), (2, "usage"))

    def test_the_console_parses_it(self):
        self.assertEqual(tui_model.parse_input_line("/asks", "alpha").kind, "asks")
        intent = tui_model.parse_input_line("/ask-policy noblock 5m", "alpha")
        self.assertEqual((intent.kind, intent.args["block"], intent.args["timeout"]), ("ask_policy", False, "5m"))
        self.assertEqual(tui_model.parse_input_line("/ask-policy nonsense", "alpha").kind, "error")
        for command in ("/asks", "/ask-policy"):
            self.assertIn(command, tui_model.SLASH_COMMANDS)
            self.assertIn(command, tui_model.SLASH_USAGE)


# --------------------------------------------------------------------------
# the popup


class PopupModelTests(unittest.TestCase):
    def model(self, *records):
        return cmd_asks.AskModel(team="alpha", asks=list(records), width=70, height=20)

    def record(self, seq=7, sender=MEMBER, kind="blocked", text="PRE-SUBMISSION STOP: tracker says otherwise"):
        return {"seq": seq, "from": sender, "kind": kind, "text": text, "ts": store.now_iso()}

    def test_it_renders_the_queue_and_the_empty_case(self):
        lines = "\n".join(cmd_asks.ask_lines(self.model(self.record(), self.record(seq=8))))
        self.assertIn("1 of 2 waiting on you", lines)
        self.assertIn("#7 alpha-reviewer · blocked", lines)
        self.assertIn("PRE-SUBMISSION STOP", lines)
        self.assertIn("Enter replies", lines)
        self.assertIn("nothing is waiting on you", "\n".join(cmd_asks.ask_lines(self.model())))

    def test_typing_then_enter_answers_the_current_ask(self):
        model = self.model(self.record())
        for ch in "proceed":
            cmd_asks.ask_key(model, ch)
        self.assertEqual(model.input, "proceed")
        intent = cmd_asks.ask_key(model, "ENTER")
        self.assertEqual((intent.kind, intent.args["seq"], intent.args["to"], intent.args["text"]), ("reply", 7, MEMBER, "proceed"))
        argv = cmd_asks.reply_args("alpha", intent)
        self.assertIn("--reply-to", argv)
        self.assertIn("7", argv)
        self.assertIn("--no-wait", argv)
        self.assertEqual(argv[argv.index("--kind") + 1], "answer")

    def test_enter_on_an_empty_line_says_so_rather_than_posting(self):
        model = self.model(self.record())
        self.assertEqual(cmd_asks.ask_key(model, "ENTER").kind, "error")
        self.assertIn("type an answer", model.status)

    def test_tab_moves_and_clears_the_half_typed_reply(self):
        model = self.model(self.record(seq=7), self.record(seq=8))
        cmd_asks.ask_key(model, "x")
        cmd_asks.ask_key(model, "TAB")
        self.assertEqual((model.index, model.input), (1, ""))
        cmd_asks.ask_key(model, "TAB")
        self.assertEqual(model.index, 0, "it wraps")

    def test_escape_dismisses_rather_than_answering(self):
        model = self.model(self.record())
        cmd_asks.ask_key(model, "y")
        self.assertEqual(cmd_asks.ask_key(model, "ESC").kind, "none", "the first Esc only clears the line")
        intent = cmd_asks.ask_key(model, "ESC")
        self.assertEqual((intent.kind, intent.args["seq"]), ("dismiss", 7))

    def test_q_closes_but_not_while_typing(self):
        model = self.model(self.record())
        cmd_asks.ask_key(model, "a")
        self.assertEqual(cmd_asks.ask_key(model, "q").kind, "none", "q is a character when a reply is half typed")
        self.assertEqual(model.input, "aq")
        model.input = ""
        self.assertEqual(cmd_asks.ask_key(model, "q").kind, "quit")


class AsksCommandTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_it_lists_what_is_waiting_and_dismisses(self):
        seq = post(self.ts)
        code, out, err = json_out(run_cli(["--json", "--team", "alpha", "asks"], self.ts.env, FakeApi()))
        self.assertEqual(code, 0, err)
        self.assertEqual([p["seq"] for p in out["pending"]], [seq])
        self.assertEqual(out["pending"][0]["from"], MEMBER)
        code, out, err = json_out(run_cli(["--json", "--team", "alpha", "asks", "--dismiss", str(seq)], self.ts.env, FakeApi()))
        self.assertEqual((code, out["dismissed"]), (0, seq))
        self.assertEqual(json_out(run_cli(["--json", "--team", "alpha", "asks"], self.ts.env, FakeApi()))[1]["pending"], [])


# --------------------------------------------------------------------------
# the daemon raising it


class DaemonPopupTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.on_connected()
        self.team = self.d.teams["alpha"]

    def opens(self):
        return [p for m, p in self.api.calls if m == "plugin.pane.open" and p.get("entrypoint") == "asks"]

    def test_it_opens_once_for_a_waiting_ask_and_backs_off(self):
        self.assertEqual(self.opens(), [])
        post(self.ts)
        self.d.scan_teams(force=True)
        self.d.raise_asks(self.clock() * 1000)
        self.assertEqual(len(self.opens()), 1)
        self.assertEqual(self.opens()[0]["env"], {"HERDR_TEAM": "alpha"})
        self.clock.advance(1)
        self.d.raise_asks(self.clock() * 1000)
        self.assertEqual(len(self.opens()), 1, "it backs off rather than reopening every tick")

    def test_a_busy_popup_slot_is_a_retry_not_an_error(self):
        from support import FakeError

        post(self.ts)
        self.d.scan_teams(force=True)
        self.api.set_response("plugin.pane.open", lambda params: (_ for _ in ()).throw(FakeError("ui_busy", "a popup pane is already open")))
        self.d.raise_asks(self.clock() * 1000)
        self.assertFalse([l for l in self.d.logged if "could not open" in l], "a busy slot is normal, not worth a log line")

    def test_it_never_closes_a_popup_it_did_not_open(self):
        post(self.ts)
        self.d.scan_teams(force=True)
        self.d.raise_asks(self.clock() * 1000)
        self.assertEqual([m for m, _p in self.api.calls if m == "popup.close"], [],
                         "popup.close shuts whatever the operator had open")

    def test_nothing_waiting_and_dismissed_asks_raise_nothing(self):
        self.d.raise_asks(self.clock() * 1000)
        self.assertEqual(self.opens(), [])
        seq = post(self.ts)
        asks.dismiss(self.ts.team, [seq])
        self.d.scan_teams(force=True)
        self.clock.advance(60)
        self.d.raise_asks(self.clock() * 1000)
        self.assertEqual(self.opens(), [], "Esc means not now, not ask me again in a second")

    def test_the_policy_turns_the_popup_off(self):
        run_cli(["--json", "--team", "alpha", "ask-policy", "--no-popup"], self.ts.env, FakeApi())
        post(self.ts)
        self.d.scan_teams(force=True)
        self.d.raise_asks(self.clock() * 1000)
        self.assertEqual(self.opens(), [])


if __name__ == "__main__":
    unittest.main()
