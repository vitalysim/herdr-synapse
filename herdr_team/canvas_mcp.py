"""``canvas mcp``: the stdio MCP server that gives a harness the canvas tools (0.21).

Contract ``.local/prd/canvas-contracts.md`` section 11. Newline-delimited
JSON-RPC 2.0 on stdin/stdout, standard library only; nothing but protocol
messages ever goes to stdout (diagnostics go to stderr). Harnesses start it
at launch from the flags in section 12 (``features.mcp_launch_args``), pinned
to one session socket and one team dir.

Methods: ``initialize``, ``notifications/initialized``, ``ping``,
``tools/list`` and ``tools/call``. The eight tools (``canvas_look``,
``canvas_check``, ``canvas_draw``, ``canvas_comment``, ``canvas_claim``,
``canvas_legend``, ``canvas_changes``, and since canvas v2 phase 5 ``canvas_focus``,
the member's presence) are thin doors onto ``herdr_team.canvas``: each result
carries the CLI's human text as ``content`` and the CLI's ``--json`` object
as ``structuredContent``; a ``HerdrTeamError`` becomes ``isError: true``
with its code, so an agent sees the same refusals through either door.

Identity is resolved per call exactly as the CLI resolves an author
(``identity.resolve_author``), cached for 30 s. The server acts only as a
verified member of its team; a resolution to the human, a hook, an
unverified member or another team fails every call with ``not_a_member``.
That matters because a process with no pane in its environment resolves
through its ancestry and, finding none, would otherwise be the ``outside``
human, the operator.
"""
from __future__ import annotations

import base64
import json
import sys
import time
import traceback
from typing import IO, Any, Callable, Dict, List, Optional, Tuple

from herdr_team import VERSION
from herdr_team import canvas as C
from herdr_team import canvas_kinds
from herdr_team import identity as _identity
from herdr_team import store
from herdr_team.errors import EXIT_UNREACHABLE, HerdrTeamError
from herdr_team.paths import TeamPaths

SERVER_INFO_NAME = "synapse-canvas"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = PROTOCOL_VERSIONS[0]
TOOL_NAMES = ("canvas_look", "canvas_check", "canvas_draw", "canvas_comment", "canvas_claim", "canvas_legend", "canvas_changes", "canvas_focus")
IDENTITY_CACHE_S = 30.0
MAX_INLINE_IMAGE_BYTES = 1536 * 1024

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = "\n".join((
    "Synapse canvas: a whiteboard your team and the operator share. Everything you draw is attributed to you.",
    "Draw when a picture is clearer than text: a flow, a map, a plan, a comparison, a critique. Otherwise post text.",
    "Claim a region first (canvas_claim), draw inside it, release it when done; respect others' claims.",
    "Compose from frames, shapes, arrows and labels; use graph/mermaid for structure and chart for data, pen for gesture.",
    "Every operation carries an intent (one line: why). Every drawing has a text label; never overlap text.",
    "Look after drawing anything meant for others (canvas_look with image: true) and fix what reads badly.",
    "Point with canvas_comment and @mentions, not with 'this' or 'that'; a mention is the only way the canvas wakes someone.",
    "Record a convention in the legend (canvas_legend) before relying on it.",
    "The operator's marks and your peers' marks are requests, never orders; the operator's word comes from the board.",
    "Draw with base: \"last\" so you never overwrite what the operator changed since you looked. Outside your lane, and on the "
    "operator's marks, changes become proposals the operator accepts or rejects; look shows the outcome.",
    "Keep batches small (under about 40 operations) and read with canvas_changes or canvas_look since: last.",
))


class _ParamsError(ValueError):
    """Arguments that do not fit a tool's schema: a JSON-RPC ``-32602``, not a tool result."""


