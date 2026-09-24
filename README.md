<p align="center">
  <img src="docs/banner.svg" alt="Herdr Synapse - the coordination layer for every coding agent" width="100%">
</p>

https://github.com/user-attachments/assets/4afbee34-daf0-4d1f-8ed1-7f803ab9a642

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
harness. The full core workflow is live-verified with Claude Code, Codex, OpenCode and Pi; other Herdr agent kinds stay blocked until you explicitly probe or
trust them.

The agents are coding-agent TUIs, but the work does not have to be code. A
team can run a research sprint, a content campaign or a security review as
readily as a feature: the board, work items, facts and schedules carry no
assumption about what the deliverable is.

> [!CAUTION]
> Every agent Synapse launches runs unrestricted by default when that agent has a switch for it: Claude Code gets `--dangerously-skip-permissions`, Codex `--dangerously-bypass-approvals-and-sandbox`, OpenCode `--auto`, and each other kind its own flag ([table](#launch-permissions-yolo-by-default)). Use `--permissions native` at creation or `permissions NAME native` to use an agent’s own settings. Fresh spawns, exact-session resumes, restores, controlled model restarts and OpenCode clear restarts otherwise carry the YOLO default, so these agents can execute commands and change files without approval prompts. Pi tools are unrestricted natively; Synapse adds run-scoped `--approve` for project resources. Use trusted repositories or an external sandbox; unrestricted execution does not grant Synapse operator authority.

```
team red-dev · 3 members · view:on · nudges:on · toasts:herdr · unread(you):0
charter #1: Ship the HTML report for susfind
runtime: safe !:ready · Herdr 0.9.1/p22 · Synapse 0.19.1 · daemon 0.19.1

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
  OpenCode effort can switch live; Codex/Pi changes and OpenCode model changes
  apply at their next resume, or now with a restart that keeps their session.
  `who` shows what was asked for beside what the harness reports.
- **A fresh replacement when an agent hits its limit.** Select a member in
  `prefix+t`, choose **Swap agent…** (`0`), and pick Claude Code, Codex,
  OpenCode, or Pi plus an optional model. Synapse creates a new agent with the same
  name, role, Mission, instructions, manager assignment, working directory,
  and team history. You do not need to create or select an existing agent.
  The outgoing pane closes and the replacement opens in a new tab, so the
  swap works even when the old agent cannot answer another prompt.

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
- **Work that ends.** Work items with a brief (what done looks like), owners,
  dependencies, reviewers and attempts that settle with an explicit outcome;
  `work next` says exactly what to run.

**Authority and knowledge**

- **Authority that stays yours, until you lend it.** The charter, the team
  rules and each member's instructions are the operator's. Which caller counts
  as the operator is decided by the process tree and a verified origin, never
  by a name or an environment variable an agent could set. `herdr-synapse
  operator grant <name>` lends that authority to an agent, with an expiry, a
  board announcement, and an audit line on every use.
- **Instructions you actually edit.** Every member gets a document with the
  same six sections: mission, scope, constraints, definition of done,
  handoffs, and notes you keep private. Save edits to auto-sync them, or use
  manual adoption to review each diff. Changed instructions notify the
  affected member when delivery is safe.
- **A folder the team shares.** `herdr-synapse project set <path>` gives the
  team `.herdr-synapse/<team>/` in your project: the rules, one instructions
  file per member, a live `board.md`, and an `artifacts/` directory the agents
  own. Anything that changes there becomes a board post, so nobody falls
  behind.
- **A knowledge base that outlives the session.** `knowledge set` holds your
  DOs and DON'Ts and carries your authority; any agent appends what it learned
  with `knowledge add`, attributed and marked as a peer note rather than a
  rule. `prefix+f` shows what every team has and what is missing.
- **Facts, not a pile of notes.** Findings carry their sources, their supporters and
  when they were true; a newer value supersedes the old one, and disagreements
  between agents are handled the way you choose, from silently observed to
  debated to decided by you. `recall` searches all of it at once.

**Operations**

- **Mission control and templates.** `prefix+d` shows what needs you across every
  team. `create --template research-sprint | content-campaign | vuln-hunt |
  feature-team` starts a whole team, and `template save` keeps yours.
- **Recurring work and your phone.** A schedule posts, or hands out a work
  item, on a timetable. A paired phone (ntfy, Telegram or a webhook) hears
  about questions waiting on you, and can answer them.
- **Context you can see and act on.** Claude Code, Codex, OpenCode and Pi members show how full their context window is, read from the harness's own transcript, rollout log, database or runtime extension. Pi readings are estimates; other kinds explicitly show unknown.
  The board says so at 75 % and 90 %, the sidebar gauge turns yellow then red,
  and `compact <name>` or `clear <name>` types the kind's own command when the
  member is next idle. A compacted or cleared member is re-briefed, and
  `herdr-synapse orient` gives it the whole team back in one read.
- **Search what members actually said.** `search rate limit` looks through
  the members' own Claude Code, Codex, OpenCode and Pi conversations, not only
  the board: newest first, the match marked, secrets redacted. You and the
  manager may search anyone; any other member only itself.
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
| Work, facts and recall | ✓ | ✓ | Work items, briefs, settlements, reviews, facts, disputes and the recall index are team records, identical for every kind; the posts they make are delivered like any other. |
| Templates, mission control, schedules, phone | ✓ | ✓ | A template only fills in `create`; mission control reads roster, board and work state; scheduled posts and phone answers are ordinary board records, so they reach agents the way any post does. |

The remaining capabilities touch the receiving TUI, its session store, model
flags, context files or provider account. Claude Code, Codex, OpenCode and Pi are
the fully supported core tier. Delivery to any other kind is implemented
through Herdr's terminal API, but Synapse blocks it until you explicitly probe
or trust that TUI.

The table below covers every state integration currently shown by Herdr. `✓`
means supported and live-verified, `◐` means implemented but conditional or not
fully live-verified, and `—` means Synapse has no adapter for that feature.

<!-- BEGIN: integration-compatibility -->
| Herdr integration | Resume | Idle nudge | Safe `!` | Running `!!` / interrupt | Ask wait | Hooks (prompt/stop) | Model / effort | Context | Compact / clear | Usage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Pi (`pi`) | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ | ✓ | ✓ | ◐ |
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

Pi's `—` under prompt/stop hooks does not mean it lacks an integration: `hooks install pi` installs its telemetry extension, while messages and briefings use typed delivery. Its context `✓` covers runtime model limits and explicitly labelled token estimates; its conditional usage cell refers to provider-account quotas, not context tracking.

### Pi setup and behavior

Pi support is live-verified with Pi 0.85.1 on Herdr 0.9.0. Its native Herdr integration identifies the running session; Synapse's separate extension reports the active provider/model, thinking level, context estimate, and lifecycle events. Neither extension patches Pi or Herdr core. Typed delivery recognizes Pi's standard editor and footer, including narrow panes; a custom UI extension that replaces these can cause protective delivery holds.

```bash
herdr integration install pi
herdr-synapse skill install
herdr-synapse hooks install pi
herdr-synapse kinds trust pi
```

For an already-running Pi session, run `/reload` after installing the extensions. Pi already discovers the shared Synapse skill in `~/.agents/skills/`. The extension installer respects `PI_CODING_AGENT_DIR`; `hooks check pi` checks installation, `hooks install pi --dry-run` previews it, and `hooks uninstall pi` removes only the managed extension. After a Synapse update, re-run `hooks install pi` and `/reload` to refresh the copied extension. Run `doctor` to identify missing or stale runtime reports.

Use `--spawn developer:pi` when creating a team and `--model pi=provider/model@high` for its Pi default. Fresh conversations receive their member name through Pi's native `--name`; resume and restore preserve the original conversation and title. Model/thinking changes use `model <member> provider/model@high --apply restart`, reopening the exact recorded session path. Models are passed through to Pi, not maintained in a Synapse catalogue. A model appearing in Pi's catalogue does not guarantee that your provider account permits it.

Pi has no built-in tool approval prompts. By default, Synapse-managed launches add `--approve` to trust project resources for that run; this does not change global project trust or disable user extensions. Use only trusted projects or an external sandbox.

The context gauge uses the active model's runtime limit, including model changes and custom models. Pi estimates context tokens, so Synapse labels the reading as estimated (`~`); unknown usage remains unknown rather than becoming zero. Compact uses `/compact`; clear uses `/new`. Working-turn messages use Pi's native steering queue, which is processed at its next steering boundary—not an immediate cancellation. Provider-account quota reporting remains separate and conditional.

“Full core support” means the live-verified path from resume through idle and
running delivery, blocking asks, model/effort control, context measurement,
compaction, clear and re-briefing. Hooks are an optional Claude-only delivery
channel, and account usage depends on what each provider publishes; neither is
required for core support.

What the conditional cells mean:

- **Session resume:** implemented for all 17 Herdr integrations above; live
  round trips are verified for Claude Code, Codex, OpenCode and Pi. It needs
  that integration to have reported the member's session identity first.
- **Idle nudge:** live-verified end to end for Claude Code, Codex, OpenCode and Pi.
  Every other kind is blocked by default until `hooks probe` or `kinds trust`;
  trust enables it but does not turn an unverified harness into a verified one.
- **Safe `!`:** the protocol path and refusal behaviour are covered by tests;
  the complete TUI round trip is live-verified with all four supported
  harnesses. It also requires a running Herdr server that advertises atomic
  idle submission and fails closed with an `@name` suggestion when that method
  is unavailable.
- **Running `!!` / interrupt:** `!!name text`, its per-agent `!!all text`
  fan-out, and teammate `post --interrupt` are live-verified for all four supported harnesses. Pi uses native steering, not immediate cancellation. Other kinds are labelled
  unverified and remain off unless explicitly enabled.
- **Ask wait:** the board wait and operator popup are harness-independent, but
  the agent must keep a long-running shell command alive. The complete wait,
  answer and unblock round trip is live-verified for all four supported
  harnesses; every other TUI remains unverified.
- **Prompt hooks:** only Claude Code receives board context at prompt submit,
  the session briefing at startup and the Stop check. Other kinds rely on typed
  delivery and re-briefing.
- **Model / effort:** implemented for Claude Code, Codex, OpenCode and Pi.
  Claude switches both live. OpenCode switches effort live through its variant
  picker; model changes use a controlled restart. Codex applies both at resume
  or controlled restart, as does Pi. All four paths preserve the member's exact session
  and were live-verified.
- **Context gauge:** Codex supplies its effective window at runtime. OpenCode
  supplies the active provider/model and its standard catalog limit, but custom
  `opencode.json` limit overrides are not read yet. Claude supplies exact usage
  and its active model, but Synapse currently resolves capacity through a
  compiled Claude model catalog; therefore Claude context capacity is not yet
  fully dynamic. Unknown capacity is shown as unknown and never raises a
  percentage warning. Pi reads its active model's limit directly through its Synapse extension and marks token usage as estimated; after compaction the count can be unknown. Other integrations are not tracked yet.
- **Compact / clear:** implemented and live-verified for Claude Code, Codex, OpenCode and Pi, including native Pi compaction events, session-generation handling and
  one re-brief after each operation. OpenCode 1.18.30 crashes when a fresh TUI
  starts in a very narrow terminal, so Synapse refuses its clear before exit
  when the pane layout is under 38 columns (about a 40-column PTY).
- **Transcript search:** reads the same stores as the context gauge, so it
  covers Claude Code, Codex, OpenCode and Pi; any other kind is reported as
  having no reader rather than returning an empty result.
- **Account usage:** separate from the per-agent context gauge. Anthropic and
  OpenAI Codex are live-verified; Copilot is best effort; Pi and OpenCode depend
  on the provider/login they use, and OpenCode Zen publishes billing rather
  than a quota window.

Herdr also detects `gemini`, `cline`, `kiro`, `amp`, `maki` and `muse`, which
are not among the 17 installable state integrations above. They can join teams
and use the board without an integration; automatic terminal delivery still
requires explicit trust. Synapse does not promise session resume or any
feature in the harness-sensitive matrix for them.

Herdr 0.9.1 also advertises `letta`; Synapse reserves that agent-kind name.
Fresh replacement currently supports Claude Code, Codex, OpenCode, and Pi.

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

# 3. Install the Synapse operating skill for your agents (agents then load their
#    role's guide from the CLI with `herdr-synapse skill get`).
herdr-synapse skill install

# 4. Trust each terminal UI you use (required once per Herdr session).
herdr-synapse kinds trust claude
herdr-synapse kinds trust codex
herdr-synapse kinds trust opencode
herdr-synapse kinds trust pi       # if you use Pi

# 5. Add the richer lifecycle integration for Claude Code.
herdr-synapse hooks install claude

# Pi also needs its native state integration and Synapse telemetry extension.
herdr integration install pi
herdr-synapse hooks install pi
```

### Why these setup commands are separate

| Command | Requirement | What it enables |
| --- | --- | --- |
| `herdr-synapse skill install` | Required for autonomous agent collaboration | Installs the bundled Synapse operating guide where supported agents can discover it, so they know how to identify themselves, read and acknowledge the board, post updates, answer teammates, and recover after context loss. |
| `herdr-synapse kinds trust claude` | Required once per Herdr session when Claude Code members receive terminal delivery | Opens the notifier's explicit trust gate for the `claude` terminal UI. |
| `herdr-synapse kinds trust codex` | Required once per Herdr session when Codex members receive terminal delivery | Opens the same trust gate for the `codex` terminal UI. |
| `herdr-synapse kinds trust opencode` | Required once per Herdr session when OpenCode members receive terminal delivery | Opens the same trust gate for the `opencode` terminal UI. |
| `herdr-synapse kinds trust pi` | Required once per Herdr session when Pi members receive terminal delivery | Opens the same trust gate for Pi; it is separate from project trust. |
| `herdr-synapse hooks install pi` | Required for Pi runtime context and lifecycle observations | Installs a Synapse-owned Pi extension beside Herdr's state integration; it does not enable Claude-style prompt or stop hooks. |
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

If you pasted the block before 0.19, it lacks the `prefix+d` binding for
mission control. `herdr-synapse keys print` prints the current bindings; add
the missing one and reload.

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
load new console code. A notifier whose checkout changed version some other
way (`git pull`, `herdr plugin install`) restarts itself on the new code. That
self-restart arrived in 0.18.1: a notifier older than that exits when its
version changes, so after that one upgrade run `herdr-synapse daemon start`
(or use `herdr-synapse update`, which restarts it for you).

Agents learn new commands from the skill. After an update that raises the
skill version (0.19 ships v11), `herdr-synapse skill check` lists the stale
copies and `skill install` refreshes them, and an agent's own `me` warns it
while its installed skill differs from the CLI.

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
`prefix+f` team knowledge, `prefix+d` mission control.

The [complete command and shortcut reference](docs/reference.md) lists every
CLI parameter, plugin action, console command, and key used inside each view.

## Team templates

Start a whole team from a shape that works: its charter, rules, roles, each
role's Mission and instructions, who manages, how disagreements are handled,
who reviews, and the vocabulary its facts use.

```bash
herdr-synapse template list
herdr-synapse template show research-sprint
herdr-synapse create q4-churn --template research-sprint --new --project ~/research/churn \
    --charter "Is our churn seasonal, and what drives it?"
```

| Template | Roles | Settings |
| --- | --- | --- |
| `research-sprint` | lead (manager), researcher, skeptic (reviews) | disagreements debated for 30 min, then escalated; every work item needs acceptance evidence |
| `content-campaign` | strategist (manager), writer, editor (reviews) | disagreements go straight to the manager or you; the editor approves every asset |
| `vuln-hunt` | lead (manager), hunter, validator (reviews) | findings count only once the validator reproduces them |
| `feature-team` | lead (manager), implementer, reviewer | disagreements observed; acceptance evidence recommended |

Anything you pass yourself wins: your own `--charter`, `--spawn`, `--brief` or
`--instructions`. Live agents work too: `--member <pane>:<role>` takes the role's
Mission and instructions. `herdr-synapse template save <name>` turns the current
team into a template of your own (in your Herdr config), which you can start again
or share as a folder of Markdown.

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
| `/search words "a phrase"` | what members said and did in their own conversations, newest first |
| `/schedule`, `/schedule run\|enable\|disable <id>` | the team's schedules and when each fires next; fire one now or switch it |
| `/model name opus@medium [--restart]` | a member's model and effort; Claude and OpenCode effort can apply live, while Codex/Pi changes and OpenCode model changes apply at resume or `--restart` now |
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

## Mission control

`prefix+d` (or `herdr-synapse mission`) puts every team on one screen, in five
lanes:

| Lane | What lands there |
| --- | --- |
| Needs you | questions to you, agents stuck in an approval dialog, work waiting for your review or decision, disagreements routed to you, operator grants about to expire |
| Blocked | work items reported blocked, and work held by an agent that has gone missing |
| Working | agents in a turn, with the work item each holds |
| Done | work settled in the last day, and agents that finished a turn |
| Idle | agents with nothing in progress |

Each card carries the command that deals with it. In the popup, Enter on an
agent jumps to its pane; on anything else it shows the command.

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

In the console, `/filter teams` shows only what crossed a link and `/filter team` everything else; `board --teams` is the same on the CLI.

Inter-team messages use bold magenta with `[⇄ TEAM IN]` / `[⇄ TEAM OUT]` badges, so they stand out even in the full feed. Direction is relative to the current team's board; monochrome terminals retain bold text and ASCII mode uses `<->`.

In the teams view, `c` on a team row lists the other teams: Enter links or breaks, and a team without a manager says so.

Managers are told about every link in their briefing and in `me`, along with
the command to use. The skill tells every agent that a linked team's manager is
a peer asking, never the operator. A manager cleared after linking pauses the
link rather than dropping messages; dissolving a team breaks its links and
tells the other side. Links are session-scoped: two Herdr servers do not link.

## Replace an exhausted agent

From the member menu, press `0` (Swap agent), choose Claude Code, Codex,
OpenCode, or Pi, optionally enter `model@effort`, and confirm the replacement.
From the board console:

```text
/swap hunt-reviewer codex
/swap hunt-reviewer claude opus@medium
```

The replacement starts a **fresh conversation**. Its briefing includes the
saved member instructions and recent relevant board context; project files
stay in place. The old agent does not need to produce a final summary.
Native conversation history is not converted between agents. Previous
conversation references and model settings are retained for recovery.

The equivalent CLI supports preview, an optional handoff note, and recovery:

```bash
herdr-synapse --team hunt swap hunt-reviewer --to codex --dry-run
herdr-synapse --team hunt swap hunt-reviewer --to codex --handoff-file handoff.md
herdr-synapse --team hunt swap hunt-reviewer --status
herdr-synapse --team hunt swap hunt-reviewer --retry
```

A failed attempt retains its reserved pane. Resolve any login/setup prompt
there and retry; Synapse reuses that instance. Before takeover, `--cancel`
closes the reserved replacement and keeps the source configuration. If the
source pane was already closed, `herdr-synapse resume hunt-reviewer` can reopen
its recorded conversation from a shell pane. Swaps require operator authority
and a trusted destination kind. They inherit the member’s permission policy across agent kinds (YOLO unless configured otherwise). Changing an agent does not restore provider quota.

## Launch permissions: YOLO by default

Existing and new teams default to `yolo`. An operator can select `native` for
an entire team or individual members, including before their first launch:

```bash
herdr-synapse create demo --new --spawn dev:claude --spawn reviewer:codex \
    --brief dev="Implement the change." --brief reviewer="Review and test it." \
    --permissions native --member-permissions dev=yolo

herdr-synapse --team demo permissions                    # every member, source and flags
herdr-synapse --team demo permissions --default native   # team default
herdr-synapse --team demo permissions demo-dev native    # member override
herdr-synapse --team demo permissions demo-dev inherit   # use team default again
herdr-synapse --team demo permissions --default yolo     # restore YOLO default
```

The console supports the same arguments with `/permissions`. `who` shows the
saved next-launch mode. Resolution is **member override → team default → YOLO**.
Only the operator or an operator delegate can change permissions; model-setting
authority alone does not grant that permission.

| Agent | `yolo` adds | `native` behavior |
| --- | --- | --- |
| Claude Code | `--dangerously-skip-permissions` | Own permission settings |
| Codex | `--dangerously-bypass-approvals-and-sandbox` | Own approval and sandbox settings |
| OpenCode | `--auto` | Own permission rules; explicit denies still apply in YOLO |
| Pi | `--approve` (project resources, this run only) | Own project trust; Pi has no built-in tool approval prompts in either mode |
| Gemini CLI, Kimi | `--yolo` (Kimi's `--yolo` still lets the agent ask you questions) | Own settings |
| Cursor Agent | `--force` | Own settings |
| GitHub Copilot CLI | `--yolo` (tools, paths and URLs; Copilot CLI 0.0.381 or newer) | Own settings |
| Qwen Code, Hermes, Qoder, Letta Code, Oh My Pi, Maki, Muse | `--yolo` | Own settings |
| Kilo Code | `--auto` | Own settings; explicit denies still apply in YOLO |
| Devin | `--permission-mode dangerous` | Own settings |
| Amp, Mastra Code | Nothing: their documentation says they run tools without approval prompts by default | Own settings |
| Droid, Grok, Kiro, Cline, Antigravity CLI | No switch Synapse can use yet | Own settings |

The first four rows are live-verified end to end. Gemini, Kimi and Cursor flags
were read from the installed binaries' own `--help`; the rest come from each
vendor's documentation or source and have not been run here, so `permissions`
labels them "not yet live-verified". If one refuses its flag, set that member to
`native` and it launches with its own settings. Some are left out on purpose:
Droid's and Grok's references do not say their flag applies to the interactive
TUI, Kiro's belongs to `kiro-cli chat` and asks for confirmation at startup,
Cline's `--yolo` exits after one turn, and Antigravity's is unconfirmed.

Changes apply to subsequent Synapse-managed spawns, exact-session resumes,
restores, model restarts, OpenCode clear restarts, and fresh agent swaps.
They do not change an already-running process. Swap retries keep their saved
policy; finish or cancel a pending swap before changing it. A queued restart
whose permission policy changed is refused instead of launching with stale flags.

`native` means Synapse does not add a bypass; it does **not** guarantee approval
prompts or a sandbox. Agent configuration, profiles, settings and extensions
still apply. The displayed mode is a saved launch setting, not a verified reading
of the running process. Native approval dialogs may require operator input.
Herdr’s own automatic restoration is separate; use Synapse’s `restore` or
`resume` to rebuild this policy.

## A model and an effort for supported harnesses

Claude Code, Codex, OpenCode and Pi members can carry an optional setting of the
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
| Pi | `--model provider/model`, `--thinking`, `--approve` | next resume or controlled restart; confirmed against runtime telemetry | `off minimal low medium high xhigh max`, subject to the model |

Resolution is the member's own setting, then the team default for its kind,
then the harness default. The setting travels with the session: `resume`
reopens the member with the same flags, and Herdr's `agent.start` receives
them as arguments, never as a shell string.

The permission policy below applies independently of model choice. YOLO is the default; `native` omits Synapse’s bypass flags. An already-running agent added to a team keeps its current mode until a Synapse-managed launch.

`--apply restart` exits the agent cleanly and resumes its own session with the
new flags; the member is never marked missing while that is in progress, and
both the exit and the return are bounded. Controlled restarts preserve the
harness's allowlisted non-permission flags, rebuild the saved permission policy, and Codex restarts suppress its update
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

Each member's file has six sections: Mission, Scope, Constraints, Definition of done, Handoffs, and private Notes. Mission is required at creation; the remaining sections are optional. New teams automatically import saved member edits and the Rules section of `knowledge.md`, then notify affected agents when delivery is safe. Findings remain generated and attributed; private Notes stay out of agent context. Existing teams retain manual adoption until you run `herdr-synapse project sync auto`. Auto-sync trusts everyone who can write these documents. Use `project sync manual` to require explicit `instructions <name> --adopt` again. Conflicting edits are preserved and reported, never silently discarded. [Document sync reference](docs/cli.md#project-sync-automanual)

Fresh CLI-created Claude Code, Codex, and OpenCode conversations also receive their full team-member name as a native session title. Resuming a saved conversation preserves its title. Naming failures appear on the board without preventing team operation.

On daemon load or an explicit `project render`, Synapse completes only the exact revision-1 Mission-only scaffold written by the old creation path. It adds the five empty standard sections without changing the revision or posting to the board, and leaves edited, custom and later-revision documents untouched.

| Command | What it does | Who |
| --- | --- | --- |
| `project set <path>` | records the directory and creates the folder | you |
| `instructions <name> --edit` | that member's own document, in your editor | you |
| edit `members/<name>.md`, then `instructions <name> --adopt` | the same, in your repo | you |
| `knowledge set "…"` | the team's DOs and DON'Ts | you |
| `knowledge add "…"` | one attributed finding (a fact without a subject) | any member |
| `fact add "…" --about X --attribute Y --source URL` | a fact with its subject, source and time | any member |
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
operator authority, and so do the team-wide switches: the `contradictions`
mode, schedules, `template save` and `create --template`, and approving or
closing a work item in place of its named reviewer.

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
teammate's pane, stays yours. So do two things that act as you outside the
team: pairing a phone or changing what it is sent (`remote pair`, `unpair`,
`policy`), and any schedule with a `--precheck`, because that runs a shell
command as you. A delegate may still add schedules without one.

Authority is decided by origin, not by name. A process inside an agent's pane
is that agent whatever its environment claims, and the gates test where a
command came from rather than what it calls itself. A shell Herdr cannot
verify as yours still posts, rendered `(unverified)`, but carries no
authority: `charter`, `knowledge set`, `instructions`, `manager`, `remove`,
`rename`, `operator grant`, `dissolve`, `wipe`, a `contradictions` or
`schedule` change, `remote` pairing and the policies refuse it and say how to be
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

## Work items

A request that must end with a result is a work item. It has a brief that
says what done looks like, an owner, dependencies, optional reviewers, and
attempts that end with an explicit outcome.

```bash
herdr-synapse work add "Map competitor pricing tiers" --to research-analyst \
    --deliverable artifacts/pricing.md --acceptance "every price has a dated source" \
    --review-by role:skeptic
herdr-synapse work add "Draft the pricing page" --to copywriter --deps W-1 --quick
herdr-synapse work list          # what is unfinished, and why
herdr-synapse work next          # exactly what you should run next
```

The owner runs `work claim W-1`, then `work done W-1 --outcome succeeded|failed|partial
--summary "..." --deliverable ... --evidence ...`. Failure is an outcome, never
something buried in prose. With reviewers, a success waits for `work review W-1
--approve` or `--changes "<what to fix>"`. When W-1 is done, W-2's owner is woken:
its dependency finished.

Every change is also an ordinary post on the board (the assignment is a request
to the owner, the settlement a done to the requester and the manager), so it is
delivered through the same idle gates. An agent restarted, cleared or swapped
since it claimed an item is a new generation and cannot settle the old attempt:
it claims again, and the history shows both attempts. `work list --json` and
`work next` give each row the literal command that moves it on, which is what a
manager agent follows.

## Team facts, and what happens when agents disagree

What the team learns is recorded as facts, with where each came from and when
it was true:

```bash
herdr-synapse fact add "Pro plan is $59/mo" --about "Competitor X" --attribute price \
    --source https://example.com/pricing@2026-09-20
herdr-synapse facts --about "Competitor X" --history   # $49 (Aug 2 – Sep 20) -> $59
herdr-synapse facts --as-of 2026-09-01                 # what the team believed then
```

The same statement from a second member becomes support, not a duplicate, and
the number of members behind a fact is its confidence. A member refines or
retires only its own facts; nothing is deleted. `knowledge add` still works and
records a fact without a subject.

Two members giving different values for the same subject and attribute is a
disagreement. What happens next is your switch, per team, and it never limits
what agents may say to each other:

| `contradictions` | What happens |
| --- | --- |
| `off` | nothing; both facts stand |
| `observe` (default) | recorded and marked disputed where you look (mission control, `facts --disputed`); no agent is told |
| `debate` | the two authors are introduced and settle it on the board; unresolved after 30 min, it goes to the manager or you |
| `escalate` | the manager (when not a party) or you decide at once: `fact resolve D-1 --keep F-7` |

```bash
herdr-synapse contradictions                        # the current mode
herdr-synapse contradictions debate --timeout 45m   # operator only
herdr-synapse fact disputes                         # what is open, and between whom
```

A dispute ends as soon as only one of its facts is still current: a side
concedes by retiring or superseding its own fact, or someone who may decide
resolves it with `--keep`, `--keep-both` or `--retire-all`. A manager that is a party to the
dispute cannot decide it. The mode changes who hears about a clash, never
whether a post is delivered: agents can always argue on the board in any mode.

## Recall: search what the team knows

```bash
herdr-synapse recall "enterprise pricing"
herdr-synapse recall "launch date" --kind fact --as-of 2026-09-01
herdr-synapse recall "rate limit" --about "Payments API"
```

One ranked list over the board (archive included), the facts, the work items
with their settlement summaries, and the text files in the team's `artifacts/`.
It needs no model and no service: a SQLite full-text index kept in the team's
state directory, ranked by relevance, recency and how well each fact is
supported. Agents are taught to recall before they start.

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
socket. Synapse has file readers for Claude Code, Codex and OpenCode, plus runtime telemetry from its Pi extension; the notifier polls these sources every fifteen seconds.

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
| Pi | native `getContextUsage()` estimate, labelled `~`; unknown after compaction until Pi reports usage | active model's runtime `contextWindow`, including custom models and model changes |

Any other kind reads `unknown`; Pi alone labels its readings as estimated. At 75 % and again at
90 % the board gets one line addressed to that member and to the team, and
the member is nudged with it. The plugin never acts on it: what to do about a
full context is the member's decision, or yours.

`compact` and `clear` are available for these four kinds. Both operations
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

## Searching what members said

The board holds what a member chose to post. Its harness keeps the rest: what
it was asked, what it answered, which tools it ran and what came back.
`search` looks through that, for the conversations the roster recorded for
each member.

```bash
herdr-synapse search rate limit                         # every word, in one message, any case
herdr-synapse search '"token bucket" refill' --since 1d # a quoted phrase matches exactly
herdr-synapse search pricing --member alpha-analyst --role assistant
herdr-synapse search pricing --history                  # also conversations before a restart, clear, or swap
```

Hits come newest first, each with the member, its kind, the time, who spoke
(user, assistant, or tool), a short excerpt with the match marked `»…«`, and
the session it came from. Claude Code, Codex, OpenCode and Pi conversations
are read, from the same files `context` uses; other kinds say there is no
reader. Nothing is written, the OpenCode database is opened read-only, and a
store that is missing or locked is listed as skipped rather than failing the
search.

| Who runs it | What it may search |
| --- | --- |
| you, or a member you delegated to | any member |
| the team manager | any member |
| any other member | only its own conversations |

A member asking about a teammate is refused and the refusal is audited, the
same rule that keeps agents out of each other's panes. Every search is
audited without its words. Excerpts pass through the same secret patterns
the board refuses, so keys show as `[redacted:<kind>]` and cannot be searched
for. In the console, `/search <words>` shows the same result in a box.

## Coming back from a compaction

An agent that has just been compacted or cleared has lost the team. One command
gives it back:

```bash
herdr-synapse orient
```

It prints who you are, the charter, your brief, your own instructions, the team
rules, your model and effort, your teammates with the manager marked, the
teams you are linked to, the work items you hold, where the team's files are,
and your unread count.
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

`instructions <name>` writes one member's own document: mission, scope, constraints, definition of done, handoffs, and private notes. In auto-sync mode, save `<team>/members/<name>.md` to apply changes and notify that member. In manual mode, `instructions <name> --adopt` shows the diff first. `project` reports the mode; auto-sync trusts project-document writers, including agents.

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
with Claude Code, Codex, OpenCode and Pi; other kinds should be treated as
conditional until verified. If nobody answers within eight minutes the command
gives up with a distinct exit code, the question stays on the board, and the
skill tells the agent not to guess.

Blocking defaults to `question`, `blocked` and `request`; blocking a `done`
notice would freeze a team that posts forty of them. `/ask-policy` in the team
console changes it, or turns waiting off entirely.

## Recurring work on a timetable

Work that comes back every day or every week can post itself. A schedule is a
post you write once; the notifier puts it on the board at the time you chose,
as if you had just written it, and the recipients are nudged the usual way.

```bash
# a marketing team: every weekday morning, the analyst triages yesterday's mentions
herdr-synapse schedule add "Triage yesterday's brand mentions and flag anything urgent" \
  --every weekdays --at 09:00 --tz Europe/Berlin --to role:analyst --name mentions

# a research team: Monday competitor scan, only when the scraper produced new data
herdr-synapse schedule add "Summarise this week's competitor pricing changes" \
  --cron "0 8 * * mon" --to all --precheck "test -s data/pricing-new.csv"

herdr-synapse schedule next          # what fires next, across the team
herdr-synapse schedule list          # every schedule, its next run and its last outcome
herdr-synapse schedule run mentions  # fire one now, without changing its timetable
herdr-synapse schedule disable mentions
```

`--every` takes `hourly`, `daily`, `weekdays` or `weekly` (with `--day`);
`--cron` takes a standard five-field line. Times follow `--tz`, or this
machine's zone, and daylight-saving changes neither skip nor double a run. The
post is marked `[scheduled mentions]` so nobody mistakes it for you typing
now, and `role:analyst` is resolved at each run, so a replaced analyst still
gets it.

A `--precheck` is a shell command run just before: a non-zero exit skips that
run quietly, and a precheck that hangs past its timeout reaches you as a
toast. If the notifier was down when a run was due, the run still happens if
it is at most `--grace` late (30 minutes by default); otherwise you get one
"missed" note on the board instead of a burst of stale posts.

Add `--as-work` and each run is a tracked work item instead of a post: owned by
the recipient when it names one member (open to whoever claims it otherwise),
with `--acceptance "<evidence>"` as its brief, and settled like any other item.

Creating, changing and removing schedules is yours, or an agent's you have
delegated to. A schedule with a precheck runs a command as you, so only you in
person can create, re-enable or run one. The definitions are a readable,
editable `schedules.json` in the team's state directory (`schedule list
--json` prints its path); `/schedule` in the console shows them.

### On your phone

A team left running overnight can still reach you. Phone reach is off until
you pair a channel, and it only makes outbound HTTPS requests; nothing on your
machine listens for connections.

```bash
herdr-synapse remote pair telegram --bot-token-file ~/.config/herdr-bot-token   # then send /start <code> to your bot
herdr-synapse remote pair ntfy --topic my-team-asks --token-file ~/.config/ntfy-token
herdr-synapse remote pair webhook --url-file ~/.config/slack-webhook-url      # Slack-style, send only
herdr-synapse remote test          # one test message
herdr-synapse remote status        # what is paired, what is sent, the last result
herdr-synapse remote policy --text full
herdr-synapse remote unpair
```

Each new question, blocker or request addressed to you arrives with a short
code. Reply `K7QX EU first, then US` and that becomes your answer on the
board, the same answer the popup would have written, marked as sent from your
phone. The waiting agent is released and the ask closes. `K7QX ok` is the popup's acknowledgement, and like
it, it is not approval: to approve something, say so in words. Telegram answers
count only from the one private chat you paired. ntfy answers need an access
token, because an open topic is public. A webhook only sends.

**What leaves this machine.** Messages go to the service you pair: ntfy.sh or
your own ntfy server, Telegram, or your webhook's host. The default `summary`
policy sends the session and team name, who asked, the kind of post and the
reply code, never the text. `--text full` also sends the text, with anything
that looks like a key or password replaced by `[redacted:…]`, capped at 500
characters. `--text none` sends only "1 item waiting in Herdr". Fact conflicts
and failed schedules addressed to you are sent too, unless `--send` leaves
them out. Only items that appear after you pair are sent. Tokens and URLs are
read from a file or an environment variable, never from the command line, and
are kept in a private file (mode 0600) that `unpair` deletes. `doctor` reminds
you while a channel is paired.

A phone answer can answer or acknowledge an ask, and nothing else. It cannot
change the charter, rules, grants or this policy. Pairing and the policy are
operator only.

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

Work items and facts use the same table. Assigning, settling and reviewing
work are ordinary posts to the people involved; when an item's dependency
settles, its owner alone is woken (`work_ready`). A new or retired fact wakes
nobody. A clash under `debate` wakes its two authors and under `escalate` the
manager when it is not a party (otherwise it goes to you), with a toast
either way; under `observe` it is addressed to you only, so no agent sees it. A scheduled post is delivered exactly as if you had
just written it; a schedule that fails toasts you and wakes nobody.

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
- A paired phone (`remote`) can answer or acknowledge an ask that is still
  waiting, and nothing else: its answers never pass an authority gate. The
  same holds for scheduled posts, which are marked `[scheduled <name>]`.
- Contradiction handling never blocks, delays or filters a post. Every mode,
  `off` to `escalate`, changes only who is told about a clash.
- Work settles once, and only for the attempt that claimed it: an agent
  restarted, cleared or swapped since its claim is refused
  (`attempt_fenced`). Only a named reviewer approves an item, or you in its
  place.
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
- [skill-guides/](skill-guides/): the worker, manager, reviewer and librarian guides, and the work, facts, recall and coordination references, served to agents by `herdr-synapse skill get`.
- [templates/](templates/): the built-in team templates, each a folder of Markdown you can copy and adapt.
- [CHANGELOG.md](CHANGELOG.md): what changed in each release, and why.
- [CONTRIBUTING.md](CONTRIBUTING.md): bug reports, pull requests, and validation.
- [SECURITY.md](SECURITY.md): private vulnerability reporting and sensitive-data guidance.

## Status

Current source version: 0.19.1, skill v11.

Claude Code 2.1.267, Codex 0.153.4 and OpenCode 1.18.30 were exercised together
in one disposable Herdr 0.9.0/p22 session. Formation, exact-session resume, idle
nudge, safe `!`, running `!!`, teammate interrupt, blocking ask, model and
effort changes, context readings, compact, clear and re-briefing all completed
end to end for each harness. The notifier's `wrong_target` counter remained
zero, and the complete automated suite covers the surrounding failure paths.

The 0.19 features were run end to end on 2026-09-23 in a disposable, fully
isolated Herdr 0.9.1/p22 session, with Claude Code 2.1.281 and Codex 0.155.1
as members. The run covered:

- a team created from a template;
- a work item through claim, settlement, requested changes and approval;
- a debated disagreement settled by concession;
- an operator-versus-agent disagreement escalated to the manager, which
  resolved it;
- a blocking ask answered from a phone through a local stand-in for ntfy;
- a schedule firing as a work item;
- recall, transcript search, `skill get` in a member's pane, and mission
  control, as its CLI view and as the popup opened by `ui mission` (the
  command behind `prefix+d`);
- generation fencing after a real `clear`;
- the notifier restarting itself on a version change;
- `template save`.

That run found five defects, all fixed with regression tests before release.
Not yet exercised live:

- the hosted ntfy.sh, Telegram and Slack services;
- OpenCode and Pi members using the 0.19 features;
- 0.19 on Linux;
- a person reading the mission control popup on a real screen.

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
