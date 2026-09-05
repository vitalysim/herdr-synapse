# herdr-team CLI contract

This file is the authority implementers code against. Plan section 5.2 lists
the inventory; this document fixes arguments, JSON output shapes, and error
codes. When an implementer must deviate, change this file in the same
change and say why in the commit message.

Conventions used below:

- `<team>` is optional wherever the team can be inferred (member pane token,
  `HERDR_TEAM_DIR`, `HERDR_TEAM`, `--team`, or `default_team` from
  `console.json`). Two teams and no hint: `team_ambiguous`.
- `record` means a board record exactly as stored (plan 6.1, schema v1).
- `member` means the roster member object from `team.json` (plan 5.1) with
  `brief` included only in `who --json` and `me`.
- Timestamps are ISO-8601 UTC with milliseconds, e.g. `2026-09-04T13:53:10.123Z`.

## 1. Invocation

```
herdr-team [--json] [--team NAME|PATH] [--session NAME] [--socket PATH] [--session-mismatch-ok] <command> [args]
herdr-team --version [--json]
herdr-team --skill [--json]
```

Global flags are accepted before or after the command name.

| Flag | Meaning |
| --- | --- |
| `--json` | Print exactly one JSON object on stdout. Human output otherwise. |
| `--team NAME` | Team name. `--team PATH` (contains `/`, or starts with `.` or `~`) is a team directory `<state>/sessions/<slug>/teams/<team>`; the state root and slug derive from it, so this works outside Herdr with no socket. |
| `--session NAME` | Named session, mirrors `herdr --session`. `default` means the default session. |
| `--socket PATH` | Socket override. Wins over `--session`, `HERDR_SOCKET_PATH`, `HERDR_SESSION`. |
| `--session-mismatch-ok` | Allow a write (`post`, `retract`, `edit`, `task`, `ack`, `charter set|edit`, `brief --set`, `use`, `rename`, `remove`, `bind`, `dissolve`) to a team whose `team.json` socket differs from the resolved socket. Without it such a write is refused with `team_session_mismatch` (plan 12, RS-08) whenever the socket was resolved explicitly (`HERDR_SOCKET_PATH`, `HERDR_SESSION`, `--session`); `--socket` counts as consent; the default-socket fallback outside Herdr (`--team <path>`, nothing configured) is not checked so the offline append of HP-05 keeps working. `add` checks always (as before). Reads never check. |
| `--version` | `herdr-team 0.1.0`; JSON `{"version","skill_version","plugin_id"}`. |
| `--skill` | Prints `skills/herdr-team/SKILL.md`; JSON `{"skill","skill_version"}`. |

Environment the CLI reads: `HERDR_SOCKET_PATH`, `HERDR_SESSION`,
`HERDR_BIN_PATH` (never a bare `herdr` when set), `HERDR_PANE_ID`,
`HERDR_WORKSPACE_ID`, `HERDR_TAB_ID`, `HERDR_PLUGIN_*`, `HERDR_TEAM_STATE_DIR`
(rig override of the state root), `HERDR_TEAM_DIR`, `HERDR_TEAM`,
`HERDR_TEAM_ROLE`, `HERDR_TEAM_MEMBER`, `HERDR_TEAM_HUMAN`, `HERDR_TEAM_HOOKS`,
`HERDR_TEAM_PYTHON` (launcher only), `HERDR_TEAM_DRY_NUDGE` (daemon tests),
`XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `HOME`.

## 2. Exit codes

| Code | Meaning | Typical error codes |
| --- | --- | --- |
| 0 | ok | |
| 1 | refused or validation failure | `team_name_invalid`, `name_invalid`, `name_reserved`, `role_invalid`, `agent_name_taken`, `member_claimed`, `team_exists`, `team_not_found`, `member_not_found`, `author_mismatch`, `text_too_long`, `invalid_utf8`, `charter_too_long`, `ref_invalid`, `path_symlink`, `team_session_mismatch`, `team_ambiguous`, `view_foreign`, `plugin_disabled`, `agent_not_found`, `agent_blocked`, `agent_not_ready`, `launch_pending`, `not_an_agent`, `board_write_failed`, `roster_conflict`, `home_unset`, `internal` |
| 2 | usage | `usage`, `unknown_command` |
| 3 | not a member, or Herdr unreachable | `not_a_member`, `team_required`, `server_not_running`, `herdr_unreachable`, `herdr_timeout`, `herdr_not_found` |
| 4 | echo rejected | `echo_rejected` |
| 5 | daemon down, or lock timeout | `daemon_down`, `lock_timeout`, `board_locked`, `settings_locked` |

Every error is one JSON object on stderr regardless of `--json`:

```json
{"code": "board_locked", "message": "post NOT written: could not acquire team.lock within 5s", "lock": "/…/team.lock"}
```

`code` is stable; `message` is for humans; extra keys are details. The JSON
object is always the first stderr line; usage errors without `--json` add
the usage text on the following lines. Warnings (unverified author, stale skill, notifier offline) go to
stderr as plain lines prefixed `warning:` and never change the exit code.

Commands never print anything but the JSON object on stdout in `--json`
mode. Cursor-advancing commands flush stdout before advancing.

## 3. Author resolution

Every command that writes resolves the author first (plan 4.3). The result
is recorded in `post` output and in records' `from` / `origin`:

| Situation | `from` | `origin.via` | `verified` |
| --- | --- | --- | --- |
| hook or startup process (`HERDR_PLUGIN_EVENT` / `HERDR_PLUGIN_ID` without an entrypoint) | `system` | `system` | true |
| console pane, focused at Enter | `human` | `console` | true |
| console pane, unfocused | `human` | `console-unfocused` | false |
| shell pane inside Herdr, process group verified | `human` | `cli` | true |
| non-agent pane, ancestry mismatch | `human` | `cli-unverified` | false |
| compose popup | `human` | `popup` | false |
| outside Herdr (`--team` / `--session` required) | `human` | `outside` | false |
| member pane, roster match by `terminal_id` | `<member>` | `cli` | true |
| member pane, `HERDR_TEAM_MEMBER` only | `<member>` | `cli` | false |
| agent pane not in any team | error `not_a_member` (3) | | |
| `--as human` from an agent pane | error `author_mismatch` (1), audited | | |
| `--relayed-for human` from a member pane | `<member>`, record `relayed_for: "human"` | `cli` | as member |

## 4. Roster

### `create <team> [options]`

```
create <team> [--charter "<text>" | --charter-file <path>] [--ref <path>]…
       --member <target>[:<role>[:<name>]]… [--brief <name>="<text>"]…
       [--from-workspace <id>] [--names plain] [--rename] [--steal] [--reuse]
