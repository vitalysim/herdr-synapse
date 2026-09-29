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
* ``migrate`` (canvas v2 phase 6, ``canvas_migrate``): what a board drawn before
  canvas v2 looks like now, and the operator's one fix (``--apply``) or answer
  (``--dismiss``). ``draw --help`` prints the op table agents get over MCP.
* Collaboration (canvas v2 phase 5, ``canvas_collab``): ``accept``, ``reject``,
  ``withdraw``, ``freeze``, ``thaw``, ``checkpoint``, ``restore`` and
  ``settings`` build those ops; ``undo --author`` reverts one author's batches;
  ``draw --base`` says which version the batch was made against; ``focus``
  writes the member's presence (not an op).

Exit codes: 0 when at least one operation applied (or the batch was empty);
1 with ``canvas_refused`` when every operation was refused.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
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


def _apply(args: argparse.Namespace, ops: List[Dict[str, Any]], atomic: bool = False, base: Any = None) -> int:
    layout, _api, _author, team, doc, author = _open(args, write=True)
    result = C.check_applied(C.apply_ops(layout, team, ops, author, atomic=atomic, doc=doc, base=base))
    return emit(args, result, C.apply_text(result))


# --------------------------------------------------------------------------
# reading


def _look(args: argparse.Namespace) -> int:
    layout, _api, _author, team, doc, author = _open(args, write=False)
    result = C.look(layout, team, _reader(author), region=_json_arg(args.region), around=args.around, since=args.since,
                    image=bool(args.image or args.exact), grid=bool(args.grid), exact=bool(args.exact),
                    advance=_may_advance(author), doc=doc, theme=args.theme or "light", block=args.block, full=bool(args.full),
                    view=args.view, proposals=bool(args.proposals), author=author)
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
    base = C.parse_base(args.base) if args.base is not None else None
    if args.file:
        envelope = C.parse_envelope(_read_batch_file(args))
        ops.extend(envelope.ops)
        atomic = atomic or envelope.atomic
        base = base if base is not None else envelope.base
    for raw in args.op or []:
        try:
            op = json.loads(raw)
        except ValueError as err:
            raise UsageError("--op is not valid JSON ({}): {}".format(err, raw[:80]))
        if not isinstance(op, dict):
            raise UsageError("--op takes one JSON object, e.g. '{\"op\": \"shape\", \"text\": \"hi\", \"intent\": \"...\"}'")
        ops.append(op)
    ops, _atomic = C.parse_batch(ops)
    return _apply(args, ops, atomic, base)


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
    if (args.batch is None) == (args.author is None):
        raise UsageError("undo B-n, or undo --author NAME [--since N]")
    if args.since is not None and args.author is None:
        raise UsageError("--since goes with --author")
    op: Dict[str, Any] = {"op": "undo"}
    if args.batch is not None:
        op.update(batch=args.batch, intent=args.intent or "undo {}".format(args.batch))
    else:
        op.update(author=args.author, intent=args.intent or "revert {}'s batches".format(args.author))
        if args.since is not None:
            op["since"] = args.since
    if args.force:
        op["force"] = True
    return _apply(args, [op])


def _proposal_op(name: str) -> Any:
    def run(args: argparse.Namespace) -> int:
        op: Dict[str, Any] = {"op": name, "id": args.id, "intent": args.intent or "{} {}".format(name, args.id)}
        if getattr(args, "note", None):
            op["note"] = args.note
        return _apply(args, [op])
    return run


def _freeze(args: argparse.Namespace) -> int:
    if (args.region is None) == (args.ids is None):
        raise UsageError("freeze REGION, or freeze --ids E-1,E-2")
    op: Dict[str, Any] = {"op": "freeze", "intent": args.intent or "hold this as it is"}
    if args.region is not None:
        op["region"] = _json_arg(args.region)
    else:
        op["ids"] = [part.strip() for part in str(args.ids).split(",") if part.strip()]
    if args.label:
        op["label"] = args.label
    return _apply(args, [op])


def _thaw(args: argparse.Namespace) -> int:
    if (args.id is None) == (args.ids is None):
        raise UsageError("thaw X-n, or thaw --ids E-1,E-2")
    op: Dict[str, Any] = {"op": "thaw", "intent": args.intent or "let it change again"}
    if args.id is not None:
        op["id"] = args.id
    else:
        op["ids"] = [part.strip() for part in str(args.ids).split(",") if part.strip()]
    return _apply(args, [op])


def _checkpoint(args: argparse.Namespace) -> int:
    if (args.label is None) == (args.remove is None):
        raise UsageError("checkpoint LABEL, or checkpoint --remove V-n")
    if args.remove is not None:
        return _apply(args, [{"op": "checkpoint", "remove": args.remove, "intent": args.intent or "remove {}".format(args.remove)}])
    return _apply(args, [{"op": "checkpoint", "label": args.label, "intent": args.intent or _default_intent(args.label)}])


