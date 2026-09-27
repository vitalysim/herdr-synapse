"""The canvas text engine (canvas v2 foundation): measure, wrap, and fit labels to their boxes.

Every width here comes from the advance widths of the bundled fonts
(``assets/fonts/font-metrics.json``, written by ``canvas_fontgen``), the
same files the page and resvg draw with, so the server, the page and the
agent's PNG agree on where a line breaks. Contract:
``.local/prd/canvas-v2-architecture.md`` section 2.

* ``measure`` is one line's width: the sum of its advances times
  ``size / units_per_em`` times ``SAFETY`` (kerning, hinting and contextual
  alternates are not modelled; ``SAFETY`` covers them). A codepoint the face
  lacks is estimated (combining marks and joiners 0, East Asian wide 1 em,
  emoji 1.25 em, anything else 0.75 em) and the result says so.
* ``wrap`` breaks greedily at spaces, then after ``/ _ . - :`` inside a long
  token, then between East Asian wide characters, and only then mid-word.
* ``fit`` sizes a box for its text under a named policy (``hug``,
  ``shrink``, ``scale_shape``, ``clamp``; more with ``register_fit``). The
  invariant ``fits`` holds for every result: each line is no wider than the
  inner box, and the lines are no taller than it.

Pure apart from the cached ``load``; the standard library only. When the
metrics file is missing or unreadable a conservative built-in face (0.62 em
per character) is used, every result is flagged ``estimated``, and one
warning is logged: the canvas never fails because a font file went missing.
"""
from __future__ import annotations

import json
import logging
import math
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

_log = logging.getLogger(__name__)

METRICS_PATH = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "font-metrics.json"

#: Line height in font sizes: the page adapter's ``LINE_HEIGHT`` and the model's.
LINE_HEIGHT = 1.25
#: Every measured width is multiplied by this: room for kerning, hinting and ``calt`` (``->`` as an arrow glyph).
SAFETY = 1.03
#: The weight canvas labels are measured and drawn in. The Phase 0 page draws one weight (Inter Medium,
#: design spec 9.2); it is also wider than Regular, so a page that draws 400 still fits.
DEFAULT_WEIGHT = 500
#: Legacy ``font: hand`` (Excalifont on the page) is measured as Inter Medium times this, flagged estimated.
#: Calibrated in Chrome 153 over ``tests/fixtures/canvas-text-corpus.json`` on 2026-09-27: Excalifont is
#: 0.78 to 1.131 times Inter's advances (median 1.00; the widest is a run of brackets), rounded up.
HAND_SCALE = 1.14
#: ``font: code`` advance per character in em: the wider of Geist Mono (0.6, resvg) and Cascadia Code
#: (1200/2048, the Phase 0 page), so code fits whichever of the two draws it.
MONO_ADVANCE_EM = 0.6
#: The built-in face's advance when the metrics file cannot be read.
FALLBACK_EM = 0.62
#: Estimates for a codepoint the face lacks, in em.
WIDE_EM, EMOJI_EM, OTHER_EM = 1.0, 1.25, 0.75
#: Where a long token may break (after one of these), before a hard break mid-word.
SOFT_BREAKS = "/_.-:"
ELLIPSIS = "…"
#: The smallest size ``shrink`` steps down to (design tokens ``type.shrink_floor``).
MIN_SIZE = 14
#: The largest box a fit may produce (``canvas.MAX_SIZE``); past it a word is broken instead.
MAX_BOX = 20000
#: The widest a single unbreakable piece (a URL, a hash, an identifier) widens a label's inner box, in ems
#: of its size: past it the piece breaks mid-word. Without it one 135-character word made a box 1480 wide
#: and an ellipse 2160 x 1080 (QA F-11); 24 em is 480 at the default size 20, 864 at ``xl``.
WORD_MAX_EMS = 24
#: A hugging label whose lines stand this many times taller than they are wide widens (a paragraph made a
#: box 320 x 2080), up to ``PARAGRAPH_MAX_EMS`` ems, towards a 4:3 block.
TOWER_RATIO = 1.5
PARAGRAPH_MAX_EMS = 40
#: Float slack when comparing widths (sums of advances are not exact).
EPS = 1e-6

