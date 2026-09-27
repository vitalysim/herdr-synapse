"""The display list as one SVG document (canvas v2 phase 1): the Python half of "one picture".

``write(dl)`` draws a display list (``canvas_display``) the way the page's
``toSVGString`` (``web/src/v2/render/svgString.js`` and ``svgAttrs.js``) does,
byte for byte in canonical mode: the goldens under ``tests/fixtures/display``
hold the two writers equal. The canonical form is specified in
``docs/display-list.md`` ("The canonical SVG"); in short:

* ``<svg xmlns width height viewBox>``, then ``<defs>`` when anything used one
  (elevation filters ``synapse-elev-N`` by level, then hatch patterns
  ``synapse-hatch-K`` and clip paths ``synapse-clip-K`` in order of first use),
  the canvas rectangle, and the four layer groups, each holding one
  ``<g data-id>`` per entry that meets the box and draws something there;
* geometry attributes first, then ``fill stroke [stroke-width
  [stroke-dasharray] stroke-linecap stroke-linejoin] [opacity] [filter]``;
* every number through ``fmt`` (1.7), no whitespace between elements.

Outside canonical mode the agent's picture (``canvas_render.render_svg``) also
rewrites text for resvg (``render_text``: emoji joiners), embeds assets and
stills, names the fonts on the root (so resvg can skip the system fonts), and
adds id badges and the labelled grid, which are not display-list items (D9).

Pure: no I/O (the ``embed`` callback reads files for the caller), and no
import of ``canvas``.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from herdr_team import canvas_display as _dl
from herdr_team import canvas_text as _ctext

fmt = _dl.fmt

SVG_NS = "http://www.w3.org/2000/svg"
CANONICAL_FAMILIES = {"sans": "Inter", "mono": "Geist Mono"}
CANONICAL_URLS = {"asset": lambda name: "synapse-asset:" + name, "still": lambda name: "synapse-still:" + name}
LAYER_NAMES = list(_dl.LAYERS)
EMPTY_BOX = (-40.0, -40.0, 440.0, 340.0)
#: The root's font list in the agent's picture: what resvg falls back through, and what lets it skip the system fonts.
RESVG_ROOT_FONT = "Inter, sans-serif"
DEFAULT_BASE_RATIO = 0.95

Node = Tuple[str, List[Tuple[str, str]], Any]
#: ``embed(src, (x, y, w, h)) -> markup or None``: an asset or still drawn into the picture, or None (draw the fallback).
Embed = Callable[[Mapping[str, Any], Tuple[float, float, float, float]], Optional[str]]

_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}\Z")
_PATH_D = re.compile(r"^[MLCQZ0-9eE.,+\-\s]*\Z")


def _finite(*values: Any) -> bool:
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)


def _is_point(p: Any) -> bool:
    return isinstance(p, (list, tuple)) and len(p) >= 2 and _finite(p[0], p[1])


def escape(value: Any) -> str:
    """Both writers' escaping: XML-invalid control characters dropped, then ``& < > "`` escaped."""
    text = _BAD_XML.sub("", str(value))
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def serialize(node: Node) -> str:
    """A node ``(tag, attrs, children)`` as markup: ``<tag/>`` without children (None), text, or child nodes."""
    if node[0] == "#raw":
        return str(node[2])  # markup an ``embed`` callback made, written as it is
    tag, attrs, kids = node
    out = "<" + tag + "".join(' {}="{}"'.format(name, escape(value)) for name, value in attrs)
    if kids is None:
        return out + "/>"
    if isinstance(kids, str):
        return out + ">" + escape(kids) + "</" + tag + ">"
    return out + ">" + "".join(serialize(kid) for kid in kids) + "</" + tag + ">"


# --------------------------------------------------------------------------
# paint


def _ink(palette: Mapping[str, str]) -> str:
    value = palette.get("base.ink")
    return value.lower() if isinstance(value, str) and _HEX.match(value) else "#1c2024"


