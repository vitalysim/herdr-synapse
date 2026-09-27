"""Canvas colours by meaning (canvas v2 foundation): tones, variants, and the tokens every renderer reads.

The design tokens live in one hand-edited file, ``herdr_team/canvas_tokens.json``
(design ``.local/prd/canvas-v2-design.md``; ``tests/test_canvas_tokens_contrast.py``
keeps its contrast promises). This module is its one reader in Python:

* ``resolve(tone, variant, kind)`` is the ``stroke``, ``fill`` and label
  ``text`` colour of an element: an agent picks a tone (``info``,
  ``warning`` ...) and the tone picks every colour, so colour carries meaning
  rather than authorship;
* ``default_tone(kind)``, ``size_min(kind)`` and ``padding(kind)`` are the
  defaults ``canvas`` and the kinds build elements with;
* ``tone_of(value)`` maps a legacy Open Color name or stored hex to its tone;
* ``asset()`` is ``assets/canvas/tokens.json``, the resolved view the page
  imports at build time, written by ``python3 -m herdr_team.canvas_theme --write``
  (``--check`` fails when the token file changed and it was not rewritten).

Pure apart from the cached ``tokens`` load; the standard library only.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

_log = logging.getLogger(__name__)

TOKENS_PATH = Path(__file__).resolve().parent / "canvas_tokens.json"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
ASSET_PATH = PLUGIN_ROOT / "assets" / "canvas" / "tokens.json"
ASSET_SCHEMA = 1

TONES = ("neutral", "info", "success", "warning", "danger", "accent", "idea", "decision")
VARIANTS = ("soft", "solid", "outline")
THEMES = ("light", "dark")

#: How each element kind uses a tone's roles (design spec 6): a note is sticky paper, a frame a tinted zone,
#: an arrow a line with a muted label, free text and ink strokes are drawn in the text colour.
KIND_GROUPS = {"box": "shape", "ellipse": "shape", "diamond": "shape", "note": "note", "frame": "frame", "arrow": "arrow",
               "text": "text", "pen": "ink", "path": "ink"}

#: Used only when ``canvas_tokens.json`` cannot be read (a broken install): the light neutral tone, so
#: the canvas still draws legibly.
_FALLBACK: Dict[str, Any] = {
    "tones": ["neutral"],
    "theme": {"light": {"base": {"canvas": "#f7f8fa", "surface": "#ffffff", "ink": "#1c2024", "ink_muted": "#5b616b",
                                 "line": "#6b7079", "grid": "#e3e5e9", "selection": "#6e56cf"},
                        "tone": {"neutral": {"fill": "#ffffff", "stroke": "#80858f", "text": "#1c2024", "solid": "#3a3f47",
                                             "on_solid": "#ffffff", "sticky": "#eceef1", "zone": "#f0f1f4", "zone_stroke": "#d9dce1"}}}},
    "size_min": {}, "padding": {}, "legacy": {"color": {}, "stroke_hex": {}, "fill_hex": {}},
}
_CACHE: Dict[str, Dict[str, Any]] = {}


def tokens() -> Dict[str, Any]:
    """The design tokens (cached); a minimal neutral palette, with one warning, when the file is unreadable."""
    cached = _CACHE.get("tokens")
    if cached is not None:
        return cached
    try:
        doc = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("theme"), dict):
            raise ValueError("no theme")
    except (OSError, ValueError) as err:
        _log.warning("canvas tokens unreadable (%s): %s; drawing in neutral only", TOKENS_PATH, err)
        doc = _FALLBACK
    _CACHE["tokens"] = doc
    return doc


def base(theme: str = "light") -> Dict[str, str]:
    """The theme's base colours: ``canvas``, ``surface``, ``ink``, ``ink_muted``, ``line``, ``grid``, ``selection``."""
    themes = tokens()["theme"]
    return dict(themes.get(theme) or themes["light"])["base"]


def roles(tone: str, theme: str = "light") -> Dict[str, str]:
    """A tone's eight colours (``fill``, ``stroke``, ``text``, ``solid``, ``on_solid``, ``sticky``, ``zone``, ``zone_stroke``)."""
    themes = tokens()["theme"]
    tones = (themes.get(theme) or themes["light"])["tone"]
    return dict(tones.get(tone) or tones["neutral"])


def resolve(tone: str, variant: str = "soft", kind: str = "box", theme: str = "light") -> Dict[str, Optional[str]]:
    """``{"stroke", "fill", "text"}`` for an element of ``kind`` in ``tone`` and ``variant`` (unknown names fall back to neutral/soft)."""
    tone = tone if tone in TONES else "neutral"
    variant = variant if variant in VARIANTS else "soft"
    t = roles(tone, theme)
    b = base(theme)
    group = KIND_GROUPS.get(kind, "other")
    if group == "arrow":
        return {"stroke": b["line"] if tone == "neutral" else t["stroke"], "fill": None,
                "text": b["ink_muted"] if tone == "neutral" else t["text"]}
    if group == "text":
        return {"stroke": t["text"], "fill": None, "text": t["text"]}
    if group == "ink":
        return {"stroke": b["ink"] if tone == "neutral" else t["stroke"], "fill": None, "text": t["text"]}
    if group == "frame":
        return {"stroke": t["stroke"] if variant == "solid" else t["zone_stroke"], "fill": t["zone"], "text": t["text"]}
    if variant == "solid":
        return {"stroke": t["solid"], "fill": t["solid"], "text": t["on_solid"]}
    if variant == "outline":
        return {"stroke": t["stroke"], "fill": b["surface"], "text": t["text"]}
    if group == "other":
        return {"stroke": t["stroke"], "fill": None, "text": t["text"]}
    return {"stroke": t["stroke"], "fill": t["sticky"] if group == "note" else t["fill"], "text": t["text"]}


