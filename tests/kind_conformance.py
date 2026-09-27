"""The kind conformance suite (canvas v2): what every registered kind must do, whatever it is.

``test_canvas_kinds`` runs it over every kind in the registry, so a new kind is
covered the day it is registered (architecture 1.8, step 5). Kind-specific
behaviour goes in ``tests/test_kind_<kind>.py``.

For every kind: it is valid, and its ``readback`` never raises on a malformed
element. For a kind with ``measure``: the fit invariant holds on seeded random
labels at random minimums, the result is never smaller than the minimum, and
measuring is deterministic. For a kind with ``emit``: deterministic. For a kind
above version 1: ``upgrade`` from every older version gives an element its
readback and measure accept. Since canvas v2 phase 1, for every kind: its
display-list entry validates (random and malformed elements alike), its
``hit``, ``handles`` and ``edit`` are well formed, and a ``translate`` or
``resize`` leaves an element that still validates.
"""
from __future__ import annotations

import random
from typing import Any, Dict, List

from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as R
from herdr_team import canvas_text as X
from herdr_team import canvas_theme as T
from herdr_team.errors import HerdrTeamError

SEED = 1301
LABELS = 300
WORDS = ("Checkout", "API", "service", "p99", "Internationalization", "a", "->", "résumé", "中文标签", "\U0001F680",
         "https://example.com/a/b/c.html", "snake_case_name", "WWWW", "(draft)")

#: Elements a kind must read back without raising: missing fields, wrong types, junk values.
MALFORMED: List[Dict[str, Any]] = [
    {},
    {"id": "E-1"},
    {"id": "E-2", "x": None, "y": "5", "w": -3, "h": "tall", "text": None, "style": "red", "fit": "hug"},
    {"id": "E-3", "text": 42, "style": {"size": "huge", "font": 7}, "fit": {"min": "x", "lines": 3, "size": -1}},
    {"id": "E-4", "points": "nowhere", "from": 3, "to": [], "style": None, "wrap": "yes"},
]


def random_text(rng: random.Random) -> str:
    if rng.random() < 0.05:
        return ""
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 12)))


def element(kind: R.Kind, rng: random.Random) -> Dict[str, Any]:
    return {"id": "E-1", "type": kind.name, "x": 0, "y": 0, "w": 160, "h": 80, "text": random_text(rng),
            "style": {"size": rng.choice((16, 20, 28, 36)), "font": rng.choice(("normal", "normal", "code", "hand"))},
            "wrap": rng.random() < 0.3}


def check_kind(case: Any, kind: R.Kind) -> None:
    """Run every conformance check for ``kind`` inside ``case`` (a ``unittest.TestCase``)."""
    case.assertIn(kind.role, R.ROLES)
    case.assertIn(kind.outline, R.OUTLINES, "{}'s outline is one layout can test".format(kind.name))
    case.assertGreaterEqual(kind.version, 1)
    if kind.fit is not None:
        case.assertIn(kind.fit, X.FIT_POLICIES, "{}'s default fit policy is registered".format(kind.name))
    for bad in MALFORMED:
        el = dict(bad, type=kind.name)
        if kind.readback is not None:
            case.assertIsInstance(kind.readback(el, True), str)
            case.assertIsInstance(kind.readback(el, False), str)
    rng = random.Random(SEED)
    if kind.measure is not None:
        for index in range(LABELS):
            el = element(kind, rng)
            minimum = (float(rng.choice((1, 40, 80, 160, 240))), float(rng.choice((1, 20, 60, 100))))
            result = kind.measure(el, minimum)
            context = (kind.name, index, el["text"][:40], el["style"], minimum)
            case.assertTrue(X.fits(result), context)
            case.assertGreaterEqual(result.w, minimum[0], context)
            case.assertEqual(result, kind.measure(el, minimum), context)
            if kind.readback is not None:
                case.assertIsInstance(kind.readback(dict(el, w=result.w, h=result.h, fit=X.fit_record(result, minimum)), True), str)
    if kind.emit is not None:
        el = element(kind, rng)
        env = {"reader": None, "by_id": {}, "scene": {}}
        case.assertEqual(kind.emit(el, env), kind.emit(el, env))
    if kind.version > 1:
        case.assertIsNotNone(kind.upgrade, "{} is above version 1, so it needs upgrade".format(kind.name))
        for stored in range(1, kind.version):
            upgraded = kind.upgrade(element(kind, rng), stored)  # type: ignore[misc]
            case.assertEqual(upgraded.get("type"), kind.name)
    check_display(case, kind, rng)


class _ResizeCtx:
    """What a kind's ``resize`` may ask of its context, answered by the canvas's own validators."""

    def number(self, op, field, low, high, default=None):
        from herdr_team import canvas as C

        return default if op.get(field) is None else C._num(op[field], field, low, high)

    def invalid(self, field, message, **details):
        from herdr_team import canvas as C

        return C._invalid(field, message, **details)

    def text_fields(self, el, text, style, wrap_w=None):
        from herdr_team import canvas as C

        return C._text_fields(el, text, style, wrap_w)


def _valid_entry(case, kind, el, context):
    entry = D.entry(el, D.environment({}))
    doc = {"dl": 1, "palettes": {theme: T.palette(theme) for theme in T.THEMES}, "entries": [entry]}
    case.assertEqual(D.validate(doc), [], context)
    case.assertIn(entry["hit"]["shape"], D.HITS, context)
    case.assertIn(entry["handles"], R.HANDLES, context)
    if entry["edit"] is not None:
        case.assertEqual(set(entry["edit"]) >= {"field", "value", "box", "font", "weight", "size", "lh", "align", "wrap", "fill"}, True, context)
        case.assertEqual(len(entry["edit"]["box"]), 4, context)
    return entry


def check_display(case, kind, rng):
    """The kind's display-list entry for random and malformed elements, and after a move and a resize."""
    for index in range(20):
        el = element(kind, rng)
        el.update(points=[[0, 0], [rng.randint(10, 300), rng.randint(-100, 100)]], point=[5, 5], z=index, updated_seq=index)
        entry = _valid_entry(case, kind, el, (kind.name, index))
        case.assertEqual(entry, D.entry(el, D.environment({})), "an entry is a function of its element")
        if kind.translate is not None:
            moved = dict(el, x=el["x"] + 7, y=el["y"] - 3, **kind.translate(el, 7, -3))
            _valid_entry(case, kind, moved, (kind.name, index, "moved"))
        if kind.resize is not None:
            try:
                resized = dict(el, **kind.resize(el, 240, 120, _ResizeCtx()))
            except HerdrTeamError:
                continue  # a kind without a size (a comment pin) refuses a resize, and says so
            _valid_entry(case, kind, resized, (kind.name, index, "resized"))
    for bad in MALFORMED:
        _valid_entry(case, kind, dict(bad, type=kind.name), (kind.name, bad))
