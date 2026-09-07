"""``herdr_team.claude_settings``: install/uninstall round trips beside Herdr's own hook (plan 9.4, SK-05, SK-06)."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from herdr_team import claude_settings as cs
from herdr_team.errors import HerdrTeamError

# Herdr's own installer (src/integration/claude_settings.rs) writes exactly this
# entry object for SessionStart: matcher "*", one command hook, timeout 10, the
# command being `bash '<single-quoted path>' session`.
HERDR_HOOK_PATH = "/Users/v/.config/herdr/integrations/claude/herdr-claude-hook.sh"


def herdr_command(action: str) -> str:
    return "bash {} {}".format(cs.shell_single_quote(HERDR_HOOK_PATH), action)


def herdr_canonical_entry() -> Dict[str, Any]:
    return {"matcher": "*", "hooks": [{"type": "command", "command": herdr_command("session"), "timeout": 10}]}


def herdr_install(doc: Dict[str, Any]) -> None:
    """Mirror of Herdr's ``ensure_command_hook`` for SessionStart: append its canonical entry unless present."""
    hooks = doc.setdefault("hooks", {})
    entries = hooks.setdefault("SessionStart", [])
    for entry in entries:
        for hook in entry.get("hooks", []):
            if hook.get("type") == "command" and hook.get("command") == herdr_command("session"):
                return
    entries.append(herdr_canonical_entry())


def herdr_uninstall(doc: Dict[str, Any]) -> None:
    """Mirror of Herdr's removal: exact command match on its own actions, drop an entry only when its hooks empty."""
    removals = {"SessionStart": ("idle", "session"), "UserPromptSubmit": ("working",), "Stop": ("idle",), "PostToolUse": ("working",)}
    hooks = doc.get("hooks") or {}
    for event, actions in removals.items():
        commands = [herdr_command(a) for a in actions]
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            entry["hooks"] = [h for h in entry.get("hooks", []) if not (h.get("type") == "command" and h.get("command") in commands)]
            if entry["hooks"]:
                kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]


def commands_in(doc: Dict[str, Any], event: str) -> List[str]:
    out: List[str] = []
    for entry in (doc.get("hooks") or {}).get(event, []):
        for hook in entry.get("hooks", []):
            out.append(hook["command"])
    return out


class SettingsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ht-cs-"))
        self.addCleanup(self._cleanup)
        self.claude_dir = self.tmp / "home" / ".claude"
        self.claude_dir.mkdir(parents=True)
        self.settings = self.claude_dir / "settings.json"
        self.hook = self.claude_dir / "hooks" / cs.HOOK_FILE_NAME

    def _cleanup(self) -> None:
        import shutil

        for root, dirs, _files in os.walk(self.tmp):
            for d in dirs:
                try:
                    os.chmod(os.path.join(root, d), 0o700)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text: str) -> None:
        self.settings.write_text(text, encoding="utf-8")

    def load(self) -> Dict[str, Any]:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def ours(self, event: str) -> str:
        return cs.hook_command(self.hook, event)


class EntryShapeTests(SettingsCase):
    def test_command_string_matches_herdr_quoting(self):
        path = Path("/Users/some one/.claude/hooks/herdr-synapse-hook.sh")
        self.assertEqual(cs.hook_command(path, "SessionStart"), "bash '/Users/some one/.claude/hooks/herdr-synapse-hook.sh' session-start")
        self.assertEqual(cs.hook_command(Path("/a'b/h.sh"), "Stop"), "bash '/a'\"'\"'b/h.sh' stop")
        self.assertEqual(cs.shell_single_quote(HERDR_HOOK_PATH), "'" + HERDR_HOOK_PATH + "'")

    def test_entries_are_separate_objects_with_plan_timeouts(self):
        session = cs.entry_for(self.hook, "SessionStart")
        self.assertEqual(list(session.keys()), ["matcher", "hooks"])
        self.assertEqual(session["matcher"], "*")
        self.assertEqual(session["hooks"], [{"type": "command", "command": self.ours("SessionStart"), "timeout": 10}])
        prompt = cs.entry_for(self.hook, "UserPromptSubmit")
        self.assertNotIn("matcher", prompt)
        self.assertEqual(prompt["hooks"][0]["timeout"], 5)
        stop = cs.entry_for(self.hook, "Stop")
        self.assertNotIn("matcher", stop)
        self.assertEqual(stop["hooks"][0]["timeout"], 10)
        self.assertEqual(set(cs.HOOK_EVENTS), {"SessionStart", "UserPromptSubmit", "Stop"})

    def test_manual_entries_is_valid_json_for_all_three(self):
        manual = json.loads(cs.manual_entries(self.hook))
        self.assertEqual(set(manual), {"SessionStart", "UserPromptSubmit", "Stop"})
        self.assertEqual(manual["SessionStart"], [cs.entry_for(self.hook, "SessionStart")])


