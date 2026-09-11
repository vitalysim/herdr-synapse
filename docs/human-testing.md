# Human testing guide

How to try herdr-synapse in your own Herdr session for the first time. The
complete list of capabilities, with UI and CLI paths, expected behaviour, and
a test checklist, is `capabilities.md` in this directory. Written
2026-09-10 after automated tests and a three-harness run against Herdr 0.9.0.
Everything here has run in a throwaway session; this is the first time it
meets your real one.

## The easiest way: the sandbox launcher

The plugin runs on the Herdr you already have installed (0.8.2); the fork
only holds its source. To try it without touching your real session at all,
use the sandbox: a named Herdr session with its own config directory, its own
plugin registry, and its own state, so nothing it does is visible to your
default session.

```bash
# from a terminal window that is NOT inside Herdr:
<plugin checkout>/bin/herdr-synapse-sandbox start ~/projects/<your-project>
```

`start` creates `~/herdr-synapsetest/`, copies your `config.toml` into it and
appends the plugin's key bindings and sidebar rows, trusts `claude`, `codex`
and `opencode` for delivery, links the plugin into the sandbox registry, and
launches the session; the plugin's startup hook starts the notifier. Inside
it, skip to step 4 below: open two panes, start agents, `prefix+t`.

Other subcommands: `status`, `attach`, `herdr <args>` and `team <args>` to
run commands against the sandbox from outside it, `stop`, `delete`,
`reset` (removes the sandbox directory, asks first). `HERDR_TEAM_SANDBOX` and
`HERDR_TEAM_SANDBOX_SESSION` change the directory and session name.

What the sandbox still shares with your real setup: the agents themselves
and their dotfiles (`~/.claude`, `~/.codex`, OpenCode's state directory).
Step 8 below edits
`~/.claude/settings.json`; skip it in the sandbox or point it at rig-local
files with `--settings`, `--hooks-dir`, and `--claude-dir`.

Steps 1 to 3 below are what `start` does for you; they remain here for a
manual setup in your real session later.

## Before you start

- Use **fresh panes** for the first team. Do not add an existing agent that
  is mid-task; the plan (M10) says start with two new panes and watch for a
  day before adding anything you care about.
- The daemon only ever types into panes that are in a team roster, and only
  one short line at a time. It never touches a pane outside a roster. The
  one thing that types *now*, without waiting for idle, is your own
  `!name text` in the console; nothing an agent does can trigger that. If
  you see anything else, run `herdr-synapse daemon stop` and tell me.
- Everything the plugin writes lives under
  `~/.local/state/herdr/plugins/herdr-synapse/` plus a pointer file under
  `~/.config/herdr/plugins/config/herdr-synapse/`. Nothing else in `~/.config`
  or `~/.claude` is touched unless you run `hooks install` (step 8).

## 1. Link the plugin (once)

```bash
herdr plugin link <plugin checkout>
herdr plugin list --json | jq '.result.plugins[] | {plugin_id, enabled, warnings}'
herdr plugin action invoke herdr-synapse.daemon-start
<plugin checkout>/bin/herdr-synapse daemon status --json
<plugin checkout>/bin/herdr-synapse install-cli --yes   # puts herdr-synapse on PATH via ~/.local/bin
herdr-synapse doctor
```

`doctor` should show `alive: true` for the daemon, your socket, slug
`default`, and `toast_delivery: terminal` (your config). Fix anything it
lists under `warnings` or `errors` first.

## 2. Trust the agent kinds you will use (once per session)

A fresh session delivers nothing until a kind is trusted; this is the gate
that protects you from the plugin typing into an agent kind that was never
verified. The live run verified the three supported kinds, so:

```bash
herdr-synapse kinds trust claude
herdr-synapse kinds trust codex
herdr-synapse kinds trust opencode
herdr-synapse kinds list
```

## 3. Keys and sidebar rows (once, optional but recommended)

The sidebar block colour-codes teams: each team's name renders in its own
colour next to its members. Re-paste it after an upgrade if `herdr-synapse
doctor` says your rows predate the colours.

```bash
herdr-synapse keys print          # six entries: prefix+t/m/u/y/i/f for teams, compose, console, view, usage, knowledge
herdr-synapse setup --print-config   # the required sidebar rows ($team_role, $team_task) and the optional block
```

Paste both into `~/.config/herdr/config.toml`, then `herdr server
reload-config`. `herdr-synapse keys check` confirms nothing collides with your
own bindings. The defaults never use `prefix+t/m/u/y/i/f`.

## 4. Make a team

Open fresh panes in a Space, start agents (`claude`, `codex`, `opencode`,
or `herdr agent start <name> --kind <kind> --pane <id>`), let them reach
idle, then either:

- **UI** (later, to add an agent to a team that exists): `prefix+t`, Space on the new agent, Enter, then type `1` for `add it to team <t>` (the last number creates a new team instead), then its role, name, and required Mission / brief; the confirm screen says `Add 1 agent to team <t>?`; afterwards every other member is nudged that `<name> joined team <t>` and the newcomer is briefed.
- **UI**: `prefix+t`, Space on the two rows, Enter, team name, charter, optional team rules, team folder, then per member a role, a name, a required Mission / brief and an optional model; confirm.
- **CLI**: `herdr-synapse create demo --charter "Try the team board end to end" --member <pane1>:reviewer --member <pane2>:worker --brief reviewer="Review the work." --brief worker="Implement the work."`.

Each member gets a one-line briefing typed into its input box once it is
idle, then reads the skill, the charter, and the board, and acknowledges.
`herdr-synapse who` shows `briefed` for each within about a minute.

Reopen `prefix+t` afterwards and it shows the team with its agents under it.
Enter on a member opens its actions: rename it, change its goal, send that
goal to it, remove it from the team, or jump to its pane. Changing a goal
only writes it to the roster; the agent sees it when you choose "send the
goal to it now", which needs the notifier.

## 5. Watch it work

```bash
herdr-synapse who                      # roster, status, headline, receipts
herdr-synapse board --last 20          # the board
prefix+u                            # the console: charter, roster, live board tail, input line
```

In the console: plain text goes to the whole team, `@name text` to one
member (that member is nudged once idle), `!name text` straight into that
member's input box right now (`!!name text` even while it works; the entry
shows `✓typed` or why not), `@@path` attaches a file to the post (type
`@@` for a file list), `?` on an empty line shows every command, `@role:worker text` to a role,
`/human` to yourself, `/urgent` before text nudges everyone, `/reply N`,
`/retract N`, `/mute name 10m`, `/peek name`, `/focus name`, `/charter`,
`/charter set`, `/use team`, `/quit`. `/interrupt @name text` is an
interrupt: urgent, and typed into that member's running turn when its kind
allows it (Claude Code, Codex and OpenCode by default;
`/interrupts off` stops it). Agents have the same with
`herdr-synapse post --to <name> --interrupt`, once per teammate per 10 min.

