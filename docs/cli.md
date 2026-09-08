# herdr-synapse CLI contract

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
herdr-synapse [--json] [--team NAME|PATH] [--session NAME] [--socket PATH] [--session-mismatch-ok] <command> [args]
herdr-synapse --version [--json]
herdr-synapse --skill [--json]
```

Global flags are accepted before or after the command name.

| Flag | Meaning |
| --- | --- |
| `--json` | Print exactly one JSON object on stdout. Human output otherwise. |
| `--team NAME` | Team name. `--team PATH` (contains `/`, or starts with `.` or `~`) is a team directory `<state>/sessions/<slug>/teams/<team>`; the state root and slug derive from it, so this works outside Herdr with no socket. |
| `--session NAME` | Named session, mirrors `herdr --session`. `default` means the default session. |
| `--socket PATH` | Socket override. Wins over `--session`, `HERDR_SOCKET_PATH`, `HERDR_SESSION`. |
| `--session-mismatch-ok` | Allow a write (`post`, `retract`, `edit`, `task`, `ack`, `charter set|edit`, `brief --set`, `use`, `rename`, `remove`, `bind`, `dissolve`) to a team whose `team.json` socket differs from the resolved socket. Without it such a write is refused with `team_session_mismatch` (plan 12, RS-08) whenever the socket was resolved explicitly (`HERDR_SOCKET_PATH`, `HERDR_SESSION`, `--session`); `--socket` counts as consent; the default-socket fallback outside Herdr (`--team <path>`, nothing configured) is not checked so the offline append of HP-05 keeps working. `add` checks always (as before). Reads never check. |
| `--version` | `herdr-synapse 0.1.3`; JSON `{"version","skill_version","plugin_id"}`. |
| `--skill` | Prints `skills/herdr-synapse/SKILL.md`; JSON `{"skill","skill_version"}`. |

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
| 1 | refused or validation failure | `team_name_invalid`, `name_invalid`, `name_reserved`, `role_invalid`, `agent_name_taken`, `member_claimed`, `team_exists`, `team_not_found`, `member_not_found`, `author_mismatch`, `text_too_long`, `invalid_utf8`, `charter_too_long`, `ref_invalid`, `path_symlink`, `team_session_mismatch`, `team_ambiguous`, `view_foreign`, `plugin_disabled`, `agent_not_found`, `agent_blocked`, `agent_not_ready`, `launch_pending`, `not_an_agent`, `board_write_failed`, `roster_conflict`, `home_unset`, `internal`, `say_unverified`, `say_multiline`, `say_too_long`, `say_control_command`, `say_timeout`, `kind_unverified`, `retract_invalid`, `edit_invalid`, `interrupt_needs_recipient`, `interrupt_cooldown`, `session_unknown`, `session_unsupported`, `outside_herdr`, `command_not_found`, `pane_busy`, `member_alive`, `no_project_dir`, `workdir_foreign_file`, `instructions_too_long`, `rules_too_long`, `operator_grant` (in `needs`) |
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

`create` also takes the working-directory setup, so a team can be complete in
one command (all four are human only):

| Flag | Effect |
| --- | --- |
| `--project <path>` | records the project directory and creates the folder |
| `--rules "<text>"` / `--rules-file <path>` | the team's DOs and DON'Ts |
| `--instructions NAME=TEXT` | long-form instructions for one member, repeatable |

The path is resolved before any write, so a bad one fails before the team
exists. `--instructions` for a name that did not join warns and is skipped
rather than failing the create. When `--project` is not given and the members
share one existing directory, `create` prints that directory and the exact
`project set` command; it never acts on the suggestion itself.

### `add <team> <target> [--role <r>] [--as <name>] [--brief "<text>"] [--steal]`

Same join routine for one member. Then posts an urgent `member_joined` system record to `all`
(`member`, `role`, `member_kind` fields): the daemon nudges every other member to read it, and the newcomer
gets the briefing instead. JSON `{"team","member":member,"renamed":bool,"notifier","briefing_job","joined_record":seq}`.

### `remove <team> <name> [--keep-name]`

Clears the three tokens and the pane label, marks the member `left`
(tombstone kept for addressing history), posts `member_gone`. JSON
`{"team","removed":"<name>","tokens_cleared":true,"name_cleared":bool}`.

### `leave`

From a member pane: `remove` on self. JSON `{"team","left":"<name>"}`.

### `bind <team> <name> <target>`

Re-attach a `missing`/`unbound`/`kind_changed` member to a live agent
(rehydration case (e) or a manual fix). The target's `agent_session` is
recorded on the member. JSON
`{"team","member":member,"previous_terminal_id":"…"}`. When the member moves
to another pane, its previous pane loses the `team:<team>/<role>` label if
that pane still carries it and hosts no agent (after a cold restart it is a
plain shell; RT-02). The daemon applies the same rule when a reconcile
rebinds a member to another terminal.

### `resume <name> [--print]` (human only)

Reopens a member's **own** harness session in the pane you run it from.
`claude --continue`, `codex resume --last` and `opencode -c` pick a
conversation by directory or by recency, never by pane, so with several
members in one checkout any of them can come back under a member's name.
`resume` runs the exact command Herdr itself uses on restore for the session
the roster recorded, from the member's directory, and replaces the CLI
process with it, so the pane becomes the agent. The table is copied entry for
entry from Herdr 0.8.2 (`src/agent_resume.rs`) and covers every integration
Herdr ships:

| Source | Command | Source | Command |
| --- | --- | --- | --- |
| `herdr:claude` | `claude --resume <id>` | `herdr:kilo` | `kilo --session <id>` |
| `herdr:codex` | `codex resume <id>` | `herdr:kimi` | `kimi --session <id>` |
| `herdr:copilot` | `copilot --resume=<id>` | `herdr:mastracode` | `mastracode --thread <id>` |
| `herdr:cursor` | `cursor-agent --resume <id>` | `herdr:omp` | `omp --resume=<path\|id>` |
| `herdr:devin` | `devin --resume <id>` | `herdr:opencode` | `opencode --session <id>` |
| `herdr:droid` | `droid --resume <id>` | `herdr:pi` | `pi --session <path\|id>` |
| `herdr:grok` | `grok --resume <id>` | `herdr:qodercli` | `qodercli --resume <id>` |
| `herdr:hermes` | `hermes --resume <id>` | `herdr:qwen` | `qwen --resume <id>` |
| `herdr:antigravity_cli` | `agy --conversation <id>` | | |

Pi and omp identify a session by an absolute path rather than an id; every
other kind uses an id. On Windows the Cursor binary is `cursor-agent.cmd`. The harness's own hook
then reports that session to Herdr for this pane and the notifier rebinds the
member here on its next scan (rehydration step 0), with a `member_restarted`
record; the old pane is left to `reconcile`.

`--print` shows the command instead of running it (this is what the
`prefix+t` action does, since a popup is not a shell). JSON
`{"team","member","kind","session":{"source","agent","kind","value","seen_at"},"argv":[…],"command":"…","cwd":"…"|null,"cwd_missing":"…"|null}`.

Refusals: `session_unknown` (1) when the member never reported a session
(its harness integration is not installed: `herdr integration install
<kind>`) or the recorded value is unusable, `session_unsupported` (1) for a
source Herdr does not issue, a source paired with an agent Herdr never pairs
it with, or a reference kind that source does not use,
`author_mismatch` (1) from a member pane, `outside_herdr` (1) without
`HERDR_PANE_ID` (the harness could not report the session for a pane),
`command_not_found` (1), `member_alive` (1) when the member is still running
that session in its own pane (go there, or stop it first).

### `dissolve <team> [--yes]`

Deletes a team. Clears tokens, labels, and the view for every member, stops
nothing else, and moves the team dir to `_archive/<team>-<ts>/`. The agents keep
running and the board is archived rather than removed, so this is recoverable
by moving the directory back. Human only.

On a terminal it asks, naming what happens, since "dissolve" does not say it;
off one, `--yes` is required (`confirmation_required`). Declining exits 0 with
`{"team","dissolved":false}`. JSON on success
`{"team","archived_to":"…","members_cleared":n}`.

In the team view (`prefix+t`), `x` on a team or one of its members dissolves it
after the same question.

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

## 5. Charter and briefs (operator authority)

Every write here refuses `author_mismatch` from an agent pane, a hook, or
`--as human`, and appends to `audit.jsonl`. The exception is a member the
operator has delegated to with `operator grant`: its writes are accepted and
audited as `operator_action`, naming the member.

**What counts as the operator.** Authority is decided by where the command
runs, and the process tree decides that, not the environment. A caller with
no `HERDR_PANE_ID` that is a descendant of an agent's pane is resolved as that
agent, so unsetting the variable no longer buys authority. An operator's own
shell, a console, and anything genuinely outside Herdr are unaffected. When
the server or `ps` cannot answer, the caller is left where it was, because
refusing on a failed lookup would lock the operator out of their own CLI.

### `manager [<name>] [--clear] [--operator [--ttl DURATION] [--note TEXT]]`

Names the one member that coordinates the team, or with no arguments says who
it is. Setting one clears any previous holder in the same write, so two
managers cannot exist. Human only (`_human_only`), so a delegated operator may
appoint one; `--operator` additionally requires the operator themselves
(`_strictly_human`), because a delegate may name a manager but may not pass its
own authority on.

The designation grants nothing. The charter, the team rules and every member's
instructions stay human-only; `--operator` is the one way to add those, and it
goes through the ordinary grant, keeping its expiry, board announcement and
per-use audit line.

What changes is what the other agents are told and how the manager's posts are
delivered:

- a `manager_changed` record naming every live member, `human`, and `all`,
  marked urgent. All three parts are load-bearing: a system record takes an
  early return in the daemon's ingest, so `manager_changed` is in
  `URGENT_SYSTEM_EVENTS` to be fanned out to the members at all, and in
  `TOAST_SYSTEM_EVENTS` to reach the operator's toast queue.
- `who` tags the row `manager`; `me` marks the teammate and tells the manager
  it is one; the Claude session-start blob names it in the teammate list; the
  typed briefing marks it in the roster (`name (role, manager)`).
- the skill (v5) tells every agent to take its assignments and handoffs as the
  plan unless they conflict with the charter, their own instructions, or
  something unsafe, and to disagree on the board rather than quietly diverge.
  Its posts are still peer requests, not operator instructions.
- **its posts to the whole team wake everyone.** An ordinary agent's broadcast
  is held (`gate.py` gate 2, `HOLD_BROADCAST`) and waits for each member's next
  board read, which measured a 42-minute median on a live team. The manager's
  does not. This lifts that one hold and nothing else: `done_hold`, the
  per-member interval, `pair_budget`, `blocked`, `dialog` and `draft` all still
  apply, and it deliberately does not route through `urgent`, which would widen
  the bypass surface.

JSON `{"team","member","previous","changed","operator"[,"expires_at"]}`.
`create <team> --manager <name>` does the same at creation, after the members
exist. In the team view (`prefix+t`), action 8 on a member row toggles it.

### `operator [list] | grant <name> [--ttl DURATION] [--note TEXT] | revoke <name>`

Shows, grants, or withdraws a member's delegation of your authority. This is
how an agent is allowed to build and run a team end to end: with a grant it
may write the charter, the team rules, any member's instructions, and the
project folder, exactly as you can.

Granting is yours alone. A delegated member passes every other operator gate
but is refused here, so authority cannot be passed on. `--ttl` defaults to 12
hours and takes the usual durations (`30m`, `2h`); `0` never expires. Both
grant and revoke append a system record to `all`, so the team sees who holds
authority, `who` tags the member `acts as operator`, and `doctor` warns while
any grant is live. `say` is unaffected and stays the operator's alone.

JSON: `list` gives `{"team","grants":[{"team","member","granted_at","granted_by","expires_at","note"}]}`;
`grant` gives that entry; `revoke` gives `{"team","member","revoked":bool}`.

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

### `instructions [<name>] [--set "<text>" | --file <path> | --edit | --adopt | --discard | --clear] [--yes] [--urgent]`

The member's own standing orders: a structured document, up to 4000
characters. Reading is open to anyone; every write is human only. Exactly one
mode per call; two are a usage error.

The document has six known sections, all optional: `Mission`, `Scope`,
`Constraints`, `Definition of done`, `Handoffs`, and `Notes`, which is private
and never sent to the agent. A heading the plugin does not know is kept in
place. Guidance for each section rides in HTML comments, which are invisible in
rendered Markdown and never injected. Plain text with no headings becomes the
`Mission`, so `--set "one sentence"` behaves as it always did, and a pre-0.6
instructions file needs no migration.

The authoritative copy lives in the team state dir, which no agent can reach
through the project checkout. When the team has a project directory the
document is mirrored to `members/<name>.md`.

| Mode | What it does |
| --- | --- |
| `--set`, `--file` | replace the document from text or a file; a file over the limit is refused, never truncated |
| `--edit` | open it in `$VISUAL`/`$EDITOR`, exactly as `charter edit` does |
| `--adopt` | import the edit made to `members/<name>.md`, after showing a unified diff and asking (`--yes` skips) |
| `--discard` | throw that edit away and restore the file from the authoritative copy |
| `--clear` | remove the document |

`--adopt` is what makes an edit the operator's word. The project folder is
inside a checkout the agents can write to and the plugin cannot tell whose
editor saved the file, so an edited `members/<name>.md` is **kept, not
imported**: the notifier stops overwriting it, posts one `instructions_edited`
record to you naming the adopt command, and waits. Until you adopt, nothing
from that file reaches any agent.

Every write bumps the member's `instructions_seq` and appends an
`instructions_updated` record addressed to **the member and to `all`**, so the
member is nudged (a record addressed only to `all` is a broadcast, which the
delivery gate holds) and teammates still learn who owns what. `--urgent`
nudges everyone at once. With no name, shows your own.

JSON `{"team","member","chars","path","record_seq","instructions_seq"}`;
`--adopt` adds `"adopted"` and `"diff"`.

### `knowledge [--limit N]`

Prints the team's rules and its findings.

### `knowledge set "<text>" | --file <path>` (human only)

The team's DOs and DON'Ts, up to 4000 characters. These carry operator
authority: they are injected into Claude members' context alongside the
charter, so only a human may set them. Stored as `rules.md` in the team state
dir (before 0.6 this was `knowledge.md`, which is still read when `rules.md`
is absent and removed on the first write; the project mirror's `knowledge.md`
is a different document, rules **plus** every finding). Appends a
`knowledge_updated` system record so members see the change on their next
board read; `--urgent` nudges them instead of waiting. A file over the limit
is refused rather than truncated.
JSON `{"team","chars","path","record_seq","rules_seq"}`.

### `knowledge add "<text>"`

Appends one finding, up to 400 characters, attributed to the pane that ran
it. Any member may do this. Findings are peer notes: they are escaped, they
are never injected as instructions, and no finding can turn into a rule.
Appends a `knowledge_finding` system record so the team sees it.
JSON `{"team","finding":{"at","author","kind","text"},"record_seq"}`.

### `knowledge clear` (human only)

Clears the rules. Findings are append-only and are not affected.

## 5a. The team working directory

### `project`

Shows the team's project directory and the folder inside it, or `none`.

### `project set <path>` (human only)

Also available at team creation as `create --project <path>`, and as a stage
in the `prefix+t` wizard, which prefills the directory the selected agents
already share (Tab skips it). Records the directory and creates `<path>/.herdr-synapse/<team>/`. This is the
consent gate: the plugin never infers a project directory and never writes
into a repository until a human runs this. The path must be an existing
directory, and the filesystem root, `$HOME`, and the plugin's own state dir
are refused.

The folder holds a generated `README.md` and `.gitignore` at the top level,
and per team a `knowledge.md`, a `members/<name>.md` per member, and an
`artifacts/` directory members own outright. Everything except `artifacts/`
is a rendered mirror of state that lives elsewhere: edits to it are reported
as drift and overwritten, never imported. A file without the plugin's marker
on its first line is somebody else's and is left alone unless `--force`.

### `project clear` (human only)

Stops the plugin writing to the folder. Nothing is deleted; the plugin never
deletes anything under a project directory, including when a member is
removed or renamed. Those get a tombstone written over their file instead.

### `project render [--force]`

Regenerates the mirror. Runs automatically after a roster change and after
any write to the knowledge base or a member's instructions.

### `knowledge-status`

Every team in the session at a glance: which have a working folder, their
rules, how many members have instructions, findings, artifacts, and any
issue (a project directory that is gone, unwritable, or holds a file the
plugin did not write). A team with no folder is printed with the exact
`project set` command that gives it one. `prefix+f` opens the same report as
a scrollable popup (`ui knowledge`, or `knowledge-pane` inside it), the way
`prefix+i` shows usage.

### The board snapshot

The notifier keeps `<project>/.herdr-synapse/<team>/board.md` current: the whole
board, archived segments included, rendered like `export --format md`. It is
rewritten only when the board has moved and at most once a minute, and its
content is stamped with the newest post's timestamp rather than the current
time, so an unchanged board never produces a change. Like the other mirrors it
carries the generated marker and is never read back; unlike them it is listed
in the generated `.gitignore`, because it is rewritten constantly.

Use `export` when you want a copy to keep or share; the snapshot is the
always-current one that lives with the team.

### Watching `artifacts/`

The notifier fingerprints `<team>/artifacts/` every 10s and appends one
`artifacts_changed` system record when files are added, changed, or removed,
whoever did it: a member, you, or another tool.

The record is summarised by directory and capped at 220 characters, so a
40-file data dump reads `artifacts: new 40 files under
codex-hunt-researcher/…/victim/` while a single report is still named in
full. It is posted only once the tree has stopped changing for a whole poll,
so a build is one record rather than one every ten seconds; a tree that never
settles is announced every five minutes, and there is a one-minute floor
between records for a team. Deferring never loses a change: the diff baseline
is the last state the board was told about, not the last scan.

The first scan after the daemon starts only seeds the fingerprint, so a
restart does not re-announce an existing folder. Deep (over 5 levels) or wide
(over 32 files) subtrees are collapsed to one entry rather than walked, and
generated directories (`.git`, `node_modules`, `__pycache__`, `dist`, …) are
skipped. `artifacts_changed` deliberately does not hold Claude's Stop hook
open; it still reaches `board --new` and the prompt-submit context.

Set `config.artifacts = {"watch": false}` in `team.json` to turn the watcher
off for one team.

## 6. Self and roster views

### `orient [--member <name>]`

Everything a member needs to pick the team back up after losing its context:
its name, role and team, the charter headline, its brief, its own instructions,
the team rules, its teammates with the manager marked, where the team's files
are, and its unread count. Findings are a count and a command, never inlined —
they are peer notes, and inlining them would let one member's text reach
another as though it carried authority.

It **calls the Claude session-start builder** (`cmd_hooks.brief_context`)
rather than composing its own text, so the block a Claude member is handed by
its hook and the block a Codex member asks for cannot drift apart. A test
asserts the two are byte-identical.

Reads the state dir only; no notifier needed. `--member` is operator-only,
because another member's orientation contains that member's instructions.
Refuses `not_a_member` (exit 3) outside a member pane, as `me` does. JSON
`{"team","member","role","kind","manager","text"}`, where `text` is the same
string the human form prints.

This is what the typed briefing now points at, and what the skill tells an
agent to run after a `/compact` or a `/clear`.

### `me`

From a member pane. JSON:

```json
{"team":"vuln-hunt","name":"vuln-hunt-reviewer","role":"reviewer","kind":"codex","pane_id":"w2:p1","terminal_id":"term_…",
 "brief":"…"|null,"charter":{"seq":3,"headline":"…"}|null,"teammates":[{"name","role","kind","status"}],
 "unread":2,"cursor":41,"verified":true,"via":"cli","skill_version":1,"skill_installed":1|null,"skill_ok":true,
 "cli":"/abs/path/herdr-synapse","notifier":"alive","session":"…f01a83b5a560"|null,
 "instructions_path":"…","knowledge_path":"…","board_path":"…","instructions_stale":false}
