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
- **Attribution and an audit trail.** Peer posts arrive framed as requests,
  not orders. Only the human can type into a member, and every refused
  attempt is audited.
- **A console built for the operator.** Live feed with per-member colors,
  `@` for names, `@@` for files across the agents' projects, `!` for members,
  `?` for help, receipts (`✓nudged`, `✓read`, `✓typed`), and hold reasons.
- **Optional Claude Code hooks.** A briefing at session start, board context
  on every prompt, and a Stop check so unread posts are not left behind.

## Install

Requirements: Herdr 0.8.2 or newer, Python 3.9 or newer (standard library
only), macOS or Linux.

```bash
herdr plugin install vitalysim/herdr-team
herdr-team setup --print-config      # paste the printed [[keys.command]] block into your Herdr config
herdr server reload-config
herdr-team kinds trust claude        # once per session, for every agent kind you use
herdr-team kinds trust codex
herdr-team hooks install claude      # optional: Claude Code hooks
```

Trusting a kind is the one deliberate step: it tells the daemon the typing
path for that kind has been checked by you. Until then members of that kind
are listed but nothing is typed into them, and `add` says so.

## Quick start

1. Open two panes and start two agents.
2. `prefix+t`, Space on both, Enter, then a team name, a charter, and a role
   and name per agent. Each member is briefed once it is idle.
3. `prefix+u` opens the console. Plain text posts to the whole team,
   `@name text` to one member, `!name text` types straight into one.
4. Ask for something: `@red-dev-claude-dev post a summary of the repo layout`.
   The member is nudged when idle, reads the board, and replies; the feed
   shows `✓nudged` and `✓read`.

Default key bindings: `prefix+t` team up, `prefix+u` console, `prefix+m`
compose popup, `prefix+y` team view in the sidebar.

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
| `/nudge name`, `/mute name 10m`, `/pause`, `/focus name`, `/peek name` | delivery and pane controls |
| `/who`, `/charter`, `/charter set text`, `/use team`, `/retract N`, `/remove name` | roster, charter, teams, board |
| `?` on an empty line, `/help` | every sign, command, and key |

`@`, `@@`, and `!` open lists; Up/Down move, Tab or Enter pick, Esc hides.

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
- Every board line the agents see is quoted under a system header; the
  charter and a member's brief are the only text with operator authority.
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
turn (`!!`) is verified for Claude Code; other kinds are typed but flagged
until checked. Hooks exist for Claude Code only. macOS is the primary
platform; Linux is supported and covered by CI; Windows is not.

## Development

```bash
git clone https://github.com/vitalysim/herdr-team.git
cd herdr-team
python3 -m unittest discover -s tests        # 1168 tests, no dependencies
bin/herdr-team-sandbox start ~/your/project  # an isolated Herdr session for live testing
```

The sandbox launcher runs the plugin in a named Herdr session with its own
configuration, plugin registry, and state, so nothing it does is visible to
your default session. See [docs/development.md](docs/development.md) for the
conventions the code follows.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
