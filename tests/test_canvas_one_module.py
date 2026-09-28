"""One module is a whole new kind (canvas v2 phase 1, 2.1): the acceptance test.

``tests/fixtures/kind_stamp.py`` defines a ``stamp`` kind with its op, measure, readback, check, drawing and hit test.
Loaded through the registry's test hook, and nothing else, it is an element type, an op with its fields, text the
canvas fits, a solid mark, part of the MCP op table and the reference, read back by ``look``, checked by ``check``
and drawn in the display list. Unloaded, every table is back as it was.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from registry_conformance import assert_registered_in_place
from support import PLUGIN_ROOT
from test_canvas import CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_check as K
from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R
from herdr_team import canvas_mcp as M
from herdr_team import canvas_svg as S
from herdr_team import canvas_theme as T
from herdr_team import reference_docs

MODULE = "tests.fixtures.kind_stamp"


class OneModuleKind(CanvasRig):
    def setUp(self):
        super().setUp()
        if str(PLUGIN_ROOT) not in sys.path:
            sys.path.insert(0, str(PLUGIN_ROOT))
        self.before = (C.ELEMENT_TYPES, C.OPS, dict(C._FIELDS), K.solid_kinds(), M.op_table())
        R._load_extra(MODULE)
        self.addCleanup(R._unload, MODULE)

    def test_the_registry_derives_every_table(self):
        self.assertIn("stamp", C.ELEMENT_TYPES)
        # Kind ops by their order (the stamp's 900), then the core ops; another module's ops may sit among them (R6).
        kind_ops = [spec.name for spec in R.ops()]
        self.assertEqual(list(C.OPS[:len(kind_ops)]), kind_ops)
        self.assertIn("stamp", kind_ops)
        orders = [spec.order for spec in R.ops()]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(C._FIELDS["stamp"], C._COMMON + ("text", "tone", "id", "client_id") + C.PLACE_FIELDS)
        self.assertIn("stamp", C.TEXT_TYPES)
        self.assertIn("stamp", C.CELL_TYPES)
        self.assertIn("stamp", K.SOLID)
        self.assertIn("stamp", K.LABELLED_SHAPES)
        self.assertIn("stamp {text, tone}", M.op_table())
        self.assertIn("| `stamp` | `text`, `tone`, `id`, `client_id`; *place* | a small pill holding a status word |", reference_docs.render_canvas_ops_section())
        self.assertEqual(C._NOUNS["stamp"], ("stamp", "stamps"))

    def test_the_op_applies_and_refuses_unknown_fields(self):
        applied = self.ok({"op": "stamp", "text": "shipped", "tone": "success", "at": [0, 0], "id": "ship", "intent": "status"})
        el = self.el(applied["ids"][0])
        self.assertEqual((el["type"], el["text"], el["style"]["tone"], el["alias"]), ("stamp", "shipped", "success", "ship"))
        self.assertGreaterEqual((el["w"], el["h"]), (48, 28))
        self.assertEqual(el["fit"]["lines"], ["shipped"], "sized from its label by its own measure")
        self.assertEqual(applied["geometry"][0]["id"], el["id"])
        refused = self.refused({"op": "stamp", "text": "x", "color": "red", "intent": "t"})
        self.assertEqual((refused["code"], refused["details"]["field"]), ("op_invalid", "color"))
        self.assertEqual(self.refused({"op": "stamp", "intent": "t"})["details"]["field"], "text")
        self.ok({"op": "edit", "id": el["id"], "text": "a much longer status word", "intent": "t"})
        self.assertGreater(self.el(el["id"])["w"], el["w"], "edit refits it like any labelled kind")

    def test_look_check_and_the_display_list_use_the_kind(self):
        eid = self.ok({"op": "stamp", "text": "TODO", "at": [0, 0], "intent": "t"})["ids"][0]
        looked = C.look(self.layout, self.team, "alpha-worker")
        self.assertIn('{} stamp "TODO"'.format(eid), looked["text"])
        found = C.check(self.layout, self.team, "alpha-worker")
        self.assertIn("stamp_todo", [p["code"] for p in found["problems"]])
        doc = D.display_list(self.scene())
        self.assertEqual(D.validate(doc), [])
        entry = next(e for e in doc["entries"] if e["id"] == eid)
        self.assertEqual([p["k"] for p in entry["items"]], ["rect", "text"])
        self.assertEqual((entry["kind"], entry["connect"], entry["edit"]["value"]), ("stamp", True, "TODO"))
        self.assertIn('data-id="{}"'.format(eid), S.write(doc))

    def test_unloading_restores_every_table(self):
        R._unload(MODULE)
        self.assertEqual((C.ELEMENT_TYPES, C.OPS, dict(C._FIELDS), K.solid_kinds(), M.op_table()), self.before)
        self.assertEqual(self.refused({"op": "stamp", "text": "x", "intent": "t"})["details"]["field"], "op")


class DroppedInModule(unittest.TestCase):
    """The real way to add a kind (QA phase 1, V-2): one file in the ``canvas_kinds`` package, and no list anywhere.

    The stamp module is copied, as ``stamp.py``, into a directory the package searches (its ``__path__``, as if the
    file sat next to ``shape.py``); the registry finds it by itself on its next load."""

    NAME = "herdr_team.canvas_kinds.stamp"

    def setUp(self):
        self.before = (C.ELEMENT_TYPES, C.OPS, dict(C._FIELDS), R.modules(), T.asset()["tools"])
        folder = Path(tempfile.mkdtemp(prefix="kind-drop-"))
        self.addCleanup(shutil.rmtree, folder, True)
        shutil.copy(PLUGIN_ROOT / "tests" / "fixtures" / "kind_stamp.py", folder / "stamp.py")
        self.folder = str(folder)
        R.__path__.append(self.folder)
        self.addCleanup(self._restore)
        R._reload()

    def _restore(self):
        if self.folder in R.__path__:
            R.__path__.remove(self.folder)
        sys.modules.pop(self.NAME, None)
        R._reload()

    def test_a_file_in_the_package_is_a_kind_with_no_list_edited(self):
        # In the place its ORDER (none: 1000, after the built-in modules) gives it, not necessarily the last (R6).
        assert_registered_in_place(self, R, R.modules(), "stamp")
        self.assertEqual(R.get("stamp").name, "stamp")
        self.assertIn("stamp", C.ELEMENT_TYPES)
        self.assertIn("stamp", C.OPS)
        self.assertIn("stamp", C.TEXT_TYPES)
        self.assertIn("stamp", K.SOLID)
        self.assertIn("stamp {text, tone}", M.op_table())
        self.assertEqual(T.asset()["tools"], self.before[4], "no tool: the page's tool bar is unchanged")

    def test_taking_the_file_out_restores_every_table(self):
        self._restore()
        self.assertEqual((C.ELEMENT_TYPES, C.OPS, dict(C._FIELDS), R.modules(), T.asset()["tools"]), self.before)
