"""The block pipeline (canvas v2 phase 2, 1.5, 1.8 and 5): create, upsert, patch, the block version, adoption, detaching,
stacks and stretch, growth that honours pins, the result's geometry and check, and ``look`` reading a block back."""
from __future__ import annotations

import json

from test_canvas import OPERATOR, REVIEWER, CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_blocks as B
from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R

KANBAN = {"op": "kanban", "id": "work", "title": "Launch work", "at": [0, 0], "intent": "track launch tasks",
          "columns": [{"id": "todo", "title": "Todo", "cards": ["Rotate API keys", {"title": "Landing page copy v3", "badges": ["P0"]}]},
                      {"id": "doing", "title": "Doing", "tone": "warning", "limit": 3, "cards": [{"title": "Gateway JWT check", "status": "blocked"}]},
                      {"id": "done", "title": "Done", "tone": "success", "cards": ["Persona research"]}]}


class BlockRig(CanvasRig):
    def by_alias(self, alias):
        return next(e for e in self.scene()["elements"] if e.get("alias") == alias)

    def members(self, root_id):
        return [e for e in self.scene()["elements"] if e.get("group") == root_id]

    def part(self, root_id, part):
        return next(e for e in self.members(root_id) if e.get("part") == part)

    def spec(self, alias):
        scene = self.scene()["elements"]
        root = next(e for e in scene if e.get("alias") == alias)
        return B.spec_of(scene, root)


class Create(BlockRig):
    def test_a_block_op_builds_its_members_and_arranges_them(self):
        applied = self.ok(KANBAN)
        root = self.by_alias("work")
        self.assertEqual((root["type"], root["block"], root["text"]), ("frame", "kanban", "Launch work"))
        self.assertEqual(applied["block"]["kind"], "kanban")
        self.assertFalse(applied["block"]["upsert"])
        parts = sorted(e["part"] for e in self.members(root["id"]))
        self.assertEqual(parts, ["c1", "c2", "c3", "c4", "doing", "done", "todo"])
        todo = self.part(root["id"], "todo")
        self.assertEqual((todo["type"], todo["block"], todo["alias"]), ("frame", "section", "work.todo"))
        c2 = self.part(root["id"], "c2")
        self.assertEqual((c2["type"], c2["frame"], c2["alias"]), ("card", todo["id"], "work.c2"))
        # The columns stack left to right, the cards top to bottom, all inside the root (it hugged them).
        columns = [self.part(root["id"], p) for p in ("todo", "doing", "done")]
        self.assertEqual([c["y"] for c in columns], [columns[0]["y"]] * 3)
        self.assertLess(columns[0]["x"], columns[1]["x"])
        for column in columns:
            self.assertTrue(C._contains(C.bounds(root), C.bounds(column)))
        cards = [self.part(root["id"], p) for p in ("c1", "c2")]
        self.assertLess(cards[0]["y"] + cards[0]["h"], cards[1]["y"] + 1)
        self.assertEqual(cards[0]["w"], cards[1]["w"], "align stretch: every card in a column is as wide")

    def test_refusals_name_the_field(self):
        refused = self.refused(dict(KANBAN, columns=[{"title": "Todo", "cards": [{"title": "x", "colour": "red"}]}]))
        self.assertEqual(refused["details"]["field"], "columns[0].cards[0].colour")
        refused = self.refused({"op": "table", "columns": ["a"], "rows": [["1", "2"]], "at": [0, 0], "intent": "t"})
        self.assertEqual(refused["details"]["field"], "rows[0].cells")
        refused = self.refused({"op": "kanban", "columns": [], "at": [0, 0], "intent": "t"})
        self.assertEqual(refused["details"]["field"], "columns")

    def test_the_result_has_the_final_geometry_and_the_check(self):
        result = self.apply([KANBAN, {"op": "sticky", "text": "a thought", "right_of": "work", "intent": "t"}])
        self.assertEqual(result["refused"], [])
        ids = {g["id"] for g in result["geometry"]}
        root = self.by_alias("work")
        self.assertIn(root["id"], ids)
        final = next(g for g in result["geometry"] if g["id"] == root["id"])
        self.assertEqual((final["x"], final["y"], final["w"], final["h"]), (root["x"], root["y"], root["w"], root["h"]))
        self.assertEqual(set(result["check"]) >= {"region", "counts", "problems"}, True)
        self.assertEqual(result["check"]["counts"]["overlap"], 0)
        self.assertIn("block", result["applied"][0])

    def test_apply_text_prints_the_block_and_the_check(self):
        text = C.apply_text(self.apply([KANBAN]))
        self.assertIn("kanban", text)
        self.assertIn("check:", text)


