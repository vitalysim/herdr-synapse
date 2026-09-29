# The canvas benchmark

`tools/canvas_bench.py` asks whether agents draw well on canvas v2. For a
plain-language request an operator would give, does an agent's drawing:

1. apply on the first try;
2. come out with no label overflowing its box, no marks overlapping and no
   arrow running through a mark it does not connect;
3. avoid needless crossings;
4. contain what was asked for: the right labels, relations, tones, chart or
   scene;
5. read back correctly to another agent through `look`?

And how long did it take, and what did it cost? Offline, the same measures run
on hand-written reference drawings. That run checks the scorer, and it
compares the v2 language (components and relations) with the 0.21 language
(shapes, arrows and frames). The build contract is
`.local/prd/canvas-v2-phase6.md` section 6; this page is how to use it.

## Commands

```
python3 tools/canvas_bench.py --check
python3 tools/canvas_bench.py offline [--prompts all|live|01,04] [--language v2|v1|both]
                                      [--controls] [--pictures] [--out DIR] [--json]
python3 tools/canvas_bench.py live --team T --member NAME [--member NAME ...] --prompts live
                                   [--deliver prompt|post --shell-pane ID] [--timeout 420] [--dry-run] --out DIR
python3 tools/canvas_bench.py report DIR [DIR ...] --out DIR
```

- `--check` validates the fixtures: 30 prompts with unique ids, the category
  counts below, at least 8 for non-developer teams, 10 marked live, every data
  file present, and no op names, field names or cell coordinates in the
  request text. It is one of the repository's gates.
- `offline` needs no model and no Herdr. It builds a throwaway team in a
  temporary state root (`tools/canvas_qa.QaTeam`, so nothing real is touched),
  copies the data files into its `artifacts/`, applies the prompt's seed, then
  the reference drawing as a member, and scores it with a second member as the
  reader. It exits 0 only when every v2 reference passes with alignment 1.0
  (drawn and read back) and no hard problem, and every negative control is
  flagged as its file says. It is also a gate.
- `--language v1` (or `both`) scores the 0.21-language references the same
  way, side by side with v2. It never fails the exit code: it is comparison
  data.
- `--pictures` renders each drawing as the agent's PNG through resvg when
  resvg is installed (skipped otherwise).
- `live` drives real agents and is run only by hand, in a disposable Herdr
  session (below). `--dry-run` prints the plan and the number of agent turns
  (prompts times members) and does nothing.
- `report` merges one or more result folders into one `summary.md` and
  `rating.md`.

## The fixtures (`tests/fixtures/canvas_bench/`)

```
prompts/NN-slug.json      one per prompt (30)
data/                     rev.csv, traffic.json, budget.tsv (small, under 50 KB in all)
controls/NAME.json        negative controls (at least 8)
README.md                 the format and how to add a prompt
```

A prompt:

```json
{"id": "01-flow-checkout", "category": "flowchart", "audience": "dev",
 "prompt": "Draw our checkout flow: ...",
 "live": true,
 "artifacts": ["rev.csv"],
 "seed": {"as": "member", "ops": [...]},
 "expect": {
   "kinds_any": ["graph"],
   "labels": ["Shopper", "Checkout API", "Payment approved", "Orders database", "Retry"],
   "relations": [["Checkout API", "Payment approved"], ["Payment approved", "Orders database", "edge"]],
   "gone_relations": [],
   "tones": {"Redis cache": "danger"},
   "stable": true,
   "proposal": {"count": 1, "about": "Budget"},
   "chart": {"type_any": ["bar", "grouped-bar"], "fields": ["month", "revenue"]},
   "scene3d": {"objects_min": 6, "links_min": 2}},
 "reference": [ ... v2 ops ... ],
 "reference_v1": [ ... 0.21-language ops ... ]}
```

- `id` is unique and equal to the file name without `.json`. `prompt` is what
  an operator would say, one to three sentences with concrete names and
  values, never op names, field names or coordinates.
- `expect` keys are optional except `labels`. Labels match case-insensitively
  after folding whitespace; a found text matches when either contains the
  other and the shorter is at least 4 characters.
- A relation is `[from, to]` (a directed edge, the default), `[from, to,
  "contains"]` (a card in a column, a node in a group, a child in a section)
  or `[from, to, "any"]` (an edge either way, or containment).
- `seed.as` is `member` (the tested member drew it earlier, so its edit is
  live) or `operator` (the edit must become a proposal).
- `reference` is required; `reference_v1` is optional (at least 10 prompts
  have one).

To add a prompt: write the file, write a reference that passes, run
`--check` and `offline --prompts NN`.

## The 30 prompts