def _restore(args: argparse.Namespace) -> int:
    return _apply(args, [{"op": "restore", "id": args.id, "intent": args.intent or "restore {}".format(args.id)}])


def _settings(args: argparse.Namespace) -> int:
    if args.human_edits is None and args.frozen is None:
        layout, _api, _identity, team, doc, _author = _open(args, write=False)
        _features.require_on(layout.session, team, doc)
        settings = C.load_scene(team)["settings"]["collab"]
        text = "the operator's marks: {} · frozen areas: {}".format(
            "proposals" if settings["human_edits"] == "propose" else "live with revert", "proposals" if settings["frozen"] == "propose" else "refused")
        return emit(args, {"settings": settings}, text)
    op: Dict[str, Any] = {"op": "settings", "intent": args.intent or "collaboration settings"}
    if args.human_edits is not None:
        op["human_edits"] = args.human_edits
    if args.frozen is not None:
        op["frozen"] = args.frozen
    return _apply(args, [op])


def _migrate(args: argparse.Namespace) -> int:
    """``canvas migrate [--apply | --dismiss]`` (canvas v2 phase 6, 2.4): the report for anyone; the answer, the operator's."""
    from herdr_team import canvas_migrate as M

    if args.apply or args.dismiss:
        action = "apply" if args.apply else "dismiss"
        intent = args.intent or ("migrate to canvas v2" if action == "apply" else "dismiss the canvas v2 migration notice")
        return _apply(args, [{"op": "migrate", "action": action, "intent": intent}])
    layout, _api, _identity, team, doc, _author = _open(args, write=False)
    _features.require_on(layout.session, team, doc)
    found = M.report_for(team)
    return emit(args, {"migration": found, "summary": M.summary(found)}, M.text(found))


def _focus(args: argparse.Namespace) -> int:
    """Presence, not an op (phase 5, 9.1): where this member works, its status and intent, for ``--ttl`` seconds."""
    from herdr_team import canvas_presence as P

    layout, _api, _identity, team, doc, author = _open(args, write=True)
    _features.require_on(layout.session, team, doc)
    if not (author.is_member and author.verified):
        raise HerdrTeamError("usage", "focus is a verified member's presence; the operator's page reports hers", EXIT_REFUSED)
    if args.clear:
        P.clear_member(team, author.name)
        return emit(args, {"ok": True, "cleared": True}, "presence cleared")
    region = None
    ids: List[str] = []
    if args.where:
        scene = C.load_scene(team)
        target = _json_arg(args.where)
        region = C.parse_region(target, scene, author.name)
        if isinstance(target, str) and not target.startswith("[") and ":" not in target and "," not in target:
            try:
                ids = [C._lookup_in(C._State.from_scene(scene, team.name), target, author.name, "where")["id"]]
            except HerdrTeamError:
                ids = []
    ttl = args.ttl if args.ttl is not None else P.FOCUS_TTL_S
    if not P.FOCUS_TTL_RANGE[0] <= ttl <= P.FOCUS_TTL_RANGE[1]:
        raise UsageError("--ttl is {} to {} seconds".format(*P.FOCUS_TTL_RANGE))
    doc_out = P.write_member(team, author, status=args.status or "drawing", region=region, ids=ids, intent=args.intent or "", ttl_s=ttl,
                             via="focus")
    human = "focus: {} {}{} for {}s".format(args.status or "drawing", C.region_cells(region) if region else "(no region)",
                                           " " + C._q(args.intent, 60) if args.intent else "", ttl)
    return emit(args, {"ok": True, "presence": doc_out}, human)


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
    "accept": _proposal_op("accept"), "reject": _proposal_op("reject"), "withdraw": _proposal_op("withdraw"), "freeze": _freeze,
    "thaw": _thaw, "checkpoint": _checkpoint, "restore": _restore, "settings": _settings, "focus": _focus, "migrate": _migrate,
}

