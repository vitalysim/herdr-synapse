"""``canvas``: look at, draw on, and point at the team canvas (0.21).

One ``Command("canvas", ...)`` with the sub-commands of
``.local/prd/canvas-contracts.md`` section 10.1. Identity comes from
``cmd_board._open_team`` exactly as for ``post``, and becomes a
``canvas.CanvasAuthor``; every sub-command refuses ``whiteboard_off`` while
the team's canvas is off (``features.require_on`` inside ``canvas``).

* ``look`` / ``changes`` read, and move the reader's "since you looked"
  cursor for a verified member or the operator.
* ``draw`` applies a batch from a file, standard input, or ``--op`` JSON.
  ``comment``, ``claim``, ``release``, ``legend``, ``portrait``, ``resolve``,
  ``undo``, ``lock`` and ``unlock`` build the same operations (``--intent``
  defaults to the label or text itself).
* ``send`` (the operator's "send to member"), ``export``, ``helper`` (the
  standalone ``sketch.py`` for agents' own scripts) and ``mcp`` (the stdio MCP
  server a harness starts at launch) complete the set.

Exit codes: 0 when at least one operation applied (or the batch was empty);
1 with ``canvas_refused`` when every operation was refused.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import canvas as C
from herdr_team import canvas_render as _render
from herdr_team import features as _features
from herdr_team import store
from herdr_team.cli import Command, add_global_arguments, api_for, emit, layout_for
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError
from herdr_team.paths import TeamPaths, team_name_from_arg

CLI = "herdr-synapse"
#: A batch file may carry a little more than the batch itself (whitespace, the envelope).
MAX_FILE_BYTES = C.MAX_BATCH_BYTES + 64 * 1024
EXPORT_FORMATS = ("json", "md", "svg", "png")


# --------------------------------------------------------------------------
# shared plumbing


def _open(args: argparse.Namespace, write: bool) -> Tuple[Any, Any, Any, TeamPaths, Dict[str, Any], C.CanvasAuthor]:
    """``(layout, api, identity author, team paths, team.json, canvas author)`` as ``post`` resolves them."""
    from herdr_team.cmd_board import _open_team

    layout, api, author, _team_name, team, doc = _open_team(args, require_server=write, write=write)
    return layout, api, author, team, doc, C.author_from_identity(author, doc, via="cli")


def _reader(author: C.CanvasAuthor) -> str:
    return author.name if author.kind != C.KIND_SYSTEM else "system"


def _may_advance(author: C.CanvasAuthor) -> bool:
    """Only a verified member or the operator moves its "since you looked" cursor."""
    return (author.is_member and author.verified) or (author.is_human and author.operator)


def _json_arg(value: Optional[str]) -> Any:
    """A region or point given as JSON (``[200, 80, 800, 440]``) stays a list; anything else is passed as text."""
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            return json.loads(value)
        except ValueError:
            raise UsageError("{} is not valid JSON".format(value))
    return value


def _default_intent(text: str) -> str:
    line = " ".join(str(text or "").split())
    return line[: C.MAX_INTENT_CHARS - 1].rstrip() + "…" if len(line) > C.MAX_INTENT_CHARS else line


def _apply(args: argparse.Namespace, ops: List[Dict[str, Any]], atomic: bool = False) -> int:
    layout, _api, _author, team, doc, author = _open(args, write=True)
    result = C.check_applied(C.apply_ops(layout, team, ops, author, atomic=atomic, doc=doc))
    return emit(args, result, C.apply_text(result))


# --------------------------------------------------------------------------
# reading


def _look(args: argparse.Namespace) -> int:
    layout, _api, _author, team, doc, author = _open(args, write=False)
    result = C.look(layout, team, _reader(author), region=_json_arg(args.region), around=args.around, since=args.since,
                    image=bool(args.image or args.exact), grid=bool(args.grid), exact=bool(args.exact),
                    advance=_may_advance(author), doc=doc, theme=args.theme or "light", block=args.block, full=bool(args.full),
                    view=args.view)
    return emit(args, result, result["text"])


def _catalog(args: argparse.Namespace) -> int:
    """``canvas catalog charts|scene3d [--type NAME]``: the chart types, or the 3D primitives, relations and layouts, with
    their channels or parameters, options and one example op each (phases 3 and 4, 1.6); generated from the registries,
    so it needs no team."""
    from herdr_team import canvas_catalog

    doc = canvas_catalog.catalog(args.what, args.type)
    return emit(args, doc, canvas_catalog.text(doc))


def _icons(args: argparse.Namespace) -> int:
    """``canvas icons [--search WORD]``: the Lucide icon names the canvas draws (phase 2, 5.3); needs no team."""
    from herdr_team import canvas_icons

    found = canvas_icons.search(args.search) if args.search else canvas_icons.names()
    text = "\n".join(found) if found else "no icon matches {}; try a shorter word".format(args.search)
    return emit(args, {"icons": found, "search": args.search}, text)


def _check(args: argparse.Namespace) -> int:
    layout, _api, _author, team, doc, author = _open(args, write=False)
    result = C.check(layout, team, _reader(author), region=_json_arg(args.region), around=args.around, mine=bool(args.mine), doc=doc)
    return emit(args, result, result["text"])


def _changes(args: argparse.Namespace) -> int:
    layout, _api, _author, team, doc, author = _open(args, write=False)
    result = C.read_changes(layout, team, _reader(author), since=args.since or "last", advance=_may_advance(author), doc=doc)
    return emit(args, result, result["text"])


# --------------------------------------------------------------------------
# drawing


def _read_batch_file(args: argparse.Namespace) -> str:
    if args.file == "-":
        stream = getattr(args, "stdin", None) or sys.stdin
        raw = stream.read(MAX_FILE_BYTES + 1)
    else:
        path = Path(os.path.expanduser(args.file))
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise HerdrTeamError("canvas_limit", "{} is larger than a batch may be ({} KB)".format(path, C.MAX_BATCH_BYTES // 1024),
                                     EXIT_REFUSED, {"limit": "MAX_BATCH_BYTES", "max": C.MAX_BATCH_BYTES})
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            raise UsageError("cannot read {}: {}".format(path, err))
    if len(raw) > MAX_FILE_BYTES:
        raise HerdrTeamError("canvas_limit", "the batch is larger than {} KB".format(C.MAX_BATCH_BYTES // 1024), EXIT_REFUSED,
                             {"limit": "MAX_BATCH_BYTES", "max": C.MAX_BATCH_BYTES})
    return raw


def _draw(args: argparse.Namespace) -> int:
    if not args.file and not args.op:
        raise UsageError("draw needs --file PATH, --file - (standard input) or --op JSON")
    ops: List[Dict[str, Any]] = []
    atomic = bool(args.atomic)
    if args.file:
        file_ops, file_atomic = C.parse_batch(_read_batch_file(args))
        ops.extend(file_ops)
        atomic = atomic or file_atomic
    for raw in args.op or []:
        try:
            op = json.loads(raw)
        except ValueError as err:
            raise UsageError("--op is not valid JSON ({}): {}".format(err, raw[:80]))
        if not isinstance(op, dict):
            raise UsageError("--op takes one JSON object, e.g. '{\"op\": \"shape\", \"text\": \"hi\", \"intent\": \"...\"}'")
        ops.append(op)
    ops, _atomic = C.parse_batch(ops)
    return _apply(args, ops, atomic)


def _comment(args: argparse.Namespace) -> int:
    op: Dict[str, Any] = {"op": "comment", "at": _json_arg(args.at), "text": args.text, "intent": args.intent or _default_intent(args.text)}
    if args.mention:
        op["mentions"] = list(args.mention)
    if args.reply_to:
        op["reply_to"] = args.reply_to
    return _apply(args, [op])


def _claim(args: argparse.Namespace) -> int:
    return _apply(args, [{"op": "claim", "region": _json_arg(args.region), "label": args.label, "intent": args.intent or _default_intent(args.label)}])


def _release(args: argparse.Namespace) -> int:
    op: Dict[str, Any] = {"op": "release", "intent": "release {}".format(args.id) if args.id else "release my claims"}
    if args.id:
        op["id"] = args.id
    return _apply(args, [op])


def _legend(args: argparse.Namespace) -> int:
    words = list(args.words or [])
    if args.remove:
        if words:
            raise UsageError("legend --remove G-n takes no symbol or meaning")
        return _apply(args, [{"op": "legend", "remove": args.remove, "intent": args.intent or "remove legend {}".format(args.remove)}])
    if len(words) != 2:
        raise UsageError("legend SYMBOL MEANING (quote each), or legend --remove G-n")
    symbol, meaning = words
    return _apply(args, [{"op": "legend", "symbol": symbol, "meaning": meaning, "intent": args.intent or _default_intent("{} = {}".format(symbol, meaning))}])


def _portrait(args: argparse.Namespace) -> int:
    layout, api, identity_author, team, doc, author = _open(args, write=True)
    # Off means no watch reading either (contract 3.2): refuse before --from-todo opens a transcript.
    _features.require_on(layout.session, team, doc)
    intent = args.intent or "my current plan"
    if args.from_todo:
        if args.step:
            raise UsageError("use --from-todo or --step, not both")
        if not author.is_member:
            raise HerdrTeamError("portrait_no_plan", "--from-todo reads a member's own to-do list; pass --step instead", EXIT_REFUSED)
        row = C._member_row(doc, author.name) or {}
        target = dict(row, team=team.name)
        plan = None
        try:
            from herdr_team import activity as _activity

            plan = _activity.plan_of(layout, target, api=api, env=args.env)
        except (HerdrTeamError, OSError, ValueError, TypeError):
            plan = None
        if not plan or not plan.get("steps"):
            raise HerdrTeamError("portrait_no_plan", "no to-do list found for {}; pass --step TEXT for each step (and --current N)".format(author.name),
                                 EXIT_REFUSED, {"member": author.name})
        op = C.portrait_op(plan, intent, args.title)
    else:
        if not args.step:
            raise UsageError("portrait needs --from-todo or --step TEXT (repeat it, one per step)")
        op = {"op": "portrait", "steps": list(args.step), "intent": intent}
        if args.current is not None:
            op["current"] = args.current
        if args.title:
            op["title"] = args.title
    result = C.check_applied(C.apply_ops(layout, team, [op], author, doc=doc))
    return emit(args, result, C.apply_text(result))


def _resolve(args: argparse.Namespace) -> int:
    return _apply(args, [{"op": "resolve", "id": args.id, "intent": args.intent or "resolve {}".format(args.id)}])


def _refit(args: argparse.Namespace) -> int:
    op: Dict[str, Any] = {"op": "refit", "intent": args.intent or "size labels again"}
    if args.ids:
        op["ids"] = [part.strip() for part in str(args.ids).split(",") if part.strip()]
    return _apply(args, [op])


def _undo(args: argparse.Namespace) -> int:
    return _apply(args, [{"op": "undo", "batch": args.batch, "intent": args.intent or "undo {}".format(args.batch)}])


def _lock(args: argparse.Namespace) -> int:
    op: Dict[str, Any] = {"op": "lock", "region": _json_arg(args.region), "intent": args.intent or "lock this region"}
    if args.label:
        op["label"] = args.label
    return _apply(args, [op])


def _unlock(args: argparse.Namespace) -> int:
    return _apply(args, [{"op": "unlock", "id": args.id, "intent": args.intent or "unlock {}".format(args.id)}])


# --------------------------------------------------------------------------
# the operator's tools, export, helper, mcp


def _send(args: argparse.Namespace) -> int:
    layout, _api, _identity, team, _doc, author = _open(args, write=True)
    result = C.send_to_member(layout, team, list(args.ids), args.to, args.note, author)
    human = "sent {} to {} (board #{}); image: {}".format(", ".join(result["elements"]), args.to, result["seq"], result["image"] or "unavailable")
    return emit(args, result, human)


def _write_out(path: str, data: bytes) -> str:
    target = Path(os.path.expanduser(path))
    try:
        store.atomic_write(target, data, fsync=False)
    except OSError as err:
        raise UsageError("cannot write {}: {}".format(target, err))
    return os.fspath(target)


def _export(args: argparse.Namespace) -> int:
    layout, _api, _identity, team, doc, author = _open(args, write=False)
    _features.require_on(layout.session, team, doc)
    reader = _reader(author)
    fmt = args.format
    scene = C.load_scene(team)
    region = C.parse_region(_json_arg(args.region), scene, reader) if args.region else None
    if fmt == "md":
        text = C.look(layout, team, reader, region=region, advance=False, doc=doc)["text"]
        if args.out:
            path = _write_out(args.out, (text + "\n").encode("utf-8"))
            return emit(args, {"format": fmt, "path": path}, path)
        return emit(args, {"format": fmt, "text": text}, text)
    if fmt == "json":
        body = json.dumps(scene, ensure_ascii=False, indent=2)
        if args.out:
            path = _write_out(args.out, (body + "\n").encode("utf-8"))
            return emit(args, {"format": fmt, "path": path}, path)
        return emit(args, {"format": fmt, "scene": scene}, body)
    rendered = _render.render_region(team, scene, region, C._dir(team) / C.RENDERS_DIR,
                                     "export-v{}-{}".format(scene["version"], "all" if region is None else "-".join(str(int(v)) for v in region)),
                                     marks=bool(args.marks), grid=bool(args.grid), reader=reader)
    if fmt == "svg":
        source = Path(rendered["svg"])
        if args.out:
            path = _write_out(args.out, source.read_bytes())
            return emit(args, {"format": fmt, "path": path}, path)
        return emit(args, {"format": fmt, "path": rendered["svg"]}, source.read_text(encoding="utf-8"))
    if not rendered["png"]:
        raise HerdrTeamError("render_unavailable", "no PNG: resvg is not installed (or failed: {}); use --format svg, or install resvg".format(rendered["image_error"]),
                             EXIT_REFUSED, {"image_error": rendered["image_error"], "svg": rendered["svg"]})
    path = _write_out(args.out, Path(rendered["png"]).read_bytes()) if args.out else rendered["png"]
    return emit(args, {"format": fmt, "path": path, "width_px": rendered["width_px"], "height_px": rendered["height_px"]}, path)


def _helper(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    _features.require_layer(layout.session)
    path = Path(__file__).resolve().parent / "sketch.py"
    payload: Dict[str, Any] = {"path": os.fspath(path), "dir": os.fspath(path.parent)}
    if args.print:
        payload["source"] = path.read_text(encoding="utf-8")
        return emit(args, payload, payload["source"])
    human = "{}\nimport it with: sys.path.insert(0, {!r}); from sketch import Sketch\nor copy it next to your script: {} canvas helper --print > sketch.py".format(
        payload["path"], payload["dir"], CLI)
    return emit(args, payload, human)


def _mcp(args: argparse.Namespace) -> int:
    from herdr_team import canvas_mcp

    layout = layout_for(args)
    api = api_for(args, layout)
    team_name = team_name_from_arg(getattr(args, "team", None), args.env)
    stdin = getattr(args, "stdin", None) or sys.stdin
    return canvas_mcp.serve(layout, args.env, stdin=stdin, stdout=args.stdout, api=api, team_name=team_name)


# --------------------------------------------------------------------------
# argparse

_ACTIONS = {
    "look": _look, "check": _check, "draw": _draw, "comment": _comment, "claim": _claim, "release": _release, "legend": _legend,
    "portrait": _portrait, "changes": _changes, "resolve": _resolve, "undo": _undo, "refit": _refit, "lock": _lock, "unlock": _unlock,
    "send": _send, "export": _export, "helper": _helper, "mcp": _mcp, "icons": _icons, "catalog": _catalog,
}

_SPECS = (
    ("look", "the canvas as text (the region in full, the rest one line each), changes since you looked, and optionally an image"),
    ("check", "layout problems (overlaps, labels that do not fit, marks half in a frame, arrows through shapes, strays), each with a fix to apply"),
    ("draw", "apply a batch of operations from --file PATH, --file - (stdin) or --op JSON"),
    ("comment", "pin a comment to an element, a comment (a reply) or a point; @name mentions wake that member"),
    ("claim", "tell the team where you are about to draw (expires after 5 minutes)"),
    ("release", "release a claim (K-n), or all of yours"),
    ("legend", "record a drawing convention: SYMBOL MEANING, or --remove G-n"),
    ("portrait", "your plan as a small frame in your home: --from-todo, or --step TEXT ... [--current N]"),
    ("changes", "what changed since you last looked (or since a version)"),
    ("resolve", "mark a comment resolved"),
    ("undo", "undo a whole batch (B-n): yours; the manager any agent's; the operator any"),
    ("refit", "size labels again (an old board, or text the page measured wider): --ids E-1,E-2, or every element you may edit"),
    ("lock", "lock a region so agents cannot draw in it (operator)"),
    ("unlock", "remove a lock (operator)"),
    ("send", "send elements to a member as a request, with their text form and a picture (operator)"),
    ("export", "the canvas as json, md (the text listing), svg, or png (png needs resvg)"),
    ("helper", "the path (or --print the source) of sketch.py, a helper that builds batches in Python"),
    ("mcp", "run the stdio MCP server that gives a harness the canvas tools (started by the harness at launch)"),
    ("icons", "the icon names cards, icons and shapes take (Lucide), or --search WORD"),
    ("catalog", "the chart types (charts) or the 3D primitives, relations and layouts (scene3d), each with an example op"),
)


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="canvas_action", metavar="<action>")
    parsers: Dict[str, argparse.ArgumentParser] = {}
    for name, help_text in _SPECS:
        p = sub.add_parser(name, help=help_text, description=help_text, allow_abbrev=False)
        add_global_arguments(p, nested=True)
        parsers[name] = p
    p = parsers["look"]
    where = p.add_mutually_exclusive_group()
    where.add_argument("--region", metavar="R", help='"c10r4:c40r22", "x0,y0,x1,y1", [x0,y0,x1,y1] or an element id')
    where.add_argument("--around", metavar="ID", help="an element or comment and 200 units around it")
    p.add_argument("--since", metavar="N|last", help="also list the changes after version N, or since you last looked")
    p.add_argument("--image", action="store_true", help="also render a PNG with id marks (needs resvg; the SVG is written either way)")
    p.add_argument("--grid", action="store_true", help="add cell dots and names to the image")
    p.add_argument("--exact", action="store_true", help="ask an open whiteboard page for the engine's own export (falls back after 5 s)")
    p.add_argument("--theme", choices=("light", "dark"), help="the image's theme (default light)")
    p.add_argument("--block", metavar="REF", help="one block's whole spec (a kanban, a table ...), one item per line")
    p.add_argument("--full", action="store_true", help="also each block's part ids, top-level neighbours and details")
    p.add_argument("--view", metavar="VIEW", help="draw the image's 3D scenes from this view: iso, front or top")
    p = parsers["check"]
    where = p.add_mutually_exclusive_group()
    where.add_argument("--region", metavar="R", help='"c10r4:c40r22", "x0,y0,x1,y1", [x0,y0,x1,y1] or an element id')
    where.add_argument("--around", metavar="ID", help="an element or comment and 200 units around it")
    p.add_argument("--mine", action="store_true", help="only problems that involve your own marks")
    p = parsers["draw"]
    p.add_argument("--file", metavar="PATH|-", help='a batch: {"ops": [...], "atomic": false} or a list of operations; - reads standard input')
    p.add_argument("--op", action="append", metavar="JSON", help="one operation as JSON (repeatable, applied after --file)")
    p.add_argument("--atomic", action="store_true", help="apply all or nothing")
    p = parsers["comment"]
    p.add_argument("at", metavar="AT", help="an element (E-3 or an alias), a comment to reply to (C-4), a cell (c11r6) or x,y")
    p.add_argument("text", metavar="TEXT", help="the comment (<= 1000 characters); @name mentions a member")
    p.add_argument("--mention", action="append", metavar="NAME", help="also mention this member (or human); repeatable")
    p.add_argument("--reply-to", dest="reply_to", metavar="C-n")
    p.add_argument("--intent", metavar="TEXT", help="why (default: the comment itself)")
    p = parsers["claim"]
    p.add_argument("region", metavar="REGION", help='"c10r4:c40r22", "x0,y0,x1,y1" or an element id')
    p.add_argument("label", metavar="LABEL", help="what you are doing there")
    p.add_argument("--intent", metavar="TEXT")
    parsers["release"].add_argument("id", nargs="?", metavar="K-n")
    p = parsers["legend"]
    p.add_argument("words", nargs="*", metavar="SYMBOL MEANING", help='an element id or a few words, then what it means')
    p.add_argument("--remove", metavar="G-n")
    p.add_argument("--intent", metavar="TEXT")
    p = parsers["portrait"]
    p.add_argument("--from-todo", dest="from_todo", action="store_true", help="read your harness's own to-do list")
    p.add_argument("--step", action="append", metavar="TEXT", help="one step of your plan (repeatable, in order)")
    p.add_argument("--current", type=int, metavar="N", help="the step you are on (1-based); earlier ones show as done")
    p.add_argument("--title", metavar="TEXT")
    p.add_argument("--intent", metavar="TEXT")
    parsers["changes"].add_argument("--since", metavar="N|last", help="default: since you last looked")
    p = parsers["resolve"]
    p.add_argument("id", metavar="C-n")
    p.add_argument("--intent", metavar="TEXT")
    p = parsers["undo"]
    p.add_argument("batch", metavar="B-n")
    p.add_argument("--intent", metavar="TEXT")
    p = parsers["refit"]
    p.add_argument("--ids", metavar="E-1,E-2", help="the elements to size again (default: every element you may edit, up to 500)")
    p.add_argument("--intent", metavar="TEXT")
    p = parsers["lock"]
    p.add_argument("region", metavar="REGION")
    p.add_argument("--label", metavar="TEXT", help='shown on the lock (default "hands off")')
    p.add_argument("--intent", metavar="TEXT")
    p = parsers["unlock"]
    p.add_argument("id", metavar="X-n")
    p.add_argument("--intent", metavar="TEXT")
    p = parsers["send"]
    p.add_argument("ids", nargs="+", metavar="ID")
    p.add_argument("--to", required=True, metavar="NAME", help="the member who gets the request")
    p.add_argument("--note", metavar="TEXT", help='what you want ("merge these")')
    p = parsers["export"]
    p.add_argument("--format", choices=EXPORT_FORMATS, default="md")
    p.add_argument("--region", metavar="R")
    p.add_argument("--out", metavar="PATH", help="write the file here instead of printing it")
    p.add_argument("--no-marks", dest="marks", action="store_false", help="svg/png: no id badges")
    p.add_argument("--grid", action="store_true", help="svg/png: cell dots and names")
    parsers["helper"].add_argument("--print", action="store_true", help="print the source instead of the path")
    parsers["icons"].add_argument("--search", metavar="WORD", help="only the icons whose name holds WORD")
    p = parsers["catalog"]
    p.add_argument("what", choices=("charts", "scene3d"), help="charts: the chart types; scene3d: the 3D primitives, relations and layouts")
    p.add_argument("--type", metavar="NAME", help="only this chart type or primitive (with its whole entry)")


def _run(args: argparse.Namespace) -> int:
    action = getattr(args, "canvas_action", None)
    if action is None:
        raise UsageError("canvas needs an action: " + ", ".join(name for name, _h in _SPECS))
    return _ACTIONS[action](args)


COMMANDS: List[Command] = [
    Command("canvas", "the team canvas: look, check, draw, comment, claim, legend, portrait, changes (whiteboard must be on)", _add_arguments, _run,
            description="Look at, draw on, and point at the team's shared canvas. Agents send Synapse Sketch operations; "
                        "see herdr-synapse skill get --reference canvas."),
]
