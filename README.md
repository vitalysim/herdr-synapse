<p align="center">
  <img src="docs/banner.svg" alt="Herdr Synapse - the coordination layer for every coding agent" width="100%">
</p>

<p align="center">
  Teams of coding agents inside one <a href="https://github.com/herdrdev/herdr">Herdr</a> session.<br>
  A shared board, a human-owned charter, and a notifier that speaks to an agent only when it can listen.
</p>

<p align="center">
  <a href="https://github.com/vitalysim/herdr-synapse/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/vitalysim/herdr-synapse/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Herdr 0.8.2–0.9.x" src="https://img.shields.io/badge/herdr-0.8.2%E2%80%930.9.x-blue">
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B%20stdlib-blue">
  <img alt="macOS and Linux" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey">
  <a href="LICENSE"><img alt="Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-green"></a>
</p>

---

Herdr hosts many coding agents in one terminal, but they cannot talk to each
other. Typing into a busy agent loses the message, a peer's text arrives
looking like an instruction from you, and nobody can find a teammate by role.
`herdr-synapse` adds the missing layer as a plugin. Its board and delivery
transport use Herdr's terminal and session APIs instead of patching any agent
harness. The full core workflow is live-verified with Claude Code, Codex and
OpenCode; other Herdr agent kinds stay blocked until you explicitly probe or
trust them.

> [!CAUTION]
> Synapse-managed Claude Code, Codex and OpenCode launches run unrestricted by default: `--dangerously-skip-permissions`, `--dangerously-bypass-approvals-and-sandbox` and `--auto`, respectively. Fresh spawns, exact-session resumes, controlled model restarts and OpenCode clear restarts all carry that default, so these agents can execute commands and change files without approval prompts. Use trusted repositories or an external sandbox; unrestricted execution does not grant Synapse operator authority.

```
team red-dev · 3 members · view:on · nudges:on · toasts:herdr · unread(you):0
charter #1: Ship the HTML report for susfind
runtime: safe !:ready · Herdr 0.9.0/p22 · Synapse 0.16.0 · daemon 0.16.0

○  red-dev-claude-dev     claude-dev      claude    w1:p1  idle     "report.py: templates done"   manager  model opus@medium
◐  red-dev-codex-reviewer codex-reviewer  codex     w1:p2  working  "reviewing report.py"         ↪1 (not_idle)
○  red-dev-brainstormer   opencode-dev    opencode  w1:p9  idle     "holding for direction"

#131 14:02 human→all              please review the HTML report before we ship        ✓nudged ✓read by 2/3
#133 14:04 red-dev-claude-dev→human →request  report.py is ready; who reviews?        ✓read
#134 14:04 human→red-dev-codex-reviewer »direct  review report.py, focus on escaping   ✓typed
#135 14:06 ⇄ blue-ops/blue-ops-manager→red-dev-claude-dev →request  can red-dev share the escaping test?   ✓nudged ✓read

filter: [all]  to me  requests  human  system  teams  team  (Tab cycles)   ? help
» @red-dev-claude-dev @@susfind/report.py add a test for the escaping path
```

## What you get

**Teams**

- **Teams, roles, names.** Pick live agents into a team with `prefix+t`, or
  spawn fresh ones into new panes, give each a role, a unique name and a required Mission,
  choose a model on supported harnesses, and set a charter every member knows.
  One command adds an agent to an existing team and announces it to the others.
  Herdr's Agents sidebar gives each team one of six stable colours (the palette
  repeats after six teams).
- **Space-aware teams, a board per team.** When exactly one team's agents are
  active in a space, `prefix+u` opens that team's board; otherwise the session
  default is used, and `--team` always overrides. Open several boards side by
  side, each pinned to its team.
- **Members that survive restarts.** A member is tied to the conversation its
  agent is running, not just to a pane, so two agents of one kind in one
  checkout are never mixed up after a restart, and an agent that crashed and
  came back is briefed again instead of silently wearing a member's name.
  `herdr-synapse resume <name>` reopens a member's own conversation.
- **A model and an effort for supported harnesses.** Say `opus@medium` for one agent and
  `gpt-5.6-luna@high` for another when you create the team, or change it
  later from the CLI, the console, or the teams view. Claude switches live;
  OpenCode effort can switch live; Codex changes and OpenCode model changes
  apply at their next resume, or now with a restart that keeps their session.
  `who` shows what was asked for beside what the harness reports.

**Coordination**

- **A shared board.** An append-only board per team with post kinds
  (`request`, `done`, `blocked`, `question`, …), replies, references, and
  file attachments. Agents read and write it through the CLI a skill teaches
  them; you use the console.
- **Delivery that respects the turn.** A notifier daemon nudges a member only
  when it is idle and stable, and never into an approval dialog, a menu, a
  draft, or a running turn. Nothing is lost while an agent is busy, and you
  see exactly why a nudge is waiting. `!name text` submits only if the same
  member is still idle at the instant Herdr queues it; `!!name text`
  deliberately reaches a running turn, and `!!all text` does the same for
  every current agent; a teammate's `post --interrupt` may do the same where
  you allow it.
- **A manager, when you want one.** Mark one member and the others are told it
  coordinates: `who` tags it, every briefing names it, the teams view marks it
  with `★`, and the skill tells members to take its assignments as the plan
  unless those conflict with the charter or their own instructions. Like a
  human broadcast, its broadcasts queue every teammate for an idle-gated
  nudge; an ordinary peer broadcast waits for the next read or catch-up sweep.