```

`session` is the tail of the harness session id the roster holds for you
(the full record is `session` in `team.json`); human output shows it with the
`resume` command that reopens it.

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
   "briefed":true,"charter_stale":false,"instructions_stale":false,"unread":0,"brief":"…"|null,"session":"…f01a83b5a560"|null}],
 "kinds":{"claude":{"trusted":true,"verified":false,"probe_ok":false,"multiline":{"one_submission":true,…}|null}}}
```

`kinds` has one entry per agent kind on the roster (never `human`), read
from the session's `kinds.json` (section 10): the three trust flags gate 4
uses and the `multiline` paste-probe record, or `null` when the kind was
never probed. `--role` filters `members` only; `kinds` always covers the
whole roster (M6 SK-03).

`--role` filters `members`; `--brief` drops `brief`, `last_seen_at`,
`hooks_last_seen`, and the `session` tag from human output only. Human rows
are glyph, name, role, kind, pane, status, headline, tags (the role column is
added to the plan 11 layout so a member reading `who` can say who does what;
SK-02). `session` is the tail of the harness session id recorded for the
member (`agent_session` in Herdr's `agent list`), the key rehydration uses
first and `resume` reopens; `null` until the member's harness reports one.

### `audit [--last N]`

JSON `{"team","entries":[{"ts","event":"author_mismatch","author":"…","via","pane_id","details":{…}}]}`.

## 7. Board

### `post "<text>" [options]`

```
post "<text>" [--to <name>[,<name>…] | all | human | role:<r>] [--kind note|request|handoff|done|blocked|question|answer]
     [--ref <path>]… [--attach <path>]… [--file <path>]… [--reply-to <seq>] [--urgent] [--interrupt] [--spill] [--as human] [--name <label>]
     [--relayed-for human] [--to-any]
```

- Default `--to`: `all` (the whole team) for every author, member or human.
  Address one member with `--to <name>`, the operator with `--to human`, and
  a role with `--to role:<r>`. Directed posts nudge their recipients. A post
  to `all` from the human nudges every member (normal holds apply); from a
  member it is read at the next board read unless `--urgent`.
- `--interrupt` (implies `--urgent`) marks a post that could not wait. The
  notifier may type its nudge into the recipient's *running turn* when the
  recipient's kind is in `config.gate.interrupt_kinds` (default `claude`:
  Claude Code queues a line typed mid-turn behind its current step) and the
  sender has not interrupted that teammate inside `interrupt_cooldown_ms`
  (default 10 min); otherwise it is an ordinary urgent nudge, delivered once
  idle. The typed line is the `[herdr-team interrupt] <sender> could not
  wait: …` envelope, never the text. Named recipients only:
  `interrupt_needs_recipient` (1) for `all` or `human` alone; a member's
  second interrupt of the same teammate inside the cooldown is
  `interrupt_cooldown` (1, details `member`, `last_seq`, `retry_in_s`); the
  human has no cooldown. The record carries `interrupt: true`; the `nudged`
  record that follows carries `interrupt: true` and `interrupt_by`.
