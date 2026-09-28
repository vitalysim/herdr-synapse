"""The collaboration commands (canvas v2 phase 5, 10.2 and 10.3): ``draw --base``, ``accept``, ``reject``, ``withdraw``,
``freeze``, ``thaw``, ``undo --author``, ``checkpoint``, ``restore``, ``settings``, ``focus``, ``look --proposals``, and the
MCP server's ``base``, ``proposals``, ``region: "operator"`` and ``canvas_focus``."""
from __future__ import annotations

import time

from test_canvas import OPERATOR, WORKER, CanvasRig
from test_canvas_mcp import McpRig

from herdr_team import canvas as C
from herdr_team import canvas_presence as P
from herdr_team import cmd_canvas


class CollabCli(CanvasRig):
    def setUp(self):
        super().setUp()
        from test_canvas import Cli

        self.helper = Cli("test_draw_from_a_file_stdin_and_op")
        self.helper.ts, self.helper.team, self.helper.layout = self.ts, self.team, self.layout
        self.helper.setUp = None
        from support import FAKE_AGENTS
        from test_cmd_roster import live_api

        self.helper.api = live_api(list(FAKE_AGENTS))
        self.box = self.ok({"op": "shape", "kind": "box", "text": "Hers", "at": [0, 0]}, OPERATOR)["ids"][0]

    def worker(self, *argv):
        return self.helper.worker(*argv)

    def human(self, *argv):
        return self.helper.human(*argv)

    def test_draw_with_a_base(self):
        base = C.current_version(self.team)
        mine = self.ok({"op": "shape", "kind": "box", "text": "Mine", "at": [600, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "move", "id": mine, "by": [0, 40]}, OPERATOR)
        code, _payload, err = self.worker("canvas", "draw", "--base", str(base + 1), "--op",
                                          '{{"op": "move", "id": "{}", "by": [20, 0], "intent": "t"}}'.format(mine))
        self.assertEqual((code, err["refused"][0]["code"]), (1, "stale_base"))
        C.look(self.layout, self.team, "alpha-worker")
        code, payload, err = self.worker("canvas", "draw", "--base", "last", "--op", '{{"op": "move", "id": "{}", "by": [20, 0], "intent": "t"}}'.format(mine))
        self.assertEqual((code, payload["base"]), (0, C.current_version(self.team) - 1), err)
        batch = self.ts.tmp / "plan.json"
        batch.write_text('{{"ops": [{{"op": "move", "id": "{}", "by": [20, 0], "intent": "t"}}], "base": {}}}'.format(mine, base + 1))
        code, _payload, err = self.worker("canvas", "draw", "--file", str(batch))
        self.assertEqual(err["refused"][0]["code"], "stale_base")
        code, _payload, err = self.worker("canvas", "draw", "--base", "soon", "--op", "{}")
        self.assertEqual(err["details"]["field"] if "details" in err else err.get("field"), "base")

    def test_proposals_through_the_cli(self):
        code, payload, err = self.worker("canvas", "draw", "--op", '{{"op": "move", "id": "{}", "by": [40, 0], "intent": "align"}}'.format(self.box))
        self.assertEqual((code, payload["proposed"][0]["proposal"]), (0, "P-1"), err)
        code, payload, err = self.worker("canvas", "look", "--proposals")
        self.assertIn("open proposals:", payload["text"])
        self.assertIn('P-1 by you: move {} (the operator\'s), waiting — "align"'.format(self.box), payload["text"])
        code, _payload, err = self.worker("canvas", "accept", "P-1")
        self.assertEqual((code, err["refused"][0]["code"]), (1, "operator_only"))
        code, payload, err = self.human("canvas", "accept", "P-1", "--note", "good")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.el(self.box)["x"], 40)
        self.worker("canvas", "draw", "--op", '{{"op": "move", "id": "{}", "by": [40, 0], "intent": "t"}}'.format(self.box))
        code, payload, err = self.human("canvas", "reject", "P-2", "--note", "no")
        self.assertEqual(code, 0, err)
        self.worker("canvas", "draw", "--op", '{{"op": "move", "id": "{}", "by": [40, 0], "intent": "t"}}'.format(self.box))
        code, payload, err = self.worker("canvas", "withdraw", "P-3")
        self.assertEqual(code, 0, err)
        self.assertEqual([p["status"] for p in self.scene()["proposals"]], ["accepted", "rejected", "withdrawn"])

    def test_freeze_thaw_settings(self):
        code, payload, err = self.human("canvas", "freeze", "c0r0:c10r10", "--label", "final")
        self.assertEqual((code, payload["applied"][0]["ids"]), (0, ["X-1"]), err)
        code, payload, err = self.human("canvas", "freeze", "--ids", self.box)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.scene()["freezes"][1]["ids"], [self.box])
        code, _payload, err = self.worker("canvas", "freeze", "c20r0:c30r10")
        self.assertEqual(err["refused"][0]["code"], "operator_only")
        code, payload, err = self.human("canvas", "thaw", "--ids", self.box)
        self.assertEqual(code, 0, err)
        code, payload, err = self.human("canvas", "thaw", "X-1")
        self.assertEqual((code, self.scene()["freezes"]), (0, []))
        code, payload, err = self.worker("canvas", "settings")
        self.assertEqual(payload["settings"], {"human_edits": "propose", "frozen": "propose"})
        code, payload, err = self.human("canvas", "settings", "--human-edits", "live", "--frozen", "refuse")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.scene()["settings"]["collab"], {"human_edits": "live", "frozen": "refuse"})
        code, payload, err = self.worker("canvas", "settings", "--frozen", "propose")
        self.assertEqual(err["refused"][0]["code"], "operator_only")

    def test_undo_by_author_checkpoint_restore(self):
        since = C.current_version(self.team)
        self.worker("canvas", "draw", "--op", '{"op": "shape", "text": "one", "at": "c40r0", "intent": "t"}')
        self.worker("canvas", "draw", "--op", '{"op": "shape", "text": "two", "at": "c60r0", "intent": "t"}')
        code, payload, err = self.human("canvas", "checkpoint", "before revert")
        self.assertEqual((code, payload["applied"][0]["ids"]), (0, ["V-1"]), err)
        code, payload, err = self.human("canvas", "undo", "--author", "alpha-worker", "--since", str(since))
        self.assertEqual((code, len(payload["applied"][0]["undo"]["batches"])), (0, 2), err)
        self.assertEqual([e["id"] for e in self.scene()["elements"]], [self.box])
        code, payload, err = self.human("canvas", "restore", "V-1")
        self.assertEqual((code, payload["applied"][0]["restore"]["added"]), (0, 2), err)
        code, payload, err = self.human("canvas", "checkpoint", "--remove", "V-1")
        self.assertEqual(code, 0, err)
        code, _payload, err = self.worker("canvas", "undo", "--author", "alpha-worker", "--force")
        self.assertEqual(err["refused"][0]["code"], "operator_only")
        code, _payload, err = self.worker("canvas", "undo")
        self.assertEqual((code, err["code"]), (2, "usage"))
        code, _payload, err = self.worker("canvas", "checkpoint")
        self.assertEqual((code, err["code"]), (2, "usage"))

    def test_focus(self):
        code, payload, err = self.worker("canvas", "focus", "c0r0:c20r10", "--intent", "the pricing table", "--status", "waiting", "--ttl", "120")
        self.assertEqual(code, 0, err)
        found = P.members(self.team, time.time())[0]
        self.assertEqual((found["status"], found["region"], found["intent"], found["ttl_s"], found["via"]),
                         ("waiting", [0, 0, 400, 200], "the pricing table", 120, "focus"))
        code, payload, err = self.worker("canvas", "focus", self.box)
        self.assertEqual(P.members(self.team, time.time())[0]["ids"], [self.box])
        code, payload, err = self.worker("canvas", "focus", "--ttl", "5")
        self.assertEqual((code, err["code"]), (2, "usage"))
        code, payload, err = self.worker("canvas", "focus", "--clear")
        self.assertEqual((code, P.members(self.team, time.time())), (0, []))
        code, payload, err = self.human("canvas", "focus", "c0r0:c1r1")
        self.assertEqual((code, err["code"]), (1, "usage"))

    def test_help_names_the_operators_commands(self):
        helps = dict(cmd_canvas._SPECS)
        for name in ("accept", "reject", "freeze", "thaw", "restore", "settings"):
            self.assertIn("(the operator)", helps[name], name)
        self.assertEqual(set(cmd_canvas._ACTIONS), {name for name, _help in cmd_canvas._SPECS})