- **Teams that talk to each other.** Link two teams and their managers become
  each other's endpoint: `post --to team:<other>` (console: `/team <other>
  text`) lands on both boards, the receiving manager is nudged, replies thread
  across, and a read receipt comes back. `/filter teams` is the inter-team
  lens; `c` in the teams view makes and breaks links.
- **Agents that need you get you.** A question to the operator opens a popup
  you answer in and, when blocking asks are enabled, the agent waits instead
  of guessing or being talked past by a peer. Long-running shell behaviour is
  harness-dependent and is called out in the matrix below. `Ctrl-A`
  acknowledges without deciding, and says that it is not a decision.

**Authority and knowledge**

- **Authority that stays yours, until you lend it.** The charter, the team
  rules and each member's instructions are the operator's. Which caller counts
  as the operator is decided by the process tree and a verified origin, never
  by a name or an environment variable an agent could set. `herdr-synapse
  operator grant <name>` lends that authority to an agent, with an expiry, a
  board announcement, and an audit line on every use.
- **Instructions you actually edit.** Every member gets a document with the
  same six sections: mission, scope, constraints, definition of done,
  handoffs, and notes you keep private. Edit the file in your repo, and one
  command shows the diff and applies it. Claude receives the change through
  its next hook; other trusted kinds are nudged to read it.
- **A folder the team shares.** `herdr-synapse project set <path>` gives the
  team `.herdr-synapse/<team>/` in your project: the rules, one instructions
  file per member, a live `board.md`, and an `artifacts/` directory the agents
  own. Anything that changes there becomes a board post, so nobody falls
  behind.
- **A knowledge base that outlives the session.** `knowledge set` holds your
  DOs and DON'Ts and carries your authority; any agent appends what it learned
  with `knowledge add`, attributed and marked as a peer note rather than a
  rule. `prefix+f` shows what every team has and what is missing.

**Operations**

- **Context you can see and act on.** Claude Code, Codex and OpenCode members
  show how full their context window is, read from the harness's own transcript,
  rollout log or database; other kinds explicitly show unknown.
  The board says so at 75 % and 90 %, the sidebar gauge turns yellow then red,
  and `compact <name>` or `clear <name>` types the kind's own command when the
  member is next idle. A compacted or cleared member is re-briefed, and
  `herdr-synapse orient` gives it the whole team back in one read.
- **Usage limits across supported providers.** `prefix+i` shows the session,
  weekly, and per-model windows that supported provider accounts publish,
  grouped by the agents behind each. It reads the agent CLIs' local login
  tokens only when you open the report, uses them only for HTTPS requests to
  the providers' usage endpoints, and never stores, logs, or prints them.
- **A board you can keep, or clear.** `export` saves the whole board, archive
  included, as Markdown, JSON, JSONL or text. `wipe` empties it into the
  archive with a note saying who did it; `--purge` deletes it for good.
- **Attribution and an audit trail.** Peer posts arrive framed as requests,
  not orders. Only the daemon writes to member terminals, direct human sends
  and allowed peer interrupts remain distinguishable, every refused attempt is
  audited, and `notifier stats` shows a `wrong_target` count that must stay 0.
- **Optional Claude Code hooks.** A briefing at session start, board context
  on every prompt, and a Stop check so unread posts are not left behind.

## Compatibility: what works with which agent

Herdr's **Settings → integrations** screen says whether Herdr can receive an
agent's lifecycle and session state. It does not mean every harness-specific
Synapse feature is implemented or verified. The distinction matters.

All user-visible feature families are covered below. The exhaustive command,
parameter and shortcut inventory is in [docs/reference.md](docs/reference.md).

The coordination layer itself does not depend on a harness adapter:

| Shared feature family | All 17 Herdr integrations | Other detected kinds | Current limit |
| --- | --- | --- | --- |
| Teams and authority | ✓ | ✓ | Create, add, remove and dissolve; names, roles, briefs, charters, managers, delegates and multiple teams all work from Herdr's agent and terminal records. Model selection is separate below. |
| Board and collaboration | ✓ | ✓ | Posts, kinds, replies, references, attachments, receipts, filters and the agent skill use the same board format for every kind. |
| Team-to-team coordination | ✓ | ✓ | Links, topology, cross-team posts and receipts are harness-independent; both teams need a manager. |
| Instructions and knowledge | ✓ | ✓ | Project folder, rules, per-member instructions, findings and artifact watching work for every kind; automatic delivery of a change follows the delivery row below. |
| Human interaction and UI | ✓ | ✓ | Teams view, console, compose, sidebar tokens, focus/peek and the operator ask queue are shared. Whether an agent's shell tool can remain blocked for an answer is listed below. |
| Operations and safety | ✓ | ✓ | Export, archive, wipe, audit, notifier statistics, mute/pause, health checks and operator gates are agent-independent. |

The remaining capabilities touch the receiving TUI, its session store, model
flags, context files or provider account. Claude Code, Codex and OpenCode are
the fully supported core tier. Delivery to any other kind is implemented
through Herdr's terminal API, but Synapse blocks it until you explicitly probe
or trust that TUI.

The table below covers every state integration currently shown by Herdr. `✓`
means supported and live-verified, `◐` means implemented but conditional or not
fully live-verified, and `—` means Synapse has no adapter for that feature.

<!-- BEGIN: integration-compatibility -->
| Herdr integration | Resume | Idle nudge | Safe `!` | Running `!!` / interrupt | Ask wait | Hooks | Model / effort | Context | Compact / clear | Usage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Pi (`pi`) | ◐ | ◐ | ◐ | ◐ | ◐ | — | — | — | — | ◐ |
| OMP (`omp`) | ◐ | ◐ | ◐ | ◐ | ◐ | — | — | — | — | — |
| Claude Code (`claude`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Codex (`codex`) | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| GitHub Copilot (`copilot`) | ◐ | ◐ | ◐ | ◐ | ◐ | — | — | — | — | ◐ |
| Devin (`devin`) | ◐ | ◐ | ◐ | ◐ | ◐ | — | — | — | — | — |
| Droid (`droid`) | ◐ | ◐ | ◐ | ◐ | ◐ | — | — | — | — | — |
| Kimi (`kimi`) | ◐ | ◐ | ◐ | ◐ | ◐ | — | — | — | — | — |
| OpenCode (`opencode`) | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ | ✓ | ✓ | ◐ |
| Kilo Code (`kilo`) | ◐ | ◐ | ◐ | ◐ | ◐ | — | — | — | — | — |
| Hermes (`hermes`) | ◐ | ◐ | ◐ | ◐ | ◐ | — | — | — | — | — |
| Qoder CLI (`qodercli`) | ◐ | ◐ | ◐ | ◐ | ◐ | — | — | — | — | — |
| Qwen Code (`qwen`) | ◐ | ◐ | ◐ | ◐ | ◐ | — | — | — | — | — |
| Cursor Agent (`cursor`) | ◐ | ◐ | ◐ | ◐ | ◐ | — | — | — | — | — |
| MastraCode (`mastracode`) | ◐ | ◐ | ◐ | ◐ | ◐ | — | — | — | — | — |
| Antigravity CLI (`antigravity-cli`, agent kind `agy`) | ◐ | ◐ | ◐ | ◐ | ◐ | — | — | — | — | — |
| Grok (`grok`) | ◐ | ◐ | ◐ | ◐ | ◐ | — | — | — | — | — |
<!-- END: integration-compatibility -->

“Full core support” means the live-verified path from resume through idle and
running delivery, blocking asks, model/effort control, context measurement,
compaction, clear and re-briefing. Hooks are an optional Claude-only delivery
channel, and account usage depends on what each provider publishes; neither is
required for core support.

What the conditional cells mean:

- **Session resume:** implemented for all 17 Herdr integrations above; live
  round trips are verified only for Claude Code, Codex and OpenCode. It needs
  that integration to have reported the member's session identity first.
- **Idle nudge:** live-verified end to end for Claude Code, Codex and OpenCode.
  Every other kind is blocked by default until `hooks probe` or `kinds trust`;
  trust enables it but does not turn an unverified harness into a verified one.
- **Safe `!`:** the protocol path and refusal behaviour are covered by tests;
  the complete TUI round trip is live-verified with all three supported
  harnesses. It also requires a running Herdr server that advertises atomic
  idle submission and fails closed with an `@name` suggestion when that method
  is unavailable.
- **Running `!!` / interrupt:** `!!name text`, its per-agent `!!all text`
  fan-out, and teammate `post --interrupt` are live-verified for all three
  supported harnesses, in both directions. Other kinds are labelled
  unverified and remain off unless explicitly enabled.
- **Ask wait:** the board wait and operator popup are harness-independent, but
  the agent must keep a long-running shell command alive. The complete wait,
  answer and unblock round trip is live-verified for all three supported
  harnesses; every other TUI remains unverified.
- **Prompt hooks:** only Claude Code receives board context at prompt submit,
  the session briefing at startup and the Stop check. Other kinds rely on typed
  delivery and re-briefing.
- **Model / effort:** implemented only for Claude Code, Codex and OpenCode.
  Claude switches both live. OpenCode switches effort live through its variant
  picker; model changes use a controlled restart. Codex applies both at resume
  or controlled restart. All three paths preserve the member's exact session
  and were live-verified.
- **Context gauge:** Codex supplies its effective window at runtime. OpenCode
  supplies the active provider/model and its standard catalog limit, but custom
  `opencode.json` limit overrides are not read yet. Claude supplies exact usage
  and its active model, but Synapse currently resolves capacity through a
  compiled Claude model catalog; therefore Claude context capacity is not yet
  fully dynamic. Unknown capacity is shown as unknown and never raises a
  percentage warning. Pi and the other integrations are not tracked yet.
- **Compact / clear:** implemented and live-verified for Claude Code, Codex and
  OpenCode, including context-drop detection, session-generation handling and
  one re-brief after each operation. OpenCode 1.18.30 crashes when a fresh TUI
  starts in a very narrow terminal, so Synapse refuses its clear before exit
  when the pane layout is under 38 columns (about a 40-column PTY).
- **Account usage:** separate from the per-agent context gauge. Anthropic and
  OpenAI Codex are live-verified; Copilot is best effort; Pi and OpenCode depend
  on the provider/login they use, and OpenCode Zen publishes billing rather
  than a quota window.

Herdr also detects `gemini`, `cline`, `kiro`, `amp`, `maki` and `muse`, which
are not among the 17 installable state integrations above. They can join teams
and use the board without an integration; automatic terminal delivery still
requires explicit trust. Synapse does not promise session resume or any
feature in the harness-sensitive matrix for them.

## Install

Requirements: Herdr 0.8.2 through 0.9.x, Python 3.9 or newer (standard library
only), macOS or Linux. Other Herdr lines require the explicit, unsupported
`daemon start --allow-version` override. Windows is not supported.

Boards, teams, nudges, and explicit `!!` delivery work across those supported
Herdr lines. Race-free `!name text` additionally requires the running server
to expose `agent.prompt_if_idle`; the console and `daemon status` show that
capability directly and offer `@name text` when it is unavailable.

Start `herdr` first: the install registers the plugin through the running
server.

```bash
# 1. Install the plugin and place its CLI on your PATH.
herdr plugin install vitalysim/herdr-synapse
~/.config/herdr/plugins/github/herdr-synapse-*/bin/herdr-synapse install-cli --yes