- `--to` names are validated against the roster (current names, names
  retired under 10 min, `role:<r>` expands and records `to_role`); a typo is
  `recipient_unknown` (1) with `roster` in details unless `--to-any`.
- Text: strict UTF-8 (`invalid_utf8`), sanitized, ≤ 2000 chars
  (`text_too_long`, hint `--spill` writes `payloads/<seq>-body.md`), marker
  check (`echo_rejected`, 4), secret patterns refused without `--force`.
- `--ref` must exist under the team dir, `payloads/`, or a roster member's
  cwd; `--attach` copies into `payloads/` (16 MiB cap) with a safe basename.
  `--file` (the console's `@@path`) picks for you: a file the team can
  already read becomes a `--ref`, anything else is copied like `--attach`;
  a relative path is looked up under your cwd, then under every member's cwd;
  a missing file or one under a dot-directory (`.ssh`, `.aws`, `.config`) is
  `ref_invalid` (1) either way.
- Works with the server down (author unverified). Never calls
  `notification.show`.

JSON `{"seq":42,"team":"vuln-hunt","notifier":"alive|offline","to":["reviewer"],"to_role":null,"kind":"request","author":{"name":"builder","via":"cli","verified":true},"spilled":false,"attached":[],"refs":[],"urgent":false,"interrupt":false}`
(`attached` lists the copies made under `payloads/`, `refs` every reference the record carries).

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

### `export [PATH] [--format md|json|jsonl|text] [options]`

Saves the whole board to a file, rotated archive segments included, so a
finished team leaves a record that outlives its session.

With no `PATH` the file is `./board-<team>-<YYYYmmdd-HHMMSS>.<ext>`; a
directory argument puts the generated name inside it. An existing file is
refused unless `--force`, symlinked paths are refused outright, and the file
is written `0600` through the usual atomic write.

| Format | What you get |
| --- | --- |
| `md` (default) | a standalone document: team, charter, roster, then every post with its sender, recipients, kind, refs and reply/retract links |
| `json` | one object: `{"schema","team","exported_at","exported_by","charter","members","records"}` |
| `jsonl` | the raw records, one per line, exactly the on-disk shape, so an export reads back into anything that reads a board file |
| `text` | the same plain rendering `board --format text` prints |

From the console, `/export [path] [--format …]` runs the same command. With no
path it writes into `<project>/.herdr-synapse/<team>/exports/` (generated and
git-ignored), falling back to your home directory when the team has no project
folder, because the console pane's own working directory is the plugin's.

`--since <seq>`, `--last <n>`, `--kind <k>` and `--from <name>` narrow the
selection; `--no-archive` limits it to the active file; `--stdout` writes to
standard output instead of a file, so an export can be piped.

Retracted posts are included, marked as retracted, because an archive that
silently drops them is not a record of what happened.

JSON `{"team","records":68,"format":"md","path":"…","bytes":41234}`.

### Opening a team's board

`herdr-synapse ui console --team <name>` opens that team's board as a split. A
session may hold **one console per team**: a second open for a team that
already has one focuses that pane instead, a console for another team opens
beside it. `prefix+u` opens the default team's board; `b` on a team row in
`prefix+t` opens that team's.

Each console is pinned to the team it was opened for (through `HERDR_TEAM` in
the pane env) and its pane is labelled `Team console: <team>`. `/use <team>`
inside a console opens that team's board rather than switching the pane, and
no longer rewrites the session-wide default team.

`console.json` records the consoles as a registry keyed by `terminal_id`:

```json
{"schema": 2, "default_team": "red-dev", "launched_at": "…",
 "consoles": {"term_abc": {"pane_id": "wC:p6", "team": "red-dev", "pid": 811,
                           "open": true, "human_label": "human", "opened_at": "…"}}}
```

`default_team` stays session-wide: it is what every CLI command infers its
team from. A pre-registry document (one flat record) is read as a one-entry
registry, so upgrading needs no migration.

A console proves it is a console by having its `terminal_id` in that registry
with a live pid; `say` and verified human posts then still require the pane to
be focused and the caller to be a descendant of it. Herdr's focus is
session-wide, so exactly one board is `say`-capable at a time: the one you are
looking at.

### `ack`

Always rewrites the member's cursor file (`touch`), even when the seq does not move: the
daemon recognises an acknowledgement by a cursor write made after the briefing landed, and
a member whose cursor already sits at the board max (the join puts it there) would
otherwise never produce one and be re-briefed after 90 s (sandbox, 2026-09-05).

Records the member's cursor at the current max and `charter_seq_acked`.
JSON `{"team","member","cursor":59,"charter_seq_acked":3}`.

### `say <member> "<text>" [--force] [--wait | --no-wait] [--timeout S]`

```
say <member> "<text>" [--force] [--wait | --no-wait] [--timeout S]
```

Type one line into a member's input box right now, with operator authority,
and record it on the board. The console's `!<member> <text>` runs this;
`!!<member> <text>` adds `--force`.

- Human only, and only from the verified team console: the author must be
  `console`, verified, with confirmed pane ancestry. A member pane or a hook
  is `author_mismatch` (1); a shell pane, the compose popup, a terminal
  outside Herdr, an unfocused console, or a console whose process ancestry
  cannot be confirmed is `say_unverified` (1). Both are audited. A shell pane
  is refused on purpose: any agent can open a pane around a command and
  mint a verified shell author that way.
- One agent member; `all`, `human`, `me`, and `role:<r>` are
  `member_not_found` (1) with a hint. The member's kind must be trusted
  (`kinds trust <kind>`), else `kind_unverified` (1).
- Text: sanitized like a post, then tabs become spaces. A newline is
  `say_multiline` (1), more than 500 characters is `say_too_long` (1), the
  marker check is `echo_rejected` (4) and a secret is `secret_detected` (1);
  none of these is bypassable. A first token in `/exit /quit /clear /logout
  /login /resume exit quit` is `say_control_command` (1) unless `--force`.
- Needs the daemon: `daemon_down` (5) before anything is written. Then a
  `direct` record (`from: human`, `to: [<member>]`, `kind: direct`, extra
  `force`) is appended and a `say` job carrying only its seq is queued. The
  daemon types the record's text as-is (never a job payload, never a
  `[herdr-team` header) after checking that the record is the console's own.