def _colour(value: Any, palette: Mapping[str, str]) -> str:
    if not isinstance(value, str):
        return _ink(palette)
    if value.startswith("#"):
        return value.lower() if _HEX.match(value) else _ink(palette)
    found = palette.get(value)
    return found.lower() if isinstance(found, str) and _HEX.match(found) else _ink(palette)


def resolve_paint(value: Any, palette: Mapping[str, str]) -> Union[None, str, Dict[str, str]]:
    """A paint for one theme: None, ``#rrggbb`` or ``{"hatch": "#rrggbb"}``; an unknown reference is ink."""
    if value is None:
        return None
    if isinstance(value, dict):
        if "hatch" in value:
            return {"hatch": _colour(value["hatch"], palette)}
        return _ink(palette)
    return _colour(value, palette)


class Defs:
    """The ``<defs>`` a drawing used, with ids handed out in order of first use (``palette.js`` ``Defs``)."""

    def __init__(self, shadows: Mapping[str, Any]) -> None:
        self.shadows = shadows or {}
        self.elevations: List[int] = []
        self.hatches: Dict[str, str] = {}
        self.clips: Dict[str, str] = {}

    def elevation(self, level: Any) -> Optional[str]:
        if isinstance(level, bool) or not isinstance(level, (int, float)) or level != int(level) or not 1 <= level <= 3:
            return None
        found = self.shadows.get(str(int(level)))
        if not isinstance(found, list) or not found:
            return None
        if int(level) not in self.elevations:
            self.elevations.append(int(level))
        return "synapse-elev-{}".format(int(level))

    def hatch(self, hex_value: str) -> str:
        if hex_value not in self.hatches:
            self.hatches[hex_value] = "synapse-hatch-{}".format(len(self.hatches))
        return self.hatches[hex_value]

    def clip(self, key: str) -> str:
        if key not in self.clips:
            self.clips[key] = "synapse-clip-{}".format(len(self.clips))
        return self.clips[key]

    @property
    def empty(self) -> bool:
        return not (self.elevations or self.hatches or self.clips)

    def nodes(self) -> List[Node]:
        out: List[Node] = []
        for level in sorted(self.elevations):
            shadows = self.shadows.get(str(level)) or []
            prims: List[Node] = []
            for index, shadow in enumerate(shadows):
                prims += [
                    ("feGaussianBlur", [("in", "SourceAlpha"), ("stdDeviation", fmt(_num(shadow.get("blur")) / 2)), ("result", "b{}".format(index))], None),
                    ("feOffset", [("in", "b{}".format(index)), ("dx", fmt(_num(shadow.get("x")))), ("dy", fmt(_num(shadow.get("y")))),
                                  ("result", "o{}".format(index))], None),
                    ("feFlood", [("flood-color", str(shadow.get("color") or "#000000").lower()), ("flood-opacity", fmt(_num(shadow.get("alpha")))),
                                 ("result", "f{}".format(index))], None),
                    ("feComposite", [("in", "f{}".format(index)), ("in2", "o{}".format(index)), ("operator", "in"),
                                     ("result", "s{}".format(index))], None),
                ]
            merge: List[Node] = [("feMergeNode", [("in", "s{}".format(i))], None) for i in range(len(shadows))]
            merge.append(("feMergeNode", [("in", "SourceGraphic")], None))
            prims.append(("feMerge", [], merge))
            out.append(("filter", [("id", "synapse-elev-{}".format(level)), ("x", "-50%"), ("y", "-50%"), ("width", "200%"), ("height", "200%"),
                                   ("color-interpolation-filters", "sRGB")], prims))
        for hex_value, ident in self.hatches.items():
            out.append(("pattern", [("id", ident), ("width", "12"), ("height", "12"), ("patternUnits", "userSpaceOnUse"),
                                    ("patternTransform", "rotate(45)")],
                        [("line", [("x1", "0"), ("y1", "0"), ("x2", "0"), ("y2", "12"), ("stroke", hex_value), ("stroke-width", "3")], None)]))
        for key, ident in self.clips.items():
            x, y, w, h = key.split(" ")
            out.append(("clipPath", [("id", ident)], [("rect", [("x", x), ("y", y), ("width", w), ("height", h)], None)]))
        return out


