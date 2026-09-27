"""Canvas rendering without a browser: scene -> SVG -> PNG, with id marks and grid dots (0.21).

Contract ``.local/prd/canvas-contracts.md`` section 8.3. Agents read the
canvas as text first and as a picture second; this module draws the picture.
The geometry is exact, the engine's hand-drawn look is not reproduced:

* ``render_svg`` draws the canonical scene (or one region of it) as one
  standalone SVG document: shapes, notes, text, frames, arrows with heads,
  pen strokes as filled outlines (a port of perfect-freehand), paths,
  sanitised ``svg`` blocks inlined, images as ``data:`` URIs, and chart,
  mermaid and viz elements as the still the page captured or a labelled
  placeholder. Claims are dashed, locks hatched, comments are pins. Every
  element carries its id in a small badge (Set-of-Mark), and ``grid`` adds
  cell dots and names (Scaffold), because models locate marked things far
  better than unmarked ones.
* ``render_png`` rasterises through ``resvg`` via the ``RUN`` hook, so tests
  fake it; a missing or failing ``resvg`` is ``None``, never an exception.
  Text is drawn in the bundled fonts (Inter, Geist Mono under
  ``assets/fonts``), the files ``canvas_text`` measured, at the lines the
  element's fit stored (0.22), so the PNG breaks lines where the model did.
* ``sanitize_svg`` rebuilds agent SVG from an allow-list and refuses the
  whole block (``svg_refused``) on scripts, foreign objects, event handlers,
  external references and CSS ``url()``; ``sanitize_path_data`` and
  ``normalize_path`` accept path commands and numbers only.

This module imports nothing from ``canvas``; ``canvas`` imports it.
"""
from __future__ import annotations

import base64
import html as _html
import math
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team import canvas_kinds as _kinds
from herdr_team import canvas_text as _ctext
from herdr_team import canvas_theme as _theme
from herdr_team import features as _features
from herdr_team import store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError
from herdr_team.paths import TeamPaths, ensure_dir

RESVG_ENV = "HERDR_SYNAPSE_RESVG"
DEFAULT_MAX_PX = 1024
RENDER_TIMEOUT_S = 10.0
#: Newest look/send/export renders kept per team (by name stem, both files of a render).
RENDER_KEEP = 50
PADDING = 40
MAX_SVG_BYTES = 100 * 1024
MAX_SVG_NODES = 5000
#: Nesting deeper than this is refused (the serialiser recurses; real drawings nest a dozen levels).
MAX_SVG_DEPTH = 100
#: Nodes a renderer draws once every ``<use>`` is expanded. A few hundred nested ``<use>`` fanning
#: out tenfold per level expand to millions of draw calls: a hang for the page's sketchy renderer
#: (svg2roughjs expands ``<use>`` recursively with no limit of its own) and work for resvg.
MAX_SVG_EXPANDED_NODES = 20000
MAX_ATTR_CHARS = 20000
DEFAULT_SVG_SIZE = (240.0, 240.0)
NOTE_FILL = "#ffec99"
TEXT_COLOR = "#1e1e1e"
PLACEHOLDER_FILL = "#f8f9fa"
MUTED = "#868e96"
#: Excalidraw's freedraw: an outline this many times the stroke width across at full pressure.
FREEHAND_SIZE = 4.25
FREEHAND_THINNING = 0.6
FREEHAND_STREAMLINE = 0.5
CAP_STEPS = 8

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


def _run_resvg(argv: List[str], timeout: float) -> Tuple[int, str]:
    """Run resvg; ``(returncode, stderr)``."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as err:
        return -1, str(err)
    return done.returncode, done.stderr.decode("utf-8", "replace")


#: Test hook: ``RUN(argv, timeout) -> (returncode, stderr)``.
RUN: Callable[[List[str], float], Tuple[int, str]] = _run_resvg


def _svg_refused(reason: str, message: str) -> HerdrTeamError:
    return HerdrTeamError("svg_refused", "svg refused: {}".format(message), EXIT_REFUSED, {"reason": reason})


def _path_invalid(message: str) -> HerdrTeamError:
    return HerdrTeamError("op_invalid", "path data: {}".format(message), EXIT_REFUSED, {"field": "d"})


# --------------------------------------------------------------------------
# resvg


def find_resvg(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """``$HERDR_SYNAPSE_RESVG`` when it names an executable, else ``resvg`` on ``PATH``, else None."""
    source = os.environ if env is None else env
    override = source.get(RESVG_ENV)
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        return None
    return shutil.which("resvg", path=source.get("PATH") or os.defpath)


#: Where resvg's font loader (fontdb) finds system fonts on macOS, in its order; plus
#: ``$HOME/Library/Fonts`` and the downloadable fonts under ``/System/Library/AssetsV2``.
MAC_FONT_DIRS = ("/Library/Fonts", "/System/Library/Fonts", "/Network/Library/Fonts")
MAC_FONT_ASSETS = "/System/Library/AssetsV2"
FONT_EXTENSIONS = (".ttf", ".ttc", ".otf", ".otc")
#: Test hook: the font folders ``font_args`` looks in, in place of the system's.
FONT_DIRS: Optional[Sequence[str]] = None
#: The bundled fonts resvg draws canvas text with (0.22), the files ``canvas_text`` measures.
FONTS_ROOT = Path(__file__).resolve().parent.parent / "assets" / "fonts"
#: Test hook: the bundled font folders, in place of ``FONTS_ROOT``'s (None: the real ones).
BUNDLED_FONT_DIRS: Optional[Sequence[str]] = None
SANS_FAMILY = "Inter"
MONO_FAMILY = "Geist Mono"


#: Font files resvg must never fall back to, by file name: LastResort (a placeholder box for every code
#: point), and macOS's hidden Nastaliq UI face, which claims Arabic but lacks most of its letters, so
#: with Inter as the base font whole lines of Arabic, Japanese and emoji drew as boxes (QA F-1, 2026-09-27).
FALLBACK_TRAPS = ("lastresort", "decotypenastaleequrdu")
#: The font families the bundled fonts answer for: a picture using only these, in characters they
#: cover, is drawn without loading the system fonts (a quarter of resvg's time on a dense board, QA F-7).
BUNDLED_FAMILIES = frozenset(("inter", "geist mono", "sans-serif", "monospace"))
_FONT_FAMILY_RE = re.compile(r"font-family\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^;\"'>]*)", re.IGNORECASE)
_FONT_SHORTHAND_RE = re.compile(r"(?:^|[\s;\"'])font\s*[:=]", re.IGNORECASE)
_SVG_TAG_RE = re.compile(r"<svg\b([^>]*)>")
_TEXT_TAG_RE = re.compile(r"<text\b([^>]*)>")
_TEXT_NODE_RE = re.compile(r">([^<]+)<")


def _is_trap(name: str) -> bool:
    return name.lower().startswith(FALLBACK_TRAPS)


def bundled_covers(svg_text: str) -> bool:
    """Whether the bundled fonts draw every character of ``svg_text`` in every family it names.

    Conservative: an unknown family, the ``font`` shorthand, a ``<text>`` naming no family when the
    document names none either (resvg's default is a serif), or a character Inter lacks (Arabic, CJK,
    emoji) all say no, and the picture loads the system fonts as before.
    """
    if _FONT_SHORTHAND_RE.search(svg_text):
        return False
    for match in _FONT_FAMILY_RE.finditer(svg_text):
        for family in match.group(1).strip("\"'").split(","):
            family = family.strip().strip("\"'").lower()
            if family and family not in BUNDLED_FAMILIES:
                return False
    root = _SVG_TAG_RE.search(svg_text)
    if not (root and "font-family" in root.group(1)) and any("font-family" not in attrs for attrs in _TEXT_TAG_RE.findall(svg_text)):
        return False
    sans = _ctext.face("normal", _ctext.DEFAULT_WEIGHT)
    if sans.builtin:
        return False
    seen = set()
    for match in _TEXT_NODE_RE.finditer(svg_text):
        for ch in _html.unescape(match.group(1)):
            if ch in seen or ch.isspace():
                continue
            if ord(ch) not in sans.advances:
                return False
            seen.add(ch)
    return True


def font_dirs(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """The folders resvg loads system fonts from on macOS, in its order."""
    source = os.environ if env is None else env
    dirs = list(MAC_FONT_DIRS)
    try:
        dirs[2:2] = [os.path.join(MAC_FONT_ASSETS, name) for name in sorted(os.listdir(MAC_FONT_ASSETS))
                     if name.startswith("com_apple_MobileAsset_Font")]
    except OSError:
        pass
    home = source.get("HOME")
    if home:
        dirs.append(os.path.join(home, "Library", "Fonts"))
    return dirs


def bundled_font_args() -> List[str]:
    """The bundled font folders, and Inter and Geist Mono as the generic sans-serif and monospace families."""
    dirs = BUNDLED_FONT_DIRS if BUNDLED_FONT_DIRS is not None else [str(FONTS_ROOT / "inter"), str(FONTS_ROOT / "geist-mono")]
    argv: List[str] = []
    for folder in dirs:
        if os.path.isdir(folder):
            argv += ["--use-fonts-dir", folder]
    if argv:
        argv += ["--sans-serif-family", SANS_FAMILY, "--monospace-family", MONO_FAMILY]
    return argv


def font_args(dirs: Optional[Sequence[str]] = None, env: Optional[Mapping[str, str]] = None,
              svg_text: Optional[str] = None) -> List[str]:
    """resvg's font arguments: the bundled fonts first, then the system fonts without the ``FALLBACK_TRAPS``.

    LastResort maps every code point to a placeholder box, and resvg hands a whole line to
    the first fallback font that covers all of it, so one character the base font lacks (an
    emoji, a check mark) turned the label into boxes (found live on 2026-09-27). Where a
    folder holds a trap, its other fonts load one by one and its subfolders whole. The
    system fonts stay for what the bundled ones lack (CJK, emoji, Arabic); when ``svg_text``
    needs nothing but the bundled fonts (``bundled_covers``), they are not loaded at all.
    """
    bundled = bundled_font_args()
    if bundled and svg_text is not None and bundled_covers(svg_text):
        return bundled + ["--skip-system-fonts"]
    if dirs is None:
        dirs = FONT_DIRS if FONT_DIRS is not None else font_dirs(env)
    folders = [d for d in dirs if os.path.isdir(d)]
    listed: List[Tuple[str, List[str]]] = []
    for folder in folders:
        try:
            listed.append((folder, sorted(os.listdir(folder))))
        except OSError:
            continue
    if not any(_is_trap(name) for _folder, names in listed for name in names):
        return bundled
    argv = bundled + ["--skip-system-fonts"]
    for folder, names in listed:
        if not any(_is_trap(name) for name in names):
            argv += ["--use-fonts-dir", folder]
            continue
        for name in names:
            path = os.path.join(folder, name)
            if os.path.isdir(path):
                argv += ["--use-fonts-dir", path]
            elif not _is_trap(name) and name.lower().endswith(FONT_EXTENSIONS):
                argv += ["--use-font-file", path]
    return argv


def _rasterise(svg_path: Path, out_path: Path, width_px: Optional[int], env: Optional[Mapping[str, str]], timeout: float,
               svg_text: Optional[str] = None) -> Tuple[Optional[Path], Optional[str]]:
    """``(png path, None)`` or ``(None, "resvg_missing" | "render_failed")``."""
    binary = find_resvg(env)
    if binary is None:
        return None, "resvg_missing"
    argv = [binary] + font_args(env=env, svg_text=svg_text)
    if width_px:
        argv += ["-w", str(int(width_px))]
    argv += [os.fspath(svg_path), os.fspath(out_path)]
    try:
        code, _err = RUN(argv, timeout)
    except Exception:  # a hook must never take the caller down
        return None, "render_failed"
    try:
        head = out_path.read_bytes()[:8] if out_path.is_file() else b""
    except OSError:
        head = b""
    if code != 0 or head != PNG_MAGIC:
        return None, "render_failed"
    try:
        os.chmod(out_path, 0o600)
    except OSError:
        pass
    return out_path, None


def render_png(svg_text: str, out_path: Path, width_px: Optional[int] = None, env: Optional[Mapping[str, str]] = None,
               timeout: float = RENDER_TIMEOUT_S) -> Optional[Path]:
    """Rasterise with resvg; the PNG path, or None when resvg is missing or fails."""
    out_path = Path(out_path)
    try:
        ensure_dir(out_path.parent)
        tmp = out_path.parent / (".tmp-" + out_path.stem + ".svg")
        store.atomic_write(tmp, svg_text.encode("utf-8"), fsync=False)
    except (OSError, HerdrTeamError):
        return None
    try:
        path, _error = _rasterise(tmp, out_path, width_px, env, timeout, svg_text)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return path


def render_region(team: TeamPaths, scene: Dict[str, Any], region: Optional[Sequence[float]], out_dir: Path, name: str,
                  marks: bool = True, grid: bool = False, reader: Optional[str] = None,
                  env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Write ``<name>.svg`` and ``<name>.png`` under ``out_dir``; ``{"svg", "png", "image_error", "width_px", "height_px", "region"}``."""
    out_dir = Path(out_dir)
    ensure_dir(out_dir)
    box = view_box(scene, region)
    width_px, height_px = pixel_size(box, DEFAULT_MAX_PX)
    svg_text = render_svg(scene, box, marks=marks, grid=grid, reader=reader, max_px=DEFAULT_MAX_PX, team=team)
    svg_path = out_dir / (name + ".svg")
    png_path = out_dir / (name + ".png")
    store.atomic_write(svg_path, svg_text.encode("utf-8"), fsync=False)
    png, error = _rasterise(svg_path, png_path, width_px, env, RENDER_TIMEOUT_S, svg_text)
    prune_renders(out_dir, RENDER_KEEP)
    return {"svg": os.fspath(svg_path), "png": os.fspath(png) if png else None, "image_error": error,
            "width_px": width_px, "height_px": height_px, "region": [round(v, 2) for v in box]}


