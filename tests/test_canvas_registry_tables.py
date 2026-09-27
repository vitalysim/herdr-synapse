"""The tables the kind registry now answers (canvas v2 phase 1, 2.4) are exactly what the scattered lists said in
Phase 0, order included: a kind that moved into its module changed nothing an agent or the page can see."""
from __future__ import annotations

import unittest

from herdr_team import canvas as C
from herdr_team import canvas_check as K
from herdr_team import canvas_kinds as R
from herdr_team import canvas_theme as T

#: Phase 0's lists, as they were written in canvas.py, canvas_check.py and canvas_theme.py (0.22, fb435e72).
PHASE0_ELEMENT_TYPES = ("box", "ellipse", "diamond", "note", "text", "arrow", "frame", "pen", "path", "svg", "mermaid", "chart", "viz",
                        "image", "comment")
PHASE0_OPS = ("shape", "arrow", "frame", "pen", "path", "svg", "graph", "mermaid", "chart", "viz", "image", "comment", "claim", "release",
              "legend", "move", "restyle", "edit", "delete", "portrait", "resolve", "lock", "unlock", "undo")
_COMMON = ("op", "intent", "if_version")
_PLACE = ("at", "right_of", "left_of", "below", "above", "inside", "gap")
_STYLE = ("color", "fill", "width", "dash", "opacity", "font", "size", "rough", "tone", "variant")
PHASE0_FIELDS = {
    "shape": _COMMON + ("kind", "text", "w", "h", "id", "client_id") + _PLACE + _STYLE,
    "arrow": _COMMON + ("from", "to", "points", "label", "head", "tail", "curve", "id", "client_id") + _STYLE,
    "frame": _COMMON + ("title", "w", "h", "children", "region", "id", "client_id") + _PLACE + _STYLE,
    "pen": _COMMON + ("points", "closed", "style", "width", "color", "fill", "opacity", "dash", "id", "client_id"),
    "path": _COMMON + ("d", "scale", "id", "client_id") + _PLACE + _STYLE,
    "svg": _COMMON + ("svg", "w", "h", "sketchy", "title", "id", "client_id") + _PLACE,
    "graph": _COMMON + ("nodes", "edges", "layout", "direction", "title", "id") + _PLACE + _STYLE,
    "mermaid": _COMMON + ("source", "w", "h", "title", "id", "client_id") + _PLACE + _STYLE,
    "chart": _COMMON + ("spec", "data", "title", "w", "h", "id", "client_id") + _PLACE,
    "viz": _COMMON + ("html", "libs", "data", "data_path", "title", "w", "h", "id", "client_id") + _PLACE,
    "image": _COMMON + ("path", "asset", "w", "h", "id", "client_id") + _PLACE,
    "comment": _COMMON + ("at", "text", "mentions", "reply_to", "client_id"),
    "claim": _COMMON + ("region", "label"),
    "release": _COMMON + ("id",),
    "legend": _COMMON + ("symbol", "meaning", "remove"),
    "move": _COMMON + ("id", "ids", "to", "by", "w", "h", "points", "from", "to_element", "frame", "right_of", "left_of", "below", "above", "inside", "gap"),
    "restyle": _COMMON + ("id", "ids") + _STYLE,
    "edit": _COMMON + ("id", "text"),
    "delete": _COMMON + ("id", "ids", "with_children"),
    "portrait": _COMMON + ("steps", "current", "title"),
    "resolve": _COMMON + ("id",),
    "lock": _COMMON + ("region", "label"),
    "unlock": _COMMON + ("id",),
    "undo": _COMMON + ("batch",),
}
PHASE0_KIND_GROUPS = {"box": "shape", "ellipse": "shape", "diamond": "shape", "note": "note", "frame": "frame", "arrow": "arrow",
                      "text": "text", "pen": "ink", "path": "ink"}
