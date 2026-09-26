"""What this machine can start: installed harnesses, their profiles and their models (0.20).

A *harness* is the program a member runs (Claude Code, Codex, OpenCode, ...).
A *profile* is a harness's own named setup: an OpenCode or Claude Code agent,
or a Codex config profile. Models are the ones the harness itself can
use with the logins on this machine.

Everything here asks the harness the way the harness would answer itself: its
own list command (``opencode agent list``, ``opencode models``,
``pi --list-models``), its own cache (Codex's ``models_cache.json``), or its
own definition folders (``.claude/agents``, ``$CODEX_HOME/*.config.toml``).
Nothing calls a provider or spends tokens. A harness that cannot say is
reported as *unknown*, never as empty, and whatever is set for it passes
through unchecked: the lists refuse typos, they never block a harness whose
answer changed shape.

Each probe runs at most once per process (``_memo``), and ``catalog`` runs the
probes of different harnesses in parallel, so ``available`` costs about as
long as the slowest harness (OpenCode and Pi take one to three seconds). Only
command-line and popup entry points call this; nothing in the notifier or a
render loop does.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from herdr_team import models as _models
from herdr_team import permissions as _permissions
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError

#: Herdr's ``interactive_agent_executable``: every kind runs as its own name except these.
EXECUTABLES: Dict[str, str] = {"cursor": "cursor-agent", "kiro": "kiro-cli"}
#: Seconds a harness gets to answer one probe.
PROBE_TIMEOUT_S = 20.0
#: OpenCode's built-in agents that are marked primary but never run a session.
OPENCODE_INTERNAL = frozenset({"compaction", "summary", "title"})
#: Claude's model aliases, which ``--model`` takes alongside full names.
CLAUDE_ALIASES = ("opus", "sonnet", "haiku", "fable")

_OPENCODE_AGENT = re.compile(r"^(?P<name>[^\s\[\]{}\"'][^\n]*?) \((?P<mode>primary|subagent|all)\)\s*$")
_VERSION = re.compile(r"\d+\.\d+[0-9A-Za-z.\-+]*")


@dataclass
class Profile:
    name: str
    description: str = ""
    #: ``builtin`` | ``user`` | ``project`` | ``config``
    source: str = ""
    #: False for a definition that cannot run a session of its own (an OpenCode subagent).
    selectable: bool = True

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "source": self.source, "selectable": self.selectable}


@dataclass
class ModelInfo:
    id: str
    provider: Optional[str] = None
    display: Optional[str] = None
    #: The efforts this model accepts; None when the harness does not say.
    efforts: Optional[List[str]] = None
    default_effort: Optional[str] = None
    context: Optional[str] = None
    thinking: Optional[bool] = None
    #: Listed by the harness but not offered in its own picker; still accepted.
    hidden: bool = False

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id}
        for key in ("provider", "display", "efforts", "default_effort", "context", "thinking"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.hidden:
            out["hidden"] = True
        return out


@dataclass
class ModelList:
    models: List[ModelInfo]
    #: Where the list came from, for the reader: a command or a file.
    source: str
    #: True when the list is the harness's whole answer, so a name outside it is a typo.
    authoritative: bool = True

    def ids(self, include_hidden: bool = True) -> List[str]:
        return [m.id for m in self.models if include_hidden or not m.hidden]


@dataclass
class Harness:
    kind: str
    executable: str
    path: Optional[str] = None
    version: Optional[str] = None
    #: None: the harness has no profiles Synapse can select. []: it has, but none are defined.
    profiles: Optional[List[Profile]] = None
    profile_flag: Optional[str] = None
    profiles_note: Optional[str] = None
    models: Optional[ModelList] = None
    models_note: Optional[str] = None
    efforts: Optional[List[str]] = None
    yolo: List[str] = field(default_factory=list)
    yolo_evidence: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    @property
    def installed(self) -> bool:
        return self.path is not None

    def to_json(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "executable": self.executable, "installed": self.installed, "path": self.path, "version": self.version,
            "profile_flag": self.profile_flag,
            "profiles": None if self.profiles is None else [p.to_json() for p in self.profiles],
            "profiles_note": self.profiles_note,
            "models": None if self.models is None else [m.to_json() for m in self.models.models],
            "models_source": None if self.models is None else self.models.source,
            "models_authoritative": None if self.models is None else self.models.authoritative,
            "models_note": self.models_note,
            "efforts": self.efforts, "yolo": self.yolo, "yolo_evidence": self.yolo_evidence, "errors": self.errors,
        }


# --------------------------------------------------------------------------
# running a harness


def _run(argv: Sequence[str], cwd: Optional[str], env: Optional[Dict[str, str]]) -> Tuple[int, str]:
    """``argv`` in ``cwd`` with no terminal and a deadline; (-1, "") when it cannot run."""
    try:
        result = subprocess.run(list(argv), cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError, ValueError):
        return -1, ""
    return result.returncode, result.stdout or ""


#: Tests replace this with a fake that answers from fixtures.
RUN: Callable[[Sequence[str], Optional[str], Optional[Dict[str, str]]], Tuple[int, str]] = _run

_memo: Dict[Tuple[str, ...], Any] = {}
_memo_lock = threading.Lock()


def clear_cache() -> None:
    with _memo_lock:
        _memo.clear()


def _cached(key: Tuple[str, ...], compute: Callable[[], Any]) -> Any:
    with _memo_lock:
        if key in _memo:
            return _memo[key]
    value = compute()
    with _memo_lock:
        _memo[key] = value
    return value


def _home(env: Optional[Dict[str, str]]) -> Path:
    home = (env or {}).get("HOME") or os.environ.get("HOME")
    return Path(home) if home else Path.home()


def _cwd(cwd: Optional[str], env: Optional[Dict[str, str]]) -> str:
    if cwd and os.path.isdir(cwd):
        return os.path.abspath(cwd)
    try:
        return os.getcwd()
    except OSError:
        return os.fspath(_home(env))


def executable(kind: str) -> str:
    return EXECUTABLES.get(kind, kind)


def which(kind: str, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    path = (env or {}).get("PATH") if env else None
    return shutil.which(executable(kind), path=path if path is not None else os.environ.get("PATH", os.defpath))


def version(kind: str, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    def compute() -> Optional[str]:
        path = which(kind, env)
        if path is None:
            return None
        code, out = RUN([path, "--version"], None, env)
        if code != 0:
            return None
        for line in out.splitlines():
            found = _VERSION.search(line)
            if found:
                return found.group(0)
        return None

    return _cached(("version", kind), compute)


# --------------------------------------------------------------------------
# profiles


def _frontmatter(path: Path) -> Dict[str, str]:
    """The ``---`` header of a Markdown definition: flat keys, folded values joined into one line."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: Dict[str, str] = {}
    key: Optional[str] = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line[:1] in (" ", "\t") and key is not None:
            out[key] = (out[key] + " " + line.strip()).strip()
            continue
        name, sep, value = line.partition(":")
        if not sep or not name.strip() or " " in name.strip():
            key = None
            continue
        key = name.strip()
        value = value.strip()
        out[key] = "" if value in (">", ">-", "|", "|-") else value.strip("\"'")
    return out


