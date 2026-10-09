"""``canvas import``: the inverse of ``canvas export``, and the only way a board outlives the team that drew it.

A team's drawing lives in session state (``<team>/whiteboard/``). ``dissolve`` renames that whole directory into the
session archive, so every mark survives -- and nothing ever read it back. ``canvas export`` had no inverse, so a board
was recoverable only by hand-editing an event log. This module is that inverse, and it is written against three measured
facts about how the canvas folds:

* **Source element ids replay verbatim into an empty canvas, and that is the only thing that keeps the bindings.**
  An arrow's ``from``/``to``, a comment's ``on`` and a child's ``frame`` are ids; renumbering them would need a rewrite
  of every reference in every kind's own fields. So the import demands an empty canvas (refusal g) and keeps the ids.
  ``_State.apply_change`` bumps the counters per id as it folds, so the ids that follow collide with nothing.
* **A foreign ``updated_seq`` poisons an element permanently.** An element carrying ``updated_seq: 400`` onto a canvas
  at v1 is refused ``canvas_stale`` for every ``if_version`` the operator can read from ``look``. The import therefore
  rewrites ``created_seq``, ``updated_seq`` and the timestamps to this canvas's own.
* **One op, one batch, one undo.** ``_apply_ops`` mints one ``batch_id`` per call and ``fold`` records the batch, so a
  whole import is one entry in ``canvas undo``. Per-element ops would blow ``MAX_BATCH_OPS`` at 100 marks and lose that.

The prototype is ``canvas_collab.op_restore`` (one undoable batch, alias-collision resolution, unbind, reroute), with
three branches inverted: an import does not delete the live elements absent from the payload, does not preserve the
stored author when it re-attributes, and does not skip comments -- a comment is the only record of why a diagram looks
as it does, and dropping it would orphan ``was_on`` undo recovery.

What never travels is in ``.local/prd/team-inheritance-spec.md`` §1.4, and each exclusion has a measured reason: a
claim whose author is gone cannot be renewed or cleared, a foreign lane is policed by ``_rule_foreign_lane``, a
fabricated ``B-n`` makes ``undo`` refuse ``element_unknown``, a checkpoint record without its snapshot file guarantees
``element_unknown``, and a recorded migration answer would suppress a notice this board may genuinely need.

Authority: the import is the operator's in person. An agent may not re-attribute a whole board to itself behind their
back, and ``--keep-authors`` is theirs alone because retaining names changes ownership semantics: members' edits
can become peer proposals. With either choice the operator can undo the import batch; provenance keeps the original
author even when the marks are re-attributed to the current operator.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team import canvas as C
from herdr_team import canvas_collab as _collab
from herdr_team import canvas_kinds as _kinds
from herdr_team import store
from herdr_team.errors import HerdrTeamError
from herdr_team.paths import TeamPaths, check_not_symlink

CLI = C.CLI

#: The payload format of the mirror's ``canvas.json``, separate from the canvas ``SCHEMA`` the scene itself carries.
PAYLOAD = 1
#: The reserved first key of a mirrored payload: the generated marker, ignored by every reader (``workdir.MARKER_JSON``).
MARKER_KEY = "//"
#: One document the import reads. Larger than ``canvas export`` writes for a full board (``MAX_EXPORT_BYTES``), because
#: a payload also carries the legend and the counters.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
#: How far back the idempotence check reads this canvas's log for the same document (it stops at a ``clear`` anyway).
MAX_IMPORT_SCAN = 5000
#: An asset the import copies, and the whole set it copies, mirroring ``workdir``'s own budget for ``canvas-assets/``.
MAX_ASSET_BYTES = 1024 * 1024
MAX_ASSETS_BYTES = 8 * 1024 * 1024
#: A data file under ``artifacts/`` that the inheritance flow brings along, and the whole set. Larger than an asset
#: because a dataset is not a picture, and the folder is gitignored, so these bytes do not reach the checkout.
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACTS_BYTES = 32 * 1024 * 1024

#: A dissolved team's archive directory: ``<session>/_archive/<team>-<stamp>/``.
STAMP_RE = re.compile(r"\A\d{8}T\d{6}Z\Z")
#: ``cmd_misc`` dumps pruned board segments under ``<team>-prune-<stamp>``; those are not a dissolved team.
PRUNE_INFIX = "-prune-"
#: Asset names as they appear inside a scene, wherever a kind stores them (``canvas_collab.keep_checkpoint_assets``
#: walks a checkpoint the same way): the name is a content hash, so finding it in the text is exact.
_ASSET_IN_SCENE_RE = re.compile(r"[0-9a-f]{32}\.(?:png|jpg|svg|html|vl\.json|json|glb)")
#: What an element carries from the board it came from, so recall, provenance and a re-import have something true to read.
PROVENANCE_KEY = "imported"
#: What the event and the applied entry carry (``event["import"]``), beside the element field above.
OP_KEY = "import"
#: Fields the import rewrites to this canvas's own (every other field of every kind is carried as it was stored).
_RESTAMPED = ("created_seq", "updated_seq", "created_at", "updated_at", "batch")


# --------------------------------------------------------------------------
# the payload (seam S2): what a mirrored canvas.json carries, and the assets it names


def payload(scene: Mapping[str, Any]) -> Dict[str, Any]:
    """The import payload of a folded scene: the elements, the legend, the counters and the source's provenance.

    A pure function of the scene, so the mirror can write it once per canvas version and leave the file alone when the
    canvas did not move. The caller adds the marker as the first key and fills ``assets_omitted`` / ``assets_conflict``
    for whatever it could not copy; both lists exist here so a reader never has to test for them.
    """
    elements = [dict(el) for el in scene.get("elements") or [] if isinstance(el, dict) and isinstance(el.get("id"), str)]
    counters = scene.get("counters") if isinstance(scene.get("counters"), dict) else {}
    return {
        "payload": PAYLOAD,
        "v": int(scene.get("v") or C.SCHEMA),
        "team": scene.get("team"),
        "scene_version": int(scene.get("version") or 0),
        "updated_at": scene.get("updated_at") if isinstance(scene.get("updated_at"), str) else None,
        "counters": {str(key): int(value) for key, value in sorted(counters.items()) if C._is_number(value)},
        "legend": [dict(entry) for entry in scene.get("legend") or [] if isinstance(entry, dict)],
        "elements": elements,
        "assets": asset_names({"elements": elements}),
        "assets_omitted": [],
        "assets_conflict": [],
    }


def asset_names(document: Mapping[str, Any]) -> List[str]:
    """The content-hash asset names the document's elements reference, sorted (so the carried set is deterministic).

    Found in the serialised elements rather than per kind: an asset name is a 32-hex content hash with a known
    extension, every kind that has one stores it as a plain string, and a walk that asks each kind would go stale the
    next time a kind gains a picture.
    """
    elements = document.get("elements")
    if not isinstance(elements, list):
        return []
    text = json.dumps(elements, ensure_ascii=False, separators=(",", ":"), default=str)
    return sorted(set(_ASSET_IN_SCENE_RE.findall(text)))


def artifact_refs(el: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """``(field, relative path)`` for every file under ``artifacts/`` this element reads: a viz's dataset, a chart's
    source file, and each 3D object's model."""
    out: List[Tuple[str, str]] = []
    data_path = el.get("data_path")
    if isinstance(data_path, str) and data_path.strip():
        out.append(("data_path", data_path.strip()))
    source = el.get("source") if isinstance(el.get("source"), dict) else {}
    if isinstance(source.get("data"), str) and source["data"].strip():
        out.append(("source.data", source["data"].strip()))
    for index, obj in enumerate(el.get("objects") or []):
        if isinstance(obj, dict) and isinstance(obj.get("src"), str) and obj["src"].strip():
            out.append(("objects[{}].src".format(index), obj["src"].strip()))
    return out


