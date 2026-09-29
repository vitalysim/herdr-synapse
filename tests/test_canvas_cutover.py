"""Canvas v2 phase 6, the agent-facing side of the cut-over (3.1 to 3.3, 6.7): the op families teach components first and
the 0.21 primitives last, the MCP ``canvas_draw`` description and ``canvas draw --help`` share one op table and one v2
example (which applies clean), the tool set is unchanged, and the benchmark's attempt trace records every decided call
while its switch file exists."""
from __future__ import annotations

import argparse
import sys

from support import PLUGIN_ROOT
from test_canvas import OPERATOR, WORKER, CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_kinds as R
from herdr_team import canvas_mcp as M
from herdr_team import cmd_canvas
from herdr_team import reference_docs
from herdr_team.errors import HerdrTeamError

FAMILY_OPS = {
    "block": ["section", "card", "sticky", "callout", "heading", "badge", "icon", "table", "kanban", "timeline"],
    "diagram": ["graph", "mindmap", "sequence", "mermaid"],
    "data": ["chart", "viz"],
    "3d": ["scene3d"],
    "primitive": ["shape", "arrow", "frame", "pen", "path", "svg", "image", "comment"],
}
HEADINGS = ["Components", "Diagrams", "Data", "3D", "Any element", "Primitives (when no component fits)"]
LEAD_ONLY = ("accept", "reject", "freeze", "thaw", "settings", "restore", "migrate")


def words(text: str) -> str:
    return " ".join(text.split())


class Families(CanvasRig):
    def test_every_op_has_its_family_and_the_groups_come_in_teaching_order(self):
        groups = R.ops_by_family()
        self.assertEqual([family for family, _specs in groups], list(R.FAMILIES))
        self.assertEqual({family: [spec.name for spec in specs] for family, specs in groups}, FAMILY_OPS)
        listed = [spec.name for _family, specs in groups for spec in specs]
        self.assertEqual(sorted(listed), sorted(spec.name for spec in R.ops()), "every kind op exactly once")

    def test_an_op_with_no_family_lands_under_more_last(self):
        if str(PLUGIN_ROOT) not in sys.path:
            sys.path.insert(0, str(PLUGIN_ROOT))
        R._load_extra("tests.fixtures.kind_stamp")
        self.addCleanup(R._unload, "tests.fixtures.kind_stamp")
        groups = R.ops_by_family()
        self.assertEqual(groups[-1][0], R.MORE)
        self.assertEqual([spec.name for spec in groups[-1][1]], ["stamp"])
        table = M.op_table()
        self.assertGreater(table.index("More: stamp {text, tone}"), table.index("Primitives (when no component fits):"))
        ops = reference_docs.render_canvas_ops_section()
        self.assertGreater(ops.index("| `stamp` |"), ops.index("| **Primitives (when no component fits)** | | |"))


class OpTable(CanvasRig):
    def test_components_first_primitives_last_and_every_agent_op_once(self):
        table = M.op_table()
        at = [table.index(heading + ":") for heading in HEADINGS]
        self.assertEqual(at, sorted(at), "the headings in teaching order")
        for name in [spec.name for spec in R.ops()] + ["move", "restyle", "edit", "delete", "patch", "place", "pin", "unpin", "refit",
                                                       "undo", "claim", "release", "legend", "portrait", "withdraw", "checkpoint"]:
            self.assertEqual(table.count(" " + name + " {") + table.startswith(name + " {"), 1, name)
        for name in LEAD_ONLY:
            self.assertNotIn(" " + name + " {", table, "{} is the operator's, not an agent's".format(name))
        self.assertLess(table.index("card {"), table.index("shape {"))
        self.assertLess(table.index("graph {"), table.index("arrow {"))

    def test_the_reference_follows_the_same_order(self):
        ops = reference_docs.render_canvas_ops_section()
        self.assertLess(ops.index("| **Components** | | |"), ops.index("| `card` |"))
        self.assertLess(ops.index("| `scene3d` |"), ops.index("| **Any element** | | |"))
        self.assertLess(ops.index("| `migrate` |"), ops.index("| **Primitives (when no component fits)** | | |"))
        self.assertLess(ops.index("| **Primitives (when no component fits)** | | |"), ops.index("| `shape` |"))
        self.assertIn("| `migrate` | `action` |", ops)


class McpText(CanvasRig):
    def test_the_tool_set_is_unchanged(self):
        tools = M.tool_definitions()
        self.assertEqual(tuple(tool["name"] for tool in tools), M.TOOL_NAMES)
        self.assertEqual(len(tools), 8)
        draw = next(tool for tool in tools if tool["name"] == "canvas_draw")
        self.assertEqual(sorted(draw["inputSchema"]["properties"]), ["atomic", "base", "ops"])

    def test_the_draw_description_leads_with_components_and_relations(self):
        tools = {tool["name"]: tool for tool in M.tool_definitions()}
        draw = tools["canvas_draw"]["description"]
        self.assertTrue(draw.startswith("Draw by naming components (card, section, kanban, table, timeline, graph, mindmap, sequence, chart, "
                                        "scene3d) and their relations; the canvas sizes, places and routes them."), draw[:200])
        self.assertIn("Use tones (neutral info success warning danger accent idea decision), never coordinates unless you must.", draw)
        self.assertIn("After drawing, call canvas_check and apply its fixes.", draw)
        self.assertIn(M.op_table(), draw)
        self.assertLess(draw.index(M.op_table()), draw.index("Example: "))
        check = tools["canvas_check"]["description"]
        for code in ("overlap", "label_overflow", "arrow_through"):
            self.assertIn(code, check)
        self.assertIn("reads back as the op that builds it", tools["canvas_look"]["description"])
        self.assertIn("its own picture of the board", tools["canvas_look"]["inputSchema"]["properties"]["exact"]["description"])

    def test_the_example_is_a_v2_graph_and_a_callout_that_applies_clean(self):
        ops = M.EXAMPLE_BATCH["ops"]
        self.assertEqual([op["op"] for op in ops], ["graph", "callout"])
        graph = ops[0]
        self.assertEqual((len(graph["nodes"]), len(graph["groups"]), graph["direction"]), (4, 1, "right"))
        self.assertIn("api -> db: SQL", graph["edges"])
        self.assertEqual(ops[1]["right_of"], graph["id"])
        result = self.apply(ops)
        self.assertEqual(result["refused"], [])
        self.assertEqual(result["check"]["problems"], [], result["check"])
        self.assertEqual(C.check(self.layout, self.team, "alpha-worker")["problems"], [])