FONT_ALIASES = {"normal": "sans", "sans": "sans", "hand": "sans", "code": "mono", "mono": "mono"}


# --------------------------------------------------------------------------
# faces and metrics


@dataclass(frozen=True)
class Face:
    """One font face's metrics; ``advance`` in em."""

    key: str
    family: str
    file: str
    weight: int
    italic: bool
    units_per_em: int
    ascender: int
    descender: int
    line_gap: int
    default_advance: int
    advances: Dict[int, int] = field(default_factory=dict, repr=False, compare=False)
    #: The built-in face: no table, every advance is ``FALLBACK_EM``.
    builtin: bool = False

    def advance(self, cp: int) -> Optional[float]:
        """The advance of ``cp`` in em, or None when the face lacks it."""
        if self.builtin:
            return FALLBACK_EM
        units = self.advances.get(cp)
        return None if units is None else units / float(self.units_per_em)


@dataclass(frozen=True)
class Metrics:
    faces: Dict[str, Face]
    #: True when the metrics file could not be read and the built-in face stands in.
    estimated: bool = False


#: Test hook: metrics to use in place of the file (like ``canvas_render.FONT_DIRS``).
METRICS: Optional[Metrics] = None
_CACHE: Dict[str, Metrics] = {}


def _builtin() -> Metrics:
    faces = {}
    for key, family, weight in (("sans-400", "Inter", 400), ("sans-500", "Inter", 500), ("sans-600", "Inter", 600),
                                ("sans-700", "Inter", 700), ("mono-400", "Geist Mono", 400)):
        faces[key] = Face(key, family, "", weight, False, 1000, 969, -242, 0, 620, builtin=True)
    return Metrics(faces, estimated=True)


def _face(key: str, raw: Dict) -> Face:
    advances: Dict[int, int] = {}
    for first, widths in raw["advances"]:
        for offset, width in enumerate(widths):
            advances[int(first) + offset] = int(width)
    return Face(key, str(raw.get("family") or ""), str(raw.get("file") or ""), int(raw.get("weight") or 400), bool(raw.get("italic")),
                int(raw["units_per_em"]), int(raw["ascender"]), int(raw["descender"]), int(raw.get("line_gap") or 0),
                int(raw.get("default_advance") or 0), advances)


def load(path: Optional[Path] = None) -> Metrics:
    """The font metrics (cached); the built-in estimate, with one warning, when the file is missing or broken. Never raises."""
    if METRICS is not None and path is None:
        return METRICS
    source = Path(path) if path is not None else METRICS_PATH
    cached = _CACHE.get(str(source))
    if cached is not None:
        return cached
    try:
        doc = json.loads(source.read_text(encoding="utf-8"))
        metrics = Metrics({key: _face(key, raw) for key, raw in doc["faces"].items()})
        if not metrics.faces:
            raise ValueError("no faces")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
        _log.warning("canvas font metrics unreadable (%s): %s; measuring with a %.2f em estimate", source, err, FALLBACK_EM)
        metrics = _builtin()
    _CACHE[str(source)] = metrics
    return metrics


def face(font: str = "normal", weight: int = DEFAULT_WEIGHT) -> Face:
    """The face that measures ``font`` (``normal``/``sans``, ``code``/``mono``, ``hand``) at the nearest weight."""
    family = FONT_ALIASES.get(str(font or "normal"), "sans")
    metrics = load()
    if not _EM_SOURCE or _EM_SOURCE[0] is not metrics:
        for cache in (_EM_CACHE, _RUN_CACHE, _FACE_CACHE, _NATURAL_CACHE):
            cache.clear()
        _EM_SOURCE[:] = [metrics]
    key = (family, int(weight or 400))
    found = _FACE_CACHE.get(key)
    if found is None:
        candidates = [f for f in metrics.faces.values() if f.key.startswith(family + "-")] or list(metrics.faces.values())
        found = _FACE_CACHE[key] = min(candidates, key=lambda f: (abs(f.weight - key[1]), -f.weight))
    return found


