# Layout engine and edge routers

How the canvas places boxes and draws the lines between them (canvas v2, phase 2). Two registries, both pure Python
(stdlib, 3.9), and neither imports `canvas` or `canvas_kinds`:

- `herdr_team/canvas_layouts/`: **layouts** place sized boxes. A block kind (a graph, a mind map, a section's stack)
  builds a `LayoutRequest` from its members and applies the `LayoutResult`.
- `herdr_team/canvas_routers/`: **routers** draw one connector between two ends. The arrow kind picks one by the arrow's
  `style.route` (its `Kind.reroute` hook), and a graph routes all its edges with one (`route_many`).
- `herdr_team/canvas_readability.py`: **readability** is the one definition of what makes a drawing clear, as numbers.
  The graph kind asks it whether a stored route is still worth keeping, `canvas check` reports on it, and both
  conformance harnesses gate on it. See [Readability](#readability) below.

Agents never see any of the three: they write structure (`nodes`, `edges`, `groups`, `same_rank`, `order`, `route`) and read back the
finished geometry.

## Layouts

| Name (aliases) | Module | What it does | Options |
|---|---|---|---|
| `layers` (`flow`, `layered`) | `layers.py` + `_rank`, `_order`, `_position` | Sugiyama, a port of dagre: cycles broken by the lighter of the depth-first back edges in input order and the greedy feedback arc set; ranks by network simplex; long edges become dummy chains; barycentre sweeps with transpose, groups contiguous and sibling groups in one order on every rank (`_order.reconcile`), `order` lists held; Brandes-Koepf coordinates, then separation and group constraints. Labels get a dummy of their size on the middle rank. Groups, `same_rank`, pins, incremental. | none |
| `tree` | `tree.py` + `_tidy` | A tidy tree for boxes of any size: levels by depth, siblings `gap` apart, cousins `1.5 x gap`. Parents from `data.parent`, else a breadth-first walk of the edges. A pinned node takes its subtree along. | `side`: `right`, `left`, `both` (split by leaf count) |
| `radial` | `radial.py` | Rings around a centre: a tree gets wedges by leaf count, any other graph breadth-first rings. | none |
| `force` | `force.py` | Fruchterman-Reingold, seeded (over 120 nodes, repulsion only within `2k`, through a grid: the grid variant), then snapped to cells as big as the largest node plus `gap`. Pins take their cells. An incremental run lets seeded nodes only settle and places new ones among their neighbours. | `iterations` |
| `grid` | `grid.py` | Row-major in `order`; columns as wide as their widest box, rows as tall as their tallest. | `cols`, `align` |
| `row`, `column` | `stack.py` | In sequence, `gap` apart. | `align` |

`run(name, request, seconds=None)` is the only entry point. It validates the request (a `LayoutError` names the field),
runs the layout under a time budget (below) and enforces the contract: every pin held exactly (else a `LayoutError`: a bug in the layout), coordinates rounded to two
decimals, `bbox` computed. A layout with `pins=False` (the stacks and the grid: a stack is an order) gets its request
without pins, and the result notes `pin_ignored <id>`.

### The contract (`tests/layout_conformance.py`)

1. Deterministic: the same request gives the same result on 3.9 and 3.14; shuffling nodes and edges without changing
   their `order` values changes nothing.
2. Pins hold, exactly.
3. Unpinned nodes keep `gap` apart from each other and from pins; an overlap two pins force is noted `pin_overlap a b`.
4. Fixed point: a request seeded with its own result comes back unchanged, and a redraw of an unchanged graph does
   not cross more nor use more wire at the same crossings (`check_layout_quality`). Every readability figure a layout
   puts in `LayoutResult.stats` matches `canvas_readability` measured on the same result.
5. Groups hug their members plus `pad`, nest by `parent`, and hold no other node.
6. No node, one node, disconnected parts, self-loops, duplicate edges, cycles, and 200 nodes with 400 edges all work.
7. No side exceeds `MAX_SIZE` (20,000) on the conformance graphs. The layered layout closes its ranks up (down to
   `MIN_RANK_GAP`, noted `ranks_closed_up`) when a long chain would run past it; a block laid out bigger than
   `MAX_BLOCK_SIZE` (50,000) is refused `canvas_limit` by the caller.

### Budgets

No op holds the canvas lock for long, whatever the graph (QA phase 2, R2). `canvas_layouts._budget.Budget` carries a
deterministic work cap (units a caller counts) and a wall-clock ceiling behind it; `running(budget)` makes it the one in
force on the thread, and code outside one gets an unlimited budget.

- **Layouts**: `run` gives each layout `LAYOUT_SECONDS` (0.45 s) under the lock, and a new graph's `prepare`
  `PREPARE_SECONDS` (1.2 s) before it. The layered layout stops its network simplex (any tree it passes through is a
  feasible ranking) and its ordering sweeps (the best order so far), and the force layout its rounds (the cells snap
  what has settled); the result notes `layout_budget`. 50 nodes take a small fraction of it.
- **Routing**: a graph routes every edge and detour under one budget: `graph.ROUTE_WORK` plus `ROUTE_WORK_PER_EDGE`
  units (an A* state expanded is one, a leg's set-up `orthogonal.LEG_WORK` and a share of what it scans) and
  `ROUTE_SECONDS`. Each leg's A* takes at most what is left; once it is spent, every remaining route is drawn straight
  through its waypoints at once, marked `blocked`, and the graph notes `route_budget`. Because the cap counts work, not
  time, the same graph routes the same way on every machine; the ceiling only guards a slow one. 50 nodes and 80
  edges on any layout, and 200 nodes and 400 edges on the layered one, route in full on the dev Mac.
- **The crossings check** lays a graph out again only when its layout counts crossings (`Layout.crossings`: the
  layered one) and remembers the count per request.

### Incremental layout and pins

`LNode.seed` is a node's previous top-left corner. With `incremental=True` the layered layout starts from the seeds' order,
keeps seeded nodes where they were and pushes only what must make room; dummies fill in without pushing. Each connected
component is then shifted so its seeded nodes' median displacement is zero. When every node is seeded and the seeds already
are a layered drawing of the graph (ranks on their lines, nothing too close, groups clean), the result is the seeds exactly.
`relayout: "full"` — a field of `graph`, `patch` and `unpin` — sends `incremental=False`: every seed **and** every
stored route is dropped, and the block is drawn as one op would draw it. Pins still hold.

A fresh layout also **spreads its lanes** until the drawing is a shape a view can fit (`layers._lane_scale`): nothing
else bounded a drawing's shape, so a graph only ever grew along the flow and the page fitted the owner's own flow at
51 %. The lane gap grows up to 3x the theme's, never shrinks, and the ranks are never closed up — a board's rank tops
are what an incremental redraw seeds its ranks from, so closing them up broke the fixed point. An incremental run
recovers the spacing the board was drawn with by laying the candidates out and comparing (`_seeded_lane_scale`), so
adding a node does not re-spread a drawing the author is looking at. Spreading stops when the drawing has given up
`FILL_KEEP` of its own density: `screen_use` measures shape and nothing else, so without that floor a square of
white space with eight boxes in it scores beautifully.

An **entry point** is pulled onto the line its own first step continues, as far as the slack in its rank allows
(`_settle_sources`), and on a fresh layout it may swap places with the neighbour standing between it and that line
(`_align_entries`) when the swap crosses no more and costs at most 12 % more wire. When two nodes in one rank feed
the same successor, only one of them can have the line; the metric records the other rather than hiding it.

A **serpentine fold** for a long chain is not shipped, and `layers.py` records why: it is a large win on the shape
(a nine-step pipeline goes from 24.7:1 and 7 % of a view to 1.1:1 and 63 %) and it cannot be stable, because one more
node that breaks the path shape puts every box back into a line.

A graph passes each member's pin as `LNode.pin` (and any member the author may not move). A dragged member is pinned by the
core; it is never moved by a layout again until someone unpins it.

## Routers

| Name (aliases) | Module | Route |
|---|---|---|
| `straight` | `straight.py` | Each bound end clipped to its outline toward its neighbouring point, straight through the waypoints. Exactly `canvas._route`: an arrow without `style.route` keeps its geometry byte for byte (T-R1). |
| `curved` (`curve`) | `curved.py` | The straight points, drawn as a smooth curve (`canvas_geometry.curve_pieces`). |
| `orthogonal` (`elbow`) | `orthogonal.py` + `_grid`, `_astar` | Axis-aligned pieces around obstacles grown by `clearance`. Ports at side midpoints, spread when several edges share a side, straight across when two ends face each other; a stub out of each side first (two ends closer than their stubs share the room between them, and meet in one straight piece when they face each other in line). A stub takes at most half the room in front of its port, and an obstacle grows by `clearance` or only as far as leaves the stubs and waypoints outside it (so a card between two others in a tight stack stays in the way); a free end with less than `MIN_ROOM` in front of its side leaves by a side across. Straight runs of waypoints route as one leg. A straight, L or Z shape when one is clear, else A* over a sparse visibility grid (a turn costs `2 x clearance`, running alongside a placed route costs three times its length), 20,000 states per leg and never more than the budget in force has left, then a straight fallback with `blocked`. A `quick` request (a graph's detour) searches greedily on a grid without corridor midlines. Drawn with rounded elbows (`canvas_geometry.rounded_path`); its label sits on its own line as near its **source** end as a clear spot allows, clear of the obstacles, of its own two ends, of the routes already placed and of the pills already placed, and near enough its own ends to read as theirs (`_label`). Crossing a placed route costs three turns (`_astar.CROSS_COST`), which used to be free. |

`route(name, request)` rounds every point to two decimals; `route_many(name, requests)` spreads ports (`with_slots`) and
lets each route see the ones before it as `others`. An orthogonal arrow keeps a stored route while it still leaves one end,
reaches the other and runs through nothing (`arrow.route_fields`), so moving something else does not redraw it.
Inside a graph, legality is not enough: `graph.route_edges` also asks `canvas_readability.route_good` whether the
stored polyline is still short for the gap it spans (`mdetour <= 1.40`), does not turn back on itself more than once,
turns no more than eight corners, and still carries its label nearer its own ends than any third node. Anything else
is cut again with the rest of the batch, and the op's answer notes `routes_recut` with the edges it cut. A polyline a
person gave its shape — an end the operator pinned, or `moved_by` on the arrow — is evidence of intent and is kept. A graph
routed `straight` or `curved` (the `force`, `grid`, `radial` and `tree` defaults) routes an edge that would be drawn
through a node orthogonally instead, that edge alone (`graph.route_edges`); a curved edge keeps its curve, the detour's
corners turned in small arcs (`graph.curve_friendly`). `check` reads a curved arrow along its drawn curve.
An arrow between neighbours of a `row` or `column` gets room for its label: the stack widens that gap
(`_zone.stack_arrange`); one that skips a card goes around it at any gap.

## Readability

`herdr_team/canvas_readability.py` is pure geometry over what is on the board — the node boxes and the edge polylines
as the router left them — so the same code measures a stored scene, a layout result before routing, and a live block.
Every number goes **down** when the picture gets clearer, except `ink_fill`, `bbox_fill`, `screen_use` and `monotone`.

It exists because a drawing a person called unreadable was reported clean: the checks measured no detour, no
turn-back, no wire-on-wire, no label attribution and no shape, and counted only the crossings between edges with four
distinct ends — which skipped nine of the eleven a reader could see.

| Number | What it is |
|---|---|
| `crossings` / `crossings_seen` | Pairs with four distinct ends whose lines cross; and the count a reader makes, which includes two wires off one node crossing more than 24 units out from it. |
| `mdetour_median` / `mdetour_max` | A route's drawn length over the Manhattan span of its own two ends. An orthogonal route cannot beat 1.00, so this needs no comparison with anything. |
| `reversals_*`, `bends_*` | How often a route turns back on an axis, and how many corners it turns. |
| `label_astray_max` | How far a pill is from its own line. The router must keep this at 0. |
| `label_orphan_max` | How far a pill sits from both ends of its arrow, as a fraction of the span. |
| `label_misattributed` | Pills nearer a third node than to either of their own ends. |
| `edge_over_node`, `edge_near_node` | Routes through, or within 12 units of, a node they do not join. |
| `edge_on_edge_len`, `parallel_bundle_len` | Wire drawn on wire, and wire drawn close enough alongside to read as one line. |
| `content_aspect`, `screen_use` | The drawing's shape, and how much of a 16:9 view it fills once fitted to it. These are one fact: above 16:9, `screen_use == 1.78 / aspect`. |
| `empty_band` | The widest strip across the flow the drawing does not use: no box, no pill, and no wire crossing it. |
| `entry_cross_offset_max` | How far an entry point sits off the line its own first step continues. |
| `band_order` | How far the bands are drawn from the order their author declared them in (Kendall distance). |
| `component_interleave` | Pairs of disconnected components whose spans across the flow overlap. |

`measure(drawn)` is quadratic in the edges' segment pairs and is never on a render or parse path: its callers bound it
with `MAX_NODES` (80) and `MAX_EDGES` (200) and memoise per block version. `edge_quality(...)` is the per-route
variant the router's keep decision calls, once per edge, and is O(1) in that route's own points.

### The checks it gives `canvas check`

Three graph kind checks, all naming the one repair that works, `{"op": "graph", "id": <alias>, "relayout": "full"}`:

| Code | Fires when |
|---|---|
| `crossings_high` | More than `max(3, edges // 6)` crossings a reader can see, and a fresh layout would cross less. |
| `routes_tangled` | `mdetour_median > 1.45`, `mdetour_max > 2.50`, `reversals_max > 3`, or more than 60 units of wire drawn along other wire. |
| `labels_adrift` | More than `max(1, edges // 4)` pills nearer a third node than their own ends, or one more than half its arrow's span from both of them. |

Their thresholds are deliberately **looser** than the gates in `tests/layout_conformance.py`: the gate is about what
the pipeline owes, the check is about what an agent should be told to fix, and a test asserts the two can never
invert. A fourth code, `graph_thin`, waits for a fold that can fit a long chain to a view — a check whose fix does not
work is worse than no check.

## Adding a layout or a router

One module in the package, nothing else. Every public module exporting `LAYOUTS` (or `ROUTERS`) is registered in the
order its `ORDER` says (`canvas_layouts._discovery`); modules starting with `_` are helpers.

```python
# herdr_team/canvas_layouts/diagonal.py
from herdr_team.canvas_layouts import Layout, LayoutRequest, LayoutResult

ORDER = 900

def diagonal(request: LayoutRequest) -> LayoutResult:
    ...  # a top-left corner for every node; hold every pin

LAYOUTS = (Layout(name="diagonal", run=diagonal, router="straight", doc="boxes on a diagonal"),)
```

The new name is at once a `graph` layout (`canvas_layout.LAYOUTS` derives from the registry), and the conformance suite
holds it to the contract. A layout with a long loop checks `_budget.active().over()` in it and returns its best drawing
so far when it is; one that counts crossings in `stats` sets `crossings=True`. A router's search spends from the same
`_budget.active()`. `tests/fixtures/layouts/layout_diagonal.py` and `router_zigzag.py` are dropped in this way by
`tests/test_canvas_layouts.py` and `tests/test_canvas_routers.py`.

## Tests and gates

- `tests/test_canvas_layouts.py`: discovery, the contract over every layout, the dropped-in layout, the `canvas_layout` facade.
- `tests/test_layout_layers.py`: ranking, `same_rank`, `order`, groups, labels, directions, pins, and the gates on
  `tests/fixtures/layouts/*.json` (20 graphs, each recording its Phase 1 crossing count): no fixture crosses more than it
  did, planar DAGs and trees cross nothing, the corpus total is at least 30 % lower (G7); adding a node moves at most 25 %
  of the others and the fixed point is exact (G8); 200 nodes with 400 edges and 500 nodes lay out in time (G9).
- `tests/test_layout_{tree,radial,force,grid,stack}.py`: each layout's own behaviour.
- `tests/router_conformance.py`, `tests/test_canvas_routers.py`: the router contract, generated mazes, ports, the fallback,
  the self-loop, the dropped-in router, and T-R1 on every golden arrow. Plus the quality contract: per route a detour,
  turn-back and bend bound (a non-regression bound only on the mazes, where a long route is correct), and per batch —
  a `fan` of seven edges off one hub, a `corridor` of five through one gap — every pill clear of the obstacles, of the
  other lines and of the other pills and on its own line, and the batch a fixed point.
- `tests/layout_conformance.py`, the readability corpus: nine committed boards under
  `tests/fixtures/layouts/readability/`, each drawn, routed and measured against its own budget under one set of
  global ceilings. Sixteen of those bounds were red on the commit the owner rejected the drawing on.
- `tests/test_canvas_readability.py`: the metric on shapes whose answers can be checked by hand, the keep decision,
  the `relayout` repair through the real canvas, and the three checks (they fire on a tangled board and say nothing on
  every board the pipeline makes).
- `tests/test_kind_graph.py`, `test_kind_mindmap.py`, `test_kind_sequence.py`, `test_kind_arrow_routes.py`: the kinds
  that use them.
- `tests/test_canvas_qa_phase2_verdict.py`: skip arrows in tight stacks, the budgets, tree cross links, long chains,
  freed aliases. `tests/registry_conformance.py`: registry tests that hold whatever modules a package gains.
