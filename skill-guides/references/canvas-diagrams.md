# Reference: canvas diagrams

Served with `canvas` while the canvas is on. Diagrams are blocks: you give
nodes, relations and tones; the canvas sizes every node to its label, lays
them out and routes the edges around what is in the way. Read one back in
`look` (it prints as its op) and change it with `patch`; see `canvas-blocks`
for `patch`, `place`, pins and upserts.

## Graphs

`graph {nodes, edges, groups, layout, direction, same_rank, order, route}`:

- A node is `{id, text, kind, tone, icon, in, detail}`: `kind` is box (the
  default), ellipse, diamond, note, card, sticky or icon; `in` names its group;
  `detail` is the tooltip.
- An edge is a string: `a -> b` (solid), `a --> b` (dashed), `a <-> b` (both
  ends), `a -- b` (no head), with `: label` after it. An object form takes
  `{from, to, label, style, head, tail, tone}`.
- `groups [{id, title, tone, parent}]` draw nested zones around their nodes.
- `layout`: `layers` (also `flow`; the default), `tree`, `radial`, `force`,
  `grid`; `direction` down (the default), right, up or left. `same_rank`
  puts nodes side by side; `order` fixes their order within a rank.
- `route`: `orthogonal` (elbows, the default for layered graphs), `straight`
  or `curved`. `restyle {ids, route: "orthogonal"}` reroutes one arrow; it is
  the fix for `arrow_through`.

Layout is incremental: a patch keeps what did not change where it was. A node
someone dragged is pinned there (`pins: db(human)` in `look`) and never moves;
`unpin` lets the layout have it again (an agent cannot lift a person's pin: ask
them). `look` prints `crossings`; `check` suggests `patch {relayout: "full"}`
when a full layout would cross fewer edges.

## Mind maps and sequences

- `mindmap {title, root, tree | topics, side both|right, branch_tones}`: `tree`
  nests topics (`{"Audience": ["Devs", "Managers"]}`); `topics [{id, text,
  under, tone, icon}]` is the flat form. `patch add: {"topics": [{"under":
  "Blog", "text": "Newsletter"}]}` grows a branch; `look --full` names the ids.
- `sequence {title, participants [{id, text, icon}], messages ["a -> b: call",
  "b --> a: reply"], notes [{over: [ids], text}], groups [{kind loop|alt|opt|
  par, label, from, to}]}`: one element; `from`/`to` are message numbers.
- `mermaid {source}`: a flowchart becomes a graph block; other Mermaid
  diagrams render on the page. Charts are in `canvas-charts`.

## Examples

A flowchart with groups:

```json
{"op": "graph", "id": "checkout", "title": "Checkout flow", "layout": "flow", "direction": "right", "below": "wire", "gap": "l",
 "intent": "show the checkout path for the review",
 "groups": [{"id": "core", "title": "Core services", "tone": "info"}],
 "nodes": [{"id": "user", "text": "Shopper", "icon": "user"},
           {"id": "api", "text": "Checkout API", "icon": "server", "in": "core"},
           {"id": "ok", "text": "Payment approved?", "kind": "diamond", "in": "core"},
           {"id": "db", "text": "Orders database", "icon": "database", "in": "core", "detail": "Postgres 17, primary in eu-west"},
           {"id": "fail", "text": "Show retry and keep the cart", "tone": "danger"}],
 "edges": ["user -> api: HTTPS", "api -> ok", "ok -> db: yes", "ok -> fail: no", "fail --> user"],
 "same_rank": [["db", "fail"]], "route": "orthogonal"}
```

A mind map:

```json
{"op": "mindmap", "id": "launch", "title": "Q4 launch", "right_of": "checkout", "gap": "l", "intent": "brainstorm the launch",
 "tree": {"Audience": ["Platform devs", "Engineering managers"],
          "Channels": {"Podcast tour": [], "Blog": ["SEO pillar post", "Guest posts"], "Conference talk": []},
          "Risks": ["Docs not ready", "Pricing unclear"]},
 "branch_tones": "auto", "side": "both"}
```

An incremental edit (the `if_version` is the graph's `v` from `look`); the
reply says what moved, the crossings, and the pins nobody may move:

```json
{"op": "patch", "id": "checkout", "if_version": 412, "intent": "add a fraud step",
 "add": {"nodes": [{"id": "fraud", "text": "Fraud check", "in": "core"}], "edges": ["api -> fraud", "fraud -> ok"]},
 "remove": {"edges": ["api -> ok"]}}
```
