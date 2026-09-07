"""State root, session slug, socket resolution, and the on-disk layout.

Plan section 4.1. Everything here is pure path arithmetic over an injected
``env`` mapping (never ``os.environ`` implicitly) so tests never touch the
real HOME.

Socket resolution mirrors Herdr (``src/session.rs``): ``--socket`` (plugin
override, wins over everything), ``--session <name>``, ``HERDR_SOCKET_PATH``,
``HERDR_SESSION`` -> ``<config>/sessions/<name>/herdr.sock``, then
``<config>/herdr.sock``. The socket is canonicalized with ``realpath`` before
anything is derived from it.

Session slug, derived structurally from the canonical socket path (the sh
gate in ``bin/hook`` applies the same rule with parameter expansion):

* ``<dir>/sessions/<name>/herdr.sock`` -> ``<name>``
* ``<dir>/herdr.sock``                 -> ``default``
* anything else                        -> ``sock-<sha1(path)[:8]>``

State root resolution order (``herdr-team doctor`` prints it):

1. ``--team <path>`` when the value looks like a path: it names a team dir
   ``<root>/sessions/<slug>/teams/<team>`` and the root is derived from it.
2. ``HERDR_TEAM_STATE_DIR`` (test-rig override of the root itself).
3. ``HERDR_TEAM_DIR`` (a team dir, set on panes the plugin spawned).
4. ``HERDR_PLUGIN_STATE_DIR`` (plugin processes; this *is* the root).
5. Pointer file ``<config_dir>/plugins/config/herdr-team/state-dir`` written
   by the startup hook and the console.
6. XDG derivation ``${XDG_STATE_HOME:-$HOME/.local/state}/<app>/plugins/herdr-team``
   where ``<app>`` is the basename of the config dir derived from the socket
   (``herdr`` or ``herdr-dev``).

Layout::

    <STATE>/sessions/<slug>/
      socket  daemon.lock  daemon.json  daemon.log  hooks.log  view.json
      console.json  kinds.json  who.json  claims.lock  panes/<terminal_id>.json
      teams/<team>/
        team.json  team.lock  board.seq  board.jsonl  charter.md
        archive/index.json  archive/board.<a>-<b>.jsonl
        cursors/<name>.json  cursors/human@<label>.json
        payloads/  briefings/<name>.txt
        notifier/ledger.jsonl  notifier/state.json  notifier/jobs/
        notifier/human-attention.jsonl
        mute.json  audit.jsonl
      _archive/<team>-<ts>/
    <config_dir>/plugins/config/herdr-team/{state-dir, allowed-sockets}

Directories are created 0700, files 0600, every managed component is
``lstat``-checked and symlinks are refused (``path_symlink``).
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional

from herdr_team import PLUGIN_ID
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError

SOCKET_NAME = "herdr.sock"
DEFAULT_SLUG = "default"
DEFAULT_APP_DIR = "herdr"
SESSIONS_DIR = "sessions"

#: Team names are a directory name and a sidebar token value (Herdr caps token
#: values at 80). 32 matches the member-name family; a longer team costs room in
#: the derived member name, which ``roster.fit_member_name`` gives to the role.
TEAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}\Z")
#: Roles are descriptive labels, never a Herdr agent name on their own under
#: prefixed naming, so they get the room the 80-char token value allows.
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}\Z")


def _max_chars(pattern: "re.Pattern[str]") -> int:
    """The length a name pattern allows, read off the pattern itself so a limit
    change cannot leave an error message or a UI hint behind."""
    return int(pattern.pattern.split("{0,")[1].split("}")[0]) + 1


MAX_TEAM_CHARS = _max_chars(TEAM_NAME_RE)
MAX_ROLE_CHARS = _max_chars(ROLE_NAME_RE)
#: Herdr itself caps agent names at 32 bytes (``src/app/agents.rs``
#: ``valid_agent_name``), and a member name IS its Herdr agent name, so this
#: one is not ours to raise.
MAX_MEMBER_CHARS = 32
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")
#: Herdr terminal ids look like ``term_...``; keep file names strictly safe.
TERMINAL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\Z")
#: Cursor / briefing file stems: member names or ``human@<label>``.
FILE_STEM_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}\Z")

STATE_SOURCE_TEAM_ARG = "team-arg"
STATE_SOURCE_OVERRIDE = "env:HERDR_TEAM_STATE_DIR"
STATE_SOURCE_TEAM_DIR = "env:HERDR_TEAM_DIR"
STATE_SOURCE_PLUGIN = "env:HERDR_PLUGIN_STATE_DIR"
STATE_SOURCE_POINTER = "pointer-file"
STATE_SOURCE_XDG = "xdg"

SOCKET_SOURCE_ARG = "socket-arg"
SOCKET_SOURCE_SESSION_ARG = "session-arg"
SOCKET_SOURCE_ENV_SOCKET = "env:HERDR_SOCKET_PATH"
SOCKET_SOURCE_ENV_SESSION = "env:HERDR_SESSION"
SOCKET_SOURCE_DEFAULT = "default"

DIR_MODE = 0o700
FILE_MODE = 0o600


# --------------------------------------------------------------------------
# plugin root and basic dirs


def plugin_root() -> Path:
    """The plugin checkout: parent of the ``herdr_team`` package."""
    return Path(__file__).resolve().parent.parent


def skill_file() -> Path:
    return plugin_root() / "skills" / PLUGIN_ID / "SKILL.md"


def home_dir(env: Mapping[str, str]) -> Path:
    home = env.get("HOME")
    if not home:
        raise HerdrTeamError("home_unset", "HOME is not set and no XDG override applies", EXIT_REFUSED)
    return Path(home)


def app_dir_name(env: Mapping[str, str]) -> str:
    """``herdr`` unless ``HERDR_TEAM_APP_DIR`` says otherwise (``herdr-dev``)."""
    return env.get("HERDR_TEAM_APP_DIR") or DEFAULT_APP_DIR


def config_dir(env: Mapping[str, str]) -> Path:
    """Herdr's config dir as the CLI would compute it from this env."""
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / app_dir_name(env)
    return home_dir(env) / ".config" / app_dir_name(env)