class StrictParseTests(SettingsCase):
    def test_duplicate_keys_rejected(self):
        with self.assertRaises(cs.StrictJsonError):
            cs.parse_strict('{"hooks": {}, "hooks": {}}')
        with self.assertRaises(cs.StrictJsonError):
            cs.parse_strict('{"hooks": {"Stop": [], "Stop": []}}')

    def test_comments_and_trailing_commas_rejected(self):
        with self.assertRaises(cs.StrictJsonError):
            cs.parse_strict('{"a": 1, // comment\n "b": 2}')
        with self.assertRaises(cs.StrictJsonError):
            cs.parse_strict('{"a": 1,}')
        with self.assertRaises(cs.StrictJsonError):
            cs.parse_strict("[1, 2]")
        with self.assertRaises(cs.StrictJsonError):
            cs.parse_strict('{"hooks": []}')

    def test_missing_and_empty_files_are_empty_objects(self):
        self.assertEqual(cs.load_strict(self.settings), {})
        self.write("   \n")
        self.assertEqual(cs.load_strict(self.settings), {})

    def test_install_refuses_unparseable_and_prints_manual_entries(self):
        original = '{"hooks": {"Stop": []}, "hooks": {}}'
        self.write(original)
        with self.assertRaises(HerdrTeamError) as ctx:
            cs.install(self.settings, self.hook)
        err = ctx.exception
        self.assertEqual(err.code, "settings_unparseable")
        self.assertEqual(err.exit_code, 1)
        manual = json.loads(err.details["manual"])
        self.assertEqual(set(manual), {"SessionStart", "UserPromptSubmit", "Stop"})
        self.assertEqual(err.details["commands"]["Stop"], self.ours("Stop"))
        self.assertEqual(self.settings.read_text(encoding="utf-8"), original, "the file must be left untouched")
        self.assertFalse(cs.backup_path(self.settings).exists())
        with self.assertRaises(HerdrTeamError) as ctx2:
            cs.uninstall(self.settings, self.hook)
        self.assertEqual(ctx2.exception.code, "settings_unparseable")