class Upsert(BlockRig):
    def test_the_same_id_reconciles_the_block_matching_by_key_then_text(self):
        self.ok(KANBAN)
        root = self.by_alias("work")
        before = {e["part"]: e["id"] for e in self.members(root["id"])}
        applied = self.ok({"op": "kanban", "id": "work", "title": "Launch work", "intent": "again",
                           "columns": [{"id": "todo", "title": "Todo", "cards": ["Rotate API keys", "Brand new card"]},
                                       {"id": "done", "title": "Done", "cards": ["Persona research"]}]})
        self.assertTrue(applied["block"]["upsert"])
        after = {e["part"]: e["id"] for e in self.members(root["id"])}
        self.assertEqual(after["c1"], before["c1"], "matched by key: the same element")
        persona = next(e for e in self.members(root["id"]) if e.get("text") == "Persona research")
        self.assertEqual(persona["id"], before["c4"], "matched by text: its generated key moved, the element stayed")
        self.assertNotIn("doing", after, "unmatched members are dropped")
        new = next(e for e in self.members(root["id"]) if e.get("text") == "Brand new card")
        self.assertEqual(new["part"], "c5", "a new item never reuses an id the block has used")
        self.assertEqual(self.by_alias("work")["id"], root["id"])

    def test_another_kind_under_that_alias_is_refused(self):
        self.ok(KANBAN)
        refused = self.refused({"op": "table", "id": "work", "columns": ["a"], "rows": [], "at": [0, 0], "intent": "t"})
        self.assertEqual(refused["code"], "alias_taken")

    def test_if_version_is_the_block_version(self):
        self.ok(KANBAN)
        root = self.by_alias("work")
        stale = self.refused({"op": "patch", "id": "work", "if_version": root["updated_seq"] - 1, "add": {"cards": ["x"]}, "intent": "t"})
        self.assertEqual(stale["code"], "canvas_stale")
        self.ok({"op": "patch", "id": "work", "if_version": root["updated_seq"], "add": {"cards": ["x"]}, "intent": "t"})
        # A member's change bumps the block's version too.
        version = self.by_alias("work")["updated_seq"]
        card = self.part(root["id"], "c1")
        self.ok({"op": "edit", "id": card["id"], "text": "Rotate every API key", "intent": "t"})
        self.assertGreater(self.by_alias("work")["updated_seq"], version)

    def test_another_members_block_is_not_theirs(self):
        self.ok(KANBAN)
        # Phase 5 (D11): a peer's patch of another member's block is a proposal for the operator (it was refused).
        proposed = self.proposed({"op": "patch", "id": self.by_alias("work")["id"], "add": {"cards": ["x"]}, "intent": "t"}, author=REVIEWER)
        self.assertEqual(proposed["reason"], "peer")


