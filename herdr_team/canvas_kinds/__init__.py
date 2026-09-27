"""The canvas component registry (canvas v2 foundation).

Every kind of element the canvas knows (``box``, ``text``, ``arrow`` today;
``card``, ``kanban``, ``chart``, ``scene3d`` later) is one ``Kind`` record,
defined in one module of this package and listed once in ``_MODULES``. The
record carries the hooks the rest of the canvas asks a kind for: which op
fields it takes, how its text is fitted (``canvas_text`` fit policies), how
big it is before layout, what it reads back as in ``look``, which checks
apply to it, how an older stored element is upgraded, and (from canvas v2
phase 1) the display-list primitives it draws as. A hook left ``None`` means
"the generic behaviour", so a kind grows hook by hook without touching the
others. Contract and rationale: ``.local/prd/canvas-v2-architecture.md``.

The page keeps the matching registry under ``web/src/canvas/kinds/``, keyed
by the same ``name``; the build writes the page's list to
``web/dist/kinds.json`` and a test holds the two lists equal for every kind
with ``page=True``.

Pure: no I/O, no Herdr calls, and no import of ``canvas`` (that module
imports this one). Kind modules may import ``canvas_text`` and
``canvas_theme``, never ``canvas``.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

#: The version of the ``Kind`` contract itself; bumped when a hook's signature changes.
API_VERSION = 1

#: How a kind takes part in layout: a ``leaf`` is sized from its own content; a ``container``
#: sizes around its children (a frame, a section); a ``composite`` lays out its own addressable
#: parts (a graph, a kanban); a ``connector`` is routed between others (an arrow); an ``overlay``
#: takes no room (a comment pin).
ROLES = ("leaf", "container", "composite", "connector", "overlay")

#: The kind modules, in registration order. Adding a kind: one module here plus this one line.
_MODULES: Tuple[str, ...] = ("shape", "text", "legacy")

Element = Dict[str, Any]
Size = Tuple[float, float]


@dataclass(frozen=True)
class Kind:
    """One element kind and its hooks. ``name`` is the element ``type`` in ``scene.json``."""

    name: str
    #: The shape of this kind's stored element; an element stores ``kv`` only when it is above 1.
    version: int = 1
    role: str = "leaf"
    #: The ops that create this kind (``shape`` creates ``box``, ``ellipse`` ...).
    ops: Tuple[str, ...] = ()
    #: Op fields this kind accepts beyond the common ones (``op``, ``intent``, ``if_version``).
    fields: Tuple[str, ...] = ()
    #: The default fit policy (a name in ``canvas_text.FIT_POLICIES``); None for kinds without text.
    fit: Optional[str] = None
    #: Whether the page needs a renderer for this kind (see the module docstring).
    page: bool = True
    #: ``(w, h) -> (x, y, w, h)``: the box text may use inside a ``w`` x ``h`` element (a diamond's is smaller).
    inset: Optional[Callable[[float, float], Tuple[float, float, float, float]]] = None
    #: ``(element, minimum (w, h)) -> fit result``: the element's size from its content, before layout.
    measure: Optional[Callable[[Element, Size], Any]] = None
    #: ``(element, full) -> str``: the element's compact form in ``look`` (without "by who — intent").
    readback: Optional[Callable[[Element, bool], str]] = None
    #: Each ``(element, env) -> [problem]``, run by ``canvas_check`` next to the global checks.
    checks: Tuple[Callable[[Element, Dict[str, Any]], List[Dict[str, Any]]], ...] = ()
    #: ``(element, stored version) -> element``: an older stored element in this version's shape.
    upgrade: Optional[Callable[[Element, int], Element]] = None
    #: ``(element, env) -> [primitive]``: the display list (canvas v2 phase 1).
    emit: Optional[Callable[[Element, Dict[str, Any]], List[Dict[str, Any]]]] = None
    #: One line for generated docs and MCP schema descriptions.
    doc: str = ""


_REGISTRY: Dict[str, Kind] = {}
_LOADED = False


def register(kind: Kind) -> Kind:
    """Add ``kind``; a second kind with the same name, or an unknown role, is a programming error."""
    if kind.role not in ROLES:
        raise ValueError("kind {}: role {!r} is not one of {}".format(kind.name, kind.role, ", ".join(ROLES)))
    if kind.name in _REGISTRY and _REGISTRY[kind.name] is not kind:
        raise ValueError("kind {} is registered twice".format(kind.name))
    _REGISTRY[kind.name] = kind
    return kind


def load() -> None:
    """Import every module in ``_MODULES`` once; each registers its ``KINDS``."""
    global _LOADED
    if _LOADED:
        return
    for module_name in _MODULES:
        module = importlib.import_module("{}.{}".format(__name__, module_name))
        for kind in getattr(module, "KINDS", ()):
            register(kind)
    _LOADED = True


def get(name: Any) -> Optional[Kind]:
    """The kind named ``name``, or None for a kind this build does not know (callers fall back to generic)."""
    load()
    return _REGISTRY.get(name) if isinstance(name, str) else None


def kinds() -> List[Kind]:
    """Every registered kind, in registration order."""
    load()
    return list(_REGISTRY.values())


def names(page_only: bool = False) -> List[str]:
    """Registered kind names; ``page_only`` keeps those the page must render."""
    return [kind.name for kind in kinds() if kind.page or not page_only]


def by_op(op: str) -> List[Kind]:
    """The kinds an op creates."""
    return [kind for kind in kinds() if op in kind.ops]


def drawn(el: Element) -> Any:
    """The element's label as it is drawn now, or None for a kind without text: at the element's current
    size and drawn text size (``fit.size``, else ``style.size``) when it fits there (the ``keep`` fit
    policy), else the size its kind would grow it to, with no further shrinking.

    ``canvas_check`` compares its size with the element's to find overflow, and
    ``canvas_render`` takes its inner box and lines to draw the label.
    """
    kind = get(el.get("type"))
    if kind is None or kind.measure is None:
        return None
    style = el.get("style") if isinstance(el.get("style"), dict) else {}
    fit = el.get("fit") if isinstance(el.get("fit"), dict) else {}
    size = fit.get("size") if isinstance(fit.get("size"), (int, float)) and fit["size"] > 0 else style.get("size")
    # A clamped label shows only its first lines by design (``label_truncated`` says so); anything else must fit.
    probe = dict(el, style=dict(style, size=size or 20), fit=dict(fit, policy="clamp" if fit.get("policy") == "clamp" else "keep"))
    w, h = max(1.0, float(el.get("w") or 1)), max(1.0, float(el.get("h") or 1))
    if el.get("type") == "text":
        # A text's box is its lines: wrapped at its wrap width when it wraps, else as wide as they need.
        stored = fit.get("min") if isinstance(fit.get("min"), list) and len(fit["min"]) == 2 else None
        wrap = (float(stored[0]) if stored else w) if el.get("wrap") else 1.0
        return kind.measure(probe, (wrap, 1.0))
    return kind.measure(probe, (w, h))