def _num(value: Any) -> float:
    return float(value) if _finite(value) else 0.0


# --------------------------------------------------------------------------
# primitives -> nodes (``svgAttrs.js``)


class _Ctx:
    def __init__(self, palette: Mapping[str, str], scale: float, families: Mapping[str, str], urls: Mapping[str, Callable[[str], str]],
                 defs: Defs, embed: Optional[Embed], resvg_text: bool) -> None:
        self.palette = palette
        self.scale = scale
        self.families = families
        self.urls = urls
        self.defs = defs
        self.embed = embed
        self.resvg_text = resvg_text

    def url(self, src: Any) -> Optional[str]:
        if not isinstance(src, dict):
            return None
        if isinstance(src.get("asset"), str) and src["asset"] and self.urls.get("asset"):
            return self.urls["asset"](src["asset"])
        if isinstance(src.get("still"), str) and src["still"] and self.urls.get("still"):
            return self.urls["still"](src["still"])
        return None


def _paint_string(resolved: Any, ctx: _Ctx) -> str:
    if resolved is None:
        return "none"
    if isinstance(resolved, dict):
        return "url(#{})".format(ctx.defs.hatch(resolved["hatch"]))
    return resolved


def _opacity(p: Mapping[str, Any]) -> Optional[str]:
    op = p.get("op")
    if not _finite(op) or op >= 1:
        return None
    return fmt(max(0.0, float(op)))


def _stroke_width(p: Mapping[str, Any], ctx: _Ctx) -> str:
    if _finite(p.get("sw_px")):
        return fmt(p["sw_px"] / ctx.scale)
    return fmt(p["sw"] if _finite(p.get("sw")) else 1)


def _stroke_tail(p: Mapping[str, Any], ctx: _Ctx) -> List[Tuple[str, str]]:
    out = [("stroke-width", _stroke_width(p, ctx))]
    found = p.get("dash")
    if isinstance(found, list) and len(found) == 2 and _finite(found[0], found[1]):
        out.append(("stroke-dasharray", "{} {}".format(fmt(found[0]), fmt(found[1]))))
    out += [("stroke-linecap", "round"), ("stroke-linejoin", "round")]
    return out


def _stroke_hex(stroke: Any) -> Optional[str]:
    return stroke["hatch"] if isinstance(stroke, dict) else stroke


def paint_attrs(p: Mapping[str, Any], ctx: _Ctx, fill: bool = True) -> List[Tuple[str, str]]:
    """``fill stroke [stroke-width [stroke-dasharray] stroke-linecap stroke-linejoin] [opacity] [filter]`` (1.8)."""
    attrs = [("fill", _paint_string(resolve_paint(p.get("fill"), ctx.palette), ctx) if fill else "none")]
    stroke = _stroke_hex(resolve_paint(p.get("stroke"), ctx.palette))
    attrs.append(("stroke", stroke or "none"))
    if stroke:
        attrs += _stroke_tail(p, ctx)
    op = _opacity(p)
    if op is not None:
        attrs.append(("opacity", op))
    if p.get("elev"):
        ident = ctx.defs.elevation(p["elev"])
        if ident:
            attrs.append(("filter", "url(#{})".format(ident)))
    return attrs


def lod_visible(p: Mapping[str, Any], scale: float) -> bool:
    lod = p.get("lod")
    if not isinstance(lod, list):
        return True
    low = lod[0] if len(lod) > 0 else None
    high = lod[1] if len(lod) > 1 else None
    if _finite(low) and not scale >= low:
        return False
    if _finite(high) and not scale < high:
        return False
    return True