- The daemon refuses in both modes when the member is absent, another
  process occupies its terminal, the kind is untrusted, the agent is
  `blocked` or `unknown`, `agent explain` shows a blocker or an overlay
  (`skip_state_update`), a dialog line is on screen, a draft sits on the
  prompt line, or another line is still being confirmed (`in_flight`).
  `--force` bypasses exactly `working` and `muted`. A job older than 10 s
  (left over from a dead daemon) is `stale`. Every refusal is recorded.
- The outcome is a `typed` system record addressed to `human` with
  `seqs:[<seq>]`, `reply_to`, `member`, `kind_of_member`, `result`
  (`typed|refused|not_submitted|failed`), `reason`, `detail`, `force`,
  `force_verified`, `elapsed_ms`. Reasons: `typed` carries `null`, `in_turn`
  (typed into a running turn) or `dry`; `refused` carries `working`, `muted`,
  `blocked`, `dialog`, `draft`, `skip_state_update`, `unknown`, `not_ready`,
  `absent`, `wrong_occupant`, `wrong_target`, `in_flight`, `stale`,
  `unverified_source`, `member_not_found`, `kind_unverified`; `failed`
  carries `hung`, `transient`, or `unconfirmed` (still idle 5 s after typing
  with the text gone from the prompt line; `not_submitted` when it is still
  there). Typing into a running turn is verified for Claude Code only; for
  other kinds `force_verified` is false and `detail` says so.
