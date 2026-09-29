"""The canvas benchmark (canvas v2 phase 6, 6.10): the fixtures, the scorer, the offline gate, the negative controls,
the 0.21-language comparison, and live mode end to end through the fake runner. No wall-clock assertion anywhere."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from support import PLUGIN_ROOT


def load(name):
    tools = str(PLUGIN_ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = load("canvas_bench")
Q = B.Q
C = B.C
store = B.store

HAS_TRACE = hasattr(C, "read_attempts")
TRACE_REASON = "canvas.read_attempts (the attempt trace, 6.7) is not in this build yet"


def _cli(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = B.main(argv, env=env)
    return code, out.getvalue(), err.getvalue()


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.prompts = B.load_prompts("all")

    def test_the_fixture_check_passes(self):
        found = B.check_fixtures()
        self.assertEqual(found["problems"], [])
        code, out, _err = _cli(["--check"])
        self.assertEqual(code, 0, out)

    def test_thirty_prompts_with_the_counts_of_6_3(self):
        self.assertEqual(len(self.prompts), 30)
        ids = [p["id"] for p in self.prompts]
        self.assertEqual(len(set(ids)), 30)
        for p in self.prompts:
            self.assertEqual(p["id"], Path(p["_file"]).stem)
        counts = {}
        for p in self.prompts:
            counts[p["category"]] = counts.get(p["category"], 0) + 1
        self.assertEqual(counts, B.CATEGORIES)
        self.assertGreaterEqual(sum(1 for p in self.prompts if p["audience"] == "nondev"), 8)
        self.assertEqual(sum(1 for p in self.prompts if p.get("live")), 10)
        self.assertGreaterEqual(sum(1 for p in self.prompts if p.get("reference_v1")), 10)
        self.assertGreaterEqual(len(B.load_controls()), 8)

    def test_every_data_file_is_there_and_small(self):
        suffixes = set()
        for p in self.prompts:
            for name in p.get("artifacts") or []:
                self.assertTrue((B.DATA_DIR / name).is_file(), name)
                suffixes.add(Path(name).suffix)
        self.assertLessEqual({".csv", ".json", ".tsv"}, suffixes)
        self.assertLess(sum(f.stat().st_size for f in B.DATA_DIR.iterdir()), 50 * 1024)

    def test_prompts_are_what_an_operator_would_say(self):
        for p in self.prompts:
            with self.subTest(p["id"]):
                text = p["prompt"]
                self.assertNotIn('"op"', text)
                for key in ("shape", "arrow", "frame", "op"):
                    self.assertIsNone(re.search(r'"{}"\s*:'.format(key), text))
                self.assertIsNone(re.search(r"\bc-?\d+r-?\d+\b", text), "a cell in the request")
                self.assertNotIn("{", text)
                self.assertNotIn("->", text)

    def test_the_check_catches_a_bad_fixture(self):
        tmp = Path(tempfile.mkdtemp(prefix="bench-fx-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        prompts, controls = tmp / "prompts", tmp / "controls"
        shutil.copytree(os.fspath(B.PROMPTS_DIR), os.fspath(prompts))
        shutil.copytree(os.fspath(B.CONTROLS_DIR), os.fspath(controls))
        doc = json.loads((prompts / "01-flow-checkout.json").read_text(encoding="utf-8"))
        doc["prompt"] = 'Draw a shape at c3r4 with {"op": "shape"} please now'
        doc["artifacts"] = ["missing.csv"]
        (prompts / "01-flow-checkout.json").write_text(json.dumps(doc), encoding="utf-8")
        (prompts / "02-flow-onboarding.json").unlink()
        saved = (B.PROMPTS_DIR, B.CONTROLS_DIR)
        B.PROMPTS_DIR, B.CONTROLS_DIR = prompts, controls
        try:
            problems = "\n".join(B.check_fixtures()["problems"])
        finally:
            B.PROMPTS_DIR, B.CONTROLS_DIR = saved
        self.assertIn("29 prompts", problems)
        self.assertIn("names a cell", problems)
        self.assertIn('"op"', problems)
        self.assertIn("missing.csv", problems)
        self.assertIn("category flowchart has 2", problems)


class LabelsAndRelations(unittest.TestCase):
    def test_label_matching(self):
        self.assertTrue(B.label_match("Checkout API", "checkout   api"))
        self.assertTrue(B.label_match("Payment approved", "Payment approved?"))
        self.assertTrue(B.label_match("Cart", "Show retry and keep the cart"))
        self.assertTrue(B.label_match("QA", "qa"))
        self.assertFalse(B.label_match("API", "Checkout API"))  # shorter than 4: only equal texts match
        self.assertFalse(B.label_match("Grindhouse", "Daily Grind"))
        self.assertFalse(B.label_match("", "x"))
        self.assertFalse(B.label_match("Budget: $40k", "Budget: $45k"))

    def test_edge_shorthand(self):
        self.assertEqual(B._parse_edge("api -> db: SQL"), ("api", "->", "db", "SQL"))
        self.assertEqual(B._parse_edge("a --> b"), ("a", "-->", "b", None))
        self.assertEqual(B._parse_edge("a <-> b"), ("a", "<->", "b", None))
        self.assertEqual(B._parse_edge("a -- b: pairs"), ("a", "--", "b", "pairs"))
        self.assertEqual(B._parse_edge({"from": "a", "to": "b", "label": "x"}), ("a", "->", "b", "x"))
        self.assertIsNone(B._parse_edge("no arrow here"))

    def model(self):
        m = B.Model()
        for t in ("Producer", "Broker", "Consumer", "Topics", "Audit log"):
            m.text(t)
        m.edge("Producer", "Broker")
        m.edge("Broker", "Audit log", directed=False)
        m.contain("Topics", "Consumer")
        return m

    def test_relation_kinds(self):
        m = self.model()
        self.assertTrue(B.relation_found(m, ["Producer", "Broker"]))
        self.assertFalse(B.relation_found(m, ["Broker", "Producer"]))  # directed
        self.assertTrue(B.relation_found(m, ["Audit log", "Broker"]))  # undirected either way
        self.assertTrue(B.relation_found(m, ["Topics", "Consumer", "contains"]))
        self.assertFalse(B.relation_found(m, ["Consumer", "Topics", "contains"]))
        self.assertTrue(B.relation_found(m, ["Consumer", "Topics", "any"]))
        self.assertTrue(B.relation_found(m, ["Broker", "Producer", "any"]))
        self.assertFalse(B.relation_found(m, ["Producer", "Consumer", "any"]))

    def test_precision_counts_invented_and_reversed_edges(self):
        m = self.model()
        m.edge("Consumer", "Producer")
        got = B.align({"labels": ["Producer", "Broker", "Consumer"], "relations": [["Producer", "Broker"], ["Producer", "Consumer"]]}, m)
        self.assertEqual(got["labels_recall"], 1.0)
        self.assertEqual(got["relations_recall"], 0.5)
        self.assertEqual(got["relations_precision"], 0.5)
        self.assertEqual(got["reversed_relations"], [["Producer", "Consumer"]])
        self.assertLess(got["alignment"], 1.0)
        # A request that names no edge does not measure precision: extra arrows there are a style.
        plain = B.align({"labels": ["Producer", "Broker"]}, m)
        self.assertEqual(plain["relations_precision"], 1.0)
        self.assertEqual(plain["alignment"], 1.0)

    def test_the_ok_facts(self):
        m = self.model()
        m.kinds.update({"graph", "arrow"})
        m.tones.append(("Broker", "danger"))
        m.status.append(("Consumer", "blocked"))
        m.charts.append({"type": "bar", "fields": ["month", "revenue"]})
        m.scenes.append({"objects": 6, "links": 1})
        got = B.align({"labels": ["Broker"], "kinds_any": ["kanban", "graph"], "tones": {"Broker": ["warning", "danger"]},
                       "status": {"Consumer": "blocked"}, "gone_relations": [["Broker", "Producer"]], "absent_labels": ["Zookeeper"],
                       "chart": {"type_any": ["bar"], "fields": ["month"]}, "scene3d": {"objects_min": 6, "links_min": 1}}, m)
        for key in ("kinds_ok", "tones_ok", "status_ok", "gone_ok", "absent_ok", "chart_ok", "scene3d_ok"):
            self.assertTrue(got[key], key)
        bad = B.align({"labels": ["Broker"], "kinds_any": ["kanban"], "tones": {"Broker": "info"}, "gone_relations": [["Producer", "Broker"]],
                       "absent_labels": ["Audit log"], "chart": {"type_any": ["line"], "fields": []}, "scene3d": {"objects_min": 7}}, m)
        for key in ("kinds_ok", "tones_ok", "gone_ok", "absent_ok", "chart_ok", "scene3d_ok"):
            self.assertFalse(bad[key], key)


class Crossings(unittest.TestCase):
    def route(self, a, b, *points):
        return {"ends": (a, b), "points": [tuple(p) for p in points]}

    def test_an_x_crosses_once(self):
        routes = [self.route("A", "B", (0, 0), (100, 100)), self.route("C", "D", (0, 100), (100, 0))]
        self.assertEqual(B.route_crossings(routes), 1)

    def test_touching_parallel_and_collinear_do_not_cross(self):
        self.assertEqual(B.route_crossings([self.route("A", "B", (0, 0), (100, 0)), self.route("C", "D", (50, 0), (50, 80))]), 0)
        self.assertEqual(B.route_crossings([self.route("A", "B", (0, 0), (100, 0)), self.route("C", "D", (0, 10), (100, 10))]), 0)
        self.assertEqual(B.route_crossings([self.route("A", "B", (0, 0), (100, 0)), self.route("C", "D", (50, 0), (150, 0))]), 0)

    def test_a_pair_counts_once_and_elbows_count(self):
        zigzag = self.route("A", "B", (0, 50), (40, 50), (40, 0), (80, 0), (80, 100))
        line = self.route("C", "D", (0, 30), (120, 30))
        self.assertEqual(B.route_crossings([zigzag, line]), 1)

    def test_a_crossing_at_a_shared_end_is_where_they_meet(self):
        shared = {"S": {"x": 90, "y": 40, "w": 40, "h": 40}}
        a = self.route("A", "S", (0, 0), (110, 60))
        b = self.route("B", "S", (0, 100), (110, 50))
        self.assertEqual(B.route_crossings([a, b], shared), 0)
        far = self.route("B", "S", (60, 100), (60, -10), (110, 45))
        self.assertEqual(B.route_crossings([a, far], shared), 1)


class Stability(unittest.TestCase):
    def el(self, eid, kind, text, x, y, w=100, h=60):
        return {"id": eid, "type": kind, "text": text, "x": x, "y": y, "w": w, "h": h}

    def test_small_moves_and_growing_containers_are_stable(self):
        before = [self.el("E-1", "frame", "Board", 0, 0, 500, 300), self.el("E-2", "box", "Alpha", 20, 40), self.el("E-3", "arrow", "", 0, 0)]
        after = [self.el("E-1", "frame", "Board", 0, 0, 700, 420), self.el("E-2", "box", "Alpha", 35, 50), self.el("E-3", "arrow", "", 300, 300)]
        self.assertTrue(B.stability(before, after, [])["stable"])

    def test_moves_resizes_and_deletions_are_not(self):
        before = [self.el("E-2", "box", "Alpha", 20, 40), self.el("E-4", "card", "Beta", 200, 40), self.el("E-5", "box", "Gamma", 400, 40)]
        after = [self.el("E-2", "box", "Alpha", 60, 40), self.el("E-4", "card", "Beta", 200, 40, 160, 60)]
        got = B.stability(before, after, [])
        self.assertFalse(got["stable"])
        self.assertEqual(len(got["moved"]), 3)
        self.assertTrue(any("deleted" in m for m in got["moved"]))
        self.assertEqual(len(B.stability(before, after, ["Alpha", "Beta", "Gamma"])["moved"]), 0)


class OnABoard(unittest.TestCase):
    """The scorer against real boards in a throwaway team."""

    def setUp(self):
        self.qa = Q.QaTeam()
        self.addCleanup(self.qa.cleanup)
        self.prompts = B.load_prompts("all")

    def apply(self, ops, who="drawer"):
        return C.apply_ops(self.qa.layout, self.qa.team, ops, self.qa.author_for(who))

    def test_readback_that_drops_an_edge_disagrees_with_the_drawing(self):
        prompt = B.prompt_by_id(self.prompts, "01-flow-checkout")
        self.apply(prompt["reference"])
        look, specs = B.read_back(self.qa.layout, self.qa.team, Q.PEER)
        drawn = B.align(prompt["expect"], B.drawn_model(C.load_scene(self.qa.team)))
        self.assertEqual(drawn["alignment"], 1.0)
        self.assertEqual(B.align(prompt["expect"], B.readback_model(look, specs))["alignment"], 1.0)
        patched = json.loads(json.dumps(specs))
        (root,) = [k for k, v in patched.items() if v.get("op") == "graph"]
        patched[root]["edges"] = [e for e in patched[root]["edges"] if not e.startswith("db -> mail")]
        look = dict(look, blocks={k: v for k, v in (look.get("blocks") or {}).items() if k != root})
        got = B.align(prompt["expect"], B.readback_model(look, patched))
        self.assertEqual(got["missing_relations"], [["Orders database", "Confirmation email"]])
        self.assertLess(got["alignment"], 1.0)
        # Through the whole score: the row says the readback is the defect.
        saved = B.read_back
        B.read_back = lambda layout, team, reader: (look, patched)
        try:
            row = B.score(self.qa.layout, self.qa.team, prompt["expect"], Q.MEMBER, Q.PEER, 0, [], True, 1, 0)
        finally:
            B.read_back = saved
        self.assertIn("readback", row["reasons"])
        self.assertEqual(row["alignment"]["drawn"], 1.0)

    def test_readback_of_a_busy_board_still_reads_each_block(self):
        prompt = B.prompt_by_id(self.prompts, "04-arch-web")
        self.apply([{"op": "section", "id": "wall", "title": "Idea wall", "layout": "grid", "cols": 10, "at": [0, 2000], "intent": "ideas"}]
                   + [{"op": "sticky", "in": "wall", "text": "Idea number {}".format(n), "intent": "ideas"} for n in range(45)])
        self.apply(prompt["reference"])
        look, specs = B.read_back(self.qa.layout, self.qa.team, Q.PEER)
        self.assertEqual(look["level"], "overview")  # past 60 elements look lists one line each
        self.assertTrue(any(s.get("op") == "graph" for s in specs.values()))
        got = B.align(prompt["expect"], B.readback_model(look, specs))
        self.assertEqual(got["labels_recall"], 1.0)
        self.assertEqual(got["relations_recall"], 1.0)

    def test_the_window_is_the_members_own_changes(self):
        self.apply([{"op": "card", "id": "a", "title": "Seed card", "at": [0, 0], "intent": "seed"}], "peer")
        since = C.current_version(self.qa.team)
        self.apply([{"op": "card", "id": "b", "title": "Member card", "right_of": "a", "intent": "t"}])
        got = B.window_changes(self.qa.team, since, Q.MEMBER)
        scene = {el["id"]: el for el in C.load_scene(self.qa.team)["elements"]}
        self.assertEqual({scene[i]["text"] for i in got["ids"]}, {"Member card"})
        self.assertEqual(B.window_changes(self.qa.team, since, Q.PEER)["ids"], set())

    def test_proposal_detection(self):
        self.apply([{"op": "card", "id": "budget", "title": "Budget: $40k", "at": [0, 0], "intent": "b"},
                    {"op": "card", "id": "owner", "title": "Owner: Maya", "right_of": "budget", "intent": "o"}], "lead")
        since = C.current_version(self.qa.team)
        C.look(self.qa.layout, self.qa.team, Q.MEMBER)
        self.apply([{"op": "edit", "id": "budget", "text": "Budget: $45k", "intent": "raise"}])
        made = B.window_changes(self.qa.team, since, Q.MEMBER)["proposals"]
        self.assertEqual(len(made), 1)
        scene = C.load_scene(self.qa.team)
        self.assertTrue(B.proposal_facts(scene, made, {"proposal": {"count": 1, "about": "Budget", "text": "45k"}})["proposal_ok"])
        self.assertFalse(B.proposal_facts(scene, made, {"proposal": {"count": 2}})["proposal_ok"])
        self.assertFalse(B.proposal_facts(scene, made, {"proposal": {"count": 1, "about": "Owner"}})["proposal_ok"])
        self.assertFalse(B.proposal_facts(scene, made, {"proposal": {"count": 1, "text": "50k"}})["proposal_ok"])

    def test_hard_problems_count_only_on_what_the_member_touched(self):
        self.apply([{"op": "card", "id": "a", "title": "Vendor call", "at": [0, 0], "intent": "one"}, {"op": "pin", "ids": ["a"], "intent": "p"},
                    {"op": "card", "id": "b", "title": "Budget review", "at": [400, 0], "intent": "two"}, {"op": "pin", "ids": ["b"], "intent": "p"},
                    {"op": "move", "id": "b", "to": [40, 20], "intent": "pile"}], "peer")
        problems = C.check(self.qa.layout, self.qa.team, Q.PEER)["problems"]
        ids = {i for p in problems if p["code"] == "overlap" for i in p["ids"]}
        self.assertTrue(ids)
        self.assertEqual(B._problem_counts(problems, set())[0]["overlap"], 0)
        self.assertEqual(B._problem_counts(problems, ids)[0]["overlap"], 1)


class Offline(unittest.TestCase):
    """The gate itself: every v2 reference holds it, every control is flagged, and the 0.21 rows exist."""

    @classmethod
    def setUpClass(cls):
        cls.prompts = B.load_prompts("all")
        cls.results = B.run_offline(cls.prompts, cls.prompts, "both", B.load_controls())

    def test_every_v2_reference_passes(self):
        rows = {r["id"]: r for r in self.results["rows"] if r["language"] == "v2"}
        self.assertEqual(len(rows), 30)
        for pid, row in sorted(rows.items()):
            with self.subTest(pid):
                self.assertTrue(row["pass"], row["reasons"])
                self.assertEqual(B.gate_reasons(row), [])
                self.assertEqual(row["alignment"], {"drawn": 1.0, "readback": 1.0})
                self.assertEqual(sum(row["hard"].values()), 0)
                self.assertTrue(row["valid_first_try"])
                if B.prompt_by_id(self.prompts, pid)["expect"].get("stable"):
                    self.assertIs(row["stable"], True, row["moved"])
        self.assertTrue(self.results["ok"], self.results["failures"] or self.results["unflagged"])

    def test_every_control_is_flagged(self):
        controls = {c["id"]: c for c in self.results["controls"]}
        self.assertGreaterEqual(len(controls), 8)
        for cid, row in sorted(controls.items()):
            with self.subTest(cid):
                self.assertTrue(row["flagged"], "{}: reasons {} crossings {}".format(cid, row["reasons"], row["crossings"]))
        self.assertGreater(controls["c08-crossings-k33"]["crossings"], 0)
        self.assertEqual(controls["c03-overflow-replayed"]["hard"]["label_overflow"], 1)

    def test_a_control_flagged_by_its_crossings_says_so_in_the_table(self):
        """A control judged by ``expect_crossings_min`` has no reason string (c08), so the report names the crossings
        it wanted and the crossings it found; no row reads as flagged with an empty expectation."""
        rows = {c["id"]: c for c in self.results["controls"]}
        c08 = rows["c08-crossings-k33"]
        self.assertEqual(c08["expected"], [])
        self.assertEqual(c08["expects"], ["at least {} crossing".format(c08["crossings_min"])]
                         if c08["crossings_min"] == 1 else ["at least {} crossings".format(c08["crossings_min"])])
        table = [line for line in B.summary_markdown(self.results).splitlines() if line.startswith("| c0") or line.startswith("| c1")]
        self.assertTrue(table)
        for line in table:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            self.assertEqual(len(cells), 4, line)
            self.assertTrue(cells[2] and not cells[2].startswith("crossings"), line)
            self.assertTrue(cells[3], line)
        found = next(line for line in table if line.startswith("| c08"))
        self.assertIn("at least 1 crossing |", found)
        self.assertRegex(found, r"\| \d+ crossings? \|$")

    def test_a_flagged_control_records_why_and_reports_one_crossing_count(self):
        """QA phase 6 F12: the rendered table named the crossings, but the row in ``results.json`` stayed
        ``{"reasons": [], "flagged": true}`` - a flagged control with no reason - and the console line printed the
        route crossings while the table printed route plus block, so one control reported two different numbers."""
        rows = {c["id"]: c for c in self.results["controls"]}
        c08 = rows["c08-crossings-k33"]
        self.assertEqual(c08["reasons"], [], "it is judged by its crossings, not by a reason string")
        self.assertEqual(c08["flagged_for"], ["{} crossings".format(c08["crossings_total"])])
        self.assertEqual(c08["crossings_total"], c08["crossings"] + c08["block_crossings"])
        self.assertIn("crossings {} ".format(c08["crossings_total"]), B.summary_line(c08))
        self.assertTrue(B.summary_line(c08).endswith("{} crossings".format(c08["crossings_total"])))
        for cid, row in sorted(rows.items()):
            with self.subTest(cid):
                self.assertTrue(row["reasons"] or row["flagged_for"], "a flagged control always says why")
        # Every row carries the total, and the per-prompt table prints that one number.
        for row in self.results["rows"]:
            self.assertEqual(row["crossings_total"], row["crossings"] + row["block_crossings"])

    def test_the_021_language_is_scored_beside_the_v2_one(self):
        both = B.compare_languages(self.results["rows"])
        with_v1 = [p["id"] for p in self.prompts if p.get("reference_v1")]
        self.assertEqual(sorted(c["id"] for c in both), sorted(with_v1))
        for c in both:
            self.assertEqual(c["ops_v2"], 1)
            self.assertGreater(c["ops_v1"], c["ops_v2"])
        text = B.summary_markdown(self.results)
        self.assertIn("## v2 language vs 0.21 language", text)
        self.assertIn("| 04-arch-web |", text)
        self.assertIn("## Negative controls", text)

    def test_the_offline_command(self):
        code, out, err = _cli(["offline", "--prompts", "13", "--no-controls"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("1/1 v2 references hold the gate", out)
        tmp = Path(tempfile.mkdtemp(prefix="bench-out-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        code, out, _err = _cli(["offline", "--controls", "--json", "--out", os.fspath(tmp)])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["rows"], [])
        self.assertTrue(all(c["flagged"] for c in doc["controls"]))
        for name in ("results.json", "summary.md", "rating.md"):
            self.assertTrue((tmp / name).is_file(), name)


class Live(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="bench-live-"))
        self.addCleanup(shutil.rmtree, self.out, True)
        self.prompts = B.load_prompts("all")

    def test_the_guard_refuses_anything_but_the_rig(self):
        for env in ({}, {"HERDR_SESSION": "syn-l6x-1"}, {"HERDR_SESSION": "main", "XDG_STATE_HOME": "/Users/someone/.local/state"},
                    {"XDG_STATE_HOME": "/var/tmp/syn-e2e/l6x/state"}, {"HERDR_SESSION": "x", "XDG_STATE_HOME": "/var/tmp/syn-e2e-other/state"}):
            with self.subTest(env=env):
                with self.assertRaises(B.BenchError):
                    B.guard_rig(env)
        B.guard_rig({"HERDR_SESSION": "syn-l6x-1", "XDG_STATE_HOME": "/var/tmp/syn-e2e/l6x/state"})
        B.guard_rig({"HERDR_SESSION": "syn-l6x-1", "XDG_STATE_HOME": "/private/var/tmp/syn-e2e/l6x/state"})
        code, _out, err = _cli(["live", "--team", "l6", "--member", "l6-drawer", "--out", os.fspath(self.out / "x")],
                               env={"HERDR_SESSION": "main", "XDG_STATE_HOME": "/Users/someone/.local/state", "PATH": "/usr/bin"})
        self.assertEqual(code, 2)
        self.assertIn("only inside the rig", err)
        self.assertFalse((self.out / "x").exists())

    def test_the_dry_run_is_the_plan(self):
        code, out, _err = _cli(["live", "--team", "l6", "--member", "a", "--member", "b", "--member", "c", "--prompts", "live",
                                "--out", os.fspath(self.out / "dry"), "--dry-run", "--json"], env={})
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(plan["turns"], 30)
        self.assertEqual(plan["steps"][0], {"prompt": "01-flow-checkout", "member": "a"})
        self.assertFalse((self.out / "dry").exists())

    def test_fake_runner_end_to_end(self):
        picked = [B.prompt_by_id(self.prompts, pid) for pid in ("09-kanban-launch", "18-chart-csv", "26-edit-add-node", "30-edit-operator")]
        results = B.run_fake(picked, self.prompts, self.out)
        written = B.write_outputs(results, self.out)
        rows = results["rows"]
        self.assertEqual(len(rows), 8)
        self.assertEqual(results["delivered"], 8)
        for row in rows:
            with self.subTest(row["id"] + "/" + row["member"]):
                self.assertTrue(row["pass"], row["reasons"])
                self.assertEqual(sum(row["hard"].values()), 0)
                self.assertEqual(row["alignment"], {"drawn": 1.0, "readback": 1.0})
                self.assertIsNone(row["tokens"]["total"])  # nothing to read in a throwaway home, and it says why
                self.assertTrue(row["tokens"]["reason"])
        self.assertEqual({r["member"] for r in rows}, set(B.FAKE_MEMBERS))
        self.assertEqual(json.loads(Path(written["results"]).read_text(encoding="utf-8"))["rows"][0]["mode"], "live")
        summary = Path(written["summary"]).read_text(encoding="utf-8")
        self.assertIn("## Live runs", summary)
        self.assertIn("qa-peer (codex)", summary)
        rating = Path(written["rating"]).read_text(encoding="utf-8")
        if any(r.get("picture") for r in rows):  # pictures only where resvg is installed
            self.assertIn("Clarity 1-5:", rating)
        else:
            self.assertIn("No pictures", rating)

    @unittest.skipUnless(HAS_TRACE, TRACE_REASON)
    def test_first_try_comes_from_the_attempt_trace(self):
        picked = [B.prompt_by_id(self.prompts, "09-kanban-launch")]
        bad = [{"op": "kanbann", "id": "x", "intent": "typo"}] + list(picked[0]["reference"])
        results = B.run_fake(picked, self.prompts, self.out, answers={"09-kanban-launch": bad})
        for row in results["rows"]:
            self.assertIs(row["valid_first_try"], False)
            self.assertEqual(row["attempts"], 1)
            self.assertEqual(row["refused_total"], 1)
            self.assertIn("first_try", row["reasons"])
            self.assertTrue(row["pass"])  # the rest applied: first-try validity is measured, not a pass condition
        good = B.run_fake(picked, self.prompts, self.out)
        self.assertTrue(all(r["valid_first_try"] is True for r in good["rows"]))

    def test_the_switch_is_removed_after_each_request(self):
        with Q.QaTeam() as qa:
            B._qa_artifacts(qa)
            prompt = B.prompt_by_id(self.prompts, "13-timeline-roadmap")
            runner = B.FakeRunner(qa.layout, qa.team, self.prompts)
            B.run_live(qa.layout, qa.team, [prompt], self.prompts, [Q.MEMBER], runner, self.out, pictures=False, env=dict(qa.env))
            self.assertFalse(B._trace_switch(qa.team).exists())
            if HAS_TRACE:
                self.assertTrue(C.read_attempts(qa.team))

    def test_wait_gives_up_on_an_agent_that_never_starts_and_on_the_timeout(self):
        class Idle(B.FakeRunner):
            def status(self, pane):
                return "idle"

        class Busy(B.FakeRunner):
            def status(self, pane):
                return "working"

        with Q.QaTeam() as qa:
            idle = Idle(qa.layout, qa.team, self.prompts)
            got = B.wait_settled(idle, "w1:p1", qa.team, timeout=600.0)
            self.assertFalse(got["worked"])
            self.assertFalse(got["timed_out"])
            self.assertGreater(got["seconds"], B.START_GRACE_S)  # the fake clock: it waited out the grace, no longer

            class Quick(Idle):
                def sleep(self, seconds):
                    super().sleep(seconds)
                    if not getattr(self, "drew", False):
                        self.drew = True
                        C.apply_ops(self.layout, self.team, [{"op": "sticky", "text": "Done already", "at": [0, 0], "intent": "t"}],
                                    C.CanvasAuthor(Q.MEMBER, C.KIND_MEMBER, "mcp", True, agent="claude", team=Q.TEAM))

            got = B.wait_settled(Quick(qa.layout, qa.team, self.prompts), "w1:p1", qa.team, timeout=600.0)
            self.assertTrue(got["worked"])  # a turn quicker than a poll is seen by what it drew
            self.assertLess(got["seconds"], B.START_GRACE_S)
            busy = Busy(qa.layout, qa.team, self.prompts)
            got = B.wait_settled(busy, "w1:p1", qa.team, timeout=30.0)
            self.assertTrue(got["timed_out"])
            self.assertEqual(got["state"], "working")

    def test_report_merges_runs(self):
        first, second = self.out / "one", self.out / "two"
        rows = [{"id": "01-flow-checkout", "mode": "live", "member": "l6-drawer", "kind": "claude", "pass": True, "hard": {"overlap": 0},
                 "alignment": {"drawn": 1.0, "readback": 1.0}, "seconds": 40.0, "tokens": {"total": 1200}, "valid_first_try": True}]
        B.write_outputs({"rows": rows, "controls": []}, first)
        B.write_outputs({"rows": [dict(rows[0], member="l6-coder", kind="codex", tokens={"total": None, "reason": "x"})], "controls": []}, second)
        code, _out, _err = _cli(["report", os.fspath(first), os.fspath(second), "--out", os.fspath(self.out / "merged")])
        self.assertEqual(code, 0)
        merged = json.loads((self.out / "merged" / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(len(merged["rows"]), 2)
        summary = (self.out / "merged" / "summary.md").read_text(encoding="utf-8")
        self.assertIn("l6-drawer (claude)", summary)
        self.assertIn("l6-coder (codex)", summary)
        self.assertIn("| 1200 |", summary)


class HerdrCommands(unittest.TestCase):
    """The real runner's command lines, against a stand-in ``herdr`` on its own PATH (never the real one)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bench-herdr-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "argv.jsonl"
        script = self.tmp / "herdr"
        script.write_text("#!{}\nimport json, sys\n"
                          "open({!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                          "if sys.argv[1:3] == ['agent', 'get']:\n"
                          "    print(json.dumps({{'id': 'cli:agent:get', 'result': {{'type': 'agent_info', 'agent': {{'agent_status': 'working'}}}}}}))\n"
                          "if sys.argv[1:3] == ['pane', 'read']:\n"
                          "    print('  \\u26a0 Usage limit reached \\u00b7 limit resets 4pm')\n"
                          "if sys.argv[1:3] == ['agent', 'fail']:\n"
                          "    sys.exit(1)\n".format(sys.executable, os.fspath(self.log)), encoding="utf-8")
        script.chmod(0o755)
        self.env = {"PATH": "{}:/usr/bin:/bin".format(self.tmp)}

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_prompt_delivery_and_status(self):
        runner = B.HerdrRunner("l6", "prompt", env=self.env)
        runner.deliver("l6-drawer", "w1:p2", "Draw it. " + B.DELIVERY_TAIL)
        self.assertEqual(runner.status("w1:p2"), "working")
        self.assertEqual(self.calls(), [["agent", "prompt", "w1:p2", "Draw it. " + B.DELIVERY_TAIL], ["agent", "get", "w1:p2"]])

    def test_post_delivery_types_the_operators_command(self):
        runner = B.HerdrRunner("l6", "post", shell_pane="w1:p1", env=self.env)
        runner.deliver("l6-coder", "w1:p3", "Chart the operator's revenue.")
        (text, keys) = self.calls()
        self.assertEqual(text[:4], ["pane", "send-text", "w1:p1", "herdr-synapse --team l6 post '@l6-coder Chart the operator'\"'\"'s revenue.'"])
        self.assertEqual(keys, ["pane", "send-keys", "w1:p1", "enter"])
        with self.assertRaises(B.BenchError):
            B.HerdrRunner("l6", "post", env=self.env)

    def test_a_failing_status_is_unknown(self):
        runner = B.HerdrRunner("l6", env=self.env)
        runner._run = lambda argv, timeout=30.0: (_ for _ in ()).throw(B.BenchError("down"))
        self.assertEqual(runner.status("w1:p2"), "unknown")

    def test_the_pane_is_read_unwrapped_and_a_failure_is_no_text(self):
        """The pane read of C2: the tail of the pane, unwrapped, and a notice found in what it prints."""
        runner = B.HerdrRunner("l6", env=self.env)
        text = runner.pane_text("w1:p2")
        self.assertEqual(self.calls(), [["pane", "read", "w1:p2", "--source", "recent-unwrapped", "--lines", str(B.PANE_LINES)]])
        self.assertEqual(B.limit_notice(text)["notice"], "usage limit reached")
        runner._run = lambda argv, timeout=30.0: (_ for _ in ()).throw(B.BenchError("no such pane"))
        self.assertEqual(runner.pane_text("w1:p9"), "")  # reading a pane must never be able to fail a run


