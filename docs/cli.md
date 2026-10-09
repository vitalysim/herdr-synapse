# herdr-synapse CLI contract

This file is the authority implementers code against. Plan section 5.2 lists
the inventory; this document fixes arguments, JSON output shapes, and error
codes. When an implementer must deviate, change this file in the same
change and say why in the commit message.

For the expandable command inventory and every GUI shortcut, see the
[command and shortcut reference](reference.md).

## Pi integration

Pi 0.85.1 uses the existing team commands with kind `pi`. Setup requires Herdr's Pi state integration, `skill install`, `hooks install pi`, and session-local `kinds trust pi`. Existing Pi processes need `/reload` after extension installation. Pi's Synapse extension is telemetry-only: prompt/stop hooks remain Claude-only, and Pi delivery stays `nudge`.

`hooks install|check|uninstall pi` manages only `<PI_CODING_AGENT_DIR or ~/.pi/agent>/extensions/herdr-synapse.ts`. Install and uninstall support `--dry-run`. Foreign files and symlinks are refused. Check returns a nonzero exit code when the managed extension is missing or outdated; it does not change delivery configuration.

Pi accepts model defaults/member overrides as `provider/model@thinking`, using native `--model` and `--thinking` flags. Thinking levels are `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`, subject to the actual model's capabilities. Use `--apply restart` to change a running member; success requires a new runtime report matching the requested setting and original native conversation. Managed resumes require the recorded absolute session path. Fresh starts receive `--name <member>`; resumed titles are preserved. Managed starts and resumes default to run-scoped `--approve` without changing global project trust; `permissions MEMBER native` omits it.

Pi context JSON can contain `used: null`, `percent: null`, and `estimated: true`; `window` is the active model's runtime limit, not a hardcoded default. Stale or identity-mismatched telemetry is ignored. Compact uses `/compact`; clear uses `/new`. A compact failure is recorded as a failed `typed` outcome, not as successful compaction. `!!` and interrupt deliveries use native steering semantics; typed means submitted, not that the agent has already acted on it.

`pi-report` is an internal, bounded JSON-stdin telemetry receiver. It verifies the live Herdr pane, terminal, Pi kind, and exact native session before writing its private snapshot. It cannot post messages, change authority, or authorize terminal delivery.

## Restore a saved team

Members with an unfinished swap are skipped until that operation is retried or cancelled.

`herdr-synapse restore <team> [--workspace ID] [--dry-run] [--refresh-flags]` restores missing members into a new `team:<name>` tab in the current workspace. Teams larger than 24 restored members use additional numbered tabs. The command requires human/operator authority and an existing team in the selected Herdr session.

A member whose own pane is still standing, still carrying its `team:<name>/<role>` label and holding no agent, is restored **into that pane** instead of a new one, so a team comes back in its own splits. A label two panes claim identifies neither, and an explicit `--workspace` outside the pane's workspace is honoured over the pane.

`--refresh-flags` repairs a member Herdr's own session restore brought back. Herdr rebuilds `codex resume <id>` or `claude --resume <id>` from the conversation reference alone, so the member runs without the approval-bypass flag, without `mcp_servers.synapse_canvas` and without any pane-local flag — it looks healthy on the roster and then asks permission for every canvas command, or has no canvas door at all. With the flag, `restore` compares each running member's live command line against what `models.launch_args` would have built and, for any behaviour-changing flag that is absent, records the controlled restart the notifier already performs: it exits the member when it is idle and resumes its conversation with the flags in place. `restore` itself never types into a pane and never stops a process. Model, effort and profile are never counted as missing; they are the member's own preference and visible in `who`. Without the flag nothing is audited and no extra call is made. Such a member is reported with `status: "relaunch"`, its `missing_launch_flags`, the `control` block and the `record_seq` of the board record.

The notifier raises the same finding on its own, once, at the one moment it can: when it adopts a member's process rather than starting it. That is a `launch_flags_missing` system record addressed to the operator, naming the member, the missing flags and the one command that repairs them. It is deliberately a notice and not an automatic repair — quitting and relaunching a pane the operator may be reading is worse than telling them. A `pane_recovered` record names a missing member's own surviving empty pane.

For any agent kind whose conversation identity comes from its integration (opencode, pi, omp, kilo, kimi, mastracode), run `herdr integration install <kind>` before you rely on restore. Without it a Herdr restart costs that member a fresh conversation and a briefing turn, because nothing recorded which conversation was in the pane.

Recorded conversations resume by exact ID using the member's effective model/effort configuration. Members without a conversation ID start fresh with their saved instructions and briefing. Already-running agents and members whose reserved pane still exists are skipped. Missing executables/directories, conflicting names, unsupported conversation references and startup failures are reported per member; eligible members continue. Failed panes remain available for inspection, and repeated calls do not duplicate them. Board history, read cursors, manager assignment, team links and member documents are preserved.

`--dry-run` performs read-only preflight and returns `{team, dry_run: true, members}`. Execution returns `{team, tabs, members, counts}`, where `counts` contains `resumed`, `relaunch`, `fresh`, `skipped` and `failed`. Each member includes `name`, `kind`, `role`, `status`, and, when applicable, its `mode`, `pane_id`, `terminal_id`, launch arguments and failure `reason`. Progress goes to stderr; `--json` keeps stdout to one result object. Exit 0 means no member failed; exit 1 means partial or complete member failure. An overlapping restore is refused with `restore_busy` (exit 5). A recorded session failing to resume never silently falls back to a new conversation.

## Create a replacement agent

`herdr-synapse [--team TEAM] swap MEMBER --to claude|codex|opencode|pi [--model MODEL[@EFFORT]] [--profile NAME] [--unlisted] [--handoff-file PATH] [--dry-run]` creates a new instance and fresh conversation for an existing logical member. It never selects an unrelated live agent. The picker member action `0` and `/swap MEMBER KIND [MODEL[@EFFORT]]` use this backend. The CLI invocation authorizes closing the outgoing pane; the interactive surfaces show a confirmation first.

Preflight checks the destination executable, settings, directory, trust, operator authority, and outgoing pane identity. A dry run does not start the daemon or allocate panes. Source panes with multiple views of one terminal are refused. Source identity is checked again immediately before close; this uses existing pane APIs, not an atomic conditional-stop method. Calling from the outgoing pane is refused.

The operation creates a labelled `swap:<operation-id-prefix>` tab in the source workspace, closes the outgoing pane without typing a command into its dialog, launches the destination, transfers membership, and queues its briefing. A current daemon is required; a running older plugin daemon is replaced through the normal daemon startup path. A detected destination with a login or provider dialog still needs operator attention before it can receive its briefing. Completion means the fresh instance is bound and its briefing queued, not that a provider has accepted a model request.

The member name, role, Mission, instruction documents/revisions, manager flag, project directory, board history/read cursors, links, delivery preferences, and existing operator-grant expiry survive. Model settings are selected for the destination kind from an explicit setting, previous saved settings for that kind, team defaults, then harness defaults. Source-native model arguments and conversation IDs do not carry into a fresh destination launch. The profile follows the same rule: `--profile NAME` (checked against the destination harness's list), else the member's own when the harness stays the same, else the one last used with that harness; a profile never crosses harnesses. The member generation increments once at takeover, and briefing/instruction acknowledgments reset. A missing initial destination session ID remains null until reported.

`Member.swap` is optional durable operation data: `{id, phase, source, destination, created_at, requested_by, cutoff_seq, handoff, note, pane, ...}`. Phases are `prepared`, `stopping`, `starting`, `briefing`, `complete`, `failed`, `cancelled`; a failure retains `resume_phase` and `error`. `Member.agent_history` stores previous agent configurations and exact conversation references. `Member.session_history` (optional, up to 20) keeps the exact references of earlier conversations of the same agent that a restart or clear replaced; only `search --history` reads it. Existing roster documents need no migration. Runtime identity writes are fenced while an operation is unfinished, including after the CLI or daemon restarts. The operation shares the restore lock during execution.

Normal board requests remain available. Old direct typing/control/probe jobs are cancelled across takeover; the destination's own initial setting is tagged with its operation ID. Orientation includes a bounded recent board handoff with original author labels plus the optional operator note (UTF-8, up to 8 KiB). It preserves instruction-private sections and does not advance the historical board cursor.

`swap MEMBER --status` returns the last operation and agent history. `--retry` continues an unfinished operation using its reserved pane and original settings; a layout-response timeout recovers the tab by its unique label. `--cancel` is available before takeover: close an owned reserved replacement and retain the original member configuration/session reference. If the source is gone, the member becomes missing and can be resumed from a shell. After takeover, retry finishes briefing setup. Both paths refuse to close a changed or unrelated replacement pane. If the source moved or changed, cancellation leaves it untouched and releases the member for explicit rebinding.

JSON execution returns `{team, member, swap}`; failures during execution also include `error` and exit 1. Preflight/authority errors use the standard error object on stderr. Dry-run JSON includes `{team, member, name, role, kind, model, effort, profile, argv, cwd, workspace_id, fresh:true, dry_run:true}`. Status also includes `agent_history`. Progress goes to stderr. `agent_swapped` records completion and `swap_control_cancelled` records discarded controls on the board. No automatic provider failover or transcript conversion is performed.

Conventions used below:

- `<team>` is optional wherever the team can be inferred, in this order:
  `--team`, `HERDR_TEAM_DIR`, `HERDR_TEAM`, the caller's own roster row, the
  team of the Herdr space the call is running in, `default_team` from
  `console.json`, then the only team. Two teams and no hint:
  `team_ambiguous`.
- A **space** infers a team when exactly one team has an active agent there
  (`workspace_id` on its roster rows, matched against `HERDR_WORKSPACE_ID` /
  the plugin context / the `<workspace>:<pane>` prefix of `HERDR_PANE_ID`).
  Zero teams or two in one space infer nothing and the next rule decides.
- **Human only** means the operator from a *trusted origin*: the team console,
  a popup, outside Herdr, or a shell pane whose ancestry Herdr confirmed
  (`Author.trusted_human`, the same `human_origin_ok` rule readers apply to a
  record). A shell Herdr cannot verify still posts, rendered `(unverified)`,
  but every authority write refuses it with `author_mismatch` and names the
  way in. A process inside an agent's pane resolves as that agent on every
  tier, whatever `HERDR_PANE_ID` or `HERDR_PLUGIN_ENTRYPOINT_ID` say.
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
| `--version` | `herdr-synapse 0.17.0`; JSON `{"version","skill_version","plugin_id"}`. |
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
| 1 | refused or validation failure | `team_name_invalid`, `name_invalid`, `name_reserved`, `role_invalid`, `agent_name_taken`, `member_claimed`, `team_exists`, `team_not_found`, `member_not_found`, `author_mismatch`, `text_too_long`, `invalid_utf8`, `charter_too_long`, `ref_invalid`, `path_symlink`, `team_session_mismatch`, `team_ambiguous`, `view_foreign`, `plugin_disabled`, `plugin_not_installed`, `plugin_source_invalid`, `plugin_source_local`, `plugin_source_unsupported`, `plugin_update_failed`, `update_step_failed`, `update_step_timeout`, `skill_update_refused`, `agent_not_found`, `agent_blocked`, `agent_not_ready`, `launch_pending`, `not_an_agent`, `board_write_failed`, `roster_conflict`, `home_unset`, `internal`, `say_unverified`, `say_multiline`, `say_too_long`, `say_control_command`, `say_timeout`, `kind_unverified`, `retract_invalid`, `edit_invalid`, `interrupt_needs_recipient`, `interrupt_cooldown`, `session_unknown`, `session_unsupported`, `outside_herdr`, `command_not_found`, `pane_busy`, `member_alive`, `no_project_dir`, `workdir_foreign_file`, `instructions_too_long`, `rules_too_long`, `operator_grant` (in `needs`) |
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
       --member <target>[:<role>[:<name>]]… --brief <name|role>="<Mission>"…
       [--from-workspace <id>] [--names plain] [--rename] [--steal] [--reuse]
create <team> --new [--workspace] [--charter …] --spawn <role>:<harness>[/<profile>][:<cwd>]… [--names plain]
       --brief <name|role>="<Mission>"… [--model <role|kind>=<model>[@<effort>]]… [--unlisted]
       [--canvas]
```

- `--canvas` (0.21, the operator or a delegate): turn this team's whiteboard
  canvas on before any member starts, so Claude Code and Codex members get the
  canvas MCP tools at launch and every briefing mentions it. A canvas is per
  team and off until turned on; without `--canvas`, `whiteboard team on`
  turns it on later (members Synapse starts after that get the tools).

- `--spawn <role>:<harness>[/<profile>]`: the harness is any kind Herdr can
  start; the profile is one of that harness's own named setups (section 9c,
  `profile`). `herdr-synapse available` lists both, with the models.
- Before any pane is laid out, a profile and every `--model` are checked
  against the harness's own lists for the member's directory: an unknown
  profile is `profile_unknown` (1), a subagent `profile_not_selectable` (1), a
  harness with no profiles `profile_unsupported` (1), an unlisted model
  `model_unlisted` (1) with `suggestions`, and an effort the named model does
  not take `effort_unsupported` (1) with its `efforts`. A harness that cannot
  answer checks nothing. `--unlisted` skips the lists (a model newer than a
  harness's cache, for example).

- `--model`: `role=` or `name=` sets that member's own model and effort —
  with `--new --spawn` the agent starts with the flags; with `--member` (a
  live agent) the setting is recorded and applies at its next resume (live
  for Claude through `model`). `kind=` (`claude`, `codex`, `opencode`, `pi`) sets
  the team default for the kind (`config.models`). A kind with no verified
  flags is refused (`model_unsupported`) before anything is written; a key
  that names nobody being added is a usage error. See section 9c.

- Every `--new --spawn` launch is unrestricted by default when the kind has a switch: Synapse appends `--dangerously-skip-permissions` for Claude Code, `--dangerously-bypass-approvals-and-sandbox` for Codex, `--auto` for OpenCode, and each other kind's own flag from `permissions.YOLO_ARGS` (for example `--yolo` for Gemini, `--force` for Cursor). A kind without a switch starts with its own settings. `--permissions native` selects native agent settings for the new team; repeat `--member-permissions NAME|ROLE=yolo|native` for individual overrides. A live `--member` is not restarted and therefore keeps its current mode. Use `permissions --default MODE` to change an existing team’s default before `create --reuse`.

- `<target>`: pane id or live agent name. Role defaults to the kind label.
  Name defaults to `<team>-<role>` (`--names plain` uses `<role>`).
- Every new member needs a non-empty Mission before any roster, pane or layout mutation. Supply it with repeatable `--brief <name|role>="<text>"`, or supply long-form `--instructions <final-name>="<structured document with a non-empty Mission>"`; when only long-form instructions are supplied, the first Mission paragraph becomes the short roster brief. New documents always contain Mission, Scope, Constraints, Definition of done, Handoffs and Notes, while explicit content and custom headings are preserved. A missing Mission is `mission_required` with all missing member names.
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
  roster, store its six-section Mission document as revision 1, stamp tokens,
  enqueue the briefing job, then ensure the notifier is running.
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
`launch_pending`, `mission_required`, `charter_too_long`, `server_not_running` (3).

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

### `add <team> <target> [--role <r>] [--as <name>] --brief "<Mission>" [--steal]`

Same join routine for one member. `--brief` is required and the member's canonical six-section document is stored before its briefing job is made visible. Then posts an urgent `member_joined` system record to `all`
(`member`, `role`, `member_kind` fields): the daemon nudges every other member to read it, and the newcomer
gets the briefing instead. JSON `{"team","member":member,"renamed":bool,"notifier","briefing_job","joined_record":seq}`.

### `remove <team> <name> [--keep-name]` (operator-authorized)

Requires a trusted human origin or an active operator delegation. Clears the
three tokens and the pane label, marks the member `left`
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

The argv carries the member's effective model and effort flags (section 9c),
and, while the team's canvas is on, the canvas MCP flags (section 9n);
`--print` shows them.

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
| `herdr:droid` | `droid --resume <id>` | `herdr:pi` | `pi --session <absolute-path>` |
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

On a terminal it asks, naming what happens, since "dissolve" does not say it,
and naming what goes into the archive with the board (the canvas marks, the
current facts, the posts and the work items, read from the live team dir before
the rename); off one, `--yes` is required (`confirmation_required`). Declining
exits 0 with `{"team","dissolved":false}`. JSON on success
`{"team","archived_to":"…","members_cleared":n,"recoverable":{…}}`.

The result then lists what is recoverable from the archive - canvas marks with
their comments and pictures, current facts, the operator's rules, board posts and
work items - and the two commands that bring them back (`knowledge import --from
<team>`, `canvas import --from-archive <team>`). Only non-zero counts are
printed, and a count that cannot be read omits its line rather than fail a
dissolve that has already happened. See [inheritance.md](inheritance.md).

In the team view (`prefix+t`), `x` on a team or one of its members dissolves it
after the same question.

### `use <team>`

Sets `default_team` in `console.json`. JSON `{"default_team":"<team>"}`.

This is the session-wide fallback. A space that holds exactly one team infers
that team first, so `use` decides calls made from spaces that hold no team;
inside a team's own space, pass `--team` to mean a different one.

### `teams`

Works offline. JSON:

```json
{"teams":[{"team":"vuln-hunt","members":3,"socket":"…","running":true,"default":true,"team_dir":"…"}],"session":"default"}
```

### `rename <old> <new>` (operator-authorized)

Requires a trusted human origin or an active operator delegation. `agent
rename` plus roster update plus a board note; the old name resolves
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

The document has six known sections: `Mission`, `Scope`,
`Constraints`, `Definition of done`, `Handoffs`, and `Notes`, which is private
and never sent to the agent. A heading the plugin does not know is kept in
place. Guidance for each section rides in HTML comments, which are invisible in
rendered Markdown and never injected. Plain text with no headings becomes the
`Mission`, so `--set "one sentence"` behaves as it always did, and a pre-0.6
instructions file needs no migration. Mission is required when a member is created or added; the other sections remain optional during creation and later edits.

The authoritative copy lives in the team state dir, which no agent can reach
through the project checkout. When the team has a project directory the
document is mirrored to `members/<name>.md`.

| Mode | What it does |
| --- | --- |
| `--set`, `--file` | replace the document from text or a file; a file over the limit is refused, never truncated |
| `--edit` | open it in `$VISUAL`/`$EDITOR`, exactly as `charter edit` does |
| `--adopt` | import the document in `members/<name>.md`, after showing a unified diff and asking (`--yes` skips); the render after it copies the file into `inherited/members/`, checks the copy, and only then writes this team's version (or leaves the file as it is and says why) |
| `--discard` | write this team's version over the file in the folder, after the same checked copy |
| `--clear` | remove the document |

In manual sync mode, edited member files are preserved until `--adopt` shows the diff and imports them. In auto mode, a settled edit of a document this team wrote is proposed with its full text; only the operator's `project confirm <id>` stores it and notifies the affected member. Neither mode adopts an edit by itself. Private Notes remain excluded from agent context.

Every write bumps the member's `instructions_seq` and appends an
`instructions_updated` record addressed to **the member and to `all`**, so the
member is nudged (a record addressed only to `all` is a broadcast, which the
delivery gate holds) and teammates still learn who owns what. `--urgent`
nudges everyone at once. With no name, shows your own.

JSON `{"team","member","chars","path","record_seq","instructions_seq"}`;
`--adopt` adds `"adopted"`, `"diff"`, and either `"snapshot"` (the checked copy the
render took) or `"held"` (the hold that kept the file as it is).

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

### `knowledge import --from <team|path> [options]` (human only)

Adopts another team's durable record into this one: its rules, its current facts
and its canvas, in that order, each step independent. The source is a team
dissolved in this session (by name), a team's folder in a project directory, or
an archived team directory. Options: `--yes` (adopt the rules without asking,
and replace rules this team already has), `--no-rules`, `--no-facts`,
`--no-canvas`, `--dry-run`, and the canvas flags `--keep-authors`,
`--team-can-edit`, `--skip-unknown`, `--skip-missing`.

The Rules it would adopt are printed word for word, in `--dry-run` and in the
real run (and are in the JSON as `rules.text`), because this command is the door
through which a file found in the folder becomes operator authority. A real run
that would adopt rules into a team with none asks first (`adopt these as team
<team>'s operator Rules?`); `--yes` and `--json` do not ask, and with no
terminal and no `--yes` the import refuses with `confirmation_required` and
changes nothing. Rules are stored through `knowledge set`. Facts come from the dissolved team's
own `facts.jsonl` when it is in the archive, else from the `facts.md` the project
mirror wrote (`facts.parse_mirror`), and are recorded through
`facts.add_inherited`: author the source team, `by_kind: "inherited"`, the
source's own sources plus one `{"kind":"team","team","fact","at"}` record,
`recorded_at` now, validity window carried, retired and superseded rows and
disputes left behind. Before the canvas, every file the source board reads under
`artifacts/` is copied into this team's own `artifacts/` (at most
`canvas_import.MAX_ARTIFACT_BYTES` each and `MAX_ARTIFACTS_BYTES` in all, never
overwriting), because a chart's data file resolves against the importing team's
folder; the result carries `artifacts: {copied, omitted, dir, from}`. The canvas
is then delegated to the `import` op, and the summary names what it left out.
Member documents are listed, never adopted (`instructions <name> --adopt`).
Every file is read where it is. When the source is this team's own folder, the
bytes each successful step read are released, so the next render may write this
team's version over them after a checked copy into `inherited/`; a file that has
changed since the import read it stays held.

