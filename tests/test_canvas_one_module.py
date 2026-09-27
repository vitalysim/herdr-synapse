"""One module is a whole new kind (canvas v2 phase 1, 2.1): the acceptance test.

``tests/fixtures/kind_badge.py`` defines a ``badge`` kind with its op, measure, readback, check, drawing and hit test.
Loaded through the registry's test hook, and nothing else, it is an element type, an op with its fields, text the
canvas fits, a solid mark, part of the MCP op table and the reference, read back by ``look``, checked by ``check``
and drawn in the display list. Unloaded, every table is back as it was.
"""
from __future__ import annotations

import sys

from support import PLUGIN_ROOT
from test_canvas import CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_check as K
from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R
from herdr_team import canvas_mcp as M
from herdr_team import canvas_svg as S
from herdr_team import reference_docs

MODULE = "tests.fixtures.kind_badge"


class OneModuleKind(CanvasRig):
    def setUp(self):
        super().setUp()
        if str(PLUGIN_ROOT) not in sys.path:
            sys.path.insert(0, str(PLUGIN_ROOT))
        self.before = (C.ELEMENT_TYPES, C.OPS, dict(C._FIELDS), K.solid_kinds(), M.op_table())
        R._load_extra(MODULE)
        self.addCleanup(R._unload, MODULE)

    def test_the_registry_derives_every_table(self):
        self.assertIn("badge", C.ELEMENT_TYPES)
        self.assertEqual(C.OPS[C.OPS.index("comment") + 1], "badge", "kind ops by order (130), then the core ops")
        self.assertEqual(C._FIELDS["badge"], C._COMMON + ("text", "tone", "id", "client_id") + C.PLACE_FIELDS)
        self.assertIn("badge", C.TEXT_TYPES)
        self.assertIn("badge", C.CELL_TYPES)
        self.assertIn("badge", K.SOLID)
        self.assertIn("badge", K.LABELLED_SHAPES)
        self.assertIn("badge {text, tone}", M.op_table())
        self.assertIn("| `badge` | `text`, `tone`, `id`, `client_id`; *place* | a small pill holding a status word |", reference_docs.render_canvas_ops_section())
        self.assertEqual(C._NOUNS["badge"], ("badge", "badges"))

    def test_the_op_applies_and_refuses_unknown_fields(self):
        applied = self.ok({"op": "badge", "text": "shipped", "tone": "success", "at": [0, 0], "id": "ship", "intent": "status"})
        el = self.el(applied["ids"][0])
        self.assertEqual((el["type"], el["text"], el["style"]["tone"], el["alias"]), ("badge", "shipped", "success", "ship"))
        self.assertGreaterEqual((el["w"], el["h"]), (48, 28))
        self.assertEqual(el["fit"]["lines"], ["shipped"], "sized from its label by its own measure")
        self.assertEqual(applied["geometry"][0]["id"], el["id"])
        refused = self.refused({"op": "badge", "text": "x", "color": "red", "intent": "t"})
        self.assertEqual((refused["code"], refused["details"]["field"]), ("op_invalid", "color"))
        self.assertEqual(self.refused({"op": "badge", "intent": "t"})["details"]["field"], "text")
        self.ok({"op": "edit", "id": el["id"], "text": "a much longer status word", "intent": "t"})
        self.assertGreater(self.el(el["id"])["w"], el["w"], "edit refits it like any labelled kind")

    def test_look_check_and_the_display_list_use_the_kind(self):
        eid = self.ok({"op": "badge", "text": "TODO", "at": [0, 0], "intent": "t"})["ids"][0]
        looked = C.look(self.layout, self.team, "alpha-worker")
        self.assertIn('{} badge "TODO"'.format(eid), looked["text"])
        found = C.check(self.layout, self.team, "alpha-worker")
        self.assertIn("badge_todo", [p["code"] for p in found["problems"]])
        doc = D.display_list(self.scene())
        self.assertEqual(D.validate(doc), [])
        entry = next(e for e in doc["entries"] if e["id"] == eid)
        self.assertEqual([p["k"] for p in entry["items"]], ["rect", "text"])
        self.assertEqual((entry["kind"], entry["connect"], entry["edit"]["value"]), ("badge", True, "TODO"))
        self.assertIn('data-id="{}"'.format(eid), S.write(doc))

    def test_unloading_restores_every_table(self):
        R._unload(MODULE)
        self.assertEqual((C.ELEMENT_TYPES, C.OPS, dict(C._FIELDS), K.solid_kinds(), M.op_table()), self.before)
        self.assertEqual(self.refused({"op": "badge", "text": "x", "intent": "t"})["details"]["field"], "op")