- `--wait` (default) polls for that record up to `--timeout` (10 s) and
  returns it as `outcome`; a refused or failed outcome is still exit 0.
  No record in time is `say_timeout` (1) with `seq` and `job`. `--no-wait`
  returns `outcome: null`; the console uses it and reads the outcome from
  the board tail.
- Neither record is mail: a member's `board --new`, `--peek`, `--to me`,
  unread count, Claude Stop hook, and prompt context never include them, and
  they never nudge. `board`, `board --kind direct`, `show`, and `--thread
  <seq>` show both; the human's `board --new` lists the `typed` outcome.
  `edit` and `retract` refuse a `direct` record (`edit_invalid`,
  `retract_invalid`, 1): send a correction with another `!` line.

JSON `{"seq":12,"team":"alpha","member":"alpha-worker","job":"3f9a1c0b2d","force":false,"text":"stop and summarize","author":{"name":"human","via":"console","verified":true},"waited":true,"outcome":{"seq":13,"result":"typed","reason":null,"detail":null,"elapsed_ms":812}}`.

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
| `say <name> "<text>" [--force]` | write a `direct` record and enqueue a type-now job (section 7); the daemon types it without waiting for idle and confirms it on the next agent poll | `{"team","member","seq","job","outcome"}` |
| `interrupts [show\|off\|on\|<kind>[,<kind>]] [--cooldown 10m]` | show or set `config.gate.interrupt_kinds` (the kinds a teammate's `post --interrupt` may be typed into mid-turn; `on` restores `claude`) and `interrupt_cooldown_ms`; the daemon reloads within 2 s; a change is human only (`author_mismatch`) | `{"team","kinds","cooldown_ms","default_kinds","changed"}` |
| `compact <name> \| --self [--reason TEXT]` | write a `direct` record carrying a `control` block and enqueue a control job; the daemon types the kind's compact command once the member is idle (section 9a) | `{"team","member","action","keystroke","kind","record_seq","job","requested"}` |
| `clear <name> --yes [--reason TEXT]` | the same, for the kind's clear command; operator only, and it asks before it runs | same shape |
| `read <name> [--lines N]` | `agent read --source visible` directly; `--lines` is refused for every member (`lines_refused`, exit 1), because scrolling an idle alternate screen types keys into the agent and only the daemon may type into a member | `{"team","member","pane_id","lines":[…]}` |

Job files: `notifier/jobs/<ts>-<id>.json` `{"v":1,"kind":"brief|nudge|focus|say|control","member","force","requested_by":author,"requested_at"}`.
A `say` job adds `"seq"` (its `direct` record) and carries no text: the daemon types the record.
A `control` job adds `"seq"` and `"action"`, and carries no keystroke: the
daemon reads the record's `control` block and refuses a job whose record is
missing, unverified, addressed elsewhere, or carrying no such block.

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

### `usage [--no-fetch] [--ascii] [--width N] [--timeout S]`

Usage limits (session, week, per model) of every provider account the
session's agents draw on, the way `/usage` in Claude Code or `/status` in
Codex show them, for all agents at once. Limits belong to an account, not a
pane, so the report groups the agents (`agent.list`; an unreachable server is
`agents_error` in the output, not fatal) by provider: `claude` → Anthropic,
`codex` → OpenAI Codex, `copilot` → GitHub Copilot, `gemini` and
`antigravity` → Google Gemini; `pi` and `opencode` by the login their auth
file holds (Anthropic OAuth, ChatGPT OAuth, or OpenCode Zen, which publishes
no window). A provider with a login on this machine is listed even without a
running agent; kinds with no known source are listed as `untracked`.

Sources, one GET each, in parallel, bounded by `--timeout` (default 8 s):
Claude Code's login (`~/.claude/.credentials.json`, else the macOS keychain
item `Claude Code-credentials`) → `api.anthropic.com/api/oauth/usage`;
`~/.codex/auth.json` → `chatgpt.com/backend-api/wham/usage`, with the newest
rollout log's `rate_limits` event as the offline fallback (`as of <age>`);
`gh auth token` (or `GH_TOKEN`) → `api.github.com/copilot_internal/user`;
`~/.gemini/oauth_creds.json` → Code Assist `retrieveUserQuota` while the
stored token is valid (never refreshed). `--no-fetch` reads local logs only.
Tokens never leave the process: not logged, not written, not in the output;
errors carry HTTP status codes only.