Two system records: `canvas_imported` to `all` when a canvas arrived, and
`knowledge_imported` to `human` summarising every step. Exit is `EXIT_REFUSED`
only when every step asked for was refused. `create --inherit <team|path>` runs
it once the new team exists, with the same option names (no `--yes`: `create`
already owns that flag, and a new team has no rules to replace).
JSON `{"from":{team,kind,dir,stamp},"rules":{…},"facts":{…},"artifacts":{…},"canvas":{…},"members":[…]}`.

## 5a. The team working directory

### `project`

Shows the team's project directory and the folder inside it, or `none`, the
sync mode, every **held** file and every copy taken before a replacement you
ordered. A held file is one the mirror left exactly as it is because it does not
hold the bytes this team last wrote there; each is listed with its reason and
the commands that resolve it, and one that has changed since the last render
looked at it says so. JSON carries `sync_state` per mirrored file (`digest`,
`pending`, `since`, `error`, `hold`), `held` (records: `path`, `kind`, `member`,
`reason`, `why`, `commands`, and `stale` when the file changed since) and
`copies` (`path`, `snapshot`, `at`, and `intact`, which is true only when the
copy still holds the bytes it was taken of). `pending` lists proposals with
their consent id, file digest, complete text and the baseline reviewed when proposed.

The reasons: `found` (this team has no record of writing what is there now),
`changed` (it changed after this team last wrote it), `proposed` (waiting for
the operator's confirmation), `replaced` (another team instance rendered it), `unreadable` (a symlink,
not a regular file, or an I/O error), `too_large` (over 1 MiB, so it cannot be
copied and is never replaced), `copy_failed` (a replacement you ordered could
not make and check its copy), `moved` (it changed while a render was writing
it), `deleted` (in `auto` mode, a synced document deleted after this team wrote
it, which sync never recreates by itself: `project render --force` or `project
sync manual` writes it again), and, for `canvas-assets/`, `unnamed` (no mark
names it and this team has no record of writing it) and `conflict` (its bytes do
not match its name). Every command a hold prints names the team (`--team
<name>`) and shell-quotes its paths. A file found holding exactly this team's
version is not held, but it is only *claimed*: this team's next version of it is
held `found`, because finding a file is not writing it. Positive older-plugin
state grants per-path upgrade grace only to knowledge and member documents
that plugin wrote, not the newly added facts or canvas mirrors. A held file you
remove yourself is no longer listed, except a synced document in `auto` mode,
which becomes `deleted`. After `mv`, `project set <new path>`
carries what this team recorded over to every file whose bytes are unchanged.

### `project set <path>` (human only)

Also available at team creation as `create --project <path>`, and as a stage
in the `prefix+t` wizard after the optional team-rules stage, which prefills the directory the selected agents
already share (Tab skips it). Records the directory and creates `<path>/.herdr-synapse/<team>/`. This is the
consent gate: the plugin never infers a project directory and never writes
into a repository until a human runs this. The path must be an existing
directory, and the filesystem root, `$HOME`, and the plugin's own state dir
are refused.

The folder holds a generated `README.md` and `.gitignore` at the top level, and
per team a `knowledge.md`, a `members/<name>.md` per member, `facts.md` (with
`facts-2.md`, `facts-3.md`, ... for a long record), `canvas.json` with
`canvas-assets/`, `canvas.md`, `inherited/` (the copies taken before a
replacement you ordered), `exports/`, `board.md`, and an `artifacts/` directory
members own outright. The records are a rendered mirror of state that lives
elsewhere, and the mirror writes a durable record in the team's folder only where the path
is empty or the file still holds exactly the bytes this team last wrote there:
anything else is held, never written over and never imported by itself.
`board.md` and `canvas.md` are git-ignored views nothing reads back. A
marker-bearing view is regenerated; a file without the marker stays untouched
until you move it aside, including with `--force`. The shared `README.md` and `.gitignore` are regenerated
too; one without the plugin's marker on its first line is left alone unless
`--force`, which writes over it without a copy.
`canvas.json` carries its marker as a reserved first-line `"//"` key, and
`canvas-assets/` is unmarked because the content hash in each name *is* the
check.

`.gitignore` covers `artifacts/`, `exports/`, `board.md`, `canvas.md` and
`inherited/` per team. What is committed is what a new team needs to use the
record: `knowledge.md`, `facts.md` and its parts, `members/`, `canvas.json` and
`canvas-assets/` ([inheritance.md](inheritance.md)). `inherited/` stays on the
disk of the checkout that wrote it and does not travel with a clone.
Caps: 64 KiB for the Markdown mirrors, 8 MiB for `board.md`, and 1 MiB for
`canvas.json`, which refuses rather than truncates - over the cap it is written
as a valid JSON pointer (`"elements": null`) naming the scene file and the export
and import commands, and `canvas.md` says so in a line. `facts.md` is never
truncated: past 256 KiB the facts continue in `facts-2.md`, `facts-3.md`, ...,
each under 256 KiB, and `facts.md` says how many parts there are and how many
facts they hold, which `knowledge import` checks what it read against. A part a
shorter record no longer needs is rewritten to say it is empty, never deleted.

### `project clear` (human only)

Stops the plugin writing to the folder. Nothing is deleted by `project clear`
itself, and a member that is removed or renamed gets a tombstone or a
forwarding note written over its file rather than a deletion.

The plugin deletes in one place under a project directory: `canvas-assets/` is
**pruned**, because an asset name is a content hash and every re-paste or re-crop
of one picture would otherwise leave another permanent blob in a committed
checkout. The prune runs only after `canvas.json` has been written, removes only
a file no mark on the board names any more, and only one whose bytes this team
wrote there (the digest recorded when it wrote the file, so a picture a `git
pull` replaced under a name this team wrote is not ours). Any other file stays
where it is and is reported once.

### `project render [--force]`

Regenerates the mirror, including the canvas mirror (`canvas.md` and
`canvas.json`), so the folder is current without a notifier running. Runs
automatically after a roster change and after writes to rules or member
instructions. A file this team did not write, or one changed since it wrote it,
is held and not written; `holds` lists every hold this render met and
`inherited` the holds and checked copies nobody has been shown yet, which the
notifier boards once. The text lists every hold on every render, so a held
folder never goes quiet. The result also carries `canvas: {elements, version,
assets, path, reason, too_large, assets_pruned, assets_held}` and, when the
canvas mirror could not be written, `canvas_reason`.

`--force` (the operator) writes this team's version over replaceable held durable records it
meets. For each one it reads the file once, writes a copy to
`<team>/inherited/<name>-<digest>.<ext>` (a member document's under
`inherited/members/`; the digest is of the bytes, so the same bytes are the same
copy), reads the copy back, and writes only if the copy matches and the file
still holds the bytes it read. If not - the file is over 1 MiB, `inherited/` is
not a writable directory, a different file already has the copy's name, or the
file changed meanwhile - it changes nothing and the hold says why, with the `mv`
that moves the file aside to a name nothing has yet. `--force` also writes
`board.md` when its path is absent or marker-bearing (a plain render leaves it
to the notifier), and writes
a synced document that was deleted in `auto` mode. `--force` never adopts anything, and never deletes a
picture this team did not write. Named canvas assets with conflicting bytes stay untouched even with force;
move them aside before mirroring again. The render after `instructions <name> --adopt`,
`instructions <name> --discard`, or a `knowledge import` / `canvas import` from
this team's own folder, or `project confirm`, replaces the file that command read the same way, and
only while it still holds those exact bytes.

### `project sync auto|manual`

Neither mode adopts saved edits by itself. New teams default to `auto`;
existing teams keep their configured mode. Auto proposes a settled edit of the
Rules or instructions of a document **this team wrote**. The file stays held,
and the operator receives the full text plus `project confirm <id>`. Manual
only holds and lists edits for `knowledge import --from <folder>` or
`instructions <name> --adopt`. Switching manual to auto applies nothing.

An edit with nothing to adopt stays held and reported. A file this team did not
write, only claimed, or another instance fully rendered is never proposed as
its own edit. `project` shows the mode and holds; JSON also includes proposals
and errors. Confirmation uses the same verified-operator checks as `knowledge set`.

The notifier waits for two unchanged scans at least two seconds apart before proposing. Confirmed changes travel through the usual rules or instructions board records and revision acknowledgements. Findings stay generated: edit only Rules and use `knowledge add` for findings. Malformed files, symlinks and changed Findings are held and reported. Deleting a synced file does not clear stored text or regenerate it in auto mode; `project render --force` or `project sync manual` writes it again.

### `project confirm <id>` (operator only)

Adopt one auto-mode proposal after reviewing its complete text, shown on the
board and by `project`. The command prints exactly the text it stores. The id
binds the file bytes, current project, team instance, stored source and revision,
and member identity. A changed file or context makes the old command refuse;
the next scan can issue a new proposal and id. A mode change to manual also
refuses the old confirmation. Markers identify the instance that rendered the
file; they cannot authenticate its editor.

The following render replaces the edited file only after a verified copy, as
above. A failure to record consent initially adopts nothing. An error after
text was saved is reported with the saved text and a warning, and the file may
remain held. A consumed id cannot store the same change twice. See
[the remaining check/replace race](inheritance.md#the-contract).

### Native conversation names

Fresh CLI-created Claude Code, Codex, OpenCode and Pi sessions use the full member name as their native conversation title. Claude and Pi receive `--name`; Codex receives `/rename` through its idle composer before briefing; OpenCode updates the exact reported session through its managed loopback server. Resuming a conversation preserves its existing title. Naming failures are reported without preventing team operation, and ambiguous Codex command submissions are not blindly replayed.

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
skipped. `artifacts_changed`, like other system awareness, deliberately does
not hold Claude's Stop hook open; it still reaches `board --new` and the
prompt-submit context.

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
 "instructions_path":"…","knowledge_path":"…","board_path":"…","instructions_stale":false,
 "whiteboard":{"team":"vuln-hunt","layer":true,"enabled":true,"viz_enabled":true,"on":true,"viz":true,"version":42|null,
               "line":"whiteboard: on · live visuals on · canvas v42; how to draw: herdr-synapse skill get --reference canvas"}}
```

`whiteboard.line` is also a line of `me`, `orient`, the Claude session-start
context and `brief --format context`: `whiteboard: off`, `whiteboard: off for
this team`, or the `on` form above (`version` only while on; section 9n).

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
- `@name` inside a post's text is prose, not a selector: only `--to` decides
  who gets the post and who is nudged. A post that *opens* with a teammate's
  `@name` and has no `--to` is told so once, because it reads like the
  console's `@name text` (the warning names the member and the `--to` to use).
  A mention anywhere later in the text (`post "please ask @alpha-worker to
  redraw"`) is not warned about and still goes to the whole team: on the board
  a mention in the middle of a sentence is just a way of writing a name. Only
  a canvas comment's `@name` addresses anyone (`canvas comment`, section 9n).
- `--interrupt` (implies `--urgent`) marks a post that could not wait. The
  notifier may type its nudge into the recipient's *running turn* when the
  recipient's kind is in `config.gate.interrupt_kinds` (default `claude`,
  `codex`, `opencode`, `pi`, all live-verified to accept a line mid-turn) and the
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
      [--receipts] [--format text|json|context] [--limit N] [--max-bytes N] [--max N] [--name LABEL]