def prune_renders(directory: Path, keep: int = RENDER_KEEP) -> int:
    """Keep the newest ``keep`` render stems in ``directory``; the number of files removed."""
    stems: Dict[str, float] = {}
    files: Dict[str, List[Path]] = {}
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_file(follow_symlinks=False) or entry.name.startswith("."):
            continue
        stem = entry.name.rsplit(".", 1)[0]
        try:
            mtime = entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        stems[stem] = max(stems.get(stem, 0.0), mtime)
        files.setdefault(stem, []).append(Path(entry.path))
    removed = 0
    for stem in sorted(stems, key=lambda s: stems[s], reverse=True)[keep:]:
        for path in files[stem]:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
    return removed


# --------------------------------------------------------------------------
# images


def sniff_image(data: bytes) -> Optional[Tuple[str, int, int]]:
    """``(mime, width_px, height_px)`` for PNG or JPEG bytes (by magic, never by name), else None."""
    if data.startswith(PNG_MAGIC) and len(data) >= 24 and data[12:16] == b"IHDR":
        return "image/png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith(JPEG_MAGIC):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = int.from_bytes(data[index + 2:index + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height = int.from_bytes(data[index + 5:index + 7], "big")
                width = int.from_bytes(data[index + 7:index + 9], "big")
                return "image/jpeg", width, height
            if length < 2:
                return None
            index += 2 + length
        return None
    return None


# --------------------------------------------------------------------------
# svg sanitising

ALLOWED_ELEMENTS = frozenset((
    "svg", "g", "defs", "title", "desc", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text",
    "tspan", "linearGradient", "radialGradient", "stop", "clipPath", "mask", "pattern", "symbol", "use", "marker",
))
#: Seen anywhere in the tree (even inside a dropped subtree) these refuse the whole block.
REFUSED_ELEMENTS = frozenset((
    "script", "foreignobject", "image", "a", "animate", "animatemotion", "animatetransform", "animatecolor", "set",
    "iframe", "embed", "object", "feimage", "audio", "video", "canvas", "handler", "listener", "html", "body",
))
GEOMETRY_ATTRS = frozenset((
    "id", "class", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "fx", "fy", "fr", "width", "height",
    "d", "points", "viewBox", "preserveAspectRatio", "transform", "dx", "dy", "rotate", "textLength", "lengthAdjust",
    "pathLength", "offset", "gradientUnits", "gradientTransform", "spreadMethod", "clipPathUnits", "maskUnits",
    "maskContentUnits", "patternUnits", "patternContentUnits", "patternTransform", "markerWidth", "markerHeight",
    "markerUnits", "refX", "refY", "orient", "version",
))
PRESENTATION_ATTRS = frozenset((
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity", "stroke-linecap",
    "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset", "stroke-miterlimit", "opacity", "color",
    "font-family", "font-size", "font-weight", "font-style", "font-variant", "text-anchor", "dominant-baseline",
    "alignment-baseline", "baseline-shift", "letter-spacing", "word-spacing", "text-decoration", "writing-mode",
    "stop-color", "stop-opacity", "clip-path", "clip-rule", "mask", "marker-start", "marker-mid", "marker-end",
    "visibility", "display", "paint-order", "vector-effect", "shape-rendering", "text-rendering", "mix-blend-mode",
    "isolation",
))
HREF_ELEMENTS = frozenset(("use", "linearGradient", "radialGradient", "pattern"))
TEXT_ELEMENTS = frozenset(("text", "tspan", "title", "desc"))
_LOCAL_REF = re.compile(r"^#[A-Za-z_][A-Za-z0-9_.:-]*\Z")
_URL_RE = re.compile(r"url\s*\(([^)]*)\)", re.IGNORECASE)
_BAD_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _local(tag: str) -> Tuple[Optional[str], str]:
    if tag.startswith("{"):
        namespace, _, name = tag[1:].partition("}")
        return namespace, name
    return None, tag


def _check_value(value: str) -> None:
    """Refuse script URLs, CSS escapes and any ``url()`` that is not a local ``#id``."""
    compact = re.sub(r"\s+", "", value).lower()
    if "javascript:" in compact or "vbscript:" in compact:
        raise _svg_refused("script_url", "a script URL")
    if "expression(" in compact or "@import" in compact or "behavior:" in compact:
        raise _svg_refused("css_url", "a CSS expression or import")
    if "\\" in value:
        raise _svg_refused("css_escape", "a CSS escape in an attribute")
    for match in _URL_RE.finditer(value):
        inner = match.group(1).strip().strip("'\"").strip()
        if not _LOCAL_REF.match(inner):
            raise _svg_refused("css_url", "url() may only point at #ids inside the block")
    if re.search(r"url\s*\(", value, re.IGNORECASE) and not _URL_RE.search(value):
        raise _svg_refused("css_url", "an unclosed url()")


