"""The graph block (canvas v2 phase 2, 4.7.1): shorthand, groups, same_rank and order, icons and details, patches and
upserts, legacy graphs, pins, the round trip (T-B1), Mermaid flowcharts, and no edge through a node (T-R6)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import test_canvas

from herdr_team import canvas as C
from herdr_team import canvas_blocks as B
from herdr_team import canvas_check as K
from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R
from herdr_team.canvas_kinds import graph as G

HUMAN = test_canvas.OPERATOR
SCENES = Path(__file__).resolve().parent / "fixtures" / "canvas_scenes"
LAYOUT_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "layouts"

CHECKOUT = {"op": "graph", "id": "checkout", "title": "Checkout flow", "layout": "flow", "direction": "right", "at": [0, 0],
            "intent": "show the checkout path",
            "groups": [{"id": "core", "title": "Core services", "tone": "info"}],
            "nodes": [{"id": "user", "text": "Shopper", "icon": "user"},
                      {"id": "api", "text": "Checkout API", "icon": "server", "in": "core"},
                      {"id": "ok", "text": "Payment approved?", "kind": "diamond", "in": "core"},
                      {"id": "db", "text": "Orders database", "icon": "database", "in": "core", "detail": "Postgres 17, primary in eu-west"},
                      {"id": "fail", "text": "Show retry and keep the cart", "tone": "danger"}],
            "edges": ["user -> api: HTTPS", "api -> ok", "ok -> db: yes", "ok -> fail: no", "fail --> user"],
            "same_rank": [["db", "fail"]], "route": "orthogonal"}


class GraphRig(test_canvas.CanvasRig):
    def test_nothing(self):
        """The rig itself (another test module borrows it)."""

    def root(self, alias):
        return next(e for e in self.scene()["elements"] if e.get("alias") == alias)

    def members(self, alias):
        rid = self.root(alias)["id"]
        return [e for e in self.scene()["elements"] if e.get("group") == rid]

    def part(self, alias, part):
        return next(e for e in self.members(alias) if e.get("part") == part)

    def spec(self, alias):
        elements = self.scene()["elements"]
        return B.spec_of(elements, self.root(alias))

    def problems(self, codes=("arrow_through", "overlap")):
        return [p for p in K.problems(self.scene()["elements"]) if p["code"] in codes]


class Shorthand(unittest.TestCase):
    def test_the_edge_grammar(self):
        self.assertEqual(G.parse_edge("a -> b"), {"from": "a", "to": "b"})
        self.assertEqual(G.parse_edge("api-gw --> db: reads rows"), {"from": "api-gw", "to": "db", "label": "reads rows", "style": "dashed"})
        self.assertEqual(G.parse_edge("a-->b"), {"from": "a", "to": "b", "style": "dashed"})
        self.assertEqual(G.parse_edge("a <-> b"), {"from": "a", "to": "b", "tail": "arrow"})
        self.assertEqual(G.parse_edge("a -- b: pairs"), {"from": "a", "to": "b", "label": "pairs", "head": "none"})
        self.assertEqual(G.parse_edge("a->b: x: y"), {"from": "a", "to": "b", "label": "x: y"})
        for bad in ("a => b", "a -> ", "-> b", "a b", ""):
            self.assertIsNone(G.parse_edge(bad), bad)

    def test_readback_uses_shorthand_when_it_says_everything(self):
        self.assertEqual(G.shorthand({"id": "e:a->b", "from": "a", "to": "b", "style": "dashed", "label": "x"}), "a --> b: x")
        self.assertEqual(G.shorthand({"id": "e:a->b", "from": "a", "to": "b", "head": "none"}), "a -- b")
        self.assertIsNone(G.shorthand({"id": "e:a->b", "from": "a", "to": "b", "tone": "danger"}))
        self.assertIsNone(G.shorthand({"id": "e:a->b", "from": "a", "to": "b", "style": "dotted"}))
        self.assertEqual(G.edge_remove_key("api -> ok"), "e:api->ok")
        self.assertEqual(G.edge_remove_key("e:api->ok#2"), "e:api->ok#2")


class Create(GraphRig):
    def test_the_spec_example_builds_a_block_of_frames_shapes_and_arrows(self):
        applied = self.ok(CHECKOUT)
        root = self.root("checkout")
        self.assertEqual((root["type"], root["block"], root["text"]), ("frame", "graph", "Checkout flow"))
        self.assertEqual(applied["block"]["kind"], "graph")
        parts = {e["part"]: e for e in self.members("checkout")}
        self.assertEqual(set(parts), {"core", "user", "api", "ok", "db", "fail", "e:user->api", "e:api->ok", "e:ok->db", "e:ok->fail",
                                      "e:fail->user"})
        self.assertEqual(parts["core"]["type"], "frame")
        self.assertEqual(parts["core"]["alias"], "checkout.core")
        for node in ("api", "ok", "db"):
            self.assertEqual(parts[node]["frame"], parts["core"]["id"], node)
            core = C.bounds(parts["core"])
            self.assertTrue(C._contains(core, C.bounds(parts[node])), node)
        self.assertEqual(parts["user"]["frame"], root["id"])
        self.assertEqual(parts["api"]["icon"], "server")
        self.assertEqual(parts["ok"]["type"], "diamond")
        self.assertEqual(parts["e:fail->user"]["style"]["dash"], "dashed")
        self.assertEqual(parts["e:user->api"]["text"], "HTTPS")
        # left to right: the shopper, then the API, then the decision, then its two outcomes side by side
        x = {n: C.bounds(parts[n])[0] for n in ("user", "api", "ok", "db", "fail")}
        self.assertLess(x["user"], x["api"])
        self.assertLess(x["api"], x["ok"])
        self.assertLess(x["ok"], x["db"])
        self.assertLess(abs(C._center(parts["db"])[0] - C._center(parts["fail"])[0]), 1, "same_rank")
        for part, el in parts.items():
            if el["type"] == "arrow":
                points = el["points"]
                self.assertTrue(all(a[0] == b[0] or a[1] == b[1] for a, b in zip(points, points[1:])), part)
        self.assertEqual(self.problems(), [])

    def test_detail_is_the_entrys_tip_and_icons_are_checked(self):
        self.ok(CHECKOUT)
        db = self.part("checkout", "db")
        entry = next(e for e in D.display_list(self.scene())["entries"] if e["id"] == db["id"])
        self.assertEqual(entry["tip"], "Postgres 17, primary in eu-west")
        self.assertEqual((entry["block"], entry["part"]), (self.root("checkout")["id"], "db"))
        refusal = self.refused({"op": "graph", "nodes": [{"id": "a", "icon": "no-such-icon-at-all"}], "intent": "t"})
        self.assertEqual(refusal["code"], "icon_unknown")

    def test_refusals_name_the_field(self):
        cases = [
            ({"nodes": ["a"], "edges": ["a -> z"]}, "edges[0].to"),
            ({"nodes": ["a", "b"], "edges": ["a => b"]}, "edges[0]"),
            ({"nodes": ["a", "a"]}, "nodes[1].id"),
            ({"nodes": [{"id": "a", "in": "nowhere"}]}, "nodes[0].in"),
            ({"nodes": ["a"], "groups": [{"id": "a"}]}, "groups[0].id"),
            ({"nodes": ["a"], "groups": [{"id": "g", "parent": "h"}, {"id": "h", "parent": "g"}]}, "groups[0].parent"),
            ({"nodes": ["a", "b"], "same_rank": "a b"}, "same_rank"),
            ({"nodes": ["a"], "layout": "spiral"}, "layout"),
            ({"nodes": ["a"], "route": "wiggly"}, "route"),
            ({"nodes": []}, "nodes"),
        ]
        for extra, field in cases:
            with self.subTest(field=field):
                self.assertEqual(self.refused(dict({"op": "graph", "intent": "t"}, **extra))["details"]["field"], field)

    def test_nested_groups_and_a_parallel_edge(self):
        self.ok({"op": "graph", "id": "p", "at": [0, 0], "intent": "t",
                 "groups": [{"id": "data", "title": "Data"}, {"id": "hot", "title": "Hot", "parent": "data"}],
                 "nodes": ["orders", {"id": "pg", "in": "hot"}, {"id": "wh", "in": "data"}],
                 "edges": ["orders -> pg: writes", "orders -> pg: reads", "pg --> wh"]})
        parts = {e["part"]: e for e in self.members("p")}
        self.assertIn("e:orders->pg#2", parts)
        self.assertEqual(parts["hot"]["frame"], parts["data"]["id"])
        self.assertTrue(C._contains(C.bounds(parts["data"]), C.bounds(parts["hot"])))
        self.assertTrue(C._contains(C.bounds(parts["hot"]), C.bounds(parts["pg"])))
        self.assertFalse(C._intersects(C.bounds(parts["data"]), C.bounds(parts["orders"])))
        self.assertEqual(self.problems(), [])

    def test_every_graph_layout_and_the_order_setting(self):
        for index, layout in enumerate(("layers", "tree", "radial", "force", "grid")):
            with self.subTest(layout=layout):
                self.ok({"op": "graph", "id": "g" + layout, "layout": layout, "at": [0, 2000 * index], "intent": "t",
                         "nodes": ["a", "b", "c", "d"], "edges": ["a -> b", "a -> c", "c -> d"]})
                self.assertEqual(self.problems(), [])
        self.ok({"op": "graph", "id": "ordered", "at": [4000, 0], "intent": "t", "nodes": ["r", "x", "y", "z"],
                 "edges": ["r -> x", "r -> y", "r -> z"], "order": [["z", "y", "x"]]})
        xs = {p: C.bounds(self.part("ordered", p))[0] for p in "xyz"}
        self.assertLess(xs["z"], xs["y"])
        self.assertLess(xs["y"], xs["x"])


class PatchAndUpsert(GraphRig):
    def test_patch_adds_removes_and_keeps_what_it_did_not_touch(self):
        self.ok(CHECKOUT)
        before = {e["part"]: e for e in self.members("checkout")}
        version = self.root("checkout")["updated_seq"]
        stale = self.refused({"op": "patch", "id": "checkout", "if_version": version - 1, "add": {"nodes": ["x"]}, "intent": "t"})
        self.assertEqual(stale["code"], "canvas_stale")
        self.ok({"op": "patch", "id": "checkout", "if_version": version, "intent": "add a fraud step",
                 "add": {"nodes": [{"id": "fraud", "text": "Fraud check", "in": "core"}], "edges": ["api -> fraud", "fraud -> ok"]},
                 "remove": {"edges": ["api -> ok"]}})
        after = {e["part"]: e for e in self.members("checkout")}
        self.assertIn("fraud", after)
        self.assertNotIn("e:api->ok", after)
        self.assertEqual(after["fraud"]["frame"], after["core"]["id"])
        for part in ("user", "api", "ok", "db", "fail"):
            self.assertEqual(after[part]["id"], before[part]["id"], "members keep their ids")
        self.assertEqual(self.problems(), [])
        unknown = self.refused({"op": "patch", "id": "checkout", "remove": {"edges": ["user -> db"]}, "intent": "t"})
        self.assertEqual(unknown["code"], "part_unknown")
        self.ok({"op": "patch", "id": "checkout", "update": {"nodes": [{"id": "fail", "text": "Retry later"}]}, "set": {"direction": "down"},
                 "intent": "t"})
        self.assertEqual(self.part("checkout", "fail")["text"], "Retry later")
        self.assertEqual(self.root("checkout")["settings"]["direction"], "down")

    def test_the_same_op_again_is_an_upsert(self):
        self.ok(CHECKOUT)
        ids = {e["part"]: e["id"] for e in self.members("checkout")}
        applied = self.ok(dict(CHECKOUT, nodes=CHECKOUT["nodes"][:4], edges=["user -> api", "api -> ok", "ok -> db"], same_rank=None))
        self.assertTrue(applied["block"]["upsert"])
        after = {e["part"]: e["id"] for e in self.members("checkout")}
        self.assertNotIn("fail", after)
        self.assertEqual({p: after[p] for p in ("user", "api", "ok", "db")}, {p: ids[p] for p in ("user", "api", "ok", "db")})

    def test_a_graph_drawn_before_0_22_converts_under_its_id(self):
        self.ok({"op": "graph", "id": "old", "at": [0, 0], "intent": "t", "nodes": ["a", "b"], "edges": [["a", "b"]]})
        root = self.root("old")
        # What 0.21 stored: a plain frame with group members and no block, parts or items.
        elements = self.scene()["elements"]
        legacy = []
        for el in elements:
            if el["id"] == root["id"]:
                legacy.append({k: v for k, v in el.items() if k not in ("block", "settings", "seq", "fit")})
            elif el.get("group") == root["id"]:
                legacy.append({k: v for k, v in el.items() if k not in ("part", "item")})
            else:
                legacy.append(el)
        self.rewrite(legacy)
        old_ids = {e.get("alias"): e["id"] for e in self.scene()["elements"]}
        self.ok({"op": "graph", "id": "old", "intent": "redraw", "nodes": ["a", "b", "c"], "edges": ["a -> b", "b -> c"]})
        parts = {e["part"]: e for e in self.members("old")}
        self.assertEqual(self.root("old")["block"], "graph")
        self.assertEqual(parts["a"]["id"], old_ids["old.a"])
        self.assertEqual(parts["b"]["id"], old_ids["old.b"])
        self.assertIn("c", parts)
        self.assertEqual(len([p for p in parts if p.startswith("e:")]), 2, "the old edge was matched, not duplicated")

    def rewrite(self, elements):
        """Store the elements as a 0.21 build would have (one appended event; the scene folds from the log)."""
        version = C.current_version(self.team)
        event = {"v": C.SCHEMA, "seq": version + 1, "ts": "2026-09-28T00:00:00Z", "batch": "B-900", "author": test_canvas.WORKER.to_event(),
                 "op": "legacy", "index": 0, "intent": "a 0.21 graph", "ids": [el["id"] for el in elements],
                 "changes": [{"target": "element", "id": el["id"], "action": "update", "value": el} for el in elements]}
        C._append_events(self.team, [event])
        C._write_scene(self.team, C._load_state(self.team), 0)


class Pins(GraphRig):
    def test_a_dragged_node_is_pinned_and_nothing_else_moves(self):
        self.ok(CHECKOUT)
        before = {e["part"]: C.bounds(e) for e in self.members("checkout") if e["type"] != "arrow"}
        db = self.part("checkout", "db")
        self.ok({"op": "move", "id": db["id"], "by": [160, 0], "intent": "drag"}, HUMAN)
        db = self.part("checkout", "db")
        self.assertEqual(db["pin"]["by"], "human")
        after = {e["part"]: C.bounds(e) for e in self.members("checkout") if e["type"] != "arrow"}
        for part in ("user", "api", "ok", "fail"):
            self.assertEqual(after[part], before[part], part)
        edge = self.part("checkout", "e:ok->db")
        self.assertLessEqual(edge["points"][-1][0], C.bounds(db)[0], "the edge follows the node")
        # an agent's patch lays the rest out again but never moves the person's pin
        self.ok({"op": "patch", "id": "checkout", "add": {"nodes": ["extra"], "edges": ["fail -> extra"]}, "intent": "t"})
        self.assertEqual(C.bounds(self.part("checkout", "db")), C.bounds(db))
        refusal = self.refused({"op": "unpin", "id": db["id"], "intent": "t"})
        self.assertEqual(refusal["code"], "pin_held")
        self.ok({"op": "unpin", "id": db["id"], "intent": "let it go"}, HUMAN)
        self.assertIsNone(self.part("checkout", "db").get("pin"))
        self.assertNotEqual(C.bounds(self.part("checkout", "db")), C.bounds(db), "unpinned, it is laid out again")


class RoundTrip(GraphRig):
    """T-B1 for graphs: the readback normalizes to what the op normalized to, and writing it back changes nothing."""

    OPS = [CHECKOUT,
           {"op": "graph", "id": "legacy", "title": "Deps", "at": [0, 2000], "intent": "t",
            "nodes": [{"id": "a", "text": "API"}, {"id": "b", "text": "DB", "kind": "ellipse", "fill": "green"}, "c"],
            "edges": [{"from": "a", "to": "b", "label": "reads"}, ["a", "c"], {"from": "c", "to": "c", "dash": True}]},
           {"op": "graph", "id": "par", "at": [3000, 0], "intent": "t", "layout": "tree", "direction": "down", "gap": "l",
            "groups": [{"id": "g"}], "nodes": ["a", {"id": "b", "in": "g"}],
            "edges": ["a -> b", "a -> b: again", {"from": "b", "to": "a", "style": "dotted", "tone": "danger"}]}]

    def test_every_graph_round_trips(self):
        kctx = C._KindCtx(None)
        kind = R.get("graph")
        for op in self.OPS:
            self.ok(op)
            with self.subTest(graph=op["id"]):
                built = B.comparable(B.normalized(kctx, kind, op))
                read = B.comparable(B.normalized(kctx, kind, self.spec(op["id"])))
                self.assertEqual(read, built)

    def test_writing_the_readback_changes_nothing(self):
        self.ok(CHECKOUT)
        self.ok({"op": "patch", "id": "checkout", "remove": {"nodes": ["fail"], "edges": ["ok -> fail", "fail --> user"]},
                 "add": {"edges": ["db -> user"]}, "intent": "t"})
        spec = self.spec("checkout")
        before = {e["id"]: C.bounds(e) for e in self.members("checkout")}
        self.ok(dict(spec, intent="write it back"))
        self.assertEqual(self.spec("checkout"), spec)
        self.assertEqual({e["id"]: C.bounds(e) for e in self.members("checkout")}, before)

    def test_look_reads_the_graph_back_as_its_op(self):
        self.ok(CHECKOUT)
        text = C.look_text(C.look(self.layout, self.team, "alpha-worker"))
        self.assertIn('"op":"graph"', text.replace(" ", ""))
        self.assertIn("user -> api: HTTPS", text)


class Checks(unittest.TestCase):
    def test_crossings_high_offers_a_full_relayout_when_it_would_help(self):
        root = {"id": "E-1", "type": "frame", "block": "graph", "alias": "g", "x": 0, "y": 0, "w": 900, "h": 900, "settings": {}}
        members = []
        # Four parallel pairs drawn as a braid: every edge crosses every other one.
        for i in range(4):
            members.append({"id": "E-{}".format(10 + i), "type": "box", "group": "E-1", "part": "a{}".format(i), "frame": "E-1",
                            "x": 200 * i, "y": 0, "w": 160, "h": 60})
            members.append({"id": "E-{}".format(20 + i), "type": "box", "group": "E-1", "part": "b{}".format(i), "frame": "E-1",
                            "x": 200 * (3 - i), "y": 400, "w": 160, "h": 60})
        for i in range(4):
            members.append({"id": "E-{}".format(30 + i), "type": "arrow", "group": "E-1", "part": "e:a{0}->b{0}".format(i), "frame": "E-1",
                            "from": "E-{}".format(10 + i), "to": "E-{}".format(20 + i),
                            "points": [[200 * i + 80, 64], [200 * (3 - i) + 80, 396]]})
        by_id = {el["id"]: el for el in [root] + members}
        [problem] = G.crossings_high(root, {"by_id": by_id})
        # The repair is the ``graph`` op's own ``relayout``, not a ``patch``: re-issuing the drawing is what an agent
        # reaches for, and before ``relayout`` was a field of ``graph`` it was byte for byte a no-op.
        self.assertEqual(problem["fix"], {"op": "graph", "id": "g", "relayout": "full", "intent": "draw g again from scratch"})
        self.assertIn("6 edge crossings a reader can see", problem["message"])
        self.assertIn("worst:", problem["message"])


class Mermaid(GraphRig):
    def test_a_flowchart_is_a_graph_block(self):
        applied = self.ok({"op": "mermaid", "id": "flow", "at": [0, 0], "intent": "t",
                           "source": "flowchart LR\n A[Start] --> B{ok?}\n subgraph s1 [Inner]\n C\n end\n B -- yes --> C\n C ==> D"})
        root = self.root("flow")
        self.assertEqual((root["block"], root["settings"]["direction"]), ("graph", "right"))
        spec = self.spec("flow")
        self.assertEqual(spec["groups"], [{"id": "s1", "title": "Inner"}])
        self.assertIn("B -> C: yes", spec["edges"])
        self.assertIn({"from": "C", "to": "D", "thick": True}, spec["edges"])
        self.assertEqual(self.part("flow", "e:C->D")["style"]["width"], 4)
        self.assertEqual(applied["alias"], "flow")


class NoEdgeThroughANode(GraphRig):
    """T-R6: after routing, no graph scene and no layout fixture has an edge through a node."""

    def test_graph_scenes(self):
        for name in ("graph-groups", "flowchart", "architecture"):
            with self.subTest(scene=name):
                self.setUp()
                doc = json.loads((SCENES / (name + ".json")).read_text())
                for batch in doc.get("batches") or [doc["ops"]]:
                    result = self.apply(batch)
                    self.assertEqual(result["refused"], [])
                self.assertEqual(self.problems(("arrow_through",)), [])

    def test_layout_fixtures_drawn_as_graphs(self):
        for index, path in enumerate(sorted(LAYOUT_FIXTURES.glob("*.json"))):
            fixture = json.loads(path.read_text())
            with self.subTest(fixture=fixture["name"]):
                self.ok({"op": "graph", "id": "f{}".format(index), "at": [0, 3000 * index], "intent": "t",
                         "nodes": [n["id"] for n in fixture["nodes"]], "edges": ["{} -> {}".format(a, b) for a, b in fixture["edges"]]})
        self.assertEqual(self.problems(("arrow_through",)), [])


if __name__ == "__main__":
    unittest.main()