class DrawHelp(CanvasRig):
    def help_of(self, action):
        parser = argparse.ArgumentParser(prog="herdr-synapse canvas")
        cmd_canvas._add_arguments(parser)
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        return sub.choices[action].format_help()

    def test_draw_help_carries_the_mcp_table_and_example(self):
        text = self.help_of("draw")
        self.assertIn(words(M.op_table()), words(text))
        self.assertIn(words(M._EXAMPLE), words(text))
        self.assertIn(words(M.DRAW_LEAD), words(text))
        self.assertIn("herdr-synapse canvas check, apply the fixes it lists", words(text))

    def test_the_subcommands_start_with_look_draw_check_and_list_migrate(self):
        names = [name for name, _help in cmd_canvas._SPECS]
        self.assertEqual(names[:3], ["look", "draw", "check"])
        self.assertIn("migrate", names)
        self.assertIn("run after every drawing meant for others; apply the listed fixes, check again", dict(cmd_canvas._SPECS)["check"])
        self.assertIn("--apply", self.help_of("migrate"))
        self.assertIn("its own picture of the board", words(self.help_of("look")))


class AttemptTrace(CanvasRig):
    def switch_on(self):
        folder = C._dir(self.team)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / C.ATTEMPTS_SWITCH).write_text("", encoding="utf-8")

    def test_off_by_default(self):
        self.ok({"op": "shape", "text": "one", "at": "c0r0", "intent": "t"})
        self.assertFalse(C._file(self.team, C.ATTEMPTS_FILE).exists())
        self.assertEqual(C.read_attempts(self.team), [])

    def test_every_decided_call_is_one_line(self):
        self.switch_on()
        applied = self.apply([{"op": "shape", "text": "one", "at": "c0r0", "intent": "t"}], base="last")
        partly = self.apply([{"op": "shape", "text": "two", "at": "c20r0", "intent": "t"}, {"op": "nonsense", "intent": "t"}])
        with self.assertRaises(HerdrTeamError) as whole:
            self.apply([{"op": "shape", "text": "three", "at": "c40r0", "intent": "t"}, {"op": "shape", "color": 7, "intent": "t"}], atomic=True)
        with self.assertRaises(HerdrTeamError):
            C.apply_ops(self.layout, self.team, "not a list", WORKER)  # type: ignore[arg-type]
        records = C.read_attempts(self.team)
        self.assertEqual(len(records), 4)
        first, second, third, fourth = records
        keys = {"ts", "author", "via", "ops", "names", "applied", "proposed", "refused", "error", "batch", "version", "base", "ms"}
        for record in records:
            self.assertEqual(set(record), keys)
            self.assertEqual((record["author"], record["via"]), ("alpha-worker", "cli"))
            self.assertIsInstance(record["ms"], int)
        self.assertEqual((first["ops"], first["names"], first["applied"], first["refused"], first["error"], first["batch"]),
                         (1, ["shape"], 1, [], None, applied["batch"]))
        self.assertEqual(first["version"], applied["version"])
        self.assertEqual(first["base"], applied["base"])
        self.assertEqual((second["ops"], second["applied"], second["refused"]), (2, 1, [{"index": 1, "op": "nonsense", "code": "op_invalid"}]))
        self.assertEqual((third["error"], third["applied"], third["batch"]), (whole.exception.code, 0, None))
        self.assertEqual([r["index"] for r in third["refused"]], [1])
        self.assertEqual((fourth["error"], fourth["ops"], fourth["names"]), ("op_invalid", 0, []))
        self.assertEqual(C.read_attempts(self.team, since_ts=second["ts"]), records[1:])
        self.assertEqual(C.read_attempts(self.team, since_ts=C._parse_iso(fourth["ts"]) + 1), [])
        self.assertEqual(partly["refused"][0]["code"], "op_invalid")

    def test_the_operator_and_the_page_are_recorded_too_and_a_full_trace_stops_growing(self):
        self.switch_on()
        self.apply([{"op": "shape", "text": "hers", "at": "c0r0"}], OPERATOR)
        self.assertEqual(C.read_attempts(self.team)[-1]["author"], C.HUMAN)
        path = C._file(self.team, C.ATTEMPTS_FILE)
        with open(path, "ab") as handle:
            handle.write(b"x" * (C.MAX_ATTEMPTS_BYTES + 1) + b"\n")
        size = path.stat().st_size
        self.apply([{"op": "shape", "text": "later", "at": "c20r0", "intent": "t"}], WORKER)
        self.assertEqual(path.stat().st_size, size)
        self.assertEqual(len(C.read_attempts(self.team)), 1, "a garbage line is skipped")

    def test_a_trace_that_cannot_be_written_never_fails_the_batch(self):
        self.switch_on()
        C._file(self.team, C.ATTEMPTS_FILE).mkdir()
        result = self.apply([{"op": "shape", "text": "fine", "at": "c0r0", "intent": "t"}])
        self.assertEqual(result["refused"], [])