class RoundTripTests(SettingsCase):
    def fixture(self) -> str:
        """A settings file already carrying Herdr's SessionStart entry plus unrelated keys."""
        doc = {
            "zeta": {"escaped": "a", "n": 100},
            "hooks": {
                "Notification": [{"matcher": "keep", "hooks": []}],
                "SessionStart": [herdr_canonical_entry()],
            },
            "alpha": 1,
        }
        return json.dumps(doc, indent=2) + "\n"

    def test_install_beside_herdr_entry_then_uninstall(self):
        self.write(self.fixture())
        result = cs.install(self.settings, self.hook)
        self.assertEqual(result["added"], ["SessionStart", "UserPromptSubmit", "Stop"])
        self.assertEqual(result["already"], [])
        self.assertTrue(result["changed"])
        doc = self.load()
        # Herdr's entry object is untouched and still first; ours is a separate object after it.
        session = doc["hooks"]["SessionStart"]
        self.assertEqual(session[0], herdr_canonical_entry())
        self.assertEqual(session[1], cs.entry_for(self.hook, "SessionStart"))
        self.assertEqual(len(session), 2)
        self.assertEqual(doc["hooks"]["UserPromptSubmit"], [cs.entry_for(self.hook, "UserPromptSubmit")])
        self.assertEqual(doc["hooks"]["Stop"], [cs.entry_for(self.hook, "Stop")])
        self.assertEqual(doc["hooks"]["Notification"], [{"matcher": "keep", "hooks": []}])
        # top-level and hooks key order preserved; new events appended at the end
        self.assertEqual(list(doc.keys()), ["zeta", "hooks", "alpha"])
        self.assertEqual(list(doc["hooks"].keys()), ["Notification", "SessionStart", "UserPromptSubmit", "Stop"])
        self.assertEqual(doc["zeta"], {"escaped": "a", "n": 100})
        # lock and backup
        self.assertTrue(Path(os.fspath(self.settings) + ".herdr-team.lock").exists())
        self.assertEqual(Path(result["backup"]).read_text(encoding="utf-8"), self.fixture())

        removed = cs.uninstall(self.settings, self.hook)
        self.assertEqual(sorted(removed["removed"]), ["SessionStart", "Stop", "UserPromptSubmit"])
        doc = self.load()
        self.assertEqual(doc["hooks"]["SessionStart"], [herdr_canonical_entry()])
        self.assertNotIn("UserPromptSubmit", doc["hooks"], "an emptied event key is dropped")
        self.assertNotIn("Stop", doc["hooks"])
        self.assertEqual(doc["hooks"]["Notification"], [{"matcher": "keep", "hooks": []}])
        self.assertEqual(doc, json.loads(self.fixture()), "uninstall restores the original document")

    def test_herdr_install_and_uninstall_on_top_of_ours(self):
        """SK-05: team hooks, then Herdr installs, then Herdr uninstalls; our entries survive every step."""
        self.write("{}\n")
        cs.install(self.settings, self.hook)
        step1 = self.load()
        self.assertEqual(len(step1["hooks"]["SessionStart"]), 1)

        herdr_install(step1)
        self.settings.write_text(json.dumps(step1, indent=2) + "\n", encoding="utf-8")
        step2 = self.load()
        self.assertEqual(commands_in(step2, "SessionStart"), [self.ours("SessionStart"), herdr_command("session")])
        self.assertEqual(commands_in(step2, "UserPromptSubmit"), [self.ours("UserPromptSubmit")])
        self.assertEqual(commands_in(step2, "Stop"), [self.ours("Stop")])
        self.assertTrue(cs.check(self.settings, self.hook)["installed"])
        # our install is idempotent on top of Herdr's entry
        again = cs.install(self.settings, self.hook)
        self.assertEqual(again["added"], [])
        self.assertEqual(again["already"], ["SessionStart", "UserPromptSubmit", "Stop"])
        self.assertFalse(again["changed"])
        self.assertEqual(self.load(), step2)

        herdr_uninstall(step2)
        self.settings.write_text(json.dumps(step2, indent=2) + "\n", encoding="utf-8")
        step3 = self.load()
        self.assertEqual(commands_in(step3, "SessionStart"), [self.ours("SessionStart")])
        self.assertEqual(commands_in(step3, "UserPromptSubmit"), [self.ours("UserPromptSubmit")])
        self.assertEqual(commands_in(step3, "Stop"), [self.ours("Stop")])
        self.assertTrue(cs.check(self.settings, self.hook)["installed"])

        # and our uninstall on top of Herdr's presence leaves Herdr's entry alone
        herdr_install(step3)
        self.settings.write_text(json.dumps(step3, indent=2) + "\n", encoding="utf-8")
        cs.uninstall(self.settings, self.hook)
        final = self.load()
        self.assertEqual(final["hooks"]["SessionStart"], [herdr_canonical_entry()])
        self.assertEqual(set(final["hooks"]), {"SessionStart"})

    def test_compact_single_line_settings_rewritten_with_indent_two(self):
        compact = '{"alpha":1,"hooks":{"SessionStart":[' + json.dumps(herdr_canonical_entry(), separators=(",", ":")) + ']},"zeta":{"x":1}}'
        self.write(compact)
        cs.install(self.settings, self.hook)
        text = self.settings.read_text(encoding="utf-8")
        self.assertEqual(text, cs.dump(self.load()))
        self.assertTrue(text.startswith('{\n  "alpha": 1,\n  "hooks": {\n    "SessionStart": ['))
        self.assertTrue(text.endswith("}\n"))
        doc = self.load()
        self.assertEqual(list(doc.keys()), ["alpha", "hooks", "zeta"])
        self.assertEqual(doc["hooks"]["SessionStart"][0], herdr_canonical_entry())
        self.assertEqual(Path(cs.backup_path(self.settings)).read_text(encoding="utf-8"), compact)

    def test_key_order_survives_when_hooks_key_is_created(self):
        self.write('{\n  "model": "opus",\n  "permissions": {"allow": ["Bash"]}\n}\n')
        cs.install(self.settings, self.hook)
        doc = self.load()
        self.assertEqual(list(doc.keys()), ["model", "permissions", "hooks"])
        self.assertEqual(list(doc["hooks"].keys()), ["SessionStart", "UserPromptSubmit", "Stop"])
        self.assertEqual(doc["permissions"], {"allow": ["Bash"]})

    def test_missing_settings_file_is_created(self):
        self.assertFalse(self.settings.exists())
        result = cs.install(self.settings, self.hook)
        self.assertIsNone(result["backup"])
        self.assertEqual(set(self.load()["hooks"]), {"SessionStart", "UserPromptSubmit", "Stop"})
        mode = stat.S_IMODE(os.stat(self.settings).st_mode)
        self.assertEqual(mode, 0o600)

    def test_existing_mode_is_kept(self):
        self.write("{}\n")
        os.chmod(self.settings, 0o644)
        cs.install(self.settings, self.hook)
        self.assertEqual(stat.S_IMODE(os.stat(self.settings).st_mode), 0o644)

    def test_uninstall_removes_only_our_command_from_a_shared_entry(self):
        shared = {
            "matcher": "*",
            "hooks": [
                {"type": "command", "command": self.ours("SessionStart"), "timeout": 10},
                {"type": "command", "command": "echo keep", "timeout": 3},
            ],
        }
        self.write(json.dumps({"hooks": {"SessionStart": [shared], "Stop": [cs.entry_for(self.hook, "Stop")]}}, indent=2))
        result = cs.uninstall(self.settings, self.hook)
        self.assertEqual(sorted(result["removed"]), ["SessionStart", "Stop"])
        doc = self.load()
        self.assertEqual(doc["hooks"]["SessionStart"], [{"matcher": "*", "hooks": [{"type": "command", "command": "echo keep", "timeout": 3}]}])
        self.assertNotIn("Stop", doc["hooks"])

    def test_uninstall_without_our_entries_is_a_noop(self):
        text = self.fixture()
        self.write(text)
        result = cs.uninstall(self.settings, self.hook)
        self.assertFalse(result["changed"])
        self.assertEqual(result["removed"], [])
        self.assertEqual(self.settings.read_text(encoding="utf-8"), text, "no rewrite when nothing changes")
        self.assertFalse(cs.backup_path(self.settings).exists())

    def test_symlinked_settings_is_written_through(self):
        real = self.tmp / "dotfiles" / "claude-settings.json"
        real.parent.mkdir(parents=True)
        real.write_text("{}\n", encoding="utf-8")
        os.symlink(os.fspath(real), os.fspath(self.settings))
        cs.install(self.settings, self.hook)
        self.assertTrue(self.settings.is_symlink())
        self.assertIn("UserPromptSubmit", json.loads(real.read_text(encoding="utf-8"))["hooks"])

    def test_entry_for_different_hook_path_is_not_ours(self):
        other = self.claude_dir / "elsewhere" / cs.HOOK_FILE_NAME
        self.write(json.dumps({"hooks": {"Stop": [cs.entry_for(other, "Stop")]}}))
        status = cs.check(self.settings, self.hook)
        self.assertFalse(status["installed"])
        self.assertFalse(status["events"]["Stop"])
        result = cs.uninstall(self.settings, self.hook)
        self.assertFalse(result["changed"])