```

- Default (no `--new`/`--peek`): `--last 30`, no cursor change.
- `--new`: posts to me or `all` since my cursor, `--limit 100`, 32 KiB cap,
  advances the cursor to the highest seq printed after stdout is flushed.
- `--peek`: same selection, never advances. Hooks only peek.
- **The archive is read when a view reaches into it.** The active file moves to
  `archive/` at every rotation (4 MiB). When the reader's cursor is older than
  the active file, `--new` and `--peek` read the archive too and page through
  it like any backlog: the cursor stops at the last post printed, so nothing is
  skipped however far behind the reader is. A reader who is current reads the
  active file only. `--last N` and the default view also read the archive when
  the active file holds fewer than N records, which is the case right after a
  rotation. Before 0.22.1 a reader who was behind at a rotation was never shown
  the posts that rotated away, and its cursor advanced past them; the unread
  counts in `me`, `who` and `brief` undercounted the same posts.
- `--name LABEL`: the operator's own label, which names the human cursor over
  the operator's inbox. **A member passing it is refused** (`usage`, exit 2).
  It is not a way to read a teammate's mail and never was: a member is already
  an identity, so the label was dropped and the read returned the member's own
  inbox under its own cursor. That is a behaviour change in 0.22: a member
  script passing `--name` starts failing, which is the point. The same refusal
  applies to `post --name`. A human path that may not use the label is told so
  in one sentence on stderr instead of having it silently dropped.
- An empty `--new` read states the cursor it is holding at and, in the same
  words `ack` uses, names what is unread but was not shown — so
  `0 posts … (cursor 135, held); 3 unread posts you have not been shown` can
  never again read as an empty inbox.
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

`default_team` stays session-wide, and is now the fallback rather than the
first answer: a session-wide default cannot be right in two spaces at once, so
the space a call runs in is consulted first (see Conventions above). `use`
still sets it, and it still decides every call from a space that holds no team
of its own. A pre-registry document (one flat record) is read as a one-entry
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

Records `charter_seq_acked`, `instructions_seq_acked` and `rules_seq_acked`, and
moves the cursor forward over everything the member has been shown, stopping
right before the first record it has not (`store.is_member_awareness`: addressed
to it or `all` by someone else, not a retract, direct line or delivery
bookkeeping; `seen` seqs count as shown). A post that lands between `board
--new` and `ack` therefore stays unread and keeps its pending nudge; the text
output names it and says to run `board --new`.
JSON `{"team","member","cursor":59,"charter_seq_acked":3,"instructions_seq_acked":2,"rules_seq_acked":1,"unread":0}`.

### `say <member|all> "<text>" [--force] [--wait | --no-wait] [--timeout S]`

```
say <member|all> "<text>" [--force] [--wait | --no-wait] [--timeout S]
```

Type one line into a member's input box right now, with operator authority,
and record it on the board. The console's `!<member> <text>` runs this;
`!!<member> <text>` adds `--force`, and `!!all <text>` runs the forced form
for every current agent.

- Human only, and only from the verified team console: the author must be
  `console`, verified, with confirmed pane ancestry. A member pane or a hook
  is `author_mismatch` (1); a shell pane, the compose popup, a terminal
  outside Herdr, an unfocused console, or a console whose process ancestry
  cannot be confirmed is `say_unverified` (1). Both are audited. A shell pane
  is refused on purpose: any agent can open a pane around a command and
  mint a verified shell author that way.
- One agent member by default. `all` is accepted only with `--force`
  (console: `!!all`); it is expanded into a separate direct record and job
  for every current non-human member. Every target kind is validated before
  anything is written, so one untrusted kind refuses the whole fan-out.
  `human`, `me`, and `role:<r>` are `member_not_found` (1) with a hint. A
  member's kind must be trusted (`kinds trust <kind>`), else
  `kind_unverified` (1).
- Text: sanitized like a post, then tabs become spaces. A newline is
  `say_multiline` (1), more than 500 characters is `say_too_long` (1), the
  marker check is `echo_rejected` (4) and a secret is `secret_detected` (1);
  none of these is bypassable. A first token in `/exit /quit /clear /logout
  /login /resume exit quit` is `say_control_command` (1) unless `--force`.
- Needs the daemon: `daemon_down` (5) before anything is written. Then a
  `direct` record (`from: human`, `to: [<member>]`, `kind: direct`, extra
  `force`) is appended and a `say` job carrying only its seq is queued. For
  `all`, this pair is repeated once per target; there is never a
  group-addressed direct record. The daemon types each record's text as-is
  (never a job payload, never a `[herdr-team` header) after checking that the
  record is the console's own and is addressed to exactly that member.
- Without `--force`, the daemon sends `agent.prompt_if_idle` with the terminal
  identity and state sequence returned by `agent.get`. Herdr checks both and
  the idle state in the same app turn that queues the input. A concurrent
  state change is `state_changed`, a newly working member is `working`, and a
  replacement is `wrong_occupant`; none writes bytes. At every connection the
  daemon sends an empty, zero-write probe to the *running server*, because a
  release string does not prove that an optional API method exists. Without
  the method, plain delivery is `capability_unavailable` and its detail points
  to `@name text` for board delivery; it never falls back to a racy
  check-then-prompt sequence. `--force` intentionally keeps `agent.prompt`.
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
  `absent`, `wrong_occupant`, `wrong_target`, `state_changed`,
  `capability_unavailable`, `update_required` (legacy history), `in_flight`, `stale`, `unverified_source`,
  `member_not_found`, `kind_unverified`; `failed`
  carries `hung`, `transient`, or `unconfirmed` (still idle 5 s after typing
  with the text gone from the prompt line; `not_submitted` when it is still
  there). Typing into a running turn is verified for Claude Code, Codex, OpenCode and Pi; for other kinds `force_verified` is false and `detail` says so. Pi processes native steering at the next steering boundary.
- `--wait` (default) polls for that record up to `--timeout` (10 s) and
  returns it as `outcome`; a refused or failed outcome is still exit 0. An
  `all` fan-out shares one deadline and returns an outcome in each delivery.
  No record in time is `say_timeout` (1) with the pending target details.
  `--no-wait` returns `outcome: null`; the console uses it and reads every
  outcome from the board tail.
- Neither record is mail: a member's `board --new`, `--peek`, `--to me`,
  unread count, Claude Stop hook, and prompt context never include them, and
  they never nudge. `board`, `board --kind direct`, `show`, and `--thread
  <seq>` show both; the human's `board --new` lists the `typed` outcome.
  `edit` and `retract` refuse a `direct` record (`edit_invalid`,
  `retract_invalid`, 1): send a correction with another `!` line.

JSON `{"seq":12,"team":"alpha","member":"alpha-worker","job":"3f9a1c0b2d","force":false,"text":"stop and summarize","author":{"name":"human","via":"console","verified":true},"waited":true,"outcome":{"seq":13,"result":"typed","reason":null,"detail":null,"elapsed_ms":812}}`.

Forced `all` JSON uses the same per-target fields under `deliveries`: `{"team":"alpha","member":"all","force":true,"text":"stop and summarize","author":{"name":"human","via":"console","verified":true},"waited":false,"deliveries":[{"member":"alpha-reviewer","seq":12,"job":"3f9a1c0b2d","outcome":null},{"member":"alpha-worker","seq":13,"job":"4a0b2d6e8f","outcome":null}]}`.

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

### `wipe [--yes] [--purge] [--reason TEXT]` (human only)

Empty the board. Without `--purge`, the store's rotation is forced: the active
file moves to `archive/board.<first>-<last>.jsonl`, `board.seq` continues, and
the fresh board opens with a `board_cleared` system note (`by`, `reason`,
`cleared_first_seq`, `cleared_last_seq`) addressed to `all`. It remains visible
on the board and in hook context but does not wake a member or hold Stop open.
Cursors are untouched; `board --since 1` still reads the archive. With
`--purge`, the archive segments, the
index and every payload are deleted too. Both ask on a terminal; off one,
`--yes` is required (`confirmation_required`, 1). Audited as `board_wiped` /
`board_purged`. JSON: `{"team","wiped","records","archived_to","note_seq",
"first_seq","last_seq","purge","purged_segments","purged_payloads"}`. The
notifier drops its asks, link inbox and pending nudges for that team when it
ingests the note. Console: `/wipe [--purge] [reason]`, y/n first.

## 8. Delivery (enqueue to the daemon; exit 5 `daemon_down` when it is dead)

| Command | Effect | JSON |
| --- | --- | --- |
| `brief <name>` | enqueue a briefing job (full gate) | `{"team","member","job":"<id>"}` |
| `nudge <name> [--force]` | enqueue an immediate nudge evaluation over unread authored mail; system/control history is ignored; terminal automatic deliveries are retried only with `--force`, which also skips `done_hold` and the interval, never the gate | `{"team","member","job"}` |
| `mute <name> \| --all [--for 10m]` | write `mute.json`; daemon and Claude shim honour it | `{"team","muted":{"*":"<until>\|null,"<name>":"<until>"}}` |
| `unmute <name> \| --all` | | same shape |
| `pause` | alias `mute --all` | same shape |
| `focus <name>` | enqueue `agent.focus` on the member's current pane | `{"team","member","job"}` |
| `say <name\|all> "<text>" [--force]` | write a `direct` record and enqueue a type-now job (section 7); `all` requires `--force` and creates one independently verified delivery per current agent | one target: `{"team","member","seq","job","outcome"}`; all: `{"team","member":"all","deliveries":[…]}` |
| `interrupts [show\|off\|on\|<kind>[,<kind>]] [--cooldown 10m]` | show or set `config.gate.interrupt_kinds` (the kinds a teammate's `post --interrupt` may be typed into mid-turn; `on` restores `claude,codex,opencode,pi`) and `interrupt_cooldown_ms`; the daemon reloads within 2 s; a change is human only (`author_mismatch`) | `{"team","kinds","cooldown_ms","default_kinds","changed"}` |
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
`{"session_dir","started":true|false,"already_running":bool,"replaced":bool,"daemon":{"pid","start_time","socket","herdr_version","protocol","version","capabilities":{"atomic_idle_prompt":true|false|null}}}`.
Errors: `herdr_version_mismatch` (1) without `--allow-version`,
`socket_not_allowed` exits 0 with `{"skipped":"socket_not_allowed"}`.

### `daemon stop`

JSON `{"stopped":bool,"pid":N|null}`.

### `daemon status`

JSON `{"alive":bool,"pid","start_time","beat_age_s","socket","socket_source","herdr_version","protocol","version","capabilities":{"atomic_idle_prompt":true|false|null},"teams":["…"],"pending":{"<team>":n},"ledger":{"wrong_target":0,"landed_working":n,…}}`.

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
which carries the window size outright), and OpenCode's `opencode.db` plus its
cached provider/model catalogue. Pi uses its native extension's context estimate and active model limit. Claude resolves the observed model's published
limit; OpenCode resolves `providerID` + `modelID`; the configured model is only
a fallback when a record omits identity. Nothing is typed and nothing is asked
of the agent. A kind with no reader is `unknown` and is never guessed at. The
notifier polls the same readers every 15 s, so this prints the notifier's
reading when it is running and reads the files itself when it is not
(`"source":"who.json"` or `"files"`).

JSON `{"team","source","members":[{"name","kind","context":{"used":number|null,"window":number|null,"percent":number|null,"source","model","at","estimated"?:true}|null}]}`. Pi snapshots are freshness-checked even when `who.json` is available; unknown usage stays null and estimated readings are labelled `~`.
When the token count exists but the model window is unknown, human output keeps
the exact count and says `window unknown`; no percentage or warning is emitted.
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
`/compact`, OpenCode `/compact`, Pi `/compact`. It is typed with `pane.send_text` and a
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

The same path for the kind's clear operation: Claude `/clear`; Codex `/new`;
OpenCode `/exit` followed by a fresh full TUI for the same member. Codex
deliberately gets `/new` rather than `/clear`, whose scrollback wipe removes
the surface Herdr's detection reads. OpenCode clear first reads `pane.layout`
and refuses with `pane_too_narrow` before exiting below width 38 (about a
40-column PTY), because OpenCode 1.18.30 crashes while starting that narrow.

Operator only, and `--self` is refused (`author_mismatch`): a member may ask
to be cleared by posting a request, but throwing away an agent's working
memory is the operator's call. It asks `y/N` on a terminal, and `--yes` is
required otherwise, `--json` included.

A clear mints a new harness session, which the notifier already reacts to: a
new generation, `briefed_at` cleared, a fresh briefing. What this adds is
attribution, so the resulting `member_restarted` is followed by a
`context_cleared` record naming the operator rather than reading like a crash.

## 9c. Model and effort

### `models [show | set <kind> <model>[@<effort>] | clear <kind>]`

Team defaults per kind in `config.models`. Read is open; writes are human only
and audited (`models_set`, `models_clear`). JSON for `show`:
`{"team","models":{"claude":{"model","effort"}},"kinds":[…],"efforts":{…}}`.

### `model [<member>] [<model>[@<effort>]] [--effort E] [--self] [--apply live|next|restart] [--reason TEXT]`

With no arguments, every member's effective setting, where each half comes
from (`member`, `default`, `harness`), and the model the harness reports
(`who.json` context). With a member and no setting, that member's. With a
setting, a write.

- **Setting** `<model>[@<effort>]`, either half optional (`opus@medium`, `opus`,
  `@high`); `--effort` is the effort half on its own. Harness-native
  vocabulary, validated per kind: Claude `low|medium|high|xhigh|max`, Codex
  `minimal|low|medium|high|xhigh|max|ultra` (each model takes a subset, read
  from Codex's own `models_cache.json`), OpenCode any token (provider-specific).
  The model and the effort are also checked against the harness's own list as
  `create` does (`model_unlisted`, `effort_unsupported`; `--unlisted` skips it).
  Another kind: `model_unsupported` (1). Unknown effort: `effort_unknown` (1)
  with the vocabulary in `efforts`.
- **Authority**: the operator or a delegate (anyone), the team manager
  (anyone), a member for itself (`--self`). Anything else is `author_mismatch`
  (1), audited.
- **Record**: the roster row's `model`/`effort`; audit `model_set`; a
  `model_changed` system record to `[member, all]` (the member is nudged,
  ordinary gates apply).
- **`--apply`**: default `live` for Claude and for an OpenCode effort-only
  change; `next` otherwise.
  - `next`: nothing else; it applies at the next `resume` or restart.
  - `live`: for Claude, a `direct` record with `control: {"action": "model",
    "keystrokes": ["/model …", "/effort …"]}`; for an OpenCode effort-only
    change, `/variants` plus the native selection. The notifier types each
    step once the member is idle. Claude closes when the transcript reports
    the model (`model_applied`), or on typing when only effort changed;
    OpenCode closes after the selection. Unsupported live combinations return
    `model_apply_unsupported` (1).
  - `restart`: needs a recorded session (`session_unknown` otherwise). A
    `direct` record with `control: {"action": "restart", "exit", "argv"}`; the
    notifier types the kind's exit command when idle, waits for the pane to
    empty (`RESTART_EXIT_S`, 30 s), starts the harness again in that pane with
    the resume argv plus the flags (`RESTART_START_S`, 90 s), and closes the
    job when the same expected session is reported again (`model_applied`,
    `restarted: true`). The returning record and launch argv are validated;
    allowlisted harness policy flags are preserved, Codex's update picker is
    disabled for the controlled start, and OpenCode effort is selected after
    startup.
    Either bound missed: `restart_failed` to `[human, all]` naming
    `herdr-synapse resume <name>`; the member is left as it was. While a
    restart is open the member is never marked missing.
- Exit codes as `compact`; a control record needs a verified origin, so a
  popup or an outside shell can record and `--apply next` but not drive a
  keystroke.

JSON (write): `{"team","member","kind","model","effort","setting","apply","by","job","record_seq","control"}`.
A restart's `control` also carries the member's `profile`, which the argv includes.

### `profile [<member>] [<profile> | --clear] [--self] [--apply next|restart] [--unlisted] [--reason TEXT]`

Which of its harness's own named setups a member launches with (0.20): an
OpenCode or Claude Code agent (`--agent NAME`) or a Codex profile (`-p NAME`,
`$CODEX_HOME/NAME.config.toml`). Kimi 0.29 refuses `--agent` outside its
experimental print mode, so it has none; Codex ignores a profile it does not
have, and any Codex profile runs Codex without its shared background server. With no arguments, every member's;
with a member, that member's; with a profile or `--clear`, a write.

- **Check**: the name must be one the harness lists for the member's directory
  (`profile_unknown`, `profile_not_selectable`, `profile_unsupported`, all 1);
  `--unlisted` skips the check.
- **Authority**: as `model`: the operator or a delegate, the manager, or the
  member itself with `--self`; anything else `author_mismatch` (1), audited.
- **Record**: the roster row's `profile`; audit `profile_set`; a
  `profile_changed` system record to `[member, all]`.
- **`--apply`**: `next` (default) applies at the next `resume`, restore, swap
  within the same harness, or controlled restart; `restart` queues the same
  restart control as `model --apply restart`, with the new profile in its argv,
  and is refused (`daemon_down`, nothing written) while no notifier runs.
  A stored profile replaces any profile selector the running process had.

JSON (write): `{"team","member","kind","profile","apply","by","job","record_seq","control"}`.

### `available [<harness>] [--cwd DIR] [--provider NAME] [--search TEXT] [--all]`

What you can build a team from, read-only and open to anyone. Without a
harness: the agents running in this session that are in no team (`running`,
null when Herdr cannot be asked), and every harness installed here
(`harnesses`), each with `version`, `yolo` flags, `profiles` (with `source`
and `selectable`) and `models` (`id`, `provider`, `efforts`,
`default_effort`, `context`, `thinking`, `hidden`), `models_source` and
`models_authoritative`. With a harness: that one in full, the models filtered
by `--provider` and `--search`; `--all` adds hidden models and harnesses that
are not installed. `--cwd` is the directory the members will work in, because
project profiles and OpenCode's models depend on it.

Where each answer comes from: OpenCode `opencode agent list` (internal agents
dropped, subagents marked) and `opencode models`; Claude Code
`<project>/.claude/agents/*.md` and `~/.claude/agents/*.md`, with the model
aliases and the names Synapse knows (not authoritative: Claude takes any
name); Codex `$CODEX_HOME/*.config.toml` and `$CODEX_HOME/models_cache.json`;
Pi `pi --list-models`. Nothing calls a provider. JSON:
`{"cwd","running":[{"pane_id","kind","state","cwd"}],"harnesses":[…],"trusted":[…]}`.

## 9d. Links between teams

Two teams of the same session may talk through their **managers**. The link is
a session-level fact in `sessions/<slug>/links.json` (`herdr_team/links.py`):
`{"links": [{"id": "<a>--<b>", "teams": [a, b], "status": "active"|"broken",
"created_at", "created_by", "broken_at", "note", "history"}]}`; the teams are
sorted, so a pair has one id. Cross-session links are out of scope.

### `link <team> <other-team> [--note TEXT]` (human only)

Both teams must exist and both must have a manager (`link_no_manager`, 1,
naming the team). Creates or re-activates the link, audits `link_set` on both
teams, and appends `link_established` to both boards `[manager, all]`
(`wake: named`) with `other_team`, `other_manager` and the command to post.
Idempotent: an active link reports `created: false` and announces nothing.

### `unlink <team> <other-team>` (human only)

Marks the link `broken` (kept for history), audits `link_broken`, appends
`link_broken` to both boards. `link_missing` / `link_broken` (1) when there is
nothing to break.

### `links [--all]`

Every active link with its state — `active`, `paused: <team> has no manager`
(a manager was cleared after linking; sends refuse until one is set), or
`broken` with `--all` — and both managers.

### `post --to team:<other>`

The recipient token resolves when an active link exists and both endpoints
have a manager (`link_missing`, `link_broken`, `link_no_manager`). Authority:
this team's manager, the operator, or a delegate; a plain member is refused
(`author_mismatch`) and pointed at its manager. It goes alone (no other
recipients) and without `--interrupt`, `--spill`, `--attach`, `--file`; `--ref`
paths travel as text. Two records are written, each under its own team lock in
team-name order, journalled in `<session>/link-outbox/<message id>.json` first:

- the **delivered copy** on the other board: `to: [<their manager>]`,
  `from_team: <this team>`, `link: {id, from_team, to_team, reply_to_id}`;
- the **mirror** on this board: `to: ["team:<other>"]`, `link: {..., "mirror": true}`.

`--reply-to <local seq>` on a link record carries `reply_to_id`; the delivered
copy's `reply_to` is resolved to the other board's local seq by a bounded read
(`LINK_THREAD_LOOKBACK`). JSON adds `link: {id, other_team, other_manager,
delivered_seq, mirror_seq, reply_to_id, queued}`. `--wait` is a usage error here (only
`human` answers a wait).

If the first append fails, nothing was written and the error is returned. If
the second fails (a lock timeout, say), the post is reported as a success with
`queued: "<team>"` and a warning, so the sender does not retry and duplicate the
half that landed; the journal stays, and the notifier (every 10 s, for journals
older than 30 s) or the next link post appends the missing copy unless that
board already carries the message id. A dissolved destination drops its copy.

### Receipts and lenses

The receiving notifier watches its manager's read position; when it passes a
delivered copy it appends `link_read` to the **sending** board (`to:
["team:<reader's team>"]`, `link_id`, `reader`, `read_seq`), which nudges
nobody and shows as `read by <manager>` on the mirror in the console.
`board --teams` and the console's `/filter teams` select link records;
`/filter team` excludes them. `me` lists `links`; the session briefing tells a
manager how to post to each linked team, and everyone else that only the
manager speaks across it. `dissolve` breaks the team's links and tells the
other side.

## 9e. Transcript search

### `search "<query>" [--member NAME]... [--since 3d|12h|ISO] [--limit N] [--history] [--role user|assistant|tool] [--context CHARS]`

Searches what members said and did in their own harness conversations, not
only what they posted: "what did the analyst find about X yesterday?". The
reader (`herdr_team/transcripts.py`) uses the same locators as `context`:
Claude's transcript (`<CLAUDE_CONFIG_DIR or ~/.claude>/projects/*/<id>.jsonl`,
or the path the SessionStart hook recorded for exactly that session), Codex's
rollout log (`<CODEX_HOME or ~/.codex>/sessions/**/rollout-*-<id>.jsonl`, then
`archived_sessions/`), OpenCode's `<XDG_DATA_HOME or ~/.local/share>/opencode/opencode.db`
(the `message` and `part` rows of that session, opened read-only), and Pi's
session file (the recorded path, or `<PI_CODING_AGENT_DIR or ~/.pi/agent>/sessions/*/*_<id>.jsonl`).
Other kinds are reported as having no reader.

**Query.** Case-insensitive. Every whitespace-separated term must occur in the
same message (AND); a double-quoted run is one term matched as an exact phrase,
with any whitespace between its words. Terms are literal substrings, never
patterns. Several arguments are joined with spaces, so `search rate limit` and
`search 'rate "token bucket"'` both work. An unbalanced quote is a usage error.

**What counts as a message.** User text, assistant text, and tool activity
(`role: tool`): a tool call's arguments and a tool result's text. Thinking and
reasoning blocks are not what was said and are skipped. Codex is read from its
`response_item` records only; `developer` messages and the harness-written
`<environment_context>` / AGENTS.md preamble are skipped.

**Scope.** The resolved team's agent members (not `left`), or `--member`
(repeatable; a member that left may be named). Only conversations recorded on
the roster are opened: the member's current `session`, and with `--history`
also `session_history` (earlier conversations of the same agent, kept when a
restart, a Claude `/clear`, or a resume replaced them; up to 20, newest last)
and the `session` of each `agent_history` entry (swaps). No directory is
scanned for other conversations, and a session id reaches a filename pattern
only when it is a plain id (`[A-Za-z0-9][A-Za-z0-9._:-]*`); anything else is
reported as `not a plain id`.

**Authority.** The operator from a trusted origin, an operator delegate, and
the team manager may search any member. Any other member searches only itself:
with no `--member` it defaults to itself, and naming anyone else is refused
with `author_mismatch` (1) and audited. Every search appends
`transcript_search` to the audit log with `{authority, query_chars, terms,
members, history, role, since, hits, shown}`; the query text itself is never
logged.

**Limits.** Files are streamed line by line and unparsable lines are skipped.
Each conversation is capped at 64 MiB; over the cap the newest 64 MiB is read
and the cap is listed in `skipped`. A missing store, a missing session, an
unreadable database, or a kind without a reader is a `skipped` reason for that
member, never an error. `--limit` is 1 to 500 (default 20); `--context` is the
excerpt length, 40 to 2000 (default 160). `--since` takes `30m`, `12h`, `3d`,
`2w`, or an ISO date or time (no zone means local time); messages without a
timestamp are left out when it is given.

**Redaction.** A matching message is passed through the board's secret
patterns (`cmd_board.SECRET_PATTERNS`, with a private key redacted through its
footer) and every match becomes `[redacted:<kind>]` before the excerpt is
taken. The terms are matched again on the redacted text, so text that only
matched inside a secret is not a hit and a key cannot be searched for.

Output is newest first. Text mode prints member, kind, local time, role, the excerpt
with the first match marked `»…«`, and the session id and file, then what was
scanned and skipped. JSON:

```json
{"team": "alpha", "query": "rate \"token bucket\"", "terms": ["rate", "token bucket"], "members": ["alpha-analyst"],
 "since": null, "role": null, "history": false, "total": 1,
 "hits": [{"member": "alpha-analyst", "kind": "claude", "session": "<id or path>", "ts": "2026-09-22T14:03:11.000Z",
           "role": "assistant", "excerpt": "…the rate limit is 600 per minute; the token bucket refills…",
           "match": [5, 9], "source_path": "/…/<id>.jsonl", "history": false}],
 "scanned": {"alpha-analyst": {"sessions": 1, "bytes": 482113, "skipped": []}}}
```

`ts` is UTC. `match` is the `[start, end)` character range of the marked match inside
`excerpt`; `total` counts every matching message, `hits` holds the newest
`--limit`. The console's `/search <query>` runs the same command and shows the
result in a box.

## 9f. Work items

State: `<team>/work.jsonl`, an append-only event log under `team.lock` (`op`:
`create`, `assign`, `claim`, `block`, `unblock`, `settle`, `review`, `reopen`,
`close`, `cancel`, `update`; each with `id`, `at`, `by`, `by_via`, `by_gen`).
The current state is derived by replaying it (`herdr_team.work`); a torn or
foreign line is skipped. Every change except readiness is also an authored board
post carrying `work: {"id", "op", ...}` (a `request` to the owner, a `done` to the
requester and the manager, a `blocked`, an `answer` for an approval), so delivery,
asks and the Stop hook treat work like any other post. A dependency finishing
appends a `work_ready` system record (wake `named`) for the next owner.

| Subcommand | Authority | Notes |
| --- | --- | --- |
| `work add "<title>" [--to M] [--deps W-1,W-2] [--review-by M\|role:R\|human] [--target T] [--deliverable T] [--constraints T] [--ownership T] [--acceptance T] [--brief-file F] [--quick]` | any member or the operator | `--to me`; no `--to` posts an open item to `all`. `config.work.acceptance` (`off`/`warn` default/`require`) governs a missing Acceptance (`brief_incomplete`); `config.work.review_by` is the default reviewer list. |
| `work list [--status S] [--owner M\|me] [--all]`, `work ready` | anyone | rows carry `ready`, `waiting_on`, `hints`, `attention`, `next` |
| `work show W-N` | anyone | adds `history` and the related `posts` |
| `work claim W-N [--force]` | the owner (anyone, for an open item), verified pane | new attempt `{n, owner, gen}`; `work_taken`, `work_waiting`, `work_already_claimed`, `work_not_claimable`; sets the member's task headline |
| `work block W-N "<why>"`, `work unblock W-N` | the owner | |
| `work done W-N --outcome succeeded\|failed\|partial --summary T [--deliverable X] [--evidence X]` | the owner, from the generation that claimed | `attempt_fenced` when the generation changed; `work_already_settled`. `succeeded` with reviewers -> `in_review`. |
| `work review W-N --approve [--note T] \| --changes T` | a named reviewer or role holder, `human` when named, the manager, the operator | the owner cannot review itself (`work_self_review`) |
| `work assign W-N <member>`, `work reopen W-N [why]`, `work close W-N [note]`, `work cancel W-N [why]`, `work update W-N [...]` | requester, manager, operator/delegate | `close` accepts `failed`/`partial`/`in_review` as done; dependency cycles are refused (`work_dep_cycle`) |
| `work next [--all]` | anyone | the hints addressed to the caller: `{id, attention, for, name, argv, why}` |

Attention codes: `unassigned`, `ready`, `waiting_on_deps`, `owner_absent`,
`stale_attempt`, `blocked`, `changes_requested`, `quiet` (no change for 2 h),
`awaiting_review`, `settled_needs_decision`.

## 9g. Facts and contradictions

State: `<team>/facts.jsonl` (`op`: `add`, `support`, `retire`, `dispute`,
`escalate`, `resolve`) under `team.lock`; findings recorded before 0.19 are read in
place from `knowledge.jsonl` as `L-n` (never rewritten). A fact:
`{id, statement, about, attribute, type, author, author_kind, author_gen,
recorded_at, valid_from, valid_to, retired_at, retired_by, retire_reason,
supersedes, superseded_by, sources, supporters, members, confidence, disputes,
legacy, status}`; `status` is `current`, `disputed`, `retired` or `superseded`.
Sources: `{"kind":"url","url","retrieved_at"}`, `{"kind":"post","seq"}`,
`{"kind":"file","path"}`.

- `fact add "<statement>" [--about S] [--attribute A] [--type T] [--source URL[@DATE]] [--post SEQ] [--ref PATH] [--valid-from D] [--valid-to D] [--supersedes F-N]`:
  exact words already recorded by someone else become `support`; the same member
  giving a new value for its own `--about`/`--attribute` supersedes its earlier
  fact; near-identical wording (character 3-gram Jaccard >= 0.85) warns.
  Superseding another member's fact needs the manager or the operator
  (`fact_not_yours`). `--type` must be in `config.vocabulary` when the team has one.
- `fact support F-N`, `fact retire F-N [why] [--valid-to D]` (own fact, or manager/operator), `fact show F-N`, `facts [--about S] [--by M] [--all] [--history] [--as-of D] [--disputed] [--limit N]`, `fact disputes [--all]`.
- `fact resolve D-N --keep F-N... | --keep-both | --retire-all [--reason T]`: the manager when it is not a party, or the operator; a party concedes by retiring its own fact, which settles the dispute (`resolved_by: concession`).
- `contradictions [off|observe|debate|escalate] [--timeout DURATION]`: show; setting is operator-only and announced with `contradictions_changed`. Stored as `config.contradictions {mode, debate_timeout_ms}`; default `observe`, 30 min.

A dispute opens when a member records a statement for an `--about`/`--attribute`
pair another member's current fact already answers differently. `observe` appends
`fact_disputed` to `human` only (no wake, invisible to members); `debate` appends
`fact_conflict` to both authors (wake `named`) and the notifier escalates it to the
manager (when not a party) or the human after the timeout (`escalate` op, one
`fact_conflict` with `escalated: true`); `escalate` sends `fact_conflict` to the
decider at once (`toast` when that is the human). No mode refuses, holds or filters
a post. `knowledge add` records a fact without a subject; `read_findings` returns
current facts with `text` = the plain statement plus `id`, `display`, `status`.

## 9h. Recall

`recall "<words>" [--kind post|fact|work|file|canvas]... [--about S] [--as-of D] [--limit N] [--no-refresh]`.
Anyone on the team. Index: `<team>/index/recall.sqlite3` (FTS5, `0600`), a cache
rebuilt from the board (active and archive; retracted posts removed; a rotation,
wipe or purge triggers a full rebuild), facts, work items, text files (<= 1 MiB,
text suffixes, no dot-files or symlinks) under the team folder's `artifacts/`, and
the team's canvas.
Terms are quoted before matching, so no input is an FTS syntax error; all words
must match, else any word. Ranking: reciprocal rank fusion (k=60) of `bm25`,
recency and standing (facts weighted by status, members and sources), multiplied
by `1 + 0.5 * closeness` when `--about` is given. JSON: `{query, hits: [{key,
kind, ref, author, ts, when, weight, bm25, score, snippet, info, closeness}],
indexed, as_of, about, team}`. File snippets are redacted like posts.

The `canvas` kind is one row per element, comments and legend entries included
(`herdr_team/canvas_index.py`): `ref` is the element id, `about` is the title of
the frame it sits in (else its alias), `info` is `{type, alias, frame, imported}`,
and `ts` is the element's `updated_at`, so `--as-of` hides a mark drawn later.
The text is each kind's own - element text, titles, card bodies and badges, table
cells, chart captions and their gist, kanban and timeline items, comment bodies
with their mentions, legend meanings - plus the kind word and every mark's
`intent`, read from the component registry rather than a list of kinds, and
bounded per row (`MAX_ROW_CHARS`) so a chart's inline data cannot crowd out a
caption. Weights: a container 1.5, a named or browser-drawn mark 1.2, a resolved
comment 0.5, else 1.0. Its signature is the scene file, the event log and the
team's canvas switch, so a draw, a `clear`, a `purge`, an import or turning the
canvas off rebuilds or drops the rows whole - no watermark, because a cleared
mark must stop being findable. Indexing never takes the canvas lock, never calls
`look`, and so writes no presence or cursor: a search is not a reader. A team
whose canvas is off or was never drawn on reports `canvas: 0` and does not fail.
`cmd_recall` prints a canvas hit as `[canvas E-12 · card]` plus `→ herdr-synapse
canvas look --around E-12`, and names the source team for an inherited mark.

## 9i. Templates

`template list`, `template show NAME`, `template save NAME [--title T]
[--description T] [--force]` (operator; from the resolved team), and `create
<team> --template NAME ...` (operator). Built-in templates: `<plugin>/templates`;
the operator's own: `<config>/plugins/config/herdr-synapse/templates` (they shadow
a built-in of the same name). A template is `team.md` (`# Title`, description,
`## Charter`, `## Rules`, `## Settings` with `manager`, `contradictions`,
`debate_timeout`, `acceptance`, `review_by`, `permissions`, `## Roles` as `- role:
kind — about`, `## Vocabulary` as `- Label: meaning`) plus `roles/<role>.md`
instructions documents. `create` fills only what was not passed (charter, rules,
`--spawn` per role with `--new`, per-role instructions keyed by role, launch
permissions), sets the manager by role once the members exist, then writes
`config.contradictions`, `config.work`, `config.vocabulary` and `config.template`.
An invalid template is refused before anything is created (`template_invalid`).

## 9j. Mission control

`mission [--ascii] [--width N]` and the `mission` popup (`ui mission`, action
`herdr-synapse.mission`, default `prefix+d`). Read-only over `team.json`,
`who.json`, the board, `work.jsonl`, `facts.jsonl` and the operator grants. Lanes:
`needs_you` (asks that are not work posts, agents in a dialog, work in review by
`human`, `failed`/`partial` work without a manager or requested by the human,
disputes routed to the human, grants expiring within an hour), `blocked`,
`working`, `done` (settled in the last 24 h, agents that finished a turn), `idle`.
Cards: `{lane, team, kind, title, detail, who, pane_id, argv, since}`. In the
popup, Enter focuses an agent card's pane, otherwise shows its `argv`.

## 9k. Skill guides

`skill get [worker|manager|reviewer|librarian] [--reference work|facts|recall|coordination|inheritance|canvas] [--list]`
prints a guide from `<plugin>/skill-guides`, headed `<!-- herdr-synapse <guide>
guide, skill vN -->`. The default is `manager` for the team manager and `worker`
otherwise. The installed `SKILL.md` stays the safety floor and tells agents to load it.
The `canvas` reference is listed and served only while the caller's team canvas
is on (a person with no team: while the layer is on); otherwise `--reference
canvas` refuses `reference_unknown`, so an agent is never taught a tool it
cannot use.

## 9l. Schedules

A schedule is an operator post written in advance: the notifier appends it
when it falls due, and ordinary delivery nudges the recipients at their next
idle. The model, cron engine and firing are `herdr_team/schedules.py`; the
commands are `herdr_team/cmd_schedule.py`.

**Storage.** Definitions are `<team>/schedules.json`, indented, written under
`team.lock`, and safe to edit by hand: `{"schema": 1, "next_id": N,
"schedules": [{"id": "s1", "name", "text", "cron", "every", "tz", "to",
"kind", "action": {"type": "post"}, "precheck", "precheck_timeout_s",
"grace_s", "enabled", "created_at", "created_by", "updated_at"}]}`. Unknown
keys are kept. An unparseable file refuses every write (`schedules_unreadable`,
1) rather than being overwritten; an invalid entry is skipped, shown by `list`,
and reported once as `schedule_failed`. The notifier's bookkeeping is
`<team>/notifier/schedules-state.json` (`armed_at`, `last_slot`,
`last_outcome` of `posted|skipped|failed|missed`, `last_seq`, `last_detail`,
`next_due`, `running`, `last_precheck`, `last_manual_*`); it never rewrites the
definitions.

**Timetable.** Five Vixie-cron fields (`M H DOM MON DOW`) with `*`, lists,
ranges, steps (`*/15`, `1-10/3`, `5/20`), month and day names, `7` for Sunday,
and the `@hourly`/`@daily`/`@weekly`/`@monthly`/`@yearly` macros. When both day
fields are restricted a day matching either fires; a day field starting with
`*` counts as unrestricted, so the two combine with AND (Vixie). Presets:
`hourly` (`--at :MM`, default `:00`), `daily`, `weekdays` (Mon-Fri), `weekly`
(`--day mon[,thu]`, default `mon`), all at `--at HH:MM` (default `09:00`). A
spec that can never fire (`0 9 30 2 *`) is refused. Times are wall-clock
minutes in `--tz` (IANA, through `zoneinfo`; `tz_unknown`, 2) or, without it,
this machine's zone, whose name is stored so the notifier uses the zone the
printed fire times used. DST: a minute repeated by a fall-back change fires
once, at its first occurrence; a minute skipped by a spring-forward change is
shifted forward by the jump (02:30 fires at 03:30), once.

### `schedule add "<text>" (--cron "M H DOM MON DOW" | --every hourly|daily|weekdays|weekly) --to <name|all|role:x> [options]` (operator)

Options: `--at HH:MM`, `--day mon..sun`, `--tz IANA`, `--kind
request|note|question` (default `request`), `--name NAME` (lowercase, unique,
usable in place of the id), `--precheck "CMD"`, `--precheck-timeout 60`
(seconds unless suffixed, at most 1h), `--grace 30m` (minutes unless
suffixed, at most 7d), `--disabled`, `--force` (the secret check, as `post`).
The text goes through `post`'s checks (sanitize, echo, secret). `--to` expands
like `post --to` and is stored as given, so `role:x` resolves at each fire;
`human`, `me` and `team:<other>` are refused (`schedule_recipient`). Prints the
id and the next three fires; JSON `{team, id, schedule, when, zone, to,
to_role, next: [iso]×3, notifier}`. Warns when the notifier is offline or when
two fires are under ten minutes apart. Audits `schedule_add`.

### `schedule list` / `schedule show <id|name>` / `schedule next [--count N]`

Open to every member. `list`: every definition with `when`, `zone`, `next`,
`state`, plus `invalid` entries. `show`: one definition, its state and the
next five fires. `next`: the next N fires (default 10, at most 100) across the
team's enabled schedules, soonest first, `{at, id, name, zone, to, kind,
text}`. `who` adds `schedules: {count, enabled, invalid, next}` for a team
that has any. The console's `/schedule` shows the list; `/schedule
run|enable|disable <id>` operates on one.

### `schedule rm|disable|enable|run <id|name>` (operator)

`rm` deletes the definition and its state (the id is never reused). `disable`
and `enable` flip `enabled`; enabling arms the schedule at that moment, so the
slots of the time it was off are neither fired nor reported missed (a hand
edit that re-enables one is treated the same way by the notifier). `run` fires
now, precheck included, without consuming a slot: `{outcome:
posted|skipped, seq, precheck}`; a precheck timeout or start failure is
`schedule_precheck_failed` (1). Audits `schedule_rm`, `schedule_disable`,
`schedule_enable`, `schedule_run`.

**Authority.** Every write needs operator authority (the operator, or a
delegate: `_human_only`). A precheck runs a shell command as the user, so
anything that can make one run (`add --precheck`, `enable` or `run` of a
schedule with one) needs the operator in person (`author_mismatch` for a
delegate, audited). The post itself is `from: human` with `origin: {via:
"schedule", verified: true, schedule, scheduled_by, slot}` (plus `manual,
run_by` for `run`), and the record carries `schedule: {id, name, slot,
manual}`; its text starts `[scheduled <name|id>]` so readers can tell it from
the operator typing now. `identity.record_human_ok` accepts that origin for
delivery and asks; no CLI author ever carries it, so it passes no gate.

**Firing.** The notifier checks each team's schedules at every tick it has a
slot due (cheaply: definitions are re-read every 5 s, due times cached). A
slot fires once: `last_slot` is persisted, and before appending, the last 400
board records are searched for a post already carrying that `schedule.slot`,
so a notifier that died between the append and the state write does not post
twice. A precheck runs as `/bin/sh -c CMD` in a worker thread (cwd: the
team's project directory when set, else the team directory; env: only `PATH`,
`HOME`, `LANG`, `HERDR_SYNAPSE_TEAM`, `HERDR_SYNAPSE_SCHEDULE`; its process
group is killed at the timeout) and is collected on a later tick: exit 0
posts, non-zero skips that run silently (state and audit only), a timeout is
`schedule_failed` to `human` (toast). A slot the notifier missed while it was
down fires once, for the latest missed slot only, when it is at most
`grace` late (a slot is always on time within its own minute); later than
that, one `schedule_missed` record goes to `human` (no wake) and the schedule
moves on. A post that cannot be written (a role nobody holds, say) is
`schedule_failed`; a locked board is retried at the next tick. Each outcome is
audited as `schedule_fire`.

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

While waiting, `warning: still waiting for the operator (30s of 480s)` goes to
stderr every 30 s (`WAIT_HEARTBEAT_S`); stdout is untouched, so `--json` still
prints exactly one object. Only a reply from a trusted human origin ends the
wait (`asks.answered_by`); a peer's reply or an unverified shell's does not.

`--wait` without `--to human`, or from the operator, is a usage error (exit 2)
and appends nothing. A `question` sent `--to all` is not an ask: it is for
teammates, and it neither pops up nor blocks.

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
 "remote":{…as remote status, without the session fields…}|null,
 "whiteboard":{"session":{"enabled","by","via","at"},"teams":{"<team>":{"enabled","viz","on","viz_on","by","at"}},
               "page":{"running":true,"pid","port","url","writable","idle_minutes"}|{"running":false},
               "watched":[{"name","pane_id","team"}],"warnings":["…"]},
 "warnings":["…"],"errors":["…"]}
```

`whiteboard` reports the three switch levels, the page server and the watched
agents (section 9n). Its warnings join the list: a page server running while
the layer is off or unused for a day, and agents watched while the sidebar
rows lack `$team_doing` or while the layer is off.

Among the warnings: a paired phone channel (section 9e), what it sends, and
whether it is outbound-only; and a team whose `project_dir` has been deleted or has become
read-only. The mirror is refreshed best effort, so without this the folder
would just stop updating with nothing on screen to say why.

### `setup --print-config`

JSON `{"required":"<toml>","optional":"<toml>","notes":[…],"toast_delivery":"terminal"|"herdr"|"off"|null,"toast_probe":{"probed":bool,"shown":bool,"reason":"…"}}`.
Never edits config. Like `doctor`, it issues one `notification.show` probe
(plan 7.2) and prints the effective toast mode; `--no-probe` skips it, and an
unreachable or unlisted socket reports `probed:false` with the reason instead
of failing, so the config blocks always print.

### `keys print` / `keys check`

`print` → `{"snippet":"<toml>","keys":{"team-up":"prefix+t","compose":"prefix+m","console":"prefix+u","toggle-view":"prefix+y","usage":"prefix+i","knowledge":"prefix+f","mission":"prefix+d","whiteboard":"prefix+a"}}`.
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

### `update [--ref REF] [--force-skill]`

Refreshes the plugin installation as one bounded sequence: a GitHub-managed
source is reinstalled with `herdr plugin install … --yes` (preserving its
recorded ref unless `--ref` overrides it), the CLI symlink is refreshed, the
bundled skill is installed, and the notifier is replaced. A `local` plugin
link is re-registered with Herdr but deliberately not pulled or modified; that
checkout is the source of truth and the latter three surfaces are refreshed.
`--ref` on a local link refuses `plugin_source_local`. Foreign skill paths
remain untouched and finish as `skill_update_refused`; `--force-skill`
explicitly replaces them.

JSON `{"before","after","source_kind","checkout","plugin_root","cli","skill","daemon"}`.

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
tombstones are never listed), then the agents that belong to no team grouped by the label and stable ID returned by `tab.list`. Each candidate row keeps its pane ID visible; the detail line spells out its pane and tab, `g` verifies that the pane still hosts the same terminal and focuses it, Enter or Space folds a tab group, and Left on a candidate returns to its tab header. Member
rows are rendered by the same `who` renderer the console uses, plus the
role; live status and tab identity come from `agent.list`, tab labels come from one `tab.list` request per refresh, and headlines, holds and mutes
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

