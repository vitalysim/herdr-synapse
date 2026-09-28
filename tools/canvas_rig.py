#!/usr/bin/env python3
"""A throwaway whiteboard page server for interaction tests (canvas v2 phase 1, 3.5).

It builds a temporary state root with one team (``tools/canvas_qa.QaTeam``: the
real HOME, socket and state are never touched), applies a scene, serves the
page on loopback and prints one JSON line::

    {"url": "http://127.0.0.1:PORT/?ticket=...&engine=v2", "team": "qa", "port": PORT, "dir": "..."}

The URL opens the page once (a one-use ticket, like ``whiteboard open``). The
server runs until ``--seconds`` pass or stdin closes, then writes
``<out>/events.jsonl`` and ``<out>/scene.json`` (what the page did, for the test
to assert on) and removes the temporary state.

    python3 tools/canvas_rig.py house --engine v2 --writable --seconds 180 --out /tmp/rig
    python3 tools/canvas_rig.py empty --engine v2 --writable
    python3 tools/canvas_rig.py synthetic-2000 --engine v2          # 2,000 boxes, for the page's frame budget

A scene is a golden scene name (``tests/fixtures/canvas_scenes``), a scene file,
``empty``, or ``synthetic-<n>`` (n boxes with short labels on a grid, drawn by
the operator so no rate limit applies). ``--writable`` opens the page as the
operator in person (it may draw); without it the page is read-only.

While it serves, the rig reads commands on stdin, one JSON object per line, and
answers each with one JSON line on stdout (canvas v2 phase 5, I-9), so a test can
act as other agents while a person uses the page. ``as`` is ``drawer`` (the
scene's member), ``peer`` (a second member), ``deputy`` (a member with an
operator grant) or ``lead`` (the operator in person over the CLI path)::

    {"as": "drawer", "ops": [...], "base": 12}        -> {"result": <apply result>}
    {"as": "peer", "focus": {"region": "c0r0:c20r10", "intent": "...", "status": "drawing", "ttl_s": 60}}  -> {"ok": true}
    {"as": "drawer", "look": {"region": "operator", "since": "last", "proposals": true}}  -> {"look": <look JSON>}
    {"settings": {"human_edits": "live"}}              -> {"result": <apply result>}   (as the lead)
    {"human": {"page": "0123456789abcdef", "viewport": [0, 0, 800, 600], "selection": ["E-1"]}}  -> {"ok": true}

A refusal answers ``{"error": {"code", "message", ...}}``. Closing stdin stops the rig.

Stdlib only; it imports ``herdr_team`` and ``canvas_qa`` from this checkout.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import canvas_qa as Q  # noqa: E402  (it puts the checkout on sys.path)

from herdr_team import canvas as C  # noqa: E402

SYNTHETIC_RE = re.compile(r"^synthetic-([1-9][0-9]{0,4})\Z")
#: Boxes per row of a synthetic board and the grid they sit on.
SYNTHETIC_COLUMNS = 50
SYNTHETIC_STEP = (200, 120)


def synthetic_ops(count: int) -> List[List[Dict[str, Any]]]:
    """``count`` labelled boxes on a grid, in batches the canvas accepts."""
    count = min(count, C.MAX_ELEMENTS)
    ops = [{"op": "shape", "kind": "box", "text": "Box {}".format(index + 1), "w": 160, "h": 80,
            "at": [(index % SYNTHETIC_COLUMNS) * SYNTHETIC_STEP[0], (index // SYNTHETIC_COLUMNS) * SYNTHETIC_STEP[1]],
            "tone": ("neutral", "info", "success", "warning", "danger", "accent", "idea", "decision")[index % 8],
            "intent": "synthetic board"} for index in range(count)]
    return [ops[i:i + C.MAX_BATCH_OPS] for i in range(0, len(ops), C.MAX_BATCH_OPS)]


def build(qa: Q.QaTeam, scene: str) -> None:
    """Apply ``scene`` to the rig's team: nothing, a synthetic board, or a golden scene as its member."""
    if scene == "empty":
        return
    found = SYNTHETIC_RE.match(scene)
    if found:
        operator = C.page_author(True)
        for batch in synthetic_ops(int(found.group(1))):
            C.check_applied(C.apply_ops(qa.layout, qa.team, batch, operator))
        return
    [path] = Q.scene_files([scene])
    doc = Q.load_scene_file(path)
    result = Q.apply_scene(qa, doc)
    if result["refused"]:
        raise SystemExit("scene {}: {} op(s) refused: {}".format(scene, len(result["refused"]), result["refused"][:3]))
    Q.apply_presence(qa, doc)