| Category | Prompts | Of them, for non-developer teams |
| --- | --- | --- |
| flowchart | 3 | onboarding a new hire |
| architecture | 3 | |
| mind map | 2 | coffee grinder research |
| kanban | 2 | a hiring pipeline |
| table | 2 | |
| timeline | 2 | a company history |
| sequence | 2 | |
| chart | 5 (from a CSV, a JSON and a TSV file, and from values in the request) | |
| 3D | 2 | a warehouse floor |
| sticky-note brainstorm | 2 | product names |
| edit of an existing board | 5 | |

The five edits start from a seeded board: add a step to a flow, move a card
and add one, add a table row, restyle two nodes, and change a figure on the
operator's own card, which must become one proposal and leave her card as it
was. Ten prompts are marked live (`"live": true`): one flowchart, architecture,
mind map, kanban, table, timeline, sequence, chart, brainstorm and edit.

## What is scored

One row per prompt and run:

| Field | What it means |
| --- | --- |
| `outcome` | `scored` for a row that measures a drawing, `no_attempt` for a row where there was no drawing to measure (below). Every other field in this table is `null` on a `no_attempt` row |
| `valid_first_try` | offline: the reference applied with nothing refused. Live: the member's first call after the request was delivered applied with nothing refused (read from the attempt trace below); empty when it made no call |
| `attempts`, `refused_total` | calls the member made, and ops refused in them |
| `hard` | `label_overflow`, `overlap` and `arrow_through` from `canvas check` on what the member touched. All three must be 0 |
| `soft` | the other check findings on what it touched (`frame_edge`, `chart_labels`, `scene3d_intersect`, `stray` ...) |
| `crossings`, `block_crossings` | pairs of edges among what it touched that cross away from a shared end: the routed arrows' own, and the ones inside the blocks it drew |
| `crossings_total` | the two added up. This is the one the console line, every table and the controls print, so a row never reports two different counts |
| `drawn` | from the board itself: labels found, relations found (and how many drawn relations were asked for), and whether the kinds, tones, deletions, chart, scene and proposal match `expect` |
| `readback` | the same, computed only from what `look` tells a second member who did not draw it. `drawn` right but `readback` wrong is a readback defect |
| `alignment` | F1 of labels and relations together, for `drawn` and for `readback` |
| `stable` | edits: every seed element not named in the request moved and resized by at most 20 units |
| `pass` | something applied (or the expected proposal exists), `hard` all 0, every expected label read back, at least 80 % of the relations read back, every expected check true, and `stable` where asked |
| `seconds` | live: from delivery to the agent idle again; offline: the apply time. Never gated |
| `tokens` | live, where the harness records them: input, output, cache and total for the window, read from the member's own transcript, rollout log or database, read-only; empty with a reason otherwise |
| `no_attempt` | only on a `no_attempt` row: the `cause`, a sentence saying why, and the pane's own words as `evidence` |

Negative controls (at least 8, one per scorer path) prove the scorer catches
problems: overlapping cards pinned on one cell, a straight arrow through an
unrelated box, a long label on a pre-0.22 box, a missing label, a missing
relation, a reversed relation, a refused first op, a K3,3 graph forced into one
order (crossings), an edit that moves everything, and an edit of the
operator's card that applied live when a proposal was expected.

## Live runs

A live run sends each live prompt to real agents, one at a time on the team
canvas, so nobody's work is scored against someone else's. It refuses to run
unless `HERDR_SESSION` is set and the state root is under `/var/tmp/syn-e2e/`:
it is meant for a disposable, isolated Herdr session, never your own. Per
prompt and member it:

1. clears the canvas (the log keeps a checkpoint) and applies the seed;
2. switches the attempt trace on and notes the canvas version and the time;
3. delivers the request with `herdr agent prompt` (or, with `--deliver post`,
   as your `@member` post typed into a shell pane), followed by one fixed
   line: "Use the team canvas. When you are done, run canvas check, fix what it
   lists, and reply done in one line.";
4. waits until the agent has worked and is idle again (or `--timeout`), then
   until the canvas version has not changed for 5 seconds;
5. asks whether there is a drawing at all: a member that made no call and
   left the board as the seed left it is classified, not scored (below);
6. scores the drawing, saves its picture and appends the row.

Clear anything that blocks an agent (an update prompt, a dialog) before the
run: the bench does not answer dialogs.

**The attempt trace.** Refused calls never reach the canvas log, so first-try
validity needs a record of every call. While a file named `bench.on` exists in
a team's canvas folder (next to `events.jsonl`), every call that applies ops
(the CLI, MCP or the page) appends one line to `attempts.jsonl` in the same folder: the author, how many
ops, which were applied or refused and why, the batch and the version. Only the
bench creates and removes the switch; without it nothing is written, and the
file stops growing at 5 MB.