`prefix+i` opens the usage popup: every agent in the session grouped by the
provider account it draws on, with session and weekly bars and reset times
(Claude Code and Codex logins read live; OpenCode depends on its provider and
OpenCode Zen exposes billing rather than a quota; `r` refreshes, `q` closes).

Ask a member to do something through the board, for example
`@demo-worker post a one-line summary of the repo layout`, and watch:
`who` shows the pending nudge, the nudge line lands only when the member is
idle, the member reads the board and replies, `board --receipts` shows
`nudged` and `read`.

## 6. Things that are expected, not bugs

- Nothing lands while a member is working, blocked at a dialog, or in the
  model picker. That is the point. `!name text` is the exception you control:
  it types now, but a dialog, the model picker, or a draft still refuses it
  (`✗ not typed (dialog)`), and only `!!` types into a working member.
- A teammate's `post --interrupt` types its notice into a working Claude Code,
  Codex or OpenCode member (the same queue behaviour `!!` uses). Other kinds
  wait for idle unless you explicitly add them to `interrupts`.
- `!name text` works only from the console pane. The compose popup answers
  `direct typing is console-only` and a shell pane `say_unverified`, because
  neither can prove it is you.
- Your posts to the whole team nudge every member once each is idle. An
  agent's post to the whole team is not nudged; members see it on their next
  board read (Claude with hooks sees it on its next turn) unless it is `/urgent`.
- Your posts from a shell pane inside Herdr are `verified`; from outside
  Herdr or from the compose popup they are `unverified`; from inside an
  agent's pane `--as human` is refused and audited. `herdr-synapse audit` lists
  refusals.
- Toasts: your config is `delivery = "terminal"`, so posts addressed to you
  arrive as terminal notifications, and the console badge is the reliable
  signal.
- Restarting the Herdr server drops agent names and tokens; the daemon
  restores them within seconds once the agents are running again, using the
  pane labels it set.

## 7. If something goes wrong

```bash
herdr-synapse daemon stop              # nothing is typed anywhere after this
herdr-synapse doctor                   # state, socket, daemon, config
herdr-synapse notifier stats           # what was delivered, held, and why
tail -50 ~/.local/state/herdr/plugins/herdr-synapse/sessions/default/daemon.log
herdr plugin disable herdr-synapse     # clears tokens and the view within 10 s, daemon exits
```

## 8. Claude hooks (optional, edits ~/.claude/settings.json)

`herdr-synapse hooks install claude` adds three hook entries in their own
objects so Herdr's own installer leaves them alone (verified both ways).
Members started after that get new board posts in context on every turn and
are held from stopping while a directed post is unread, capped at three
times per ten minutes. `herdr-synapse hooks uninstall claude` removes exactly
those entries.

## Known gaps

- Claude Code, Codex and OpenCode are verified end to end. Other kinds
  (Gemini, Cursor Agent, Kimi, Antigravity and the rest) need one probe each:
  `herdr-synapse hooks probe <kind> --member <name>` runs one round trip and
  records the result.
- Hooks exist only for Claude Code. Codex and OpenCode use typed briefings.
- OpenCode 1.18.30 crashes when its full TUI starts in a very narrow terminal;
  Synapse refuses clear before exit when the pane layout is under 38 columns.
- The console opens as a split in the current tab, not as its own tab.
- `herdr-synapse read <name>` shows only the visible screen of a member.
- Fresh Claude Code, Codex and OpenCode agents spawned by Synapse run unrestricted by default. A manually started agent added to a team keeps its existing permission mode until a Synapse-managed resume or restart.
