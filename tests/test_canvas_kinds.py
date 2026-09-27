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
        self.assertEqual(set(C.ELEMENT_TYPES), set(R.names()))
        self.assertEqual(R.names(), list(C.ELEMENT_TYPES), "registration order follows the element types")
        self.assertEqual([k.name for k in R.by_op("shape")], list(C.SHAPE_KINDS))

    def test_the_labelled_kinds_fit_and_read_back(self):
        for name, policy in (("box", "hug"), ("note", "shrink"), ("ellipse", "scale_shape"), ("diamond", "scale_shape"), ("text", "hug")):
            kind = R.get(name)
            self.assertEqual((kind.fit, kind.measure is not None, kind.readback is not None), (policy, True, True), name)
        self.assertEqual({k.name: k.role for k in R.kinds() if k.role != "leaf"},
                         {"arrow": "connector", "frame": "container", "comment": "overlay"})

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


class Conformance(unittest.TestCase):
    def test_every_registered_kind_conforms(self):
        for kind in R.kinds():
            with self.subTest(kind=kind.name):
                kind_conformance.check_kind(self, kind)


if __name__ == "__main__":
    unittest.main()
