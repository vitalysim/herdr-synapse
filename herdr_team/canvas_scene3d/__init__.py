"""The scene3d registry (canvas v2 phase 4, ``.local/prd/canvas-v2-phase3-4.md`` 3): one module per primitive.

A ``scene3d`` element is a small 3D scene an agent describes by structure:
objects made of primitives (a box, a sphere, a glTF model from ``artifacts/``),
placed by relations (``on``, ``right_of``, ``inside``, ``around`` ...) and group
layouts, never by coordinates unless it insists (``pos``). Python solves the
relations into positions (``_solver``), projects the solved scene into an
orthographic drawing for the agent's picture (``_project``), and reads it back
as text (``_describe``); the page renders the same solved scene with three.js.

Every public module of this package that exports ``PRIMITIVES`` is registered,
in the order its ``ORDER`` says (the ``canvas_kinds`` pattern): a new primitive
is one module and nothing else. ``relations`` and ``layouts`` are registries of
their own (``RELATIONS``, ``LAYOUTS``). Modules whose name starts with ``_`` are
helpers: ``_vec`` (vectors and boxes), ``_tokens`` (the design tokens),
``_spec`` (validating objects and links), ``_solver``, ``_gltf`` (reading and
packing glTF), ``_project`` and ``_describe``.

Pure: no I/O (a loader reads files only through the ``FetchIO`` it is handed),
no import of ``canvas`` or ``canvas_kinds``. Python 3.9, stdlib only.
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from herdr_team.canvas_layouts._discovery import discover

#: The version of the ``Primitive`` contract; bumped when a hook's signature changes.
API_VERSION = 1
#: Where a module without its own ``ORDER`` is registered: after the built-in ones (10 ... 90), by module name.
DEFAULT_ORDER = 1000

#: The ids an object may take (the op's ``objects[].id``).
MAX_OBJECTS = 150
MAX_LINKS = 150
#: The whole stored element, as compact JSON.
MAX_ELEMENT_BYTES = 48 * 1024
#: A primitive's own size limits, in scene units.
MIN_SIZE = 0.001
MAX_SIZE = 10_000.0
#: How finely ``faces`` tessellates round shapes for tests (the projections use closed-form silhouettes).
DETAIL = 16

Vec = Tuple[float, float, float]


@dataclass(frozen=True)
class Face:
    """A planar polygon of a primitive in its local frame (centred on x and z, base at y = 0), wound counter-clockwise
    seen from outside; ``normal`` points out."""

    points: Tuple[Vec, ...]
    normal: Vec


@dataclass(frozen=True)
class Model:
    """What a loader read: the asset bytes the page loads, the model's native size and the facts readback prints."""

    #: The asset to store (a packed ``.glb``); the element names it by the store's content address.
    data: bytes
    #: The source file under ``artifacts/`` and the sha256 of what was read.
    src: str
    sha256: str
    #: The model's world bounds in its own units: ``(x0, y0, z0, x1, y1, z1)``.
    bounds: Tuple[float, float, float, float, float, float]
    triangles: int
    #: ``meshes``, ``materials``, ``textures``, ``nodes`` (the first 8 names), ``packed`` (a .gltf turned into one GLB).
    facts: Dict[str, Any]


@dataclass(frozen=True)
class Loader:
    """How a primitive reads its file (glTF): ``read(io, obj, field) -> Model`` through ``FetchIO``, never ``open``."""

    name: str
    #: The object field naming the file (``src``).
    field: str
    suffixes: Tuple[str, ...]
    read: Callable[[Any, Dict[str, Any], str], Model]
    doc: str = ""


@dataclass(frozen=True)
class Primitive:
    """One shape an object may be (``shape``). Hooks get the object's params as ``normalize`` returned them."""

    name: str
    #: Its own object fields, beyond the common ones (``size`` for a box, ``radius`` and ``height`` for a cylinder).
    params: Tuple[str, ...]
    #: ``(obj, field) -> params``: its params validated with their defaults; refusals raise ``refuse(...)`` naming
    #: ``objects[3].radius``. ``obj`` is the raw object; ``field`` is ``objects[3]``.
    normalize: Callable[[Dict[str, Any], str], Dict[str, Any]]
    #: ``params -> (w, h, d)``: its local box size, centred on x and z with its base at y = 0 (before rotation).
    extent: Callable[[Dict[str, Any]], Vec]
    #: ``(params, detail) -> [Face]``: its surface in its local frame, closed and facing out (empty for shapes the
    #: projection draws otherwise: text, arrows, groups).
    faces: Callable[[Dict[str, Any], int], List[Face]]
    #: ``(params, to2d) -> [outline]``: its projected outline in closed form (a sphere's circle, a cylinder's two
    #: ellipses and their tangents), where ``to2d`` maps a local 3D point to the picture. Each outline is
    #: ``{"part": "body" | "cap", "d": path, "points": [[x, y], ...] (samples on the outline)}``. None: its faces.
    silhouette: Optional[Callable[[Dict[str, Any], Callable[[Sequence[float]], Tuple[float, float]]], List[Dict[str, Any]]]] = None
    #: ``params -> str``: ``box 1.2×0.4×1.2``, ``cylinder r0.6 h1.4``.
    describe: Callable[[Dict[str, Any]], str] = lambda params: ""
    #: May hold children (``in``): its extent is the union of its children after its layout.
    container: bool = False
    #: Reads a file (glTF); its ``Model`` gives the extent.
    loader: Optional[Loader] = None
    order: int = 100
    doc: str = ""
    #: One valid ``scene3d`` op using it, for the catalog, the guides' test and conformance.
    example: str = ""