def text_layout(p: Mapping[str, Any], scale: float) -> Dict[str, Any]:
    """The effective size and baselines of a text at ``scale`` (``lod.js`` ``textLayout``; the zoom rule of 1.6)."""
    size = _num(p.get("size")) if _finite(p.get("size")) else float("nan")
    lines = p.get("lines") if isinstance(p.get("lines"), list) else []
    zoom = p.get("zoom")
    lh_raw = p.get("lh") if _finite(p.get("lh")) and p.get("lh") != 0 else None
    if not isinstance(zoom, dict) or not (size > 0):
        ys = [float(line.get("y")) if isinstance(line, dict) and _finite(line.get("y")) else float("nan") for line in lines]
        return {"size": size, "ys": ys, "zoomed": False}
    min_px = _num(zoom.get("min_px"))
    size_eff = max(size, min_px / scale if scale > 0 else size)
    lh_ratio = (lh_raw if lh_raw is not None else size * 1.25) / size
    base_ratio = float(p["base_ratio"]) if _finite(p.get("base_ratio")) else DEFAULT_BASE_RATIO
    lh_eff = lh_ratio * size_eff
    bottom = _num(zoom.get("bottom"))
    top = bottom - len(lines) * lh_eff
    return {"size": size_eff, "ys": [top + base_ratio * size_eff + i * lh_eff for i in range(len(lines))], "zoomed": True}


def _text_node(p: Mapping[str, Any], ctx: _Ctx) -> Optional[Node]:
    if not _finite(p.get("x")) or not isinstance(p.get("lines"), list):
        return None
    lay = text_layout(p, ctx.scale)
    if not (_finite(lay["size"]) and lay["size"] > 0):
        return None
    fill = resolve_paint(p["fill"] if "fill" in p else "base.ink", ctx.palette)
    anchor = p.get("anchor") if p.get("anchor") in ("start", "middle", "end") else "start"
    attrs = [("font-size", fmt(lay["size"])), ("font-family", ctx.families.get(str(p.get("font"))) or ctx.families["sans"]),
             ("font-weight", fmt(p["weight"] if _finite(p.get("weight")) else 400)), ("fill", _paint_string(fill, ctx)),
             ("text-anchor", anchor)]
    op = _opacity(p)
    if op is not None:
        attrs.append(("opacity", op))
    kids: List[Node] = []
    for index, line in enumerate(p["lines"]):
        y = lay["ys"][index]
        if not line or not isinstance(line, dict) or not _finite(y):
            continue
        # xml:space on each <text>, not on the group: a browser's own style for <text> resets white-space, so an
        # inherited xml:space is ignored there and leading indentation collapses (QA phase 1, finding 4).
        la = [("x", fmt(p["x"])), ("y", fmt(y)), ("xml:space", "preserve")]
        if line.get("dir") == "rtl":
            la.append(("unicode-bidi", "plaintext"))  # browsers mirror text-anchor under direction="rtl", resvg does not
        text = "" if line.get("t") is None else str(line.get("t"))
        if ctx.resvg_text:
            text = _render_text(text)
            if line.get("dir") == "rtl":
                # resvg ignores unicode-bidi and lays every line out with a left-to-right base, so the agent read
                # "אושר?" and mixed Arabic and Japanese in another order than the page drew them (R-8, phase 1 QA #9).
                # An isolate gives the line the right-to-left base the page's plaintext gives it; the anchor stays.
                text = RTL_ISOLATE + text + POP_ISOLATE
        kids.append(("text", la, text))
    return ("g", attrs, kids)


#: resvg shapes a line with each fallback font in turn and gives up on the line when two fonts disagree on its glyph
#: count, which emoji joiners, selectors, keycaps and skin tones make them do; dropping them keeps the base emoji.
_EMOJI_JOINERS = re.compile("[‍⃣︎️\U0001F3FB-\U0001F3FF]")
#: A flag is a pair of regional indicators that only an emoji font joins; drawn as its letters.
_REGIONAL = re.compile("[\U0001F1E6-\U0001F1FF]")