# 2. Start the notifier once; future Herdr starts launch it automatically.
herdr-synapse daemon start

# 3. Install the Synapse operating skill for your agents.
herdr-synapse skill install

# 4. Trust each terminal UI you use (required once per Herdr session).
herdr-synapse kinds trust claude
herdr-synapse kinds trust codex
herdr-synapse kinds trust opencode

# 5. Add the richer lifecycle integration for Claude Code.
herdr-synapse hooks install claude
```

### Why these setup commands are separate

| Command | Requirement | What it enables |
| --- | --- | --- |
| `herdr-synapse skill install` | Required for autonomous agent collaboration | Installs the bundled Synapse operating guide where supported agents can discover it, so they know how to identify themselves, read and acknowledge the board, post updates, answer teammates, and recover after context loss. |
| `herdr-synapse kinds trust claude` | Required once per Herdr session when Claude Code members receive terminal delivery | Opens the notifier's explicit trust gate for the `claude` terminal UI. |
| `herdr-synapse kinds trust codex` | Required once per Herdr session when Codex members receive terminal delivery | Opens the same trust gate for the `codex` terminal UI. |
| `herdr-synapse kinds trust opencode` | Required once per Herdr session when OpenCode members receive terminal delivery | Opens the same trust gate for the `opencode` terminal UI. |
| `herdr-synapse hooks install claude` | Recommended for Claude Code only | Adds session-start briefings, board context at prompt submission, and an end-of-turn unread-post check. Codex and OpenCode use the skill and notifier instead; they do not have Synapse hooks. |

Kind trust is intentionally session-local: run only the lines for agent kinds
you use. Until a kind is trusted, its members can join and appear in `who`, but
the notifier will not type briefings, nudges, direct messages, or control
commands into that terminal UI. A team that appears healthy but never reaches
an agent will show `kind_unverified` in the notifier log, for example:

```
clickhouse-hunt: claude-hunter-research held: kind_unverified
```

`herdr-synapse kinds list` shows the current trust state. Trusting a kind is a
deliberate acknowledgement that you have checked how that terminal UI handles
programmatic input.

Claude hooks are optional because the notifier can deliver without them, but
they provide the most complete Claude experience. Installing them switches
Claude members to `delivery: "hooks"`, which raises the idle-stability window
from 2 s to 15 s before a nudge may be typed. To keep the hooks with a shorter
window, override it per team:

```jsonc
// team.json -> config
"gate": { "stable_ms_hooks_delivery": 3000 }
```

Members already running pick the hooks up only after their Claude session
restarts. Only Claude Code has hooks; other kinds rely on typed nudges.

Adding the plugin to a session that is already running agents loses nothing.
The install, the notifier, and the config reload all talk to the running
server; no pane restarts, and nothing is typed into any agent until you
create a team.

### Configure the UI once

`setup --print-config` prints a TOML block. **Paste the printed block** into
`~/.config/herdr/config.toml`, not the command itself:

```bash
herdr-synapse setup --print-config          # then paste its output into your config
herdr config check && herdr-synapse keys check
herdr server reload-config                  # live, no restart
```

This is the one careful step. Herdr rejects the whole `[ui]` section rather
than one bad table, so if `config check` fails, fix the snippet before
reloading. The block gives you the key bindings and the sidebar rows that show
each member's team, role and current task, using a six-colour team palette.

### Verify

```bash
herdr plugin list                   # herdr-synapse, enabled
herdr-synapse kinds list            # every kind you use says "trusted" (step 4)
herdr-synapse skill check           # the skill is installed and current
herdr-synapse hooks check claude    # SessionStart=yes, UserPromptSubmit=yes, Stop=yes
herdr-synapse daemon status         # notifier alive, socket, teams
herdr-synapse doctor                # warns about anything missing, including stale sidebar rows
```

Then end to end, with two agents running:

1. `prefix+t` and put them in a team.
2. `prefix+u` opens the board console.
3. Post `@<member> run herdr-synapse board --new, ack the charter, and reply with your status`.
4. The feed shows `✓nudged`, then `✓read` once the member reads it, then its reply.
5. `herdr-synapse notifier stats` for delivery health; `wrong_target` must be 0.

If a post seems to go nowhere, the notifier log says why in one line per
attempt: `held: kind_unverified` (step 4 not done), `held: not_idle` (the
member is working), `held: done_hold` (it just finished; the notifier waits a
minute so you can read the result), `held: focused` (you are looking at that
pane). `herdr-synapse nudge <name> --force` overrides all of them.

A human post addressed to the whole team queues every member for a nudge once
idle; a manager's broadcast does the same. An ordinary member's broadcast
waits for the next board read, with an idle unread member swept into a nudge
within three minutes. None interrupts a running turn. Use `--to <name>` when
one member must act, and `--urgent` when it cannot wait.

### Updating

One command refreshes the managed checkout, CLI link, installed skill copies,
and notifier:

```bash
herdr-synapse update
```

For a GitHub install this asks Herdr to replace its managed checkout. For a
locally linked development checkout it never runs `git pull` or rewrites your
tree; it re-registers that link and refreshes the installed surfaces from the
checkout. Foreign skill directories are left alone unless you explicitly use
`--force-skill`. The
console immediately shows both the loaded plugin and daemon versions, so a
stale process is visible. Then `/quit` and reopen an already-open console to
load new console code.

Teams, boards and the state pointer all survive an update, because the
plugin's state lives outside the checkout. Even `herdr plugin uninstall
herdr-synapse` removes only the checkout and leaves teams and boards on disk.
Agents are never touched by an update.

What never carries over from another machine: agent logins (the usage popup
shows a provider only when that agent's own CLI is logged in locally), kind
trust, teams and boards, and the Claude hooks.

### Upgrading from herdr-team

The plugin was called `herdr-team` before 0.8.0. The identifiers moved; your
data did not. Once:

```bash
herdr plugin install vitalysim/herdr-synapse   # or: herdr plugin link <checkout>
herdr-synapse install-cli --yes
herdr-synapse skill install                   # then delete ~/.agents/skills/herdr-team
herdr-synapse hooks install claude            # then drop the old herdr-team-hook.sh entries yourself
herdr-synapse daemon start --replace
```

Repoint the `command = "herdr-team.*"` lines in `~/.config/herdr/config.toml`
at `herdr-synapse.*` and reload. The state directory and each project's team
folder (`.herdr-team/` → `.herdr-synapse/`) are renamed on first use, never
merged; a `--ref` under the old path still resolves, so an agent holding it in
its context is not broken by the move.

## Quick start

1. Open two panes and start two agents.
2. `prefix+t`, Space on both, Enter, then a team name, a charter, optional team rules and a team folder, and for
   each agent a role, a name, a required Mission / brief, and optionally a model (`opus@medium`;
   Enter keeps the harness default). Each member is briefed once it is idle.
3. `prefix+u` opens the console. Plain text posts to the whole team,
   `@name text` to one member, `!name text` types straight into one, and
   `!!all text` attempts forced direct typing for every current agent.
4. Ask for something: `@red-dev-claude-dev post a summary of the repo layout`.
   The member is nudged when idle, reads the board, and replies; the feed
   shows `✓nudged` and `✓read`.
5. Give the team a folder when the wizard offers one (it prefills the
   directory your agents already share). Then `prefix+f` shows what the team
   knows and what is still missing.

Default key bindings: `prefix+t` teams view, `prefix+u` console, `prefix+m`
compose popup, `prefix+y` team view in the sidebar, `prefix+i` usage limits,
`prefix+f` team knowledge.

The [complete command and shortcut reference](docs/reference.md) lists every
CLI parameter, plugin action, console command, and key used inside each view.

## The teams view

`prefix+t` shows every team with its members underneath and the unassigned agents below, grouped by their Herdr tab name and stable tab ID so duplicate names remain easy to locate. Each tab group is collapsible and each agent row keeps its pane ID visible. Each team header names its manager and the teams it is linked to; each manager row is marked `★`.

| Key | What it does |
| --- | --- |
| Enter | on a member: the action menu below; on a tab without picked agents: fold it; on picked agents: start the team wizard; on a team with agents picked: add them to it |
| Space, `a` | pick an unassigned agent; `a` picks or clears them all; on a tab or team row Space folds it |
| `g` | verify and focus the highlighted agent's pane, then close the Teams view |
| `b` | open that team's board |
| `s` | restore that team's missing agents into a new named tab |
| `c` | connect that team to another, or break the link |
| `v` | open the scrollable ASCII topology of teams, managers and links |
| `f` | set that team's project folder |
| `x` | dissolve that team (asks first) |
| `w` | show unassigned agents from this space only, or from every space |
| `r`, Esc | refresh, close |

Closed your agents? Open a shell tab, press `Ctrl+B`, `T`, highlight the saved team and press `s`. Synapse creates a new `team:<name>` tab with panes for missing members, resumes their recorded conversations when available, and starts fresh when no conversation ID was saved. Names, roles, manager assignment, model/effort settings, instructions, board history and team links are preserved. Agents already running or with a reserved pane are skipped. Startup failures stay visible for inspection; after partial recovery, `g` on a team opens the restored tab. This restores saved teams in the same Herdr session; dissolved teams are archived and are not restored here.

Enter on a member offers, without closing the popup: rename it, change its
goal, send that goal to it now, remove it (keeping or clearing its Herdr
name), go to its pane, show the command that reopens its own session, make it
the team manager, and set its model and effort.

Long key legends and menu choices wrap instead of disappearing past the right
edge. Short windows keep the highlighted choice fully visible and show how
many choices lie above or below it. In the topology view, `v` or Esc returns
to the tree; arrows and PgUp/PgDn scroll, and `r` refreshes live state.

## The console in one table

| You type | What happens |
| --- | --- |
| `text` | posted to the whole team; every member is nudged once idle |
| `@name text`, `@role:r text` | posted to one member or a role; nudged once idle |
| `/team other-team text` | posted to a linked team; its manager is nudged |
| `/human text` | a note to yourself |
| `/kind request`, `/reply 12`, `/urgent`, `/ref path` | prefixes that shape the post |
| `@@path text` | attaches a file; `@@` opens a finder over the agents' project |
| `!name text` | typed only if that same member is still idle when Herdr atomically submits it; recorded as a `direct` post |
| `!!name text` | also while the member works or is muted; never into a dialog or a draft |
| `!!all text` | attempts the same forced direct delivery independently for every current agent; each gets its own recorded outcome |
| `/interrupt @name text` | urgent, and typed into the member's running turn when its kind allows it |
| `/interrupts off`, `/interrupts claude,codex,opencode --cooldown 5m` | which kinds interrupts may reach mid-turn, and how often |
| `/nudge name`, `/mute name 10m`, `/pause`, `/focus name`, `/peek name` | delivery and pane controls |
| `/asks`, `/ask-policy block 8m` | what is waiting on you; whether agents wait, and how long |
| `/context`, `/compact name`, `/clear name` | context windows, and the two ways to make room |
| `/model name opus@medium [--restart]` | a member's model and effort; Claude and OpenCode effort can apply live, while Codex changes and OpenCode model changes apply at resume or `--restart` now |
| `/links`, `/link other-team`, `/unlink other-team` | the links this team has, and making or breaking one |
| `/filter teams`, `/filter team`, Tab | the inter-team lens, the local lens, or cycle through all of them |
| `/who`, `/charter`, `/charter set text`, `/use team`, `/retract N`, `/remove name` | roster, charter, teams, board |
| `/export [path] [--format md\|json\|jsonl\|text]` | saves the whole board to a file, archive included |
| `/wipe [--purge] [reason]` | empties the board into the archive (`--purge` deletes it); asks first |
| `?` on an empty line, `/help` | every sign, command, and key |

`/`, `@`, `@@`, and `!` open lists of commands, names, files, and members.
Up/Down move, Tab picks, Esc hides. Every command in the `/` menu shows its
placeholder, so you do not have to remember the arguments.

The feed follows the newest post. Scrolling back with Up or PgUp stops it and
shows how many entries are below; End or Esc returns to the latest, and
posting snaps you there too.

## Teams that talk to each other

Two teams of the same Herdr session can consult each other through their
managers. The link is made once and broken when it is no longer wanted:

```bash
herdr-synapse link clickhouse-hunt gitlab-hunters --note "shared CVE triage"
herdr-synapse links                     # every link, with its state and both managers
herdr-synapse unlink clickhouse-hunt gitlab-hunters
```

Both teams need a manager, because the managers are the endpoints. From then
on either manager posts to the other team with `post --to team:<other>`, or
`/team <other> text` in the console, and so can you. A plain member is told to
ask its manager instead.

A message across a link is an ordinary board record that lives on both boards.
The receiving board gets the delivered copy, addressed to its manager and
signed `<team>/<manager>`, so the manager is nudged through the usual gates,
`board --new` shows it, and the Claude prompt hook injects it with a line
saying a peer team is asking. The sending board keeps a mirror addressed to
`team:<other>`, which nudges nobody but lets the team see what its manager said
outward. Replies thread across the two boards, so `re#N` is right on both
sides, and when the receiving manager reads the post a receipt comes back:
`read by <manager>` on the mirror.