## Rows that were never attempted

A benchmark that judges models has to be able to say "this was not measured".
Three different things used to arrive as the same failing row: an account out
of quota, a turn that never finished, and a model that answered and drew
nothing. Herdr reports a quota-blocked Claude pane as `agent_status: done`
while the pane itself reads "You've hit your weekly limit" - that is its
detection authority and it is not going to change - so the wait cannot tell a
refused turn from a finished one. On 2026-09-29 exactly that happened.

So the bench decides for itself. A row is a **no-attempt** row when both
sources of evidence agree that nothing happened: the member made no call the
attempt trace could see, **and** the board is exactly as the seed left it.
Either one alone is still a measurement: a batch refused as a whole leaves no mark on the board
but is a call the model made, and anything that applied is a drawing. (Note
that a *refused op* is a real measurement, and `refused_total` is the count of
them; it has nothing to do with this.)

Such a row is not scored. It carries `outcome: "no_attempt"`, every
measurement `null` (never 0, and never `pass: false`), and a `no_attempt`
record:

```json
{"outcome": "no_attempt", "pass": null, "hard": null, "attempts": 0,
 "no_attempt": {"cause": "usage_limit",
   "reason": "the member's pane printed a usage or rate-limit notice, so its harness refused the turn rather than the model answering it: '⚠ Usage limit reached · limit resets 4pm'",
   "evidence": ["  ⚠ Usage limit reached · limit resets 4pm"],
   "pane_source": "recent-unwrapped", "pane_text_read": true,
   "state": "done", "worked": false, "timed_out": false, "waited_seconds": 95.0}}
```

The cause is evidence, not a guess. When a turn produced nothing, and only
then, the bench reads the tail of the member's pane (`herdr pane read <pane>
--source recent-unwrapped`) and records the lines it matched verbatim, so a
reader can see why the row was classified that way instead of taking the
classifier's word for it. Reading the pane can never fail a run: when it
cannot be read the row says so and is classified from the wait alone.

| `cause` | What it means | Whose result it is |
| --- | --- | --- |
| `usage_limit` | the pane printed a usage or rate-limit notice ("You've hit your weekly limit", "Usage limit reached", "limit resets", "Continuing automatically at" ...) | the harness |
| `timeout` | the wait reached `--timeout` with the turn still running | the harness |
| `never_started` | the pane never went to work; the agent never woke up | the harness |
| `drew_nothing` | the member worked, settled, made no call and changed nothing: it answered in words or not at all | the model |

**No-attempt rows are excluded from every rate.** Pass rate, first-try
validity, the hard-problem total, mean crossings, the alignments, the median
seconds and the per-kind table are all computed over the rows that were
attempted; `rows` is still every row in the group, and `scored` and
`no_attempt` say how it splits. Only `total_tokens` covers every row, because
a refused turn can still have cost tokens and that spend is real.

`summary.md` says the count before any table, lists every such row with its
cause and its evidence under "Not measured", and, when a whole agent kind
produced only no-attempt rows, prints **not measured** for that kind instead
of figures. That last part matters: a zero reads as a measurement, and "the
model scored 0" is the one sentence this benchmark must never print by
accident. `drew_nothing` is the one cause that is the model's own answer, and
even then it is not a wrong drawing - read it beside the pass rate, not inside
it.

Old result files have no `outcome` field. They are read as scored rows, which
is what they were.

## Reading the results

A run writes into `--out`:

- `results.json`: the rows above, plus `no_attempt`: one entry per row that
  was never attempted, with its cause, its reason and its evidence.
- `summary.md`: one table per mode (prompt by member, or prompt by language),
  the columns in short form, then the totals (pass rate, first-try validity,
  hard problems, mean crossings, mean drawn and readback alignment, median
  seconds, total tokens) and the list of failures with their reasons. The hard
  problem total must be 0. Rows that were never attempted are counted
  separately, excluded from every rate, and listed under "Not measured"; a
  kind with nothing but those is printed as **not measured** rather than as
  zeros. A drawing that passes `drawn` but not `readback`
  points at `look`, not at the agent. The v1 language rows are there to
  compare: 0.21 drawings of graphs are loose shapes, so they have no block
  readback, and the ops and bytes columns show how much an agent had to write.
- `rating.md`: the owner's clarity form, one row per picture with the request
  and the picture's path. Open each picture in `pictures/`, write a number
  from 1 (unreadable) to 5 (clear at a glance) after "Clarity 1-5:", and a
  sentence after "Would you change anything:".
- `pictures/` (live, and offline with `--pictures`): each drawing as the
  agent's PNG, named `<prompt>-<member>.png`.