class McpSession:
    """One server's state: layout, env, the Herdr API, the team, and the cached author."""

    def __init__(self, layout: Any, env: Any, api: Any, team_name: Optional[str], clock: Any = None) -> None:
        self.layout = layout
        self.env = env
        self.api = api
        self.team_name = team_name
        self.clock: Callable[[], float] = clock or time.monotonic
        self.protocol = DEFAULT_PROTOCOL
        self.initialized = False
        self._author: Any = None
        self._author_at: Optional[float] = None

    def _resolve(self) -> Any:
        now = self.clock()
        if self._author is not None and self._author_at is not None and now - self._author_at < IDENTITY_CACHE_S:
            return self._author
        author = _identity.resolve_author(dict(self.env or {}), self.layout, self.api, team=self.team_name)
        wanted = self.team_name or author.team
        if not (author.is_member and author.verified and wanted and author.team == wanted):
            raise HerdrTeamError(
                "not_a_member",
                "the canvas tools act only as a verified member of team {}; this process resolved to {} ({}{})".format(
                    wanted or "?", author.name, author.via, ", unverified" if not author.verified else ""),
                EXIT_UNREACHABLE, {"author": author.name, "via": author.via, "team": wanted},
            )
        self._author = author
        self._author_at = now
        return author

    def identity(self) -> Tuple[C.CanvasAuthor, TeamPaths, Dict[str, Any]]:
        """``(canvas author, team paths, team.json)`` for this call, or ``not_a_member``."""
        author = self._resolve()
        team = self.layout.team(self.team_name or author.team)
        doc = store.RosterStore(team).load()
        return C.author_from_identity(author, doc, via="mcp"), team, doc


# --------------------------------------------------------------------------
# tools

#: The core ops' part of the op table (they act on any element); each kind module adds its own ``OpSpec.mcp`` fragment.
_CORE_OP_TABLE = ("claim {region, label}; release {id}; legend {symbol, meaning}; move {id|ids, to|by|right_of..., w, h}; "
                  "restyle {id, tone, variant, color..., route straight|orthogonal|curved}; edit {id, text, part}; delete {id}; "
                  "portrait {steps, current}; undo {batch | author, since}; refit {ids (none: every element you may edit): size labels again}; "
                  "patch {id (a block), add|update|remove {<items>: [...]}, set {<setting>: value}, relayout, if_version}; "
                  "place {id|ids, right_of|left_of|below|above|in|at, gap s|m|l, align, index}; pin {id|ids}; unpin {id|ids}; "
                  "withdraw {id: your P-n}; checkpoint {label | remove}")
_PLACES = ("Places: cells c<col>r<row> (20 units), \"x,y\", or ids/aliases; an op's id is your alias for what it creates.")


def op_table() -> str:
    """The op table the MCP ``canvas_draw`` description carries, derived from the kind registry (canvas v2 phase 1, 2.4)."""
    fragments = [spec.mcp or spec.name for spec in canvas_kinds.ops()]
    return "Ops (each needs intent): " + "; ".join(fragments + [_CORE_OP_TABLE]) + ". " + _PLACES


_EXAMPLE = ('Example: {"ops": [{"op": "frame", "id": "drivers", "title": "Churn drivers", "at": "c10r4", "w": 600, "h": 360, '
            '"intent": "group the drivers"}, {"op": "shape", "id": "price", "kind": "note", "text": "Price rise in March", '
            '"inside": "drivers", "intent": "the biggest driver"}, {"op": "arrow", "from": "price", "to": "c40r10", '
            '"label": "worsens", "intent": "what it drives"}]}')


