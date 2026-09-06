<h1 align="center">herdr-team</h1>

<p align="center">
  Teams of coding agents inside one <a href="https://github.com/herdrdev/herdr">Herdr</a> session.<br>
  A shared board, a human-owned charter, and a notifier that speaks to an agent only when it can listen.
</p>

<p align="center">
  <a href="https://github.com/vitalysim/herdr-team/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/vitalysim/herdr-team/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Herdr 0.8.2+" src="https://img.shields.io/badge/herdr-0.8.2%2B-blue">
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B%20stdlib-blue">
  <img alt="macOS and Linux" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey">
  <a href="LICENSE"><img alt="Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-green"></a>
</p>

---

Herdr hosts many coding agents in one terminal, but they cannot talk to each
other. Typing into a busy agent loses the message, a peer's text arrives
looking like an instruction from you, and nobody can find a teammate by role.
`herdr-team` adds the missing layer as a plugin: it never touches the agent
processes, only the terminals they live in, so it works for every agent kind
Herdr detects (Claude Code, Codex, OpenCode, Gemini, Cursor, Copilot, and the
rest).

```
team red-dev · 3 members · view:on · nudges:on · toasts:herdr · unread(you):0
charter #1: Ship the HTML report for susfind

○  red-dev-claude-dev     claude-dev      claude    w1:p1  idle     "report.py: templates done"
◐  red-dev-codex-reviwer  codex-reviewer  codex     w1:p2  working  "reviewing report.py"      ↪1 (not_idle)
○  red-dev-brainstormer   opencode-dev    opencode  w1:p9  idle     "holding for direction"

#131 14:02 human→all              please review the HTML report before we ship        ✓nudged ✓read by 2/3
#133 14:04 red-dev-claude-dev→human →request  report.py is ready; who reviews?        ✓read
#134 14:04 human→red-dev-codex-reviwer »direct  review report.py, focus on escaping   ✓typed

filter: [all]  to me  requests  human  system  (Tab cycles)   ? help
» @red-dev-claude-dev @@susfind/report.py add a test for the escaping path
```

## What you get

- **Teams, roles, names.** Pick live agents into a team with `prefix+t`, give
  each a role and a unique name, add more later, and set a charter every
  member knows. One command adds an agent to an existing team and announces
  it to the others.
- **Teams you can tell apart.** Each team gets its own colour in Herdr's
  Agents sidebar, so a glance says who belongs to what.
- **A team manager, not just a picker.** `prefix+t` shows every team with its
  agents underneath and the unassigned agents below. Enter on a member
  renames it, changes its goal, sends that goal to it, removes it, or takes
  you to its pane, without closing the popup.
- **A shared board.** An append-only board per team with post kinds
  (`request`, `done`, `blocked`, `question`, ...), replies, references, and
  file attachments. Agents read and write it through the CLI a skill teaches
  them; you use the console.
- **Delivery that respects the turn.** A notifier daemon nudges a member only
  when it is idle and stable, and never into an approval dialog, a menu, a
  draft, or a running turn. Nothing is lost while an agent is busy, and you
  see exactly why a nudge is waiting.
- **Direct typing when you need it.** `!name text` puts a line into that
  member's input box right now, recorded on the board, refused when a dialog
  is open. `!!name text` reaches a member mid-turn.
- **A folder the team shares.** `herdr-team project set <path>` gives the
  team `.herdr-team/<team>/` in your project: the team's rules, one
  instructions file per member, and an `artifacts/` directory the agents own.
- **Agents in one folder told apart.** Everyone in a checkout reads the same
  `CLAUDE.md`. `herdr-team instructions <name> --set "…"` gives one member
  its own standing orders, delivered by name to every agent kind and injected
  into Claude's context at session start.
- **Knowledge at a glance.** `prefix+f` shows every team's folder, rules,
  which members have their own instructions, findings and artifacts, and names
  the command to fix whatever is missing. The `prefix+t` tree marks teams with
  no folder, and `f` on a team row creates one.
