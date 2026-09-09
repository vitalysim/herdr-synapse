"""The unified plugin update path."""

import os
import unittest
from unittest import mock

from herdr_team import paths
from herdr_team import cmd_update
from support import FakeApi, TempState, fake_plugin_info
from test_cmd_board import json_out, run_cli


def plugin_entry(kind="local", version="0.15.5", **source):
    entry = fake_plugin_info(root=os.fspath(paths.plugin_root()))
    entry["version"] = version
    entry["source"] = {"kind": kind, **source}
    return entry


def step_result(_executable, args, _env, timeout=cmd_update.SYNAPSE_STEP_TIMEOUT_S):
    if list(args[:2]) == ["install-cli", "--yes"]:
        return {"path": "/home/test/.local/bin/herdr-synapse", "created": False, "replaced": False}
    if list(args[:2]) == ["skill", "install"]:
        return {"version": 10, "ok": True}
    if list(args[:3]) == ["daemon", "start", "--replace"]:
        return {"daemon": {"pid": 42, "herdr_version": "0.9.0", "capabilities": {"atomic_idle_prompt": True}}}
    raise AssertionError(args)


class UpdateCommandTests(unittest.TestCase):
    def test_local_link_is_not_pulled_but_every_installed_surface_refreshes(self):
        with TempState() as ts:
            api = FakeApi(responses={"plugin.list": {"type": "plugin_list", "plugins": [plugin_entry()]}})
            api.set_cli(["plugin", "link", os.fspath(paths.plugin_root()), "--enabled"], 0, "linked\n", "")
            with mock.patch.object(cmd_update, "_run_synapse_step", side_effect=step_result) as steps:
                code, payload, err = json_out(run_cli(["--json", "update"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["checkout"]["action"], "relinked")
            self.assertEqual(api.runs, [["plugin", "link", os.fspath(paths.plugin_root()), "--enabled"]])
            self.assertEqual([call.args[1] for call in steps.call_args_list], [
                ["install-cli", "--yes"], ["skill", "install"], ["daemon", "start", "--replace"],
            ])

    def test_github_checkout_reinstalls_recorded_source_then_uses_new_root(self):
        with TempState() as ts:
            before = plugin_entry("github", "0.15.5", owner="vitalysim", repo="herdr-synapse", requested_ref="main")
            after = plugin_entry("github", "0.16.0", owner="vitalysim", repo="herdr-synapse", requested_ref="main")
            calls = [before, after]

            def listing(_params):
                return {"type": "plugin_list", "plugins": [calls.pop(0)]}

            api = FakeApi(responses={"plugin.list": listing})
            api.set_cli(["plugin", "install", "vitalysim/herdr-synapse", "--ref", "main", "--yes"], 0, "installed\n", "")
            with mock.patch.object(cmd_update, "_run_synapse_step", side_effect=step_result):
                code, payload, err = json_out(run_cli(["--json", "update"], ts.env, api))
            self.assertEqual(code, 0, err)
            self.assertEqual((payload["before"], payload["after"]), ("0.15.5", "0.16.0"))
            self.assertEqual(payload["checkout"], {"action": "reinstalled", "source": "vitalysim/herdr-synapse", "ref": "main"})
            self.assertEqual(api.runs, [["plugin", "install", "vitalysim/herdr-synapse", "--ref", "main", "--yes"]])

    def test_ref_on_local_link_refuses_before_any_mutation(self):
        with TempState() as ts:
            api = FakeApi(responses={"plugin.list": {"type": "plugin_list", "plugins": [plugin_entry()]}})
            api.set_cli(["plugin", "link", os.fspath(paths.plugin_root()), "--enabled"], 0, "linked\n", "")
            with mock.patch.object(cmd_update, "_run_synapse_step") as steps:
                code, payload, err = json_out(run_cli(["--json", "update", "--ref", "next"], ts.env, api))
            self.assertEqual(code, 1)
            self.assertIsNone(payload)
            self.assertEqual(err["code"], "plugin_source_local")
            steps.assert_not_called()

    def test_skill_attention_is_reported_after_notifier_refresh(self):
        with TempState() as ts:
            api = FakeApi(responses={"plugin.list": {"type": "plugin_list", "plugins": [plugin_entry()]}})
            api.set_cli(["plugin", "link", os.fspath(paths.plugin_root()), "--enabled"], 0, "linked\n", "")

            def needs_attention(executable, args, env, timeout=cmd_update.SYNAPSE_STEP_TIMEOUT_S):
                result = step_result(executable, args, env, timeout)
                if list(args[:2]) == ["skill", "install"]:
                    result["ok"] = False
                return result

            with mock.patch.object(cmd_update, "_run_synapse_step", side_effect=needs_attention) as steps:
                code, payload, err = json_out(run_cli(["--json", "update"], ts.env, api))
            self.assertEqual(code, 1)
            self.assertIsNone(payload)
            self.assertEqual(err["code"], "skill_update_refused")
            self.assertEqual(len(steps.call_args_list), 3)
            self.assertIn("update", err)


if __name__ == "__main__":
    unittest.main()