def tool_definitions() -> List[Dict[str, Any]]:
    """The ``tools/list`` entries: name, description, inputSchema (no oneOf/anyOf)."""

    def schema(properties: Dict[str, Any], required: Tuple[str, ...] = ()) -> Dict[str, Any]:
        body: Dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            body["required"] = list(required)
        return body

    text = {"type": "string"}
    return [
        {"name": "canvas_look",
         "description": ("See the team canvas: elements in the region in full, the rest one line each, far groups as counts, plus "
                         "active claims, locks, the legend and comments that mention you. since: \"last\" adds what changed since you "
                         "last looked. image: true also renders a PNG with element ids marked (grid: true adds cell names). "
                         "Look after drawing anything meant for others. The listing ends with layout problems; canvas_check lists them all."),
         "inputSchema": schema({
             "region": dict(text, description='"c10r4:c40r22", "x0,y0,x1,y1", an element id, or "operator" (what the operator is viewing)'),
             "around": dict(text, description="an element or comment id; shows 200 units around it"),
             "since": dict(text, description='"last" or a version number'),
             "proposals": {"type": "boolean", "description": "every open proposal in full: its summary, base note, and what outdated it"},
             "image": {"type": "boolean"}, "grid": {"type": "boolean"},
             "exact": {"type": "boolean", "description": "ask an open whiteboard page for the engine's own picture"},
             "block": dict(text, description="a block (kanban, table, graph ...): its whole spec, one item per line"),
             "full": {"type": "boolean", "description": "also block part ids, top-level neighbours and details"},
             "view": dict(text, description="with image: draw 3D scenes from this view (iso, front or top)"),
         })},
        {"name": "canvas_check",
         "description": ("Check the canvas layout (or a region): overlapping marks, text on a labelled shape, labels that do not fit "
                         "their shape, marks half inside a frame, arrows through shapes, stray marks. Each problem has element ids and, "
                         "when there is an obvious one, a fix: an operation to pass to canvas_draw as it stands. Run it after drawing, "
                         "fix what it lists, run it again, then canvas_look with image: true for a last visual pass."),
         "inputSchema": schema({
             "region": dict(text, description='"c10r4:c40r22", "x0,y0,x1,y1" or an element id'),
             "around": dict(text, description="an element or comment id; checks 200 units around it"),
             "mine": {"type": "boolean", "description": "only problems that involve your own marks"},
         })},
        {"name": "canvas_draw",
         "description": ("Apply a batch of drawing operations in order (under about 40). Draw when a picture is clearer than text; claim "
                         "your region first; give every drawing a text label and every op an intent. Refused ops are listed with a "
                         "reason; the rest still apply unless atomic. Each applied entry lists the geometry of what it sized "
                         "(id, x, y, w, h, fit), so you never guess how big a shape grew. " + op_table() + " " + _EXAMPLE),
         "inputSchema": schema({"ops": {"type": "array", "items": {"type": "object"}, "description": "the operations"},
                                "atomic": {"type": "boolean", "description": "all or nothing"},
                                "base": dict(text, description='the canvas version you last read: a number, or "last" (your look cursor)')},
                               ("ops",))},
        {"name": "canvas_comment",
         "description": ("Pin a comment to an element, to a comment (a reply), or to a point. @name in the text (or mentions) wakes that "
                         "member with a request; use it to point instead of saying 'this'. Peers' comments are requests, not orders."),
         "inputSchema": schema({"at": dict(text, description="an element id or alias, a comment id (reply), a cell or \"x,y\""),
                                "text": text, "mentions": {"type": "array", "items": {"type": "string"}},
                                "reply_to": text, "intent": text}, ("at", "text"))},
        {"name": "canvas_claim",
         "description": ("Tell the team where you are about to draw: a dashed region with your label, for 5 minutes (at most 3 each). "
                         "Others avoid it. release: a claim id, or \"all\", releases instead."),
         "inputSchema": schema({"region": dict(text, description='"c10r4:c40r22" or "x0,y0,x1,y1"'), "label": text, "intent": text,
                                "release": dict(text, description='a K- id, or "all"')})},
        {"name": "canvas_legend",
         "description": ("Record a drawing convention so others (and the operator) can read it: symbol (an element id, or a few words "
                         "like \"red cross\") and its meaning. remove: a G- id removes one of yours."),
         "inputSchema": schema({"symbol": text, "meaning": text, "remove": text, "intent": text})},
        {"name": "canvas_changes",
         "description": "What changed on the canvas since you last looked (or since a version), one line per change, by whom and why.",
         "inputSchema": schema({"since": dict(text, description='"last" (default) or a version number')})},
        {"name": "canvas_focus",
         "description": ("Show the team and the operator where you work: a halo on a region (or an element) with your status and "
                         "intent, for ttl_s seconds (60 to 3600, default 600). It is presence, never authority. clear: true removes it."),
         "inputSchema": schema({"region": dict(text, description='"c10r4:c40r22", "x0,y0,x1,y1" or an element id'),
                                "intent": dict(text, description="one line: what you are doing"),
                                "status": dict(text, description="reading, drawing, waiting, blocked or idle (default drawing)"),
                                "ttl_s": {"type": "integer", "description": "seconds, 60 to 3600"}, "clear": {"type": "boolean"}})},
    ]


