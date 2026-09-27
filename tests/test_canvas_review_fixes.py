"""Regressions for the 0.21 whiteboard review: undo authority and dangling references, frame growth,
moves, graph input, force layout, chart nesting, log growth, SVG ``<use>`` expansion, purge, Pi launch
flags, the portrait's off switch, and watch redaction."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from support import FAKE_AGENTS, TempState  # noqa: F401  (TempState keeps support's harness fake installed)
from test_canvas import OPERATOR, REVIEWER, WORKER, CanvasRig
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

from herdr_team import activity as A
from herdr_team import canvas as C
from herdr_team import canvas_layout as L
from herdr_team import canvas_render as R
from herdr_team import features as F
from herdr_team import models
from herdr_team.errors import HerdrTeamError


def svg(body: str) -> str:
    return "<svg xmlns='http://www.w3.org/2000/svg' xmlns:xlink='http://www.w3.org/1999/xlink' viewBox='0 0 100 100'>{}</svg>".format(body)


# --------------------------------------------------------------------------
# undo


class Undo(CanvasRig):
    def test_undo_never_reverts_an_element_the_author_cannot_edit(self):
        self.ok({"op": "shape", "text": "one", "at": "c0r0", "intent": "t"})
        self.ok({"op": "shape", "text": "two", "at": "c20r0", "intent": "t"})
        self.ok({"op": "arrow", "from": "E-1", "to": "E-2", "label": "operator says ok"}, OPERATOR)
        moved = self.apply([{"op": "move", "id": "E-1", "by": [0, 100], "intent": "t"}])
        self.assertIn("E-3", moved["applied"][0]["ids"], "the operator's bound arrow re-routed with the move")
        self.ok({"op": "edit", "id": "E-3", "text": "OPERATOR: DO NOT SHIP"}, OPERATOR)
        self.assertEqual(self.refused({"op": "edit", "id": "E-3", "text": "x", "intent": "t"})["code"], "element_not_yours")
        result = self.apply([{"op": "undo", "batch": moved["batch"], "intent": "take it back"}])
        self.assertEqual(result["refused"], [])
        self.assertEqual((self.el("E-1")["x"], self.el("E-1")["y"]), (0, 0), "the worker's own element is restored")
        arrow = self.el("E-3")
        self.assertEqual((arrow["text"], arrow["author"]), ("OPERATOR: DO NOT SHIP", "human"), "the operator's later edit survives")
        self.assertEqual(arrow["points"][0][1], 40, "the arrow still follows the restored end")
        self.assertEqual([w["code"] for w in result["warnings"]], ["undo_skipped"])
        self.assertEqual(result["warnings"][0]["ids"], ["E-3"])

    def test_undo_unbinds_what_pointed_at_the_elements_it_deletes(self):
        first = self.apply([{"op": "frame", "title": "F", "at": "c0r0", "w": 400, "h": 300, "intent": "t"},
                            {"op": "shape", "text": "src", "at": "c40r0", "intent": "t"}])
        self.apply([{"op": "shape", "text": "in", "inside": "E-1", "w": 100, "h": 40, "intent": "t"},
                    {"op": "arrow", "from": "E-2", "to": "E-3", "intent": "t"},
                    {"op": "comment", "at": "E-2", "text": "why?", "intent": "t"}], REVIEWER)
        self.assertEqual(self.el("E-3")["frame"], "E-1")
        points = self.el("E-4")["points"]
        self.ok({"op": "undo", "batch": first["batch"], "intent": "t"})
        ids = [e["id"] for e in self.scene()["elements"]]
        self.assertNotIn("E-1", ids)
        self.assertNotIn("E-2", ids)
        self.assertIsNone(self.el("E-3")["frame"])
        arrow = self.el("E-4")
        self.assertEqual((arrow["from"], arrow["to"], arrow["points"]), (None, "E-3", points), "the end unbinds and keeps its points")
        self.assertIsNone(self.el("C-1")["on"])
        self.assertNotIn("E-2", C.look_text(C.look(self.layout, self.team, "alpha-worker")))


# --------------------------------------------------------------------------
# placement and moves


class FramesAndMoves(CanvasRig):
    def test_placing_inside_someone_elses_frame_never_grows_it(self):
        frame = self.ok({"op": "frame", "title": "ops", "at": "c0r0", "w": 200, "h": 120}, OPERATOR)["ids"][0]
        refusal = self.refused({"op": "shape", "w": 400, "h": 400, "inside": frame, "intent": "t"})
        self.assertEqual(refusal["code"], "element_not_yours")
        self.assertEqual((self.el(frame)["w"], self.el(frame)["h"]), (200, 120))
        small = self.ok({"op": "shape", "w": 100, "h": 40, "inside": frame, "intent": "t"})["ids"][0]
        self.assertEqual(self.el(small)["frame"], frame, "what fits still joins the frame")
        self.assertEqual((self.el(frame)["w"], self.el(frame)["h"]), (200, 120))
        self.ok({"op": "shape", "w": 400, "h": 400, "inside": frame}, OPERATOR)
        self.assertGreater(self.el(frame)["w"], 200, "the frame's editor may grow it")

    def test_growing_a_frame_into_a_lock_is_refused(self):
        frame = self.ok({"op": "frame", "title": "mine", "at": [0, 0], "w": 200, "h": 120, "intent": "t"})["ids"][0]
        self.ok({"op": "lock", "region": [0, 300, 400, 400]}, OPERATOR)
        refusal = self.refused({"op": "shape", "w": 150, "h": 250, "inside": frame, "intent": "t"})
        self.assertEqual(refusal["code"], "canvas_locked")
        self.assertEqual(self.el(frame)["h"], 120)

    def test_move_inside_a_frame_makes_the_element_its_child(self):
        frame = self.ok({"op": "frame", "title": "F", "at": [0, 0], "w": 400, "h": 300, "intent": "t"})["ids"][0]
        child = self.ok({"op": "shape", "text": "c", "at": [2000, 2000], "intent": "t"})["ids"][0]
        self.ok({"op": "move", "id": child, "inside": frame, "intent": "t"})
        self.assertEqual(self.el(child)["frame"], frame)
        before = self.el(child)["x"]
        self.ok({"op": "move", "id": frame, "by": [100, 0], "intent": "t"})
        self.assertEqual(self.el(child)["x"], before + 100, "the child moves with its frame")
        self.assertEqual(self.refused({"op": "move", "id": frame, "inside": frame, "intent": "t"})["code"], "op_invalid")

    def test_move_by_cannot_leave_the_canvas(self):
        eid = self.ok({"op": "shape", "text": "far", "at": [900000, 0], "intent": "t"})["ids"][0]
        refusal = self.refused({"op": "move", "id": eid, "by": [900000, 0], "intent": "t"})
        self.assertEqual(refusal["code"], "op_invalid")
        self.assertEqual(self.el(eid)["x"], 900000)
        pen = self.ok({"op": "pen", "points": [[999000, 0], [999001, 1]], "intent": "t"})["ids"][0]
        self.assertEqual(self.refused({"op": "move", "id": pen, "w": 20000, "intent": "t"})["code"], "op_invalid")
        self.assertEqual(self.refused({"op": "move", "id": eid, "to": "c99999r0", "intent": "t"})["code"], "op_invalid")


# --------------------------------------------------------------------------
# malformed input


class MalformedInput(CanvasRig):
    def test_a_graph_edge_with_a_non_string_end_refuses_only_that_op(self):
        for edges in ([[["a"], "b"]], [{"from": {"x": 1}, "to": "a"}]):
            with self.subTest(edges=edges):
                result = self.apply([{"op": "shape", "text": "kept", "at": "c0r0", "intent": "t"},
                                     {"op": "graph", "nodes": ["a", "b"], "edges": edges, "intent": "t"}])
                self.assertEqual([a["op"] for a in result["applied"]], ["shape"])
                self.assertEqual(result["refused"][0]["code"], "op_invalid")

    def test_an_unexpected_error_in_one_op_is_that_ops_refusal(self):
        def boom(ctx, op):
            raise TypeError("unhashable type: 'list'")

        with mock.patch.dict(C._HANDLERS, {"legend": boom}):
            result = self.apply([{"op": "shape", "text": "kept", "at": "c0r0", "intent": "t"}, {"op": "legend", "intent": "t"}])
        self.assertEqual([a["op"] for a in result["applied"]], ["shape"])
        self.assertEqual((result["refused"][0]["code"], result["refused"][0]["details"]["error"]), ("op_invalid", "TypeError"))
        self.assertEqual(C.current_version(self.team), 1)

    def test_deeply_nested_batches_are_refused_not_raised(self):
        deep = "[" * 200000 + "]" * 200000  # under MAX_BATCH_BYTES, past any interpreter's recursion limit
        with self.assertRaises(HerdrTeamError) as ctx:
            C.parse_batch('{"ops": [{"op": "chart", "spec": {"x": ' + deep + '}, "intent": "t"}]}')
        self.assertEqual(ctx.exception.code, "op_invalid")
        nested: list = []
        for _ in range(3000):
            nested = [nested]
        try:  # 3.9's encoder stops at its recursion limit (the whole batch); 3.14's does not (the chart's depth check)
            result = self.apply([{"op": "chart", "spec": {"x": nested}, "at": "c0r0", "intent": "t"}])
        except HerdrTeamError as err:
            self.assertEqual(err.code, "op_invalid")
        else:
            self.assertEqual([r["code"] for r in result["refused"]], ["chart_refused"])
        self.assertEqual(C.current_version(self.team), 0)

    def test_a_url_nested_past_the_depth_limit_is_still_refused(self):
        for levels in (1, 31, 32, 40):
            spec = {"mark": "bar", "data": {"url": "/api/team/alpha/scene"}}
            for _ in range(levels):
                spec = {"layer": [spec]}
            with self.subTest(levels=levels):
                self.assertEqual(self.refused({"op": "chart", "spec": spec, "at": "c0r0", "intent": "t"})["code"], "chart_refused")


# --------------------------------------------------------------------------
# force layout


class ForceLayout(CanvasRig):
    def test_isolated_nodes_stay_near_the_rest(self):
        nodes = ["n{}".format(i) for i in range(12)]
        edges = [("n{}".format(i), "n{}".format(i + 1)) for i in range(9)]
        positions = L.layout(nodes, edges, "force")
        width = max(x for x, _ in positions.values()) + L.NODE_W
        height = max(y for _, y in positions.values()) + L.NODE_H
        self.assertLess(max(width, height), 2000, (width, height))
        boxes = [(x, y, x + L.NODE_W, y + L.NODE_H) for x, y in positions.values()]
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                self.assertFalse(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3])
        applied = self.ok({"op": "graph", "nodes": ["m{}".format(i) for i in range(30)],
                           "edges": [["m{}".format(i), "m{}".format(i + 1)] for i in range(24)], "layout": "force", "at": "c0r0", "intent": "t"})
        frame = self.el(applied["ids"][0])
        self.assertLess(max(frame["w"], frame["h"]), 3000)

    def test_force_layout_runs_before_the_canvas_lock(self):
        real = L.layout
        held = []

        def probe(nodes, edges, algorithm="layered", direction="down", **kw):
            if algorithm == "force":
                lock = C._canvas_lock(self.team)
                free = lock.try_acquire()
                if free:
                    lock.release()
                held.append(not free)
            return real(nodes, edges, algorithm, direction, **kw)

        with mock.patch.object(L, "layout", probe):
            self.ok({"op": "graph", "nodes": ["a", "b", "c"], "edges": [["a", "b"]], "layout": "force", "at": "c0r0", "intent": "t"})
        self.assertEqual(held, [False], "the force layout was computed while canvas.lock was free")


# --------------------------------------------------------------------------
# the event log


class LogGrowth(CanvasRig):
    def test_one_agent_op_cannot_write_more_than_the_change_cap(self):
        ids = [self.ok({"op": "shape", "text": "x" * 1500, "at": [0, 200 * i], "intent": "t"})["ids"][0] for i in range(6)]
        with mock.patch.object(C, "MAX_OP_CHANGE_BYTES", 8 * 1024):
            refusal = self.refused({"op": "move", "ids": ids, "by": [20, 0], "intent": "t"})
            self.assertEqual((refusal["code"], refusal["details"]["limit"]), ("canvas_limit", "MAX_OP_CHANGE_BYTES"))
            self.ok({"op": "move", "ids": ids, "by": [20, 0]}, OPERATOR)

    def test_the_byte_budget_per_minute_refuses_a_batch(self):
        ids = [self.ok({"op": "shape", "text": "x" * 1500, "at": [0, 200 * i], "intent": "t"})["ids"][0] for i in range(4)]
        version = C.current_version(self.team)
        with mock.patch.object(C, "MAX_CHANGE_BYTES_PER_MINUTE", 30 * 1024):
            with self.assertRaises(HerdrTeamError) as ctx:
                for _ in range(10):
                    self.apply([{"op": "move", "ids": ids, "by": [20, 0], "intent": "t"}])
        self.assertEqual(ctx.exception.code, "canvas_rate")
        self.assertGreaterEqual(ctx.exception.details["retry_after_s"], 1)
        self.assertLess(C.current_version(self.team), version + 10)

    def test_reading_backwards_is_right_at_every_chunk_size(self):
        self.ok({"op": "shape", "text": "a", "at": "c0r0", "intent": "t"})
        self.ok({"op": "shape", "text": "b" * 1800, "at": "c0r20", "intent": "t"})
        self.ok({"op": "shape", "text": "c", "at": "c0r40", "intent": "t"})
        expected = [e["seq"] for e in reversed(C._read_events(self.team))]
        for chunk in (1, 7, 64, 1000, 65536):
            with self.subTest(chunk=chunk):
                self.assertEqual([e["seq"] for e in C._events_backwards(self.team, chunk=chunk)], expected)


# --------------------------------------------------------------------------
# svg


class SvgUse(unittest.TestCase):
    def test_nested_use_fan_out_is_refused(self):
        defs = ["<g id='l0'><rect width='1' height='1'/></g>"]
        for level in range(1, 7):
            defs.append("<g id='l{}'>{}</g>".format(level, "".join("<use href='#l{}'/>".format(level - 1) for _ in range(10))))
        with self.assertRaises(HerdrTeamError) as ctx:
            R.sanitize_svg(svg("<defs>{}</defs><use href='#l6'/>".format("".join(defs))))
        self.assertEqual((ctx.exception.code, ctx.exception.details["reason"]), ("svg_refused", "use_expansion"))

    def test_use_cycles_and_deep_trees_are_refused(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            R.sanitize_svg(svg("<g id='a'><use xlink:href='#a'/></g>"))
        self.assertEqual(ctx.exception.details["reason"], "use_cycle")
        with self.assertRaises(HerdrTeamError) as ctx:
            R.sanitize_svg(svg("<g>" * 2000 + "</g>" * 2000))
        self.assertEqual(ctx.exception.details["reason"], "too_deep")

    def test_ordinary_use_still_works(self):
        out = R.sanitize_svg(svg("<defs><symbol id='dot'><circle r='2'/></symbol></defs>" + "<use href='#dot' x='{0}'/>" * 50))
        self.assertEqual(out.count("<use"), 50)


# --------------------------------------------------------------------------
# purge and launch flags


class LaunchConfig(CanvasRig):
    def test_purge_keeps_the_launch_config_that_live_members_point_at(self):
        spec = F.mcp_spec(self.layout, self.team)
        self.ok({"op": "shape", "text": "x", "at": "c0r0", "intent": "t"})
        self.assertTrue(C.purge(self.team))
        self.assertTrue(os.path.isfile(spec["config_file"]))
        self.assertFalse((F.whiteboard_dir(self.team) / C.EVENTS_FILE).exists())
        self.assertEqual(C.summary(self.team)["version"], 0)
        self.assertFalse(C.purge(self.team), "nothing left to delete")
        argv = models.preserved_launch_args("claude", ["claude", "--mcp-config", spec["config_file"]])
        self.assertTrue(os.path.isfile(argv[1]))

    def test_pi_gets_no_mcp_flag(self):
        spec = F.mcp_spec(self.layout, self.team)
        self.assertEqual(F.mcp_launch_args("pi", spec), [])
        self.assertNotIn("pi", F.MCP_KINDS)
        self.assertEqual(models.preserved_launch_args("pi", ["pi", "--mcp-config", spec["config_file"], "--offline"]), ["--offline"])


# --------------------------------------------------------------------------
# the portrait's switch


class PortraitOff(CanvasRig):
    def test_from_todo_reads_nothing_while_the_whiteboard_is_off(self):
        F.set_layer(self.ts.session, False, "human", "cli")
        api = live_api(list(FAKE_AGENTS))
        with mock.patch("herdr_team.activity.plan_of") as plan_of:
            code, _payload, err = json_out(run_cli(["--json", "canvas", "portrait", "--from-todo"], env_no_daemon(self.ts, HERDR_PANE_ID="w2:p2"), api))
        self.assertEqual((code, err["code"]), (1, "whiteboard_off"))
        plan_of.assert_not_called()
        self.assertFalse((self.ts.session.root / A.WATCH_CACHE_DIR).exists())


# --------------------------------------------------------------------------
# watch redaction


#: A made-up Stripe key, assembled here so push protection does not take the fixture for a real one.
FAKE_STRIPE_KEY = "rk_" + "live_51H" + "x" * 22


class WatchRedaction(unittest.TestCase):
    SECRETS = {
        "PGPASSWORD=hunter2hunter2 psql -h db -U app": "hunter2hunter2",
        "mysql -uroot -pTopSecret123 app": "TopSecret123",
        "curl -u admin:S3cr3tPassw0rd https://api.example.com": "S3cr3tPassw0rd",
        "git clone https://oauth2:glpat-AbCdEfGhIjKlMnOpQrSt@gitlab.example.com/x.git": "glpat-AbCdEfGhIjKlMnOpQrSt",
        "stripe --api-key " + FAKE_STRIPE_KEY: FAKE_STRIPE_KEY,
        "curl 'https://maps.example.com/?key=AIzaSyA1234567890abcdefghijklmnopqrstu'": "AIzaSyA1234567890abcdefghijklmnopqrstu",
        "gh auth login --with-token github_pat_11ABCDEFG0123456789_abcdefghijklmnop": "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
        "export MYSQL_PWD=s3cretpw && mysql": "s3cretpw",
        "psql --password s3cretpass": "s3cretpass",
        "curl -H 'Authorization: Basic YWRtaW46cGFzc3dvcmQ='": "YWRtaW46cGFzc3dvcmQ=",
    }

    def test_command_lines_are_redacted_on_cards(self):
        for command, secret in self.SECRETS.items():
            with self.subTest(command=command):
                icon, text, _files = A.describe_tool("Bash", {"command": command})
                self.assertEqual(icon, "run")
                self.assertNotIn(secret, text)
                self.assertIn("[redacted:", text)

    def test_the_doing_token_carries_the_program_never_its_arguments(self):
        cases = {
            "PGPASSWORD=hunter2hunter2 psql -h db": "$ psql",
            "cd /repo && git commit -m 'fix'": "$ git commit",
            "sudo -E /usr/bin/make test": "$ make test",
            "timeout 30 ./scripts/deploy.sh --token abc": "$ deploy.sh",
            "echo hunter2": "$ echo",
        }
        for command, doing in cases.items():
            with self.subTest(command=command):
                _icon, text, _files = A.describe_tool("Bash", {"command": command})
                card = {"actions": [{"icon": "run", "text": text}]}
                self.assertEqual(A.doing_line(card), doing)

    def test_ordinary_commands_are_left_alone(self):
        for command in ("git status", "pytest -q tests/test_x.py", "npm run build -- --max_tokens=4000"):
            with self.subTest(command=command):
                self.assertEqual(A.describe_tool("Bash", {"command": command})[1], command)


if __name__ == "__main__":
    unittest.main()
