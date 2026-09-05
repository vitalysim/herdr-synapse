"""hooks.py: the bin/hook slow-path reconciler and the Claude Stop decision."""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest

from herdr_team import hooks, paths, store
from herdr_team.cli import main as cli_main
from support import FAKE_MEMBERS, FakeApi, FakeError, TempState, fake_agent


def event_env(ts, event="pane.agent_detected", pane_id="w2:p1", **extra):
    env = ts.env_with(
        HERDR_PLUGIN_EVENT=event,
        HERDR_PLUGIN_EVENT_JSON=json.dumps({"type": event.replace(".", "_"), "data": {"pane_id": pane_id, "ignored": {"deep": True}}}),
        HERDR_PLUGIN_ID="herdr-team",
        HERDR_PANE_ID="wA:p6",
        HERDR_WORKSPACE_ID="wA",
        HERDR_TAB_ID="wA:t1",
    )
    env.update(extra)
    return env


def run_event(ts, api, event="agent_detected", **kw):
    out = io.StringIO()
    env = event_env(ts, "pane." + event if event != "agent_detected" else "pane.agent_detected", **kw)
    code = hooks.run_hook_event([event], env, api=api, stdout=out)
    return code, json.loads(out.getvalue())


def member(ts, name):
    doc = store.read_json(ts.team.team_json)
    for m in doc["members"]:
        if m["name"] == name:
            return m
    raise AssertionError("no member " + name)


class ParseEventTests(unittest.TestCase):
    def test_reads_only_event_name_and_pane_id(self):
        env = {"HERDR_PLUGIN_EVENT": "pane.agent_detected", "HERDR_PLUGIN_EVENT_JSON": json.dumps({"type": "pane_agent_detected", "data": {"pane_id": "w2:p1", "agent": "claude", "released": True}})}
        ev = hooks.parse_event(env)
        self.assertEqual((ev.event, ev.pane_id), ("agent_detected", "w2:p1"))

    def test_top_level_pane_id_and_argv_fallback(self):
        ev = hooks.parse_event({"HERDR_PLUGIN_EVENT_JSON": json.dumps({"pane_id": "w3:p2"})}, "pane_closed")
        self.assertEqual((ev.event, ev.pane_id), ("pane_closed", "w3:p2"))
        ev = hooks.parse_event({"HERDR_PLUGIN_EVENT": "pane.exited"}, "agent_detected")
        self.assertEqual((ev.event, ev.pane_id), ("pane_exited", None))

    def test_garbage_json_and_unknown_event(self):
        ev = hooks.parse_event({"HERDR_PLUGIN_EVENT": "workspace.closed", "HERDR_PLUGIN_EVENT_JSON": "{not json"}, "bogus")
        self.assertEqual(ev.event, "bogus")
        self.assertIsNone(ev.pane_id)
        self.assertEqual(ev.raw, {})


class HookEventSkipTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.api = FakeApi(self.ts.socket_path)

    def test_no_team_skips_with_exit_0(self):
        ts = TempState(write_team=False)
        self.addCleanup(ts.cleanup)
        import shutil

        shutil.rmtree(ts.team.root)
        code, out = run_event(ts, self.api)
        self.assertEqual(code, 0)
        self.assertEqual(out["skipped"], "no_team")
        self.assertEqual(out["changes"], [])
        self.assertEqual(self.api.calls, [])

    def test_daemon_alive_skips(self):
        from herdr_team import daemon as D

        start = D.process_start_time(os.getpid())
        store.write_json(self.ts.session.daemon_json, {"pid": os.getpid(), "start_time": start, "beat_at": "x", "socket": os.fspath(self.ts.socket_path), "socket_inode": 1, "version": "0.1.0", "herdr_version": "0.8.2", "protocol": 20})
        code, out = run_event(self.ts, self.api)
        self.assertEqual((code, out["skipped"]), (0, "daemon_alive"))
        self.assertEqual(self.api.calls, [])

    def test_socket_not_allowed_skips(self):
        allowed = paths.allowed_sockets_file(self.ts.config_dir)
        allowed.parent.mkdir(parents=True, exist_ok=True)
        allowed.write_text("/nonexistent/other.sock\n")
        code, out = run_event(self.ts, self.api)
        self.assertEqual((code, out["skipped"]), (0, "socket_not_allowed"))
        self.assertEqual(self.api.calls, [])

    def test_unreachable_herdr_leaves_roster_alone(self):
        self.api.unreachable = True
        code, out = run_event(self.ts, self.api, "pane_closed")
        self.assertEqual((code, out["skipped"]), (0, "herdr_unreachable"))
        self.assertEqual(member(self.ts, "alpha-reviewer")["status"], "active")

    def test_missing_pane_id_and_unknown_event(self):
        out = io.StringIO()
        env = self.ts.env_with(HERDR_PLUGIN_EVENT="pane.agent_detected", HERDR_PLUGIN_EVENT_JSON="{}")
        code = hooks.run_hook_event(["agent_detected"], env, api=self.api, stdout=out)
        self.assertEqual((code, json.loads(out.getvalue())["skipped"]), (0, "no_pane_id"))
        out = io.StringIO()
        code = hooks.run_hook_event(["workspace_closed"], self.ts.env, api=self.api, stdout=out)
        self.assertEqual((code, json.loads(out.getvalue())["skipped"]), (0, "unknown_event"))

    def test_layout_failure_is_exit_0(self):
        out = io.StringIO()
        env = {"HERDR_SOCKET_PATH": os.fspath(self.ts.socket_path), "HERDR_TEAM_DIR": "/not/a/team/dir"}
        code = hooks.run_hook_event(["agent_detected"], env, api=self.api, stdout=out)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["skipped"].startswith("layout:"))

    def test_through_cli_without_server_exits_0(self):
        out, err = io.StringIO(), io.StringIO()
        env = event_env(self.ts, "pane.closed", "w2:p2")
        # cmd_hooks.run_hook_event prints through sys.stdout (it does not forward args.stdout).
        with contextlib.redirect_stdout(out):
            code = cli_main(["hook-event", "pane_closed", "--json"], env=env, stdout=out, stderr=err)
        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["skipped"], "herdr_unreachable")
        self.assertEqual(payload["pane_id"], "w2:p2")
        self.assertTrue(self.ts.session.hooks_log.is_file())


class ReconcileDetectedTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.api = FakeApi(self.ts.socket_path)
        self.api.set_response("agent.rename", {"type": "ok"})
        self.api.set_response("pane.rename", {"type": "ok"})

    def calls(self, method):
        return [p for m, p in self.api.calls if m == method]

    def test_missing_member_becomes_active_and_is_idempotent(self):
        doc = store.read_json(self.ts.team.team_json)
        doc["members"][0]["status"] = "missing"
        store.write_json(self.ts.team.team_json, doc)
        code, out = run_event(self.ts, self.api)
        self.assertEqual(code, 0)
        self.assertEqual(out["team"], "alpha")
        self.assertEqual(len(out["changes"]), 1)
        change = out["changes"][0]
        self.assertEqual(change["member"], "alpha-reviewer")
        self.assertEqual(change["fields"]["status"], "active")
        self.assertEqual(change["fields"]["generation"], 2)
        m = member(self.ts, "alpha-reviewer")
        self.assertEqual((m["status"], m["generation"]), ("active", 2))
        self.assertEqual(self.calls("agent.rename"), [])  # live name already matches
        stamps = self.calls("pane.report_metadata")
        self.assertEqual(stamps[0]["tokens"], {"team": "alpha", "team_role": "reviewer"})
        self.assertEqual(self.calls("pane.rename")[0]["label"], "team:alpha/reviewer")
        record = store.read_json(self.ts.session.pane_record("term_r1"))
        self.assertEqual(record, {"team": "alpha", "name": "alpha-reviewer", "gen": 2})
        revision = store.read_json(self.ts.team.team_json)["revision"]
        # second run: nothing changes, revision stays
        code, out = run_event(self.ts, self.api)
        self.assertEqual(out["changes"], [])
        self.assertEqual(store.read_json(self.ts.team.team_json)["revision"], revision)

    def test_lost_name_is_reapplied(self):
        self.api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": fake_agent("w2:p1", "term_r1", "codex", None)})
        code, out = run_event(self.ts, self.api)
        self.assertEqual(self.calls("agent.rename"), [{"target": "w2:p1", "name": "alpha-reviewer"}])
        self.assertEqual(member(self.ts, "alpha-reviewer")["status"], "active")

    def test_name_taken_marks_conflict(self):
        self.api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": fake_agent("w2:p1", "term_r1", "codex", None)})
        self.api.set_error("agent.rename", "agent_name_taken", "name alpha-reviewer is taken")
        code, out = run_event(self.ts, self.api)
        self.assertEqual(member(self.ts, "alpha-reviewer")["status"], "name_conflict")

    def test_human_rename_is_adopted(self):
        self.api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": fake_agent("w2:p1", "term_r1", "codex", "alice")})
        code, out = run_event(self.ts, self.api)
        m = member(self.ts, "alice")
        self.assertEqual(m["previous_names"][0]["name"], "alpha-reviewer")
        self.assertEqual(self.calls("agent.rename"), [])

    def test_restart_rebinds_by_pane_id_and_kind(self):
        # cold restart: new terminal id on the same pane, same kind, no name
        self.api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": fake_agent("w2:p1", "term_new", "codex", None)})
        doc = store.read_json(self.ts.team.team_json)
        doc["members"][0]["status"] = "missing"
        store.write_json(self.ts.team.team_json, doc)
        code, out = run_event(self.ts, self.api)
        m = member(self.ts, "alpha-reviewer")
        self.assertEqual((m["terminal_id"], m["status"], m["generation"]), ("term_new", "active", 2))
        self.assertEqual(self.calls("agent.rename"), [{"target": "w2:p1", "name": "alpha-reviewer"}])
        self.assertTrue(self.ts.session.pane_record("term_new").is_file())

    def test_different_kind_on_the_terminal_is_kind_changed(self):
        self.api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": fake_agent("w2:p1", "term_r1", "gemini", None)})
        code, out = run_event(self.ts, self.api)
        self.assertEqual(member(self.ts, "alpha-reviewer")["status"], "kind_changed")
        cleared = [p for p in self.calls("pane.report_metadata") if p["tokens"].get("team") is None]
        self.assertTrue(cleared)
        self.assertEqual(self.calls("agent.rename"), [])

    def test_released_agent_marks_members_missing_and_clears_tokens(self):
        self.api.set_error("agent.get", "agent_not_found", "agent target w2:p1 not found")
        code, out = run_event(self.ts, self.api)
        self.assertEqual(member(self.ts, "alpha-reviewer")["status"], "missing")
        self.assertEqual(out["changes"][0]["fields"]["status"], "missing")
        self.assertTrue(self.calls("pane.report_metadata"))

    def test_launch_pending_never_renames(self):
        self.api.set_response("agent.get", lambda params: {"type": "agent_info", "agent": fake_agent("w2:p1", "term_r1", "codex", None, launch_pending=True)})
        code, out = run_event(self.ts, self.api)
        self.assertEqual(self.calls("agent.rename"), [])

    def test_unrelated_pane_touches_nothing(self):
        code, out = run_event(self.ts, self.api, pane_id="wA:p6")
        self.assertEqual(out["changes"], [])
        self.assertIsNone(out["team"])
        self.assertEqual(member(self.ts, "alpha-reviewer")["status"], "active")
        self.assertEqual(member(self.ts, "alpha-worker")["status"], "active")

    def test_identity_env_is_scrubbed_for_the_real_api(self):
        # No injected api: the hook builds one from a scrubbed env; the socket is absent so it reports unreachable.
        out = io.StringIO()
        env = event_env(self.ts)
        code = hooks.run_hook_event(["agent_detected"], env, stdout=out)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["skipped"], "herdr_unreachable")
        log = self.ts.session.hooks_log.read_text()
        self.assertIn("Herdr unreachable", log)


class ReconcileGoneTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.api = FakeApi(self.ts.socket_path)

    def test_pane_closed_marks_missing(self):
        self.api.set_error("agent.get", "agent_not_found", "gone")
        code, out = run_event(self.ts, self.api, "pane_closed", pane_id="w2:p2")
        self.assertEqual(member(self.ts, "alpha-worker")["status"], "missing")
        self.assertEqual(member(self.ts, "alpha-reviewer")["status"], "active")
        cleared = [p for m, p in self.api.calls if m == "pane.report_metadata"]
        self.assertEqual(cleared, [])  # closed panes get no token calls

    def test_pane_closed_records_a_gone_console_as_closed(self):
        """UI-05, daemon dead: the hook fixes console.json when the console terminal left pane.list."""
        from support import fake_pane

        self.api.set_error("agent.get", "agent_not_found", "gone")
        store.write_json(self.ts.session.console_json, {"pane_id": "w3:p2", "terminal_id": "term_console", "pid": os.getpid(), "open": True, "default_team": "alpha"})
        self.api.set_response("pane.list", {"type": "pane_list", "panes": [fake_pane("w1:p1", "term_shell")]})
        code, out = run_event(self.ts, self.api, "pane_closed", pane_id="w3:p2")
        self.assertEqual(code, 0)
        self.assertTrue(out.get("console_closed"))
        doc = store.read_json(self.ts.session.console_json)
        self.assertEqual((doc["open"], doc["pid"]), (False, None))
        # pane_exited never touches the record (the pane, and the terminal, still exist)
        store.write_json(self.ts.session.console_json, {"pane_id": "w3:p2", "terminal_id": "term_console", "pid": os.getpid(), "open": True})
        code, out = run_event(self.ts, self.api, "pane_exited", pane_id="w3:p2")
        self.assertNotIn("console_closed", out)
        self.assertTrue(store.read_json(self.ts.session.console_json)["open"])

    def test_pane_exited_clears_tokens(self):
        self.api.set_error("agent.get", "agent_not_found", "gone")
        code, out = run_event(self.ts, self.api, "pane_exited", pane_id="w2:p2")
        self.assertEqual(member(self.ts, "alpha-worker")["status"], "missing")
        cleared = [p for m, p in self.api.calls if m == "pane.report_metadata"]
        self.assertEqual(cleared[0]["pane_id"], "w2:p2")

    def test_still_present_agent_is_a_no_op(self):
        code, out = run_event(self.ts, self.api, "pane_exited", pane_id="w2:p2")
        self.assertEqual(out["changes"], [])
        self.assertEqual(member(self.ts, "alpha-worker")["status"], "active")


class StopDecisionTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.layout = self.ts.layout
        self.board = store.BoardStore(self.ts.team)

    def post(self, to, text="hello", author="alpha-worker"):
        return self.board.append({"from": author, "from_kind": "claude", "to": [to], "kind": "request", "text": text, "origin": {"via": "cli", "verified": True}})

    def test_blocks_once_per_new_posts_and_caps(self):
        seq = self.post("alpha-reviewer")
        code, text = hooks.stop_decision(self.layout, "alpha", "alpha-reviewer", {}, now=1000.0)
        self.assertEqual(code, 2)
        self.assertIn("[herdr-team stop] 1 unread board post for alpha-reviewer (seq {})".format(seq), text)
        # same posts again: released
        self.assertEqual(hooks.stop_decision(self.layout, "alpha", "alpha-reviewer", {}, now=1001.0), (0, ""))
        # newer posts block again, up to three per window
        self.post("alpha-reviewer", "two")
        self.assertEqual(hooks.stop_decision(self.layout, "alpha", "alpha-reviewer", {}, now=1002.0)[0], 2)
        self.post("all", "three")
        self.assertEqual(hooks.stop_decision(self.layout, "alpha", "alpha-reviewer", {}, now=1003.0)[0], 2)
        self.post("alpha-reviewer", "four")
        self.assertEqual(hooks.stop_decision(self.layout, "alpha", "alpha-reviewer", {}, now=1004.0), (0, ""))
        # window expired: blocks again
        self.assertEqual(hooks.stop_decision(self.layout, "alpha", "alpha-reviewer", {}, now=1000.0 + 601.0)[0], 2)

    def test_stop_hook_active_and_mute_release(self):
        self.post("alpha-reviewer")
        self.assertEqual(hooks.stop_decision(self.layout, "alpha", "alpha-reviewer", {"stop_hook_active": True}), (0, ""))
        store.write_json(self.ts.team.mute_json, {"*": None})
        self.assertEqual(hooks.stop_decision(self.layout, "alpha", "alpha-reviewer", {}), (0, ""))
        store.write_json(self.ts.team.mute_json, {"alpha-reviewer": "2099-01-01T00:00:00Z"})
        self.assertEqual(hooks.stop_decision(self.layout, "alpha", "alpha-reviewer", {}), (0, ""))
        store.write_json(self.ts.team.mute_json, {"alpha-reviewer": "2000-01-01T00:00:00Z"})
        self.assertEqual(hooks.stop_decision(self.layout, "alpha", "alpha-reviewer", {})[0], 2)

    def test_own_posts_and_read_posts_do_not_block(self):
        self.post("alpha-reviewer", author="alpha-reviewer")
        self.assertEqual(hooks.stop_decision(self.layout, "alpha", "alpha-reviewer", {}), (0, ""))
        seq = self.post("alpha-reviewer")
        store.Cursors(self.ts.team).advance("alpha-reviewer", seq, "term_r1", "cli")
        self.assertEqual(hooks.stop_decision(self.layout, "alpha", "alpha-reviewer", {}), (0, ""))

    def test_cmd_hooks_maps_block_to_exit_7(self):
        from herdr_team import cmd_hooks

        self.post("alpha-reviewer")
        code, text = cmd_hooks.stop_decision(self.layout, "alpha", "alpha-reviewer", {})
        self.assertEqual(code, cmd_hooks.EXIT_HOOK_BLOCK)
        self.assertTrue(text.startswith("[herdr-team stop]"))


class LogTests(unittest.TestCase):
    def test_log_line_writes_and_rotates(self):
        ts = TempState()
        self.addCleanup(ts.cleanup)
        hooks.log_line(ts.layout, "hello")
        text = ts.session.hooks_log.read_text()
        self.assertIn("hello", text)
        self.assertRegex(text, r"^\d{4}-\d\d-\d\dT")
        with open(ts.session.hooks_log, "ab") as fh:
            fh.write(b"x" * (hooks.LOG_ROTATE_BYTES + 10) + b"\n")
        hooks.log_line(ts.layout, "after")
        self.assertTrue((ts.session.root / "hooks.log.1").is_file())
        self.assertLess(ts.session.hooks_log.stat().st_size, 200)
        self.assertIn("after", ts.session.hooks_log.read_text())

    def test_alarm_is_armed_and_cleared(self):
        ts = TempState()
        self.addCleanup(ts.cleanup)
        previous = signal.getsignal(signal.SIGALRM)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        api = FakeApi(ts.socket_path)
        out = io.StringIO()
        hooks.run_hook_event(["agent_detected"], event_env(ts), api=api, stdout=out)
        self.assertEqual(signal.alarm(0), 0, "alarm left armed after the hook")

    def test_alarm_fires_exit_0_in_a_subprocess(self):
        ts = TempState()
        self.addCleanup(ts.cleanup)
        script = (
            "import sys, time, json\n"
            "sys.path.insert(0, {root!r})\n"
            "from herdr_team import hooks\n"
            "hooks.ALARM_S = 1\n"
            "class Slow:\n"
            "    def request(self, method, params=None, timeout=None):\n"
            "        time.sleep(5)\n"
            "        return {{}}\n"
            "env = json.loads(sys.argv[1])\n"
            "sys.exit(hooks.run_hook_event(['agent_detected'], env, api=Slow()))\n"
        ).format(root=os.fspath(paths.plugin_root()))
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, "-c", script, json.dumps(event_env(ts))], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        elapsed = time.monotonic() - t0
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 4.0)
        payload = json.loads(proc.stdout.decode().strip().splitlines()[-1])
        self.assertEqual(payload["skipped"], "timeout")
        self.assertIn("alarm", ts.session.hooks_log.read_text())


if __name__ == "__main__":
    unittest.main()