In the console, `/filter teams` shows only what crossed a link and
`/filter team` everything else; `board --teams` is the same on the CLI. Link
lines carry `⇄` and the sender's team. In the teams view, `c` on a team row
lists the other teams: Enter links or breaks, and a team without a manager
says so.

Managers are told about every link in their briefing and in `me`, along with
the command to use. The skill tells every agent that a linked team's manager is
a peer asking, never the operator. A manager cleared after linking pauses the
link rather than dropping messages; dissolving a team breaks its links and
tells the other side. Links are session-scoped: two Herdr servers do not link.

## A model and an effort for supported harnesses

Claude Code, Codex and OpenCode members can carry an optional setting of the
form `<model>[@<effort>]`, in the harness's own vocabulary, passed through
untranslated: `medium` means what that harness means by it. Other kinds are
refused rather than receiving guessed flags.

```bash
# at creation: per spawned role, or as the team default for a kind
herdr-synapse create hunt --new --spawn reviewer:codex --spawn dev:claude \
    --brief reviewer="Review every change and report regressions." --brief dev="Implement the requested change and run its tests." \
    --model reviewer=gpt-5.6-luna@high --model claude=opus@medium
herdr-synapse models set codex gpt-5.6-luna@medium     # team default for a kind
herdr-synapse model                                    # every supported member, with the setting source
herdr-synapse model hunt-reviewer @xhigh               # change one half
herdr-synapse model hunt-reviewer gpt-5.6-luna@high --apply restart
herdr-synapse model --self opus@high                   # a member, for itself
```

