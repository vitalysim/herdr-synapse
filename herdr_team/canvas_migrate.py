"""Boards drawn before canvas v2: what the new renderer draws differently, and the one fix (canvas v2 phase 6, 2).

A board from 0.21.x (or an early 0.22 build) opens on v2 by replay: ``canvas._load_state`` folds the same
``events.jsonl``, every stored element is a v2 element, and nothing is rewritten (the log keeps ``SCHEMA = 1``).
Nothing Excalidraw-only ever reached the server, so no element is lost; what changes is the look:

* labels measured by today's metrics: an element stored before 0.22 has no ``fit`` record, keeps its stored size,
  and a label that no longer fits is drawn clamped (``label_overflow``) until it is refitted;
* marks in the old sketch style (``rough`` > 0, or the hand font ``font: hand``) are drawn clean. 0.21 drew every
  mark that way by default, so on a board from 0.21 this is usually every mark on it, not a few sketchy ones.

``report(scene)`` counts that, pure over a folded scene. Only the operator is told (a banner on her writable page, a
line in her ``look``, ``canvas migrate``), and only she fixes it: the lead-only core op ``migrate`` with ``action:
apply`` refits every pre-0.22 label and draws the sketch marks clean in one batch, which undo takes back whole;
``action: dismiss`` only records her answer. Either writes the ``migration`` setting, so the notice goes.
"""
from __future__ import annotations

import os
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from herdr_team import canvas as C
from herdr_team import canvas_check as _check
from herdr_team import canvas_collab as _collab
from herdr_team import canvas_text as _ctext
from herdr_team import canvas_theme as _theme
from herdr_team.errors import HerdrTeamError

#: The setting ``migrate`` writes: ``{"action": "apply" | "dismiss", "seq": n, "by": name}``.
SETTING = "migration"
ACTIONS = ("apply", "dismiss")
#: The most ids a report lists (and ``apply`` refits at a time); ``pre_022`` stays exact, so a board past the cap is
#: fixed by running ``apply`` again (``op_migrate`` records the answer only once nothing is left).
MAX_IDS = 2000
#: ``apply`` refits in steps of at most this many ids (``canvas.MAX_REFIT``), each under its own routing budget.
STEP = C.MAX_REFIT
#: Reports kept in memory, by team and version (``look`` and ``/display`` pay once per version).
CACHE_SIZE = 16
_CACHE: Dict[Tuple[str, int, str], Dict[str, Any]] = {}


# --------------------------------------------------------------------------
# the report (2.3)


def _style(el: Mapping[str, Any]) -> Mapping[str, Any]:
    return el.get("style") if isinstance(el.get("style"), dict) else {}


def is_sketch(el: Mapping[str, Any]) -> bool:
    """A mark in the old sketch style: ``rough`` above 0, or the hand font. v2 draws both clean (2.2).

    0.21's own defaults were ``rough: 1`` and the hand font, so nearly every mark of a 0.21 board is one of these; the
    words the operator reads say "the old sketch style", never "hand-drawn", because she did not draw them by hand.
    """
    style = _style(el)
    rough = style.get("rough")
    return (C._is_number(rough) and float(rough) > 0) or style.get("font") == "hand"


def is_pre_022(el: Mapping[str, Any]) -> bool:
    """An element whose kind sizes its label (``refit`` would) but that was never fitted: it was stored before 0.22."""
    return not isinstance(el.get("fit"), dict) and C._refittable(dict(el))


def _colours(el: Mapping[str, Any]) -> Tuple[bool, bool]:
    """``(legacy, unknown)``: an element without a tone whose stroke or fill a legacy table names (drawn in that tone),
    or whose stroke or fill is a hex no table knows (drawn as given)."""
    style = _style(el)
    if style.get("tone") in _theme.TONES:
        return False, False
    legacy = unknown = False
    for role in ("stroke", "fill"):
        value = style.get(role)
        if not isinstance(value, str) or not value.strip() or value.strip().lower() in ("transparent", "none"):
            continue
        if _theme.tone_of(value):
            legacy = True
        elif value.strip().startswith("#"):
            unknown = True
    return legacy, unknown