def xdg_state_home(env: Mapping[str, str]) -> Path:
    xdg = env.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg)
    return home_dir(env) / ".local" / "state"


def canonicalize(path: "os.PathLike[str] | str") -> Path:
    """``realpath``: resolves every existing symlink component, keeps missing tails."""
    return Path(os.path.realpath(os.fspath(path)))


def looks_like_path(value: str) -> bool:
    """``--team`` and ``HERDR_TEAM_DIR`` take either a team name or a path."""
    return "/" in value or value.startswith(".") or value.startswith("~")


# --------------------------------------------------------------------------
# socket resolution


@dataclass(frozen=True)
class SocketResolution:
    path: Path  # canonical
    raw: Path  # as configured, before realpath
    source: str  # one of the SOCKET_SOURCE_* constants
    session_name: Optional[str]  # named session when one was used, else None
    explicit: bool  # --socket or --session given on the command line


def validate_session_name(name: str) -> Optional[str]:
    """Herdr's rule; ``default`` normalizes to ``None``."""
    if name == DEFAULT_SLUG:
        return None
    if not SESSION_NAME_RE.match(name):
        raise HerdrTeamError(
            "session_name_invalid",
            "session name may only contain ASCII letters, numbers, '.', '_' and '-'",
            EXIT_REFUSED,
            {"session": name},
        )
    return name


def socket_path_for(config: Path, session_name: Optional[str]) -> Path:
    if session_name is None:
        return config / SOCKET_NAME
    return config / SESSIONS_DIR / session_name / SOCKET_NAME


def resolve_socket(
    env: Mapping[str, str],
    session: Optional[str] = None,
    socket: Optional[str] = None,
) -> SocketResolution:
    """Mirror Herdr's socket resolution; ``socket`` is the plugin's own override."""
    if socket:
        raw = Path(os.path.expanduser(socket))
        return SocketResolution(canonicalize(raw), raw, SOCKET_SOURCE_ARG, None, True)
    if session is not None:
        name = validate_session_name(session)
        raw = socket_path_for(config_dir(env), name)
        return SocketResolution(canonicalize(raw), raw, SOCKET_SOURCE_SESSION_ARG, name, True)
    env_socket = env.get("HERDR_SOCKET_PATH")
    if env_socket:
        raw = Path(env_socket)
        return SocketResolution(canonicalize(raw), raw, SOCKET_SOURCE_ENV_SOCKET, None, False)
    env_session = env.get("HERDR_SESSION")
    if env_session:
        name = validate_session_name(env_session)
        raw = socket_path_for(config_dir(env), name)
        return SocketResolution(canonicalize(raw), raw, SOCKET_SOURCE_ENV_SESSION, name, False)
    raw = socket_path_for(config_dir(env), None)
    return SocketResolution(canonicalize(raw), raw, SOCKET_SOURCE_DEFAULT, None, False)


