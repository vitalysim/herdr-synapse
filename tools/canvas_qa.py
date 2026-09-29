#!/usr/bin/env python3
"""Canvas QA: apply golden scenes to a throwaway team, check them, render them, and count what reads badly.

Each scene file (``tests/fixtures/canvas_scenes/*.json``) is ``{"name", "about", "ops": [...]}``, or
``"batches": [[...], ...]`` for scenes that need several batches. For every scene this tool:

1. builds a temporary state root with one team and its canvas turned on (the real HOME, socket and
   state are never touched), and applies the ops as a member through ``canvas.apply_ops``;
2. runs the layout check (``canvas.check``) and counts its problems by code;
3. renders the whole scene with the real resvg and the real fonts (the PNG agents get from ``look``);
4. probes every text it can find with pixels: for each labelled shape, free text and arrow label it
   renders that element's text alone (everything but ``<text>`` stripped) over the region around it,
   and the scene with every text stripped, then measures:

   * ``overflow``: ink outside the element's own outline (a box's rectangle, an ellipse, a diamond),
     beyond ``--tolerance`` units;
   * ``collision``: ink on another solid mark the element does not sit inside (a label under a door,
     a caption on a neighbour's box); for arrow labels this is tracked separately, and another arrow
     label's pill counts as a mark too;
   * ``contrast``: the WCAG ratio between the ink's core pixels and what is under them (4.5:1, or 3:1
     from 24 units up); run once per theme the renderer supports;
   * ``lines``: the line breaks the server drew (a ``<text>`` per line), to compare with the page's.

5. reads what the whole render cannot show: characters resvg found no font for (``tofu``, from its
   warnings: the boxes stay inside their shape, so no pixel probe sees them), and frame titles and
   arrow-label pills that reach past the picture's edge (``cut_off``).

With ``--page`` it also serves each scene's whiteboard page on loopback, opens it in a throwaway
headless Chrome (its own profile, killed afterwards), reads the line breaks the page drew, compares
them with the server's (blank lines and indentation included; emoji differences the server makes on
purpose, ``canvas_render.render_text``, excused by rule), reads the page's own fit audit
(``page_overflow``: a label wider or taller than the room its container gives it), and saves light and
dark screenshots of the page. The page is canvas v2, the display-list page and the page's default since
canvas v2 phase 6 (``--engine v2``, the default): it reads them through its QA hook ``window.__synapseV2``
(lines, the fit audit, fit to view), and also gates any CSS ``filter`` on the page (AC-4.2: nothing inverts
colours in the dark theme). ``--engine v1`` opens the classic Excalidraw page (``?engine=v1``) instead. The v2 page is read
at full detail (scale 1, above every level-of-detail band), one screen-sized tile at a time, so a board bigger than
the screen is compared line for line and never against its zoomed-out skeletons. Every entry that draws text is probed
and compared, a table's cells and a sequence's names and messages included.

It prints one line per scene (``--json`` for everything) and exits 0 when every gated count is zero,
1 when any is not, 2 when a scene is invalid (an op refused, a bad file), and 3 when resvg is missing.

    python3 tools/canvas_qa.py                          # every golden scene, output under .local/qa/canvas
    python3 tools/canvas_qa.py tests/fixtures/canvas_scenes/house.json --out /tmp/qa --json
    python3 tools/canvas_qa.py house --page-lines page-lines.json   # compare with lines read off the page
    python3 tools/canvas_qa.py --page                   # also read the v2 page's line breaks in headless Chrome
    python3 tools/canvas_qa.py --page --engine v1       # the same against the classic (Excalidraw) page
    python3 tools/canvas_qa.py house --serve 120        # also serve the scene to a browser for 120 s

Stdlib only; it imports ``herdr_team`` from this checkout.
"""
from __future__ import annotations

import argparse
import inspect
import json
import math
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from herdr_team import canvas as C  # noqa: E402
from herdr_team import canvas_check as K  # noqa: E402
from herdr_team import canvas_kinds  # noqa: E402
from herdr_team import canvas_render as R  # noqa: E402
from herdr_team import features, paths, store  # noqa: E402

SCENES_DIR = REPO / "tests" / "fixtures" / "canvas_scenes"
#: The data files a scene names in ``artifacts`` (a folder here) are copied into the QA team's ``artifacts/`` before it
#: applies (canvas v2 phase 3: charts read CSV and JSON there; 3D scenes read glTF models).
ARTIFACTS_DIR = REPO / "tests" / "fixtures" / "canvas_artifacts"
OUT_DIR = REPO / ".local" / "qa" / "canvas"
TEAM = "qa"
MEMBER = "qa-drawer"
#: Canvas v2 phase 5: a second member, and a member holding an operator grant (a delegate), for multi-author scenes.
PEER = "qa-peer"
DEPUTY = "qa-deputy"
#: Who a scene batch's ``as`` names: the drawer (the default), the peer, the deputy, or the lead (the operator in person).
AUTHOR_KEYS = ("drawer", "peer", "deputy", "lead")

#: Kinds whose ``text`` is drawn inside their outline: every kind whose label its own ``measure`` fits, from the
#: registry, so a new labelled kind is probed with no edit here (QA phase 1, V-2). Arrows carry a label pill at their
#: midpoint and are probed separately.
LABELLED = tuple(kind.name for kind in canvas_kinds.kinds() if kind.measure is not None and kind.role != "connector")
#: Pixels per canvas unit in the probes, and the longest probe side before the scale drops.
PROBE_SCALE = 2.0
PROBE_MAX_PX = 2400
#: Units of slack around an outline before ink counts as outside it (anti-aliasing, stroke width).
TOLERANCE = 2.0
#: Fewer stray pixels than this is anti-aliasing noise, not a finding.
MIN_PIXELS = 4
#: Alpha at or above which a text-only pixel is glyph core (used for colour and contrast).
CORE_ALPHA = 230
INK_ALPHA = 64
#: WCAG 2.2 SC 1.4.3: 4.5:1 for body text, 3:1 for large text (24 px and up, or 18.66 px bold).
CONTRAST_BODY = 4.5
CONTRAST_LARGE = 3.0
LARGE_TEXT = 24.0

#: Counts that fail the Phase 0 gate; ``--strict`` adds the tracked ones (routing lands in Phase 2).
GATED_CHECKS = ("overlap", "text_on_label", "label_overflow", "frame_edge")
TRACKED_CHECKS = ("arrow_through", "stray", "route_loop", "label_astray")

SVG_NS = "http://www.w3.org/2000/svg"
TEXT_TAGS = ("text",)
KEEP_TAGS = ("defs", "style", "font-face")

Box = Tuple[float, float, float, float]


# --------------------------------------------------------------------------
# PNG decoding (8-bit, non-interlaced: what resvg writes)