_SPECS = (
    ("look", "the canvas as text (the region in full, the rest one line each), changes since you looked, and optionally an image"),
    ("draw", "apply a batch of operations from --file PATH, --file - (stdin) or --op JSON: name components and their relations"),
    ("check", "run after every drawing meant for others; apply the listed fixes, check again (overlap, label_overflow and "
              "arrow_through must be 0)"),
    ("comment", "pin a comment to an element, a comment (a reply) or a point; @name mentions wake that member"),
    ("claim", "tell the team where you are about to draw (expires after 5 minutes)"),
    ("release", "release a claim (K-n), or all of yours"),
    ("legend", "record a drawing convention: SYMBOL MEANING, or --remove G-n"),
    ("portrait", "your plan as a small frame in your home: --from-todo, or --step TEXT ... [--current N]"),
    ("changes", "what changed since you last looked (or since a version)"),
    ("resolve", "mark a comment resolved"),
    ("undo", "undo a batch (B-n), or --author NAME [--since N]: every batch of one author; skips what others changed later (--force: the operator)"),
    ("refit", "size labels again (an old board, or text the page measured wider): --ids E-1,E-2, or every element you may edit"),
    ("lock", "lock a region so agents cannot draw in it (operator)"),
    ("unlock", "remove a lock (operator)"),
    ("send", "send elements to a member as a request, with their text form and a picture (operator)"),
    ("export", "the canvas as json, md (the text listing), svg, or png (png needs resvg)"),
    ("helper", "the path (or --print the source) of sketch.py, a helper that builds batches in Python"),
    ("mcp", "run the stdio MCP server that gives a harness the canvas tools (started by the harness at launch)"),
    ("icons", "the icon names cards, icons and shapes take (Lucide), or --search WORD"),
    ("catalog", "the chart types (charts) or the 3D primitives, relations and layouts (scene3d), each with an example op"),
    ("accept", "accept a proposal (P-n): exactly what it showed lands (the operator)"),
    ("reject", "reject a proposal (P-n), with an optional --note (the operator)"),
    ("withdraw", "take back your own open proposal (P-n)"),
    ("freeze", "hold a region (or --ids) as it is: others' changes there become proposals or are refused (the operator)"),
    ("thaw", "lift a freeze (X-n), or let go of --ids (the operator)"),
    ("checkpoint", "save the canvas as a named checkpoint (V-n), or --remove one of yours"),
    ("restore", "restore a checkpoint (V-n) as one batch; a checkpoint of now is saved first (the operator)"),
    ("settings", "the collaboration settings: --human-edits propose|live, --frozen propose|refuse (the operator); no flags prints them"),
    ("focus", "your presence on the canvas: where you work (REGION or ID), --status, --intent, --ttl; --clear removes it"),
    ("migrate", "a board drawn before canvas v2: what is drawn differently (anyone); --apply resizes old labels and draws the marks "
                "in 0.21's sketch style clean in one batch, --dismiss hides the notice (the operator)"),
)


def draw_epilog() -> str:
    """``canvas draw --help``'s epilog: the MCP ``canvas_draw`` op table and example, one text (canvas v2 phase 6, 3.3)."""
    from herdr_team import canvas_mcp

    def fill(text: str) -> str:
        return textwrap.fill(text, width=100, subsequent_indent="  ", break_long_words=False, break_on_hyphens=False)

    return "\n\n".join([fill(canvas_mcp.DRAW_LEAD), fill(canvas_mcp.op_table()), fill(canvas_mcp._EXAMPLE),
                         fill("Then: {} canvas check, apply the fixes it lists, check again; {} canvas look --image last.".format(CLI, CLI))])