#: The Claude pane of 2026-09-29, verbatim, quota-blocked while ``herdr agent get`` reported the turn as ``done``
#: (QA phase 6, finding C2). The U+00A0 after the box-drawing glyph and the mixed capitalisation of the notice are
#: the pane's own, and both are why matching folds whitespace and ignores case.
QUOTA_PANE = (
    "❯ Probe only: reply with the single word ready and nothing else. Do not use\n"
    "  any tools.\n"
    "  ⎽ You've hit your weekly limit · resets 4pm (Asia/Jerusalem)\n"
    "\n"
    "⏺ Usage limit reached · continuing automatically at 4pm · esc to\n"
    "  cancel\n"
    "\n"
    "  ⚠ Usage limit reached · limit resets 4pm\n"
    "    Continuing automatically at 4pm · esc to cancel · /usage-credits to\n"
    "    continue now\n"
)


class LimitNotices(unittest.TestCase):
    """Reading a harness refusal out of a pane. The only input is text, so these are plain unit tests."""

    def test_the_strings_the_rig_actually_printed(self):
        for text in ("You've hit your weekly limit · resets 4pm (Asia/Jerusalem)", "⚠ Usage limit reached",
                     "  ⚠ Usage limit reached · limit resets 4pm", "    Continuing automatically at 4pm · esc to cancel"):
            with self.subTest(text=text):
                found = B.limit_notice(text)
                self.assertIsNotNone(found)
                self.assertIn(found["notice"], B.LIMIT_NOTICES)
                self.assertEqual(found["lines"], [text.rstrip()])  # verbatim, indentation and glyphs included

    def test_the_whole_pane_gives_the_notice_and_its_own_line(self):
        found = B.limit_notice(QUOTA_PANE)
        self.assertEqual(found["notice"], "you've hit your weekly limit")
        self.assertIn("You've hit your weekly limit", found["line"])
        self.assertEqual(len(found["lines"]), 1)

    def test_a_notice_split_by_the_panes_wrapping_is_still_found(self):
        found = B.limit_notice("some reply\nContinuing\nautomatically at 4pm · esc to cancel")
        self.assertEqual(found["notice"], "continuing automatically at")
        self.assertEqual(found["lines"], ["Continuing", "automatically at 4pm · esc to cancel"])

    def test_a_typographic_apostrophe_reads_the_same(self):
        self.assertEqual(B.limit_notice("You’ve hit your weekly limit")["notice"], "you've hit your weekly limit")

    def test_an_ordinary_pane_matches_nothing(self):
        for text in ("", "\n\n", "Draw our checkout flow: shopper, cart, checkout API, payment approved.",
                     "canvas check: 0 problems\n❯ done"):
            with self.subTest(text=text):
                self.assertIsNone(B.limit_notice(text))

    def test_pane_read_output_in_either_shape(self):
        self.assertEqual(B.pane_text_of("plain\nlines"), "plain\nlines")
        self.assertEqual(B.pane_text_of('{"result": {"lines": ["a", "b"]}}'), "a\nb")
        self.assertEqual(B.pane_text_of('{"text": "a\\nb"}'), "a\nb")
        self.assertEqual(B.pane_text_of("{not json"), "{not json")
        self.assertEqual(B.pane_text_of(None), "")


