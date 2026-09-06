"""``herdr-team export``: the board saved as a file that outlives the session."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from herdr_team import cmd_board, render, store
from support import TempState
from test_cmd_roster import json_out, run_cli


class ExportRendererTests(unittest.TestCase):
    def records(self, count: int = 3):
        return [{
            "v": 1, "seq": n, "ts": "2026-09-06T12:00:{:02d}.000Z".format(n),
            "from": "alpha-worker", "from_kind": "claude", "to": ["all"], "to_role": None,
            "kind": "note", "text": "post {}".format(n), "refs": [], "reply_to": None,
        } for n in range(1, count + 1)]

    def test_the_document_stands_on_its_own(self):
        out = render.render_export_markdown(
            self.records(), "alpha",
            charter={"seq": 2, "text": "Ship the report."},
            members=[{"name": "alpha-worker", "role": "worker", "kind": "claude", "status": "active"}],
            exported_at="2026-09-06T12:30:00Z", exported_by="human",
        )
        self.assertIn("# Team board: alpha", out)
        self.assertIn("Ship the report.", out)          # the charter the posts refer to
        self.assertIn("| alpha-worker |", out)          # the roster
        self.assertIn("Exported 2026-09-06T12:30:00Z by human.", out)
        self.assertIn("3 records, posts 1-3.", out)
        for n in (1, 2, 3):
            self.assertIn("post {}".format(n), out)

    def test_an_empty_board_is_still_a_document(self):
        out = render.render_export_markdown([], "alpha")
        self.assertIn("# Team board: alpha", out)
        self.assertIn("_No posts._", out)

    def test_a_posts_metadata_survives(self):
        record = self.records(1)[0]
        record.update({"to": ["alpha-reviewer"], "kind": "request", "urgent": True,
                       "reply_to": 7, "refs": ["payloads/3-diff.md"]})
        out = render.render_export_markdown([record], "alpha")
        self.assertIn("alpha-reviewer", out)
        self.assertIn("request", out)
        self.assertIn("urgent", out)
        self.assertIn("reply to #7", out)
        self.assertIn("payloads/3-diff.md", out)

    def test_a_giant_post_is_truncated_not_dropped(self):
        record = self.records(1)[0]
        record["text"] = "x" * (render.EXPORT_MAX_TEXT_CHARS + 500)
        out = render.render_export_markdown([record], "alpha")
        self.assertIn("truncated at", out)
        self.assertLess(len(out), render.EXPORT_MAX_TEXT_CHARS + 2000)

    def test_the_filename_is_safe_and_dated(self):
        name = cmd_board.export_filename("red/../dev", "md", now="2026-09-06T20:07:44.000Z")
        self.assertNotIn("/", name)
        self.assertTrue(name.startswith("board-red-.-dev-") or name.startswith("board-red-"), name)
        self.assertTrue(name.endswith(".md"))
        self.assertTrue(cmd_board.export_filename("t", "jsonl").endswith(".jsonl"))
        self.assertTrue(cmd_board.export_filename("t", "text").endswith(".txt"))


class ExportCommandTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.cleanup)
        # macOS resolves /var to /private/var, and the CLI canonicalizes its target.
        self.out = Path(tempfile.mkdtemp(prefix="ht-exp-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.out, ignore_errors=True))
        for index in range(4):
            store.BoardStore(self.state.team).append({
                "from": "alpha-worker", "from_kind": "claude", "from_pane": "w2:p2", "from_terminal": "t",
                "from_gen": 1, "to": ["all"], "to_role": None, "kind": "note",
                "text": "post number {}".format(index), "refs": [], "reply_to": None,
            })

    def cli(self, *argv):
        return run_cli(["--json", "export"] + list(argv), self.state.env)

    def test_it_writes_a_markdown_file_by_default(self):
        target = self.out / "board.md"
        code, payload, _ = json_out(self.cli(os.fspath(target)))
        self.assertEqual(code, 0)
        self.assertEqual(payload["format"], "md")
        self.assertEqual(payload["records"], 4)
        self.assertEqual(payload["path"], os.fspath(target))
        text = target.read_text(encoding="utf-8")
        self.assertIn("# Team board:", text)
        self.assertIn("post number 3", text)

    def test_a_directory_target_gets_a_generated_name(self):
        code, payload, _ = json_out(self.cli(os.fspath(self.out)))
        self.assertEqual(code, 0)
        written = Path(payload["path"])
        self.assertEqual(written.parent, self.out)
        self.assertTrue(written.name.startswith("board-"))
        self.assertTrue(written.is_file())

    def test_jsonl_round_trips_into_records(self):
        target = self.out / "board.jsonl"
        code, _payload, _ = json_out(self.cli(os.fspath(target), "--format", "jsonl"))
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([r["seq"] for r in rows], [1, 2, 3, 4])
        self.assertEqual(rows[0]["kind"], "note")

    def test_json_carries_the_charter_and_roster(self):
        target = self.out / "board.json"
        json_out(self.cli(os.fspath(target), "--format", "json"))
        doc = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(sorted(doc), ["charter", "exported_at", "exported_by", "members", "records", "schema", "team"])
        self.assertEqual(len(doc["records"]), 4)
        self.assertTrue(doc["members"])

    def test_an_existing_file_is_refused_without_force(self):
        target = self.out / "board.md"
        target.write_text("mine\n", encoding="utf-8")
        code, _payload, err = json_out(self.cli(os.fspath(target)))
        self.assertNotEqual(code, 0)
        self.assertIn("path_exists", str(err))
        self.assertEqual(target.read_text(encoding="utf-8"), "mine\n")
        code, _payload, _ = json_out(self.cli(os.fspath(target), "--force"))
        self.assertEqual(code, 0)
        self.assertIn("# Team board:", target.read_text(encoding="utf-8"))

    def test_a_symlinked_target_is_refused(self):
        real = self.out / "real.md"
        link = self.out / "link.md"
        real.write_text("x", encoding="utf-8")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        code, _payload, err = json_out(self.cli(os.fspath(link), "--force"))
        self.assertNotEqual(code, 0)
        self.assertIn("symlink", str(err))

    def test_stdout_writes_no_file(self):
        """``args.stdout`` is the CLI's output stream, so the flag needs its own dest."""
        code, out, _ = run_cli(["export", "--stdout"], self.state.env)
        self.assertEqual(code, 0)
        self.assertIn("# Team board:", out)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_last_and_since_narrow_the_selection(self):
        code, payload, _ = json_out(self.cli(os.fspath(self.out / "a.md"), "--last", "2"))
        self.assertEqual(payload["records"], 2)
        code, payload, _ = json_out(self.cli(os.fspath(self.out / "b.md"), "--since", "3"))
        self.assertEqual(payload["records"], 1)

    def test_the_file_is_private(self):
        target = self.out / "board.md"
        self.cli(os.fspath(target))
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()