def default_tone(kind: str) -> Tuple[str, str]:
    """``(tone, variant)`` an element of ``kind`` gets when the op names none: a note is an idea sticky, the rest neutral."""
    sticky = tokens().get("sticky") if isinstance(tokens().get("sticky"), dict) else {}
    if kind == "note":
        return str(sticky.get("default_tone") or "idea"), "soft"
    return "neutral", "soft"


def tone_of(value: Any) -> Optional[str]:
    """The tone a legacy Open Color name (``blue``) or a stored stroke or fill hex stands for, or None."""
    if not isinstance(value, str):
        return None
    legacy = tokens().get("legacy") if isinstance(tokens().get("legacy"), dict) else {}
    key = value.strip().lower()
    for table in ("color", "stroke_hex", "fill_hex"):
        found = (legacy.get(table) or {}).get(key)
        if found in TONES:
            return found
    return None


def size_min(kind: str, default: Tuple[float, float] = (160, 80)) -> Tuple[float, float]:
    """The smallest ``w`` x ``h`` a ``kind`` is drawn at (design tokens ``size_min``)."""
    found = (tokens().get("size_min") or {}).get(kind)
    if isinstance(found, list) and len(found) == 2:
        return float(found[0]), float(found[1])
    return float(default[0]), float(default[1])


def padding(kind: str, default: Tuple[float, float] = (16, 12)) -> Tuple[float, float]:
    """``(x, y)`` room kept between a ``kind``'s edge (or inset) and its label, per side."""
    found = (tokens().get("padding") or {}).get(kind)
    if isinstance(found, dict) and "x" in found and "y" in found:
        return float(found["x"]), float(found["y"])
    return float(default[0]), float(default[1])


def _luminance(color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    raw = color.lstrip("#")
    r, g, b = (channel(int(raw[i:i + 2], 16)) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    """The WCAG 2.2 contrast ratio of two ``#rrggbb`` colours."""
    a, b = _luminance(fg), _luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


# --------------------------------------------------------------------------
# assets/canvas/tokens.json: the resolved view the page imports


def asset() -> Dict[str, Any]:
    """The ``assets/canvas/tokens.json`` document: base colours, every tone resolved per kind group and variant, sizes and fonts."""
    doc = tokens()
    light = base("light")
    groups = {"shape": "box", "note": "note", "frame": "frame", "arrow": "arrow", "text": "text", "ink": "pen"}
    resolved = {group: {tone: {variant: resolve(tone, variant, kind) for variant in VARIANTS} for tone in TONES}
                for group, kind in groups.items()}
    return {
        "v": ASSET_SCHEMA,
        "source": "herdr_team/canvas_tokens.json; edit that file, then run python3 -m herdr_team.canvas_theme --write",
        "line_height": 1.25,
        "fonts": {"sans": {"family": "Inter", "files": {"400": "inter/Inter-Regular.ttf", "500": "inter/Inter-Medium.ttf",
                                                        "600": "inter/Inter-SemiBold.ttf", "700": "inter/Inter-Bold.ttf"}},
                  "mono": {"family": "Geist Mono", "files": {"400": "geist-mono/GeistMono-Regular.ttf"}}},
        "ink": light["ink"], "muted": light["ink_muted"], "canvas": light["canvas"], "surface": light["surface"],
        "line": light["line"], "grid": light["grid"],
        "tones": resolved["shape"],
        "kinds": {group: resolved[group] for group in groups if group != "shape"},
        "kind_groups": dict(KIND_GROUPS),
        "defaults": {kind: list(default_tone(kind)) for kind in ("box", "ellipse", "diamond", "note", "text", "frame", "arrow", "pen")},
        "size_min": doc.get("size_min") or {},
        "padding": doc.get("padding") or {},
        "radius": doc.get("radius") or {},
        "type": doc.get("type") or {},
        "legacy": doc.get("legacy") or {},
        "phase0_excalidraw": doc.get("phase0_excalidraw") or {},
        "theme": doc.get("theme") or {},
    }


def dumps(doc: Dict[str, Any]) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m herdr_team.canvas_theme", description="Write assets/canvas/tokens.json.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write assets/canvas/tokens.json")
    mode.add_argument("--check", action="store_true", help="exit 1 when the file is stale")
    return parser


def main(argv: Sequence[str] = ()) -> int:
    args = _parser().parse_args(list(argv))
    text = dumps(asset())
    if args.check:
        try:
            current = ASSET_PATH.read_text(encoding="utf-8")
        except OSError:
            current = ""
        if current != text:
            sys.stderr.write("assets/canvas/tokens.json is stale; run python3 -m herdr_team.canvas_theme --write\n")
            return 1
        return 0
    if args.write:
        ASSET_PATH.parent.mkdir(parents=True, exist_ok=True)
        ASSET_PATH.write_text(text, encoding="utf-8")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
