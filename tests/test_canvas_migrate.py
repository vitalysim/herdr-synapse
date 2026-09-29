"""Boards drawn before canvas v2 (canvas v2 phase 6, 2.6): the golden 0.21 boards replay with nothing lost, draw on v2,
report what v2 draws differently, and the operator's one fix applies in one batch that undo takes back.

The boards are ``tests/fixtures/migration/v021/<name>/``, written by the 0.21.2 code itself (``tools/make_v021_boards.py``).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest import mock

from support import PLUGIN_ROOT, whiteboard_on
from test_canvas import OPERATOR, REVIEWER, WORKER, CanvasRig, fake_resvg
from test_canvas_display_delta import RouteCase

from herdr_team import canvas as C
from herdr_team import canvas_display as D
from herdr_team import canvas_migrate as M
from herdr_team import canvas_render as R
from herdr_team.errors import HerdrTeamError

FIXTURES = PLUGIN_ROOT / "tests" / "fixtures" / "migration" / "v021"
BOARDS = ("agent-board", "arrow-labels", "flowchart", "font-sizes", "frame-children", "house", "i18n", "legacy-mix", "sketch", "text-notes")
#: What each board reports under today's metrics (2.3). 0.21 drew every mark by hand (``rough: 1``, the hand font), so
#: nearly every mark is a sketch mark; a comment never is.
EXPECTED = {
    "agent-board": {"pre_022": 15, "overflow": 6, "sketch": 20, "legacy_colour": 21, "unknown_hex": 0, "vega_lite": 0},
    "arrow-labels": {"pre_022": 12, "overflow": 1, "sketch": 12, "legacy_colour": 12, "unknown_hex": 0, "vega_lite": 0},
    "flowchart": {"pre_022": 23, "overflow": 14, "sketch": 36, "legacy_colour": 36, "unknown_hex": 0, "vega_lite": 0},
    "font-sizes": {"pre_022": 11, "overflow": 10, "sketch": 11, "legacy_colour": 11, "unknown_hex": 0, "vega_lite": 0},
    "frame-children": {"pre_022": 31, "overflow": 30, "sketch": 32, "legacy_colour": 32, "unknown_hex": 0, "vega_lite": 0},
    "house": {"pre_022": 8, "overflow": 6, "sketch": 10, "legacy_colour": 10, "unknown_hex": 0, "vega_lite": 0},
    "i18n": {"pre_022": 10, "overflow": 8, "sketch": 11, "legacy_colour": 11, "unknown_hex": 0, "vega_lite": 0},
    "legacy-mix": {"pre_022": 16, "overflow": 6, "sketch": 23, "legacy_colour": 24, "unknown_hex": 1, "vega_lite": 1},
    "sketch": {"pre_022": 5, "overflow": 2, "sketch": 9, "legacy_colour": 9, "unknown_hex": 0, "vega_lite": 0},
    "text-notes": {"pre_022": 8, "overflow": 6, "sketch": 8, "legacy_colour": 8, "unknown_hex": 0, "vega_lite": 0},
}
#: A member of team alpha holding an operator grant (a delegate): an operator for agents, never the lead.
DELEGATE = C.CanvasAuthor("alpha-reviewer", "member", "cli", True, agent="codex", operator=True)
APPLY = {"op": "migrate", "action": "apply", "intent": "migrate to canvas v2"}
DISMISS = {"op": "migrate", "action": "dismiss", "intent": "not now"}
#: What an op rewrites on every element it writes back: the rest of an element an undo restores is as it was stored.
STAMPS = ("updated_seq", "updated_at")


def load_board(team, name: str) -> None:
    """Put a golden 0.21 board's log and assets in ``team``'s canvas folder, as 0.21.2 left them."""
    folder = C._dir(team)
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(FIXTURES / name / "events.jsonl"), str(folder / C.EVENTS_FILE))
    if (FIXTURES / name / "assets").is_dir():
        shutil.copytree(str(FIXTURES / name / "assets"), str(folder / C.ASSETS_DIR))


def unstamped(el):
    return {k: v for k, v in el.items() if k not in STAMPS}


class Fixtures(CanvasRig):
    def test_every_board_is_there_and_small(self):
        self.assertEqual(sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()), sorted(BOARDS))
        total = sum(p.stat().st_size for p in FIXTURES.rglob("*") if p.is_file())
        self.assertLess(total, 400 * 1024)
        readme = (FIXTURES.parent / "README.md").read_text(encoding="utf-8")
        self.assertIn("cf048860", readme)
        for name in BOARDS:
            self.assertIn("`{}`".format(name), readme)


class Replay(CanvasRig):
    def board(self, name):
        load_board(self.team, name)
        return C.load_scene(self.team)

    def test_folding_a_021_log_loses_nothing(self):
        for name in BOARDS:
            with self.subTest(board=name):
                stored = json.loads((FIXTURES / name / "scene.json").read_text(encoding="utf-8"))
                self.tearDown_board()
                scene = self.board(name)
                folded = {el["id"]: el for el in scene["elements"]}
                self.assertEqual(sorted(folded), sorted(el["id"] for el in stored["elements"]))
                for el in stored["elements"]:
                    # Every stored field reads back unchanged (a kind may add today's fields to an old element on read).
                    now = folded[el["id"]]
                    self.assertEqual({k: now.get(k) for k in el}, el, el["id"])
                self.assertEqual(scene["version"], stored["version"])
                self.assertEqual(len(scene["legend"]), len(stored.get("legend") or []))
                self.assertEqual(len(scene["locks"]), len(stored.get("locks") or []))

    def tearDown_board(self):
        folder = C._dir(self.team)
        for name in (C.EVENTS_FILE, C.SCENE_FILE):
            (folder / name).unlink() if (folder / name).exists() else None
        shutil.rmtree(str(folder / C.ASSETS_DIR), ignore_errors=True)
        C._DISPLAY_CACHE.clear()

    def test_every_board_draws_on_v2_and_reads_back(self):
        for name in BOARDS:
            with self.subTest(board=name):
                self.tearDown_board()
                scene = self.board(name)
                doc = D.display_list(scene)
                self.assertEqual(D.validate(doc), [])
                self.assertEqual({e["id"] for e in doc["entries"] if e["id"].startswith(("E-", "C-"))},
                                 {el["id"] for el in scene["elements"]})
                looked = C.look(self.layout, self.team, "alpha-worker", advance=False)
                for el in scene["elements"]:
                    if el.get("type") != "comment":
                        self.assertIn(el["id"], looked["text"])
                with mock.patch.object(R, "find_resvg", return_value="/fake/resvg"), mock.patch.object(R, "RUN", fake_resvg):
                    pictured = C.look(self.layout, self.team, "alpha-worker", image=True, advance=False)
                self.assertTrue(pictured["image"], pictured.get("image_error"))

    def test_legacy_mix_holds_every_021_kind(self):
        scene = self.board("legacy-mix")
        kinds = {el["type"] for el in scene["elements"]}
        self.assertTrue({"frame", "box", "ellipse", "diamond", "note", "text", "arrow", "chart", "viz", "image", "comment"} <= kinds, kinds)
        self.assertTrue(any(el.get("type") == "chart" and el.get("spec_asset") for el in scene["elements"]), "a Vega-Lite chart")
        self.assertTrue(any((el.get("style") or {}).get("rough") == 2 for el in scene["elements"]))
        self.assertTrue(any((el.get("style") or {}).get("font") == "hand" for el in scene["elements"]))
        self.assertTrue(scene["legend"] and scene["locks"])
        self.assertTrue(any(el.get("mentions") for el in scene["elements"] if el.get("type") == "comment"))
        self.assertTrue(any(el.get("author") == C.HUMAN for el in scene["elements"]), "the operator's own marks")


class Report(CanvasRig):
    def test_each_board_reports_what_v2_draws_differently(self):
        for name in BOARDS:
            with self.subTest(board=name):
                Replay.tearDown_board(self)
                load_board(self.team, name)
                found = M.report_for(self.team)
                summary = M.summary(found)
                self.assertTrue(found["pending"])
                self.assertIsNone(found["state"])
                self.assertEqual({k: summary[k] for k in EXPECTED[name]}, EXPECTED[name])
                self.assertEqual(found["pre_022"], len(found["refit"]))
                self.assertEqual(summary["ids_total"], len(set(found["refit"]) | set(found["sketch"])))

    def test_legacy_mix_names_its_sketch_marks(self):
        load_board(self.team, "legacy-mix")
        found = M.report_for(self.team)
        scene = C.load_scene(self.team)
        rough = [el["id"] for el in scene["elements"] if (el.get("style") or {}).get("rough") == 2]
        self.assertTrue(rough and set(rough) <= set(found["sketch"]))
        self.assertEqual(found["vega_lite"], 1)

    def test_a_022_board_and_an_empty_board_are_never_pending(self):
        self.assertFalse(M.report_for(self.team)["pending"])
        self.ok({"op": "card", "title": "Checkout", "body": "- cart\n- pay", "at": "c0r0", "intent": "t"})
        self.ok({"op": "shape", "kind": "box", "text": "a box with a label", "at": "c20r0", "intent": "t"})
        self.ok({"op": "arrow", "from": "c0r10", "to": "c20r10", "label": "flows", "intent": "t"})
        found = M.report_for(self.team)
        self.assertEqual((found["pending"], found["pre_022"], found["sketch"]), (False, 0, []))

    def test_the_report_is_pure_and_cached_per_version(self):
        load_board(self.team, "house")
        scene = C.load_scene(self.team)
        self.assertEqual(M.report(scene), M.report_for(self.team))
        first = M.report_for(self.team)
        first["refit"].clear()
        self.assertTrue(M.report_for(self.team)["refit"], "callers get a copy")


class Apply(CanvasRig):
    def test_apply_refits_cleans_and_undo_takes_it_back_whole(self):
        for name in BOARDS:
            with self.subTest(board=name):
                Replay.tearDown_board(self)
                load_board(self.team, name)
                before = {el["id"]: el for el in C.load_scene(self.team)["elements"]}
                found = M.report_for(self.team)
                result = self.apply([APPLY], OPERATOR)
                self.assertEqual(result["refused"], [])
                self.assertEqual(len(result["applied"]), 1)
                batch = result["batch"]
                info = result["applied"][0]["migrate"]
                self.assertEqual((info["action"], info["refitted"], info["clean"], info["batch"]), ("apply", len(found["refit"]), len(found["sketch"]), batch))
                self.assertIn("undo {} to take it back".format(batch), C.apply_text(result))
                after = {el["id"]: el for el in C.load_scene(self.team)["elements"]}
                problems = C.check(self.layout, self.team, "human")["problems"]
                overflow = [p for p in problems if p["code"] == "label_overflow" and set(p["ids"]) & set(before)]
                self.assertEqual(overflow, [], "no label from before 0.22 overflows once refitted")
                self.assertEqual([p for p in problems if p["code"] == "overlap"], [], "refitting makes room, it never piles marks up")
                for eid in found["sketch"]:
                    style = after[eid].get("style") or {}
                    self.assertEqual(style.get("rough") or 0, 0, eid)
                    self.assertNotEqual(style.get("font"), "hand", eid)
                    if (before[eid].get("style") or {}).get("font") == "hand":
                        self.assertEqual(style.get("font"), "normal", eid)
                for eid in found["refit"]:
                    self.assertIsInstance(after[eid].get("fit"), dict, eid)
                self.assertFalse(M.report_for(self.team)["pending"])
                self.assertEqual(C.load_scene(self.team)["settings"]["migration"], {"action": "apply", "seq": result["version"], "by": C.HUMAN})
                undone = self.apply([{"op": "undo", "batch": batch, "intent": "take it back"}], OPERATOR)
                self.assertEqual(undone["refused"], [])
                back = {el["id"]: el for el in C.load_scene(self.team)["elements"]}
                self.assertEqual({k: unstamped(v) for k, v in back.items()}, {k: unstamped(v) for k, v in before.items()})
                self.assertNotIn("migration", C.load_scene(self.team)["settings"])
                self.assertTrue(M.report_for(self.team)["pending"], "the notice is back after the undo")

    def test_apply_is_one_event_and_nothing_to_do_is_refused(self):
        load_board(self.team, "house")
        version = C.current_version(self.team)
        result = self.apply([APPLY], OPERATOR)
        self.assertEqual(result["version"], version + 1, "one batch, one event")
        # A board inside the cap is finished by that one apply, so the answer is recorded with it.
        self.assertEqual(result["applied"][0]["migrate"]["left"], 0)
        self.assertNotIn("still to go", C.apply_text(result))
        refusal = self.refused(dict(APPLY), OPERATOR)
        self.assertEqual((refusal["code"], refusal["details"]["field"]), ("op_invalid", "action"))
        self.assertIn("nothing to migrate", refusal["message"])
        self.assertEqual(self.refused({"op": "migrate", "action": "later", "intent": "t"}, OPERATOR)["details"]["field"], "action")
        self.assertEqual(self.refused(dict(APPLY, if_version=1), OPERATOR)["details"]["field"], "if_version")
        self.assertIn("migrated the board to canvas v2", C.read_changes(self.layout, self.team, "human", since=version)["text"])

    def test_a_board_past_the_cap_is_fixed_in_as_many_applies_as_it_takes(self):
        """``MAX_IDS`` caps what one apply fixes (2.3). A board with more than that must not be recorded as migrated
        with the rest of it still at its old sizes: while the cap holds work back the answer is not written, so the
        notice and the op stay and the operator can run it again (or dismiss it). A board inside the cap is unaffected
        (``test_apply_is_one_event_and_nothing_to_do_is_refused`` holds that side)."""
        load_board(self.team, "house")
        with mock.patch.object(M, "MAX_IDS", 2):
            first = self.apply([APPLY], OPERATOR)
            self.assertEqual(first["refused"], [])
            self.assertGreater(first["applied"][0]["migrate"]["left"], 0)
            self.assertIn("still to go on this board, so the notice stays", C.apply_text(first))
            found = M.report_for(self.team)
            self.assertEqual((found["pending"], found["state"]), (True, None), "still pending, so she can finish it")
            rounds = 1
            while M.report_for(self.team)["pending"] and rounds < 20:
                self.assertEqual(self.apply([dict(APPLY)], OPERATOR)["refused"], [])
                rounds += 1
        found = M.report_for(self.team)
        self.assertEqual((found["pending"], found["state"]["action"], found["pre_022"], found["sketch"]), (False, "apply", 0, []))
        self.assertGreater(rounds, 1, "the cap really split the work")

    def test_dismiss_changes_nothing_but_the_answer(self):
        load_board(self.team, "sketch")
        before = C.load_scene(self.team)["elements"]
        result = self.apply([DISMISS], OPERATOR)
        self.assertEqual(result["refused"], [])
        self.assertEqual(C.load_scene(self.team)["elements"], before)
        found = M.report_for(self.team)
        self.assertEqual((found["pending"], found["state"]["action"]), (False, "dismiss"))
        self.assertTrue(found["sketch"], "the counts stay readable after a dismiss")
        self.assertIn("dismissed", C.apply_text(result))

    def test_only_the_operator_in_person_answers(self):
        load_board(self.team, "house")
        for who in (WORKER, REVIEWER, DELEGATE):
            with self.subTest(author=who.name, operator=who.operator):
                self.assertEqual(self.refused(dict(APPLY), who)["code"], "operator_only")
                self.assertEqual(self.refused(dict(DISMISS), who)["code"], "operator_only")
        with self.assertRaises(HerdrTeamError) as caught:
            self.apply([APPLY], C.page_author(False))
        self.assertEqual(caught.exception.code, "read_only")
        self.assertTrue(M.report_for(self.team)["pending"])
        self.assertEqual(self.apply([APPLY], C.page_author(True))["refused"], [], "the operator's writable page applies")
        self.assertFalse(M.report_for(self.team)["pending"])


class Look(CanvasRig):
    def test_the_line_is_the_operators_alone(self):
        load_board(self.team, "legacy-mix")
        mine = C.look(self.layout, self.team, "human", author=OPERATOR, advance=False)
        line = mine["text"].splitlines()[1]
        self.assertTrue(line.startswith("migration: drawn before canvas v2 · 6 labels need resizing · "
                                        "23 marks in the old sketch style now drawn clean"), line)
        self.assertTrue(line.endswith("herdr-synapse canvas migrate --apply (or --dismiss)"), line)
        self.assertEqual((mine["migration"]["pending"], mine["migration"]["overflow"]), (True, 6))
        for reader, author in (("alpha-worker", WORKER), ("alpha-reviewer", DELEGATE)):
            theirs = C.look(self.layout, self.team, reader, author=author, advance=False)
            self.assertNotIn("migration", theirs)
            self.assertNotIn("canvas v2", theirs["text"])
        untrusted = C.CanvasAuthor(C.HUMAN, "human", "cli", False, operator=False)
        self.assertNotIn("migration", C.look(self.layout, self.team, "human", author=untrusted, advance=False))
        self.apply([DISMISS], OPERATOR)
        self.assertNotIn("migration", C.look(self.layout, self.team, "human", author=OPERATOR, advance=False))

    def test_every_count_agrees_with_its_number(self):
        """QA phase 6, 3.4: the line read "1 label need resizing" on ``arrow-labels``, the one board with a single
        overflowing label, while the page's banner said "1 label needs resizing" - and the operator reads both, side
        by side. Every phrase the notice builds is checked at 1 and at 2 here."""
        load_board(self.team, "arrow-labels")
        found = M.report_for(self.team)
        self.assertEqual(found["overflow"] and len(found["overflow"]), 1, "one label over its shape")
        line = C.look(self.layout, self.team, "human", author=OPERATOR, advance=False)["text"].splitlines()[1]
        self.assertIn("1 label needs resizing", line)
        self.assertNotIn("label need resizing", line)
        for count, expected in ((1, "1 label needs resizing"), (2, "2 labels need resizing")):
            self.assertEqual(M.notice_parts({"overflow": count}), [expected])
        for count, expected in ((1, "1 label to size again"), (3, "3 labels to size again")):
            self.assertEqual(M.notice_parts({"refit": count}), [expected])
        for count, expected in ((1, "1 mark in the old sketch style now drawn clean"),
                                (4, "4 marks in the old sketch style now drawn clean")):
            self.assertEqual(M.notice_parts({"sketch": count}), [expected])
        # The only other numbers the operator reads are counted after a colon or before a past participle, which both
        # forms fit: "labels that do not fit now: 1", "migrated: 1 refitted (1 grew, ...)".
        one = M.text({"pending": True, "overflow": ["E-1"], "refit": ["E-1"], "sketch": ["E-2"], "pre_022": 1})
        self.assertIn("labels that do not fit now: 1: E-1", one)
        self.assertIn("1 label needs resizing", one)

    def test_the_sketch_count_names_the_style_not_a_hand(self):
        """0.21's defaults were ``rough: 1`` and the hand font, so the count is nearly every mark of the board (here
        every one of the 10): calling them "hand-drawn" would tell the operator she drew an architecture diagram by
        hand. Every word she reads names the style instead."""
        load_board(self.team, "house")
        found = M.report_for(self.team)
        live = [el for el in self.scene()["elements"] if not el.get("deleted")]
        self.assertEqual((found["sketch"] and len(found["sketch"]), len(live)), (10, 10), "0.21 drew every mark sketchy")
        mine = C.look(self.layout, self.team, "human", author=OPERATOR, advance=False)
        words = "\n".join([mine["text"], M.text(found), " ".join(M.notice_parts(found)), M.look_line(found) or ""])
        self.assertNotIn("hand-drawn", words)
        self.assertIn("10 marks in the old sketch style now drawn clean", words)
        self.assertIn("marks in the old sketch style, 0.21's default look", M.text(found))

    def test_no_line_after_apply(self):
        load_board(self.team, "house")
        self.apply([APPLY], OPERATOR)
        self.assertNotIn("migration:", C.look(self.layout, self.team, "human", author=OPERATOR, advance=False)["text"])


class DisplayNotice(RouteCase):
    def setUp(self):
        super().setUp()
        self.team = self.ts.team
        C.purge(self.team)
        whiteboard_on(self.ts.session, self.team, via="console")
        load_board(self.team, "house")
        C._DISPLAY_CACHE.clear()

    def test_the_writable_page_gets_the_notice_while_it_is_pending(self):
        doc = self.get("/api/teams/alpha/display").json()
        notice = doc["migration"]
        self.assertEqual((notice["pending"], notice["refit"], notice["overflow"], notice["sketch"]), (True, 8, 6, 10))
        self.assertNotIn("ids", json.dumps(notice).replace("ids_total", ""), "counts, no id lists")
        self.assertEqual(self.get("/api/teams/alpha/display?since={}".format(doc["version"])).json()["migration"]["pending"], True)
        result = self.post("/api/teams/alpha/ops", {"ops": [APPLY]}).json()
        self.assertEqual(result["refused"], [])
        delta = self.get("/api/teams/alpha/display?since={}".format(doc["version"])).json()
        self.assertIsNone(delta["migration"], "a delta says it settled")
        self.assertNotIn("migration", self.get("/api/teams/alpha/display").json(), "a whole list carries it only while pending")
        self.post("/api/teams/alpha/ops", {"ops": [{"op": "undo", "batch": result["batch"]}]})
        again = self.get("/api/teams/alpha/display?since={}".format(result["version"])).json()
        self.assertTrue(again["migration"]["pending"], "an undo brings it back")

    def test_a_read_only_page_never_sees_it(self):
        cookie, _csrf = self.sign_in(False)
        self.assertNotIn("migration", self.get("/api/teams/alpha/display", cookie).json())
        self.assertNotIn("migration", self.get("/api/teams/alpha/display?since=1", cookie).json())
        refused = self.post("/api/teams/alpha/ops", {"ops": [APPLY]}, cookie=cookie)
        self.assertEqual(refused.status, 403)


class MigrateCli(CanvasRig):
    def setUp(self):
        super().setUp()
        from support import FAKE_AGENTS
        from test_canvas import Cli
        from test_cmd_roster import live_api

        self.helper = Cli("test_draw_from_a_file_stdin_and_op")
        self.helper.ts, self.helper.team, self.helper.layout = self.ts, self.team, self.layout
        self.helper.setUp = None
        self.helper.api = live_api(list(FAKE_AGENTS))
        load_board(self.team, "legacy-mix")

    def test_anyone_reads_the_report_and_only_the_operator_answers(self):
        code, payload, err = self.helper.worker("canvas", "migrate")
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["migration"]["pending"])
        self.assertEqual(payload["summary"]["vega_lite"], 1)
        code, _payload, err = self.helper.worker("canvas", "migrate", "--apply")
        self.assertEqual(err["refused"][0]["code"], "operator_only")
        code, payload, err = self.helper.human("canvas", "migrate", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["applied"][0]["migrate"]["action"], "apply")
        code, payload, err = self.helper.human("canvas", "migrate")
        self.assertEqual((code, payload["migration"]["pending"], payload["migration"]["state"]["action"]), (0, False, "apply"))
        code, _payload, err = self.helper.human("canvas", "migrate", "--apply", "--dismiss")
        self.assertEqual(code, 2)

    def test_the_text_says_what_changes(self):
        found = M.report_for(self.team)
        text = M.text(found)
        self.assertIn("labels from before canvas v2: 16 (16 refit on apply)", text)
        self.assertIn("the classic canvas (?engine=v1) still draws them", text)
        self.assertEqual(M.result_text({"action": "apply", "refitted": 14, "grew": 6, "moved": 2, "clean": 3, "batch": "B-12"}),
                         "migrated: 14 refitted (6 grew, 2 moved to fit), 3 drawn clean (rough, hand font); undo B-12 to take it back")


class Checked(CanvasRig):
    def test_the_generator_check_passes(self):
        import subprocess
        import sys

        done = subprocess.run([sys.executable, str(PLUGIN_ROOT / "tools" / "make_v021_boards.py"), "--check"], capture_output=True, text=True,
                              check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