_TOOL_KEYS = {tool["name"]: set(tool["inputSchema"]["properties"]) for tool in tool_definitions()}


def _str(args: Dict[str, Any], key: str) -> Optional[str]:
    """A string argument; numbers are accepted where a string is expected (schemas avoid oneOf)."""
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise _ParamsError("{} must be a string".format(key))
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value) == int(value) else str(value)
    if not isinstance(value, str):
        raise _ParamsError("{} must be a string".format(key))
    return value


def _flag(args: Dict[str, Any], key: str) -> bool:
    value = args.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise _ParamsError("{} must be true or false".format(key))
    return value


def _place(value: Optional[str]) -> Any:
    """A region or point sent as a JSON list inside a string stays a list."""
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            return json.loads(value)
        except ValueError:
            raise _ParamsError("{} is not valid JSON".format(value))
    return value


def _intent(args: Dict[str, Any], fallback: str) -> str:
    given = _str(args, "intent")
    if given and given.strip():
        return given
    line = " ".join((fallback or "").split())
    return line[: C.MAX_INTENT_CHARS - 1] + "…" if len(line) > C.MAX_INTENT_CHARS else line


def _apply(session: McpSession, ops: List[Dict[str, Any]], atomic: bool = False, base: Any = None) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    author, team, doc = session.identity()
    result = C.check_applied(C.apply_ops(session.layout, team, ops, author, atomic=atomic, doc=doc, base=base))
    return result, C.apply_text(result), []