create <team> --new [--workspace] [--charter …] --spawn <role>:<kind>[:<cwd>]… [--names plain]
```

- `<target>`: pane id or live agent name. Role defaults to the kind label.
  Name defaults to `<team>-<role>` (`--names plain` uses `<role>`).
- Plan 5.5 (role defaults to the kind label) and plan 12 (a role equal to a
  kind label is refused) are resolved thus: a kind label is an acceptable
  role only under prefixed naming, where the member name stays
  `<team>-<kind>` (`t-claude`, `t-claude-2`) and can never be mistaken for
  a kind. With `--names plain` the name would *be* the kind label, so the
  default role falls back to `agent`, `agent2`, ... and an explicit kind
  label role is refused. Reserved words (`human`, `all`, `me`, ...) are
  refused as roles always.
- Validates team name, every role, every name (grammar, reserved words, kind
  labels, live-name collisions, duplicates within the call) before renaming,
  labelling, or stamping anything. A failure leaves no half-named team.
- Per target: `agent get` (refuse `agent_not_found` with a `pane get`
  cross-check, `agent_target_ambiguous`, `launch_pending`), claim check
  (`member_claimed` unless `--steal`), `agent rename` unless already named
  and `--rename` absent, `pane rename team:<team>/<role>`, append to the
  roster, stamp tokens, `ensure_daemon()`, enqueue the briefing job.
- `--new`: one `layout.apply` over the socket, then `agent start` per leaf.
- Sets `default_team` when it is the only team; asks (or refuses without
  `--use`) when a second team would change it.

JSON:

```json
{"team":"vuln-hunt","team_dir":"…/teams/vuln-hunt","created":true,
 "members":[{"name":"vuln-hunt-reviewer","role":"reviewer","kind":"codex","pane_id":"w2:p1","terminal_id":"term_…","status":"active","renamed":true}],
 "charter":{"seq":1,"headline":"…"},"notifier":"alive","default_team":true,"briefing_jobs":["…"]}