# --------------------------------------------------------------------------
# the document: what --from accepts, and what it refuses


@dataclass(frozen=True)
class Document:
    """One importable document: the file it was read from, the scene inside it, and where its assets are."""

    path: Path
    #: ``mirror`` (the project folder's ``canvas.json``), ``export`` (the ``canvas export --format json`` wrapper),
    #: or ``scene`` (a bare ``scene.json``, live or archived).
    form: str
    scene: Dict[str, Any]
    assets_dir: Optional[Path]
    digest: str
    bytes: int
    #: Where it came from, for the result text and the per-element provenance.
    source: Dict[str, Any] = field(default_factory=dict)
    #: The source team's ``artifacts/`` folder, when it can be found: a dataset or a model is read from the *target*
    #: team's own folder, so a board that reads one cannot be imported into a differently named team until the file is
    #: brought along. Refusal (e) names this, and the inheritance flow copies from it.
    artifacts_source: Optional[Path] = None


def _refuse_from(path: Any, message: str, **details: Any) -> HerdrTeamError:
    return C._invalid("from", message, path=os.fspath(path) if isinstance(path, (str, Path)) else path, **details)


def _not_a_scene(path: Any) -> HerdrTeamError:
    return _refuse_from(path, "{} is not a canvas scene: a scene is the JSON that `{} canvas export --format json` "
                              "writes (an object with v, team, version and elements)".format(path, CLI))


def _damaged(path: Any, why: str) -> HerdrTeamError:
    """A file that *is* the right kind of file and cannot be read: told apart from the wrong kind of file.

    The repair is different and the operator cannot guess which they are looking at. "You passed the wrong file" sends
    somebody looking for the right one; "the right file is damaged" sends them to the other copy -- which matters most
    when the source is a dissolved team's archive they can no longer re-export.
    """
    return _refuse_from(path, "{} is damaged ({}): export the board again with `{} canvas export --format json`, or "
                              "import another copy of it (a cleared canvas keeps one under "
                              "whiteboard/archive/<stamp>/)".format(path, why, CLI), damaged=why)


def _document_in(directory: Path) -> Tuple[Path, Optional[Path]]:
    """The document inside a directory, and its assets folder: a knowledge path, a dissolved team, or a cleared canvas."""
    for name, assets in (("canvas.json", directory / "canvas-assets"),
                         (os.path.join("whiteboard", "scene.json"), directory / "whiteboard" / C.ASSETS_DIR),
                         ("scene.json", directory / C.ASSETS_DIR)):
        candidate = directory / name
        if candidate.is_file():
            return candidate, (assets if assets.is_dir() else None)
    raise _refuse_from(directory, "{} holds no canvas to import: --from takes a team's folder in the project directory "
                                  "(its canvas.json), a dissolved team's directory (its whiteboard/scene.json), or a "
                                  "cleared canvas under whiteboard/archive/<stamp>/ (its scene.json)".format(directory))


def _sibling_assets(path: Path) -> Optional[Path]:
    for name in ("canvas-assets", C.ASSETS_DIR):
        found = path.parent / name
        if found.is_dir():
            return found
    return None


def _artifacts_source(path: Path) -> Optional[Path]:
    """The source team's ``artifacts/`` folder, found two ways and never guessed.

    A mirrored ``canvas.json`` sits in the team's own folder in the project directory, so ``artifacts/`` is its
    sibling. A dissolved team's directory carries its ``team.json``, which names the project directory the team used,
    so the folder is where the mirror put it. Neither read writes anything.
    """
    for candidate in (path.parent / "artifacts", path.parent.parent / "artifacts"):
        if candidate.is_dir():
            return candidate
    team_json = path.parent / "team.json" if (path.parent / "team.json").is_file() else path.parent.parent / "team.json"
    if not team_json.is_file():
        return None
    try:
        from herdr_team import workdir as _workdir

        doc = store.read_json(team_json, default={}) or {}
        project = _workdir.project_dir_of(doc)
        name = doc.get("team")
        if not project or not isinstance(name, str) or not name:
            return None
        found = Path(_workdir.paths_for(project, name)["artifacts"])
    except (HerdrTeamError, OSError, ValueError, KeyError, ImportError):
        return None
    return found if found.is_dir() else None


