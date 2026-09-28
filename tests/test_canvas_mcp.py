"""The canvas stdio MCP server (``herdr_team.canvas_mcp``, 0.21): JSON-RPC, tools, identity, refusals."""
from __future__ import annotations

import base64
import io
import json
import unittest
from unittest import mock

from support import FAKE_AGENTS
from test_canvas import PNG_MAGIC, CanvasRig, fake_resvg
from test_cmd_roster import live_api

from herdr_team import VERSION
from herdr_team import canvas as C
from herdr_team import canvas_mcp as MCP
from herdr_team import canvas_render as R
from herdr_team import features as F
from herdr_team import store
from herdr_team.identity import Author


def request(msg_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def call(msg_id, name, arguments=None):
    return request(msg_id, "tools/call", {"name": name, "arguments": arguments or {}})


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class McpRig(CanvasRig):
    def setUp(self):
        super().setUp()
        self.api = live_api(list(FAKE_AGENTS))
        self.env = dict(self.ts.env, HERDR_PANE_ID="w2:p2")
        self.clock = Clock()
        self.session = MCP.McpSession(self.layout, self.env, self.api, "alpha", clock=self.clock)

    def handle(self, message):
        return MCP.handle(message, self.session)

    def tool(self, name, arguments=None):
        reply = self.handle(call(7, name, arguments))
        self.assertNotIn("error", reply, reply)
        return reply["result"]

    def as_author(self, author):
        return mock.patch.object(MCP._identity, "resolve_author", return_value=author)


class Protocol(McpRig):
    def test_initialize_negotiates_and_describes_the_server(self):
        reply = self.handle(request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t"}}))
        result = reply["result"]
        self.assertEqual((reply["jsonrpc"], reply["id"]), ("2.0", 1))
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(result["serverInfo"], {"name": "synapse-canvas", "version": VERSION})
        self.assertEqual(len(result["instructions"].splitlines()), 10)
        self.assertEqual(self.handle(request(2, "initialize", {"protocolVersion": "1999-01-01"}))["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(self.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertTrue(self.session.initialized)
        self.assertEqual(self.handle(request(3, "ping"))["result"], {})

    def test_errors(self):
        self.assertEqual(self.handle(request(1, "resources/list"))["error"]["code"], -32601)
        self.assertIsNone(self.handle({"jsonrpc": "2.0", "method": "whatever"}), "a notification never gets a reply")
        self.assertEqual(self.handle({"id": 1, "method": "ping"})["error"]["code"], -32600)
        self.assertEqual(self.handle(call(2, "canvas_erase"))["error"]["code"], -32602)
        self.assertEqual(self.handle(call(3, "canvas_look", {"zoom": 2}))["error"]["code"], -32602)
        self.assertEqual(self.handle(call(4, "canvas_draw", {"ops": "shape"}))["error"]["code"], -32602)
        self.assertEqual(self.handle(request(5, "tools/call", {"arguments": {}}))["error"]["code"], -32602)
        self.assertIsNone(self.handle({"jsonrpc": "2.0", "id": 9, "result": {}}), "a client's response is ignored")

    def test_tools_list(self):
        tools = self.handle(request(1, "tools/list"))["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], list(MCP.TOOL_NAMES))
        text = json.dumps(tools)
        self.assertNotIn("oneOf", text)
        self.assertNotIn("anyOf", text)
        for tool in tools:
            self.assertFalse(tool["inputSchema"]["additionalProperties"], tool["name"])
            self.assertTrue(tool["description"])
        draw = next(t for t in tools if t["name"] == "canvas_draw")
        self.assertEqual(draw["inputSchema"]["required"], ["ops"])
        self.assertIn("Example:", draw["description"])
        comment = next(t for t in tools if t["name"] == "canvas_comment")
        self.assertEqual(comment["inputSchema"]["required"], ["at", "text"])


class Tools(McpRig):
    def test_draw_and_look_as_the_member_the_pane_resolves_to(self):
        result = self.tool("canvas_draw", {"ops": [{"op": "shape", "id": "a", "kind": "note", "text": "hello", "at": "c0r0", "intent": "t"}]})
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["applied"][0]["ids"], ["E-1"])
        self.assertEqual(result["content"], [{"type": "text", "text": C.apply_text(result["structuredContent"])}])
        self.assertEqual(C.load_scene(self.team)["elements"][0]["author"], "alpha-worker")
        event = json.loads((F.whiteboard_dir(self.team) / "events.jsonl").read_text().splitlines()[0])
        self.assertEqual(event["author"]["via"], "mcp")
        look = self.tool("canvas_look", {"since": "last", "region": "[0, 0, 400, 400]"})
        self.assertFalse(look["isError"])
        self.assertEqual(look["structuredContent"]["reader"], "alpha-worker")
        self.assertIn('E-1 note "hello"', look["content"][0]["text"])
        self.assertEqual(C.cursor(self.team, "alpha-worker"), 1)

    def test_blocks_through_the_tools(self):
        # Phase 2: a block op, a patch, look --block and the batch's geometry and check, all through MCP.
        result = self.tool("canvas_draw", {"ops": [{"op": "kanban", "id": "work", "at": "c0r0", "intent": "t",
                                                    "columns": [{"title": "Todo", "cards": ["A"]}, {"title": "Done", "cards": []}]}]})
        self.assertFalse(result["isError"], result)
        content = result["structuredContent"]
        self.assertEqual(content["applied"][0]["block"]["kind"], "kanban")
        self.assertIn("geometry", content)
        self.assertIn("check", content)
        patched = self.tool("canvas_draw", {"ops": [{"op": "place", "id": "work.c1", "in": "work.done", "intent": "t"}]})
        self.assertFalse(patched["isError"], patched)
        look = self.tool("canvas_look", {"block": "work", "full": True})
        self.assertFalse(look["isError"], look)
        self.assertEqual(look["structuredContent"]["block"]["columns"][1]["cards"], ["A"])
        self.assertIn("patch {id (a block)", C.op_table() if hasattr(C, "op_table") else __import__("herdr_team.canvas_mcp", fromlist=["x"]).op_table())

    def test_look_with_an_image_inlines_the_png(self):
        self.tool("canvas_draw", {"ops": [{"op": "shape", "text": "x", "at": "c0r0", "intent": "t"}]})
        with mock.patch.object(R, "find_resvg", return_value="/fake/resvg"), mock.patch.object(R, "RUN", fake_resvg):
            result = self.tool("canvas_look", {"image": True, "grid": True})
        self.assertEqual([c["type"] for c in result["content"]], ["text", "image"])
        self.assertEqual(result["content"][1]["mimeType"], "image/png")
        self.assertTrue(base64.b64decode(result["content"][1]["data"]).startswith(PNG_MAGIC))
        without = self.tool("canvas_look", {"image": True})
        self.assertEqual([c["type"] for c in without["content"]], ["text"], "no resvg: the text (with the svg path) only")

    def test_comment_claim_legend_changes(self):
        self.tool("canvas_draw", {"ops": [{"op": "shape", "id": "a", "text": "x", "at": "c0r0", "intent": "t"}]})
        comment = self.tool("canvas_comment", {"at": "a", "text": "@reviewer look", "mentions": ["human"]})
        self.assertFalse(comment["isError"], comment)
        self.assertEqual(C.load_scene(self.team)["elements"][-1]["mentions"], ["human", "alpha-reviewer"])
        self.assertEqual([r["to"] for r in store.BoardStore(self.team).read() if r.get("event") == "canvas_sent"], [["human", "alpha-reviewer"]])
        claim = self.tool("canvas_claim", {"region": "c0r0:c10r10", "label": "mine"})
        self.assertEqual(claim["structuredContent"]["applied"][0]["ids"], ["K-1"])
        released = self.tool("canvas_claim", {"release": "all"})
        self.assertEqual(released["structuredContent"]["applied"][0]["ids"], ["K-1"])
        legend = self.tool("canvas_legend", {"symbol": "a", "meaning": "the start"})
        self.assertEqual(C.load_scene(self.team)["legend"][0]["symbol"], "E-1")
        self.assertFalse(legend["isError"])
        self.assertFalse(self.tool("canvas_legend", {"remove": "G-1"})["isError"])
        changes = self.tool("canvas_changes")
        self.assertEqual(len(changes["structuredContent"]["changes"]), 6)
        self.assertEqual(self.tool("canvas_changes", {"since": "last"})["structuredContent"]["changes"], [])
        self.assertEqual(len(self.tool("canvas_changes", {"since": 4})["structuredContent"]["changes"]), 2, "a number is accepted for a string")

    def test_refusals_are_tool_errors_with_their_code(self):
        refused = self.tool("canvas_draw", {"ops": [{"op": "shape", "text": "no intent"}]})
        self.assertTrue(refused["isError"])
        self.assertEqual(refused["structuredContent"]["code"], "canvas_refused")
        self.assertEqual(refused["structuredContent"]["refused"][0]["details"]["field"], "intent")
        self.assertTrue(refused["content"][0]["text"].startswith("canvas_refused: "))
        F.set_team(self.team, enabled=False)
        off = self.tool("canvas_look")
        self.assertTrue(off["isError"])
        self.assertEqual((off["structuredContent"]["code"], off["structuredContent"]["scope"]), ("whiteboard_off", "team"))

    def test_only_a_verified_member_of_the_pinned_team(self):
        cases = [
            Author("human", "human", "outside", False, team="alpha"),
            Author("system", None, "hook", True, team="alpha"),
            Author("alpha-worker", "claude", "cli", False, team="alpha"),
            Author("beta-worker", "claude", "cli", True, team="beta"),
        ]
        for author in cases:
            with self.subTest(author=author.name, via=author.via):
                session = MCP.McpSession(self.layout, self.env, self.api, "alpha", clock=self.clock)
                with self.as_author(author):
                    reply = MCP.handle(call(1, "canvas_draw", {"ops": [{"op": "shape", "at": "c0r0", "intent": "t"}]}), session)
                self.assertTrue(reply["result"]["isError"])
                self.assertEqual(reply["result"]["structuredContent"]["code"], "not_a_member")
        self.assertFalse(F.whiteboard_dir(self.team).joinpath("events.jsonl").exists(), "nothing was drawn")

    def test_identity_is_cached_for_thirty_seconds(self):
        member = Author("alpha-worker", "claude", "cli", True, team="alpha")
        with mock.patch.object(MCP._identity, "resolve_author", return_value=member) as resolve:
            self.tool("canvas_changes")
            self.clock.now += 29
            self.tool("canvas_changes")
            self.assertEqual(resolve.call_count, 1)
            self.clock.now += 2
            self.tool("canvas_changes")
            self.assertEqual(resolve.call_count, 2)
            self.assertEqual(resolve.call_args[1]["team"], "alpha")

    def test_an_unexpected_failure_is_a_tool_error_not_a_crash(self):
        with mock.patch.object(C, "read_changes", side_effect=RuntimeError("disk on fire")), mock.patch("sys.stderr", io.StringIO()):
            result = self.tool("canvas_changes")
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["code"], "internal")


class Serve(McpRig):
    def test_stdio_round_trip(self):
        lines = [
            json.dumps(request(1, "initialize", {"protocolVersion": "2025-06-18"})),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            "",
            "{not json",
            json.dumps(call(2, "canvas_draw", {"ops": [{"op": "shape", "text": "hi", "at": "c0r0", "intent": "t"}]})),
            json.dumps([request(3, "ping"), {"jsonrpc": "2.0", "method": "notifications/cancelled"}]),
            json.dumps(call(4, "canvas_look")),
        ]
        stdout = io.StringIO()
        code = MCP.serve(self.layout, self.env, stdin=io.StringIO("\n".join(lines) + "\n"), stdout=stdout, api=self.api, team_name="alpha")
        self.assertEqual(code, 0)
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(replies[0]["id"], 1)
        self.assertEqual(replies[1]["error"]["code"], -32700)
        self.assertEqual(replies[2]["result"]["structuredContent"]["applied"][0]["ids"], ["E-1"])
        self.assertEqual(replies[3], [{"jsonrpc": "2.0", "id": 3, "result": {}}])
        self.assertIn("E-1", replies[4]["result"]["content"][0]["text"])
        self.assertEqual(len(replies), 5, "notifications and blank lines get no reply")


if __name__ == "__main__":
    unittest.main()
