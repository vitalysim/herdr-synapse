# Development

Internals, conventions, and the status log for people changing the plugin. The user-facing overview is the [README](../README.md), the complete command and key inventory is [reference.md](reference.md), and the implementation contract is [cli.md](cli.md).

## Layout

```
herdr-plugin.toml        manifest (plan section 10): startup, 10 actions, 3 event hooks, 8 panes
bin/herdr-synapse           sh launcher: HERDR_TEAM_PYTHON > python3 > /usr/bin/python3, refuses < 3.9
bin/hook                 sh gate for manifest events: exit 0 in ~15 ms when daemon.json names a live pid
console.sh               console pane wrapper: prints the relaunch hint and waits on failure
herdr_team/
  __init__.py            VERSION, SKILL_VERSION, PLUGIN_ID
  errors.py              exit-code contract, HerdrTeamError, JSON error emission
  paths.py               state root, socket resolution, slug, session/team layout, pointer file
  api.py                 NDJSON socket client + `herdr` subprocess wrappers (the only door to Herdr)
  store.py               the three locks, atomic writes, BoardStore, Cursors, RosterStore, BoardTailer
  cli.py                 argparse root, Command dataclass, registry, dispatch
  reference_docs.py      generated command, action, key, and console sections of docs/reference.md
  cmd_*.py               command groups (board, roster, misc, hooks, skill, daemon, ui); each exports COMMANDS
  sanitize.py render.py identity.py roster.py charter.py     board and roster logic
  gate.py nudge.py ledger.py daemon.py                        notifier
  hooks.py claude_settings.py                                 event reconciler and Claude hooks
  tui_model.py console.py picker.py compose.py                UIs (curses console, picker, compose popups)
  features.py            the whiteboard switches (session layer, team canvas, viz) and the launch-time MCP spec
  canvas.py              the team canvas: Synapse Sketch ops, events.jsonl and scene.json, look, notices, assets
  canvas_layout.py canvas_mermaid.py   graph layouts (per-node sizes) and the Mermaid flowchart subset parser (used by canvas.py)
  canvas_render.py       SVG sanitiser, the agent's picture (the display list through canvas_svg), PNG through resvg (the RUN hook)
  canvas_geometry.py     pure geometry every renderer shares: arrow routes and heads, label pills, frame titles, bounds, view box, paths
  canvas_display.py      the display list (docs/display-list.md): the scene as primitives with lines broken and colours as tokens
  canvas_svg.py          the display list as canonical SVG, byte-identical to the page's toSVGString (plus badges and grid for agents)
  canvas_check.py        layout checks as a registry (CHECKS, register_check) plus each kind's own checks
  canvas_labels.py       where an arrow label goes: on its route, clear of marks and other labels (pure; canvas stores label_at)
  canvas_blocks.py       blocks (canvas v2 phase 2): the one pipeline for block ops, patch, place, pin and unpin; arranging
                         containers after every op (stacks, hug, pins); the result's geometry and check; look's block readback
  canvas_icons.py        Lucide icons (assets/icons, vendored) resolved by name and drawn as display-list paths
  canvas_layouts/ canvas_routers/   the layout and edge router registries (docs/layout-engine.md)
  canvas_kinds/          the component registry: one module per kind with its ops, size, readback, checks and drawing (sdk.py is
                         the OpContext an op handler draws through; canvas derives every op and kind table from the registry)
  canvas_text.py         the text engine: measure with the bundled fonts' metrics, wrap, fit policies (hug, shrink, scale_shape, clamp, keep)
  canvas_theme.py        tones and variants resolved from canvas_tokens.json (the design tokens); writes assets/canvas/tokens.json
  canvas_fontgen.py      stdlib TrueType reader that writes assets/fonts/font-metrics.json
  canvas_collab.py       collaboration (canvas v2 phase 5, docs/collaboration.md): the review gate and its rule registry,
                         proposals, accept/reject/withdraw, freezes, settings, per-author undo, checkpoints, the ghosts
  canvas_presence.py     presence files under whiteboard/presence/ (members' focus, the operator's pages), never in the log
  canvas_mcp.py          the stdio MCP server (`canvas mcp`) injected at launch for members Synapse starts
  sketch.py              standalone, stdlib-only batch builder for agents (`canvas helper`)
  whiteboard_server.py   the loopback page server: tickets, cookie, CSP, JSON API, SSE, sealed viz frames
  views.py activity.py   generated team views (the page's Team tab); watch cards and the team_doing token
hooks/claude/            the Claude Code hook shim (written by the hooks implementer)
assets/fonts/            Inter and Geist Mono (OFL, unmodified), their licences, font-metrics.json (generated); README.md
assets/canvas/tokens.json   the resolved design tokens the page imports (generated by canvas_theme)
web/                     the whiteboard page sources (Vite, React, Excalidraw; the v2 renderer under web/src/v2/); web/dist/ is the checked-in build
tools/canvas_qa.py       golden scenes: overflow, overlap, contrast in both themes, page lines (--page, --engine v2)
tools/canvas_rig.py      a throwaway page server on one scene for interaction tests (prints its URL as JSON)
skills/herdr-synapse/SKILL.md   printed by `herdr-synapse --skill`
docs/reference.md        complete user-facing command and shortcut reference
docs/cli.md              implementation and JSON command contract
tests/                   unittest suite; tests/support.py has TempState, FakeHerdrServer, FakeApi
```