class Patch(BlockRig):
    def setUp(self):
        super().setUp()
        self.ok(KANBAN)
        self.root = self.by_alias("work")

    def test_add_update_remove_set(self):
        self.ok({"op": "patch", "id": "work", "intent": "t", "add": {"cards": [{"title": "Ship it", "in": "done"}]},
                 "update": {"cards": [{"id": "c1", "title": "Rotate the keys"}]}, "remove": {"cards": ["c3"]}})
        spec = self.spec("work")
        todo, doing, done = spec["columns"]
        self.assertEqual(todo["cards"][0], "Rotate the keys")
        self.assertEqual(doing["cards"], [])
        self.assertIn({"id": "c5", "title": "Ship it"}, done["cards"])
        self.ok({"op": "patch", "id": "work", "set": {"title": "Launch board"}, "intent": "t"})
        self.assertEqual(self.by_alias("work")["text"], "Launch board")

    def test_a_card_moves_between_columns_by_update(self):
        self.ok({"op": "patch", "id": "work", "update": {"cards": [{"id": "c2", "in": "done"}]}, "intent": "t"})
        c2 = self.part(self.root["id"], "c2")
        self.assertEqual(c2["frame"], self.part(self.root["id"], "done")["id"])
        self.assertEqual(c2["alias"], "work.c2", "the alias survives the move (D7)")

    def test_an_unknown_key_names_the_nearest(self):
        refused = self.refused({"op": "patch", "id": "work", "remove": {"cards": ["c9"]}, "intent": "t"})
        self.assertEqual(refused["code"], "part_unknown")
        self.assertIn("the nearest are", refused["message"])
        refused = self.refused({"op": "patch", "id": "work", "update": {"cards": [{"id": "cc2", "title": "x"}]}, "intent": "t"})
        self.assertEqual((refused["code"], refused["details"]["nearest"][0]), ("part_unknown", "c2"))

    def test_a_patch_needs_a_block_and_something_to_do(self):
        self.assertEqual(self.refused({"op": "patch", "id": "work", "intent": "t"})["details"]["field"], "add")
        card = self.part(self.root["id"], "c1")
        refused = self.refused({"op": "patch", "id": card["id"], "add": {"cards": ["x"]}, "intent": "t"})
        self.assertIn("patch the block (work), not its part", refused["message"])
        box = self.ok({"op": "shape", "text": "x", "at": [2000, 0], "intent": "t"})["ids"][0]
        self.assertIn("not a block", self.refused({"op": "patch", "id": box, "add": {"x": []}, "intent": "t"})["message"])

    def test_a_graph_drawn_before_blocks_is_refused_as_legacy(self):
        legacy = {"id": "E-900", "type": "frame", "text": "old", "x": 3000, "y": 0, "w": 400, "h": 300, "author": "alpha-worker",
                  "author_kind": "member", "z": 1, "updated_seq": 1}
        node = dict(legacy, id="E-901", type="box", group="E-900", frame="E-900", x=3020, y=40, w=160, h=80)
        self.apply([])  # the team exists
        scene = self.scene()
        scene["elements"] += [legacy, node]
        C.store.write_json(C._file(self.team, C.SCENE_FILE), scene)
        refused = self.refused({"op": "patch", "id": "E-900", "add": {"nodes": []}, "intent": "t"})
        self.assertEqual(refused["code"], "block_legacy")