class CollabMcp(McpRig):
    def test_draw_with_a_base_look_proposals_and_the_operators_view(self):
        box = self.ok({"op": "shape", "kind": "box", "text": "Hers", "at": [0, 0]}, OPERATOR)["ids"][0]
        base = C.current_version(self.team)
        self.ok({"op": "move", "id": box, "by": [0, 40]}, OPERATOR)
        drawn = self.tool("canvas_draw", {"ops": [{"op": "move", "id": box, "by": [40, 0], "intent": "t"}], "base": str(base)})
        self.assertFalse(drawn["isError"])
        self.assertEqual(drawn["structuredContent"]["proposed"][0]["base_note"], ["{}: moved by c0r+2 (v{})".format(box, base + 1)])
        drawn = self.tool("canvas_draw", {"ops": [{"op": "move", "id": box, "by": [40, 0], "intent": "t"}], "base": "last"})
        self.assertFalse(drawn["isError"])
        look = self.tool("canvas_look", {"proposals": True})
        self.assertIn("open proposals:", look["content"][0]["text"])
        refused = self.tool("canvas_look", {"region": "operator"})
        self.assertTrue(refused["isError"])
        P.write_human(self.team, "0123456789abcdef", {"viewport": [0, 0, 400, 200]}, time.time())
        look = self.tool("canvas_look", {"region": "operator"})
        self.assertEqual(look["structuredContent"]["region"], [0, 0, 400, 200])
        self.assertIn("operator: viewing c0r0:c20r10", look["content"][0]["text"])

    def test_canvas_focus(self):
        focused = self.tool("canvas_focus", {"region": "c0r0:c10r10", "intent": "sketching", "status": "drawing", "ttl_s": 300})
        self.assertFalse(focused["isError"], focused)
        found = P.members(self.team, time.time())[0]
        self.assertEqual((found["name"], found["intent"], found["ttl_s"]), ("alpha-worker", "sketching", 300))
        self.assertIn("error", self.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                            "params": {"name": "canvas_focus", "arguments": {"ttl_s": 5}}}))
        self.assertIn("error", self.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                            "params": {"name": "canvas_focus", "arguments": {"status": "asleep"}}}))
        self.assertFalse(self.tool("canvas_focus", {"clear": True})["isError"])
        self.assertEqual(P.members(self.team, time.time()), [])

    def test_the_tool_list_describes_the_new_fields(self):
        tools = {tool["name"]: tool for tool in self.tool_list()}
        self.assertIn("base", tools["canvas_draw"]["inputSchema"]["properties"])
        self.assertIn("proposals", tools["canvas_look"]["inputSchema"]["properties"])
        self.assertIn('"operator"', tools["canvas_look"]["inputSchema"]["properties"]["region"]["description"])
        self.assertIn("withdraw {id: your P-n}", tools["canvas_draw"]["description"])

    def tool_list(self):
        return self.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]
