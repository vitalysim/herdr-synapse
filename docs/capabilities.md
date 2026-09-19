# herdr-synapse capabilities reference

Fresh agent replacement: open a member's action menu and press `0`, choose Claude Code, Codex, OpenCode, or Pi, optionally set its model/effort, and confirm. Synapse creates a new instance in a new tab, closes the outgoing pane, preserves member configuration, and queues a handoff. Console: `/swap <member> <kind> [model@effort]`. CLI: `swap <member> --to <kind>`, with `--dry-run`, `--status`, `--retry`, and pre-takeover `--cancel`. No existing replacement agent is needed. See [the command contract](cli.md#create-a-replacement-agent).

Replacement acceptance checklist: verify configuration and historical read positions survive; the destination is a newly created instance; an unrelated agent of the same kind is untouched; a quota-blocked source does not need to answer a prompt; failed startup can be inspected and retried in its reserved pane; old direct/control jobs do not reach the replacement. Native transcripts stay separate. Completion reports creation and queued briefing, not verified provider availability.

Team restoration: in `Ctrl+B`, `T`, highlight a saved team and press `s`, or run `herdr-synapse restore <team>`. Missing agents reopen in a new named tab using their saved conversations, or start fresh when no conversation ID exists. Roles, manager, settings, instructions, board history and team links are retained. Running/reserved panes are skipped; startup failures remain available to inspect. See [the command contract](cli.md#restore-a-saved-team) for preflight and partial-result behavior.

Everything the plugin can do, how to drive it from the Herdr UI and from the
CLI, what you should observe, and how to check it. Written for the first
human test in a real session and verified line by line against the code on
2026-09-05. The CLI contract with every JSON shape is `docs/cli.md`; the
guided first run is `docs/human-testing.md`; what agents are taught is
`skills/herdr-synapse/SKILL.md`; the complete command and key inventory is
[`docs/reference.md`](reference.md).

Conventions: "UI" means keys and panes inside Herdr (needs the key snippet
from `herdr-synapse keys print` pasted into your config once). "CLI" means
`herdr-synapse …` from any pane, or from outside Herdr with `--team`. Every
command accepts `--json`. Exit codes: 0 ok, 1 refused, 2 usage, 3 not a
member or Herdr unreachable, 4 echo rejected, 5 daemon down or lock timeout.
Each section ends with **Verified** (which rig test exercised it live on
Herdr 0.8.2).

Global flags on every command: `--team NAME|PATH`, `--session NAME`,
`--socket PATH`, `--session-mismatch-ok`. Environment the CLI honours:
`HERDR_TEAM_HUMAN` (your label on human posts), `HERDR_TEAM_MEMBER` (offline
identity fallback), `HERDR_TEAM_HOOKS=off` (disable the Claude hooks for an
agent at launch), `HERDR_TEAM_NO_DAEMON=1` (never auto-start the daemon),
`HERDR_TEAM_STATE_DIR` (state root override, for rigs).

Where things live: `~/.local/state/herdr/plugins/herdr-synapse/sessions/<session>/`
holds `daemon.json`, `daemon.log`, `who.json`, `kinds.json`, `view.json`,
`console.json`, `mute.json`, and `teams/<team>/` with `team.json`,
`board.jsonl`, `cursors/`, `payloads/`, `notifier/`, `audit.jsonl`.

## 1. Teams

| Capability | UI | CLI |
| --- | --- | --- |
| Create from live agents | `prefix+t` picker (section 7); after the charter it offers optional team rules and a team folder, then requires a Mission / brief for every member | `herdr-synapse create <team> --member <pane\|name>[:<role>[:<name>]] … --brief NAME\|ROLE=MISSION … [--charter "…"\|--charter-file p] [--ref p] [--names plain] [--rename] [--reuse] [--use]`; an explicit non-empty Mission in `--instructions FINAL-NAME=TEXT` may supply and derive the brief instead |
| Create from every agent in a Space | picker: `w` then `a` | `create <team> --from-workspace <ws-id>`: waits up to 60 s for agents still launching and warns about the rest |
| Add agents to an existing team | picker: select the agents, Enter; when teams exist a numbered choice follows (`1  add it to team <t>  (N members)`, last number `create a new team`; type the number or move with the arrows); adding skips the charter stage, asks role, name, and required Mission / brief per agent, and confirms with `Add N agents to team <t>?`; every other member is nudged with a `member_joined` record and the newcomer is briefed. A kind that is not trusted yet (`kinds list`) is flagged on the confirm screen and by `add` (`kind_trusted: false`, a warning): nothing is typed into it until `herdr-synapse kinds trust <kind>` | `herdr-synapse add <team> <pane\|name> [--role <r>] [--as <name>] --brief "<Mission>"`, one per agent; each new member is briefed once idle |
| Create from scratch | | `create <team> --new [--workspace ID] --spawn <role>:<kind>[:<cwd>] … --brief <role\|name>=<Mission> … [--model <role\|kind>=<model>[@<effort>]] …` lays out the panes and starts the agents with their model and effort flags (section 5e) |
| Add a member later | | `add <team> <pane\|name> [--role r] [--as name] --brief MISSION [--rename] [--steal] [--model <setting>]` |
| Link two teams through their managers | picker: `c` on a team row (Enter links or breaks; a team with no manager is refused with the reason); console `/link`, `/unlink`, `/links` | `link <team> <other> [--note]`, `unlink`, `links [--all]` (section 5f) |
| Clear a board | console `/wipe [--purge] [reason]` (y/n first) | `wipe [--yes] [--purge] [--reason]`: every post moves to `archive/` (seqs and cursors kept; `board --since 1` reads it), or is deleted for good with `--purge`; operator only |
| Post to a linked team | console `/team <other> text`, `@team:` in the mention menu | `post --to team:<other>` (the manager, the operator, or a delegate) |
| Model and effort per member | picker: a fourth prompt per agent (after role, name, brief) when creating or adding, and member action `9` afterwards; console `/model <name> <setting> [--restart]` | `models set <kind> <setting>` (team default), `model <member> <setting> [--apply live\|next\|restart] [--self]`; `who`/`me` show it (section 5e) |
| See who is on which team | `prefix+t`: teams with their agents underneath, then the agents in no team grouped by Herdr tab label and stable tab ID; every candidate keeps its pane ID visible; `g` verifies and focuses the highlighted agent's pane; Enter folds a team or an unselected tab, `↑↓`/PgUp/PgDn move, the list scrolls | `who`, `teams` |
| Manage one member | `prefix+t`, Enter on a member: a numbered menu with rename, change its goal, send the goal now, remove it (with or without keeping its Herdr agent name), and go to its pane. Rename and goal are pre-filled and validated before anything is written; remove asks `y` (Enter is deliberately not yes). Rename and remove require operator authority. A member whose agent is missing or unsettled refuses rename, send and focus, because its pane is stale | `rename`, `brief <name> --set`, `brief <name>`, `remove`, `focus` |
| Remove, leave | `prefix+t` → Enter on the member → 4, or console `/remove name` (asks y/n) | `remove <team> <name> [--keep-name]` requires the operator or a delegate (clears tokens and label, clears the Herdr name unless `--keep-name`, keeps a tombstone); `leave` remains available from the member's own pane |
| Re-attach a missing member | | `bind <team> <name> <target>`: refuses a kind mismatch unless the member is `kind_changed`, refuses a terminal another team claims, bumps the generation, re-applies name, label, tokens, clears the stale label on the old pane, records the target's harness session, posts `member_restarted` |
| Let an agent build and run teams | | `operator grant <name> [--ttl 2h]` (yours alone; a delegate cannot pass it on): that member may then write the charter, the rules, any member's instructions and the project folder. Every use is audited as `operator_action`, the grant is announced on the board, `who` tags the member, and `doctor` warns while it is live. `operator revoke <name>` ends it |
| Reopen a member's own conversation | `prefix+t`, Enter on the member, 7: shows the command | `resume <name>` from a shell pane (human only): runs the command Herdr's own restore would use for the session the roster recorded, in the member's directory, replacing the shell; the notifier rebinds the member to that pane by the session. Covers all 17 sources Herdr ships an integration for (`docs/cli.md` section 4), pi and omp by absolute path rather than id. `--print` only shows it. Never `--continue`: that picks by directory or recency and can bring back another member's conversation |
| Dissolve | | `dissolve <team> --yes` (mandatory flag; human only; archives the team, clears tokens and labels, keeps the agents' Herdr names) |
| Several teams | console `/use team` | `use <team>` sets the default team for human posts; `teams` lists teams and whether their session runs (works offline); `create --use` makes a second team the default at creation |
| A team per Herdr space | `prefix+u` and every other action open the team of the space they were pressed in | inference order: `--team`, `HERDR_TEAM_DIR`/`HERDR_TEAM`, the caller's roster row, the team of this space (exactly one team with an active agent there), `default_team`, the only team |

Rules you will see enforced: an agent belongs to one team at a time
(`member_claimed` unless `--steal`); team names match `[a-z][a-z0-9_-]{0,31}` and roles `{0,63}`;
roles `[a-z][a-z0-9_-]{0,63}`. A member name is its Herdr agent name, which Herdr caps at 32, so `<team>-<role>` is fitted: the role's leading segments go first (kind labels and `dev`), then whole trailing segments of the team, so `red-dev` + `opencode-dev-brainstormer` gives `red-dev-brainstormer` and `clickhouse-vulnerability-hunt` + `manager` gives `clickhouse-vulnerability-manager`. The role always survives, so two members never derive the same name. Every name is validated before anything is
renamed or written, so a failed create leaves nothing behind. Under the
default naming a role may equal a kind label (`t-claude`); with `--names
plain` the default role becomes `agent`, `agent2`, … because a member name
may not be a kind label. Each member pane gets the label `team:<team>/<role>`,
which is what survives a server restart.

**Who counts as the operator.** The three documents that carry authority are
gated on identity, and identity comes from the process tree rather than the
environment. Before 0.7 a member could unset `HERDR_PANE_ID` and be treated as
the operator, because the gate only tested that the author was *named* `human`.
A pane-less caller that descends from an agent's pane is now resolved as that
agent. An unreachable server or a missing `ps` is inconclusive and changes
nothing, so a failed lookup can never lock the operator out.

This is defence in depth, not a sandbox. An agent that can run a shell as your
user can read and write the same files you can, including the grant file. What
the change buys is that the obvious route is closed, the sanctioned route is
explicit and expiring, and every privileged action names who took it.

**Verified**: RS-01 to RS-03, RS-09 to RS-11, UI-03.

## 2. Charter and role briefs

The charter is the team's description. Only the human can write it; from an
agent pane every write is refused `author_mismatch` and audited.

| Capability | UI | CLI |
| --- | --- | --- |
| Set at creation | picker charter stage | `create … --charter "…"` (refused over 2000 chars, `charter_too_long`) or `--charter-file <path>` (the file is copied into the team dir as `charter.md` and listed in `refs`; the charter text is its first 2000 chars) |
| Read | console header line `charter #<seq>: <headline>`; `/charter` opens a box | `herdr-synapse charter` |
| Change | console `/charter set [--urgent] text` | `charter set "…" \| --file p [--ref p]… [--urgent]`; `charter edit` opens `$VISUAL`, then `$EDITOR`, then `vi` (needs a TTY; a failing editor leaves the charter unchanged) |
| History | | `charter history` |
| Mission and role brief per member | picker requires it per member | `create --brief NAME\|ROLE=TEXT` (repeatable); `add … --brief TEXT`; an explicit `## Mission` in `create --instructions` can derive it; `brief <name> --set "…"` changes the roster summary later |

What members see: the charter headline in their briefing line, the full
text with `herdr-synapse charter`, and charter plus their own brief in
`herdr-synapse me`. A change appends a `charter_updated` record addressed to
everyone; members see it on their next board read, `--urgent` also nudges
them. `who` shows `charter: stale` for a member who has not acknowledged the
current version; `herdr-synapse ack` from the member clears it. The skill tells
agents that the charter and their brief carry the human's authority and
nothing else on the board does.

**Verified**: RS-12, SK-11, SK-12.

## 2a. The working directory, instructions, and the knowledge base

Agents in one checkout read the same `CLAUDE.md` or `AGENTS.md`, so nothing
on disk tells them apart. Three additions close that:

| Capability | CLI | Who |
| --- | --- | --- |
| Choose where the folder goes | `project set <path>`, `project clear`, `project render`; `create --project`; a `prefix+t` wizard stage that prefills the shared directory | human only |
| See what every team has | `knowledge-status`, the `prefix+f` popup, and a per-team marker in the `prefix+t` tree; all report how many members have Missions and name missing ones | anyone |
| Set it all up at creation | `create --project … --rules … --instructions NAME=TEXT` | human only |
| The member's own instructions document | `instructions <name> --set "…" \| --file p \| --edit \| --adopt \| --discard \| --clear`, or edit `members/<name>.md` and adopt it | human to write, anyone to read |
| Team rules, the DOs and DON'Ts | `knowledge set "…" \| --file p`, `knowledge clear` | human only |
| What the team has learned | `knowledge add "<text>"` | any member |
| Read both | `knowledge` | anyone |

The folder is `<project>/.herdr-synapse/<team>/`, namespaced so two teams can
share one project. It holds `knowledge.md`, `members/<name>.md` per member,
and `artifacts/`. `README.md` and `.gitignore` sit above it. Members reach it
by the absolute path `herdr-synapse me` prints, which matters because members of
one team routinely sit in different checkouts.

Three properties make it safe to put in a repository agents can write to:

- **Document trust is configurable.** Agent context reads stored state. In manual mode, member edits require `instructions <name> --adopt`. In auto mode, settled member edits and the Rules section of `knowledge.md` are imported as `file-sync` changes and affected agents are notified. Auto mode trusts all project-document writers; it is the default for new teams, while existing teams keep manual mode until an operator runs `project sync auto`. Generated findings never become instructions, and private Notes never enter agent context.
- **Consent is explicit.** `config.project_dir` is empty until a human runs
  `project set`. Nothing is inferred from member cwds, so the plugin cannot
  write into the wrong repository or into two of them.
- **Nothing is deleted.** Removing a member writes a tombstone over its file;
  renaming one writes a forwarding note under the old name. A file without
  the plugin's marker on its first line is never overwritten without
  `--force`, the rule `skill install` already used.

**How a change reaches an agent.** Writing a file is not telling anyone, so
every change becomes a board record and rides the delivery model that already
exists:

| Change | Record | Who learns, and when |
| --- | --- | --- |
| `knowledge set` | `knowledge_updated` | everyone, next board read |
| `instructions --set/--edit/--adopt` | `instructions_updated` | the member itself, nudged when idle; everyone else on their next board read |
| editing `members/<name>.md` | auto: `instructions_updated`; manual: `instructions_edited` | auto: affected member when safe; manual: you, to adopt it |
| editing Rules in `knowledge.md` (auto mode) | `knowledge_updated` | every active agent when safe; Findings edits are reported as conflicts |
| `project set` | `project_set` | everyone, next board read |
| `knowledge add` | `knowledge_finding` | everyone, next board read |
| a file in `artifacts/` | `artifacts_changed` | everyone, next board read |

For Claude with hooks that means the next *prompt*, through the prompt-submit
hook, not the next session. For every other kind it means its next
`herdr-synapse board --new`, which the skill tells it to run every turn. These
are `system` records addressed to `all`, so they deliberately do **not** nudge:
a rules edit cannot interrupt four agents mid-turn. `--urgent` is the opt-in
that does wake everyone, exactly as on the charter.

The `artifacts/` watch runs on the notifier's tick. It fingerprints the tree
and posts one record naming what was added, changed, or removed, whoever made
the change: a member, the operator by hand, or any other tool. Records are
summarised by directory and capped at 220 characters, and are posted only once
the tree has settled, so a data dump is one short line rather than a stream of
long ones. The first scan after a daemon start only seeds the fingerprint.
Deep or wide subtrees collapse to one entry, which also means a big drop can
never evict a real artifact from the fingerprint and make it look deleted.

Rules carry operator authority and are injected into Claude members' context
with the charter. Findings do not: they are attributed, escaped so one can
never open a fence or forge a role prefix, and pointed at rather than
inlined, so a peer's note can never reach another member as an instruction.

**How an instructions change reaches its member.** A Claude member with hooks
gets the document spliced into its next turn, and only while it has not
acknowledged that revision, so it costs context once rather than every turn.
Every other kind gets the ordinary nudge from the record, which names the
member, plus the skill's standing rule to run `herdr-synapse instructions` when
the board says they changed. `who` shows `instructions: stale` until the
member runs `ack`, which now records the charter, the instructions, and the
rules together. The document is versioned per member (`instructions_seq`) and
the rules per team (`rules_seq`), the same monotonic pattern the charter uses.

### The command menu

Typing `/` at the start of a line opens a menu of every console command with
its placeholder and a one-line description, filtered as you type. Arrows move,
Tab completes the command (never the placeholder, which you would have to
delete), Esc hides it. Enter is deliberately not consumed by the menu: it runs
the line, so a fully typed command does not need a second press. The compose
popup shows only the post directives it parses.

### Saving the board

`herdr-synapse export` writes the whole board, archived segments included, to a
file: markdown by default, or `json`, `jsonl` and `text`. The markdown form is
a standalone document carrying the charter and roster the posts refer to, so
it still reads correctly long after the session is gone; `jsonl` is the raw
record shape, so an export goes back into any tool that reads a board file.
`/export` does the same from the console. An existing file is refused without
`--force`, symlinked targets are refused, and the file is written `0600`.

## 3. Names

- Every member has a unique name, enforced by Herdr's own `agent.rename`.
  Grammar `[a-z][a-z0-9_-]{0,31}`; default `<team>-<role>`; the picker and
  `--member …:<role>:<name>` let you choose. Refused as names: `human`,
  `all`, `me`, `system`, `team`, `none`, every kind label and kind alias
  (`claude`, `claude-code`, `cursor-agent`, …).
- Names are how everyone addresses each other: `--to <name>`, `@name` in the
  console, `brief`, `focus`, `mute`, `nudge`, `read`, and Herdr's own
  `herdr agent prompt`.
- Renaming: `herdr-synapse rename <old> <new>`, or Herdr's `herdr agent rename`,
  which the daemon adopts on its next 2 s scan: the roster updates, a
  `renamed` record goes to the board, and the old name keeps resolving for
  ten minutes. `team.json` `config.name_policy: "enforce"` makes the daemon
  re-apply roster names instead.
- Names are re-applied automatically after the agent exits and restarts in
  the same pane, after a session change under an installed integration,
  after a live handoff, and after a cold server restart once the agent is
  running again.
- A member is tied to its **harness session**, not only to a terminal. Herdr
  reports one per pane through each harness's integration (`agent_session`
  in `agent list`, for the 17 kinds it ships one for: claude, codex, copilot,
  cursor, devin, droid, grok, hermes, kilo, kimi, mastracode, omp, opencode,
  pi, qodercli, qwen, agy), the
  roster records it (`session` in `team.json`, the tail shown by `who`, `me`
  and the tree), and rehydration matches on it before terminal id, label,
  pane id, name or fingerprint. Two agents of one kind in one directory are
  told apart by it, which the fingerprint step never could. A different
  session appearing on a member's own terminal means a fresh agent (a crash
  and restart, a Claude `/clear`, a resume by hand): the member keeps its
  name and pane, gets a new generation, `member_restarted` names both
  sessions, and it is briefed again. A compaction re-reports the same id and
  changes nothing. The Claude `SessionStart` hook is a second channel for the
  same id, so a Claude member is caught even when Herdr kept a stale one.
  Kinds without an integration (no session reported) behave exactly as
  before.