```

Errors: `team_name_invalid`, `team_exists` (unless `--reuse`), `role_invalid`,
`name_invalid`, `name_reserved`, `agent_name_taken` (details `candidates`),
`member_claimed` (details `owner_team`), `agent_not_found`, `not_an_agent`,
`launch_pending`, `charter_too_long`, `server_not_running` (3).

### `add <team> <target> [--role <r>] [--as <name>] [--brief "<text>"] [--steal]`

Same join routine for one member. JSON `{"team","member":member,"renamed":bool,"notifier","briefing_job"}`.

### `remove <team> <name> [--keep-name]`

Clears the three tokens and the pane label, marks the member `left`
(tombstone kept for addressing history), posts `member_gone`. JSON
`{"team","removed":"<name>","tokens_cleared":true,"name_cleared":bool}`.

### `leave`

From a member pane: `remove` on self. JSON `{"team","left":"<name>"}`.

### `bind <team> <name> <target>`

Re-attach a `missing`/`unbound`/`kind_changed` member to a live agent
(rehydration case (e) or a manual fix). JSON
`{"team","member":member,"previous_terminal_id":"…"}`. When the member moves
to another pane, its previous pane loses the `team:<team>/<role>` label if
that pane still carries it and hosts no agent (after a cold restart it is a
plain shell; RT-02). The daemon applies the same rule when a reconcile
rebinds a member to another terminal.

### `dissolve <team> [--yes]`

Clears tokens, labels, and the view for every member, stops nothing else,
moves the team dir to `_archive/<team>-<ts>/`. JSON
`{"team","archived_to":"…","members_cleared":n}`.

### `use <team>`

Sets `default_team` in `console.json`. JSON `{"default_team":"<team>"}`.

### `teams`

Works offline. JSON:

```json
{"teams":[{"team":"vuln-hunt","members":3,"socket":"…","running":true,"default":true,"team_dir":"…"}],"session":"default"}
```

### `rename <old> <new>`

`agent rename` plus roster update plus a board note; the old name resolves
for 10 minutes. JSON `{"team","old","new"}`.

## 5. Charter and briefs (human only)

Every write here refuses `author_mismatch` from an agent pane, a hook, or
`--as human`, and appends to `audit.jsonl`.

### `charter [<team>]`

JSON `{"team","charter":{"seq":3,"text":"…","refs":["charter.md"],"updated_at":"…","updated_by":"human"}|null}`.
Human output prints the text, then `refs:` lines.

### `charter set "<text>" | --file <path> [--ref <path>]… [--urgent]`

Sanitizes, refuses over 2000 chars with `charter_too_long` (hint:
`--charter-file`), copies `--file` into the team dir as `charter.md` and
adds it to `refs`, bumps `charter.seq`, appends a `system` record
`charter_updated` to `all` carrying the text; `--urgent` makes it a nudge to
every member. JSON `{"team","charter":{…},"record_seq":57,"urgent":false}`.

### `charter edit`

Opens `$VISUAL`/`$EDITOR` (shell-split, so `code --wait` works; `vi` when
unset) on a temp copy in the caller's pane, then behaves like
`charter set --file`. Refused without a TTY (`no_tty`); an editor that
cannot be started or exits non-zero is `editor_failed` (exit 1). The editor
is the one interactive subprocess exempt from the `stdin=DEVNULL` and
timeout rule: it must own the terminal until the human quits it.

### `charter history`

JSON `{"team","history":[{"seq":57,"ts":"…","charter_seq":3,"text":"…","refs":[…]}]}` oldest first.

### `brief <name> --set "<text>"`

Sets the member's role brief (≤ 300 chars delivered in the briefing line;
the rest via `me`). JSON `{"team","member":"<name>","brief":"…"}`.

## 6. Self and roster views

### `me`

From a member pane. JSON:

```json
{"team":"vuln-hunt","name":"vuln-hunt-reviewer","role":"reviewer","kind":"codex","pane_id":"w2:p1","terminal_id":"term_…",
 "brief":"…"|null,"charter":{"seq":3,"headline":"…"}|null,"teammates":[{"name","role","kind","status"}],
 "unread":2,"cursor":41,"verified":true,"via":"cli","skill_version":1,"skill_installed":1|null,"skill_ok":true,
 "cli":"/abs/path/herdr-team","notifier":"alive"}
```

Errors: `not_a_member` (3) with `hint` details listing known teams.

### `who [--brief] [--role <r>] [--ascii]`

Reads `who.json`; falls back to `agent list` only when the daemon pid is
dead or the beat is older than two intervals (`source` says which). Never
calls `agent read`. JSON:

```json
{"team":"vuln-hunt","source":"who.json","daemon":{"alive":true,"beat_age_s":4.2,"pid":4021},
 "charter":{"seq":3,"headline":"…","text":"…","refs":[…]}|null,"default_team":true,"view":"on",
 "toasts":"terminal","nudges":"on"|"paused","unread_for_you":1,
 "members":[{"name":"vuln-hunt-reviewer","role":"reviewer","kind":"codex","status":"active","agent_status":"idle",
   "pane_id":"w2:p1","terminal_id":"term_…","workspace_id":"w2","last_headline":"→ review diff","pending_nudges":0,
   "muted_until":null,"verified_kind":true,"delivery":"nudge","hooks_last_seen":null,"last_seen_at":"…",
   "briefed":true,"charter_stale":false,"unread":0,"brief":"…"|null}],
 "kinds":{"claude":{"trusted":true,"verified":false,"probe_ok":false,"multiline":{"one_submission":true,…}|null}}}