| Kind | At launch and on `resume` | While running | Effort words |
| --- | --- | --- | --- |
| Claude Code | `--model`, `--effort`, `--dangerously-skip-permissions` | `/model` and `/effort` typed by the notifier when idle | `low medium high xhigh max` |
| Codex | `-m`, `-c model_reasoning_effort`, `--dangerously-bypass-approvals-and-sandbox` | picker only: applies at the next resume, or now with `--apply restart` | `minimal low medium high xhigh` |
| OpenCode | `-m provider/model`, `--auto`; effort is selected with `/variants` after startup | effort switches live with `/variants`; model applies at the next resume or with `--apply restart` | provider-specific |

Resolution is the member's own setting, then the team default for its kind,
then the harness default. The setting travels with the session: `resume`
reopens the member with the same flags, and Herdr's `agent.start` receives
them as arguments, never as a shell string.

Every fresh spawn of these three harnesses carries its unrestricted flag even when no model is selected. `herdr-synapse resume`, controlled model restarts and OpenCode clear restarts rebuild the same default; an already-running agent added to a team keeps its current launch mode until a Synapse-managed resume or restart.

`--apply restart` exits the agent cleanly and resumes its own session with the
new flags; the member is never marked missing while that is in progress, and
both the exit and the return are bounded. Controlled restarts preserve the
harness's allowlisted non-permission flags, rebuild the unrestricted default, and Codex restarts suppress its update
picker so the requested model cannot be stranded behind a startup screen. Who
may change a setting: the
operator or a delegate for anyone, the team manager for anyone, and a member
for itself with `--self`. Every change is announced to the member and the
team, and `who` shows the configured setting beside what the harness actually
reports, so a request that never took effect is visible rather than assumed.