def session_name_from_socket(socket_path: Path) -> Optional[str]:
    """``<name>`` for ``.../sessions/<name>/herdr.sock``, else None."""
    if socket_path.name != SOCKET_NAME:
        return None
    parent = socket_path.parent
    if parent.parent.name == SESSIONS_DIR and SESSION_NAME_RE.match(parent.name):
        return parent.name
    return None


def session_slug(socket_path: "os.PathLike[str] | str") -> str:
    """Structural slug of the canonical socket path (see module docstring)."""
    path = canonicalize(socket_path)
    name = session_name_from_socket(path)
    if name is not None:
        return name
    if path.name == SOCKET_NAME:
        return DEFAULT_SLUG
    digest = hashlib.sha1(os.fspath(path).encode("utf-8", "surrogateescape")).hexdigest()
    return "sock-" + digest[:8]


def config_dir_for_socket(socket_path: "os.PathLike[str] | str") -> Path:
    """The Herdr config dir that owns this socket.

    ``<config>/herdr.sock`` -> ``<config>``;
    ``<config>/sessions/<name>/herdr.sock`` -> ``<config>``;
    anything else -> the socket's parent directory.
    """
    path = canonicalize(socket_path)
    if session_name_from_socket(path) is not None:
        return path.parent.parent.parent
    return path.parent


# --------------------------------------------------------------------------
# pointer file and allowed sockets


def plugin_config_dir(config: Path) -> Path:
    return config / "plugins" / "config" / PLUGIN_ID


def pointer_file(config: Path) -> Path:
    return plugin_config_dir(config) / "state-dir"


def allowed_sockets_file(config: Path) -> Path:
    return plugin_config_dir(config) / "allowed-sockets"


def read_pointer(config: Path) -> Optional[Path]:
    """State root recorded for this config dir, or None when absent or empty."""
    path = pointer_file(config)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise HerdrTeamError("path_symlink", "refusing symlinked pointer file", EXIT_REFUSED, {"path": str(path)})
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not line:
        return None
    return Path(line)


def write_pointer(config: Path, state_root: Path) -> Path:
    """Record ``state_root`` for this config dir; returns the pointer path."""
    from herdr_team import store  # local import: store depends on this module

    target = pointer_file(config)
    ensure_dir(target.parent)
    store.atomic_write(target, (os.fspath(state_root) + "\n").encode("utf-8"))
    return target


def socket_allowed(config: Path, socket_path: Path) -> bool:
    """``allowed-sockets`` absent means allow; present means the canonical path must be listed."""
    path = allowed_sockets_file(config)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return True
    if stat.S_ISLNK(st.st_mode):
        raise HerdrTeamError("path_symlink", "refusing symlinked allowed-sockets file", EXIT_REFUSED, {"path": str(path)})
    wanted = os.fspath(canonicalize(socket_path))
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if os.fspath(canonicalize(entry)) == wanted:
            return True
    return False


# --------------------------------------------------------------------------
# state root


@dataclass(frozen=True)
class StateRootResolution:
    path: Path
    source: str  # one of the STATE_SOURCE_* constants
    team_dir: Optional[Path] = None  # when the root was derived from a team dir


def validate_team_name(name: str) -> str:
    if not TEAM_NAME_RE.match(name or ""):
        raise HerdrTeamError(
            "team_name_invalid",
            "team name must match [a-z][a-z0-9_-]{0,31}",
            EXIT_REFUSED,
            {"team": name},
        )
    return name


def state_root_from_team_dir(team_dir: Path) -> Path:
    """``<root>/sessions/<slug>/teams/<team>`` -> ``<root>``; refuses other shapes."""
    team_dir = Path(os.path.expanduser(os.fspath(team_dir)))
    parts = team_dir.parts
    if (
        len(parts) >= 5
        and team_dir.parent.name == "teams"
        and team_dir.parent.parent.parent.name == SESSIONS_DIR
        and TEAM_NAME_RE.match(team_dir.name)
    ):
        return team_dir.parent.parent.parent.parent
    raise HerdrTeamError(
        "team_dir_invalid",
        "expected <state>/sessions/<slug>/teams/<team>",
        EXIT_REFUSED,
        {"path": os.fspath(team_dir)},
    )


