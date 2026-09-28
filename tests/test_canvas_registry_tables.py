"""The tables the kind registry answers (canvas v2 phase 1, 2.4): conformance tests that hold for any registered kind.

Every derived table is checked against the registry itself, so a new kind module passes with no edit here (QA phase 1,
V-2). Phase 0's lists, as they were written before the registry, stay as a guard: restricted to the Phase 0 kinds and
ops, each derived table still says exactly what they said, in their order, so a kind that moved into its module changed
nothing an agent or the page can see."""
from __future__ import annotations

import importlib
import unittest
from pathlib import Path

from herdr_team import canvas as C
from herdr_team import canvas_check as K
from herdr_team import canvas_kinds as R
from herdr_team import canvas_theme as T

#: Phase 0's kind modules, in their registration order (they were the hand-kept ``_MODULES`` list).
PHASE0_MODULES = ("shape", "text", "arrow", "frame", "pen", "path", "svg", "diagram", "chart", "viz", "image", "comment")

#: Phase 0's lists, as they were written in canvas.py, canvas_check.py and canvas_theme.py (0.22, fb435e72).
PHASE0_ELEMENT_TYPES = ("box", "ellipse", "diamond", "note", "text", "arrow", "frame", "pen", "path", "svg", "mermaid", "chart", "viz",
                        "image", "comment")
PHASE0_OPS = ("shape", "arrow", "frame", "pen", "path", "svg", "graph", "mermaid", "chart", "viz", "image", "comment", "claim", "release",
              "legend", "move", "restyle", "edit", "delete", "portrait", "resolve", "lock", "unlock", "undo")