```

`kinds` has one entry per agent kind on the roster (never `human`), read
from the session's `kinds.json` (section 10): the three trust flags gate 4
uses and the `multiline` paste-probe record, or `null` when the kind was
never probed. `--role` filters `members` only; `kinds` always covers the
whole roster (M6 SK-03).

`--role` filters `members`; `--brief` drops `brief`, `last_seen_at`,
`hooks_last_seen` from human output only. Human rows are glyph, name, role,
kind, pane, status, headline, tags (the role column is added to the plan 11
layout so a member reading `who` can say who does what; SK-02).

### `audit [--last N]`

JSON `{"team","entries":[{"ts","event":"author_mismatch","author":"…","via","pane_id","details":{…}}]}`.

## 7. Board

### `post "<text>" [options]`

```
post "<text>" [--to <name>[,<name>…] | all | human | role:<r>] [--kind note|request|handoff|done|blocked|question|answer]
     [--ref <path>]… [--attach <path>]… [--reply-to <seq>] [--urgent] [--spill] [--as human] [--name <label>]
     [--relayed-for human] [--to-any]
```

- Default `--to`: `all` (the whole team) for every author, member or human.
  Address one member with `--to <name>`, the operator with `--to human`, and
  a role with `--to role:<r>`. Only directed posts trigger a nudge; a post to
  `all` is read at the next board read (or nudged to everyone with `--urgent`).
- `--to` names are validated against the roster (current names, names
  retired under 10 min, `role:<r>` expands and records `to_role`); a typo is
  `recipient_unknown` (1) with `roster` in details unless `--to-any`.
- Text: strict UTF-8 (`invalid_utf8`), sanitized, ≤ 2000 chars
  (`text_too_long`, hint `--spill` writes `payloads/<seq>-body.md`), marker
  check (`echo_rejected`, 4), secret patterns refused without `--force`.
- `--ref` must exist under the team dir, `payloads/`, or a roster member's
  cwd; `--attach` copies into `payloads/` (16 MiB cap) with a safe basename.
- Works with the server down (author unverified). Never calls
  `notification.show`.

JSON `{"seq":42,"team":"vuln-hunt","notifier":"alive|offline","to":["reviewer"],"to_role":null,"kind":"request","author":{"name":"builder","via":"cli","verified":true},"spilled":false,"attached":[]}`.

### `board [options]`

```
board [--new | --peek] [--to me | --from <name> | --kind <k> | --thread <seq> | --since <seq> | --last N]
      [--receipts] [--format text|json|context] [--limit N] [--max-bytes N] [--max N]
```

- Default (no `--new`/`--peek`): `--last 30`, no cursor change.
- `--new`: posts to me or `all` since my cursor, `--limit 100`, 32 KiB cap,
  advances the cursor to the highest seq printed after stdout is flushed.
- `--peek`: same selection, never advances. Hooks only peek.
- `--format context`: the Claude hook format (fixed header line, fenced
  posts). `--max 20 --max-bytes 4096` defaults in that format.
- `--receipts`: adds `nudged` (board `system` records) and `read` (cursors).
- Lines the store could not use are reported, never hidden (F-03): JSON
  gains `"skipped": {"corrupt": n, "fragment": n, "duplicates": n, "grammar": n}`
  when any count is non-zero; human and context output print
  `warning: board: skipped N line(s): …` on stderr. The exit code stays 0.

JSON:

```json
{"team":"vuln-hunt","reader":"vuln-hunt-reviewer","cursor":{"before":40,"after":42,"advanced":true},
 "posts":[record…],"truncated":false,"count":2,
 "receipts":{"42":{"nudged":["…ts"],"read":["vuln-hunt-reviewer"],"read_by":"1/2"}}}
```

### `show <seq> [--cat]`

JSON `{"team","post":record,"refs":[{"path":"payloads/42-diff.md","bytes":1234,"content":"…"|null,"truncated":false}]}`.
`--cat` inlines refs under `payloads/` or roster roots only, 64 KiB each.

### `retract <seq>`

Appends a `retract` record; the daemon cancels pending nudges. Own posts
only unless human. JSON `{"team","seq":58,"retracts":42}`.

### `edit <seq> "<text>"`

Appends a new record with `supersedes`. JSON `{"team","seq":59,"supersedes":42}`.

### `task "<text>"`

Sets the member's current task headline (24 columns, refreshed as the
`team_task` token by the daemon). JSON `{"team","member","task":"…","set_at":"…"}`.

### `ack`

Always rewrites the member's cursor file (`touch`), even when the seq does not move: the
daemon recognises an acknowledgement by a cursor write made after the briefing landed, and
a member whose cursor already sits at the board max (the join puts it there) would
otherwise never produce one and be re-briefed after 90 s (sandbox, 2026-09-05).

Records the member's cursor at the current max and `charter_seq_acked`.
JSON `{"team","member","cursor":59,"charter_seq_acked":3}`.

### `inbox --human [--last N] [--since <seq>]`

What the human has not seen (plan 7.2): board posts addressed to `human`
(`to` contains `human`, `from` is not `human`) plus the daemon's
`notifier/human-attention.jsonl`, the mirror of every toast it tried to show.
Read-only: never advances a cursor, never calls `notification.show`, works
with the server down. Registered by `cmd_misc.py`.

- `--last N` (default 30): the newest N posts and the newest N attention
  entries; `0` prints none of either.
- `--since <seq>`: only posts with `seq` above this, applied before `--last`.
- `reader` is `human@<label>` from a human path (`human@human` without a
  console label) and the member name when run from a member pane; `cursor` is
  that reader's cursor seq (0 when none) and `unread` counts the printed
  posts above it.
- `attention` entries are the daemon's `_mirror_toast` records, one per
  toast attempt: `reason` is the `notification.show` verdict (`shown`,
  `busy`, `rate_limited`, `disabled`, `no_foreground_client`, …), `shown`
  whether the toast was displayed, `kind` the notification kind. Malformed
  lines are skipped.

JSON:

```json
{"team":"vuln-hunt","reader":"human@human","cursor":40,"unread":1,
 "posts":[record…],
 "attention":[{"ts":"…","team":"vuln-hunt","seqs":[42],"title":"#42 question from builder","body":"…","reason":"shown","shown":true,"kind":"post"}]}