## Try it

The plugin runs on supported Herdr 0.8.x and 0.9.x builds. Safe single-bang
direct chat is capability-gated: the running server must expose
`agent.prompt_if_idle`; no release number is treated as proof. The
zero-interference way to develop is the sandbox launcher, a named session with its own
config, registry, and state: from a terminal outside Herdr run
`bin/herdr-synapse-sandbox start`, then `prefix+t` inside it. Read
`docs/human-testing.md` (the guided first run) and `docs/capabilities.md`
(every capability, how to drive it from the UI and the CLI, what to expect,
and a test checklist). Short form: `herdr plugin link <this dir>`,
`herdr plugin action invoke herdr-synapse.daemon-start`, `herdr-synapse kinds trust
claude` (and `codex`), paste `herdr-synapse keys print` and `herdr-synapse setup
--print-config` into your config, reload, then `prefix+t` to pick two fresh
panes into a team. A post with no recipient goes to the whole team; `@name`
addresses one member and nudges it; your posts to the whole team nudge everyone,
an agent's only with `--urgent`. `!name text` types
the line into that member's input box right now (`!!name text` even while it
works) and records it as a `direct` post. `@@path` attaches a file to a post
(`@@` lists files); `?` on an empty line shows every command.

## Dev loop

```bash
python3 -m unittest discover -s tests -v            # Homebrew python (3.14)
/usr/bin/python3 -m unittest discover -s tests -v   # Apple python (3.9): both must pass
python3 -m herdr_team.reference_docs --check
python3 -m herdr_team.canvas_fontgen --write        # after changing a font in assets/fonts (tests run --check)
python3 -m herdr_team.canvas_theme --write          # after editing herdr_team/canvas_tokens.json or a kind (tests run --check)
python3 tools/canvas_qa.py [--page [--engine v2]]   # the canvas v2 golden scenes: overflow, overlap, contrast, page lines
python3 -m herdr_team.canvas_display --check-goldens    # display-list goldens (--write-goldens <scene> after a drawing change)
python3 -m herdr_team.canvas_charts._sanitize --check   # the raw ECharts allow table and hostile corpus (--write after a change)
python3 -m herdr_team.canvas_charts._fixtures --check   # the chart option and format fixtures the page tests read
python3 -m herdr_team.canvas_icons --check          # the vendored Lucide icons match their checksums
python3 -m herdr_team.canvas_collab --check-fixtures    # tests/fixtures/collab (results, presence, display, diff vectors) the page reads
python3 tools/canvas_rig.py house --engine v2 --writable  # serve one scene on loopback for a browser or a CDP test
./bin/herdr-synapse --version
./bin/herdr-synapse --skill
./bin/herdr-synapse <command> --help
cd web && npm ci && npm run build                   # only when web/src changes: rebuilds web/dist, MANIFEST.json, licences
```