def _clean_style(value: str) -> str:
    kept = []
    for declaration in value.split(";"):
        if ":" not in declaration:
            continue
        prop, _, raw = declaration.partition(":")
        prop = prop.strip().lower()
        raw = raw.strip()
        _check_value(raw)
        if prop in PRESENTATION_ATTRS and raw:
            kept.append("{}:{}".format(prop, raw))
    return ";".join(kept)


def _attr_escape(value: str) -> str:
    value = _BAD_XML_CHARS.sub("", value)
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _text_escape(value: str) -> str:
    value = _BAD_XML_CHARS.sub("", value)
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


#: resvg shapes a line with each fallback font in turn and gives up on the line when two fonts
#: disagree on its glyph count, which emoji joiners, selectors, keycaps and skin tones make them
#: do: one "☀️" cost every other emoji on its line. Dropping them keeps the base emoji.
_EMOJI_JOINERS = re.compile("[‍⃣︎️\U0001F3FB-\U0001F3FF]")
#: A flag is a pair of regional indicators that only an emoji font joins; drawn as its letters.
_REGIONAL = re.compile("[\U0001F1E6-\U0001F1FF]")


def render_text(value: str) -> str:
    """``value`` as the renderer draws it: emoji modifiers dropped, flags as their two letters."""
    value = _EMOJI_JOINERS.sub("", value)
    return _REGIONAL.sub(lambda m: chr(ord(m.group(0)) - 0x1F1E6 + ord("A")), value)


def _label(value: str) -> str:
    """Text the renderer draws (never an agent's svg markup): escaped, emoji made drawable."""
    return _text_escape(render_text(value))


def _parse_svg(markup: str) -> ET.Element:
    if not isinstance(markup, str) or not markup.strip():
        raise _svg_refused("empty", "no markup")
    if len(markup.encode("utf-8")) > MAX_SVG_BYTES:
        raise _svg_refused("too_large", "more than {} KB".format(MAX_SVG_BYTES // 1024))
    lowered = markup.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise _svg_refused("doctype", "a DOCTYPE or ENTITY declaration")
    if "<?xml-stylesheet" in lowered:
        raise _svg_refused("stylesheet", "an external stylesheet")
    try:
        root = ET.fromstring(markup.strip().encode("utf-8"))
    except (ET.ParseError, ValueError) as err:
        raise _svg_refused("not_xml", "not well-formed XML ({})".format(err))
    namespace, name = _local(root.tag)
    if name != "svg" or namespace not in (None, SVG_NS):
        raise _svg_refused("not_svg", "the root element must be <svg>")
    count = 0
    for node in root.iter():
        count += 1
        if count > MAX_SVG_NODES:
            raise _svg_refused("too_many_nodes", "more than {} elements".format(MAX_SVG_NODES))
        _ns, local = _local(node.tag)
        if local.lower() in REFUSED_ELEMENTS:
            raise _svg_refused("element:" + local, "<{}> is not allowed".format(local))
    _check_depth(root)
    _check_expansion(root)
    return root


def _check_depth(root: ET.Element) -> None:
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_SVG_DEPTH:
            raise _svg_refused("too_deep", "elements nest more than {} levels deep".format(MAX_SVG_DEPTH))
        stack.extend((child, depth + 1) for child in node)


def _href_target(node: ET.Element, ids: Dict[str, ET.Element]) -> Optional[ET.Element]:
    """The element an href-bearing node (``use``, a gradient, a pattern) draws from, when it is local."""
    if _local(node.tag)[1] not in HREF_ELEMENTS:
        return None
    for key, value in node.attrib.items():
        namespace, attr = _local(key)
        if attr == "href" and namespace in (None, XLINK_NS):
            ref = str(value).strip()
            return ids.get(ref[1:]) if ref.startswith("#") else None
    return None


def _check_expansion(root: ET.Element) -> None:
    """Refuse ``<use>`` chains that loop, or that expand past ``MAX_SVG_EXPANDED_NODES`` drawn nodes.

    Each node's drawn size is itself plus its children plus, for an href, the
    element it draws from; memoised, so shared definitions cost once here
    even when a renderer draws them many times. Iterative, so a long chain
    cannot exhaust the interpreter's stack.
    """
    ids: Dict[str, ET.Element] = {}
    for node in root.iter():
        ident = node.get("id")
        if ident and ident not in ids:
            ids[ident] = node
    size: Dict[int, int] = {}
    on_path = set()
    stack: List[Tuple[ET.Element, bool]] = [(root, False)]
    while stack:
        node, done = stack.pop()
        key = id(node)
        target = _href_target(node, ids)
        if done:
            on_path.discard(key)
            total = 1 + sum(size[id(child)] for child in node) + (size[id(target)] if target is not None else 0)
            size[key] = min(total, MAX_SVG_EXPANDED_NODES + 1)
            continue
        if key in size:
            continue
        on_path.add(key)
        stack.append((node, True))
        for dep in list(node) + ([target] if target is not None else []):
            if id(dep) in on_path:
                raise _svg_refused("use_cycle", "an href that refers back to itself")
            if id(dep) not in size:
                stack.append((dep, False))
    if size[id(root)] > MAX_SVG_EXPANDED_NODES:
        raise _svg_refused("use_expansion", "more than {} elements once every <use> is expanded".format(MAX_SVG_EXPANDED_NODES))


def _clean_attrs(node: ET.Element, name: str) -> List[Tuple[str, str]]:
    attrs: List[Tuple[str, str]] = []
    for key, value in node.attrib.items():
        namespace, attr = _local(key)
        if attr.lower().startswith("on"):
            raise _svg_refused("event_handler", "event handler {}".format(attr))
        value = str(value)
        if len(value) > MAX_ATTR_CHARS:
            raise _svg_refused("too_large", "attribute {} is too long".format(attr))
        if attr == "href" and namespace in (None, XLINK_NS):
            target = value.strip()
            if not _LOCAL_REF.match(target):
                raise _svg_refused("external_href", "href may only point at #ids inside the block")
            if name in HREF_ELEMENTS:
                attrs.append(("href", target))
            continue
        if namespace is not None:
            continue  # xml:space, inkscape:*, sodipodi:* ... dropped
        _check_value(value)
        if attr == "style":
            style = _clean_style(value)
            if style:
                attrs.append(("style", style))
        elif attr in GEOMETRY_ATTRS or attr in PRESENTATION_ATTRS:
            attrs.append((attr, value))
    return attrs


def _serialise(node: ET.Element, out: List[str], root: bool = False) -> None:
    namespace, name = _local(node.tag)
    if namespace not in (None, SVG_NS) or name not in ALLOWED_ELEMENTS:
        return  # rebuilt from the allow-list: anything else is dropped with its subtree
    attrs = _clean_attrs(node, name)
    if root:
        attrs = [("xmlns", SVG_NS)] + [(k, v) for k, v in attrs if k != "xmlns"]
    out.append("<" + name + "".join(' {}="{}"'.format(k, _attr_escape(v)) for k, v in attrs))
    children = list(node)
    text = node.text if name in TEXT_ELEMENTS and node.text else ""
    if not children and not text:
        out.append("/>")
        return
    out.append(">")
    if text:
        out.append(_text_escape(text))
    for child in children:
        _serialise(child, out)
        if name in TEXT_ELEMENTS and child.tail:
            out.append(_text_escape(child.tail))
    out.append("</" + name + ">")


def sanitize_svg(markup: str) -> str:
    """Agent SVG rebuilt from the allow-list; ``svg_refused`` (with ``details.reason``) on anything else."""
    root = _parse_svg(markup)
    out: List[str] = []
    _serialise(root, out, root=True)
    return "".join(out)


def _number(value: Any) -> Optional[float]:
    try:
        number = float(str(value).strip().rstrip("px").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def svg_size(markup: str) -> Optional[Tuple[float, float]]:
    """The natural size of sanitised SVG: its viewBox, else numeric width/height, else None."""
    try:
        root = ET.fromstring(markup.encode("utf-8"))
    except (ET.ParseError, ValueError):
        return None
    box = root.get("viewBox")
    if box:
        parts = re.split(r"[\s,]+", box.strip())
        if len(parts) == 4:
            w, h = _number(parts[2]), _number(parts[3])
            if w and h:
                return w, h
    w, h = _number(root.get("width")), _number(root.get("height"))
    if w and h:
        return w, h
    return None


# --------------------------------------------------------------------------
# path data

_PATH_CHARS = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE.,+\-\s]*\Z")
_NUM_RE = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_PATH_ARGS = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}


def _skip_separators(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n,":
        pos += 1
    return pos


def parse_path(d: str) -> List[Tuple[str, List[float]]]:
    """``[(command, args)]`` of SVG path data; ``op_invalid`` on anything but commands and numbers."""
    if not isinstance(d, str) or not d.strip():
        raise _path_invalid("empty")
    if not _PATH_CHARS.match(d):
        raise _path_invalid("only path commands (MLHVCSQTAZ) and numbers are allowed")
    segments: List[Tuple[str, List[float]]] = []
    command: Optional[str] = None
    pos = 0
    size = len(d)
    while True:
        pos = _skip_separators(d, pos)
        if pos >= size:
            break
        ch = d[pos]
        if ch.isalpha():
            if ch in "eE":
                raise _path_invalid("unexpected exponent")
            command = ch
            pos += 1
            if command in "Zz":
                segments.append((command, []))
                command = None
                continue
        elif command is None:
            raise _path_invalid("numbers before a command")
        count = _PATH_ARGS[command.upper()]
        args: List[float] = []
        for index in range(count):
            pos = _skip_separators(d, pos)
            if command in "Aa" and index in (3, 4):
                if pos < size and d[pos] in "01":
                    args.append(float(d[pos]))
                    pos += 1
                    continue
                raise _path_invalid("arc flags must be 0 or 1")
            match = _NUM_RE.match(d, pos)
            if match is None:
                raise _path_invalid("{} needs {} numbers".format(command, count))
            value = float(match.group(0))
            if not math.isfinite(value):
                raise _path_invalid("a number is not finite")
            args.append(value)
            pos = match.end()
        segments.append((command, args))
        if command == "M":
            command = "L"
        elif command == "m":
            command = "l"
    if not segments or segments[0][0] not in "Mm":
        raise _path_invalid("must start with M")
    return segments


def _fmt(value: float) -> str:
    text = "{:.3f}".format(value).rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def sanitize_path_data(d: str) -> str:
    """SVG path data with only path commands and numbers; ``op_invalid`` otherwise."""
    return " ".join(cmd + " ".join(_fmt(a) for a in args) for cmd, args in parse_path(d))


def _vector_angle(ux: float, uy: float, vx: float, vy: float) -> float:
    return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)


def arc_points(x1: float, y1: float, rx: float, ry: float, rotation: float, large: float, sweep: float, x2: float, y2: float,
               steps: int = 16) -> List[Tuple[float, float]]:
    """Points along an SVG elliptical arc (endpoint parameterisation, SVG 1.1 F.6.5)."""
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        return [(x2, y2)]
    phi = math.radians(rotation)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cos_phi * dx2 + sin_phi * dy2
    y1p = -sin_phi * dx2 + cos_phi * dy2
    rx, ry = abs(rx), abs(ry)
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        rx *= math.sqrt(lam)
        ry *= math.sqrt(lam)
    numerator = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    denominator = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, numerator / denominator)) if denominator else 0.0
    if bool(large) == bool(sweep):
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2.0
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2.0
    ux, uy = (x1p - cxp) / rx, (y1p - cyp) / ry
    vx, vy = (-x1p - cxp) / rx, (-y1p - cyp) / ry
    theta = _vector_angle(1.0, 0.0, ux, uy)
    delta = _vector_angle(ux, uy, vx, vy)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi
    out = []
    for k in range(1, steps + 1):
        t = theta + delta * k / steps
        out.append((cx + rx * math.cos(t) * cos_phi - ry * math.sin(t) * sin_phi,
                    cy + rx * math.cos(t) * sin_phi + ry * math.sin(t) * cos_phi))
    return out


