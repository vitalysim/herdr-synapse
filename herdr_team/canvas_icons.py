"""Icons for the canvas (canvas v2 phase 2, 4.2): Lucide, vendored, drawn as display-list paths.

The icon set is ``@iconify-json/lucide`` (ISC; ``assets/icons/lucide/``,
unmodified, source and checksums in ``assets/icons/README.md``). Python turns an
icon into ``path`` primitives in its 24-unit box inside one ``group`` with a
scale and a place (``emit``), so the page draws icons with no icon code of its
own and the agent's picture draws the same paths (phase 2, D8).

* ``resolve(name)`` finds an icon by its name, in any case, with ``_`` or spaces
  for ``-``, by our own aliases (``assets/icons/aliases.json``: ``db`` is
  ``database``) and by Lucide's own (``alert-circle`` is ``circle-alert``);
* ``suggest(name)`` names the nearest icons for a refusal; ``search(word)`` lists
  the icons whose name holds a word (``canvas icons --search``);
* ``paths(name)`` is the icon as absolute ``M L C Q Z`` path data;
* ``python3 -m herdr_team.canvas_icons --check`` verifies the vendored files
  against their recorded sha256 and that every alias names an icon.

Pure apart from one cached read of the JSON files; the standard library only.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from herdr_team import canvas_geometry as _geo

ROOT = Path(__file__).resolve().parent.parent / "assets" / "icons"
ICONS_PATH = ROOT / "lucide" / "icons.json"
ALIASES_PATH = ROOT / "aliases.json"
README_PATH = ROOT / "README.md"
#: The files ``--check`` verifies (their sha256 is recorded in the README).
VENDORED = ("lucide/icons.json", "lucide/info.json", "lucide/LICENSE")
#: The icon box Lucide draws in, and its stroke width there.
BOX = 24.0
STROKE = 2.0
#: Four cubic pieces draw a circle this close (the usual kappa).
KAPPA = 0.5522847498307936
#: The most names ``search`` lists.
SEARCH_MAX = 60

_CACHE: Dict[str, Any] = {}
_SHAPES: Dict[str, List[Tuple[str, bool, bool]]] = {}


def _fmt(value: float) -> str:
    rounded = round(float(value) + 0.0, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return ("{:.2f}".format(rounded)).rstrip("0").rstrip(".")


def load() -> Dict[str, Any]:
    """``{"icons": {name: body}, "lucide_aliases": {alias: parent}, "aliases": {alias: name}}`` (read once)."""
    found = _CACHE.get("data")
    if found is not None:
        return found
    try:
        raw = json.loads(ICONS_PATH.read_text(encoding="utf-8"))
        ours = json.loads(ALIASES_PATH.read_text(encoding="utf-8")).get("aliases") or {}
    except (OSError, ValueError):
        raw, ours = {}, {}
    icons = {name: str(entry.get("body") or "") for name, entry in (raw.get("icons") or {}).items() if isinstance(entry, dict)}
    lucide = {name: str(entry.get("parent")) for name, entry in (raw.get("aliases") or {}).items() if isinstance(entry, dict) and entry.get("parent")}
    data = {"icons": icons, "lucide_aliases": lucide, "aliases": {str(k): str(v) for k, v in ours.items()}}
    _CACHE["data"] = data
    return data


def names() -> List[str]:
    """Every icon name, sorted."""
    return sorted(load()["icons"])


def _key(name: str) -> str:
    return re.sub(r"[\s_]+", "-", name.strip().lower())


def resolve(name: Any) -> Optional[str]:
    """The Lucide name ``name`` stands for, or None: exact, lower case with ``-`` for ``_`` and spaces, our aliases, then
    Lucide's own aliases."""
    if not isinstance(name, str) or not name.strip():
        return None
    data = load()
    icons = data["icons"]
    if name in icons:
        return name
    key = _key(name)
    for candidate in (key, data["aliases"].get(key), data["aliases"].get(name.strip().lower())):
        seen = 0
        while candidate is not None and candidate not in icons and seen < 4:
            candidate = data["lucide_aliases"].get(candidate)
            seen += 1
        if candidate is not None and candidate in icons:
            return candidate
    return None


def suggest(name: Any, limit: int = 3) -> List[str]:
    """The icons nearest ``name`` (names and aliases), for a refusal's ``did_you_mean``."""
    if not isinstance(name, str):
        return []
    data = load()
    pool = list(data["icons"]) + list(data["aliases"]) + list(data["lucide_aliases"])
    found = []
    for match in difflib.get_close_matches(_key(name), pool, n=limit * 3, cutoff=0.5):
        target = resolve(match)
        if target is not None and target not in found:
            found.append(target)
    return found[:limit]