#: RIGHT-TO-LEFT ISOLATE and POP DIRECTIONAL ISOLATE: a right-to-left line's base direction, for resvg (the agent's PNG).
RTL_ISOLATE = "\u2067"
POP_ISOLATE = "\u2069"


def _render_text(value: str) -> str:
    """``value`` as resvg draws it: emoji modifiers dropped, flags as their two letters (the agent's picture only)."""
    value = _EMOJI_JOINERS.sub("", value)
    return _REGIONAL.sub(lambda m: chr(ord(m.group(0)) - 0x1F1E6 + ord("A")), value)


def _image_node(x: float, y: float, w: float, h: float, href: str, p: Optional[Mapping[str, Any]]) -> Node:
    attrs = [("x", fmt(x)), ("y", fmt(y)), ("width", fmt(w)), ("height", fmt(h)), ("preserveAspectRatio", "xMidYMid meet"), ("href", href)]
    op = _opacity(p) if p is not None else None
    if op is not None:
        attrs.append(("opacity", op))
    return ("image", attrs, None)


class _Raw(str):
    """Markup an ``embed`` callback made: written as it is."""


def _head_node(head: Any, stroke: Optional[str], p: Mapping[str, Any], ctx: _Ctx) -> Optional[Node]:
    if not isinstance(head, dict):
        return None
    if head.get("shape") == "dot":
        if not _finite(head.get("cx"), head.get("cy"), head.get("r")):
            return None
        return ("circle", [("cx", fmt(head["cx"])), ("cy", fmt(head["cy"])), ("r", fmt(head["r"])), ("fill", stroke or "none"), ("stroke", "none")], None)
    points = head.get("points")
    if not isinstance(points, list) or len(points) < 2 or not all(_is_point(pt) for pt in points):
        return None
    joined = " ".join("{},{}".format(fmt(pt[0]), fmt(pt[1])) for pt in points)
    if head.get("shape") == "triangle":
        return ("polygon", [("points", joined), ("fill", stroke or "none"), ("stroke", "none")], None)
    attrs = [("points", joined), ("fill", "none"), ("stroke", stroke or "none")]
    if stroke:
        attrs += [("stroke-width", _stroke_width(p, ctx)), ("stroke-linecap", "round"), ("stroke-linejoin", "round")]
    return ("polyline", attrs, None)


def _points_attr(points: Sequence[Sequence[float]]) -> str:
    return " ".join("{},{}".format(fmt(pt[0]), fmt(pt[1])) for pt in points)


def _children(items: Any, ctx: _Ctx) -> List[Node]:
    out: List[Node] = []
    for item in items if isinstance(items, list) else []:
        node = primitive_node(item, ctx)
        if node is not None:
            out.append(node)
    return out


def _embedded(src: Mapping[str, Any], p: Mapping[str, Any], ctx: _Ctx) -> Optional[Node]:
    if ctx.embed is None:
        return None
    markup = ctx.embed(src, (float(p["x"]), float(p["y"]), float(p["w"]), float(p["h"])))
    return ("#raw", [], _Raw(markup)) if markup else None