def absolute_path(segments: List[Tuple[str, List[float]]]) -> Tuple[List[Tuple[str, List[float]]], List[Tuple[float, float]]]:
    """The segments with absolute coordinates, and every point that bounds the drawing (ends and control points)."""
    out: List[Tuple[str, List[float]]] = []
    points: List[Tuple[float, float]] = []
    cx = cy = sx = sy = 0.0
    last_cubic: Optional[Tuple[float, float]] = None
    last_quad: Optional[Tuple[float, float]] = None
    for command, args in segments:
        rel = command.islower()
        upper = command.upper()
        ox, oy = (cx, cy) if rel else (0.0, 0.0)
        cubic: Optional[Tuple[float, float]] = None
        quad: Optional[Tuple[float, float]] = None
        if upper == "M":
            cx, cy = args[0] + ox, args[1] + oy
            sx, sy = cx, cy
            out.append(("M", [cx, cy]))
            points.append((cx, cy))
        elif upper == "L":
            cx, cy = args[0] + ox, args[1] + oy
            out.append(("L", [cx, cy]))
            points.append((cx, cy))
        elif upper == "H":
            cx = args[0] + (cx if rel else 0.0)
            out.append(("H", [cx]))
            points.append((cx, cy))
        elif upper == "V":
            cy = args[0] + (cy if rel else 0.0)
            out.append(("V", [cy]))
            points.append((cx, cy))
        elif upper == "C":
            c1 = (args[0] + ox, args[1] + oy)
            c2 = (args[2] + ox, args[3] + oy)
            cx, cy = args[4] + ox, args[5] + oy
            out.append(("C", [c1[0], c1[1], c2[0], c2[1], cx, cy]))
            points += [c1, c2, (cx, cy)]
            cubic = c2
        elif upper == "S":
            c1 = (2 * cx - last_cubic[0], 2 * cy - last_cubic[1]) if last_cubic else (cx, cy)
            c2 = (args[0] + ox, args[1] + oy)
            cx, cy = args[2] + ox, args[3] + oy
            out.append(("S", [c2[0], c2[1], cx, cy]))
            points += [c1, c2, (cx, cy)]
            cubic = c2
        elif upper == "Q":
            c1 = (args[0] + ox, args[1] + oy)
            cx, cy = args[2] + ox, args[3] + oy
            out.append(("Q", [c1[0], c1[1], cx, cy]))
            points += [c1, (cx, cy)]
            quad = c1
        elif upper == "T":
            c1 = (2 * cx - last_quad[0], 2 * cy - last_quad[1]) if last_quad else (cx, cy)
            cx, cy = args[0] + ox, args[1] + oy
            out.append(("T", [cx, cy]))
            points += [c1, (cx, cy)]
            quad = c1
        elif upper == "A":
            x2, y2 = args[5] + ox, args[6] + oy
            points += arc_points(cx, cy, args[0], args[1], args[2], args[3], args[4], x2, y2)
            cx, cy = x2, y2
            out.append(("A", [args[0], args[1], args[2], args[3], args[4], cx, cy]))
        else:  # Z
            cx, cy = sx, sy
            out.append(("Z", []))
        last_cubic = cubic
        last_quad = quad
    return out, points


def normalize_path(d: str) -> Tuple[str, float, float]:
    """``(d, w, h)``: absolute path data moved so its bounds start at (0, 0), and its natural size."""
    segments, points = absolute_path(parse_path(d))
    min_x = min(p[0] for p in points)
    min_y = min(p[1] for p in points)
    max_x = max(p[0] for p in points)
    max_y = max(p[1] for p in points)
    parts = []
    for command, args in segments:
        if command == "H":
            moved = [args[0] - min_x]
        elif command == "V":
            moved = [args[0] - min_y]
        elif command == "A":
            moved = args[:5] + [args[5] - min_x, args[6] - min_y]
        else:
            moved = [value - (min_x if i % 2 == 0 else min_y) for i, value in enumerate(args)]
        parts.append(command + " ".join(_fmt(v) for v in moved))
    return " ".join(parts), max(1.0, max_x - min_x), max(1.0, max_y - min_y)


# --------------------------------------------------------------------------
# freehand strokes


def _densify(pts: List[Tuple[float, float, Optional[float]]], step: float) -> List[Tuple[float, float, Optional[float]]]:
    """A Catmull-Rom spline through sparse points (agents draw with a few grid cells), about ``step`` units apart."""
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for index in range(len(pts) - 1):
        p0 = pts[max(0, index - 1)]
        p1, p2 = pts[index], pts[index + 1]
        p3 = pts[min(len(pts) - 1, index + 2)]
        length = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        count = min(24, max(1, int(math.ceil(length / max(step, 1.0)))))
        for k in range(1, count + 1):
            t = k / float(count)
            t2, t3 = t * t, t * t * t
            x = 0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            pressure = p1[2] if p1[2] is None or p2[2] is None else p1[2] + (p2[2] - p1[2]) * t
            out.append((x, y, pressure))
    return out


