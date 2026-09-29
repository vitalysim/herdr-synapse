#!/usr/bin/env python3
"""Write the golden 0.21 boards (canvas v2 phase 6, 2.5): boards drawn by the 0.21.2 code itself, for the migration tests.

For each board this tool

1. extracts ``herdr_team`` (and ``assets``) of commit ``cf048860`` (0.21.2) with ``git archive`` into a scratch folder
   (read-only on the repository: no branch is switched, no worktree is made);
2. runs that ``herdr_team`` in a subprocess over a temporary state root (the real HOME, socket and state are never
   touched), with one team whose canvas is on, a member ``qa-drawer``, a peer ``qa-peer`` and the operator;
3. applies the board's ops with a fixed clock, downgraded to what 0.21.2 took: an op 0.21.2 did not have is skipped,
   a field it did not take is dropped (both listed), and what it still refused is listed;
4. copies ``events.jsonl``, ``scene.json`` and the ``assets/`` the board stored into
   ``tests/fixtures/migration/v021/<name>/``, and writes ``tests/fixtures/migration/README.md``.

The boards are the golden scenes ``house``, ``flowchart``, ``sketch``, ``text-notes``, ``arrow-labels``,
``frame-children``, ``agent-board``, ``font-sizes`` and ``i18n`` (``tests/fixtures/canvas_scenes``), plus ``legacy-mix``,
built here: every 0.21 kind (Open Color names and author hexes, ``rough``, the hand font, a Vega-Lite chart over a CSV,
a Mermaid flowchart, a viz, an image, comments with a mention, a claim, a lock, a legend entry) and edits made
through the 0.21 page.

It is not a gate (it needs git); its output is checked in.

    python3 tools/make_v021_boards.py                 # every board
    python3 tools/make_v021_boards.py house sketch    # some boards
    python3 tools/make_v021_boards.py --check         # the fixtures exist and fold (no git needed)

Stdlib only.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import zlib
from pathlib import Path
from typing import Any, Dict, List, Sequence

REPO = Path(__file__).resolve().parent.parent
COMMIT = "cf048860"
VERSION = "0.21.2"
SCENES_DIR = REPO / "tests" / "fixtures" / "canvas_scenes"
OUT = REPO / "tests" / "fixtures" / "migration" / "v021"
README = REPO / "tests" / "fixtures" / "migration" / "README.md"
GOLDEN = ("house", "flowchart", "sketch", "text-notes", "arrow-labels", "frame-children", "agent-board", "font-sizes", "i18n")
BOARDS = GOLDEN + ("legacy-mix",)
#: The whole fixture set stays under this size (2.5).
MAX_TOTAL_BYTES = 400 * 1024
#: The fixed clock the boards are drawn at (2026-09-20T10:00:00Z), one second per batch.
EPOCH = 1789898400.0

REV_CSV = "month,region,revenue\n2026-01,EU,120\n2026-01,US,180\n2026-02,EU,135\n2026-02,US,176\n2026-03,EU,150\n2026-03,US,201\n"


def _png(width: int = 8, height: int = 6, rgb: Sequence[int] = (25, 113, 194)) -> bytes:
    """A tiny solid PNG (the ``legacy-mix`` image)."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def legacy_mix() -> Dict[str, Any]:
    """Every 0.21 kind, in the 0.21 language, by the member, the operator (CLI) and the operator's page."""
    vega = {"$schema": "https://vega.github.io/schema/vega-lite/v5.json", "mark": "bar",
            "encoding": {"x": {"field": "month", "type": "ordinal"}, "y": {"field": "revenue", "type": "quantitative"},
                         "color": {"field": "region", "type": "nominal"}}}
    drawer = [
        {"op": "frame", "id": "mix", "title": "Legacy mix", "at": "c0r0", "w": 1400, "h": 900, "intent": "one of every 0.21 kind"},
        {"op": "shape", "id": "blue", "kind": "box", "text": "Open Color blue", "color": "blue", "fill": "blue", "at": "c2r4", "w": 180, "h": 80,
         "intent": "a named colour"},
        {"op": "shape", "id": "hex", "kind": "box", "text": "Author palette hex", "color": "#2f9e44", "fill": "#b2f2bb", "right_of": "blue",
         "w": 180, "h": 80, "intent": "an author colour by hex"},
        {"op": "shape", "id": "odd", "kind": "box", "text": "Unknown hex", "color": "#123456", "fill": "#fedcba", "right_of": "hex", "w": 160, "h": 80,
         "intent": "a hex no table knows"},
        {"op": "shape", "id": "rough", "kind": "ellipse", "text": "Hand drawn idea", "rough": 2, "font": "hand", "color": "red", "below": "blue",
         "w": 200, "h": 100, "intent": "a sketchy mark"},
        {"op": "shape", "id": "decide", "kind": "diamond", "text": "Ship it?", "rough": 1, "right_of": "rough", "w": 160, "h": 110,
         "intent": "a rough decision"},
        {"op": "shape", "id": "note", "kind": "note", "text": "A sticky in the hand font", "font": "hand", "right_of": "decide", "w": 180, "h": 100,
         "intent": "a note"},
        {"op": "shape", "id": "tight", "kind": "box", "text": "Quarterly revenue forecast for the enterprise segment", "at": "c2r20",
         "w": 120, "h": 60, "intent": "a label the old metrics fitted tightly"},
        {"op": "shape", "kind": "text", "text": "Free text that wraps across a couple of lines when it is long enough", "right_of": "tight",
         "w": 260, "intent": "free text"},
        {"op": "arrow", "from": "blue", "to": "rough", "label": "sketched as", "intent": "a labelled arrow"},
        {"op": "arrow", "from": "rough", "to": "decide", "intent": "an arrow with no label"},
        {"op": "chart", "id": "rev", "title": "Revenue by region", "spec": vega, "data": "rev.csv", "at": "c2r28", "w": 420, "h": 260,
         "intent": "a Vega-Lite chart over a CSV"},
        {"op": "mermaid", "id": "flow", "source": "flowchart LR\n  A[Start] --> B{Tests pass?}\n  B -->|yes| C[Ship]\n  B -->|no| D[Fix]",
         "right_of": "rev", "intent": "a Mermaid flowchart"},
        {"op": "viz", "id": "pulse", "title": "Pulse", "html": "<div id=\"p\">pulse</div><script>document.getElementById('p').textContent = 'ok';</script>",
         "at": "c50r4", "w": 200, "h": 120, "intent": "a live visual"},
        {"op": "image", "id": "pic", "path": "diagram.png", "below": "pulse", "w": 80, "h": 60, "intent": "an image"},
        {"op": "comment", "at": "tight", "text": "@qa-peer can you check this forecast?", "intent": "ask the peer"},
        {"op": "claim", "region": "c50r20:c68r30", "label": "next: the pricing notes", "intent": "where I draw next"},
        {"op": "legend", "symbol": "red ellipse", "meaning": "a hand-drawn idea, not decided", "intent": "a convention"},
    ]
    lead = [
        {"op": "lock", "region": "c50r34:c68r44", "label": "hands off: pricing", "intent": "the operator keeps this"},
        {"op": "shape", "kind": "box", "text": "Pricing (the operator's)", "at": "c51r36", "w": 200, "h": 80, "intent": "the operator's own mark"},
    ]
    page = [
        {"op": "move", "id": "note", "by": [0, 40], "intent": "the operator's edit"},
        {"op": "edit", "id": "odd", "text": "Unknown hex, renamed on the page", "intent": "the operator's edit"},
        {"op": "restyle", "id": "hex", "size": "l", "intent": "the operator's edit"},
    ]
    peer = [{"op": "comment", "at": "tight", "text": "Numbers look right to me.", "intent": "reply"}]
    return {"name": "legacy-mix", "about": "every 0.21 kind in the 0.21 language, with edits from the operator's page",
            "batches": [{"as": "drawer", "ops": drawer}, {"as": "lead", "ops": lead}, {"as": "page", "ops": page}, {"as": "peer", "ops": peer}],
            "artifacts": {"rev.csv": REV_CSV}, "binary": {"diagram.png": _png()}}


