# Changelog

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