def freehand_outline(points: Sequence[Sequence[float]], width: float, smooth: bool = True) -> List[Tuple[float, float]]:
    """The filled outline polygon of a pen stroke (a Python port of perfect-freehand).

    Streamlined input points, a radius per point from pressure (real or
    simulated from speed, as the engine does for a mouse), offsets on both
    sides of the stroke direction, and round caps. ``smooth=False`` keeps the
    points as given and a constant width.
    """
    pts = []
    for raw in points:
        try:
            x, y = float(raw[0]), float(raw[1])
            pressure = float(raw[2]) if len(raw) > 2 and raw[2] is not None else None
        except (TypeError, ValueError, IndexError):
            continue
        if pts and pts[-1][0] == x and pts[-1][1] == y:
            continue
        pts.append((x, y, pressure))
    if not pts:
        return []
    size = max(1.0, float(width) * FREEHAND_SIZE)
    if len(pts) == 1:
        x, y, _p = pts[0]
        r = size / 2.0
        return [(round(x + r * math.cos(a), 2), round(y + r * math.sin(a), 2))
                for a in (2 * math.pi * k / 16 for k in range(16))]
    streamline = FREEHAND_STREAMLINE if smooth else 0.0
    thinning = FREEHAND_THINNING if smooth else 0.0
    if smooth:
        pts = _densify(pts, size)
    line = [pts[0]]
    for x, y, p in pts[1:]:
        px, py, _pp = line[-1]
        t = 1.0 - streamline
        nx, ny = px + (x - px) * t, py + (y - py) * t
        if (nx, ny) != (px, py):
            line.append((nx, ny, p))
    if smooth and (line[-1][0], line[-1][1]) != (pts[-1][0], pts[-1][1]):
        line.append(pts[-1])
    if len(line) < 2:
        line = [pts[0], pts[-1]]
    simulate = all(p is None for _x, _y, p in pts)
    radii = []
    previous = 0.5
    for index, (x, y, p) in enumerate(line):
        if simulate:
            dist = math.hypot(x - line[index - 1][0], y - line[index - 1][1]) if index else 0.0
            speed = min(1.0, dist / size)
            target = min(1.0, 1.0 - speed)
            pressure = min(1.0, previous + (target - previous) * (speed * 0.275))
        else:
            pressure = p if p is not None else 0.5
        pressure = max(0.0, min(1.0, pressure))
        previous = pressure
        radii.append(max(0.25, size / 2.0 * (1.0 - thinning * (1.0 - pressure))))
    left: List[Tuple[float, float]] = []
    right: List[Tuple[float, float]] = []
    normals: List[Tuple[float, float]] = []
    count = len(line)
    for index, (x, y, _p) in enumerate(line):
        ax, ay, _a = line[max(0, index - 1)]
        bx, by, _b = line[min(count - 1, index + 1)]
        vx, vy = bx - ax, by - ay
        length = math.hypot(vx, vy) or 1.0
        nx, ny = -vy / length, vx / length
        normals.append((nx, ny))
        r = radii[index]
        left.append((x + nx * r, y + ny * r))
        right.append((x - nx * r, y - ny * r))

    def cap(cx: float, cy: float, r: float, start_angle: float) -> List[Tuple[float, float]]:
        return [(cx + r * math.cos(start_angle - math.pi * k / CAP_STEPS), cy + r * math.sin(start_angle - math.pi * k / CAP_STEPS))
                for k in range(1, CAP_STEPS)]

    end_x, end_y, _e = line[-1]
    start_x, start_y, _s = line[0]
    end_cap = cap(end_x, end_y, radii[-1], math.atan2(normals[-1][1], normals[-1][0]))
    start_cap = cap(start_x, start_y, radii[0], math.atan2(normals[0][1], normals[0][0]) + math.pi)
    polygon = left + end_cap + list(reversed(right)) + start_cap
    return [(round(x, 2), round(y, 2)) for x, y in polygon]


# --------------------------------------------------------------------------
# svg rendering

_HEX = re.compile(r"^#[0-9a-f]{6}\Z")


def _color(value: Any, default: Optional[str]) -> Optional[str]:
    return value if isinstance(value, str) and _HEX.match(value) else default


def _n(value: float) -> str:
    return _fmt(float(value))


def _bounds(el: Dict[str, Any]) -> Tuple[float, float, float, float]:
    x, y = float(el.get("x") or 0), float(el.get("y") or 0)
    return x, y, x + max(1.0, float(el.get("w") or 1)), y + max(1.0, float(el.get("h") or 1))


def _intersects(a: Sequence[float], b: Sequence[float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def view_box(scene: Dict[str, Any], region: Optional[Sequence[float]] = None,
             max_px: int = DEFAULT_MAX_PX) -> Tuple[float, float, float, float]:
    """The region, or everything on the canvas plus ``PADDING``, as ``(x0, y0, x1, y1)``.

    Everything is what the picture draws, not only element boxes: an arrow label's pill (wider than a
    short arrow) and a frame's title, which stands above the frame when the board is zoomed out, so
    they are never cut at the picture's edge (QA F-10). A title grows with the units per pixel, which
    grow with the box, so the box is settled over a few rounds at ``max_px``.
    """
    if region is not None:
        x0, y0, x1, y1 = (float(v) for v in region)
        return min(x0, x1), min(y0, y1), max(x0, x1, min(x0, x1) + 1), max(y0, y1, min(y0, y1) + 1)
    elements = [e for e in scene.get("elements") or [] if isinstance(e, dict)]
    boxes = [_bounds(e) for e in elements]
    for item in list(scene.get("claims") or []) + list(scene.get("locks") or []):
        region_box = item.get("region") if isinstance(item, dict) else None
        if isinstance(region_box, list) and len(region_box) == 4:
            boxes.append(tuple(float(v) for v in region_box))  # type: ignore[arg-type]
    if not boxes:
        return -PADDING, -PADDING, 400.0 + PADDING, 300.0 + PADDING
    for el in elements:
        if el.get("type") == "arrow" and el.get("text"):
            pill = arrow_label_pill(el)
            if pill is not None:
                (px, py, pw, ph), _size, _lines = pill
                boxes.append((px, py, px + pw, py + ph))

    def padded(found: Sequence[Sequence[float]]) -> Tuple[float, float, float, float]:
        return (min(b[0] for b in found) - PADDING, min(b[1] for b in found) - PADDING,
                max(b[2] for b in found) + PADDING, max(b[3] for b in found) + PADDING)

    box = padded(boxes)
    frames = [e for e in elements if e.get("type") == "frame" and e.get("text")]
    for _round in range(4):
        if not frames:
            break
        u = max(box[2] - box[0], box[3] - box[1], 1.0) / float(max_px)
        titles = [t for t in (frame_title_box(el, u) for el in frames) if t is not None]
        grown = padded(boxes + titles)
        if grown == box:
            break
        box = grown
    return box


def pixel_size(box: Sequence[float], max_px: int = DEFAULT_MAX_PX) -> Tuple[int, int]:
    """The PNG size for a view box: the longer side scaled to ``max_px``."""
    width, height = box[2] - box[0], box[3] - box[1]
    scale = max_px / max(width, height, 1.0)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


#: Deprecated since 0.22 (``canvas_text`` measures with real font metrics): the old estimated width of one
#: character in font sizes, kept with ``chars_per_line`` and ``wrap_text`` for callers outside the canvas.
CHAR_W = 0.55


def chars_per_line(width: float, size: float) -> int:
    """Characters that fit on one line ``width`` wide at font ``size``.

    Rounded before truncating: 77 / (0.55 x 28) is 4.999999999999999 in floating point, which
    split a five-letter word that the model had sized for one line.
    """
    return max(1, int(round(float(width) / (CHAR_W * float(size or 20)), 6)))


def wrap_text(text: str, max_chars: int) -> List[str]:
    """Word-wrap each line of ``text`` at ``max_chars`` characters (deprecated: ``canvas_text.wrap`` measures real widths)."""
    max_chars = max(1, int(max_chars))
    out: List[str] = []
    for raw in (text or "").split("\n"):
        words = raw.split(" ")
        line = ""
        for word in words:
            while len(word) > max_chars:
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:max_chars])
                word = word[max_chars:]
            candidate = word if not line else line + " " + word
            if len(candidate) <= max_chars:
                line = candidate
            else:
                out.append(line)
                line = word
        out.append(line)
    return out


def _font(style: Dict[str, Any]) -> str:
    """The SVG font family: Geist Mono for ``code``, Inter for the rest (``hand`` has no bundled face and draws in Inter)."""
    return MONO_FAMILY if style.get("font") == "code" else SANS_FAMILY


def _font_key(style: Dict[str, Any]) -> str:
    return str(style.get("font") or "normal")


def _stroke_attrs(style: Dict[str, Any], fill: Optional[str], default_stroke: str = TEXT_COLOR) -> str:
    stroke = _color(style.get("stroke"), default_stroke)
    width = float(style.get("width") or 2)
    parts = ['stroke="{}"'.format(stroke), 'stroke-width="{}"'.format(_n(width)), 'fill="{}"'.format(fill or "none"),
             'stroke-linejoin="round"', 'stroke-linecap="round"']
    dash = style.get("dash")
    if dash == "dashed":
        parts.append('stroke-dasharray="{} {}"'.format(_n(width * 4), _n(width * 3)))
    elif dash == "dotted":
        parts.append('stroke-dasharray="{} {}"'.format(_n(width * 0.5), _n(width * 3)))
    opacity = style.get("opacity")
    if isinstance(opacity, (int, float)) and opacity < 100:
        parts.append('opacity="{}"'.format(_n(max(0.0, opacity) / 100.0)))
    return " ".join(parts)


def _lines_svg(lines: Sequence[str], anchor_x: float, top: float, size: float, style: Dict[str, Any], fill: str, anchor: str,
               weight: int = _ctext.DEFAULT_WEIGHT) -> str:
    """A group carrying the font, with one ``<text>`` per line at its own baseline; the first where the browser would put it.

    One ``<text>`` per line, not a ``<tspan>`` each: resvg runs the bidi algorithm over a whole
    ``<text>``, so the words of right-to-left lines drew over each other (QA F-4, 2026-09-27).
    ``xml:space="preserve"`` keeps indentation and runs of spaces, and a blank line is an empty
    ``<text>`` at its own baseline, so the lines after it stay where the page draws them (QA F-8).
    """
    line_height = _ctext.line_height(size)
    first = top + _ctext.baseline(size, _font_key(style), weight)
    out = ['<g font-size="{}" font-family="{}" font-weight="{}" fill="{}" text-anchor="{}" xml:space="preserve">'.format(
        _n(size), _font(style), weight, fill, anchor)]
    for index, line in enumerate(lines):
        out.append('<text x="{}" y="{}">{}</text>'.format(_n(anchor_x), _n(first + index * line_height), _label(line)))
    out.append("</g>")
    return "".join(out)