def sketch_021() -> Dict[str, Any]:
    """``sketch`` in the 0.21 language (today's golden scene of that name is drawn with v2 components): a hand-drawn
    whiteboard of pen strokes, rough shapes and the hand font."""
    ops = [
        {"op": "frame", "id": "board", "title": "Whiteboard sketch", "at": "c0r0", "w": 900, "h": 560, "rough": 1, "intent": "a rough board"},
        {"op": "shape", "id": "idea", "kind": "ellipse", "text": "Big idea", "rough": 2, "font": "hand", "at": "c3r4", "w": 180, "h": 100,
         "intent": "the idea"},
        {"op": "shape", "id": "why", "kind": "note", "text": "Why now: churn is up 4% since March", "font": "hand", "right_of": "idea",
         "w": 200, "h": 110, "intent": "why"},
        {"op": "shape", "id": "risk", "kind": "diamond", "text": "Risky?", "rough": 2, "color": "red", "below": "idea", "w": 150, "h": 110,
         "intent": "a doubt"},
        {"op": "arrow", "from": "idea", "to": "why", "label": "because", "rough": 2, "intent": "reason"},
        {"op": "arrow", "from": "idea", "to": "risk", "rough": 1, "intent": "doubt"},
        {"op": "pen", "points": ["c30r8", "c34r6", "c38r9", "c42r7"], "style": "smooth", "color": "blue", "intent": "a squiggle"},
        {"op": "pen", "points": [[620, 300], [700, 300], [700, 360], [620, 360]], "closed": True, "style": "straight", "intent": "a box by hand"},
        {"op": "shape", "kind": "text", "text": "circle the winner", "font": "hand", "at": "c31r19", "w": 180, "intent": "a note to self"},
    ]
    return {"name": "sketch", "about": "a hand-drawn board in the 0.21 language (pen, rough shapes, the hand font)",
            "batches": [{"as": "drawer", "ops": ops}], "artifacts": {}, "binary": {}}


