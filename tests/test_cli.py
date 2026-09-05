import argparse
import io
import json
import unittest

from herdr_team import SKILL_VERSION, VERSION, cli
from herdr_team.errors import HerdrTeamError
from support import TempState


def run_cli(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, env=env if env is not None else {}, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class RegistryTests(unittest.TestCase):
    def test_registry_loads_every_module(self):
        commands = cli.load_commands()
        self.assertIsInstance(commands, list)
        for command in commands:
            self.assertIsInstance(command, cli.Command)

    def test_duplicate_names_rejected(self):
        def noop_args(parser):
            pass

        def noop_run(args):
            return 0

        class Mod:
            COMMANDS = [cli.Command("dup", "a", noop_args, noop_run), cli.Command("dup", "b", noop_args, noop_run)]

        import sys

        sys.modules["_ht_dup_mod"] = Mod
        try:
            with self.assertRaises(RuntimeError):
                cli.load_commands(["_ht_dup_mod"])
        finally:
            del sys.modules["_ht_dup_mod"]

    def test_custom_command_dispatch_and_globals(self):
        seen = {}

        def add_arguments(parser):
            parser.add_argument("target")

        def run(args):
            seen["target"] = args.target
            seen["json"] = args.json
            seen["team"] = args.team
            seen["socket"] = args.socket
            seen["session"] = args.session
            seen["env"] = args.env
            return cli.emit(args, {"ok": True, "target": args.target}, "target " + args.target)

        parser = cli.build_parser([cli.Command("probe", "probe help", add_arguments, run, aliases=("pr",))])
        args = parser.parse_args(["--team", "alpha", "pr", "--json", "--socket", "/x.sock", "w1:p1"])
        args.env = {"E": "1"}
        args.stdout = io.StringIO()
        self.assertEqual(args._command.run(args), 0)
        self.assertEqual(seen, {"target": "w1:p1", "json": True, "team": "alpha", "socket": "/x.sock", "session": None, "env": {"E": "1"}})
        self.assertEqual(json.loads(args.stdout.getvalue()), {"ok": True, "target": "w1:p1"})

    def test_emit_human_mode(self):
        args = argparse.Namespace(json=False, stdout=io.StringIO())
        cli.emit(args, {"a": 1}, "hello")
        self.assertEqual(args.stdout.getvalue(), "hello\n")
        args = argparse.Namespace(json=False, stdout=io.StringIO())
        cli.emit(args, {"a": 1}, lambda: "lazy")
        self.assertEqual(args.stdout.getvalue(), "lazy\n")


class MainTests(unittest.TestCase):
    def test_version_human(self):
        code, out, err = run_cli(["--version"])
        self.assertEqual((code, out, err), (0, "herdr-team {}\n".format(VERSION), ""))

    def test_version_json(self):
        code, out, err = run_cli(["--version", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"version": VERSION, "skill_version": SKILL_VERSION, "plugin_id": "herdr-team"})

    def test_skill_prints_skill_file(self):
        code, out, err = run_cli(["--skill"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("---\nname: herdr-team\n"), out[:60])
        code, out, _ = run_cli(["--skill", "--json"])
        self.assertEqual(json.loads(out)["skill_version"], SKILL_VERSION)

    def test_unknown_command_exit_2(self):
        code, out, err = run_cli(["definitely-not-a-command"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        first, rest = err.split("\n", 1)
        self.assertEqual(json.loads(first)["code"], "usage")
        self.assertIn("usage: herdr-team", rest)

    def test_unknown_command_json_error_shape(self):
        code, out, err = run_cli(["definitely-not-a-command", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        lines = err.strip().splitlines()
        self.assertEqual(len(lines), 1)
        obj = json.loads(lines[0])
        self.assertEqual(obj["code"], "usage")
        self.assertIn("message", obj)

    def test_no_command_exit_2(self):
        code, out, err = run_cli(["--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["code"], "usage")

    def test_help_exits_0(self):
        code, out, err = run_cli(["--help"])
        self.assertEqual(code, 0)

    def test_command_error_becomes_json_on_stderr(self):
        def run(args):
            raise HerdrTeamError("board_locked", "post NOT written", details={"lock": "/x/team.lock"})

        original = cli.load_commands
        cli.load_commands = lambda modules=cli.COMMAND_MODULES: [cli.Command("boom", "x", lambda p: None, run)]
        try:
            code, out, err = run_cli(["boom", "--json"])
        finally:
            cli.load_commands = original
        self.assertEqual(code, 5)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err), {"code": "board_locked", "message": "post NOT written", "lock": "/x/team.lock"})

    def test_command_error_human_mode_still_json_on_stderr(self):
        def run(args):
            raise HerdrTeamError("not_a_member", "not a member")

        original = cli.load_commands
        cli.load_commands = lambda modules=cli.COMMAND_MODULES: [cli.Command("boom", "x", lambda p: None, run)]
        try:
            code, out, err = run_cli(["boom"])
        finally:
            cli.load_commands = original
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(err)["code"], "not_a_member")


class HelperTests(unittest.TestCase):
    def test_layout_for_and_api_for(self):
        with TempState() as ts:
            args = argparse.Namespace(env=ts.env, team=None, session=None, socket=None)
            layout = cli.layout_for(args)
            self.assertEqual(layout.session.root, ts.session.root)
            client = cli.api_for(args, layout)
            self.assertEqual(client.socket_path, layout.socket)
            args.api = "preset"
            self.assertEqual(cli.api_for(args), "preset")


if __name__ == "__main__":
    unittest.main()