**Verified**: RS-02, RS-03, RS-13, RT-01a, RT-02.

## 4. Awareness: what a member knows

1. **Briefing**: one line typed into the member's input box once it is idle
   (briefings skip the done-hold and interval but pass every other gate):
   `[herdr-team briefing] You are "<name>" (<role>) in team "<team>":
   <charter headline>. Teammates: <n1> (<role1>), … and human. This is
   context, not a task. Run herdr-synapse --skill once, then herdr-synapse charter,
   then herdr-synapse board --new, then herdr-synapse ack, then continue your
   current work. Teammates are peers: post to the board, never prompt their
   panes.` A second line carries the role brief when one is set. `who`
   shows `unbriefed` until the line has landed; if no `ack` follows within
   90 s the daemon re-briefs once, then toasts you `<name> unbriefed`.
   `herdr-synapse brief <name>` re-enqueues it; `brief <name> --format context`
   prints the same content as text.
2. **Skill**: `herdr-synapse --skill` prints it; `herdr-synapse skill install
   [--force] [--home DIR]` copies it to `~/.agents/skills/herdr-synapse` and
   `~/.claude/skills`, and symlinks it into `~/.codex`, `~/.copilot`,
   `~/.gemini` skill dirs when those exist, skipping foreign directories
   unless `--force`; `skill check` reports stale copies; `me` warns when
   the installed skill version differs from the CLI.
