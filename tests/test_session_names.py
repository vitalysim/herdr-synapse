import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from herdr_team import roster, session_names as names
from support import TempState, fake_agent


class SessionNameTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.cleanup)
        self.paths = self.state.layout.team(self.state.team_name)
        self.member = roster.load_team(self.paths).find("alpha-worker").to_json()
        self.member["kind"] = "codex"
        self.name = self.member["name"]
        self.live = fake_agent(self.member["pane_id"], self.member["terminal_id"], "codex", self.name)
        self.entry = dict(names.prepare("codex", self.name, [])[1], terminal_id=self.member["terminal_id"], generation=self.member["generation"])
        names.save(self.paths, self.name, self.entry)
        self.team = SimpleNamespace(paths=self.paths, name=self.state.team_name, member=lambda name: self.member)
        self.daemon = Mock()
        self.daemon.api.request.return_value = {"agent": self.live}
        self.daemon._prompt_line.return_value = ""
        self.daemon._type_keystroke.return_value = ("landed_working", {})
        self.daemon.agents = {self.member["terminal_id"]: self.live}

    def test_claude_flag_and_codex_no_fake_flag(self):
        self.assertEqual(names.prepare("claude", self.name, ["--model", "some-model"])[0], ["--model", "some-model", "--name", self.name])
        self.assertEqual(names.prepare("codex", self.name, ["--yolo"])[0], ["--yolo"])

    def test_opencode_uses_managed_loopback_tui(self):
        argv, entry = names.prepare("opencode", self.name, [])
        self.assertEqual(argv[:3], ["--hostname", "127.0.0.1", "--port"])
        self.assertEqual(int(argv[3]), entry["port"])
        self.assertNotIn("run", argv)

    def test_codex_rename_runs_once_before_brief(self):
        self.assertTrue(names.before_brief(self.daemon, self.team, self.member))
        self.daemon._type_keystroke.assert_called_once_with(self.member["pane_id"], "/rename " + self.name)
        self.assertFalse(names.before_brief(self.daemon, self.team, self.member))
        self.assertEqual(names.load(self.paths)[self.name]["status"], "sent")

    def test_draft_and_unknown_composer_block_rename(self):
        for text in ("my unsent work", None):
            self.daemon._prompt_line.return_value = text
            self.assertTrue(names.before_brief(self.daemon, self.team, self.member))
        self.daemon._type_keystroke.assert_not_called()

    def test_changed_terminal_or_busy_agent_cannot_receive_rename(self):
        for changes in ({"terminal_id": "replacement"}, {"agent_status": "working"}):
            self.daemon.api.request.return_value = {"agent": dict(self.live, **changes)}
            self.assertTrue(names.before_brief(self.daemon, self.team, self.member))
        self.daemon._type_keystroke.assert_not_called()

    def test_uncertain_send_is_not_replayed(self):
        self.daemon._type_keystroke.return_value = ("transient", {"code": "timeout"})
        names.before_brief(self.daemon, self.team, self.member)
        self.assertFalse(names.before_brief(self.daemon, self.team, self.member))
        self.assertEqual(names.load(self.paths)[self.name]["status"], "failed")
        self.daemon._append_system.assert_called_once()

    def test_deadline_does_not_block_brief_forever(self):
        names.save(self.paths, self.name, dict(self.entry, created=time.time() - 121))
        self.assertFalse(names.before_brief(self.daemon, self.team, self.member))
        self.daemon._type_keystroke.assert_not_called()

    def test_exact_session_and_generation_required(self):
        entry = dict(self.entry, session_id="original")
        live = dict(self.live, agent_session={"source": "herdr:codex", "kind": "id", "value": "replacement"})
        self.assertFalse(names.current(entry, self.member, live))
        live["agent_session"]["value"] = "original"
        self.assertTrue(names.current(entry, self.member, live))
        self.assertFalse(names.current(entry, dict(self.member, generation=99), live))

    def test_codex_success_verified_against_native_index(self):
        self.live["agent_session"] = {"source": "herdr:codex", "kind": "id", "value": "session-one"}
        names.save(self.paths, self.name, dict(self.entry, session_id="session-one", status="sent", sent_at=time.time()))
        with patch.object(names, "codex_title", return_value=self.name):
            names.poll(self.daemon, self.team, time.time())
        self.assertEqual(names.load(self.paths)[self.name]["status"], "complete")
        with patch.object(names, "codex_title") as title:
            names.poll(self.daemon, self.team, time.time() + 3)
            title.assert_not_called()

    def test_opencode_retry_bounded(self):
        self.member["kind"] = self.live["agent"] = "opencode"
        self.live["agent_session"] = {"source": "herdr:opencode", "kind": "id", "value": "opencode-one"}
        names.save(self.paths, self.name, dict(self.entry, kind="opencode", port=12345))
        with patch.object(names, "opencode_title", side_effect=OSError("offline")) as call:
            for offset in (0, 6, 12, 18):
                names.poll(self.daemon, self.team, time.time() + offset)
            self.assertEqual(call.call_count, 3)
        self.assertEqual(names.load(self.paths)[self.name]["status"], "failed")

    def test_arm_captures_resolved_session_before_roster_report(self):
        member = roster.load_team(self.paths).find(self.name)
        member.kind = "codex"
        names.arm(self.paths, member, self.entry, {"value": "original"})
        entry = names.load(self.paths)[self.name]
        self.assertEqual(entry["session_id"], "original")
        live = dict(self.live, agent_session={"source": "herdr:codex", "value": "replacement"})
        self.assertFalse(names.current(entry, dict(self.member, generation=self.member["generation"] + 1), live))

    def test_first_opencode_report_requires_delivered_briefing(self):
        self.member["kind"] = self.live["agent"] = "opencode"
        self.member["generation"] += 1
        self.live["agent_session"] = {"source": "herdr:opencode", "value": "first"}
        entry = dict(self.entry, kind="opencode", port=12345)
        names.save(self.paths, self.name, entry)
        self.team.pending = {self.name: SimpleNamespace(kind="brief", landed_ms=42)}
        with patch.object(names, "opencode_title") as rename:
            names.poll(self.daemon, self.team, time.time())
            rename.assert_called_once_with(12345, "first", self.name)
        self.assertEqual(names.load(self.paths)[self.name]["status"], "complete")

    def test_unexplained_first_report_cannot_rename_replacement(self):
        self.member["kind"] = self.live["agent"] = "opencode"
        self.member["generation"] += 1
        self.live["agent_session"] = {"source": "herdr:opencode", "value": "replacement"}
        names.save(self.paths, self.name, dict(self.entry, kind="opencode", port=12345))
        with patch.object(names, "opencode_title") as rename:
            names.poll(self.daemon, self.team, time.time())
            rename.assert_not_called()

    def test_native_index_uses_exact_id_and_latest_title(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"CODEX_HOME": tmp}):
            Path(tmp, "session_index.jsonl").write_text('\n'.join(json.dumps(row) for row in [
                {"id": "one", "thread_name": "old"}, {"id": "two", "thread_name": "wrong"}, {"id": "one", "thread_name": "new"}]))
            self.assertEqual(names.codex_title("one"), "new")
            self.assertIsNone(names.codex_title("missing"))


if __name__ == "__main__":
    unittest.main()