def _vega_lite(el: Mapping[str, Any]) -> bool:
    return el.get("type") == "chart" and (el.get("spec_asset") is not None or el.get("spec") is not None) and \
        el.get("echarts_asset") is None and el.get("echarts") is None


def state_of(scene: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The operator's recorded answer (the ``migration`` setting), or None."""
    settings = scene.get("settings") if isinstance(scene.get("settings"), dict) else {}
    value = settings.get(SETTING)
    if not isinstance(value, dict) or value.get("action") not in ACTIONS:
        return None
    return {"action": value["action"], "seq": value.get("seq"), "by": value.get("by")}


def report_of(elements: Iterable[Mapping[str, Any]], state: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The report over live elements (measured under whatever corrections are in force)."""
    live = [el for el in elements if isinstance(el, dict) and isinstance(el.get("id"), str) and not el.get("deleted")]
    live.sort(key=lambda el: (C._id_number(el.get("id")), str(el.get("id"))))
    pre = [el for el in live if is_pre_022(el)]
    solid = _check.solid_kinds()
    overflow = [el["id"] for el in live if el.get("type") in solid and _check.label_room(dict(el)) is not None]
    sketch = [el["id"] for el in live if el.get("type") != "comment" and is_sketch(el)]
    legacy = unknown = vega = 0
    for el in live:
        found = _colours(el)
        legacy += int(found[0])
        unknown += int(found[1])
        vega += int(_vega_lite(el))
    answered = dict(state) if isinstance(state, dict) else None
    return {
        "pending": answered is None and bool(pre or sketch),
        "state": answered,
        "pre_022": len(pre),
        "refit": [el["id"] for el in pre][:MAX_IDS],
        "overflow": overflow[:MAX_IDS],
        "sketch": sketch[:MAX_IDS],
        "legacy_colour": legacy,
        "unknown_hex": unknown,
        "vega_lite": vega,
    }


def report(scene: Mapping[str, Any], *, corrections: Optional[Tuple[Dict[str, float], str]] = None) -> Dict[str, Any]:
    """What canvas v2 draws differently on this folded scene (2.3), pure::

        {"pending", "state", "pre_022", "refit": [ids], "overflow": [ids], "sketch": [ids],
         "legacy_colour", "unknown_hex", "vega_lite"}

    ``pending`` is true while the operator has not answered (``state`` is None) and the board holds a pre-0.22 label or a
    mark in the old sketch style; an empty board, and a board drawn in 0.22, are never pending. ``corrections`` is the team's browser
    table and token (``canvas.corrections``) the overflow is measured under.
    """
    table, token = corrections if corrections is not None else ({}, "")
    with _ctext.corrected(table, token):
        return report_of(scene.get("elements") or [], state_of(scene))


def report_for(team: Any) -> Dict[str, Any]:
    """``report`` of the team's canvas now, cached by team, version and corrections (a copy: callers may change it)."""
    try:
        corrections = C.corrections(team)
    except (OSError, HerdrTeamError, ValueError):
        corrections = ({}, "")
    version = C.current_version(team)
    try:
        stat = os.stat(C._file(team, C.EVENTS_FILE))
        log = "{}:{}".format(stat.st_size, stat.st_mtime_ns)
    except OSError:
        log = ""
    # The log's size and time too: a log replaced under the same version (a restored backup, a test) is a new board.
    key = (os.fspath(team.root), int(version), corrections[1] + "|" + log)
    found = _CACHE.get(key)
    if found is None:
        found = report(C.load_scene(team), corrections=corrections)
        _CACHE[key] = found
        while len(_CACHE) > CACHE_SIZE:
            del _CACHE[next(iter(_CACHE))]
    return {k: (list(v) if isinstance(v, list) else (dict(v) if isinstance(v, dict) else v)) for k, v in found.items()}


def summary(found: Mapping[str, Any]) -> Dict[str, Any]:
    """The report without its id lists (what ``/display`` and ``look --json`` carry): the counts, and ``ids_total``, how
    many distinct marks the fix would touch."""
    ids = set(found.get("refit") or []) | set(found.get("sketch") or [])
    return {"pending": bool(found.get("pending")), "state": found.get("state"), "pre_022": int(found.get("pre_022") or 0),
            "refit": len(found.get("refit") or []), "overflow": len(found.get("overflow") or []), "sketch": len(found.get("sketch") or []),
            "legacy_colour": int(found.get("legacy_colour") or 0), "unknown_hex": int(found.get("unknown_hex") or 0),
            "vega_lite": int(found.get("vega_lite") or 0), "ids_total": len(ids)}


def _count(n: int, one: str, many: str) -> str:
    return "{} {}".format(n, one if n == 1 else many)


def notice_parts(found: Mapping[str, Any]) -> List[str]:
    """``["14 labels need resizing", "3 marks in the old sketch style now drawn clean"]`` (the counts that are not
    zero). 0.21 drew every mark sketchy by default, so that second count is usually the whole board (``is_sketch``).

    Every phrase agrees with its count, and with the page's banner word for word where the two say the same thing
    (``MigrationBanner.noticeParts``): the operator reads both, side by side, so "1 label need resizing" is a bug
    (QA phase 6, 3.4)."""
    refit = found.get("refit")
    refit_n = len(refit) if isinstance(refit, list) else int(refit or 0)
    overflow = found.get("overflow")
    overflow_n = len(overflow) if isinstance(overflow, list) else int(overflow or 0)
    sketch = found.get("sketch")
    sketch_n = len(sketch) if isinstance(sketch, list) else int(sketch or 0)
    parts: List[str] = []
    if overflow_n:
        parts.append("{} resizing".format(_count(overflow_n, "label needs", "labels need")))
    elif refit_n:
        parts.append("{} to size again".format(_count(refit_n, "label", "labels")))
    if sketch_n:
        parts.append("{} now drawn clean".format(_count(sketch_n, "mark in the old sketch style",
                                                          "marks in the old sketch style")))
    return parts


def look_line(found: Mapping[str, Any]) -> Optional[str]:
    """The operator's one ``look`` line while the report is pending (2.4), else None."""
    if not found.get("pending"):
        return None
    return " · ".join(["migration: drawn before canvas v2"] + notice_parts(found) + ["{} canvas migrate --apply (or --dismiss)".format(C.CLI)])


def text(found: Mapping[str, Any]) -> str:
    """``canvas migrate`` without a flag: the report in words."""
    state = found.get("state")
    lines: List[str] = []
    if found.get("pending"):
        lines.append(look_line(found) or "")
    elif isinstance(state, dict):
        lines.append("migration: {} by {} at v{}".format("applied" if state.get("action") == "apply" else "dismissed",
                                                       C._who(state.get("by") or C.HUMAN, None), state.get("seq")))
    else:
        lines.append("migration: nothing to do (no label from before canvas v2 and no mark in the old sketch style)")
    refit, overflow, sketch = (list(found.get(key) or []) for key in ("refit", "overflow", "sketch"))
    lines.append("labels from before canvas v2: {} ({} refit on apply){}".format(found.get("pre_022") or 0, len(refit),
                                                                                ": " + C._ids_text(refit) if refit else ""))
    lines.append("labels that do not fit now: {}{}".format(len(overflow), ": " + C._ids_text(overflow) if overflow else ""))
    lines.append("marks in the old sketch style, 0.21's default look (drawn clean on v2): {}{}".format(
        len(sketch), ": " + C._ids_text(sketch) if sketch else ""))
    lines.append("colours drawn by their tone: {} · colours drawn as given: {} · Vega-Lite charts (still drawn): {}".format(
        found.get("legacy_colour") or 0, found.get("unknown_hex") or 0, found.get("vega_lite") or 0))
    lines.append("not on v2: rough strokes, the hand font and Excalidraw's own browser state (library, scroll, zen and grid "
                 "mode); the classic canvas (?engine=v1) still draws them")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the op (2.4)


def _chunks(ids: Sequence[str], size: int) -> List[List[str]]:
    return [list(ids[i:i + size]) for i in range(0, len(ids), size)]


#: The most marks the fix moves apart after refitting (the check's own ``move`` fix, one pass).
MAX_SEPARATE = 50


def _overlap_pairs(elements: Iterable[Mapping[str, Any]]) -> Set[FrozenSet[str]]:
    return {frozenset(p["ids"]) for p in _check.problems([dict(el) for el in elements]) if p.get("code") == "overlap"}


def _separate(ctx: Any, overlapping: Set[FrozenSet[str]]) -> None:
    """Marks that overlap only because the refit grew them (a host that grew around a longer label reaching a neighbour
    the push-out took for its own) move to the free spot the check names, as ``check``'s fix would; overlaps the board
    already had are left for the operator.

    Nothing else is repaired, on purpose. The fix's contract is the hard one: no ``label_overflow`` left on a label it
    can grow (2.6). What growing a label uncovers elsewhere stays the operator's to decide, and ``canvas check`` names
    it with its own fix: a mark that now reaches into a frame it is not a member of (the check would move it into that
    frame, which is not the migration's call), and an arrow whose line grew short under a label that no longer sits on
    it (``label_astray``). An arrow drawn before 0.22 has no stored ``label_at``, so the renderer placed its label by
    today's rules at draw time and ``check`` could not see it; the fix stores that same spot, which is what makes the
    check honest about it rather than what puts it there.
    """
    moved = 0
    for problem in _check.problems(list(ctx.live())):
        if moved >= MAX_SEPARATE:
            break
        fix = problem.get("fix") if isinstance(problem.get("fix"), dict) else None
        if problem.get("code") != "overlap" or frozenset(problem["ids"]) in overlapping or fix is None or fix.get("op") != "move":
            continue
        target = ctx.el(str(fix.get("id") or ""))
        if target is None:
            continue
        try:
            C._op_move(ctx, dict(fix, intent=ctx.intent))
        except HerdrTeamError:
            continue
        moved += 1
        ctx.warn("moved_to_fit", "{} moved to a free spot: the labels around it grew to fit".format(target["id"]), [target["id"]])


def op_migrate(ctx: Any, op: Dict[str, Any]) -> None:
    """``{"op": "migrate", "action": "apply" | "dismiss"}``, the operator in person only. ``apply`` sets ``rough: 0`` and
    ``font: normal`` on every mark in the old sketch style, refits every pre-0.22 label (grow only, pushing neighbours, as ``refit``
    does; after the clean-up, so a label is measured in the face it is drawn in), and records the answer; ``dismiss``
    only records it. An apply with nothing to do is refused."""
    if not _collab.is_lead(ctx.author):
        raise _collab._lead_only("migrating the board to canvas v2")
    if op.get("if_version") is not None:
        raise C._invalid("if_version", "migrate acts on the whole board; it takes no if_version")
    action = op.get("action")
    if action not in ACTIONS:
        raise C._invalid("action", "migrate takes action: apply (resize old labels, draw the old sketch style clean) or dismiss")
    state = ctx.state
    found = report_of(list(ctx.live()), None)
    moved_to_fit = 0
    grew = 0
    refitted: List[str] = []
    clean: List[str] = []
    if action == "apply":
        if not found["refit"] and not found["sketch"]:
            raise C._invalid("action",
                             "nothing to migrate: the board has no label from before canvas v2 and no mark in the old sketch style")
        before = {eid: C.bounds(el) for eid, el in state.elements.items()}
        overlapping = _overlap_pairs(state.elements.values())
        # Clean first: a label is then measured in the face it is drawn in (Inter, not the hand font).
        for eid in found["sketch"]:
            el = ctx.el(eid)
            if el is None:
                continue
            style = dict(_style(el))
            if C._is_number(style.get("rough")) and float(style["rough"]) > 0:
                style["rough"] = 0
            if style.get("font") == "hand":
                style["font"] = "normal"
            ctx.update(el, style=style)
            clean.append(eid)
        for chunk in _chunks(found["refit"], STEP):
            live = [eid for eid in chunk if ctx.el(eid) is not None]
            if not live:
                continue
            ctx.pushes = 0
            with C._work_budget.running(C._work_budget.Budget(work=C.OP_ROUTE_WORK, seconds=C.OP_ROUTE_SECONDS)):
                C._op_refit(ctx, {"op": "refit", "ids": live})
        refitted = [eid for eid in found["refit"] if isinstance((ctx.el(eid) or {}).get("fit"), dict)]
        _separate(ctx, overlapping)
        targets = set(found["refit"])
        for eid, el in ctx.pending.items():
            if el is None or eid not in before:
                continue
            now_box = C.bounds(el)
            old = before[eid]
            if eid in targets and (now_box[2] - now_box[0], now_box[3] - now_box[1]) != (old[2] - old[0], old[3] - old[1]):
                grew += 1
            elif eid not in targets and (now_box[0], now_box[1]) != (old[0], old[1]):
                moved_to_fit += 1
    # A board with more than MAX_IDS marks to fix is done in as many applies as it takes: while the cap held work back,
    # the answer is not recorded, so the notice (and this op) stays instead of calling a half-fixed board migrated.
    # A board inside the cap settles in one apply and records the answer with it, as it always did.
    left = 0
    if action == "apply" and (len(found["refit"]) >= MAX_IDS or len(found["sketch"]) >= MAX_IDS):
        rest = report_of(list(ctx.live()), None)
        left = int(rest["pre_022"]) + len(rest["sketch"])
    if not left:
        value = {"action": action, "seq": ctx.seq, "by": ctx.author.name}
        ctx.other("setting", "update" if SETTING in state.settings else "add", SETTING, value)
    info = {"action": action, "refitted": len(refitted), "grew": grew, "moved": moved_to_fit, "clean": len(clean),
            "batch": ctx.batch_id, "left": left}
    ctx.extra["migrate"] = dict(info)
    ctx.entry_extra["migrate"] = info


def result_text(info: Mapping[str, Any]) -> str:
    """``migrated: 14 refitted (6 grew, 2 moved to fit), 3 drawn clean (rough, hand font); undo B-12 to take it back``.

    A board past the ``MAX_IDS`` cap says how many marks are left and that the notice stays until they are done.
    """
    if info.get("action") == "dismiss":
        return "migration notice dismissed; nothing on the board changed (canvas migrate --apply still fixes it)"
    left = int(info.get("left") or 0)
    return "migrated: {} refitted ({} grew, {} moved to fit), {} drawn clean (rough, hand font); undo {} to take it back{}".format(
        info.get("refitted", 0), info.get("grew", 0), info.get("moved", 0), info.get("clean", 0), info.get("batch") or "the batch",
        "" if not left else "; {} still to go on this board, so the notice stays: run it again".format(left))


def summarize(event: Mapping[str, Any]) -> str:
    """One change line for a ``migrate`` event (``canvas changes``, History)."""
    info = event.get("migrate") if isinstance(event.get("migrate"), dict) else {}
    if info.get("action") == "dismiss":
        return "dismissed the canvas v2 migration notice"
    return "migrated the board to canvas v2 ({} refitted, {} drawn clean)".format(info.get("refitted", 0), info.get("clean", 0))