def search(word: str) -> List[str]:
    """Icons whose name (or an alias of theirs) holds ``word``, best first, at most ``SEARCH_MAX``."""
    key = _key(word)
    if not key:
        return names()[:SEARCH_MAX]
    data = load()
    found: Dict[str, int] = {}
    for name in data["icons"]:
        if key in name:
            found[name] = min(found.get(name, 9), 0 if name == key else 1 if name.startswith(key) else 2)
    for alias, target in list(data["aliases"].items()) + list(data["lucide_aliases"].items()):
        resolved = resolve(target)
        if key in alias and resolved is not None:
            found[resolved] = min(found.get(resolved, 9), 3)
    return sorted(found, key=lambda n: (found[n], len(n), n))[:SEARCH_MAX]


# --------------------------------------------------------------------------
# shapes as path data


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ellipse_d(cx: float, cy: float, rx: float, ry: float) -> str:
    kx, ky = rx * KAPPA, ry * KAPPA
    pts = [
        ("M", [cx + rx, cy]),
        ("C", [cx + rx, cy + ky, cx + kx, cy + ry, cx, cy + ry]),
        ("C", [cx - kx, cy + ry, cx - rx, cy + ky, cx - rx, cy]),
        ("C", [cx - rx, cy - ky, cx - kx, cy - ry, cx, cy - ry]),
        ("C", [cx + kx, cy - ry, cx + rx, cy - ky, cx + rx, cy]),
        ("Z", []),
    ]
    return _join(pts)


def _rect_d(x: float, y: float, w: float, h: float, rx: float, ry: float) -> str:
    rx, ry = min(rx, w / 2.0), min(ry, h / 2.0)
    if rx <= 0 or ry <= 0:
        return _join([("M", [x, y]), ("L", [x + w, y]), ("L", [x + w, y + h]), ("L", [x, y + h]), ("Z", [])])
    kx, ky = rx * (1 - KAPPA), ry * (1 - KAPPA)
    return _join([
        ("M", [x + rx, y]), ("L", [x + w - rx, y]), ("C", [x + w - kx, y, x + w, y + ky, x + w, y + ry]),
        ("L", [x + w, y + h - ry]), ("C", [x + w, y + h - ky, x + w - kx, y + h, x + w - rx, y + h]),
        ("L", [x + rx, y + h]), ("C", [x + kx, y + h, x, y + h - ky, x, y + h - ry]),
        ("L", [x, y + ry]), ("C", [x, y + ky, x + kx, y, x + rx, y]), ("Z", []),
    ])


def _poly_d(points: str, closed: bool) -> str:
    values = [float(v) for v in re.findall(r"[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?", points or "")]
    pairs = list(zip(values[0::2], values[1::2]))
    if len(pairs) < 2:
        return ""
    parts: List[Tuple[str, List[float]]] = [("M", list(pairs[0]))] + [("L", list(p)) for p in pairs[1:]]
    if closed:
        parts.append(("Z", []))
    return _join(parts)


def _join(parts: Sequence[Tuple[str, List[float]]]) -> str:
    return " ".join(command + " ".join(_fmt(v) for v in args) if args else command for command, args in parts)


def _shape_d(tag: str, attrs: Dict[str, str]) -> str:
    if tag == "path":
        try:
            return _join(_geo.path_mlcqz(attrs.get("d", "")))
        except (ValueError, TypeError, IndexError, Exception):  # noqa: B902 - a bad icon draws nothing, never breaks a board
            return ""
    if tag == "circle":
        r = _num(attrs.get("r"))
        return _ellipse_d(_num(attrs.get("cx")), _num(attrs.get("cy")), r, r) if r > 0 else ""
    if tag == "ellipse":
        rx, ry = _num(attrs.get("rx")), _num(attrs.get("ry"))
        return _ellipse_d(_num(attrs.get("cx")), _num(attrs.get("cy")), rx, ry) if rx > 0 and ry > 0 else ""
    if tag == "rect":
        rx = _num(attrs.get("rx"), 0.0)
        ry = _num(attrs.get("ry"), rx)
        if "ry" in attrs and "rx" not in attrs:
            rx = ry
        return _rect_d(_num(attrs.get("x")), _num(attrs.get("y")), _num(attrs.get("width")), _num(attrs.get("height")), rx, ry)
    if tag == "line":
        return _join([("M", [_num(attrs.get("x1")), _num(attrs.get("y1"))]), ("L", [_num(attrs.get("x2")), _num(attrs.get("y2"))])])
    if tag in ("polyline", "polygon"):
        return _poly_d(attrs.get("points", ""), tag == "polygon")
    return ""