## 9m. Phone reach (`remote`)

Opt-in. When an agent asks the operator something (section 9b), the notifier
can also send it to the operator's phone and read the answer back. Only
outbound HTTPS is used and nothing listens on a port: replies are fetched on
the notifier's own schedule. Code: `herdr_team/remote.py` (channels, policy,
relay) and `herdr_team/cmd_remote.py` (commands).

| channel | sends with | reads answers |
| --- | --- | --- |
| `ntfy` (ntfy.sh or self-hosted) | `POST <server>/<topic>`, headers `Title`, `Priority`, `Tags: herdr-synapse`, `Authorization: Bearer <token>` when set | only with an access token: `GET <server>/<topic>/json?poll=1&since=<id>`. Without one the topic is public, so the channel is **outbound-only** |
| `telegram` (a bot you made with @BotFather) | Bot API `sendMessage` to the one pinned chat | `getUpdates` with `timeout=0` (never a long poll) and an offset; only messages from the pinned private chat count |
| `webhook` | `POST <url>` with `{"text": "…"}` (Slack incoming-webhook shape) | never: **outbound-only** |

Every request has a 5 s timeout, redirects are refused (they could carry the
`Authorization` header elsewhere), and all network I/O runs on one worker
thread, so a slow server or a dead resolver never holds the tick.

