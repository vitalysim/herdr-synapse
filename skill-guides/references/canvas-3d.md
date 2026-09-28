# Reference: canvas 3D scenes

Served with `canvas` while the canvas is on. A `scene3d` is one element: a 3D
scene described by structure (objects and how they sit against each other),
not coordinates. The canvas solves the placement, the page renders it live
(orbit, save a view), and you read it back as text. Use it for physical
layouts (racks, rooms, benches), architecture where height means something,
or a glTF model; flat boxes and arrows read better as a `graph`.

## The op

`scene3d {id, title, objects, links, camera, lights, ground, labels, units, w, h}`

- Axes: y up, x right, z toward the viewer. Sizes are in `units` (`m`; a
  label only). `w`, `h` are the card's size on the canvas.
- `camera`: `iso` (default), `front`, `top`, `side`, `orbit`, or `{"preset":
  "orbit", "az": 30, "el": 20, "zoom": 1.2, "target": "db"}`. `lights`:
  `studio|soft|day|night|flat`. `ground: false` drops the grid. `labels`:
  `auto` (those that fit), `all`, `none`.
- An object: `{id, shape, <its params>, tone, finish matte|glossy|metal|glass,
  opacity 0.2-1, label, rotate (yaw degrees or [x, y, z]), note, <a relation>}`.
  At most 150 objects and 150 links; 48 KB for the whole scene.

| shape | params (defaults) |
|---|---|
| `box` | `size [w, h, d]` (1, 1, 1) |
| `sphere` · `cone` · `cylinder` | `radius` (0.5); `height` (1) for cone and cylinder |
| `plane` | `size [w, d]` (4, 4), `thickness` (0.02): a floor, a table top |
| `text3d` | `text`, `height` (0.3) |
| `arrow3d` | `from`, `to` (object ids or `[x, y, z]`) |
| `group` | `layout` (row, stack, grid, ring, free), `gap`, `cols`, `radius` |
| `gltf` | `src` (under `artifacts/`), `height` or `size` (uniform fit) |

`canvas catalog scene3d` lists every shape with an example.

## Placing: relations

- `on: <id>` rests on its top; `at: [dx, dz]` offsets it from the centre.
- `above` / `below`, `left_of` / `right_of`, `in_front_of` / `behind`: beside
  it with a `gap`; `inside` sits on its inner floor (it must fit);
  `around: <id>` spreads siblings on a ring (`ring` radius, `angle`).
- `gap`: `s`, `m` (default), `l` (10, 25, 50 % of the larger footprint) or a
  number of units. `align start|center|end` on the axes the relation leaves.
- One relation per axis group: `on` + `right_of` + `behind` is fine;
  `right_of` + `left_of` is refused. `pos: [x, y, z]` places absolutely and
  cannot be mixed with relations. A relation cycle is refused, naming it.
- `in: <group>` puts an object in a group; the group's `layout` places it
  (only `free` lets children use relations, to siblings).
- With no relation, objects flow in a row on the ground. Overlaps are pushed
  apart and noted; `overlap: true` means one is intended.
- `links: ["a -> b", "a -> b: label", {"from", "to", "label", "tone"}]`.

## glTF models

- `.glb` (16 MB) or `.gltf` (2 MB of JSON, its `.bin` and textures beside it
  or as `data:` URIs) under the team's `artifacts/`. It is checked and packed
  into one asset when the op applies: a later edit of the file changes
  nothing until you name `src` again or `patch {relayout: "full"}`.
- Refused: Draco, meshopt and KTX2/Basis compression (export uncompressed),
  over 300,000 triangles a model (1,000,000 a scene), textures over 4,096 px,
  a model over 10,000 units across as placed (give `height`) or of no size.

## Reading it back

- `look` prints one line per object, 12 at most (shape, size, label, the
  relation that placed it), the links, notes, conflicts and which stills exist.
  `--full` adds boxes and neighbours; `look --block <id>` prints the full op.
- `look --image --view iso|front|top`: the page's still, else a server drawing.
- `patch`: `add`, `update`, `remove` on `objects` and `links`; `set` the
  settings. A removed object's links go; what was placed against it is freed.