def primitive_node(p: Any, ctx: _Ctx) -> Optional[Node]:
    """One primitive as a node, or None when it is hidden at this scale, unknown or malformed."""
    if not isinstance(p, dict) or not lod_visible(p, ctx.scale):
        return None
    kind = p.get("k")
    if kind == "rect":
        if not _finite(p.get("x"), p.get("y"), p.get("w"), p.get("h")) or p["w"] < 0 or p["h"] < 0:
            return None
        attrs = [("x", fmt(p["x"])), ("y", fmt(p["y"])), ("width", fmt(p["w"])), ("height", fmt(p["h"]))]
        if _finite(p.get("r")) and fmt(p["r"]) != "0" and p["r"] > 0:
            attrs.append(("rx", fmt(p["r"])))
        return ("rect", attrs + paint_attrs(p, ctx), None)
    if kind == "ellipse":
        if not _finite(p.get("cx"), p.get("cy"), p.get("rx"), p.get("ry")) or p["rx"] < 0 or p["ry"] < 0:
            return None
        return ("ellipse", [("cx", fmt(p["cx"])), ("cy", fmt(p["cy"])), ("rx", fmt(p["rx"])), ("ry", fmt(p["ry"]))] + paint_attrs(p, ctx), None)
    if kind in ("poly", "line"):
        points = p.get("points")
        if not isinstance(points, list) or len(points) < 2 or not all(_is_point(pt) for pt in points):
            return None
        closed = kind == "poly" and bool(p.get("closed"))
        return ("polygon" if closed else "polyline", [("points", _points_attr(points))] + paint_attrs(p, ctx, fill=closed), None)
    if kind == "path":
        if not isinstance(p.get("d"), str) or not _PATH_D.match(p["d"]):
            return None
        attrs = [("d", p["d"])]
        if p.get("rule") == "evenodd":
            attrs.append(("fill-rule", "evenodd"))
        return ("path", attrs + paint_attrs(p, ctx), None)
    if kind == "arrow":
        if not isinstance(p.get("d"), str) or not _PATH_D.match(p["d"]):
            return None
        stroke = _stroke_hex(resolve_paint(p.get("stroke"), ctx.palette))
        shaft = [("d", p["d"]), ("fill", "none"), ("stroke", stroke or "none")]
        if stroke:
            shaft += _stroke_tail(p, ctx)
        nodes: List[Node] = [("path", shaft, None)]
        for head in p.get("heads") if isinstance(p.get("heads"), list) else []:
            node = _head_node(head, stroke, p, ctx)
            if node is not None:
                nodes.append(node)
        op = _opacity(p)
        return ("g", [] if op is None else [("opacity", op)], nodes)
    if kind == "text":
        return _text_node(p, ctx)
    if kind == "image":
        if not _finite(p.get("x"), p.get("y"), p.get("w"), p.get("h")):
            return None
        if ctx.embed is not None:
            embedded = _embedded(p.get("src") or {}, p, ctx)
            if embedded is not None:
                return embedded
            return ("g", [], _children(p.get("fallback"), ctx)) if p.get("fallback") else None
        href = ctx.url(p.get("src"))
        if not href:
            return None
        return _image_node(p["x"], p["y"], p["w"], p["h"], href, p)
    if kind == "slot":
        return slot_node(p, ctx)
    if kind == "group":
        attrs: List[Tuple[str, str]] = []
        parts = []
        screen = p.get("screen")
        if isinstance(screen, list) and len(screen) == 2 and _finite(screen[0], screen[1]):
            parts.append("translate({} {}) scale({})".format(fmt(screen[0]), fmt(screen[1]), fmt(1 / ctx.scale)))
        t = p.get("t")
        if isinstance(t, list) and len(t) == 6 and _finite(*t):
            parts.append("matrix({})".format(" ".join(fmt(v) for v in t)))
        if parts:
            attrs.append(("transform", " ".join(parts)))
        clip = p.get("clip")
        if isinstance(clip, list) and len(clip) == 4 and _finite(*clip):
            attrs.append(("clip-path", "url(#{})".format(ctx.defs.clip(" ".join(fmt(v) for v in clip)))))
        op = _opacity(p)
        if op is not None:
            attrs.append(("opacity", op))
        return ("g", attrs, _children(p.get("items"), ctx))
    return None


def slot_node(p: Mapping[str, Any], ctx: _Ctx) -> Node:
    """A slot drawn without the browser: its still when it has one, else its fallback primitives."""
    inner: List[Node] = []
    still = p.get("still")
    has_box = _finite(p.get("x"), p.get("y"), p.get("w"), p.get("h"))
    if isinstance(still, str) and still and has_box:
        if ctx.embed is not None:
            embedded = _embedded({"still": still}, p, ctx)
            if embedded is not None:
                inner.append(embedded)
        else:
            href = ctx.url({"still": still})
            if href:
                inner.append(_image_node(p["x"], p["y"], p["w"], p["h"], href, None))
    if not inner:
        inner = _children(p.get("fallback"), ctx)
    return ("g", [("data-slot", "" if p.get("slot") is None else str(p.get("slot")))], inner)