def _unfilter(kind: int, line: bytearray, prev: bytearray, bpp: int) -> bytearray:
    """One PNG scanline with its filter undone, in place (Paeth inlined: it is most of the run time)."""
    stride = len(line)
    if kind == 0:
        return line
    if not any(line):
        # A zero residual repeats the prediction: a blank row under a blank row, the row above
        # under Up, and the row above under Paeth when that row is one colour (a flat background).
        if kind == 1 or not any(prev):
            return line
        if kind == 2 or (kind == 4 and prev[:bpp] * (stride // bpp) == prev):
            return bytearray(prev)
    if kind == 1:
        for i in range(bpp, stride):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif kind == 2:
        for i in range(stride):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif kind == 3:
        for i in range(bpp):
            line[i] = (line[i] + (prev[i] >> 1)) & 0xFF
        for i in range(bpp, stride):
            line[i] = (line[i] + ((line[i - bpp] + prev[i]) >> 1)) & 0xFF
    elif kind == 4:
        for i in range(bpp):
            line[i] = (line[i] + prev[i]) & 0xFF
        for i in range(bpp, stride):
            a, b, c = line[i - bpp], prev[i], prev[i - bpp]
            pa = b - c if b > c else c - b
            pb = a - c if a > c else c - a
            pc = a + b - c - c
            if pc < 0:
                pc = -pc
            if pa <= pb and pa <= pc:
                line[i] = (line[i] + a) & 0xFF
            elif pb <= pc:
                line[i] = (line[i] + b) & 0xFF
            else:
                line[i] = (line[i] + c) & 0xFF
    return line


def read_png(path: Path) -> Tuple[int, int, List[bytes]]:
    """``(width, height, rows)`` with every row as RGBA bytes."""
    data = Path(path).read_bytes()
    if data[:8] != R.PNG_MAGIC:
        raise ValueError("{} is not a PNG".format(path))
    pos, idat, palette, trns = 8, [], b"", b""
    width = height = depth = ctype = interlace = 0
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype, _comp, _filter, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if depth != 8 or interlace:
        raise ValueError("{}: only 8-bit non-interlaced PNGs are read (depth {}, interlace {})".format(path, depth, interlace))
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    bpp = channels
    rows: List[bytes] = []
    prev = bytearray(stride)
    at = 0
    for _ in range(height):
        kind = raw[at]
        line = _unfilter(kind, bytearray(raw[at + 1:at + 1 + stride]), prev, bpp)
        at += 1 + stride
        prev = line
        rows.append(_to_rgba(bytes(line), ctype, palette, trns))
    return width, height, rows


def _to_rgba(line: bytes, ctype: int, palette: bytes, trns: bytes) -> bytes:
    if ctype == 6:
        return line
    out = bytearray()
    if ctype == 2:
        for i in range(0, len(line), 3):
            out += line[i:i + 3] + b"\xff"
    elif ctype == 0:
        for v in line:
            out += bytes((v, v, v, 255))
    elif ctype == 4:
        for i in range(0, len(line), 2):
            out += bytes((line[i], line[i], line[i], line[i + 1]))
    else:
        for index in line:
            alpha = trns[index] if index < len(trns) else 255
            out += palette[3 * index:3 * index + 3] + bytes((alpha,))
    return bytes(out)


# --------------------------------------------------------------------------
# colour


def _luminance(rgb: Sequence[float]) -> float:
    def channel(v: float) -> float:
        v = v / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: Sequence[float], b: Sequence[float]) -> float:
    """The WCAG contrast ratio of two sRGB colours (1 to 21)."""
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _median(values: List[int]) -> int:
    values = sorted(values)
    return values[len(values) // 2] if values else 0


# --------------------------------------------------------------------------
# the throwaway team


class QaTeam:
    """A temporary state root with one team (``qa``) whose canvas is on; one member draws (``author``), and for multi-author
    scenes (canvas v2 phase 5) a peer, a deputy with an operator grant, and the lead (``authors``).

    The environment is built from scratch (like ``tests/support.TempState``), so no call can
    reach the real socket, HOME or state root.
    """

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="canvas-qa-"))
        config_dir = self.tmp / "cfg" / "herdr"
        state_root = self.tmp / "state"
        for d in (self.tmp / "home", config_dir, self.tmp / "st", state_root):
            d.mkdir(parents=True, exist_ok=True)
        self.env: Dict[str, str] = {
            "HOME": os.fspath(self.tmp / "home"),
            "XDG_CONFIG_HOME": os.fspath(self.tmp / "cfg"),
            "XDG_STATE_HOME": os.fspath(self.tmp / "st"),
            "HERDR_TEAM_STATE_DIR": os.fspath(state_root),
            "HERDR_SOCKET_PATH": os.fspath(config_dir / "herdr.sock"),
            "HERDR_ENV": "1",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        self.session = paths.session_paths(state_root, "default")
        paths.ensure_session_dirs(self.session)
        self.team = self.session.team(TEAM)
        paths.ensure_team_dirs(self.team)
        member = {"name": MEMBER, "role": "drawer", "kind": "claude", "terminal_id": "term_qa", "pane_id": "w1:p1",
                  "workspace_id": "w1", "tab_id": "w1:t1", "label": "team:qa/drawer", "cwd": os.fspath(self.tmp),
                  "managed": False, "session": None, "status": "active", "generation": 1, "delivery": "nudge",
                  "joined_at": "2026-09-27T10:00:00Z", "last_seen_at": None, "briefed_at": None, "briefing_seq": None,
                  "charter_seq_acked": None, "brief": None}
        human = {"name": "human", "role": "operator", "kind": "human", "terminal_id": None, "status": "active"}
        others = [dict(member, name=name, role=role, kind=kind, terminal_id="term_" + role, pane_id="w1:p{}".format(n), label="team:qa/" + role)
                  for n, (name, role, kind) in enumerate(((PEER, "peer", "codex"), (DEPUTY, "deputy", "claude")), start=2)]
        store.write_json(self.team.team_json, {
            "schema": 1, "team": TEAM, "created_at": "2026-09-27T10:00:00Z", "socket": self.env["HERDR_SOCKET_PATH"],
            "state_dir": os.fspath(state_root), "naming": "prefixed", "revision": 1,
            "charter": {"seq": 1, "text": "Canvas QA.", "refs": [], "updated_at": "2026-09-27T10:00:00Z", "updated_by": "human"},
            "members": [member] + others + [human]})
        features.set_layer(self.session, True, "human", "cli")
        features.set_team(self.team, enabled=True, by="human", via="cli")
        self.layout = paths.resolve_layout(self.env)
        self.author = C.CanvasAuthor(MEMBER, C.KIND_MEMBER, "cli", True, agent="claude", team=TEAM)
        self.authors: Dict[str, Any] = {
            "drawer": self.author,
            "peer": C.CanvasAuthor(PEER, C.KIND_MEMBER, "cli", True, agent="codex", team=TEAM),
            "deputy": C.CanvasAuthor(DEPUTY, C.KIND_MEMBER, "cli", True, agent="claude", operator=True, team=TEAM),
            "lead": C.CanvasAuthor(C.HUMAN, C.KIND_HUMAN, "cli", True, operator=True),
        }

    def author_for(self, key: Optional[str]) -> Any:
        """``drawer`` (the default), ``peer``, ``deputy`` or ``lead``, or a member's full name."""
        if not key:
            return self.author
        for found in self.authors.values():
            if key == found.name:
                return found
        if key not in self.authors:
            raise ValueError("no QA author called {} (one of {})".format(key, ", ".join(AUTHOR_KEYS)))
        return self.authors[key]

    def seed_artifacts(self, folders: Sequence[str]) -> Path:
        """Give the team a project folder and copy each ``tests/fixtures/canvas_artifacts/<folder>`` into its ``artifacts/``
        (subfolders kept); the team's artifacts folder."""
        from herdr_team import workdir

        project = self.tmp / "project"
        project.mkdir(exist_ok=True)
        doc = store.read_json(self.team.team_json)
        doc.setdefault("config", {})["project_dir"] = os.fspath(project)
        store.write_json(self.team.team_json, doc)
        art = Path(workdir.paths_for(os.fspath(project), TEAM)["artifacts"])
        art.mkdir(parents=True, exist_ok=True)
        for folder in folders:
            source = ARTIFACTS_DIR / folder
            if not source.is_dir():
                raise ValueError("no artifacts folder {} (tests/fixtures/canvas_artifacts)".format(folder))
            shutil.copytree(os.fspath(source), os.fspath(art), dirs_exist_ok=True)
        return art

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self) -> "QaTeam":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.cleanup()


# --------------------------------------------------------------------------
# scenes