# --------------------------------------------------------------------------
# measuring


@dataclass(frozen=True)
class Measure:
    width: float
    estimated: bool


def _zero_width(ch: str) -> bool:
    cp = ord(ch)
    return (unicodedata.category(ch) in ("Mn", "Me", "Cf") or 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF)


def _wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


def _emoji(ch: str) -> bool:
    cp = ord(ch)
    return cp >= 0x1F000 or 0x1F1E6 <= cp <= 0x1F1FF or (cp >= 0x2000 and unicodedata.category(ch) == "So")


def _estimate(ch: str) -> float:
    if _zero_width(ch):
        return 0.0
    if _emoji(ch):
        return EMOJI_EM
    if _wide(ch):
        return WIDE_EM
    return OTHER_EM


#: Advances already looked up, per character and per run of text, and the face per (family, weight), for
#: the metrics in ``_EM_SOURCE`` (all cleared when they change). Wrapping measures the same words again
#: and again (every step of ``scale_shape``), so this is what keeps fitting fast.
_EM_CACHE: Dict[Tuple[str, str, str], Tuple[float, bool]] = {}
_RUN_CACHE: Dict[Tuple[str, str, str], Tuple[float, bool]] = {}
_FACE_CACHE: Dict[Tuple[str, int], "Face"] = {}
_EM_SOURCE: List[Metrics] = []
CACHE_MAX = 200000


def _char_em(fc: Face, font: str, ch: str) -> Tuple[float, bool]:
    """One character's advance in em for ``font`` measured with face ``fc``, and whether it is an estimate."""
    key = (fc.key, font, ch)
    hit = _EM_CACHE.get(key)
    if hit is not None:
        return hit
    if ch == "\t":
        em, estimated = 4 * _char_em(fc, font, " ")[0], False
    elif fc.builtin:
        em, estimated = (0.0 if _zero_width(ch) else EMOJI_EM if _emoji(ch) else WIDE_EM if _wide(ch) else FALLBACK_EM), True
    else:
        found = fc.advance(ord(ch))
        if found is None:
            em, estimated = _estimate(ch), not _zero_width(ch)
        elif font == "mono":
            em, estimated = (0.0 if _zero_width(ch) or found == 0 else max(found, MONO_ADVANCE_EM)), False
        else:
            em, estimated = found, False
    if font == "hand" and em:
        em, estimated = em * HAND_SCALE, True
    if len(_EM_CACHE) > CACHE_MAX:
        _EM_CACHE.clear()
    _EM_CACHE[key] = (em, estimated)
    return em, estimated


def _family(font: str) -> str:
    """``sans``, ``mono`` or ``hand`` (sans advances, scaled)."""
    font = str(font or "normal")
    return "hand" if font == "hand" else FONT_ALIASES.get(font, "sans")


def measure(text: str, font: str = "normal", size: float = 20, weight: int = DEFAULT_WEIGHT) -> Measure:
    """One line's width at ``size`` (a newline counts as nothing; split lines first)."""
    fc = face(font, weight)
    family = _family(font)
    text = str(text or "")
    key = (fc.key, family, text)
    hit = _RUN_CACHE.get(key)
    if hit is None:
        total = 0.0
        estimated = fc.builtin
        for ch in text:
            if ch == "\n":
                continue
            em, guess = _char_em(fc, family, ch)
            total += em
            estimated = estimated or guess
        if len(_RUN_CACHE) > CACHE_MAX:
            _RUN_CACHE.clear()
        hit = _RUN_CACHE[key] = (total, estimated)
    return Measure(hit[0] * float(size) * SAFETY, hit[1])


def _width(text: str, font: str, size: float, weight: int) -> float:
    return measure(text, font, size, weight).width