def read_document(raw: Any, stamp: Optional[str] = None) -> Document:
    """``--from``'s path read and recognised, or refused by name. Reads; never writes, and never under a lock.

    Recognised in this order: the mirror's ``canvas.json`` (its ``payload`` version), the ``canvas export --format
    json`` wrapper (measured: the export emits ``{"format": "json", "scene": {...}}``, not a bare scene), a bare
    ``scene.json``, and a directory holding one of those.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise C._invalid("from", "import needs --from <path>: a team's folder in the project directory, a dissolved "
                                 "team's directory, or a scene file that `{} canvas export --format json` wrote".format(CLI))
    path = Path(os.path.expanduser(raw.strip()))
    assets_dir: Optional[Path] = None
    if path.is_dir():
        path, assets_dir = _document_in(path)
    else:
        assets_dir = _sibling_assets(path)
    check_not_symlink(path)
    try:
        size = os.lstat(path).st_size
    except OSError as err:
        raise _refuse_from(path, "cannot read {}: {}".format(path, err))
    if size > MAX_DOCUMENT_BYTES:
        raise C._too_big("from", "MAX_DOCUMENT_BYTES", MAX_DOCUMENT_BYTES,
                         "{} is {} MB; one import reads at most {} MB: export a region of that board as a picture "
                         "instead, or import it on a build with a larger cap".format(path, size // (1024 * 1024),
                                                                                     MAX_DOCUMENT_BYTES // (1024 * 1024)))
    data = store.read_bytes(path) or b""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as err:
        raise _damaged(path, "it is not UTF-8 text, from byte {}".format(err.start))
    try:
        document = json.loads(text)
    except ValueError as err:
        # A file that does not even begin as a JSON object is the wrong kind of file; one that begins as an object and
        # then fails to parse is the right kind of file, broken. The two have different repairs.
        if not text.lstrip().startswith("{"):
            raise _not_a_scene(path)
        where = getattr(err, "pos", None)
        raise _damaged(path, "it stops mid-JSON{}".format(" at byte {}".format(where) if isinstance(where, int) else ""))
    if not isinstance(document, dict):
        raise _not_a_scene(path)
    form, scene = _recognise(path, document)
    schema = scene.get("v")
    if C._is_number(schema) and int(schema) > C.SCHEMA:
        raise _refuse_from(path, "{} is schema v{}; this build reads v{}: export it again from the build that wrote "
                                 "it, or update {}".format(path, int(schema), C.SCHEMA, CLI), schema=int(schema))
    if scene.get("elements") is None and ("too_large" in scene or "element_count" in scene):
        # The mirror's pointer form (``workdir.render_canvas_snapshot``): the scene was over the mirror's cap, so the
        # file carries the board's name and the way to the live one instead of half a scene that looks importable.
        marks = scene.get("element_count")
        hint = scene.get("too_large") if isinstance(scene.get("too_large"), dict) else {}
        export = hint.get("export") or "{} canvas export --team {} --format json --out <file>".format(CLI, scene.get("team") or "<team>")
        raise _refuse_from(path, "{} is a pointer, not a scene: that canvas was too large to mirror ({} marks). Export "
                                 "it from the live team with `{}`, then import that file.".format(
                                     path, marks if marks is not None else "too many", export),
                           pointer=True)
    if not isinstance(scene.get("elements"), list):
        raise _not_a_scene(path)
    source = {"team": scene.get("team"), "version": int(scene.get("version") or 0),
              "updated_at": scene.get("updated_at") if isinstance(scene.get("updated_at"), str) else None,
              "path": os.fspath(path), "form": form, "stamp": stamp}
    return Document(path=path, form=form, scene=scene, assets_dir=assets_dir,
                    digest=hashlib.sha256(data).hexdigest(), bytes=len(data), source=source,
                    artifacts_source=_artifacts_source(path))


def _recognise(path: Path, document: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """``(form, scene-shaped mapping)``. A payload's ``scene_version`` reads back as the scene's ``version``."""
    if C._is_number(document.get("payload")):
        scene = dict(document)
        scene["version"] = int(document.get("scene_version") or 0)
        return "mirror", scene
    if document.get("format") == "json" and isinstance(document.get("scene"), dict):
        return "export", dict(document["scene"])
    if "elements" in document and ("v" in document or "version" in document):
        return "scene", dict(document)
    raise _not_a_scene(path)


# --------------------------------------------------------------------------
# the three things called "archive": only one of them is a dissolved team


def dissolved(session: Any, team_name: str) -> List[Dict[str, Any]]:
    """Every dissolved copy of ``team_name`` in this session's archive, newest first.

    Anchored on the whole team name, because a team name may contain ``-``: ``alpha-beta-<stamp>`` is team
    ``alpha-beta``, never team ``alpha``. A directory without both ``team.json`` and ``whiteboard/events.jsonl`` is
    not a dissolved team, which is what excludes ``cmd_misc``'s ``<team>-prune-<stamp>`` board dumps.
    """
    root = session.archive_dir
    pattern = re.compile(r"\A" + re.escape(team_name) + r"-(\d{8}T\d{6}Z)\Z")
    found: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    for name in names:
        match = pattern.match(name)
        if match is None:
            continue
        directory = root / name
        if not (directory / "team.json").is_file():
            continue
        if not (directory / "whiteboard" / C.EVENTS_FILE).is_file() and not (directory / "whiteboard" / C.SCENE_FILE).is_file():
            continue
        found.append({"team": team_name, "stamp": match.group(1), "path": os.fspath(directory)})
    found.sort(key=lambda entry: entry["stamp"], reverse=True)
    return found


def archive_paths(entry: Mapping[str, Any], team_name: str) -> TeamPaths:
    """A dissolved team's directory wrapped in an ordinary ``TeamPaths``, so the ordinary readers read it.

    Measured: ``canvas.load_scene`` over this takes no lock and writes nothing, so reading an archive leaves it byte
    for byte as it was. Never build an archive path by hand, and never let a writer near one.
    """
    return TeamPaths(Path(entry["path"]), team_name)


def readable_stamp(stamp: Any) -> str:
    """``20261001T120000Z`` as ``2026-10-01 12:00``, for a line a person reads."""
    text = str(stamp or "")
    if not STAMP_RE.match(text):
        return text
    return "{}-{}-{} {}:{}".format(text[0:4], text[4:6], text[6:8], text[9:11], text[11:13])


def archive_summary(entry: Mapping[str, Any], team_name: str) -> Dict[str, Any]:
    """What is actually recoverable from one archived team: read from it, so the operator chooses on evidence."""
    out = dict(entry)
    out["at"] = readable_stamp(entry.get("stamp"))
    paths = archive_paths(entry, team_name)
    try:
        scene = C.load_scene(paths)
    except (HerdrTeamError, OSError, ValueError):
        out.update(unreadable=True)
        return out
    elements = [el for el in scene.get("elements") or [] if isinstance(el, dict)]
    assets = asset_names({"elements": elements})
    present = 0
    total_bytes = 0
    for name in assets:
        try:
            total_bytes += os.lstat(paths.root / "whiteboard" / C.ASSETS_DIR / name).st_size
            present += 1
        except OSError:
            continue
    out.update(version=int(scene.get("version") or 0),
               elements=len([el for el in elements if el.get("type") != "comment"]),
               comments=len([el for el in elements if el.get("type") == "comment"]),
               assets=present, asset_bytes=total_bytes, empty=not elements)
    return out


def archive_line(summary: Mapping[str, Any], team_name: str) -> str:
    """One ``--list`` line: the command that imports it, then what is in it."""
    command = "{} canvas import --from-archive {}@{}".format(CLI, team_name, summary.get("stamp"))
    if summary.get("unreadable"):
        return "{}   (its canvas cannot be read)".format(command)
    if summary.get("empty"):
        return "{}   (empty)".format(command)
    parts = ["{} mark{}".format(summary["elements"], "" if summary["elements"] == 1 else "s")]
    if summary.get("comments"):
        parts.append("{} comment{}".format(summary["comments"], "" if summary["comments"] == 1 else "s"))
    if summary.get("assets"):
        parts.append("{} picture{}".format(summary["assets"], "" if summary["assets"] == 1 else "s"))
    return "{}   ({}, v{}, dissolved {})".format(command, ", ".join(parts), summary.get("version"), summary.get("at"))


def resolve_archive(session: Any, spec: Any, latest: bool = False) -> Dict[str, Any]:
    """``TEAM`` or ``TEAM@STAMP`` resolved to one dissolved team; several candidates are never guessed between."""
    text = str(spec or "").strip()
    if not text:
        raise C._invalid("from_archive", "--from-archive takes a dissolved team's name, or name@stamp")
    team_name, _, stamp = text.partition("@")
    team_name = team_name.strip()
    stamp = stamp.strip()
    if PRUNE_INFIX in team_name:
        raise C._invalid("from_archive", "{} is a pruned board dump, not a dissolved team: a prune keeps board segments "
                                         "only, with no canvas. List what can be imported with: {} canvas import "
                                         "--from-archive <team> --list".format(team_name, CLI))
    found = dissolved(session, team_name)
    if stamp:
        exact = [entry for entry in found if entry["stamp"] == stamp]
        if not exact:
            raise C._invalid("from_archive", "no {}@{} in this session's archive ({}); the stamps there are: {}".format(
                team_name, stamp, os.fspath(session.archive_dir), ", ".join(e["stamp"] for e in found) or "none"),
                team=team_name, stamp=stamp)
        return exact[0]
    if not found:
        raise C._invalid("from_archive", "no dissolved {} in this session's archive ({}); a team dissolved in another "
                                         "Herdr session is under that session's state: import it with `--from <path to "
                                         "its whiteboard/scene.json>`".format(team_name, os.fspath(session.archive_dir)),
                         team=team_name)
    if len(found) > 1 and not latest:
        lines = [archive_line(archive_summary(entry, team_name), team_name) for entry in found]
        raise C._invalid("from_archive", "team {} was dissolved {} times; name the one you want:\n  {}\n(or take the "
                                         "newest with --latest)".format(team_name, len(found), "\n  ".join(lines)),
                         team=team_name, stamps=[entry["stamp"] for entry in found])
    return found[0]


def recoverable(paths: TeamPaths) -> Dict[str, Any]:
    """What an archived team directory still holds, each count best effort: a count that cannot be read is left out.

    ``dissolve`` prints this after the rename, read from the archive rather than from memory, so it reports what is
    actually there. It must never fail the dissolve, which has already cleared the tokens and moved the directory.
    """
    out: Dict[str, Any] = {}
    try:
        scene = C.load_scene(paths)
        elements = [el for el in scene.get("elements") or [] if isinstance(el, dict)]
        out["canvas_elements"] = len([el for el in elements if el.get("type") != "comment"])
        out["canvas_comments"] = len([el for el in elements if el.get("type") == "comment"])
        names = asset_names({"elements": elements})
        out["canvas_assets"] = len([n for n in names if (paths.root / "whiteboard" / C.ASSETS_DIR / n).is_file()])
    except (HerdrTeamError, OSError, ValueError):
        pass
    try:
        from herdr_team import facts as _facts

        out["facts"] = len(_facts.load(paths).current())
    except (HerdrTeamError, OSError, ValueError, TypeError):
        pass
    try:
        out["rules_chars"] = len(paths.rules_md.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    try:
        out["posts"] = sum(1 for line in (store.read_bytes(paths.board_jsonl) or b"").split(b"\n") if line.strip())
    except (OSError, ValueError, AttributeError):
        pass
    try:
        from herdr_team import work as _work

        out["work"] = len(_work.load(paths))
    except (HerdrTeamError, OSError, ValueError, TypeError, AttributeError, ImportError):
        pass
    return {key: value for key, value in out.items() if value}


# --------------------------------------------------------------------------
# authority, options and the validator


@dataclass(frozen=True)
class Refusal:
    """One reason this import cannot apply: the letter of the spec table, the code, and the repair in the message."""

    letter: str
    code: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    def error(self) -> HerdrTeamError:
        return C._error(self.code, self.message, **self.details)

    def to_json(self) -> Dict[str, Any]:
        return {"refusal": self.letter, "code": self.code, "message": self.message, "details": dict(self.details)}


@dataclass
class Report:
    """What one import would do, and every reason it would not. ``--dry-run`` prints it; the op raises its first refusal.

    Built by one pure function so the two can never disagree: a dry run that said yes to an import the op refuses would
    be worse than no dry run at all.
    """

    source: Dict[str, Any] = field(default_factory=dict)
    refusals: List[Refusal] = field(default_factory=list)
    elements: List[Dict[str, Any]] = field(default_factory=list)
    skipped: Dict[str, List[str]] = field(default_factory=dict)
    assets: List[Dict[str, Any]] = field(default_factory=list)
    asset_bytes: Dict[str, bytes] = field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    already: List[str] = field(default_factory=list)
    fresh: List[str] = field(default_factory=list)
    authorship: Dict[str, Any] = field(default_factory=dict)
    legend: List[Dict[str, Any]] = field(default_factory=list)
    bytes: int = 0
    digest: str = ""

    def raise_first(self) -> None:
        if self.refusals:
            raise self.refusals[0].error()

    def to_json(self) -> Dict[str, Any]:
        return {"source": dict(self.source), "refusals": [r.to_json() for r in self.refusals],
                "elements": len(self.elements), "comments": sum(1 for el in self.elements if el.get("type") == "comment"),
                "counts": dict(self.counts), "skipped": {k: list(v) for k, v in self.skipped.items() if v},
                "assets": [dict(a) for a in self.assets], "artifacts": [dict(a) for a in self.artifacts],
                "already": list(self.already), "new": list(self.fresh), "authorship": dict(self.authorship),
                "legend": len(self.legend), "bytes": self.bytes, "digest": self.digest}


def options_of(op: Mapping[str, Any], author: Any) -> Dict[str, Any]:
    """The import's options as the op carries them, with the author they are checked against."""
    return {"keep_authors": bool(op.get("keep_authors")), "skip_unknown": bool(op.get("skip_unknown")),
            "skip_missing": bool(op.get("skip_missing")), "team_can_edit": bool(op.get("team_can_edit")),
            "author": author}


