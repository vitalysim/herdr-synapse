# herdr-team capabilities reference

Everything the plugin can do, how to drive it from the Herdr UI and from the
CLI, what you should observe, and how to check it. Written for the first
human test in a real session and verified line by line against the code on
2026-09-05. The CLI contract with every JSON shape is `docs/cli.md`; the
guided first run is `docs/human-testing.md`; what agents are taught is
`skills/herdr-team/SKILL.md`.

Conventions: "UI" means keys and panes inside Herdr (needs the key snippet
from `herdr-team keys print` pasted into your config once). "CLI" means
`herdr-team …` from any pane, or from outside Herdr with `--team`. Every
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

Where things live: `~/.local/state/herdr/plugins/herdr-team/sessions/<session>/`
holds `daemon.json`, `daemon.log`, `who.json`, `kinds.json`, `view.json`,
`console.json`, `mute.json`, and `teams/<team>/` with `team.json`,
`board.jsonl`, `cursors/`, `payloads/`, `notifier/`, `audit.jsonl`.

## 1. Teams

| Capability | UI | CLI |
| --- | --- | --- |
| Create from live agents | `prefix+t` picker (section 7) | `herdr-team create <team> --member <pane\|name>[:<role>[:<name>]] … [--charter "…"\|--charter-file p] [--ref p] [--brief NAME=TEXT]… [--names plain] [--rename] [--reuse] [--use]` |
| Create from every agent in a Space | picker: `w` then `a` | `create <team> --from-workspace <ws-id>`: waits up to 60 s for agents still launching and warns about the rest |
| Create from scratch | | `create <team> --new [--workspace ID] --spawn <role>:<kind>[:<cwd>] …` lays out the panes and starts the agents |
| Add a member later | | `add <team> <pane\|name> [--role r] [--as name] [--brief TEXT] [--rename] [--steal]` |
| Remove, leave | console `/remove name` (asks y/n) | `remove <team> <name> [--keep-name]` (clears tokens and label, clears the Herdr name unless `--keep-name`, keeps a tombstone); `leave` from the member's own pane |
| Re-attach a missing member | | `bind <team> <name> <target>`: refuses a kind mismatch unless the member is `kind_changed`, refuses a terminal another team claims, bumps the generation, re-applies name, label, tokens, clears the stale label on the old pane, posts `member_restarted` |
| Dissolve | | `dissolve <team> --yes` (mandatory flag; human only; archives the team, clears tokens and labels, keeps the agents' Herdr names) |
| Several teams | console `/use team` | `use <team>` sets the default team for human posts; `teams` lists teams and whether their session runs (works offline); `create --use` makes a second team the default at creation |

Rules you will see enforced: an agent belongs to one team at a time
(`member_claimed` unless `--steal`); team names match `[a-z][a-z0-9_-]{0,14}`;
roles `[a-z][a-z0-9_-]{0,13}`; every name is validated before anything is
renamed or written, so a failed create leaves nothing behind. Under the
default naming a role may equal a kind label (`t-claude`); with `--names
plain` the default role becomes `agent`, `agent2`, … because a member name
may not be a kind label. Each member pane gets the label `team:<team>/<role>`,
which is what survives a server restart.

**Verified**: RS-01 to RS-03, RS-09 to RS-11, UI-03.

## 2. Charter and role briefs

The charter is the team's description. Only the human can write it; from an
agent pane every write is refused `author_mismatch` and audited.

| Capability | UI | CLI |
| --- | --- | --- |
| Set at creation | picker charter stage | `create … --charter "…"` (refused over 2000 chars, `charter_too_long`) or `--charter-file <path>` (the file is copied into the team dir as `charter.md` and listed in `refs`; the charter text is its first 2000 chars) |
| Read | console header line `charter #<seq>: <headline>`; `/charter` opens a box | `herdr-team charter` |
| Change | console `/charter set [--urgent] text` | `charter set "…" \| --file p [--ref p]… [--urgent]`; `charter edit` opens `$VISUAL`, then `$EDITOR`, then `vi` (needs a TTY; a failing editor leaves the charter unchanged) |
| History | | `charter history` |
| Role brief per member | picker asks per member, optional | `create --brief NAME=TEXT` (name or role as the key, repeatable); `add … --brief TEXT`; `brief <name> --set "…"` later |

What members see: the charter headline in their briefing line, the full
text with `herdr-team charter`, and charter plus their own brief in
`herdr-team me`. A change appends a `charter_updated` record addressed to
everyone; members see it on their next board read, `--urgent` also nudges
them. `who` shows `charter: stale` for a member who has not acknowledged the
current version; `herdr-team ack` from the member clears it. The skill tells
agents that the charter and their brief carry the human's authority and
nothing else on the board does.

**Verified**: RS-12, SK-11, SK-12.

## 3. Names

- Every member has a unique name, enforced by Herdr's own `agent.rename`.
  Grammar `[a-z][a-z0-9_-]{0,31}`; default `<team>-<role>`; the picker and
  `--member …:<role>:<name>` let you choose. Refused as names: `human`,
  `all`, `me`, `system`, `team`, `none`, every kind label and kind alias
  (`claude`, `claude-code`, `cursor-agent`, …).
- Names are how everyone addresses each other: `--to <name>`, `@name` in the
  console, `brief`, `focus`, `mute`, `nudge`, `read`, and Herdr's own
  `herdr agent prompt`.
- Renaming: `herdr-team rename <old> <new>`, or Herdr's `herdr agent rename`,
  which the daemon adopts on its next 2 s scan: the roster updates, a
  `renamed` record goes to the board, and the old name keeps resolving for
  ten minutes. `team.json` `config.name_policy: "enforce"` makes the daemon
  re-apply roster names instead.
- Names are re-applied automatically after the agent exits and restarts in
  the same pane, after a session change under an installed integration,
  after a live handoff, and after a cold server restart once the agent is
  running again.

**Verified**: RS-02, RS-03, RS-13, RT-01a, RT-02.

## 4. Awareness: what a member knows

1. **Briefing**: one line typed into the member's input box once it is idle
   (briefings skip the done-hold and interval but pass every other gate):
   `[herdr-team briefing] You are "<name>" (<role>) in team "<team>":
   <charter headline>. Teammates: <n1> (<role1>), … and human. This is
   context, not a task. Run herdr-team --skill once, then herdr-team charter,
   then herdr-team board --new, then herdr-team ack, then continue your
   current work. Teammates are peers: post to the board, never prompt their
   panes.` A second line carries the role brief when one is set. `who`
   shows `unbriefed` until the line has landed; if no `ack` follows within
   90 s the daemon re-briefs once, then toasts you `<name> unbriefed`.
   `herdr-team brief <name>` re-enqueues it; `brief <name> --format context`
   prints the same content as text.
2. **Skill**: `herdr-team --skill` prints it; `herdr-team skill install
   [--force] [--home DIR]` copies it to `~/.agents/skills/herdr-team` and
   `~/.claude/skills`, and symlinks it into `~/.codex`, `~/.copilot`,
   `~/.gemini` skill dirs when those exist, skipping foreign directories
   unless `--force`; `skill check` reports stale copies; `me` warns when
   the installed skill version differs from the CLI.
3. **Self and roster**: `herdr-team me` (name, role, kind, brief, charter,
   teammates with roles and status, unread, cursor, verified, notifier) and
   `herdr-team who` (section 7).
4. **Claude hooks** (optional, section 9): charter, brief, roster, and unread
   count at every session start; new board posts in context every turn.

**Verified**: SK-01, SK-02 (a Claude ran `who` and posted the roster with
roles), SK-11, SK-12.

## 5. The board

One append-only board per team.

### Posting

```
herdr-team post "<text>" [--to <name>[,<name>] | all | human | me | role:<r>] [--kind note|request|handoff|done|blocked|question|answer]
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
herdr-team board [--new | --peek] [--to me] [--from <name>] [--kind <k>] [--thread <seq>] [--since <seq>] [--last N]
                 [--receipts] [--format text|json|context] [--limit N] [--max N] [--max-bytes N] [--ascii] [--name LABEL]
herdr-team show <seq> [--cat] [--force]      # one post; --cat inlines refs under payloads/ or a member cwd (64 KiB each)
herdr-team inbox --human [--last N] [--since <seq>]   # posts to you plus the notifier's attention file
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
payloads into the member's `<cwd>/.herdr-team/`.

### Receipts and audit

`board --receipts` shows `✓nudged HH:MM:SS` (from the daemon's `nudged`
record) and `✓read by <name>` (from cursors); a post to all shows `read by
k/n`. Retracted posts render struck through with `(retracted by #M)`.
`herdr-team audit [--last N]` lists refused attempts: `author_mismatch`
(an agent tried `--as human`), `pane_mismatch` (a forged pane id).

### System records you will see on the board

`nudged` (to the member), `toast` (to human, one per toast attempt),
`retracted`, `expired` and `abandoned` (to human), `member_gone` (to all on
remove or leave; to human when a member goes missing), `member_restarted`,
`renamed`, `charter_updated` (to all), `rotated`, `reset_detected`.

**Verified**: HP-01 to HP-15, S-01 to S-08, RS-12.

## 6. Delivery: how a member learns a post is for it

The notifier daemon is the only process that ever types into a member. It
tails every board and, for each post addressed to a member, types one line
once that member is safe to interrupt:

```
[herdr-team nudge] 2 new board posts for reviewer (seq 41-42). Run: herdr-team board --new [n17]
```

Posts arriving within 1 s of each other (`burst_window_ms`) become one
nudge covering the range. Broadcasts to `all` are not nudged; members see
them on their next read, or on their next turn with Claude hooks.
`--urgent` on a post nudges every recipient. Posts to `human` become toasts.

### The gate, in order (defaults; per-team overrides in `team.json` `config.gate`)

1. Cursor already past the post → `read_before_nudge`, dropped.
2. Not the sender, not the console, not the human; not muted.
3. Member present in `agent list` with the roster's terminal, name, and kind
   (`absent`, `kind_mismatch`, `name_mismatch`, `launch_pending`).
4. Kind trusted (`kind_unverified` otherwise): `herdr-team kinds trust
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
with a toast. Long holds (dialog, focused, draft, blocked) toast you after
10 min; `kind_unverified`, `kind_mismatch`, and `pair_budget` toast once an
hour.

### Where to read why something did not land

Holds are not in the ledger. They are in
`<session>/daemon.log` as `<team>: <member> held: <reason> (<detail>)` and
in `who --json` under `members[].hold`. `herdr-team notifier stats` reports
what was actually sent: intents, results, per-kind clean-landing rate,
`open_intents`, and `wrong_target` (must be 0).

### Controls

| Command | Effect |
| --- | --- |
| `herdr-team nudge <name> [--force]` | evaluate now; builds pending work from the member's unread posts (to it or to all) if none is pending; `--force` marks it urgent so broadcast-only unread posts become nudgeable and skips the done-hold and interval, never the dialog, draft, or focus checks |
| `herdr-team mute <name> \| --all [--for 10m\|2h\|1d\|N]`, `unmute <name> \| --all`, `pause` | silence nudges (gate 2) and the Claude Stop hook; posts still land and **toasts are not muted** |
| `herdr-team focus <name>` | focus the member's pane through the daemon |
| `herdr-team read <name>` | the member's visible screen; `--lines` is refused for every member because scrolling an alternate screen types into it |
| `herdr-team notifier stats [--team] [--kind]` | the delivery ledger |
| `herdr-team kinds list \| trust <kind> [--reason "…"] \| untrust <kind>` | the trust override behind gate 4; `list` prints `<kind>  delivers\|held  <flags>`; a kind also becomes `verified` on its own after 20 clean round trips |
| `team.json` → `config.gate` | `stable_ms_screen` 2000, `stable_ms_hook` 750, `stable_ms_hooks_delivery` 15000, `done_hold_ms` 60000, `min_interval_ms` 20000, `global_interval_ms` 1500, `focus_max_hold_ms` 300000, `focus_snapshot_stable_ms` 3000, `dialog_hold_cap_ms` 600000, `pair_budget` 10, `pair_window_ms` 600000, `sample_gap_reset_ms` 10000, `post_ttl_ms` 1800000, `burst_window_ms` 1000, `nudge_focused` `never\|always`; the daemon reloads it within 2 s and ignores the whole block if any key is invalid |
| `daemon start --dry-nudge` | log nudges instead of typing them (for a dry run) |

Measured in the rig with `done_hold_ms 5000`: post to the member working in
about 2 s when idle; a nudge lands about 6 s after a dialog closes; the
member's read shows about 3 s after that. With the default 60 s done-hold,
add up to a minute after the member goes idle.

**Verified**: ND-01 to ND-12 (ND-05 and ND-06 by unit tests), ND-02b (model
picker and permission dialog received no bytes), ND-04 (a real nudge
produced a reply on the board), F-01 to F-04, SK-08.

## 7. Human paths

### Console pane

`prefix+u`, `herdr-team ui console`, or the `herdr-team.console` plugin
action. Opens as a split in the current tab; a second open focuses the
existing console instead of opening another. `herdr-team ui who` opens it as
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
  text` → a role. Typing `@` (at the start or after a space) opens a name
  list above the input line: every member with role, kind, and status, then
  `role:<r>` groups, `all`, and `human`; keep typing to filter (a role or
  part of a name matches), Up/Down move, Tab or Enter insert the pick, Esc
  hides the list. The compose popup has the same list. Prefixes `/all`, `/human`, `/kind k`, `/reply N` (with no
  `@`, addressed to the author of #N), `/urgent`, `/ref path`. Commands:
  `/retract N` and `/remove name` (ask `y`/`n`); `/mute [name|all] [30s|10m|2h]`,
  `/unmute [name]`, `/pause [10m]`; `/nudge name [--force]`; `/focus name`;
  `/peek name` (the member's screen in a box, safe while it works);
  `/who` and `/charter` (boxes, Esc or `q` closes); `/filter [all|to me|
  requests|human|system]`; `/as label`; `/use team`; `/charter set [--urgent]
  text`; `/help`; `/quit`.
- **Keys**: Up/Down scroll the feed by one line, PgUp/PgDn by ten; Tab cycles
  the filter; Enter posts; Alt+Enter inserts a newline; Left/Right, Home/End,
  Ctrl-A/Ctrl-E, Backspace/Delete edit the input line; Ctrl-U clears it,
  Ctrl-K kills to the end; Esc clears the status line or closes a box;
  Ctrl-C clears a non-empty line and quits when the line is empty. Text over
  2000 characters spills to a file.
- Your post is `verified` when the console is the focused pane at Enter;
  typed while unfocused it is recorded unverified with the status `posted
  while unfocused: recorded as unverified` and still counts for nudges.
- Lifecycle: closing the console pane by hand is recorded so it is not
  reopened later; a server stop leaves it "open", and the daemon reopens it
  after the restart.

### Compose popup

`prefix+m`, `herdr-team ui compose`, or the `herdr-team.compose` action. One
line, console syntax. **Default recipient is the member whose pane you had
focused when it opened** (shown as `default @<name>` in its header); plain
text goes to `all` only when the focused pane is not a member. Esc closes;
the popup exits after a successful post and keeps the line after a refused
one; a second open while one is up fails `popup already open` and the CLI
falls back to the console. Posts from the popup are unverified and count
for nudges.

### CLI from a shell pane, and outside Herdr

| Path | Identity recorded | Nudges members? |
| --- | --- | --- |
| `herdr-team post …` from a shell pane inside Herdr | `human`, verified (ancestry reaches the pane's shell) | yes |
| From a pane where ancestry cannot be proven (tmux, screen, ssh) | `human`, `cli-unverified`, stderr says why, never refused | **no** (rendered `(unverified)`) |
| Outside Herdr: `herdr-team --team <name> post …` | `human`, `outside`, unverified; works with the server down | yes |
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
`herdr-team <team>: expired|abandoned|<name> waiting|<name> not nudged|<name>
ping-pong paused|<name> unbriefed|name conflict`. Your config has
`[ui.toast] delivery = "terminal"`, so they arrive as terminal notifications
to the foreground client; `herdr` shows an in-app toast; `off` records
`disabled`. Every attempt is written to the board as a `toast` system
record and to the attention file that `inbox --human` reads. Mute does not
suppress toasts.

**Verified**: HP-01 to HP-14 (all three delivery modes), HP-06 and HP-07,
UI-01, UI-02 (peek never makes the console look like an agent).

## 8. Herdr UI integration

| Capability | How | Notes |
| --- | --- | --- |
| Key bindings | `herdr-team keys print` → paste → `herdr server reload-config`; `keys check` reports collisions | `prefix+t` team-up, `prefix+m` compose, `prefix+u` console, `prefix+y` view toggle; all unbound in Herdr's defaults |
| Plugin actions | `herdr plugin action invoke herdr-team.<team-up\|compose\|console\|who\|toggle-view\|daemon-start>` | same entrypoints as the keys |
| Sidebar rows | `herdr-team setup --print-config` → paste the required block → reload | `$team_role` and `$team_task` per member; the optional block switches status glyphs to symbols for every agent |
| Tokens | automatic | `team`, `team_role` stay while the team exists; `team_task` is the member's `task` text (under 30 min old) or its last post headline with a kind glyph, restamped at the 30 s heartbeat when it changed, TTL 120 s: it fading is the health signal |
| Team view | `prefix+y`, or `herdr-team view on\|off\|toggle [--force]` | filters the Agents panel to the team plus any blocked agent elsewhere; refuses to replace a view another plugin owns unless `--force`; `plugin_disabled` when disabled |
| Pane labels | automatic | `team:<team>/<role>`; the key for restart recovery |
| Herdr's own commands | `herdr agent rename`, `herdr agent list` | renames are adopted; names and tokens are visible in `agent list` |

**Verified**: RS-04, RS-05, UI-04, UI-06, UI-07, UI-08, sidebar rows rendered
in the rig frame.

## 9. Claude Code hooks (optional)

`herdr-team hooks install claude [--settings p] [--hooks-dir p] [--claude-dir p]
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
- **Stop**: for **any unread post to the member or to all** (including
  system records such as `charter_updated`, excluding `nudged` and `toast`),
  Claude is held from finishing with `[herdr-team stop] N unread board
  post(s) for <name> (seq a-b). Run: herdr-team board --new, then herdr-team
  ack, then finish.`, at most three times per ten minutes; not while muted;
  never on Esc or Ctrl+C.
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
| Daemon | `herdr-team daemon start [--replace] [--allow-version] [--dry-nudge] \| stop [--timeout N] \| status`, the `daemon-start` action; the plugin's startup hook starts it on every server start; `create`, `add`, `bind`, and `ui picker` start it if needed | one instance per session; refuses a Herdr other than 0.8.x without `--allow-version`; exits when the manifest version changes or the server stays unreachable for 60 s (the startup hook brings it back); `start` also re-applies the team view and reconciles the console |
| Cold server restart | nothing to do | panes come back as shells; members show `gone` after a 30 s grace; once the agents run again the daemon rebinds each member by terminal, then label, then pane id and kind, then name, re-applies names, restamps tokens; a member that matches only by kind and directory is left `missing` with the candidate named in `daemon.log` until you `bind` it; unread posts are kept, their TTL restarts |
| Live update or handoff | nothing to do | the daemon reconnects and reconciles; a surviving daemon is kept |
| Console after restart | nothing to do | a console that was open is reopened; a dead `Team console` shell is detected by process info and replaced |
| Member exits or pane closes | nothing to do | member `missing`, its tokens cleared, `member_gone` to you; the manifest event hooks do this when the daemon is dead |
| Health | `herdr-team doctor [--no-probe] [--no-fix]` | socket, session, state dirs, plugin state, daemon ping age, toast mode and one probe, teams, warnings; may reopen the console |
| Disable | `herdr plugin disable herdr-team` | within 10 s the daemon clears tokens and the view and exits; **pane labels remain** until `teardown`, `remove`, or `dissolve` |
| Full cleanup | `herdr-team teardown` | any time: stops a live daemon, clears tokens, labels, the view, and a stale console record |
| Housekeeping | `herdr-team gc` (session trees whose socket is gone, lock free, older than 7 days), `prune --keep-days N` (rotated board segments into `_archive/`) | |
| Isolation | automatic | every team lives under its session's slug; a write to a team from a different socket is refused `team_session_mismatch` unless `--session-mismatch-ok` |

**Verified**: PK-03 to PK-10, RT-01a, RT-02, RT-05, UI-05, F-04.

## 11. Safety properties you can check

- Only the daemon types into an agent, one line at a time, only into panes
  that are in a team roster. `herdr-team notifier stats` reports
  `wrong_target`; it must stay 0.
- Only the daemon sends toasts, plus the single probe from `doctor` and
  `setup` (skip with `--no-probe`).
- Nothing is typed while a member is working, blocked, in a menu, has a
  draft, or is the pane you are looking at (until the 5 min focus hold
  expires and its screen has been still for 3 s).
- Authorship is stamped by the system from the pane and process, never
  claimed by text. `herdr-team audit` shows refusals.
- Every board line renders inside a quote under a system header; the hook
  context frames peer posts as requests; the skill says the charter and the
  member's brief are the only operator-authority text.
- Emergency stop: `herdr-team daemon stop`. Nothing is typed anywhere after
  that.

## 12. Not built or not verified yet

- Delivery verified end to end only for Claude and Codex. opencode, gemini,
  cursor-agent, kimi, agy need one `hooks probe` each (or `kinds trust`)
  before they receive nudges.
- Codex under its default sandbox cannot reach the Herdr socket from a tool
  call and asks to rerun unsandboxed; approve its read-only `herdr-team`
  commands or start it with an approval policy that allows them.
- Hooks exist for Claude only. The only briefing path is the typed line.
- No shared task list with claiming; no cross-session or cross-machine
  teams; no Windows.
- The console opens as a split in the current tab, not its own tab.
- Restarting the Herdr server restores a Claude pane with `claude --resume`
  and drops its launch flags (model, permissions, allowed tools), so hooks
  and cheap models configured on the command line are lost on restart.

## 13. Test checklist

Each row: do this, expect that.

| # | Do | Expect |
| --- | --- | --- |
| C1 | `herdr-team doctor` after linking and `daemon-start` | daemon alive, your socket, slug `default`, no errors |
| C2 | `herdr-team kinds trust claude`; `kinds list` | a `claude` row in the `delivers` column with `trusted` in the flags (`--json`: `delivers: true, trusted: true`) |
| C3 | two fresh idle agents; `prefix+t`; Space on both; Enter; team name; charter; per member role, name, brief (fields are prefilled: Ctrl-U clears before typing, Enter accepts the default); confirm | `who` lists both with roles; `herdr agent list` shows names and tokens; `herdr pane list` shows labels `team:<t>/<role>` |
| C4 | wait about a minute; `who` | the `unbriefed` tag disappears from both rows once the briefing lines landed; each screen shows the `[herdr-team briefing]` lines and the agent running `herdr-team ack` (otherwise one re-brief after 90 s, then a `<name> unbriefed` toast) |
| C5 | ask a member "what is this team for and what is your role" | it answers from `charter` and `me` with the right names |
| C6 | `herdr-team post "hello team"` from a shell pane | `board --last 1`: from `human`, to `all`, no `(unverified)`; nobody nudged |
| C7 | `herdr-team post --to <member> "reply on the board with pong"` | `who` shows `↪1`; the nudge lands after the member has been idle for the stable window plus the 60 s done-hold (lower `config.gate.done_hold_ms` to see it sooner); the member replies; `board --receipts` shows `✓nudged` and `✓read by <member>` |
| C8 | same while the member is mid-turn | `daemon.log` shows `held: not_idle (working)` (or `who --json` `.members[].hold`); it lands after the turn plus the windows |
| C9 | open the member's model picker (`/model`), post to it, wait 20 s | nothing typed; `daemon.log` shows `held: dialog (… select model …)` or `held: skip_state_update`; after Esc it lands within about 6 s |
| C10 | make a member hit a permission prompt, post to it | nothing typed; `daemon.log` `held: not_idle (blocked)` or `held: blocked`; `notifier stats` shows no intent; after you answer the dialog it lands after the windows |
| C11 | ask a member to run `herdr-team post --as human "x"` | exit 1 `author_mismatch`; `herdr-team audit` lists it; the console feed shows a warning line |
| C12 | ask a member to post to its teammate by name | the teammate shows `↪1`, is nudged after its idle windows, and replies; `board --receipts` on the request shows `nudged` and `read by <teammate>` |
| C13 | `post --to role:<r> "…"` with two holders | two `nudged` records at least 1.5 s apart; `board --json` shows `"to_role": "<r>"`; the header reads `-> role:<r> (a,b)` |
| C14 | `prefix+u`; type `@member ping`; Enter | console status `posted #N to <member>`; `board --last 1` header `human -> <member>` without `(unverified)`; the member shows `↪1` |
| C15 | in the console `/mute member 2m`; post to it | the member's row gains `muted`; `daemon.log` `held: muted (… ms left)`; `/unmute member` delivers (use `/pause 2m` to see the header countdown) |
| C16 | `retract <seq>` of a pending directed post | `daemon.log` `retract of #N cancelled the pending nudge`; a `retracted` record; the feed shows it struck with `(retracted by #M)`; `who` drops the `↪` |
| C17 | ask a member to run `herdr-team post --to human "ping"` | a terminal notification titled `#<seq> note from <member>: ping`; a `toast` record; `inbox --human` lists the post and an attention line |
| C18 | `charter set "new goal"`; then `who` | `charter_updated` on the board; members `charter: stale` until they `ack` |
| C19 | `herdr agent rename <member> <new>` | within a few seconds `who` shows the new name, a `renamed` record appears, and `post --to <old>` still works for 10 min |
| C20 | `prefix+y` | Agents panel shows only the team plus blocked agents; again restores |
| C21 | paste the sidebar block, reload; have a member run `task "…"` | `$team_role` shows at once; `$team_task` within about 30 s |
| C22 | `herdr-team daemon stop`; wait 2 min; `who` | `agent list` tokens lose `team_task`, keep `team` and `team_role`; `who` header says `notifier down` (`source: agent-list` in JSON); `daemon start` restores |
| C23 | stop and restart the Herdr server; restart the agents in their panes | `who` shows `gone <age>` after 30 s, then `active` once the agents idle; `member_restarted` records; names, labels, tokens back; an undelivered post is nudged after the normal gate (the restored Claude runs `claude --resume` without its launch flags) |
| C24 | `herdr-team hooks install claude`; start a fresh Claude member; post to it | its next session start shows the briefing context block; its next prompt shows `[herdr-team board: 1 posts from peers; …]`; at Stop a `[herdr-team stop] 1 unread board post …` block once per new seq, at most 3 per 10 min; `daemon.log` shows `held: stop_blocked` instead of a second delivery |
| C25 | `herdr plugin disable herdr-team` | tokens and the view gone within 10 s, daemon exited; pane labels remain until `teardown`; `enable` and `daemon start` recover |
| C26 | `herdr-team notifier stats` at the end | `wrong_target 0`, `open_intents 0`, a clean rate per kind; hold reasons are in `daemon.log` and `who --json`, not here |