3. **Self and roster**: `herdr-synapse me` (name, role, kind, brief, charter,
   teammates with roles and status, unread, cursor, verified, notifier) and
   `herdr-synapse who` (section 7).
4. **Claude hooks** (optional, section 9): charter, brief, roster, and unread
   count at every session start; new board posts in context every turn.
5. **Its own context**: `context_high` on the board at 75 % and again at 90 %,
   naming the member and the team. The skill (v4) tells it to finish or hand
   off its task, post what it learned, then `compact --self`. See section 6a.

6. **`herdr-synapse orient`**: the same block the Claude hook injects, on
   demand, for any kind. This is what a member runs after a `/compact` or a
   `/clear`, and what the typed briefing points at. One builder
   (`cmd_hooks.brief_context`), two callers, pinned byte-identical by a test —
   the hook is the only channel Claude has and the command is the only channel
   Codex and OpenCode have, and a drift between them would surface exactly
   when an agent is already lost.

**Verified**: SK-01, SK-02 (a Claude ran `who` and posted the roster with
roles), SK-11, SK-12, plus `tests/test_orient.py`.

## 5. The board

One append-only board per team.

### Posting

```
herdr-synapse post "<text>" [--to <name>[,<name>] | all | human | me | role:<r>] [--kind note|request|handoff|done|blocked|question|answer]
                [--reply-to <seq>] [--ref <path>]… [--attach <path>]… [--urgent] [--spill] [--relayed-for human] [--name <label>] [--to-any] [--force]
```

- **No `--to` goes to the whole team**, whoever posts. `--to <name>`
  addresses one member and is what nudges it. `--to human` reaches you;
  `--to me` is yourself. `--to role:<r>` expands to every holder of the
  role and records `to_role`. A typo is `recipient_unknown` with the roster
  in the error (and a hint when the word is a kind label) unless `--to-any`.
- Text is sanitized (control characters, escape sequences, bidi and
  zero-width characters removed) and capped at 2000 characters
  (`text_too_long`); with `--spill` the post keeps the first 500 characters,
  is flagged `truncated`, and the full body goes to `payloads/<seq>-body.md`.
  Refused with exit 4 `echo_rejected` when the text starts with
  `[herdr-team`, contains `[n<digits>]`, or contains a nudge, briefing, or
  probe marker anywhere. Secret patterns are refused unless `--force`.
- Long content goes in files: `--ref` names an existing file under the team
  dir, `payloads/`, or a member's cwd; `--attach` copies a file into
  `payloads/` (16 MiB cap, safe basename).
- `--reply-to <seq>` threads (`reply_to_unknown` for a missing seq);
  `retract <seq>` and `edit <seq> "…"` append records rather than rewrite;
  `task "<text>"` (500 chars) publishes your current headline;
  `ack` marks the board and charter read.
- Posting works with the Herdr server down (author then unverified).
  `post` never sends a toast itself.

### Reading

```
herdr-synapse board [--new | --peek] [--to me] [--from <name>] [--kind <k>] [--thread <seq>] [--since <seq>] [--last N]
                 [--receipts] [--format text|json|context] [--limit N] [--max N] [--max-bytes N] [--ascii] [--name LABEL]
herdr-synapse show <seq> [--cat] [--force]      # one post; --cat inlines refs under payloads/ or a member cwd (64 KiB each)
herdr-synapse inbox --human [--last N] [--since <seq>]   # posts to you plus the notifier's attention file
```

With no mode `board` prints the last 30 without touching your cursor.
`--new` shows posts addressed to you or to all since your cursor and
advances it only past the posts actually printed; `--peek` never advances.
`--thread` shows a post with its replies, retractions, and supersedes;
`--kind` includes `retract` and `system`. Every post renders as a header
(`#seq from (kind, pane) -> to · kind · time · re #N`) with every text line
inside a blockquote, so a post can never imitate a header or a nudge.
Unparseable, torn, or duplicate lines are counted (`skipped`, with a stderr
warning) and dropped. A well-formed record whose origin cannot be trusted
(a raw append claiming `human` or `system`, a shell path whose ancestry
could not be proven) is rendered with `(unverified)` in its header and never
counts for nudges. For a kind recorded as unable to read the state dir
(`payload_readable: false` in `kinds.json`), `board --new` copies referenced
payloads into the member's `<cwd>/.herdr-synapse/`.

### Receipts and audit

`board --receipts` shows `✓nudged HH:MM:SS` (from the daemon's `nudged`
record) and `✓read by <name>` (from cursors); a post to all shows `read by
k/n`. Retracted posts render struck through with `(retracted by #M)`.
`herdr-synapse audit [--last N]` lists refused attempts: `author_mismatch`
(an agent tried `--as human`), `pane_mismatch` (a forged pane id).

### System records you will see on the board

`manager_changed` (to every member, `human` and `all`, urgent),
`nudged` (to the member), `toast` (to human, one per toast attempt),
`retracted`, `expired` and `abandoned` (to human), `member_gone` (to all on
remove or leave; to human when a member goes missing), `member_restarted`,
`renamed`, `charter_updated` (to all), `rotated`, `reset_detected`,
`context_high`, `context_compacted` and `context_cleared` (to the member and
to all).

**Verified**: HP-01 to HP-15, S-01 to S-08, RS-12.

## 6. Delivery: how a member learns a post is for it

The notifier daemon is the only process that ever types into a member. It
tails every board and, for each post addressed to a member, types one line
once that member is safe to interrupt:

```
[herdr-team nudge] 2 new board posts for reviewer (seq 41-42). Run: herdr-synapse board --new [n17]
```

Posts arriving within 1 s of each other (`burst_window_ms`) become one
nudge covering the range. A broadcast to `all` from the human nudges every member (normal holds apply); an agent's broadcast is not nudged directly, but an idle member holding it is swept into one within three minutes (see the safety section); `--urgent` nudges immediately.
`--urgent` on a post nudges every recipient. Posts to `human` become toasts.

### The gate, in order (defaults; per-team overrides in `team.json` `config.gate`)

1. Cursor already past the post → `read_before_nudge`, dropped.
2. Not the sender, not the console, not the human; not muted.
3. Member present in `agent list` with the roster's terminal, name, and kind
   (`absent`, `kind_mismatch`, `name_mismatch`, `launch_pending`).
4. Kind trusted (`kind_unverified` otherwise): `herdr-synapse kinds trust
   <kind>`, one passed `hooks probe`, or 20 clean round trips in the ledger.
   A fresh session holds everything until this is true.
5. Status idle or done (`not_idle`).
6. Stable for 2 s (`unstable`); 4 s when the idle is weak, meaning detection
   matched no rule or only the title (`weak_idle`); 750 ms for kinds with a
   hook-backed integration; 15 s for members on Claude-hooks delivery. Then a
   60 s hold after going idle (`done_hold`), skipped by `--urgent`,
   `nudge --force`, briefings, and probes.
7. `agent explain`: not `blocked`, no `visible_blocker`, no
   `skip_state_update` (menus and viewers leave the old state published).
8. Detection text: no permission or question dialog, model picker, or
   transcript viewer visible (`dialog`, with the matched marker logged).
9. Prompt line empty (`draft_present`); Claude's ghost-text suggestion and
   Codex's placeholder do not count as drafts.
10. Pane not focused by you (`focused`, with `nudge_focused = never`); after
    5 min a focused pane is nudged once its screen has been still for 3 s;
    `nudge_focused = always` disables the hold.
11. Rate limits: one in flight per member (`in_flight`), 20 s between nudges
    to the same member (`interval`; one immediate follow-up allowed when the
    member read the board but posts remain), 1.5 s globally
    (`global_interval`), a ping-pong budget of 10 exchanges per 10 min
    between two members (`pair_budget`), and a 10 min `stop_blocked` hold
    after a Claude Stop hook already blocked on the same posts.

After typing, the daemon classifies the outcome: landed in a fresh turn,
landed inside a turn the agent had already started, not submitted, refused,
hung. Unread after landing → re-nudge 2 min after that landing, then 5 min
after the next, then 10 min after the next, each only after the member has
completed a turn in between; then `abandoned` with a toast. An unread post
expires after 30 min of the member being present (`post_ttl_ms`; the clock
pauses while it is missing and restarts after a daemon restart) → `expired`
with a toast. Expiry and abandonment are terminal for automatic delivery:
the member's cursor stays unread, but a durable ledger tombstone prevents the
three-minute sweep from recreating the same pending work, including after a
daemon restart. `nudge --force` is the deliberate retry. Long holds (dialog,
focused, draft, blocked) toast you after 10 min; `kind_unverified`,
`kind_mismatch`, and `pair_budget` toast once an hour.

### Where to read why something did not land

Holds are not in the ledger. They are in
`<session>/daemon.log` as `<team>: <member> held: <reason> (<detail>)` and
in `who --json` under `members[].hold`. `herdr-synapse notifier stats` reports
what was actually sent: intents, results, per-kind clean-landing rate,
`open_intents`, and `wrong_target` (must be 0).

### Controls

