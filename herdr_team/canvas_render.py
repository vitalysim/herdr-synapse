"""Canvas rendering without a browser: scene -> SVG -> PNG, with id marks and grid dots (0.21).

Contract ``.local/prd/canvas-contracts.md`` section 8.3. Agents read the
canvas as text first and as a picture second; this module draws the picture.
The geometry is exact, the engine's hand-drawn look is not reproduced:

* ``render_svg`` draws the canonical scene (or one region of it) as one
  standalone SVG document. Since canvas v2 phase 1 it draws the display list
  (``canvas_display``: every kind draws itself) through ``canvas_svg``, the
  same list and the same SVG mapping the page uses, in the light or the dark
  theme; this module adds what only the agent's picture has: sanitised ``svg``
  blocks inlined, images and the stills the page captured as ``data:`` URIs,
  text rewritten for resvg, every element's id in a small badge
  (Set-of-Mark), and ``grid`` cell dots and names (Scaffold), because models
  locate marked things far better than unmarked ones. The geometry moved to
  ``canvas_geometry``; every old name is still importable from here.
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

from herdr_team import canvas_display as _display
from herdr_team import canvas_svg as _svg
from herdr_team import canvas_text as _ctext
from herdr_team import features as _features
from herdr_team import store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError
from herdr_team.paths import TeamPaths, ensure_dir

# The geometry moved to ``canvas_geometry`` (canvas v2 phase 1): every old name stays importable from here.
from herdr_team.canvas_geometry import (  # noqa: F401,E402 - re-exported for callers and tests
    ARROW_LABEL_EMS, ARROW_LABEL_PAD, ARROW_LABEL_SCALE, CAP_STEPS, CURVE_BOW, DEFAULT_MAX_PX, FRAME_BAND, FRAME_TITLE_AT,
    FRAME_TITLE_WEIGHT, FREEHAND_SIZE, FREEHAND_STREAMLINE, FREEHAND_THINNING, PADDING, _bounds, _curve_pieces, _densify,
    _fit_line, _fmt, _frame_title, _intersects, _points, _vector_angle, absolute_path, arc_points, arrow_label_center,
    arrow_label_pill, arrow_label_text, arrow_label_width, arrow_midpoint, arrow_route, curve_pieces, drawn_bounds,
    fit_line, frame_title, frame_title_box, freehand_outline, head_points, normalize_path, normalize_region, parse_path,
    path_mlcqz, pixel_size, sanitize_path_data, settle_box, units_per_px, view_box,
)

RESVG_ENV = "HERDR_SYNAPSE_RESVG"
RENDER_TIMEOUT_S = 10.0
#: Newest look/send/export renders kept per team (by name stem, both files of a render).
RENDER_KEEP = 50
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
                  env: Optional[Mapping[str, str]] = None, theme: str = "light") -> Dict[str, Any]:
    """Write ``<name>.svg`` and ``<name>.png`` under ``out_dir``; ``{"svg", "png", "image_error", "width_px", "height_px", "region"}``."""
    out_dir = Path(out_dir)
    ensure_dir(out_dir)
    svg_text, box = picture(scene, region, marks=marks, grid=grid, reader=reader, max_px=DEFAULT_MAX_PX, team=team, theme=theme)
    width_px, height_px = pixel_size(box, DEFAULT_MAX_PX)
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
# svg rendering

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


_ASSET_NAME = re.compile(r"^[0-9a-f]{32}\.(png|jpg|svg)\Z")
_STILL_NAME = re.compile(r"^E-[0-9]+-v[0-9]+\.png\Z")


def _asset_bytes(team: Optional[TeamPaths], name: Any) -> Optional[bytes]:
    if team is None or not isinstance(name, str) or not _ASSET_NAME.match(name):
        return None
    try:
        return store.read_bytes(_features.whiteboard_dir(team) / "assets" / name)
    except (OSError, HerdrTeamError):
        return None


def _still_bytes(team: Optional[TeamPaths], name: Any) -> Optional[bytes]:
    if team is None or not isinstance(name, str) or not _STILL_NAME.match(name):
        return None
    try:
        data = store.read_bytes(_features.whiteboard_dir(team) / "stills" / name)
    except (OSError, HerdrTeamError):
        return None
    return data if data and data.startswith(PNG_MAGIC) else None


def _image_tag(data: bytes, mime: str, x0: float, y0: float, w: float, h: float) -> str:
    return '<image x="{}" y="{}" width="{}" height="{}" preserveAspectRatio="xMidYMid meet" href="data:{};base64,{}"/>'.format(
        _n(x0), _n(y0), _n(w), _n(h), mime, base64.b64encode(data).decode("ascii"))


def _n(value: float) -> str:
    return _svg.fmt(value)


def _inline_svg(data: bytes, box: Tuple[float, float, float, float]) -> Optional[str]:
    """A stored SVG asset, sanitised again as it is read, placed in ``box`` as a nested ``<svg>`` (None when it is refused)."""
    try:
        root = _parse_svg(data.decode("utf-8", "replace"))
    except HerdrTeamError:
        return None
    x0, y0, w, h = box
    if not root.get("viewBox"):
        size = svg_size(data.decode("utf-8", "replace"))
        if size:
            root.set("viewBox", "0 0 {} {}".format(_n(size[0]), _n(size[1])))
    for key in ("x", "y", "width", "height", "preserveAspectRatio"):
        root.attrib.pop(key, None)
    out: List[str] = []
    _serialise(root, out, root=True)
    markup = "".join(out)
    placement = '<svg x="{}" y="{}" width="{}" height="{}" preserveAspectRatio="xMidYMid meet"'.format(_n(x0), _n(y0), _n(w), _n(h))
    return markup.replace('<svg xmlns="{}"'.format(SVG_NS), placement, 1)


def embedder(team: Optional[TeamPaths]) -> _svg.Embed:
    """The agent's picture's ``embed``: a team's assets and stills inlined (``data:`` URLs, SVG nested), None when missing."""
    def embed(src: Mapping[str, Any], box: Tuple[float, float, float, float]) -> Optional[str]:
        x0, y0, w, h = box
        if isinstance(src.get("still"), str):
            data = _still_bytes(team, src["still"])
            return _image_tag(data, "image/png", x0, y0, w, h) if data else None
        name = src.get("asset")
        data = _asset_bytes(team, name)
        if data is None:
            return None
        if str(name).endswith(".svg"):
            return _inline_svg(data, box)
        sniffed = sniff_image(data)
        return _image_tag(data, sniffed[0], x0, y0, w, h) if sniffed else None
    return embed


