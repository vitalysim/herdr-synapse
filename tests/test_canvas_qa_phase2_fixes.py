"""Regressions for the canvas v2 phase 2 QA findings (``.local/qa/phase2/qa-report.md``): each test names its finding."""
from __future__ import annotations

import json
from pathlib import Path

from test_canvas import OPERATOR, WORKER, CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_blocks as B
from herdr_team import canvas_check as K
from herdr_team import canvas_display as D

KANBAN = {"op": "kanban", "id": "work", "title": "Work", "at": [0, 0], "intent": "t",
          "columns": [{"id": "todo", "title": "Todo", "cards": ["A", "B"]}, {"id": "doing", "title": "Doing", "cards": ["C"]},
                      {"id": "done", "title": "Done", "cards": ["D"]}]}
GRAPH = {"op": "graph", "id": "g", "at": [0, 0], "intent": "t", "direction": "right",
         "nodes": [{"id": "api", "text": "API"}, {"id": "log", "text": "Log"}, {"id": "db", "text": "DB"}, {"id": "user", "text": "User"}],
         "edges": ["user -> api", "api -> log", "log -> db", "api -> db"]}


class Base(CanvasRig):
    def elements(self):
        return self.scene()["elements"]

    def root(self, alias):
        return next(e for e in self.elements() if e.get("alias") == alias)

    def spec(self, alias):
        return B.spec_of(self.elements(), self.root(alias))

    def members(self, alias):
        rid = self.root(alias)["id"]
        return [e for e in self.elements() if e.get("group") == rid]


class F1GroupedGraphs(Base):
    def test_the_reported_grouped_graph_lays_out(self):
        op = json.loads((Path(__file__).resolve().parent / "fixtures" / "qa_phase2" / "grouped-swap.json").read_text())
        applied = self.ok(dict(op, at=[0, 0]))
        self.assertTrue(applied["ids"])


class F2Adoption(Base):
    def test_cards_joining_a_column_take_new_parts_and_the_board_stays_patchable(self):
        self.ok(KANBAN)
        for title in ("Loose 1", "Loose 2", "Loose 3"):
            self.ok({"op": "card", "title": title, "in": "work.doing", "intent": "t"})
        parts = sorted(e["part"] for e in self.members("work") if e.get("type") == "card")
        self.assertEqual(parts, ["c1", "c2", "c3", "c4", "c5", "c6", "c7"])
        self.assertEqual(self.root("work")["seq"]["c"], 7)
        aliases = {e.get("alias") for e in self.members("work") if e.get("type") == "card"}
        self.assertTrue({"work.c5", "work.c6", "work.c7"} <= aliases, aliases)
        self.ok({"op": "patch", "id": "work", "add": {"cards": [{"title": "E", "in": "done"}]}, "intent": "t"})
        self.assertIn("c8", {e["part"] for e in self.members("work") if e.get("type") == "card"})
        self.ok({"op": "patch", "id": "work", "remove": {"cards": ["c6"]}, "intent": "t"})


