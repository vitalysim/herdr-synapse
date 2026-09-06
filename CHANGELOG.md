# Changelog

## 0.2.0 (2026-09-06)

- A team can have a working directory in your project. `herdr-team project set <path>` records it and creates `<path>/.herdr-team/<team>/`, namespaced so two teams can share one project. It holds `knowledge.md`, a `members/<name>.md` per member, and an `artifacts/` directory members own outright, plus a generated `README.md` and `.gitignore` above them. `herdr-team me` prints the absolute paths, which matters because members of one team routinely sit in different checkouts. The plugin never infers the directory and writes nothing into a project until you set it.
- `prefix+f` (`knowledge-status`, `ui knowledge`) shows every team's knowledge base at a glance, the way `prefix+i` shows usage: folder, rules, which members have instructions, findings, artifacts, and any problem. A team with no folder is printed with the command that gives it one.
- The `prefix+t` tree marks a team that has no folder, its detail line summarises the one that does, and `f` on a team row sets or changes the folder without leaving the popup, prefilled with the directory that team's members share.
- A team can be set up in one step. `create` takes `--project <path>`, `--rules "…"` / `--rules-file`, and `--instructions NAME=TEXT`, and the `prefix+t` wizard has a folder stage that prefills the directory the selected agents already share (Tab skips it). Without `--project`, `create` prints the directory the members share and the exact `project set` command rather than acting on it.
- Per-member instructions: `herdr-team instructions <name> --set "…"` (human only) is the long form of a brief, up to 4000 characters. Agents sharing a checkout read the same `CLAUDE.md`; this is what makes their jobs different. Claude members also receive it in their session context.
- A knowledge base per team: `herdr-team knowledge set "…"` (human only) holds the DOs and DON'Ts and carries operator authority, and `herdr-team knowledge add "<text>"` lets any member append an attributed finding. Rules and findings are deliberately separate authorities: a finding is escaped, is pointed at rather than injected, and can never reach a teammate as an instruction.
- Nothing under a project directory is ever deleted. Removing a member writes a tombstone over its file, renaming one leaves a forwarding note under the old name, and a file the plugin did not write is never overwritten without `--force`.
- The Claude session-start context block is now escaped line by line and capped. It previously interpolated the member's brief raw into text that reaches Claude unfenced.
- A member whose name, role, team and teammates overflowed the 400-character briefing budget was never briefed at all: the error escaped the enqueue path's handler. It now falls back to the short briefing and logs why.
- Changes reach agents instead of only landing on disk. Every rules edit, instructions change, finding, and file added to `artifacts/` appends a board record, so Claude picks it up on its next prompt through the prompt-submit hook and every other kind on its next board read. These are broadcast records, so they deliberately do not nudge anyone mid-turn; `--urgent` on `knowledge set` and `instructions --set` is the opt-in that does.
- The notifier watches the team's `artifacts/` tree and posts one batched record when files are added, changed, or removed, whoever did it: a member, you by hand, or another tool. The first scan after a start only seeds the fingerprint, so a restart never re-announces existing files.
- The skill is v2 and teaches the folder, the member's own instructions file, and the knowledge base.

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
