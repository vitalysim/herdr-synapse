"""``say``: a human line typed into one member now (docs/cli.md section 7).

Store grammar for ``direct`` records and ``typed`` outcomes, the console-only
author rule, the CLI, the daemon's job and asynchronous confirmation, and the
readers that must never treat a typed line as mail (hooks, inbox, ledger).
"""

import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from herdr_team import cmd_board, cmd_hooks, gate, hooks, identity, render, roster, store
from herdr_team import daemon as D
from herdr_team import ledger as L
from herdr_team.errors import HerdrTeamError
from support import FAKE_AGENTS, FakeApi, TempState, fake_agent, fake_explain, fake_process_info, fake_read
from test_cmd_board import json_out, pane_api, run_cli, write_live_daemon
from test_daemon import make_daemon, post, trust_kinds
from test_identity import own_process_info, pane_info

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "detection"
CONSOLE_TERMINAL = "term_console"


def fixture(name):
    return (FIXTURES / (name + ".txt")).read_text(encoding="utf-8")


def write_console(ts, terminal=CONSOLE_TERMINAL):
    store.write_json(ts.session.console_json, {"pane_id": "w7:p1", "terminal_id": terminal, "pid": os.getpid(), "open": True, "default_team": "alpha"})


def direct_record(ts, member="alpha-reviewer", text="stop and summarize", force=False, via="console", verified=True, terminal=CONSOLE_TERMINAL, sender="human", kind="direct"):
    return store.BoardStore(ts.team).append({
        "from": sender, "from_kind": "human" if sender == "human" else "claude", "from_terminal": terminal,
        "origin": {"via": via, "verified": verified}, "to": [member], "kind": kind, "text": text, "force": force,
    })


def typed_record(ts, seq, result="typed", reason=None, member="alpha-reviewer", **extra):
    rec = {
        "from": "system", "kind": "system", "event": "typed", "text": "typed #{} into {}".format(seq, member), "to": ["human"],
        "origin": {"via": "system", "verified": True}, "seqs": [seq], "reply_to": seq, "member": member, "result": result, "reason": reason,
    }
    rec.update(extra)
    return store.BoardStore(ts.team).append(rec)


def err_json(err):
    """The JSON error object on stderr, which may follow ``warning:`` lines."""
    if isinstance(err, dict):
        return err
    for line in str(err or "").splitlines():
        if line.lstrip().startswith("{"):
            return json.loads(line)
    return err


