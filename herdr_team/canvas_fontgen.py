"""Generate ``assets/fonts/font-metrics.json`` from the bundled fonts (canvas v2).

The canvas measures text in Python (``canvas_text``) with the advance widths
of the same font files the page and resvg draw with, so the three agree on
where a line breaks. This module reads those widths straight out of the
TrueType files with ``struct`` (the ``head``, ``hhea``, ``maxp``, ``OS/2``,
``name``, ``cmap`` and ``hmtx`` tables; no kerning, see
``canvas_text``) and writes them as one JSON file::

    python3 -m herdr_team.canvas_fontgen --write
    python3 -m herdr_team.canvas_fontgen --check   # exits 1 and names each stale face

Advances are stored as runs ``[first codepoint, [advance, ...]]`` over
consecutive mapped codepoints, in font units, private use left out: compact,
and loading is a loop rather than a parse of thousands of keys. Every face
carries its file's sha256, so a font swapped without regenerating fails
``--check`` (a unit test runs it). Python 3.9 and the standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

GENERATOR = "herdr_team.canvas_fontgen 1"
SCHEMA = 1
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = PLUGIN_ROOT / "assets" / "fonts"
METRICS_PATH = FONTS_DIR / "font-metrics.json"

#: The faces the canvas measures with: key -> (file under ``assets/fonts``, weight). ``sans-500`` is the
#: one weight the Excalidraw page draws in Phase 0 (design spec 9.2), the rest are for resvg and later phases.
FACES: Tuple[Tuple[str, str, int], ...] = (
    ("sans-400", "inter/Inter-Regular.ttf", 400),
    ("sans-500", "inter/Inter-Medium.ttf", 500),
    ("sans-600", "inter/Inter-SemiBold.ttf", 600),
    ("sans-700", "inter/Inter-Bold.ttf", 700),
    ("mono-400", "geist-mono/GeistMono-Regular.ttf", 400),
)


class FontError(ValueError):
    """A font file this reader cannot use (not TrueType/OpenType, or a required table missing or short)."""


def _private_use(cp: int) -> bool:
    return 0xE000 <= cp <= 0xF8FF or cp >= 0xF0000 or 0xD800 <= cp <= 0xDFFF


def _tables(data: bytes) -> Dict[str, Tuple[int, int]]:
    """The table directory: tag -> (offset, length)."""
    if len(data) < 12:
        raise FontError("too short to be a font")
    version = data[:4]
    if version not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        raise FontError("not a TrueType or OpenType font (a collection or a web font?)")
    count = struct.unpack_from(">H", data, 4)[0]
    out: Dict[str, Tuple[int, int]] = {}
    for index in range(count):
        tag, _checksum, offset, length = struct.unpack_from(">4sIII", data, 12 + 16 * index)
        if offset + length > len(data):
            raise FontError("table {} runs past the end of the file".format(tag.decode("latin-1")))
        out[tag.decode("latin-1")] = (offset, length)
    for required in ("head", "hhea", "maxp", "cmap", "hmtx"):
        if required not in out:
            raise FontError("the {} table is missing".format(required))
    return out


def _name(data: bytes, table: Tuple[int, int], name_id: int) -> Optional[str]:
    """One ``name`` record: Windows English (UTF-16BE) first, then Mac Roman."""
    offset, _length = table
    _fmt, count, strings = struct.unpack_from(">HHH", data, offset)
    found: Dict[Tuple[int, int, int], str] = {}
    for index in range(count):
        platform, encoding, language, nid, length, start = struct.unpack_from(">HHHHHH", data, offset + 6 + 12 * index)
        if nid != name_id:
            continue
        raw = data[offset + strings + start: offset + strings + start + length]
        if platform == 3:
            found[(0, 0 if language == 0x409 else 1, index)] = raw.decode("utf-16-be", "replace")
        elif platform == 1 and encoding == 0:
            found[(1, 0 if language == 0 else 1, index)] = raw.decode("mac_roman", "replace")
    return found[min(found)] if found else None


def _cmap_format4(data: bytes, offset: int) -> Dict[int, int]:
    seg_x2 = struct.unpack_from(">H", data, offset + 6)[0]
    segs = seg_x2 // 2
    ends = struct.unpack_from(">{}H".format(segs), data, offset + 14)
    starts_at = offset + 16 + seg_x2
    starts = struct.unpack_from(">{}H".format(segs), data, starts_at)
    deltas = struct.unpack_from(">{}h".format(segs), data, starts_at + seg_x2)
    ranges_at = starts_at + 2 * seg_x2
    ranges = struct.unpack_from(">{}H".format(segs), data, ranges_at)
    out: Dict[int, int] = {}
    for seg in range(segs):
        start, end, delta, range_offset = starts[seg], ends[seg], deltas[seg], ranges[seg]
        if start == 0xFFFF:
            continue
        for cp in range(start, end + 1):
            if range_offset == 0:
                glyph = (cp + delta) & 0xFFFF
            else:
                at = ranges_at + 2 * seg + range_offset + 2 * (cp - start)
                glyph = struct.unpack_from(">H", data, at)[0]
                if glyph:
                    glyph = (glyph + delta) & 0xFFFF
            if glyph:
                out[cp] = glyph
    return out


def _cmap_format12(data: bytes, offset: int) -> Dict[int, int]:
    groups = struct.unpack_from(">I", data, offset + 12)[0]
    out: Dict[int, int] = {}
    for index in range(groups):
        start, end, glyph = struct.unpack_from(">III", data, offset + 16 + 12 * index)
        for cp in range(start, min(end, 0x10FFFF) + 1):
            if glyph + cp - start:
                out[cp] = glyph + cp - start
    return out


def _cmap(data: bytes, table: Tuple[int, int]) -> Dict[int, int]:
    """Codepoint -> glyph from the best Unicode subtable: format 12 (full Unicode) over format 4 (BMP)."""
    offset, _length = table
    count = struct.unpack_from(">H", data, offset + 2)[0]
    best: Optional[Tuple[int, int]] = None  # (rank, subtable offset)
    for index in range(count):
        platform, encoding, sub = struct.unpack_from(">HHI", data, offset + 4 + 8 * index)
        fmt = struct.unpack_from(">H", data, offset + sub)[0]
        unicode_table = platform == 0 or (platform == 3 and encoding in (1, 10))
        if not unicode_table or fmt not in (4, 12):
            continue
        rank = 0 if fmt == 12 else 1
        if best is None or rank < best[0]:
            best = (rank, offset + sub)
    if best is None:
        raise FontError("no Unicode cmap subtable in format 4 or 12")
    return _cmap_format12(data, best[1]) if best[0] == 0 else _cmap_format4(data, best[1])


def read_face(path: Path) -> Dict[str, Any]:
    """Everything the canvas needs from one font file (``FontError`` when it cannot be read)."""
    data = Path(path).read_bytes()
    try:
        tables = _tables(data)
        units = struct.unpack_from(">H", data, tables["head"][0] + 18)[0]
        ascender, descender, line_gap = struct.unpack_from(">hhh", data, tables["hhea"][0] + 4)
        metrics_count = struct.unpack_from(">H", data, tables["hhea"][0] + 34)[0]
        glyphs = struct.unpack_from(">H", data, tables["maxp"][0] + 4)[0]
        advances = struct.unpack_from(">{}H".format(2 * metrics_count), data, tables["hmtx"][0])[0::2]
        cap_height = x_height = None
        if "OS/2" in tables:
            os2 = tables["OS/2"][0]
            if struct.unpack_from(">H", data, os2)[0] >= 2 and tables["OS/2"][1] >= 90:
                x_height, cap_height = struct.unpack_from(">hh", data, os2 + 86)
        family = None
        if "name" in tables:
            family = _name(data, tables["name"], 16) or _name(data, tables["name"], 1)
        cmap = _cmap(data, tables["cmap"])
    except struct.error as err:
        raise FontError("a table is shorter than its format: {}".format(err))
    if not units or not advances:
        raise FontError("no units per em or no horizontal metrics")

    def advance(glyph: int) -> int:
        return advances[glyph] if glyph < len(advances) else advances[-1]

    runs: List[List[Any]] = []
    for cp in sorted(cp for cp, glyph in cmap.items() if not _private_use(cp) and glyph < max(glyphs, 1)):
        if runs and runs[-1][0] + len(runs[-1][1]) == cp:
            runs[-1][1].append(advance(cmap[cp]))
        else:
            runs.append([cp, [advance(cmap[cp])]])
    return {"family": family, "units_per_em": units, "ascender": ascender, "descender": descender, "line_gap": line_gap,
            "cap_height": cap_height, "x_height": x_height, "default_advance": advance(0), "advances": runs}


def generate(fonts_dir: Path = FONTS_DIR) -> Dict[str, Any]:
    """The whole ``font-metrics.json`` document for ``FACES``."""
    faces: Dict[str, Any] = {}
    for key, rel, weight in FACES:
        path = Path(fonts_dir) / rel
        face = read_face(path)
        entry: Dict[str, Any] = {"file": rel, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                 "family": face.pop("family"), "weight": weight, "italic": False}
        entry.update(face)
        faces[key] = entry
    return {"v": SCHEMA, "generator": GENERATOR, "faces": faces}


def dumps(doc: Dict[str, Any]) -> str:
    """Stable text for the document: one line per face field and one per advance run, so diffs stay readable."""
    lines = ["{", '  "v": {},'.format(json.dumps(doc["v"])), '  "generator": {},'.format(json.dumps(doc["generator"])), '  "faces": {']
    faces = list(doc["faces"].items())
    for face_index, (key, face) in enumerate(faces):
        lines.append("    {}: {{".format(json.dumps(key)))
        scalars = [(k, v) for k, v in face.items() if k != "advances"]
        for k, v in scalars:
            lines.append("      {}: {},".format(json.dumps(k), json.dumps(v)))
        lines.append('      "advances": [')
        runs = face.get("advances") or []
        for run_index, run in enumerate(runs):
            lines.append("        {}{}".format(json.dumps(run, separators=(",", ":")), "," if run_index < len(runs) - 1 else ""))
        lines.append("      ]")
        lines.append("    }}{}".format("," if face_index < len(faces) - 1 else ""))
    lines += ["  }", "}", ""]
    return "\n".join(lines)


def stale_faces(current: str, fresh: Dict[str, Any]) -> List[str]:
    """The face keys whose stored entry differs from ``fresh`` (every key when the file is unreadable)."""
    try:
        stored = json.loads(current)
    except ValueError:
        return sorted(fresh["faces"])
    if not isinstance(stored, dict) or stored.get("generator") != fresh["generator"] or stored.get("v") != fresh["v"]:
        return sorted(fresh["faces"])
    faces = stored.get("faces") if isinstance(stored.get("faces"), dict) else {}
    keys = set(faces) | set(fresh["faces"])
    return sorted(key for key in keys if faces.get(key) != fresh["faces"].get(key))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m herdr_team.canvas_fontgen", description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write assets/fonts/font-metrics.json")
    mode.add_argument("--check", action="store_true", help="exit 1 when the file is stale")
    return parser


def main(argv: Sequence[str] = ()) -> int:
    args = _parser().parse_args(list(argv))
    fresh = generate()
    text = dumps(fresh)
    if args.check:
        try:
            current = METRICS_PATH.read_text(encoding="utf-8")
        except OSError:
            current = ""
        if current != text:
            stale = stale_faces(current, fresh) or ["(formatting)"]
            sys.stderr.write("assets/fonts/font-metrics.json is stale ({}); run python3 -m herdr_team.canvas_fontgen --write\n".format(
                ", ".join(stale)))
            return 1
        return 0
    if args.write:
        METRICS_PATH.write_text(text, encoding="utf-8")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