class CheckAndScanTests(SettingsCase):
    def test_check_reports_events_and_parse_error(self):
        self.write("{}\n")
        status = cs.check(self.settings, self.hook)
        self.assertFalse(status["installed"])
        self.assertEqual(status["events"], {"SessionStart": False, "UserPromptSubmit": False, "Stop": False})
        self.assertIsNone(status["parse_error"])
        self.assertFalse(status["hook_exists"])
        cs.install(self.settings, self.hook)
        status = cs.check(self.settings, self.hook)
        self.assertTrue(status["installed"])
        self.write('{"a": 1, "a": 2}')
        status = cs.check(self.settings, self.hook)
        self.assertFalse(status["installed"])
        self.assertIn("duplicate key", status["parse_error"])

    def test_scan_duplicates_across_four_files(self):
        project = self.tmp / "proj"
        (project / ".claude").mkdir(parents=True)
        stop = cs.entry_for(self.hook, "Stop")
        foreign = {"hooks": [{"type": "command", "command": "echo hi"}]}
        self.write(json.dumps({"hooks": {"Stop": [stop], "Notification": [foreign]}}))
        (self.claude_dir / "settings.local.json").write_text(json.dumps({"hooks": {"Stop": [stop]}}), encoding="utf-8")
        (project / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [stop], "Notification": [foreign]}}), encoding="utf-8")
        (project / ".claude" / "settings.local.json").write_text("{not json", encoding="utf-8")
        files = cs.settings_files(self.claude_dir, [project])
        self.assertEqual([p.name for p in files], ["settings.json", "settings.local.json", "settings.json", "settings.local.json"])
        dups = cs.scan_duplicates(self.claude_dir, [project], self.hook)
        by_event = {d["event"]: d for d in dups}
        self.assertEqual(set(by_event), {"Stop", "Notification"})
        self.assertEqual(by_event["Stop"]["count"], 3)
        self.assertTrue(by_event["Stop"]["ours"])
        self.assertEqual(by_event["Notification"]["count"], 2)
        self.assertFalse(by_event["Notification"]["ours"])
        self.assertEqual(cs.scan_duplicates(self.claude_dir, None, self.hook)[0]["count"], 2)

    def test_duplicate_within_one_file_counts(self):
        stop = cs.entry_for(self.hook, "Stop")
        self.write(json.dumps({"hooks": {"Stop": [stop, stop]}}))
        dups = cs.scan_duplicates(self.claude_dir, None, self.hook)
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0]["count"], 2)
        self.assertEqual(dups[0]["files"], [os.fspath(self.settings)] * 2)