class ConsoleExportTests(unittest.TestCase):
    """`/export` from the console, where you actually read the board."""

    def model(self):
        from herdr_team import tui_model

        return tui_model.build_console_model("alpha", {}, [])

    def parse(self, line: str):
        from herdr_team import tui_model

        model = self.model()
        for ch in line:
            tui_model.apply_key(model, ch)
        return tui_model.apply_key(model, "ENTER"), model

    def test_bare_export_uses_the_defaults(self):
        intent, _model = self.parse("/export")
        self.assertEqual(intent.kind, "export")
        self.assertEqual(intent.args["format"], "md")
        self.assertIsNone(intent.args["path"])

    def test_a_path_is_carried_through(self):
        intent, _model = self.parse("/export ~/board.md")
        self.assertEqual(intent.args["path"], "~/board.md")

    def test_a_format_is_carried_through(self):
        intent, _model = self.parse("/export --format jsonl")
        self.assertEqual(intent.args["format"], "jsonl")
        self.assertIsNone(intent.args["path"])

    def test_a_path_and_a_format_together(self):
        intent, _model = self.parse("/export /tmp/b.json --format json")
        self.assertEqual((intent.args["path"], intent.args["format"]), ("/tmp/b.json", "json"))

    def test_a_bad_format_is_refused(self):
        intent, model = self.parse("/export --format docx")
        self.assertEqual(intent.kind, "error")
        self.assertIn("md, json", intent.args["message"] + (model.status or ""))

    def test_the_help_names_it(self):
        from herdr_team import tui_model

        self.assertIn("/export", "\n".join(tui_model.help_lines()))


class ConsoleExportDestinationTests(unittest.TestCase):
    """The console pane's cwd is the plugin directory, so a bare /export needs a home."""

    def setUp(self):
        from herdr_team import roster

        self.state = TempState()
        self.addCleanup(self.state.cleanup)
        self.project = Path(tempfile.mkdtemp(prefix="ht-proj-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.project, ignore_errors=True))
        self.roster = roster

    def set_project(self):
        def apply(doc):
            doc.config["project_dir"] = os.fspath(self.project)

        self.roster.update_team(self.state.team, apply)

    def test_it_uses_the_team_folder_when_there_is_one(self):
        from herdr_team import console

        self.set_project()
        target = console.default_export_dir(self.state.layout, self.state.team_name)
        self.assertEqual(target, self.project / ".herdr-team" / self.state.team_name / "exports")
        self.assertTrue(target.is_dir())

    def test_it_never_writes_into_the_plugin_directory(self):
        from herdr_team import console, paths

        self.set_project()
        target = console.default_export_dir(self.state.layout, self.state.team_name)
        self.assertNotIn(os.fspath(paths.plugin_root()), os.fspath(target))

    def test_it_falls_back_to_home_without_a_project(self):
        from herdr_team import console

        target = console.default_export_dir(self.state.layout, self.state.team_name)
        self.assertEqual(target, Path(os.path.expanduser("~")))

    def test_exports_are_git_ignored(self):
        from herdr_team import workdir

        body = workdir.gitignore_body(["alpha", "beta"])
        self.assertIn("alpha/exports/", body)
        self.assertIn("beta/exports/", body)
        self.assertIn("alpha/artifacts/", body)
