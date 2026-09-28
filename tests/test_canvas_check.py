"""The canvas layout check (0.21): each problem kind on plain elements, fixes that apply and converge on a
real canvas, and the three doors to it (``canvas check``, the MCP ``canvas_check`` tool, ``look``)."""
from __future__ import annotations

import json
import unittest

from test_canvas import WORKER, CanvasRig
from test_canvas_mcp import McpRig
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli
from support import FAKE_AGENTS

from herdr_team import canvas as C
from herdr_team import canvas_check as K


def el(eid, kind, x, y, w, h, text="", seq=1, author="alpha-worker", **extra):
    out = {"id": eid, "type": kind, "x": x, "y": y, "w": w, "h": h, "text": text, "created_seq": seq, "author": author,
           "style": {"size": 20}, "frame": None}
    out.update(extra)
    return out


def codes(found):
    return [(p["code"], p["ids"]) for p in found]


class Checks(unittest.TestCase):
    def test_overlap_names_the_later_mark_and_moves_it_to_a_free_spot(self):
        a = el("E-1", "box", 0, 0, 160, 80, "A", seq=1)
        b = el("E-2", "box", 100, 40, 160, 80, "B", seq=2)
        [problem] = K.problems([a, b], "alpha-worker")
        self.assertEqual((problem["code"], problem["ids"], problem["yours"]), ("overlap", ["E-2", "E-1"], True))
        self.assertIn("overlaps E-1 box \"A\" by 60x40", problem["message"])
        fix = problem["fix"]
        self.assertEqual((fix["op"], fix["id"]), ("move", "E-2"))
        moved = dict(b, x=fix["to"][0], y=fix["to"][1])
        self.assertEqual(K.problems([a, moved]), [], "the suggested spot is free")
        self.assertTrue(fix["intent"].startswith("fix the layout: overlap"))

    def test_touching_edges_and_one_mark_inside_another_are_not_overlaps(self):
        self.assertEqual(K.problems([el("E-1", "box", 0, 0, 100, 100), el("E-2", "box", 98, 0, 100, 100)]), [])
        panel = el("E-1", "box", 0, 0, 400, 400)
        self.assertEqual(K.problems([panel, el("E-2", "note", 20, 20, 160, 100, "inside")]), [], "a grouping, not a problem")

    def test_text_on_a_labelled_shape(self):
        box = el("E-1", "box", 0, 0, 200, 100, "walls")
        text = el("E-2", "text", 20, 30, 80, 25, "again", seq=2)
        self.assertEqual(codes(K.problems([box, text])), [("text_on_label", ["E-2", "E-1"])])
        self.assertEqual(K.problems([dict(box, text=""), text]), [], "text on an unlabelled shape is its label")

    def test_a_label_bigger_than_its_shape_gets_the_size_it_needs(self):
        # A box drawn before 0.22 at a fixed size too small for its label (0.22 shapes grow to fit when drawn).
        door = el("E-1", "box", 0, 0, 40, 60, "door")
        [problem] = K.problems([door])
        self.assertEqual(problem["code"], "label_overflow")
        need = K.label_room(door)
        self.assertEqual({k: problem["fix"][k] for k in ("id", "w", "h")}, {"id": "E-1", "w": need[0], "h": 60})
        self.assertGreater(need[0], 40)
        self.assertIsNone(K.label_room(dict(door, w=need[0])))
        self.assertIsNone(K.label_room(dict(door, w=need[0], h=60, fit={"policy": "hug", "min": [40, 60], "size": 20, "lines": ["door"]})))
        sun = el("E-2", "ellipse", 0, 0, 60, 60, "the warm afternoon sun")
        need = K.label_room(sun)
        self.assertIsNone(K.label_room(dict(sun, w=need[0], h=need[1])), "the size it asks for is enough")

    def test_frame_edge_grows_the_frame_or_moves_the_mark_in(self):
        frame = el("E-1", "frame", 100, 100, 400, 300, "Home")
        low = el("E-2", "text", 280, 370, 100, 50, "A cozy home", frame=None, wrap=True)
        [problem] = K.problems([frame, low])
        self.assertEqual(problem["code"], "frame_edge")
        self.assertIn("past its bottom edge", problem["message"])
        self.assertIn("grow the frame to 400x340", problem["message"])
        self.assertEqual({k: problem["fix"][k] for k in ("id", "w", "h")}, {"id": "E-1", "w": 400, "h": 340})
        mostly_out = dict(low, y=380)
        self.assertEqual({k: K.problems([frame, mostly_out])[0]["fix"][k] for k in ("id", "inside")}, {"id": "E-2", "inside": "E-1"})
        left = el("E-3", "box", 60, 200, 100, 60, "L")
        [problem] = K.problems([frame, left])
        self.assertEqual({k: problem["fix"][k] for k in ("id", "inside")}, {"id": "E-3", "inside": "E-1"})
        self.assertEqual(K.problems([frame, el("E-4", "box", 0, 0, 900, 900)]), [], "a mark around the whole frame is not half in it")
        portrait = dict(frame, id="E-5", role="portrait")
        self.assertEqual(K.problems([portrait, low]), [], "a plan portrait is not a frame to stay inside")

    def test_an_arrow_through_a_mark_it_does_not_connect(self):
        a = el("E-1", "box", 0, 0, 100, 60, "A")
        b = el("E-2", "box", 400, 0, 100, 60, "B")
        wall = el("E-3", "box", 200, -20, 80, 100, "wall")
        arrow = el("E-4", "arrow", 100, 30, 300, 1, "", points=[[100, 30], [400, 30]], **{"from": "E-1", "to": "E-2"})
        self.assertEqual(codes(K.problems([a, b, wall, arrow])), [("arrow_through", ["E-4", "E-3"])])
        around = dict(arrow, points=[[100, 30], [150, 30], [150, 140], [400, 140], [400, 30]], h=110)
        self.assertEqual(K.problems([a, b, wall, around]), [], "routed around it")

    def test_a_stray_mark_needs_company(self):
        near = [el("E-{}".format(i), "box", i * 200, 0, 160, 80, "n{}".format(i)) for i in range(1, 4)]
        far = el("E-9", "note", 5000, 5000, 160, 100, "far")
        self.assertEqual(codes(K.problems(near + [far])), [("stray", ["E-9"])])
        self.assertEqual(K.problems(near[:2] + [far]), [], "too few marks to call one stray")

    def test_order_region_and_the_readers_own_first(self):
        mine = [el("E-1", "box", 0, 0, 160, 80, "A"), el("E-2", "box", 100, 40, 160, 80, "B", seq=2)]
        theirs = [el("E-3", "box", 1000, 0, 160, 80, "C", author="alpha-reviewer"),
                  el("E-4", "box", 1100, 40, 160, 80, "D", seq=4, author="alpha-reviewer")]
        door = el("E-5", "box", 1000, 400, 40, 60, "door", author="alpha-reviewer")
        found = K.problems(theirs + [door] + mine, "alpha-worker")
        self.assertEqual([p["code"] for p in found], ["overlap", "overlap", "label_overflow"])
        self.assertEqual(found[0]["ids"], ["E-2", "E-1"], "the reader's own come first within a kind")
        self.assertEqual(codes(K.problems(theirs + mine, region=[0, 0, 400, 200])), [("overlap", ["E-2", "E-1"])])
        lines = K.problem_lines(found, limit=1)
        self.assertTrue(lines[0].startswith("  - overlap: E-2"))
        self.assertTrue(lines[1].startswith('    fix: {"op":"move","id":"E-2"'))
        self.assertEqual(lines[-1], "  … 2 more: herdr-synapse canvas check")