def default_state_root(config: Path, env: Mapping[str, str]) -> Path:
    """XDG derivation: ``<xdg_state>/<app>/plugins/herdr-team`` with ``<app>`` from the config dir."""
    return xdg_state_home(env) / config.name / "plugins" / PLUGIN_ID


def resolve_state_root(
    env: Mapping[str, str],
    config: Path,
    team_path: Optional[str] = None,
) -> StateRootResolution:
    """Apply the resolution order from the module docstring."""
    if team_path and looks_like_path(team_path):
        team_dir = canonicalize(os.path.expanduser(team_path))
        return StateRootResolution(state_root_from_team_dir(team_dir), STATE_SOURCE_TEAM_ARG, team_dir)
    override = env.get("HERDR_TEAM_STATE_DIR")
    if override:
        return StateRootResolution(Path(override), STATE_SOURCE_OVERRIDE)
    team_dir_env = env.get("HERDR_TEAM_DIR")
    if team_dir_env:
        team_dir = Path(team_dir_env)
        return StateRootResolution(state_root_from_team_dir(team_dir), STATE_SOURCE_TEAM_DIR, team_dir)
    plugin_state = env.get("HERDR_PLUGIN_STATE_DIR")
    if plugin_state:
        return StateRootResolution(Path(plugin_state), STATE_SOURCE_PLUGIN)
    pointer = read_pointer(config)
    if pointer is not None:
        return StateRootResolution(pointer, STATE_SOURCE_POINTER)
    return StateRootResolution(default_state_root(config, env), STATE_SOURCE_XDG)


# --------------------------------------------------------------------------
# layout builders


def _safe_stem(value: str, what: str) -> str:
    if not FILE_STEM_RE.match(value or ""):
        raise HerdrTeamError("name_invalid", "{} contains characters not allowed in a file name".format(what), EXIT_REFUSED, {what: value})
    return value


@dataclass(frozen=True)
class TeamPaths:
    """Every managed path inside one team dir."""

    root: Path
    name: str

    @property
    def team_json(self) -> Path:
        return self.root / "team.json"

    @property
    def team_lock(self) -> Path:
        return self.root / "team.lock"

    @property
    def board_seq(self) -> Path:
        return self.root / "board.seq"

    @property
    def board_jsonl(self) -> Path:
        return self.root / "board.jsonl"

    @property
    def charter_md(self) -> Path:
        return self.root / "charter.md"

    @property
    def mirror_json(self) -> Path:
        """Digest of what the plugin last wrote to each editable mirror file.

        Without it a mirror that differs from what we would write now is
        ambiguous: the operator may have edited it, or the plugin's own format
        may have changed. Only the first is an edit to adopt.
        """
        return self.root / "mirror.json"

    @property
    def rules_md(self) -> Path:
        """The operator's rules for this team (read through ``charter.get_rules``).

        Named ``rules.md`` because the project mirror's ``knowledge.md`` is a
        different document: it holds these rules *plus* every member finding.
        Two files with one name confused agents told to "read knowledge.md".
        """
        return self.root / "rules.md"

    @property
    def knowledge_md(self) -> Path:
        """Where the rules lived before 0.6; read as a fallback, never written."""
        return self.root / "knowledge.md"

    @property
    def knowledge_jsonl(self) -> Path:
        """Append-only member findings, attributed; the rules live in ``knowledge_md``."""
        return self.root / "knowledge.jsonl"

    @property
    def instructions_dir(self) -> Path:
        return self.root / "instructions"

    def instructions(self, member_name: str) -> Path:
        """The authoritative copy of one member's long-form instructions."""
        return self.instructions_dir / (_safe_stem(member_name, "name") + ".md")

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    @property
    def archive_index(self) -> Path:
        return self.archive_dir / "index.json"

    def archive_segment(self, first_seq: int, last_seq: int) -> Path:
        return self.archive_dir / "board.{}-{}.jsonl".format(int(first_seq), int(last_seq))

    @property
    def cursors_dir(self) -> Path:
        return self.root / "cursors"

    def cursor(self, member_name: str) -> Path:
        return self.cursors_dir / (_safe_stem(member_name, "name") + ".json")

    def human_cursor(self, label: str) -> Path:
        return self.cursors_dir / ("human@" + _safe_stem(label, "label") + ".json")

    @property
    def payloads_dir(self) -> Path:
        return self.root / "payloads"

    @property
    def briefings_dir(self) -> Path:
        return self.root / "briefings"

    def briefing(self, member_name: str) -> Path:
        return self.briefings_dir / (_safe_stem(member_name, "name") + ".txt")

    @property
    def notifier_dir(self) -> Path:
        return self.root / "notifier"

    @property
    def ledger(self) -> Path:
        return self.notifier_dir / "ledger.jsonl"

    @property
    def notifier_state(self) -> Path:
        return self.notifier_dir / "state.json"

    @property
    def human_attention(self) -> Path:
        return self.notifier_dir / "human-attention.jsonl"

    @property
    def jobs_dir(self) -> Path:
        return self.notifier_dir / "jobs"

    @property
    def mute_json(self) -> Path:
        return self.root / "mute.json"

    @property
    def audit_jsonl(self) -> Path:
        return self.root / "audit.jsonl"

    def directories(self) -> List[Path]:
        """Every directory ``ensure_team_dirs`` creates, parents first."""
        return [
            self.root,
            self.archive_dir,
            self.cursors_dir,
            self.payloads_dir,
            self.briefings_dir,
            self.notifier_dir,
            self.jobs_dir,
            self.instructions_dir,
        ]


