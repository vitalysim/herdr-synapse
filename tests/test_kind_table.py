"""The ``table`` kind (canvas v2 phase 2, 4.4): inline columns and rows, widths from content, clamped cells, parts."""
from __future__ import annotations

from test_canvas import OPERATOR, CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_display as D

RISKS = {"op": "table", "id": "risks", "title": "Launch risks", "at": [0, 0], "intent": "t",
         "columns": ["Risk", {"title": "Owner", "key": "owner"}, {"title": "Level", "key": "level", "align": "center", "width": "s"}],
         "rows": [["Docs not ready", "writer", "High"], {"id": "price", "cells": ["Pricing unclear", "pm", "Medium"], "tone": "warning"}]}


class Table(CanvasRig):
    def table(self):
        return next(e for e in self.scene()["elements"] if e.get("alias") == "risks")

    def test_one_element_holds_every_cell(self):
        applied = self.ok(RISKS)
        self.assertEqual(len(applied["ids"]), 1, "inline parts: one element (D1)")
        el = self.table()
        self.assertEqual([c["id"] for c in el["columns"]], ["c1", "owner", "level"])
        self.assertEqual(el["rows"][0]["cells"], {"c1": "Docs not ready", "owner": "writer", "level": "High"})
        self.assertEqual(el["columns"][2]["width"], "s")

    def test_parts_and_the_display(self):
        self.ok(RISKS)
        entry = next(e for e in D.display_list(self.scene())["entries"] if e["kind"] == "table")
        parts = [p["part"] for p in entry["parts"]]
        self.assertEqual(parts[:3], ["h.c1", "h.owner", "h.level"])
        self.assertIn("price.owner", parts)
        self.assertEqual(entry["parts"][0]["lod"], D.LOD_LABEL)
        self.assertEqual(next(p for p in entry["parts"] if p["part"] == "r1.c1")["lod"], D.LOD_BODY)
        fills = [p.get("fill") for p in entry["items"] if p.get("k") == "rect"]
        self.assertIn("tone.neutral.zone", fills, "the header row")
        self.assertIn("tone.warning.fill", fills, "the toned row")

    def test_editing_a_cell_is_a_patch(self):
        self.ok(RISKS)
        version = self.table()["updated_seq"]
        self.ok({"op": "edit", "id": "risks", "part": "r1.owner", "text": "docs team", "if_version": version, "intent": "t"}, author=OPERATOR)
        self.assertEqual(self.table()["rows"][0]["cells"]["owner"], "docs team")
        self.ok({"op": "edit", "id": "risks", "part": "h.owner", "text": "Who", "intent": "t"})
        self.assertEqual(self.table()["columns"][1]["title"], "Who")

    def test_patch_rows_and_settings(self):
        self.ok(RISKS)
        self.ok({"op": "patch", "id": "risks", "add": {"rows": [["Legal", "legal", "Low"]]}, "remove": {"rows": ["r1"]},
                 "set": {"zebra": True}, "intent": "t"})
        el = self.table()
        self.assertEqual([r["id"] for r in el["rows"]], ["price", "r2"], "a new row never reuses an id (r1 was used)")
        self.assertTrue(el["settings"]["zebra"])

    def test_a_clamped_cell_is_reported_with_a_fix(self):
        self.ok(dict(RISKS, max_lines=1, rows=[["A risk description far too long for one line of the column", "x", "High"]]))
        el = self.table()
        self.assertTrue(el["fit"]["truncated"])
        problem = next(p for p in C.check(self.layout, self.team, "alpha-worker")["problems"] if p["code"] == "label_truncated")
        self.assertEqual(problem["fix"]["op"], "patch")
        self.assertEqual(problem["fix"]["set"], {"max_lines": 2})
        self.ok(problem["fix"])
        self.assertEqual(self.table()["settings"]["max_lines"], 2)

    def test_every_remedy_check_names_for_a_clamped_cell_is_reachable(self):
        """QA phase 6 F1: check used to advise "raise max_lines or widen the column" for a cell no `max_lines` (capped
        at 4) and no column width could hold, so a live agent applied its fix twice, was refused a third time and left
        the board reporting a problem for ever. Each rung is now measured before it is named."""
        long_cell = "word " * 32  # 160 characters: four lines of the default column still clamp it
        self.ok(dict(RISKS, max_lines=4, rows=[[long_cell.strip(), "x", "High"]]))
        problem = next(p for p in C.check(self.layout, self.team, "alpha-worker")["problems"] if p["code"] == "label_truncated")
        self.assertIn("widen Risk to ", problem["message"])
        self.assertEqual(problem["fix"]["op"], "patch")
        [column] = problem["fix"]["update"]["columns"]
        self.assertEqual(column["id"], "c1")
        self.assertLessEqual(column["width"], 600, "a column takes width 80 to 600")
        self.ok(problem["fix"])
        self.assertEqual([p["code"] for p in C.check(self.layout, self.team, "alpha-worker")["problems"]
                          if p["code"] == "label_truncated"], [], "one fix, and the cell is whole")

    def test_a_cell_no_width_can_hold_is_told_to_be_shortened_and_offers_no_fix(self):
        """The rung of last resort: check says how many characters fit and does not pretend an op can do it, because
        only the cell's author can choose which words to drop."""
        wide = "需要注意的事项" * 28  # 196 characters, each as wide as it is tall: 4 lines of 600 still clamp it
        self.ok(dict(RISKS, max_lines=2, rows=[[wide, "x", "High"]]))
        problem = next(p for p in C.check(self.layout, self.team, "alpha-worker")["problems"] if p["code"] == "label_truncated")
        self.assertIsNone(problem["fix"])
        self.assertIn("no column width and no max_lines holds it", problem["message"])
        self.assertRegex(problem["message"], r"shorten r1\.c1 to about \d+ characters")

    def test_limits(self):
        refused = self.refused(dict(RISKS, columns=["c"] * 17, rows=[]))
        self.assertEqual(refused["code"], "canvas_limit")
        refused = self.refused(dict(RISKS, rows=[{"cells": {"nope": "x"}}]))
        self.assertEqual(refused["details"]["field"], "rows[0].cells.nope")