Human text: one block per provider (title, plan, the agents behind it) with a
bar, `% used`, `⚠` at 75 % and `‼` at 90 %, and the reset time. JSON
`{"v":1,"generated_at","fetched","agents":N,"providers":[{"id","title","plan","login","source","fetched_at","as_of","ok","error","note","windows":[{"id","label","percent","resets_at","severity","scope","detail"}],"kinds":[…],"agents":[{"kind","name","pane_id","status"}]}],"untracked":[{"kind","pane_id","reason"}],"agents_error"}`.
`ui usage` (`prefix+i`, action `herdr-synapse.usage`) opens the same report as
a popup that refreshes every minute (`r` refreshes now, Up/Down scroll, `q`
closes). Registered by `cmd_usage.py` with the hidden `usage-pane`
entrypoint (`not_a_plugin_pane` outside the popup unless `--force`).

## 9a. Context windows

### `context [<name>] [--ascii] [--width N]`

How full each member's context window is, read from the harness's own files:
Claude's transcript (`message.usage`), Codex's rollout log (`token_count`,
which carries the window size outright), OpenCode's `opencode.db`. Nothing is
typed and nothing is asked of the agent. A kind with no reader is `unknown`
and is never guessed at. The notifier polls the same readers every 15 s, so
this prints the notifier's reading when it is running and reads the files
itself when it is not (`"source":"who.json"` or `"files"`).

JSON `{"team","source","members":[{"name","kind","context":{"used","window","percent","source","model","at"}|null}]}`.
`who` carries the same reading as a `context 94%` tag, and the notifier
publishes it as the `team_context` pane token (source
`herdr-synapse:context`, TTL 2 min) so the Herdr sidebar can show it.

At 75 % and again at 90 % the notifier appends one `context_high` record
addressed to the member and to `all`. It says so once per crossing and never
acts: what to do about a full context is the member's decision, or the
operator's.

### `compact <name> | --self [--reason TEXT]`

Ask the notifier to summarise a member's context in place. The operator may
compact anyone; a member may compact itself with `--self`. A member never
compacts a peer: it posts a request and the peer or the operator decides.

The keystroke is per kind and only the verified ones are offered
(`control_unsupported`, exit 1, otherwise): Claude `/compact`, Codex
`/compact`, OpenCode `/compact`. It is typed with `pane.send_text` and a
separate Enter rather than `agent.prompt`, because a prompt is delivered as a
bracketed paste and a pasted `/compact` is read as text to answer rather than
as a command to run.

Delivery goes through the ordinary gate. Nothing about being blocked, showing
a dialog, holding a draft, or hosting the wrong occupant is bypassed; only
the `done_hold` and the per-member interval are, the way a briefing does.
Idle is required, so a control keystroke never enters a running turn.