class NoAttempt(unittest.TestCase):
    """A live turn that produced nothing is classified, not scored (QA phase 6, C2).

    Every path runs through ``run_live`` with the fake runner and its fake clock, so there is no agent, no Herdr and
    no wall-clock assertion anywhere: an empty answer is a member that drew nothing, and a subclass decides what its
    pane says and what ``agent get`` would report.
    """

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="bench-noattempt-"))
        self.addCleanup(shutil.rmtree, self.out, True)
        self.prompts = B.load_prompts("all")

    def one(self, pid="09-kanban-launch", answers=None, pane_texts=None, runner_class=None, timeout=420.0):
        """One live row for one member, and the runner that served it."""
        with Q.QaTeam() as qa:
            B._qa_artifacts(qa)
            prompt = B.prompt_by_id(self.prompts, pid)
            pane = str(B._roster_member(qa.team, Q.MEMBER)["pane_id"])
            runner = (runner_class or B.FakeRunner)(qa.layout, qa.team, self.prompts, answers=answers,
                                                   pane_texts={pane: pane_texts} if pane_texts else None)
            results = B.run_live(qa.layout, qa.team, [prompt], self.prompts, [Q.MEMBER], runner, self.out,
                                 timeout=timeout, pictures=False, env=dict(qa.env))
            return results, runner, pane

    def test_a_quota_notice_is_a_no_attempt_row_and_not_a_failure(self):
        results, runner, pane = self.one(answers={"09-kanban-launch": []}, pane_texts=QUOTA_PANE)
        row = results["rows"][0]
        self.assertEqual(row["outcome"], B.OUTCOME_NO_ATTEMPT)
        self.assertFalse(B.attempted(row))
        self.assertEqual(row["no_attempt"]["cause"], "usage_limit")
        self.assertIn("You've hit your weekly limit", "\n".join(row["no_attempt"]["evidence"]))  # verbatim evidence
        self.assertIn("usage or rate-limit notice", row["no_attempt"]["reason"])
        self.assertEqual(row["no_attempt"]["pane_source"], "recent-unwrapped")
        self.assertTrue(row["no_attempt"]["pane_text_read"])
        # Nothing was drawn, so nothing is reported as measured: None everywhere, never 0 and never False.
        for key in ("pass", "valid", "valid_first_try", "hard", "soft", "drawn", "readback", "alignment",
                    "crossings", "crossings_total", "stable"):
            with self.subTest(key=key):
                self.assertIsNone(row[key])
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row.get("picture"))
        self.assertEqual(runner.pane_reads, [pane])
        self.assertEqual(results["no_attempt"], [{"id": "09-kanban-launch", "member": Q.MEMBER, "kind": "claude",
                                                  "cause": "usage_limit", "reason": row["no_attempt"]["reason"],
                                                  "evidence": row["no_attempt"]["evidence"]}])
        self.assertIn("NOT MEASURED", B.summary_line(row))
        self.assertIn("usage_limit", B.summary_line(row))

    def test_a_timeout_says_timeout(self):
        class Stuck(B.FakeRunner):
            def status(self, pane):
                return "working"

        results, _runner, _pane = self.one(answers={"09-kanban-launch": []}, runner_class=Stuck, timeout=30.0)
        row = results["rows"][0]
        self.assertEqual(row["no_attempt"]["cause"], "timeout")
        self.assertTrue(row["wait"]["timed_out"])
        self.assertIn("never finished", row["no_attempt"]["reason"])
        self.assertIsNone(row["pass"])

    def test_an_agent_that_never_wakes_up_is_not_a_model_that_drew_nothing(self):
        class Asleep(B.FakeRunner):
            def status(self, pane):
                return "done"  # what Herdr reports for a quota-blocked Claude pane (C2)

        results, _runner, _pane = self.one(answers={"09-kanban-launch": []}, runner_class=Asleep)
        never = results["rows"][0]["no_attempt"]
        self.assertEqual(never["cause"], "never_started")
        self.assertFalse(never["worked"])
        self.assertIn("could not be read", never["reason"])  # no pane text: a limit notice is not ruled out

        settled, _runner, _pane = self.one(answers={"09-kanban-launch": []}, pane_texts="canvas check: 0 problems\ndone")
        nothing = settled["rows"][0]["no_attempt"]
        self.assertEqual(nothing["cause"], "drew_nothing")
        self.assertTrue(nothing["worked"])
        self.assertIn("model's own outcome", nothing["reason"])

    def test_a_refused_batch_is_still_an_attempt(self):
        """A batch refused as a whole leaves no mark on the board, but it is a call the model made: a measurement."""
        if not HAS_TRACE:
            self.skipTest(TRACE_REASON)
        results, runner, _pane = self.one(answers={"09-kanban-launch": [{"op": "kanbann", "id": "x", "intent": "typo"}]},
                                          pane_texts=QUOTA_PANE)
        row = results["rows"][0]
        self.assertEqual(row["outcome"], B.OUTCOME_SCORED)
        self.assertIs(row["valid_first_try"], False)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(runner.pane_reads, [])  # a row with a call never reads the pane

    def test_a_drawing_is_scored_exactly_as_before(self):
        results = B.run_fake([B.prompt_by_id(self.prompts, "09-kanban-launch")], self.prompts, self.out)
        self.assertEqual(results["no_attempt"], [])
        for row in results["rows"]:
            with self.subTest(row["member"]):
                self.assertEqual(row["outcome"], B.OUTCOME_SCORED)
                self.assertTrue(B.attempted(row))
                self.assertTrue(row["pass"], row["reasons"])
                self.assertEqual(row["alignment"], {"drawn": 1.0, "readback": 1.0})
                self.assertEqual(sum(row["hard"].values()), 0)
                self.assertNotIn("no_attempt", row)
        totals = B.aggregate(results["rows"])
        self.assertEqual((totals["rows"], totals["scored"], totals["no_attempt"]), (2, 2, 0))
        self.assertEqual((totals["pass_rate"], totals["hard_total"]), (1.0, 0))
        self.assertTrue(totals["measured"])

    def test_offline_rows_are_always_scored_rows(self):
        """Offline has no harness to refuse a turn: a reference that draws nothing is a scorer finding, not a
        no-attempt row, and the gate must keep failing it."""
        results = B.run_offline([B.prompt_by_id(self.prompts, "11-table-vendors")], self.prompts, "v2", controls=B.load_controls())
        for row in results["rows"] + results["controls"]:
            with self.subTest(row["id"]):
                self.assertEqual(row["outcome"], B.OUTCOME_SCORED)
        self.assertTrue(results["ok"])


