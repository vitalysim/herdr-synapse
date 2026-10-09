"""The display list (canvas v2 phase 1): the scene as absolute drawing primitives every renderer draws.

Contract: ``docs/display-list.md`` (``.local/prd/canvas-v2-phase1.md`` 1). The
Python geometry turns the canonical scene into a versioned list of entries, one
per element, claim or lock, each holding primitives in world units: rectangles,
ellipses, polygons, paths, arrows whose heads are already computed, text whose
lines are already broken, images, browser-drawn slots and groups. Colours are
token references (``tone.info.fill``, ``base.ink``) and both palettes travel in
the document, so a renderer switches theme by lookup and never inverts colours.

Two SVG writers draw it, byte for byte the same: ``canvas_svg`` in Python (the
agent's PNG through resvg) and ``web/src/v2/render/svgString.js`` on the page.
Nothing here measures on behalf of a renderer later: renderers never wrap,
measure or move text.

* ``display_list(scene)`` is the whole document; ``entries(scene, ids)`` the
  entries of some elements (a delta); ``entry(el, env)`` one of them.
* Each kind draws itself (``canvas_kinds.Kind.emit``); an unknown kind, or an
  element its kind cannot draw, is the placeholder card, never an error.
* ``paints(el)`` is the one place a stored hex becomes a token reference (1.4).
* ``validate(doc)`` is the executable schema; ``dumps(doc)`` the canonical JSON.
* ``python3 -m herdr_team.canvas_display --write-goldens [scene ...]`` rewrites
  ``tests/fixtures/display`` (only the named scenes when given; ``--check-goldens``
  fails when they are stale).
* Semantic zoom (canvas v2 phase 2, 6.1): ``LOD_BODY``, ``LOD_LABEL``,
  ``skeleton`` and ``title_pair`` are the helpers every kind draws its level of
  detail with; the bands are tokens (``canvas_theme.lod``).

Pure: no I/O outside ``main``, and no import of ``canvas`` (which imports this
module). The same scene always gives the same bytes.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from herdr_team import canvas_geometry as _geo
from herdr_team import canvas_kinds as _kinds
from herdr_team import canvas_text as _ctext
from herdr_team import canvas_theme as _theme

DL_VERSION = 1
GENERATOR = "herdr_team.canvas_display 1"
LAYERS = _kinds.LAYERS
LAYER_ORDER = {name: index for index, name in enumerate(LAYERS)}
#: The fonts a display list draws with (``fonts`` in the document).
FONTS = {"sans": {"family": "Inter", "weights": [400, 500, 600, 700]}, "mono": {"family": "Geist Mono", "weights": [400]}}
HITS = ("rect", "ellipse", "diamond", "line", "frame", "pin", "none")
EDIT_WRAPS = ("box", "width", "auto", "line")
PRIMITIVES = ("rect", "ellipse", "poly", "line", "path", "arrow", "text", "image", "slot", "group")
#: Where the goldens live and the scenes they are made from.
REPO = Path(__file__).resolve().parent.parent
GOLDENS_DIR = REPO / "tests" / "fixtures" / "display"
SCENES_DIR = REPO / "tests" / "fixtures" / "canvas_scenes"

Box = Tuple[float, float, float, float]
Primitive = Dict[str, Any]
Env = Dict[str, Any]

_HEX = re.compile(r"^#[0-9a-fA-F]{6}\Z")
#: A paint reference (1.4): ``base.*``, ``tone.*``, ``chip.*``, and since phases 3 and 4 a chart's (``chart.paper``,
#: ``chart.cat.3``) and a 3D material's (``mat.info.top``).
_REF = re.compile(r"^(base\.[a-z_]+|tone\.[a-z]+\.[a-z_]+|chip\.([0-9]+|human)\.(bg|fg)|chart\.[a-z_]+(\.[0-9]+)?|mat\.[a-z]+\.[a-z_]+)\Z")
#: A slot's ``ref.doc``: an asset the page fetches (a chart's datasets, a glTF model).
_DOC_ASSET = re.compile(r"^[0-9a-f]{32}\.(json|glb)\Z")
_STILL = re.compile(r"^E-[1-9][0-9]*-v[0-9]+(-[a-z]{1,12})?\.png\Z")
_PATH_D = re.compile(r"^[MLCQZ0-9eE.,+\- ]*\Z")
_ID_NUMBER = re.compile(r"^[A-Z]-([0-9]+)\Z")

#: Legacy ink for a label that stored no colour (a pre-0.22 note).
INK = "base.ink"
MUTED = "base.ink_muted"
#: The placeholder card of a kind the page draws itself, or one this build does not know.
CARD_RADIUS = 8

#: Semantic zoom (phase 2, 6.1): body-level text draws at ``titles`` (0.35) screen pixels per unit and above, a label at
#: ``overview`` (0.15) and above; between them a body is a skeleton of bars (``skeleton``).
_LOD = _theme.lod()
LOD_TITLES = _LOD["titles"]
LOD_OVERVIEW = _LOD["overview"]
LOD_BODY: List[Optional[float]] = [LOD_TITLES, None]
LOD_LABEL: List[Optional[float]] = [LOD_OVERVIEW, None]
LOD_SKELETON: List[Optional[float]] = [LOD_OVERVIEW, LOD_TITLES]
TITLE_MIN_PX = _LOD["title_min_px"]
SKELETON_H = _LOD["skeleton_h"]
SKELETON_LINES = int(_LOD["skeleton_lines"])
#: A tooltip (``detail``) is cut to this many characters in the entry's ``tip``.
TIP_MAX = 500


# --------------------------------------------------------------------------
# numbers (1.7)


_INF = float("inf")


def r2(value: Any) -> Any:
    """``value`` rounded to 2 decimals half away from zero (an integral result as an int, never -0)."""
    number = float(value)
    if number != number or number == _INF or number == -_INF:
        return 0
    n = int((number if number >= 0 else -number) * 100 + 0.5)  # a non-negative float: int() is floor
    if not n:
        return 0
    if n % 100 == 0:
        return n // 100 if number > 0 else -(n // 100)
    return n / 100.0 if number > 0 else -n / 100.0


def _rounded(obj: Any) -> Any:
    """Every float in ``obj`` through ``r2`` (ints, bools, strings and None as they are)."""
    kind = type(obj)
    if kind is float:
        return r2(obj)
    if kind is dict:
        return {key: _rounded(value) for key, value in obj.items()}
    if kind is list or kind is tuple:
        return [_rounded(value) for value in obj]
    if isinstance(obj, float) and not isinstance(obj, bool):
        return r2(obj)
    return obj


def num(value: Any, default: float) -> float:
    """``value`` as a finite float, else ``default``."""
    if isinstance(value, bool):
        return default
    try:
        found = float(value)
    except (TypeError, ValueError):
        return default
    return found if math.isfinite(found) else default


def style_of(el: Mapping[str, Any]) -> Dict[str, Any]:
    return el.get("style") if isinstance(el.get("style"), dict) else {}


def box_of(el: Mapping[str, Any]) -> Box:
    """``(x0, y0, x1, y1)`` of an element's box (at least one unit wide and tall; junk reads as 0 and 1)."""
    x, y = num(el.get("x"), 0.0), num(el.get("y"), 0.0)
    return x, y, x + max(1.0, num(el.get("w"), 1.0)), y + max(1.0, num(el.get("h"), 1.0))


def xywh(box: Sequence[float]) -> List[float]:
    return [box[0], box[1], box[2] - box[0], box[3] - box[1]]


def points_of(raw: Any) -> List[List[float]]:
    out = []
    for point in raw if isinstance(raw, list) else []:
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            out.append([x, y])
    return out