@dataclass(frozen=True)
class SessionPaths:
    """Every managed path inside one session dir."""

    root: Path
    slug: str

    @property
    def socket_file(self) -> Path:
        return self.root / "socket"

    @property
    def daemon_lock(self) -> Path:
        return self.root / "daemon.lock"

    @property
    def daemon_json(self) -> Path:
        return self.root / "daemon.json"

    @property
    def daemon_log(self) -> Path:
        return self.root / "daemon.log"

    @property
    def hooks_log(self) -> Path:
        return self.root / "hooks.log"

    @property
    def view_json(self) -> Path:
        return self.root / "view.json"

    @property
    def console_json(self) -> Path:
        return self.root / "console.json"

    @property
    def console_lock(self) -> Path:
        """Guards the console registry: several console processes write it now."""
        return self.root / "console.lock"

    @property
    def kinds_json(self) -> Path:
        return self.root / "kinds.json"

    @property
    def who_json(self) -> Path:
        return self.root / "who.json"

    @property
    def claims_lock(self) -> Path:
        # Plan 5.1 names ``claims.lock`` at the state root; the lock budget is
        # three files, so cross-team claims take ``team.lock`` of every team
        # involved in name order instead. Path kept for doctor output only.
        return self.root / "claims.lock"

    @property
    def operators_json(self) -> Path:
        """Which members the operator has delegated its authority to (``herdr_team.operator``)."""
        return self.root / "operators.json"

    @property
    def panes_dir(self) -> Path:
        return self.root / "panes"

    def pane_record(self, terminal_id: str) -> Path:
        if not TERMINAL_ID_RE.match(terminal_id or ""):
            raise HerdrTeamError("terminal_id_invalid", "terminal id is not file-name safe", EXIT_REFUSED, {"terminal_id": terminal_id})
        return self.panes_dir / (terminal_id + ".json")

    @property
    def teams_dir(self) -> Path:
        return self.root / "teams"

    @property
    def archive_dir(self) -> Path:
        return self.root / "_archive"

    def team(self, name: str) -> TeamPaths:
        return TeamPaths(self.teams_dir / validate_team_name(name), name)

    def team_archive(self, name: str, timestamp: str) -> Path:
        return self.archive_dir / "{}-{}".format(validate_team_name(name), _safe_stem(timestamp, "timestamp"))

    def list_teams(self) -> List[str]:
        """Team names with a ``team.json`` under ``teams/``, sorted."""
        try:
            entries = sorted(os.listdir(self.teams_dir))
        except OSError:
            return []
        found = []
        for entry in entries:
            if TEAM_NAME_RE.match(entry) and (self.teams_dir / entry / "team.json").is_file():
                found.append(entry)
        return found

    def directories(self) -> List[Path]:
        return [self.root, self.panes_dir, self.teams_dir, self.archive_dir]


