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

    def test_limits(self):
        refused = self.refused(dict(RISKS, columns=["c"] * 17, rows=[]))
        self.assertEqual(refused["code"], "canvas_limit")
        refused = self.refused(dict(RISKS, rows=[{"cells": {"nope": "x"}}]))
        self.assertEqual(refused["details"]["field"], "rows[0].cells.nope")