def agent_board_021() -> Dict[str, Any]:
    """``agent-board`` in the 0.21 language (today's golden scene is v2 components): what an agent drew on 0.21, frames of
    boxes and arrows, a 0.21 graph, a plan as notes and a comment."""
    ops = [
        {"op": "frame", "id": "arch", "title": "Checkout architecture", "at": "c0r0", "w": 1000, "h": 420, "intent": "the system"},
        {"op": "shape", "id": "web", "kind": "box", "text": "Web app", "color": "blue", "at": "c2r4", "w": 160, "h": 70, "intent": "client"},
        {"op": "shape", "id": "api", "kind": "box", "text": "Checkout API", "color": "purple", "right_of": "web", "gap": 80, "w": 180, "h": 70,
         "intent": "service"},
        {"op": "shape", "id": "db", "kind": "ellipse", "text": "Orders DB", "color": "green", "right_of": "api", "gap": 80, "w": 170, "h": 80,
         "intent": "storage"},
        {"op": "shape", "id": "psp", "kind": "box", "text": "Payment provider", "color": "orange", "below": "api", "gap": 60, "w": 180, "h": 70,
         "intent": "external"},
        {"op": "arrow", "from": "web", "to": "api", "label": "POST /checkout", "intent": "call"},
        {"op": "arrow", "from": "api", "to": "db", "label": "insert order", "intent": "write"},
        {"op": "arrow", "from": "api", "to": "psp", "label": "charge", "dash": "dashed", "intent": "charge"},
        {"op": "graph", "id": "deploy", "title": "Deploy pipeline", "direction": "right", "at": "c0r24",
         "nodes": [{"id": "build", "text": "Build"}, {"id": "test", "text": "Test"}, {"id": "stage", "text": "Staging"}, {"id": "prod", "text": "Production"}],
         "edges": [{"from": "build", "to": "test"}, {"from": "test", "to": "stage"}, {"from": "stage", "to": "prod", "label": "approve"}],
         "intent": "how it ships"},
        {"op": "frame", "id": "plan", "title": "Plan", "at": "c54r0", "w": 420, "h": 420, "intent": "next steps"},
        {"op": "shape", "kind": "note", "text": "1. Add idempotency keys to /checkout", "inside": "plan", "w": 360, "h": 70, "intent": "step"},
        {"op": "shape", "kind": "note", "text": "2. Retry the provider with backoff", "inside": "plan", "w": 360, "h": 70, "intent": "step"},
        {"op": "shape", "kind": "note", "text": "3. Alert on failed charges over 2%", "inside": "plan", "w": 360, "h": 70, "intent": "step"},
        {"op": "comment", "at": "psp", "text": "Timeouts here caused last week's incident", "intent": "context"},
    ]
    return {"name": "agent-board", "about": "an agent's 0.21 board: frames, boxes, arrows, a 0.21 graph, a plan in notes",
            "batches": [{"as": "drawer", "ops": ops}], "artifacts": {}, "binary": {}}