Claude's compaction blocks its pane for two to three minutes, so the job is
not finished when the keystroke lands: it stays open until the effect shows
up, and is never typed twice. The effect is a session phase change (Claude
reports `source=compact` on the same session id) or a large fall in the token
count, whichever the notifier sees first; either one appends a
`context_compacted` record naming who asked. After `CONTROL_OBSERVE_S`
(6 min) with nothing observed, the notifier says so and closes the job rather
than retrying.

A compaction the notifier did not ask for is recorded too, so a teammate
reading the board knows this member now works from a summary.

### `clear <name> --yes [--reason TEXT]`

The same path for the kind's clear command: Claude `/clear`, Codex and
OpenCode `/new`. Codex deliberately gets `/new` rather than `/clear`, whose
scrollback wipe removes the surface Herdr's detection reads.

Operator only, and `--self` is refused (`author_mismatch`): a member may ask
to be cleared by posting a request, but throwing away an agent's working
memory is the operator's call. It asks `y/N` on a terminal, and `--yes` is
required otherwise, `--json` included.

A clear mints a new harness session, which the notifier already reacts to: a
new generation, `briefed_at` cleared, a fresh briefing. What this adds is
attribution, so the resulting `member_restarted` is followed by a
`context_cleared` record naming the operator rather than reading like a crash.

## 9b. Human in the loop

An agent addressing the operator is the only thing on the board that needs a
person, and until 0.12 it got a banner that faded in three to five seconds and
could not be answered, while the agent carried straight on. Measured on a live
team over three days: 88 posts to `human`, ten of them the agent actually
waiting on a decision, **six of those never answered by anyone** and the other
four answered by *peers* — one of which overrode a genuine pre-submission halt.

### `asks [--dismiss <seq>]`

What is waiting on you: every post from a member addressed to `human` that the
operator has not replied to. A reply from a teammate does not clear one — only
`from: human` does, which is the whole point. Retracted and dismissed asks drop
out. JSON `{"team","pending":[{"seq","from","kind","ts","text"}]}`.

`--dismiss <seq>` is "not now": the ask stays on the board and in this list's
source, but the popup stops reopening for it. A waiting agent stays blocked.

`--ack <seq>` is the shortcut for "seen": it posts an acknowledgement from you,
which closes the ask and releases a waiting agent. The text is deliberately not
"ok" — for a `question`, `blocked` or `request` it reads *"Seen by the operator.
This is an acknowledgement, not a decision: if you were waiting on one, say what
you would do and stop."* An agent that asked whether to submit something must
not read "seen" as approval, which is exactly the mistake a peer's reply caused.
For a `done` or a `note`, which is finished by being read, it says only "Seen by
the operator."

### The popup

The daemon opens the `asks` popup when something is waiting and no other popup
is up. It is **one popup for the whole queue**, not one per post: Herdr allows
exactly one popup at a time (`ui_busy` otherwise), so 88 posts cannot be 88
modals. It lists what is pending, and:

| key | |
| --- | --- |
| type + `Enter` | posts your reply as `--kind answer --reply-to <seq>`, which is what unblocks a waiting agent |
| `Ctrl-A` | acknowledges without typing: closes the ask and unblocks the agent |
| `Tab` / `↑` `↓` | move between asks |
| `Esc` | leaves this one waiting and stops the popup reopening for it |
| `q` | closes the popup |

It closes itself when the queue empties, however the asks were answered. The
toast path is unchanged and still fires; the popup is additive.

### `ask-policy [--block | --no-block] [--kinds …] [--timeout 8m] [--popup | --no-popup]`

Per team, stored at `config.ask` — deliberately not under `config.gate`, where
one unknown key throws the whole gate config back to its defaults. The bare
command reads and needs no authority; every write is human-only and audited,
the same shape as `interrupts`. Also `/ask-policy` in the team console.

| key | default | |
| --- | --- | --- |
| `block` | `true` | an asking post waits for you |
| `block_kinds` | `question, blocked, request` | which kinds wait |
| `timeout_s` | `480` (8 min) | how long before it gives up |
| `popup` | `true` | raise the popup at all |

The popup fires on **anything** addressed to you. Blocking does not: it
defaults to the three asking kinds, because blocking a `done` notice for eight
minutes would freeze a team that posts forty of them in three days.

### `post … --wait | --no-wait [--timeout DURATION]`

`--wait` blocks until you reply; `--no-wait` never blocks whatever the policy
says. Without either, the team policy decides, and only for a member posting an
asking kind to `human` — the operator's own posts and console posts never wait.

The wait is capped at **9 minutes** (`MAX_WAIT_S`) whatever you ask for, because
Claude Code kills a shell command at ten and a longer wait would end as a killed
process with no error the agent could read. It polls with a non-persisting
`BoardTailer`, which reads only new bytes, rather than `BoardStore.read`, which
re-parses up to 4 MB per call. The board lock is released before the wait
begins, so a waiting member never blocks anyone else's post.

On an answer: exit 0, `{"waited":true,"answer":{"seq","from","text"}}`. On a
timeout: **exit 6**, code `wait_no_answer` — distinct from `EXIT_REFUSED` so an
agent can tell "nobody answered" from "the post was rejected" without reading
prose. The post stays on the board either way.

A waiting agent shows `working`, so gate 5 holds every nudge for it and the
idle sweep skips it. The one thing that still reaches it is `--interrupt`.

### `doctor`

Never fails on warnings; `ok:false` only on hard problems. JSON:

```json
{"ok":true,"version":"0.1.0","python":"3.9.6",
 "herdr":{"bin":"/…/herdr","version":"0.8.2","protocol":20,"reachable":true},
 "socket":{"path":"…","source":"env:HERDR_SOCKET_PATH","session_name":null,"allowed":true},
 "slug":"default","config_dir":"…",
 "state_root":{"path":"…","source":"pointer-file","candidates":[{"source":"env:HERDR_PLUGIN_STATE_DIR","path":null},…]},
 "pointer":"…/plugins/config/herdr-synapse/state-dir"|null,
 "plugin":{"installed":true,"enabled":true,"path":"…","warnings":[]},
 "toast_delivery":"terminal","daemon":{…as daemon status…},
 "teams":[{"team","members","missing":n}],"console":{"open":bool,"pane_id"},
 "warnings":["…"],"errors":["…"]}
```

Among the warnings: a team whose `project_dir` has been deleted or has become
read-only. The mirror is refreshed best effort, so without this the folder
would just stop updating with nothing on screen to say why.

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