def authority(author: Any, options: Mapping[str, Any]) -> None:
    """Who may import, checked before any file is touched (so a member cannot probe for paths).

    ``--keep-authors`` is tested first and by name: a member who passed it must read why that flag in particular is the
    operator's, not a sentence about importing in general.
    """
    if options.get("keep_authors") and not _collab.is_lead(author):
        raise C._error("operator_only", "importing a board with its original authors is for the operator in person. "
                                        "Drop --keep-authors when asking the operator to import it as their own work. "
                                        "The operator can edit marks or undo the batch with either choice.",
                       author=getattr(author, "name", None))
    if not _collab.is_lead(author):
        raise _collab._lead_only("importing a board")
    if options.get("team_can_edit") and options.get("keep_authors"):
        # ``human_edits`` governs the operator's own marks. Under --keep-authors the marks stay their authors', so the
        # setting would change nothing about them and the flag would read as a promise the import cannot keep.
        raise C._invalid("team_can_edit", "--team-can-edit and --keep-authors ask for opposite things: the first makes "
                                          "the board yours and lets the team edit it, the second leaves every mark with "
                                          "the name that drew it (which only that member, the manager, a delegate or you "
                                          "may change). Pass one of them.")


def _kind_noun(el: Mapping[str, Any]) -> str:
    kind = _kinds.kind_of(el)
    return kind.name if kind is not None else str(el.get("type") or "?")