def _project_roots(cwd: str) -> List[Path]:
    """The member's directory and, when it is inside a git checkout, the checkout root."""
    here = Path(cwd)
    roots = [here]
    for parent in [here] + list(here.parents):
        if (parent / ".git").exists():
            if parent != here:
                roots.append(parent)
            break
    return roots


def _claude_profiles(cwd: str, env: Optional[Dict[str, str]]) -> List[Profile]:
    seen: Dict[str, Profile] = {}
    folders = [(root / ".claude" / "agents", "project") for root in _project_roots(cwd)] + [(_home(env) / ".claude" / "agents", "user")]
    for folder, source in folders:
        try:
            files = sorted(folder.glob("*.md"))
        except OSError:
            continue
        for path in files:
            meta = _frontmatter(path)
            name = meta.get("name") or path.stem
            if name and name not in seen:  # a project definition wins over a user one, as in Claude Code
                seen[name] = Profile(name=name, description=meta.get("description", ""), source=source)
    return sorted(seen.values(), key=lambda p: p.name)


def _opencode_profiles(cwd: str, env: Optional[Dict[str, str]]) -> Optional[List[Profile]]:
    path = which("opencode", env)
    if path is None:
        return None
    code, out = RUN([path, "agent", "list"], cwd, env)
    if code != 0:
        return None
    rows: List[Profile] = []
    for line in out.splitlines():
        found = _OPENCODE_AGENT.match(line)
        if not found:
            continue
        name, mode = found.group("name").strip(), found.group("mode")
        if name in OPENCODE_INTERNAL:
            continue
        rows.append(Profile(name=name, source="opencode", selectable=mode != "subagent",
                            description="subagent: runs inside another agent's session" if mode == "subagent" else ""))
    return rows or None