# --------------------------------------------------------------------------
# line breaking


#: Thai and Lao vowels written as letters of their own that still belong to the syllable: the following
#: ones never start a line, the leading ones never end one (a hard break split "ทำ" before its SARA AM).
_NO_LINE_START = frozenset("\u0e30\u0e32\u0e33\u0e45\u0eb0\u0eb2\u0eb3")
_NO_LINE_END = frozenset("\u0e40\u0e41\u0e42\u0e43\u0e44\u0ec0\u0ec1\u0ec2\u0ec3\u0ec4")


def _joined_to_next(text: str, index: int) -> bool:
    """Whether a break between ``text[index - 1]`` and ``text[index]`` would split one visible character (or syllable)."""
    if index <= 0 or index >= len(text):
        return False
    return (_zero_width(text[index]) or text[index - 1] == "‍" or 0x1F3FB <= ord(text[index]) <= 0x1F3FF
            or text[index] in _NO_LINE_START or text[index - 1] in _NO_LINE_END)


def _soft_break(text: str, index: int) -> bool:
    """Whether a line may break before ``text[index]`` without a hard break (rules 2 and 3)."""
    if _joined_to_next(text, index):
        return False
    before, after = text[index - 1], text[index]
    return before in SOFT_BREAKS or (_wide(before) and not _zero_width(after)) or (_wide(after) and before not in "([{")


def segments(token: str) -> List[str]:
    """``token`` cut at every soft break: the pieces a line never splits without a hard break."""
    out: List[str] = []
    start = 0
    for index in range(1, len(token)):
        if _soft_break(token, index):
            out.append(token[start:index])
            start = index
    out.append(token[start:])
    return out


def _break_token(token: str, max_width: float, font: str, size: float, weight: int) -> Tuple[List[str], bool]:
    """A token wider than the line, cut into pieces that fit: soft breaks first (rightmost that fits), then hard. And whether any was hard."""
    widths = [_width(ch, font, size, weight) for ch in token]
    pieces: List[str] = []
    hard = False
    start = 0
    while start < len(token):
        total = 0.0
        fits = start
        for index in range(start, len(token)):
            total += widths[index]
            if total > max_width + EPS:
                break
            fits = index + 1
        if fits == len(token):
            break
        # The longest prefix that fits (at least one character, never splitting a joined one).
        fits = max(start + 1, fits)
        cut = next((index for index in range(fits, start, -1) if _soft_break(token, index)), 0)
        if not cut:
            hard = True
            cut = fits
            while cut > start + 1 and _joined_to_next(token, cut):
                cut -= 1
            if _joined_to_next(token, cut):
                cut = fits
        pieces.append(token[start:cut])
        start = cut
    pieces.append(token[start:])
    return pieces, hard


def _wrap(text: str, max_width: float, font: str, size: float, weight: int) -> Tuple[List[str], bool]:
    """Greedy lines for ``text`` at ``max_width`` (every ``\\n`` breaks), and whether a word had to be broken mid-word."""
    lines: List[str] = []
    hard = False
    space = _width(" ", font, size, weight)
    for raw in str(text or "").replace("\r\n", "\n").split("\n"):
        line = ""
        line_w = 0.0
        started = False
        for word in raw.split(" "):
            word_w = _width(word, font, size, weight)
            if started and line_w + space + word_w <= max_width + EPS:
                line, line_w = line + " " + word, line_w + space + word_w
                continue
            if started:
                lines.append(line)
            pieces = [word]
            if word_w > max_width + EPS:
                pieces, broke = _break_token(word, max_width, font, size, weight)
                hard = hard or broke
            lines.extend(pieces[:-1])
            line, line_w, started = pieces[-1], _width(pieces[-1], font, size, weight), True
        lines.append(line)
    return lines, hard


def wrap(text: str, max_width: float, font: str = "normal", size: float = 20, weight: int = DEFAULT_WEIGHT) -> List[str]:
    """The lines ``text`` breaks into at ``max_width`` (rules in the module docstring)."""
    return _wrap(text, max(1.0, float(max_width)), font, size, weight)[0]


