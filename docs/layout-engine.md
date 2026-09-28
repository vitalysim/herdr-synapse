# Layout engine and edge routers

How the canvas places boxes and draws the lines between them (canvas v2, phase 2). Two registries, both pure Python
(stdlib, 3.9), and neither imports `canvas` or `canvas_kinds`:

- `herdr_team/canvas_layouts/`: **layouts** place sized boxes. A block kind (a graph, a mind map, a section's stack)
  builds a `LayoutRequest` from its members and applies the `LayoutResult`.
- `herdr_team/canvas_routers/`: **routers** draw one connector between two ends. The arrow kind picks one by the arrow's
  `style.route` (its `Kind.reroute` hook), and a graph routes all its edges with one (`route_many`).

Agents never see either: they write structure (`nodes`, `edges`, `groups`, `same_rank`, `order`, `route`) and read back the
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

`run(name, request)` is the only entry point. It validates the request (a `LayoutError` names the field), runs the layout
and enforces the contract: every pin held exactly (else a `LayoutError`: a bug in the layout), coordinates rounded to two
decimals, `bbox` computed. A layout with `pins=False` (the stacks and the grid: a stack is an order) gets its request
without pins, and the result notes `pin_ignored <id>`.

### The contract (`tests/layout_conformance.py`)

1. Deterministic: the same request gives the same result on 3.9 and 3.14; shuffling nodes and edges without changing
   their `order` values changes nothing.
2. Pins hold, exactly.
3. Unpinned nodes keep `gap` apart from each other and from pins; an overlap two pins force is noted `pin_overlap a b`.
4. Fixed point: a request seeded with its own result comes back unchanged.
5. Groups hug their members plus `pad`, nest by `parent`, and hold no other node.
6. No node, one node, disconnected parts, self-loops, duplicate edges, cycles, and 200 nodes with 400 edges all work.
7. No side exceeds `MAX_SIZE` (20,000); a caller refuses a bigger drawing with `too_big`.

### Incremental layout and pins

`LNode.seed` is a node's previous top-left corner. With `incremental=True` the layered layout starts from the seeds' order,
keeps seeded nodes where they were and pushes only what must make room; dummies fill in without pushing. Each connected
component is then shifted so its seeded nodes' median displacement is zero. When every node is seeded and the seeds already
are a layered drawing of the graph (ranks on their lines, nothing too close, groups clean), the result is the seeds exactly.
`relayout: "full"` in a patch sends `incremental=False`: seeds are ignored, pins still hold.

A graph passes each member's pin as `LNode.pin` (and any member the author may not move). A dragged member is pinned by the
core; it is never moved by a layout again until someone unpins it.

## Routers

| Name (aliases) | Module | Route |
|---|---|---|
| `straight` | `straight.py` | Each bound end clipped to its outline toward its neighbouring point, straight through the waypoints. Exactly `canvas._route`: an arrow without `style.route` keeps its geometry byte for byte (T-R1). |
| `curved` (`curve`) | `curved.py` | The straight points, drawn as a smooth curve (`canvas_geometry.curve_pieces`). |
| `orthogonal` (`elbow`) | `orthogonal.py` + `_grid`, `_astar` | Axis-aligned pieces around obstacles grown by `clearance`. Ports at side midpoints, spread when several edges share a side, straight across when two ends face each other; a stub out of each side first (two ends closer than their stubs share the room between them, and meet in one straight piece when they face each other in line). A straight, L or Z shape when one is clear, else A* over a sparse visibility grid (a turn costs `2 x clearance`, running alongside a placed route costs three times its length), 20,000 states per leg, then a straight fallback with `blocked`. Drawn with rounded elbows (`canvas_geometry.rounded_path`); its label sits on the longest inner piece, clear of obstacles. |

`route(name, request)` rounds every point to two decimals; `route_many(name, requests)` spreads ports (`with_slots`) and
lets each route see the ones before it as `others`. An orthogonal arrow keeps a stored route while it still leaves one end,
reaches the other and runs through nothing (`arrow.route_fields`), so moving something else does not redraw it. A graph
routed `straight` (the `force`, `grid` and `radial` default) routes an edge that would cross a node orthogonally
instead, that edge alone (`graph.route_edges`). An arrow between neighbours of a `row` or `column` gets room for its label:
the stack widens that gap (`_zone.stack_arrange`).

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
holds it to the contract. `tests/fixtures/layouts/layout_diagonal.py` and `router_zigzag.py` are dropped in this way by
`tests/test_canvas_layouts.py` and `tests/test_canvas_routers.py`.

## Tests and gates

- `tests/test_canvas_layouts.py`: discovery, the contract over every layout, the dropped-in layout, the `canvas_layout` facade.
- `tests/test_layout_layers.py`: ranking, `same_rank`, `order`, groups, labels, directions, pins, and the gates on
  `tests/fixtures/layouts/*.json` (20 graphs, each recording its Phase 1 crossing count): no fixture crosses more than it
  did, planar DAGs and trees cross nothing, the corpus total is at least 30 % lower (G7); adding a node moves at most 25 %
  of the others and the fixed point is exact (G8); 200 nodes with 400 edges and 500 nodes lay out in time (G9).
- `tests/test_layout_{tree,radial,force,grid,stack}.py`: each layout's own behaviour.
- `tests/router_conformance.py`, `tests/test_canvas_routers.py`: the router contract, generated mazes, ports, the fallback,
  the self-loop, the dropped-in router, and T-R1 on every golden arrow.
- `tests/test_kind_graph.py`, `test_kind_mindmap.py`, `test_kind_sequence.py`, `test_kind_arrow_routes.py`: the kinds
  that use them.
