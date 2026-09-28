# 3D scenes (`scene3d`)

How the canvas holds, solves, draws and reads back a 3D scene (canvas v2, phase 4). An agent describes objects and how
they sit against each other; Python solves the placement, the page renders the solved scene with three.js, and an agent
that cannot read images reads it as text. The skill guide is `skill-guides/references/canvas-3d.md`; this page is the
contract behind it.

Code, all pure Python (stdlib, 3.9):

| Where | What |
|---|---|
| `herdr_team/canvas_kinds/scene3d.py` | The kind: an inline block (`Block.parts == "inline"`), its op, `load` (before the canvas lock), `build`, readback, emit, checks |
| `herdr_team/canvas_scene3d/__init__.py` | The primitive registry (`Primitive`, `Loader`, `Model`, `Face`), `catalog()` |
| `canvas_scene3d/<shape>.py` | One module per primitive: `box`, `sphere`, `cylinder`, `cone`, `plane`, `text3d`, `arrow3d`, `group`, `gltf` |
| `canvas_scene3d/relations.py`, `layouts.py` | The relation and group-layout registries |
| `canvas_scene3d/_spec.py` | Validating objects, links and settings (every refusal names its field) |
| `canvas_scene3d/_solver.py` | The solver and the stored (compact) form |
| `canvas_scene3d/_gltf.py` | Reading, checking and packing glTF 2.0 (`struct` and `json`) |
| `canvas_scene3d/_project.py` | Server projections (`iso`, `front`, `top`) as display-list primitives |
| `canvas_scene3d/_describe.py` | The readback: summary, gist lines, `look --json` facts |
| `canvas_scene3d/_plot3d.py` | The iso plot box the GL chart types (`bar3d`, `scatter3d`, `surface`) draw with |
| `web/src/v2/scene3d/` | The page: one shared WebGL renderer, a builder per primitive, the glTF loader, stills |

Neither `canvas_scene3d` nor its modules import `canvas` or `canvas_kinds`: files come only through the `FetchIO` that
`Block.load` is handed.

## The op

```json
{"op": "scene3d", "id": "topo", "title": "Prod topology", "units": "m", "camera": "iso", "lights": "studio",
 "ground": true, "labels": "auto", "objects": [...], "links": [...], "w": 640, "h": 420}
```

- **Settings** (`patch set`): `units` (`m`, `cm`, `mm`, `units`; a label only), `camera` (a preset of
  `tokens.scene3d.cameras`, or `{preset, az, el, zoom, target}`, what the page's "Save view" sends), `lights` (a preset
  of `tokens.scene3d.lights`), `ground` (a grid under the bounds), `labels` (`auto`, `all`, `none`).
- **Collections**: `objects` (key `id`, at most 150) and `links` (key `a->b`, `a->b#2` for a parallel one; at most 150).
  References: `links.from`/`to` and `arrow3d.from`/`to` drop with their object; `in` and every relation are cleared.
- **An object**: `id`, `shape` (default `box`), the shape's own params, and the common fields `tone`, `finish`,
  `opacity` (0.2 to 1), `label` (80 characters), `rotate` (yaw degrees, or `[x, y, z]` degrees in three.js's `XYZ`
  order), `note` (200 characters, not drawn), `in` (a group), `overlap`, `pos`, one relation per axis group, `at`,
  `align`, `gap`, and for `around` its `ring` and `angle`. Objects are stored as given, in canonical form, with no
  defaults filled in, so `look --block` prints the op that rebuilds the scene (T-B1).