@dataclass(frozen=True)
class TextBlock:
    lines: Tuple[str, ...]
    width: float
    height: float
    size: float
    estimated: bool


def line_height(size: float) -> float:
    return LINE_HEIGHT * float(size)


def block(text: str, max_width: float, font: str = "normal", size: float = 20, weight: int = DEFAULT_WEIGHT) -> TextBlock:
    """``text`` wrapped at ``max_width``: its lines, widest line, height and whether any width was estimated."""
    lines = wrap(text, max_width, font, size, weight)
    measured = [measure(line, font, size, weight) for line in lines]
    return TextBlock(tuple(lines), max((m.width for m in measured), default=0.0), len(lines) * line_height(size), float(size),
                     any(m.estimated for m in measured))


def baseline(size: float, font: str = "normal", weight: int = DEFAULT_WEIGHT) -> float:
    """The first baseline below a block's top: the half-leading model browsers use, so resvg and the page agree."""
    fc = face(font, weight)
    ascender = fc.ascender / float(fc.units_per_em)
    descender = fc.descender / float(fc.units_per_em)
    return (LINE_HEIGHT - (ascender - descender)) * float(size) / 2.0 + ascender * float(size)


# --------------------------------------------------------------------------
# fit policies

Inset = Callable[[float, float], Tuple[float, float, float, float]]


@dataclass(frozen=True)
class FitRequest:
    """What a fit policy sizes: the text and its font, the box's minimum and maximum, padding and inset."""

    text: str
    font: str = "normal"
    size: float = 20
    weight: int = DEFAULT_WEIGHT
    min_w: float = 1
    min_h: float = 1
    #: ``hug``: the widest the box grows before the text wraps (a single word may still widen it).
    max_w: float = MAX_BOX
    pad_x: float = 0
    pad_y: float = 0
    #: ``(w, h) -> (x, y, w, h)``: the part of a ``w`` x ``h`` shape text may use (None: the whole box).
    inset: Optional[Inset] = None
    #: ``clamp``: lines kept.
    max_lines: int = 0
    min_size: float = MIN_SIZE
    #: A grown side rounds up to a multiple of this (0: no rounding); a side left at its minimum is kept exact.
    snap: float = 0


@dataclass(frozen=True)
class FitResult:
    w: float
    h: float
    size: float
    lines: Tuple[str, ...]
    #: ``(x, y, w, h)`` relative to the box: where the lines go.
    inner: Tuple[float, float, float, float]
    policy: str
    grew: bool = False
    shrunk: bool = False
    truncated: bool = False
    estimated: bool = False
    font: str = "normal"
    weight: int = DEFAULT_WEIGHT


FitPolicy = Callable[[FitRequest], FitResult]


def _inner(request: FitRequest, w: float, h: float) -> Tuple[float, float, float, float]:
    x, y, iw, ih = request.inset(w, h) if request.inset is not None else (0.0, 0.0, w, h)
    return (x + request.pad_x, y + request.pad_y, max(0.0, iw - 2 * request.pad_x), max(0.0, ih - 2 * request.pad_y))


def _snap(value: float, minimum: float, step: float) -> float:
    """``value`` rounded up to the grid when it grew past ``minimum`` (a minimum is kept exactly)."""
    value = float(math.ceil(value - 1e-9))
    if step and value > minimum:
        value = max(minimum, math.ceil(value / step - 1e-9) * step)
    return min(float(MAX_BOX), max(minimum, value))


def _estimated(lines: Sequence[str], request: FitRequest, size: float) -> bool:
    return any(measure(line, request.font, size, request.weight).estimated for line in lines)


def _result(request: FitRequest, policy: str, w: float, h: float, size: float, lines: Sequence[str], **flags: bool) -> FitResult:
    return FitResult(w, h, float(size), tuple(lines), _inner(request, w, h), policy, font=request.font, weight=request.weight,
                     estimated=_estimated(lines, request, size), **flags)