_COMMON = ("op", "intent", "if_version")
#: Phase 2 added ``in`` (join a container's layout) and ``index`` to the placement fields, and ``route`` to the style
#: fields (additive: an op without them means what it meant).
_PLACE = ("at", "right_of", "left_of", "below", "above", "inside", "in", "gap", "index")
_STYLE = ("color", "fill", "width", "dash", "opacity", "font", "size", "rough", "tone", "variant", "route")
PHASE0_FIELDS = {
    "shape": _COMMON + ("kind", "text", "w", "h", "icon", "id", "client_id") + _PLACE + _STYLE,
    "arrow": _COMMON + ("from", "to", "points", "label", "head", "tail", "curve", "id", "client_id") + _STYLE,
    "frame": _COMMON + ("title", "w", "h", "children", "region", "id", "client_id") + _PLACE + _STYLE,
    "pen": _COMMON + ("points", "closed", "style", "width", "color", "fill", "opacity", "dash", "id", "client_id", "in"),
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
    "edit": _COMMON + ("id", "text", "part"),
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


def subsequence(short, long) -> bool:
    """Whether ``short`` appears in ``long`` in the same order (other items may sit between)."""
    it = iter(long)
    return all(any(item == other for other in it) for item in short)


def restricted(table, keys):
    """``table`` (a set or a dict) with only the given keys."""
    if isinstance(table, dict):
        return {k: v for k, v in table.items() if k in keys}
    return frozenset(k for k in table if k in keys)


class DerivedTables(unittest.TestCase):
    def test_ops_are_the_registrys_by_order_then_the_core_ops(self):
        self.assertEqual(C.OPS, tuple(spec.name for spec in R.ops()) + C.CORE_OPS)
        self.assertEqual(len(set(C.OPS)), len(C.OPS))
        orders = [spec.order for spec in R.ops()]
        self.assertEqual(orders, sorted(orders))
        # Phase 0's ops keep their order, and refit joined the core ops.
        self.assertTrue(subsequence(PHASE0_OPS, C.OPS), C.OPS)
        # Phase 1 appended refit, phase 2 patch, place, pin and unpin (5.1).
        self.assertEqual(C.CORE_OPS, PHASE0_OPS[12:] + ("refit", "patch", "place", "pin", "unpin") +
                         ("accept", "reject", "withdraw", "freeze", "thaw", "settings", "checkpoint", "restore"))  # phase 5

    def test_every_op_takes_its_specs_fields(self):
        for spec in R.ops():
            with self.subTest(op=spec.name):
                expected = tuple(dict.fromkeys(_COMMON + spec.fields + (C.PLACE_FIELDS if spec.place else ()) + (C.STYLE_FIELDS if spec.style else ())))
                self.assertEqual(C._FIELDS[spec.name], expected)
                self.assertEqual(len(set(expected)), len(expected))
                self.assertTrue(spec.doc and spec.mcp, "{} has its doc line and MCP fragment".format(spec.name))
        self.assertEqual(set(C._FIELDS), set(C.OPS))
        self.assertEqual(set(C._HANDLERS), set(C.OPS))
        # Phase 0's ops take exactly the fields they took.
        for name, fields in PHASE0_FIELDS.items():
            with self.subTest(phase0=name):
                # Every field it took; phase 2 may add fields to the graph (groups, same_rank, order, route), phase 3 the
                # chart types' fields to the chart (its spec, data, title, w and h stay).
                self.assertLessEqual(set(fields), set(C._FIELDS[name]), name)
                if name == "undo":
                    # Phase 5 (6.1): undo also takes one author's batches since a version, and the operator's force.
                    self.assertEqual(C._FIELDS[name], fields + ("author", "since", "force"))
                elif name not in ("graph", "chart"):
                    self.assertEqual(C._FIELDS[name], fields)
        self.assertEqual(C._FIELDS["refit"], _COMMON + ("id", "ids"))
        self.assertEqual(C._FIELDS["patch"], _COMMON + ("id", "add", "update", "remove", "set", "relayout"))
        self.assertEqual(C._FIELDS["place"], _COMMON + ("id", "ids", "right_of", "left_of", "below", "above", "in", "at", "gap", "align", "index"))
        self.assertEqual((C._FIELDS["pin"], C._FIELDS["unpin"]), (_COMMON + ("id", "ids"), _COMMON + ("id", "ids", "relayout")))

    def test_the_kind_lists_are_the_registrys(self):
        kinds = R.kinds()
        self.assertEqual(C.ELEMENT_TYPES, tuple(R.element_types()))
        self.assertEqual(C.SHAPE_KINDS, tuple(k.name for k in R.subkinds("shape")))
        self.assertEqual(C.TEXT_TYPES, frozenset(k.name for k in kinds if k.measure is not None and k.stored_as is None))
        self.assertEqual(C.CELL_TYPES, frozenset(k.name for k in kinds if k.cell))
        self.assertEqual(K.SOLID, frozenset(k.name for k in kinds if k.solid))
        self.assertEqual(K.LABELLED_SHAPES, frozenset(k.name for k in kinds if k.labelled))
        self.assertEqual(T.KIND_GROUPS, {k.name: k.tone_group for k in kinds if k.tone_group != "other"})
        for kind in kinds:
            if kind.role != "overlay":
                self.assertEqual(C._NOUNS[kind.name], kind.nouns())
        self.assertEqual(C._COUNT_ORDER[:len([k for k in kinds if k.role != "overlay"])], tuple(k.name for k in kinds if k.role != "overlay"))
        for name in C.SHAPE_SIZES:
            self.assertIn(name, C.SHAPE_KINDS)

    def test_the_phase0_kinds_read_as_they_did(self):
        phase0 = set(PHASE0_ELEMENT_TYPES)
        self.assertTrue(subsequence(PHASE0_ELEMENT_TYPES, C.ELEMENT_TYPES), C.ELEMENT_TYPES)
        self.assertTrue(subsequence(("box", "ellipse", "diamond", "note", "text"), C.SHAPE_KINDS), C.SHAPE_KINDS)
        self.assertEqual(restricted(C.CELL_TYPES, phase0), PHASE0_CELL_TYPES)
        self.assertEqual(restricted(K.SOLID, phase0), PHASE0_SOLID)
        self.assertEqual(restricted(K.LABELLED_SHAPES, phase0), PHASE0_LABELLED)
        self.assertEqual(restricted(C.TEXT_TYPES, phase0), frozenset(("box", "ellipse", "diamond", "note", "text")))
        self.assertEqual(restricted(T.KIND_GROUPS, phase0), PHASE0_KIND_GROUPS)
        self.assertEqual(restricted(C._NOUNS, set(PHASE0_NOUNS)), PHASE0_NOUNS)
        self.assertEqual(restricted(C.SHAPE_SIZES, phase0), {"box": (160, 80), "ellipse": (160, 80), "diamond": (200, 120), "note": (180, 120)})
        self.assertTrue(subsequence(tuple(t for t in PHASE0_ELEMENT_TYPES if t != "comment"), C._COUNT_ORDER))

    def test_every_kind_module_in_the_package_is_found_with_no_list(self):
        package = Path(R.__file__).resolve().parent
        public = sorted(p.stem for p in package.glob("*.py") if not p.stem.startswith("_"))
        found = R.modules()
        self.assertEqual(len(set(found)), len(found))
        for stem in public:
            module = importlib.import_module("herdr_team.canvas_kinds." + stem)
            exports = hasattr(module, "KINDS") or hasattr(module, "OPS")
            self.assertEqual(stem in found, exports, "{}: a module is a kind module exactly when it exports KINDS or OPS".format(stem))
        orders = [getattr(importlib.import_module("herdr_team.canvas_kinds." + name), "ORDER", R.DEFAULT_ORDER) for name in found]
        self.assertEqual(orders, sorted(orders))
        self.assertTrue(subsequence(PHASE0_MODULES, found), found)
        self.assertFalse(hasattr(R, "_MODULES"), "no hand-kept module list")
        # Every registered kind and op came from a module the package holds (or a test's extra module).
        owners = {owner.rsplit(".", 1)[-1] for owner in R._OWNER.values() if owner.startswith("herdr_team.canvas_kinds.")}
        self.assertLessEqual(owners, set(found))

    def test_the_page_tables(self):
        self.assertEqual(R.API_VERSION, 5)  # 5: OpSpec.proposable (canvas v2 phase 5), additive
        self.assertEqual(R.names(page_only=True), list(PHASE0_ELEMENT_TYPES), "the 15 kinds the v1 page draws (D8): frozen")
        self.assertLessEqual({"chart", "mermaid", "viz"}, set(R.slots()))
        for slot in R.slots():
            self.assertTrue(slot and slot.isidentifier(), slot)

    def test_the_page_tools_and_token_defaults_are_the_registrys(self):
        asset = T.asset()
        tools = R.tools()
        self.assertEqual([t["id"] for t in asset["tools"]], [k.name for k in tools])
        keys = [k.tool.key for k in tools]
        self.assertEqual(len(set(keys)), len(keys))
        for kind, entry in zip(tools, asset["tools"]):
            with self.subTest(kind=kind.name):
                self.assertIn(kind.tool.gesture, R.GESTURES)
                self.assertNotIn(kind.tool.key, R.RESERVED_TOOL_KEYS)
                if kind.tool.gesture == "shape":
                    self.assertEqual(kind.subkind_of, "shape")
                self.assertEqual((entry["kind"], entry["key"], entry["gesture"], entry["one_shot"]),
                                 (kind.name, kind.tool.key, kind.tool.gesture, kind.tool.one_shot))
        # Phase 2 (6.4): the sticky took N from the Phase 0 note; card (C) and section (S) are new, with their templates.
        self.assertTrue(subsequence(("box", "ellipse", "diamond", "sticky", "card", "text", "arrow", "pen", "frame", "section"),
                                    [k.name for k in tools]), [k.name for k in tools])
        self.assertIsNone(R.get("note").tool)
        by_kind = {t["kind"]: t for t in asset["tools"]}
        self.assertEqual((by_kind["sticky"]["key"], by_kind["sticky"]["gesture"], by_kind["sticky"]["template"]),
                         ("n", "block", {"op": "sticky", "text": ""}))
        self.assertEqual((by_kind["card"]["key"], by_kind["card"]["template"]), ("c", {"op": "card", "title": ""}))
        self.assertEqual((by_kind["section"]["key"], by_kind["section"]["gesture"], by_kind["section"]["template"]),
                         ("s", "frame", {"op": "section", "title": "Section", "layout": "free"}))
        for entry in asset["tools"]:
            self.assertEqual(set(entry) - {"template"}, {"id", "kind", "key", "title", "glyph", "gesture", "one_shot", "order"})
        self.assertEqual(set(asset["defaults"]), set(T.kind_groups()))
        for name, pair in asset["defaults"].items():
            self.assertEqual(tuple(pair), T.default_tone(name))
        self.assertEqual(asset["defaults"]["note"], ["idea", "soft"])
        self.assertEqual(asset["lod"]["titles"], 0.35)
        self.assertEqual(asset["lod"]["overview"], 0.15)

    def test_a_bad_tool_is_refused_at_registration(self):
        saved = dict(R._REGISTRY)
        self.addCleanup(lambda: (R._REGISTRY.clear(), R._REGISTRY.update(saved)))
        for tool, subkind in ((R.Tool(key="q", title="Q", glyph="Q", gesture="teleport"), None),
                              (R.Tool(key="v", title="Q", glyph="Q", gesture="pen"), None),
                              (R.Tool(key="r", title="Q", glyph="Q", gesture="pen"), None),  # the rectangle's key
                              (R.Tool(key="QQ", title="Q", glyph="Q", gesture="pen"), None),
                              (R.Tool(key="q", title="Q", glyph="Q", gesture="shape"), None)):  # shape gesture, not a shape
            with self.subTest(tool=tool):
                with self.assertRaises(ValueError):
                    R.register(R.Kind(name="test-tool", subkind_of=subkind, tool=tool))
        R.register(R.Kind(name="test-tool-ok", subkind_of="shape", tool=R.Tool(key="q", title="Q", glyph="Q", gesture="shape")))

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