- **Links**: `"a -> b"`, `"a -> b: label"` or `{from, to, label, tone, id}`.
- **Limits**: 150 objects, 150 links, 48 KB of JSON for the whole element (`canvas_limit`: "split the scene, or use a
  glTF model"); models as below.

## Primitives

| Shape | Params (defaults) | Extent (`w, h, d`) | Drawn by the projection as |
|---|---|---|---|
| `box` | `size [w, h, d]` (1, 1, 1) | the size | its visible faces |
| `sphere` | `radius` (0.5) | `2r` each way | a circle |
| `cylinder` | `radius` (0.5), `height` (1), `segments` (24, page only) | `2r, h, 2r` | its hull (two half ellipses and their tangents), and its cap when seen |
| `cone` | `radius` (0.5), `height` (1) | `2r, h, 2r` | the base ellipse and the tangents from the apex |
| `plane` | `size [w, d]`, or none (`"fit"`) to fit what stands on it; `thickness` (0.02) | `w, t, d` (4 × 4 while solving a fitted one) | a thin box |
| `text3d` | `text`, `height` (0.3), `billboard` (true) | the text's measured width at that height | text at its centre |
| `arrow3d` | `from`, `to` (ids or points), `radius` (0.04), `head` (0.15) | the segment's box | an arrow with a head |
| `group` | `layout` (row), `gap` (0.2), `cols`, `radius` | the union of its children | its children |
| `gltf` | `src`, `height` or `size` (a number or `[w, h, d]`), `center` (true) | its native bounds, scaled uniformly | its box, dashed |

A primitive is one module exporting `PRIMITIVES` (`ORDER` sets its place): `normalize(ctx, obj, field)` validates its
params (refusals name `objects[3].radius`), `extent(params)` is its local box (centred on x and z, base at y = 0),
`faces(params, detail)` its closed outward surface, `silhouette(params, proj)` an optional closed-form outline,
`describe(params)` its readback phrase, and `example` one valid op. `tests/scene3d_conformance.py` holds every
primitive to that; `tests/fixtures/prim_torus.py` is the one-module proof. The page needs its builder too
(`web/src/v2/scene3d/primitives/<shape>.js` and `manifest.js`); `tests/test_scene3d_web_contract.py` holds the two
lists equal.

## Relations and the solver

An object's position is the centre of its footprint at its base. `R` is the reference's box.

| Relation | Group | Places | Default gap |
|---|---|---|---|
| `on` | vertical | base = R's top; centred on R, or `at: [dx, dz]` from its centre | 0 (contact) |
| `above` / `below` | vertical | base = R.top + gap / top = R.base − gap | `m` |
| `inside` | vertical | base = R's inner floor (R.base + 0.02), centred; must fit, else the conflict `does_not_fit` | 0 (contact) |
| `left_of` / `right_of` | x | max x = R.min x − gap / min x = R.max x + gap | `m` |
| `in_front_of` / `behind` | z | min z = R.max z + gap / max z = R.min z − gap | `m` |
| `around` | xz | on a ring of `ring` (default R's footprint radius + gap + its own) at `angle` (default evenly among R's ring, from −90°) | `m` |

- Gap tokens `s`, `m`, `l` are 10, 25 and 50 % of the larger of the two footprints' largest side; a number is in units.
- The axes a relation leaves are taken from the first relation that names a reference: base-aligned on y, centred on
  x and z, or as `align` (`start`, `center`, `end`) says.
- One relation per group (a second is refused naming both); `pos` wins over all and is refused with any.
- `register_relation(Relation(...))` adds one: a name, a group, the axes it sets and one pure `place` function.

`_solver.solve(objects, links, units, models)`:

1. Primitive extents, rotated conservatively (the axis-aligned box of the turned box).
2. Groups, children first: a group's children are laid out by its layout (`row`, `stack`, `grid`, `ring`; `free` lets
   them use relations to their siblings), then centred on x and z with the base at y = 0: the group is one box.
3. A stable topological order of each scope (ties by spec order); a cycle is refused `relation_cycle` naming it
   (`a → b → a`), and so is an object placed against something it holds.
4. Placement by relations; objects with none flow in a row along +x from the last such object, `m` apart.
5. Collisions against everything placed before, except what it rests on or sits inside (and their groups) and anything
   with `overlap: true`: a hit is pushed along the relation's direction (`around`: outward; `on`/`inside`: +x) by the
   overlap and the gap, and noted (`c overlapped b by 1.00 m; moved right 1.30`). After three pushes it stays, with the
   conflict `unresolved_overlap`.