Canvas tests fake `resvg` through `canvas_render.RUN` and `find_resvg` (and
`tests/support.py` pins `canvas_render.FONT_DIRS` and `BUNDLED_FONT_DIRS` to
none, so resvg's font arguments never depend on the machine or the checkout;
`tests/test_canvas_render_fonts.py` sets the bundle back and, when resvg is
installed, checks the real ink against `canvas_text`), the
page server's process through `whiteboard_server.SPAWN`, and the browser
through `cmd_whiteboard.OPEN_BROWSER`; `tests/test_canvas_e2e.py` runs the
whole path (switch, draw, look, mention, page server on loopback) in one test.

Adding a canvas kind is one module in `herdr_team/canvas_kinds/`, and no
list anywhere: the registry imports every module there whose name does not
start with `_` and registers what it exports, `KINDS` (the `Kind` record with
its hooks: `measure`, `readback`, `checks`, `emit` for the display list,
`hit`, `translate`, `resize`, and an optional `tool` for the page's tool bar)
and `OPS` (each `OpSpec` with its fields and a `create(ctx, op)` that draws
through `canvas_kinds.sdk.OpContext`, never through `canvas`). A module's
`ORDER` sets its place in the registration order (the built-in ones use 10
to 120; without one it comes after them). The op table, the accepted fields,
`look`, `check`, the picture, the page and its tool bar (the `tools` list in
`assets/canvas/tokens.json`), the MCP op table, the canvas section of
`docs/reference.md` and `tools/canvas_qa.py` all follow from the registry;
`tests/test_canvas_one_module.py` drops a `stamp` kind from
`tests/fixtures/kind_stamp.py` into the package to prove it. After adding a
kind, run `python3 -m herdr_team.canvas_theme --write` and
`python3 -m herdr_team.reference_docs --write` for the generated files. Only
content the browser draws itself (a `slot`) needs a renderer under
`web/src/v2/render/slots/`.

A **block** (canvas v2 phase 2, `.local/prd/canvas-v2-phase2.md`) is a kind an
agent describes by structure: its `Kind.block` is a `canvas_kinds.sdk.Block`
(its `Collection`s of items, its `settings`, `normalize`, `build` and `spec`),
and `canvas_blocks` runs every create, upsert, `patch`, part edit and `refit`
through the same path: parse each item, normalize, build through a
`BlockContext` (`root_fields`, `member`, `edge`, `drop`, `set_order`), then
arrange. A container block is stored as a frame (`stored_as="frame"`, the
element carries `block: "<kind>"`; `canvas_kinds.kind_of(el)` finds its kind),
so every frame mechanism holds; its members carry `group` (the root) and `part`
(the item id). Its `Kind.arrange(root, members, env)` returns an
`Arrangement` (member boxes, routes, inner frames, the root's box or none to
hug it); stacks use `_zone.stack_arrange` over `canvas_layouts` (`row`,
`column`, `grid`), and `align: stretch` is the core's. After every op,
`canvas_blocks.settle` arranges what the op changed, innermost first; a moved
member of a positional block is pinned, one in a stack reordered by where its
centre landed (`drop_index`, shared with the page through
`tests/fixtures/display/stack-drop-vectors.json`). Pins are never moved by a
layout, by growth push-out, or by an agent when a person set them
(`tests/test_canvas_pins.py` is the property test); a container someone else
may not edit is neither joined nor re-arranged by them. A `Collection`'s `refs`
say what its items name in other collections (`(field, collection, how)`: `drop`,
`clear`, `key`, `start`, `end`), so `patch remove` takes what names a removed
item with it (`canvas_blocks.cascade`); an adopted member's part is claimed in
the root's `seq`, and a `Kind` with `stretch=False` keeps its shape under
`align: stretch`. `tests/test_canvas_qa_phase2_fixes.py` holds the Phase 2 QA
regressions. `readback` round trip:
`normalize(parse(spec(...)))` equals the normalized op
(`tests/test_canvas_blocks.py` `RoundTrip`). A new block is one module too:
`tests/test_canvas_block_one_module.py` loads `tests/fixtures/kind_checklist.py`
and proves it. Layouts and routers are registries of their own, one module
each: see `docs/layout-engine.md`.

Collaboration (canvas v2 phase 5, `docs/collaboration.md`) is
`canvas_collab`: every op an agent applies runs as before, then one review gate
(`canvas_collab.gate`, over the rules in `register_rule` order) says whether
its element changes go live, become a proposal (`P-n`, a ghost only the
operator accepts), or are refused. Adding a rule is one function and one
`register_rule` call, plus rows in `tests/fixtures/collab/matrix.json` (the
authority matrix `tests/test_canvas_collab_matrix.py` runs); a rule dropped in
from `tests/fixtures/collab_rule_pinned.py` proves it. After changing what a
result, a ghost or presence looks like, run
`python3 -m herdr_team.canvas_collab --write-fixtures` (the page's tests read
`tests/fixtures/collab/`). `tools/canvas_rig.py` takes JSON commands on stdin
(as `drawer`, `peer`, `deputy` or `lead`) so page tests can act as agents.

Icons come from `assets/icons/` (Lucide in the Iconify format, vendored
unmodified; `assets/icons/README.md` has the source, checksums and how to
refresh). `python3 -m herdr_team.canvas_icons --check` verifies them.

The display-list goldens (`tests/fixtures/display/`) hold both SVG writers to
the same bytes: after a change to what a kind draws, run
`python3 -m herdr_team.canvas_display --write-goldens <scene ...>` for the
scenes you own (each scene file has one owner), look at the pictures before
and after, and give the reason in the commit message; the page's vitest parity
test reads the same files. A scene that needs data files names a folder of
`tests/fixtures/canvas_artifacts/` in its `artifacts` field; `canvas_qa` and
`canvas_rig` copy it into the QA team's `artifacts/` before the ops apply.

A kind the browser draws (a `slot`: chart, mermaid, viz, scene3d) uses slot
contract v2 (canvas v2 phases 3 and 4, `.local/prd/canvas-v2-phase3-4.md`
section 1): `Block.load(op, io)` reads `artifacts/` through a `FetchIO` before
the canvas lock (its refusal refuses the op under it), `bctx.store_asset` keeps
a content-addressed asset only when the op applies, `Kind.still_views` names
the stills the page may post (`POST /stills/<E-n>?v=&view=`), `Kind.gist`
reads the element back in words, and `Kind.draw_view` draws a view for
`look --image --view`. `tests/test_slot_contract.py` proves it on a test-only
`gauge` kind (`tests/fixtures/kind_gauge.py`).

Charts (canvas v2 phase 3, `docs/charts.md`) are one module per chart type in
`herdr_team/canvas_charts/` (a `ChartType` with its channels, options and
hooks; `_*` modules are the shared helpers). Python compiles every chart:
validation, the model and doc, the frame (axes, ticks, rotation), the ECharts
option, its own drawing and the gist. `tests/test_charts_registry.py` runs
`tests/chart_conformance.py` over every registered type and drops a
`lollipop` type in from `tests/fixtures/chart_lollipop.py`. After changing a
chart type, run `python3 -m herdr_team.canvas_charts._fixtures --write` (the
option and format fixtures the page's tests read) and, after changing the
raw-option sanitiser, `python3 -m herdr_team.canvas_charts._sanitize --write`
(`assets/canvas/echarts-allow.json` and the shared hostile corpus); tests run
both with `--check`.

Rules for code in this package:

- Python 3.9 syntax: no `match`, no `X | Y` at runtime, `from __future__
  import annotations` first in every module, `tomllib` guarded. No third
  party packages; display width comes from `unicodedata.east_asian_width`.
- Every Herdr call goes through `herdr_team.api`. `HERDR_BIN_PATH` when set,
  never a bare `herdr`; every subprocess has `stdin=DEVNULL`, captured
  output, and a timeout. Every socket call has a client-side timeout. The
  one exemption is `charter edit`, which hands the terminal to
  `$VISUAL`/`$EDITOR` until the human quits it.
- Every `flock` goes through `herdr_team.store.FileLock`; nobody else
  imports `fcntl`. The core three are `<session>/daemon.lock`,
  `<team>/team.lock` and `~/.claude/settings.json.herdr-team.lock`; narrower
  ones exist for restore, links, recall, session names and the remote
  (`grep FileLock`). The canvas adds `<team>/whiteboard/canvas.lock`
  (`canvas_busy`, exit 5) for every canvas write. `flock` is not re-entrant
  and `BoardStore.append` takes `team.lock` itself, so never post to the
  board while holding `canvas.lock`: `canvas.apply_ops` posts after
  releasing it.
- `time.monotonic()` for windows, rate limits, and backoffs; wall clock only
  for TTLs and timestamps.
- Directories 0700, files 0600, `lstat` before every managed path, symlinks
  refused, temp files in the target directory (`store.atomic_write`).
- Every command accepts `--json` and prints one JSON object; errors are one
  JSON object on stderr with the contract exit code (`docs/cli.md` section 2).
- Only the daemon calls `agent.prompt` and `notification.show`, and never on
  a terminal outside a roster.
  A human-only `say` job (the console's `!name text`) asks the daemon to type
  one recorded `direct` line now; agents cannot enqueue it and the daemon types
  only a record whose origin is the verified console.

## Test rig rules (summary of plan section 14)

The owner's Claude runs in `wA:p6` on the default session with
`HERDR_SOCKET_PATH` exported, so a bare `herdr` in any script reaches the
owner's 17 agents. Until milestone M10:

- Never link this plugin into `~/.config/herdr/plugins.json`. The rig has its
  own XDG config and state dirs and its own registry; it writes
  `allowed-sockets` so hooks and actions no-op elsewhere.
- Every Herdr call in rig scripts goes through `$RIG/bin/hr`, which clears
  `HERDR_SOCKET_PATH`, `HERDR_CLIENT_SOCKET_PATH`, `HERDR_WORKSPACE_ID`,
  `HERDR_TAB_ID`, `HERDR_PANE_ID`, sets the rig XDG dirs and
  `HERDR_SESSION=$(cat $RIG/session-name)`.
- Before any mutating step `hr status server` must show a socket under
  `$RIG/xdg-config/herdr/sessions/`. Never target a `wA:*` id.
- Never `herdr server stop`, never `pkill`; kill only the pid in
  `daemon.json` after checking its start time.
- Never write `~/.config/herdr/config.toml` or `agent-detection/`;
  `herdr integration install` needs owner approval.
- Clean up with `hr session stop` and `hr session delete` on names recorded
  in `$RIG/sessions-created` only.
- Unit tests never touch the real HOME, socket, or `~/.claude`; use
  `tests/support.TempState` and `FakeHerdrServer`.

## State layout

```
<STATE>/sessions/<slug>/                      slug: default | <session name> | sock-<sha1[:8]>
  daemon.lock daemon.json daemon.log hooks.log view.json console.json kinds.json who.json
  panes/<terminal_id>.json
  features.json watch.json watch-cache/ whiteboard.json whiteboard.log whiteboard-tickets/   (0.21, the visual layer)
  teams/<team>/ team.json team.lock board.seq board.jsonl charter.md archive/ cursors/ payloads/
                briefings/ notifier/{ledger.jsonl,state.json,jobs/} mute.json audit.jsonl
                whiteboard/{canvas.lock,events.jsonl,scene.json,cursors/,assets/,stills/,renders/,exports/,
                            notices.json,rate.json,mcp.json,archive/,presence/,checkpoints/}
  _archive/<team>-<ts>/
<config_dir>/plugins/config/herdr-synapse/{state-dir, allowed-sockets}
```

State root resolution (`herdr-synapse doctor` prints it): `--team <path>` →
`HERDR_TEAM_STATE_DIR` → `HERDR_TEAM_DIR` → `HERDR_PLUGIN_STATE_DIR` →
pointer file → `${XDG_STATE_HOME:-$HOME/.local/state}/<app>/plugins/herdr-synapse`.

## Status

- 2026-09-28, canvas v2 phase 5 (collaboration, `docs/collaboration.md`), server side: the review gate and its rule
  registry (`canvas_collab`), proposals with accept, reject and withdraw, base and stale edits, lanes with sliding and
  automatic claims, freezes and the collaboration settings, per-author undo with skips, checkpoints and restore, anchored
  comments, presence files with `POST`/`GET /presence` and the SSE `presence` event (`canvas_presence`), the readback in
  `look` and every result, the CLI and MCP commands, the `canvas-collab` reference, the `collab` golden scene, and the
  fixtures the page reads (`tests/fixtures/collab/`). The authority matrix is 512 rows. Behaviour changes: undo skips
  what someone else changed later (`force` restores the old overwrite), an agent's change to the operator's or a peer's
  marks is a proposal where it was refused, a member's drawing in free space claims its area, and delegates no longer
  change the operator's marks live nor undo her batches.
- 2026-09-27, 0.22.0 in progress (canvas v2 foundation and Phase 0; contract
  `.local/prd/canvas-v2-architecture.md`, design `canvas-v2-design.md`, QA
  `canvas-v2-qa.md`). Python side: bundled Inter 4.1 and Geist Mono 1.7.2
  with a stdlib metrics generator; `canvas_text` (measure, wrap, fit policies
  with the invariant held over 10,000 seeded labels); `canvas_kinds` with all
  15 element types registered and a conformance suite
  (`tests/kind_conformance.py`); `canvas_theme` over the designer's token
  file; shapes and graph nodes sized before placement and layout; tones;
  resvg with the bundled fonts; checks as a registry. Calibrated in Chrome
  over `tests/fixtures/canvas-text-corpus.json` (see `assets/fonts/README.md`).
  `tools/canvas_qa.py` server side: 8 of 9 golden scenes pass (baseline 0 of
  9); `house` still overlaps, an absolute-coordinate composition whose grown
  parts collide inside an unlabelled shape. Phase 0.1 (verdict R-1, R-3):
  shapes host what is put on them and growth makes room (`canvas.py`, "making
  room"); arrow labels are placed by `canvas_labels` and drawn in pills above
  every mark on both sides; all 9 scenes pass with and without `--page`, and
  the arrow-label metric (which now also counts other labels' pills) is 0.
- 2026-09-26, 0.21.0 (the whiteboard, canvas and watch; contract
  `.local/prd/canvas-contracts.md`): built by four parallel builders and one
  integration pass. Suite: 2630 tests green under Homebrew python 3.14 and
  Apple python 3.9. `tests/test_canvas_e2e.py` runs the whole path in one
  process (switch, a member's batch, look with a faked resvg, the mention's
  nudge, MCP, the page server started by `whiteboard open`, a page edit, off).
  A real-process smoke with a temp state root and a dead socket (enable, draw,
  `look --image` through the installed resvg 0.47, `open`, ticket to cookie,
  scene, views, a forged `Host`, `disable` stopping the server) passed.
  Pi gets no MCP flag (0.85.1 has no `--mcp-config` and exits on an unknown
  flag; found by reading its source), so Pi members use the canvas CLI.
  Review fixes (`tests/test_canvas_review_fixes.py`): undo skips elements the
  undoer cannot edit and unbinds what pointed at what it deletes; `inside`
  grows a frame only for its editor and never into a lock, and a `move …
  inside` adopts; moves and resizes stay within `MAX_COORD`; a malformed op
  is that op's `op_invalid`, never a crash; the force layout is bounded,
  snapped to a grid and computed before `canvas.lock`; agent ops are capped
  in log bytes (per op and per minute) and the log is read backwards in
  linear time; SVG `<use>` loops, deep trees and expansions refuse; chart
  specs past 64 levels refuse; purge keeps `mcp.json`; `portrait
  --from-todo` checks the switch first; watch redaction covers command-line
  secret shapes and `team_doing` shows only the program. Still open: log
  compaction independent of `clear` (the byte caps bound its growth rate).
  Not yet exercised live: contract section 20 (Codex's MCP flags,
  Claude forwarding `HERDR_PANE_ID` to MCP servers, the page in a real Herdr
  session, `look --exact` against a real browser).

Integration pass of 2026-09-04 (the plugin was developed inside a fork of Herdr; this log moved here with it):

- 2026-09-05: `herdr-synapse say` and the console's `!name text` / `!!name text`
  type one line into a member now (docs/cli.md section 7, capabilities section
  7 and 11). Suite: 1141 tests green under both interpreters.

- Suite: 945 tests, green under Homebrew python 3.14.6 and Apple python
  3.9.6 (3 skips on 3.9). `tests/test_schema_conformance.py` (37 tests)
  checks fakes and call shapes against `tests/fixtures/herdr-api-0.8.2.schema.json`.
- Second review of that pass (`tests/test_review_findings_2.py`, 20 tests):
  a live row with `agent: null` (launch pending) is no evidence of another
  kind, so `roster.rehydrate_match` binds it by terminal and the daemon
  adopts ids only, keeping `starting`/`missing` until a kind is detected
  (the hook path's rule); `on_connected` runs its three phases inside the
  same `_phase` boundary as `tick`, so a `board_locked` at connect time is
  counted and retried by the grace-window poll instead of exiting the
  daemon; `config.gate.post_ttl_ms`, `pair_window_ms` and
  `sample_gap_reset_ms` now govern the TTL, the pair budget window and the
  per-team stability reset; `nudge_focused` is validated against
  `never|always`; `reconcile_console` treats an empty foreground as unknown
  and honours a 15 s `launched_at` grace stamped by every console open.
- Closed in this pass: gate config from `team.json` (`config.gate`, docs
  section 10) and the gate 11 follow-up exception; one rehydration matcher
  (`roster.rehydrate_match`) shared by the daemon and `bind`; the daemon
  loop survives one bad event, job, or member evaluation (`phase_errors`
  counter); pane entrypoints honour `allowed-sockets`; `inbox` and
  `notifier stats` documented; `agent start`, `plugin.pane.open`, and
  `agent.read` parsed by their real server shapes through `herdr_team.api`.
- Hygiene: `roster.join` reaches `ensure_daemon`, so its unit tests patch it
  (an unpatched call double-forks the unittest runner into a real daemon that
  retries against the dead temp socket for 60 s). The daemon now exits, and
  skips the final `daemon.json`, when its `<session>/` directory has been
  removed under it instead of recreating the directory.
- Rig M5 real-delivery pass (2026-09-05, cheap Claude haiku): Claude Code
  2.1.261 paints a prompt suggestion (faint "ghost text", SGR 2) inside the
  prompt box after every turn; the plain detection read cannot tell it from
  a typed draft, so gate 9 held every nudge and briefing as `draft_present`
  while it stayed on screen. The daemon now reads the visible viewport with
  styling (`agent.read --source visible --format ansi`) only when the plain
  text shows a draft and drops the faint runs on the prompt line
  (`gate.styled_prompt_line_text`, `tests/test_ghost_text.py`, live fixtures
  under `tests/fixtures/detection/claude_ghost_suggestion*`). Also observed:
  Herdr's session restore relaunches Claude as `claude --resume <id>`, dropping
  `--model`, `--effort`, `--allowedTools`, and `--settings` (hooks), so a
  restored member runs the owner's default model without the team hooks.
- Rig M7 UI pass (2026-09-05, `tests/test_m7_findings.py`, 9 tests): the
  `/peek` box is now cut from the head when taller than the feed, keeping the
  title border and the bottom of the member's screen (spinner, prompt box,
  status line) instead of blank leading rows (`tui_model.fit_peek`);
  `console.read_key` no longer collapses a burst of Escapes, or Esc followed
  by a control key or curses keycode, into one `ESC` (the extra byte is pushed
  back with `unget_wch`/`ungetch`), which had made six Escapes in the picker
  act as one and swallowed the `ctrl+u` behind them; `view toggle` under a
  foreign owner now means "turn ours on", so `--force` replaces the foreign
  view instead of clearing it when a stale `view.json` still said `on`. Live
  facts recorded: the picker's role field is prefilled with `<kind>-dev` and
  the name with `<team>-<role>`, so a driver must `ctrl+u` before typing;
  `dissolve` keeps the members' Herdr names (documented; `remove` clears
  them); `team_task` lands at the daemon's next heartbeat restamp (10 to 20 s
  after `task`), not at once; the console is a `split` per the manifest even
  though plan 7.3 says "opened as a tab".
- Open: `hooks._reconcile_detected` still carries its own per-pane matcher
  rather than `roster.rehydrate_match` (the two now agree on a null live
  kind, ids adopted and status kept on both paths, and on a different
  detected kind; they still differ on step (d): the daemon's exact-name
  match ignores the kind and reports `kind_changed`, the hook requires the
  kind to match); the gate 11 follow-up is only
  reachable with `done_hold_ms` below the 20 s interval, an owner decision;
  a console pane opened by Herdr directly from the manifest `[[panes]]`
  entry (not through `ui console`) carries no `launched_at`, so only the
  empty-foreground rule protects it while booting;
  `doctor` and `setup` send one `notification.show` probe themselves (plan
  7.2), the single non-daemon caller, which the rule above should either
  bless or the probe should move behind a daemon job.