| Command | Effect |
| --- | --- |
| `herdr-synapse nudge <name> [--force]` | evaluate now; builds pending work from unread human/member-authored messages (to it or to all) if none is pending; system and delivery history is never converted to mail; terminal automatic deliveries are skipped unless `--force`, which also marks the work urgent and skips the done-hold and interval, never the dialog, draft, or focus checks |
| `herdr-synapse mute <name> \| --all [--for 10m\|2h\|1d\|N]`, `unmute <name> \| --all`, `pause` | silence nudges (gate 2) and the Claude Stop hook; posts still land and **toasts are not muted** |
| `herdr-synapse focus <name>` | focus the member's pane through the daemon |
| `herdr-synapse say <name\|all> "<text>" [--force]` (console: `!name text`, `!!name text`, `!!all text`) | type one line into one member, or with forced `all` into every current agent, with operator authority; each target is a separate `direct` board record and job; plain `!` atomically requires the inspected terminal to remain idle through submission, while `--force`/`!!` deliberately permits a running turn; dialogs, permission prompts, overlays, and drafts still refuse; every target gets a `typed` outcome and feed tag (`✓typed`, `✗ not typed (working)`, …); human only, from the focused console only |
| `herdr-synapse post --to <name> --interrupt "<text>"` (console: `/interrupt @name text`) | urgent, and when the recipient's kind is in `config.gate.interrupt_kinds` (default `claude`, `codex`, `opencode`, `pi`) and the sender is out of its cooldown for that teammate (10 min), the daemon types the nudge into the recipient's *running turn* instead of waiting for idle: `[herdr-team interrupt] <sender> could not wait: 1 urgent board post for <name> (seq N). Run: herdr-synapse board --new [nK]`. Dialog, overlay, draft, focus, and rate-limit gates still hold it; otherwise it is an ordinary urgent nudge. The feed shows `⚡INTERRUPT` on the post and `⚡interrupted` once typed; `who` shows `⚡armed`, `⚡cooldown`, or `⚡kind_not_allowed` next to `↪N`. Named recipients only; a member's repeat inside the cooldown is `interrupt_cooldown` at the CLI; the human has no cooldown |
| `herdr-synapse interrupts [show\|off\|on\|<kind>,<kind>] [--cooldown 10m]` (console: `/interrupts …`) | show or set the team's interrupt kinds and cooldown (`config.gate`); changing them is human only |
| `herdr-synapse read <name>` | the member's visible screen; `--lines` is refused for every member because scrolling an alternate screen types into it |
| `herdr-synapse notifier stats [--team] [--kind]` | the delivery ledger |
| `herdr-synapse kinds list \| trust <kind> [--reason "…"] \| untrust <kind>` | the trust override behind gate 4; `list` prints `<kind>  delivers\|held  <flags>`; a kind also becomes `verified` on its own after 20 clean round trips |
| `team.json` → `config.gate` | `stable_ms_screen` 2000, `stable_ms_hook` 750, `stable_ms_hooks_delivery` 15000, `done_hold_ms` 60000, `min_interval_ms` 20000, `global_interval_ms` 1500, `focus_max_hold_ms` 300000, `focus_snapshot_stable_ms` 3000, `dialog_hold_cap_ms` 600000, `pair_budget` 10, `pair_window_ms` 600000, `sample_gap_reset_ms` 10000, `post_ttl_ms` 1800000, `burst_window_ms` 1000, `nudge_focused` `never\|always`, `interrupt_kinds` `["claude","codex","opencode","pi"]`, `interrupt_cooldown_ms` 600000; the daemon reloads it within 2 s and ignores the whole block if any key is invalid |
| `daemon start --dry-nudge` | log nudges instead of typing them (for a dry run) |

Measured in the rig with `done_hold_ms 5000`: post to the member working in
about 2 s when idle; a nudge lands about 6 s after a dialog closes; the
member's read shows about 3 s after that. With the default 60 s done-hold,
add up to a minute after the member goes idle.

**Verified**: ND-01 to ND-12 (ND-05 and ND-06 by unit tests), ND-02b (model
picker and permission dialog received no bytes), ND-04 (a real nudge
produced a reply on the board), F-01 to F-04, SK-08.

## 5b. The team manager

One optional member per team, `Member.manager`, set by `herdr-synapse manager
<name>` or `create --manager`, and toggled by action 8 in the team view. At most
one holder, enforced by the setter: the write that sets one clears every other.
A boolean on the member rather than a name in team config, so a rename carries
it and a removal drops it with no stale name to clean up.

It confers no authority. Every human-only gate is unchanged, and `--operator`
is the one way to add the operator's writing powers, through the existing grant
with its expiry and audit. Appointing is `_human_only` (a delegate may);
granting is `_strictly_human` (a delegate may not).

What it changes:

| Surface | What it shows |
| --- | --- |
| Board | one urgent `manager_changed` naming every member, `human` and `all` |
| `who` | a `manager` tag beside `acts as operator` |
| `me` | the teammate is marked; the manager is told it is one |
| Session-start (Claude) | `name (role, kind, team manager)` in the teammate list |
| Typed briefing | `name (role, manager)` in the roster, dropped first if the 400-char budget bites |
| Skill v5 | take its assignments as the plan; disagree on the board; it is not the operator |

**Delivery.** A post addressed to the whole team is held by gate 2
(`HOLD_BROADCAST`) unless urgent, so an ordinary agent's broadcast waits for
each member's next board read — a 42-minute median on a live team. The
manager's broadcast is not held. `PendingWork.from_manager` lifts that one hold
and nothing else; it is deliberately not routed through `urgent`, which also
bypasses `done_hold` and the per-member interval.

**A system record does not deliver itself.** `_ingest_record` returns early for
`from: system`, so naming recipients on one achieves nothing on its own. Two
lists make it move: `URGENT_SYSTEM_EVENTS` (with `urgent: true` on the record)
fans it out to the members, and `TOAST_SYSTEM_EVENTS` puts it in the operator's
toast queue. Both were missing on the first cut, and the result was a record
that existed on the board and reached nobody.

**Verified**: `tests/test_manager.py` (model round-trip, the single-holder
invariant, the authority split, the record's addressing and urgency, both
delivery halves, the gate lifting only the broadcast hold, the briefing budget,
`who`/`me`/hooks/picker). Live on `clickhouse-hunt`.

## 5c. Coming back from a compaction

What a member gets back depends on its kind, and that asymmetry is the whole
reason `orient` exists:

| Kind | On a clear | On a compaction |
| --- | --- | --- |
| Claude | new session id → generation bump, `briefed_at` cleared, `member_restarted`, a briefing job; **and** the SessionStart hook re-injects the full block | same session id with `source=compact` → `briefed_at` cleared and a briefing job (no generation bump, it is not a restart); the hook fires again |
| Codex, OpenCode | new session id → the same roster handling, but the typed briefing is the **only** channel: no hooks exist for these kinds | no session change at all. The only sign is the token count falling past `CONTROL_DROP_RATIO`, seen by `_check_context_drop` — which queues the briefing whether or not anyone asked for the compaction (0.13.0) |

Until 0.11.0 that last cell did nothing but append a record: a compacted Codex
or OpenCode member was **never re-briefed**, so the two kinds with no hooks —
the ones for which the typed line is the only channel there is — were exactly
the ones that got nothing back. `_note_compacted` now clears `briefed_at` and
enqueues a briefing whatever the kind; the Claude path reaches the same
function with `briefed_at` already cleared, so it is a no-op there.

Until 0.13.0 that held only for a *requested* compaction: the harness compacting
on its own at 90 % — the case the warnings exist to pre-empt — wrote the record
and stopped. `context_high` is likewise delivered now: the system-event table
(`SYSTEM_EVENT_DELIVERY`) has a `named` mode that gives each member the record
names an ordinary, fully gated nudge.

`rt.rebriefed`, the once-only allowance for a briefing nobody acknowledged, is
now reset when a briefing lands. Nothing ever put it back, so the second clear
or compaction of a member's life got one attempt and then gave up for as long
as the notifier lived.

## 5d. Human in the loop

An agent addressing the operator is the one thing on the board that needs a
person, and the schema never said so: `kind: "answer"` exists in the enum and
no code branched on it, and there is no `answered` or `resolved` field. So
pending-ness is derived, in `herdr_team/asks.py`, and that is the only place
that decides it.

**An ask** is a post from a member whose recipients include `human`. It stops
being pending when the **operator** replies to it, when it is retracted, or
when the operator dismisses it. A reply from a peer does not clear it — that is
the defect this exists for: on the team this was built against, six of ten asks
were never answered and the other four were answered by other agents, one of
them overriding a genuine pre-submission halt.

| Surface | |
| --- | --- |
| `asks` | what is waiting on you; `--dismiss <seq>` stops the popup reopening |
| the `asks` popup | one popup for the whole queue, opened by the notifier |
| `ask-policy` | per-team `config.ask`; `/ask-policy` in the console |
| `post --wait` | the agent blocks until the operator answers; a stderr heartbeat every 30 s |

**One popup, not one per post.** Herdr allows exactly one popup at a time
(`ui_busy` otherwise) and a popup has no pane id to address, so the popup is
the queue. The daemon opens it from a new `asks` tick phase; `ui_busy` is an
ordinary answer there, not an error, since the slot is shared with compose, the
picker and whatever the operator opened. `popup.close` is never called: it
closes whatever the operator has open.

**The wait** is `post --wait`, which works for every kind because every agent
is just waiting on a shell command. Two details it gets right that `say --wait`
does not: it polls a non-persisting `BoardTailer` rather than
`BoardStore.read`, which re-parses up to 4 MB per call; and it appends and
releases the team lock *before* waiting, so a member waiting for minutes never
fails anyone else's post with `board_locked`. Capped at `MAX_WAIT_S` (9 min)
because Claude Code kills a shell command at ten and a longer wait would end as
a killed process with no error the agent could read. Timeout is exit
**`EXIT_NO_ANSWER` (6)**, code `wait_no_answer`, distinct from `EXIT_REFUSED`
so an agent can tell "nobody answered" from "rejected".

**What each harness does to a nine-minute shell command** — the wait is only
as good as the shell tool it runs in, and the cap was calibrated to Claude Code:

| Kind | Its shell tool | What skill v8 tells it |
| --- | --- | --- |
| Claude Code | kills the command at 10 min; output only at the end | give the tool a 10-minute timeout |
| Codex | runs it in an exec cell that **yields after 10 s with the process still running**; the model must keep calling `wait` | keep waiting on the exec cell until it exits |
| OpenCode | optional `timeout`, 2 min by default and 10 min max | pass a 10-minute timeout |
| Pi | its `bash` tool waits for completion; an explicitly short timeout can terminate the wait | allow enough time for the board wait; answer/unblock verified with a bounded live test |

The wait, operator answer and unblock round trip is live-verified for all four
rows. The stderr heartbeat remains useful for Codex: a silent wait looks hung
to a model watching a yielded cell.

**Not an ask:** a `question` sent `--to all`. It is for teammates, and it
neither pops up nor blocks; `--to human` is the operator channel.

**The policy** lives at `config.ask`, deliberately not under `config.gate`:
that namespace is whitelisted and, until 0.13.0, one unknown key there threw the
whole gate config back to defaults. It no longer does — an unknown key is skipped
and named in the daemon log — but the separation stays. Popup on anything addressed to the operator; blocking
only on `question`, `blocked`, `request`, because blocking a `done` notice for
eight minutes would freeze a team that posted forty of them in three days.

A waiting agent shows `working`, so gate 5 holds every nudge and the idle sweep
skips it. `--interrupt` still reaches it, which is correct.

**Verified**: `tests/test_asks.py` — pending derivation including the peer-reply
case, both wait outcomes, the lock staying free, the tailer being used, the
policy landing outside `config.gate`, the popup model, and the daemon opening
once and treating `ui_busy` as a retry.