def fmt(value: Any) -> str:
    """How both SVG writers print a number (1.7): 2 decimals half away from zero, trailing zeros dropped."""
    number = value if type(value) is float or type(value) is int else num(value, 0.0)
    if number != number or number == _INF or number == -_INF:
        return "0"
    n = int((number if number >= 0 else -number) * 100 + 0.5)
    if not n:
        return "0"
    whole, frac = divmod(n, 100)
    sign = "-" if number < 0 else ""
    if not frac:
        return sign + str(whole)
    return sign + str(whole) + ("." + str(frac // 10) if frac % 10 == 0 else ".{:02d}".format(frac))


# --------------------------------------------------------------------------
# colour (1.4)


def _hex(value: Any) -> Optional[str]:
    return value.lower() if isinstance(value, str) and _HEX.match(value) else None


def _group(kind: str) -> str:
    found = _kinds.get(kind)
    return found.tone_group if found is not None else "other"


def tone_refs(el: Mapping[str, Any], kind: Optional[str] = None) -> Dict[str, Optional[str]]:
    """``{"stroke", "fill", "text"}`` as token references from the element's ``tone`` and ``variant`` (a kind that takes no
    hex colours draws from these)."""
    style = style_of(el)
    tone = style.get("tone") if style.get("tone") in _theme.TONES else "neutral"
    variant = style.get("variant") if style.get("variant") in _theme.VARIANTS else _theme.default_tone(kind or str(el.get("type")))[1]
    return _theme.resolve_ref(tone, variant, kind or str(el.get("type") or ""))


def paints(el: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    """``{"stroke", "fill", "text"}`` of an element as paints: the one place a stored hex becomes a token reference.

    With a known ``style.tone``, a stored colour equal to what the tone resolves to in the light theme
    becomes that role's reference; any other hex is an explicit colour and stays literal (the escape
    hatch). Without a tone (an element from before 0.22) the legacy tables name the tone of its stroke
    and of its fill. A note without paper gets the idea sticky; a label with no colour of its own is
    drawn in its stroke's colour, else in ink. A role with nothing stored is None. A label on a literal
    fill stays literal too, so it keeps its contrast with that fill in every theme.
    """
    style = style_of(el)
    kind = str(el.get("type") or "")
    stored = {role: _hex(style.get(role)) for role in ("stroke", "fill", "text")}
    out: Dict[str, Optional[str]] = {"stroke": None, "fill": None, "text": None}
    tone = style.get("tone") if style.get("tone") in _theme.TONES else None
    if tone is not None:
        variant = style.get("variant") if style.get("variant") in _theme.VARIANTS else "soft"
        light = _theme.resolve(tone, variant, kind, "light")
        refs = _theme.resolve_ref(tone, variant, kind)
        for role, value in stored.items():
            if value is None:
                continue
            match = light.get(role)
            out[role] = refs[role] if isinstance(match, str) and match.lower() == value and refs.get(role) else value
    else:
        for role in ("stroke", "fill"):
            value = stored[role]
            if value is None:
                continue
            legacy = _theme.tone_of(value)
            ref = _theme.resolve_ref(legacy, "soft", kind).get(role) if legacy else None
            out[role] = ref or value
        if stored["text"] is not None:
            legacy = _theme.tone_of(stored["text"])
            out["text"] = (_theme.resolve_ref(legacy, "soft", kind).get("text") if legacy else None) or stored["text"]
    if kind == "note" and out["fill"] is None:
        out["fill"] = "tone.idea.sticky"
    fill = out["fill"]
    if isinstance(fill, str) and fill.startswith("#") and isinstance(out["text"], str) and not out["text"].startswith("#"):
        # A label on an explicit fill (a legacy ``fill: "blue"``) keeps the colour it was chosen against: that fill
        # is the same in every theme, so a reference would put light text on it in the dark theme.
        out["text"] = stored["text"] or _theme.palette("light").get(out["text"], "#1c2024")
    return out


def text_paint(el: Mapping[str, Any], found: Optional[Dict[str, Optional[str]]] = None) -> str:
    """A label's paint: its own colour, else its stroke's (``style.text or style.stroke``), else ink."""
    found = found if found is not None else paints(el)
    return found["text"] or found["stroke"] or INK


def dash(style: Mapping[str, Any], width: float) -> Optional[List[float]]:
    """``[on, off]`` in world units for a dashed or dotted line of ``width``, None for a solid one."""
    if style.get("dash") == "dashed":
        return [width * 4, width * 3]
    if style.get("dash") == "dotted":
        return [width * 0.5, width * 3]
    return None


def opacity(style: Mapping[str, Any]) -> Optional[float]:
    """``op`` (0 to 1) for a style below 100 % opacity, else None."""
    value = style.get("opacity")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 100:
        return max(0.0, float(value)) / 100.0
    return None


def stroke_fields(el: Mapping[str, Any], stroke: Optional[str], width: Optional[float] = None) -> Dict[str, Any]:
    """``stroke``, ``sw`` and ``dash`` of a stroked mark (the style's width and dash)."""
    style = style_of(el)
    sw = (num(style.get("width"), 2.0) or 2.0) if width is None else width
    out: Dict[str, Any] = {"stroke": stroke, "sw": sw}
    found = dash(style, sw)
    if found is not None:
        out["dash"] = found
    return out


def with_opacity(prim: Primitive, el: Mapping[str, Any]) -> Primitive:
    found = opacity(style_of(el))
    if found is not None:
        prim["op"] = found
    return prim


# --------------------------------------------------------------------------
# text


def font_of(style: Mapping[str, Any]) -> str:
    """The display list's font: ``mono`` for ``code``, else ``sans`` (``hand`` has no bundled face)."""
    return "mono" if style.get("font") == "code" else "sans"


def measure_key(style: Mapping[str, Any]) -> str:
    """The ``canvas_text`` font key a label was measured with (``hand`` is measured wider)."""
    font = style.get("font")
    return font if isinstance(font, str) and font in _ctext.FONT_ALIASES else "normal"


def direction(line: str) -> Optional[str]:
    """``rtl`` when the line's first strong character is right to left (QA R-8), else None."""
    for ch in line:
        kind = unicodedata.bidirectional(ch)
        if kind in ("R", "AL"):
            return "rtl"
        if kind == "L":
            return None
    return None


def text_prim(lines: Sequence[str], x: float, top: float, size: float, style: Mapping[str, Any], fill: Any, anchor: str,
              box: Optional[Sequence[float]], weight: int = _ctext.DEFAULT_WEIGHT) -> Primitive:
    """A ``text`` primitive: one line per entry, each at its own baseline below ``top`` (the half-leading model).

    ``box`` is the room the lines were fitted to; None makes it the lines' own extent."""
    key = measure_key(style)
    lh = _ctext.line_height(size)
    first = top + _ctext.baseline(size, key, weight)
    out_lines = []
    for index, line in enumerate(lines):
        entry: Dict[str, Any] = {"y": first + index * lh, "t": line, "w": _ctext.measure(line, key, size, weight).width}
        found = direction(line)
        if found:
            entry["dir"] = found
        out_lines.append(entry)
    if box is None:
        widest = max((line["w"] for line in out_lines), default=0.0)
        left = x if anchor == "start" else (x - widest / 2.0 if anchor == "middle" else x - widest)
        box = (left, top, widest, lh * len(out_lines))
    return {"k": "text", "x": x, "anchor": anchor, "font": font_of(style), "weight": int(weight), "size": size, "lh": lh,
            "fill": fill, "box": [box[0], box[1], box[2], box[3]], "lines": out_lines}


def _same_words(lines: Sequence[str], text: str) -> bool:
    """Whether ``lines`` hold exactly ``text``'s characters (whitespace aside): stored lines still draw the label (QA F-9)."""
    return "".join("".join(lines).split()) == "".join(text.split())


def label(el: Mapping[str, Any], fill: Any) -> Optional[Primitive]:
    """A labelled element's text: the lines its fit stored (else its kind's wrap at its size), in its inner box.

    A free ``text`` hangs from its top-left; any other label is centred in its kind's inner box.
    """
    text = str(el.get("text") or "")
    if not text:
        return None
    style = style_of(el)
    x0, y0, x1, y1 = box_of(el)
    result = _kinds.drawn(el)
    if result is None:
        return block(text, (x0, y0, x1 - x0, y1 - y0), style, fill)
    fit = el.get("fit") if isinstance(el.get("fit"), dict) else {}
    stored = fit.get("lines")
    lines: Sequence[str] = result.lines
    ix, iy, iw, ih = result.inner
    size = float(result.size)
    if isinstance(stored, list) and stored and all(isinstance(line, str) for line in stored) and (fit.get("truncated") or _same_words(stored, text)):
        # The lines the fit stored, while they still fit the room (a line the page measured wider no longer may).
        key = measure_key(style_of(el))
        if all(_ctext.measure(line, key, size).width <= iw + 0.5 for line in stored):
            lines = stored
    inner = (x0 + ix, y0 + iy, iw, ih)
    if el.get("type") == "text":
        return text_prim(lines, x0, y0, size, style, fill, "start", inner)
    top = y0 + iy + (ih - _ctext.line_height(size) * len(lines)) / 2.0
    return text_prim(lines, x0 + ix + iw / 2.0, top, size, style, fill, "middle", inner)


def block(text: str, box: Sequence[float], style: Mapping[str, Any], fill: Any, size: Optional[float] = None,
          weight: int = _ctext.DEFAULT_WEIGHT) -> Primitive:
    """``text`` wrapped to the box (16 units of room) and centred in it: a label no kind measured."""
    x, y, w, h = box
    size = size if size is not None else num(style.get("size"), 20.0) or 20.0
    key = measure_key(style)
    lines = _ctext.wrap(text, max(1.0, w - 16), key, size, weight)
    top = y + (h - _ctext.line_height(size) * len(lines)) / 2.0
    return text_prim(lines, x + w / 2.0, top, size, style, fill, "middle", (x, y, w, h), weight)


def skeleton(box: Sequence[float], line_ws: Sequence[float], lh: float, anchor: str = "start") -> List[Primitive]:
    """Body text zoomed out (phase 2, 6.1): one bar per line (at most ``SKELETON_LINES``), ``SKELETON_H`` tall and as wide
    as the line, in ``base.grid``, drawn only between the overview and titles bands. ``box`` is the text's ``[x, y, w, h]``."""
    x, y, w, _h = (float(v) for v in box)
    out: List[Primitive] = []
    for index, line_w in enumerate(list(line_ws)[:SKELETON_LINES]):
        width = max(4.0, min(float(line_w), w))
        left = x if anchor == "start" else (x + (w - width) / 2.0 if anchor == "middle" else x + w - width)
        out.append({"k": "rect", "x": left, "y": y + index * lh + (lh - SKELETON_H) / 2.0, "w": width, "h": SKELETON_H, "r": SKELETON_H / 2.0,
                    "fill": "base.grid", "lod": list(LOD_SKELETON)})
    return out


def body(prim: Primitive) -> List[Primitive]:
    """A body-level text primitive as it draws at every zoom: itself at ``LOD_BODY`` and its skeleton below that."""
    lines = prim.get("lines") or []
    if not lines:
        return []
    prim["lod"] = list(LOD_BODY)
    box = prim.get("box") or [prim.get("x", 0), 0, 0, 0]
    top = float(box[1])
    anchor = str(prim.get("anchor") or "start")
    return [prim] + skeleton([box[0], top, box[2], box[3]], [num(line.get("w"), 0.0) for line in lines], float(prim.get("lh") or 20), anchor)


def title_pair(el: Mapping[str, Any], title: str, x: float, y: float, size: float, weight: int, fill: Any, top_level: bool,
               room: Optional[float] = None) -> List[Primitive]:
    """A container's title by the title rule (phase 2, 6.1; phase 1, 1.6 generalised): in its band, cut to ``room``, from
    ``TITLE_MIN_PX / size`` screen pixels per unit up; below that, a top-level container's whole title stands above it,
    never under ``TITLE_MIN_PX`` on screen. A nested container's band title stays down to the overview band."""
    lh = _ctext.line_height(size)
    width = room if room is not None else max(0.0, box_of(el)[2] - x)
    switch = TITLE_MIN_PX / float(size)
    band = text_prim([_geo.fit_line(title, width, size, weight)], x, y, size, {}, fill, "start", (x, y, width, lh), weight)
    if not top_level:
        band["lod"] = list(LOD_LABEL)
        return [band]
    band["lod"] = [switch, None]
    x0, y0 = box_of(el)[0], box_of(el)[1]
    above = text_prim([title], x0, y0 - lh, size, {}, fill, "start", None, weight)
    above.update(lod=[None, switch], zoom={"min_px": TITLE_MIN_PX, "grow": "up", "bottom": y0},
                 base_ratio=_ctext.baseline(1, "normal", weight))
    return [band, above]


def text_edit(el: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """What a double-click edits in a labelled element: its whole text in its inner box, at its drawn size."""
    kind = _kinds.kind_of(el)
    if kind is None or kind.edit_field is None:
        return None
    style = style_of(el)
    x0, y0, x1, y1 = box_of(el)
    result = _kinds.drawn(el) if kind.measure is not None else None
    size = float(result.size) if result is not None else num(style.get("size"), 20.0) or 20.0
    if result is not None:
        ix, iy, iw, ih = result.inner
        inner = [x0 + ix, y0 + iy, iw, ih]
    else:
        inner = [x0, y0, x1 - x0, y1 - y0]
    free = el.get("type") == "text"
    return {"field": kind.edit_field, "value": str(el.get(kind.edit_field) or ""), "box": inner, "font": font_of(style),
            "weight": _ctext.DEFAULT_WEIGHT, "size": size, "lh": _ctext.line_height(size), "align": "start" if free else "center",
            "wrap": ("width" if el.get("wrap") else "auto") if free else "box", "fill": text_paint(el)}


# --------------------------------------------------------------------------
# shared drawings


def card(el: Mapping[str, Any], subtitle: str, title: Optional[str] = None) -> List[Primitive]:
    """The placeholder card: a dashed box with the element's title and a muted line saying what it is."""
    x0, y0, x1, y1 = box_of(el)
    w, h = x1 - x0, y1 - y0
    title = str(el.get("text") or el.get("type") or "") if title is None else title
    size = max(min(h / 6.0, 22.0), 10.0)
    per_unit = max(_ctext.measure(subtitle, size=1).width, 0.01)
    small = max(4.0, min(size * 0.7, (w - 8) / per_unit))  # the subtitle shrinks to fit a narrow card
    items: List[Primitive] = [{"k": "rect", "x": x0, "y": y0, "w": w, "h": h, "r": CARD_RADIUS, "fill": "base.surface",
                               "stroke": "tone.neutral.stroke", "sw_px": 1.5, "dash": [8, 6]}]
    if title:
        items.append(block(title, (x0, y0, w, h * 0.8), {}, INK, size))
    sub = text_prim([subtitle], x0 + w / 2.0, y0 + h * 0.8 - _ctext.baseline(small, "normal", 400), small, {}, MUTED, "middle",
                    (x0, y0 + h * 0.8 - _ctext.line_height(small), w, _ctext.line_height(small)), weight=400)
    items.append(sub)
    return items


def generic_emit(el: Mapping[str, Any], env: Optional[Env] = None) -> List[Primitive]:
    """An element no kind can draw: the placeholder card, with its type and text (never an error)."""
    return card(el, "{} (not drawn by this version)".format(str(el.get("type") or "element")[:40]))


# --------------------------------------------------------------------------
# entries (1.2)


def _chip_index(author: Any, env: Env) -> Optional[int]:
    info = (env.get("authors") or {}).get(author) if isinstance(author, str) else None
    if not isinstance(info, dict):
        return None
    index = info.get("index")
    return index if isinstance(index, int) and not isinstance(index, bool) and index >= 0 else None


def chip(author: Any, env: Env) -> Dict[str, str]:
    """The author chip: token references plus up to two initials; the operator is ``chip.human``."""
    name = str(author or "")
    index = _chip_index(name, env)
    if name == "human" or index is None:
        base = "chip.human"
        initials = "OP" if name == "human" else "".join(ch for ch in name if ch.isalnum())[:2].upper()
    else:
        base = "chip.{}".format(index % _theme.chip_count())
        initials = "".join(ch for ch in name if ch.isalnum())[:2].upper()
    return {"bg": base + ".bg", "fg": base + ".fg", "initials": initials or "?"}


def _in_lock(box: Box, env: Env) -> bool:
    for lock in env.get("locks") or []:
        region = lock.get("region") if isinstance(lock, dict) else None
        if isinstance(region, list) and len(region) == 4:
            x0, y0, x1, y1 = (num(v, 0.0) for v in region)
            if box[0] < x1 and x0 < box[2] and box[1] < y1 and y0 < box[3]:
                return True
    return False


def _edit_from_label(el: Mapping[str, Any], kind: Any, items: Sequence[Primitive]) -> Optional[Dict[str, Any]]:
    """The editor over the label the kind just drew (its room, size and colour), so nothing is measured twice."""
    label_prim = next((p for p in items if p.get("k") == "text" and p.get("zoom") is None and "layer" not in p), None)
    if label_prim is None:
        return None
    free = el.get("type") == "text" or (kind.measure is not None and not kind.labelled)
    return {"field": kind.edit_field, "value": str(el.get(kind.edit_field) or ""), "box": list(label_prim["box"]), "font": label_prim["font"],
            "weight": label_prim["weight"], "size": label_prim["size"], "lh": label_prim["lh"], "align": "start" if free else "center",
            "wrap": ("width" if el.get("wrap") else "auto") if free else "box", "fill": label_prim["fill"]}


def _default_hit(el: Mapping[str, Any]) -> Dict[str, Any]:
    return {"shape": "rect", "box": xywh(box_of(el))}


def _union(boxes: Iterable[Sequence[float]]) -> Optional[Box]:
    found = [b for b in boxes if b is not None]
    if not found:
        return None
    return min(b[0] for b in found), min(b[1] for b in found), max(b[2] for b in found), max(b[3] for b in found)


_NUMBER = re.compile(r"[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?")


def _apply(t: Sequence[float], x: float, y: float) -> Tuple[float, float]:
    return t[0] * x + t[2] * y + t[4], t[1] * x + t[3] * y + t[5]


def prim_bounds(prim: Mapping[str, Any], t: Optional[Sequence[float]] = None) -> Optional[Box]:
    """World bounds of what a primitive draws (strokes aside); None for one sized in screen pixels or at a zoom."""
    kind = prim.get("k")
    points: List[Tuple[float, float]] = []
    if prim.get("zoom") is not None:
        return None
    if kind in ("rect", "image", "slot"):
        x, y, w, h = (num(prim.get(key), 0.0) for key in ("x", "y", "w", "h"))
        points = [(x, y), (x + w, y + h)]
    elif kind == "ellipse":
        cx, cy, rx, ry = (num(prim.get(key), 0.0) for key in ("cx", "cy", "rx", "ry"))
        points = [(cx - rx, cy - ry), (cx + rx, cy + ry)]
    elif kind in ("poly", "line"):
        points = [(p[0], p[1]) for p in points_of(prim.get("points"))]
    elif kind in ("path", "arrow"):
        values = [float(v) for v in _NUMBER.findall(str(prim.get("d") or ""))]
        points = list(zip(values[0::2], values[1::2]))
        for head in prim.get("heads") or []:
            if head.get("shape") == "dot":
                cx, cy, r = num(head.get("cx"), 0.0), num(head.get("cy"), 0.0), num(head.get("r"), 0.0)
                points += [(cx - r, cy - r), (cx + r, cy + r)]
            else:
                points += [(p[0], p[1]) for p in points_of(head.get("points"))]
    elif kind == "text":
        box = prim.get("box")
        if isinstance(box, list) and len(box) == 4:
            x, y, w, h = (num(v, 0.0) for v in box)
            points = [(x, y), (x + w, y + h)]
    elif kind == "group":
        if prim.get("screen") is not None:
            anchor = prim["screen"]
            points = [(num(anchor[0], 0.0), num(anchor[1], 0.0))]
        else:
            inner = prim.get("t") if isinstance(prim.get("t"), list) and len(prim["t"]) == 6 else None
            merged = t if inner is None else (inner if t is None else [
                t[0] * inner[0] + t[2] * inner[1], t[1] * inner[0] + t[3] * inner[1],
                t[0] * inner[2] + t[2] * inner[3], t[1] * inner[2] + t[3] * inner[3],
                t[0] * inner[4] + t[2] * inner[5] + t[4], t[1] * inner[4] + t[3] * inner[5] + t[5]])
            return _union(prim_bounds(item, merged) for item in prim.get("items") or [])
    if not points:
        return None
    if t is not None:
        points = [_apply(t, x, y) for x, y in points]
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def _tip(el: Mapping[str, Any]) -> Optional[str]:
    detail = el.get("detail")
    if not isinstance(detail, str) or not detail.strip():
        return None
    detail = detail.strip()
    return detail if len(detail) <= TIP_MAX else detail[:TIP_MAX - 1].rstrip() + "…"


def _extras(el: Mapping[str, Any], kind: Any) -> Dict[str, Any]:
    """The phase 2 entry fields (6.2), each only when it applies: ``parts``, ``pin``, ``block`` and ``part``, ``container``
    and ``tip``."""
    out: Dict[str, Any] = {}
    if kind is not None and kind.parts is not None:
        found = kind.parts(dict(el))
        if found:
            out["parts"] = found
    pin = el.get("pin")
    if isinstance(pin, dict) and pin.get("by") in ("human", "agent"):
        out["pin"] = pin["by"]
    if isinstance(el.get("part"), str) and isinstance(el.get("group"), str):
        out["block"], out["part"] = el["group"], el["part"]
    settings = el.get("settings") if isinstance(el.get("settings"), dict) else {}
    if el.get("type") == "frame" and settings.get("layout") in ("row", "column", "grid"):
        out["container"] = {"layout": settings["layout"], "gap": _theme.gap(settings.get("gap"), 20),
                            "order": [str(i) for i in el.get("order") or [] if isinstance(i, str)]}
    tip = _tip(el)
    if tip is not None:
        out["tip"] = tip
    return out


def entry(el: Mapping[str, Any], env: Env) -> Dict[str, Any]:
    """One element's entry (1.2): its primitives and what the page needs to select, resize, connect and edit it."""
    kind = _kinds.kind_of(el)
    items: List[Primitive]
    try:
        items = list(kind.emit(dict(el), env)) if kind is not None and kind.emit is not None else generic_emit(el, env)
    except (TypeError, ValueError, KeyError, IndexError, AttributeError, ZeroDivisionError, OverflowError):
        kind = None
        try:
            items = generic_emit(el, env)
        except (TypeError, ValueError, KeyError, IndexError, AttributeError, ZeroDivisionError, OverflowError):
            x0, y0, x1, y1 = box_of(el)
            items = [{"k": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "fill": None, "stroke": "tone.neutral.stroke", "sw_px": 1.5,
                      "dash": [8, 6]}]
    box = box_of(el)
    try:
        drawn = kind.bounds(dict(el)) if kind is not None and kind.bounds is not None else box
    except (TypeError, ValueError, KeyError, IndexError, AttributeError, ZeroDivisionError):
        drawn = box
    bbox = _union([drawn] + [prim_bounds(item) for item in items]) or box
    hit: Dict[str, Any] = _default_hit(el)
    edit: Optional[Dict[str, Any]] = None
    if kind is not None:
        try:
            if kind.hit is not None:
                hit = kind.hit(dict(el))
            if kind.text_edit is not None:
                edit = kind.text_edit(dict(el))
            elif kind.edit_field is not None and kind.measure is not None:
                edit = _edit_from_label(el, kind, items) or text_edit(el)
        except (TypeError, ValueError, KeyError, IndexError, AttributeError, ZeroDivisionError):
            hit, edit = _default_hit(el), None
    frame = el.get("frame") if isinstance(el.get("frame"), str) else None
    extras: Dict[str, Any] = {}
    try:
        extras = _extras(el, kind)
    except (TypeError, ValueError, KeyError, IndexError, AttributeError, ZeroDivisionError):
        extras = {}
    out = {
        "id": str(el.get("id") or ""), "kind": kind.name if kind is not None else str(el.get("type") or "element"),
        "layer": kind.layer if kind is not None else "marks",
        "z": int(num(el.get("z"), 0.0)), "v": int(num(el.get("updated_seq"), 0.0)),
        "bbox": list(bbox), "hit": hit, "handles": kind.handles if kind is not None else "none",
        "connect": bool(kind.connectable) if kind is not None else False, "edit": edit,
        "frame": frame, "author": str(el.get("author") or ""), "chip": chip(el.get("author"), env),
        "locked": _in_lock(box, env), "items": items,
    }
    out.update(extras)
    return _rounded(out)


#: World units kept between a claim's label and the claim beside it.
CLAIM_LABEL_GAP = 4.0
#: World units the claim's dashed border is drawn *outside* its region (canvas v2 layout clarity, V2).
#: The border is the only ink a claim puts on the board, it is 2 screen px wide at any zoom, and the region now
#: snaps to the marks it holds (``canvas_check.claim_snap``), so its edge runs along their edges: drawn on the
#: region itself the stroke lands on their outline, which at 41% zoom read as a chart's axis labels being cut off.
#: The entry's ``bbox`` stays the region, because that is the authority the claim carries.
CLAIM_BORDER_OUT = 3.0
#: World room above and below the region the claim's label may use. The label is drawn at a fixed *screen* size, so
#: its height in world units grows as the board is zoomed out, and the clip that stops it running over the claim
#: beside it is a horizontal stop and nothing else: the vertical range is whatever cannot cut it at a usable zoom.
CLAIM_LABEL_ROOM = 2000.0
#: Screen pixels between the bottom of the label's ink and the dashed border below it: the border's own 2 px stroke
#: reaches 1 px past ``CLAIM_BORDER_OUT``, and a descender must clear that.
CLAIM_LABEL_LIFT = CLAIM_BORDER_OUT + 3.0


def _claim_neighbour(claim: Mapping[str, Any], box: Tuple[float, float, float, float], env: Env) -> Optional[float]:
    """The left edge of the nearest other claim that starts right of this one's left edge, beside it (its region meets this
    one's rows), or None. A claim's label is drawn at a fixed screen size from its top-left corner, so without a stop it
    runs over its neighbour's label when zoomed out."""
    x0, y0, _x1, y1 = box
    best: Optional[float] = None
    for other in env.get("claims") or []:
        region = other.get("region")
        if other is claim or other.get("id") == claim.get("id") or not isinstance(region, list) or len(region) != 4:
            continue
        ox0, oy0, _ox1, oy1 = (num(v, 0.0) for v in region)
        if ox0 > x0 and oy0 < y1 and oy1 > y0 and (best is None or ox0 < best):
            best = ox0
    return best


#: The zooms, in screen pixels per unit, between which a claim's dashed border may be drawn differently so that it is
#: never drawn through words (layout findings N4). The words near a claim depend on the zoom - a top-level frame's
#: title stands above the frame below ``TITLE_MIN_PX / size`` and keeps 12 px on screen, so it grows in world units
#: without bound, and a claim's own label is a fixed screen size - so no region can hold them at every zoom. The border
#: is fitted per band instead, each band ``CLAIM_BAND_STEP`` wide so it is never much more generous than the zoom it
#: is seen at, from ``CLAIM_BAND_TOP`` (where every title is in its band, inside its frame) down to
#: ``CLAIM_BAND_FLOOR`` (20,000 units in 500 pixels). The theme's level-of-detail edges are band edges too, so within
#: a band the same words draw. A band drawn the same as the one beside it adds nothing to the entry.
CLAIM_BAND_TOP = 0.75
CLAIM_BAND_STEP = 1.4
CLAIM_BAND_FLOOR = 1.0 / 40.0
#: Screen pixels the dashed stroke stops short of a line of text on each side: its own half width and two more, so a
#: descender clears it.
CLAIM_TEXT_GAP_PX = 3.0
#: How far from a claim words are looked for, in units: the furthest a zoomed title stands above its frame at
#: ``CLAIM_BAND_FLOOR`` is under 600.
CLAIM_TEXT_REACH = 1000.0


def _band_ends(low: Optional[float], high: Optional[float]) -> Tuple[float, float]:
    """The units per pixel at the far (zoomed out) and near ends of a band of zoom, both just inside it.

    Just inside, because what is measured there is drawn by ``lod`` and ``lod`` is half open: at exactly ``1 / low``
    the scale can round below ``low`` and a label drawn in this band is not seen, and at ``1 / high`` it is the next
    band's label that draws. A border fitted that way broke for another claim's label where the label was not, and
    ran through it where it was (QA round 2).
    """
    far = 1.0 / ((low if low is not None else CLAIM_BAND_FLOOR / CLAIM_BAND_STEP) * (1.0 + 1e-6))
    near = 1.0 / (high * (1.0 - 1e-6)) if high is not None else 1.0
    return far, near


def _claim_bands() -> List[Tuple[Optional[float], Optional[float]]]:
    """The bands of zoom ``(low, high)`` a claim's border is fitted in, closest first (``lod`` order)."""
    edges = {CLAIM_BAND_TOP, float(LOD_TITLES), float(LOD_OVERVIEW)}
    scale = CLAIM_BAND_TOP
    while scale > CLAIM_BAND_FLOOR:
        scale /= CLAIM_BAND_STEP
        edges.add(max(scale, CLAIM_BAND_FLOOR))
    ordered = sorted((e for e in edges if CLAIM_BAND_FLOOR <= e <= CLAIM_BAND_TOP), reverse=True)
    bands: List[Tuple[Optional[float], Optional[float]]] = [(ordered[0], None)]
    bands += [(ordered[i + 1], ordered[i]) for i in range(len(ordered) - 1)]
    bands.append((None, ordered[-1]))
    return bands


#: The corners a claim's label may hang from, best first: above the top-left (where every claim label was drawn until
#: layout findings N4), above the top-right, then just inside the top-right and the top-left - inside a frame's title
#: band, which is empty once the board is zoomed out far enough for the title to stand above the frame, and still
#: inside a whole-board picture, which a label below the region is not - and last below the bottom-left and right.
CLAIM_LABEL_CORNERS = ("tl", "tr", "itr", "itl", "bl", "br")


def _claim_label(claim: Mapping[str, Any], env: Env, box: Tuple[float, float, float, float], corner: str = "tl") -> Dict[str, Any]:
    """A claim's label: hung outside one of the region's corners (``corner``, above the top-left by default) at a fixed
    screen size."""
    author = str(claim.get("author") or "")
    reader = env.get("reader")
    who = "you" if reader and author == reader else ("the operator" if author == "human" else author)
    found = chip(author, env)
    text = "{} {}: {}".format(claim.get("id"), who, claim.get("label") or "")[:120]
    x0, y0, x1, y1 = box
    # Above the region's top edge, not just inside it. A claim snaps to the marks it holds, so its top-left corner is
    # usually a frame's own top-left corner - and that is where a frame draws its title. The dashed rectangle and this
    # pill were being drawn straight through "Customer interview findings, week 38" on nearly every graph render, in
    # both themes (QA phase 6 V2, still open as F8). Text on text is the owner's own word for unclear.
    left = corner in ("tl", "bl", "itl")
    top = -(_ctext.line_height(12) + CLAIM_LABEL_LIFT) if corner in ("tl", "tr") else CLAIM_LABEL_LIFT
    label: Dict[str, Any] = {"k": "group", "screen": [x0 if left else x1, y1 if corner in ("bl", "br") else y0],
                             "items": [text_prim([text], 4 if left else -4, top, 12, {}, found["bg"], "start" if left else "end",
                                                 None, 400)]}
    right = _claim_neighbour(claim, (x0, y0, x1, y1), env) if left else None
    if right is not None:
        # A claim beside it on the right (QA phase 5 L8): the label stops where that claim, and its label, begin.
        label = {"k": "group", "clip": [x0, y0 - CLAIM_LABEL_ROOM, max(0.0, right - CLAIM_LABEL_GAP - x0),
                                        (y1 - y0) + 2 * CLAIM_LABEL_ROOM], "items": [label]}
    return label


#: A ring that holds every box: ``_words_at`` measuring for a claim's label, which is not looking along a border.
_EVERYWHERE = ((-float("inf"), -float("inf"), float("inf"), float("inf")), (float("inf"), float("inf"), -float("inf"), -float("inf")))


def _claim_labels(claim: Mapping[str, Any], env: Env, box: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    """A claim's label for every band of zoom: hung from the first corner where it lands on no words, or left out.

    The label is a fixed 12 px on screen and hangs outside the region's top-left corner, which is where a top-level
    frame's zoomed title stands too: on a member's claim of exactly their graph's frame, at a whole-board fit,
    "K-2 alpha-member: ..." was printed straight over "Checkout flow for returning shoppers" (QA round 2 of layout
    findings N4; 15 golden boards at some zoom). Text over text is what the owner calls unclear, so per band the label
    tries the other corners (``CLAIM_LABEL_CORNERS``), measured against the words the board draws there at both ends
    of the band, and where every corner lands on words it is left out: at that zoom it could only be read as a smudge.
    It is left out too once the board is zoomed out so far that the label is wider than the whole board. A claim whose
    first corner is clear at every zoom it is measured at keeps the label it always had, there.
    """
    from herdr_team import canvas_render as _render  # it imports this module: late on purpose

    x0, y0, x1, y1 = box
    variants = [(corner, _claim_label(claim, env, box, corner)) for corner in CLAIM_LABEL_CORNERS]
    source = env.get("_claim_word_source")
    if source is None:
        source = []
        for el in (env.get("by_id") or {}).values():
            if not isinstance(el, dict) or el.get("type") == "comment":
                continue
            reach = _geo.drawn_bounds(dict(el))
            if el.get("type") in ("frame", "heading") and str(el.get("text") or "").strip():
                reach = (reach[0], reach[1] - CLAIM_TEXT_REACH, reach[2] + CLAIM_TEXT_REACH, reach[3])
            source.append((reach, el))
        env["_claim_word_source"] = source
    cache = env.setdefault("_claim_entries", {})
    # The labels of the claims placed before this one, where they were placed: this one keeps off them, so two labels
    # never land on each other, and the later claim is the one that moves.
    others = [{"id": "label:" + str(oid), "items": items}
              for oid, items in (env.get("_claim_label_items") or {}).items() if oid != claim.get("id") and items]
    def hull(boxes: Sequence[Sequence[float]]) -> Tuple[float, float, float, float]:
        return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))

    def meets(reach: Sequence[float], zone: Sequence[float]) -> bool:
        return not (reach[2] < zone[0] or reach[0] > zone[2] or reach[3] < zone[1] or reach[1] > zone[3])

    # Where each corner's label lands at both ends of every band (one short line each: cheap), then the board's words
    # gathered once for the whole of that, so a band asks only about the few marks beside the corner it tries.
    board = env.get("_claim_board")
    if board is None:
        board = env["_claim_board"] = hull([reach for reach, _el in source]) if source else (x0, y0, x1, y1)
    wide = max(board[2] - board[0], x1 - x0)
    # A label is a screen-sized group hung from a world point, cut by a world clip (the claim beside it): measured once
    # at one pixel per unit relative to its anchor, it is the anchor plus those offsets times ``u`` at any zoom.
    shapes = {}
    for corner, prim in variants:
        inner, clip = (prim["items"][0], prim["clip"]) if "clip" in prim else (prim, None)
        ax, ay = float(inner["screen"][0]), float(inner["screen"][1])
        rel = [(b[0] - ax, b[1] - ay, b[2] - ax, b[3] - ay) for b in _render.text_boxes({"entries": [{"items": [inner]}]}, 1.0)]
        shapes[corner] = (ax, ay, rel, (clip[0], clip[1], clip[0] + clip[2], clip[1] + clip[3]) if clip else None)

    def spot(corner: str, u: float) -> List[Tuple[float, float, float, float]]:
        ax, ay, rel, cut = shapes[corner]
        out = []
        for r in rel:
            b = (ax + r[0] * u, ay + r[1] * u, ax + r[2] * u, ay + r[3] * u)
            if cut is not None:
                b = (max(b[0], cut[0]), max(b[1], cut[1]), min(b[2], cut[2]), min(b[3], cut[3]))
            if b[2] > b[0] and b[3] > b[1]:
                out.append(b)
        return out

    bands = []
    for low, high in _claim_bands():
        far, near = _band_ends(low, high)
        spots = {corner: spot(corner, far) + spot(corner, near) for corner, _prim in variants}
        if any(b[2] - b[0] > wide for b in spots["tl"]):
            # Zoomed out until the label is wider than the whole board: it can only lie across the drawing, and the
            # drawing is a thumbnail. Not drawn, and not measured, which is what keeps a large board's bill flat.
            spots = {corner: [] for corner in spots}
        bands.append((low, high, far, near, spots))
    every = [b for band in bands for boxes in band[4].values() for b in boxes]
    pool: List[Tuple[Sequence[float], Dict[str, Any]]] = []
    if every:
        zone = hull(every)
        for reach, el in source:
            if meets(reach, zone):
                key = el.get("id")
                if key not in cache:
                    cache[key] = entry(el, env)
                pool.append((reach, cache[key]))
        for overlay in env.get("_overlay_words") or []:
            bbox = overlay.get("bbox")
            if isinstance(bbox, list) and len(bbox) == 4:
                reach = (bbox[0], bbox[1] - CLAIM_TEXT_REACH, bbox[2] + CLAIM_TEXT_REACH, bbox[3])
                if meets(reach, zone):
                    pool.append((reach, overlay))
    # Measured against ``_EVERYWHERE``, an entry's words are the same for every claim: one memo for the whole list.
    memo: Dict[Any, Any] = env.setdefault("_claim_label_words", {})
    chosen: List[Tuple[Optional[float], Optional[float], Optional[str]]] = []
    for low, high, far, near, spots in bands:
        pick: Optional[str] = None
        gap = CLAIM_TEXT_GAP_PX * far
        for corner in CLAIM_LABEL_CORNERS:
            mine = spots[corner]
            if not mine:
                continue
            zx0, zy0, zx1, zy1 = hull(mine)
            near_by = [found for reach, found in pool
                       if not (reach[2] < zx0 or reach[0] > zx1 or reach[3] < zy0 or reach[1] > zy1)] + others
            words = _words_at(near_by, far, memo, _EVERYWHERE, env) + _words_at(near_by, near, memo, _EVERYWHERE, env)
            if not any(a[0] < w[2] + gap and a[2] > w[0] - gap and a[1] < w[3] + gap and a[3] > w[1] - gap
                       for a in mine for w in words):
                pick = corner
                break
        if chosen and chosen[-1][2] == pick:
            chosen[-1] = (low, chosen[-1][1], pick)
        else:
            chosen.append((low, high, pick))
    if len(chosen) == 1 and chosen[0][2] == "tl":
        return [variants[0][1]]
    out = []
    by_corner = dict(variants)
    for low, high, pick in chosen:
        if pick is not None:
            out.append(dict(by_corner[pick], lod=[low, high]))
    return out


def _claim_near(claim: Mapping[str, Any], env: Env, rect: Sequence[float]) -> List[Dict[str, Any]]:
    """The entries whose words can reach a claim's border at some zoom: an element whose drawn box meets a ring round
    the border, a frame or heading whose title can stand above it into that ring, and the other claims' labels.

    The ring is what keeps this off the board's hot path: a claim holds most of what is near it, and words well
    inside its border or well outside cannot meet a 2 px line at any zoom, so only the few along it are measured.
    """
    ring = 40.0
    outer = (rect[0] - ring, rect[1] - ring, rect[2] + ring, rect[3] + ring)
    inner = (rect[0] + ring, rect[1] + ring, rect[2] - ring, rect[3] - ring)
    cache = env.setdefault("_claim_entries", {})
    found: List[Dict[str, Any]] = []

    def meets(box: Sequence[float]) -> bool:
        if box[2] < outer[0] or box[0] > outer[2] or box[3] < outer[1] or box[1] > outer[3]:
            return False
        return not (inner[0] < inner[2] and inner[1] < inner[3] and box[0] > inner[0] and box[2] < inner[2]
                    and box[1] > inner[1] and box[3] < inner[3])

    for el in (env.get("by_id") or {}).values():
        if not isinstance(el, dict) or el.get("type") == "comment":
            continue
        box = _geo.drawn_bounds(dict(el))
        if el.get("type") in ("frame", "heading") and str(el.get("text") or "").strip():
            # A zoomed title stands above the frame and may run past its right edge: reach up and right for it.
            box = (box[0], box[1] - CLAIM_TEXT_REACH, box[2] + CLAIM_TEXT_REACH, box[3])
        if not meets(box):
            continue
        key = el.get("id")
        if key not in cache:
            cache[key] = entry(el, env)
        found.append(cache[key])
    for overlay in env.get("_overlay_words") or []:
        bbox = overlay.get("bbox")
        # A proposal's pill hangs above its box at a fixed screen size: reach up and right for it, like a title.
        if isinstance(bbox, list) and len(bbox) == 4 and meets((bbox[0], bbox[1] - CLAIM_TEXT_REACH, bbox[2] + CLAIM_TEXT_REACH, bbox[3])):
            found.append(overlay)
    placed = env.get("_claim_label_items") or {}
    for other in env.get("claims") or []:
        region = other.get("region")
        if other is claim or other.get("id") == claim.get("id") or not isinstance(region, list) or len(region) != 4:
            continue
        ox0, oy0, ox1, oy1 = (num(v, 0.0) for v in region)
        if other.get("id") in placed:
            # Hung from whichever corner it was placed at, per band (``_claim_labels``): measured as it draws.
            if placed[other["id"]] and meets((ox0 - CLAIM_TEXT_REACH * 4, oy0 - CLAIM_TEXT_REACH, ox1 + CLAIM_TEXT_REACH * 4,
                                              oy1 + CLAIM_TEXT_REACH)):
                found.append({"id": "label:" + str(other.get("id")), "items": placed[other["id"]]})
        elif meets((ox0, oy0 - CLAIM_TEXT_REACH, ox0 + CLAIM_TEXT_REACH * 4, oy0)):
            found.append({"id": "label:" + str(other.get("id")), "claim_label": other,
                          "items": [_claim_label(other, env, (ox0, oy0, ox1, oy1))]})
    return found


def _scaled(item: Mapping[str, Any]) -> bool:
    """Whether a primitive's words move with the zoom other than by its ``lod``: a zoomed title or a screen group."""
    if item.get("zoom") is not None or item.get("screen") is not None:
        return True
    fallback = item.get("fallback")
    inner = list(item.get("items") or ()) + (list(fallback.get("items") or ()) if isinstance(fallback, dict) else [])
    return any(isinstance(p, dict) and _scaled(p) for p in inner)


def _on_ring(box: Sequence[float], ring: Tuple[Tuple[float, float, float, float], Tuple[float, float, float, float]]) -> bool:
    """Whether a box reaches the band between ``ring``'s outer and inner rectangles: where a border's gap can need it."""
    outer, inner = ring
    if box[2] < outer[0] or box[0] > outer[2] or box[3] < outer[1] or box[1] > outer[3]:
        return False
    return not (box[0] > inner[0] and box[2] < inner[2] and box[1] > inner[1] and box[3] < inner[3])


def _words_at(near_by: Sequence[Dict[str, Any]], u: float, memo: Dict[Any, Any],
              ring: Tuple[Tuple[float, float, float, float], Tuple[float, float, float, float]], env: Env) -> List[Tuple[float, float, float, float]]:
    """``canvas_render.text_boxes`` of ``near_by`` at ``u``, measured once per entry rather than once per band.

    Words in world units only appear and disappear at their ``lod`` edges, so each of an entry's items is measured
    once with its ``lod`` set aside and kept with it; a zoomed title is one ``zoom_box``; only a screen-sized group
    (another claim's label) is walked at each zoom. This is what keeps a claim's border off the display list's bill.
    """
    from herdr_team import canvas_render as _render  # it imports this module: late on purpose

    scale = 1.0 / u if u > 0 else float("inf")
    out: List[Tuple[float, float, float, float]] = []
    for found in near_by:
        key = found.get("id")
        shape = memo.get(key)
        if shape is None:
            fixed: List[Tuple[Optional[List[Any]], Tuple[float, float, float, float]]] = []
            titles: List[Mapping[str, Any]] = []
            labels: List[Tuple[Tuple[float, float], Tuple[float, float, float, float], Optional[Tuple[float, float]]]] = []
            other = found.get("claim_label")
            if other is not None:
                # Another claim's label hangs from its region's corner in screen pixels: measured once at one pixel
                # per unit, it is that corner plus the same offsets times ``u`` at any zoom, cut by its own stop.
                ox0, oy0 = num(other["region"][0], 0.0), num(other["region"][1], 0.0)
                right = _claim_neighbour(other, tuple(num(v, 0.0) for v in other["region"]), env)  # type: ignore[arg-type]
                bare = _claim_label(dict(other, region=[ox0, oy0, ox0 + 1e9, oy0 + 1.0]), {**env, "claims": []},
                                    (ox0, oy0, ox0 + 1e9, oy0 + 1.0))
                for box in _render.text_boxes({"entries": [{"items": [bare]}]}, 1.0):
                    labels.append(((ox0, oy0), (box[0] - ox0, box[1] - oy0, box[2] - ox0, box[3] - oy0),
                                   (ox0, right - CLAIM_LABEL_GAP) if right is not None else None))
            for item in [] if other is not None else found.get("items") or ():
                if not isinstance(item, dict):
                    continue
                if item.get("k") == "text" and isinstance(item.get("zoom"), dict):
                    titles.append(item)
                elif not _scaled(item):
                    lod = item.get("lod") if isinstance(item.get("lod"), list) else None
                    bare_item = {k: v for k, v in item.items() if k != "lod"}
                    fixed += [(lod, box) for box in _render.text_boxes({"entries": [{"items": [bare_item]}]}, 1.0)
                              if _on_ring(box, ring)]
                else:
                    titles.append(item)  # screen-sized words inside a mark: walked at each zoom below
            shape = memo[key] = (fixed, titles, labels)
        fixed, titles, labels = shape
        for lod, box in fixed:
            if lod is not None:
                low, high = (lod + [None, None])[:2]
                if (isinstance(low, (int, float)) and not scale >= low) or (isinstance(high, (int, float)) and not scale < high):
                    continue
            out.append(box)
        for prim in titles:
            if prim.get("k") == "text" and isinstance(prim.get("zoom"), dict):
                box = zoom_box(prim, u)
                if box is not None:
                    out.append(box)
            else:
                out.extend(_render.text_boxes({"entries": [{"items": [prim]}]}, u))
        for (ax, ay), (dx0, dy0, dx1, dy1), stop in labels:
            box = (ax + dx0 * u, ay + dy0 * u, ax + dx1 * u, ay + dy1 * u)
            if stop is not None:
                box = (max(box[0], stop[0]), box[1], min(box[2], stop[1]), box[3])
            if box[2] > box[0]:
                out.append(box)
    return out


def _claim_border(rect: Sequence[float], words: Sequence[Sequence[float]], u: float) -> Optional[str]:
    """The claim's border as path data with a gap wherever a line of text sits on it, or None when nothing does.

    A legend in a fieldset: the line stops short of the words and starts again past them, so the words read whole and
    the region still reads as a rectangle. Each side is a straight run; a line of text is on a side when its box
    reaches into the stroke there (half the 2 px stroke and ``CLAIM_TEXT_GAP_PX`` either side, at ``u``).
    """
    x0, y0, x1, y1 = rect
    clear = (1.0 + CLAIM_TEXT_GAP_PX) * u
    sides = (("h", y0, x0, x1), ("v", x1, y0, y1), ("h", y1, x1, x0), ("v", x0, y1, y0))
    runs: List[str] = []
    cut = False
    for axis, at, start, stop in sides:
        lo, hi = min(start, stop), max(start, stop)
        gaps = []
        for t in words:
            across = (t[1], t[3]) if axis == "h" else (t[0], t[2])
            along = (t[0], t[2]) if axis == "h" else (t[1], t[3])
            if across[0] < at + clear and across[1] > at - clear and along[1] > lo and along[0] < hi:
                gaps.append((along[0] - clear, along[1] + clear))
        pieces = [(lo, hi)]
        for g0, g1 in sorted(gaps):
            nxt = []
            for p0, p1 in pieces:
                if g1 <= p0 or g0 >= p1:
                    nxt.append((p0, p1))
                    continue
                if g0 > p0:
                    nxt.append((p0, g0))
                if g1 < p1:
                    nxt.append((g1, p1))
            pieces = nxt
        cut = cut or bool(gaps)
        for p0, p1 in pieces:
            if p1 - p0 <= 0.0:
                continue
            if axis == "h":
                runs.append("M{} {}L{} {}".format(fmt(p0), fmt(at), fmt(p1), fmt(at)))
            else:
                runs.append("M{} {}L{} {}".format(fmt(at), fmt(p0), fmt(at), fmt(p1)))
    if not cut:
        return None
    # Every side gapped (a claim smaller than the words it sits on): nothing to draw. A zero-length run here was a dot
    # in the middle of the words with a round cap, and resvg logged "path dashing failed" on it (QA round 2).
    return "".join(runs)


def _claim_entry(claim: Mapping[str, Any], env: Env) -> Optional[Dict[str, Any]]:
    region = claim.get("region")
    if not isinstance(region, list) or len(region) != 4:
        return None
    x0, y0, x1, y1 = (num(v, 0.0) for v in region)
    author = str(claim.get("author") or "")
    found = chip(author, env)
    rect = (x0 - CLAIM_BORDER_OUT, y0 - CLAIM_BORDER_OUT, x1 + CLAIM_BORDER_OUT, y1 + CLAIM_BORDER_OUT)
    paint = {"fill": None, "stroke": found["bg"], "sw_px": 2, "dash": [8, 6]}
    whole = dict({"k": "rect", "x": rect[0], "y": rect[1], "w": rect[2] - rect[0], "h": rect[3] - rect[1]}, **paint)
    # Per band of zoom, the border as it must be drawn there; consecutive bands that draw it the same are one.
    near_by = _claim_near(claim, env, rect)
    memo: Dict[Any, Any] = {}
    # Words in world units matter only where a gap can reach: within the widest gap of the furthest band of a side.
    reach = (1.0 + CLAIM_TEXT_GAP_PX) * CLAIM_BAND_STEP / CLAIM_BAND_FLOOR
    ring = ((rect[0] - reach, rect[1] - reach, rect[2] + reach, rect[3] + reach),
            (rect[0] + reach, rect[1] + reach, rect[2] - reach, rect[3] - reach))
    drawn: List[Tuple[Optional[float], Optional[float], Optional[str]]] = []
    for low, high in _claim_bands() if near_by else [(None, None)]:
        far, near = _band_ends(low, high)
        # Both ends of the band: the far end draws every zoomed title at its widest and the stroke at its widest in
        # units, the near end draws the words a closer zoom shows that the far end leaves out.
        words = (_words_at(near_by, far, memo, ring, env) + _words_at(near_by, near, memo, ring, env)) if near_by else []
        path = _claim_border(rect, words, far)
        if drawn and drawn[-1][2] == path:
            drawn[-1] = (low, drawn[-1][1], path)
        else:
            drawn.append((low, high, path))
    items: List[Dict[str, Any]] = []
    for low, high, path in drawn:
        if path == "":
            continue  # at this zoom the words cover the whole border: it is not drawn (``_claim_border``)
        prim = dict(whole) if path is None else dict({"k": "path", "d": path}, **paint)
        if len(drawn) > 1:
            prim["lod"] = [low, high]
        items.append(prim)
    placed = env.get("_claim_label_items") or {}
    items.extend(placed[claim.get("id")] if claim.get("id") in placed else _claim_labels(claim, env, (x0, y0, x1, y1)))
    out = {"id": str(claim.get("id") or ""), "kind": "claim", "layer": "overlays", "z": -1, "v": 0,
           "bbox": [x0, y0, x1, y1], "hit": {"shape": "none"}, "handles": "none", "connect": False, "edit": None,
           "frame": None, "author": author, "chip": found, "locked": False, "items": items}
    if isinstance(claim.get("expires_at"), str):
        out["until"] = claim["expires_at"]
    return _rounded(out)


def _lock_entry(lock: Mapping[str, Any], env: Env) -> Optional[Dict[str, Any]]:
    region = lock.get("region")
    if not isinstance(region, list) or len(region) != 4:
        return None
    x0, y0, x1, y1 = (num(v, 0.0) for v in region)
    text = "{} locked: {}".format(lock.get("id"), lock.get("label") or "hands off")[:120]
    items = [{"k": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "fill": {"hatch": "tone.neutral.solid"}, "op": 0.45,
              "stroke": "tone.neutral.solid", "sw_px": 2},
             {"k": "group", "screen": [x0, y1], "items": [text_prim([text], 4, -6 - _ctext.baseline(12, "normal", 400), 12, {}, INK, "start",
                                                                    None, 400)]}]
    by = str(lock.get("by") or "human")
    return _rounded({"id": str(lock.get("id") or ""), "kind": "lock", "layer": "overlays", "z": -2, "v": 0,
                     "bbox": [x0, y0, x1, y1], "hit": {"shape": "none"}, "handles": "none", "connect": False, "edit": None,
                     "frame": None, "author": by, "chip": chip(by, env), "locked": False, "items": items})


def _id_number(value: Any) -> int:
    match = _ID_NUMBER.match(str(value or ""))
    return int(match.group(1)) if match else 0


def order_key(item: Mapping[str, Any]) -> Tuple[int, int, int, str]:
    """Render order (1.1): layer, then ``z``, then the id's number (then the id)."""
    return LAYER_ORDER.get(str(item.get("layer")), 1), int(num(item.get("z"), 0.0)), _id_number(item.get("id")), str(item.get("id"))


def environment(scene: Mapping[str, Any], reader: Optional[str] = None, stills: Optional[Set[str]] = None) -> Env:
    """What entries are drawn against: authors (chips), locks (``locked``), the reader, and the stills that exist."""
    elements = [el for el in scene.get("elements") or [] if isinstance(el, dict)]
    return {"authors": scene.get("authors") if isinstance(scene.get("authors"), dict) else {},
            "locks": [lock for lock in scene.get("locks") or [] if isinstance(lock, dict)],
            "claims": [claim for claim in scene.get("claims") or [] if isinstance(claim, dict)],
            "reader": reader, "by_id": {el.get("id"): el for el in elements}, "stills": stills}


def entries(scene: Mapping[str, Any], ids: Optional[Iterable[str]] = None, *, reader: Optional[str] = None,
            env: Optional[Env] = None, stills: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """The entries of the given ids (``K-`` ids are claims, ``X-`` locks and then freezes, ``P-`` proposals), or of
    everything, in render order. Canvas v2 phase 5: an element a freeze covers says ``frozen: true``, one an open
    proposal aims at says ``pending: [P-n]``, and each freeze and open proposal is an overlay entry (``canvas_collab``)."""
    env = env if env is not None else environment(scene, reader, stills)
    wanted = set(ids) if ids is not None else None
    out: List[Dict[str, Any]] = []
    collab = None
    frozen: Dict[str, str] = {}
    pending: Dict[str, List[str]] = {}
    if scene.get("freezes") or any(isinstance(p, dict) and p.get("status") == "open" for p in scene.get("proposals") or []):
        from herdr_team import canvas_collab as collab  # imports canvas, which imports this module: late on purpose

        frozen, pending = collab.element_flags(scene)
    for el in scene.get("elements") or []:
        if isinstance(el, dict) and (wanted is None or el.get("id") in wanted):
            found = entry(el, env)
            if found["id"] in frozen:
                found["frozen"] = True
            if found["id"] in pending:
                found["pending"] = list(pending[found["id"]])
            out.append(found)
    for lock in scene.get("locks") or []:
        if isinstance(lock, dict) and (wanted is None or lock.get("id") in wanted):
            found = _lock_entry(lock, env)
            if found is not None:
                out.append(found)
    if wanted is None:
        # The claims below measure the words along their borders from these same entries (``_claim_near``).
        env.setdefault("_claim_entries", {}).update((e["id"], e) for e in out if e.get("id"))
    # Made before the claims so a claim's border and label keep off a proposal's pill too (QA round 2: claim K-1's
    # corner ran over the end of proposal P-1's pill on the collab board).
    overlays = collab.display_entries(scene, env, wanted) if collab is not None else []
    env["_overlay_words"] = [e for e in overlays if isinstance(e, dict) and e.get("kind") == "proposal"]
    # Every claim's label is placed before any border is fitted, so a border breaks for the other labels where they
    # are drawn, not where the first corner would have put them.
    placed = env.setdefault("_claim_label_items", {})
    for claim in scene.get("claims") or []:
        region = claim.get("region") if isinstance(claim, dict) else None
        if isinstance(region, list) and len(region) == 4 and claim.get("id") not in placed:
            placed[claim.get("id")] = _claim_labels(claim, env, tuple(num(v, 0.0) for v in region))  # type: ignore[arg-type]
    for claim in scene.get("claims") or []:
        if isinstance(claim, dict) and (wanted is None or claim.get("id") in wanted):
            found = _claim_entry(claim, env)
            if found is not None:
                out.append(found)
    out.extend(overlays)
    out.sort(key=order_key)
    return out


def frame_titles(items: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """The zoomed text primitives (a title above a frame) among an entry's items."""
    return [item for item in items if item.get("k") == "text" and isinstance(item.get("zoom"), dict)]


def zoom_box(prim: Mapping[str, Any], u: float) -> Optional[Box]:
    """Where a zoomed text (1.6) draws at ``u`` units per pixel, when its ``lod`` shows it there."""
    scale = 1.0 / u if u > 0 else float("inf")
    if not lod_visible(prim, scale):
        return None
    size = num(prim.get("size"), 16.0) or 16.0
    zoom = prim.get("zoom") or {}
    size_eff = max(size, num(zoom.get("min_px"), 0.0) / scale)
    lh_eff = (num(prim.get("lh"), size * 1.25) / size) * size_eff
    lines = prim.get("lines") or []
    bottom = num(zoom.get("bottom"), 0.0)
    widest = max((num(line.get("w"), 0.0) for line in lines), default=0.0) * size_eff / size
    x = num(prim.get("x"), 0.0)
    return x, bottom - len(lines) * lh_eff, x + widest, bottom


def lod_visible(prim: Mapping[str, Any], scale: float) -> bool:
    """``lod: [min, max]``: drawn only when ``min <= scale < max`` (either end may be null)."""
    lod = prim.get("lod")
    if not isinstance(lod, list) or len(lod) != 2:
        return True
    low, high = lod
    if isinstance(low, (int, float)) and not scale >= low:
        return False
    if isinstance(high, (int, float)) and not scale < high:
        return False
    return True


def bbox_of(found: Sequence[Mapping[str, Any]], max_px: int = _geo.DEFAULT_MAX_PX) -> Box:
    """The document ``bbox`` (1.1): every entry's bbox padded by 40, grown until it holds every frame title and every
    claim's label as a picture of it at ``max_px`` draws them (the settling rule of ``canvas_geometry.view_box``).

    A claim's label hangs outside its region at a fixed screen size, and on a whole-board picture the only corner
    clear of words is often outside the board: without the room, the label was cut in half at the picture's top
    edge, or, moved below the region to keep off a frame title (layout findings N4), left out of the picture."""
    from herdr_team import canvas_render as _render  # it imports this module: late on purpose

    boxes = [tuple(e["bbox"]) for e in found if isinstance(e.get("bbox"), list) and len(e["bbox"]) == 4]
    titles = [prim for e in found for prim in frame_titles(e.get("items") or [])]
    claims = {"entries": [{"items": [p for p in e.get("items") or () if isinstance(p, dict) and p.get("k") == "group"]}
                          for e in found if e.get("kind") == "claim"]}
    if not boxes:
        return -_geo.PADDING, -_geo.PADDING, 400.0 + _geo.PADDING, 300.0 + _geo.PADDING

    def padded(more: Sequence[Sequence[float]]) -> Box:
        return (min(b[0] for b in more) - _geo.PADDING, min(b[1] for b in more) - _geo.PADDING,
                max(b[2] for b in more) + _geo.PADDING, max(b[3] for b in more) + _geo.PADDING)

    box = padded(boxes)
    labels: List[Tuple[float, float, float, float]] = []
    for _round in range(4):
        if not titles and not claims["entries"]:
            break
        u = _geo.units_per_px(box, max_px)
        extra = [b for b in (zoom_box(prim, u) for prim in titles) if b is not None]
        if claims["entries"]:
            # Kept from every round: a label picks its corner by the zoom, and the zoom moves as the box grows, so a
            # label held above the board on one round and below it on the next must find room on both.
            labels += _render.text_boxes(claims, u)
        grown = padded(list(boxes) + extra + labels)
        if grown == box:
            break
        box = grown
    return box


def display_list(scene: Mapping[str, Any], region: Optional[Sequence[float]] = None, *, reader: Optional[str] = None,
                 stills: Optional[Set[str]] = None) -> Dict[str, Any]:
    """The whole display list of a scene (1.1), or of the entries that meet ``region``."""
    found = entries(scene, reader=reader, stills=stills)
    if region is not None:
        x0, y0, x1, y1 = _geo.normalize_region(region)
        found = [e for e in found if e["bbox"][0] < x1 and x0 < e["bbox"][2] and e["bbox"][1] < y1 and y0 < e["bbox"][3]]
    return {
        "dl": DL_VERSION, "generator": GENERATOR, "team": str(scene.get("team") or ""), "version": int(num(scene.get("version"), 0.0)),
        "bbox": [r2(v) for v in bbox_of(found)], "layers": list(LAYERS), "fonts": FONTS,
        "palettes": {theme: _theme.palette(theme) for theme in _theme.THEMES},
        "shadows": {theme: _theme.shadows(theme) for theme in _theme.THEMES},
        "entries": found,
    }


def dumps(doc: Any) -> str:
    """The canonical JSON of a display list (goldens compare this string)."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------
# the executable schema


def _paint_ok(value: Any, palettes: Mapping[str, Mapping[str, str]]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        if set(value) != {"hatch"}:
            return "a paint object is {hatch: paint}"
        return _paint_ok(value["hatch"], palettes)
    if not isinstance(value, str):
        return "a paint is null, #rrggbb, a token reference or {hatch}"
    if value.startswith("#"):
        return None if _HEX.match(value) and value == value.lower() else "colours are #rrggbb in lower case"
    if not _REF.match(value):
        return "{!r} is not a token reference".format(value)
    for theme, palette in palettes.items():
        if value not in palette:
            return "{} does not resolve in the {} palette".format(value, theme)
    return None


def _check_prim(prim: Any, where: str, palettes: Mapping[str, Mapping[str, str]], out: List[str]) -> None:
    if not isinstance(prim, dict) or prim.get("k") not in PRIMITIVES:
        out.append("{}: not a primitive".format(where))
        return
    kind = prim["k"]
    if "lod" in prim and not _lod_ok(prim["lod"]):
        out.append("{}: lod is [min, max]".format(where))
    if "layer" in prim and prim["layer"] not in LAYERS:
        out.append("{}: unknown layer".format(where))
    for key in ("fill", "stroke"):
        if key in prim:
            problem = _paint_ok(prim[key], palettes)
            if problem:
                out.append("{}.{}: {}".format(where, key, problem))
    need = {"rect": ("x", "y", "w", "h"), "ellipse": ("cx", "cy", "rx", "ry"), "image": ("x", "y", "w", "h"),
            "slot": ("x", "y", "w", "h"), "text": ("x", "size", "lh")}.get(kind, ())
    for key in need:
        if not isinstance(prim.get(key), (int, float)) or isinstance(prim.get(key), bool):
            out.append("{}: {} needs a number {}".format(where, kind, key))
    if kind in ("poly", "line") and len(points_of(prim.get("points"))) < 2:
        out.append("{}: {} needs two points".format(where, kind))
    if kind in ("path", "arrow") and not (isinstance(prim.get("d"), str) and _PATH_D.match(prim["d"]) and prim["d"][:1] == "M"):
        out.append("{}: {} d must be absolute M L C Q Z".format(where, kind))
    if kind == "arrow":
        for head in prim.get("heads") or []:
            if not isinstance(head, dict) or head.get("at") not in ("start", "end") or head.get("shape") not in ("chevron", "triangle", "dot"):
                out.append("{}: a head is {{at, shape, ...}}".format(where))
    if kind == "text":
        if prim.get("anchor") not in ("start", "middle", "end") or prim.get("font") not in FONTS:
            out.append("{}: text anchor or font".format(where))
        for line in prim.get("lines") or []:
            if not isinstance(line, dict) or not isinstance(line.get("t"), str) or not isinstance(line.get("y"), (int, float)):
                out.append("{}: a line is {{y, t, w}}".format(where))
    if kind == "image":
        src = prim.get("src")
        if not isinstance(src, dict) or not (isinstance(src.get("asset"), str) or isinstance(src.get("still"), str)):
            out.append("{}: image src is {{asset}} or {{still}}".format(where))
    if kind == "slot":
        _check_slot(prim, where, out)
    for key in ("items", "fallback"):
        for index, child in enumerate(prim.get(key) or []):
            _check_prim(child, "{}.{}[{}]".format(where, key, index), palettes, out)


def _check_slot(prim: Mapping[str, Any], where: str, out: List[str]) -> None:
    """A slot's phases 3 and 4 fields (1.4), each optional: ``ref.doc``, ``views``, ``drawn`` and ``gl``."""
    ref = prim.get("ref")
    if not isinstance(ref, dict) or not isinstance(ref.get("id"), str) or not isinstance(ref.get("v"), int) or isinstance(ref.get("v"), bool):
        out.append("{}: a slot ref is {{id, v}}".format(where))
    elif "doc" in ref and not (isinstance(ref["doc"], str) and _DOC_ASSET.match(ref["doc"])):
        out.append("{}: ref.doc is an asset name (<32 hex>.json or .glb)".format(where))
    still = prim.get("still")
    if still is not None and not (isinstance(still, str) and _STILL.match(still)):
        out.append("{}: still is a still name or null".format(where))
    if "views" in prim:
        views = prim["views"]
        if not isinstance(views, dict) or not views or any(not isinstance(k, str) or not re.match(r"^[a-z]{0,12}\Z", k) for k in views) or \
                any(v is not None and not (isinstance(v, str) and _STILL.match(v)) for v in views.values()):
            out.append("{}: views maps each view to a still name or null".format(where))
        elif still is not None and still not in views.values():
            out.append("{}: still is the primary view's still".format(where))
    for flag in ("drawn", "gl"):
        if flag in prim and prim[flag] is not True:
            out.append("{}: {} is true when present".format(where, flag))
    if prim.get("drawn") is True and not prim.get("fallback"):
        out.append("{}: a drawn slot has its drawing in fallback".format(where))


def _lod_ok(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and all(v is None or (isinstance(v, (int, float)) and not isinstance(v, bool)) for v in value)


def _box_ok(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)


def _check_extras(item: Mapping[str, Any], where: str, out: List[str]) -> None:
    """The optional phase 2 entry fields (6.2): ``parts``, ``pin``, ``block``/``part``, ``container`` and ``tip``."""
    if "parts" in item:
        parts = item["parts"]
        if not isinstance(parts, list):
            out.append("{}: parts is a list".format(where))
        else:
            for index, part in enumerate(parts):
                here = "{}.parts[{}]".format(where, index)
                hit = part.get("hit") if isinstance(part, dict) else None
                if not isinstance(part, dict) or not isinstance(part.get("part"), str) or not part["part"]:
                    out.append(here + ": a part is {part, hit, edit}")
                    continue
                if not isinstance(hit, dict) or hit.get("shape") not in ("rect", "ellipse", "diamond") or not _box_ok(hit.get("box")):
                    out.append(here + ": hit is {shape: rect|ellipse|diamond, box}")
                edit = part.get("edit")
                if edit is not None and (not isinstance(edit, dict) or edit.get("wrap") not in EDIT_WRAPS or edit.get("part") != part["part"]
                                         or not _box_ok(edit.get("box"))):
                    out.append(here + ": edit is an edit object naming its part")
                if "lod" in part and not _lod_ok(part["lod"]):
                    out.append(here + ": lod is [min, max]")
    if "pin" in item and item["pin"] not in ("human", "agent"):
        out.append("{}: pin is human or agent".format(where))
    if ("block" in item) != ("part" in item) or ("block" in item and not (isinstance(item["block"], str) and isinstance(item["part"], str))):
        out.append("{}: block and part come together, as strings".format(where))
    if "container" in item:
        found = item["container"]
        if not isinstance(found, dict) or found.get("layout") not in ("row", "column", "grid") or not isinstance(found.get("order"), list) \
                or not isinstance(found.get("gap"), (int, float)):
            out.append("{}: container is {{layout, gap, order}}".format(where))
    if "tip" in item and (not isinstance(item["tip"], str) or len(item["tip"]) > TIP_MAX):
        out.append("{}: tip is a string of at most {} characters".format(where, TIP_MAX))
    # Canvas v2 phase 5 (4.6, 5.2): what a freeze covers, what proposals aim at, and the proposal and freeze entries.
    if "frozen" in item and item["frozen"] is not True:
        out.append("{}: frozen is true when present".format(where))
    if "pending" in item and (not isinstance(item["pending"], list) or not item["pending"]
                              or not all(isinstance(p, str) and p.startswith("P-") for p in item["pending"])):
        out.append("{}: pending is a list of P- ids".format(where))
    if item.get("kind") == "proposal":
        found = item.get("proposal")
        if not isinstance(found, dict) or not isinstance(found.get("author"), str) or not isinstance(found.get("outdated"), bool) \
                or not all(isinstance(found.get(key), list) for key in ("targets", "created", "deleted", "summary", "base_note", "reasons")):
            out.append("{}: proposal is {{author, intent, reason, reasons, outdated, targets, created, deleted, batch, summary, base_note}}".format(where))
        if not str(item.get("id") or "").startswith("P-"):
            out.append("{}: a proposal's id is P-n".format(where))
    elif "proposal" in item:
        out.append("{}: only a proposal entry has proposal".format(where))
    if item.get("kind") == "freeze":
        found = item.get("freeze")
        if not isinstance(found, dict) or found.get("mode") not in ("propose", "refuse") or \
                (found.get("region") is None) == (found.get("ids") is None):
            out.append("{}: freeze is {{region | ids, label, mode}}".format(where))


def validate(doc: Any) -> List[str]:
    """Every way ``doc`` breaks the display list contract (empty when it is valid)."""
    out: List[str] = []
    if not isinstance(doc, dict) or doc.get("dl") != DL_VERSION:
        return ["not a display list v{}".format(DL_VERSION)]
    palettes = doc.get("palettes") if isinstance(doc.get("palettes"), dict) else {}
    if set(palettes) != set(_theme.THEMES):
        out.append("palettes must hold {}".format(", ".join(_theme.THEMES)))
    listed = doc.get("entries") if isinstance(doc.get("entries"), list) else doc.get("upserts")
    if not isinstance(listed, list):
        return out + ["no entries"]
    seen: Set[str] = set()
    keys = []
    for index, item in enumerate(listed):
        where = "entries[{}]".format(index)
        if not isinstance(item, dict):
            out.append(where + ": not an object")
            continue
        where = item.get("id") or where
        if item.get("id") in seen:
            out.append("{}: id listed twice".format(where))
        seen.add(str(item.get("id")))
        if item.get("layer") not in LAYERS:
            out.append("{}: unknown layer".format(where))
        box = item.get("bbox")
        if not (isinstance(box, list) and len(box) == 4 and box[0] <= box[2] and box[1] <= box[3]):
            out.append("{}: bbox is [x0, y0, x1, y1]".format(where))
        hit = item.get("hit")
        if not isinstance(hit, dict) or hit.get("shape") not in HITS:
            out.append("{}: hit shape".format(where))
        if item.get("handles") not in _kinds.HANDLES:
            out.append("{}: handles".format(where))
        edit = item.get("edit")
        if edit is not None and (not isinstance(edit, dict) or edit.get("wrap") not in EDIT_WRAPS or edit.get("align") not in ("center", "start")):
            out.append("{}: edit".format(where))
        _check_extras(item, where, out)
        for key in ("bg", "fg"):
            problem = _paint_ok((item.get("chip") or {}).get(key), palettes)
            if problem:
                out.append("{}.chip: {}".format(where, problem))
        for n, prim in enumerate(item.get("items") or []):
            _check_prim(prim, "{}.items[{}]".format(where, n), palettes, out)
        keys.append(order_key(item))
    if "entries" in doc and keys != sorted(keys):
        out.append("entries are not in render order")
    return out


# --------------------------------------------------------------------------
# goldens (``--write-goldens`` / ``--check-goldens``)


def golden_scene(path: Path) -> Dict[str, Any]:
    """A golden scene applied the way ``tools/canvas_qa.py`` applies it: the resulting scene, deterministic."""
    sys.path.insert(0, str(REPO / "tools"))
    try:
        import canvas_qa  # noqa: WPS433 - the QA tool owns the throwaway team
    finally:
        sys.path.pop(0)
    doc = canvas_qa.load_scene_file(path)
    with canvas_qa.QaTeam() as qa:
        canvas_qa.apply_scene(qa, doc)
        from herdr_team import canvas  # the scene comes from the real op path; imported late on purpose

        scene = canvas.load_scene(qa.team)
    return normalize_scene(scene)


#: Fixed stand-ins for what differs between runs (clock times), so goldens compare exactly.
_EPOCH = "2026-01-01T00:00:00.000Z"


def normalize_scene(scene: Dict[str, Any]) -> Dict[str, Any]:
    """``scene`` with its clock times fixed (they never reach the display list, but a scene fixture stays stable)."""
    out = dict(scene, updated_at=_EPOCH)
    out["elements"] = [dict(el, created_at=_EPOCH, updated_at=_EPOCH) for el in scene.get("elements") or []]
    out["batches"] = {key: dict(value, at=_EPOCH) for key, value in (scene.get("batches") or {}).items()}
    out["claims"] = []
    # Canvas v2 phase 5: proposals (and the element values they hold), freezes and checkpoints carry clock times too.
    if scene.get("proposals"):
        out["proposals"] = [dict(p, at=_EPOCH, decided_at=_EPOCH if p.get("decided_at") else None,
                                 changes=[dict(c, value=dict(c["value"], created_at=_EPOCH, updated_at=_EPOCH) if isinstance(c.get("value"), dict)
                                               else c.get("value")) for c in p.get("changes") or []])
                            if "changes" in p else dict(p, at=_EPOCH, decided_at=_EPOCH if p.get("decided_at") else None)
                            for p in scene["proposals"]]
    for key in ("freezes", "checkpoints"):
        if scene.get(key):
            out[key] = [dict(item, at=_EPOCH) for item in scene[key]]
    return out


def fmt_vectors() -> List[List[Any]]:
    """``[value, fmt(value)]`` pairs both writers are held to (``fmt-vectors.json``)."""
    values = [0, -0.0, 1, -1, 0.004, 0.005, -0.005, 0.015, 0.025, 0.1, 0.105, 0.125, 1.005, 1.015, 2.675, -2.675, 12.345, 12.3449,
              123.456, 224.775, 999.995, -999.995, 1e-7, -1e-7, 0.5, -0.5, 1.5, 2.5, 10.1, 10.01, 10.001, 100, -100, 1234567.891,
              -1234567.891, 0.3333333, 0.6666667, 1.1, 1.2, 1.3, 33.335, 44.445, 55.555, 66.665, 77.775, 88.885, 99.995, 0.07,
              0.994, 0.995, 0.996, 7.0000001, -7.0000001, 1e6, 3.14159, -3.14159, 1024.0, 548.5, 6.25e-3, -12.5]
    return [[value, fmt(value)] for value in values]


def _golden_paths(name: str) -> Dict[str, Path]:
    return {"scene": GOLDENS_DIR / "scenes" / (name + ".json"), "dl": GOLDENS_DIR / (name + ".json"),
            "light": GOLDENS_DIR / (name + ".light.svg"), "dark": GOLDENS_DIR / (name + ".dark.svg")}


def golden_outputs(scene: Mapping[str, Any]) -> Dict[str, str]:
    """What the goldens of one scene hold: the display list and both themes' canonical SVG."""
    from herdr_team import canvas_svg  # canvas_svg imports this module; imported late on purpose

    doc = display_list(scene, stills=set())
    return {"dl": dumps(doc) + "\n", "light": canvas_svg.write(doc, theme="light") + "\n", "dark": canvas_svg.write(doc, theme="dark") + "\n"}


def _goldens(write: bool, only: Sequence[str] = ()) -> int:
    """Write (or check) the goldens; ``only`` names the scenes to rewrite (phase 2, D16: one owner per scene file), and
    the others are left as they are."""
    stale: List[str] = []
    files: Dict[Path, str] = {GOLDENS_DIR / "fmt-vectors.json": json.dumps(fmt_vectors(), indent=0) + "\n"}
    known = {path.stem for path in SCENES_DIR.glob("*.json")}
    unknown = [name for name in only if name not in known]
    if unknown:
        sys.stderr.write("no golden scene called {} (tests/fixtures/canvas_scenes)\n".format(", ".join(unknown)))
        return 2
    for path in sorted(SCENES_DIR.glob("*.json")):
        name = path.stem
        if only and name not in only:
            continue
        paths = _golden_paths(name)
        if write or not paths["scene"].is_file():
            scene = golden_scene(path)
            files[paths["scene"]] = json.dumps(scene, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
        else:
            scene = json.loads(paths["scene"].read_text(encoding="utf-8"))
        outputs = golden_outputs(scene)
        for key in ("dl", "light", "dark"):
            files[paths[key]] = outputs[key]
    for path, text in sorted(files.items()):
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current == text:
            continue
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        stale.append(str(path.relative_to(REPO)))
    if stale and not write:
        sys.stderr.write("stale display-list goldens (run python3 -m herdr_team.canvas_display --write-goldens):\n  "
                         + "\n  ".join(stale) + "\n")
        return 1
    for name in stale if write else ():
        sys.stdout.write("wrote {}\n".format(name))
    return 0


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m herdr_team.canvas_display", description="Print a scene's display list, or write its goldens.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--scene", help="a scene.json (or a golden scene file) to print the display list of")
    mode.add_argument("--write-goldens", nargs="*", metavar="SCENE", default=None,
                      help="rewrite tests/fixtures/display from the golden scenes (only the named ones when given)")
    mode.add_argument("--check-goldens", action="store_true", help="exit 1 when tests/fixtures/display is stale")
    parser.add_argument("--out", help="write the display list here instead of printing it")
    args = parser.parse_args(list(argv))
    if args.write_goldens is not None or args.check_goldens:
        return _goldens(args.write_goldens is not None, args.write_goldens or ())
    if not args.scene:
        parser.print_usage(sys.stderr)
        return 2
    path = Path(args.scene)
    raw = json.loads(path.read_text(encoding="utf-8"))
    scene = golden_scene(path) if isinstance(raw, dict) and ("ops" in raw or "batches" in raw) else raw
    text = dumps(display_list(scene)) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