#: Golden scenes whose file today is drawn with v2 components: their 0.21 board is written here in the 0.21 language.
WRITTEN_021 = {"sketch": sketch_021, "agent-board": agent_board_021}


def golden(name: str) -> Dict[str, Any]:
    if name in WRITTEN_021:
        return WRITTEN_021[name]()
    doc = json.loads((SCENES_DIR / "{}.json".format(name)).read_text(encoding="utf-8"))
    batches = doc.get("batches") or [doc.get("ops")]
    out = []
    for batch in batches:
        if isinstance(batch, dict):
            out.append({"as": batch.get("as") or "drawer", "ops": batch.get("ops") or []})
        else:
            out.append({"as": "drawer", "ops": batch})
    return {"name": name, "about": str(doc.get("about") or ""), "batches": out, "artifacts": {}, "binary": {}}


# The driver runs inside the extracted 0.21.2 tree (its own ``herdr_team``): it reads one board as JSON on stdin and
# writes the result as JSON on stdout.
DRIVER = r'''
import base64, json, os, sys, tempfile, shutil
from pathlib import Path
from herdr_team import canvas as C, features, paths, store
job = json.load(sys.stdin)
tmp = Path(tempfile.mkdtemp(prefix="v021-board-"))
try:
    state_root = tmp / "state"
    for d in (tmp / "home", tmp / "cfg" / "herdr", tmp / "st", state_root, tmp / "project"):
        d.mkdir(parents=True, exist_ok=True)
    env = {"HOME": str(tmp / "home"), "XDG_CONFIG_HOME": str(tmp / "cfg"), "XDG_STATE_HOME": str(tmp / "st"),
           "HERDR_TEAM_STATE_DIR": str(state_root), "HERDR_SOCKET_PATH": str(tmp / "cfg" / "herdr" / "herdr.sock"), "HERDR_ENV": "1",
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    session = paths.session_paths(state_root, "default")
    paths.ensure_session_dirs(session)
    team = session.team("qa")
    paths.ensure_team_dirs(team)
    def member(name, role, kind, n):
        return {"name": name, "role": role, "kind": kind, "terminal_id": "term_" + role, "pane_id": "w1:p{}".format(n), "workspace_id": "w1",
                "tab_id": "w1:t1", "label": "team:qa/" + role, "cwd": str(tmp / "project"), "managed": False, "session": None,
                "status": "active", "generation": 1, "delivery": "nudge", "joined_at": "2026-09-20T09:00:00Z", "last_seen_at": None,
                "briefed_at": None, "briefing_seq": None, "charter_seq_acked": None, "brief": None}
    store.write_json(team.team_json, {
        "schema": 1, "team": "qa", "created_at": "2026-09-20T09:00:00Z", "socket": env["HERDR_SOCKET_PATH"], "state_dir": str(state_root),
        "naming": "prefixed", "revision": 1, "config": {"project_dir": str(tmp / "project")},
        "charter": {"seq": 1, "text": "0.21 board.", "refs": [], "updated_at": "2026-09-20T09:00:00Z", "updated_by": "human"},
        "members": [member("qa-drawer", "drawer", "claude", 1), member("qa-peer", "peer", "codex", 2),
                    {"name": "human", "role": "operator", "kind": "human", "terminal_id": None, "status": "active"}]})
    features.set_layer(session, True, "human", "cli")
    features.set_team(team, enabled=True, by="human", via="cli")
    layout = paths.resolve_layout(env)
    art = C.artifacts_dir(layout, team)
    if art is not None:
        art.mkdir(parents=True, exist_ok=True)
        for name, text in job["artifacts"].items():
            (art / name).write_text(text, encoding="utf-8")
        for name, data in job["binary"].items():
            (art / name).write_bytes(base64.b64decode(data))
    authors = {"drawer": C.CanvasAuthor("qa-drawer", C.KIND_MEMBER, "cli", True, agent="claude", team="qa"),
               "peer": C.CanvasAuthor("qa-peer", C.KIND_MEMBER, "cli", True, agent="codex", team="qa"),
               "lead": C.CanvasAuthor(C.HUMAN, C.KIND_HUMAN, "cli", True, operator=True),
               "page": C.page_author(True)}
    report = {"skipped": [], "dropped": [], "refused": [], "applied": 0}
    for number, batch in enumerate(job["batches"]):
        ops = []
        for index, op in enumerate(batch["ops"]):
            name = op.get("op")
            if name not in C._FIELDS:
                report["skipped"].append({"batch": number, "index": index, "op": name})
                continue
            allowed = set(C._FIELDS[name])
            extra = sorted(k for k in op if k not in allowed)
            if extra:
                report["dropped"].append({"batch": number, "index": index, "op": name, "fields": extra})
            ops.append({k: v for k, v in op.items() if k in allowed})
        if not ops:
            continue
        who = batch.get("as") or "drawer"
        author = authors.get(who)
        if author is None:
            report["skipped"].append({"batch": number, "as": who})
            continue
        result = C.apply_ops(layout, team, ops, author, now=job["epoch"] + number)
        report["applied"] += len(result.get("applied") or [])
        for item in result.get("refused") or []:
            report["refused"].append({"batch": number, "op": item.get("op"), "code": item.get("code"), "message": item.get("message")})
    folder = C._dir(team)
    out = Path(job["out"])
    out.mkdir(parents=True, exist_ok=True)
    for name in ("events.jsonl", "scene.json"):
        data = (folder / name).read_bytes().replace(str(tmp).encode("utf-8"), b"/tmp/v021-board")
        (out / name).write_bytes(data)
    assets = folder / "assets"
    if assets.is_dir() and any(assets.iterdir()):
        shutil.copytree(str(assets), str(out / "assets"), dirs_exist_ok=True)
    if job["artifacts"] or job["binary"]:
        # The team's artifacts the board read (a chart's CSV): the rig serves them back (tools/canvas_rig.py v021-<name>).
        (out / "artifacts").mkdir(exist_ok=True)
        for name, text in job["artifacts"].items():
            (out / "artifacts" / name).write_text(text, encoding="utf-8")
    print(json.dumps(report))
finally:
    shutil.rmtree(str(tmp), ignore_errors=True)
'''