class F4Removal(Base):
    def test_removing_a_node_takes_its_edges(self):
        self.ok(GRAPH)
        result = self.apply([{"op": "patch", "id": "g", "remove": {"nodes": ["log"]}, "intent": "t"}])
        self.assertEqual(result["refused"], [])
        edges = self.spec("g")["edges"]
        self.assertEqual(sorted(edges), ["api -> db", "user -> api"])
        self.assertTrue(any(w["code"] == "removed_with" for w in result.get("warnings") or []), result.get("warnings"))
        self.ok({"op": "patch", "id": "g", "add": {"nodes": [{"id": "cache", "text": "Cache"}]}, "intent": "t"})

    def test_removing_a_column_moves_its_cards_to_the_first(self):
        self.ok(KANBAN)
        self.ok({"op": "patch", "id": "work", "remove": {"columns": ["doing"]}, "intent": "t"})
        columns = self.spec("work")["columns"]
        self.assertEqual([c["title"] for c in columns], ["Todo", "Done"])
        self.assertEqual(columns[0]["cards"], ["A", "B", "C"])

    def test_removing_a_participant_takes_its_messages_and_renumbers_groups(self):
        self.ok({"op": "sequence", "id": "s", "at": [0, 0], "intent": "t", "participants": ["u", "api", "db"],
                 "messages": ["u -> api: go", "api -> db: read", "db --> api: rows", "api --> u: done"],
                 "notes": [{"over": ["api", "db"], "text": "hot path"}, {"right_of": "u", "text": "waits", "after": 4}],
                 "groups": [{"kind": "loop", "label": "retry", "from": 2, "to": 3}, {"kind": "opt", "from": 1, "to": 4}]})
        self.ok({"op": "patch", "id": "s", "remove": {"participants": ["db"]}, "intent": "t"})
        spec = self.spec("s")
        texts = [m.split(": ")[-1] if isinstance(m, str) else m["text"] for m in spec["messages"]]
        self.assertEqual(texts, ["go", "done"])
        self.assertEqual(len(spec.get("groups") or []), 1, "the loop spanned only db's messages")
        self.assertEqual((spec["groups"][0]["from"], spec["groups"][0]["to"]), (1, 2))
        notes = spec.get("notes") or []
        self.assertEqual(notes[0]["over"], ["api"])
        self.assertEqual(notes[1]["after"], 2)

    def test_removing_a_table_column_drops_its_cells(self):
        self.ok({"op": "table", "id": "t", "at": [0, 0], "intent": "t", "columns": ["Risk", "Owner"], "rows": [["a", "b"]]})
        self.ok({"op": "patch", "id": "t", "remove": {"columns": ["c2"]}, "intent": "t"})
        self.assertEqual(len(self.spec("t")["columns"]), 1)

    def test_a_node_dragged_out_of_its_graph_leaves_with_loose_arrows(self):
        self.ok(GRAPH)
        user = next(e for e in self.members("g") if e.get("part") == "user")
        before = self.root("g")
        self.ok({"op": "move", "id": user["id"], "to": [before["x"] + before["w"] + 1600, before["y"]], "frame": None, "intent": "t"},
                OPERATOR)
        spec = self.spec("g")
        self.assertNotIn("user", [n if isinstance(n, str) else n["id"] for n in spec["nodes"]])
        self.assertFalse(any("user" in e for e in spec["edges"] if isinstance(e, str)), spec["edges"])
        moved = self.el(user["id"])
        self.assertIsNone(moved.get("group"))
        loose = [e for e in self.elements() if e.get("type") == "arrow" and user["id"] in (e.get("from"), e.get("to"))]
        self.assertEqual(len(loose), 1)
        self.assertIsNone(loose[0].get("group"))
        self.assertIsNone(loose[0].get("frame"))
        after = self.root("g")
        self.assertLessEqual(after["w"], before["w"] + 40, "the graph does not stay stretched to hold the departed node")
        self.ok({"op": "patch", "id": "g", "add": {"nodes": [{"id": "cache", "text": "Cache"}]}, "intent": "t"})
        # The readback builds the same graph again.
        self.ok(dict(self.spec("g"), intent="t"))


class F16ColumnDelete(Base):
    def test_deleting_a_column_keeps_its_cards_on_the_board(self):
        self.ok(KANBAN)
        doing = next(e for e in self.members("work") if e.get("part") == "doing")
        self.ok({"op": "delete", "id": doing["id"], "intent": "t"}, OPERATOR)
        columns = self.spec("work")["columns"]
        self.assertEqual([c["title"] for c in columns], ["Todo", "Done"])
        self.assertEqual(columns[0]["cards"], ["A", "B", "C"])
        codes = [p["code"] for p in C.check(self.layout, self.team, "alpha-worker")["problems"]]
        self.assertNotIn("frame_edge", codes)


class F3StickyFit(Base):
    def test_a_sticky_draws_the_lines_its_fit_stored_at_every_length(self):
        from herdr_team import canvas_kinds

        base = "Customers churn after the second invoice because the price jump is not explained anywhere in the app. "
        for count in (100, 150, 200, 400, 1000):
            for size in ("s", "m", "l"):
                with self.subTest(count=count, size=size):
                    eid = self.ok({"op": "sticky", "text": (base * 20)[:count], "size": size, "at": [0, 0], "intent": "t"})["ids"][0]
                    el = self.el(eid)
                    drawn = canvas_kinds.drawn(el)
                    self.assertEqual(list(drawn.lines), el["fit"]["lines"])
                    self.assertEqual(drawn.size, el["fit"]["size"])
                    codes = [p["code"] for p in C.check(self.layout, self.team, "alpha-worker")["problems"] if eid in p.get("ids", [])]
                    self.assertNotIn("label_overflow", codes)
                    self.ok({"op": "delete", "id": eid, "intent": "t"})