_REGISTRY: Dict[str, Primitive] = {}
_OWNER: Dict[str, str] = {}
_LOADED = False
_CHANGED: List[Callable[[], None]] = []


def register(prim: Primitive) -> Primitive:
    """Add a primitive; a second one with the same name, or a param that shadows a common object field, is a programming
    error."""
    from herdr_team.canvas_scene3d import _spec

    if not prim.name or not prim.name.replace("_", "").isalnum() or prim.name != prim.name.lower():
        raise ValueError("primitive {!r}: a name is lower-case letters, digits and _".format(prim.name))
    if prim.name in _REGISTRY and _REGISTRY[prim.name] is not prim:
        raise ValueError("primitive {} is registered twice".format(prim.name))
    shadowed = [p for p in prim.params if p in _spec.common_fields()]
    if shadowed:
        raise ValueError("primitive {}: {} is a common object field".format(prim.name, ", ".join(shadowed)))
    if len(set(prim.params)) != len(prim.params):
        raise ValueError("primitive {}: a param is listed twice".format(prim.name))
    _REGISTRY[prim.name] = prim
    return prim


def _register_module(module: Any, owner: str) -> None:
    for prim in getattr(module, "PRIMITIVES", ()):
        register(prim)
        _OWNER[prim.name] = owner


def load() -> None:
    """Import every primitive module in this package once (``relations`` and ``layouts`` register themselves)."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        for _order, short, module in discover(__name__, __path__, "PRIMITIVES"):
            _register_module(module, "{}.{}".format(__name__, short))
    except BaseException:
        _LOADED = False
        raise


def _load_extra(module_name: str) -> None:
    """Test hook: register one more module's ``PRIMITIVES`` (the one-module proof's ``torus``)."""
    load()
    _register_module(importlib.import_module(module_name), module_name)
    for hook in _CHANGED:
        hook()


def _unload(module_name: str) -> None:
    """Test hook: take the primitives ``_load_extra`` registered from ``module_name`` out again."""
    for name, owner in list(_OWNER.items()):
        if owner == module_name:
            _REGISTRY.pop(name, None)
            del _OWNER[name]
    sys.modules.pop(module_name, None)
    for hook in _CHANGED:
        hook()


def on_change(hook: Callable[[], None]) -> None:
    """Call ``hook`` whenever a test hook changes the registry (derived tables are cached)."""
    if hook not in _CHANGED:
        _CHANGED.append(hook)


def get(name: Any) -> Optional[Primitive]:
    load()
    return _REGISTRY.get(name) if isinstance(name, str) else None


def primitives() -> List[Primitive]:
    """Every registered primitive, in registration order."""
    load()
    return list(_REGISTRY.values())


def names() -> List[str]:
    return [prim.name for prim in primitives()]


def loaders() -> List[str]:
    """The loaders the primitives use (``gltf``), in registration order."""
    out: List[str] = []
    for prim in primitives():
        if prim.loader is not None and prim.loader.name not in out:
            out.append(prim.loader.name)
    return out


def catalog(name: Optional[str] = None) -> Dict[str, Any]:
    """What ``canvas catalog scene3d`` prints: the primitives with their params, doc and example, the relations and the
    group layouts (interface I-8). ``name`` narrows it to one primitive, relation or layout."""
    from herdr_team.canvas_scene3d import _spec, layouts, relations

    prims = [{"name": p.name, "params": list(p.params), "doc": p.doc, "example": p.example, "container": p.container,
              "loader": p.loader.name if p.loader is not None else None} for p in primitives()]
    rels = [{"name": r.name, "group": r.group, "doc": r.doc, "gap": r.gap} for r in relations.relations()]
    lays = [{"name": l.name, "params": list(l.params), "doc": l.doc} for l in layouts.layouts()]
    out: Dict[str, Any] = {"primitives": prims, "relations": rels, "layouts": lays, "common": list(_spec.common_fields()),
                           "settings": dict(_spec.SETTINGS_DOC), "limits": {"objects": MAX_OBJECTS, "links": MAX_LINKS,
                                                                            "element_bytes": MAX_ELEMENT_BYTES}}
    if name is not None:
        out = {key: [item for item in value if item.get("name") == name] if isinstance(value, list) and key in ("primitives", "relations", "layouts")
               else value for key, value in out.items()}
    return out