_NATURAL_CACHE: Dict[Tuple[str, str, int], Tuple[float, float]] = {}


def _natural(text: str, request: FitRequest, size: float) -> Tuple[float, float]:
    """The widest hard line unwrapped, and the widest piece no soft break splits (both at ``size``)."""
    key = (str(text), _family(request.font), int(request.weight))
    hit = _NATURAL_CACHE.get(key)
    if hit is None:
        widest = longest = 0.0
        for raw in str(text).split("\n"):
            widest = max(widest, _width(raw, request.font, 1.0, request.weight))
            for word in raw.split(" "):
                for piece in segments(word) if word else ():
                    longest = max(longest, _width(piece, request.font, 1.0, request.weight))
        if len(_NATURAL_CACHE) > 4096:
            _NATURAL_CACHE.clear()
        hit = _NATURAL_CACHE[key] = (widest, longest)
    return hit[0] * float(size), hit[1] * float(size)


def _balanced(text: str, width: float, request: FitRequest, size: float) -> float:
    """The narrowest width at which a single hard line still wraps into two lines (no orphan word), or ``width``."""
    if "\n" in text:
        return width
    words = text.split(" ")
    if len(words) < 3:
        return width
    font, weight = request.font, request.weight
    space = _width(" ", font, size, weight)
    widths = [_width(word, font, size, weight) for word in words]
    best = width
    for cut in range(1, len(words)):
        first = sum(widths[:cut]) + space * (cut - 1)
        second = sum(widths[cut:]) + space * (len(words) - cut - 1)
        candidate = max(first, second)
        if candidate < best - EPS and len(_wrap(text, candidate, font, size, weight)[0]) == 2:
            best = candidate
    return best


def _hug_at(request: FitRequest, size: float, policy: str = "hug", shrunk: bool = False) -> FitResult:
    if request.inset is not None:
        # A shape whose text sits in an inset (an ellipse, a diamond) cannot hug: it grows as a whole.
        return replace(fit_scale_shape(replace(request, size=size)), policy=policy, shrunk=shrunk)
    text = request.text
    if not text:
        return _result(request, policy, max(1.0, request.min_w), max(1.0, request.min_h), size, [], shrunk=shrunk)
    min_inner = max(0.0, request.min_w - 2 * request.pad_x)
    natural, longest = _natural(text, request, size)
    word_cap = word_max_width(size)
    max_inner = max(min_inner, request.max_w - 2 * request.pad_x, min(longest, word_cap, MAX_BOX - 2 * request.pad_x))
    wrap_w = max(min_inner, min(natural, max_inner))
    lines, _hard = _wrap(text, max(wrap_w, 1.0), request.font, size, request.weight)
    if len(lines) * line_height(size) > TOWER_RATIO * wrap_w and natural > wrap_w + EPS:
        # A paragraph: wrap wider, towards a 4:3 block, rather than stand as a tower.
        area = sum(_width(raw, request.font, size, request.weight) for raw in text.split("\n")) * line_height(size)
        wider = min(max(wrap_w, math.sqrt(area * 4.0 / 3.0)), max(wrap_w, PARAGRAPH_MAX_EMS * size), natural)
        if wider > wrap_w + EPS:
            wrap_w = wider
            lines = _wrap(text, wrap_w, request.font, size, request.weight)[0]
    if len(lines) == 2 and wrap_w > min_inner + EPS:
        narrower = max(min_inner, _balanced(text, wrap_w, request, size))
        if narrower < wrap_w - EPS:
            relined = _wrap(text, narrower, request.font, size, request.weight)[0]
            if len(relined) == 2:
                lines = relined
    widest = max((_width(line, request.font, size, request.weight) for line in lines), default=0.0)
    w = _snap(max(request.min_w, widest + 2 * request.pad_x), request.min_w, request.snap)
    h = _snap(max(request.min_h, len(lines) * line_height(size) + 2 * request.pad_y), request.min_h, request.snap)
    return _result(request, policy, w, h, size, lines, grew=w > request.min_w or h > request.min_h, shrunk=shrunk)