## 5e. Model and effort (0.14.0)

Each member carries an optional model and reasoning effort, `<model>[@<effort>]`,
resolved member override → team default for the kind (`config.models`) →
harness default. The vocabulary is the harness's own and is passed through
untranslated. What each installed harness accepts (checked against the
binaries and then live-verified on 2026-09-11):

| Kind | At launch (and on `resume`) | Live | Effort words |
| --- | --- | --- | --- |
| Claude Code 2.1.268 | `--model <alias\|name>`, `--effort <e>`, `--dangerously-skip-permissions`; `claude --resume <id> …` | `/model <m>` and `/effort <e>` both take an argument: the notifier types them | `low medium high xhigh max` |
| Codex 0.153.4 | `-m <model>`, `-c model_reasoning_effort="<e>"`, `--dangerously-bypass-approvals-and-sandbox`; on `codex resume <id>` too | `/model` is a picker: no keystroke. `--apply restart` exits (`/quit`) and resumes with the flags | `minimal low medium high xhigh` |
| OpenCode 1.18.30 | `-m provider/model`, `--auto`; `--session <id>` too; effort is selected after startup | `/variants` accepts the effort selection live; a model change uses `/exit` and a controlled restart | provider-specific; any token |

Herdr's `agent.start` hands `args` to the binary verbatim
(`herdr agent start NAME --kind K --pane P -- ARG…`), so every flag above is
argv, never a shell string.

**YOLO by default, with an explicit native opt-out.** `permissions --default native` changes the team default; `permissions MEMBER native` overrides one member, and `inherit` removes that override. Creation accepts `--permissions` and repeated `--member-permissions NAME|ROLE=MODE`. In YOLO mode, Synapse-managed launches carry the flags in the table. Native mode adds no bypass flags and leaves native configuration in control; it does not promise sandboxing or approval prompts. Synapse rebuilds the saved policy on `resume`, restore, fresh swaps, controlled model restart and OpenCode clear/restart; a live agent added to a team keeps its current launch mode until one of those Synapse-managed starts. Conflicting approval, permission and sandbox selectors from a previous process are not carried into a restart, while unrelated allowlisted flags still are. This execution mode does not turn an agent into the Synapse operator: charter, roster and other privileged changes still pass the process-tree authority gate.

**Verified live:** Claude changed model and effort inside the running TUI;
Codex restarted its exact session with the requested model and effort; OpenCode
changed effort through `/variants`, then restarted the exact session for a
model change and re-applied the effort. Controlled restarts validate the exact
returning record and argv, preserve allowlisted non-permission flags, rebuild
the saved permission policy (YOLO by default), and disable the Codex startup update picker for that
restart.

**Changing it while the agent runs.** A change is recorded on the roster and
announced (`model_changed`, the member nudged). Claude: a control job types
the lines once the member is idle; the job closes when the transcript reports
the model (`model_applied`), or on typing when only the effort changed, since
the effort is not observable. OpenCode effort changes are selected live with
`/variants`. Codex changes and OpenCode model changes apply at the next resume
by default; `--apply restart` is a control job that types the exit command when idle,
waits for the pane to empty (30 s), starts the harness again with the resume
argv plus the flags (90 s), and closes when the session is reported again. A
resumed agent reports the *same* session id with a new phase — the exact shape
the notifier read as a compaction — so an open restart claims that event first,
and while one is open the member is never marked missing.

**Authority:** the operator or a delegate for anyone, the team manager for
anyone, a member for itself (`--self`). A control record needs a verified
origin (as `compact`), so a popup or an outside shell can record and
`--apply next` but not drive a keystroke.

**Seeing it:** `who` tags `model opus@medium` and, when the harness reports
something else, `runs claude-sonnet-5`; `model` with no arguments is the table
with sources; `me`, `orient`, and the session briefing name the member's own.

## 5f. Teams talking to teams (0.15.0)

A **link** joins two teams of the same session; their **managers** are the
endpoints. A message across it is an ordinary board record that lives on both
boards — the delivered copy on the receiving board, addressed to its manager
and signed `<team>/<manager>`, and a mirror on the sending board addressed to
`team:<other>` — so nudging, `board --new`, the Claude prompt hook and the
console all work unchanged. Replies thread across by message id; the receiving
notifier sends a `link_read` receipt back, shown as `read by <manager>`.

Who may speak: the team manager, the operator, a delegate. Who may read: as
always, anyone on either team. Linking and sending both need managers on both
ends; a manager cleared later pauses the link (`links` says which team to fix).
Dissolving a team breaks its links and tells the other side. Two Herdr servers
do not link.

In the teams view every manager is marked `★` in bold; team headers carry
`manager: <name>` and `⇄ <other>`; `c` opens the chooser. In the console
`/filter teams` is the inter-team lens and `/filter team` the local one.

## 6a. Context windows

The notifier reads how full each member is from the harness's own files every
`CONTEXT_POLL_S` (15 s), skipping any member whose file has not moved. Nothing
is typed and nothing is asked of the agent.

| Kind | Source | Tokens | Window |
| --- | --- | --- | --- |
| Claude | newest `message.usage` in the transcript the hook recorded, else the transcript named by the session id | exact, cache reads included | from the observed model's published limit; the configured model is a fallback when the record omits it |
| Codex | newest `token_count` in `~/.codex/sessions/**/rollout-*-<id>.jsonl` | exact, `last_token_usage` (not the cumulative total) | exact, `model_context_window` |
| OpenCode | newest assistant `message.data.tokens.total` in `opencode.db` | exact | `limit.context` for its `providerID` + `modelID` in OpenCode's cached model catalogue |
| Pi | private, identity-validated runtime snapshot from the Synapse Pi extension | estimated by Pi; nullable after compaction | active `getContextUsage().contextWindow` / `ctx.model.contextWindow`, including custom models and runtime switches |

Readers tail their files rather than parsing them (`TAIL_BYTES` 256 KB), open
SQLite read-only with WAL updates visible, and cache OpenCode's catalogue by
mtime, because these files reach tens of megabytes on a working machine. A
kind with no reader reports `unknown`. A readable token count whose model
window cannot be resolved keeps the count but reports a null window and
percentage; it never inherits 200k or raises a context warning from a guess.

| Surface | What it shows |
| --- | --- |
| `herdr-synapse context [<name>]` | a bar per member; the notifier's reading when it is up, the files directly when it is not |
| `who` | a `context 94%` tag, marked `!` at 75 % and `!!` at 90 % |
| Sidebar | `team_context`, `team_context_warn` or `team_context_crit` under source `herdr-synapse:context`, TTL 2 min; exactly one carries a value, which is how the gauge changes colour, since Herdr styles a token from a fixed `fg` and cannot colour by value |
| Board | one `context_high` per crossing, addressed to the member and to `all` |

### Compacting and clearing

| Command | Effect |
| --- | --- |
| `herdr-synapse compact <name> \| --self [--reason TEXT]` (console `/compact`) | enqueue a `control` job; the daemon types the kind's compact command once the member is idle. Operator, or a member on itself |
| `herdr-synapse clear <name> --yes [--reason TEXT]` (console `/clear`, which asks y/N) | the same for the clear command. Operator only; `--self` is refused |

Keystrokes: Claude `/compact` and `/clear`; Codex `/compact` and `/new`;
OpenCode `/compact`, while clear exits with `/exit` and starts a fresh full TUI
for the same member. Codex takes `/new` deliberately, since its `/clear` also
wipes the scrollback the detection layer reads. OpenCode clear is refused
before exit when `pane.layout` reports a width below 38 (about a 40-column PTY),
because OpenCode 1.18.30 crashes while starting its full TUI that narrow. Kinds other than Claude Code, Codex, OpenCode and Pi are `control_unsupported`.

Pi uses `/compact` and `/new`. Native success/failure events, not token-count drops, settle compaction; an event from an older request or different native session cannot complete a control. The runtime snapshot expires after 20 seconds without a report, and the notifier clears stale context on its next context poll. The extension emits at most one report per two seconds, normally one heartbeat per five seconds, never one subprocess per generated token. It reports no prompt text, editor content, credentials, or transcripts. Pi keeps normal typed delivery and still requires kind trust.

Properties that hold:

- The job is a pointer. The command appends a `direct` record carrying a
  `control` block and the job names its seq; the daemon re-reads that record
  and refuses a job whose record is missing, unverified, addressed elsewhere,
  or carrying no such block, exactly as `say` does.
- The keystroke is `pane.send_text` plus a separate `pane.send_keys ["enter"]`,
  never `agent.prompt`: a prompt is a bracketed paste, and a pasted `/compact`
  reaches a TUI agent as text to answer rather than a command to run.
  `pane.send_input` brackets its text too, so it is not the way through either.
- Delivery uses the ordinary gate. Only `done_hold` and the per-member
  interval are bypassed, the way a briefing does. Idle is required; blocked,
  dialog, overlay, draft, wrong occupant and pane-stuck all hold it.
- It is typed once. A landed control job stays open until its effect is seen,
  and `CONTROL_OBSERVE_S` (6 min) closes it with a note rather than a retry.
- The effect is a session phase change (Claude reports `source=compact` on the
  same id) or a fall to under `CONTROL_DROP_RATIO` (0.7) of the reading taken
  when the keystroke landed. Either appends `context_compacted` naming who
  asked. A clear is closed by the new session and appends `context_cleared`.
- A compaction nobody asked for is recorded too, since a teammate needs to
  know this member now works from a summary. A reading from a different
  session is a new baseline, not a fall.

**Verified**: unit tests in `tests/test_context.py` (readers, tailing,
thresholds, forged jobs, gate holds, typed-once, all completion paths). Live:
the four readings against the numbers the agents showed themselves, plus a
compact and clear of every harness with its context/session transition and
single re-brief observed.

## 7. Human paths

### Console pane

`prefix+u`, `herdr-synapse ui console`, or the `herdr-synapse.console` plugin
action. Opens as a split in the current tab; a second open focuses the
existing console instead of opening another. `herdr-synapse ui who` opens it as
a popup on the roster box.

- **Header**: team, members, view on/off, nudges on/paused with countdown,
  toast mode, your unread count; second line `charter #<seq>: <headline>`.