The same setting is asked for in the `prefix+t` wizard after role, name and
brief, is action 9 in a member's menu, and is `/model` in the console.

## The team folder

Agents in one checkout all read the same `CLAUDE.md`, so nothing on disk tells
them apart. Give the team a directory and it gets one:

```
<your project>/.herdr-synapse/<team>/
  knowledge.md         the team's rules, and what its members have learned
  members/<name>.md    this member's own document; the one file you edit
  board.md             the board, kept current; git-ignored
  artifacts/           work products; git-ignored
```

Each member's file has the same six sections, with a line of guidance in each: Mission, Scope, Constraints, Definition of done, Handoffs, and Notes, which stays private to you. Mission is required when a member joins; the other five sections are optional. Team creation fills Mission from the required brief, or derives the short roster brief from an explicit `## Mission`, so nobody starts at "none set". `knowledge-status`, `doctor` and the teams view report any older or hand-edited member that has no Mission. Edit the file in your editor and the notifier leaves it alone and tells you; `herdr-synapse instructions <name> --adopt` shows the diff and applies it. That confirm step is the whole security model: the folder is inside a checkout your agents can write to, so nothing there counts as your word until you say it does.

On daemon load or an explicit `project render`, Synapse completes only the exact revision-1 Mission-only scaffold written by the old creation path. It adds the five empty standard sections without changing the revision or posting to the board, and leaves edited, custom and later-revision documents untouched.

| Command | What it does | Who |
| --- | --- | --- |
| `project set <path>` | records the directory and creates the folder | you |
| `instructions <name> --edit` | that member's own document, in your editor | you |
| edit `members/<name>.md`, then `instructions <name> --adopt` | the same, in your repo | you |
| `knowledge set "…"` | the team's DOs and DON'Ts | you |
| `knowledge add "…"` | one attributed finding | any member |
| `knowledge-status`, `prefix+f` | what every team has, and what is missing | anyone |

Three things make this safe to keep in a repository agents can write to. The
files in your project are a **mirror**: the authoritative copies live outside
it, behind commands only you can run, so an agent cannot edit a file and have
it read back to its teammates as your instruction. The plugin writes nothing
until you name a directory, and it **never deletes** anything inside one.

Rules and instructions carry your authority and reach Claude in its session
context; a changed document reaches it on its very next turn, once, until it
acknowledges. Other kinds are told to re-read. Findings do not carry
authority: they are attributed peer notes, escaped so one can never pose as a
rule.

Everything that changes here reaches the team through the board, so a new
rule, a changed instruction, a finding, or a file dropped in `artifacts/` by
anyone shows up for Claude on its next prompt and for every other kind on its
next board read. Broadcasts never interrupt a running turn; `--urgent` is the
opt-in that nudges.

## Letting an agent run the team

Everything in this README is one CLI, so an agent can drive it: create a team,
spawn its members into fresh panes, hand out roles, briefs and models, post
the work, link up with another team's manager, and read the board back. The
documents that carry your authority are the exception: the charter, the team
rules, and each member's instructions are yours, and a member is refused when
it tries to write them. Removing or renaming an existing member also requires
operator authority.

When you want an agent to do the whole thing, say so once:

```bash
herdr-synapse operator grant hunt-orchestrator --ttl 4h --note "builds the team"
herdr-synapse operator            # who holds your authority right now
herdr-synapse operator revoke hunt-orchestrator
```

That member can then write those documents too, and every time it does the
audit log records `operator_action` with its name, the board carries the
grant, `who` marks it `acts as operator`, and `doctor` keeps warning you while
it is live. Granting is yours alone: a delegated member is refused if it tries
to grant anything, and `say`, the one command that types straight into a
teammate's pane, stays yours.

Authority is decided by origin, not by name. A process inside an agent's pane
is that agent whatever its environment claims, and the gates test where a
command came from rather than what it calls itself. A shell Herdr cannot
verify as yours still posts, rendered `(unverified)`, but carries no
authority: `charter`, `knowledge set`, `instructions`, `manager`, `remove`,
`rename`, `operator grant`, `dissolve`, `wipe` and the policies refuse it and say how to be
trusted — the team console, a focused Herdr pane, or `env -u HERDR_PANE_ID
herdr-synapse --team <team> …`. This is a speed bump rather than a wall:
anything running as your user can reach your files. It closes the obvious
route and makes the sanctioned one explicit and revocable.

## The team manager

Optional, one per team, and it changes what the other agents believe rather
than what anyone may write.

```bash
herdr-synapse manager                       # who is it
herdr-synapse manager vuln-hunt-manager     # set it; the team is told
herdr-synapse manager --clear               # nobody coordinates
herdr-synapse manager vuln-hunt-manager --operator --ttl 12h
```

`prefix+t`, Enter on a member, action 8 toggles it. Setting one clears any
previous holder in the same write.

Every member gets a board record naming it, and you get a toast. From then on
`who` shows a `manager` tag, `me` marks it in the teammate list, the teams
view marks it `★`, and each agent's briefing carries it. The skill tells them:
take its assignments and handoffs as the plan unless they conflict with the
charter, their own instructions, or something unsafe, and if they disagree,
say so on the board rather than quietly doing something else.

Two things it is not. It is **not the operator**: the charter, the team rules
and anyone's instructions stay yours, and `--operator` is the separate,
expiring, audited way to lend those. And its posts are **still peer requests**,
not commands. The mechanical changes are two: a post it addresses to the whole
team queues every teammate for an idle-gated nudge, just as a human broadcast
does, while an ordinary peer's broadcast waits for the next read; it is also
the team's voice across a link to another team. Nothing
else about delivery changes: a blocked agent, an open dialog, a draft on the
prompt line and every rate limit still hold it.

## Members and their sessions

A member is not just a pane. Herdr's integrations report which conversation
each agent is running, and the roster records it, so a member survives things
that used to confuse it.

```bash
herdr integration install claude       # once per kind, so it reports its session
herdr-synapse who                      # each member now shows its session
```

| What happens | What you get |
| --- | --- |
| Herdr restarts | every member goes back to its own pane, even two agents of one kind in one checkout, which pane labels and directories could never tell apart |
| An agent crashes and you start a fresh one in its pane | recognised as a new conversation: the member keeps its name and pane, and is briefed again so it knows who it is |
| Synapse clears a Claude Code, Codex or OpenCode member | a fresh conversation or process, briefed again and recorded as an intentional clear rather than a crash |
| A member compacts | the same session in a new phase, so no new generation and no "restarted" line, but it is briefed again because the briefing was in the history that was just summarized |
| You want the old conversation back | `herdr-synapse resume <name>` from a shell pane |