def session_paths(state_root: Path, slug: str) -> SessionPaths:
    if not SESSION_NAME_RE.match(slug or "") and not slug.startswith("sock-"):
        raise HerdrTeamError("session_slug_invalid", "session slug is not file-name safe", EXIT_REFUSED, {"slug": slug})
    return SessionPaths(Path(state_root) / SESSIONS_DIR / slug, slug)


@dataclass(frozen=True)
class Layout:
    """Everything one invocation needs to know about where things live."""

    env_socket: SocketResolution
    config_dir: Path
    state_root: StateRootResolution
    session: SessionPaths

    @property
    def socket(self) -> Path:
        return self.env_socket.path

    @property
    def slug(self) -> str:
        return self.session.slug

    def team(self, name: str) -> TeamPaths:
        return self.session.team(name)

    def to_json(self) -> dict:
        return {
            "socket": os.fspath(self.socket),
            "socket_source": self.env_socket.source,
            "session_name": self.env_socket.session_name,
            "config_dir": os.fspath(self.config_dir),
            "state_root": os.fspath(self.state_root.path),
            "state_root_source": self.state_root.source,
            "slug": self.slug,
            "session_dir": os.fspath(self.session.root),
        }


def resolve_layout(
    env: Mapping[str, str],
    session: Optional[str] = None,
    socket: Optional[str] = None,
    team: Optional[str] = None,
) -> Layout:
    """Resolve socket, config dir, state root, and session dir for one call.

    When ``team`` is a path the slug comes from that path (the caller may be
    outside Herdr with no socket at all); otherwise from the socket.
    """
    sock = resolve_socket(env, session=session, socket=socket)
    config = config_dir_for_socket(sock.path)
    root = resolve_state_root(env, config, team_path=team)
    if root.team_dir is not None:
        slug = root.team_dir.parent.parent.name
    else:
        slug = session_slug(sock.path)
    return Layout(sock, config, root, session_paths(root.path, slug))


def team_name_from_arg(team: Optional[str], env: Mapping[str, str]) -> Optional[str]:
    """The team *name* implied by ``--team``/``HERDR_TEAM_DIR``/``HERDR_TEAM``, or None."""
    if team:
        if looks_like_path(team):
            return validate_team_name(Path(os.path.expanduser(team)).name)
        return validate_team_name(team)
    team_dir = env.get("HERDR_TEAM_DIR")
    if team_dir:
        return validate_team_name(Path(team_dir).name)
    name = env.get("HERDR_TEAM")
    if name:
        return validate_team_name(name)
    return None


# --------------------------------------------------------------------------
# filesystem hygiene


def check_not_symlink(path: "os.PathLike[str] | str") -> None:
    """Raise ``path_symlink`` when ``path`` exists and is a symlink."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise HerdrTeamError("path_symlink", "refusing to use a symlinked path", EXIT_REFUSED, {"path": os.fspath(path)})


def ensure_dir(path: "os.PathLike[str] | str", mode: int = DIR_MODE) -> Path:
    """``mkdir -p`` with ``mode`` on every component this call creates.

    Existing components are accepted as they are, except that the final
    component and any component created here are refused when they are
    symlinks or non-directories.
    """
    target = Path(path)
    missing: List[Path] = []
    cursor = target
    while True:
        try:
            st = os.lstat(cursor)
        except FileNotFoundError:
            missing.append(cursor)
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
            continue
        if stat.S_ISLNK(st.st_mode):
            if cursor == target or missing:
                raise HerdrTeamError("path_symlink", "refusing symlinked directory", EXIT_REFUSED, {"path": os.fspath(cursor)})
        elif not stat.S_ISDIR(st.st_mode):
            raise HerdrTeamError("path_not_directory", "expected a directory", EXIT_REFUSED, {"path": os.fspath(cursor)})
        break
    for component in reversed(missing):
        try:
            os.mkdir(component, mode)
        except FileExistsError:
            check_not_symlink(component)
            continue
        try:
            os.chmod(component, mode)
        except OSError:
            pass
    return target


def ensure_session_dirs(session: SessionPaths) -> None:
    """Create the session tree, refusing symlinks below the state root."""
    for directory in session.directories():
        ensure_dir(directory)


def ensure_team_dirs(team: TeamPaths) -> None:
    for directory in team.directories():
        ensure_dir(directory)