def load_scene_file(path: Path) -> Dict[str, Any]:
    """``{"name", "about", "batches", "file"}`` from a scene file; ``ValueError`` when it is malformed."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("{}: a scene is a JSON object".format(path))
    batches = doc.get("batches")
    if batches is None:
        batches = [doc.get("ops")]
    if not isinstance(batches, list) or not batches:
        raise ValueError("{}: a scene needs ops (a non-empty list) or batches (a list of them)".format(path))
    # A batch is a list of ops (drawn by the member), or since canvas v2 phase 5 {"as": drawer|peer|deputy|lead, "ops", "base"?}.
    authors: List[str] = []
    bases: List[Any] = []
    plain: List[List[Dict[str, Any]]] = []
    for batch in batches:
        if isinstance(batch, dict):
            if any(key not in ("as", "ops", "base") for key in batch) or batch.get("as", "drawer") not in AUTHOR_KEYS:
                raise ValueError("{}: a batch object is {{\"as\": {}, \"ops\": [...], \"base\"?}}".format(path, "|".join(AUTHOR_KEYS)))
            authors.append(str(batch.get("as") or "drawer"))
            bases.append(batch.get("base"))
            batch = batch.get("ops")
        else:
            authors.append("drawer")
            bases.append(None)
        if not isinstance(batch, list) or not batch:
            raise ValueError("{}: a scene needs ops (a non-empty list) or batches (a list of them)".format(path))
        plain.append(batch)
    batches = plain
    exempt = doc.get("page_line_exempt") or {}
    if not isinstance(exempt, dict):
        raise ValueError("{}: page_line_exempt maps element ids to the reason their page lines may differ".format(path))
    artifacts = doc.get("artifacts") or []
    if isinstance(artifacts, str):
        artifacts = [artifacts]
    if not isinstance(artifacts, list) or not all(isinstance(a, str) and a and "/" not in a and a not in (".", "..") for a in artifacts):
        raise ValueError("{}: artifacts names folders under tests/fixtures/canvas_artifacts".format(path))
    gates = doc.get("gates") or {}
    if not isinstance(gates, dict) or any(key not in ("strict",) for key in gates):
        raise ValueError("{}: gates is {{\"strict\": true}} (a Phase 2 scene also gates arrow_through, stray and arrow-label collisions)".format(path))
    return {"name": str(doc.get("name") or Path(path).stem), "about": str(doc.get("about") or ""), "batches": batches,
            "as": authors, "bases": bases, "file": os.fspath(path), "page_line_exempt": {str(k): str(v) for k, v in exempt.items()},
            "gates": dict(gates), "artifacts": list(artifacts), "presence": doc.get("presence") or []}


def scene_files(targets: Sequence[str]) -> List[Path]:
    """Scene files from paths, directories, or bare names under the golden set."""
    found: List[Path] = []
    for target in targets or [os.fspath(SCENES_DIR)]:
        path = Path(target)
        if not path.exists() and (SCENES_DIR / (target + ".json")).is_file():
            path = SCENES_DIR / (target + ".json")
        if path.is_dir():
            found.extend(sorted(path.glob("*.json")))
        elif path.is_file():
            found.append(path)
        else:
            raise ValueError("no scene at {}".format(target))
    return found


def apply_scene(qa: QaTeam, scene: Dict[str, Any]) -> Dict[str, Any]:
    """Apply every batch as its author (the member unless a batch says ``as``; after copying the scene's artifacts in); op
    counts, refusals, proposals and warning codes."""
    if scene.get("artifacts"):
        qa.seed_artifacts(scene["artifacts"])
    ops = applied = proposed = 0
    refused: List[Dict[str, Any]] = []
    warnings: Dict[str, int] = {}
    authors = scene.get("as") or ["drawer"] * len(scene["batches"])
    bases = scene.get("bases") or [None] * len(scene["batches"])
    for number, batch in enumerate(scene["batches"]):
        result = C.apply_ops(qa.layout, qa.team, batch, qa.author_for(authors[number]), base=bases[number])
        ops += len(batch)
        applied += len(result.get("applied") or [])
        proposed += len(result.get("proposed") or [])
        for item in result.get("refused") or []:
            refused.append({"batch": number, "index": item.get("index"), "code": item.get("code"), "message": item.get("message")})
        for warning in result.get("warnings") or []:
            code = str(warning.get("code") if isinstance(warning, dict) else warning)
            warnings[code] = warnings.get(code, 0) + 1
    out = {"ops": ops, "applied": applied, "refused": refused, "warnings": warnings}
    if proposed:
        out["proposed"] = proposed
        out["applied"] = applied + proposed  # a proposal is a success (phase 5): the scene applied as its author meant
    return out


def apply_presence(qa: QaTeam, scene: Dict[str, Any], now: Optional[float] = None) -> int:
    """Write a scene's ``presence`` entries (canvas v2 phase 5): ``{"as": drawer|peer|deputy, "focus": {region, intent,
    status, ttl_s}}`` for a member, ``{"human": {page, viewport, selection, editing, cursor}}`` for the operator's page.
    Presence is never part of a golden: it is written only for readers (``look``, the rig's page), with the clock now."""
    from herdr_team import canvas_presence as P

    moment = time.time() if now is None else float(now)
    written = 0
    for item in scene.get("presence") or []:
        if not isinstance(item, dict):
            raise ValueError("a scene's presence entry is {as, focus} or {human}")
        if "human" in item:
            P.write_human(qa.team, None, item["human"], moment)
        else:
            author = qa.author_for(item.get("as"))
            focus = item.get("focus") or {}
            region = C.parse_region(focus["region"], C.load_scene(qa.team), author.name) if focus.get("region") is not None else None
            P.write_member(qa.team, author, status=focus.get("status") or "drawing", region=region, intent=focus.get("intent") or "",
                           ttl_s=int(focus.get("ttl_s") or P.FOCUS_TTL_S), via="focus", now=moment)
        written += 1
    return written


# --------------------------------------------------------------------------
# rendering


def themes_supported() -> List[str]:
    """``["light"]``, plus ``"dark"`` once ``canvas_render.render_svg`` takes a ``theme`` argument."""
    try:
        params = inspect.signature(R.render_svg).parameters
    except (TypeError, ValueError):
        return ["light"]
    return ["light", "dark"] if "theme" in params else ["light"]


def _theme_kw(theme: str) -> Dict[str, Any]:
    """``render_svg``'s theme argument, left out while the renderer has none (light only)."""
    return {"theme": theme} if len(themes_supported()) > 1 else {}


_NO_FONT_RE = re.compile(r"No fonts with a (.*?)/U\+([0-9A-Fa-f]+) character")


def missing_glyphs(stderr: str) -> List[str]:
    """The code points resvg drew as boxes (``U+0628``...), from its "No fonts with a ... character" warnings."""
    return sorted({"U+" + match.group(2).upper() for match in _NO_FONT_RE.finditer(stderr or "")})


def cut_off(scene: Dict[str, Any], box: Sequence[float], units_per_px: float) -> List[str]:
    """Ids of frames whose title and arrows whose label pill reach past the picture ``box``."""
    out = []
    for el in scene.get("elements") or []:
        if not isinstance(el, dict) or el.get("deleted"):
            continue
        drawn: Optional[Sequence[float]] = None
        if el.get("type") == "frame":
            drawn = R.frame_title_box(el, units_per_px)
        elif el.get("type") == "arrow":
            pill = R.arrow_label_pill(el)
            if pill is not None:
                (px, py, pw, ph), _size, _lines = pill
                drawn = (px, py, px + pw, py + ph)
        if drawn is not None and not _contains(tuple(box), tuple(drawn)):  # type: ignore[arg-type]
            out.append(str(el["id"]))
    return out


def render_scene(qa: QaTeam, scene: Dict[str, Any], out: Path, name: str, theme: str) -> Dict[str, Any]:
    """The whole scene as ``look --image`` draws it (id marks on), as ``<name>.svg`` and ``<name>.png``; plus the
    glyphs resvg had no font for and the titles and pills the picture cuts off."""
    box = R.view_box(scene)
    width_px, height_px = R.pixel_size(box, R.DEFAULT_MAX_PX)
    svg = R.render_svg(scene, box, marks=True, max_px=R.DEFAULT_MAX_PX, team=qa.team, **_theme_kw(theme))
    svg_path, png_path = out / (name + ".svg"), out / (name + ".png")
    svg_path.write_text(svg, encoding="utf-8")
    warnings: List[str] = []
    original = R.RUN

    def run(argv: List[str], timeout: float) -> Tuple[int, str]:
        code, err = original(argv, timeout)
        warnings.append(err)
        return code, err

    R.RUN = run
    try:
        png = R.render_png(svg, png_path, width_px)
    finally:
        R.RUN = original
    units_per_px = (box[2] - box[0]) / float(width_px)
    return {"svg": os.fspath(svg_path), "png": os.fspath(png) if png else None, "width_px": width_px, "height_px": height_px,
            "units_per_px": round(units_per_px, 3), "tofu": missing_glyphs("\n".join(warnings)),
            "cut_off": cut_off(scene, box, units_per_px)}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _strip(svg: str, keep_text: bool) -> Tuple[str, bool]:
    """The SVG with only its text (``keep_text``) or with every text removed; and whether any text is clipped or masked."""
    ET.register_namespace("", SVG_NS)
    root = ET.fromstring(svg)
    clipped = False

    def has_text(node: ET.Element) -> bool:
        return _local(node.tag) in TEXT_TAGS or any(has_text(child) for child in node)

    def walk(node: ET.Element, inherited: bool) -> None:
        nonlocal clipped
        masked = inherited or bool(node.get("clip-path") or node.get("mask"))
        for child in list(node):
            tag = _local(child.tag)
            if tag in TEXT_TAGS:
                if keep_text:
                    clipped = clipped or masked or bool(child.get("clip-path") or child.get("mask"))
                else:
                    node.remove(child)
                continue
            if keep_text and tag not in KEEP_TAGS and not has_text(child):
                node.remove(child)
                continue
            walk(child, masked)

    walk(root, False)
    return ET.tostring(root, encoding="unicode"), clipped


def _lines(svg: str) -> List[str]:
    """The lines the server drew: each ``<text>`` (a ``<tspan>`` each in a multi-line one) in document order."""
    root = ET.fromstring(svg)
    out: List[str] = []
    for node in root.iter():
        if _local(node.tag) != "text":
            continue
        spans = [child for child in node if _local(child.tag) == "tspan"]
        if spans:
            out.extend("".join(span.itertext()) for span in spans)
        else:
            out.append("".join(node.itertext()))
    return out


def _fill_of(node: ET.Element, inherited: Optional[str]) -> Optional[str]:
    style = dict(part.split(":", 1) for part in (node.get("style") or "").replace(" ", "").split(";") if ":" in part)
    return style.get("fill") or node.get("fill") or inherited


def _text_fills(svg: str) -> List[str]:
    """The fill every ``<text>`` in the SVG is drawn with (inherited fills resolved), in document order."""
    fills: List[str] = []

    def walk(node: ET.Element, inherited: Optional[str]) -> None:
        fill = _fill_of(node, inherited)
        if _local(node.tag) == "text" and fill:
            fills.append(fill)
        for child in node:
            walk(child, fill)

    walk(ET.fromstring(svg), None)
    return fills


def _hex_rgb(value: str) -> Optional[List[int]]:
    value = value.strip().lower()
    if len(value) == 4 and value.startswith("#"):
        value = "#" + "".join(c * 2 for c in value[1:])
    if len(value) != 7 or not value.startswith("#"):
        return None
    try:
        return [int(value[i:i + 2], 16) for i in (1, 3, 5)]
    except ValueError:
        return None


def _probe_png(svg: str, path: Path, width_px: int) -> Tuple[int, int, List[bytes]]:
    png = R.render_png(svg, path, width_px)
    if png is None:
        raise RuntimeError("resvg failed on {}".format(path))
    try:
        return read_png(png)
    finally:
        for leftover in (png,):
            try:
                os.unlink(leftover)
            except OSError:
                pass


# --------------------------------------------------------------------------
# pixel probes


def _inside(el: Dict[str, Any], x: float, y: float, slack: float) -> bool:
    """Whether a canvas point lies in the element's outline grown by ``slack`` (shrunk when negative)."""
    x0, y0, x1, y1 = K.box_of(el)
    kind = canvas_kinds.outline(el)  # the kind's drawn outline (rect, ellipse or diamond), from the registry
    if kind == "ellipse":
        rx, ry = (x1 - x0) / 2.0 + slack, (y1 - y0) / 2.0 + slack
        if rx <= 0 or ry <= 0:
            return False
        dx, dy = x - (x0 + x1) / 2.0, y - (y0 + y1) / 2.0
        return (dx / rx) ** 2 + (dy / ry) ** 2 <= 1.0
    if kind == "diamond":
        hw, hh = (x1 - x0) / 2.0 + slack, (y1 - y0) / 2.0 + slack
        if hw <= 0 or hh <= 0:
            return False
        return abs(x - (x0 + x1) / 2.0) / hw + abs(y - (y0 + y1) / 2.0) / hh <= 1.0
    return x0 - slack <= x <= x1 + slack and y0 - slack <= y <= y1 + slack


def _contains(outer: Box, inner: Box) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _text_size(el: Dict[str, Any]) -> float:
    style = el.get("style") if isinstance(el.get("style"), dict) else {}
    size = float(style.get("size") or 20)
    return size * 0.8 if el.get("type") == "arrow" else size


def probe(qa: QaTeam, scene: Dict[str, Any], el: Dict[str, Any], solid: List[Dict[str, Any]], work: Path, theme: str,
          scale: float, tolerance: float) -> Dict[str, Any]:
    """Pixel facts about one element's text: overflow, collisions, contrast and its drawn lines."""
    base = K.box_of(el)
    size = _text_size(el)
    margin = max(80.0, 4.0 * size, 0.5 * max(base[2] - base[0], base[3] - base[1]))
    alone = {"elements": [el], "authors": scene.get("authors") or {}}
    for _attempt in range(4):
        region = (base[0] - margin, base[1] - margin, base[2] + margin, base[3] + margin)
        longest = max(region[2] - region[0], region[3] - region[1])
        px = int(min(PROBE_MAX_PX, math.ceil(longest * scale)))
        ink_svg, clipped = _strip(R.render_svg(alone, region, marks=False, max_px=px, team=qa.team, **_theme_kw(theme)), True)
        width, height, ink = _probe_png(ink_svg, work / "ink.png", R.pixel_size(region, px)[0])
        edge = any(row[3] >= INK_ALPHA or row[-1] >= INK_ALPHA for row in ink) or \
            max(ink[0][3::4]) >= INK_ALPHA or max(ink[-1][3::4]) >= INK_ALPHA
        if not edge:
            break
        margin *= 2.5
    sx, sy = (region[2] - region[0]) / float(width), (region[3] - region[1]) / float(height)
    lines = _lines(ink_svg)
    pixels: List[Tuple[float, float, int, int]] = []  # canvas x, y, row, column
    for j, row in enumerate(ink):
        alphas = row[3::4]
        if max(alphas) < INK_ALPHA:
            continue
        cy = region[1] + (j + 0.5) * sy
        for i, a in enumerate(alphas):
            if a >= INK_ALPHA:
                pixels.append((region[0] + (i + 0.5) * sx, cy, j, i))
    found: Dict[str, Any] = {"id": el["id"], "type": el.get("type"), "lines": lines, "ink_px": len(pixels), "clipped": clipped,
                             "reaches_probe_edge": edge}
    if not pixels:
        return found
    ink_box = (min(p[0] for p in pixels), min(p[1] for p in pixels), max(p[0] for p in pixels), max(p[1] for p in pixels))
    found["ink_box"] = [round(v, 1) for v in ink_box]
    slack = tolerance + max(sx, sy)
    if el.get("type") != "arrow":
        outside = [p for p in pixels if not _inside(el, p[0], p[1], slack)]
        if len(outside) >= MIN_PIXELS:
            sides = []
            for side, past in (("left", ink_box[0] < base[0] - slack), ("top", ink_box[1] < base[1] - slack),
                               ("right", ink_box[2] > base[2] + slack), ("bottom", ink_box[3] > base[3] + slack)):
                if past:
                    sides.append(side)
            reach = max(base[0] - ink_box[0], base[1] - ink_box[1], ink_box[2] - base[2], ink_box[3] - base[3], 0.0)
            found["overflow"] = {"px": len(outside), "units": round(reach, 1), "sides": sides or ["outline"]}
    # Marks the ink lands on: solid marks it is not inside of (a label on its own panel is a grouping).
    hits = []
    for other in solid:
        if other["id"] == el["id"]:
            continue
        box = K.box_of(other)
        if box[0] > ink_box[2] or box[2] < ink_box[0] or box[1] > ink_box[3] or box[3] < ink_box[1]:
            continue
        if el.get("type") != "arrow" and _contains(box, base):
            continue
        count = sum(1 for p in pixels if _inside(other, p[0], p[1], -slack))
        if count >= MIN_PIXELS:
            hits.append({"other": other["id"], "other_type": other.get("type"), "px": count})
    if hits:
        found["collisions"] = hits
    # Contrast: the core of the glyphs against what the scene draws under them with every text removed.
    core = [(p[2], p[3]) for p in pixels if ink[p[2]][4 * p[3] + 3] >= CORE_ALPHA]
    if len(core) < MIN_PIXELS:
        top = max(ink[p[2]][4 * p[3] + 3] for p in pixels)
        core = [(p[2], p[3]) for p in pixels if ink[p[2]][4 * p[3] + 3] >= 0.8 * top]
    # Only the ink's own box is drawn for the background: the rest of the region never matters here.
    under = (ink_box[0] - sx, ink_box[1] - sy, ink_box[2] + 2 * sx, ink_box[3] + 2 * sy)
    under_px = max(1, int(math.ceil(max(under[2] - under[0], under[3] - under[1]) / min(sx, sy))))
    bg_svg, _ = _strip(R.render_svg(scene, under, marks=False, max_px=under_px, team=qa.team, **_theme_kw(theme)), False)
    bw, bh, bg = _probe_png(bg_svg, work / "bg.png", R.pixel_size(under, under_px)[0])
    bsx, bsy = (under[2] - under[0]) / float(bw), (under[3] - under[1]) / float(bh)

    def under_at(j: int, i: int) -> bytes:
        x, y = region[0] + (i + 0.5) * sx, region[1] + (j + 0.5) * sy
        row = bg[min(bh - 1, max(0, int((y - under[1]) / bsy)))]
        col = min(bw - 1, max(0, int((x - under[0]) / bsx)))
        return row[4 * col:4 * col + 4]

    # The ink colour is the fill the text is drawn with; the pixels decide only when the SVG does not
    # say (a colour emoji's pixels are not the text colour, and thin strokes blend into the background).
    fills = {f for f in _text_fills(ink_svg)}
    several = len(fills) > 1
    ink_rgb = _hex_rgb(fills.pop()) if len(fills) == 1 else None
    if ink_rgb is None:
        ink_rgb = [_median([ink[j][4 * i + c] for j, i in core]) for c in range(3)]
    below = [under_at(j, i) for j, i in core]
    bg_rgb = [_median([px_[c] for px_ in below]) for c in range(3)]
    ratio = contrast(ink_rgb, bg_rgb)
    if several:
        # Texts in several colours on several fills (a chart: ink on the paper, labels on bars): each glyph pixel against
        # what is under it, then the median, since a median ink against a median background is neither.
        ratios = sorted(contrast(list(ink[j][4 * i:4 * i + 3]), list(px_[:3])) for (j, i), px_ in zip(core, below))
        ratio = ratios[len(ratios) // 2]
    need = CONTRAST_LARGE if size >= LARGE_TEXT else CONTRAST_BODY
    found["contrast"] = {"ratio": round(ratio, 2), "need": need, "ink": "#%02x%02x%02x" % tuple(ink_rgb),
                         "under": "#%02x%02x%02x" % tuple(bg_rgb)}
    return found


# --------------------------------------------------------------------------
# one scene


def _text_of(el: Dict[str, Any]) -> str:
    text = " ".join(str(el.get("text") or "").split())
    return text if len(text) <= 48 else text[:47] + "…"


def evaluate(scene_doc: Dict[str, Any], out: Path, scale: float = PROBE_SCALE, tolerance: float = TOLERANCE,
             themes: Optional[Sequence[str]] = None, page_lines: Optional[Dict[str, List[str]]] = None,
             strict: bool = False, serve: float = 0.0, browser: Optional[Browser] = None, engine: str = "v2") -> Dict[str, Any]:
    """Apply, check, render and probe one scene; its report (``gate.ok`` says whether it passes)."""
    name = scene_doc["name"]
    themes = list(themes or themes_supported())
    # A scene may gate more than the run does (phase 2: its own ``gates``, strict for every scene built on blocks and routes).
    strict = bool(strict or (scene_doc.get("gates") or {}).get("strict"))
    report: Dict[str, Any] = {"scene": name, "file": scene_doc["file"], "themes": themes, "strict": strict}
    with QaTeam() as qa:
        report.update(apply_scene(qa, scene_doc))
        if scene_doc.get("presence"):
            apply_presence(qa, scene_doc)
        scene = C.load_scene(qa.team)
        report["look_collab"] = look_findings(qa, scene_doc, scene)
        live = [e for e in scene.get("elements") or [] if isinstance(e, dict) and not e.get("deleted")]
        report["elements"] = len(live)
        problems = C.check(qa.layout, qa.team, MEMBER)["problems"]
        by_code: Dict[str, int] = {}
        for problem in problems:
            by_code[problem["code"]] = by_code.get(problem["code"], 0) + 1
        report["check"] = by_code
        report["check_problems"] = [{"code": p["code"], "ids": p["ids"], "message": p["message"]} for p in problems]
        solid = [e for e in live if e.get("type") in K.SOLID]
        # Arrow labels also keep clear of each other's pills (QA R-3: they must never run into other text).
        pills = [dict(id=e["id"] + "~label", type="label", of=e["id"], x=b[0][0], y=b[0][1], w=b[0][2], h=b[0][3])
                 for e in live if e.get("type") == "arrow" for b in [R.arrow_label_pill(e)] if b is not None]
        # Every element that draws text: labelled kinds and arrows by their text, and any other entry whose display list
        # holds text (a table's cells, a sequence's names and messages: QA phase 2, F14).
        drawn_text = _entries_with_text(scene)
        texts = [e for e in live if ((e.get("type") in LABELLED or e.get("type") == "arrow") and str(e.get("text") or "").strip())
                 or (e["id"] in drawn_text and e.get("type") not in LABELLED and e.get("type") != "arrow" and e.get("type") != "frame")]
        work = Path(tempfile.mkdtemp(prefix="probe-", dir=os.fspath(qa.tmp)))
        report["renders"] = {}
        report["pixel"] = {}
        for theme in themes:
            suffix = "" if theme == "light" else "-" + theme
            report["renders"][theme] = render_scene(qa, scene, out, name + suffix, theme)
            probes = [probe(qa, scene, el, solid + [p for p in pills if p["of"] != el["id"]] if el.get("type") == "arrow" else solid,
                            work, theme, scale, tolerance) for el in texts]
            by_id = {el["id"]: el for el in texts}
            overflow = [dict(p["overflow"], id=p["id"], type=p["type"], text=_text_of(by_id[p["id"]])) for p in probes if p.get("overflow")]
            collisions, arrow_hits = [], []
            for p in probes:
                for hit in p.get("collisions") or []:
                    row = dict(hit, id=p["id"], type=p["type"], text=_text_of(by_id[p["id"]]))
                    (arrow_hits if p["type"] == "arrow" else collisions).append(row)
            low = [dict(p["contrast"], id=p["id"], type=p["type"]) for p in probes
                   if p.get("contrast") and p["contrast"]["ratio"] < p["contrast"]["need"]]
            ratios = [p["contrast"]["ratio"] for p in probes if p.get("contrast")]
            report["pixel"][theme] = {
                "probed": len(probes), "overflow": overflow, "collisions": collisions, "arrow_label_collisions": arrow_hits,
                "low_contrast": low, "min_contrast": min(ratios) if ratios else None,
                "clipped": [p["id"] for p in probes if p.get("clipped")],
                "no_ink": [p["id"] for p in probes if not p["ink_px"]],
            }
            if theme == themes[0]:
                report["lines"] = {p["id"]: p["lines"] for p in probes}
        # The v1 (Excalidraw) page draws only the element types it has builders for (phase 1 D8; a block stored as a frame
        # is a frame there, D2): a scene with any other type is checked on the v2 page only.
        foreign = sorted({str(e.get("type")) for e in live
                          if canvas_kinds.get(e.get("type")) is None or not canvas_kinds.get(e.get("type")).page})
        if browser is not None and engine == "v1" and foreign:
            report["page_skipped"] = "the v1 page does not draw {}".format(", ".join(foreign))
        elif browser is not None:
            shot = out / (name + ("-page-v2.png" if engine == "v2" else "-page.png"))
            report["page_png"] = os.fspath(shot)
            report["engine"] = engine
            page_lines = read_page_lines(qa, browser, shot, engine)
            report["page_lines_read"] = page_lines
            report["page_overflow"] = [row for row in browser.audit if row.get("type") != "arrow" and (row.get("overflowPx") or 0) > 0.5]
            # The v2 hook also reports lines whose whitespace the browser collapsed (a lost indent: phase 1 QA #4).
            report["page_collapsed"] = [row for row in browser.audit if (row.get("collapsed") or 0) > 0]
            if engine == "v2":
                report["page_filters"] = list(getattr(browser, "filters", []) or [])
                report["page_hooks"] = hook_findings(getattr(browser, "hooks", None) or {}, chart_frame_texts(live))
                report["page_collab"] = collab_findings(getattr(browser, "hooks", None) or {}, collab_expected(qa))
        if page_lines is not None:
            report["page_lines"] = compare_lines(report["lines"], page_lines, scene_doc.get("page_line_exempt"))
        if serve > 0:
            serve_scene(qa, serve)
    check_ids = sorted({i for p in problems if p["code"] == "label_overflow" for i in p["ids"]})
    pixel_ids = sorted({o["id"] for t in themes for o in report["pixel"][t]["overflow"]})
    report["label_overflow"] = {"check": check_ids, "pixel": pixel_ids, "count": len(set(check_ids) | set(pixel_ids))}
    report["png"] = report["renders"][themes[0]]["png"]
    report["gate"] = gate(report, strict)
    return report


def _entries_with_text(scene: Dict[str, Any]) -> set:
    """The ids of the display list's entries that draw any text line."""
    from herdr_team import canvas_display

    def has_text(items: Any) -> bool:
        return any(isinstance(p, dict) and ((p.get("k") == "text" and p.get("lines")) or has_text(p.get("items"))) for p in items or [])

    return {e["id"] for e in canvas_display.display_list(scene).get("entries") or [] if has_text(e.get("items"))}


def compare_lines(server: Dict[str, List[str]], page: Dict[str, List[str]], exempt: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Line breaks that differ between the server's drawing and the page's.

    Every line counts, blank ones too, and so do indentation and runs of spaces (a drawing that
    dropped them read the same when whitespace was collapsed); only trailing spaces are ignored. The
    page's lines go through ``canvas_render.render_text`` first: the emoji the server draws on purpose
    without joiners and skin tones, and flags as letters, are not a difference. ``exempt`` names
    elements whose lines may differ (a scene's ``page_line_exempt``: scripts the metrics only
    estimate); their differences are listed apart and do not fail the gate.
    """
    def norm(lines: Iterable[str]) -> List[str]:
        return [R.render_text(str(line)).rstrip() for line in lines]

    exempt = exempt or {}
    mismatches, excused = [], []
    compared = 0
    for eid, lines in sorted(server.items()):
        if eid not in page:
            continue
        compared += 1
        if norm(lines) != norm(page[eid]):
            row = {"id": eid, "server": lines, "page": page[eid]}
            (excused if eid in exempt else mismatches).append(dict(row, reason=exempt[eid]) if eid in exempt else row)
    missing = sorted(set(server) - set(page))
    return {"compared": compared, "mismatches": mismatches, "exempt": excused, "missing_on_page": missing}


def chart_frame_texts(elements: Sequence[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Per flat ECharts chart, the axis labels, axis title and legend names its frame fixed (``chart_frame``): the texts
    the page's ECharts picture must draw too, since Python laid its axes out for both pictures (phase 3, 2.5). A label the
    frame thins away (``interval``) is not expected; a truncated one (``…``) is compared by what precedes the ellipsis."""
    out: Dict[str, List[str]] = {}
    for el in elements:
        frame = el.get("chart_frame") if el.get("engine") == "echarts" else None
        if not isinstance(frame, dict):
            continue
        want: List[str] = []
        for axis in (frame.get("axes") or {}).values():
            if not isinstance(axis, dict):
                continue
            every = int(axis.get("interval") or 0) + 1
            for item in axis.get("labels") or []:
                if isinstance(item, dict) and int(item.get("v") or 0) % every == 0 and item.get("t"):
                    want.append(str(item["t"]))
            want += [str(t["t"]) for t in axis.get("ticks") or [] if isinstance(t, dict) and t.get("t")]
            if axis.get("title"):
                want.append(str(axis["title"]))
        legend = frame.get("legend")
        for item in (legend.get("items") or []) if isinstance(legend, dict) else []:
            if isinstance(item, dict) and item.get("name"):
                want.append(str(item["name"]))
        if want:
            out[str(el["id"])] = want
    return out


def _drawn(text: str, texts: Sequence[str]) -> bool:
    if text in texts:
        return True
    if text.endswith("…"):
        stem = text[:-1].rstrip()
        return any(str(t).startswith(stem) for t in texts)
    return False


def hook_findings(hooks: Dict[str, Any], frame_texts: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
    """What the v2 page's chart and scene hooks say, per theme: every chart rendered with no overlapping labels, its
    ECharts picture drawing every axis label, axis title and legend name its frame fixed (``frame_texts``; the chart
    slot keeps the Python drawing's text hidden beneath the picture, so the page-line comparison cannot see the chart's
    own text: QA phase34 L9), and every visible scene rendered (canvas v2 phases 3 and 4, G4). A page built before the
    hooks reports ``absent``."""
    out: Dict[str, Any] = {"problems": [], "charts": 0, "scenes": 0, "chart_texts_compared": 0}
    frame_texts = frame_texts or {}
    seen = False
    for theme, found in sorted(hooks.items()):
        charts = found.get("charts") if isinstance(found, dict) else None
        scene3d = found.get("scene3d") if isinstance(found, dict) else None
        if charts is None and scene3d is None:
            continue
        seen = True
        for chart in charts or []:
            out["charts"] = max(out["charts"], len(charts))
            if chart.get("failed") or not chart.get("rendered"):
                out["problems"].append("chart {} not rendered ({}): {}".format(chart.get("id"), theme, chart.get("failed") or "still waiting"))
            elif chart.get("labelOverlaps"):
                out["problems"].append("chart {} has {} overlapping labels ({})".format(chart.get("id"), chart["labelOverlaps"], theme))
            want = frame_texts.get(str(chart.get("id")))
            if want and chart.get("rendered") and isinstance(chart.get("texts"), list):
                out["chart_texts_compared"] += 1
                missing = [t for t in want if not _drawn(t, chart["texts"])]
                if missing:
                    out["problems"].append("chart {} does not draw {} of its frame's labels ({}): {}".format(
                        chart.get("id"), len(missing), theme, ", ".join(repr(t) for t in missing[:6])))
        scenes = (scene3d or {}).get("scenes") if isinstance(scene3d, dict) else None
        for scene in scenes or []:
            out["scenes"] = max(out["scenes"], len(scenes))
            if scene.get("visible") and not scene.get("rendered"):
                out["problems"].append("scene {} not rendered ({})".format(scene.get("id"), theme))
    if not seen:
        out["absent"] = True
    return out


def collab_expected(qa: QaTeam) -> Dict[str, int]:
    """What the page's ``collab()`` hook must count on this board (I-8): the display list's proposals, outdated ones and
    frozen entries, and one halo per fresh member presence."""
    from herdr_team import canvas_presence

    entries = C.display(qa.team)["entries"]
    members = [d for d in canvas_presence.read_all(qa.team, time.time())["entries"] if d.get("kind") == "member"]
    return {"proposals": sum(1 for e in entries if e.get("kind") == "proposal"),
            "outdated": sum(1 for e in entries if e.get("kind") == "proposal" and (e.get("proposal") or {}).get("outdated")),
            "frozen": sum(1 for e in entries if e.get("frozen")), "halos": len(members)}


def collab_findings(hooks: Dict[str, Any], expected: Dict[str, int]) -> Dict[str, Any]:
    """The v2 page's ``collab()`` counts (canvas v2 phase 5, I-8) against the display list, per theme. A page built before
    the hook reports ``absent``."""
    out: Dict[str, Any] = {"problems": [], "expected": expected}
    seen = False
    for theme, found in sorted(hooks.items()):
        counts = found.get("collab") if isinstance(found, dict) else None
        if not isinstance(counts, dict):
            continue
        seen = True
        out[theme] = counts
        for key, want in sorted(expected.items()):
            if key in counts and counts[key] != want:
                out["problems"].append("collab {} {} drawn, {} in the display list ({})".format(key, counts[key], want, theme))
    if not seen:
        out["absent"] = True
    return out


def look_findings(qa: QaTeam, scene_doc: Dict[str, Any], scene: Dict[str, Any]) -> Dict[str, Any]:
    """G7: ``look`` as the drawer says what the collaboration state is: the operator and the peers present, the open
    proposals, the drawer's decided ones, freezes, settings and checkpoints, and the element tags (canvas v2 phase 5)."""
    if not (scene.get("proposals") or scene.get("freezes") or scene.get("checkpoints") or scene_doc.get("presence")):
        return {"problems": []}
    text = C.look(qa.layout, qa.team, MEMBER, advance=False)["text"]
    lines = text.splitlines()
    want = []
    if any("human" in item for item in scene_doc.get("presence") or []):
        want.append("operator: ")
    if any("focus" in item for item in scene_doc.get("presence") or []):
        want.append("here: ")
    if any(p.get("status") == "open" for p in scene.get("proposals") or []):
        want += ["proposals (", "[proposal P-"]
    if scene.get("freezes"):
        want += ["frozen: ", " [frozen]"]
    if scene.get("proposals") or scene.get("freezes"):
        want.append("settings: ")
    if scene.get("checkpoints"):
        want.append("checkpoints: ")
    problems = ["look as {} lacks {!r}".format(MEMBER, piece) for piece in want if not any(piece in line for line in lines)]
    return {"problems": problems, "checked": want}  # the lines themselves hold ages: never in a report that must repeat


def gate(report: Dict[str, Any], strict: bool) -> Dict[str, Any]:
    """Every reason this scene fails, gated counts first; ``ok`` when there is none."""
    failed: List[str] = []
    if report.get("refused"):
        failed.append("refused {} op(s): the scene itself is invalid".format(len(report["refused"])))
    codes = GATED_CHECKS + (TRACKED_CHECKS if strict else ())
    for code in codes:
        if report["check"].get(code):
            failed.append("check {} {}".format(code, report["check"][code]))
    for theme, pixel in report["pixel"].items():
        for key in ("overflow", "collisions", "low_contrast", "clipped") + (("arrow_label_collisions",) if strict else ()):
            if pixel.get(key):
                failed.append("pixel {} {} ({})".format(key, len(pixel[key]), theme))
    for theme, render in (report.get("renders") or {}).items():
        for key in ("tofu", "cut_off"):
            if render.get(key):
                failed.append("render {} {} ({})".format(key, len(render[key]), theme))
    page = report.get("page_lines")
    if page and page["mismatches"]:
        failed.append("page line breaks differ on {} of {}".format(len(page["mismatches"]), page["compared"]))
    if report.get("page_overflow"):
        failed.append("page overflow {}".format(len(report["page_overflow"])))
    if report.get("page_collapsed"):
        failed.append("page collapsed whitespace on {}".format(", ".join(r["id"] for r in report["page_collapsed"])))
    if report.get("page_filters"):
        failed.append("page css filter on {} (AC-4.2: nothing inverts colours)".format(", ".join(report["page_filters"])))
    for line in (report.get("page_hooks") or {}).get("problems") or []:
        failed.append("page {}".format(line))
    for line in (report.get("page_collab") or {}).get("problems") or []:
        failed.append("page {}".format(line))
    for line in (report.get("look_collab") or {}).get("problems") or []:
        failed.append(line)
    return {"ok": not failed, "failed": failed}


def serve_scene(qa: QaTeam, seconds: float) -> None:
    """Serve the scene's whiteboard page on loopback for ``seconds`` and print a one-use URL (for page checks)."""
    import threading
    from unittest import mock

    from herdr_team import activity, views
    from herdr_team import whiteboard_server as W

    with mock.patch.object(views, "team_views", side_effect=lambda *a, **k: []), \
            mock.patch.object(activity, "cards", side_effect=lambda *a, **k: []):
        server = W.make_server(qa.layout, qa.env, port=0, writable=False, api=None)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print("page: {}".format(W.mint_ticket(qa.layout, False, "human", port=port)), flush=True)
        try:
            time.sleep(seconds)
        finally:
            server.shutdown()
            server.server_close()


# --------------------------------------------------------------------------
# the page (headless Chrome over the DevTools protocol)

#: Reads the line breaks the page drew off Excalidraw's scene: each Synapse text (a free text, or a
#: shape's or an arrow's bound label) keeps its soft wraps in ``text`` and the source in ``originalText``.
PAGE_LINES_JS = r"""(() => {
  if (document.fonts && document.fonts.status !== 'loaded') return 'fonts loading';
  const root = document.querySelector('.excalidraw');
  if (!root) return 'no canvas';
  const key = Object.keys(root).find((k) => k.startsWith('__reactFiber$'));
  let fiber = key ? root[key] : null;
  while (fiber && !(fiber.stateNode && fiber.stateNode.scene && typeof fiber.stateNode.scene.getNonDeletedElements === 'function')) fiber = fiber.return;
  if (!fiber) return 'no scene';
  const out = {};
  for (const el of fiber.stateNode.scene.getNonDeletedElements()) {
    const s = el.customData && el.customData.synapse;
    if (el.type === 'text' && s && s.id && (s.derived === null || s.derived === 'text' || s.derived === 'label')) out[s.id] = String(el.text).split('\n');
  }
  return out;
})()"""
#: How many elements the v1 page's scene holds (a scene whose elements draw no text still settles).
PAGE_COUNT_JS = r"""(() => {
  const root = document.querySelector('.excalidraw');
  const key = root ? Object.keys(root).find((k) => k.startsWith('__reactFiber$')) : null;
  let fiber = key ? root[key] : null;
  while (fiber && !(fiber.stateNode && fiber.stateNode.scene && typeof fiber.stateNode.scene.getNonDeletedElements === 'function')) fiber = fiber.return;
  return fiber ? fiber.stateNode.scene.getNonDeletedElements().length : 0;
})()"""
#: Fits the whole scene into the viewport before a screenshot (Excalidraw's own zoom-to-fit).
#: The page's own fit audit (``window.__synapseFitAudit``, ``web/src/canvas/adapter.js``): per fitted label, how far
#: its text runs past the room its container gives it.
PAGE_AUDIT_JS = r"""(() => (window.__synapseFitAudit ? window.__synapseFitAudit() : []))()"""
PAGE_FIT_JS = r"""(() => {
  const root = document.querySelector('.excalidraw');
  const key = root && Object.keys(root).find((k) => k.startsWith('__reactFiber$'));
  let fiber = key ? root[key] : null;
  while (fiber && !(fiber.stateNode && typeof fiber.stateNode.scrollToContent === 'function')) fiber = fiber.return;
  if (!fiber) return false;
  fiber.stateNode.scrollToContent(undefined, { fitToViewport: true, viewportZoomFactor: 0.9, animate: false });
  return true;
})()"""
#: The v2 page (``?engine=v2``, canvas v2 phase 1) answers through its QA hook ``window.__synapseV2``
#: (``web/src/v2/render/qa.js``): ``ready()``, ``lines()`` read off its rendered ``<text>`` nodes, ``audit()`` (each line's
#: length against its box) and ``fit()`` (the camera fitted to the display list's bbox).
PAGE_V2_LINES_JS = r"""(() => {
  const qa = window.__synapseV2;
  if (!qa) return 'no v2 page';
  if (!qa.ready()) return 'not ready';
  return qa.lines();
})()"""
PAGE_V2_AUDIT_JS = r"""(() => (window.__synapseV2 ? window.__synapseV2.audit() : []))()"""
#: The v2 page's lines are read at full detail (``PAGE_V2_SCALE``, at or above every level-of-detail band, where the page
#: draws what the probe renders), tile by tile over the board so no entry is culled (QA phase 2, F14): a board bigger
#: than the screen at 0.35 shows skeletons at fit, not text.
PAGE_V2_SCALE = 1.0
PAGE_V2_MAX_TILES = 400
PAGE_V2_TILES_JS = r"""(() => {
  const qa = window.__synapseV2;
  if (!qa || !qa.ready()) return null;
  const dl = qa.dl();
  const cam = qa.camera();
  return {bbox: dl.bbox, w: window.innerWidth, h: window.innerHeight, scale: cam ? cam.scale : null};
})()"""
PAGE_V2_FIT_JS = r"""(() => (window.__synapseV2 ? (window.__synapseV2.fit(), true) : false))()"""
#: AC-4.2: nothing on the page inverts colours (Phase 0's dark mode was an ``invert`` filter over the canvas). The display
#: list's own elevation shadows (``elev``: ``filter="url(#synapse-elev-N)"`` on a card or a sticky, phase 2) change no colour.
#: The v2 page's QA hooks for what it draws itself (canvas v2 phases 3 and 4, ``registerQA``): each chart's and scene's
#: state; null for a page built before them.
PAGE_V2_HOOKS_JS = r"""(() => {
  const qa = window.__synapseV2 || {};
  const read = (name) => (typeof qa[name] === 'function' ? qa[name]() : null);
  return {charts: read('charts'), scene3d: read('scene3d'), collab: read('collab')};
})()"""
#: Seconds the page gets to draw its charts and scenes (lazy chunks, WebGL) before their hooks are read.
HOOKS_WAIT_S = 20.0
PAGE_FILTERS_JS = r"""(() => [...document.querySelectorAll('*')].filter((el) => {
    const found = getComputedStyle(el).filter;
    return found !== 'none' && !/^url\("?#synapse-elev-\d+"?\)$/.test(found);
  })
  .map((el) => el.tagName.toLowerCase() + (el.className && typeof el.className === 'string' ? '.' + el.className.split(' ')[0] : '')).slice(0, 5))()"""
ENGINES = ("v1", "v2")
CHROME_PATHS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "google-chrome", "chromium", "chromium-browser")
PAGE_WAIT_S = 20.0


def find_chrome(explicit: Optional[str] = None) -> Optional[str]:
    """``--page PATH``, ``$CHROME``, the macOS app, or a Chrome/Chromium on ``PATH``.

    A path given on the command line must be a browser: falling back from it silently once ran a whole gate on the
    default engine because a shell quoting slip landed a flag in ``--page`` (``--page "--engine v1"``).
    """
    given = [explicit] if explicit else []
    for candidate in given + [os.environ.get("CHROME") or ""] + list(CHROME_PATHS):
        if not candidate:
            continue
        found = candidate if os.path.isfile(candidate) else shutil.which(candidate)
        if found and os.access(found, os.X_OK):
            return found
        if candidate in given:
            raise SystemExit("canvas_qa: --page {!r} is not a browser (a path, or a name on PATH)".format(candidate))
    return None


class Cdp:
    """The smallest DevTools client that works: one WebSocket to one page target, calls in order."""

    def __init__(self, ws_url: str, timeout: float = 30.0) -> None:
        import base64
        import socket
        from urllib.parse import urlparse

        url = urlparse(ws_url)
        self.sock = socket.create_connection((url.hostname, url.port), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET {} HTTP/1.1\r\nHost: {}:{}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Key: {}\r\nSec-WebSocket-Version: 13\r\n\r\n").format(url.path, url.hostname, url.port, key).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("DevTools closed the connection during the handshake")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError("DevTools refused the WebSocket: {}".format(head.split(b"\r\n", 1)[0].decode(errors="replace")))
        self.next_id = 0

    def _exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("DevTools closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, payload: bytes, opcode: int = 1) -> None:
        mask = os.urandom(4)
        n = len(payload)
        head = bytes((0x80 | opcode,)) + (bytes((0x80 | n,)) if n < 126 else bytes((0x80 | 126,)) + struct.pack(">H", n)
                                           if n < 65536 else bytes((0x80 | 127,)) + struct.pack(">Q", n))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _message(self) -> Dict[str, Any]:
        parts = b""
        while True:
            b0, b1 = self._exact(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            payload = self._exact(n)
            opcode = b0 & 0x0F
            if opcode == 8:
                raise RuntimeError("DevTools closed the connection")
            if opcode == 9:
                self._send(payload, 10)
                continue
            parts += payload
            if b0 & 0x80:
                return json.loads(parts.decode("utf-8"))

    def call(self, method: str, **params: Any) -> Dict[str, Any]:
        self.next_id += 1
        self._send(json.dumps({"id": self.next_id, "method": method, "params": params}).encode())
        while True:
            message = self._message()
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise RuntimeError("{}: {}".format(method, message["error"]))
                return message.get("result") or {}

    def evaluate(self, expression: str) -> Any:
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        return (result.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Browser:
    """A throwaway headless Chrome (its own profile under a temp dir); ``close`` kills only this process."""

    def __init__(self, binary: str) -> None:
        self.binary = binary
        self._start()

    def _start(self) -> None:
        import subprocess
        from urllib.request import urlopen

        binary = self.binary
        self.profile = Path(tempfile.mkdtemp(prefix="canvas-qa-chrome-"))
        #: The page's fit audit, read with the last settled lines.
        self.audit: List[Dict[str, Any]] = []
        self.proc = subprocess.Popen([binary, "--headless=new", "--remote-debugging-port=0", "--user-data-dir=" + os.fspath(self.profile),
                                      "--no-first-run", "--no-default-browser-check", "--window-size=1400,900", "about:blank"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = self.profile / "DevToolsActivePort"
        deadline = time.monotonic() + 15
        while not (port_file.is_file() and port_file.read_text().strip()):
            if time.monotonic() > deadline or self.proc.poll() is not None:
                self.close()
                raise RuntimeError("Chrome did not open its DevTools port")
            time.sleep(0.1)
        port = int(port_file.read_text().split()[0])
        with urlopen("http://127.0.0.1:{}/json/list".format(port), timeout=10) as reply:
            targets = json.loads(reply.read().decode())
        page = next(t for t in targets if t.get("type") == "page")
        self.cdp = Cdp(page["webSocketDebuggerUrl"])
        self.cdp.call("Page.enable")
        self.cdp.call("Runtime.enable")

    def lines(self, url: str, shot: Optional[Path] = None, engine: str = "v2") -> Dict[str, List[str]]:
        """Open ``url`` and read the page's line breaks once they hold still (fonts loaded, two equal reads)."""
        v2 = engine == "v2"
        self.filters: List[str] = []
        self.cdp.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "light"}])
        self.cdp.call("Page.navigate", url=url + "&engine=" + ("v2" if v2 else "v1"))
        deadline, last = time.monotonic() + PAGE_WAIT_S, None
        while time.monotonic() < deadline:
            time.sleep(0.5)
            value = self.cdp.evaluate(PAGE_V2_LINES_JS if v2 else PAGE_LINES_JS)
            # A board of charts has no text the v1 page lays out: no lines, but its elements have arrived.
            drawn = bool(value) or (not v2 and isinstance(value, dict) and (self.cdp.evaluate(PAGE_COUNT_JS) or 0) > 0)
            if isinstance(value, dict) and drawn and value == last:
                audit = self.cdp.evaluate(PAGE_V2_AUDIT_JS if v2 else PAGE_AUDIT_JS)
                self.audit = audit if isinstance(audit, list) else []
                if v2:
                    value, self.audit = self._tiled_v2(value, self.audit)
                if shot is not None:
                    self.cdp.evaluate(PAGE_V2_FIT_JS if v2 else PAGE_FIT_JS)
                    time.sleep(0.5)
                    if v2:
                        self.hooks = {"light": self._hooks()}
                    self.screenshot(shot)
                    self.cdp.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
                    time.sleep(0.5)
                    if v2:
                        self.hooks["dark"] = self._hooks()
                    self.screenshot(shot.with_name(shot.stem + "-dark.png"))
                    if v2:
                        found = self.cdp.evaluate(PAGE_FILTERS_JS)
                        self.filters = found if isinstance(found, list) else []
                return value
            last = value
        raise RuntimeError("the page never settled: {}".format(last if isinstance(last, str) else "lines kept changing"))

    def _hooks(self) -> Dict[str, Any]:
        """The page's chart and scene hooks once every chart has rendered or failed (or the wait is over)."""
        deadline = time.monotonic() + HOOKS_WAIT_S
        found: Any = None
        while time.monotonic() < deadline:
            found = self.cdp.evaluate(PAGE_V2_HOOKS_JS)
            charts = (found or {}).get("charts") if isinstance(found, dict) else None
            scenes = ((found or {}).get("scene3d") or {}).get("scenes") if isinstance(found, dict) and isinstance(found.get("scene3d"), dict) else None
            waiting = [c for c in charts or [] if not c.get("rendered") and not c.get("failed")] + \
                [s for s in scenes or [] if s.get("visible") and not s.get("rendered")]
            if not waiting:
                break
            time.sleep(0.5)
        return found if isinstance(found, dict) else {}

    def _tiled_v2(self, first: Dict[str, List[str]], first_audit: List[Dict[str, Any]]) -> Tuple[Dict[str, List[str]], List[Dict[str, Any]]]:
        """The v2 page's lines and audit read at ``PAGE_V2_SCALE``, one screen-sized tile at a time over the board."""
        info = self.cdp.evaluate(PAGE_V2_TILES_JS)
        if not isinstance(info, dict) or not isinstance(info.get("bbox"), list) or len(info["bbox"]) != 4:
            return first, first_audit
        x0, y0, x1, y1 = (float(v) for v in info["bbox"])
        scale = PAGE_V2_SCALE
        tile_w, tile_h = float(info.get("w") or 1400) / scale, float(info.get("h") or 900) / scale
        cols, rows = max(1, int(math.ceil((x1 - x0) / tile_w))), max(1, int(math.ceil((y1 - y0) / tile_h)))
        if cols * rows > PAGE_V2_MAX_TILES:
            return first, first_audit
        lines: Dict[str, List[str]] = {}
        audit: Dict[Tuple[str, bool], Dict[str, Any]] = {}
        for row in range(rows):
            for col in range(cols):
                cx, cy = x0 + (col + 0.5) * tile_w, y0 + (row + 0.5) * tile_h
                self.cdp.evaluate("window.__synapseV2.zoom({}, [{}, {}]), true".format(scale, cx, cy))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not self.cdp.evaluate("window.__synapseV2.ready()"):
                    time.sleep(0.05)
                got = self.cdp.evaluate(PAGE_V2_LINES_JS)
                if isinstance(got, dict):
                    lines.update(got)
                found = self.cdp.evaluate(PAGE_V2_AUDIT_JS)
                for item in found if isinstance(found, list) else []:
                    key = (str(item.get("id")), bool(item.get("vertical") or item.get("collapsed")))
                    if key not in audit or (item.get("overflowPx") or 0) > (audit[key].get("overflowPx") or 0):
                        audit[key] = item
        return lines, list(audit.values())

    def screenshot(self, path: Path) -> None:
        import base64

        data = self.cdp.call("Page.captureScreenshot", format="png").get("data") or ""
        path.write_bytes(base64.b64decode(data))

    def dead(self) -> bool:
        """The browser is gone: the machine (or an out-of-memory kill under load) took it, not a scene."""
        return self.proc.poll() is not None

    def restart(self) -> None:
        """Close this browser and open another one, so one lost Chrome does not fail every scene after it."""
        self.close()
        self._start()

    def close(self) -> None:
        cdp = getattr(self, "cdp", None)
        if cdp is not None:
            cdp.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


#: Test hook and ``--dist``: the page build ``--page`` serves (None: ``web/dist``).
PAGE_DIST: Optional[Path] = None


def read_page_lines(qa: QaTeam, browser: Browser, shot: Optional[Path], engine: str = "v2") -> Dict[str, List[str]]:
    """The scene's page served on loopback, opened in the browser, and its line breaks read."""
    import threading
    from unittest import mock

    from herdr_team import activity, views
    from herdr_team import whiteboard_server as W

    with mock.patch.object(views, "team_views", side_effect=lambda *a, **k: []), \
            mock.patch.object(activity, "cards", side_effect=lambda *a, **k: []):
        server = W.make_server(qa.layout, qa.env, port=0, static_dir=PAGE_DIST, writable=False, api=None)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            return browser.lines(W.mint_ticket(qa.layout, False, "human", port=server.server_address[1]), shot, engine)
        finally:
            server.shutdown()
            server.server_close()


# --------------------------------------------------------------------------
# output


def summary_line(report: Dict[str, Any]) -> str:
    pixel = report["pixel"][report["themes"][0]]
    checks = ", ".join("{} {}".format(k, v) for k, v in sorted(report["check"].items())) or "none"
    contrast_text = "{:.1f}".format(pixel["min_contrast"]) if pixel["min_contrast"] is not None else "-"
    page = report.get("page_lines")
    page_text = "  page lines differ {}/{}".format(len(page["mismatches"]), page["compared"]) if page else ""
    if "page_overflow" in report:
        page_text += "  page overflow {}".format(len(report["page_overflow"]))
    if report.get("engine") == "v2":
        page_text += "  (v2 page, css filters {})".format(len(report.get("page_filters") or []))
    if report.get("page_skipped"):
        page_text += "  page skipped: {}".format(report["page_skipped"])
    render = report["renders"][report["themes"][0]]
    return ("{:<16} {}  ops {}/{}  check: {}  label_overflow {}  pixel overflow {} collisions {} arrow-label {}  "
            "min contrast {}  tofu {} cut off {}{}\n    png {}").format(
        report["scene"], "PASS" if report["gate"]["ok"] else "FAIL", report["applied"], report["ops"], checks,
        report["label_overflow"]["count"], len(pixel["overflow"]), len(pixel["collisions"]), len(pixel["arrow_label_collisions"]),
        contrast_text, len(render.get("tofu") or []), len(render.get("cut_off") or []), page_text, report["png"])


# --------------------------------------------------------------------------
# canvas v2 phase 5: the authority matrix and the collaboration numbers (G9)

MATRIX = REPO / "tests" / "fixtures" / "collab" / "matrix.json"


def matrix_table() -> str:
    """The authority matrix (``tests/fixtures/collab/matrix.json``, which ``tests/test_canvas_collab_matrix.py`` runs) as
    a table: one row per op and target, one column per actor, each cell ``default / live_refuse``."""
    rows = json.loads(MATRIX.read_text(encoding="utf-8"))["rows"]
    actors = ("lead", "deputy", "manager", "member")
    cells: Dict[Tuple[str, str, str], Dict[str, str]] = {}
    order: List[Tuple[str, str]] = []
    for row in rows:
        key = (row["op"], row["target"])
        if key not in order:
            order.append(key)
        word = row["expect"] + (" " + row["reason"] if row.get("reason") else "") + (" " + row["code"] if row.get("code") else "")
        cells.setdefault((row["op"], row["target"], row["actor"]), {})[row["settings"]] = word
    lines = ["| op | target | " + " | ".join(actors) + " |", "| --- | --- | " + " | ".join("---" for _ in actors) + " |"]
    for op, target in order:
        shown = []
        for actor in actors:
            found = cells.get((op, target, actor), {})
            first, second = found.get("default", "-"), found.get("live_refuse", "-")
            shown.append(first if first == second else "{} / {}".format(first, second))
        lines.append("| {} | {} | {} |".format(op, target, " | ".join(shown)))
    return "\n".join(lines) + "\n\ncells: default settings / human_edits live + frozen refuse ({} rows)".format(len(rows))


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else 0.0


def _big_board(qa: QaTeam, count: int) -> None:
    lead = qa.author_for("lead")
    ops = [{"op": "shape", "kind": "box", "text": "Box {}".format(i + 1), "w": 160, "h": 80, "at": [(i % 50) * 200, (i // 50) * 120],
            "intent": "board"} for i in range(count)]
    for start in range(0, len(ops), C.MAX_BATCH_OPS):
        C.apply_ops(qa.layout, qa.team, ops[start:start + C.MAX_BATCH_OPS], lead)


def perf_collab() -> Dict[str, Any]:
    """The phase 5 numbers of G9: the gate per op, a 40-op member batch on a 2,000-element board with the gate on and off,
    the display list with 200 open proposals, a presence write, look with 10 presence files, what a stream reads per
    presence poll, and restoring a 2,000-element checkpoint."""
    from unittest import mock

    from herdr_team import canvas_collab as K
    from herdr_team import canvas_presence as P

    out: Dict[str, Any] = {}
    with QaTeam() as qa:
        doc = load_scene_file(SCENES_DIR / "collab.json")
        apply_scene(qa, doc)
        apply_presence(qa, doc)
        spent: List[float] = []
        real = K.review_op

        def timed(ctx: Any, name: str, raised: bool) -> None:
            started = time.perf_counter()
            try:
                real(ctx, name, raised)
            finally:
                spent.append((time.perf_counter() - started) * 1000.0)

        visit = next(e["id"] for e in C.load_scene(qa.team)["elements"] if e.get("alias") == "visit")
        with mock.patch.object(K, "review_op", timed):
            for n in range(40):
                C.apply_ops(qa.layout, qa.team, [{"op": "restyle", "id": visit, "tone": ("info", "success")[n % 2], "intent": "perf"}], qa.author)
        out["gate_ms_p50"] = round(_median(spent), 3)
        P.write_member(qa.team, qa.author, status="drawing", intent="warm")  # the secret patterns load once per process
        started = time.perf_counter()
        for _ in range(100):
            P.write_member(qa.team, qa.author, status="drawing", region=[0, 0, 400, 200], intent="perf")
        out["presence_write_ms"] = round((time.perf_counter() - started) * 10.0, 3)
        for name in os.listdir(P.presence_dir(qa.team)):
            os.unlink(P.presence_dir(qa.team) / name)  # look first with no presence at all
        C.look(qa.layout, qa.team, MEMBER, advance=False)  # warm
        timings = []
        for _ in range(5):
            started = time.perf_counter()
            C.look(qa.layout, qa.team, MEMBER, advance=False)
            timings.append((time.perf_counter() - started) * 1000.0)
        base_look = _median(timings)
        for n in range(10):
            (P.presence_dir(qa.team) / "perf{}.json".format(n)).write_text(json.dumps(
                {"v": 1, "name": "perf{}".format(n), "kind": "member", "agent": None, "at": C._iso(time.time()), "ttl_s": 600,
                 "status": "drawing", "region": [0, 0, 100, 100], "ids": [], "intent": "perf", "via": "focus"}))
        timings = []
        for _ in range(5):
            started = time.perf_counter()
            C.look(qa.layout, qa.team, MEMBER, advance=False)
            timings.append((time.perf_counter() - started) * 1000.0)
        out["look_presence_added_ms"] = round(_median(timings) - base_look, 3)
        started = time.perf_counter()
        for _ in range(20):
            P.signature(qa.team)
            P.read_all(qa.team, time.time())
        out["presence_poll_ms"] = round((time.perf_counter() - started) * 50.0, 3)
    # The same 40-op batch on one 2,000-element board, gate on and off in turn: an atomic batch whose last op is refused runs
    # every op and writes nothing, so each run sees the same board.
    batch = [{"op": "shape", "kind": "box", "text": "New {}".format(n), "at": [12000 + (n % 8) * 200, (n // 8) * 120], "intent": "perf"} for n in range(40)]
    runs: Dict[bool, List[float]] = {True: [], False: []}
    with QaTeam() as qa:
        _big_board(qa, C.MAX_ELEMENTS - 40)
        for _round in range(5):
            for gate_on in (True, False):
                with mock.patch.object(K, "GATE_ON", gate_on):
                    started = time.perf_counter()
                    try:
                        C.apply_ops(qa.layout, qa.team, batch + [{"op": "no_such_op"}], qa.author, atomic=True)
                    except C.HerdrTeamError:
                        pass
                    runs[gate_on].append((time.perf_counter() - started) * 1000.0)
    out["batch40_ms_gate_on"] = round(min(runs[True]), 1)  # the least disturbed of five runs each, taken in turn
    out["batch40_ms_gate_off"] = round(min(runs[False]), 1)
    out["batch40_gate_added_ms"] = round(out["batch40_ms_gate_on"] - out["batch40_ms_gate_off"], 1)
    with QaTeam() as qa, mock.patch.object(K, "MAX_OPEN_PROPOSALS_PER_AUTHOR", 250):
        _big_board(qa, 400)
        lead_boxes = [e["id"] for e in C.load_scene(qa.team)["elements"]][:200]

        def cold() -> float:
            found = []
            for _round in range(5):
                C._DISPLAY_CACHE.clear()
                started = time.perf_counter()
                C.display(qa.team)
                found.append(time.perf_counter() - started)
            return min(found)

        plain = cold()
        for start in range(0, 200, 50):
            C.apply_ops(qa.layout, qa.team, [{"op": "move", "id": eid, "by": [0, 20], "intent": "perf"} for eid in lead_boxes[start:start + 50]],
                        qa.author, now=time.time() + start)
        out["display_200_proposals_added_ms"] = round((cold() - plain) * 1000.0, 1)
        out["display_proposals"] = sum(1 for e in C.display(qa.team)["entries"] if e.get("kind") == "proposal")
    with QaTeam() as qa:
        _big_board(qa, C.MAX_ELEMENTS - 2)
        lead = qa.author_for("lead")
        C.apply_ops(qa.layout, qa.team, [{"op": "checkpoint", "label": "perf"}], lead)
        C.apply_ops(qa.layout, qa.team, [{"op": "delete", "ids": ["E-{}".format(n) for n in range(1, 301)]}], lead)
        started = time.perf_counter()
        result = C.apply_ops(qa.layout, qa.team, [{"op": "restore", "id": "V-1"}], lead)
        out["restore_2000_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        out["restore"] = result["applied"][0].get("restore") if result["applied"] else result["refused"]
        # A full restore (QA phase 5 L13): every element deleted, then all of them put back in one op.
        ids = [e["id"] for e in C.load_scene(qa.team)["elements"]]
        for start in range(0, len(ids), 500):
            C.apply_ops(qa.layout, qa.team, [{"op": "delete", "ids": ids[start:start + 500]}], lead)
        started = time.perf_counter()
        result = C.apply_ops(qa.layout, qa.team, [{"op": "restore", "id": "V-1"}], lead)
        out["restore_full_2000_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        out["restore_full"] = result["applied"][0].get("restore") if result["applied"] else result["refused"]
    out["budgets"] = {"gate_ms_p50": 3.0, "batch40_gate_added_ms": 40.0 if sys.version_info >= (3, 11) else 60.0,
                      "display_200_proposals_added_ms": 40.0, "presence_write_ms": 2.0, "look_presence_added_ms": 5.0,
                      "restore_2000_ms": 1500.0, "restore_full_2000_ms": 1500.0 if sys.version_info >= (3, 11) else 2500.0}
    out["within"] = {key: out.get(key, 0.0) <= limit for key, limit in out["budgets"].items()}
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Measure text overflow, overlap and contrast on golden canvas scenes.")
    parser.add_argument("scenes", nargs="*", help="scene files, directories, or names in tests/fixtures/canvas_scenes (default: all)")
    parser.add_argument("--out", default=os.fspath(OUT_DIR), help="where the renders and report.json go (default: .local/qa/canvas)")
    parser.add_argument("--json", action="store_true", help="print the whole report as one JSON object")
    parser.add_argument("--scale", type=float, default=PROBE_SCALE, help="probe pixels per canvas unit (default 2)")
    parser.add_argument("--tolerance", type=float, default=TOLERANCE, help="units of slack around outlines (default 2)")
    parser.add_argument("--theme", action="append", choices=("light", "dark"), help="themes to probe (default: every one the renderer has)")
    parser.add_argument("--page-lines", help="JSON of line breaks read off the page: {scene: {element id: [lines]}} or {element id: [lines]}")
    parser.add_argument("--strict", action="store_true", help="also gate arrow_through, stray and arrow-label collisions (Phase 2)")
    parser.add_argument("--serve", type=float, default=0.0, metavar="SECONDS", help="serve each scene's page on loopback this long")
    parser.add_argument("--dist", help="serve this page build instead of web/dist (a scratch `vite build --outDir`)")
    parser.add_argument("--engine", choices=ENGINES, default="v2",
                        help="the page engine --page opens: v2 (the display-list renderer, the page's default) or v1 (the classic "
                             "Excalidraw page, ?engine=v1)")
    parser.add_argument("--matrix", action="store_true", help="print the canvas v2 phase 5 authority matrix as a table and exit")
    parser.add_argument("--perf", choices=("collab",), help="measure the canvas v2 phase 5 numbers (G9) and exit")
    parser.add_argument("--page", nargs="?", const="", metavar="CHROME",
                        help="also open each scene's page in a throwaway headless Chrome, read its line breaks and compare them "
                             "(screenshots light and dark); CHROME defaults to $CHROME or the installed Chrome")
    args = parser.parse_args(argv)

    if args.matrix:
        print(matrix_table())
        return 0
    if args.perf == "collab":
        found = perf_collab()
        print(json.dumps(found, indent=1) if args.json else "\n".join("{:<34} {}".format(k, v) for k, v in found.items()))
        return 0 if all(found["within"].values()) else 1
    if R.find_resvg() is None:
        print("canvas_qa: resvg is not on PATH (brew install resvg)", file=sys.stderr)
        return 3
    global PAGE_DIST
    PAGE_DIST = Path(args.dist) if args.dist else None
    themes = args.theme or themes_supported()
    if "dark" in themes and "dark" not in themes_supported():
        print("canvas_qa: the server renderer has no dark theme yet (render_svg takes no theme)", file=sys.stderr)
        return 2
    try:
        files = scene_files(args.scenes)
        scenes = [load_scene_file(path) for path in files]
    except (OSError, ValueError) as err:
        print("canvas_qa: {}".format(err), file=sys.stderr)
        return 2
    page: Optional[Dict[str, Any]] = None
    if args.page_lines:
        page = json.loads(Path(args.page_lines).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    browser: Optional[Browser] = None
    if args.page is not None:
        chrome = find_chrome(args.page or None)
        if chrome is None:
            print("canvas_qa: no Chrome found for --page (pass its path, or set CHROME)", file=sys.stderr)
            return 2
        browser = Browser(chrome)
    try:
        reports = run_scenes(scenes, out, args, themes, page, browser)
    finally:
        if browser is not None:
            browser.close()
    doc = {"scenes": reports, "ok": all(r["gate"]["ok"] for r in reports), "themes": themes,
           "resvg": R.find_resvg(), "scale": args.scale, "tolerance": args.tolerance, "strict": args.strict}
    (out / "report.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    if args.json:
        print(json.dumps(doc, ensure_ascii=False))
    else:
        print("{} of {} scenes pass; report {}".format(sum(1 for r in reports if r["gate"]["ok"]), len(reports), out / "report.json"))
    if any(r.get("refused") or "error" in r for r in reports):
        return 2
    return 0 if doc["ok"] else 1


#: What a lost browser looks like from here: Chrome died (a loaded machine, an out-of-memory kill) and the socket went
#: with it. Without a restart the first scene to meet this fails, and so does every scene after it.
LOST_BROWSER = ("BrokenPipeError", "ConnectionResetError", "ConnectionAbortedError", "TimeoutError")


def browser_lost(browser: Optional[Browser], err: BaseException) -> bool:
    """The browser, not the scene, is what failed: it exited, or the DevTools socket is gone."""
    if browser is None:
        return False
    return browser.dead() or type(err).__name__ in LOST_BROWSER or "DevTools closed the connection" in str(err)


def scene_report(scene: Dict[str, Any], out: Path, args: argparse.Namespace, themes: List[str],
                 lines: Optional[Dict[str, List[str]]], browser: Optional[Browser]) -> Dict[str, Any]:
    """One scene's report. A broken scene is a finding, not a crash of the run; a scene whose browser died is tried
    once more on a fresh one."""
    for attempt in (1, 2):
        try:
            return evaluate(scene, out, args.scale, args.tolerance, themes, lines, args.strict, args.serve, browser, args.engine)
        except Exception as err:
            if attempt == 2 or browser is None or not browser_lost(browser, err):
                return {"scene": scene["name"], "file": scene["file"], "error": "{}: {}".format(type(err).__name__, err),
                        "gate": {"ok": False, "failed": ["error"]}}
            if not args.json:
                print("{:<16} browser lost ({}: {}); starting another and trying again".format(
                    scene["name"], type(err).__name__, err), flush=True)
            try:
                browser.restart()
            except Exception as restart_err:
                return {"scene": scene["name"], "file": scene["file"],
                        "error": "{}: {}".format(type(restart_err).__name__, restart_err),
                        "gate": {"ok": False, "failed": ["error"]}}
    raise AssertionError("unreachable")  # pragma: no cover


def run_scenes(scenes: List[Dict[str, Any]], out: Path, args: argparse.Namespace, themes: List[str],
               page: Optional[Dict[str, Any]], browser: Optional[Browser]) -> List[Dict[str, Any]]:
    """Evaluate each scene in turn, printing its summary line as it finishes (unless ``--json``).

    A scene whose browser died is tried once more on a fresh one (``Browser.restart``), so an infrastructure event on a
    loaded machine costs one scene's time instead of failing the whole run from that scene on.
    """
    reports = []
    for scene in scenes:
        lines = None
        if page is not None:
            lines = page.get(scene["name"]) if isinstance(page.get(scene["name"]), dict) else page
        report = scene_report(scene, out, args, themes, lines, browser)
        reports.append(report)
        if not args.json:
            print(summary_line(report) if "error" not in report else "{:<16} ERROR {}".format(report["scene"], report["error"]), flush=True)
    return reports


if __name__ == "__main__":
    sys.exit(main())