### Files

- `<config_dir>/plugins/config/herdr-synapse/remote.json` (0600, directory
  0700, symlinks refused): `{"v":1,"channel","pair_id","paired_at","paired_by",
  "<channel>":{…settings and secret…},"policy":{"send":[…],"text":"…"}}`. One
  per Herdr config dir, shared by every session under it; messages name the
  session slug and the team.
- `remote-poll.json` beside it: the channel's read cursor, the last 300 reply
  ids already handled, and a 30 s lease. The cursor belongs to the channel,
  not a session: Telegram drops updates once any reader moves past them. Only
  the lease holder reads, at most once per 5 s per channel across sessions,
  and it routes each answer to the session that issued the code.
- `<session>/remote-state.json`: this session's dedupe keys (`ask:<team>:<seq>`,
  `event:<team>:<seq>`), the codes it issued (`{"team","seq","asker","kind",
  "created_at","sent_at","message_id","state":"open"|"closed"|"answered"}`),
  the outbox, and `last_send` / `last_poll`. A restart never resends a sent
  key and never reapplies a handled reply id.

Secrets are read from a file (its first line) or from an environment variable
**name**, never from argv; `--token`, `--bot-token` and `--url` exist only to
be refused with `secret_in_argv` (exit 2) without echoing the value. The secret
is copied into `remote.json`; `unpair` deletes it. No secret is written to
`daemon.log`, `audit.jsonl`, or either state file, and errors that could echo
a URL are scrubbed to `[secret]`.

### `remote status`

No authority needed. JSON: `{"channel","paired","receives","outbound_only",
"outbound_only_reason","pending_pair","pair_expired","destination",
"paired_at","policy":{"send","text"},"leaves_machine","token_configured",
"config","session","outbox","open_codes","last_send","last_poll","notifier"}`.
`destination` never shows a secret, an open topic's full name (it is that
topic's only password), or a chat id.

### `remote pair ntfy --topic T [--server URL] [--token-file PATH | --token-env VAR]` (human only)
### `remote pair telegram --bot-token-file PATH | --bot-token-env VAR` (human only)
### `remote pair webhook --url-file PATH | --url-env VAR` (human only)

Replaces any previous pairing (the policy is kept) and audits `remote_paired`
in the resolved team when there is one. Servers and URLs must be `https`
(`http` only for a loopback ntfy). ntfy's `since` starts at the pairing time.

Telegram pairs in two steps. `pair telegram` stores the token and prints a
one-time code (8 characters, 15 minutes) and, when `getMe` answers, a
`https://t.me/<bot>?start=<code>` link; JSON adds `code`, `link`,
`expires_in_s`. The operator sends `/start <code>` to the bot from a
**private** chat. The notifier's next read pins that chat id, or
`remote pair --complete` does it at once (`remote_pair_waiting` while nothing
matched, `remote_pair_expired`, `remote_nothing_to_complete`). A group chat, a
wrong code, or a message older than the pairing never pins. Nothing is sent
until the chat is pinned.

Refusals: `remote_secret_unreadable`, `remote_invalid`, `author_mismatch`
(a member or a delegate; pairing decides what leaves the machine).

### `remote unpair` (human only)

Deletes the channel and its secret, keeps the policy, drops the read cursor;
the notifier discards anything still queued for the old pairing.
`remote_not_paired` when nothing is paired. Audited `remote_unpaired`.

### `remote test`

Sends one message on the paired channel from the CLI (operator or delegate).
`remote_send_failed` with the scrubbed reason when it does not go out. Audited
`remote_sent` with `category: "test"`.

### `remote policy [--send asks,conflicts,failures,settled | none] [--text full|summary|none]`

A bare call reads; a change is human only and audited `remote_policy`.

| `--send` category | what triggers a message |
| --- | --- |
| `asks` (default) | a new ask to `human` of kind `question`, `blocked` or `request` |
| `conflicts` (default) | a `fact_conflict` system record addressed to `human` |
| `failures` (default) | a `schedule_failed` system record addressed to `human` |
| `settled` | an ask the phone was sent was answered or withdrawn in Herdr |

| `--text` | what leaves the machine |
| --- | --- |
| `summary` (default) | session and team, who asked, the kind and seq, the reply code; never the text |
| `full` | the same plus the post text, with every `cmd_board.SECRET_PATTERNS` match replaced by `[redacted:<kind>]` before the text is capped at 500 characters |
| `none` | only `1 item waiting in Herdr`: no names, no text, no code |

Only items that appear **after** pairing are sent; what was already waiting
stays in the popup. An ask answered in Herdr before its message went out is
dropped, never sent late.

### The notifier's `remote` phase

After `asks` in every tick. It reads `remote.json` at most every 2 s (by
mtime), queues new asks from `open_asks` and relayed system records as the
board tail ingests them, and hands at most one request to the worker: a poll
when one is due (5 s while a code is out or a pairing waits, 60 s otherwise,
30 s after a failed poll), else the next due send (one per second at most).
A failed send retries after 5 s, 15 s, 1 min, 5 min, then every 15 min, and
is dropped after 8 attempts. Each send is audited `remote_sent`
(`channel`, `category`, `seq`, `code`, `text` mode; never the text).

### Answering from the phone

Each ask message carries a 4-character code (letters and digits without
`0 O 1 I`). A reply `<code> <answer>` (case-insensitive), or on Telegram a
reply to the bot's message without the code, becomes this record on that
team's board, the same shape the popup writes:

```json
{"from":"human","kind":"answer","to":["<asker>"],"reply_to":<ask seq>,"text":"<answer>",
 "origin":{"via":"remote","verified":true,"channel":"ntfy|telegram",…}}
```

`<code> ok` or `<code> ack` is the popup's `Ctrl-A`: the text is the same
acknowledgement, *not a decision*. `asks.answered_by` accepts the record
(`identity.record_human_ok`), so the ask closes, a waiting `post --wait`
returns, and the asker is nudged. The answer goes through the post sanitizer:
a secret, a nudge marker, an empty or over-500-character answer posts nothing
and gets a short reply saying why. An unknown or expired (7 days) code gets
`unknown code <code>`; an ask that is no longer waiting gets `no longer
waiting`. Every applied answer is audited `remote_answer` (`channel`, `code`,
`reply_to`, `seq`, `ack`, `chars`).

A phone answer can only answer or acknowledge an ask that is still pending.
No CLI author ever carries `via: "remote"`, and `Author.trusted_human` does
not accept it, so it never passes an authority gate: it cannot change a
charter, rules, grants, links, or this policy.

## 9n. Whiteboard, canvas and watch (0.21)

A visual layer over the teams: a shared canvas per team that agents and the
operator draw on, a local page that shows it with the team's generated views,
and watch, which shows what any flagged agent is doing. The build contract is
`.local/prd/canvas-contracts.md`; this section is the user-facing command and
JSON contract.

### Switches

| Level | Stored in | Default | Who flips it | Command |
| --- | --- | --- | --- | --- |
| the layer, this Herdr session | `<session>/features.json` `{"v":1,"whiteboard":{"enabled","by","via","at"}}` | **off** | the operator in person | `whiteboard enable` / `disable` |
| one team's canvas | `team.json` `config.whiteboard.enabled` | **off** (absent = off; only a literal `true` is on) | the operator or a delegate | `whiteboard team on` / `off`, `create --canvas` |
| one team's live visuals (`viz`) | `team.json` `config.whiteboard.viz` | on (absent = on), inside a team whose canvas is on | the operator or a delegate | `whiteboard viz on` / `off` |
| one agent's watch | `<session>/watch.json` | not watched | the operator in person | `watch` / `unwatch` |

A team's canvas is on when the layer is on and its own switch is; its live
visuals when the canvas is on and the viz switch is. Watch needs only the
layer.

Off means off. With the layer off the page server is not running and cannot
start, no watch polling runs and no `team_doing` token is shown, every
`canvas` command and MCP tool refuses `whiteboard_off` (details `scope:
"session"`) and changes nothing, `look` included, the page API answers 403,
`me`/`orient`/the briefing say `whiteboard: off`, `skill get` neither lists
nor serves the `canvas` reference, and members Synapse starts get no MCP
flags. Nothing stored is deleted. A team switched off is the same for that
team (`scope: "team"`). Viz off refuses `viz` operations with `viz_off`;
existing `viz` elements stay and show as their still or placeholder.

Commands that work while off: `whiteboard status`, `enable`, `team on|off`,
`viz on|off`, `clear`, `purge`, `views`, and `watch list`.

### `whiteboard [status]`

Anyone. JSON:

```json
{"session":{"enabled":true,"by":"human","via":"console","at":"…"},
 "teams":{"alpha":{"enabled":true,"viz":true,"on":true,"viz_on":true,"by":null,"at":null,
                   "canvas":{"version":42,"elements":18,"comments_open":2,"claims_active":1,"updated_at":"…"}|null}},
 "server":{"v":1,"pid":4242,"port":51234,"start_time":"…","started_at":"…","started_by":{…},"writable":true,"version":"0.21.0","beat_at":"…",
           "page_at":"…","streams":1,"alive":true,"url":"http://127.0.0.1:51234/","beat_age_s":4.2}|null,
 "watched":2}
```

### `whiteboard enable` / `whiteboard disable` (the operator in person)

A trusted human origin only (console, popup, a verified shell, outside
Herdr); a delegate is refused `author_mismatch`, audited. `disable` also stops
the page server and clears every watched pane's `team_doing` token, even when
the layer was already off. `enable` turns no team's canvas on: a canvas is
per team (`whiteboard team on`, `create --canvas`, or `d` in `prefix+t`), so
only a team whose own switch is on is announced. When the effective switch of
a team changes, that team's board gets one `whiteboard_state` record; one
saying its canvas came on wakes every member at its next idle. JSON:
`{"enabled","changed","by","via","at","notices":{"<team>":<seq>},"server_stopped","tokens_cleared","warnings":[…]}`.

### `whiteboard team on|off` / `whiteboard viz on|off` (the operator or a delegate)

Writes `team.json` `config.whiteboard` (`enabled` or `viz`, plus `by`, `via`,
`at`) under the roster lock and bumps its revision; a no-op writes nothing.
A change agents can see posts `whiteboard_state` (nothing while the layer is
off). JSON `{"team","switch":{"team","layer","enabled","viz_enabled","on","viz"},"changed":["enabled"|"viz"],"notice_seq":<seq>|null,"by":"operator"|"delegate"}`.
A member without a delegation is refused `author_mismatch`.

### `whiteboard open [--no-browser] [--engine v1|v2]` / `whiteboard stop` (people only)

`open` refuses `whiteboard_off` while the layer is off; members and hooks are
refused `author_mismatch`. It starts the page server if none runs, mints a
one-use opening ticket (120 s) and opens `http://127.0.0.1:<port>/?ticket=…`
in the browser with Python's `webbrowser`. `--engine v1` adds `&engine=v1`
to that URL, which opens the classic Excalidraw canvas; no flag (or `--engine
v2`) opens canvas v2, the page's default since 0.22. Over SSH (`SSH_CONNECTION`,
`SSH_CLIENT` or `SSH_TTY` set), or when no browser opens, it prints the URL and
`ssh -L <port>:127.0.0.1:<port> <user>@<host>` to run on the machine with the
browser. JSON `{"url","port","pid","writable","started","browser":"opened"|"skipped"|"ssh"|"failed","ssh_hint":"…"|null,"ticket_ttl_s":120}`.

The page can draw only when the server was started by a trusted human
(`writable`); otherwise it is read-only. The plugin action
`herdr-synapse.whiteboard` (default `prefix+a`) runs as the plugin, which
cannot vouch for a person, so it opens the `whiteboard` popup, whose command
(`whiteboard open --popup`) resolves as the operator through the popup tier.
The popup closes at once when a browser opened, and otherwise shows the URL
until Enter.

`stop` sends SIGTERM to the pid in `<session>/whiteboard.json` after checking
its recorded start time. JSON `{"stopped":bool}`. `whiteboard-serve [--port N]`
(hidden) is the server in the foreground, the command `open` spawns.

### `whiteboard views`

Any member of the team or the operator; works while off (it reads team data
only). Prints the generated team views as one JSON object (the page's Team
tab):