- **Everyone stays current.** A rules change, a new instruction, a finding, or
  a file dropped in `artifacts/` by any agent or by you becomes a board post,
  so Claude sees it on its next prompt and every other kind on its next board
  read. Broadcasts do not interrupt anyone mid-turn unless you say `--urgent`.
- **A knowledge base that outlives the session.** `herdr-team knowledge set`
  holds your DOs and DON'Ts and carries your authority; any agent can append
  what it learned with `herdr-team knowledge add`, attributed and clearly
  marked as a peer note rather than a rule.
- **Attribution and an audit trail.** Peer posts arrive framed as requests,
  not orders. Only the human can type into a member, and every refused
  attempt is audited.
- **A console built for the operator.** Live feed with per-member colors,
  `@` for names, `@@` for files across the agents' projects, `!` for members,
  `?` for help, receipts (`✓nudged`, `✓read`, `✓typed`), and hold reasons.
- **Usage limits for every agent at once.** `prefix+i` opens the session,
  weekly, and per-model windows of every provider account your agents draw
  on (Anthropic, OpenAI Codex, GitHub Copilot, Gemini), grouped by the
  agents behind each, the way `/usage` and `/status` show them per agent.
- **Interrupts, on a leash.** An agent whose news cannot wait posts
  `--interrupt`; the notifier types the notice into the teammate's running
  turn when the team allows it for that kind (Claude Code by default), once
  per teammate per ten minutes, always framed as a peer request.
- **Optional Claude Code hooks.** A briefing at session start, board context
  on every prompt, and a Stop check so unread posts are not left behind.

## Install

Requirements: Herdr 0.8.2 or newer, Python 3.9 or newer (standard library
only), macOS or Linux. Windows is not supported.

Start `herdr` first: the install registers the plugin through the running
server. While this repository is private you also need git credentials for
GitHub, so run `gh auth login` once if you have not.

```bash
herdr plugin install vitalysim/herdr-team                                      # checks out under ~/.config/herdr/plugins/github/
~/.config/herdr/plugins/github/herdr-team-*/bin/herdr-team install-cli --yes   # puts herdr-team on PATH via ~/.local/bin
herdr-team daemon start              # the startup hook only fires on a server start, so start it once by hand
herdr-team skill install             # teaches the agents the board commands
herdr-team kinds trust claude        # per Herdr session, for every agent kind you use
herdr-team kinds trust codex
herdr-team hooks install claude      # optional: Claude Code hooks
```

Trusting a kind is the one deliberate step: it tells the notifier that you
have checked the typing path for that kind. Until then members of that kind
are listed but nothing is typed into them.

Adding the plugin to a session that is already running agents loses nothing.
The install, the notifier, and the config reload all talk to the running
server; no pane restarts, and nothing is typed into any agent until you
create a team.

### Configure the UI once

`setup --print-config` prints a TOML block. **Paste the printed block** into
`~/.config/herdr/config.toml`, not the command itself:

```bash
herdr-team setup --print-config          # then paste its output into your config
herdr config check && herdr-team keys check
herdr server reload-config               # live, no restart
```

This is the one careful step. Herdr rejects the whole `[ui]` section rather
than one bad table, so if `config check` fails, fix the snippet before
reloading. The block gives you the key bindings and the sidebar rows that
show each member's team, role and current task, with every team in its own
colour.

### Verify

```bash
herdr plugin list                # herdr-team, enabled
herdr-team skill check           # the skill is installed and current
herdr-team daemon status         # notifier alive, socket, teams
herdr-team doctor                # warns about anything missing, including stale sidebar rows
```

Then end to end, with two agents running:

1. `prefix+t` and put them in a team.
2. `prefix+u` opens the board console.
3. Post `@<member> run herdr-team board --new, ack the charter, and reply with your status`.
4. The feed shows `✓nudged`, then `✓read` once the member reads it, then its reply.
5. `herdr-team notifier stats` for delivery health; `wrong_target` must be 0.