def _layer_of(item: Mapping[str, Any], entry: Mapping[str, Any]) -> str:
    layer = item.get("layer") or entry.get("layer")
    return layer if layer in LAYER_NAMES else "marks"


# --------------------------------------------------------------------------
# the document (``svgString.js``)


def normalize_box(box: Any) -> Tuple[float, float, float, float]:
    """A region or bbox as ``(x0, y0, x1, y1)``: min and max per axis, at least one unit wide and tall."""
    if not isinstance(box, (list, tuple)) or len(box) != 4 or not _finite(*box):
        return EMPTY_BOX
    a, b, c, d = (float(v) for v in box)
    x0, y0 = min(a, c), min(b, d)
    return x0, y0, max(a, c, x0 + 1), max(b, d, y0 + 1)


def pixel_size(box: Sequence[float], max_px: int = 1024) -> Tuple[int, int]:
    """The picture's size in pixels: the longer side ``max_px`` (Python's ``round``, half to even)."""
    width, height = box[2] - box[0], box[3] - box[1]
    scale = max_px / max(width, height, 1)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def _meets(entry: Mapping[str, Any], box: Sequence[float]) -> bool:
    b = entry.get("bbox")
    if not isinstance(b, list) or len(b) != 4 or not _finite(*b):
        return True
    return b[0] < box[2] and box[0] < b[2] and b[1] < box[3] and box[1] < b[3]


def write(dl: Mapping[str, Any], *, theme: str = "light", box: Optional[Sequence[float]] = None, max_px: int = 1024,
          families: Optional[Mapping[str, str]] = None, urls: Optional[Mapping[str, Callable[[str], str]]] = None,
          embed: Optional[Embed] = None, resvg_text: bool = False, marks: Optional[Sequence[Tuple[str, float, float, bool]]] = None,
          grid: bool = False) -> str:
    """``dl`` (or the ``box`` of it) as one SVG document; canonical with the defaults (1.8).

    ``embed``, ``resvg_text``, ``marks`` (``(id, x, y, above)`` badges) and ``grid`` make the agent's
    picture instead (``canvas_render.render_svg``): assets and stills inlined, text rewritten for
    resvg, the fonts named on the root, id badges and grid dots on top.
    """
    area = normalize_box(box if box is not None else dl.get("bbox"))
    width_px, height_px = pixel_size(area, max_px)
    u = (area[2] - area[0]) / width_px
    palettes = dl.get("palettes") if isinstance(dl.get("palettes"), dict) else {}
    palette = palettes.get(theme) if isinstance(palettes.get(theme), dict) else (palettes.get("light") or {})
    shadows_all = dl.get("shadows") if isinstance(dl.get("shadows"), dict) else {}
    shadows = shadows_all.get(theme) if isinstance(shadows_all.get(theme), dict) else {}
    defs = Defs(shadows)
    ctx = _Ctx(palette, 1 / u, families or CANONICAL_FAMILIES, urls or CANONICAL_URLS, defs, embed, resvg_text)
    listed = [e for e in (dl.get("entries") or []) if isinstance(e, dict) and _meets(e, area)]
    body: List[str] = []
    for layer in LAYER_NAMES:
        body.append('<g data-layer="{}">'.format(layer))
        for item in listed:
            nodes = [node for node in (primitive_node(p, ctx) for p in item.get("items") or []
                                       if isinstance(p, dict) and _layer_of(p, item) == layer) if node is not None]
            if not nodes:
                continue
            body.append('<g data-id="{}">'.format(escape(item.get("id") if item.get("id") is not None else "")))
            body.extend(serialize(node) for node in nodes)
            body.append("</g>")
        body.append("</g>")
    canvas = resolve_paint("base.canvas", palette)
    w, h = area[2] - area[0], area[3] - area[1]
    root = '<svg xmlns="{}" width="{}" height="{}" viewBox="{} {} {} {}"'.format(SVG_NS, width_px, height_px, fmt(area[0]), fmt(area[1]), fmt(w), fmt(h))
    out = [root + (' font-family="{}">'.format(RESVG_ROOT_FONT) if resvg_text else ">")]
    if not defs.empty:
        out.append("<defs>" + "".join(serialize(node) for node in defs.nodes()) + "</defs>")
    out.append('<rect x="{}" y="{}" width="{}" height="{}" fill="{}"/>'.format(fmt(area[0]), fmt(area[1]), fmt(w), fmt(h), canvas))
    if grid:
        out.extend(grid_marks(area, u, palette))
    out.extend(body)
    for ident, x, y, above in marks or ():
        out.append(badge(ident, x, y, above, u))
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------
# the agent's extras: id badges and the labelled grid (D9)