- **Roster rows**: glyph and name, kind, pane, status, "headline", `↪N
  (reason)` for queued nudges with why they are waiting (`focused` means you
  have that pane focused and the daemon will not type into it; `not_idle`,
  `done_hold`, `dialog`, `draft_present`, `muted`, `stop_blocked`, …; the
  full list is in section 6), `muted`, `gone <age>`, `unbriefed`,
  `charter: stale`, `hooks: silent`, `kind unverified` (no role column here;
  roles are in the CLI `who`). Narrow panes drop the kind, pane, and headline
  columns.
- **Feed**: the board tail with receipts, struck retractions, system
  records, and warnings such as a refused `--as human`. Long posts wrap
  over as many rows as they need: the header (`#seq time from → to kind`)
  starts the first row, continuation rows are indented, explicit newlines in
  the post are kept, receipts follow the last row. Scrolling moves by post;
  the topmost post may show only its last rows. Lines are color
  coded: each member gets a stable color by its position in the roster
  (cyan, green, magenta, yellow, blue, white, then wrapping), used for its
  roster row, its posts, and its row in the `@` list; your own posts are
  bold; system records are dim; warnings are red. Terminals without colors
  fall back to bold and dim only.
- **Input**: plain text → whole team; `@name text` → one member; `@role:r
  text` → a role; `/interrupt @name text` → an interrupt (section 6): urgent,
  and typed into that member's running turn when its kind allows it and the
  sender is out of cooldown; `/interrupts off|on|<kind>,… [--cooldown
  10m]` sets that policy. Typing `@` (at the start or after a space) opens a name
  list above the input line: every member with role, kind, and status, then
  `role:<r>` groups, `all`, and `human`; keep typing to filter (a role or
  part of a name matches), Up/Down move, Tab or Enter insert the pick, Esc
  hides the list. The compose popup has the same list. `!name text` types
  the line only if Herdr atomically confirms that the inspected terminal is
  still idle (a `direct` record; the member is not nudged and does not see it
  as mail); `!!name text` deliberately uses the unrestricted path and also
  types into a member that is working or muted; `!!all text` fans that path
  out as a separate recorded delivery for every current agent. Typing `!` at
  the start of the line opens the same list with members only; `!!` adds the
  `all` choice. Every line that starts
  with `!` is such an attempt and never becomes a post: a wrong name is an
  error, and text that should start with `!` is posted as `/all !text` or
  `@name !text`. The entry shows `… typing`, then `✓typed`, `✓typed (in
  running turn)`, `✗ not typed (working|dialog|draft|overlay open|…)`,
  `✗ blocked`, `✗ not submitted`, or `✗ no outcome (notifier?)`; the status
  line repeats the outcome (`(!!name text forces)` when `!!` would help) and
  the member's row reads `» typing` while the daemon confirms. Refused
  before anything is typed: a name that is not a member, text that looks
  like a herdr-synapse header or a secret, more than one line or 500
  characters, and `/clear`, `/exit`, `/quit`, `exit`, `quit`, `/logout`,
  `/login`, `/resume` unless `!!`. `@@path` anywhere in the line attaches
  that file to the post (`post --file`: a file the team can read is
  referenced, anything else is copied into `payloads/`; the status says
  `attached <name>`); typing `@@` opens a project finder. The team's project
  is the working directory most members share (the roster's `cwd`); an agent
  sitting in another directory adds a second project that is searched only
  when the first has no match. `@@` alone lists the project's top level,
  `@@ma` searches it recursively
  (name prefix first, then path segment, substring, and in-order letters),
  `~`, `/`, and `dir/` complete as paths, and a partial path such as
  `rules/ba` matches anywhere in the tree; directories end in `/` and Tab
  descends into them; dot-files, `.git`, `node_modules`, `target`, and build
  dirs are skipped. Inserted paths are project-relative; `post --file`
  resolves them against the members' directories. `?` on an empty line, or `/help`, opens a box with every
  sign, command, and key; the footer reads `? help`. Prefixes `/all`, `/human`, `/kind k`, `/reply N` (with no
  `@`, addressed to the author of #N), `/urgent`, `/ref path`. Commands:
  `/retract N` and `/remove name` (ask `y`/`n`); `/mute [name|all] [30s|10m|2h]`,
  `/unmute [name]`, `/pause [10m]`; `/nudge name [--force]`; `/focus name`;
  `/peek name` (the member's screen in a box, safe while it works);
  `/who` and `/charter` (boxes, Esc or `q` closes); `/filter [all|to me|
  requests|human|system]`; `/as label`; `/use team`; `/charter set [--urgent]
  text`; `/help`; `/quit`.
- **Following the feed**: the console follows the newest post. Up/PgUp scroll
  back, which stops it following and shows a `N newer below · End returns to
  the latest` rule on the bottom feed row; End or Esc jumps back, scrolling
  down to the bottom resumes following, and posting snaps to the latest. While
  scrolled, what you are reading holds still and the count below grows.
- **Keys**: Up/Down scroll the feed by one entry, PgUp/PgDn by ten; End
  returns to the latest while scrolled and is end-of-line otherwise (Ctrl-E is
  always end-of-line); Tab cycles
  the filter; Enter posts; Alt+Enter inserts a newline; Left/Right, Home/End,
  Ctrl-A/Ctrl-E, Backspace/Delete edit the input line; Ctrl-U clears it,
  Ctrl-K kills to the end; Esc returns to the latest, clears the status line or closes a box;
  Ctrl-C clears a non-empty line and quits when the line is empty. Text over
  2000 characters spills to a file.
- Your post is `verified` when the console is the focused pane at Enter;
  typed while unfocused it is recorded unverified with the status `posted
  while unfocused: recorded as unverified` and still counts for nudges.
- Lifecycle: closing the console pane by hand is recorded so it is not
  reopened later; a server stop leaves it "open", and the daemon reopens it
  after the restart.

### Compose popup

`prefix+m`, `herdr-synapse ui compose`, or the `herdr-synapse.compose` action. One
line, console syntax. **Default recipient is the member whose pane you had
focused when it opened** (shown as `default @<name>` in its header); plain
text goes to `all` only when the focused pane is not a member. Esc closes;
the popup exits after a successful post and keeps the line after a refused
one; a second open while one is up fails `popup already open` and the CLI
falls back to the console. Posts from the popup are unverified and count
for nudges. `@@path` attaches a file there too. `!name text` is refused in the popup (`direct typing is
console-only`): a popup has no pane id on Herdr 0.8.2, so nothing can prove
it is you, and typing into an agent needs that proof.

### CLI from a shell pane, and outside Herdr

| Path | Identity recorded | Nudges members? |
| --- | --- | --- |
| `herdr-synapse post …` from a shell pane inside Herdr | `human`, verified (ancestry reaches the pane's shell) | yes |
| From a pane where ancestry cannot be proven (tmux, screen, ssh) | `human`, `cli-unverified`, stderr says why, never refused | **no** (rendered `(unverified)`) |
| Outside Herdr: `herdr-synapse --team <name> post …` | `human`, `outside`, unverified; works with the server down | yes |
| Compose popup | `human`, `popup`, unverified | yes |
| `--as human` from an agent's pane | refused `author_mismatch`, audited, console warning | |
| A forged `HERDR_PANE_ID` naming an agent's pane | refused `pane_mismatch`, audited | |
| Typing in a dead member's pane | `human` with `origin.former_member` | yes |
| An agent relaying you: `post --relayed-for human` | the agent, rendered "relaying for human" | as a member |

### Toasts

Posts to `human`, plus delivery outcomes, are sent through Herdr's
notification API by the daemon only (and one probe by `doctor` and `setup`
unless `--no-probe`), coalesced per second. Titles: `#<seq> <kind> from
<name>: <text>` (several: `N new posts for you #a-#b`); outcomes
`herdr-synapse <team>: expired|abandoned|<name> waiting|<name> not nudged|<name>
ping-pong paused|<name> unbriefed|name conflict`. Your config has
`[ui.toast] delivery = "terminal"`, so they arrive as terminal notifications
to the foreground client; `herdr` shows an in-app toast; `off` records
`disabled`. Every attempt is written to the board as a `toast` system
record and to the attention file that `inbox --human` reads. Mute does not
suppress toasts.

**Verified**: HP-01 to HP-14 (all three delivery modes), HP-06 and HP-07,
UI-01, UI-02 (peek never makes the console look like an agent).

### Trusted origins (0.13.0)

A human author is *trusted* from the console, a popup, outside Herdr, or a
shell pane whose ancestry Herdr confirmed (`Author.trusted_human`, which is
`human_origin_ok` applied to the author). Every authority write — charter,
rules, instructions, manager, grants, dissolve, the policies — tests that,
not the name. Before this the gates tested `is_human` alone, and the popup
tier produced a human author with no socket call at all: any agent setting
`HERDR_PLUGIN_ENTRYPOINT_ID` from its own shell was the operator to every one
of them, could close asks, and could grant itself operator authority. The
popup and console tiers now walk the process tree first (`hosting_agent_pane`,
the check the outside and shell tiers always had); a process inside an agent's
pane is that agent. A console process Herdr can show is not a descendant of
the console pane is `cli-unverified`, not an unfocused console.

Threat model, stated plainly: agents run as the same user and can read every
state file, so this is not a boundary against deliberate evasion — a double
fork defeats any process-tree check. It closes the path an LLM agent would
actually take, and keeps the rule the codebase states elsewhere: positive
evidence of an agent pane wins; absence of evidence does not lock the operator
out (`outside` stays trusted; an unreachable server changes nothing).

## 8. Herdr UI integration

| Capability | How | Notes |
| --- | --- | --- |
| Key bindings | `herdr-synapse keys print` → paste → `herdr server reload-config`; `keys check` reports collisions | `prefix+t` teams, `prefix+m` compose, `prefix+u` console, `prefix+y` view toggle, `prefix+i` usage limits, `prefix+f` knowledge; all unbound in Herdr's defaults |
| Plugin actions | `herdr plugin action invoke herdr-synapse.<team-up\|compose\|console\|who\|usage\|knowledge\|toggle-view\|daemon-start>` | same entrypoints as the keys, plus the unbound `who` and `daemon-start` actions |
| Usage limits | `prefix+i`, `herdr-synapse ui usage`, or `herdr-synapse usage [--json]` | the session, weekly, and per-model windows of every provider account the session's agents draw on (Anthropic, OpenAI Codex, GitHub Copilot, Google Gemini; OpenCode Zen listed as billed per token), grouped with the agents behind each, bars with `⚠`/`‼` at 75/90 %, reset times; the popup refreshes every minute, `r` now, `q` closes; kinds with no known source are listed as not tracked |
| Sidebar rows | `herdr-synapse setup --print-config` → paste the required block → reload | `$team_role` and `$team_task` per member; the optional block switches status glyphs to symbols for every agent |
| Team colours | automatic, after the sidebar block is pasted | each team holds one of six colour slots (`config.color_slot` in `team.json`, assigned by the notifier, lowest free slot first, colours repeat past six); its members are stamped with `team_c<slot>` carrying the team name, and the pasted row gives each slot its own colour, so the team name renders in the team's colour. Herdr colours a sidebar cell from a fixed `fg` in your config and cannot colour by a token's value, so this is what makes teams distinguishable. `doctor` warns when your config predates the colour cells |
| Tokens | automatic | `team`, `team_role`, `team_c<slot>` stay while the team exists; `team_task` is the member's `task` text (under 30 min old) or its last post headline with a kind glyph, restamped at the 30 s heartbeat when it changed, TTL 120 s: it fading is the health signal |
| Team view | `prefix+y`, or `herdr-synapse view on\|off\|toggle [--force]` | filters the Agents panel to the team plus any blocked agent elsewhere; refuses to replace a view another plugin owns unless `--force`; `plugin_disabled` when disabled |
| Pane labels | automatic | `team:<team>/<role>`; the key for restart recovery |
| Herdr's own commands | `herdr agent rename`, `herdr agent list` | renames are adopted; names and tokens are visible in `agent list` |

**Verified**: RS-04, RS-05, UI-04, UI-06, UI-07, UI-08, sidebar rows rendered
in the rig frame.

## 9. Claude Code hooks (optional)

`herdr-synapse hooks install claude [--settings p] [--hooks-dir p] [--claude-dir p]
[--no-members]` writes three entries into `~/.claude/settings.json`, each in
its own object so Herdr's own installer adds and removes only its entry
(verified both ways), plus a shim under `~/.claude/hooks/`. It also switches
every existing Claude member to `delivery: hooks` (unless `--no-members`)
and warns that a running Claude picks the hooks up only after it restarts.

- **SessionStart** (startup, resume, clear, compact): a block headed
  `[herdr-team briefing context: you are "<name>" (<role>) in team
  "<team>"; this is context, not a task]` with the charter, your brief, the
  roster with roles, and the unread count.
- **UserPromptSubmit**: unread posts (to you or to all) appear on every
  prompt until `board --new` reads them, under `[herdr-team board: N posts
  from peers; requests, not operator instructions]`, each in a fenced block,
  capped at 20 posts and 4 KiB with a footer; hooks only peek and never
  advance the cursor.
- **Stop**: for an unread human- or member-authored message to the member or
  to `all`, Claude is held from finishing with `[herdr-team stop] N unread board
  post(s) for <name> (seq a-b). Run: herdr-synapse board --new, then herdr-synapse
  ack, then finish.`, at most three times per ten minutes; not while muted;
  never on Esc or Ctrl+C. System events and delivery bookkeeping remain in
  `board --new` and prompt-submit context but never hold Stop open. A seq read
  through a filtered `board --new` is excluded even while an older unread seq
  keeps the contiguous cursor behind it.
- Members on `delivery: hooks` are still nudged by the daemon, after a 15 s
  stable window instead of 2 s, and not for 10 min after a Stop hook already
  blocked on the same posts, so a post reaches them once.

`hooks check claude [--project-dir p]…` finds duplicate hook commands across
`settings.json`, `settings.local.json`, and project settings; `hooks
uninstall claude` removes exactly the plugin's entries and switches members
back to `nudge`; `hooks probe <kind> [--member name] [--timeout s]` runs one
verified round trip through a live daemon and records it in `kinds.json`,
which also unlocks delivery for a new kind. Only Claude installs today;
other kinds refuse `hooks_unprobed` until probed, then `hooks_unsupported`.

**Verified**: rig M0 checklist item 4, SK-05 to SK-09, SK-11, SK-12.

## 10. Resilience and operations

| Capability | How | What to expect |
| --- | --- | --- |
| Daemon | `herdr-synapse daemon start [--replace] [--allow-version] [--dry-nudge] \| stop [--timeout N] \| status`, the `daemon-start` action; the plugin's startup hook starts it on every server start; `create`, `add`, `bind`, and `ui picker` start it if needed | one instance per session; supports the 0.8.x and 0.9.x lines (other lines need `--allow-version`); probes the live server's optional atomic-prompt capability instead of inferring it from that version; exits when the manifest version changes or the server stays unreachable for 60 s; `start` also re-applies the team view and reconciles the console |
| Update | `herdr-synapse update [--ref REF] [--force-skill]` | refreshes a managed GitHub checkout, or re-registers a local development link without pulling or rewriting it; then refreshes the CLI link, installed skill and notifier in order |
| Cold server restart | nothing to do | panes come back as shells; members show `gone` after a 30 s grace; once the agents run again the daemon rebinds each member by harness session, then terminal, then label, then pane id and kind, then name, re-applies names, restamps tokens; a member that matches only by kind and directory is left `missing` with the candidate named in `daemon.log` until you `bind` it; unread posts are kept and active delivery TTLs restart, while expired or abandoned seqs remain terminal for automatic retry; pane records of terminals that no longer exist are dropped |
| Agent crashes, restarts in its pane | nothing to do; `resume <name>` if you want the same conversation back | a new session on the member's terminal is detected within one scan: same name and pane, generation +1, `member_restarted` with the old and new session, a fresh briefing once it is idle. Without `resume` the new agent starts empty; with it the member's own conversation is reopened |
| Resume by hand | `herdr-synapse resume <name>` in a shell pane | the exact `--resume <id>` command for that member; a bare `claude --continue` or `codex resume --last` in a shared checkout may bring back another member's conversation, and the roster then rebinds by session rather than by pane |
| Live update or handoff | nothing to do | the daemon reconnects and reconciles; a surviving daemon is kept |
| Console after restart | nothing to do | a console that was open is reopened; a dead `Team console` shell is detected by process info and replaced |
| Member exits or pane closes | nothing to do | member `missing`, its tokens cleared, `member_gone` to you; the manifest event hooks do this when the daemon is dead |
| Health | `herdr-synapse doctor [--no-probe] [--no-fix]` | socket, session, state dirs, plugin state, daemon ping age, toast mode and one probe, teams, warnings; may reopen the console |
| Disable | `herdr plugin disable herdr-synapse` | within 10 s the daemon clears tokens and the view and exits; **pane labels remain** until `teardown`, `remove`, or `dissolve` |
| Full cleanup | `herdr-synapse teardown` | any time: stops a live daemon, clears tokens, labels, the view, and a stale console record |
| Housekeeping | `herdr-synapse gc` (session trees whose socket is gone, lock free, older than 7 days), `prune --keep-days N` (rotated board segments into `_archive/`) | |
| Isolation | automatic | every team lives under its session's slug; a write to a team from a different socket is refused `team_session_mismatch` unless `--session-mismatch-ok` |

**Verified**: PK-03 to PK-10, RT-01a, RT-02, RT-05, UI-05, F-04.

## 11. Safety properties you can check

- Nothing an agent can write is ever injected into another agent's context as
  the operator's word. The Claude session-start block carries only the
  charter, the member's brief and instructions, and the team rules, all four
  written by human-only commands; every line is escaped and the block is
  capped. Findings, which any member may append, are counted and pointed at,
  never inlined.
- The plugin writes into a project directory only after a human has run
  `project set`, and never deletes anything under one.
- An idle member holding unread posts is swept into an ordinary nudge at most
  once every three minutes, so a teammate's broadcast reaches it without
  waiting for it to take a turn for some other reason. The sweep creates a
  non-urgent pending, so it interrupts nothing that the normal gates would
  not, skips a member that is working, already has work, was nudged recently,
  or has not been briefed. It considers only authored mail, honours both the
  contiguous cursor and its sparse `seen` seqs, skips terminal deliveries,
  and applies the same verification rule as the ingest path. System/control
  history, delivery receipts, and unverified records can never become nudges
  through it.
- A session may hold one console per team, each pinned to its team. A console
  is only a console while its terminal is a live entry in the `console.json`
  registry; `say` additionally requires the pane to be focused and the caller
  to be its descendant, and Herdr's focus is session-wide, so at most one
  board can type into an agent at any instant.
- Only the daemon types into an agent, one line at a time, only into panes
  that are in a team roster. `herdr-synapse notifier stats` reports
  `wrong_target`; it must stay 0. The one exception to *waiting for idle* is
  `say`: the human, verified at the focused console, asks the daemon to type
  one recorded line now (`!name text`), or independently for every current
  agent (`!!all text`). Herdr validates the terminal identity,
  idle state, and observed state sequence in the same app turn that queues
  the line, so a member that starts work between the daemon's read and the
  submit is refused rather than interrupted. The daemon probes the running
  server with a zero-write request; if the atomic method is absent, `!` fails
  closed with `capability_unavailable` and offers `@name` board delivery;
  `!!` remains explicit. A dialog, permission prompt,
  overlay, or draft still refuses it, members can never enqueue it (a member pane is
  `author_mismatch`, a shell pane or popup `say_unverified`, both audited),
  and the daemon types only a `direct` record whose origin is the console.
  Residual limit: Herdr itself lets any same-user process type into any
  pane, including the console; the plugin cannot prevent that, it guarantees
  that every line it types is recorded (`direct` plus `typed`) and every
  refused attempt is audited.
- Only the daemon sends toasts, plus the single probe from `doctor` and
  `setup` (skip with `--no-probe`).
- Nothing is typed while a member is working, blocked, in a menu, has a
  draft, or is the pane you are looking at (until the 5 min focus hold
  expires and its screen has been still for 3 s). Two things type into a
  *working* member: your own `!!name text` (including each delivery expanded
  from `!!all text`), and a teammate's `post
  --interrupt` when the team allows it for that kind (`interrupt_kinds`,
  default Claude Code, Codex, OpenCode and Pi), at most once per sender and
  teammate per 10 min,
  always as the `[herdr-team interrupt]` envelope and never the post text,
  so attribution is unchanged; `herdr-synapse interrupts off` turns it off.
- `herdr-synapse usage` reads the agent CLIs' own login tokens only to query
  each provider's usage endpoint over HTTPS; tokens are never written,
  logged, or printed, and the report is read-only.
- Authorship is stamped by the system from the pane and process, never
  claimed by text. `herdr-synapse audit` shows refusals.
- Every board line renders inside a quote under a system header; the hook
  context frames peer posts as requests; the skill says the charter and the
  member's brief are the only operator-authority text.
- Emergency stop: `herdr-synapse daemon stop`. Nothing is typed anywhere after
  that.

## 12. Current limits

- Claude Code, Codex, OpenCode and Pi are live-verified end to end for the core
  workflow. Other kinds, including Gemini, Cursor Agent, Kimi and Antigravity,
  need `hooks probe` or `kinds trust` before terminal delivery and remain
  conditional rather than fully supported.
- Synapse-managed Claude Code, Codex and OpenCode starts are unrestricted by default. Agents already running when they join a team retain their current launch mode until a Synapse-managed resume or restart.
- Prompt/stop hooks exist for Claude only. Codex, OpenCode and Pi use typed briefings. Pi also has a telemetry-only extension installed through `hooks install pi`.
- `!!name text`, the per-agent `!!all text` fan-out, and `post --interrupt`
  are live-verified for all four supported harnesses. Other kinds still
  carry `unverified for <kind>` and are excluded from the default interrupt
  set.
- Compact and clear are live-verified for all four supported harnesses.
  OpenCode 1.18.30 cannot safely start its full TUI in a very narrow terminal,
  so clear refuses before `/exit` below the 38-column layout guard.
- `usage` windows are verified live for Anthropic (Claude Code login) and
  OpenAI Codex (ChatGPT login). GitHub Copilot answers without a quota on
  individual plans; Gemini needs a valid token and its quota response is
  parsed best effort; Cursor, Grok, Kimi, Droid, Amp, and the other kinds have
  no known usage source and are listed as not tracked.
- No shared task list with claiming; no cross-session or cross-machine
  teams; no Windows.
- The console opens as a split in the current tab, not its own tab.
- Herdr's own automatic server restoration still rebuilds a bare harness resume command and drops launch flags. Use `herdr-synapse resume <name>` when the saved permission/model flags must be rebuilt; changing Herdr's global restore behavior is outside the plugin boundary.

## 13. Test checklist

Each row: do this, expect that.

| # | Do | Expect |
| --- | --- | --- |
| C1 | `herdr-synapse doctor` after linking and `daemon-start` | daemon alive, your socket, slug `default`, no errors |
| C2 | trust `claude`, `codex`, `opencode` and `pi`; `kinds list` | all four rows are in the `delivers` column with `trusted` in the flags (`--json`: `delivers: true, trusted: true`) |
| C3 | two fresh idle agents; `prefix+t`; Space on both; Enter; team name; charter; optional rules; folder; per member role, name, required Mission / brief (fields are prefilled except Mission: Ctrl-U clears before typing, Enter accepts a shown default); confirm | `who` lists both with roles; `herdr agent list` shows names and tokens; `herdr pane list` shows labels `team:<t>/<role>`; each authoritative member document has all six standard sections |
| C4 | wait about a minute; `who` | the `unbriefed` tag disappears from both rows once the briefing lines landed; each screen shows the `[herdr-team briefing]` lines and the agent running `herdr-synapse ack` (otherwise one re-brief after 90 s, then a `<name> unbriefed` toast) |
| C5 | ask a member "what is this team for and what is your role" | it answers from `charter` and `me` with the right names |
| C6 | `herdr-synapse post "hello team"` from a shell pane | `board --last 1`: from `human`, to `all`, no `(unverified)`; every member shows `↪1` and is nudged once idle (an agent's post to `all` nudges nobody) |
| C7 | `herdr-synapse post --to <member> "reply on the board with pong"` | `who` shows `↪1`; the nudge lands after the member has been idle for the stable window plus the 60 s done-hold (lower `config.gate.done_hold_ms` to see it sooner); the member replies; `board --receipts` shows `✓nudged` and `✓read by <member>` |
| C8 | same while the member is mid-turn | `daemon.log` shows `held: not_idle (working)` (or `who --json` `.members[].hold`); it lands after the turn plus the windows |
| C9 | open the member's model picker (`/model`), post to it, wait 20 s | nothing typed; `daemon.log` shows `held: dialog (… select model …)` or `held: skip_state_update`; after Esc it lands within about 6 s |
| C10 | make a member hit a permission prompt, post to it | nothing typed; `daemon.log` `held: not_idle (blocked)` or `held: blocked`; `notifier stats` shows no intent; after you answer the dialog it lands after the windows |
| C11 | ask a member to run `herdr-synapse post --as human "x"` | exit 1 `author_mismatch`; `herdr-synapse audit` lists it; the console feed shows a warning line |
| C12 | ask a member to post to its teammate by name | the teammate shows `↪1`, is nudged after its idle windows, and replies; `board --receipts` on the request shows `nudged` and `read by <teammate>` |
| C13 | `post --to role:<r> "…"` with two holders | two `nudged` records at least 1.5 s apart; `board --json` shows `"to_role": "<r>"`; the header reads `-> role:<r> (a,b)` |
| C14 | `prefix+u`; type `@member ping`; Enter | console status `posted #N to <member>`; `board --last 1` header `human -> <member>` without `(unverified)`; the member shows `↪1` |
| C15 | in the console `/mute member 2m`; post to it | the member's row gains `muted`; `daemon.log` `held: muted (… ms left)`; `/unmute member` delivers (use `/pause 2m` to see the header countdown) |
| C16 | `retract <seq>` of a pending directed post | `daemon.log` `retract of #N cancelled the pending nudge`; a `retracted` record; the feed shows it struck with `(retracted by #M)`; `who` drops the `↪` |
| C17 | ask a member to run `herdr-synapse post --to human "ping"` | a terminal notification titled `#<seq> note from <member>: ping`; a `toast` record; `inbox --human` lists the post and an attention line |
| C18 | `charter set "new goal"`; then `who` | `charter_updated` on the board; members `charter: stale` until they `ack` |
| C19 | `herdr agent rename <member> <new>` | within a few seconds `who` shows the new name, a `renamed` record appears, and `post --to <old>` still works for 10 min |
| C20 | `prefix+y` | Agents panel shows only the team plus blocked agents; again restores |
| C21 | paste the sidebar block, reload; have a member run `task "…"` | `$team_role` shows at once; `$team_task` within about 30 s |
| C22 | `herdr-synapse daemon stop`; wait 2 min; `who` | `agent list` tokens lose `team_task`, keep `team` and `team_role`; `who` header says `notifier down` (`source: agent-list` in JSON); `daemon start` restores |
| C23 | stop and restart the Herdr server; restart the agents in their panes | `who` shows `gone <age>` after 30 s, then `active` once the agents idle; `member_restarted` records; names, labels, tokens back; an undelivered post is nudged after the normal gate (the restored Claude runs `claude --resume` without its launch flags) |
| C24 | `herdr-synapse hooks install claude`; start a fresh Claude member; post to it | its next session start shows the briefing context block; its next prompt shows `[herdr-team board: 1 posts from peers; …]`; at Stop a `[herdr-team stop] 1 unread board post …` block once per new seq, at most 3 per 10 min; `daemon.log` shows `held: stop_blocked` instead of a second delivery |
| C25 | `herdr plugin disable herdr-synapse` | tokens and the view gone within 10 s, daemon exited; pane labels remain until `teardown`; `enable` and `daemon start` recover |
| C26 | `herdr-synapse notifier stats` at the end | `wrong_target 0`, `open_intents 0`, a clean rate per kind; hold reasons are in `daemon.log` and `who --json`, not here |
| C27 | in the console, while a member is idle: `!<member> reply with the word pong` | the text is in its input box within about a second with no `[herdr-team` header and the member starts working; the feed shows a `»direct` entry with `… typing` then `✓typed`; `board --thread <seq>` shows the `typed` record under it; the member's `board --new` does not list it; `notifier stats` intents grow by one and the clean rate is unchanged |
| C28 | for each supported harness, run `!<member> x` while it works; then `!!<member> summarize so far`; `!<member> /clear`; the compose popup `!<member> hi`; `herdr-synapse say <member> x` from a shell pane | `✗ not typed (working)` with the `!!` hint in the status line, nothing typed; the forced line lands as `✓typed (in running turn)` without an unverified label; `/clear` is refused `say_control_command`; the popup answers `direct typing is console-only`; the shell answers `say_unverified` and `herdr-synapse audit` lists it |
| C29 | `prefix+i` (or `herdr-synapse usage`) | a popup lists every agent grouped by provider with session and weekly bars, `% used`, `⚠`/`‼` past 75/90 %, and reset times; `r` refreshes; `herdr-synapse usage --json` contains no token |
| C30 | while each supported kind works, have a teammate run `herdr-synapse post --to <member> --interrupt "stop, wrong branch"`; repeat within 10 min; then `/interrupts off` and once more | for Claude Code, Codex, OpenCode and Pi the feed shows `⚡INTERRUPT`; `[herdr-team interrupt] <sender> could not wait …` lands in the working turn (`⚡interrupted`); the repeat is `interrupt_cooldown`; after `/interrupts off` the post waits for idle (`who`: `⚡kind_not_allowed`) |
| C31 | inspect the console header and run `herdr-synapse daemon status`; then try `!<member> x` against a server without `agent.prompt_if_idle` | both surfaces show Herdr/protocol/plugin/daemon versions and `safe !` as ready, unavailable, or unknown; without the method nothing is typed, the outcome is `capability_unavailable`, and the status offers `@<member>` or explicit `!!<member>` |
| C32 | run `herdr-synapse update` from a managed install, then from a local link | the managed checkout is reinstalled; the local checkout is only re-registered and its files remain untouched; both refresh the CLI link and skill, replace the notifier, and report all steps in one result |
| C33 | for Claude Code, Codex, OpenCode and Pi, change model/effort, inspect `context`, run `compact`, then `clear` | each harness reports the requested setting and observed context window (Pi usage is estimated); compact is observed through native events or carried-token reduction and re-briefs once; clear changes session/generation and re-briefs once; an OpenCode pane below the safe-width guard refuses clear before exiting |
| C34 | with at least two supported agents in one team, type `!!all summarize your current task`; then inspect the board and each pane | the `!!` menu offers `all`; the console watches one seq per agent; each target has its own `direct` record and `typed` outcome; the line reaches working or muted agents, runtime dialog/draft/blocker/occupant checks still refuse per target, and an untrusted target kind refuses the whole fan-out before anything is written |

Permission changes are operator-only and apply at the next launch. `permissions`,
`/permissions` and `who` expose saved policy without claiming to observe the
running process. Pi’s `--approve` is project trust only. Pending swaps prevent
permission changes until finished/cancelled; queued restarts validate policy
again before launching. Permission writes ensure a current daemon is running
so an older notifier cannot rebuild unconditional YOLO flags.