6. Fitted floors (a primitive whose `hugs(params)` says so: a `plane` with no `size`), top-level and unturned: its width
   and depth cover the footprint of what rests on it (`on`, `inside`) and of what is placed against those, plus a margin
   (the larger of 10 % of the footprint's longer side and 25 % of its shorter), rounded up to 0.1 and centred under
   them; a floor on a floor is fitted first. Nothing on it: 4 × 4. Readback says `plane 7.1×4.5 fitted`.
7. Links run between the points where the line between two boxes' centres leaves each box.
8. Every number is snapped to 1e-4, so Python 3.9 and 3.14 write the same scene.

The whole solve of 150 objects takes about 45 ms. It runs in `Block.load`, before the canvas lock (with the glTF
reads), and again under the lock only when the op the lock sees differs from what was loaded (a patch).

## The stored element

```json
{"type": "scene3d", "kv": 1, "text": "Prod topology", "alias": "topo", "x": 0, "y": 0, "w": 640, "h": 420,
 "settings": {"units": "m", "camera": "iso", "lights": "studio", "ground": true, "labels": "auto"},
 "objects": [{"id": "base", "shape": "plane", "tone": "neutral"}, "..."],
 "links": [{"id": "lb->api", "from": "lb", "to": "api"}, "..."],
 "solved": {"v": 1, "bounds": [-4, 0, -2.5, 4, 2.22, 2.5],
            "objects": [["base", [0, 0, 0], [0, 0, 0], [8, 0.02, 5], null, ""],
                        ["lb", [-3, 0.02, 0], [0, 0, 0], [1.2, 0.4, 1.2], null, "on base, at (−3.0, 0.0)"],
                        ["flow", [-0.05, 0.45, -1.5], [0, 0, 0], [1.18, 0.19, 0.08], null, "from ball to tank",
                         {"points": [[-0.6, 0.49, -1.5], [0.5, 0.6, -1.5]]}]],
            "links": [["lb->api", "lb", "api", [[-2.4, 0.3497, 0], [-2.1, 0.4146, 0]], null, null]],
            "models": {"arm": {"src": "models/arm-v3.glb", "sha256": "451f…", "asset": "451f0e00acc8280a883c984fd8494b5e.glb", "native": [0.4, 0.8, 0.3],
                               "scale": 1.0, "offset": [0, 0.4, 0], "tris": 12, "facts": {"meshes": 1, "materials": 1, "nodes": ["arm-v3"]}}},
            "notes": ["…"], "conflicts": [{"code": "does_not_fit", "ids": ["n", "cab"], "message": "…", "fix": {"update": {...}}}]}}
```

- An object row is `[id, pos, rot, ext, parent, rel]` (`_solver.OBJECT_ROW`), plus `{"points": [...]}` for an
  `arrow3d`. `pos` is the footprint centre at the base in world units; `rot` the Euler degrees; `ext` the axis-aligned
  size after rotation; `parent` the group it is in; `rel` the relations as read back. Its box is
  `[x − w/2, y, z − d/2, x + w/2, y + h, z + d/2]`.
- A link row is `[key, from, to, [start, end], label, tone]`.
- `models` holds each glTF object's model: the asset the page loads (`<32 hex>.glb`), the native size, the uniform
  `scale` and the `offset` that puts its box's footprint centre at the origin with its base at 0, the triangle count
  and readback facts.
- `notes` and `conflicts` are the solver's (above); a conflict carries the `patch` body that fixes it.

`_solver.compact` and `_solver.expand` convert between this and the dict form `solve` returns; `expand` reads junk as an
empty scene. The page reads both forms (`web/src/v2/scene3d/scene.js`).

## glTF

- `src` under the team's `artifacts/`, read through `FetchIO` (the same root, real-path and `..` rules as chart data):
  a `.glb` of 16 MB or less, or a `.gltf` of 2 MB of JSON or less whose buffers and images are `data:` URIs or relative
  files beside it (`FetchIO.sibling`: no scheme, no `..`, no absolute path, percent-decoded first), 16 MB in all.
- Checked (`_gltf.check`): the GLB header and chunks, `asset.version` 2.0, `extensionsRequired` within
  `KHR_materials_unlit`, `KHR_texture_transform`, `KHR_mesh_quantization`, `KHR_materials_emissive_strength`; Draco,
  meshopt and KTX2/Basis refused with a message; images PNG, JPEG or WebP of 4,096 px a side or less (from their
  headers); every bufferView and accessor inside its buffer; 300,000 triangles a model, 1,000,000 a scene, 10,000
  nodes, 256 levels, no node its own ancestor.
- Bounds: the default scene's, from each `POSITION` accessor's `min`/`max` through the node transforms. A model with no
  size (a zero matrix) or one over 10,000 units across as placed (a node scale of `1e30`, unless `height` or `size`
  brings it back) is refused, as a primitive would be.
- Packing: a `.gltf` (or a `.glb` naming a URI) becomes one GLB (buffers concatenated on 4-byte boundaries, images as
  bufferViews, no URI left); a `.glb` is stored as it is. It goes into the asset store as `glb` (`bctx.store_asset`),
  so the page never reads `artifacts/`, and a later edit of the file changes nothing until an update names `src` again,
  the op is upserted, or `patch {relayout: "full"}` re-reads every model.
- The hostile corpus is `tests/fixtures/scene3d/hostile/*.json`; the model builders are
  `tests/fixtures/scene3d/make_fixtures.py`.

## What agents get back

- **`look`**: the readback line (`7 objects, 3 links · camera iso · bounds 8.0×2.2×5.0 m`), then the gist: one line per
  top-level object with its shape, size, label and relations (`db cylinder r0.6 h1.4 "Postgres" on base, right_of api
  (gap 1.00)`), a group with its members (`id "label"`, and a tone that differs from its siblings'), up to 12 object
  lines (`… +N objects: canvas look --block <id>` past them; `Kind.gist_lines`), then `links:`, `notes:`, conflicts and
  which stills the page posted (`stills iso ✓ front ✓ top ✗`, `Kind.gist_env`). `look --full` lists every object, children included, with its box,
  tone, finish, glTF facts and nearest neighbours (`0.3 m left of api2`). `look --json` adds
  `scene3d: {id: {bounds, objects [{id, shape, aabb, rel}], links, notes, conflicts}}`.
- **`look --image [--view iso|front|top]`**: the page's still of that view when it posted one (light theme), else the
  projection.
- **The projection** (`_project.project(el, view, box)`): an orthographic camera at the view's `az`/`el`
  (`tokens.scene3d.cameras`) fitted to the bounds with a 6 % margin (the camera's `zoom` and `target` apply); the ground
  grid in `base.grid`; the objects far to near (separated boxes by which side of each other they lie, the rest by
  depth); visible faces shaded `mat.<tone>.top|left|right` with `mat.<tone>.edge` edges, glass and opacity as `op`;
  links and arrows as `arrow` primitives; labels on pills above their object, moved up, under it or beside it on a
  leader to keep clear of other labels and objects, and dropped (reported to `scene3d_labels`) when nothing fits. At
  most 3,000 primitives. Cached by content, view and box (64 entries): about 30 ms cold and 2 ms warm for 150 objects.
- **The display list**: a card, the fitted title, and one `slot` (`slot: "scene3d"`, `ref {id, v}`, `views` for iso,
  front and top, `still` the camera's view, `gl: true`, `drawn: true`, `fallback` the projection of that view).

## Checks

| Code | Severity | Fires when | Fix |
|---|---|---|---|
| `scene3d_intersect` | 0 | Two solid boxes share more than 1 % of the smaller's volume with no `overlap`, contact or group between them | `update` one `above` the other with gap `s` (or `overlap: true`) |
| `scene3d_labels` | 1 | The primary projection at the element's size drops labels (or, with `labels: all`, crowds them) | `move` to the size where they fit; `set labels: auto` |
| `scene3d_relation` | 1 | The solver left a conflict | The conflict's own `patch` (a bigger host, a stack above) |
| `scene3d_floating` | 3 | A `pos` object hangs above the ground | `update {pos: null, on: <what is below>}` |
| `scene3d_heavy` | 3 | Its models hold over 600,000 triangles | none |

## 3D data charts

`bar3d`, `scatter3d` and `surface` are chart types (`herdr_team/canvas_charts/<type>.py`, `gl: true`), not scenes. They
compile to an echarts-gl option (`grid3D` orthographic at the iso angles, data through `$doc` refs) and draw the
agent's picture with `_plot3d.Plot3D`: the plot box in iso, its floor and far walls, the marks far to near
(`chart.seq` shades for bars and surface cells, `chart.cat` for scatter groups) and collision-culled tick labels.