PHASE0_SOLID = frozenset(("box", "ellipse", "diamond", "note", "text", "path", "svg", "mermaid", "chart", "viz", "image"))
PHASE0_LABELLED = frozenset(("box", "ellipse", "diamond", "note"))
PHASE0_CELL_TYPES = frozenset(("box", "ellipse", "diamond", "note", "text", "frame", "svg", "chart", "viz", "mermaid", "image"))
PHASE0_NOUNS = {
    "box": ("box", "boxes"), "ellipse": ("ellipse", "ellipses"), "diamond": ("diamond", "diamonds"), "note": ("note", "notes"),
    "text": ("text", "texts"), "arrow": ("arrow", "arrows"), "frame": ("frame", "frames"), "pen": ("pen stroke", "pen strokes"),
    "path": ("path", "paths"), "svg": ("svg block", "svg blocks"), "mermaid": ("mermaid diagram", "mermaid diagrams"),
    "chart": ("chart", "charts"), "viz": ("live visual", "live visuals"), "image": ("image", "images"),
    "comments": ("comment", "comments"), "claims": ("claim", "claims"), "legend": ("legend entry", "legend entries"),
    "locks": ("lock", "locks"),
}


class DerivedTables(unittest.TestCase):
    def test_ops_keep_their_order_and_refit_joins_the_core_ops(self):
        self.assertEqual(C.OPS, PHASE0_OPS + ("refit",))
        self.assertEqual(C.OPS[:12], tuple(spec.name for spec in R.ops()))
        self.assertEqual(C.CORE_OPS, PHASE0_OPS[12:] + ("refit",))

    def test_every_op_takes_the_fields_it_took(self):
        for name, fields in PHASE0_FIELDS.items():
            with self.subTest(op=name):
                self.assertEqual(C._FIELDS[name], fields)
        self.assertEqual(C._FIELDS["refit"], _COMMON + ("id", "ids"))
        self.assertEqual(set(C._FIELDS), set(C.OPS))
        self.assertEqual(set(C._HANDLERS), set(C.OPS))

    def test_the_kind_lists_are_the_registrys(self):
        self.assertEqual(C.ELEMENT_TYPES, PHASE0_ELEMENT_TYPES)
        self.assertEqual(C.SHAPE_KINDS, ("box", "ellipse", "diamond", "note", "text"))
        self.assertEqual(C.TEXT_TYPES, frozenset(C.SHAPE_KINDS))
        self.assertEqual(C.CELL_TYPES, PHASE0_CELL_TYPES)
        self.assertEqual(C.SHAPE_SIZES, {"box": (160, 80), "ellipse": (160, 80), "diamond": (200, 120), "note": (180, 120)})
        self.assertEqual(K.SOLID, PHASE0_SOLID)
        self.assertEqual(K.LABELLED_SHAPES, PHASE0_LABELLED)
        self.assertEqual(T.KIND_GROUPS, PHASE0_KIND_GROUPS)
        self.assertEqual(C._NOUNS, PHASE0_NOUNS)
        self.assertEqual(C._COUNT_ORDER[:14], tuple(t for t in PHASE0_ELEMENT_TYPES if t != "comment"))

    def test_every_kind_module_is_listed_once(self):
        self.assertEqual(R._MODULES, ("shape", "text", "arrow", "frame", "pen", "path", "svg", "diagram", "chart", "viz", "image", "comment"))
        self.assertEqual(R.API_VERSION, 2)
        self.assertEqual(R.names(page_only=True), list(PHASE0_ELEMENT_TYPES), "the 15 kinds the v1 page draws (D8)")
        self.assertEqual(sorted(R.slots()), ["chart", "mermaid", "viz"])

    def test_an_op_name_or_a_repeated_field_is_refused_at_registration(self):
        def create(ctx, op):
            return None

        with self.assertRaises(ValueError):
            R.register_op(R.OpSpec(name="claim", fields=(), create=create), reserved=C.CORE_OPS)
        with self.assertRaises(ValueError):
            R.register_op(R.OpSpec(name="test-dup", fields=("a", "a"), create=create))
        with self.assertRaises(ValueError):
            R.register_op(R.OpSpec(name="test-bad", fields=(), create=None))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            R.register_op(R.OpSpec(name="shape", fields=(), create=create))
        with self.assertRaises(ValueError):
            R.register(R.Kind(name="test-group", tone_group="rainbow"))


if __name__ == "__main__":
    unittest.main()