def audit_events(ts):
    path = ts.team.audit_jsonl
    if not path.exists():
        return []
    return [json.loads(line)["event"] for line in path.read_bytes().decode("utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------
# records


class DirectRecordTests(unittest.TestCase):
    def test_direct_kind_and_typed_event_are_valid_records(self):
        rec = store.normalize_record({"from": "human", "kind": "direct", "text": "x", "to": ["alpha-reviewer"], "force": True})
        self.assertEqual((rec["kind"], rec["force"], rec["event"]), ("direct", True, None))
        typed = store.normalize_record({"from": "system", "kind": "system", "event": "typed", "text": "x", "to": ["human"], "seqs": [3], "reply_to": 3, "result": "typed", "reason": None})
        self.assertEqual((typed["event"], typed["seqs"], typed["result"], typed["reply_to"]), ("typed", [3], "typed", 3))
        with self.assertRaises(HerdrTeamError) as ctx:
            store.normalize_record({"from": "human", "kind": "direct", "event": "typed", "text": "x", "to": ["a"]})
        self.assertEqual(ctx.exception.code, "record_invalid")

    def test_is_direct_line(self):
        self.assertTrue(store.is_direct_line({"kind": "direct"}))
        self.assertTrue(store.is_direct_line({"kind": "system", "event": "typed"}))
        self.assertFalse(store.is_direct_line({"kind": "system", "event": "nudged"}))
        self.assertFalse(store.is_direct_line({"kind": "note"}))
        self.assertFalse(store.is_direct_line("nope"))

    def test_kind_and_event_lists_agree_across_modules(self):
        self.assertEqual(tuple(store.RECORD_KINDS), tuple(cmd_board.RECORD_KINDS))
        self.assertEqual(set(store.RECORD_KINDS), set(render.KINDS))
        self.assertEqual(tuple(store.SYSTEM_EVENTS), tuple(cmd_board.SYSTEM_EVENTS))
        self.assertEqual(set(store.SYSTEM_EVENTS), set(roster.SYSTEM_EVENTS))
        self.assertIn("direct", store.RECORD_KINDS)
        self.assertIn("typed", store.SYSTEM_EVENTS)
        self.assertNotIn("direct", cmd_board.POST_KINDS)  # post --kind direct stays impossible

    def test_direct_and_typed_render_verified_with_a_glyph(self):
        direct = {"v": 1, "seq": 1, "ts": "2026-09-05T10:00:00Z", "from": "human", "from_kind": "human", "kind": "direct", "text": "x", "to": ["alpha-reviewer"], "origin": {"via": "console", "verified": True}}
        typed = {"v": 1, "seq": 2, "ts": "2026-09-05T10:00:01Z", "from": "system", "kind": "system", "event": "typed", "text": "x", "to": ["human"], "origin": {"via": "system", "verified": True}}
        self.assertFalse(render.is_unverified(direct))
        self.assertFalse(render.is_unverified(typed))
        self.assertEqual(render.glyph_for_kind("direct", False), "»")
        self.assertEqual(render.glyph_for_kind("direct", True), ">>")
        self.assertIn("direct", render.render_header(direct, ascii_only=False))


# --------------------------------------------------------------------------
# identity


class ConsoleAncestryTests(unittest.TestCase):
    def _env(self, ts):
        return ts.env_with(HERDR_PLUGIN_ENTRYPOINT_ID="console", HERDR_PLUGIN_ID="herdr-team", HERDR_PANE_ID="w7:p1")

    def _api(self, process_info=None):
        api = FakeApi()
        api.set_response("pane.get", lambda p: pane_info("w7:p1", CONSOLE_TERMINAL, None, focused=True))
        api.set_response("pane.process_info", process_info or own_process_info("w7:p1"))
        return api

    def test_console_origin_records_how_ancestry_was_decided(self):
        with TempState() as ts:
            write_console(ts)
            with mock.patch.object(identity, "ps_table", return_value={os.getpid(): os.getppid()}):
                author = identity.resolve_author(self._env(ts), ts.layout, self._api())
            self.assertEqual(author.origin.get("ancestry"), "confirmed")
            self.assertTrue(author.verified)
            # process info names other processes and this one descends from none of them
            with mock.patch.object(identity, "ps_table", return_value={os.getpid(): 1}):
                author = identity.resolve_author(self._env(ts), ts.layout, self._api(fake_process_info("w7:p1")))
            self.assertEqual(author.origin.get("ancestry"), "unconfirmed")
            self.assertFalse(author.verified)
            # no process info at all: the focused console is still a verified poster, but say refuses it
            api = self._api()
            api.set_error("pane.process_info", "unknown_method", "no process info")
            with mock.patch.object(identity, "ps_table", return_value={os.getpid(): os.getppid()}):
                author = identity.resolve_author(self._env(ts), ts.layout, api)
            self.assertEqual(author.origin.get("ancestry"), "unavailable")
            self.assertTrue(author.verified)


# --------------------------------------------------------------------------
# the CLI


class SayCommandTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        write_console(self.ts)
        trust_kinds(self.ts)
        self.env = self.ts.env_with(HERDR_PLUGIN_ENTRYPOINT_ID="console", HERDR_PLUGIN_ID="herdr-team", HERDR_PANE_ID="w7:p1")
        patcher = mock.patch.object(identity, "ps_table", return_value={os.getpid(): os.getppid()})
        patcher.start()
        self.addCleanup(patcher.stop)

    def console_api(self, focused=True, ancestry=True):
        api = pane_api()
        member_panes = api.responses["pane.get"]

        def pane_get(params):
            if params.get("pane_id") == "w7:p1":
                return pane_info("w7:p1", CONSOLE_TERMINAL, None, focused=focused)
            return member_panes(params)

        api.set_response("pane.get", pane_get)
        if ancestry:
            api.set_response("pane.process_info", own_process_info("w7:p1"))
        else:
            api.set_error("pane.process_info", "unknown_method", "no process info")
        return api

    def say(self, *args, env=None, api=None):
        code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "say"] + list(args), env if env is not None else self.env, api if api is not None else self.console_api()))
        return code, payload, err_json(err)

    def records(self):
        return store.BoardStore(self.ts.team).read()

    def jobs(self):
        return sorted(os.listdir(self.ts.team.jobs_dir)) if self.ts.team.jobs_dir.exists() else []

    def assert_nothing_written(self):
        self.assertEqual(self.records(), [])
        self.assertEqual(self.jobs(), [])

    def test_member_pane_is_refused_and_audited(self):
        code, _, err = self.say("alpha-worker", "x", env=self.ts.env_with(HERDR_PANE_ID="w2:p1"), api=pane_api())
        self.assertEqual((code, err["code"]), (1, "author_mismatch"))
        self.assertEqual(audit_events(self.ts)[-1], "author_mismatch")
        self.assert_nothing_written()

    def test_unverified_humans_are_refused(self):
        cases = [
            ("outside", self.ts.env, FakeApi(), "outside"),
            ("compose popup", self.ts.env_with(HERDR_PLUGIN_ENTRYPOINT_ID="compose", HERDR_PLUGIN_ID="herdr-team", HERDR_PLUGIN_CONTEXT_JSON=json.dumps({"nonce": "abc", "focused_pane_id": "w2:p1"})), FakeApi(), "popup"),
            ("unfocused console", self.env, self.console_api(focused=False), "console-unfocused"),
            ("shell pane", self.ts.env_with(HERDR_PANE_ID="w3:p1"), pane_api(), None),
            ("console without process info", self.env, self.console_api(ancestry=False), "console"),
        ]
        for name, env, api, via in cases:
            with self.subTest(name):
                code, _, err = self.say("alpha-reviewer", "x", env=env, api=api)
                self.assertEqual((code, err["code"]), (1, "say_unverified"), err)
                if via is not None:
                    self.assertEqual(err["via"], via)
                if name == "console without process info":
                    self.assertEqual(err["ancestry"], "unavailable")
        self.assertIn("say_unverified", audit_events(self.ts))
        self.assert_nothing_written()

    def test_echo_and_secrets_are_refused_even_with_force(self):
        code, _, err = self.say("alpha-reviewer", "[herdr-team nudge] 1 new post", "--force")
        self.assertEqual((code, err["code"]), (4, "echo_rejected"))
        code, _, err = self.say("alpha-reviewer", "see [n17] please")
        self.assertEqual((code, err["code"]), (4, "echo_rejected"))
        code, _, err = self.say("alpha-reviewer", "use key AKIAABCDEFGHIJKLMNOP", "--force")
        self.assertEqual((code, err["code"]), (1, "secret_detected"))
        self.assert_nothing_written()

    def test_text_rules(self):
        write_live_daemon(self.ts)
        code, _, err = self.say("alpha-reviewer", "first\nsecond")
        self.assertEqual((code, err["code"]), (1, "say_multiline"))
        code, _, err = self.say("alpha-reviewer", "x" * 501)
        self.assertEqual((code, err["code"], err["max"]), (1, "say_too_long", 500))
        code, _, err = self.say("alpha-reviewer", "x" * 2001)
        self.assertEqual((code, err["code"]), (1, "text_too_long"))
        code, _, err = self.say("alpha-reviewer", "/clear")
        self.assertEqual((code, err["code"], err["word"]), (1, "say_control_command", "/clear"))
        self.assert_nothing_written()
        code, payload, err = self.say("alpha-reviewer", "/compact now", "--no-wait")
        self.assertEqual(code, 0, err)
        code, payload, err = self.say("alpha-reviewer", "/clear", "--force", "--no-wait")
        self.assertEqual(code, 0, err)
        self.assertTrue(store.BoardStore(self.ts.team).get(payload["seq"])["force"])

    def test_escape_sequences_tabs_and_line_separators_never_reach_the_line(self):
        clean = cmd_board.prepare_say_text("\x1b[201~\x1b[A\rls\tnow", False)
        self.assertNotIn("\x1b", clean)
        self.assertNotIn("\t", clean)
        self.assertIn("ls", clean)
        self.assertIn("now", clean)
        clean = cmd_board.prepare_say_text("\x9b201~ok", False)
        self.assertNotIn("\x9b", clean)
        self.assertNotIn(" ", cmd_board.prepare_say_text("a b", False))
        self.assertEqual(cmd_board.prepare_say_text("  a\t\tb  ", False), "a  b")

    def test_group_targets_and_unknown_members(self):
        for target, hinted in (("all", True), ("role:worker", True), ("human", True), ("me", True), ("nobody", False)):
            with self.subTest(target):
                code, _, err = self.say(target, "x")
                self.assertEqual((code, err["code"]), (1, "member_not_found"))
                self.assertEqual("hint" in err, hinted)
        self.assert_nothing_written()

    def test_untrusted_kind_is_refused(self):
        store.write_json(self.ts.session.kinds_json, {})
        code, _, err = self.say("alpha-reviewer", "x")
        self.assertEqual((code, err["code"], err["kind"]), (1, "kind_unverified", "codex"))
        self.assert_nothing_written()

    def test_daemon_down_writes_nothing(self):
        code, _, err = self.say("alpha-reviewer", "x", "--no-wait")
        self.assertEqual((code, err["code"]), (5, "daemon_down"))
        self.assert_nothing_written()

    def test_record_and_job_shape(self):
        write_live_daemon(self.ts)
        code, payload, err = self.say("alpha-reviewer", "stop and summarize", "--no-wait")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(payload), ["author", "force", "job", "member", "outcome", "seq", "team", "text", "waited"])
        self.assertEqual((payload["member"], payload["waited"], payload["outcome"], payload["force"]), ("alpha-reviewer", False, None, False))
        self.assertEqual(payload["author"], {"name": "human", "via": "console", "verified": True})
        record = store.BoardStore(self.ts.team).get(payload["seq"])
        self.assertEqual((record["kind"], record["from"], record["to"], record["urgent"], record["event"], record["force"]), ("direct", "human", ["alpha-reviewer"], False, None, False))
        self.assertEqual((record["origin"]["via"], record["origin"]["verified"], record["from_terminal"]), ("console", True, CONSOLE_TERMINAL))
        self.assertEqual(record["text"], "stop and summarize")
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        job = store.read_json(self.ts.team.jobs_dir / jobs[0])
        self.assertEqual((job["kind"], job["member"], job["seq"], job["force"]), ("say", "alpha-reviewer", payload["seq"], False))
        self.assertEqual(job["requested_by"], {"name": "human", "via": "console", "verified": True})
        self.assertNotIn("text", job)  # the daemon types the board record, never a job payload

    def test_wait_returns_the_outcome(self):
        write_live_daemon(self.ts)
        timer = threading.Timer(0.3, lambda: typed_record(self.ts, 1, "typed", None))
        timer.start()
        self.addCleanup(timer.cancel)
        code, payload, err = self.say("alpha-reviewer", "pong", "--timeout", "3")
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["waited"])
        self.assertEqual((payload["outcome"]["result"], payload["outcome"]["reason"], payload["outcome"]["seq"]), ("typed", None, 2))

    def test_wait_timeout_reports_seq_and_job(self):
        write_live_daemon(self.ts)
        code, _, err = self.say("alpha-reviewer", "pong", "--timeout", "0.3")
        self.assertEqual((code, err["code"], err["seq"], err["member"]), (1, "say_timeout", 1, "alpha-reviewer"))
        self.assertTrue(err["job"])
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(self.jobs()), 1)

    def test_direct_is_not_member_mail_but_stays_on_the_board(self):
        seq = direct_record(self.ts)
        member_env = self.ts.env_with(HERDR_PANE_ID="w2:p1")
        code, payload, err = json_out(run_cli(["--json", "board", "--new"], member_env, pane_api()))
        self.assertEqual(code, 0, err)
        self.assertEqual((payload["count"], payload["cursor"]["advanced"]), (0, False))
        code, payload, _ = json_out(run_cli(["--json", "board"], member_env, pane_api()))
        self.assertEqual([r["kind"] for r in payload["posts"]], ["direct"])
        code, payload, _ = json_out(run_cli(["--json", "board", "--kind", "direct"], member_env, pane_api()))
        self.assertEqual(payload["count"], 1)
        # the direct line is addressed to the member, so the human's inbox lists only the daemon's typed outcome
        code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "board", "--new"], self.ts.env, FakeApi()))
        self.assertEqual(payload["posts"], [])
        outcome = typed_record(self.ts, seq)
        records = self.records()
        self.assertEqual(cmd_board.unread_for(self.ts.team, records, "alpha-reviewer", False), 0)
        self.assertEqual(cmd_board.unread_for(self.ts.team, records, "human@human", True), 1)
        code, payload, _ = json_out(run_cli(["--json", "--team", "alpha", "board", "--new"], self.ts.env, FakeApi()))
        self.assertEqual([r["seq"] for r in payload["posts"]], [outcome])
        code, payload, _ = json_out(run_cli(["--json", "board", "--new"], member_env, pane_api()))
        self.assertEqual(payload["count"], 0)
        doc = store.read_json(self.ts.team.team_json)
        receipts = cmd_board.compute_receipts(self.ts.team, doc, records, records)
        self.assertNotIn(str(seq), receipts)
        self.assertNotIn(str(outcome), receipts)

    def test_edit_and_retract_refuse_direct(self):
        seq = direct_record(self.ts)
        code, _, err = json_out(run_cli(["--json", "--team", "alpha", "retract", str(seq)], self.ts.env, FakeApi()))
        self.assertEqual((code, err["code"]), (1, "retract_invalid"))
        code, _, err = json_out(run_cli(["--json", "--team", "alpha", "edit", str(seq), "changed"], self.ts.env, FakeApi()))
        self.assertEqual((code, err["code"]), (1, "edit_invalid"))
        self.assertEqual(len(self.records()), 1)