class RatesWithoutNoAttemptRows(unittest.TestCase):
    """The aggregates and the summary over a mix of scored and no-attempt rows."""

    @staticmethod
    def row(**fields):
        base = {"id": "01-flow-checkout", "mode": "live", "member": "l6-coder", "kind": "codex", "outcome": B.OUTCOME_SCORED,
                "pass": True, "valid": True, "valid_first_try": True, "hard": {"overlap": 0, "arrow_through": 0, "label_overflow": 0},
                "crossings": 0, "block_crossings": 0, "crossings_total": 0, "alignment": {"drawn": 1.0, "readback": 1.0},
                "seconds": 40.0, "tokens": {"total": 100}}
        base.update(fields)
        return base

    @classmethod
    def missing(cls, cause="usage_limit", **fields):
        facts = {"cause": cause, "reason": "why", "evidence": ["⚠ Usage limit reached · limit resets 4pm"],
                 "pane_source": "recent-unwrapped", "pane_text_read": True}
        row = dict(cls.row(), **B.unscored_row(facts))
        row.update({"id": "07-mind-launch", "mode": "live", "member": "l6-drawer", "kind": "claude", "seconds": 95.0,
                    "tokens": {"total": None, "reason": "no Claude transcript"}})
        row.update(fields)
        return row

    def test_every_rate_is_over_the_rows_that_were_attempted(self):
        rows = [self.row(), self.row(id="04-arch-web", **{"pass": False}), self.missing(), self.missing(id="09-kanban-launch")]
        totals = B.aggregate(rows)
        self.assertEqual((totals["rows"], totals["scored"], totals["no_attempt"]), (4, 2, 2))
        self.assertEqual(totals["no_attempt_causes"], {"usage_limit": 2})
        self.assertEqual(totals["pass_rate"], 0.5)  # 1 of the 2 attempted, not 1 of 4
        self.assertEqual(totals["first_try_rate"], 1.0)
        self.assertEqual(totals["hard_total"], 0)
        self.assertEqual(totals["median_seconds"], 40.0)  # the 95 s of a refused turn does not move the median
        self.assertEqual(totals["total_tokens"], 200)  # tokens are spend, and spend counts even for a refused turn
        self.assertTrue(totals["measured"])

    def test_a_group_with_nothing_attempted_reports_no_figures_at_all(self):
        totals = B.aggregate([self.missing(), self.missing(id="09-kanban-launch", cause="timeout")])
        self.assertFalse(totals["measured"])
        self.assertEqual((totals["rows"], totals["scored"], totals["no_attempt"]), (2, 0, 2))
        self.assertEqual(totals["no_attempt_causes"], {"usage_limit": 1, "timeout": 1})
        for key in ("pass_rate", "first_try_rate", "hard_total", "mean_crossings", "mean_drawn_alignment",
                    "mean_readback_alignment", "median_seconds"):
            with self.subTest(key=key):
                self.assertIsNone(totals[key], "{} must not read as a measurement".format(key))

    def test_rows_from_a_run_before_outcome_existed_still_count(self):
        old = self.row()
        del old["outcome"]
        self.assertTrue(B.attempted(old))
        self.assertEqual(B.aggregate([old])["scored"], 1)

    def test_the_summary_says_a_kind_was_not_measured_instead_of_scoring_it_zero(self):
        rows = [self.row(), self.row(id="04-arch-web", **{"pass": False}),
                self.missing(), self.missing(id="09-kanban-launch"), self.missing(id="11-table-vendors", cause="timeout")]
        text = B.summary_markdown({"rows": rows, "controls": []})
        self.assertIn("**3 of 5 rows were not attempted (usage_limit 2, timeout 1), and are left out of every rate below.**", text)
        self.assertIn("## Not measured", text)
        self.assertIn("⚠ Usage limit reached · limit resets 4pm", text)  # the pane's own words reach the report
        self.assertIn("**l6-drawer (claude) was not measured.**", text)
        claude = [line for line in text.splitlines() if line.startswith("| l6-drawer (claude) |")]
        self.assertEqual(len(claude), 1, claude)
        self.assertIn("**not measured**", claude[0])
        self.assertNotIn("0.00", claude[0])  # a zero pass rate for a kind nobody measured is the trap
        codex = next(line for line in text.splitlines() if line.startswith("| l6-coder (codex) |"))
        self.assertIn("| 0.50 |", codex)
        # The no-attempt rows are out of the failure list too: they are not failed drawings.
        failures = text.split("## Failures", 1)[1]
        self.assertIn("04-arch-web", failures)
        self.assertNotIn("07-mind-launch", failures)
        table = text.split("## Live runs", 1)[1].split("### Aggregates", 1)[0]
        for row_line in [line for line in table.splitlines() if line.startswith("| 07-mind-launch |")]:
            self.assertIn("no attempt: usage_limit", row_line)
            self.assertNotIn("| yes |", row_line)

    def test_a_merged_report_carries_the_index(self):
        out = Path(tempfile.mkdtemp(prefix="bench-merge-"))
        self.addCleanup(shutil.rmtree, out, True)
        B.write_outputs({"rows": [self.row()], "controls": []}, out / "one")
        B.write_outputs({"rows": [self.missing()], "controls": []}, out / "two")
        merged = B.merge_results([out / "one", out / "two"])
        self.assertEqual([e["cause"] for e in merged["no_attempt"]], ["usage_limit"])
        self.assertEqual(len(merged["rows"]), 2)

