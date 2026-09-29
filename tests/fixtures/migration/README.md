# Golden 0.21 boards

Boards drawn by the 0.21.2 code itself (commit `cf048860`), for the canvas v2 migration tests
(`tests/test_canvas_migrate.py`, canvas v2 phase 6, 2.5) and the page's migration banner
(`tools/canvas_rig.py v021-<name>`).

Each `v021/<name>/` holds the `events.jsonl` and `scene.json` 0.21.2 wrote, the `assets/` it stored and, for
`legacy-mix`, the `artifacts/` its chart reads.
They were made with `python3 tools/make_v021_boards.py`, which extracts `herdr_team` of `cf048860` with
`git archive` (no branch switch, no worktree) and applies each board over a temporary state root with a fixed
clock. An op 0.21.2 did not have is skipped and a field it did not take is dropped; both are listed below.
Absolute temporary paths in the files are replaced with `/tmp/v021-board`.

The boards: the golden scenes `house`, `flowchart`, `sketch`, `text-notes`, `arrow-labels`, `frame-children`, `agent-board`, `font-sizes`, `i18n` (`tests/fixtures/canvas_scenes`), and `legacy-mix`,
built by the generator: every 0.21 kind (Open Color names and author hexes, `rough: 2`, `font: hand`, a
Vega-Lite chart over a CSV, a Mermaid flowchart, a viz, an image, comments with a mention, a claim, a lock, a
legend entry) and edits made through the 0.21 page. Today's `sketch` and `agent-board` scenes are drawn with v2
components, so their 0.21 boards are written in the 0.21 language in the generator (`sketch_021`, `agent_board_021`).

| Board | Ops applied | Ops skipped (not in 0.21.2) | Fields dropped | Refused by 0.21.2 |
| --- | --- | --- | --- | --- |
| `house` | 10 | none | none | none |
| `flowchart` | 2 | none | none | none |
| `sketch` | 9 | none | none | none |
| `text-notes` | 8 | none | none | none |
| `arrow-labels` | 12 | none | none | none |
| `frame-children` | 32 | none | none | none |
| `agent-board` | 14 | none | none | none |
| `font-sizes` | 11 | none | none | none |
| `i18n` | 11 | none | none | none |
| `legacy-mix` | 24 | none | none | none |

Regenerate (needs git; not a gate): `python3 tools/make_v021_boards.py`. Check: `python3 tools/make_v021_boards.py --check`.