def mark_anchor(el: Dict[str, Any]) -> Tuple[float, float]:
    """Where an element's id badge goes: its top-left, an arrow's midpoint, a comment's pin."""
    if el.get("type") == "arrow":
        pill = arrow_label_pill(el)
        if pill is not None:
            # A labelled arrow's badge goes just above its label's pill.
            (px, py, pw, _ph), _size, _lines = pill
            return px + pw / 2.0, py - 17
        return arrow_midpoint(_points(el.get("points")))
    if el.get("type") == "comment":
        point = el.get("point") if isinstance(el.get("point"), list) else [el.get("x") or 0, el.get("y") or 0]
        return float(point[0]) + 12, float(point[1]) - 22
    return float(el.get("x") or 0), float(el.get("y") or 0)


def picture(scene: Dict[str, Any], region: Optional[Sequence[float]] = None, marks: bool = True, grid: bool = False,
            reader: Optional[str] = None, max_px: int = DEFAULT_MAX_PX, team: Optional[TeamPaths] = None,
            theme: str = "light") -> Tuple[str, Tuple[float, float, float, float]]:
    """``(svg, box)``: the agent's picture of the scene (or a region) and the box it shows."""
    dl = _display.display_list(scene, reader=reader)
    box = normalize_region(region) if region is not None else tuple(float(v) for v in dl["bbox"])
    badges: List[Tuple[str, float, float, bool]] = []
    if marks:
        drawn = [e for e in scene.get("elements") or [] if isinstance(e, dict) and _intersects(drawn_bounds(e), box)]
        drawn.sort(key=lambda e: (int(e.get("z") or 0), str(e.get("id"))))
        for el in drawn:
            try:
                x, y = mark_anchor(el)
            except (TypeError, ValueError, KeyError, IndexError):
                continue
            badges.append((str(el.get("id") or ""), x, y, el.get("type") == "text"))
    svg_text = _svg.write(dl, theme=theme, box=box, max_px=max_px, embed=embedder(team), resvg_text=True, marks=badges, grid=grid)
    return svg_text, box  # type: ignore[return-value]


def render_svg(scene: Dict[str, Any], region: Optional[Sequence[float]] = None, marks: bool = True, grid: bool = False,
               reader: Optional[str] = None, max_px: int = DEFAULT_MAX_PX, team: Optional[TeamPaths] = None, theme: str = "light") -> str:
    """The scene (or a region of it) as one standalone SVG document, with id marks and optional grid dots.

    Since canvas v2 phase 1 this draws the display list (``canvas_display``) with ``canvas_svg``, the same
    list and the same mapping the page draws, in ``theme`` (``light`` or ``dark``)."""
    return picture(scene, region, marks=marks, grid=grid, reader=reader, max_px=max_px, team=team, theme=theme)[0]