JSON `{"kind":"claude","action":"install","settings":"~/.claude/settings.json","hook":"~/.claude/hooks/herdr-synapse-hook.sh","added":["SessionStart","UserPromptSubmit","Stop"],"removed":[],"already":[],"backup":"…","duplicates":[…],"members_updated":["…"],"probe":{"nonce","round_trip_ms","paste_multiline":bool}|null,"ok":true}`.
`install` for a kind other than `claude` refuses `hooks_unprobed` until `probe` passes.
`check` adds `events`, `installed`, `hook_exists`, `shim_current`, and `project_dirs`; it
sets `ok:false` and appends the warning `duplicate hook commands found; a hook registered
twice runs twice` whenever `duplicates` is non-empty (same text as `install`; M6 SK-06).
`--settings`, `--hooks-dir`, `--claude-dir`, `--project-dir` (repeatable), `--cli`, and
`--no-members` (leave member `delivery` untouched) point every action at rig-local files.

### `install-cli`

Symlinks `~/.local/bin/herdr-synapse` to `bin/herdr-synapse`. JSON `{"path","target","created":bool,"replaced":bool}`.

### `view on|off|toggle [--force]`

Ownership probe first. JSON `{"view":"on|off","source":"plugin:herdr-synapse","label":"team:vuln-hunt","owner":"own|none|foreign","previous":"…"|null}`.
Errors: `view_foreign` (1) without `--force`, `plugin_disabled` (1). `toggle`
turns the view off when we own it or `view.json` says `on`; under a foreign
owner our view is not showing, so `toggle --force` turns it on (replacing the
foreign view) regardless of a stale `view.json` (M7 UI-06).

### `ui picker|compose|console|who|usage|close [--target-pane <id>]`

Opens plugin panes over the socket (`plugin.pane.open`, `plugin.pane.focus`,
`popup.close`); retries once after 500 ms on `plugin_pane_open_failed`, then
falls back to the console with `--target-pane`. `ui who` opens the console
entrypoint as a popup started on its roster box (`HERDR_TEAM_CONSOLE_VIEW=who`
in the pane env). `ui picker` runs `ensure_daemon()` first unless
`HERDR_TEAM_NO_DAEMON=1`. JSON `{"ui":"picker","opened":true,"placement":"popup|split|tab","pane_id":"…"|null,"fallback":"console"|null,"retried":bool}`;
`ui close` → `{"ui":"close","closed":true}`. Registered by `cmd_ui.py`
together with the pane entrypoints below.

`ui picker` is the team manager. Its first screen is a tree: every team in
the session with its agent members underneath (the `human` member and `left`
tombstones are never listed), then the agents that belong to no team. Member
rows are rendered by the same `who` renderer the console uses, plus the
role; live status comes from `agent.list`, and headlines, holds and mutes
from `who.json` when the notifier is running. Enter folds a team, Space
picks an unassigned agent, and the list scrolls with the cursor.

Enter on a member opens a numbered action menu; each action shells out to
this CLI and the popup stays open: `rename <old> <new>`, `brief <name>
--set`, `brief <name>`, `remove <team> <name> [--keep-name]`, and `focus
<name>` (which closes the popup). Rename and goal validate locally first,
`remove` asks `y` (Enter is not yes), and every action re-checks the
member's name and `terminal_id` against `team.json` immediately before the
call, so a roster that changed while the popup was open refuses rather than
acting on the wrong agent. `--dry-run` disables the actions.

With agents selected, a numbered choice follows: `add it to team <t>` per
team (one `add` per agent, the charter untouched) or `create a new team`.

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
`herdr-synapse <console|compose|picker> skipped: socket not allowed` on stderr
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

`kind` is one of `note|request|handoff|done|blocked|question|answer|direct|retract|system`.
`direct` records (`from:"human"`, `to:["<member>"]`, extra `force`) are lines the human typed into
one member with `say` (section 7): on the board for everyone, never nudged, never a member's mail.
A post made with `--interrupt` carries `interrupt: true` (and `urgent: true`): its nudge may be typed
into the recipient's running turn (section 7). The `nudged` record of such a delivery carries
`interrupt: true` and `interrupt_by: [<sender>…]`; an ordinary nudge carries neither.
`system` records set `from:"system"`, `kind:"system"`, and `event` in
`nudged|toast|retracted|expired|abandoned|member_gone|member_restarted|rotated|reset_detected|charter_updated|renamed|typed|member_joined`.
`member_joined` (from `add`) is `urgent` and carries `member`, `role`, `member_kind`; like an urgent `charter_updated` it nudges every member, except the newcomer.
A `typed` record (`to:["human"]`) is the outcome of a `say`: `seqs`, `reply_to`, `member`, `kind_of_member`,
`result`, `reason`, `detail`, `force`, `force_verified`, `elapsed_ms`.
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
(`"never"`, one of `never|always`), `interrupt_kinds` (`["claude"]`, the
agent kinds whose running turn a teammate's `post --interrupt` may be typed
into; an empty list turns interrupts off), `interrupt_cooldown_ms` (600000,
one interrupt per sender and target). `post_ttl_ms` is the target-active time after which an
unread post is `expired` (paused while the member is `missing`);
`pair_window_ms` is the window of the `pair_budget` ping-pong count between
two members; `sample_gap_reset_ms` is the `agent list` sample gap that voids
the stable window of that team's terminals (other terminals keep the
default); `burst_window_ms` is how long a nudge waits after its newest post
arrived, so a same-second burst becomes one nudge covering the seq range
(M5 ND-03). An unknown key, a negative or non-numeric value, a non-string
`nudge_focused`, a `nudge_focused` outside `never|always`, or an
`interrupt_kinds` that is not a list of names makes the
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

Metadata tokens stamped on a member's pane (source `herdr-synapse:roster`, no TTL):
`team`, `team_role`, and `team_c1`..`team_c6`. Exactly one `team_c<slot>` carries the team
name and the rest are cleared in the same patch, so a member that moves teams cannot keep a
stale colour. The slot is `config.color_slot` in `team.json`, assigned once by the notifier
(lowest slot no other team in the session holds; colours repeat past six) and persisted. It
exists because Herdr styles a sidebar cell from a fixed `fg` in the user's config and cannot
colour by a token's value; the pasted row lists one cell per slot, and a row drops the tokens
that have no value. `team_task` (source `herdr-synapse:task`, TTL 120 s) is separate.

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