HOUSE = [
    {"op": "frame", "id": "home", "title": "Home", "at": [100, 100], "w": 400, "h": 300, "intent": "scene"},
    {"op": "shape", "kind": "box", "text": "walls", "at": [160, 240], "w": 120, "h": 100, "intent": "walls"},
    {"op": "shape", "kind": "box", "text": "door", "at": [240, 330], "w": 40, "h": 60, "intent": "door"},
    {"op": "shape", "kind": "ellipse", "text": "the warm afternoon sun", "at": [400, 120], "w": 60, "h": 60, "intent": "sun"},
    {"op": "shape", "kind": "text", "text": "A cozy home", "at": [280, 380], "w": 100, "intent": "label"},
    {"op": "shape", "kind": "text", "text": "walls again", "at": [170, 270], "intent": "text on the walls"},
]


class OnACanvas(CanvasRig):
    def test_applying_the_fixes_one_by_one_converges_to_a_clean_canvas(self):
        # The live test's house (2026-09-26/27), with the problems Haiku left in it and a few more.
        self.apply(HOUSE)
        first = C.check(self.layout, self.team, WORKER.name)["problems"]
        # 0.22: every shape was sized for its label when drawn, so none overflows; what is left is placement.
        # The sun that grew into the walls moved to the nearest free spot and its frame grew to hold it (QA F-2),
        # so nothing sticks out of the frame any more.
        self.assertEqual(sorted({p["code"] for p in first}), ["overlap", "text_on_label"])
        for _ in range(10):
            found = C.check(self.layout, self.team, WORKER.name)["problems"]
            fixes = [p["fix"] for p in found if p["fix"]]
            if not fixes:
                break
            self.ok(fixes[0])
        self.assertEqual(C.check(self.layout, self.team, WORKER.name)["problems"], [])
        self.assertIn("no layout problems", C.check(self.layout, self.team, WORKER.name)["text"])

    def test_check_scopes_and_look_ends_with_the_list(self):
        self.apply(HOUSE)
        result = C.check(self.layout, self.team, WORKER.name, around="E-4")
        self.assertIn("overlap", {p["code"] for p in result["problems"]})
        self.assertEqual(C.check(self.layout, self.team, WORKER.name, region="c100r100:c110r110")["problems"], [], "nothing out there")
        self.assertIn("apply a fix: herdr-synapse canvas draw --op", result["text"])
        self.assertEqual(C.check(self.layout, self.team, "alpha-reviewer", mine=True)["problems"], [])
        text = C.look(self.layout, self.team, WORKER.name)["text"]
        self.assertIn("problems (", text)
        self.assertIn("each fix is an operation you can apply", text)