class ShimFileTests(SettingsCase):
    def test_render_bakes_cli_path_and_keeps_marker(self):
        text = cs.render_shim(Path("/opt/x y/bin/herdr-synapse"))
        self.assertIn("HERDR_TEAM_CLI='/opt/x y/bin/herdr-synapse'", text)
        self.assertNotIn(cs.CLI_PLACEHOLDER, text)
        self.assertIn(cs.SHIM_MARKER + str(cs.SHIM_VERSION), text)
        self.assertTrue(text.startswith("#!/bin/sh\n"))

    def test_write_shim_mode_and_foreign_refusal(self):
        hooks_dir = self.claude_dir / "hooks"
        target = cs.write_shim(hooks_dir, Path("/opt/herdr-synapse"))
        self.assertEqual(target, hooks_dir / cs.HOOK_FILE_NAME)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o700)
        self.assertTrue(cs.shim_is_ours(target))
        # rewrite is fine for our own file
        cs.write_shim(hooks_dir, Path("/opt/other/herdr-synapse"))
        self.assertIn("/opt/other/herdr-synapse", target.read_text(encoding="utf-8"))
        # a foreign file with the same name is never overwritten or removed
        target.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        with self.assertRaises(HerdrTeamError) as ctx:
            cs.write_shim(hooks_dir, Path("/opt/herdr-synapse"))
        self.assertEqual(ctx.exception.code, "hook_file_foreign")
        self.assertFalse(cs.remove_shim(hooks_dir))
        self.assertTrue(target.exists())

    def test_remove_shim_removes_only_ours(self):
        hooks_dir = self.claude_dir / "hooks"
        self.assertFalse(cs.remove_shim(hooks_dir))
        cs.write_shim(hooks_dir, Path("/opt/herdr-synapse"))
        self.assertTrue(cs.remove_shim(hooks_dir))
        self.assertFalse((hooks_dir / cs.HOOK_FILE_NAME).exists())


if __name__ == "__main__":
    unittest.main()