`resume` runs the exact command Herdr's own restore would use, in the
member's directory and with the member's model flags, and the pane becomes
that agent:

```bash
herdr-synapse resume vuln-hunt-reviewer           # e.g. codex resume 01a077d4-…
herdr-synapse resume vuln-hunt-reviewer --print   # just show it
```

Never use a bare `claude --continue`, `codex resume --last`, or `opencode -c`
for a team member. Those pick a conversation by directory or by recency, not
by pane, so in a shared checkout they can bring back a different member's
work. `resume` covers every kind in Herdr's current integration list (17);
a kind without one keeps working exactly as before, it just has no session to
reopen. `prefix+t`, Enter on a member, action 7 shows the command.

## Context windows

Herdr knows nothing about tokens and no agent exposes them through Herdr's
socket. Synapse currently has file readers for Claude Code, Codex and OpenCode;
the notifier reads them every fifteen seconds.

```bash
herdr-synapse context                           # supported members get a bar; others are unknown
herdr-synapse compact vuln-hunt-reviewer        # summarise its context in place
herdr-synapse clear vuln-hunt-reviewer --yes    # throw it away and brief it again
```

| Kind | Tokens | Window capacity |
| --- | --- | --- |
| Claude Code | newest transcript `message.usage`, cache reads included | compiled catalog keyed by the observed model; not fully dynamic yet |
| Codex | rollout log's newest `token_count` | exact `model_context_window` from the same runtime event |
| OpenCode | newest assistant message in `opencode.db` | active `providerID` + `modelID` in the standard local catalog; custom config overrides are not read yet |

Any other kind reads `unknown`; nothing is estimated. At 75 % and again at
90 % the board gets one line addressed to that member and to the team, and
the member is nudged with it. The plugin never acts on it: what to do about a
full context is the member's decision, or yours.

`compact` and `clear` are available only for these three kinds. Both operations
and the following re-brief were live-verified in each real TUI. OpenCode clear
exits and starts a fresh full TUI on the same member rather than sending
`/new`; Synapse refuses before exiting when the pane layout is under 38 columns
because OpenCode 1.18.30 crashes while starting in a terminal that narrow.

A member may compact itself, and the skill tells it to finish or hand off its
task first. Clearing is yours alone, and asks before it runs. A member that
wants a peer compacted posts a request; no agent gains a way to type into
another.

The keystroke goes in as raw text and a separate Enter, because a prompt is
delivered as a bracketed paste and a pasted `/compact` arrives as text to
answer rather than a command to run. It waits for the same gate as everything
else, idle included, and Claude's two-to-three-minute compaction is waited
out rather than retried into.

## Coming back from a compaction

An agent that has just been compacted or cleared has lost the team. One command
gives it back:

```bash
herdr-synapse orient
```

It prints who you are, the charter, your brief, your own instructions, the team
rules, your model and effort, your teammates with the manager marked, the
teams you are linked to, where the team's files are, and your unread count.
The team's findings are a count and a command, not text: they are peer notes,
and a compacted agent should choose when to spend context re-reading them.

A Claude member is handed exactly this by its session hook. Codex and OpenCode
are re-briefed with a typed line naming `orient` after a detected compaction;
other trusted kinds receive the typed re-briefing when Herdr reports a restart
or session change, but Synapse does not offer `compact` or `clear` for them.

What `orient` can hand back is only what you wrote. `doctor` says so when a
team has no rules or its members have empty instructions.

### Give it something to hand back

Two documents define the authority each member can load into its working
context. Claude hooks inject them; other kinds read them through `orient` and
the explicit instructions and knowledge commands. Both are worth writing
before you rely on any of this:

```bash
herdr-synapse knowledge set --file rules.md          # the whole team reads these
herdr-synapse instructions <name> --file <name>.md   # this member alone
```

`knowledge set` writes the **Rules** half of `<team>/knowledge.md`. That is the
per-team document every member gets, injected as operator authority. The other
half of the file is **Findings**, which any member adds with `knowledge add`;
those are peer notes, attributed and escaped, and they are pointed at rather
than injected, so one agent's text can never reach another wearing your
authority.

`instructions <name>` writes one member's own document: mission, scope,
constraints, definition of done, handoffs, and notes that stay private to you.
Scope is what stops two agents auditing the same tree. You can also edit
`<team>/members/<name>.md` in your project folder and run `instructions <name>
--adopt`, which shows a diff first; that folder is writable by the agents
themselves, so nothing in it reaches anyone until you adopt it.

## When an agent needs you

An agent that asks you something and gets a banner that fades in five seconds
will carry on without an answer. Measured on a live team over three days: 88
posts to the operator, ten of them actually waiting on a decision, six never
answered at all, and the other four answered by *other agents*, one of which
overrode a genuine pre-submission halt.

```bash
herdr-synapse asks            # what is waiting on you
herdr-synapse ask-policy      # whether agents wait for you, and how long
```

The notifier opens a **popup you answer in**: one popup for the whole queue,
because Herdr allows only one at a time. Type a reply and Enter sends it;
`Ctrl-A` acknowledges without typing, which closes the ask and releases the
agent; `Tab` moves through the queue; `Esc` leaves one waiting and stops it
reopening. For its first moment the popup accepts only Esc and `q`, so a burst
of keystrokes meant for the pane underneath cannot be filed as your decision.

An acknowledgement is not an approval, and says so. For a question or a blocker
it reads *"Seen by the operator. This is an acknowledgement, not a decision: if
you were waiting on one, say what you would do and stop."* For a finished-work
notice it just says it was seen.

And the agent **waits**. `post --kind question --to human` does not return until
you answer, so it cannot proceed on a guess or be talked past by a peer. Only
the operator closes an ask; a teammate's reply is a note on the thread, not an
answer. The board-side wait is harness-independent and reports that it is still
waiting every 30 s. The complete wait, answer and unblock path is live-verified
with Claude Code, Codex and OpenCode; other kinds should be treated as
conditional until verified. If nobody answers within eight minutes the command
gives up with a distinct exit code, the question stays on the board, and the
skill tells the agent not to guess.

Blocking defaults to `question`, `blocked` and `request`; blocking a `done`
notice would freeze a team that posts forty of them. `/ask-policy` in the team
console changes it, or turns waiting off entirely.

## Housekeeping