def _walk(node: Any, inherited: Dict[str, str], out: List[Tuple[str, bool, bool]]) -> None:
    attrs = dict(inherited)
    attrs.update({k: v for k, v in node.attrib.items() if k in ("fill", "stroke")})
    tag = node.tag.rsplit("}", 1)[-1]
    if tag in ("g", "svg"):
        for child in node:
            _walk(child, attrs, out)
        return
    d = _shape_d(tag, dict(node.attrib))
    if d:
        out.append((d, attrs.get("fill", "currentColor") not in ("none", ""), attrs.get("stroke", "none") not in ("none", "")))


def shapes(name: str) -> List[Tuple[str, bool, bool]]:
    """``(d, filled, stroked)`` of each shape of the icon ``name`` (a Lucide name, already resolved)."""
    found = _SHAPES.get(name)
    if found is not None:
        return found
    body = load()["icons"].get(name)
    out: List[Tuple[str, bool, bool]] = []
    if body:
        try:
            _walk(ET.fromstring("<svg>" + body + "</svg>"), {}, out)
        except ET.ParseError:
            out = []
    _SHAPES[name] = out
    return out


def paths(name: str) -> List[str]:
    """The icon as absolute ``M L C Q Z`` path data in its 24x24 box (an unknown name has none)."""
    resolved = resolve(name)
    return [d for d, _filled, _stroked in shapes(resolved)] if resolved else []


def emit(name: str, x: float, y: float, size: float, paint: Any, lod: Optional[list] = None) -> Optional[Dict[str, Any]]:
    """The icon as one ``group`` primitive: its paths in icon units, scaled to ``size`` and placed at ``(x, y)``, stroked
    (and, where Lucide fills, filled) in ``paint``; None for a name that is no icon."""
    resolved = resolve(name)
    if resolved is None:
        return None
    scale = float(size) / BOX
    items = []
    for d, filled, stroked in shapes(resolved):
        items.append({"k": "path", "d": d, "fill": paint if filled else None, "stroke": paint if stroked else None, "sw": STROKE})
    group: Dict[str, Any] = {"k": "group", "t": [scale, 0, 0, scale, x, y], "items": items}
    if lod is not None:
        group["lod"] = list(lod)
    return group


# --------------------------------------------------------------------------
# --check and the command line


def recorded_hashes() -> Dict[str, str]:
    """``{relative path: sha256}`` as ``assets/icons/README.md`` records them."""
    try:
        text = README_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    return {m.group(1): m.group(2) for m in re.finditer(r"`([\w./-]+)`\s*\|\s*`([0-9a-f]{64})`", text)}


def check() -> List[str]:
    """Every way the vendored icons differ from what the README records, and every alias that names no icon."""
    problems = []
    recorded = recorded_hashes()
    for rel in VENDORED:
        path = ROOT / rel
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            problems.append("{} is missing".format(rel))
            continue
        if recorded.get(rel) != digest:
            problems.append("{} sha256 {} is not the one assets/icons/README.md records ({})".format(rel, digest, recorded.get(rel)))
    data = load()
    for alias, target in sorted(data["aliases"].items()):
        if resolve(target) is None:
            problems.append("alias {} names {}, which is not an icon".format(alias, target))
    for name in ("info", "lightbulb", "star", "triangle-alert", "octagon-alert", "scale", "circle-help"):
        if not paths(name):
            problems.append("{} (a callout icon) draws nothing".format(name))
    return problems


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m herdr_team.canvas_icons", description="The canvas's Lucide icons.")
    parser.add_argument("--check", action="store_true", help="verify the vendored files and the aliases")
    parser.add_argument("--search", metavar="WORD", help="list the icons whose name holds a word")
    args = parser.parse_args(list(argv))
    if args.check:
        problems = check()
        for problem in problems:
            sys.stderr.write(problem + "\n")
        return 1 if problems else 0
    found = search(args.search) if args.search else names()
    sys.stdout.write("\n".join(found) + ("\n" if found else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
