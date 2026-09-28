# Reference: canvas blocks

Served with `canvas` while the canvas is on. A block is one op that gives a
finished, laid-out result: you name the items, their relations and their
tones; the canvas sizes, places and draws them. Every op also takes `id`,
`intent` and a placement (`at`, `right_of`, `below`..., `in`, `gap s|m|l`).

## Content

- `section {title, layout row|column|grid|free, gap, padding s|m|l, align
  start|center|end|stretch, cols, grid "CxR", children | region, w, h, tone}`:
  a titled zone. A stack (`row`, `column`, `grid`) places what joins it (`in`)
  in order; `free` keeps it where you put it, and `grid "8x6"` gives it local
  cells: with `in`, `at` and `points` are cells of the section (`c0r0` its
  corner, `c8r6` the far one).
- `card {title, body, icon, badges, owner, status, detail, size s|m|l, tone,
  variant}`: body lines starting `- ` are bullets; `status` is todo, doing,
  review, blocked or done; `detail` is kept for the tooltip, never drawn.
- `sticky {text, tone, size s|m|l}`: square paper; an idea by default.
- `callout {kind note|tip|important|warning|danger|decision|question, title,
  body, icon}`: the kind picks the tone and icon.
- `heading {text, level 1|2|3, tone, align}` · `badge {text, tone, variant,
  icon, size s|m}` · `icon {name, size s|m|l|xl, tone, label}`: names are
  Lucide's; `canvas icons --search <word>` lists them (`db`, `k8s` work too).

## Structured views

- `table {title, columns, rows, header, zebra, max_lines 1-4}`: a column is a
  title or `{key, title, align, width s|m|l}`; a row is a list of cells or
  `{id, cells, tone}`. Cells wrap and keep `max_lines`; a cell is a part
  (`<row>.<column>`, `h.<column>` for a header).
- `kanban {title, columns [{id, title, tone, limit, cards [...]}]}`: a card is a
  title or a card object. Cards are `<board>.c<n>` (`work.c2`): the alias
  survives a move. A column with a `limit` warns `wip_exceeded`.
- `timeline {title, events [{at, end, title, body, tone, icon, milestone}],
  scale}`: dates are `YYYY-MM-DD` or `YYYY-MM`; without dates the axis is order.

## Changing what exists

- `patch {id: <block>, add, update, remove, set, relayout}`: `add: {"cards":
  [{"title": "x", "in": "done"}]}`, `update: {"rows": [{"id": "r1", "cells":
  {"owner": "pm"}}]}`, `remove: {"cards": ["c3"]}`, `set: {"max_lines": 3}`. An
  unknown key is refused `part_unknown` with the nearest keys. What names a
  removed item goes with it (a node's edges, a participant's messages); a removed
  column's cards move to the first column.
- The same op with the same `id` again updates the block in place (an upsert):
  items match by id, then by exact text; the rest are new or dropped.
- `if_version` guards against a stale read: it is the block's version (the `v`
  `look` prints); refused `canvas_stale` means look again.
- `place {id | ids, right_of | left_of | below | above | in | at, gap, align,
  index}` moves things as one group; `in` joins a stack at `index`.
- `pin {ids}` holds marks: no layout, growth or other author moves them. A
  person's pin (`pins: c3(human)` in `look`) is theirs: an agent cannot move,
  resize, delete or unpin it. Ask them.
- `edit {id, part, text}` changes one part (a table cell, a card's `body`).

The reply's `block` says the box, version, what moved and the pins; the
batch's `geometry` and `check` say where everything ended and what reads badly.

## Examples

A kanban, then a move:

```json
{"op": "kanban", "id": "work", "title": "Launch work", "at": [0, 0], "intent": "track launch tasks",
 "columns": [
  {"id": "todo", "title": "Todo", "cards": ["Rotate API keys", {"title": "Landing page copy v3", "owner": "alpha-worker", "badges": ["P0"]}]},
  {"id": "doing", "title": "Doing", "tone": "warning", "limit": 3, "cards": [{"title": "Gateway JWT check", "owner": "human", "status": "blocked"}]},
  {"id": "done", "title": "Done", "tone": "success", "cards": ["Persona research"]}]}
```

```json
{"op": "place", "id": "work.c2", "in": "work.done", "intent": "the copy is final"}
```

A composed architecture board:

```json
{"ops": [
 {"op": "section", "id": "arch", "intent": "auth redesign overview", "title": "Auth redesign", "tone": "info", "layout": "row", "gap": "l", "right_of": "work"},
 {"op": "card", "in": "arch", "id": "gw", "intent": "gateway", "title": "API gateway", "icon": "network", "body": "Terminates TLS and forwards the JWT", "badges": ["P0"]},
 {"op": "card", "in": "arch", "id": "auth", "intent": "auth service", "title": "Auth service", "icon": "shield-check", "tone": "accent", "body": "Issues and rotates tokens"},
 {"op": "arrow", "from": "gw", "to": "auth", "label": "verify", "route": "orthogonal", "intent": "call path"},
 {"op": "callout", "kind": "decision", "right_of": "arch", "intent": "open question", "title": "Open question", "body": "Keep sessions or go stateless?"}]}
```

A table and a timeline:

```json
{"ops": [
 {"op": "table", "id": "risks", "title": "Launch risks", "below": "work", "intent": "track launch risks",
  "columns": ["Risk", {"title": "Owner", "key": "owner"}, {"title": "Level", "key": "level", "align": "center", "width": "s"}],
  "rows": [["Docs not ready", "writer", "High"], {"id": "price", "cells": ["Pricing unclear", "pm", "Medium"], "tone": "warning"}]},
 {"op": "timeline", "id": "plan", "title": "Launch plan", "right_of": "risks", "intent": "launch dates",
  "events": [{"at": "2026-10-05", "title": "Beta"}, {"at": "2026-10-20", "title": "Docs freeze", "tone": "warning"},
             {"at": "2026-11-03", "title": "Launch", "tone": "success", "icon": "rocket", "milestone": true},
             {"at": "2026-10-06", "end": "2026-10-24", "title": "Private beta"}]}]}
```

A freeform sketch on a section's local grid:

```json
{"ops": [
 {"op": "section", "id": "wire", "intent": "rough wireframe", "title": "Settings wireframe", "layout": "free", "grid": "8x6", "below": "risks", "gap": "l"},
 {"op": "pen", "in": "wire", "intent": "page outline", "points": ["c0r0", "c8r0", "c8r6", "c0r6"], "closed": true, "style": "straight"},
 {"op": "pen", "in": "wire", "intent": "sidebar divider", "points": ["c2r0", "c2r6"], "style": "straight"},
 {"op": "sticky", "in": "wire", "at": "c4r2", "intent": "question", "text": "Should billing live here or in its own tab?", "size": "s"},
 {"op": "shape", "kind": "box", "in": "wire", "at": "c5r5", "intent": "primary action", "text": "Save", "variant": "solid", "tone": "accent", "w": 120, "h": 40}]}
```