# --------------------------------------------------------------------------
# the daemon


class SayJobTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        write_console(self.ts)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.on_connected()
        self.team = self.d.teams["alpha"]

    def job(self, member, seq, force=False, **extra):
        obj = {"v": 1, "kind": "say", "member": member, "seq": seq, "force": force, "requested_by": {"name": "human", "via": "console", "verified": True}, "requested_at": store.now_iso()}
        obj.update(extra)
        path = self.ts.team.jobs_dir / "{}-say.json".format(int(time.time() * 1000000))
        store.write_json(path, obj)
        return path

    def consume(self):
        self.clock.advance(1)
        self.d.consume_jobs(self.clock() * 1000)

    def say(self, member="alpha-reviewer", text="stop and summarize", force=False, **job_extra):
        seq = direct_record(self.ts, member, text, force)
        self.job(member, seq, force, **job_extra)
        self.consume()
        return seq

    def typed(self):
        return [r for r in store.BoardStore(self.ts.team).read() if r.get("event") == "typed"]

    def prompts(self):
        return [p for m, p in self.api.calls if m == "agent.prompt"]

    def agents(self, **overrides):
        rows = []
        for agent in FAKE_AGENTS:
            row = dict(agent)
            row.update(overrides.get(str(agent.get("name")), {}))
            rows.append(row)
        self.api.set_response("agent.list", {"type": "agent_list", "agents": rows})

    def poll(self, seconds=1):
        self.clock.advance(seconds)
        self.d.poll_agents(force=True)
        self.d.confirm_says(self.clock() * 1000)

    def last_typed(self):
        typed = self.typed()
        self.assertTrue(typed, "no typed record")
        return typed[-1]

    def test_plain_say_types_the_record_text_and_confirms_on_the_next_poll(self):
        seq = self.say()
        prompts = self.prompts()
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0], {"target": "w2:p1", "text": "stop and summarize"})  # raw text, no wait, no marker, no nonce
        self.assertIn("alpha-reviewer", self.team.say_inflight)
        rt = self.team.rt("alpha-reviewer")
        self.assertTrue(rt.in_flight)
        self.assertIsNone(rt.last_nudge_ms)
        self.assertIsNotNone(self.d.global_last_nudge_ms)
        self.assertEqual((self.d.counters["says"], self.d.counters["nudges"]), (1, 0))
        row = [m for m in self.d.build_who()["teams"]["alpha"]["members"] if m["name"] == "alpha-reviewer"][0]
        self.assertEqual((row["say"], row["pending_nudges"]), ("confirming", 0))
        self.assertEqual(self.typed(), [])
        self.assertTrue(self.d._has_pending())
        self.agents(**{"alpha-reviewer": {"agent_status": "working", "state_change_seq": 2}})
        self.poll()
        typed = self.last_typed()
        self.assertEqual((typed["to"], typed["seqs"], typed["reply_to"], typed["member"], typed["result"], typed["reason"], typed["kind_of_member"]), (["human"], [seq], seq, "alpha-reviewer", "typed", None, "codex"))
        self.assertEqual(self.team.say_inflight, {})
        self.assertFalse(rt.in_flight)
        self.assertEqual(self.team.pending, {})
        attempts = list(self.team.ledger.attempts().values())
        self.assertEqual(len(attempts), 1)
        self.assertEqual((attempts[0]["delivery"], attempts[0]["kind"], attempts[0]["seqs"], attempts[0]["result"], attempts[0]["force"]), ("say", "codex", [seq], "landed_working", False))
        self.assertEqual(self.team.ledger.open_intents(), [])

    def test_prompt_uses_the_short_timeout(self):
        seen = []
        original = self.api.request

        def request(method, params=None, timeout=None):
            if method == "agent.prompt":
                seen.append(timeout)
            return original(method, params, timeout)

        self.api.request = request
        self.say()
        self.assertEqual(seen, [D.SAY_PROMPT_TIMEOUT_S])
        self.assertLess(D.SAY_PROMPT_TIMEOUT_S, 8.0)

    def test_working_member_is_refused_without_force_and_typed_with_it(self):
        seq = self.say("alpha-worker")  # the worker is ``working`` in FAKE_AGENTS
        self.assertEqual(self.prompts(), [])
        typed = self.last_typed()
        self.assertEqual((typed["result"], typed["reason"], typed["member"], typed["seqs"]), ("refused", "working", "alpha-worker", [seq]))
        self.assertEqual(self.team.ledger.counts()["intents"], 0)
        seq = self.say("alpha-worker", force=True)
        self.assertEqual(len(self.prompts()), 1)
        self.poll()
        typed = self.last_typed()
        self.assertEqual((typed["result"], typed["reason"], typed["force"], typed["force_verified"], typed["seqs"]), ("typed", "in_turn", True, True, [seq]))
        self.assertIsNone(typed["detail"])

    def test_force_on_an_unverified_kind_is_typed_but_flagged(self):
        self.api.set_response("agent.get", lambda p: {"type": "agent_info", "agent": fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer", status="working", state_change_seq=3)})
        self.say(force=True)
        self.assertEqual(len(self.prompts()), 1)
        self.agents(**{"alpha-reviewer": {"agent_status": "working", "state_change_seq": 3}})
        self.poll()
        typed = self.last_typed()
        self.assertEqual((typed["result"], typed["reason"], typed["force_verified"]), ("typed", "in_turn", False))
        self.assertIn("unverified for codex", typed["detail"])
        self.assertIn("unverified for codex", typed["text"])

    def test_dialog_is_refused_in_both_modes(self):
        self.api.set_response("agent.read", lambda p: fake_read(str(p["target"]), fixture("codex_approval"), "detection"))
        for force in (False, True):
            with self.subTest(force=force):
                self.say(force=force)
                self.assertEqual(self.prompts(), [])
                typed = self.last_typed()
                self.assertEqual((typed["result"], typed["reason"]), ("refused", "dialog"))
                self.assertEqual(typed["detail"], "Allow command?")

    def test_blocked_unknown_and_overlays_are_refused_in_both_modes(self):
        self.api.set_response("agent.explain", fake_explain("codex", "blocked", "codex.approval"))
        for force in (False, True):
            self.say(force=force)
            self.assertEqual((self.last_typed()["result"], self.last_typed()["reason"]), ("refused", "blocked"))
        self.api.set_response("agent.explain", fake_explain("codex", "idle", "codex.transcript", skip_state_update=True, skipped_update_reason="transcript viewer open"))
        for force in (False, True):
            self.say(force=force)
            typed = self.last_typed()
            self.assertEqual((typed["result"], typed["reason"], typed["detail"]), ("refused", "skip_state_update", "transcript viewer open"))
        self.api.set_response("agent.explain", fake_explain("codex", "idle"))
        self.api.set_response("agent.get", lambda p: {"type": "agent_info", "agent": fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer", status="blocked")})
        self.say(force=True)
        self.assertEqual((self.last_typed()["result"], self.last_typed()["reason"]), ("refused", "blocked"))
        self.api.set_response("agent.get", lambda p: {"type": "agent_info", "agent": fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer", status="unknown")})
        self.say(force=True)
        self.assertEqual((self.last_typed()["result"], self.last_typed()["reason"]), ("refused", "unknown"))
        self.assertEqual(self.prompts(), [])
        self.api.set_response("agent.get", lambda p: {"type": "agent_info", "agent": fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer")})
        self.api.set_error("agent.prompt", "agent_blocked", "agent is blocked and requires interactive input")
        self.say()
        self.assertEqual(len(self.prompts()), 1)
        typed = self.last_typed()
        self.assertEqual((typed["result"], typed["reason"]), ("refused", "blocked"))
        self.assertIn("agent_blocked", typed["detail"])

    def test_draft_refuses_both_modes_and_mute_only_the_plain_one(self):
        self.d._prompt_line = lambda *a, **k: "half typed"
        for force in (False, True):
            self.say(force=force)
            typed = self.last_typed()
            self.assertEqual((typed["result"], typed["reason"], typed["detail"]), ("refused", "draft", "half typed"))
        self.assertEqual(self.prompts(), [])
        self.d._prompt_line = lambda *a, **k: None
        store.write_json(self.ts.team.mute_json, {"alpha-reviewer": None})
        self.say()
        self.assertEqual((self.last_typed()["result"], self.last_typed()["reason"]), ("refused", "muted"))
        self.assertEqual(self.prompts(), [])
        self.say(force=True)
        self.assertEqual(len(self.prompts()), 1)

    def test_unverified_sources_and_stale_jobs_are_refused_with_a_record(self):
        cases = [
            ("popup origin", dict(via="popup", verified=False)),
            ("unverified console", dict(verified=False)),
            ("shell origin", dict(via="cli", verified=True)),
            ("wrong terminal", dict(terminal="term_other")),
            ("member author", dict(sender="alpha-worker")),
            ("a note, not a direct line", dict(kind="note")),
        ]
        for name, kwargs in cases:
            with self.subTest(name):
                seq = direct_record(self.ts, "alpha-reviewer", "rm -rf /", **kwargs)
                self.job("alpha-reviewer", seq)
                self.consume()
                typed = self.last_typed()
                self.assertEqual((typed["result"], typed["reason"], typed["seqs"]), ("refused", "unverified_source", [seq]))
        # the job names another member than the record
        seq = direct_record(self.ts, "alpha-worker")
        self.job("alpha-reviewer", seq)
        self.consume()
        self.assertEqual((self.last_typed()["result"], self.last_typed()["reason"]), ("refused", "unverified_source"))
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.d.counters["unverified_skipped"], len(cases) + 1)
        # a job left over from a dead daemon
        seq = direct_record(self.ts)
        self.job("alpha-reviewer", seq, requested_at="2026-09-05T10:00:00.000Z")
        self.consume()
        self.assertEqual((self.last_typed()["result"], self.last_typed()["reason"], self.last_typed()["seqs"]), ("refused", "stale", [seq]))
        # a job without a seq has nothing to report against
        before = len(self.typed())
        self.job("alpha-reviewer", None)
        self.consume()
        self.assertEqual(len(self.typed()), before)
        self.assertTrue(any("names no board seq" in line for line in self.d.logged))
        self.assertEqual(self.prompts(), [])

    def test_missing_member_absent_agent_and_untrusted_kind(self):
        seq = direct_record(self.ts, "ghost")
        self.job("ghost", seq)
        self.consume()
        self.assertEqual((self.last_typed()["result"], self.last_typed()["reason"], self.last_typed()["member"]), ("refused", "member_not_found", "ghost"))
        self.api.set_error("agent.get", "agent_not_found", "gone")
        self.d.reconcile_due = False
        self.say()
        self.assertEqual((self.last_typed()["result"], self.last_typed()["reason"]), ("refused", "absent"))
        self.assertTrue(self.d.reconcile_due)
        self.api.set_response("agent.get", lambda p: {"type": "agent_info", "agent": fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer")})
        store.write_json(self.ts.session.kinds_json, {})
        self.say()
        self.assertEqual((self.last_typed()["result"], self.last_typed()["reason"]), ("refused", "kind_unverified"))
        self.assertEqual(self.prompts(), [])

    def test_prompt_errors_map_to_outcomes(self):
        rt = self.team.rt("alpha-reviewer")
        self.api.set_error("agent.prompt", "herdr_timeout", "timed out")
        self.say()
        typed = self.last_typed()
        self.assertEqual((typed["result"], typed["reason"]), ("failed", "hung"))
        self.assertIsNotNone(rt.pane_stuck_until_ms)
        self.assertFalse(rt.in_flight)
        self.api.set_error("agent.prompt", "agent_not_ready", "agent is launching")
        self.say()
        self.assertEqual((self.last_typed()["result"], self.last_typed()["reason"]), ("refused", "not_ready"))
        self.api.set_error("agent.prompt", "agent_prompt_failed", "input buffer full")
        self.say()
        self.assertEqual((self.last_typed()["result"], self.last_typed()["reason"]), ("failed", "hung"))
        self.api.set_error("agent.prompt", "internal", "boom")
        self.say()
        typed = self.last_typed()
        self.assertEqual((typed["result"], typed["reason"]), ("failed", "transient"))
        self.assertIn("internal", typed["detail"])
        counts = self.team.ledger.counts()
        self.assertEqual((counts["hung"], counts["transient"], counts["intents"]), (2, 2, 4))
        self.assertEqual(self.team.say_inflight, {})

    def test_idle_after_the_window_is_unconfirmed_or_not_submitted(self):
        self.say()
        self.poll(2)
        self.assertEqual(self.typed(), [])  # still inside the confirmation window
        self.poll(4)
        typed = self.last_typed()
        self.assertEqual((typed["result"], typed["reason"]), ("failed", "unconfirmed"))
        self.assertEqual(self.team.say_inflight, {})
        self.say()
        with mock.patch.object(gate, "prompt_line_text", return_value="stop and summarize"):
            self.poll(6)
        typed = self.last_typed()
        self.assertEqual((typed["result"], typed["reason"]), ("not_submitted", "not_submitted"))
        self.assertIn("press Enter", typed["detail"])

    def test_blocked_after_landing_is_typed_with_a_dialog_note(self):
        self.say()
        self.agents(**{"alpha-reviewer": {"agent_status": "blocked"}})
        self.poll()
        typed = self.last_typed()
        self.assertEqual((typed["result"], typed["reason"]), ("typed", None))
        self.assertIn("now shows a dialog", typed["detail"])

    def test_second_say_while_confirming_is_refused_in_flight(self):
        self.say()
        self.say(text="and another")
        self.assertEqual(len(self.prompts()), 1)
        typed = self.last_typed()
        self.assertEqual((typed["result"], typed["reason"]), ("refused", "in_flight"))
        self.agents(**{"alpha-reviewer": {"agent_status": "working", "state_change_seq": 2}})
        self.poll()
        self.assertEqual([t["result"] for t in self.typed()], ["refused", "typed"])

    def test_direct_records_never_create_pending_and_a_pending_nudge_survives_a_say(self):
        direct_record(self.ts)
        self.d.tick()
        self.assertEqual(self.team.pending, {})
        self.assertTrue(any("typed into" in line and "not nudged" in line for line in self.d.logged))
        d2, _api2, _clock2 = make_daemon(self.ts)
        d2.on_connected()
        self.assertEqual(d2.teams["alpha"].pending, {})
        self.assertFalse(any("rebuilt pending" in line for line in d2.logged))
        # a nudge already waiting is untouched by the say and held while the say confirms
        post(self.ts, "alpha-reviewer")
        self.d.tick()
        pending = self.team.pending["alpha-reviewer"]
        seqs = list(pending.seqs)
        self.say()
        self.d.evaluate_pending()
        self.assertIs(self.team.pending["alpha-reviewer"], pending)
        self.assertEqual((pending.seqs, pending.landed_ms, pending.attempts), (seqs, None, 0))
        self.assertEqual(len(self.prompts()), 1)

    def test_nudge_job_scan_ignores_direct_lines(self):
        direct_record(self.ts)
        path = self.ts.team.jobs_dir / "{}-nudge.json".format(int(time.time() * 1000000))
        store.write_json(path, {"v": 1, "kind": "nudge", "member": "alpha-reviewer", "force": False, "requested_by": {"name": "human"}, "requested_at": "now"})
        self.consume()
        self.assertEqual(self.team.pending, {})
        self.assertTrue(any("nothing unread" in line for line in self.d.logged))

    def test_dry_nudge_say_is_reported_as_dry(self):
        d, api, clock = make_daemon(self.ts, env=self.ts.env_with(HERDR_TEAM_DRY_NUDGE="1"))
        d.on_connected()
        seq = direct_record(self.ts)
        self.job("alpha-reviewer", seq)
        clock.advance(1)
        d.consume_jobs(clock() * 1000)
        self.assertEqual([m for m, _ in api.calls if m == "agent.prompt"], [])
        typed = [r for r in store.BoardStore(self.ts.team).read() if r.get("event") == "typed"][-1]
        self.assertEqual((typed["result"], typed["reason"]), ("typed", "dry"))
        self.assertEqual(d.teams["alpha"].ledger.counts()["dry"], 1)
        self.assertEqual(d.teams["alpha"].say_inflight, {})

    def test_say_source_problem_is_pure(self):
        good = {"seq": 4, "kind": "direct", "from": "human", "to": ["a"], "origin": {"via": "console", "verified": True}, "from_terminal": "t1", "text": "x"}
        self.assertIsNone(D.say_source_problem(good, "a", "t1"))
        self.assertIn("not direct", D.say_source_problem(dict(good, kind="note"), "a", "t1"))
        self.assertIn("not the human", D.say_source_problem(dict(good, **{"from": "b"}), "a", "t1"))
        self.assertIn("addressed to", D.say_source_problem(good, "b", "t1"))
        self.assertIn("unverified", D.say_source_problem(dict(good, origin={"via": "console", "verified": False}), "a", "t1"))
        self.assertIn("popup", D.say_source_problem(dict(good, origin={"via": "popup", "verified": True}), "a", "t1"))
        self.assertIn("console", D.say_source_problem(good, "a", "t2"))
        self.assertIn("console", D.say_source_problem(good, "a", None))
        self.assertIn("no text", D.say_source_problem(dict(good, text="  "), "a", "t1"))
        self.assertIn("no board record", D.say_source_problem(None, "a", "t1"))


# --------------------------------------------------------------------------
# readers: hooks and ledger


class SayReaderTests(unittest.TestCase):
    def test_direct_and_typed_never_block_stop_or_enter_the_prompt_context(self):
        with TempState() as ts:
            seq = direct_record(ts)
            typed_record(ts, seq)
            self.assertEqual(hooks.stop_decision(ts.layout, "alpha", "alpha-reviewer", {}, now=1000.0), (0, ""))
            self.assertEqual(hooks.unread_for(ts.team, "alpha-reviewer"), [])
            self.assertEqual(cmd_hooks._addressed_unread(store.BoardStore(ts.team).read(), "alpha-reviewer", 0), [])
            note = post(ts, "alpha-reviewer")
            code, text = hooks.stop_decision(ts.layout, "alpha", "alpha-reviewer", {}, now=1001.0)
            self.assertEqual(code, 2)
            self.assertIn("1 unread board post", text)
            self.assertEqual([r["seq"] for r in cmd_hooks._addressed_unread(store.BoardStore(ts.team).read(), "alpha-reviewer", 0)], [note])

    def test_say_attempts_do_not_count_as_round_trips_or_open_intents(self):
        with TempState() as ts:
            ledger = L.Ledger(ts.team)

            def attempt(attempt_id, **extra):
                return L.Attempt(id=attempt_id, member="alpha-reviewer", kind="codex", seqs=[1], hook_authority=False, weak_idle=True, focused=False,
                                 prompt_line_empty=True, gate_ms=1.0, queue_ms=1.0, attempts=1, extra=dict(extra))

            for i in range(20):
                ledger.record_intent(attempt("n{}".format(i), delivery="nudge"))
                ledger.record_result("n{}".format(i), L.RESULT_LANDED_WORKING, {"elapsed_ms": 500.0})
                ledger.record_outcome("n{}".format(i), True, 1000.0)
            for i in range(5):
                ledger.record_intent(attempt("s{}".format(i), delivery="say"))
                ledger.record_result("s{}".format(i), L.RESULT_LANDED_IN_TURN, {"elapsed_ms": 300.0})
            ledger.record_intent(attempt("s-open", delivery="say"))
            self.assertEqual(ledger.clean_rate("codex"), 1.0)
            stats = ledger.stats()
            self.assertEqual((stats["kinds"]["codex"]["round_trips"], stats["kinds"]["codex"]["clean"], stats["kinds"]["codex"]["verified"]), (20, 20, True))
            counts = ledger.counts()
            self.assertEqual((counts["landed_in_turn"], counts["intents"], counts["open_intents"]), (5, 26, 0))
            self.assertEqual(ledger.open_intents(), [])


if __name__ == "__main__":
    unittest.main()