def _target_already(scene: Mapping[str, Any], source_team: Any) -> Dict[str, str]:
    """``source id -> this canvas's id`` for marks already imported from that board (the per-element idempotence key).

    Never matched on element id alone (ids are per canvas and two unrelated boards both start at ``E-1``) and never on
    text or geometry (a board with ten ``card "step"`` marks would collapse into one).
    """
    out: Dict[str, str] = {}
    for el in scene.get("elements") or []:
        if not isinstance(el, dict):
            continue
        record = el.get(PROVENANCE_KEY)
        if isinstance(record, dict) and record.get("team") == source_team and isinstance(record.get("id"), str):
            out[record["id"]] = str(el.get("id"))
    return out


def validate(document: Document, scene: Mapping[str, Any], doc: Mapping[str, Any], artifacts_root: Optional[Path],
             options: Mapping[str, Any]) -> Report:
    """Every refusal of the spec's table, and everything the import would do, without writing anything.

    Pure over its arguments (it reads the asset files beside the document; it writes nothing and takes no lock), so
    ``--dry-run`` and the op reach the same verdict by construction. Refusals come back in the order the op would
    raise them, most fundamental first.
    """
    author = options.get("author")
    keep = bool(options.get("keep_authors"))
    report = Report(source=dict(document.source), digest=document.digest, bytes=document.bytes)
    report.authorship = {"mode": "keep" if keep else "reattribute",
                         "author": getattr(author, "name", None),
                         "team_can_edit": bool(options.get("team_can_edit")),
                         "author_kind": C.KIND_HUMAN if getattr(author, "is_human", False) else C.KIND_MEMBER}
    report.legend = [entry for entry in document.scene.get("legend") or [] if isinstance(entry, dict)]
    refusals: List[Refusal] = []
    raw = [el for el in document.scene.get("elements") or [] if isinstance(el, dict) and isinstance(el.get("id"), str)]

    # (k) already imported -- unless the batch that brought it was undone, or every mark it brought has since been
    # deleted: either way nothing of it is on this board, a re-import is the repair, and the refusal's own
    # ``canvas undo`` would exit 1 with "nothing in it can be taken back now" (X9).
    prior = options.get("prior")
    if isinstance(prior, dict):
        entry = (scene.get("batches") or {}).get(prior.get("batch")) if isinstance(scene.get("batches"), dict) else None
        remaining = _target_already(scene, document.source.get("team"))
        still_here = any(el["id"] in remaining for el in raw)
        if still_here and not (isinstance(entry, dict) and entry.get("undone")):
            refusals.append(Refusal("k", "canvas_refused",
                                    "that board was imported already as {batch} at v{version}; `{cli} canvas changes "
                                    "--since {since}` shows it, and `{cli} canvas undo {batch}` takes it back".format(
                                        batch=prior.get("batch"), version=prior.get("version"),
                                        since=max(0, int(prior.get("version") or 1) - 1), cli=CLI),
                                    {"batch": prior.get("batch"), "version": prior.get("version")}))

    live = [el for el in scene.get("elements") or [] if isinstance(el, dict)]
    mapped = _target_already(scene, document.source.get("team"))
    report.already = sorted((eid for eid in mapped if any(el.get("id") == eid for el in raw)), key=C._id_number)
    report.fresh = sorted((el["id"] for el in raw if el["id"] not in mapped), key=C._id_number)

    # (g) a non-empty canvas: an import replaces nothing, and the source ids it keeps would collide.
    if live:
        tail = ""
        if report.already:
            tail = " ({} of these {} marks are already here from {}, {} are new)".format(
                len(report.already), len(raw), document.source.get("team"), len(report.fresh))
        refusals.append(Refusal("g", "canvas_refused",
                                "this canvas already holds {} mark{}; an import replaces nothing and ids would "
                                "collide{}: save a checkpoint (`{} canvas checkpoint \"before the import\"`) and "
                                "`{} whiteboard clear` (which archives it under whiteboard/archive/<stamp>/), then "
                                "import".format(len(live), "" if len(live) == 1 else "s", tail, CLI, CLI),
                                {"elements": len(live), "already": len(report.already), "new": len(report.fresh)}))

    # (f) over budget: named against the document, because MAX_ELEMENTS's own message talks about the canvas.
    if len(raw) > C.MAX_ELEMENTS:
        refusals.append(Refusal("f", "canvas_limit",
                                "{} holds {} marks; a canvas holds {}: import a region of it, or `{} whiteboard clear` "
                                "this canvas first".format(document.path, len(raw), C.MAX_ELEMENTS, CLI),
                                {"field": "elements", "limit": "MAX_ELEMENTS", "max": C.MAX_ELEMENTS}))

    unknown: Dict[str, List[str]] = {}
    newer: Dict[str, List[str]] = {}
    missing_artifacts: List[Dict[str, Any]] = []
    needs_asset: Dict[str, List[str]] = {}
    for el in raw:
        kind = _kinds.kind_of(el)
        if kind is None:
            unknown.setdefault(str(el.get("type") or "?"), []).append(el["id"])
            continue
        stored_kv = el.get("kv")
        stored_kv = int(stored_kv) if isinstance(stored_kv, int) and not isinstance(stored_kv, bool) else 1
        if stored_kv > kind.version:
            newer.setdefault("{} mark (kv {}; this build knows {})".format(kind.name, stored_kv, kind.version), []).append(el["id"])
            continue
        for field_name, rel in artifact_refs(el):
            ok = False
            if artifacts_root is not None:
                try:
                    C._artifact_path(artifacts_root, rel, field_name)
                    ok = True
                except HerdrTeamError:
                    ok = False
            report.artifacts.append({"id": el["id"], "field": field_name, "path": rel, "resolves": ok})
            if not ok:
                missing_artifacts.append({"id": el["id"], "field": field_name, "path": rel})
        for name in asset_names({"elements": [el]}):
            needs_asset.setdefault(name, []).append(el["id"])

    if unknown:
        if options.get("skip_unknown"):
            report.skipped["unknown"] = sorted((eid for ids in unknown.values() for eid in ids), key=C._id_number)
        else:
            shown = "; ".join("{} ({}: {})".format(kind_name, len(ids), C._ids_text(ids)) for kind_name, ids in sorted(unknown.items()))
            refusals.append(Refusal("d", "op_invalid",
                                    "this build does not know the mark {}: update {}, or import without them with "
                                    "--skip-unknown".format(shown, CLI),
                                    {"field": "elements", "types": sorted(unknown)}))
    if newer:
        shown = "; ".join("{} were stored by a newer build of the {}".format(C._ids_text(ids), label) for label, ids in sorted(newer.items()))
        refusals.append(Refusal("c", "op_invalid",
                                "{}: update {} to import them".format(shown, CLI),
                                {"field": "elements", "ids": sorted((eid for ids in newer.values() for eid in ids), key=C._id_number)}))

    # (e) a dataset or model that is not under this team's artifacts/: every absent one is named, not just the first.
    if missing_artifacts:
        if options.get("skip_missing"):
            report.skipped.setdefault("missing_artifact", []).extend(sorted({item["id"] for item in missing_artifacts}, key=C._id_number))
        else:
            where = os.fspath(artifacts_root) if artifacts_root is not None else "the team's artifacts/ folder"
            wanted = sorted({item["path"] for item in missing_artifacts})
            # Name the copy. A board that reads a dataset resolves it against the *importing* team's artifacts/, so
            # inheriting one into a differently named team fails on a file that is sitting one directory away -- and
            # the first version of this message did not say where. ``knowledge import`` copies these itself.
            found = [rel for rel in wanted if document.artifacts_source is not None
                     and (document.artifacts_source / rel).is_file()]
            hint = ""
            if found and artifacts_root is not None:
                hint = " ({} {} in {}: `cp {} {}/`)".format(
                    ", ".join(found), "is" if len(found) == 1 else "are", document.artifacts_source,
                    " ".join(os.fspath(document.artifacts_source / rel) for rel in found), where)
            refusals.append(Refusal("e", "op_invalid",
                                    "{} read {}, which is not under this team's artifacts/: copy it to {}{} and import "
                                    "again, or import without them with --skip-missing".format(
                                        C._ids_text(sorted({item["id"] for item in missing_artifacts}, key=C._id_number)),
                                        ", ".join(wanted), where, hint),
                                    {"field": "elements", "paths": wanted,
                                     "artifacts_source": os.fspath(document.artifacts_source) if document.artifacts_source else None}))

    # A mark left out by --skip-missing takes its pictures with it: carrying them would write bytes into this canvas's
    # assets/ that no element on it names, and the asset folder is never garbage-collected.
    if report.skipped.get("missing_artifact"):
        left_out = set(report.skipped["missing_artifact"])
        for name in list(needs_asset):
            needs_asset[name] = [eid for eid in needs_asset[name] if eid not in left_out]
            if not needs_asset[name]:
                del needs_asset[name]

    # the pictures: carried from beside the document, or already on this canvas. The name is the integrity check.
    target_assets = options.get("target_assets")
    absent: Dict[str, List[str]] = {}
    conflict: List[str] = []
    carried_bytes = 0
    for name in sorted(needs_asset):
        here = isinstance(target_assets, Path) and (target_assets / name).is_file()
        data: Optional[bytes] = None
        if not here and document.assets_dir is not None:
            candidate = document.assets_dir / name
            try:
                check_not_symlink(candidate)
                size = os.lstat(candidate).st_size
            except (OSError, HerdrTeamError):
                size = -1
            if 0 <= size <= MAX_ASSET_BYTES and carried_bytes + size <= MAX_ASSETS_BYTES:
                data = store.read_bytes(candidate)
        if data is not None:
            if hashlib.sha256(data).hexdigest()[:32] != name.split(".")[0]:
                conflict.append(name)
                data = None
            else:
                carried_bytes += len(data)
                report.asset_bytes[name] = data
        report.assets.append({"name": name, "ids": list(needs_asset[name]), "bytes": len(data) if data else 0,
                              "present": bool(here), "carried": bool(data)})
        if not here and data is None and name not in conflict:
            absent.setdefault(name, needs_asset[name])
    if conflict:
        refusals.append(Refusal("j", "op_invalid",
                                "{} is not what it says it is (its content hash differs from its name): the archive is "
                                "damaged; do not import it".format(", ".join(sorted(conflict))),
                                {"field": "assets", "assets": sorted(conflict)}))
    if absent:
        ids = sorted({eid for pairs in absent.values() for eid in pairs}, key=C._id_number)
        if options.get("skip_missing"):
            report.skipped.setdefault("missing_asset", []).extend(ids)
        else:
            refusals.append(Refusal("i", "op_invalid",
                                    "{} show a picture this import does not carry ({}): import from the folder that "
                                    "holds whiteboard/assets/ (or the mirror's canvas-assets/), or import without them "
                                    "with --skip-missing".format(C._ids_text(ids), ", ".join(sorted(absent))),
                                    {"field": "elements", "assets": sorted(absent), "ids": ids}))

    # (n) a system-authored element under --keep-authors: a hook cannot have drawn it legitimately.
    if keep:
        system = sorted((el["id"] for el in raw if el.get("author_kind") == C.KIND_SYSTEM), key=C._id_number)
        if system:
            refusals.append(Refusal("n", "author_mismatch",
                                    "{} say they were drawn by a hook or a startup process, which can read the canvas "
                                    "but not draw on it: import without --keep-authors, which makes every mark "
                                    "yours".format(C._ids_text(system)), {"ids": system}))

    dropped = set(report.skipped.get("unknown") or []) | set(report.skipped.get("missing_artifact") or []) \
        | set(report.skipped.get("missing_asset") or [])
    report.elements = [el for el in sorted(raw, key=lambda item: C._id_number(item["id"])) if el["id"] not in dropped]
    for el in report.elements:
        noun = _kind_noun(el)
        report.counts[noun] = report.counts.get(noun, 0) + 1
    report.refusals = refusals
    return report