```json
{"team":"alpha","generated_at":"…",
 "work":{"nodes":[{"id":"W-1","title":"…","status":"in_progress","owner":"alpha-worker","attempt":2,"ready":false,"waiting_on":["W-2"],"review":false}],
         "edges":[{"from":"W-2","to":"W-1"}]},
 "facts":{"subjects":[{"about":"Competitor X","facts":[{"id":"F-7","attribute":"price","statement":"…","author":"…","support":2,"status":"current","superseded_by":null,"disputes":["D-1"]}]}],
          "disputes":[{"id":"D-1","facts":["F-7","F-9"],"mode":"debate","open":true,"about":"…","attribute":"…"}]},
 "topology":{"team":"alpha","manager":"alpha-lead","members":[{"name","role","kind","profile","status","agent_status","pane_id","color"}],
             "links":[{"team":"beta","state":"active","manager":"beta-lead"}]},
 "timeline":[{"seq":12,"ts":"…","kind":"request","event":null,"from":"human","to":["alpha-worker"],"text":"≤ 160 chars"}],
 "lanes":{"needs_you":[…],"blocked":[…],"working":[…],"done":[…],"idle":[…]},
 "canvas":{"version":42,"elements":18,"comments_open":2,"claims_active":1,"updated_at":"…"}|null}
```

Edges run from a dependency to the item that waits on it. A fact's `status`
is `current`, `retired` or `superseded`; `disputes` lists its open ones and
`support` counts the distinct members behind it. Subjects are sorted, facts
with no subject last (`about: null`). `timeline` is the newest 200 board
records, oldest first, secrets redacted and text clipped. `lanes` is mission
control for this team (section 9j). `color` is the member's canvas colour
once it has drawn. `canvas` is null while the team's canvas is off. Every part
degrades to empty on an unreadable file. Live states come from `who.json`;
when it has no row for the team, one `agent.list` fills them in.

### `whiteboard clear` (the operator or a delegate) / `whiteboard purge [--all-teams] [--yes]` (the operator in person)

`clear` moves the team's canvas (events, scene, assets, stills) to
`whiteboard/archive/<utc>/`; ids keep counting. JSON `{"team","archived","version"}`.
`purge` deletes the stored canvas of this team, or of every team, for good;
it asks first on a terminal and otherwise refuses `confirmation_required`
without `--yes`. JSON `{"purged":["alpha"],"teams":["alpha"]}`.

### `canvas …`

Identity as for `post`; members write only as verified members of their own
team; unverified callers, hooks and startup processes can read only.

```
canvas look [--region R|operator | --around ID] [--since N|last] [--image] [--grid] [--exact] [--theme light|dark] [--block REF] [--full] [--view VIEW] [--proposals]
canvas check [--region R | --around ID] [--mine]
canvas draw [--file PATH|-] [--op JSON]... [--atomic] [--base N|last]
canvas comment AT TEXT [--mention NAME]... [--reply-to C-n] [--intent TEXT]
canvas claim REGION LABEL [--intent TEXT]
canvas release [K-n]
canvas legend SYMBOL MEANING [--intent TEXT] | canvas legend --remove G-n
canvas portrait (--from-todo | --step TEXT... [--current N]) [--title TEXT]
canvas changes [--since N|last]
canvas resolve C-n
canvas undo B-n [--force] | canvas undo --author NAME [--since N] [--force]
canvas refit [--ids E-1,E-2]
canvas migrate [--apply | --dismiss] [--json]  (anyone reads; the operator applies or dismisses)
canvas withdraw P-n
canvas checkpoint LABEL | canvas checkpoint --remove V-n
canvas focus [REGION|ID] [--intent TEXT] [--status S] [--ttl SECONDS] | canvas focus --clear
canvas accept P-n [--note TEXT]              (the operator)
canvas reject P-n [--note TEXT]              (the operator)
canvas freeze REGION | --ids E-1,E-2 [--label TEXT]   (the operator)
canvas thaw X-n | --ids E-1,E-2              (the operator)
canvas restore V-n                           (the operator)
canvas settings [--human-edits propose|live] [--frozen propose|refuse]   (the operator; no flags prints them)
canvas lock REGION [--label TEXT]            (operator)
canvas unlock X-n                            (operator)
canvas send ID... --to NAME [--note TEXT]    (operator)
canvas export [--format json|md|svg|png] [--region R] [--out PATH]
canvas import (--from PATH | --from-archive TEAM[@STAMP]) [--list] [--latest] [--keep-authors]
              [--team-can-edit] [--skip-unknown] [--skip-missing] [--dry-run] [--intent TEXT]   (the operator)
canvas helper [--print]
canvas icons [--search WORD]
canvas catalog charts|scene3d [--type NAME]
canvas mcp
```

**Space.** Canvas units, y down, a grid cell is 20 units. `c<col>r<row>`
names the point at a cell's top-left (`c17r6` = 340,120); a point is also
`"x,y"`, `[x,y]` or an element (its centre); a region is `"c10r4:c40r22"`,
`"x0,y0,x1,y1"`, `[x0,y0,x1,y1]` or an element (its bounds).
`|x|,|y| ≤ 1,000,000`, sizes up to 20,000 (a laid-out block such as a graph up
to 50,000). Each author gets a home region
`[i*1000, -1000, i*1000+800, -400]` the first time it draws; an element with
no placement goes to the next free slot there. The operator's index is -1, so
their lane sits immediately left of the first member's: since 0.22 they draw
without coordinates like anyone else. Placement is one of `at`,
`right_of`, `left_of`, `below`, `above` (with `gap`: `s` 20, `m` 40 (the
default), `l` 80, or a number), `inside` a frame, or `in` a container (a
section, a kanban column) to join its layout at `index` (phase 2; `in` with
`at` or `points` in a free section resolves cells in its local grid). A block
(a stack, a kanban, a timeline, a graph) takes members only on purpose (`in`,
`place in`, a drop), never by where something lands.

**Ids.** `E-n` elements (frames too), `C-n` comments, `K-n` claims, `X-n`
locks and freezes, `G-n` legend entries, `B-n` batches, and since canvas v2
phase 5 `P-n` proposals and `V-n` checkpoints, per team, never reused (the ids
a proposal would create are reserved when it is made). An op's
`id` is an alias scoped to its author (`^[A-Za-z][A-Za-z0-9_.-]{0,63}$`, not
`X-9` shaped); references resolve canonical id, then the author's own alias,
then an alias exactly one other author uses.

**Operations** (a batch is `{"ops":[…],"atomic":false,"base":N}` or a list; every op
has `op` and a one-line `intent`, required from agents; text is cleaned like a
post, and secrets or marker text refuse):

