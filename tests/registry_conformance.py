"""Registry assertions that hold whatever modules a package gains (QA phase 2, R6).

The kind, layout and router registries discover their modules; a test that
lists every module, or assumes a test fixture registers last, fails the day a
real module is added. These helpers check the promises instead: the built-in
modules are all there and keep their relative order, and the whole list is in
discovery order (``ORDER``, then module name), wherever a new module falls.
"""
from __future__ import annotations

import sys
from typing import Any, Sequence

DEFAULT_ORDER = 1000


def order_of(package: Any, module: str) -> int:
    """A discovered module's ``ORDER`` (``DEFAULT_ORDER`` without one)."""
    found = sys.modules.get("{}.{}".format(package.__name__, module))
    return getattr(found, "ORDER", DEFAULT_ORDER) if found is not None else DEFAULT_ORDER


def assert_contains_in_order(case: Any, found: Sequence[str], expected: Sequence[str]) -> None:
    """Every name of ``expected`` is in ``found``, in the same relative order; others may sit anywhere among them."""
    case.assertEqual([name for name in found if name in set(expected)], list(expected), found)


def assert_discovery_order(case: Any, package: Any, modules: Sequence[str]) -> None:
    """``modules`` is sorted by ``(ORDER, module name)``, as discovery registers them."""
    keys = [(order_of(package, name), name) for name in modules]
    case.assertEqual(keys, sorted(keys), modules)


def assert_registered_in_place(case: Any, package: Any, modules: Sequence[str], name: str) -> None:
    """``name`` is one of ``modules``, in the place its ``ORDER`` gives it (not necessarily the last)."""
    case.assertIn(name, modules)
    assert_discovery_order(case, package, modules)