def prior_import(team: TeamPaths, digest: str) -> Optional[Dict[str, Any]]:
    """The import of these exact bytes this canvas already holds, or None.

    Read backwards from the end of the log and stopped by a ``clear``: after a clear the board is gone, and importing
    the same document again is the recovery, not a duplicate. Bounded by ``MAX_IMPORT_SCAN`` so a long log cannot make
    an import slow.
    """
    seen = 0
    for event in C._events_backwards(team):
        seen += 1
        if seen > MAX_IMPORT_SCAN or event.get("op") == "clear":
            return None
        info = event.get(OP_KEY)
        if isinstance(info, dict) and info.get("digest") == digest:
            return {"batch": event.get("batch"), "version": int(event.get("seq") or 0), "team": info.get("team"),
                    "elements": info.get("elements")}
    return None


def copy_artifacts(document: Document, target_root: Optional[Path]) -> Dict[str, Any]:
    """Bring the data files the source board reads into the importing team's ``artifacts/``. ``{copied, omitted, dir}``.

    Why this exists at all: a chart's ``data_path`` and a 3D object's ``src`` are resolved against the *importing*
    team's own ``artifacts/`` folder (``canvas._artifact_path``), not against the board they came from. A team that
    inherits under the same name shares that folder with the team that died and never notices; a team that inherits
    under a different name is refused (e) for a file sitting one directory away. Measured: inheriting a 14-mark board
    with one chart from ``alpha`` into ``beta`` imported nothing at all.

    Never overwrites: a file already there is this team's, and the two are not the same claim. Copies only what an
    element names, so the folder does not grow by the whole of somebody else's dataset collection. Best effort, like
    every other step of the inheritance: what it could not copy is named, and refusal (e) still has the last word.
    """
    out: Dict[str, Any] = {"copied": [], "omitted": [], "dir": os.fspath(target_root) if target_root else None,
                           "from": os.fspath(document.artifacts_source) if document.artifacts_source else None}
    source_root = document.artifacts_source
    if target_root is None or source_root is None:
        out["reason"] = "no artifacts folder {}".format("for this team" if target_root is None else "in the source")
        return out
    wanted: List[str] = []
    for el in document.scene.get("elements") or []:
        if not isinstance(el, dict):
            continue
        for _field, rel in artifact_refs(el):
            if rel not in wanted:
                wanted.append(rel)
    total = 0
    for rel in wanted:
        try:
            source = _artifact_under(source_root, rel)
            target = _artifact_target(target_root, rel)
        except HerdrTeamError as err:
            out["omitted"].append({"path": rel, "why": err.code})
            continue
        if target.exists():
            continue  # already this team's; the import reads it from here either way
        if source is None:
            out["omitted"].append({"path": rel, "why": "not in the source"})
            continue
        size = source.stat().st_size
        if size > MAX_ARTIFACT_BYTES or total + size > MAX_ARTIFACTS_BYTES:
            out["omitted"].append({"path": rel, "why": "over the budget", "bytes": int(size)})
            continue
        data = store.read_bytes(source)
        if data is None:
            out["omitted"].append({"path": rel, "why": "unreadable"})
            continue
        try:
            from herdr_team import paths as _paths

            _paths.ensure_dir(target.parent)
            check_not_symlink(target)
            store.atomic_write(target, data, fsync=False)
        except (HerdrTeamError, OSError) as err:
            out["omitted"].append({"path": rel, "why": type(err).__name__})
            continue
        total += len(data)
        out["copied"].append(rel)
    return out


