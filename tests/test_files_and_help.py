"""``@@path`` attaches a file to a console post (``post --file``); ``?`` on an empty line opens the help box."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from herdr_team import cmd_board, console, store
from herdr_team import tui_model as tm
from herdr_team.tui_model import Intent
from support import FakeApi, TempState
from test_cmd_board import json_out, run_cli, write_live_daemon
from test_mention import MEMBERS, console as console_model, type_keys
from test_tui_model import drive, model_with, record


class FileTokenTests(unittest.TestCase):
    def test_tokens_anywhere_in_the_line_become_files(self):
        spec, err = tm.parse_post_directives("look at @@src/main.rs and @@~/notes.md please")
        self.assertIsNone(err)
        self.assertEqual((spec.text, spec.files, spec.to), ("look at and please", ["src/main.rs", "~/notes.md"], []))
        spec, _ = tm.parse_post_directives("@alpha-worker @@diff.patch review this")
        self.assertEqual((spec.to, spec.files, spec.text), (["alpha-worker"], ["diff.patch"], "review this"))
        spec, _ = tm.parse_post_directives("mail me at foo@@bar.com")
        self.assertEqual((spec.files, spec.text), ([], "mail me at foo@@bar.com"))  # mid-word is text

    def test_a_file_alone_gets_a_default_text(self):
        spec, err = tm.parse_post_directives("@@README.md")
        self.assertIsNone(err)
        self.assertEqual((spec.text, spec.files), ("file: README.md", ["README.md"]))
        spec, _ = tm.parse_post_directives("@@docs/ @@a.txt")
        self.assertEqual(spec.text, "file: docs, a.txt")
        self.assertIn("files", spec.to_args())

    def test_intent_and_argv_carry_the_files(self):
        intent = tm.parse_input_line("@alpha-worker @@diff.patch look", "alpha")
        self.assertEqual((intent.kind, intent.args["files"], intent.args["text"]), ("post", ["diff.patch"], "look"))
        argv = console.post_args(Intent("post", {"text": "look", "to": ["alpha-worker"], "files": ["diff.patch", "~/x.md"]}), "alpha")
        self.assertEqual(argv, ["--team", "alpha", "post", "look", "--to", "alpha-worker", "--file", "diff.patch", "--file", "~/x.md"])


class FileMenuTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.base = self.ts.tmp / "work"
        (self.base / "src").mkdir(parents=True)
        (self.base / "docs").mkdir()
        (self.base / ".git").mkdir()
        (self.base / "src" / "main.rs").write_text("fn main() {}\n")
        (self.base / "src" / "lib.rs").write_text("x" * 2048)
        (self.base / "README.md").write_text("hi\n")
        (self.base / ".env").write_text("SECRET=1\n")

    def test_context_and_candidates(self):
        self.assertEqual(tm.mention_context("see @@sr", 8), (4, 8, "sr", "@@"))
        self.assertEqual(tm.mention_context("@@", 2), (0, 2, "", "@@"))
        self.assertIsNone(tm.mention_context("foo@@bar", 8))
        rows = tm.path_candidates("", os.fspath(self.base))
        self.assertEqual([r["insert"] for r in rows], ["docs/", "src/", "README.md"])  # dirs first, dot-entries hidden
        rows = tm.path_candidates("src/", os.fspath(self.base))
        self.assertEqual([r["insert"] for r in rows], ["src/lib.rs", "src/main.rs"])
        self.assertIn("2.0KB", rows[0]["label"])
        rows = tm.path_candidates("src/MA", os.fspath(self.base))
        self.assertEqual([r["insert"] for r in rows], ["src/main.rs"])
        rows = tm.path_candidates(".", os.fspath(self.base))
        self.assertEqual([r["insert"] for r in rows], [".git/", ".env"])
        self.assertEqual(tm.path_candidates("nowhere/", os.fspath(self.base)), [])
        rows = tm.path_candidates(os.fspath(self.base) + "/RE")
        self.assertEqual([r["insert"] for r in rows], [os.fspath(self.base) + "/README.md"])

    def test_menu_completes_directories_without_closing_and_files_with_a_space(self):
        model = console_model()
        model.file_base = os.fspath(self.base)
        type_keys(model, "@@")
        self.assertEqual([r["insert"] for r in tm.mention_menu(model)], ["docs/", "src/", "README.md"])
        lines = tm.mention_lines(model, 120)
        self.assertTrue(lines[0].startswith(tm.MENTION_MARKER + " @@docs/"), lines[0])
        self.assertIn("files under", lines[-1])
        tm.apply_key(model, "DOWN")
        tm.apply_key(model, "TAB")
        self.assertEqual(model.input, "@@src/")
        self.assertEqual([r["insert"] for r in tm.mention_menu(model)], ["src/lib.rs", "src/main.rs"])
        tm.apply_key(model, "DOWN")
        tm.apply_key(model, "ENTER")
        self.assertEqual(model.input, "@@src/main.rs ")
        self.assertEqual(tm.mention_menu(model), [])
        styled_model = console_model()
        styled_model.file_base = os.fspath(self.base)
        type_keys(styled_model, "@@")
        styles = [style for _, style in tm.mention_rows_styled(styled_model, 120)]
        self.assertEqual(styles[0], tm.STYLE_MENU_SELECTED)
        self.assertTrue(all(style in (tm.STYLE_MENU, tm.STYLE_DIM) for style in styles[1:]), styles)
        type_keys(model, "please read")
        intent = tm.apply_key(model, "ENTER")
        self.assertEqual((intent.kind, intent.args["files"], intent.args["text"]), ("post", ["src/main.rs"], "please read"))


class PostFileTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        write_live_daemon(self.ts)
        self.outside = self.ts.tmp / "elsewhere"
        self.outside.mkdir()
        (self.outside / "notes.md").write_text("remember\n")
        (self.ts.team.root / "plan.md").write_text("plan\n")
        (self.ts.tmp / ".secrets").mkdir()
        (self.ts.tmp / ".secrets" / "key.txt").write_text("k\n")

    def post(self, *args):
        return json_out(run_cli(["--json", "--team", "alpha", "post"] + list(args), self.ts.env, FakeApi()))

    def test_file_under_the_team_dir_is_a_ref_and_elsewhere_is_attached(self):
        code, payload, err = self.post("see plan", "--file", os.fspath(self.ts.team.root / "plan.md"))
        self.assertEqual(code, 0, err)
        self.assertEqual((payload["refs"], payload["attached"]), (["plan.md"], []))
        code, payload, err = self.post("see notes", "--file", os.fspath(self.outside / "notes.md"))
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["refs"], [])
        self.assertEqual(payload["attached"], ["payloads/{}-notes.md".format(payload["seq"])])
        self.assertTrue((self.ts.team.root / payload["attached"][0]).is_file())
        record = store.BoardStore(self.ts.team).get(payload["seq"])
        self.assertEqual(record["refs"], payload["attached"])

    def test_missing_and_dot_directory_files_are_refused(self):
        code, _, err = self.post("x", "--file", os.fspath(self.outside / "nope.md"))
        self.assertEqual((code, err["code"]), (1, "ref_invalid"))
        code, _, err = self.post("x", "--file", os.fspath(self.ts.tmp / ".secrets" / "key.txt"))
        self.assertEqual((code, err["code"]), (1, "ref_invalid"))
        self.assertIn("dot-directory", err["message"])
        self.assertEqual(store.BoardStore(self.ts.team).read(), [])

    def test_console_reports_the_attachment(self):
        calls = []

        def fake_run_cli(args, env, timeout=0.0):
            calls.append(list(args))
            return 0, {"seq": 7, "to": ["all"], "notifier": "alive", "attached": ["payloads/7-notes.md"], "refs": ["payloads/7-notes.md"]}, None

        original = console.run_cli
        console.run_cli = fake_run_cli
        try:
            state = console.ConsoleState(self.ts.layout, "alpha", self.ts.env)
            model = console.build_model(self.ts.layout, "alpha", state, env=self.ts.env)
            console.execute_intent(Intent("post", {"text": "see", "to": ["all"], "files": ["~/notes.md"]}), model, state, FakeApi())
        finally:
            console.run_cli = original
        self.assertIn("--file", calls[0])
        self.assertIn("attached notes.md", model.status)


class HelpTests(unittest.TestCase):
    def test_question_mark_on_an_empty_line_opens_the_help_box(self):
        model = model_with([record(1)], width=120, height=30)
        intent = tm.apply_key(model, "?")
        self.assertEqual(intent.kind, "help")
        self.assertIsNotNone(model.peek)
        joined = "\n".join(model.peek)
        for needle in ("/peek", "!name text", "@@path", "/charter set", "/quit", "help (Esc closes)"):
            self.assertIn(needle, joined, needle)
        self.assertEqual(model.input, "")
        screen = tm.render_console(model, 120, 30)
        self.assertTrue(any("help (Esc closes)" in line for line in screen))
        self.assertIsNone(tm.apply_key(model, "ESC"))
        self.assertIsNone(model.peek)

    def test_question_mark_inside_text_is_a_character(self):
        model = model_with([record(1)])
        drive(model, "why")
        self.assertIsNone(tm.apply_key(model, "?"))
        self.assertEqual(model.input, "why?")
        self.assertIsNone(model.peek)

    def test_slash_help_opens_the_box_and_the_footer_advertises_it(self):
        model = model_with([record(1)])
        drive(model, "/help")
        intent = tm.apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "help")
        self.assertIsNotNone(model.peek)
        self.assertIn("/peek", model.status)
        self.assertIn("? help", tm.filter_line(model))
        self.assertLessEqual(len(tm.help_lines()), 14)
        for line in tm.help_lines():
            self.assertNotIn(">", line)  # prompt markers are neutralized inside boxes; keep the text free of them


if __name__ == "__main__":
    unittest.main()
