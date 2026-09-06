# Changelog

## 0.3.0 (2026-09-06)

- `herdr-team export` saves the whole board to a file, rotated archive segments included, so a finished team leaves a record that outlives its session. Markdown by default (a standalone document with the charter and roster the posts refer to, then every post with its sender, recipients, kind, refs and reply links), plus `json`, `jsonl` (the raw on-disk shape, so it reads back into anything) and `text`. `--since`, `--last`, `--kind` and `--from` narrow it; `--stdout` pipes it; an existing file is refused without `--force` and symlinked targets are refused outright.
- `/export [path] [--format …]` does the same from the team console, which is where you are when you decide you want to keep the board. With no path it writes into the team folder's `exports/` (git-ignored), or your home directory when the team has no folder — never the console pane's own working directory, which is the plugin's.

## 0.2.1 (2026-09-06)

- The console feed follows the newest post again, and says so when it does not. `Up` scrolled the feed and the offset was carried across every refresh for ever, so one keypress left you permanently behind the tail with nothing on screen explaining it — on a live team the console sat on record #64 while the board was at #68. Scrolling back now shows a `4 newer below · End returns to the latest` rule, `End` or `Esc` jumps back, reaching the bottom resumes following, and posting snaps to the latest.
- The console's 250 ms refresh can no longer be lost. `nodelay(False)` is ncurses' *blocking* mode and does not restore a timeout set earlier, so the first `Esc`, paste, or unrecognised escape sequence stopped the console updating until the next keypress. The tick is now re-armed every pass, the refresh runs off the wall clock rather than off an idle key, and `Esc` no longer costs ncurses' default one-second delay. The picker, compose and usage popups had the same defect and are fixed too.
- An idle console no longer re-formats the whole board four times a second: 17.3% CPU at 300 records becomes 0.09%. A ~12-stat signature over everything the model reads gates the rebuild, with a 5 s backstop so relative timestamps keep moving.
- `artifacts_changed` records are short. Listing up to 8 paths per category produced 1003-character records on a real team — twice what the skill asks agents for, and a quarter of the context block every member shares. Records are now summarised by directory and capped at 220 characters: a 40-file data dump reads `artifacts: new 40 files under codex-hunt-researcher/…/victim/`, while a single report is still named in full.
- The artifacts watcher waits for a tree to stop changing before posting, so a build or a data dump is one record instead of one every ten seconds, with a five-minute ceiling for a tree that never settles and a one-minute floor between records. Deferring is lossless: the diff baseline is what the board was last told, not the last scan. `config.artifacts.watch = false` in `team.json` turns it off per team.
- An `artifacts_changed` record no longer holds Claude's Stop hook open. It counted as unread mail, so a teammate saving a file could send a member that had finished its turn back to the board. It still reaches `board --new` and the prompt-submit context.
- A large drop into `artifacts/` no longer makes the watcher announce files as removed that were never deleted. The fingerprint stopped mid-walk at 500 entries, so a 600-file dump evicted a real report from it; deep and wide subtrees are now collapsed to one entry instead.

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