```

Human output: the posts rendered exactly like `board` (or the line
`no posts to human`), then, when the file has entries, `-- attention (n):`
followed by one indented line per entry, `<ts> <kind> [<reason>] <title>`.

## 8. Delivery (enqueue to the daemon; exit 5 `daemon_down` when it is dead)

| Command | Effect | JSON |
| --- | --- | --- |
| `brief <name>` | enqueue a briefing job (full gate) | `{"team","member","job":"<id>"}` |
| `nudge <name> [--force]` | enqueue an immediate nudge evaluation; `--force` skips `done_hold` and the interval, never the gate | `{"team","member","job"}` |
| `mute <name> \| --all [--for 10m]` | write `mute.json`; daemon and Claude shim honour it | `{"team","muted":{"*":"<until>\|null,"<name>":"<until>"}}` |
| `unmute <name> \| --all` | | same shape |
| `pause` | alias `mute --all` | same shape |
| `focus <name>` | enqueue `agent.focus` on the member's current pane | `{"team","member","job"}` |
| `read <name> [--lines N]` | `agent read --source visible` directly; `--lines` is refused for every member (`lines_refused`, exit 1), because scrolling an idle alternate screen types keys into the agent and only the daemon may type into a member | `{"team","member","pane_id","lines":[…]}` |

Job files: `notifier/jobs/<ts>-<id>.json` `{"v":1,"kind":"brief|nudge|focus","member","force","requested_by":author,"requested_at"}`.

## 9. Plugin and setup

### `daemon start [--replace] [--allow-version]`

Detaches the daemon (plan 8.1). Second start with a live daemon exits 0
`already_running`. `--replace` waits up to 10 s for the lock then SIGTERMs
the pid in `daemon.json` (start-time verified). JSON
`{"session_dir","started":true|false,"already_running":bool,"replaced":bool,"daemon":{"pid","start_time","socket","herdr_version","protocol"}}`.
Errors: `herdr_version_mismatch` (1) without `--allow-version`,
`socket_not_allowed` exits 0 with `{"skipped":"socket_not_allowed"}`.

### `daemon stop`

JSON `{"stopped":bool,"pid":N|null}`.

### `daemon status`

JSON `{"alive":bool,"pid","start_time","beat_age_s","socket","socket_source","herdr_version","protocol","version","teams":["…"],"pending":{"<team>":n},"ledger":{"wrong_target":0,"landed_working":n,…}}`.

### `notifier stats [--team NAME] [--kind KIND]`

Delivery-ledger statistics (plan 8.3) read straight from
`notifier/ledger.jsonl`; works without the daemon and without a socket.
`--team` restricts to one team (`team_not_found`, 1, with `teams` in
details when it does not exist), otherwise every team in the session is
listed. `--kind` keeps only that agent kind's bucket under `kinds`.
Registered by `cmd_daemon.py`.

JSON:

```json
{"session_dir":"…/sessions/default",
 "teams":{"vuln-hunt":{
   "counts":{"intents":12,"open_intents":0,"outcomes":11,"cursor_advanced":10,
             "landed_working":10,"landed_in_turn":1,"not_submitted":0,"wrong_occupant":0,"wrong_target":0,
             "hung":0,"refused":0,"transient":1,"dry":0},
   "kinds":{"codex":{"round_trips":11,"clean":10,"clean_rate":0.9,"verified":true}},
   "path":"…/notifier/ledger.jsonl"}},
 "wrong_target":0}
```

- `counts` always carries every result key (`landed_working`,
  `landed_in_turn`, `not_submitted`, `wrong_occupant`, `wrong_target`,
  `hung`, `refused`, `transient`, `dry`) plus `intents`, `open_intents`
  (intents without a result yet), `outcomes`, and `cursor_advanced`.
- `kinds.<kind>.round_trips` counts attempts with a result; `clean` those
  that landed while working with the cursor advancing within 5 min on the
  first attempt; `clean_rate` is the clean share of the last 20 non-`dry`
  round trips, `null` below that window; `verified` is `clean_rate >= 0.9`.
- Top-level `wrong_target` sums the teams' counters and must stay 0.

Human output: one `team <name>: intents n open n landed_working n
landed_in_turn n transient n wrong_target n` line per team, then one
indented `<kind>: n round trips, clean rate 85%|n/a [(verified)]` line per
kind; `no teams in <session_dir>` when there is nothing to report.

### `doctor`

Never fails on warnings; `ok:false` only on hard problems. JSON:

```json
{"ok":true,"version":"0.1.0","python":"3.9.6",
 "herdr":{"bin":"/…/herdr","version":"0.8.2","protocol":20,"reachable":true},
 "socket":{"path":"…","source":"env:HERDR_SOCKET_PATH","session_name":null,"allowed":true},
 "slug":"default","config_dir":"…",
 "state_root":{"path":"…","source":"pointer-file","candidates":[{"source":"env:HERDR_PLUGIN_STATE_DIR","path":null},…]},
 "pointer":"…/plugins/config/herdr-team/state-dir"|null,
 "plugin":{"installed":true,"enabled":true,"path":"…","warnings":[]},
 "toast_delivery":"terminal","daemon":{…as daemon status…},
 "teams":[{"team","members","missing":n}],"console":{"open":bool,"pane_id"},
 "warnings":["…"],"errors":["…"]}
```

### `setup --print-config`

JSON `{"required":"<toml>","optional":"<toml>","notes":[…],"toast_delivery":"terminal"|"herdr"|"off"|null,"toast_probe":{"probed":bool,"shown":bool,"reason":"…"}}`.
Never edits config. Like `doctor`, it issues one `notification.show` probe
(plan 7.2) and prints the effective toast mode; `--no-probe` skips it, and an
unreachable or unlisted socket reports `probed:false` with the reason instead
of failing, so the config blocks always print.

### `keys print` / `keys check`

`print` → `{"snippet":"<toml>","keys":{"team-up":"prefix+t","compose":"prefix+m","console":"prefix+u","toggle-view":"prefix+y"}}`.
`check` runs `herdr config check` → `{"ok":bool,"collisions":[{"key","bound_to"}],"output":"…"}`.

### `skill install [--force]` / `skill check`

JSON `{"version":1,"ok":bool,"installed":[{"path","kind":"copy|symlink"}],"skipped":[{"path","reason"}],"stale":[…]}`.
Refuses to replace a foreign directory or symlink without `--force`.

### `hooks install|uninstall|probe|check <kind>`

JSON `{"kind":"claude","action":"install","settings":"~/.claude/settings.json","hook":"~/.claude/hooks/herdr-team-hook.sh","added":["SessionStart","UserPromptSubmit","Stop"],"removed":[],"already":[],"backup":"…","duplicates":[…],"members_updated":["…"],"probe":{"nonce","round_trip_ms","paste_multiline":bool}|null,"ok":true}`.
`install` for a kind other than `claude` refuses `hooks_unprobed` until `probe` passes.
`check` adds `events`, `installed`, `hook_exists`, `shim_current`, and `project_dirs`; it
sets `ok:false` and appends the warning `duplicate hook commands found; a hook registered
twice runs twice` whenever `duplicates` is non-empty (same text as `install`; M6 SK-06).
`--settings`, `--hooks-dir`, `--claude-dir`, `--project-dir` (repeatable), `--cli`, and
`--no-members` (leave member `delivery` untouched) point every action at rig-local files.

### `install-cli`

Symlinks `~/.local/bin/herdr-team` to `bin/herdr-team`. JSON `{"path","target","created":bool,"replaced":bool}`.

### `view on|off|toggle [--force]`

Ownership probe first. JSON `{"view":"on|off","source":"plugin:herdr-team","label":"team:vuln-hunt","owner":"own|none|foreign","previous":"…"|null}`.
Errors: `view_foreign` (1) without `--force`, `plugin_disabled` (1). `toggle`
turns the view off when we own it or `view.json` says `on`; under a foreign
owner our view is not showing, so `toggle --force` turns it on (replacing the
foreign view) regardless of a stale `view.json` (M7 UI-06).

### `ui picker|compose|console|who|close [--target-pane <id>]`

Opens plugin panes over the socket (`plugin.pane.open`, `plugin.pane.focus`,
`popup.close`); retries once after 500 ms on `plugin_pane_open_failed`, then
falls back to the console with `--target-pane`. `ui who` opens the console
entrypoint as a popup started on its roster box (`HERDR_TEAM_CONSOLE_VIEW=who`
in the pane env). `ui picker` runs `ensure_daemon()` first unless
`HERDR_TEAM_NO_DAEMON=1`. JSON `{"ui":"picker","opened":true,"placement":"popup|split|tab","pane_id":"…"|null,"fallback":"console"|null,"retried":bool}`;
`ui close` → `{"ui":"close","closed":true}`. Registered by `cmd_ui.py`
together with the pane entrypoints below.

Opening the console entrypoint stamps `launched_at` in `console.json`
(section 10). `doctor` and `daemon start` close a pane labelled `Team
console` only when it hosts no agent, its `pane process-info` foreground is
known to be a shell (an empty foreground list is unknown, not a shell), and
no console launch is younger than 15 s; a console still booting is never
closed by a concurrent `doctor` or `daemon start`. Labelled panes skipped
for either reason are reported as `unresolved`; because the `[[startup]]`
hook runs before restored panes have spawned their shells, the daemon
repeats the pass 3 s after every connect and every 3 s while something stays
unresolved, at most 12 times (RT-05).

Console lifecycle (UI-05): SIGTERM to the console pid ends it cleanly
(`open:false`, exit 0, the pane closes); SIGHUP keeps Python's default so a
session stop leaves `open:true` and the restart reopens the console. A
console pane closed while the server is up (`plugin pane close`, the
human) cannot record its own exit because Herdr hangs it up first, so the
daemon (and the `pane.closed` hook when the daemon is dead) checks
`pane.list` for the console's `terminal_id` and writes `open:false` plus
`closed_at` when it is gone; that console is not reopened later. After a
`pane move` the record's `pane_id` is stale; only `terminal_id` is used.

### `teardown`

Dead-daemon cleanup: clears tokens and labels on every roster pane, clears
the view, removes stale `console.json`. JSON `{"tokens_cleared":n,"labels_cleared":n,"view_cleared":bool,"daemon_stopped":bool}`.

### `gc`

Removes session trees whose socket is gone, lock free, older than 7 days.
JSON `{"removed":["…"],"kept":["…"]}`.

### `prune --keep-days N`

Archives board segments older than N days into `_archive`. JSON `{"team","archived":["…"],"bytes_freed":n}`.

### `hook-event <agent_detected|pane_closed|pane_exited>` (internal, hidden)

The `bin/hook` slow path. Exits 0 in every non-bug case. JSON
`{"event","pane_id","team"|null,"changes":[…],"skipped":"no_team|socket_not_allowed|daemon_alive"|null}`.
`agent_detected` rebinds by terminal id, then team label plus kind, pane id
plus kind, exact name (plan 4.2 (a) to (d)); a different detected kind on
the member's terminal is `kind_changed`. A re-fetched row whose `agent` is
null (launch pending, detection not yet run) is no evidence of another
kind: the hook adopts the live ids and keeps the status (`starting`,
`missing`), the same rule as the daemon's reconcile.

### Pane entrypoints (no `--json`)

`console`, `compose`, `picker` run the curses UIs when launched by the
manifest panes (`HERDR_PLUGIN_ENTRYPOINT_ID` set). From a plain shell they
refuse with `not_a_plugin_pane` unless `--target-pane` (console) or `--force`
is given. Right after that check they apply the same socket gate as `ui`
(plan 4.1 / PK-07): when `allowed-sockets` exists and does not list the
resolved socket, each entrypoint prints
`herdr-team <console|compose|picker> skipped: socket not allowed` on stderr
and exits 0 before any socket call, before `console.json` is written and
before the curses loop starts, so the pane script shows the hint and nothing
is touched. `plugin.pane.open` answers `plugin_pane_opened` (pane id at
`plugin_pane.pane.pane_id`) for split/tab/overlay/zoomed placements and a
bare `{"type":"ok"}` for `popup`, which is why `ui`'s `"pane_id"` is `null`
for popups.

## 10. Shared record grammar

Board record (plan 6.1), all keys always present:

```json
{"v":1,"seq":42,"ts":"…","from":"builder","from_label":null,"from_kind":"claude","from_pane":"w1:p2","from_terminal":"term_…","from_gen":1,
 "origin":{"via":"cli","verified":true,"pid":4021,"ppid":4010,"workspace_id":"w1","tab_id":"w1:t1","socket":"…"},
 "to":["reviewer"],"to_role":null,"kind":"request","text":"…","refs":[],"reply_to":null,"retracts":null,"supersedes":null,
 "urgent":false,"ttl_ms":null,"truncated":false,"event":null,"relayed_for":null}
```

`system` records set `from:"system"`, `kind:"system"`, and `event` in
`nudged|toast|retracted|expired|abandoned|member_gone|member_restarted|rotated|reset_detected|charter_updated|renamed`.
Readers render `from:human` without a console/popup/outside/verified-shell
origin, or `from:system` without `kind:system`, as `(unverified)`.

Cursor file `cursors/<name>.json`: `{"v":1,"seq":42,"terminal_id":"…","surfaced_by":"cli|hook","updated":"…"}`.

`board.seq`: `{"next":43,"active_first_seq":1}`.

`team.json` (plan 5.1) carries an optional `config` object next to `members`
and `charter`: `{"name_policy":"adopt"|"enforce","gate":{…}}`. `config.gate`
overrides the daemon's plan 8.2 tunables for that team; the daemon reloads
it on its 2 s roster scan and logs the mapping once per change. Keys, all
optional, milliseconds unless named otherwise, defaults in parentheses:
`stable_ms_screen` (2000), `stable_ms_hook` (750),
`stable_ms_hooks_delivery` (15000), `done_hold_ms` (60000),
`min_interval_ms` (20000), `global_interval_ms` (1500),
`focus_max_hold_ms` (300000), `focus_snapshot_stable_ms` (3000),
`dialog_hold_cap_ms` (600000), `pair_budget` (10, count),
`pair_window_ms` (600000), `sample_gap_reset_ms` (10000),
`post_ttl_ms` (1800000), `burst_window_ms` (1000), `nudge_focused`
(`"never"`, one of `never|always`). `post_ttl_ms` is the target-active time after which an
unread post is `expired` (paused while the member is `missing`);
`pair_window_ms` is the window of the `pair_budget` ping-pong count between
two members; `sample_gap_reset_ms` is the `agent list` sample gap that voids
the stable window of that team's terminals (other terminals keep the
default); `burst_window_ms` is how long a nudge waits after its newest post
arrived, so a same-second burst becomes one nudge covering the seq range
(M5 ND-03). An unknown key, a negative or non-numeric value, a non-string
`nudge_focused`, or a `nudge_focused` outside `never|always` makes the
daemon ignore the whole `gate` object (logged as `config.gate ignored`) and
run the default gate, so one bad field never changes every gate. Example:
`{"config":{"gate":{"done_hold_ms":0}}}` delivers to an idle member right
after the stable window.

`daemon.json` (one line, compact): `{"pid":4021,"start_time":"Thu Sep  4 13:53:10 2026","beat_at":"…","socket":"…","socket_inode":123,"version":"0.1.0","herdr_version":"0.8.2","protocol":20}`.
`start_time` is `ps -o lstart= -p <pid>` with surrounding whitespace trimmed.

`console.json`: `{"pane_id","terminal_id","pid","open":true,"default_team":"…","human_label":"…","opened_at","launched_at"}`,
plus `closed_at` (and `pid:null`) after the daemon or hook recorded a pane
closed while the server was up; the next console open drops `closed_at`.
`opened_at` is written by the console process itself; `launched_at` by
whoever opened the pane (`ui console`, `ui who`, the console fallback, or
the reopen in `doctor`/`daemon start`), so a console that has not written
its own record yet is still recognisable as launching.

`mute.json`: `{"*": "<until>"|null, "<name>": "<until>"}` with ISO timestamps.

`kinds.json` (session dir, one object per agent kind): `{"claude": {"trusted": true,
"verified": false, "probe": {"nonce", "round_trip_ms", "paste_multiline", "ok", "result",
"member", "recorded_at", "source"}, "multiline": {"one_submission": true, "submissions": 1,
"lines_sent": 2, "lines_received": 2, "probed_at": "…", "source": "…", "agent_version": "…",
"herdr_version": "…"}}}`. `trusted` is the owner override, `verified` is set by the daemon
after 20 clean round trips, `probe` by `hooks probe <kind>`; any one of them passes gate 4.
`multiline` is the SK-03 paste probe (plan 12, "newline with paste off → single line"):
`herdr agent prompt <member> $'line one\nline two'` followed by a count of the ❯
submissions that appeared; `one_submission: true` means the kind keeps an embedded newline
inside one paste, `false` that the newline submitted the first line on its own (the daemon
must then deliver one line per prompt). Written by hand or by a future probe; `who --json`
shows it under `kinds.<kind>.multiline`.

### `kinds list | trust <kind> [--reason <text>] | untrust <kind>`

The owner override behind gate 4. A fresh session has no `kinds.json` and the
daemon holds every delivery as `kind_unverified` until the kind is verified by
the ledger (20 clean round trips), probed (`hooks probe <kind>` records one
verified round trip), or trusted here. `trust` records `{"trusted": true,
"trusted_at", "trusted_by": "owner", "reason"}` under the kind and keeps any
probe or `multiline` data; `untrust` removes only those four keys. `list` shows
per kind whether it `delivers` and why. The daemon reads the file on every
evaluation, so no restart is needed. Unknown labels are `kind_unknown` (1).

JSON `list`: `{"kinds":[{"kind","delivers","trusted","verified","probe_ok","multiline","trusted_at","reason"}],"path"}`;
`trust`/`untrust`: `{"kind","action","row":{...},"path"}`.
