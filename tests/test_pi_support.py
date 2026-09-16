"""Pi parity, identity and failure contracts; no real HOME or model calls."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from herdr_team import cmd_usage, context, gate, models, pi_support as pi, roster, session_names, store
from herdr_team import daemon as D
from herdr_team.errors import HerdrTeamError
from support import FakeApi, TempState, fake_agent

EDITOR = "──────────────────────────────────────────────────────────────────────\n{}\n──────────────────────────────────────────────────────────────────────\n/private/var/tmp • pi-probe\n$0.000 (sub) 0.0%/272k (auto)    (openai-codex) gpt-5.5 • thinking off\n"


class PiTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.native = {"source": "herdr:pi", "agent": "pi", "kind": "path", "value": "/tmp/pi-session.jsonl"}
        self.member = {"name": "alpha-worker", "kind": "pi", "pane_id": "w2:p2", "terminal_id": "term_w1", "session": self.native}
        self.agent = fake_agent("w2:p2", "term_w1", "pi", "alpha-worker", agent_session=self.native)
        self.api = FakeApi()
        self.api.set_response("agent.get", {"agent": self.agent})
        self.env = dict(self.ts.env, HERDR_ENV="1", HERDR_PANE_ID="w2:p2")
        self.now = time.time()
        self.payload = {"version": 1, "mode": "tui", "instance": "one", "at": self.now,
                        "session": self.native["value"], "provider": "custom", "model": "future-model",
                        "used": 42000, "window": 123456, "effort": "high", "compact": None}

    def receive(self, **updates):
        pi.receive(self.ts.layout, self.api, self.env, dict(self.payload, **updates), self.now)

    def test_snapshot_is_bound_to_native_session_and_expires(self):
        self.receive()
        data = pi.read_snapshot(self.ts.layout.session, self.member, self.now)
        self.assertEqual(pi.reading(data).to_json()["window"], 123456)
        self.assertTrue(pi.reading(data).estimated)
        for update in ({"pane_id": "w2:p3"}, {"terminal_id": "term_other"},
                       {"session": dict(self.native, value="/tmp/another.jsonl")}):
            self.assertIsNone(pi.read_snapshot(self.ts.layout.session, dict(self.member, **update), self.now))
        self.assertIsNone(pi.read_snapshot(self.ts.layout.session, self.member, self.now + pi.TTL + 1))

    def test_reject_wrong_kind_session_source_mode_and_out_of_order(self):
        for patch in ({"session": "/tmp/other.jsonl"}, {"mode": "rpc"}, {"at": self.now - 50},
                      {"at": float("nan")}, {"at": 10**1000},
                      {"compact": {"id": "bad", "at": 10**1000, "status": "success"}},
                      {"window": True}, {"used": -1}, {"model": "bad\nmodel"}):
            with self.assertRaises(HerdrTeamError, msg=patch):
                self.receive(**patch)
        self.agent["agent"] = "codex"
        with self.assertRaises(HerdrTeamError):
            self.receive()
        self.agent["agent"] = "pi"
        self.receive()
        self.receive(at=self.now - 1, window=999)
        self.assertEqual(pi.read_snapshot(self.ts.layout.session, self.member, self.now)["window"], 123456)

    def test_unknown_usage_keeps_runtime_model_and_window(self):
        self.receive(used=None)
        reading = pi.reading(pi.read_snapshot(self.ts.layout.session, self.member, self.now))
        self.assertEqual(reading.model, "custom/future-model")
        self.assertIsNone(reading.percent)
        self.assertIsNone(reading.to_json()["used"])
        self.assertEqual(reading.window, 123456)
        rendered = cmd_usage.render_context({"team": "alpha", "members": [{"name": "p", "kind": "pi", "context": reading.to_json()}]}, 100, False)
        self.assertIn("unknown tokens", rendered)
        self.assertIn("window 123,456", rendered)
        self.assertNotIn("0 tokens", rendered)

    def test_model_changes_update_limit_without_a_catalogue(self):
        self.receive()
        self.receive(at=self.now + 1, model="other", window=987654)
        result = pi.reading(pi.read_snapshot(self.ts.layout.session, self.member, self.now))
        self.assertEqual((result.model, result.window), ("custom/other", 987654))

    def test_native_argv_exact_resume_and_fresh_names(self):
        self.assertEqual(models.launch_args("pi", "custom/future-model", "off"),
                         ["--model", "custom/future-model", "--thinking", "off", "--approve"])
        self.assertEqual(models.resume_argv("pi", self.native, None, None), ["pi", "--session", self.native["value"], "--approve"])
        with self.assertRaises(HerdrTeamError):
            models.resume_argv("pi", dict(self.native, kind="id", value="partial-id"), None, None)
        self.assertEqual(session_names.prepare("pi", "alpha-worker", ["--approve"])[0], ["--approve", "--name", "alpha-worker"])
        self.assertIsNone(models.live_keystrokes("pi", "x", None))
        self.assertEqual(models.exit_keystroke("pi"), "/quit")
        args = ["node", "/opt/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js", "--api-key", "SECRET", "--no-tools"]
        self.assertEqual(models.preserved_launch_args("pi", models.foreground_argv("pi", [{"argv": args}])), ["--no-tools"])

    def test_pi_editor_requires_borders_and_native_footer_and_preserves_multiline_draft(self):
        self.assertEqual(gate.prompt_line_text(EDITOR.format(""), "pi"), "")
        self.assertEqual(gate.prompt_line_text(EDITOR.format("").replace("0.0%/", "?/"), "pi"), "")
        working = EDITOR.format("").replace("─" * 70, "── ⠹ Working " + "─" * 58, 1)
        self.assertEqual(gate.prompt_line_text(working, "pi"), "")
        self.assertEqual(gate.prompt_line_text(working.replace("\n\n", "\nreal draft\n"), "pi"), "real draft")
        self.assertEqual(gate.prompt_line_text(EDITOR.format("first\nsecond"), "pi"), "first\nsecond")
        self.assertIsNone(gate.prompt_line_text(EDITOR.format("unsent draft\n" + "─" * 70), "pi"))
        self.assertIsNone(gate.prompt_line_text(EDITOR.format("─" * 70), "pi"))
        self.assertIsNone(gate.prompt_line_text("Pick a model\n> model", "pi"))
        self.assertIsNone(gate.prompt_line_text("Compacting context... (escape to cancel)\n" + EDITOR.format(""), "pi"))
        self.assertIsNone(gate.prompt_line_text(EDITOR.format("").replace("%/", "% of "), "pi"))
        narrow = "─" * 36 + "\n{}\n" + "─" * 36 + "\n/private/var/tmp/herdr-synapse-pi...\n↑24k ↓780 R30k CH0.0% $0.161 (sub...\n"
        self.assertEqual(gate.prompt_line_text(narrow.format(""), "pi"), "")
        self.assertEqual(gate.prompt_line_text(narrow.format("draft"), "pi"), "draft")
        self.assertEqual(gate.prompt_line_text(EDITOR.format("").replace(" (auto)", ""), "pi"), "")

    def test_pi_gate_preflight_can_read_editor_but_final_unknown_and_drafts_refuse(self):
        from test_gate import snap, pend, NOW

        base = dict(kind="pi", agent_kind="pi", screen_detection_skipped=True)
        for text, allowed in ((None, True), (EDITOR.format(""), True), ("", False),
                              ("Select a model", False), (EDITOR.format("draft\nmore"), False)):
            decision = gate.evaluate(snap(**base, detection_text=text), pend(), NOW, None, 0)
            self.assertEqual(decision.deliver, allowed, (text, decision))

    def test_installer_respects_override_idempotence_dry_run_and_foreign_files(self):
        with tempfile.TemporaryDirectory() as root:
            args = SimpleNamespace(env={"PI_CODING_AGENT_DIR": root}, action="install", dry_run=True)
            target = pi.extension_path(args.env)
            self.assertTrue(pi.install(args, Path("/bin/cli"))["would_write"])
            self.assertFalse(target.exists())
            args.dry_run = False
            self.assertTrue(pi.install(args, Path("/bin/cli"))["installed"])
            self.assertTrue(pi.install(args, Path("/bin/cli"))["already"])
            args.action = "check"
            self.assertTrue(pi.install(args, Path("/bin/cli"))["ok"])
            args.action = "uninstall"
            pi.install(args, Path("/bin/cli"))
            self.assertFalse(target.exists())
            target.write_text("foreign", encoding="utf-8")
            args.action = "install"
            with self.assertRaises(HerdrTeamError):
                pi.install(args, Path("/bin/cli"))
            self.assertEqual(target.read_text(), "foreign")

    def test_control_identity_never_matches_a_replacement_or_other_session(self):
        control = {"terminal_id": "term_w1", "native_session": self.native["value"]}
        self.assertTrue(pi.matches_control(self.payload, self.member, control))
        self.assertFalse(pi.matches_control(dict(self.payload, session="/tmp/new.jsonl"), self.member, control))
        self.assertFalse(pi.matches_control(self.payload, dict(self.member, terminal_id="term_other"), control))

    def test_malformed_snapshot_and_prior_timestamp_do_not_crash_or_override_identity(self):
        path = pi.snapshot_path(self.ts.layout.session, "term_w1")
        path.parent.mkdir(parents=True, exist_ok=True)
        for bad in ([], {"at": "oops"}, {"at": None}, {"at": 10**1000},
                    dict(self.payload, at=10**1000, terminal_id="term_w1", pane_id="w2:p2")):
            store.write_json(path, bad)
            self.assertIsNone(pi.read_snapshot(self.ts.layout.session, self.member, self.now))
            self.receive()
            self.assertIsNotNone(pi.read_snapshot(self.ts.layout.session, self.member, self.now))

    def test_native_compaction_success_failure_stale_and_duplicate(self):
        from test_daemon import make_daemon

        daemon, _, _ = make_daemon(self.ts)
        daemon.scan_teams(force=True)
        team = daemon.teams["alpha"]
        name = "alpha-worker"
        runtime = team.rt(name)
        original_note = daemon._note_compacted
        daemon._note_compacted = Mock()
        pending = {"action": "compact", "typed_at": self.now - 1, "terminal_id": "term_w1",
                   "native_session": self.native["value"]}
        runtime.control_pending = dict(pending)
        data = dict(self.payload, compact={"id": "old", "at": self.now - 2, "status": "success"})
        daemon._poll_pi_compaction(team, self.member, data, 1000)
        daemon._note_compacted.assert_not_called()
        data["compact"] = {"id": "new", "at": self.now, "status": "success"}
        other_session = dict(data, session="/tmp/replacement.jsonl")
        other_session["compact"] = dict(data["compact"], id="other-session")
        daemon._poll_pi_compaction(team, self.member, other_session, 1000)
        daemon._note_compacted.assert_not_called()
        daemon._poll_pi_compaction(team, dict(self.member, terminal_id="term_replaced"), dict(data, compact=dict(data["compact"], id="other-terminal")), 1000)
        daemon._note_compacted.assert_not_called()
        daemon._poll_pi_compaction(team, self.member, data, 1000)
        daemon._poll_pi_compaction(team, self.member, data, 1000)
        self.assertEqual(daemon._note_compacted.call_count, 1)
        data["compact"] = {"id": "failed", "at": self.now, "status": "failed"}
        daemon._note_compacted = original_note
        runtime.control_pending = dict(pending)
        team.pending[name] = D.Pending(kind="control", landed_ms=900)
        daemon._poll_pi_compaction(team, self.member, data, 1000)
        self.assertIsNone(runtime.control_pending)
        self.assertNotIn(name, team.pending)
        records = store.BoardStore(team.paths).read(last=10)
        self.assertTrue(any("Pi compaction failed" in record.get("text", "") for record in records))

    def test_model_completion_requires_new_instance_current_session_and_actual_effort(self):
        from test_daemon import make_daemon

        doc = store.read_json(self.ts.team.team_json)
        member = next(m for m in doc["members"] if m["name"] == "alpha-worker")
        member.update(self.member)
        store.write_json(self.ts.team.team_json, doc)
        daemon, _, _ = make_daemon(self.ts)
        daemon.scan_teams(force=True)
        team = daemon.teams["alpha"]
        rt = team.rt("alpha-worker")
        rt.control_pending = {"action": "model", "terminal_id": "term_w1", "native_session": self.native["value"],
                              "model": "custom/future-model", "effort": "high", "typed_at": self.now - 1,
                              "pi_previous_instance": "one"}
        daemon._note_model_applied = Mock()
        for index, (instance, effort, expected) in enumerate((("one", "high", 0), ("two", "off", 0), ("two", "high", 1))):
            self.receive(instance=instance, effort=effort, at=self.now + index, used=None)
            rt.context_read_ms = None
            daemon.poll_context(team, 1000 + index * 20000)
            self.assertEqual(daemon._note_model_applied.call_count, expected)