def fit_hug(request: FitRequest) -> FitResult:
    """Grow the box to the text: wrap at the minimum width, or wider up to ``max_w``; a word wider than that widens it."""
    return _hug_at(request, float(request.size))


def word_max_width(size: float) -> float:
    """The widest a single unbreakable piece widens a label's inner box at ``size`` (``WORD_MAX_EMS``)."""
    return WORD_MAX_EMS * float(size)


def _fits_box(request: FitRequest, w: float, h: float, size: float) -> Optional[List[str]]:
    """The lines when the text fits a ``w`` x ``h`` box at ``size`` with no word broken, else None.

    A box whose inner width already reaches ``word_max_width`` may break a piece wider than that mid-word:
    the only way such a piece fits at all.
    """
    _x, _y, iw, ih = _inner(request, w, h)
    if iw <= 0 or ih <= 0:
        return None
    breaks = iw + EPS >= word_max_width(size)
    if not breaks and _natural(request.text, request, size)[1] > iw + EPS:
        return None  # a piece no soft break splits is wider than the box: it would break mid-word
    lines, hard = _wrap(request.text, iw, request.font, size, request.weight)
    if (hard and not breaks) or len(lines) * line_height(size) > ih + EPS:
        return None
    return lines


def fit_shrink(request: FitRequest) -> FitResult:
    """Keep the minimum box and step the size down by 2 to ``min_size`` until the text fits; then hug at that size."""
    size = float(request.size)
    floor = min(size, float(request.min_size))
    w, h = max(1.0, request.min_w), max(1.0, request.min_h)
    step = size
    while True:
        lines = _fits_box(request, w, h, step)
        if lines is not None:
            return _result(request, "shrink", w, h, step, lines, shrunk=step < size)
        if step <= floor:
            break
        step = max(floor, step - 2)
    return _hug_at(request, floor, "shrink", shrunk=floor < size)


