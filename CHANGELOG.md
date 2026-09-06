# Changelog

## 0.1.3 (2026-09-06)

- Each team gets its own colour in Herdr's Agents sidebar. The plugin gives every team one of six colour slots, persisted in `team.json` as `config.color_slot`, and stamps the team name into that slot's metadata token (`team_c1`..`team_c6`); the sidebar block from `setup --print-config` now carries one differently-coloured cell per slot, and a row drops the tokens that have no value, so exactly one coloured team name renders per member. Herdr styles a cell from a fixed colour in your config and cannot colour by a token's value, which is why the slots exist.
- The sidebar rows also show the team name at all: until now the `team` token was stamped but never displayed.
- `doctor` warns when your `config.toml` configures the Agents sidebar but predates the colour cells, and tells you to re-paste the block.

## 0.1.2 (2026-09-06)

- `prefix+t` is a team manager: it shows every team with its agents underneath and the agents that belong to no team below them, folds a team away with Enter, and scrolls when the list outgrows the popup. Enter on a member opens a numbered menu that renames it (the team name and the Herdr agent name together), changes its goal, sends that goal to it, removes it from the team (with or without keeping its Herdr name), or jumps to its pane. Actions run without closing the popup.
- A removed member stays removed: `Team.find` no longer resolves a tombstone ahead of a live member that re-used its name, `remove`, `rename` and `brief --set` refuse a member that already left, and the notifier drops a removed member's queued work instead of keeping it for ever (it could still be delivered).
- A rename carries the member's read position to its new name, so it no longer replays the whole board.

## 0.1.1 (2026-09-06)

- Usage limits across agents: `herdr-team usage` and the `prefix+i` popup (`ui usage`) show the session, weekly, and per-model windows of every provider account the session's agents draw on (Anthropic, OpenAI Codex, GitHub Copilot, Google Gemini), grouped with the agents behind each; tokens never leave the process.
- Agent interrupts: `post --interrupt` (console `/interrupt @name text`) may be typed into a working teammate's turn when the team allows it for that kind (`config.gate.interrupt_kinds`, default Claude only), once per sender and teammate per cooldown (`interrupt_cooldown_ms`, default 10 min); `herdr-team interrupts` and `/interrupts` show or set the policy; the feed shows `⚡INTERRUPT` and `⚡interrupted`, `who` shows `⚡armed|cooldown|kind_not_allowed`.
- The daemon restores its signal handlers when it exits, the Claude hook shim survives dash, and the suite passes on Linux.
- The `herdr-team` launcher follows symlinks, so the `install-cli` link in `~/.local/bin` works (it computed the plugin root from the link's directory).

## 0.1.0 (2026-09-06)

First public release, built and verified against Herdr 0.8.2.

- Teams from live agents: the `prefix+t` picker creates a team or adds agents to an existing one; roles up to 32 characters, unique member names, per-member briefs, a human-owned charter.
- Shared board: append-only, per team, with kinds, replies, references, and attachments (`post --file`, the console's `@@path` project finder).
- Idle-gated delivery: the notifier daemon types a nudge only when a member is idle and stable, never into a dialog, a draft, or a working turn; per-kind trust (`kinds trust`), holds visible in `who` and the console.
- Direct typing: `herdr-team say` and the console's `!name text` / `!!name text` put one recorded line into a member's input box now (console only; every refusal is recorded).
- Console: live feed with per-member colors, `@` names, `@@` files, `!` members, `?` help, receipts, hold reasons; compose popup; team view for the sidebar.
- Claude Code hooks (optional): briefing at session start, board context on each prompt, a Stop check for unread posts.
- Human broadcasts nudge every member; `add` announces the newcomer to the team.