def _text_block(text: str, x: float, y: float, w: float, h: float, style: Dict[str, Any], align: str = "middle",
                color: Optional[str] = None, lines: Optional[Sequence[str]] = None) -> str:
    """``text`` wrapped to the box (or the given ``lines``): centred in it, or from its top-left with ``align="start"``."""
    size = float(style.get("size") or 20)
    font = _font_key(style)
    if lines is None:
        lines = _ctext.wrap(text, max(1.0, w - (16 if align == "middle" else 0)), font, size)
    fill = color or _color(style.get("text"), None) or _color(style.get("stroke"), TEXT_COLOR) or TEXT_COLOR
    if align == "middle":
        top = y + (h - _ctext.line_height(size) * len(lines)) / 2.0
        return _lines_svg(lines, x + w / 2.0, top, size, style, fill, "middle")
    return _lines_svg(lines, x, y, size, style, fill, "start")


def _same_words(lines: Sequence[str], text: str) -> bool:
    """Whether ``lines`` hold exactly ``text``'s characters (whitespace aside): stored lines still draw the label.

    Whitespace is removed, not collapsed: a long token breaks after ``/`` or mid-word with no space
    at the break, and comparing the lines joined with spaces threw such lines away (QA F-9).
    """
    return "".join("".join(lines).split()) == "".join(text.split())


def _label_svg(el: Dict[str, Any], color: Optional[str]) -> str:
    """A labelled element's text: the lines its fit stored (else its kind's wrap at its size), in its inner box."""
    style = el.get("style") if isinstance(el.get("style"), dict) else {}
    text = str(el.get("text") or "")
    x0, y0, x1, y1 = _bounds(el)
    result = _kinds.drawn(el)
    if result is None:
        return _text_block(text, x0, y0, x1 - x0, y1 - y0, style, color=color)
    fit = el.get("fit") if isinstance(el.get("fit"), dict) else {}
    stored = fit.get("lines")
    lines: Sequence[str] = result.lines
    if isinstance(stored, list) and all(isinstance(line, str) for line in stored) and (fit.get("truncated") or _same_words(stored, text)):
        lines = stored
    drawn_style = dict(style, size=result.size)
    ix, iy, iw, ih = result.inner
    fill = color or _color(style.get("text"), None) or _color(style.get("stroke"), TEXT_COLOR) or TEXT_COLOR
    if el.get("type") == "text":
        return _lines_svg(lines, x0, y0, result.size, drawn_style, fill, "start")
    top = y0 + iy + (ih - _ctext.line_height(result.size) * len(lines)) / 2.0
    return _lines_svg(lines, x0 + ix + iw / 2.0, top, result.size, drawn_style, fill, "middle")


def _shape(el: Dict[str, Any]) -> str:
    kind = el.get("type")
    style = el.get("style") if isinstance(el.get("style"), dict) else {}
    x0, y0, x1, y1 = _bounds(el)
    w, h = x1 - x0, y1 - y0
    text = str(el.get("text") or "")
    if kind == "text":
        return _label_svg(el, None)
    fill = _color(style.get("fill"), None)
    if kind == "note":
        fill = fill or NOTE_FILL
    attrs = _stroke_attrs(style, fill)
    if kind == "ellipse":
        body = '<ellipse cx="{}" cy="{}" rx="{}" ry="{}" {}/>'.format(_n(x0 + w / 2), _n(y0 + h / 2), _n(w / 2), _n(h / 2), attrs)
    elif kind == "diamond":
        cx, cy = x0 + w / 2, y0 + h / 2
        body = '<polygon points="{},{} {},{} {},{} {},{}" {}/>'.format(_n(cx), _n(y0), _n(x1), _n(cy), _n(cx), _n(y1), _n(x0), _n(cy), attrs)
    else:
        radius = 4 if kind == "note" else 8
        body = '<rect x="{}" y="{}" width="{}" height="{}" rx="{}" {}/>'.format(_n(x0), _n(y0), _n(w), _n(h), radius, attrs)
    if text:
        # A note stored before 0.22 has no label colour: it was drawn in ink on its paper.
        color = TEXT_COLOR if kind == "note" and not _color(style.get("text"), None) else None
        body += _label_svg(el, color)
    return body


#: A frame's title band (``canvas.FRAME_TOP``) and where its title sits in it (design spec 6.2).
FRAME_BAND = 40
FRAME_TITLE_AT = (20, 8)


def _fit_line(text: str, width: float, size: float, weight: int) -> str:
    """``text`` on one line no wider than ``width``, ending with … when cut."""
    if _ctext.measure(text, size=size, weight=weight).width <= width:
        return text
    while text and _ctext.measure(text + "…", size=size, weight=weight).width > width:
        text = text[:-1]
    return text.rstrip() + "…" if text else ""


FRAME_TITLE_WEIGHT = 600


def _frame_title(el: Dict[str, Any], u: float) -> Optional[Tuple[str, float, float, float]]:
    """A frame's title as drawn at ``u`` canvas units per pixel: ``(line, x, top, size)``, or None without one."""
    title = str(el.get("text") or "")
    if not title:
        return None
    x0, y0, x1, _y1 = _bounds(el)
    size = max(12.0 * u, 16.0)
    if size <= FRAME_BAND - 2 * FRAME_TITLE_AT[1]:
        # Inside the band the frame keeps free for it, cut to the frame's width.
        line = _fit_line(title[:120], (x1 - x0) - 2 * FRAME_TITLE_AT[0], size, FRAME_TITLE_WEIGHT)
        return line, x0 + FRAME_TITLE_AT[0], y0 + FRAME_TITLE_AT[1], size
    # Zoomed far out the band is too small to read: above the frame, as large as it needs.
    return title[:120], x0, y0 - _ctext.line_height(size), size


def frame_title_box(el: Dict[str, Any], u: float) -> Optional[Tuple[float, float, float, float]]:
    """Where a frame's title is drawn at ``u`` units per pixel, ``(x0, y0, x1, y1)``; it may stand above the frame."""
    title = _frame_title(el, u)
    if title is None:
        return None
    line, x, top, size = title
    return x, top, x + _ctext.measure(line, size=size, weight=FRAME_TITLE_WEIGHT).width, top + _ctext.line_height(size)


def _frame(el: Dict[str, Any], u: float) -> str:
    style = el.get("style") if isinstance(el.get("style"), dict) else {}
    x0, y0, x1, y1 = _bounds(el)
    stroke = _color(style.get("stroke"), MUTED)
    out = '<rect x="{}" y="{}" width="{}" height="{}" rx="{}" fill="{}" stroke="{}" stroke-width="{}"/>'.format(
        _n(x0), _n(y0), _n(x1 - x0), _n(y1 - y0), 16 if style.get("tone") else 6, _color(style.get("fill"), "none"), stroke,
        _n(max(1.0, 1.5 * u)))
    title = _frame_title(el, u)
    if title is not None:
        line, x, top, size = title
        color = _color(style.get("text"), None) or MUTED
        out += _lines_svg([line], x, top, size, {}, color, "start", FRAME_TITLE_WEIGHT)
    return out


def _head(kind: str, tip: Tuple[float, float], back: Tuple[float, float], width: float, color: str) -> str:
    dx, dy = tip[0] - back[0], tip[1] - back[1]
    length = math.hypot(dx, dy)
    if kind == "none" or length == 0:
        return ""
    ux, uy = dx / length, dy / length
    if kind == "dot":
        return '<circle cx="{}" cy="{}" r="{}" fill="{}"/>'.format(_n(tip[0]), _n(tip[1]), _n(3 + width), color)
    size = 10.0 + 2.0 * width
    bx, by = tip[0] - ux * size, tip[1] - uy * size
    nx, ny = -uy * size * 0.5, ux * size * 0.5
    left, right = (bx + nx, by + ny), (bx - nx, by - ny)
    if kind == "triangle":
        return '<polygon points="{},{} {},{} {},{}" fill="{}"/>'.format(
            _n(left[0]), _n(left[1]), _n(tip[0]), _n(tip[1]), _n(right[0]), _n(right[1]), color)
    return '<polyline points="{},{} {},{} {},{}" fill="none" stroke="{}" stroke-width="{}" stroke-linecap="round" stroke-linejoin="round"/>'.format(
        _n(left[0]), _n(left[1]), _n(tip[0]), _n(tip[1]), _n(right[0]), _n(right[1]), color, _n(width))


def _points(raw: Any) -> List[Tuple[float, float]]:
    out = []
    for point in raw or []:
        try:
            out.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _curve_path(points: List[Tuple[float, float]]) -> str:
    if len(points) == 2:
        (ax, ay), (bx, by) = points
        mx, my = (ax + bx) / 2, (ay + by) / 2
        dx, dy = bx - ax, by - ay
        cx, cy = mx - dy * 0.2, my + dx * 0.2
        return "M{} {} Q{} {} {} {}".format(_n(ax), _n(ay), _n(cx), _n(cy), _n(bx), _n(by))
    parts = ["M{} {}".format(_n(points[0][0]), _n(points[0][1]))]
    for index in range(1, len(points) - 1):
        (cx, cy), (nx, ny) = points[index], points[index + 1]
        end = (nx, ny) if index == len(points) - 2 else ((cx + nx) / 2, (cy + ny) / 2)
        parts.append("Q{} {} {} {}".format(_n(cx), _n(cy), _n(end[0]), _n(end[1])))
    return " ".join(parts)