## Checks

`scene3d_intersect` (objects share space: the fix stacks one `above`),
`scene3d_labels` (the fix enlarges the card), `scene3d_relation` (too big to
fit `inside`, or still overlapping), `scene3d_floating` (a `pos` in the air),
`scene3d_heavy` (models the page partly draws as boxes). Each has a fix op.

## Examples

```json
{"op": "scene3d", "id": "topo", "intent": "the prod topology in 3D", "title": "Prod topology",
 "objects": [
  {"id": "base", "shape": "plane", "size": [8, 5], "tone": "neutral"},
  {"id": "lb", "shape": "box", "size": [1.2, 0.4, 1.2], "tone": "info", "label": "Load balancer", "on": "base", "at": [-3, 0]},
  {"id": "api", "shape": "group", "layout": "row", "gap": 0.3, "on": "base", "right_of": "lb", "label": "API pool"},
  {"id": "api1", "shape": "box", "size": [0.8, 1.2, 0.8], "tone": "accent", "in": "api"},
  {"id": "api2", "shape": "box", "size": [0.8, 1.2, 0.8], "tone": "accent", "in": "api"},
  {"id": "db", "shape": "cylinder", "radius": 0.6, "height": 1.4, "tone": "success", "label": "Postgres", "on": "base", "right_of": "api", "gap": 1},
  {"id": "cache", "shape": "sphere", "radius": 0.35, "tone": "warning", "label": "Redis", "above": "api", "gap": 0.3}],
 "links": [{"from": "lb", "to": "api"}, {"from": "api", "to": "db", "label": "SQL"}, {"from": "api", "to": "cache", "tone": "warning"}]}
```

A rack stacked inside a glass cabinet, then a glTF model on a bench:

```json
{"ops": [
 {"op": "scene3d", "id": "rack", "intent": "rack layout for the new cluster", "title": "Rack R12", "camera": "front", "right_of": "topo",
  "objects": [{"id": "cab", "shape": "box", "size": [1.0, 2.0, 1.0], "tone": "neutral", "finish": "glass", "label": "R12"},
   {"id": "nodes", "shape": "group", "layout": "stack", "gap": 0.05, "inside": "cab"},
   {"id": "n1", "shape": "box", "size": [0.9, 0.2, 0.9], "tone": "info", "label": "gpu-1", "in": "nodes"},
   {"id": "n2", "shape": "box", "size": [0.9, 0.2, 0.9], "tone": "warning", "label": "gpu-2 (degraded)", "in": "nodes"},
   {"id": "sw", "shape": "box", "size": [0.9, 0.1, 0.9], "tone": "accent", "label": "ToR switch", "in": "nodes"}]},
 {"op": "scene3d", "id": "robot", "intent": "the arm prototype on its bench", "title": "Arm v3 on bench", "camera": "orbit", "lights": "soft",
  "below": "rack", "objects": [{"id": "bench", "shape": "box", "size": [2.0, 0.9, 1.0], "tone": "neutral"},
   {"id": "arm", "shape": "gltf", "src": "models/arm-v3.glb", "height": 0.8, "on": "bench", "label": "Arm v3"},
   {"id": "tag", "shape": "text3d", "text": "payload 1.2 kg", "height": 0.08, "above": "arm", "gap": 0.1}]}]}
```

Add a replica and save a view (`if_version` is the `v` that `look` printed):

```json
{"op": "patch", "id": "topo", "if_version": 7, "intent": "add a read replica",
 "add": {"objects": [{"id": "replica", "shape": "cylinder", "radius": 0.5, "height": 1.2, "tone": "success", "label": "Replica",
                      "on": "base", "behind": "db", "gap": 0.8}], "links": ["db -> replica"]},
 "set": {"camera": {"preset": "orbit", "az": 60, "el": 30}}}
```

3D data is a chart, not a scene: `chart {"type": "bar3d" | "scatter3d" |
"surface", "x", "y", "z", ...}` (`canvas catalog charts --type bar3d`).