def badge(ident: str, x: float, y: float, above: bool, u: float) -> str:
    """An id badge at an element's anchor (Set-of-Mark): inside a shape's corner, above a free text's first line."""
    size = 11.0 * u
    width = (_ctext.measure(ident, size=11.0, weight=700).width + 6.0) * u
    height = 15.0 * u
    x -= 2 * u
    y -= (height + 2 * u) if above else 2 * u
    return ('<rect x="{}" y="{}" width="{}" height="{}" rx="{}" fill="#1e1e1e" opacity="0.85"/>'
            '<text x="{}" y="{}" font-size="{}" font-family="Inter" font-weight="bold" fill="#ffffff">{}</text>').format(
        fmt(x), fmt(y), fmt(width), fmt(height), fmt(3 * u), fmt(x + 3 * u), fmt(y + 11.5 * u), fmt(size), escape(_render_text(ident)))


def grid_marks(box: Sequence[float], u: float, palette: Mapping[str, str]) -> List[str]:
    """Grid dots every 100 units (sparser on a big picture), every other one named by its cell (Scaffold)."""
    x0, y0, x1, y1 = box
    step = 100.0
    while max(x1 - x0, y1 - y0) / step > 60:
        step *= 2
    dot = _colour("base.line", palette)
    muted = _colour("base.ink_muted", palette)
    out = []
    gx = math.ceil(x0 / step) * step
    while gx <= x1:
        gy = math.ceil(y0 / step) * step
        while gy <= y1:
            out.append('<circle cx="{}" cy="{}" r="{}" fill="{}"/>'.format(fmt(gx), fmt(gy), fmt(1.8 * u), dot))
            if round(gx / step) % 2 == 0 and round(gy / step) % 2 == 0:
                out.append('<text x="{}" y="{}" font-size="{}" font-family="Inter" fill="{}">c{}r{}</text>'.format(
                    fmt(gx + 3 * u), fmt(gy - 3 * u), fmt(9 * u), muted, int(math.floor(gx / 20)), int(math.floor(gy / 20))))
            gy += step
        gx += step
    return out


def fragment(items: Sequence[Mapping[str, Any]], palette: Optional[Mapping[str, str]] = None, scale: float = 1.0,
             resvg_text: bool = False, families: Optional[Mapping[str, str]] = None) -> str:
    """Some primitives as bare SVG markup (no document, no defs): what ``write`` draws for them, for tests and tools."""
    from herdr_team import canvas_theme  # the light palette by default; imported late to keep this module's imports small

    ctx = _Ctx(palette if palette is not None else canvas_theme.palette("light"), scale, families or CANONICAL_FAMILIES, CANONICAL_URLS,
               Defs({}), None, resvg_text)
    return "".join(serialize(node) for node in (primitive_node(p, ctx) for p in items) if node is not None)