def arrow_midpoint(points: List[Tuple[float, float]]) -> Tuple[float, float]:
    """Halfway along the polyline."""
    if not points:
        return 0.0, 0.0
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:])]
    half = sum(lengths) / 2.0
    for (a, b), length in zip(zip(points, points[1:]), lengths):
        if half <= length and length > 0:
            t = half / length
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        half -= length
    return points[-1]


#: An arrow label wraps where the page's Excalidraw 0.18 wraps it, at the wider of 0.7 x the arrow's
#: width and 11 x its font size, so the picture and the page break it alike; it sits in a pill with this
#: padding (design spec 6.1).
ARROW_LABEL_WIDTH_FRACTION = 0.7
ARROW_LABEL_MIN_EMS = 11
ARROW_LABEL_PAD = (8, 2)


def arrow_label_width(el: Dict[str, Any], size: float) -> float:
    """The width an arrow's label wraps at."""
    return max(ARROW_LABEL_WIDTH_FRACTION * max(1.0, float(el.get("w") or 1)), ARROW_LABEL_MIN_EMS * float(size))


def _arrow(el: Dict[str, Any], u: float) -> str:
    style = el.get("style") if isinstance(el.get("style"), dict) else {}
    points = _points(el.get("points"))
    if len(points) < 2:
        return ""
    color = _color(style.get("stroke"), TEXT_COLOR) or TEXT_COLOR
    width = float(style.get("width") or 2)
    attrs = _stroke_attrs(style, None)
    if el.get("curve"):
        body = '<path d="{}" {}/>'.format(_curve_path(points), attrs)
    else:
        body = '<polyline points="{}" {}/>'.format(" ".join("{},{}".format(_n(x), _n(y)) for x, y in points), attrs)
    body += _head(str(el.get("head") or "arrow"), points[-1], points[-2], width, color)
    body += _head(str(el.get("tail") or "none"), points[0], points[1], width, color)
    pill = arrow_label_pill(el)
    if pill is not None:
        (px, py, box_w, box_h), size, lines = pill
        palette = _theme.base()
        body += '<rect x="{}" y="{}" width="{}" height="{}" rx="6" fill="{}" stroke="{}" stroke-width="1" opacity="0.95"/>'.format(
            _n(px), _n(py), _n(box_w), _n(box_h), palette["surface"], palette["grid"])
        color = _color(style.get("text"), None) or color
        body += _text_block(str(el.get("text")), px, py, box_w, box_h, dict(style, size=size), color=color, lines=lines)
    return body


def arrow_label_pill(el: Dict[str, Any]) -> Optional[Tuple[Tuple[float, float, float, float], float, List[str]]]:
    """An arrow label's pill ``(x, y, w, h)`` centred on the arrow's midpoint, its font size and lines; None without one."""
    label = str(el.get("text") or "")
    points = _points(el.get("points"))
    if not label or len(points) < 2:
        return None
    style = el.get("style") if isinstance(el.get("style"), dict) else {}
    mx, my = arrow_midpoint(points)
    size = float(style.get("size") or 20) * 0.8
    font = _font_key(style)
    lines = _ctext.wrap(label, arrow_label_width(el, size), font, size)
    widest = max(_ctext.measure(line, font, size).width for line in lines)
    box_w = widest + 2 * ARROW_LABEL_PAD[0]
    box_h = len(lines) * _ctext.line_height(size) + 2 * ARROW_LABEL_PAD[1]
    return (mx - box_w / 2, my - box_h / 2, box_w, box_h), size, lines


def _pen(el: Dict[str, Any]) -> str:
    style = el.get("style") if isinstance(el.get("style"), dict) else {}
    color = _color(style.get("stroke"), TEXT_COLOR) or TEXT_COLOR
    width = float(style.get("width") or 2)
    points = el.get("points") or []
    fill = _color(style.get("fill"), None)
    opacity = style.get("opacity")
    alpha = ' opacity="{}"'.format(_n(opacity / 100.0)) if isinstance(opacity, (int, float)) and opacity < 100 else ""
    body = ""
    flat = _points(points)
    if el.get("closed") and fill and len(flat) >= 3:
        body += '<polygon points="{}" fill="{}" stroke="none"{}/>'.format(" ".join("{},{}".format(_n(x), _n(y)) for x, y in flat), fill, alpha)
    if el.get("smooth", True):
        stroke_points = list(points) + ([points[0]] if el.get("closed") and points else [])
        outline = freehand_outline(stroke_points, width, smooth=True)
        if outline:
            body += '<polygon points="{}" fill="{}" stroke="none"{}/>'.format(" ".join("{},{}".format(_n(x), _n(y)) for x, y in outline), color, alpha)
        return body
    tag = "polygon" if el.get("closed") else "polyline"
    return body + '<{} points="{}" {}/>'.format(tag, " ".join("{},{}".format(_n(x), _n(y)) for x, y in flat), _stroke_attrs(style, None))


def _path(el: Dict[str, Any]) -> str:
    style = el.get("style") if isinstance(el.get("style"), dict) else {}
    try:
        d = sanitize_path_data(str(el.get("d") or ""))
    except HerdrTeamError:
        return ""
    scale = float(el.get("scale") or 1) or 1.0
    width = float(style.get("width") or 2)
    fill = _color(style.get("fill"), None)
    return '<path d="{}" transform="translate({} {}) scale({})" {}/>'.format(
        _attr_escape(d), _n(el.get("x") or 0), _n(el.get("y") or 0), _n(scale),
        _stroke_attrs(dict(style, width=width / scale), fill))


def _asset_bytes(team: Optional[TeamPaths], name: Any) -> Optional[bytes]:
    if team is None or not isinstance(name, str) or not re.match(r"^[0-9a-f]{32}\.(png|jpg|svg)\Z", name):
        return None
    try:
        return store.read_bytes(_features.whiteboard_dir(team) / "assets" / name)
    except (OSError, HerdrTeamError):
        return None


def _still_bytes(team: Optional[TeamPaths], el: Dict[str, Any]) -> Optional[bytes]:
    if team is None:
        return None
    name = "{}-v{}.png".format(el.get("id"), int(el.get("updated_seq") or 0))
    if not re.match(r"^E-[0-9]+-v[0-9]+\.png\Z", name):
        return None
    try:
        data = store.read_bytes(_features.whiteboard_dir(team) / "stills" / name)
    except (OSError, HerdrTeamError):
        return None
    return data if data and data.startswith(PNG_MAGIC) else None


def _image_tag(data: bytes, mime: str, x0: float, y0: float, w: float, h: float) -> str:
    return '<image x="{}" y="{}" width="{}" height="{}" preserveAspectRatio="xMidYMid meet" href="data:{};base64,{}"/>'.format(
        _n(x0), _n(y0), _n(w), _n(h), mime, base64.b64encode(data).decode("ascii"))


def _card(el: Dict[str, Any], team: Optional[TeamPaths], u: float) -> str:
    x0, y0, x1, y1 = _bounds(el)
    w, h = x1 - x0, y1 - y0
    kind = el.get("type")
    title = str(el.get("text") or kind or "")
    still = _still_bytes(team, el) if kind in ("chart", "mermaid", "viz") else None
    if still is not None:
        return _image_tag(still, "image/png", x0, y0, w, h)
    if kind == "viz":
        libs = [str(lib) for lib in el.get("libs") or []]
        subtitle = "live visual{} · runs on the page".format(" ({})".format(", ".join(libs)) if libs else "")
    elif kind == "mermaid":
        subtitle = "mermaid {} · rendered on the page".format(el.get("diagram") or "diagram")
    elif kind == "chart":
        subtitle = "chart{} · rendered on the page".format(" of {}".format(el.get("data")) if el.get("data") else "")
    elif kind == "image":
        subtitle = "image (not stored)"
    else:
        subtitle = "svg block (not stored)"
    size = max(min(h / 6.0, 22.0), 10.0)
    per_unit = max(_ctext.measure(subtitle, size=1).width, 0.01)
    small = max(4.0, min(size * 0.7, (w - 8) / per_unit))  # the subtitle shrinks to fit a narrow card
    out = '<rect x="{}" y="{}" width="{}" height="{}" rx="8" fill="{}" stroke="{}" stroke-width="{}" stroke-dasharray="{} {}"/>'.format(
        _n(x0), _n(y0), _n(w), _n(h), PLACEHOLDER_FILL, MUTED, _n(max(1.0, 1.5 * u)), _n(6 * u + 4), _n(4 * u + 3))
    out += _text_block(title, x0, y0, w, h * 0.8, {"size": size, "stroke": TEXT_COLOR})
    out += '<text x="{}" y="{}" font-size="{}" font-family="{}" fill="{}" text-anchor="middle">{}</text>'.format(
        _n(x0 + w / 2), _n(y0 + h * 0.8), _n(small), SANS_FAMILY, MUTED, _label(subtitle))
    return out


def _inline_svg(el: Dict[str, Any], team: Optional[TeamPaths], u: float) -> str:
    data = _asset_bytes(team, el.get("asset"))
    if data is None:
        return _card(el, team, u)
    try:
        root = _parse_svg(data.decode("utf-8", "replace"))
    except HerdrTeamError:
        return _card(el, team, u)
    x0, y0, x1, y1 = _bounds(el)
    if not root.get("viewBox"):
        size = svg_size(data.decode("utf-8", "replace"))
        if size:
            root.set("viewBox", "0 0 {} {}".format(_n(size[0]), _n(size[1])))
    for key in ("x", "y", "width", "height", "preserveAspectRatio"):
        root.attrib.pop(key, None)
    out: List[str] = []
    _serialise(root, out, root=True)
    markup = "".join(out)
    placement = '<svg x="{}" y="{}" width="{}" height="{}" preserveAspectRatio="xMidYMid meet"'.format(_n(x0), _n(y0), _n(x1 - x0), _n(y1 - y0))
    return markup.replace('<svg xmlns="{}"'.format(SVG_NS), placement, 1)


