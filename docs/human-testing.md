# Human testing guide

How to try herdr-team in your own Herdr session for the first time. The
complete list of capabilities, with UI and CLI paths, expected behaviour, and
a test checklist, is `capabilities.md` in this directory. Written
2026-09-05 after four rig runs against Herdr 0.8.2. Everything here has run
in a throwaway session; this is the first time it meets your real one.

## The easiest way: the sandbox launcher

The plugin runs on the Herdr you already have installed (0.8.2); the fork
only holds its source. To try it without touching your real session at all,
use the sandbox: a named Herdr session with its own config directory, its own
plugin registry, and its own state, so nothing it does is visible to your
default session.

```bash
# from a terminal window that is NOT inside Herdr:
~/MyPlace/projects/herdr-fork/plugins/herdr-team/bin/herdr-team-sandbox start ~/MyPlace/projects/<your-project>
```

`start` creates `~/herdr-teamtest/`, copies your `config.toml` into it and
appends the plugin's key bindings and sidebar rows, trusts `claude` and
`codex` for delivery, links the plugin into the sandbox registry, and
launches the session; the plugin's startup hook starts the notifier. Inside
it, skip to step 4 below: open two panes, start agents, `prefix+t`.

Other subcommands: `status`, `attach`, `herdr <args>` and `team <args>` to
run commands against the sandbox from outside it, `stop`, `delete`,
`reset` (removes the sandbox directory, asks first). `HERDR_TEAM_SANDBOX` and
`HERDR_TEAM_SANDBOX_SESSION` change the directory and session name.

What the sandbox still shares with your real setup: the agents themselves
and their dotfiles (`~/.claude`, `~/.codex`). Step 8 below edits
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
  you see anything else, run `herdr-team daemon stop` and tell me.
- Everything the plugin writes lives under
  `~/.local/state/herdr/plugins/herdr-team/` plus a pointer file under
  `~/.config/herdr/plugins/config/herdr-team/`. Nothing else in `~/.config`
  or `~/.claude` is touched unless you run `hooks install` (step 8).

## 1. Link the plugin (once)

```bash
herdr plugin link ~/MyPlace/projects/herdr-fork/plugins/herdr-team
herdr plugin list --json | jq '.result.plugins[] | {plugin_id, enabled, warnings}'
herdr plugin action invoke herdr-team.daemon-start
~/MyPlace/projects/herdr-fork/plugins/herdr-team/bin/herdr-team daemon status --json
~/MyPlace/projects/herdr-fork/plugins/herdr-team/bin/herdr-team install-cli --yes   # puts herdr-team on PATH via ~/.local/bin
herdr-team doctor
```

`doctor` should show `alive: true` for the daemon, your socket, slug
`default`, and `toast_delivery: terminal` (your config). Fix anything it
lists under `warnings` or `errors` first.

## 2. Trust the agent kinds you will use (once per session)

A fresh session delivers nothing until a kind is trusted; this is the gate
that protects you from the plugin typing into an agent kind that was never
verified. Your rig runs verified Claude and Codex, so:

```bash
herdr-team kinds trust claude
herdr-team kinds trust codex
herdr-team kinds list
```

## 3. Keys and sidebar rows (once, optional but recommended)

```bash
herdr-team keys print          # four [[keys.command]] entries: prefix+t team-up, prefix+m compose, prefix+u console, prefix+y view
herdr-team setup --print-config   # the required sidebar rows ($team_role, $team_task) and the optional block
```

Paste both into `~/.config/herdr/config.toml`, then `herdr server
reload-config`. `herdr-team keys check` confirms nothing collides with your
own bindings. The defaults never use `prefix+t/m/u/y`.

## 4. Make a team

Open two fresh panes in a Space, start an agent in each (`claude`, `codex`,
or `herdr agent start <name> --kind <kind> --pane <id>`), let them reach
idle, then either:

- **UI** (later, to add an agent to a team that exists: `prefix+t`, Space on
  the new agent, Enter, then type `1` for `add it to team <t>` (the last
  number creates a new team instead), then its role, name, and brief; the
  confirm screen says `Add 1 agent to team <t>?`; afterwards every other
  member is nudged that `<name> joined team <t>` and the newcomer is briefed)
- **UI**: `prefix+t`, Space on the two rows, Enter, team name, charter, then
  per member a role, a name, an optional brief, confirm; or
- **CLI**: `herdr-team create demo --charter "Try the team board end to end" --member <pane1>:reviewer --member <pane2>:worker`.

Each member gets a one-line briefing typed into its input box once it is
idle, then reads the skill, the charter, and the board, and acknowledges.
`herdr-team who` shows `briefed` for each within about a minute.

## 5. Watch it work

```bash
herdr-team who                      # roster, status, headline, receipts
herdr-team board --last 20          # the board
prefix+u                            # the console: charter, roster, live board tail, input line
```

In the console: plain text goes to the whole team, `@name text` to one
member (that member is nudged once idle), `!name text` straight into that
member's input box right now (`!!name text` even while it works; the entry
shows `✓typed` or why not), `@@path` attaches a file to the post (type
`@@` for a file list), `?` on an empty line shows every command, `@role:worker text` to a role,
`/human` to yourself, `/urgent` before text nudges everyone, `/reply N`,
`/retract N`, `/mute name 10m`, `/peek name`, `/focus name`, `/charter`,
`/charter set`, `/use team`, `/quit`.

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
- `!name text` works only from the console pane. The compose popup answers
  `direct typing is console-only` and a shell pane `say_unverified`, because
  neither can prove it is you.
- Your posts to the whole team nudge every member once each is idle. An
  agent's post to the whole team is not nudged; members see it on their next
  board read (Claude with hooks sees it on its next turn) unless it is `/urgent`.
- Your posts from a shell pane inside Herdr are `verified`; from outside
  Herdr or from the compose popup they are `unverified`; from inside an
  agent's pane `--as human` is refused and audited. `herdr-team audit` lists
  refusals.
- Toasts: your config is `delivery = "terminal"`, so posts addressed to you
  arrive as terminal notifications, and the console badge is the reliable
  signal.
- Restarting the Herdr server drops agent names and tokens; the daemon
  restores them within seconds once the agents are running again, using the
  pane labels it set.

## 7. If something goes wrong

```bash
herdr-team daemon stop              # nothing is typed anywhere after this
herdr-team doctor                   # state, socket, daemon, config
herdr-team notifier stats           # what was delivered, held, and why
tail -50 ~/.local/state/herdr/plugins/herdr-team/sessions/default/daemon.log
herdr plugin disable herdr-team     # clears tokens and the view within 10 s, daemon exits
```

## 8. Claude hooks (optional, edits ~/.claude/settings.json)

`herdr-team hooks install claude` adds three hook entries in their own
objects so Herdr's own installer leaves them alone (verified both ways).
Members started after that get new board posts in context on every turn and
are held from stopping while a directed post is unread, capped at three
times per ten minutes. `herdr-team hooks uninstall claude` removes exactly
those entries.

## Known gaps

- Only Claude and Codex have been verified end to end. Other kinds
  (opencode, gemini, cursor-agent, kimi, agy) need one probe each:
  `herdr-team hooks probe <kind> --member <name>` runs one round trip and
  records the result.
- The console opens as a split in the current tab, not as its own tab.
- `herdr-team read <name>` shows only the visible screen of a member.
- Codex under its default sandbox cannot reach the Herdr socket from a tool
  call and asks to rerun unsandboxed; approve read-only `herdr-team`
  commands when it asks, or start it with an approval policy that allows
  them.
