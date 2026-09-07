# Development

Internals, conventions, and the status log for people changing the plugin. The user-facing overview is the [README](../README.md); the command contract is [cli.md](cli.md).

## Layout

```
herdr-plugin.toml        manifest (plan section 10): startup, 6 actions, 3 event hooks, 3 panes
bin/herdr-synapse           sh launcher: HERDR_TEAM_PYTHON > python3 > /usr/bin/python3, refuses < 3.9
bin/hook                 sh gate for manifest events: exit 0 in ~15 ms when daemon.json names a live pid
console.sh               console pane wrapper: prints the relaunch hint and waits on failure
herdr_team/
  __init__.py            VERSION, SKILL_VERSION, PLUGIN_ID
  errors.py              exit-code contract, HerdrTeamError, JSON error emission
  paths.py               state root, socket resolution, slug, session/team layout, pointer file
  api.py                 NDJSON socket client + `herdr` subprocess wrappers (the only door to Herdr)
  store.py               the three locks, atomic writes, BoardStore, Cursors, RosterStore, BoardTailer
  cli.py                 argparse root, Command dataclass, registry, dispatch
  cmd_*.py               command groups (board, roster, misc, hooks, skill, daemon, ui); each exports COMMANDS
  sanitize.py render.py identity.py roster.py charter.py     board and roster logic
  gate.py nudge.py ledger.py daemon.py                        notifier
  hooks.py claude_settings.py                                 event reconciler and Claude hooks
  tui_model.py console.py picker.py compose.py                UIs (curses console, picker, compose popups)
hooks/claude/            the Claude Code hook shim (written by the hooks implementer)
skills/herdr-synapse/SKILL.md   printed by `herdr-synapse --skill`
docs/cli.md              command contract
tests/                   unittest suite; tests/support.py has TempState, FakeHerdrServer, FakeApi
```

## Try it

The plugin runs on the installed Herdr 0.8.2; no fork build is needed. The
zero-interference way is the sandbox launcher, a named session with its own
config, registry, and state: from a terminal outside Herdr run
`bin/herdr-synapse-sandbox start`, then `prefix+t` inside it. Read
`docs/human-testing.md` (the guided first run) and `docs/capabilities.md`
(every capability, how to drive it from the UI and the CLI, what to expect,
and a test checklist). Short form: `herdr plugin link <this dir>`,
`herdr plugin action invoke herdr-synapse.daemon-start`, `herdr-synapse kinds trust
claude` (and `codex`), paste `herdr-synapse keys print` and `herdr-synapse setup
--print-config` into your config, reload, then `prefix+t` to pick two fresh
panes into a team. A post with no recipient goes to the whole team; `@name`
addresses one member and nudges it; your posts to the whole team nudge everyone,
an agent's only with `--urgent`. `!name text` types
the line into that member's input box right now (`!!name text` even while it
works) and records it as a `direct` post. `@@path` attaches a file to a post
(`@@` lists files); `?` on an empty line shows every command.

## Dev loop

```bash
python3 -m unittest discover -s tests -v            # Homebrew python (3.14)
/usr/bin/python3 -m unittest discover -s tests -v   # Apple python (3.9): both must pass
./bin/herdr-synapse --version
./bin/herdr-synapse --skill
./bin/herdr-synapse <command> --help
```

Rules for code in this package:

- Python 3.9 syntax: no `match`, no `X | Y` at runtime, `from __future__
  import annotations` first in every module, `tomllib` guarded. No third
  party packages; display width comes from `unicodedata.east_asian_width`.
- Every Herdr call goes through `herdr_team.api`. `HERDR_BIN_PATH` when set,
  never a bare `herdr`; every subprocess has `stdin=DEVNULL`, captured
  output, and a timeout. Every socket call has a client-side timeout. The
  one exemption is `charter edit`, which hands the terminal to
  `$VISUAL`/`$EDITOR` until the human quits it.
- Exactly three `flock` files, all through `herdr_team.store`:
  `<session>/daemon.lock`, `<team>/team.lock`,
  `~/.claude/settings.json.herdr-team.lock`. Nobody else imports `fcntl`.
