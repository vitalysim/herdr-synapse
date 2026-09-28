"""The canvas component registry (canvas v2 foundation): registration rules and lookups.

Kind modules add their own tests; this file covers the registry mechanics only."""
from __future__ import annotations

import unittest

import kind_conformance
from herdr_team import canvas as C
from herdr_team import canvas_kinds as R


class Registry(unittest.TestCase):
    def setUp(self):
        R.load()  # the real kinds first, so tearDown restores them and not an empty registry
        self.saved = dict(R._REGISTRY)

    def tearDown(self):
        R._REGISTRY.clear()
        R._REGISTRY.update(self.saved)

    def test_a_registered_kind_is_found_by_name_and_op(self):
        kind = R.register(R.Kind(name="test-card", ops=("test-card",), fit="hug"))
        self.assertIs(R.get("test-card"), kind)
        self.assertIn(kind, R.by_op("test-card"))
        self.assertIn("test-card", R.names())

    def test_an_unknown_name_is_none_so_callers_fall_back(self):
        self.assertIsNone(R.get("no-such-kind"))
        self.assertIsNone(R.get(None))

    def test_a_second_kind_with_the_same_name_is_refused(self):
        R.register(R.Kind(name="test-twice"))
        with self.assertRaises(ValueError):
            R.register(R.Kind(name="test-twice", role="container"))

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(ValueError):
            R.register(R.Kind(name="test-role", role="widget"))

    def test_page_only_leaves_out_kinds_the_page_does_not_render(self):
        R.register(R.Kind(name="test-headless", page=False))
        self.assertNotIn("test-headless", R.names(page_only=True))
        self.assertIn("test-headless", R.names())

    def test_every_listed_module_imports_and_registers_valid_kinds(self):
        for kind in R.kinds():
            self.assertIn(kind.role, R.ROLES, kind.name)
            self.assertGreaterEqual(kind.version, 1, kind.name)


class PhaseZeroKinds(unittest.TestCase):
    def test_every_element_type_is_a_registered_kind(self):
        # A kind stored as another kind's type (a section is a frame, phase 2 D2) is a kind but not an element type.
        self.assertEqual(set(C.ELEMENT_TYPES), {k.name for k in R.kinds() if k.stored_as is None})
        self.assertEqual(R.element_types(), list(C.ELEMENT_TYPES), "registration order follows the element types")
        self.assertEqual([k.name for k in R.by_op("shape")], list(C.SHAPE_KINDS))

    def test_the_labelled_kinds_fit_and_read_back(self):
        for name, policy in (("box", "hug"), ("note", "shrink"), ("ellipse", "scale_shape"), ("diamond", "scale_shape"), ("text", "hug")):
            kind = R.get(name)
            self.assertEqual((kind.fit, kind.measure is not None, kind.readback is not None), (policy, True, True), name)
        phase0 = {"arrow": "connector", "frame": "container", "comment": "overlay"}
        found = {k.name: k.role for k in R.kinds() if k.role != "leaf"}
        self.assertEqual({name: role for name, role in found.items() if name in phase0}, phase0)
        for name, role in found.items():
            if name not in phase0:
                self.assertIsNotNone(R.get(name).stored_as or R.get(name).block or R.get(name).arrange or R.get(name).role == "connector",
                                     "{} is a {}: a block or container of phase 2".format(name, role))

    def test_insets_match_the_check_and_the_page(self):
        for name, room in (("ellipse", 2 ** -0.5), ("diamond", 0.5)):
            x, y, w, h = R.get(name).inset(200, 100)
            self.assertAlmostEqual(w, 200 * room)
            self.assertAlmostEqual(h, 100 * room)
            self.assertAlmostEqual(x, (200 - w) / 2)

    def test_readback_notes_what_the_fit_did(self):
        note = {"id": "E-9", "type": "note", "text": "long", "x": 0, "y": 0, "w": 180, "h": 120, "style": {"size": 20},
                "fit": {"policy": "shrink", "size": 16, "lines": ["long"], "truncated": False, "estimated": True}}
        self.assertEqual(R.get("note").readback(note, True), 'E-9 note "long" [0,0 180x120] (shrunk to 16px, estimated)')

    def test_drawn_is_the_label_at_the_elements_current_size(self):
        box = {"id": "E-1", "type": "box", "text": "Checkout API", "x": 0, "y": 0, "w": 160, "h": 80, "style": {"size": 20}}
        found = R.drawn(box)
        self.assertEqual((found.w, found.h, found.policy), (160, 80, "keep"))
        small = R.drawn(dict(box, w=40, h=40))
        self.assertGreater(small.w, 40, "too small: what it would grow to")
        self.assertIsNone(R.drawn({"type": "arrow"}))


class StoredAs(unittest.TestCase):
    """Kinds stored as another kind's element type (phase 2, 1.2): a section is ``{"type": "frame", "block": "section"}``."""

    def setUp(self):
        R.load()
        self.saved = dict(R._REGISTRY)

    def tearDown(self):
        R._REGISTRY.clear()
        R._REGISTRY.update(self.saved)

    def test_kind_of_reads_the_block_of_a_stored_as_kind(self):
        self.assertIs(R.kind_of({"type": "frame", "block": "section"}), R.get("section"))
        self.assertIs(R.kind_of({"type": "frame"}), R.get("frame"))
        self.assertIs(R.kind_of({"type": "frame", "block": "no-such-block"}), R.get("frame"), "an unknown block is a plain frame")
        self.assertIs(R.kind_of({"type": "card", "block": "section"}), R.get("card"), "block only counts on the stored type")
        self.assertIsNone(R.kind_of({"type": "no-such-kind"}))
        self.assertNotIn("section", R.element_types())
        self.assertIn("section", [k.name for k in R.blocks()])

    def test_a_stored_as_kind_needs_a_kind_stored_as_itself(self):
        with self.assertRaises(ValueError):
            R.register(R.Kind(name="test-nowhere", stored_as="no-such-kind"))
        with self.assertRaises(ValueError):
            R.register(R.Kind(name="test-chained", stored_as="section"))
        R.register(R.Kind(name="test-stored", stored_as="frame"))
        self.assertNotIn("test-stored", R.element_types())

    def test_a_tool_template_must_be_an_op(self):
        with self.assertRaises(ValueError):
            R.register(R.Kind(name="test-template", tool=R.Tool(key="q", title="Q", glyph="Q", gesture="block", template='["op"]')))
        with self.assertRaises(ValueError):
            R.register(R.Kind(name="test-template2", tool=R.Tool(key="q", title="Q", glyph="Q", gesture="block", template="{not json")))
        kind = R.register(R.Kind(name="test-template3", tool=R.Tool(key="q", title="Q", glyph="Q", gesture="block", template='{"op": "card"}')))
        self.assertEqual(kind.tool.template_op(), {"op": "card"})


class Conformance(unittest.TestCase):
    def test_every_registered_kind_conforms(self):
        for kind in R.kinds():
            with self.subTest(kind=kind.name):
                kind_conformance.check_kind(self, kind)


if __name__ == "__main__":
    unittest.main()
