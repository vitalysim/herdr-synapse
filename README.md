# herdr-team

Teams of coding agents inside one Herdr session: a roster with unique
names, a human-owned charter, a shared append-only board, and a notifier
daemon that nudges members only when they are stably idle. Plugin for Herdr
0.8.2, Python 3.9+ standard library, macOS first.

Plan: `.local/prd/agent-teams-prototype-plan.md` in the fork checkout.
CLI contract: `docs/cli.md` (the authority for every command's arguments,
JSON shape, and exit code).

## Layout

```
herdr-plugin.toml        manifest (plan section 10): startup, 6 actions, 3 event hooks, 3 panes
bin/herdr-team           sh launcher: HERDR_TEAM_PYTHON > python3 > /usr/bin/python3, refuses < 3.9
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
skills/herdr-team/SKILL.md   printed by `herdr-team --skill`
docs/cli.md              command contract
tests/                   unittest suite; tests/support.py has TempState, FakeHerdrServer, FakeApi
```

## Dev loop

```bash
cd plugins/herdr-team
python3 -m unittest discover -s tests -v            # Homebrew python (3.14)
/usr/bin/python3 -m unittest discover -s tests -v   # Apple python (3.9): both must pass
./bin/herdr-team --version
./bin/herdr-team --skill
./bin/herdr-team <command> --help
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
<config_dir>/plugins/config/herdr-team/{state-dir, allowed-sockets}
```

State root resolution (`herdr-team doctor` prints it): `--team <path>` →
`HERDR_TEAM_STATE_DIR` → `HERDR_TEAM_DIR` → `HERDR_PLUGIN_STATE_DIR` →
pointer file → `${XDG_STATE_HOME:-$HOME/.local/state}/<app>/plugins/herdr-team`.

## Status

Integration pass of 2026-09-04 (fork status log lives in `FORK.md`):

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
