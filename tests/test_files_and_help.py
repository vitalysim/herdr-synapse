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
        self.assertIn("project ", lines[-1])
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


class ProjectFinderTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.root = self.ts.tmp / "project"
        for d in ("src", "docs", ".git", "node_modules", "src/deep"):
            (self.root / d).mkdir(parents=True)
        (self.root / "src" / "main.rs").write_text("fn main() {}\n")
        (self.root / "src" / "deep" / "matrix.rs").write_text("x\n")
        (self.root / "docs" / "manual.md").write_text("m\n")
        (self.root / "README.md").write_text("hi\n")
        (self.root / ".env").write_text("SECRET=1\n")
        (self.root / "node_modules" / "pkg.js").write_text("x\n")
        self.other = self.ts.tmp / "other"
        (self.other / "lib").mkdir(parents=True)
        (self.other / "lib" / "matrix.py").write_text("y\n")
        tm._FILE_INDEX_CACHE.clear()

    def test_index_skips_dot_and_build_dirs_and_is_cached(self):
        entries = tm.file_index(os.fspath(self.root))
        rels = [rel for rel, _ in entries]
        self.assertEqual(rels, ["docs/", "src/", "README.md", "docs/manual.md", "src/deep/", "src/main.rs", "src/deep/matrix.rs"])
        self.assertIs(tm.file_index(os.fspath(self.root)), entries)  # cached within the TTL
        self.assertEqual(tm.file_index(os.fspath(self.ts.tmp / "missing")), [])

    def test_match_ranks(self):
        self.assertEqual(tm._match_rank("ma", "src/main.rs"), 0)
        self.assertEqual(tm._match_rank("sr", "src/main.rs"), 1)
        self.assertEqual(tm._match_rank("ain", "src/main.rs"), 2)
        self.assertEqual(tm._match_rank("mrs", "src/main.rs"), 3)  # in-order letters of the name
        self.assertIsNone(tm._match_rank("smr", "src/main.rs"))  # letters spread over the path do not count
        self.assertIsNone(tm._match_rank("readme", "tests/fixtures/detection/claude_model_picker.txt"))
        self.assertIsNone(tm._match_rank("mr", "src/main.rs"))  # two letters: prefix or substring only
        self.assertIsNone(tm._match_rank("zzz", "src/main.rs"))

    def test_bare_word_searches_every_root_and_paths_complete_under_the_first(self):
        roots = [os.fspath(self.root), os.fspath(self.other)]
        rows = tm.file_candidates("ma", roots, base_dir=os.fspath(self.ts.tmp))
        self.assertEqual([r["insert"] for r in rows], ["src/main.rs", "docs/manual.md", "src/deep/matrix.rs"])  # the team's project answers alone
        self.assertNotIn(" in ", rows[0]["label"])
        rows = tm.file_candidates("lib", roots, base_dir=os.fspath(self.ts.tmp))  # nothing in the project: the other root answers
        self.assertEqual([r["insert"] for r in rows], [os.fspath(self.other / "lib") + "/", os.fspath(self.other / "lib" / "matrix.py")])
        self.assertIn("in other", rows[0]["label"])
        rows = tm.file_candidates("", roots, base_dir=os.fspath(self.ts.tmp))
        self.assertEqual([r["insert"] for r in rows], ["docs/", "src/", "README.md"])  # node_modules hidden from the bare listing
        self.assertEqual([r["insert"] for r in tm.file_candidates("node", [os.fspath(self.root)])], [])  # and not indexed either
        self.assertEqual([r["insert"] for r in tm.path_candidates("node", os.fspath(self.root))], ["node_modules/"])  # but reachable by name
        rows = tm.file_candidates("src/", roots, base_dir=os.fspath(self.ts.tmp))
        self.assertEqual([r["insert"] for r in rows], ["src/deep/", "src/main.rs"])
        rows = tm.file_candidates("deep/", roots, base_dir=os.fspath(self.ts.tmp))  # a partial path from deeper down
        self.assertEqual([r["insert"] for r in rows], ["src/deep/", "src/deep/matrix.rs"])
        rows = tm.file_candidates("deep/ma", roots, base_dir=os.fspath(self.ts.tmp))
        self.assertEqual([r["insert"] for r in rows], ["src/deep/matrix.rs"])
        rows = tm.file_candidates("mrs", [os.fspath(self.root)])
        self.assertEqual([r["insert"] for r in rows], ["src/main.rs", "src/deep/matrix.rs"])  # in-order letters of the name, shorter first
        self.assertEqual(tm.file_candidates("zzz", roots), [])
        # no roots: the console's own directory
        rows = tm.file_candidates("", [], base_dir=os.fspath(self.other))
        self.assertEqual([r["insert"] for r in rows], ["lib/"])

    def test_member_roots_come_from_the_roster(self):
        members = [
            {"name": "a", "kind": "claude", "cwd": os.fspath(self.other)},
            {"name": "b", "kind": "codex", "cwd": os.fspath(self.root)},
            {"name": "c", "kind": "opencode", "cwd": os.fspath(self.root)},
            {"name": "gone", "kind": "codex", "cwd": os.fspath(self.other), "status": "left"},
            {"name": "home", "kind": "codex", "cwd": os.fspath(self.ts.tmp)},
            {"name": "human", "kind": "human"},
            {"name": "nowhere", "kind": "codex", "cwd": os.fspath(self.ts.tmp / "missing")},
        ]
        self.assertEqual(tm.member_file_roots(members, home=os.fspath(self.ts.tmp)), [os.fspath(self.root), os.fspath(self.other)])

    def test_console_and_compose_models_take_the_roots_from_team_json(self):
        doc = store.read_json(self.ts.team.team_json)
        for member in doc["members"]:
            if member.get("kind") != "human":
                member["cwd"] = os.fspath(self.root)
        store.write_json(self.ts.team.team_json, doc)
        state = console.ConsoleState(self.ts.layout, "alpha", self.ts.env)
        model = console.build_model(self.ts.layout, "alpha", state, env=self.ts.env)
        self.assertEqual(model.file_roots, [os.fspath(self.root)])
        for ch in "@@ma":
            tm.apply_key(model, ch)
        self.assertEqual([r["insert"] for r in tm.mention_menu(model)][:1], ["src/main.rs"])
        self.assertTrue(any("project {}".format(self.root) in line for line in tm.mention_lines(model, 160)))
        console.refresh(model, self.ts.layout, state)
        self.assertEqual(model.file_roots, [os.fspath(self.root)])
        from herdr_team import compose
        popup = compose.build_model(self.ts.layout, "alpha", {})
        self.assertEqual(popup.file_roots, [os.fspath(self.root)])


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

    def test_relative_file_resolves_under_a_member_directory(self):
        doc = store.read_json(self.ts.team.team_json)
        doc["members"][0]["cwd"] = os.fspath(self.outside)
        store.write_json(self.ts.team.team_json, doc)
        self.assertFalse((Path.cwd() / "notes.md").exists())
        code, payload, err = self.post("see notes", "--file", "notes.md")
        self.assertEqual(code, 0, err)
        self.assertEqual((payload["refs"], payload["attached"]), ([os.path.realpath(self.outside / "notes.md")], []))  # under a member's cwd: referenced, not copied

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