def _image(el: Dict[str, Any], team: Optional[TeamPaths], u: float) -> str:
    data = _asset_bytes(team, el.get("asset"))
    sniffed = sniff_image(data) if data else None
    if data is None or sniffed is None:
        return _card(el, team, u)
    x0, y0, x1, y1 = _bounds(el)
    return _image_tag(data, sniffed[0], x0, y0, x1 - x0, y1 - y0)


def _comment(el: Dict[str, Any], u: float, colors: Dict[str, str]) -> str:
    point = el.get("point") if isinstance(el.get("point"), list) else [el.get("x") or 0, el.get("y") or 0]
    x, y = float(point[0]), float(point[1])
    color = MUTED if el.get("resolved") else colors.get(str(el.get("author")), TEXT_COLOR)
    r = 10.0 * u
    number = str(el.get("id") or "C-?").split("-", 1)[-1]
    return ('<circle cx="{}" cy="{}" r="{}" fill="{}" stroke="#ffffff" stroke-width="{}"/>'
            '<text x="{}" y="{}" font-size="{}" font-family="Inter" fill="#ffffff" text-anchor="middle">{}</text>').format(
        _n(x), _n(y), _n(r), color, _n(1.5 * u), _n(x), _n(y + 4 * u), _n(11 * u), _label(number))


def _region_rect(region: Sequence[float]) -> Tuple[float, float, float, float]:
    x0, y0, x1, y1 = (float(v) for v in region)
    return x0, y0, x1 - x0, y1 - y0


def _claim(claim: Dict[str, Any], u: float, colors: Dict[str, str], reader: Optional[str]) -> str:
    region = claim.get("region")
    if not isinstance(region, list) or len(region) != 4:
        return ""
    x, y, w, h = _region_rect(region)
    author = str(claim.get("author") or "")
    color = colors.get(author, MUTED)
    who = "you" if reader and author == reader else ("the operator" if author == "human" else author)
    label = "{} {}: {}".format(claim.get("id"), who, claim.get("label") or "")
    return ('<rect x="{}" y="{}" width="{}" height="{}" fill="none" stroke="{}" stroke-width="{}" stroke-dasharray="{} {}"/>'
            '<text x="{}" y="{}" font-size="{}" font-family="Inter" fill="{}">{}</text>').format(
        _n(x), _n(y), _n(w), _n(h), color, _n(2 * u), _n(8 * u), _n(6 * u),
        _n(x + 4 * u), _n(y + 14 * u), _n(12 * u), color, _label(label[:120]))


def _lock(lock: Dict[str, Any], u: float) -> str:
    region = lock.get("region")
    if not isinstance(region, list) or len(region) != 4:
        return ""
    x, y, w, h = _region_rect(region)
    label = "{} locked: {}".format(lock.get("id"), lock.get("label") or "hands off")
    return ('<rect x="{}" y="{}" width="{}" height="{}" fill="url(#synapse-hatch)" opacity="0.5" stroke="{}" stroke-width="{}"/>'
            '<text x="{}" y="{}" font-size="{}" font-family="Inter" fill="{}">{}</text>').format(
        _n(x), _n(y), _n(w), _n(h), MUTED, _n(2 * u), _n(x + 4 * u), _n(y + h - 6 * u), _n(12 * u), TEXT_COLOR, _label(label[:120]))


def mark_anchor(el: Dict[str, Any]) -> Tuple[float, float]:
    """Where an element's id badge goes: its top-left, an arrow's midpoint, a comment's pin."""
    if el.get("type") == "arrow":
        mx, my = arrow_midpoint(_points(el.get("points")))
        # A labelled arrow keeps its label at the midpoint; the badge goes just above it.
        return (mx, my - 32) if el.get("text") else (mx, my)
    if el.get("type") == "comment":
        point = el.get("point") if isinstance(el.get("point"), list) else [el.get("x") or 0, el.get("y") or 0]
        return float(point[0]) + 12, float(point[1]) - 22
    return float(el.get("x") or 0), float(el.get("y") or 0)


def _badge(el: Dict[str, Any], u: float) -> str:
    label = str(el.get("id") or "")
    x, y = mark_anchor(el)
    size = 11.0 * u
    width = (_ctext.measure(label, size=11.0, weight=700).width + 6.0) * u
    height = 15.0 * u
    x -= 2 * u
    # Inside the corner of a shape, but above a bare text element so it never covers the first line.
    y -= (height + 2 * u) if el.get("type") == "text" else 2 * u
    return ('<rect x="{}" y="{}" width="{}" height="{}" rx="{}" fill="#1e1e1e" opacity="0.85"/>'
            '<text x="{}" y="{}" font-size="{}" font-family="Inter" font-weight="bold" fill="#ffffff">{}</text>').format(
        _n(x), _n(y), _n(width), _n(height), _n(3 * u), _n(x + 3 * u), _n(y + 11.5 * u), _n(size), _label(label))


def _grid(box: Sequence[float], u: float) -> List[str]:
    x0, y0, x1, y1 = box
    step = 100.0
    while max(x1 - x0, y1 - y0) / step > 60:
        step *= 2
    out = []
    gx = math.ceil(x0 / step) * step
    while gx <= x1:
        gy = math.ceil(y0 / step) * step
        while gy <= y1:
            out.append('<circle cx="{}" cy="{}" r="{}" fill="#adb5bd"/>'.format(_n(gx), _n(gy), _n(1.8 * u)))
            if round(gx / step) % 2 == 0 and round(gy / step) % 2 == 0:
                out.append('<text x="{}" y="{}" font-size="{}" font-family="Inter" fill="{}">c{}r{}</text>'.format(
                    _n(gx + 3 * u), _n(gy - 3 * u), _n(9 * u), MUTED, int(math.floor(gx / 20)), int(math.floor(gy / 20))))
            gy += step
        gx += step
    return out


def render_svg(scene: Dict[str, Any], region: Optional[Sequence[float]] = None, marks: bool = True, grid: bool = False,
               reader: Optional[str] = None, max_px: int = DEFAULT_MAX_PX, team: Optional[TeamPaths] = None) -> str:
    """The scene (or a region of it) as one standalone SVG document, with id marks and optional grid dots."""
    box = view_box(scene, region)
    width_px, height_px = pixel_size(box, max_px)
    u = (box[2] - box[0]) / float(width_px)  # canvas units per output pixel
    elements = [e for e in scene.get("elements") or [] if isinstance(e, dict) and _intersects(_bounds(e), box)]
    elements.sort(key=lambda e: (int(e.get("z") or 0), str(e.get("id"))))
    authors = scene.get("authors") if isinstance(scene.get("authors"), dict) else {}
    colors = {name: _color(info.get("color"), MUTED) or MUTED for name, info in authors.items() if isinstance(info, dict)}
    parts = [
        '<svg xmlns="{}" width="{}" height="{}" viewBox="{} {} {} {}" font-family="Inter, sans-serif">'.format(
            SVG_NS, width_px, height_px, _n(box[0]), _n(box[1]), _n(box[2] - box[0]), _n(box[3] - box[1])),
        '<defs><pattern id="synapse-hatch" width="12" height="12" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        '<line x1="0" y1="0" x2="0" y2="12" stroke="{}" stroke-width="3"/></pattern></defs>'.format(MUTED),
        '<rect x="{}" y="{}" width="{}" height="{}" fill="{}"/>'.format(_n(box[0]), _n(box[1]), _n(box[2] - box[0]), _n(box[3] - box[1]),
                                                                       _theme.base()["canvas"]),
    ]
    if grid:
        parts.extend(_grid(box, u))
    frames = [e for e in elements if e.get("type") == "frame"]
    comments = [e for e in elements if e.get("type") == "comment"]
    for el in frames:
        parts.append(_frame(el, u))
    for el in elements:
        kind = el.get("type")
        if kind in ("frame", "comment"):
            continue
        try:
            if kind in ("box", "ellipse", "diamond", "note", "text"):
                parts.append(_shape(el))
            elif kind == "arrow":
                parts.append(_arrow(el, u))
            elif kind == "pen":
                parts.append(_pen(el))
            elif kind == "path":
                parts.append(_path(el))
            elif kind == "svg":
                parts.append(_inline_svg(el, team, u))
            elif kind == "image":
                parts.append(_image(el, team, u))
            else:
                parts.append(_card(el, team, u))
        except (TypeError, ValueError, KeyError):
            continue  # one malformed element never blanks the picture
    for lock in scene.get("locks") or []:
        if isinstance(lock, dict):
            parts.append(_lock(lock, u))
    for claim in scene.get("claims") or []:
        if isinstance(claim, dict):
            parts.append(_claim(claim, u, colors, reader))
    for el in comments:
        parts.append(_comment(el, u, colors))
    if marks:
        for el in elements:
            parts.append(_badge(el, u))
    parts.append("</svg>")
    return "".join(parts)
