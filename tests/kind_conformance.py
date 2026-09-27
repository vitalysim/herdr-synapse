"""The kind conformance suite (canvas v2): what every registered kind must do, whatever it is.

``test_canvas_kinds`` runs it over every kind in the registry, so a new kind is
covered the day it is registered (architecture 1.8, step 5). Kind-specific
behaviour goes in ``tests/test_kind_<kind>.py``.

For every kind: it is valid, and its ``readback`` never raises on a malformed
element. For a kind with ``measure``: the fit invariant holds on seeded random
labels at random minimums, the result is never smaller than the minimum, and
measuring is deterministic. For a kind with ``emit``: deterministic. For a kind
above version 1: ``upgrade`` from every older version gives an element its
readback and measure accept.
"""
from __future__ import annotations

import random
from typing import Any, Dict, List

from herdr_team import canvas_kinds as R
from herdr_team import canvas_text as X

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