- `time.monotonic()` for windows, rate limits, and backoffs; wall clock only
  for TTLs and timestamps.
- Directories 0700, files 0600, `lstat` before every managed path, symlinks
  refused, temp files in the target directory (`store.atomic_write`).
- Every command accepts `--json` and prints one JSON object; errors are one
  JSON object on stderr with the contract exit code (`docs/cli.md` section 2).
- Only the daemon calls `agent.prompt` and `notification.show`, and never on
  a terminal outside a roster.
  A human-only `say` job (the console's `!name text`) asks the daemon to type
  one recorded `direct` line now; agents cannot enqueue it and the daemon types
  only a record whose origin is the verified console.

## Test rig rules (summary of plan section 14)

The owner's Claude runs in `wA:p6` on the default session with
`HERDR_SOCKET_PATH` exported, so a bare `herdr` in any script reaches the
owner's 17 agents. Until milestone M10:

- Never link this plugin into `~/.config/herdr/plugins.json`. The rig has its
  own XDG config and state dirs and its own registry; it writes
  `allowed-sockets` so hooks and actions no-op elsewhere.
- Every Herdr call in rig scripts goes through `$RIG/bin/hr`, which clears
  `HERDR_SOCKET_PATH`, `HERDR_CLIENT_SOCKET_PATH`, `HERDR_WORKSPACE_ID`,
  `HERDR_TAB_ID`, `HERDR_PANE_ID`, sets the rig XDG dirs and
  `HERDR_SESSION=$(cat $RIG/session-name)`.
- Before any mutating step `hr status server` must show a socket under
  `$RIG/xdg-config/herdr/sessions/`. Never target a `wA:*` id.
- Never `herdr server stop`, never `pkill`; kill only the pid in
  `daemon.json` after checking its start time.
- Never write `~/.config/herdr/config.toml` or `agent-detection/`;
  `herdr integration install` needs owner approval.
- Clean up with `hr session stop` and `hr session delete` on names recorded
  in `$RIG/sessions-created` only.
- Unit tests never touch the real HOME, socket, or `~/.claude`; use
  `tests/support.TempState` and `FakeHerdrServer`.

## State layout

```
<STATE>/sessions/<slug>/                      slug: default | <session name> | sock-<sha1[:8]>
  daemon.lock daemon.json daemon.log hooks.log view.json console.json kinds.json who.json
  panes/<terminal_id>.json
  teams/<team>/ team.json team.lock board.seq board.jsonl charter.md archive/ cursors/ payloads/
                briefings/ notifier/{ledger.jsonl,state.json,jobs/} mute.json audit.jsonl
  _archive/<team>-<ts>/
<config_dir>/plugins/config/herdr-synapse/{state-dir, allowed-sockets}
```

State root resolution (`herdr-synapse doctor` prints it): `--team <path>` →
`HERDR_TEAM_STATE_DIR` → `HERDR_TEAM_DIR` → `HERDR_PLUGIN_STATE_DIR` →
pointer file → `${XDG_STATE_HOME:-$HOME/.local/state}/<app>/plugins/herdr-synapse`.

## Status

Integration pass of 2026-09-04 (the plugin was developed inside a fork of Herdr; this log moved here with it):

- 2026-09-05: `herdr-synapse say` and the console's `!name text` / `!!name text`
  type one line into a member now (docs/cli.md section 7, capabilities section
  7 and 11). Suite: 1141 tests green under both interpreters.

- Suite: 945 tests, green under Homebrew python 3.14.6 and Apple python
  3.9.6 (3 skips on 3.9). `tests/test_schema_conformance.py` (37 tests)
  checks fakes and call shapes against `tests/fixtures/herdr-api-0.8.2.schema.json`.
- Second review of that pass (`tests/test_review_findings_2.py`, 20 tests):
  a live row with `agent: null` (launch pending) is no evidence of another
  kind, so `roster.rehydrate_match` binds it by terminal and the daemon
  adopts ids only, keeping `starting`/`missing` until a kind is detected
  (the hook path's rule); `on_connected` runs its three phases inside the
  same `_phase` boundary as `tick`, so a `board_locked` at connect time is
  counted and retried by the grace-window poll instead of exiting the
  daemon; `config.gate.post_ttl_ms`, `pair_window_ms` and
  `sample_gap_reset_ms` now govern the TTL, the pair budget window and the
  per-team stability reset; `nudge_focused` is validated against
  `never|always`; `reconcile_console` treats an empty foreground as unknown
  and honours a 15 s `launched_at` grace stamped by every console open.
- Closed in this pass: gate config from `team.json` (`config.gate`, docs
  section 10) and the gate 11 follow-up exception; one rehydration matcher
  (`roster.rehydrate_match`) shared by the daemon and `bind`; the daemon
  loop survives one bad event, job, or member evaluation (`phase_errors`
  counter); pane entrypoints honour `allowed-sockets`; `inbox` and
  `notifier stats` documented; `agent start`, `plugin.pane.open`, and
  `agent.read` parsed by their real server shapes through `herdr_team.api`.
- Hygiene: `roster.join` reaches `ensure_daemon`, so its unit tests patch it
  (an unpatched call double-forks the unittest runner into a real daemon that
  retries against the dead temp socket for 60 s). The daemon now exits, and
  skips the final `daemon.json`, when its `<session>/` directory has been
  removed under it instead of recreating the directory.
- Rig M5 real-delivery pass (2026-09-05, cheap Claude haiku): Claude Code
  2.1.261 paints a prompt suggestion (faint "ghost text", SGR 2) inside the
  prompt box after every turn; the plain detection read cannot tell it from
  a typed draft, so gate 9 held every nudge and briefing as `draft_present`
  while it stayed on screen. The daemon now reads the visible viewport with
  styling (`agent.read --source visible --format ansi`) only when the plain
  text shows a draft and drops the faint runs on the prompt line
  (`gate.styled_prompt_line_text`, `tests/test_ghost_text.py`, live fixtures
  under `tests/fixtures/detection/claude_ghost_suggestion*`). Also observed:
  Herdr's session restore relaunches Claude as `claude --resume <id>`, dropping
  `--model`, `--effort`, `--allowedTools`, and `--settings` (hooks), so a
  restored member runs the owner's default model without the team hooks.
- Rig M7 UI pass (2026-09-05, `tests/test_m7_findings.py`, 9 tests): the
  `/peek` box is now cut from the head when taller than the feed, keeping the
  title border and the bottom of the member's screen (spinner, prompt box,
  status line) instead of blank leading rows (`tui_model.fit_peek`);
  `console.read_key` no longer collapses a burst of Escapes, or Esc followed
  by a control key or curses keycode, into one `ESC` (the extra byte is pushed
  back with `unget_wch`/`ungetch`), which had made six Escapes in the picker
  act as one and swallowed the `ctrl+u` behind them; `view toggle` under a
  foreign owner now means "turn ours on", so `--force` replaces the foreign
  view instead of clearing it when a stale `view.json` still said `on`. Live
  facts recorded: the picker's role field is prefilled with `<kind>-dev` and
  the name with `<team>-<role>`, so a driver must `ctrl+u` before typing;
  `dissolve` keeps the members' Herdr names (documented; `remove` clears
  them); `team_task` lands at the daemon's next heartbeat restamp (10 to 20 s
  after `task`), not at once; the console is a `split` per the manifest even
  though plan 7.3 says "opened as a tab".
- Open: `hooks._reconcile_detected` still carries its own per-pane matcher
  rather than `roster.rehydrate_match` (the two now agree on a null live
  kind, ids adopted and status kept on both paths, and on a different
  detected kind; they still differ on step (d): the daemon's exact-name
  match ignores the kind and reports `kind_changed`, the hook requires the
  kind to match); the gate 11 follow-up is only
  reachable with `done_hold_ms` below the 20 s interval, an owner decision;
  a console pane opened by Herdr directly from the manifest `[[panes]]`
  entry (not through `ui console`) carries no `launched_at`, so only the
  empty-foreground rule protects it while booting;
  `doctor` and `setup` send one `notification.show` probe themselves (plan
  7.2), the single non-daemon caller, which the rule above should either
  bless or the probe should move behind a daemon job.
