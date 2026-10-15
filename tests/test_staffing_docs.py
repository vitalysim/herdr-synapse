"""Executable documentation contracts; all native/API activity is simulated."""
import re
import shlex
import unittest
from unittest import mock

from herdr_team import cli, model_inventory, roster, staffing, store
from support import PLUGIN_ROOT, TempState
from test_autonomous_staffing import inventory
from test_cmd_roster import env_no_daemon, json_out, run_cli, write_live_daemon
from test_templates import spawning_api


def examples(relative):
    text = (PLUGIN_ROOT / relative).read_text(encoding="utf-8")
    for block in re.findall(r"```(?:bash|sh)\n(.*?)```", text, re.S):
        for line in block.replace("\\\n", " ").splitlines():
            argv = shlex.split(line, comments=True)
            if argv and argv[0] == "herdr-synapse":
                yield argv[1:]


class StaffingDocsTests(unittest.TestCase):
    def test_readme_and_walkthrough_create_only_the_leader_with_a_recruiting_mission(self):
        for relative in ("README.md", "docs/autonomous-staffing.md"):
            commands = [argv for argv in examples(relative) if argv[:2] == ["create", "task-team"]]
            self.assertEqual(len(commands), 1, relative)
            with self.subTest(document=relative), TempState(write_team=False) as ts:
                argv = [str(ts.home) if value == "$project_dir" else value for value in commands[0]]
                write_live_daemon(ts)
                info = store.read_json(ts.session.daemon_json)
                info["capabilities"] = {staffing.CAPABILITY: True}
                store.write_json(ts.session.daemon_json, info)
                with mock.patch.object(model_inventory, "discover", return_value=inventory()):
                    code, result, error = json_out(run_cli(["--json"] + argv, env_no_daemon(ts), spawning_api(["codex"])))
                self.assertEqual(code, 0, error)
                team = roster.load_team(ts.session.team("task-team"))
                members = [member for member in team.members if not member.is_human]
                self.assertEqual([member.name for member in members], [team.manager().name])
                self.assertIn("staff add as needed", team.manager().brief)
                self.assertIn("Do not wait for me to enumerate workers", team.manager().brief)
                self.assertEqual(team.config["staffing"]["max_workers"], 4)
                self.assertEqual(set(team.config["staffing"]["approved"]), {"codex:openai"})
                self.assertEqual(team.config["permissions"], "native")
                self.assertEqual(members[0].permissions, None)
                self.assertTrue(result["briefing_jobs"])

    def test_every_walkthrough_command_parses_without_execution(self):
        parser = cli.build_parser()
        commands = list(examples("docs/autonomous-staffing.md"))
        self.assertGreaterEqual(len(commands), 15)
        for argv in commands:
            with self.subTest(command=argv):
                parser.parse_args(argv)

    def test_manager_guide_teaches_demand_driven_staffing_and_preserves_boundaries(self):
        text = (PLUGIN_ROOT / "skill-guides/manager.md").read_text(encoding="utf-8")
        for phrase in ("build the needed team yourself", "research first", "staff add",
                       "Do not wait for the human to enumerate workers", "approved pool",
                       "not an operator grant", "Ambiguous starts keep their slot",
                       "quota failures", "No recursive teams"):
            self.assertIn(phrase, text)
        self.assertLessEqual(len(text.splitlines()), 120)


if __name__ == "__main__":
    unittest.main()