def _artifact_under(root: Path, rel: str) -> Optional[Path]:
    """``rel`` as a readable file under ``root``, or None when it is not there (refused when the path itself is not one)."""
    try:
        return C._artifact_path(root, rel, "data")
    except HerdrTeamError as err:
        if err.code == "path_refused" and "not a file under" in err.message:
            return None
        raise


def _artifact_target(root: Path, rel: str) -> Path:
    """Where ``rel`` goes under ``root``, with the same rules ``_artifact_path`` reads it back by (and no ``..``)."""
    text = rel.strip()
    if text.startswith("artifacts/"):
        text = text[len("artifacts/"):]
    parts = Path(text).parts
    if Path(text).is_absolute() or ".." in parts or "\\" in text or not parts or ":" in parts[0]:
        raise C._error("path_refused", "{} must be a path relative to the team's artifacts/ folder".format(rel), path=rel)
    return root / text


# --------------------------------------------------------------------------
# the op


def op_import(ctx: Any, op: Dict[str, Any]) -> None:
    """``{"op": "import", "from": PATH, ...}`` (the operator in person): one board replayed as one undoable batch.

    Every refusal is raised before anything is written, by the same validator ``--dry-run`` calls. The assets are
    staged (``ctx.staged``) rather than written here, so a refusal later in the batch leaves no file behind.
    """
    options = options_of(op, ctx.author)
    authority(ctx.author, options)
    if op.get("if_version") is not None:
        raise C._invalid("if_version", "import acts on the whole board; it takes no if_version")
    document = read_document(op.get("from"), stamp=op.get("stamp") if isinstance(op.get("stamp"), str) else None)
    options["prior"] = prior_import(ctx.team, document.digest)
    options["target_assets"] = C._dir(ctx.team) / C.ASSETS_DIR
    try:
        root = C.artifacts_dir(ctx.layout, ctx.team, ctx.doc)
    except (HerdrTeamError, OSError, ValueError):
        root = None
    report = validate(document, ctx.state.to_scene(ctx.now), ctx.doc, root, options)
    report.raise_first()

    for name, data in sorted(report.asset_bytes.items()):
        ctx.staged[name] = data
    provenance = {"team": document.source.get("team"), "author": None, "author_kind": None,
                  "at": document.source.get("updated_at"), "from": document.source.get("stamp") or document.source.get("form")}
    keep = bool(options.get("keep_authors"))
    collapsed: List[str] = []
    imported: List[str] = []
    boxes: List[Sequence[float]] = []
    for el in report.elements:
        value = dict(el)
        record = dict(provenance, id=el["id"], author=el.get("author"), author_kind=el.get("author_kind"))
        value[PROVENANCE_KEY] = record
        value["created_seq"] = ctx.seq
        value["updated_seq"] = ctx.seq
        value["created_at"] = ctx.ts
        value["updated_at"] = ctx.ts
        value["batch"] = ctx.batch_id
        if not keep:
            value["author"] = ctx.author.name
            value["author_kind"] = C.KIND_HUMAN if ctx.author.is_human else C.KIND_MEMBER
        elif el.get("author_kind") == C.KIND_HUMAN:
            # The same operator in the same session: their old marks stay theirs, and _may_edit leaves them operator-only.
            value["author"] = C.HUMAN
        value.pop("nudged", None)
        if value.get("alias"):
            # Re-attributing a whole board to one author collapses ``aliases`` (alias -> {author: id}): two source marks
            # sharing an alias under different authors would become one reachable alias, silently. The loser loses it.
            owner = (ctx.state.aliases.get(value["alias"]) or {}).get(str(value.get("author")))
            taken = next((other for other in imported if (ctx.el(other) or {}).get("alias") == value["alias"]
                          and (ctx.el(other) or {}).get("author") == value.get("author")), None)
            if taken is not None or (owner is not None and owner != el["id"] and ctx.el(owner) is not None):
                collapsed.append(el["id"])
                value["alias"] = None
        ctx.put(value)
        imported.append(el["id"])
        boxes.append(C.bounds(value))
    if collapsed:
        ctx.warn("alias_collapsed", "{} lost its short name: another mark on this board already answers to it; give it "
                                    "one again with `{} canvas draw` if you need it".format(C._ids_text(collapsed), CLI), collapsed)
    # The board is **reproduced**, not re-arranged. Measured on the owner's own 25-mark board: without these three
    # lines the import re-laid out its graph block and re-routed ten arrows, because every imported mark looks to
    # ``canvas_blocks._detect`` like a member just added to a container, and to ``_settle_labels`` like a label just
    # drawn. The picture that came back was not the picture that was saved. ``detected`` and ``undo_restored`` are the
    # switches the engine already has for "this handler has placed these marks itself" and "these were written back as
    # they were stored" -- which is exactly what an import does.
    ctx.detected = True
    ctx.dirty.clear()
    ctx.undo_restored.update(imported)
    # A lead-only core op bypasses the review gate (``review_op`` short-circuits on ``batch.lead``), so the operator's
    # own locked regions are checked here or not at all.
    C._check_locks(ctx, boxes)
    C._unbind(ctx, [el for el in (ctx.el(eid) for eid in imported) if el is not None])
    C._reroute_bound(ctx, imported, skip=imported)
    team_can_edit = bool(options.get("team_can_edit"))
    if team_can_edit:
        # Inside the import, not beside it: one batch, so ``undo B-n`` takes the setting back with the marks, and the
        # result line cannot claim a mode the scene does not have. ``op_settings``'s own write, to the same key.
        current = dict(ctx.state.settings.get(_collab.SETTINGS_KEY) or {})
        current["human_edits"] = "live"
        ctx.other("setting", "update" if _collab.SETTINGS_KEY in ctx.state.settings else "add",
                  _collab.SETTINGS_KEY, dict(_collab.settings_of_values(current)))
    info = {"digest": document.digest, "team": document.source.get("team"), "version": document.source.get("version"),
            "stamp": document.source.get("stamp"), "form": document.source.get("form"),
            "elements": len([eid for eid in imported if eid.startswith("E-")]),
            "comments": len([eid for eid in imported if eid.startswith("C-")]),
            "assets": len(report.asset_bytes), "skipped": {k: list(v) for k, v in report.skipped.items() if v},
            "authors": "keep" if keep else "reattribute", "team_can_edit": team_can_edit, "batch": ctx.batch_id}
    ctx.extra[OP_KEY] = dict(info)
    ctx.entry_extra[OP_KEY] = dict(info)


