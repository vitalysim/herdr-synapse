"""One module is a whole new block (canvas v2 phase 2, 8.2, G10): the acceptance test for composites.

``tests/fixtures/kind_checklist.py`` defines a ``checklist`` block (``checklist {title, items: [{id, text, done}]}``,
members are free texts in a stack column) with its op, collection, build, readback, arrangement, drawing and check.
Loaded through the registry's test hook and nothing else, it is created, patched, read back by ``look``, drawn in the
display list, checked, and listed in the MCP op table; unloaded, every table is back as it was.
"""
from __future__ import annotations

import sys

from support import PLUGIN_ROOT
from test_canvas import CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_blocks as B
from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R
from herdr_team import canvas_mcp as M

MODULE = "tests.fixtures.kind_checklist"


class OneModuleBlock(CanvasRig):
    def setUp(self):
        super().setUp()
        if str(PLUGIN_ROOT) not in sys.path:
            sys.path.insert(0, str(PLUGIN_ROOT))
        self.before = (C.ELEMENT_TYPES, C.OPS, dict(C._FIELDS), M.op_table())
        R._load_extra(MODULE)
        self.addCleanup(R._unload, MODULE)

    def root(self):
        return next(e for e in self.scene()["elements"] if e.get("alias") == "todo")

    def test_create_patch_look_display_and_check(self):
        applied = self.ok({"op": "checklist", "id": "todo", "title": "Launch", "items": ["Write docs", {"text": "Ship", "done": True}],
                           "at": [0, 0], "intent": "t"})
        self.assertEqual(applied["block"]["kind"], "checklist")
        root = self.root()
        self.assertEqual((root["type"], root["block"]), ("frame", "checklist"), "stored as a frame (D2)")
        self.assertNotIn("checklist", C.ELEMENT_TYPES)
        items = [e for e in self.scene()["elements"] if e.get("group") == root["id"]]
        self.assertEqual(sorted(e["part"] for e in items), ["i1", "i2"])
        self.assertLess(items[0]["y"], items[1]["y"], "a stack column")
        self.ok({"op": "patch", "id": "todo", "add": {"items": ["Celebrate"]}, "update": {"items": [{"id": "i1", "done": True}]}, "intent": "t"})
        spec = B.spec_of(self.scene()["elements"], self.root())
        self.assertEqual(spec["items"], [{"id": "i1", "text": "Write docs", "done": True}, {"id": "i2", "text": "Ship", "done": True},
                                         {"id": "i3", "text": "Celebrate", "done": False}])
        text = C.look(self.layout, self.team, "alpha-worker")["text"]
        self.assertIn('checklist todo "Launch"', text)
        self.assertIn("checklist_open", C.check(self.layout, self.team, "alpha-worker")["text"])
        doc = D.display_list(self.scene())
        self.assertEqual(D.validate(doc), [])
        entry = next(e for e in doc["entries"] if e["id"] == root["id"])
        self.assertEqual((entry["kind"], entry["container"]["layout"]), ("checklist", "column"))
        self.assertIn("checklist {title, items [text | {id, text, done}]}", M.op_table())

    def test_unloading_restores_every_table(self):
        R._unload(MODULE)
        self.assertEqual((C.ELEMENT_TYPES, C.OPS, dict(C._FIELDS), M.op_table()), self.before)
        self.assertEqual(self.refused({"op": "checklist", "items": ["x"], "intent": "t"})["details"]["field"], "op")
