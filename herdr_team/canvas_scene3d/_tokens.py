"""The scene3d design tokens (canvas v2 phase 4, 1.5), read from ``canvas_tokens.json``; nothing here picks a colour.

The ``scene3d`` block holds the camera presets (``az`` and ``el`` in degrees),
the light presets the page builds, the material finishes, the face shading
the projections use and the label sizes. A projected face is painted with
``mat.<tone>.<face>`` (``top``, ``left``, ``right``) and its edges with
``mat.<tone>.edge``: ``canvas_theme`` derives those from each tone's ``solid``.
"""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team import canvas_theme

#: The shades of a projected face.
FACES = ("top", "left", "right")


def block() -> Dict[str, Any]:
    found = canvas_theme.section("scene3d", {})
    return found if isinstance(found, dict) else {}


def camera(name: str) -> Dict[str, Any]:
    cams = block().get("cameras") or {}
    return dict(cams.get(name) or cams.get("iso") or {"az": 45, "el": 35.264})


def cameras() -> List[str]:
    return list(block().get("cameras") or {})


def lights() -> List[str]:
    return list(block().get("lights") or {})


def finishes() -> List[str]:
    return list(block().get("finish") or {})


def finish(name: str) -> Dict[str, Any]:
    return dict((block().get("finish") or {}).get(name) or {})


def label_px() -> float:
    return float((block().get("label") or {}).get("size_px", 12))


def label_min_px() -> float:
    return float((block().get("label") or {}).get("min_px", 10))


def mat_ref(tone: str, face: str) -> str:
    """The paint of a face (``top``, ``left``, ``right``) or of the edges (``edge``) in ``tone``."""
    return "mat.{}.{}".format(tone if tone in canvas_theme.TONES else "neutral", face)