# --------------------------------------------------------------------------
# what it reads back as


def result_text(info: Mapping[str, Any]) -> str:
    """``imported 41 marks and 3 comments from beta (v37, dissolved 2026-10-01 12:00); 2 pictures copied; undo B-7 ...``."""
    parts = ["imported {} mark{}".format(info.get("elements", 0), "" if info.get("elements") == 1 else "s")]
    if info.get("comments"):
        parts.append("{} comment{}".format(info["comments"], "" if info["comments"] == 1 else "s"))
    where = "from {}".format(info.get("team") or "another board")
    tail = ["v{}".format(info.get("version"))]
    if info.get("stamp"):
        tail.append("dissolved {}".format(readable_stamp(info["stamp"])))
    text = "{} {} ({})".format(" and ".join(parts), where, ", ".join(tail))
    if info.get("assets"):
        text += "; {} picture{} copied".format(info["assets"], "" if info["assets"] == 1 else "s")
    for reason, label in (("unknown", "not drawn by this build"), ("missing_artifact", "without their data file"),
                          ("missing_asset", "without their picture")):
        left = (info.get("skipped") or {}).get(reason) or []
        if left:
            text += "; {} left out ({})".format(C._ids_text(list(left)), label)
    text += "; undo {} to take it back".format(info.get("batch") or "the batch")
    if info.get("authors") == "keep":
        text += "\noriginal author names retained; the operator can edit marks or undo {}".format(info.get("batch") or "the batch")
    else:
        text += "\n" + team_edit_line(bool(info.get("team_can_edit")))
    return text


def team_edit_line(team_can_edit: bool) -> str:
    """Who may work on a re-attributed board, and the one command that changes the answer.

    Measured, and the reason this line exists: a re-attributed import makes every mark the operator's, and the default
    ``human_edits: propose`` turns every edit, move and delete of the operator's marks by a member, the manager or a
    delegate into a proposal (reason ``human_made``). A team that inherited a 30-mark board hit the 20-proposal limit
    trying to tidy it. The board came from agents who could edit it freely; saying nothing here would hand the team a
    diagram they can read and not touch, and the knob that fixes it appeared in no message and no document.
    """
    if team_can_edit:
        return ("the team can work on this board directly (human_edits: live, so their changes apply and you can "
                "revert any of them)")
    return ("these marks are yours now, so the team's changes to them arrive as proposals; `{} canvas settings "
            "--human-edits live` lets them work on the board directly (import with --team-can-edit to do both at "
            "once)".format(CLI))


def summarize(event: Mapping[str, Any]) -> str:
    """One change line for an ``import`` event (``canvas changes``, the page's History)."""
    info = event.get(OP_KEY) if isinstance(event.get(OP_KEY), dict) else {}
    return "imported {} marks from {} (v{})".format(info.get("elements", 0), info.get("team") or "another board",
                                                    info.get("version"))


def dry_run_text(report: Report) -> str:
    """One line per decision, in the shape ``cmd_restore``'s dry run uses: what would happen, then what would refuse it."""
    source = report.source
    lines = ["dry run: {} ({}, v{}) → {} mark{}{}".format(
        source.get("path"), source.get("team") or "an unnamed board", source.get("version"),
        len(report.elements), "" if len(report.elements) == 1 else "s",
        ", nothing written" if not report.refusals else ", and it would be refused")]
    if report.counts:
        lines.append("  marks: " + ", ".join("{} {}".format(count, noun) for noun, count in sorted(report.counts.items())))
    if report.legend:
        lines.append("  legend: {} entr{}".format(len(report.legend), "y" if len(report.legend) == 1 else "ies"))
    lines.append("  authors: {}".format("kept as they were" if report.authorship.get("mode") == "keep"
                                        else "every mark becomes {}'s".format(report.authorship.get("author"))))
    if report.authorship.get("mode") != "keep":
        lines.append("  the team: " + team_edit_line(bool(report.authorship.get("team_can_edit"))))
    lines.append("  bytes: {} of {} KB; marks: {} of {}".format(report.bytes, MAX_DOCUMENT_BYTES // 1024,
                                                                len(report.elements), C.MAX_ELEMENTS))
    for asset in report.assets:
        lines.append("  picture {}: {}".format(asset["name"], "already here" if asset["present"] else
                                               ("{} bytes to copy".format(asset["bytes"]) if asset["carried"] else "not carried")))
    for entry in report.artifacts:
        lines.append("  {} reads {} ({})".format(entry["id"], entry["path"], "found" if entry["resolves"] else "not found"))
    if report.already:
        lines.append("  {} already here, {} new".format(len(report.already), len(report.fresh)))
    for reason, ids in sorted(report.skipped.items()):
        if ids:
            lines.append("  left out ({}): {}".format(reason, C._ids_text(list(ids))))
    for refusal in report.refusals:
        lines.append("  refused {} ({}): {}".format(refusal.letter, refusal.code, refusal.message))
    return "\n".join(lines)