| `op` | Does | Who |
| --- | --- | --- |
| `shape` | box, ellipse, diamond, note or text (`kind`, `text`, placement, `w`, `h`, style); sized from its label before it is placed, so `w`/`h` are its minimum (0.22); a text's `w` is the width it wraps at and its height follows its lines | writer |
| `arrow` | `from`/`to` element or point, `label`, `head`, `tail`, `curve`; or `points` | writer |
| `frame` | a titled frame: placement + size, `children`, or a `region` | writer |
| `pen` | a freehand stroke: 2..500 `points` (cells or coordinates, optional pressure), `closed`, `style` smooth/straight | writer |
| `path` | SVG path data (commands and numbers only), `scale` | writer |
| `svg` | sanitised SVG markup ≤ 100 KB, `sketchy` | writer |
| `graph` | nodes (`id`, `text`, `kind`, `tone`, `color`, `fill`) and edges laid out (`layered`, `radial`, `force`, `grid`) as native shapes, each node sized from its label before layout | writer |
| `mermaid` | a flowchart becomes native shapes; other diagrams render on the page | writer |
| `chart` | a chart block (canvas v2 phase 3): `type` (bar, line, area, scatter, pie, donut, heatmap, histogram, box, funnel, treemap, sankey, and the 3D bar3d, scatter3d, surface) over `data` (a `.csv`, `.tsv` or `.json` under the team's `artifacts/`, 5 MB and 100,000 rows) or inline `rows` (500, 32 KB), with its channels (`x`, `y`, `color`, `category`, `value` ...), `aggregate`, `filter`, `top`, `sort`, `stack`, `format`, `units`, `highlight`, `annotations`, `caption`; the data is read when the op runs and kept (`patch {relayout: "full"}` re-reads it). Or `spec` (a raw Vega-Lite spec, no `url`/`href` keys) or `echarts` (a raw ECharts option, sanitised). `canvas catalog charts` lists the types; `docs/charts.md` has the contract | writer |
| `viz` | HTML and JavaScript for a sealed frame (`libs` ⊆ d3, three, p5; `data` or `data_path`); `synapse.width`/`height` are the frame's size and a lone fixed-size `<canvas>` or `<svg>` is scaled to it; `viz_off` unless the team's viz is on | writer |
| `image` | a PNG or JPEG from `artifacts/`, `whiteboard/renders/` or the author's cwd (bytes checked) | writer |
| `comment` | a pinned comment; `@name` and `mentions` post `canvas_sent` | writer |
| `claim` / `release` | a five-minute region claim (three per author) | writer / own |
| `legend` | a recorded convention (`symbol`, `meaning`), or `remove` | writer |
| `move`, `restyle`, `edit`, `delete` | change elements (a `w`/`h` in a `move` is a labelled shape's new minimum, and a text's `w` the width it wraps at; an edit, a new size or a new font refits the label from the minimum, so a box can shrink back); `if_version` refuses `canvas_stale` | editor |
| `portrait` | the author's plan as a frame of steps in its home | self |
| `resolve` | close a comment | its author, a mentioned member, manager, operator |
| `lock` / `unlock` | an operator region lock | operator |
| `undo` | `batch`, or every not-undone batch of one `author` after `since` (its own undo batches aside), newest first: write back what they touched, skipping every element someone outside them changed later (`undo_skipped`, and the result's `undo: {batches, restored, of, skipped}`) and anything the undoer may not edit; `force` (the operator in person) writes back anyway | its author; the manager or a delegate for agents' batches; the operator in person for anything |
| `refit` | size labels again (`ids`, or every element the author may edit); a block root named in `ids` is rebuilt from its own spec | editor |
| `section`, `card`, `sticky`, `callout`, `heading`, `badge`, `icon` | content blocks (phase 2): a titled zone that lays out what joins it (`layout` row, column, grid or free, `gap`, `padding`, `align` incl. `stretch`, `cols`, `grid "CxR"`, `children` or `region`); a card (`title`, `body` with `- ` bullets, `icon`, `badges`, `owner`, `status`, `detail`, `size` s/m/l, `tone`, `variant`); a sticky (`text`, `tone`, `size`); a callout (`kind` note/tip/important/warning/danger/decision/question, `title`, `body`, `icon`); a heading (`text`, `level` 1-3); a badge (`text`, `tone`, `variant`, `icon`, `size`); an icon (`name`, `size`, `tone`, `label`; `canvas icons --search`) | writer |
| `table`, `kanban`, `timeline` | structured blocks: `columns` and `rows` held inline, every cell a part; `columns` of `cards` (a card is `<board>.c<n>`); `events` on a dated or ordinal axis. The same op with the same `id` updates the block in place (items match by id, then by text) | writer |
| `patch` | inside a block (`id`): `add`, `update`, `remove` (by key) and `set` (settings), `relayout` incremental or full; a key that names no item refuses `part_unknown` with the nearest keys; `if_version` is the block's version (its root's `updated_seq`, which every member change bumps) | editor |
| `place` | `id` or `ids` as one group: `right_of`, `left_of`, `below`, `above` (with `gap`, `align` start/center/end), `at`, or `in` a container at `index`; a member of a positional block (a graph, a timeline) placed or dragged is pinned where it went, one in a stack is reordered | editor |
| `pin` / `unpin` | hold elements where they are (`pin: {"by": "human"\|"agent", "who"}`): no layout, growth push-out or other author moves them. An agent never moves, resizes, deletes or unpins a person's pin (`pin_held`), nor a container holding one; `unpin` re-lays out the block | editor |
| `accept` / `reject` | decide a proposal (`id`, `note`): accept replays exactly what it showed (refused `proposal_outdated` when a target changed since); reject leaves the canvas as it is | the operator in person |
| `withdraw` | take an open proposal back (`id`) | its author; the operator |
| `freeze` / `thaw` | hold a `region` or elements (`ids`, and what they hold) as they are, with a `label`; `thaw` an `id`, or let go of `ids` | the operator in person |
| `settings` | `human_edits` (`propose`, the default, or `live`: agents' changes to the operator's marks apply, marked `touched_human`) and `frozen` (`propose`, the default, or `refuse`) | the operator in person |
| `checkpoint` | save the elements as `V-n` (`label`; 3 named each, 30 for the operator, 10 automatic), or `remove` one | writer (remove: its author, the operator) |
| `restore` | an automatic checkpoint of now, then the difference to `V-n` as one op (comments stay; claims, locks, freezes, proposals and settings are untouched); undo it like any batch | the operator in person |
| `migrate` | a board drawn before 0.22 (0.22): `action: apply` refits every element the migration report lists and draws the marks in 0.21's sketch style clean, in one batch, then records the `migration` setting; `action: dismiss` records only the setting. `if_version` is not accepted; an `apply` with nothing to do is refused `op_invalid` | the operator in person |

Writer: a verified member, the operator, or a delegate. Editor: the element's
author, the manager for any agent's element, the operator for anything; the
human's elements are the operator's alone. Since canvas v2 phase 5 an agent's
op that would change the operator's marks, a peer's, land in a peer's lane
(claims and home) or in a frozen area becomes a proposal instead (see
**Collaboration** below). Operations inside an operator lock
refuse `canvas_locked`; inside another author's claim a manager's or a
delegate's apply with an `inside_claim` warning; overlapping text gets an `overlap` warning, and an
element that crosses a frame's edge a `frame_edge` warning. `inside` a
frame (on a new element or a `move`) makes the element its child; the frame
grows to fit only for its editor and never into a lock, so placing something
too big inside someone else's frame refuses `element_not_yours`. A shape that
grew to fit its label past the frame it was placed in grows that frame too,
and every frame around it in turn, where its author may (0.22). A frame with no
room left takes a new child in the next row below, or in a new column to its
right when that keeps it nearer square. Growth never covers a neighbour the
op did not ask to cover (their asked boxes do not meet), and each move comes
with a `moved_to_fit` warning. A mark whose asked box lies on a shape (a door
on the walls) sits on it: the shape grows to hold it, like a frame, and
carries it when it has to move. A new mark that would cover a neighbour only
because one of them grew moves to the free spot nearest where it was put: on
its own shape when it sits on one, else beside the neighbour (its frame grows
to hold it). A shape that grows later (an edit, a restyle, a shape holding a
grown mark) pushes what it now covers along the way it grew, by whole grid
steps, with whatever sits on it; a neighbour its author may not change, or in
a lock, stays. A moved mark keeps how far it was moved (`nudged`, cleared by
a deliberate `move`), so a later point on where it was asked to be is a point
on it, in the same batch or a later one. A collision the op itself asked for
is left to `check`. An unlabelled shape drawn over labelled ones goes under
them, so its fill never hides their text.

**Collaboration** (canvas v2 phase 5; `docs/collaboration.md`). The operator
in person leads: the collaboration layer never refuses them, and only they
accepts, rejects, freezes, thaws, restores, changes the settings or forces an
undo (`operator_only` for anyone else, delegates too). Every other author's
proposable op (the edit ops and every kind's create op; comments are always
live) runs, then a review gate decides: live, a **proposal**, or a refusal.
A proposal (`P-n`) keeps nothing on the canvas; the result lists it under
`proposed: [{index, op, proposal, reason, reasons, message, targets, created,
summary, base_note}]` and the text says `#1 move → proposal P-3: E-4 is the
operator's; it waits for the operator`. Reasons, in rule order:
`frozen`, `human_made` (the operator's marks, or a new mark on or in them),
`peer` (a peer's marks), `foreign_lane` (a plain member's new mark in another
author's claim or home). Refusals: `element_busy` (the element the operator is
editing now; retry after 5 s), `frozen` (under `frozen: refuse`),
`stale_base` (an op aimed at what the operator changed after the batch's
`base`; what another agent changed only warns `stale_base`), `in_proposal` (a
later op naming what only a proposal of this batch holds), `proposal_limit`
(20 open per author, 200 in all). A live op renews its author's claim it works
in once half its time is gone, and a plain member's new marks in free space
claim it (`auto: true`; the applied entry says `auto_claim: K-n`). A comment on
an element follows it (`anchor`) and keeps its point, with `was_on`, when the
element goes. A member's result carries `operator: {viewport, selection,
editing, pointing_at, age_s}` (null when no operator page is fresh), and a
member's draw and look write its presence (`focus` sets it on purpose).
Proposals never wake anyone; they join the author's `canvas_changed` line.

**Blocks** (phase 2). A block op builds a finished, laid-out result; every
apply result then carries, per op, `block` (`{id, kind, box, version, moved,
pins, upsert, notes, layout?, crossings?}`) and, for the batch, `geometry`
(the final box and fit of everything it created, moved or resized, after all
arranging, at most 50) and `check` (the layout problems within 200 units of
what it touched and involving it, at most 10, each with its fix, and a count
per code). `look` prints a block as the op that builds it (compact JSON, cut
at item boundaries, at most 12 lines; `--block REF` prints it whole, one item
per line) with its members folded in; an element in a container says where
(` in arch#1/2 (row)`, ` part c2 of work`); `--full` adds each block's part
ids and a top-level element's nearest neighbours; `look --json` has `blocks:
{id: spec}`. `canvas icons` lists the Lucide icon names cards, icons and
shapes take (`--search WORD`); an unknown name refuses `icon_unknown` with
`did_you_mean`. `restyle {ids, route: "orthogonal"}` reroutes an arrow around
what is in its way (`straight`, `orthogonal` or `curved`; `route` applies to
arrows only).

**Arrow labels** (0.22). A label sits in a pill on its arrow's line (on a
curve, the drawn curve), clear of every mark and every other label: nearest
the middle, else further along the line, else beside it. A new arrow between
two elements whose line is too short for its label moves its later end away
along the arrow's main axis (and what sits on it, by whole grid steps) until
the label fits between them; an arrow by `points` never moves anything. The
arrow stores the spot (`label_at`, the pill's centre) and the lines
(`"fit": {"lines", "size"}`, wrapped at 11 em however long the arrow); a
label keeps its spot while it stays on the line and clear. The picture and
the page draw labels above every mark; on the page the operator rewrites a
label by typing on its arrow (double-click the line).

**Style** (on `shape`, `arrow`, `frame`, `pen`, `path`, `graph` and its
nodes, `mermaid`, `restyle`): `tone` is `neutral`, `info`, `success`,
`warning`, `danger`, `accent`, `idea` or `decision` and sets the stroke, fill
and label colour together from the design tokens (`herdr_team/canvas_tokens.json`);
`variant` is `soft` (a tint, the default), `solid` or `outline`. A note
defaults to `idea`, everything else to `neutral`; authorship is not a colour
(0.22). `color` is a name (read as its tone: `red` is `danger`) or `#rrggbb`
(drawn as given), `fill` a name, `#rrggbb` or `none`; either wins over the
tone in the same op. `width` 1, 2, 4 (`thin`, `bold`, `extra`), `dash`
`solid`/`dashed`/`dotted`, `opacity` 10..100, `font` `normal` (the default,
Inter), `code` (monospace) or `hand` (the legacy sketch font), `size` `s`,
`m`, `l`, `xl` (16, 20, 28, 36), `rough` 0 (the default), 1 or 2.

**Fit** (0.22). A labelled element stores how its label was fitted:
`"fit": {"policy", "min": [w, h], "size", "lines": [...], "truncated", "estimated"}`.
`policy` is `hug` for a box and a text (it grows on the 20-unit grid,
wrapping at 320, or 240 for a graph node and 600 for a text; a paragraph
wraps wider, up to 40 em, rather than stand as a tower), `shrink` for a
note (the paper keeps its size and the text steps down to 14 before it grows;
a note in a graph hugs instead, at its neighbours' size) and `scale_shape` for
an ellipse or a diamond (it grows as a whole until its inscribed box holds the
label). `min` is what the op asked for, or the kind's minimum (box and ellipse
160x80, diamond 200x120, note 180x120, graph node 160x60, a graph note or
diamond its shape's minimum); `size` is the size drawn (smaller than `style.size` only after
`shrink`); `lines` are the lines `look`, `check` and the picture use;
`estimated` says a character the bundled fonts lack (CJK, emoji) was
estimated. Widths come from the bundled fonts' own metrics
(`assets/fonts/font-metrics.json`), the files the page and resvg draw with.
Lines break at spaces, then after `/ _ . - :` inside a long token, then
between Chinese or Japanese characters, and mid-word only when no wider box is
possible or the piece is wider than 24 em (480 at size 20): a long URL or
identifier breaks rather than make a giant shape. Elements drawn before 0.22 have no `fit` and refit the next time
their text, size or style changes.

**Result** (`draw --json`, the MCP `structuredContent`, the page's `POST /ops`):

```json
{"team":"alpha","version":44,"batch":"B-12","atomic":false,
 "applied":[{"index":0,"op":"claim","ids":["K-3"]},{"index":1,"op":"frame","ids":["E-2"],"alias":"drivers"},
            {"index":2,"op":"shape","ids":["E-3"],"alias":"price",
             "geometry":[{"id":"E-3","x":220,"y":120,"w":180,"h":120,"fit":{"policy":"shrink","size":20,"lines":2}}]}],
 "refused":[{"index":5,"op":"pen","code":"canvas_locked","message":"…","details":{"lock":"X-1"}}],
 "aliases":{"drivers":"E-2"},
 "warnings":[{"index":4,"code":"inside_claim","message":"…","ids":["E-5"]}],
 "notices":{"canvas_changed":51,"canvas_sent":[52]}}
```

Each applied entry lists the `geometry` of every labelled element the op
created or refitted (id, bounds, and `fit` as policy, drawn size and line
count; `truncated` or `estimated` when true), at most 50, with
`geometry_omitted` counting the rest; the text form prints
`#2 shape E-3 → 180x120 (shrink, 2 lines)`. Exit 0 when at least one op applied; `canvas_refused` (1, details = the
result) when every op was refused or an atomic batch was. Batch-level errors
raise: `whiteboard_off`, `author_unverified`, `canvas_limit` (over 100 ops or
512 KB), `canvas_rate` (over 300 ops, or 8 MB written to the log, a minute per
agent, `retry_after_s`), `canvas_busy` (5, the canvas lock). One agent op that
would write over 2 MB to the log (a move or restyle over many large elements;
every touched element is logged whole) refuses `canvas_limit`
(`MAX_OP_CHANGE_BYTES`).

**`look`** lists the region in full, the rest one line each, far clusters by
author and direction, then the claims, locks, legend and the comments that
mention the reader; `--since` adds the changes; `--image` renders a PNG with
element ids (and `--grid` cell labels) through `resvg` when it is installed, drawn with the
bundled Inter and Geist Mono at each label's stored lines
(`image_error: "resvg_missing"` otherwise); `--theme dark` draws it in the
dark theme (since canvas v2 phase 1 the picture is the display list, drawn
with the same token palettes the page uses); `--exact` asks an open writable page for
its own picture of the board (either engine answers; 5 s, else the server render). JSON
`{"team","version","reader","switch","region","region_cells","level","elements","elsewhere","clusters","omitted","since","changes","claims","locks","legend","comments_for_you","image","svg","image_error","exact","text"}`.
`look --since last` and `changes` advance the reader's cursor. `look` ends
with the layout problems in view (at most 8; `canvas check` lists them all).
A chart or 3D scene reads back as its gist (canvas v2 phases 3 and 4): the
readback line, then up to 6 lines in words (a chart's channels and source,
extremes with their categories, trends and shares; a scene's objects with
their relations), all of them with `--full` (a chart adds its columns and
the model's first rows); `--block REF` prints its spec. JSON gains `gist`
(`{id: [lines]}`) and the facts a kind adds under its own key (`charts`:
`{id: {type, engine, source, frame_notes}}`; `scene3d`). `--image --view
iso|front|top` draws each slot with still views in that view: the page's still
of it when there is one, else the kind's own drawing (a scene's projection).

**`catalog charts|scene3d [--type NAME]`** prints what the chart types take
(channels with the types they accept, options, caps and one example op each),
or the 3D primitives with their parameters, the relations and the group
layouts; generated from the registries, so a new type or primitive module
shows up with no edit. It needs no team. JSON is the registry's
`catalog()` with `what`.

**`check [--region R | --around ID] [--mine]`** reviews the layout by geometry,
for agents that cannot read images and for those that misjudge them:
`overlap` (two marks partly on top of each other; one wholly inside another is
a grouping), `text_on_label` (text lying on a labelled shape), `label_overflow`
(a shape's or a text's label needs more room than it has at the size it is
drawn, measured with the bundled fonts; since 0.22 shapes grow to fit, so this
finds elements drawn before that), `label_truncated` (a clamped label shows
only part of its text; for a table cell the fix is the smallest change that
keeps it whole - more `max_lines` (up to 4), else a wider column (up to 600),
else, with no fix, the number of characters the cell must be cut to), `frame_edge` (a mark half inside a frame), `arrow_through` (an arrow
crossing a mark it does not connect), `route_loop` (an arrow whose route crosses
itself or runs back through one of its own ends), `label_astray` (an arrow label
placed so far beside its line that it reads as something else's) and `stray` (a
mark 1500+ units from the rest). Each problem has `ids`, a `message`, `yours`, and `fix`: an operation
that applies as it stands (`move … to` a free spot, a resize, a frame grown,
or `inside` a frame) or null; an `arrow_through` fix restyles a bound arrow
orthogonal, and an arrow already routed orthogonal gets none (no clear way
around: move what is in the way). JSON
`{"team","version","reader","region","mine","problems":[{"code","ids","message","fix","yours"}],"text"}`.
Reads only; the reader's own problems come first.

A printed repair respects ownership. If the reader cannot make it, `fix` is
null and `fix_by` names who can. A graph relayout checks the members' ownership
as well as the root's: an operator-owned root under `human_edits: live` does
not grant permission to change a peer's nodes. An ambiguous graph alias uses
`{"op":"patch","id":"E-n","relayout":"full","intent":"…"}` instead.
Normal role-specific freeze, lock and pin rules still apply; agent repairs
respect human pins, while the operator retains their authority. A permitted repair of a frozen own graph
can become one proposal rather than change the drawing.

When the reader's own exact current repair proposal is open, `proposal` names
it and `fix` is null: do not file the same request again. An unrelated payload,
an outdated target or an old record without repair identity does not count.
When several findings share one repair, only the first prints it; the others
carry `fix_with: {code, ids}` pointing to that finding. Run that operation once,
then check again. The findings themselves are not hidden.

Graph readability also reports `crossings_high`, `routes_tangled`,
`labels_adrift`, `graph_thin` and `bands_apart`, with the same permission and
proposal guidance. `bands_apart` means a full relayout would materially close
an empty routing corridor between groups.

A claim's region snaps outward around the marks it holds, including arrow
labels and frame titles (`claim_snapped` names what it took in). Canvas v2
breaks a dashed border under a title above its frame at overview zoom, and
moves the claim label to a clear corner or omits it when every corner is on
words. Only the claim's author receives a `claim_edge` repair; other readers
are told whose claim it is, rather than creating a duplicate claim over it.
The classic canvas keeps its earlier claim rendering.

**`refit [--ids E-1,E-2]`** (canvas v2 phase 1) sizes labelled elements again
from their minimum (`fit.min`, or the size of an element stored before 0.22)
with today's font metrics and the page's measurements (below): a board drawn
before 0.22 gets the sizes its labels need, and a label the browser measured
wider than the metrics said gets its room. It only ever grows a box; a shape
that grows pushes its neighbours (`moved_to_fit`) like any other growth, and a
labelled arrow places its label again. Without `--ids` it takes every element
the caller may edit (at most 500; elements in a lock are left as they are).
The op is `{"op": "refit", "ids": [...]}`; `if_version` holds with ids. Its
result carries `geometry` as `shape` does.

**`migrate [--apply | --dismiss] [--json]`** (0.22) is the one-time move of a
board drawn before canvas v2. Replaying the log loses nothing (0.21 stored no
element type 0.22 cannot draw), but two things draw differently: a label
measured with the bundled fonts may no longer fit the box 0.21 gave it, and
0.21's sketch style - rough strokes (`rough`) and the hand font, which it drew
with by default - is drawn clean. Without a flag it prints the report, which
anyone may read:

```json
{"pending": true, "state": null, "pre_022": 3, "refit": ["E-4", "E-7", "E-9"], "overflow": ["E-9"], "sketch": ["E-12"],
 "legacy_colour": 2, "unknown_hex": 1, "vega_lite": 1}
```

`pending` is true while the operator has neither applied nor dismissed it and
there is something to do (a mark in the old sketch style, or an element drawn
before 0.22 that `refit` would size); `state` is
`{"action": "apply"|"dismiss", "seq", "by"}` once they have. `pre_022` counts
labelled elements with no `fit` record; `refit` lists those whose kind can be
measured (what `--apply` refits, at most 2,000), `overflow` the ones whose
label no longer fits, and `sketch` the marks with `rough` above 0 or `font:
hand`. `legacy_colour`, `unknown_hex` and `vega_lite` only inform: those draw
as they are. An empty board, or one drawn only with 0.22, is never pending.

`--apply` and `--dismiss` are the `migrate` op, refused `operator_only` for
anyone but the operator in person, delegates included. `--apply` refits every
id in `refit` in one batch (in `refit` steps of at most 500, grow only,
neighbours pushed aside with `moved_to_fit`), sets `rough: 0` and `font:
normal` on every `sketch` id, and records the setting once nothing is left;
pins and freezes do not stop it, and comments follow their elements. A board
with more than 2,000 marks to fix takes more than one `--apply`: each is its
own undoable batch, the answer is recorded only by the last one, and until then
the report stays pending and the result says how many are still to go. It
answers `migrated: 14 refitted (6 grew, 2 moved to fit), 3 drawn clean (rough,
hand font); undo B-12 to take it back`. Undoing that batch restores the elements and removes the setting,
so the report is pending again. `--dismiss` records only the setting. While the
report is pending, the operator's own `look` carries one line (and `--json` a
`migration` object), members and delegates see nothing:

```
migration: drawn before canvas v2 · 14 labels need resizing · 3 marks in the old sketch style now drawn clean · herdr-synapse canvas migrate --apply (or --dismiss)
```

A board whose old labels all still fit, but were never sized by 0.22, says
`N labels to size again` instead of `need resizing`. Because 0.21 drew every
mark sketchy by default, the sketch count is usually every mark of a 0.21
board; `--apply` therefore restyles the whole board, which one undo takes back.
Growing labels can uncover a problem the old sizes hid, so run `canvas check`
after it: the fix moves a mark it pushed onto a neighbour, but an arrow whose
line is now short under its label (`label_astray`) and a mark that now reaches
into a frame it does not belong to are left for you to place.

Storage, per team under `whiteboard/`: `events.jsonl` (one event per applied
op, the source of truth), `scene.json`, `cursors/`, `assets/`, `stills/`,
`renders/`, `exports/`, `notices.json`, `rate.json`, `mcp.json`, `archive/`.
Nothing is written into the project folder. Every canvas write runs under
`whiteboard/canvas.lock` (a timeout is `canvas_busy`, exit 5); board records
are posted only after it is released.

An event (`canvas changes --json`, the page's `ops` stream):

```json
{"v":1,"seq":43,"ts":"…","batch":"B-12",
 "author":{"name":"alpha-worker","kind":"member","agent":"claude","via":"mcp","verified":true},
 "op":"shape","index":3,"intent":"the biggest driver","ids":["E-3","E-2"],
 "changes":[{"target":"element","action":"add","id":"E-3","value":{…}},{"target":"element","action":"update","id":"E-2","value":{…}}]}
```

`seq` is the canvas version after the event; folding `changes` in order
rebuilds the scene. `ids` lists every id the op created or changed, in the
order of the result's applied ids. An `undo` event also carries `undoes` (the
`B-` id it reverted), a `portrait` event `portrait: {"current","total"}`, and
the first event after `whiteboard clear` (`op: "clear"`, no changes) carries
`counters`, so ids keep counting when the scene is rebuilt from the log.

A `path` element stores `d` normalised: absolute commands, moved so the
path's own bounds start at 0,0. Its `x`,`y`,`w`,`h` is the placed bounding
box, and it draws as `translate(x,y) scale(scale)` followed by `d`; a resize
keeps the aspect ratio by changing `scale`. `if_version` is accepted on every
op and enforced on `move`, `restyle`, `edit` and `delete`.

`canvas send ID... --to NAME [--note TEXT]` (the operator) renders the
elements' bounds plus 40 with their ids and posts `canvas_sent` to that
member; an unknown member refuses `member_not_found`. JSON
`{"seq","image","svg","elements"}`.

`canvas export` prints the text listing by default (`--format md`); `json` is
the scene, `svg` and `png` the server render (`--region R`, `--no-marks`
drops the id badges, `--grid` adds cell dots and names). `--out PATH` writes a
file instead of printing; `--format png` without `--out` prints the path of
the render under `whiteboard/renders/`, and refuses `render_unavailable` when
`resvg` is missing (`$HERDR_SYNAPSE_RESVG`, else `PATH`).

`canvas import` is the inverse of `canvas export` and the only way a board comes
back: it replays a scene into an **empty** canvas as one undoable batch (the
`import` core op, so it lands in the event log, folds into the scene, reaches the
page and is undone like any batch). `--from PATH` takes a project folder's
`canvas.json`, a dissolved team's directory, a `whiteboard/archive/<stamp>/`
folder (a canvas lost to `whiteboard clear`) or a file `canvas export --format
json` wrote; `--from-archive TEAM[@STAMP]` takes a team dissolved in this
session, with `--list` to see what each archived copy holds and `--latest` to
take the newest without naming its stamp. Source element ids are kept, so every
binding survives; `created_seq`/`updated_seq` are rewritten to this batch, and
the id counters are seeded past what arrived. Each mark records its origin under
`imported`. Marks are re-attributed to the importer unless `--keep-authors` (the
operator in person; they can edit marks or `undo B-n` with either choice - see
[collaboration.md](collaboration.md) and [inheritance.md](inheritance.md)). A
re-attributed board is the operator's, so with the default `human_edits:
propose` the team's edits of it arrive as proposals; `--team-can-edit` sets
`human_edits: live` inside the same batch (and `undo B-n` takes that back with
the marks). It is refused with `--keep-authors`, which asks for the opposite.
`--dry-run` reports every refusal the real run would raise and writes nothing:
no lock file, no asset, no event. Refusals name their repair, including a canvas
that is not empty, an unreadable or over-cap scene (the mirror's pointer form,
which names `canvas export` instead), a mark whose data file or picture is
missing (`--skip-missing`, and the message names the file it found beside the
document and the `cp` that repairs it), a kind this build does not know
(`--skip-unknown`), an asset whose bytes do not hash to its name, a document
that is damaged rather than the wrong kind of file, and a re-import of a
document this canvas already holds. Nothing in the source is ever written to.

`canvas helper` prints the path of `sketch.py`, a standalone, stdlib-only
Python helper that builds batches (`--print` prints its source):

```python
from sketch import Sketch
s = Sketch(default_intent="map the churn drivers")
f = s.frame("Churn drivers", at="c10r4", w=600, h=360)
price = s.shape("note", "Price rise in March", inside=f)
s.print()   # python3 plan.py | herdr-synapse canvas draw --file -
```

Each method returns the op's alias. Generated aliases are tagged per run
(`s<4 characters>.<n>`, for example `s3f9.1`), so running the same script
twice does not collide with `alias_taken`; `Sketch(prefix=…)` sets the tag.

### The MCP server (`canvas mcp`) and launch-time injection

`canvas mcp` is a stdio MCP server (newline-delimited JSON-RPC 2.0; protocol
versions 2025-06-18, 2025-03-26, 2024-11-05) named `synapse-canvas`, with
tools `canvas_look`, `canvas_check`, `canvas_draw`, `canvas_comment`,
`canvas_claim`, `canvas_legend` and `canvas_changes`. Each result carries the CLI's text and
its JSON as `structuredContent`; `canvas_look` with `image` adds the PNG. It
acts only as a verified member of the team its `--team` names; anything else
fails `not_a_member`.

Members Synapse starts get it at launch while their team's canvas is on,
without editing any harness configuration:

| Kind | Appended last to its launch argv |
| --- | --- |
| `claude` | `--mcp-config <session>/teams/<team>/whiteboard/mcp.json` |
| `codex` | `-c mcp_servers.synapse_canvas.command="<plugin>/bin/herdr-synapse"` `-c mcp_servers.synapse_canvas.args=["--socket","<socket>","--team","<team dir>","canvas","mcp"]` |
| any other kind, Pi included | nothing (the CLI works for every kind; Pi has no `--mcp-config` and exits on an unknown flag) |

`mcp.json` is `{"mcpServers":{"synapse_canvas":{"type":"stdio","command":…,"args":[…]}}}`.
`create --spawn`, `resume`, `restore` (resumed and fresh) and `swap` build the
same flags. A controlled restart (`model --apply restart`, `profile --apply
restart`) carries them from the live argv through its `preserved` flags, and
only values that are Synapse's own (an absolute `…/whiteboard/mcp.json`, or
`mcp_servers.synapse_canvas.command|args`): a user's inline `--mcp-config`
JSON or another `-c` override is dropped. A member started while the canvas
was off has no MCP tools until it is relaunched; a member whose canvas is
switched off later keeps the server, whose calls then return `whiteboard_off`.
In YOLO mode the tools run without prompts; in `native` mode the harness asks
once.

### The page

A stdlib HTTP server bound to `127.0.0.1` only, on a random free port,
recorded in `<session>/whiteboard.json` and logging to `whiteboard.log`. It
stops on `whiteboard stop`, on `disable`, and after 30 minutes with no page
open. The opening ticket is exchanged once for an `HttpOnly`,
`SameSite=Strict` cookie and never stays in the address bar; every request
checks `Host` and `Origin`, every write a CSRF header; the page runs under a
strict Content-Security-Policy with no remote loads. Agent `viz` code runs only
in `<iframe sandbox="allow-scripts">` documents served from `/viz/<team>/<id>`
with their own policy (`connect-src 'none'`); the vendored d3, three and p5 are
served from `/viz-lib/`. The page itself (canvas v2 with ECharts and three.js,
Mermaid, Vega-Lite, and the classic Excalidraw canvas at `?engine=v1`) is
prebuilt in `web/dist/` with its licences and a hash list, so nothing needs
Node to run it. Tabs: Canvas, Team (the views above), Activity (watch cards),
and Diagrams (every mermaid, chart, viz and svg element with its source).

Security model, in full (a speed bump against the obvious routes, not a
sandbox against a process running as the same user):

- Loopback only: the server binds `127.0.0.1`, never `0.0.0.0`.
- The opening ticket is 32 random bytes, valid for 120 s and once; its hash
  is stored in `<session>/whiteboard-tickets/` (files 0600). `GET /?ticket=`
  answers `303` to `/` with `synapse_wb=<session>; HttpOnly; SameSite=Strict;
  Path=/`. Page sessions live in memory (at most 64), so a restarted server
  needs a new ticket.
- Every request: `Host` must be `127.0.0.1:<port>` or `localhost:<port>`
  (`bad_host`); a present `Origin` must be the server's own (`bad_origin`);
  `Sec-Fetch-Site: cross-site` or `same-site` is refused (`bad_origin`),
  because another page on another localhost port counts as same-site and
  would carry the cookie. Every `POST` needs `Origin` and the session's CSRF
  token in `X-Synapse-CSRF` (`bad_csrf`). No CORS headers except on
  `/viz-lib/`.
- The page's CSP allows only its own origin and `wasm-unsafe-eval` (no
  inline script, no `unsafe-eval`, no remote origins); assets are served
  under `sandbox`; `/viz/<team>/<id>` documents run under `sandbox
  allow-scripts` with `connect-src 'none'`, and load scripts only from
  `/viz-lib/`.
- The page writes as the operator only when the server was started by the
  operator in person and the ticket was minted for them; otherwise every
  write, including stills and exports, answers `403 read_only`. `look
  --exact` therefore needs a writable page open. `whiteboard open` by the
  operator restarts a running read-only server as a writable one.

`whiteboard open` passes the starter to the `whiteboard-serve` process in two
environment variables: `HERDR_SYNAPSE_WB_WRITABLE` (`1` or `0`, default
read-only) and `HERDR_SYNAPSE_WB_BY` (JSON `{"name","via","verified"}`), plus
`HERDR_TEAM_STATE_DIR` so the child resolves the same state root.

The JSON API lives under `/api/`: `session`, `teams`, and per team
`/api/teams/<t>/` `scene`, `changes?since=N`, `display[?since=N]`, `measure`
(POST), `ops` (POST), `send` (POST),
`uploads` (POST, PNG or JPEG), `stills/<id>?v=N[&view=V]` (POST; `view` one of the element kind's still views, `iso`, `front` or `top` for a 3D scene), `exports/<id>`
(POST), `assets/<name>`, `artifact?path=`, `text?ids=E-1,C-2`, `views`, `presence`
(GET, and POST from a writable page); plus
`/api/activity?team=` and the event stream `/api/stream?team=&since=` (`hello`,
`scene`, `ops`, `presence`, `state`, `views`, `activity`, `export_request`, `bye`).
`POST /ops` takes the batch envelope with `base` (canvas v2 phase 5).
Errors are `{"code","message",…}` with these statuses:

`GET display` (canvas v2 phase 1) answers the team's display list
(`docs/display-list.md`): every element, claim and lock as drawing primitives
with the text already broken into lines and colours as theme tokens, the list
the v2 page and `look --image` draw. With `since=N` it answers what changed
after version N, `{"dl","version","since","full":false,"bbox","upserts","removes"}`,
or the whole list with `"full": true` when a delta cannot say it (the canvas was
cleared, a lock or an author changed, more than 400 ids changed, or N is ahead).
Read-only pages may read it.

`POST measure` is the page's text check (writable pages only):
`{"version","metrics","lines":[{"id","font","weight","size","t","w"}]}` with at
most 200 lines and 64 KB, one request per 2 s per page (`429 measure_rate`,
`retry_after`). A line is kept when it is one of that element's drawn lines at
that font, weight and size, the element did not change after `version`, the
page measured against the same font metrics (`metrics` is the sha256 of
`assets/fonts/font-metrics.json`), and its width is more than 0.5 units over
the server's and at most 4 times it; otherwise it is ignored with a reason
(`not_a_line`, `not_wider`, `implausible`, `stale`). Kept widths go to
`whiteboard/measure.json` (the widest ever seen per line, at most 5,000, the
oldest dropped; other metrics empty it) and every measurement on the team uses
them from then on; an element whose label no longer fits as stored is refitted
by a `refit` op in the operator's name (intent "the page measured wider text").
JSON `{"accepted","ignored":[{"i","why"}],"refit","batch","version"}`.

The page opens on canvas v2, the display-list renderer (0.22). `?engine=v1`
(and a ticket link with `&engine=v1`, whose redirect keeps it) opens the
classic Excalidraw page, which is loaded only then; a top-bar chip switches
between the two ("Classic canvas" on v2, "Canvas v2" on v1), and the page keeps
the choice in the browser under `synapse-engine-v022` (the key earlier builds
used, `synapse-engine`, is ignored and removed). A v2 page whose server has no
`display` route falls back to v1 for that visit.

While the team's migration report (see `canvas migrate`) is pending, the full
`GET display` answer carries an optional `migration` object: the report
without the id lists, plus `ids_total`. A delta carries it when it changes (the
object, or `null` once applied or dismissed). A writable v2 page shows it as a
banner with **Fix sizes** (the `migrate` op with `action: apply` through `POST
ops`), **Details**, **Not now** (this visit only) and **Dismiss**; read-only
pages and v1 show nothing.

`POST presence` (canvas v2 phase 5, writable pages only) is the operator's page
telling agents where they are: `{"page": "<16 hex>", "viewport": [x0,y0,x1,y1],
"selection": [ids], "editing": id|null, "cursor": [x,y], "away": bool}` (every
key but `page` optional, at most 4 KB, at most 8 a second per page: `429
presence_rate`, `retry_after`), stored as `whiteboard/presence/human-<page>.json`
for 30 s; JSON `{"ok": true, "ttl_s": 30}`. `GET presence` (read-only pages
too) answers `{"at", "entries": [...]}`, the fresh records of their pages and of
members (`whiteboard/presence/<name>.json`, written by their draws, looks and
`canvas focus`). The stream sends `presence` (the same, plus `team`; no `id:`)
after `scene` or `changes` at start, whenever the directory changes, and at
least every 15 s while any record is fresh. Presence is never authority: it can
only refuse an agent's op on what they are editing now (`element_busy`).

| Status | Codes |
| --- | --- |
| 400 | `usage`, `op_invalid`, `element_unknown`, `svg_refused`, `chart_refused`, `mention_unknown`, `secret_detected`, `echo_rejected` |
| 401 | `not_signed_in` |
| 403 | `whiteboard_off`, `viz_off`, `read_only`, `author_mismatch`, `author_unverified`, `operator_only`, `element_not_yours`, `canvas_locked`, `frozen`, `path_refused`, `not_a_member`, `bad_host`, `bad_origin`, `bad_csrf` |
| 404 | `team_not_found`, `member_not_found`, `artifacts_unset`, `not_found` |
| 405 | `method_not_allowed` |
| 409 | `canvas_stale`, `canvas_refused`, `alias_taken`, `stale_base`, `element_busy`, `in_proposal`, `proposal_outdated` |
| 413 | `canvas_limit`, `image_refused`, `body_too_large` (bodies over 6 MB) |
| 415 | `unsupported_media_type` |
| 429 | `canvas_rate`, `measure_rate`, `presence_rate`, `proposal_limit` |
| 503 | `canvas_busy`, `page_not_built`, `too_many_streams` (more than 8) |
| 500 | anything else, as `internal`, logged without a traceback to the page |

`whiteboard open` itself can refuse `whiteboard_start_failed` (the server did
not write `whiteboard.json` within 5 s; see `whiteboard.log`).

Rebuilding the page needs Node: `cd web && npm ci && npm run build`. The
post-build step regenerates `dist/MANIFEST.json`, `dist/manifest.sha256` and
`dist/licenses/`. Commit `web/dist/` and `web/package-lock.json`;
`web/.gitignore` ignores `node_modules/`.

### `watch …` / `unwatch TARGET`

```
watch TARGET                  flag an agent (pane id, agent name, or member name); operator in person
watch show [TARGET]           activity cards (operator; the manager for its own team's members)
watch list                    the watched agents
unwatch TARGET                drop the flag and clear its sidebar token
```

Who: `watch TARGET` and `unwatch TARGET` are the operator in person;
`watch show` is the operator, or a manager or delegate for its own team's
members (it appends a `watch_show` audit record); `watch list` is anyone. A
target is a pane id, a member name (the hinted team first), a Herdr agent
name, a terminal id, or a recorded watch entry; `watch` with no words is
`watch list`. An unknown target, one that is not running, or a shell pane
refuses `agent_not_found` (exit 1). Every form except `watch list` refuses
`whiteboard_off` while the layer is off. `unwatch` on an agent that is not
watched exits 0 with `removed: null`.

`watch.json`: `{"v":1,"agents":[{"terminal_id","pane_id","name","kind","team","by","at"}]}`,
keyed by terminal id (a moved pane keeps its watch), at most 20
(`watch_limit`). Each watched agent's reader offsets and last card live in
`<session>/watch-cache/<terminal_id>.json`; the notifier sweeps cache files
older than a day. A card:

```json
{"key":"term_w1","terminal_id":"term_w1","pane_id":"w2:p2","name":"alpha-worker","team":"alpha","role":"worker","kind":"claude","profile":null,
 "watched":true,"state":"working","headline":"Pulling Q3 exports",
 "plan":{"steps":[{"text":"fetch Q3 exports","status":"in_progress"}],"current":2,"total":7,"source":"todo"}|null,
 "actions":[{"icon":"run","text":"python pull.py","at":"…"}],"files":["src/pull.py"],"context":{"percent":41.0,"used":82000,"window":200000}|null,
 "last_prompt":"…","doing":"▶ 2/7 fetch Q3 exports","reader":"claude","reader_error":null|"unknown","reader_note":"…"|null,"updated_at":"…"}
```

`state` is `working`, `idle`, `done`, `blocked`, `unknown` or `not_running`.
`plan.source` is `todo` (Claude), `update_plan` (Codex) or `opencode_todo`;
Pi and other kinds have no plan. `reader_error` is `unknown` when a reader
failed or the transcript format drifted (the card still has its title and
state); `reader_note` explains it, for example that no harness session was
reported yet and `herdr integration install codex` would help.

Cards are read from what each harness already writes (terminal title, Claude
and Pi transcripts, the Codex rollout, OpenCode's database), incrementally and
at most 512 KB per agent per refresh (1 MB on the first look at an agent); only tool names and short arguments are
shown, never output, and every string passes the board's secret patterns plus
the shapes a command line carries (`NAME_PASSWORD=…`, password flags,
`user:pass`, credentials in a URL, an `Authorization` header, more key
prefixes). The notifier stamps a watched pane's `team_doing` token (source
`herdr-synapse:watch`, 60 s TTL, refreshed every 15 s, ≤ 32 columns) while the
layer is on; for a command it shows only the program (`$ psql`, `$ git
commit`), never its arguments, because every agent can read pane tokens; `setup --print-config` puts `$team_doing` on a row of its own.
`o` on an agent row in `prefix+t` toggles watch; while the layer is off it
asks to turn it on and watch in one step. `d` on any row of `prefix+t` opens
the whiteboard menu, which runs these same commands from the popup (the
operator in person): `watch`/`unwatch`, `whiteboard enable`/`disable` (off asks
first), `whiteboard open`, `--team T whiteboard team|viz on|off`, and for an
agent in no team `create <name> --member <pane>:<role>:<name> --brief
<name>=<Mission>`, a team of one whose name defaults to `<agent>-canvas`. A
choice that needs the layer while it is off runs `whiteboard enable` first.

### Board records

All of these are `from: system`, `kind: system`:

| `event` | Delivery | When |
| --- | --- | --- |
| `canvas_changed` | awareness, never a wake | one per author per minute: `alpha-worker drew on the canvas: 3 notes, 2 arrows, 1 pen stroke, moved 2 (v38–v42). Look: herdr-synapse canvas look --since 37`; `extra.canvas` has `author`, `from`, `to`, `counts`, `batches` |
| `canvas_sent` | a named, idle-gated nudge; a toast when `human` is addressed | a comment's `@mention`, or the operator's "Send to…": the text form and a PNG path; an agent's words are quoted as a peer's request |
| `whiteboard_state` | awareness | a switch flip changed what a team's members may do; `extra.whiteboard` has `before`, `after`, `by` |
| `canvas_imported` | awareness, addressed to `all` | a board was inherited from another team: the source, the mark count, the batch and the `canvas undo` that takes it back, so the team knows what it is looking at is not its own work |

### Error codes

| Code | Exit | Meaning |
| --- | --- | --- |
| `whiteboard_off` | 1 | the layer or the team is off (`details.scope`: `session` or `team`) |
| `viz_off` | 1 | the team's live visuals are off |
| `canvas_locked`, `element_not_yours`, `operator_only`, `element_unknown`, `alias_taken`, `op_invalid`, `canvas_stale` | 1 | op refusals (in `refused`) |
| `canvas_limit`, `canvas_rate`, `canvas_refused` | 1 | size, rate, or every op refused |
| `canvas_busy` | 5 | the canvas lock timed out |
| `svg_refused`, `chart_refused`, `image_refused`, `path_refused`, `artifacts_unset`, `mention_unknown` | 1 | content refusals |
| `portrait_no_plan`, `render_unavailable`, `watch_limit`, `agent_not_found`, `member_not_found`, `confirmation_required` | 1 | command refusals |
| `whiteboard_start_failed`, `whiteboard_not_running` | 1 | the page server did not start / is not running |
| `read_only`, `not_signed_in`, `bad_host`, `bad_origin`, `bad_csrf`, `page_not_built` | — | page HTTP (403, 401, 403, 403, 403, 503) |

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
(`"never"`, one of `never|always`), `interrupt_kinds`
(`["claude","codex","opencode","pi"]`, the
agent kinds whose running turn a teammate's `post --interrupt` may be typed
into; an empty list turns interrupts off), `interrupt_cooldown_ms` (600000,
one interrupt per sender and target). `post_ttl_ms` is the target-active time after which an
unread post is `expired` (paused while the member is `missing`); expiry and
abandonment leave the cursor unread but are terminal for automatic delivery,
persist across daemon restarts, and may be retried explicitly with
`nudge --force`;
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

`daemon.json` (one line, compact): `{"pid":4021,"start_time":"Thu Sep  4 13:53:10 2026","beat_at":"…","socket":"…","socket_inode":123,"version":"0.16.0","herdr_version":"0.9.0","protocol":22,"capabilities":{"atomic_idle_prompt":true}}`.
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

## Launch permissions

`permissions [MEMBER [yolo|native|inherit]]` shows or saves next-launch settings.
`permissions --default yolo|native` sets the team default. The same arguments
work with `/permissions` in the console. Writes require operator authority;
reads are available to members. Settings resolve member → team → `yolo`.
`inherit` removes the member override. New teams also accept `create
--permissions MODE` and repeated `--member-permissions NAME|ROLE=MODE`.

The JSON result contains `team`, `default`, and `members`. Each member has
`name`, `kind`, `mode`, `source` (`member`, `team`, `default`), `flags`, `evidence`
(`live`, `help`, `docs`, or null for a kind with no switch), `effect`,
`applies: "next launch"`, and `running_mode: "unknown"`. `evidence` says how the
flag is known: exercised live, read from the installed binary's `--help`, or
taken from the vendor's documentation only. `who` includes this
policy under each agent’s `permissions`; create output uses `launch_permissions`
to distinguish it from the persisted member override.

Saved policy applies to create, resume, restore, swap and controlled restarts.
The canvas MCP flags (section 9n) follow the same paths and are appended after
the permission flags.
Pending swaps lock their policy until completion/cancellation. Stale queued
restart argv is rejected if the policy changes. Running agents are unchanged.
An outdated daemon must be replaced successfully before a new policy is saved.

Native mode adds no bypass flags and still respects native config and profiles;
it does not guarantee sandboxing or approval dialogs. Pi’s YOLO flag trusts
project resources, not tools: Pi has no built-in tool approval prompts.
See the [permission table](../README.md#launch-permissions-yolo-by-default).