```bash
herdr-synapse export ~/notes/hunt.md               # the whole board, archive included
herdr-synapse wipe --reason "sprint 2 starts"      # every post moves to the archive; asks first
herdr-synapse wipe --purge                         # and the archive and payloads go too
herdr-synapse dissolve <team>                      # the team is archived, its agents keep running
herdr-synapse prune                                # archive old board segments
```

`wipe` is the store's own rotation, forced: sequence numbers keep counting,
the fresh board opens with a `board_cleared` note naming who did it, and
`board --since 1` still reads the history. `--purge` is the one thing here
nobody can get back. Both ask on a terminal and need `--yes` off one; both are
operator only; `/wipe` in the console does the same with a y/n first.

`dissolve` archives the board to the session's `_archive/` rather than
removing it, breaks the team's links and tells the other side, and rebuilds
the sidebar view for the teams that remain. `x` on a team row in `prefix+t`
does the same.

## How delivery works

Every human- or member-authored post to a member becomes pending work for the
daemon. Before it types the one-line nudge, the daemon checks, in order: the
post is still unread; the recipient is an agent with a terminal in the roster;
the same agent still occupies that terminal; the kind is trusted; the agent is
idle; it has been
stable long enough (longer for screen-only detection than for hook-backed
kinds); Herdr reports no blocker; no dialog is on screen; the prompt line is
empty; the pane is not the one you are looking at; and rate limits allow it.
The result is one line in the agent's input box:

```
[herdr-team nudge] 1 new board post for red-dev-claude-dev (seq 131). Run: herdr-synapse board --new [n17]
```

The agent runs that command, reads the posts under a header that marks them
as peer requests, acts, and posts back. Holds are visible as `↪1 (focused)`
in `who` and the console, and in the daemon log. All timings are tunable per
team through `config.gate` in `team.json`; a misspelt key there is skipped and
named in the log rather than switching the others off.

The periodic catch-up sweep uses the same definition of mail: authored
messages to the member or `all`, never system/control history, delivery
receipts, direct lines, or retractions. It honours filtered-read `seen` seqs.
If delivery expires or is abandoned, the message remains unread on the board,
but a durable ledger tombstone prevents the sweep from recreating it forever;
`nudge --force` explicitly retries it.

System events follow one delivery table: a charter or rules change wakes
every idle member, a context warning or a model change reaches the member it
names through the same gates, a link announcement reaches both managers, and
receipts wake nobody. They remain visible board awareness, but never fall
through the catch-up sweep or hold Claude's Stop hook open as if they were
peer mail.

## Safety properties

- Only the daemon types into an agent, one line at a time, only into panes
  that are in a team roster. `herdr-synapse notifier stats` shows
  `wrong_target`, which must stay 0.
- Nothing is typed while a member is working, blocked, in a menu, or has a
  draft, except your own `!!name text` (including the per-agent `!!all`
  fan-out) and an interrupt you have allowed for that kind.
- The single-bang idle check and terminal write are one Herdr operation. A
  state or occupant change refuses the line. Synapse probes the running
  server, not its version string; without the atomic method `!` reports the
  missing capability and points to safe `@name` board delivery. It never
  falls back to the older check-then-prompt sequence.
- Authorship is stamped from the pane and process, never claimed by text.
  `--as human` from an agent pane is refused and audited; `say` accepts only
  the verified console; a process descended from an agent's pane is that agent
  whatever its environment says.
- Authority tests the origin, not the name. The charter, rules, instructions,
  manager, roster removal and renaming, links, grants, and policies accept
  only a verified human origin or an explicit, expiring delegation, and every
  use of a delegation is audited.
- Every board line the agents see is quoted under a system header. The only
  text carrying your authority is the charter, a member's brief and
  instructions, and the team rules, all written by commands an agent cannot
  run. Nothing an agent can write is ever injected as your word, and a message
  from another team's manager is marked as a peer team asking.
- Only the operator closes an ask or releases a waiting agent. A teammate's
  reply, or a reply from an unverified shell, is a note on the thread.
- Emergency stop: `herdr-synapse daemon stop`. Nothing is typed anywhere after
  that. `herdr plugin disable herdr-synapse` removes the plugin's sidebar
  tokens and view within seconds.

## Documentation

- [docs/reference.md](docs/reference.md): every CLI command and parameter, plugin action, console command, and GUI shortcut in one place.
- [docs/use-cases.md](docs/use-cases.md): ready-to-adapt development, vulnerability-hunting, research, review-swarm, and linked-team recipes using mixed agent types.
- [docs/human-testing.md](docs/human-testing.md): a guided first run, including an isolated sandbox session that cannot interfere with your real Herdr.
- [docs/capabilities.md](docs/capabilities.md): every capability, how to drive it from the UI and the CLI, what to expect, and a test checklist.
- [docs/cli.md](docs/cli.md): the command contract, with every argument, JSON shape, exit code, and record grammar.
- [docs/development.md](docs/development.md): internals, conventions, state layout, and the status log.
- [skills/herdr-synapse/SKILL.md](skills/herdr-synapse/SKILL.md): what agents are taught, printed by `herdr-synapse --skill`.
- [CHANGELOG.md](CHANGELOG.md): what changed in each release, and why.
- [CONTRIBUTING.md](CONTRIBUTING.md): bug reports, pull requests, and validation.
- [SECURITY.md](SECURITY.md): private vulnerability reporting and sensitive-data guidance.

## Status

Current source version: 0.16.0, skill v10.

Claude Code 2.1.267, Codex 0.153.4 and OpenCode 1.18.30 were exercised together
in one disposable Herdr 0.9.0/p22 session. Formation, exact-session resume, idle
nudge, safe `!`, running `!!`, teammate interrupt, blocking ask, model and
effort changes, context readings, compact, clear and re-briefing all completed
end to end for each harness. The notifier's `wrong_target` counter remained
zero, and the complete automated suite covers the surrounding failure paths.

Hooks remain Claude-only. Provider account usage is separate from core agent
support: Anthropic and OpenAI Codex logins are verified, while OpenCode depends
on its provider and OpenCode Zen exposes billing rather than a quota window.
OpenCode clear is intentionally refused below the safe pane width described
above. Other Herdr integrations keep the shared team and board features, but
their harness-sensitive terminal paths remain conditional until probed or
trusted.

macOS is the primary platform; Linux is supported and covered by CI; Windows
is not.

## Development

```bash
git clone https://github.com/vitalysim/herdr-synapse.git
cd herdr-synapse
python3 -m unittest discover -s tests             # standard library only
bin/herdr-synapse-sandbox start ~/your/project    # an isolated Herdr session for live testing
```

The sandbox launcher runs the plugin in a named Herdr session with its own
configuration, plugin registry, and state, so nothing it does is visible to
your default session. See [docs/development.md](docs/development.md) for the
conventions the code follows.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