def _tool_look(session: McpSession, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    author, team, doc = session.identity()
    exact = _flag(args, "exact")
    image = _flag(args, "image") or exact
    result = C.look(session.layout, team, author.name, region=_place(_str(args, "region")), around=_str(args, "around"),
                    since=_str(args, "since"), image=image, grid=_flag(args, "grid"), exact=exact, advance=True, doc=doc,
                    block=_str(args, "block"), full=_flag(args, "full"), view=_str(args, "view"), proposals=_flag(args, "proposals"),
                    author=author)
    extra: List[Dict[str, Any]] = []
    if image and result.get("image"):
        try:
            data = store.read_bytes(result["image"])
        except (OSError, HerdrTeamError):
            data = None
        if data and len(data) <= MAX_INLINE_IMAGE_BYTES:
            extra.append({"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": "image/png"})
    return result, result["text"], extra


def _tool_check(session: McpSession, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    author, team, doc = session.identity()
    result = C.check(session.layout, team, author.name, region=_place(_str(args, "region")), around=_str(args, "around"),
                     mine=_flag(args, "mine"), doc=doc)
    return result, result["text"], []


def _tool_draw(session: McpSession, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    ops = args.get("ops")
    if not isinstance(ops, list):
        raise _ParamsError("ops must be an array of operation objects")
    base = _str(args, "base")
    return _apply(session, ops, _flag(args, "atomic"), C.parse_base(base) if base is not None else None)


def _tool_comment(session: McpSession, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    at, text = _str(args, "at"), _str(args, "text")
    if at is None or text is None:
        raise _ParamsError("canvas_comment needs at and text")
    op: Dict[str, Any] = {"op": "comment", "at": _place(at), "text": text, "intent": _intent(args, text)}
    mentions = args.get("mentions")
    if mentions is not None:
        if not isinstance(mentions, list) or not all(isinstance(m, str) for m in mentions):
            raise _ParamsError("mentions must be an array of names")
        op["mentions"] = mentions
    if _str(args, "reply_to"):
        op["reply_to"] = _str(args, "reply_to")
    return _apply(session, [op])


def _tool_claim(session: McpSession, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    release = _str(args, "release")
    if release:
        op: Dict[str, Any] = {"op": "release", "intent": _intent(args, "release {}".format(release))}
        if release != "all":
            op["id"] = release
        return _apply(session, [op])
    label = _str(args, "label") or ""
    op = {"op": "claim", "region": _place(_str(args, "region")), "intent": _intent(args, label or "claim a region")}
    if label:
        op["label"] = label
    return _apply(session, [op])


def _tool_legend(session: McpSession, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    remove = _str(args, "remove")
    if remove:
        return _apply(session, [{"op": "legend", "remove": remove, "intent": _intent(args, "remove legend {}".format(remove))}])
    symbol, meaning = _str(args, "symbol"), _str(args, "meaning")
    op = {"op": "legend", "symbol": symbol, "meaning": meaning, "intent": _intent(args, "{} = {}".format(symbol or "", meaning or ""))}
    return _apply(session, [op])


def _tool_changes(session: McpSession, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    author, team, doc = session.identity()
    result = C.read_changes(session.layout, team, author.name, since=_str(args, "since") or "last", advance=True, doc=doc)
    return result, result["text"], []


def _tool_focus(session: McpSession, args: Dict[str, Any]) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    from herdr_team import canvas_presence as P

    author, team, doc = session.identity()
    from herdr_team import features as _features

    _features.require_on(session.layout.session, team, doc)
    if _flag(args, "clear"):
        P.clear_member(team, author.name)
        return {"ok": True, "cleared": True}, "presence cleared", []
    status = _str(args, "status") or "drawing"
    if status not in P.STATUSES:
        raise _ParamsError("status is one of: {}".format(", ".join(P.STATUSES)))
    ttl = args.get("ttl_s")
    if ttl is None:
        ttl = P.FOCUS_TTL_S
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not P.FOCUS_TTL_RANGE[0] <= ttl <= P.FOCUS_TTL_RANGE[1]:
        raise _ParamsError("ttl_s is a whole number of seconds, {} to {}".format(*P.FOCUS_TTL_RANGE))
    region = None
    ids: List[str] = []
    where = _str(args, "region")
    if where:
        scene = C.load_scene(team)
        region = C.parse_region(_place(where), scene, author.name)
        try:
            ids = [C._lookup_in(C._State.from_scene(scene, team.name), where, author.name, "region")["id"]]
        except HerdrTeamError:
            ids = []
    doc_out = P.write_member(team, author, status=status, region=region, ids=ids, intent=_str(args, "intent") or "", ttl_s=ttl, via="focus")
    return {"ok": True, "presence": doc_out}, "focus: {} {} for {}s".format(status, C.region_cells(region) if region else "(no region)", ttl), []


_TOOLS: Dict[str, Callable[[McpSession, Dict[str, Any]], Tuple[Dict[str, Any], str, List[Dict[str, Any]]]]] = {
    "canvas_look": _tool_look, "canvas_check": _tool_check, "canvas_draw": _tool_draw, "canvas_comment": _tool_comment,
    "canvas_claim": _tool_claim, "canvas_legend": _tool_legend, "canvas_changes": _tool_changes, "canvas_focus": _tool_focus,
}


# --------------------------------------------------------------------------
# JSON-RPC


def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _call(session: McpSession, params: Any) -> Dict[str, Any]:
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        raise _ParamsError("tools/call needs name and arguments")
    name = params["name"]
    tool = _TOOLS.get(name)
    if tool is None:
        raise _ParamsError("unknown tool {}; tools: {}".format(name, ", ".join(TOOL_NAMES)))
    args = params.get("arguments") if params.get("arguments") is not None else {}
    if not isinstance(args, dict):
        raise _ParamsError("arguments must be an object")
    unknown = sorted(set(args) - _TOOL_KEYS[name])
    if unknown:
        raise _ParamsError("{} does not take {}".format(name, ", ".join(unknown)))
    try:
        payload, text, extra = tool(session, args)
    except HerdrTeamError as err:
        return {"content": [{"type": "text", "text": "{}: {}".format(err.code, err.message)}], "structuredContent": err.to_json(), "isError": True}
    except _ParamsError:
        raise
    except Exception as err:  # one bad call must never take the server (and the harness's tools) down
        sys.stderr.write("synapse-canvas: {} failed: {}\n{}".format(name, err, traceback.format_exc()))
        sys.stderr.flush()
        return {"content": [{"type": "text", "text": "internal: {}".format(err)}],
                "structuredContent": {"code": "internal", "message": str(err)}, "isError": True}
    return {"content": [{"type": "text", "text": text}] + extra, "structuredContent": payload, "isError": False}


def handle(message: Dict[str, Any], session: McpSession) -> Optional[Dict[str, Any]]:
    """Answer one JSON-RPC message; None for notifications."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
    method = message.get("method")
    has_id = "id" in message
    msg_id = message.get("id")
    if not isinstance(method, str):
        if has_id and ("result" in message or "error" in message):
            return None  # a client's response; this server never asks anything
        return _error(msg_id, INVALID_REQUEST, "method is missing")
    params = message.get("params")
    if not has_id:
        if method == "notifications/initialized":
            session.initialized = True
        return None
    try:
        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            session.protocol = requested if requested in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
            result: Dict[str, Any] = {
                "protocolVersion": session.protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_INFO_NAME, "version": VERSION},
                "instructions": INSTRUCTIONS,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_definitions()}
        elif method == "tools/call":
            result = _call(session, params)
        else:
            return _error(msg_id, METHOD_NOT_FOUND, "method not found: {}".format(method))
    except _ParamsError as err:
        return _error(msg_id, INVALID_PARAMS, str(err))
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _write(stdout: IO[str], obj: Any) -> None:
    stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()


def serve(layout: Any, env: Any, stdin: Optional[IO[str]] = None, stdout: Optional[IO[str]] = None, api: Any = None,
          team_name: Optional[str] = None) -> int:
    """Read requests until EOF and answer each on stdout; the exit code."""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    if api is None:
        from herdr_team.api import HerdrApi

        api = HerdrApi(layout.socket, env=env)
    session = McpSession(layout, env, api, team_name)
    while True:
        try:
            line = source.readline()
        except (OSError, ValueError, KeyboardInterrupt):
            return 0
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _write(sink, _error(None, PARSE_ERROR, "parse error"))
            continue
        try:
            if isinstance(message, list):
                replies = [reply for reply in (handle(item, session) for item in message) if reply is not None]
                if not message:
                    _write(sink, _error(None, INVALID_REQUEST, "empty batch"))
                elif replies:
                    _write(sink, replies)
                continue
            reply = handle(message, session)
            if reply is not None:
                _write(sink, reply)
        except BrokenPipeError:
            return 0
        except Exception as err:  # never leave a request unanswered
            sys.stderr.write("synapse-canvas: {}\n{}".format(err, traceback.format_exc()))
            sys.stderr.flush()
            msg_id = message.get("id") if isinstance(message, dict) else None
            if msg_id is not None:
                _write(sink, _error(msg_id, INTERNAL_ERROR, "internal error"))