def extract(scratch: Path) -> Path:
    """``git archive <COMMIT> herdr_team assets`` into ``scratch`` (read-only on the repository)."""
    data = subprocess.run(["git", "-C", os.fspath(REPO), "archive", "--format=tar", COMMIT, "herdr_team", "assets"],
                          check=True, stdout=subprocess.PIPE).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        try:
            archive.extractall(os.fspath(scratch), filter="data")  # our own repository's archive
        except TypeError:  # Python before the extraction filters (3.9 without the backport)
            archive.extractall(os.fspath(scratch))  # noqa: S202 - our own repository's archive
    return scratch


def build(tree: Path, board: Dict[str, Any]) -> Dict[str, Any]:
    import base64

    out = OUT / board["name"]
    if out.exists():
        shutil.rmtree(os.fspath(out))
    job = {"batches": board["batches"], "artifacts": board["artifacts"],
           "binary": {k: base64.b64encode(v).decode("ascii") for k, v in board["binary"].items()},
           "out": os.fspath(out), "epoch": EPOCH}
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": os.fspath(tree), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-c", DRIVER], cwd=os.fspath(tree), env=env, input=json.dumps(job).encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if done.returncode != 0:
        raise SystemExit("board {}: the 0.21.2 driver failed:\n{}".format(board["name"], done.stderr.decode("utf-8", "replace")[-3000:]))
    return json.loads(done.stdout.decode("utf-8").strip().splitlines()[-1])