class F7OthersContainers(Base):
    def test_an_agent_may_not_add_to_the_operators_section(self):
        self.ok({"op": "section", "id": "osec", "title": "Operator section", "layout": "row", "at": [0, 0], "intent": "t"}, OPERATOR)
        self.ok({"op": "card", "id": "oc", "title": "Operator card", "in": "osec", "intent": "t"}, OPERATOR)
        before = {k: self.root("osec")[k] for k in ("x", "y", "w", "h")}
        refused = self.refused({"op": "card", "title": "Agent card", "body": "text " * 100, "in": "osec", "intent": "t"})
        self.assertEqual(refused["code"], "element_not_yours")
        mine = self.ok({"op": "card", "title": "Agent card", "at": [2000, 0], "intent": "t"})["ids"][0]
        self.assertEqual(self.refused({"op": "place", "id": mine, "in": "osec", "intent": "t"})["code"], "element_not_yours")
        self.assertEqual({k: self.root("osec")[k] for k in ("x", "y", "w", "h")}, before)

    def test_the_operator_may_still_add_an_agents_card(self):
        self.ok({"op": "section", "id": "osec", "title": "Operator section", "layout": "row", "at": [0, 0], "intent": "t"}, OPERATOR)
        mine = self.ok({"op": "card", "title": "Agent card", "at": [2000, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "place", "id": mine, "in": "osec", "intent": "t"}, OPERATOR)
        self.assertEqual(self.el(mine)["frame"], self.root("osec")["id"])


class F8PlaceInKanban(Base):
    def test_a_card_placed_in_the_board_joins_a_column_not_the_columns(self):
        self.ok(KANBAN)
        result = self.apply([{"op": "place", "id": "work.c1", "in": "work", "intent": "t"}])
        self.assertEqual(result["refused"], [])
        spec = self.spec("work")
        self.assertEqual([c["title"] for c in spec["columns"]], ["Todo", "Doing", "Done"])
        self.assertEqual(sum(len(c["cards"]) for c in spec["columns"]), 4)
        loose = self.ok({"op": "card", "title": "Loose", "at": [3000, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "place", "id": loose, "in": "work", "intent": "t"})
        spec = self.spec("work")
        self.assertEqual(len(spec["columns"]), 3)
        self.assertEqual(sum(len(c["cards"]) for c in spec["columns"]), 5)
        # The readback builds the same board again.
        self.ok(dict(spec, intent="t"))
        self.assertEqual(self.spec("work"), spec)


class F15HandMove(Base):
    def test_dragging_one_node_moves_no_other_and_its_group_follows(self):
        self.ok({"op": "graph", "id": "inc", "at": [0, 0], "intent": "t", "layout": "flow", "direction": "right",
                 "groups": [{"id": "eng", "title": "Engineering"}],
                 "nodes": [{"id": "sev", "text": "Customer impact?", "kind": "diamond"}, {"id": "war", "text": "Open incident channel", "in": "eng"},
                           {"id": "fix", "text": "Ship the mitigation", "in": "eng"}, {"id": "log", "text": "Log it for the weekly review"}],
                 "edges": ["sev -> war: yes", "sev -> log: no", "war -> fix"], "same_rank": [["war", "log"]]})
        before = {e["alias"]: (e["x"], e["y"]) for e in self.elements() if e.get("alias") and e.get("type") != "arrow"}
        war = self.root("inc.war")
        result = self.apply([{"op": "move", "ids": [war["id"]], "by": [0, 60], "intent": "drag"}], OPERATOR)
        self.assertEqual([w["code"] for w in result.get("warnings") or [] if w["code"] == "frame_edge"], [])
        after = {e["alias"]: (e["x"], e["y"]) for e in self.elements() if e.get("alias") and e.get("type") != "arrow"}
        moved = {a for a in before if before[a] != after[a]}
        self.assertEqual(moved - {"inc.eng"}, {"inc.war"})
        eng = self.root("inc.eng")
        war = self.root("inc.war")
        self.assertTrue(eng["y"] <= war["y"] and war["y"] + war["h"] <= eng["y"] + eng["h"])


class F17TimelineAdoption(Base):
    def test_a_card_dropped_on_a_dated_timeline_gets_a_date(self):
        self.ok({"op": "timeline", "id": "t", "at": [0, 0], "intent": "t",
                 "events": [{"at": "2026-01-01", "title": "Start"}, {"at": "2026-03-01", "title": "Middle"}, {"at": "2026-05-01", "title": "End"}]})
        self.ok({"op": "card", "title": "Dropped", "in": "t", "intent": "t"})
        spec = self.spec("t")
        self.assertTrue(all(isinstance(e, dict) and e.get("at") for e in spec["events"]), spec["events"])
        self.assertEqual(self.root("t")["axis"]["mode"], "time")
        codes = [p["code"] for p in C.check(self.layout, self.team, "alpha-worker")["problems"]]
        self.assertNotIn("date_unparsed", codes)


ROW = [{"op": "section", "id": "pay", "title": "Payments overview", "tone": "info", "layout": "row", "at": [0, 0], "intent": "t"},
       {"op": "card", "in": "pay", "id": "web", "title": "Web checkout", "body": "Collects card details with the hosted form", "intent": "t"},
       {"op": "card", "in": "pay", "id": "psp", "title": "Payment provider", "body": "- authorises\n- captures\n- refunds", "intent": "t"},
       {"op": "card", "in": "pay", "id": "ledger", "title": "Ledger", "body": "Double-entry books, append only", "intent": "t"},
       {"op": "arrow", "from": "web", "to": "psp", "label": "token", "route": "orthogonal", "intent": "t"},
       {"op": "arrow", "from": "psp", "to": "ledger", "label": "webhook", "route": "orthogonal", "intent": "t"}]


class F5AdjacentRoutes(Base):
    def test_arrows_between_neighbours_in_a_row_are_straight_with_their_labels_on_them(self):
        for gap in (None, "m", "l"):
            with self.subTest(gap=gap):
                self.setUp()
                ops = [dict(ROW[0], gap=gap) if gap else ROW[0]] + ROW[1:]
                result = self.apply(ops)
                self.assertEqual(result["refused"], [])
                for arrow in (e for e in self.elements() if e.get("type") == "arrow"):
                    points = arrow["points"]
                    self.assertEqual(len(points), 2, points)
                    self.assertEqual(points[0][1], points[1][1])
                    self.assertLess(points[0][0], points[1][0])
                    self.assertAlmostEqual(arrow["label_at"][1], points[0][1], delta=1)
                    self.assertTrue(points[0][0] < arrow["label_at"][0] < points[1][0])
                codes = [p["code"] for p in C.check(self.layout, self.team, "alpha-worker")["problems"]]
                self.assertFalse({"route_loop", "label_astray", "arrow_through"} & set(codes), codes)

    def test_the_router_never_hooks_back_between_close_facing_ends(self):
        from herdr_team import canvas_routers as R

        a = R.End(box=(0.0, 0.0, 100.0, 60.0), id="a")
        b = R.End(box=(120.0, 0.0, 220.0, 80.0), id="b")
        found = R.route("orthogonal", R.RouteRequest(id="e", a=a, b=b, clearance=20.0, radius=8.0))
        xs = [p[0] for p in found.points]
        self.assertEqual(xs, sorted(xs), found.points)
        offset = R.End(box=(120.0, 50.0, 220.0, 110.0), id="c")
        found = R.route("orthogonal", R.RouteRequest(id="e", a=a, b=offset, clearance=20.0, radius=8.0))
        xs = [p[0] for p in found.points]
        self.assertEqual(xs, sorted(xs), found.points)

    def test_check_names_a_loop_and_a_label_far_from_its_line(self):
        self.ok({"op": "shape", "id": "a", "text": "A", "at": [0, 0], "intent": "t"})
        self.ok({"op": "shape", "id": "b", "text": "B", "at": [600, 0], "intent": "t"})
        arrow = self.ok({"op": "arrow", "from": "a", "to": "b", "label": "go", "intent": "t"})["ids"][0]
        el = self.el(arrow)
        x0, y0 = el["points"][0]
        x1, y1 = el["points"][-1]
        self.ok({"op": "move", "id": arrow, "points": [[x0, y0], [x0 + 40, y0], [x0 + 40, y0 - 40], [x0 - 60, y0 - 40], [x0 - 60, y0], [x1, y1]],
                 "intent": "t"})
        codes = [p["code"] for p in C.check(self.layout, self.team, "alpha-worker")["problems"]]
        self.assertIn("route_loop", codes)


class F9DetourThroughNodes(Base):
    def test_force_grid_and_radial_graphs_route_around_nodes(self):
        org = {"nodes": [{"id": "n{}".format(i), "text": "Person {}".format(i)} for i in range(14)],
               "edges": ["n{} -> n{}".format((i - 1) // 2, i) for i in range(1, 14)]}
        net = {"nodes": [{"id": x, "text": x.title()} for x in ("web", "api", "auth", "db", "cache", "queue", "worker", "mail")],
               "edges": ["web -> api", "api -> auth", "api -> db", "api -> cache", "api -> queue", "queue -> worker", "worker -> db",
                         "worker -> mail", "auth -> db"]}
        for name, graph, layout in (("org", org, "grid"), ("org", org, "force"), ("net", net, "radial")):
            with self.subTest(graph=name, layout=layout):
                self.setUp()
                self.ok(dict(graph, op="graph", id="g", layout=layout, at=[0, 0], intent="t"))
                codes = [p["code"] for p in C.check(self.layout, self.team, "alpha-worker")["problems"]]
                self.assertNotIn("arrow_through", codes)


class F10LevelOfDetail(Base):
    def test_shape_and_arrow_labels_follow_the_bands(self):
        self.ok({"op": "shape", "id": "a", "text": "Alpha", "at": [0, 0], "intent": "t"})
        self.ok({"op": "shape", "id": "b", "text": "Beta", "at": [600, 0], "intent": "t"})
        self.ok({"op": "arrow", "from": "a", "to": "b", "label": "verify", "intent": "t"})
        dl = D.display_list(self.scene())
        for entry in dl["entries"]:
            texts = [p for p in entry["items"] if p.get("k") == "text"]
            for prim in texts:
                self.assertFalse(D.lod_visible(prim, 0.1), (entry["kind"], prim.get("lines")))
            if entry["kind"] == "arrow":
                self.assertTrue(all(not D.lod_visible(p, 0.3) for p in texts))
                self.assertTrue(any(p.get("k") == "rect" and D.lod_visible(p, 0.3) and p.get("fill") == "base.grid" for p in entry["items"]),
                                "a skeleton bar stands in for the label")
            else:
                self.assertTrue(all(D.lod_visible(p, 0.3) for p in texts))


class F11RouteRestyle(Base):
    def test_restyling_a_curve_to_orthogonal_draws_elbows(self):
        self.ok({"op": "shape", "id": "a", "text": "A", "at": [0, 0], "intent": "t"})
        self.ok({"op": "shape", "id": "b", "text": "B", "at": [600, 300], "intent": "t"})
        arrow = self.ok({"op": "arrow", "from": "a", "to": "b", "curve": True, "intent": "t"})["ids"][0]
        self.ok({"op": "restyle", "id": arrow, "route": "orthogonal", "intent": "t"})
        el = self.el(arrow)
        self.assertFalse(el.get("curve"))
        entry = next(e for e in D.display_list(self.scene())["entries"] if e["id"] == arrow)
        d = next(p for p in entry["items"] if p.get("k") == "arrow")["d"]
        points = el["points"]
        self.assertTrue(all(a[0] == b[0] or a[1] == b[1] for a, b in zip(points, points[1:])), points)
        # Every curve piece is an elbow's rounding: its control point is one of the route's corners.
        corners = {(float(x), float(y)) for x, y in points[1:-1]}
        tokens = d.replace("M", " M ").replace("L", " L ").replace("Q", " Q ").split()
        controls = [(float(tokens[i + 1]), float(tokens[i + 2])) for i, t in enumerate(tokens) if t == "Q"]
        self.assertTrue(all(c in corners for c in controls), (d, points))


class F12Limits(Base):
    def test_a_graph_laid_out_past_the_size_limit_is_refused_by_name(self):
        import random

        rng = random.Random(7)
        nodes = [{"id": "n{}".format(i), "text": "Node {} {}".format(i, rng.choice(["api", "database", "a worker queue"]))} for i in range(199)]
        pairs = set()
        for i in range(1, 199):
            pairs.add((rng.randrange(0, i), i))
        while len(pairs) < 399:
            a, b = rng.randrange(199), rng.randrange(199)
            if a != b:
                pairs.add((min(a, b), max(a, b)))
        result = self.apply([{"op": "graph", "id": "g", "nodes": nodes, "edges": ["n{} -> n{}".format(a, b) for a, b in sorted(pairs)],
                              "at": [0, 0], "intent": "t"}])
        if result["refused"]:
            self.assertEqual(result["refused"][0]["code"], "canvas_limit")
            self.assertIn("split it", result["refused"][0]["message"])
        else:
            root = self.root("g")
            self.assertLessEqual(max(root["w"], root["h"]), C.MAX_BLOCK_SIZE)

    def test_force_on_500_nodes_is_quick(self):
        import random
        import time

        from herdr_team import canvas_layouts as L

        rng = random.Random(7)
        nodes = tuple(L.LNode(id="n{}".format(i), w=float(rng.choice((160, 200))), h=60.0, order=i) for i in range(500))
        edges = tuple(L.LEdge(id="e{}".format(i), a="n{}".format(rng.randrange(0, i)), b="n{}".format(i)) for i in range(1, 500))
        start = time.perf_counter()
        result = L.run("force", L.LayoutRequest(nodes=nodes, edges=edges, incremental=False))
        self.assertLess(time.perf_counter() - start, 6.0, "the grid variant keeps a round near linear (budget 2 s on the dev Mac)")
        self.assertEqual(len(result.positions), 500)


class F18MindmapIds(Base):
    def test_look_full_names_each_topic_by_its_text(self):
        self.ok({"op": "mindmap", "id": "m", "title": "Launch", "at": [0, 0], "intent": "t", "tree": {"Audience": ["Devs"], "Channels": []}})
        lines = B.block_lines(self.elements(), self.root("m"), "alpha-worker", True)
        ids = next(line for line in lines if line.strip().startswith("ids:"))
        self.assertIn('"Audience"', ids)
        self.assertRegex(ids, r't1=E-\d+ "Audience"')


class F19ResultLines(Base):
    def test_a_create_moves_nothing_and_pins_print_parts(self):
        applied = self.ok({"op": "graph", "id": "g", "at": [0, 0], "intent": "t", "nodes": ["a", "b", "c"], "edges": ["a -> b", "b -> c"]})
        self.assertEqual(applied["block"]["moved"], [])
        b = self.root("g.b")
        result = self.apply([{"op": "move", "ids": [b["id"]], "by": [0, 40], "intent": "drag"}], OPERATOR)
        pinned = self.apply([{"op": "pin", "id": "g.c", "intent": "t"}])
        entry = pinned["applied"][0]
        self.assertEqual(entry["block"]["kind"], "graph")
        text = C.apply_text(pinned)
        self.assertIn("c(agent)", text)
        self.assertIn("b(human)", text)
        self.assertEqual(result["refused"], [])


class F20Messages(Base):
    def test_alias_names_id_and_a_bad_group_range_says_what_it_spans(self):
        refused = self.refused({"op": "card", "alias": "x", "title": "T", "at": [0, 0], "intent": "t"})
        self.assertIn("did you mean 'id'", refused["message"])
        refused = self.refused({"op": "sequence", "id": "s", "at": [0, 0], "intent": "t", "participants": ["a", "b"], "messages": ["a -> b: hi"],
                                "groups": [{"kind": "loop", "from": 1, "to": 3}]})
        self.assertIn("spans messages 1 to 3", refused["message"])

    def test_stretch_keeps_a_sticky_square(self):
        self.ok({"op": "section", "id": "s", "title": "Ideas", "layout": "row", "align": "stretch", "at": [0, 0], "intent": "t"})
        self.ok({"op": "card", "in": "s", "title": "Tall", "body": "line " * 80, "intent": "t"})
        sticky = self.ok({"op": "sticky", "in": "s", "text": "Short", "intent": "t"})["ids"][0]
        el = self.el(sticky)
        self.assertEqual(el["w"], el["h"])

    def test_an_agent_move_that_changes_no_stack_order_says_so(self):
        self.ok(KANBAN)
        result = self.apply([{"op": "move", "id": "work.c1", "by": [0, 4], "intent": "t"}])
        self.assertIn("stack_order", [w["code"] for w in result.get("warnings") or []])


class F14QaTool(Base):
    def test_canvas_qa_probes_tables_and_sequences_and_gates_the_new_route_checks(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("canvas_qa_f14", Path(__file__).resolve().parent.parent / "tools" / "canvas_qa.py")
        qa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qa)
        self.ok({"op": "table", "id": "t", "at": [0, 0], "intent": "t", "columns": ["Risk", "Owner"], "rows": [["a", "b"]]})
        self.ok({"op": "sequence", "id": "s", "at": [0, 600], "intent": "t", "participants": ["u", "api"], "messages": ["u -> api: go"]})
        found = qa._entries_with_text(self.scene())
        self.assertIn(self.root("t")["id"], found)
        self.assertIn(self.root("s")["id"], found)
        self.assertTrue({"route_loop", "label_astray"} <= set(qa.TRACKED_CHECKS))
        self.assertGreaterEqual(qa.PAGE_V2_SCALE, D.LOD_TITLES)