class Stacks(BlockRig):
    def test_in_joins_a_section_at_an_index_and_moves_reorder(self):
        self.ok({"op": "section", "id": "row", "title": "Row", "layout": "row", "at": [0, 0], "intent": "t"})
        a = self.ok({"op": "card", "id": "a", "title": "A", "in": "row", "intent": "t"})["ids"][0]
        b = self.ok({"op": "card", "id": "b", "title": "B", "in": "row", "intent": "t"})["ids"][0]
        c = self.ok({"op": "card", "id": "c", "title": "C", "in": "row", "index": 0, "intent": "t"})["ids"][0]
        self.assertEqual(self.by_alias("row")["order"], [c, a, b])
        self.assertLess(self.el(c)["x"], self.el(a)["x"])
        # A drag reorders: B dropped with its centre left of A's goes before A.
        self.ok({"op": "move", "id": b, "to": [self.el(a)["x"] - 10, self.el(a)["y"]], "intent": "t"})
        self.assertEqual(self.by_alias("row")["order"], [c, b, a])
        self.ok({"op": "place", "id": a, "in": "row", "index": 0, "intent": "t"})
        self.assertEqual(self.by_alias("row")["order"], [a, c, b])

    def test_a_drag_between_kanban_columns_joins_the_column_at_the_drop(self):
        self.ok(KANBAN)
        root = self.by_alias("work")
        done = self.part(root["id"], "done")
        c1 = self.part(root["id"], "c1")
        persona = self.part(root["id"], "c4")
        # The page sends move {by, frame}: dropped just above Persona research.
        dx, dy = persona["x"] - c1["x"], persona["y"] - c1["y"] - 30
        self.ok({"op": "move", "id": c1["id"], "by": [dx, dy], "frame": done["id"], "intent": "t"}, author=OPERATOR)
        self.assertEqual(self.el(done["id"])["order"], [c1["id"], persona["id"]])
        self.assertEqual(self.spec("work")["columns"][0]["cards"], [{"id": "c2", "title": "Landing page copy v3", "badges": ["P0"]}])
        todo = self.part(root["id"], "todo")
        self.assertEqual(self.el(todo["id"])["count"], 1)

    def test_a_card_dropped_outside_leaves_the_block(self):
        self.ok(KANBAN)
        root = self.by_alias("work")
        c1 = self.part(root["id"], "c1")
        self.ok({"op": "move", "id": c1["id"], "to": [3000, 3000], "frame": None, "intent": "t"})
        loose = self.el(c1["id"])
        self.assertEqual((loose.get("group"), loose.get("part")), (None, None))
        self.assertNotIn("Rotate API keys", json.dumps(self.spec("work")))

    def test_a_sticky_dropped_in_a_column_becomes_a_card(self):
        self.ok(KANBAN)
        root = self.by_alias("work")
        sticky = self.ok({"op": "sticky", "text": "Draft the FAQ", "at": [3000, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "place", "id": sticky, "in": "work.todo", "intent": "t"})
        el = self.el(sticky)
        self.assertEqual((el["type"], el["group"], el["part"]), ("card", root["id"], "c5"))
        self.assertIn("Draft the FAQ", json.dumps(self.spec("work")))

    def test_a_card_joining_a_timeline_is_adopted_and_pinned_other_kinds_refused(self):
        self.ok({"op": "timeline", "id": "plan", "at": [0, 0], "intent": "t", "events": [{"at": "2026-10-05", "title": "Beta"}]})
        card = self.ok({"op": "card", "title": "Later", "at": [3000, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "place", "id": card, "in": "plan", "intent": "t"})
        el = self.el(card)
        self.assertEqual((el["part"], el["pin"]["by"]), ("e2", "agent"))
        box = self.ok({"op": "shape", "text": "no", "at": [3000, 400], "intent": "t"})["ids"][0]
        self.assertEqual(self.refused({"op": "place", "id": box, "in": "plan", "intent": "t"})["code"], "block_member")

    def test_deleting_a_root_without_children_detaches_its_members(self):
        self.ok(KANBAN)
        root = self.by_alias("work")
        ids = [e["id"] for e in self.members(root["id"])]
        self.ok({"op": "delete", "id": root["id"], "intent": "t"})
        for eid in ids:
            el = self.el(eid)
            self.assertEqual((el.get("group"), el.get("part")), (None, None))

    def test_deleting_a_member_removes_the_item(self):
        self.ok(KANBAN)
        root = self.by_alias("work")
        self.ok({"op": "delete", "id": self.part(root["id"], "c3")["id"], "intent": "t"})
        self.assertEqual(self.spec("work")["columns"][1]["cards"], [])


class Growth(BlockRig):
    def test_a_grown_block_pushes_its_neighbour_but_never_a_pinned_one(self):
        self.ok({"op": "section", "id": "s", "title": "S", "layout": "column", "at": [0, 0], "intent": "t"})
        right = self.ok({"op": "shape", "text": "right", "at": [400, 0], "intent": "t"})["ids"][0]
        below = self.ok({"op": "shape", "text": "below", "at": [0, 260], "intent": "t"})["ids"][0]
        self.ok({"op": "pin", "id": below, "intent": "hold it"})
        before_below = (self.el(below)["x"], self.el(below)["y"])
        result = self.apply([{"op": "card", "in": "s", "title": "A card with a long title that grows the section a lot", "body": "\n".join(
            "- line {}".format(i) for i in range(8)), "size": "l", "intent": "t"}])
        self.assertEqual(result["refused"], [])
        self.assertEqual((self.el(below)["x"], self.el(below)["y"]), before_below, "a pin is never pushed")
        codes = [w["code"] for w in result["warnings"]]
        self.assertIn("blocked_by_pin", codes)
        section = self.by_alias("s")
        self.assertFalse(C._intersects(C.bounds(section), C.bounds(self.el(right))), "the unpinned neighbour made way")


class Look(BlockRig):
    def test_a_block_reads_back_as_its_spec_with_members_folded(self):
        self.ok(KANBAN)
        text = C.look(self.layout, self.team, "alpha-worker")["text"]
        self.assertIn('kanban work "Launch work"', text)
        self.assertIn('"op":"kanban"', text)
        self.assertNotIn("card work.c1", text, "members are folded into the spec")
        full = C.look(self.layout, self.team, "alpha-worker", full=True)["text"]
        self.assertIn("ids: ", full)

    def test_look_block_prints_the_whole_spec_and_json_carries_blocks(self):
        self.ok(KANBAN)
        found = C.look(self.layout, self.team, "alpha-worker", block="work.c2")
        self.assertEqual(found["block"]["op"], "kanban")
        self.assertIn('  "columns": [', found["text"])
        root = self.by_alias("work")
        self.assertEqual(found["blocks"][root["id"]]["id"], "work")

    def test_relations_in_a_stack(self):
        self.ok({"op": "section", "id": "row", "title": "Row", "layout": "row", "at": [0, 0], "intent": "t"})
        self.ok({"op": "card", "id": "a", "title": "A", "in": "row", "intent": "t"})
        self.ok({"op": "card", "id": "b", "title": "B", "in": "row", "intent": "t"})
        text = C.look(self.layout, self.team, "alpha-worker")["text"]
        self.assertIn("in row#2/2 (row)", text)
        self.assertIn(": a b", text)


class RoundTrip(BlockRig):
    """T-B1: what an agent reads back is what it could write (the normalized readback equals the normalized op)."""

    OPS = [KANBAN,
           {"op": "table", "id": "risks", "title": "Risks", "at": [2000, 0], "intent": "t", "zebra": True,
            "columns": ["Risk", {"title": "Owner", "key": "owner"}, {"title": "Level", "key": "level", "align": "center", "width": "s"}],
            "rows": [["Docs", "writer", "High"], {"id": "price", "cells": ["Pricing", "pm", "Medium"], "tone": "warning"}]},
           {"op": "timeline", "id": "plan", "title": "Plan", "at": [0, 1400], "intent": "t",
            "events": [{"at": "2026-10-05", "title": "Beta"}, {"at": "2026-11-03", "title": "Launch", "milestone": True, "tone": "success"}]},
           {"op": "section", "id": "sec", "title": "Sec", "layout": "grid", "cols": 3, "gap": "m", "padding": "l", "at": [3000, 1400], "intent": "t"}]

    def test_every_block_round_trips(self):
        kctx = C._KindCtx(None)  # parsing and normalizing need no canvas
        for op in self.OPS:
            self.ok(op)
            with self.subTest(op=op["op"]):
                kind = R.get(op["op"])
                built = B.comparable(B.normalized(kctx, kind, op))
                read = B.comparable(B.normalized(kctx, kind, self.spec(op["id"])))
                self.assertEqual(read, built)

    def test_the_round_trip_holds_after_patches(self):
        self.ok(KANBAN)
        self.ok({"op": "patch", "id": "work", "remove": {"cards": ["c1"]}, "add": {"cards": ["Fresh"]}, "intent": "t"})
        kctx = C._KindCtx(None)
        spec = self.spec("work")
        again = B.comparable(B.normalized(kctx, R.get("kanban"), spec))
        self.ok(dict(spec, intent="write it back"))
        self.assertEqual(B.comparable(B.normalized(kctx, R.get("kanban"), self.spec("work"))), again, "writing the readback changes nothing")


class Display(BlockRig):
    def test_entries_carry_block_part_container_and_tip(self):
        self.ok(dict(KANBAN, columns=[{"id": "todo", "title": "Todo", "cards": [{"title": "A", "detail": "the whole story"}]}]))
        doc = D.display_list(self.scene())
        self.assertEqual(D.validate(doc), [])
        root = self.by_alias("work")
        by_id = {e["id"]: e for e in doc["entries"]}
        self.assertEqual(by_id[root["id"]]["kind"], "kanban")
        self.assertEqual(by_id[root["id"]]["container"]["layout"], "row")
        card = self.part(root["id"], "c1")
        entry = by_id[card["id"]]
        self.assertEqual((entry["block"], entry["part"], entry["tip"]), (root["id"], "c1", "the whole story"))
        self.assertEqual([p["part"] for p in entry["parts"]], ["title", "body"])


class DropIndex(BlockRig):
    def test_the_shared_vectors(self):
        path = C.Path(__file__).resolve().parent / "fixtures" / "display" / "stack-drop-vectors.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        for case in doc["cases"]:
            with self.subTest(case=case):
                self.assertEqual(B.drop_index(case["layout"], case["centre"], case["siblings"]), case["index"])


if __name__ == "__main__":
    import unittest

    unittest.main()