def _lazy_parser(base: Any) -> Any:
    """A parser class whose epilog is built only when its help is printed (the draw op table imports every kind)."""

    class _Lazy(base):  # type: ignore[misc, valid-type]
        epilog_source: Optional[Any] = None

        def format_help(self) -> str:
            if self.epilog is None and self.epilog_source is not None:
                self.epilog = self.epilog_source()
            return super().format_help()

    return _Lazy


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="canvas_action", metavar="<action>", parser_class=_lazy_parser(type(parser)))
    parsers: Dict[str, argparse.ArgumentParser] = {}
    for name, help_text in _SPECS:
        extra: Dict[str, Any] = {"formatter_class": argparse.RawDescriptionHelpFormatter} if name == "draw" else {}
        p = sub.add_parser(name, help=help_text, description=help_text, allow_abbrev=False, **extra)
        add_global_arguments(p, nested=True)
        parsers[name] = p
    parsers["draw"].epilog_source = draw_epilog  # type: ignore[attr-defined]
    p = parsers["look"]
    where = p.add_mutually_exclusive_group()
    where.add_argument("--region", metavar="R", help='"c10r4:c40r22", "x0,y0,x1,y1", [x0,y0,x1,y1] or an element id')
    where.add_argument("--around", metavar="ID", help="an element or comment and 200 units around it")
    p.add_argument("--since", metavar="N|last", help="also list the changes after version N, or since you last looked")
    p.add_argument("--image", action="store_true", help="also render a PNG with id marks (needs resvg; the SVG is written either way)")
    p.add_argument("--grid", action="store_true", help="add cell dots and names to the image")
    p.add_argument("--exact", action="store_true", help="ask an open whiteboard page for its own picture of the board (falls back after 5 s)")
    p.add_argument("--theme", choices=("light", "dark"), help="the image's theme (default light)")
    p.add_argument("--block", metavar="REF", help="one block's whole spec (a kanban, a table ...), one item per line")
    p.add_argument("--full", action="store_true", help="also each block's part ids, top-level neighbours and details")
    p.add_argument("--view", metavar="VIEW", help="draw the image's 3D scenes from this view: iso, front or top")
    p.add_argument("--proposals", action="store_true", help="print every open proposal in full (its summary, base note, what outdated it)")
    p = parsers["check"]
    where = p.add_mutually_exclusive_group()
    where.add_argument("--region", metavar="R", help='"c10r4:c40r22", "x0,y0,x1,y1", [x0,y0,x1,y1] or an element id')
    where.add_argument("--around", metavar="ID", help="an element or comment and 200 units around it")
    p.add_argument("--mine", action="store_true", help="only problems that involve your own marks")
    p = parsers["draw"]
    p.add_argument("--file", metavar="PATH|-", help='a batch: {"ops": [...], "atomic": false} or a list of operations; - reads standard input')
    p.add_argument("--op", action="append", metavar="JSON", help="one operation as JSON (repeatable, applied after --file)")
    p.add_argument("--atomic", action="store_true", help="apply all or nothing")
    p.add_argument("--base", metavar="N|last", help="the canvas version you last read (last: your look cursor); what the operator changed "
                                                    "since is refused, what others changed applies with a warning")
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
    p.add_argument("batch", metavar="B-n", nargs="?")
    p.add_argument("--author", metavar="NAME", help="every not-undone batch of this author (a member, or human)")
    p.add_argument("--since", type=int, metavar="N", help="with --author: only batches after version N")
    p.add_argument("--force", action="store_true", help="write back even what others changed later (the operator)")
    p.add_argument("--intent", metavar="TEXT")
    for name in ("accept", "reject", "withdraw"):
        p = parsers[name]
        p.add_argument("id", metavar="P-n")
        if name != "withdraw":
            p.add_argument("--note", metavar="TEXT", help="one line for the proposer")
        p.add_argument("--intent", metavar="TEXT")
    p = parsers["freeze"]
    p.add_argument("region", nargs="?", metavar="REGION", help='"c10r4:c40r22", "x0,y0,x1,y1" or an element id')
    p.add_argument("--ids", metavar="E-1,E-2", help="freeze these elements (and what they hold) instead of a region")
    p.add_argument("--label", metavar="TEXT")
    p.add_argument("--intent", metavar="TEXT")
    p = parsers["thaw"]
    p.add_argument("id", nargs="?", metavar="X-n")
    p.add_argument("--ids", metavar="E-1,E-2", help="let go of these elements in every id freeze")
    p.add_argument("--intent", metavar="TEXT")
    p = parsers["checkpoint"]
    p.add_argument("label", nargs="?", metavar="LABEL")
    p.add_argument("--remove", metavar="V-n")
    p.add_argument("--intent", metavar="TEXT")
    p = parsers["restore"]
    p.add_argument("id", metavar="V-n")
    p.add_argument("--intent", metavar="TEXT")
    p = parsers["settings"]
    p.add_argument("--human-edits", dest="human_edits", choices=("propose", "live"), help="agents' changes to the operator's marks")
    p.add_argument("--frozen", choices=("propose", "refuse"), help="agents' changes in frozen areas")
    p.add_argument("--intent", metavar="TEXT")
    p = parsers["focus"]
    p.add_argument("where", nargs="?", metavar="REGION|ID", help='where you work: "c10r4:c40r22" or an element')
    p.add_argument("--status", choices=("reading", "drawing", "waiting", "blocked", "idle"), help="default drawing")
    p.add_argument("--intent", metavar="TEXT", help="one line: what you are doing")
    p.add_argument("--ttl", type=int, metavar="SECONDS", help="how long it shows (60 to 3600, default 600)")
    p.add_argument("--clear", action="store_true", help="remove your presence")
    p = parsers["migrate"]
    how = p.add_mutually_exclusive_group()
    how.add_argument("--apply", action="store_true", help="resize the labels from before canvas v2 and draw the marks in 0.21's sketch "
                                                          "style clean, in one batch (undo B-n takes it back); the operator")
    how.add_argument("--dismiss", action="store_true", help="hide the notice and change nothing; the operator")
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
    Command("canvas", "the team canvas: look, draw, check, comment, claim, legend, portrait, changes, migrate (whiteboard must be on)", _add_arguments, _run,
            description="Look at, draw on, and point at the team's shared canvas. Agents send Synapse Sketch operations; "
                        "see herdr-synapse skill get --reference canvas."),
]