class Tokens(unittest.TestCase):
    """Tokens from the member's own harness files, only inside the window, read-only."""

    def setUp(self):
        self.qa = Q.QaTeam()
        self.addCleanup(self.qa.cleanup)
        self.home = Path(self.qa.env["HOME"])
        self.env = dict(self.qa.env)

    def set_member(self, name, **fields):
        doc = store.read_json(self.qa.team.team_json)
        for row in doc["members"]:
            if row["name"] == name:
                row.update(fields)
        store.write_json(self.qa.team.team_json, doc)

    @staticmethod
    def iso(ts):
        return C._iso(ts)

    def test_claude_transcript_usage_in_the_window(self):
        transcript = self.home / "t.jsonl"
        lines = [
            {"timestamp": self.iso(900.0), "message": {"id": "m0", "usage": {"input_tokens": 999, "output_tokens": 999}}},
            {"timestamp": self.iso(1001.0), "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}}},
            {"timestamp": self.iso(1002.0), "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}}},
            {"timestamp": self.iso(1003.0), "message": {"id": "m2", "usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3}}},
        ]
        transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        record = self.qa.layout.session.pane_record("term_qa")
        record.parent.mkdir(parents=True, exist_ok=True)
        store.write_json(record, {"transcript_path": os.fspath(transcript)})
        got = B.member_tokens(self.qa.layout, self.qa.team, Q.MEMBER, 1000.0, 1100.0, self.env)
        self.assertEqual(got, {"input": 11, "output": 7, "cache": 103, "total": 121, "source": "claude transcript"})

    def test_codex_rollout_delta(self):
        sid = "019a0000-0000-7000-8000-000000000001"
        rollout = self.home / ".codex" / "sessions" / "2026" / "09" / "28" / "rollout-2026-09-28T10-00-00-{}.jsonl".format(sid)
        rollout.parent.mkdir(parents=True, exist_ok=True)

        def count(ts, total):
            return {"timestamp": self.iso(ts), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": total, "cached_input_tokens": total // 2, "output_tokens": total // 10, "total_tokens": total + total // 10}}}}

        rollout.write_text("\n".join(json.dumps(x) for x in (count(900, 1000), count(1050, 1500), count(1080, 2000), count(1200, 9000))) + "\n",
                           encoding="utf-8")
        self.set_member(Q.PEER, session={"value": sid})
        got = B.member_tokens(self.qa.layout, self.qa.team, Q.PEER, 1000.0, 1100.0, self.env)
        self.assertEqual(got, {"input": 1000, "output": 100, "cache": 500, "total": 1100, "source": "codex rollout"})

    def test_opencode_messages_in_the_window(self):
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(os.fspath(db))
        conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
        rows = [("a", "ses_1", 900000, {"role": "assistant", "tokens": {"input": 50, "output": 50}}),
                ("b", "ses_1", 1010000, {"role": "assistant", "tokens": {"input": 20, "output": 4, "reasoning": 1, "cache": {"read": 7, "write": 3}}}),
                ("c", "ses_1", 1020000, {"role": "user"}),
                ("d", "ses_2", 1030000, {"role": "assistant", "tokens": {"input": 99}})]
        conn.executemany("INSERT INTO message VALUES (?, ?, ?, ?)", [(i, s, t, json.dumps(d)) for i, s, t, d in rows])
        conn.commit()
        conn.close()
        self.set_member(Q.PEER, kind="opencode", session={"value": "ses_1"})
        got = B.member_tokens(self.qa.layout, self.qa.team, Q.PEER, 1000.0, 1100.0, self.env)
        self.assertEqual(got, {"input": 20, "output": 5, "cache": 10, "total": 35, "source": "opencode.db"})

    def test_an_unreadable_kind_says_why(self):
        self.set_member(Q.PEER, kind="gemini")
        got = B.member_tokens(self.qa.layout, self.qa.team, Q.PEER, 0.0, 1.0, self.env)
        self.assertIsNone(got["total"])
        self.assertIn("gemini", got["reason"])


if __name__ == "__main__":
    unittest.main()
