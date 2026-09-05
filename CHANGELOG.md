# Changelog

## 0.1.0 (2026-09-06)

First public release, built and verified against Herdr 0.8.2.

- Teams from live agents: the `prefix+t` picker creates a team or adds agents to an existing one; roles up to 32 characters, unique member names, per-member briefs, a human-owned charter.
- Shared board: append-only, per team, with kinds, replies, references, and attachments (`post --file`, the console's `@@path` project finder).
- Idle-gated delivery: the notifier daemon types a nudge only when a member is idle and stable, never into a dialog, a draft, or a working turn; per-kind trust (`kinds trust`), holds visible in `who` and the console.
- Direct typing: `herdr-team say` and the console's `!name text` / `!!name text` put one recorded line into a member's input box now (console only; every refusal is recorded).
- Console: live feed with per-member colors, `@` names, `@@` files, `!` members, `?` help, receipts, hold reasons; compose popup; team view for the sidebar.
- Claude Code hooks (optional): briefing at session start, board context on each prompt, a Stop check for unread posts.
- Human broadcasts nudge every member; `add` announces the newcomer to the team.