def fit_scale_shape(request: FitRequest) -> FitResult:
    """Grow the shape (aspect kept) until its inset holds the text; start from the area estimate, then 10% steps."""
    size = float(request.size)
    w0, h0 = max(1.0, request.min_w), max(1.0, request.min_h)
    if not request.text:
        return _result(request, "scale_shape", w0, h0, size, [])
    lines = _fits_box(request, w0, h0, size)
    if lines is not None:
        return _result(request, "scale_shape", w0, h0, size, lines)
    _x, _y, iw, ih = _inner(request, w0, h0)
    natural, longest = _natural(request.text, request, size)
    total = sum(_width(raw, request.font, size, request.weight) for raw in request.text.split("\n"))
    area = total * line_height(size)
    scale = 1.0
    if iw > 0 and ih > 0:
        scale = max(1.0, math.sqrt(area / (iw * ih)) * 0.9, min(longest, word_max_width(size)) / iw * 0.9)
    for _step in range(400):
        # Each side stops at the largest box; the other keeps growing (a very flat or tall minimum).
        w, h = min(float(MAX_BOX), w0 * scale), min(float(MAX_BOX), h0 * scale)
        if _fits_box(request, w, h, size) is not None:
            w = _snap(w, w0, request.snap)
            h = _snap(h, h0, request.snap)
            final = _fits_box(request, w, h, size)
            if final is not None:
                return _result(request, "scale_shape", w, h, size, final, grew=True)
        if w >= MAX_BOX and h >= MAX_BOX:
            break
        scale *= 1.1
    # Past the largest shape: break words in the biggest box rather than overflow, and keep what fits.
    w = h = float(MAX_BOX)
    _x, _y, iw, ih = _inner(request, w, h)
    lines = _wrap(request.text, max(1.0, iw), request.font, size, request.weight)[0]
    keep = max(1, int((ih + EPS) // line_height(size)))
    return _result(request, "scale_shape", w, h, size, lines[:keep], grew=True, truncated=len(lines) > keep)


def _ellipsize(line: str, width: float, request: FitRequest, size: float) -> str:
    text = line.rstrip()
    while text and _width(text + ELLIPSIS, request.font, size, request.weight) > width + EPS:
        text = text[:-1].rstrip()
    return text + ELLIPSIS if _width(text + ELLIPSIS, request.font, size, request.weight) <= width + EPS else ""


def fit_clamp(request: FitRequest) -> FitResult:
    """Keep the width, wrap at it, keep ``max_lines`` lines (the last ends with …); the box grows only to hold them."""
    size = float(request.size)
    w, h = max(1.0, request.min_w), max(1.0, request.min_h)
    # Never narrower than the widest single character (the one thing a clamp cannot break).
    widest_char = max((_width(ch, request.font, size, request.weight) for ch in set(request.text) if ch != "\n"), default=0.0)
    while _inner(request, w, h)[2] + EPS < widest_char and w < MAX_BOX:
        w = min(float(MAX_BOX), math.ceil(w * 1.1 + 1))
    _x, _y, iw, _ih = _inner(request, w, h)
    lines = _wrap(request.text, max(1.0, iw), request.font, size, request.weight)[0] if request.text else []
    keep = max(1, int(request.max_lines or 1))
    truncated = len(lines) > keep
    if truncated:
        lines = lines[:keep]
        lines[-1] = _ellipsize(lines[-1], iw, request, size)
    need = len(lines) * line_height(size)
    while _inner(request, w, h)[3] + EPS < need and h < MAX_BOX:
        h = min(float(MAX_BOX), math.ceil(h * 1.1 + 1))
    h = _snap(h, request.min_h, request.snap)
    return _result(request, "clamp", w, h, size, lines, grew=h > request.min_h or w > request.min_w, truncated=truncated)


def fit_keep(request: FitRequest) -> FitResult:
    """The box as it is, when the text fits it at its size with no word broken; else grown the way its kind
    grows (an inset shape as a whole, anything else by hugging). How ``canvas_check`` and ``canvas_render``
    read an element that is already sized: whether its label fits now, and what it would need."""
    size = float(request.size)
    w, h = max(1.0, request.min_w), max(1.0, request.min_h)
    lines = _fits_box(request, w, h, size) if request.text else []
    if lines is not None:
        return _result(request, "keep", w, h, size, lines)
    return fit_scale_shape(request) if request.inset is not None else _hug_at(request, size)


FIT_POLICIES: Dict[str, FitPolicy] = {"hug": fit_hug, "shrink": fit_shrink, "scale_shape": fit_scale_shape, "clamp": fit_clamp,
                                      "keep": fit_keep}


def register_fit(name: str, policy: FitPolicy) -> None:
    """Add a fit policy; a second one with the same name is a programming error."""
    if name in FIT_POLICIES and FIT_POLICIES[name] is not policy:
        raise ValueError("fit policy {} is registered twice".format(name))
    FIT_POLICIES[name] = policy


def fit(policy: str, request: FitRequest) -> FitResult:
    """Size a box for its text under ``policy`` (an unknown name is a KeyError: a programming error)."""
    return FIT_POLICIES[policy](request)


def fits(result: FitResult) -> bool:
    """The invariant: every line fits the inner width, and all lines fit the inner height."""
    _x, _y, iw, ih = result.inner
    if len(result.lines) * line_height(result.size) > ih + EPS and result.lines:
        return False
    return all(_width(line, result.font, result.size, result.weight) <= iw + EPS for line in result.lines)


def fit_record(result: FitResult, minimum: Sequence[float]) -> Dict[str, object]:
    """The element's ``fit`` field (contract 2.6): what it was sized from and what it draws."""
    size = float(result.size)
    return {"policy": result.policy, "min": [int(round(minimum[0])), int(round(minimum[1]))],
            "size": int(size) if size.is_integer() else round(size, 2), "lines": list(result.lines),
            "truncated": result.truncated, "estimated": result.estimated}