class Doors(CanvasRig):
    def setUp(self):
        super().setUp()
        self.api = live_api(list(FAKE_AGENTS))

    def test_the_cli(self):
        self.apply(HOUSE)
        code, payload, err = json_out(run_cli(["--json", "canvas", "check", "--mine"], env_no_daemon(self.ts, HERDR_PANE_ID="w2:p2"), self.api))
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["mine"])
        self.assertTrue(payload["problems"])
        fix = next(p["fix"] for p in payload["problems"] if p["fix"])
        code, _out, err = run_cli(["canvas", "draw", "--op", json.dumps(fix)], env_no_daemon(self.ts, HERDR_PANE_ID="w2:p2"), self.api)
        self.assertEqual(code, 0, err)


class Mcp(McpRig):
    def test_the_tool(self):
        self.apply(HOUSE)
        tools = [t["name"] for t in self.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
        self.assertIn("canvas_check", tools)
        result = self.tool("canvas_check", {"mine": True})
        self.assertIn("overlap", result["content"][0]["text"])
        self.assertTrue(result["structuredContent"]["problems"])


class ArrowThroughFix(CanvasRig):
    """Phase 2 (3.4): a bound arrow through a mark gets a ready fix, a route around it; a free arrow is moved by hand."""

    def test_the_fix_reroutes_a_bound_arrow(self):
        a = self.ok({"op": "shape", "text": "A", "at": [0, 0], "intent": "t"})["ids"][0]
        b = self.ok({"op": "shape", "text": "B", "at": [800, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "shape", "text": "in the way", "at": [400, 0], "intent": "t"})
        arrow = self.ok({"op": "arrow", "from": a, "to": b, "intent": "t"})["ids"][0]
        free = self.ok({"op": "arrow", "points": [[300, 40], [700, 40]], "intent": "t"})["ids"][0]
        problems = [p for p in C.check(self.layout, self.team, "alpha-worker")["problems"] if p["code"] == "arrow_through"]
        bound = next(p for p in problems if p["ids"][0] == arrow)
        self.assertEqual({k: bound["fix"][k] for k in ("op", "ids", "route")}, {"op": "restyle", "ids": [arrow], "route": "orthogonal"})
        self.assertTrue(bound["fix"]["intent"].startswith("route around"))
        self.assertIsNone(next(p for p in problems if p["ids"][0] == free)["fix"])
        self.ok(bound["fix"])
        self.assertEqual(self.el(arrow)["style"]["route"], "orthogonal")

    def test_route_applies_to_arrows_only(self):
        eid = self.ok({"op": "shape", "text": "A", "at": [0, 0], "intent": "t"})["ids"][0]
        refused = self.refused({"op": "restyle", "id": eid, "route": "orthogonal", "intent": "t"})
        self.assertIn("route applies to arrows", refused["message"])


if __name__ == "__main__":
    unittest.main()
