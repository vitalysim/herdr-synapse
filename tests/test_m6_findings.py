"""Regression tests for the two plugin gaps found in the M6 rig run (2026-09-05, team4 rig).

- SK-03: ``who --json`` said nothing about ``kinds.json``, so the multi-line paste probe
  recorded there (``kinds.json[kind].multiline``) was invisible; ``who`` now carries a
  ``kinds`` block per roster kind.
- SK-06: ``hooks check claude`` reported ``ok: false`` on duplicate hook commands but
  printed no warning line, unlike ``hooks install``.
"""

from __future__ import annotations

import os
import unittest

from herdr_team import claude_settings, store
from support import FakeApi, TempState
from test_cmd_roster import json_out, live_api, run_cli, write_live_daemon


class WhoKindsTests(unittest.TestCase):
    def test_who_json_exposes_kinds_json_multiline(self):
        with TempState() as ts:
            multiline = {"one_submission": True, "submissions": 1, "lines_sent": 2, "probed_at": "2026-09-05T14:11:20.000Z", "source": "rig SK-03"}
            store.write_json(ts.session.kinds_json, {"claude": {"trusted": True, "multiline": multiline}, "codex": {"probe": {"ok": True, "nonce": 1}}})
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "who"], ts.env, live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(sorted(payload["kinds"]), ["claude", "codex"], "one entry per agent kind on the roster, never human")
            self.assertEqual(payload["kinds"]["claude"], {"trusted": True, "verified": False, "probe_ok": False, "multiline": multiline})
            self.assertEqual(payload["kinds"]["codex"], {"trusted": False, "verified": False, "probe_ok": True, "multiline": None})

    def test_who_kinds_survive_missing_or_malformed_kinds_json(self):
        with TempState() as ts:
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "who"], ts.env, live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["kinds"], {"codex": {"trusted": False, "verified": False, "probe_ok": False, "multiline": None},
                                                "claude": {"trusted": False, "verified": False, "probe_ok": False, "multiline": None}})
            ts.session.kinds_json.write_text("[]")
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "who"], ts.env, live_api()))
            self.assertEqual(code, 0, err)
            self.assertFalse(payload["kinds"]["claude"]["trusted"])
            store.write_json(ts.session.kinds_json, {"claude": {"multiline": "yes"}})
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "who"], ts.env, live_api()))
            self.assertIsNone(payload["kinds"]["claude"]["multiline"], "a non-object multiline record is reported as absent")

    def test_who_kinds_cover_every_roster_kind_even_with_role_filter(self):
        with TempState() as ts:
            write_live_daemon(ts)
            store.write_json(ts.session.kinds_json, {"claude": {"multiline": {"one_submission": False}}})
            code, payload, err = json_out(run_cli(["--json", "--team", "alpha", "who", "--role", "reviewer"], ts.env, live_api()))
            self.assertEqual(code, 0, err)
            self.assertEqual([m["name"] for m in payload["members"]], ["alpha-reviewer"])
            self.assertIn("claude", payload["kinds"], "the role filter narrows members, not the kinds summary")
            self.assertFalse(payload["kinds"]["claude"]["multiline"]["one_submission"])


class HooksCheckDuplicateWarningTests(unittest.TestCase):
    def test_check_warns_when_duplicates_found(self):
        with TempState() as ts:
            claude_dir = ts.home / ".claude"
            hooks_dir = claude_dir / "hooks"
            hook_path = hooks_dir / claude_settings.HOOK_FILE_NAME
            claude_settings.write_shim(hooks_dir, ts.tmp / "herdr-synapse")
            claude_settings.install(claude_dir / "settings.json", hook_path)
            claude_settings.install(claude_dir / "settings.local.json", hook_path)
            args = ["--json", "hooks", "check", "claude", "--claude-dir", os.fspath(claude_dir), "--cli", os.fspath(ts.tmp / "herdr-synapse")]
            code, payload, err = json_out(run_cli(args, ts.env_with(PWD=os.fspath(ts.tmp)), FakeApi()))
            self.assertEqual(code, 0, err)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["duplicates"])
            self.assertIn("duplicate hook commands found; a hook registered twice runs twice", payload["warnings"])
            code, out, _ = run_cli(["hooks", "check", "claude", "--claude-dir", os.fspath(claude_dir), "--cli", os.fspath(ts.tmp / "herdr-synapse")], ts.env_with(PWD=os.fspath(ts.tmp)), FakeApi())
            self.assertIn("warning: duplicate hook commands found", out)

    def test_check_without_duplicates_has_no_duplicate_warning(self):
        with TempState() as ts:
            claude_dir = ts.home / ".claude"
            hooks_dir = claude_dir / "hooks"
            hook_path = hooks_dir / claude_settings.HOOK_FILE_NAME
            claude_settings.write_shim(hooks_dir, ts.tmp / "herdr-synapse")
            claude_settings.install(claude_dir / "settings.json", hook_path)
            args = ["--json", "hooks", "check", "claude", "--claude-dir", os.fspath(claude_dir), "--cli", os.fspath(ts.tmp / "herdr-synapse")]
            code, payload, err = json_out(run_cli(args, ts.env_with(PWD=os.fspath(ts.tmp)), FakeApi()))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["duplicates"], [])
            self.assertFalse(any("duplicate" in w for w in payload["warnings"]))


if __name__ == "__main__":
    unittest.main()