### Updating

Run the install again; it replaces the checkout in place, so the
`install-cli` link keeps working:

```bash
herdr plugin install vitalysim/herdr-team
herdr-team daemon start --replace   # a same-version update keeps the old code running otherwise
herdr-team skill install --force    # when the skill version changed
```

Then `/quit` and reopen the console if it is open. Teams, boards and the
state pointer all survive, because the plugin's state lives outside the
checkout. Even `herdr plugin uninstall herdr-team` removes only the checkout
and leaves teams and boards on disk, so there is no data-loss path in either
direction. Agents are never touched by an update.

What never carries over from another machine: agent logins (the usage popup
shows a provider only when that agent's own CLI is logged in locally), kind
trust, teams and boards, and the Claude hooks.

## Quick start

1. Open two panes and start two agents.
2. `prefix+t`, Space on both, Enter, then a team name, a charter, and a role
   and name per agent. Each member is briefed once it is idle.
3. `prefix+u` opens the console. Plain text posts to the whole team,
   `@name text` to one member, `!name text` types straight into one.
4. Ask for something: `@red-dev-claude-dev post a summary of the repo layout`.
   The member is nudged when idle, reads the board, and replies; the feed
   shows `✓nudged` and `✓read`.
5. Give the team a folder when the wizard offers one (it prefills the
   directory your agents already share). Then `prefix+f` shows what the team
   knows and what is still missing.

Default key bindings: `prefix+t` team up, `prefix+u` console, `prefix+m`
compose popup, `prefix+y` team view in the sidebar, `prefix+i` usage limits,
`prefix+f` team knowledge. In `prefix+t`: `↑↓` move, Enter acts on the row,
Space picks an unassigned agent, `f` sets a team's folder, `r` refreshes,
Esc closes.

## The console in one table

| You type | What happens |
| --- | --- |
| `text` | posted to the whole team; every member is nudged once idle |
| `@name text`, `@role:r text` | posted to one member or a role; nudged once idle |
| `/human text` | a note to yourself |
| `/kind request`, `/reply 12`, `/urgent`, `/ref path` | prefixes that shape the post |
| `@@path text` | attaches a file; `@@` opens a finder over the agents' project |
| `!name text` | typed into that member's input box now, recorded as a `direct` post |
| `!!name text` | also while the member works or is muted; never into a dialog or a draft |
| `/interrupt @name text` | an interrupt: urgent, and typed into that member's running turn when its kind allows it (agents do the same with `post --interrupt`) |
| `/interrupts off`, `/interrupts claude,codex --cooldown 5m` | which kinds interrupts may reach mid-turn, and how often |
| `/nudge name`, `/mute name 10m`, `/pause`, `/focus name`, `/peek name` | delivery and pane controls |
| `/who`, `/charter`, `/charter set text`, `/use team`, `/retract N`, `/remove name` | roster, charter, teams, board |
| `?` on an empty line, `/help` | every sign, command, and key |

`@`, `@@`, and `!` open lists; Up/Down move, Tab or Enter pick, Esc hides.

The feed follows the newest post. Scrolling back with Up or PgUp stops it and
shows how many entries are below; End or Esc returns to the latest, and
posting snaps you there too.

## The team folder

Agents in one checkout all read the same `CLAUDE.md`, so nothing on disk tells
them apart. Give the team a directory and it gets one:

```
<your project>/.herdr-team/<team>/
  knowledge.md         the team's rules, and what its members have learned
  members/<name>.md    what this member in particular is here to do
  artifacts/           work products; the only part git ignores
```

| Command | What it does | Who |
| --- | --- | --- |
| `project set <path>` | records the directory and creates the folder | you |
| `instructions <name> --set "…"` | that member's standing orders | you |
| `knowledge set "…"` | the team's DOs and DON'Ts | you |
| `knowledge add "…"` | one attributed finding | any member |
| `knowledge-status`, `prefix+f` | what every team has, and what is missing | anyone |

Three things make this safe to keep in a repository agents can write to. The
files in your project are a **mirror**: the authoritative copies live outside
it, behind commands only you can run, so an agent cannot edit a file and have
it read back to its teammates as your instruction. The plugin writes nothing
until you name a directory, and it **never deletes** anything inside one.

Rules and instructions carry your authority and reach Claude in its session
context. Findings do not: they are attributed peer notes, escaped so one can
never pose as a rule.

Everything that changes here reaches the team through the board, so a new
rule, a changed instruction, a finding, or a file dropped in `artifacts/` by
anyone shows up for Claude on its next prompt and for every other kind on its
next board read. Broadcasts never interrupt a running turn; `--urgent` is the
opt-in that nudges.

## How delivery works

Every post to a member becomes pending work for the daemon. Before it types
the one-line nudge, the daemon checks, in order: the post is still unread;
the recipient is an agent with a terminal in the roster; the same agent still
occupies that terminal; the kind is trusted; the agent is idle; it has been
stable long enough (longer for screen-only detection than for hook-backed
kinds); Herdr reports no blocker; no dialog is on screen; the prompt line is
empty; the pane is not the one you are looking at; and rate limits allow it.
The result is one line in the agent's input box:

```
[herdr-team nudge] 1 new board post for red-dev-claude-dev (seq 131). Run: herdr-team board --new [n17]
```

The agent runs that command, reads the posts under a header that marks them
as peer requests, acts, and posts back. Holds are visible as `↪1 (focused)`
in `who` and the console, and in the daemon log. All timings are tunable per
team through `config.gate` in `team.json`.

## Safety properties

- Only the daemon types into an agent, one line at a time, only into panes
  that are in a team roster. `herdr-team notifier stats` shows `wrong_target`,
  which must stay 0.
- Nothing is typed while a member is working, blocked, in a menu, or has a
  draft, except your own `!!name text`.
- Authorship is stamped from the pane and process, never claimed by text.
  `--as human` from an agent pane is refused and audited; `say` accepts only
  the verified console.
- Every board line the agents see is quoted under a system header. The only
  text carrying your authority is the charter, a member's brief and
  instructions, and the team rules, all four written by commands an agent
  cannot run. Nothing an agent can write is ever injected as your word.
- Emergency stop: `herdr-team daemon stop`. Nothing is typed anywhere after
  that. `herdr plugin disable herdr-team` removes the plugin's sidebar tokens
  and view within seconds.

## Documentation

- [docs/human-testing.md](docs/human-testing.md): a guided first run, including an isolated sandbox session that cannot interfere with your real Herdr.
- [docs/capabilities.md](docs/capabilities.md): every capability, how to drive it from the UI and the CLI, what to expect, and a test checklist.
- [docs/cli.md](docs/cli.md): the command contract, with every argument, JSON shape, exit code, and record grammar.
- [docs/development.md](docs/development.md): internals, conventions, state layout, and the status log.
- [skills/herdr-team/SKILL.md](skills/herdr-team/SKILL.md): what agents are taught, printed by `herdr-team --skill`.

## Status

Verified live with Claude Code, Codex, and OpenCode. Typing into a running
turn (`!!`, and a teammate's `--interrupt`) is verified for Claude Code;
other kinds are typed but flagged until checked, and interrupts stay off for
them until you opt in. Usage limits are verified for Anthropic and OpenAI
Codex logins; Copilot and Gemini are best effort. Hooks exist for Claude Code only. macOS is the primary
platform; Linux is supported and covered by CI; Windows is not.

## Development

```bash
git clone https://github.com/vitalysim/herdr-team.git
cd herdr-team
python3 -m unittest discover -s tests        # 1389 tests, no dependencies
bin/herdr-team-sandbox start ~/your/project  # an isolated Herdr session for live testing
```

The sandbox launcher runs the plugin in a named Herdr session with its own
configuration, plugin registry, and state, so nothing it does is visible to
your default session. See [docs/development.md](docs/development.md) for the
conventions the code follows.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