def write_readme(reports: Dict[str, Dict[str, Any]]) -> None:
    lines = [
        "# Golden 0.21 boards",
        "",
        "Boards drawn by the {} code itself (commit `{}`), for the canvas v2 migration tests".format(VERSION, COMMIT),
        "(`tests/test_canvas_migrate.py`, canvas v2 phase 6, 2.5) and the page's migration banner",
        "(`tools/canvas_rig.py v021-<name>`).",
        "",
        "Each `v021/<name>/` holds the `events.jsonl` and `scene.json` 0.21.2 wrote, the `assets/` it stored and, for",
        "`legacy-mix`, the `artifacts/` its chart reads.",
        "They were made with `python3 tools/make_v021_boards.py`, which extracts `herdr_team` of `{}` with".format(COMMIT),
        "`git archive` (no branch switch, no worktree) and applies each board over a temporary state root with a fixed",
        "clock. An op 0.21.2 did not have is skipped and a field it did not take is dropped; both are listed below.",
        "Absolute temporary paths in the files are replaced with `/tmp/v021-board`.",
        "",
        "The boards: the golden scenes `{}` (`tests/fixtures/canvas_scenes`), and `legacy-mix`,".format("`, `".join(GOLDEN)),
        "built by the generator: every 0.21 kind (Open Color names and author hexes, `rough: 2`, `font: hand`, a",
        "Vega-Lite chart over a CSV, a Mermaid flowchart, a viz, an image, comments with a mention, a claim, a lock, a",
        "legend entry) and edits made through the 0.21 page. Today's `sketch` and `agent-board` scenes are drawn with v2",
        "components, so their 0.21 boards are written in the 0.21 language in the generator (`sketch_021`, `agent_board_021`).",
        "",
        "| Board | Ops applied | Ops skipped (not in 0.21.2) | Fields dropped | Refused by 0.21.2 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name in BOARDS:
        rep = reports.get(name)
        if rep is None:
            continue
        skipped = ", ".join(sorted({str(s.get("op") or s.get("as")) for s in rep["skipped"]})) or "none"
        dropped: Dict[str, int] = {}
        for item in rep["dropped"]:
            for field in item["fields"]:
                dropped[field] = dropped.get(field, 0) + 1
        dropped_text = ", ".join("`{}` ({})".format(k, v) for k, v in sorted(dropped.items())) or "none"
        refused = "; ".join("{} {}".format(r.get("op"), r.get("code")) for r in rep["refused"]) or "none"
        lines.append("| `{}` | {} | {} | {} | {} |".format(name, rep["applied"], skipped, dropped_text, refused))
    lines += ["", "Regenerate (needs git; not a gate): `python3 tools/make_v021_boards.py`. Check: `python3 tools/make_v021_boards.py --check`.", ""]
    README.write_text("\n".join(lines), encoding="utf-8")
    (README.parent / "report.json").write_text(json.dumps(reports, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def total_bytes() -> int:
    return sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file()) if OUT.is_dir() else 0


def check() -> int:
    """Every board is there and folds in this checkout; the set is under its size limit."""
    sys.path.insert(0, os.fspath(REPO))
    problems: List[str] = []
    for name in BOARDS:
        folder = OUT / name
        for part in ("events.jsonl", "scene.json"):
            if not (folder / part).is_file():
                problems.append("{}/{} is missing".format(name, part))
    if total_bytes() > MAX_TOTAL_BYTES:
        problems.append("the fixtures are {} KB; the limit is {} KB".format(total_bytes() // 1024, MAX_TOTAL_BYTES // 1024))
    for line in problems:
        print("make_v021_boards: " + line)
    if not problems:
        print("make_v021_boards: {} boards, {} KB".format(len(BOARDS), total_bytes() // 1024))
    return 1 if problems else 0


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="make_v021_boards.py", description="Write the golden 0.21 boards from the 0.21.2 code.")
    parser.add_argument("boards", nargs="*", help="board names (default: all)")
    parser.add_argument("--check", action="store_true", help="only check the fixtures exist and fit the size limit")
    args = parser.parse_args(list(argv))
    if args.check:
        return check()
    wanted = list(args.boards) or list(BOARDS)
    unknown = [name for name in wanted if name not in BOARDS]
    if unknown:
        parser.error("unknown board(s): {} (one of {})".format(", ".join(unknown), ", ".join(BOARDS)))
    previous = json.loads((README.parent / "report.json").read_text(encoding="utf-8")) if (README.parent / "report.json").is_file() else {}
    scratch = Path(tempfile.mkdtemp(prefix="v021-tree-"))
    try:
        tree = extract(scratch)
        reports = dict(previous)
        for name in wanted:
            board = legacy_mix() if name == "legacy-mix" else golden(name)
            reports[name] = build(tree, board)
            rep = reports[name]
            print("{}: {} applied, {} skipped, {} with dropped fields, {} refused".format(
                name, rep["applied"], len(rep["skipped"]), len(rep["dropped"]), len(rep["refused"])))
        write_readme(reports)
    finally:
        shutil.rmtree(os.fspath(scratch), ignore_errors=True)
    size = total_bytes()
    print("fixtures: {} KB".format(size // 1024))
    return 0 if size <= MAX_TOTAL_BYTES else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
