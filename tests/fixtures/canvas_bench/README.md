# Canvas benchmark fixtures

`tools/canvas_bench.py` reads this folder (canvas v2 phase 6, section 6). `python3 tools/canvas_bench.py --check`
enforces every rule below.

```
prompts/NN-slug.json   one per request (30): the request, what the board must hold, reference batches
data/                  the files a request names (rev.csv, traffic.json, budget.tsv), copied into the team's artifacts/
controls/NAME.json     drawings that are wrong on purpose (at least 8, one per scorer path); each must be flagged
```

## A prompt

```json
{"id": "01-flow-checkout", "category": "flowchart", "audience": "dev", "live": true,
 "prompt": "Draw our checkout flow: ...",
 "artifacts": ["rev.csv"],
 "seed": {"as": "member", "from": "01-flow-checkout"},
 "expect": {"kinds_any": ["graph"], "labels": ["Shopper", "Checkout API"], "relations": [["Shopper", "Cart"]]},
 "reference": [{"op": "graph", "...": "..."}],
 "reference_v1": [{"op": "shape", "...": "..."}],
 "notes": "why an expectation is the way it is"}
```

- `id` is unique and equals the file stem. `category` is one of flowchart, architecture, mindmap, kanban, table,
  timeline, sequence, chart, 3d, sticky, edit (the counts are fixed: 3, 3, 2, 2, 2, 2, 2, 5, 2, 2, 5). `audience` is
  `dev` or `nondev` (at least 8 `nondev`). Exactly 10 prompts are `live`.
- `prompt` is what an operator would say: plain words, concrete names and values, no op names or fields, no JSON, no
  cells like `c12r4`. Live mode sends it followed by one fixed line (run canvas check, fix, reply done).
- `seed` is drawn before the request: `as: member` (the tested member drew it, so its edit applies live) or
  `as: operator` (an edit of it must become a proposal); `ops` inline, or `from` another prompt's `reference`.
- `reference` (required) is the answer in the v2 language; it must pass with alignment 1.0 drawn and read back, no
  hard problem, on the first try. `reference_v1` (at least 10 prompts) is the same answer in the 0.21 language
  (`frame`, `shape`, `arrow` with `right_of`/`below`); it is comparison data and never fails the gate.

## expect

Only `labels` is required.

| key | meaning |
|---|---|
| `labels` | texts the board must show and read back. A found text matches when it equals the label (case and spaces folded), or when either contains the other and the shorter has at least 4 characters. |
| `relations` | `[from, to]` a directed edge (an arrow, a graph edge, a sequence message, a 3D link; a mind map branch or a headless line either way), `[from, to, "contains"]` (a card in a column, a node in a group, a sticky in a section, a table row's first cell holding the others), `[from, to, "any"]` (either). |
| `gone_relations` | relations that must no longer be there (an edge an edit replaced). |
| `kinds_any` | at least one of these kinds is on the board (`graph`, `kanban`, `table`, `sticky`, ...). |
| `tones`, `status` | `{label: tone}` / `{label: status}`; a list accepts any of its values. |
| `absent_labels` | texts that must not be on the board (an operator's card an agent should only propose to change). |
| `chart` | `{type_any, fields}`: a chart of one of these types over these columns. |
| `scene3d` | `{objects_min, links_min}`. |
| `stable`, `changes` | edits: every seed element not named in `changes` keeps its place within 20 units (and its size, unless it is a container that grows with its content). |
| `proposal` | `{count, about, text}`: the member's change became exactly `count` proposals, on the element whose label matches `about`, whose summary says `text`. |

Relations are precise as well as recalled: every edge drawn between two expected labels must be an expected relation
(when the request names any edge at all), so a reversed or an invented edge lowers the alignment.

## A control

```json
{"id": "c06-reversed-relation", "about": "Edges drawn the wrong way round.",
 "expect_fail": ["relations", "reversed"],
 "expect": {"labels": ["Producer", "Broker", "Consumer"], "relations": [["Producer", "Broker"], ["Broker", "Consumer"]]},
 "reference": [{"op": "graph", "...": "..."}]}
```

A control names a `prompt` to borrow its expectation, seed and data, or has its own `expect` and `seed`. It draws with
`reference`, or appends 0.21-style element `events` straight to the log (a mark sized before 0.22). It is flagged when
every reason in `expect_fail` is among the row's reasons (`overlap`, `arrow_through`, `label_overflow`, `labels`,
`relations`, `reversed`, `first_try`, `stable`, `proposal`, `absent`, `tones`, `kinds`, ...) and its crossings reach
`expect_crossings_min` when it has one.

## Adding a prompt

1. Write `prompts/NN-slug.json` (keep the category counts; replace a prompt rather than add a 31st).
2. `python3 tools/canvas_bench.py --check`.
3. `python3 tools/canvas_bench.py offline --prompts NN --no-controls`: the reference must hold the gate. If it does not,
   the row says why (`missing labels`, `missing relations`, `moved`, a refused op).
4. If the scorer needs a new path, add a control that must be flagged by it.