def _codex_home(env: Optional[Dict[str, str]]) -> Path:
    value = (env or {}).get("CODEX_HOME") or os.environ.get("CODEX_HOME")
    return Path(value) if value else _home(env) / ".codex"


def _codex_profiles(env: Optional[Dict[str, str]]) -> List[Profile]:
    try:
        files = sorted(_codex_home(env).glob("*.config.toml"))
    except OSError:
        return []
    return [Profile(name=p.name[: -len(".config.toml")], source="config", description=os.fspath(p))
            for p in files if p.name != "config.toml" and p.name[: -len(".config.toml")]]


def profiles(kind: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Optional[List[Profile]]:
    """The profiles ``kind`` can launch with here, subagents included but marked; None when it cannot say."""
    if kind not in _models.PROFILE_FLAGS:
        return None
    where = _cwd(cwd, env)

    def compute() -> Optional[List[Profile]]:
        if kind == "opencode":
            return _opencode_profiles(where, env)
        if kind == "claude":
            return _claude_profiles(where, env)
        if kind == "codex":
            return _codex_profiles(env)
        return None

    return _cached(("profiles", kind, where), compute)


# --------------------------------------------------------------------------
# models


def _codex_models(env: Optional[Dict[str, str]]) -> Optional[ModelList]:
    path = _codex_home(env) / "models_cache.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rows: List[ModelInfo] = []
    for item in (doc.get("models") if isinstance(doc, dict) else None) or []:
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
            continue
        levels = [str(level.get("effort")) for level in item.get("supported_reasoning_levels") or []
                  if isinstance(level, dict) and isinstance(level.get("effort"), str)]
        window = item.get("context_window")
        rows.append(ModelInfo(id=item["slug"], display=item.get("display_name") if isinstance(item.get("display_name"), str) else None,
                              efforts=levels or None,
                              default_effort=item.get("default_reasoning_level") if isinstance(item.get("default_reasoning_level"), str) else None,
                              context=_short_count(window) if isinstance(window, int) else None,
                              hidden=item.get("visibility") not in (None, "list")))
    return ModelList(rows, os.fspath(path)) if rows else None


def _opencode_models(cwd: str, env: Optional[Dict[str, str]]) -> Optional[ModelList]:
    path = which("opencode", env)
    if path is None:
        return None
    code, out = RUN([path, "models"], cwd, env)
    if code != 0:
        return None
    rows = []
    for line in out.splitlines():
        text = line.strip()
        if "/" in text and " " not in text:
            rows.append(ModelInfo(id=text, provider=text.split("/", 1)[0]))
    return ModelList(rows, "opencode models") if rows else None


def _pi_models(env: Optional[Dict[str, str]]) -> Optional[ModelList]:
    path = which("pi", env)
    if path is None:
        return None
    code, out = RUN([path, "--list-models"], None, env)
    if code != 0:
        return None
    rows = []
    header: Optional[List[str]] = None
    for line in out.splitlines():
        cells = line.split()
        if not cells:
            continue
        if cells[0] == "provider" and "model" in cells:
            header = cells
            continue
        if header is None or len(cells) < 2:
            continue
        row = dict(zip(header, cells))
        provider, model = row.get("provider"), row.get("model")
        if not provider or not model:
            continue
        thinking = row.get("thinking")
        rows.append(ModelInfo(id="{}/{}".format(provider, model), provider=provider, context=row.get("context"),
                              thinking=True if thinking == "yes" else False if thinking == "no" else None))
    return ModelList(rows, "pi --list-models") if rows else None


def _claude_models() -> ModelList:
    from herdr_team import context as _context

    ids = list(CLAUDE_ALIASES) + [m for m in list(_context.CLAUDE_1M_MODELS) + list(_context.CLAUDE_200K_MODELS) if m not in CLAUDE_ALIASES]
    efforts = list(_models.EFFORTS.get("claude") or ())
    return ModelList([ModelInfo(id=m, provider="anthropic", efforts=efforts or None) for m in ids],
                     "Synapse's list of Claude names; Claude Code takes any alias or full model name", authoritative=False)


def _short_count(value: int) -> str:
    if value >= 1_000_000 and value % 1_000_000 == 0:
        return "{}M".format(value // 1_000_000)
    if value >= 1000:
        return "{}K".format(value // 1000)
    return str(value)


def models(kind: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Optional[ModelList]:
    """The models ``kind`` can run here; None when Synapse passes no model to it or the harness cannot say."""
    if kind not in _models.KINDS:
        return None
    where = _cwd(cwd, env)

    def compute() -> Optional[ModelList]:
        if kind == "codex":
            return _codex_models(env)
        if kind == "opencode":
            return _opencode_models(where, env)
        if kind == "pi":
            return _pi_models(env)
        return _claude_models()

    return _cached(("models", kind, where if kind == "opencode" else ""), compute)


# --------------------------------------------------------------------------
# the whole picture


def all_kinds() -> List[str]:
    from herdr_team import roster as _roster

    return sorted(_roster.KIND_LABELS)


def describe(kind: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, with_version: bool = True) -> Harness:
    harness = Harness(kind=kind, executable=executable(kind), path=which(kind, env),
                      profile_flag=_models.PROFILE_FLAGS.get(kind),
                      yolo=list(_permissions.YOLO_ARGS.get(kind, ())), yolo_evidence=_permissions.YOLO_EVIDENCE.get(kind))
    efforts = _models.EFFORTS.get(kind) if kind in _models.KINDS else None
    harness.efforts = list(efforts) if efforts else None
    if not harness.installed:
        return harness
    if with_version:
        harness.version = version(kind, env)
    if kind in _models.PROFILE_FLAGS:
        harness.profiles = profiles(kind, cwd, env)
        if harness.profiles is None:
            harness.profiles_note = "{} did not list its profiles".format(kind)
    if kind in _models.KINDS:
        harness.models = models(kind, cwd, env)
        if harness.models is None:
            harness.models_note = "{} did not list its models; any name is passed through".format(kind)
    else:
        harness.models_note = "Synapse passes no model to {}; it uses its own setting".format(kind)
    return harness


def catalog(kinds: Optional[Sequence[str]] = None, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None,
            installed_only: bool = True, with_version: bool = True) -> List[Harness]:
    """Every harness (installed ones unless asked otherwise), each probed in parallel."""
    wanted = list(kinds) if kinds else all_kinds()
    present = [k for k in wanted if not installed_only or which(k, env) is not None]
    if not present:
        return []
    # Every probe of every harness at once; ``describe`` then reads the memo.
    probes: List[Callable[[], Any]] = []
    for kind in present:
        if which(kind, env) is None:
            continue
        if with_version:
            probes.append(lambda k=kind: version(k, env))
        if kind in _models.PROFILE_FLAGS:
            probes.append(lambda k=kind: profiles(k, cwd, env))
        if kind in _models.KINDS:
            probes.append(lambda k=kind: models(k, cwd, env))
    if probes:
        with ThreadPoolExecutor(max_workers=min(12, len(probes))) as pool:
            list(pool.map(lambda probe: probe(), probes))
    return [describe(k, cwd, env, with_version) for k in present]


# --------------------------------------------------------------------------
# checks used by create, add, model, models, profile and swap


def _match(kind: str, listing: ModelList, model: str) -> Optional[ModelInfo]:
    want = model.strip().lower()
    for info in listing.models:
        if info.id.lower() == want:
            return info
    if kind == "pi":
        # Pi takes ``provider/id``, a bare id, or a fuzzy pattern, with an optional ``:<thinking>``.
        bare = want.rsplit(":", 1)[0] if ":" in want and "/" not in want.rsplit(":", 1)[1] else want
        for info in listing.models:
            if info.id.lower() == bare or info.id.lower().split("/", 1)[-1] == bare:
                return info
        for info in listing.models:
            if bare and bare in info.id.lower():
                return info
    return None


def _suggest(value: str, choices: List[str], limit: int = 6) -> List[str]:
    close = difflib.get_close_matches(value, choices, n=limit, cutoff=0.45)
    return close or choices[:limit]


def model_problem(kind: str, listing: Optional[ModelList], model: Optional[str], effort: Optional[str]) -> Optional[HerdrTeamError]:
    """Why ``model``/``effort`` is not one ``listing`` offers, or None; pure, so the picker can ask it too.

    Only an authoritative, non-empty list refuses anything.
    """
    if listing is None or not listing.authoritative or not listing.models or (model is None and effort is None):
        return None
    found: Optional[ModelInfo] = None
    if model is not None:
        found = _match(kind, listing, model)
        if found is None:
            visible = listing.ids(include_hidden=False)
            suggestions = _suggest(model, visible)
            return HerdrTeamError(
                "model_unlisted",
                "{} does not list {!r} here; closest: {}. See: herdr-synapse available {}{} (or pass --unlisted if you know it exists)".format(
                    kind, model, ", ".join(suggestions), kind, " --search " + model.split("/")[-1][:20] if len(visible) > 30 else ""),
                EXIT_REFUSED, {"kind": kind, "model": model, "suggestions": suggestions, "source": listing.source},
            )
    if effort is not None and found is not None and found.efforts and effort not in found.efforts:
        return HerdrTeamError(
            "effort_unsupported",
            "{} takes the efforts {}; not {!r}".format(found.id, ", ".join(found.efforts), effort),
            EXIT_REFUSED, {"kind": kind, "model": found.id, "effort": effort, "efforts": list(found.efforts)},
        )
    return None


def check_model(kind: str, model: Optional[str], effort: Optional[str], cwd: Optional[str] = None,
                env: Optional[Dict[str, str]] = None, unlisted: bool = False) -> None:
    """Refuse a model the harness does not list here, or an effort that model does not take.

    ``unlisted`` skips the list entirely (a model newer than the harness's cache, for example).
    """
    if unlisted or (model is None and effort is None) or kind not in _models.KINDS:
        return
    problem = model_problem(kind, models(kind, cwd, env), model, effort)
    if problem is not None:
        raise problem


def check_profile(kind: str, profile: Optional[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None,
                  unlisted: bool = False) -> Optional[str]:
    """The profile name, validated; refused when the harness lists its profiles here and this is not one it can run."""
    name = _models.validate_profile(kind, profile)
    if name is None or unlisted:
        return name
    listing = profiles(kind, cwd, env)
    if listing is None:
        return name
    runnable = [p.name for p in listing if p.selectable]
    match = next((p for p in listing if p.name == name), None)
    if match is not None and match.selectable:
        return name
    if match is not None:
        raise HerdrTeamError(
            "profile_not_selectable",
            "{} is a subagent: {} runs it inside another agent's session, never as a member. Choose: {}".format(name, kind, ", ".join(runnable) or "none defined"),
            EXIT_REFUSED, {"kind": kind, "profile": name, "profiles": runnable},
        )
    raise HerdrTeamError(
        "profile_unknown",
        "{} has no profile {!r} {}; choose: {} (herdr-synapse available {}; --unlisted to pass it anyway)".format(
            kind, name, "for this directory" if kind in ("opencode", "claude") else "here", ", ".join(runnable) or "none defined", kind),
        EXIT_REFUSED, {"kind": kind, "profile": name, "profiles": runnable, "suggestions": _suggest(name, runnable)},
    )