def command(qa: Q.QaTeam, message: Any) -> Dict[str, Any]:
    """One control command (I-9): apply ops, focus, look or settings as one of the rig's authors; the answer object."""
    import time

    from herdr_team import canvas_presence as P
    from herdr_team.errors import HerdrTeamError

    if not isinstance(message, dict):
        return {"error": {"code": "usage", "message": "a command is one JSON object"}}
    try:
        if "human" in message:
            P.write_human(qa.team, None, message["human"], time.time())
            return {"ok": True}
        if "settings" in message:
            op = dict(message["settings"] or {}, op="settings")
            return {"result": C.apply_ops(qa.layout, qa.team, [op], qa.author_for("lead"))}
        author = qa.author_for(message.get("as"))
        if "ops" in message:
            return {"result": C.apply_ops(qa.layout, qa.team, list(message["ops"] or []), author, base=message.get("base"),
                                          atomic=bool(message.get("atomic")))}
        if "focus" in message:
            focus = message["focus"] or {}
            region = C.parse_region(focus["region"], C.load_scene(qa.team), author.name) if focus.get("region") is not None else None
            P.write_member(qa.team, author, status=focus.get("status") or "drawing", region=region, ids=focus.get("ids") or (),
                           intent=focus.get("intent") or "", ttl_s=int(focus.get("ttl_s") or P.FOCUS_TTL_S), via="focus")
            return {"ok": True}
        if "look" in message:
            look = message["look"] or {}
            found = C.look(qa.layout, qa.team, author.name, region=look.get("region"), since=look.get("since"),
                           proposals=bool(look.get("proposals")), author=author, advance=bool(look.get("advance", True)))
            return {"look": found}
        return {"error": {"code": "usage", "message": "a command has ops, focus, look, settings or human"}}
    except HerdrTeamError as err:
        return {"error": err.to_json()}
    except (KeyError, TypeError, ValueError) as err:
        return {"error": {"code": "usage", "message": "{}: {}".format(type(err).__name__, err)}}


def _control(qa: Q.QaTeam, done: threading.Event) -> None:
    """Read commands on stdin until it closes, answering each with one line (I-9)."""
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                answer: Dict[str, Any] = {"error": {"code": "usage", "message": "not JSON"}}
            else:
                answer = command(qa, message)
            print(json.dumps(answer, ensure_ascii=False, default=str), flush=True)
    except (OSError, ValueError):
        return
    finally:
        done.set()


def serve(qa: Q.QaTeam, engine: str, writable: bool, seconds: float, out: Path, dist: Optional[Path] = None) -> int:
    from unittest import mock

    from herdr_team import activity, views
    from herdr_team import whiteboard_server as W

    with mock.patch.object(views, "team_views", side_effect=lambda *a, **k: []), \
            mock.patch.object(activity, "cards", side_effect=lambda *a, **k: []):
        server = W.make_server(qa.layout, qa.env, port=0, static_dir=dist, writable=writable, api=None)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = W.mint_ticket(qa.layout, writable, "human", port=port)
        if engine:
            url += "&engine=" + engine
        print(json.dumps({"url": url, "team": Q.TEAM, "port": port, "dir": os.fspath(out)}), flush=True)
        done = threading.Event()
        if not sys.stdin.isatty():
            threading.Thread(target=_control, args=(qa, done), daemon=True).start()
        try:
            done.wait(seconds)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
    out.mkdir(parents=True, exist_ok=True)
    events = C._file(qa.team, C.EVENTS_FILE)
    (out / "events.jsonl").write_bytes(events.read_bytes() if events.is_file() else b"")
    (out / "scene.json").write_text(json.dumps(C.load_scene(qa.team), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="canvas_rig.py", description="Serve a throwaway whiteboard page for interaction tests.")
    parser.add_argument("scene", nargs="?", default="empty", help="a golden scene name or file, empty, or synthetic-<n>")
    parser.add_argument("--engine", choices=("v1", "v2"), help="open the page with ?engine=")
    parser.add_argument("--writable", action="store_true", help="open the page as the operator in person (it may draw)")
    parser.add_argument("--seconds", type=float, default=120.0, help="serve this long at most (default 120)")
    parser.add_argument("--out", help="where events.jsonl and scene.json go (default: a new folder under the system temp dir)")
    parser.add_argument("--dist", help="serve this build of the page instead of web/dist (a scratch `vite build --outDir`)")
    args = parser.parse_args(argv)
    out = Path(args.out) if args.out else Path(__import__("tempfile").mkdtemp(prefix="canvas-rig-out-"))
    with Q.QaTeam() as qa:
        try:
            build(qa, args.scene)
        except ValueError as err:
            print(json.dumps({"error": str(err)}), flush=True)
            return 2
        return serve(qa, args.engine or "", bool(args.writable), max(1.0, float(args.seconds)), out, Path(args.dist) if args.dist else None)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
