# The display list

The display list is the contract between the canvas geometry (Python) and
every renderer: the agent's PNG (`herdr_team/canvas_svg.py` through resvg) and
the v2 page (`web/src/v2/render/`). Python turns the canonical scene into a
versioned list of drawing primitives in absolute world units, with text already
broken into lines and colours as theme tokens. Renderers never wrap, measure or
move text, and never compute geometry.

- Producer: `herdr_team/canvas_display.py` (`display_list`, `entries`, `entry`,
  `paints`, `validate`, `dumps`). Each kind draws itself through its
  `canvas_kinds.Kind.emit` hook.
- Served by `GET /api/teams/<team>/display[?since=<version>]` (full or delta).
- Goldens: `tests/fixtures/display/<scene>.json`, `<scene>.light.svg`,
  `<scene>.dark.svg` and `fmt-vectors.json`, regenerated only by
  `python3 -m herdr_team.canvas_display --write-goldens [scene ...]` (only the
  named scenes when given: each scene has one owner, canvas v2 phase 2 D16;
  `--check-goldens` fails when any is stale). The Python writer and the
  page's `toSVGString` must both produce the golden SVG byte for byte.

## Document

```json
{"dl": 1, "generator": "herdr_team.canvas_display 1", "team": "alpha", "version": 412,
 "bbox": [-40, -40, 1320, 760],
 "layers": ["zones", "marks", "labels", "overlays"],
 "fonts": {"sans": {"family": "Inter", "weights": [400, 500, 600, 700]}, "mono": {"family": "Geist Mono", "weights": [400]}},
 "palettes": {"light": {"base.canvas": "#f7f8fa", "tone.info.fill": "#e8f2fe", "...": "..."}, "dark": {"...": "..."}},
 "shadows": {"light": {"1": [{"x": 0, "y": 1, "blur": 2, "color": "#101828", "alpha": 0.06}]}, "dark": {}},
 "entries": [Entry, ...]}
```

- `bbox` is `[x0, y0, x1, y1]`: every entry's `bbox` padded by 40, grown until
  it also holds each frame title and each claim's label as a 1024-pixel picture
  of the board draws them (the settling rule of `canvas_geometry.view_box`,
  which answers this box when the board has claims). The page fits to it.
- `entries` come in render order: layer (`zones`, `marks`, `labels`,
  `overlays`), then `z`, then the number in the id, then the id. Within an
  entry, `items` draw in list order. Claims have `z` -1 and locks -2.
- A **delta** (`GET /display?since=S`) is
  `{"dl": 1, "version": V, "since": S, "full": false, "bbox": [...], "upserts": [Entry], "removes": ["E-9", "K-2"]}`.
  With `"full": true` it is a whole document instead (`entries`, palettes,
  shadows and fonts), and the page replaces what it holds.
- `dl` changes only for a breaking change. A new primitive kind or an optional
  field keeps it; renderers skip what they do not know.

## Entry

One per element, claim (`K-n`), lock (`X-n`), and since phase 5 freeze (`X-n`) and open proposal (`P-n`).

```json
{"id": "E-4", "kind": "box", "layer": "marks", "z": 7, "v": 12,
 "bbox": [120, 40, 332, 104],
 "hit": {"shape": "rect", "box": [120, 40, 212, 64]},
 "handles": "box", "connect": true,
 "edit": {"field": "text", "value": "Checkout API", "box": [136, 52, 180, 40], "font": "sans", "weight": 500,
          "size": 20, "lh": 25, "align": "center", "wrap": "box", "fill": "tone.neutral.text"},
 "frame": "E-2", "author": "alpha", "chip": {"bg": "chip.0.bg", "fg": "chip.0.fg", "initials": "AL"},
 "locked": false, "items": [Primitive, ...]}
```

| Field | Meaning |
|---|---|
| `id`, `kind` | Element id and its kind (`canvas_kinds.kind_of`: a frame with `block: "section"` is a `section`, a plain frame a `frame`), or `claim` / `lock`. An unknown type keeps its name and draws the placeholder card. |
| `layer` | The entry's layer; an item may override it with its own `layer` (an arrow's label pill is in `labels`). |
| `z`, `v` | The element's `z` and its `updated_seq`. Memoise an entry by `(id, v)`. |
| `bbox` | `[x0, y0, x1, y1]` of everything the entry draws at any zoom, except the title above a frame (it depends on the zoom). |
| `hit` | What a click selects. Boxes are `[x, y, w, h]`. `rect`, `ellipse`, `diamond` `{box}`; `line {points, tol_px}` (an arrow adds `pill: [x, y, w, h]`); `frame {box, band}` (only the title band and an 8 px screen rim hit); `pin {x, y, r_px}`; `none`. |
| `handles` | `box` (8), `width` (east and west), `ends` (an arrow's start and end) or `none`. |
| `connect` | An arrow may start or end on it. |
| `edit` | What a double-click edits, or null. `box` is `[x, y, w, h]` in world units; `value` is the whole text; `align` is `center` or `start`; `wrap` is `box`, `width` (a text with a wrap width), `auto` (a text without one) or `line` (one line, a frame title). |
| `frame` | The element's frame, or null. |
| `author`, `chip` | The author and their chip: token references plus up to two initials. The operator is `chip.human`, initials `OP`. |
| `locked` | The element lies in a lock's region. |
| `until` | Claims only: the ISO expiry. |
| `parts` | Optional (phase 2). The parts a person may hit and edit one by one: a table's cells, a sequence's participants and messages, a card's `title` and `body`. `[{"part": "r2.owner", "hit": {"shape": "rect", "box": [x, y, w, h]}, "edit": Edit \| null, "lod": [min, max]?}]`, hit before the entry's own `hit`, the last in the list on top. Their `edit` is an edit object with `"part"`; a commit sends `edit {id, part, text}`. |
| `pin` | Optional: `"human"` or `"agent"`, who pinned it (no layout, growth or other author moves it). |
| `block`, `part` | Optional, together: a block member's root id and its item id (`c2`). |
| `container` | Optional: a stack container's `{"layout": "row" \| "column" \| "grid", "gap": n, "order": [ids]}`, the page's drop hint. The index a drop takes is `tests/fixtures/display/stack-drop-vectors.json`'s rule. |
| `tip` | Optional: the element's `detail`, cut to 500 characters, for a tooltip. |
| `frozen` | Optional (phase 5): `true` when a freeze covers the element (an id freeze's elements and what they hold, or a region freeze's). |
| `pending` | Optional (phase 5): the open proposals (`["P-3"]`) aimed at the element; the page may mark it. |

### Proposals and freezes (canvas v2 phase 5)

An open proposal (`P-n`) is an entry of kind `proposal` in `overlays`, at `z`
1,000,000 plus its number, drawn by Python so the agent's picture and the page
show the same ghost (`canvas_collab.proposal_entry`):

```json
{"id": "P-3", "kind": "proposal", "layer": "overlays", "z": 1000003, "v": 412,
 "bbox": [..], "hit": {"shape": "rect", "box": [..]}, "handles": "none", "connect": false, "edit": null,
 "frame": null, "author": "alpha", "chip": {"bg": "chip.0.bg", "fg": "chip.0.fg", "initials": "AL"}, "locked": false,
 "proposal": {"author": "alpha", "intent": "…", "reason": "human_made", "reasons": ["human_made"], "outdated": false,
              "targets": ["E-4"], "created": [], "deleted": [], "batch": "B-40", "summary": ["…"], "base_note": []},
 "items": [Primitive, ...]}
```

Its `items` are, in order: each proposed element's own items at half its
opacity (`op`; a browser-drawn slot becomes its drawing, or a dashed box); a
dashed `tone.accent.stroke` line from each moved element's old centre to its
new one; a dashed `tone.danger.stroke` box over each element it deletes; an
outline around it all (`tone.warning.stroke` when outdated, else
`tone.accent.stroke`); and a label pill above the outline's top-left in the
author's chip colours, "P-3 · alpha suggests: <intent>" (with " (outdated)"),
from scale 0.35 up. `outdated` is computed when the list is made: a target
changed or went since the proposal was made, or a new element's container went.
A decided proposal is not in the list (a delta removes it).

A freeze (`X-n`, sharing the counter with locks) is an entry of kind `freeze`
in `overlays` at `z` -3: a dashed `tone.info.stroke` rect over its region, or
around the current union of its elements' boxes (it follows them), and the
label "X-6 frozen: <label>" below it. Its `hit` is `{"shape": "frame", "box",
"band": 12}` (the rim only), and it carries `"freeze": {"region" | "ids",
"label", "mode": "propose" | "refuse"}` (`mode` is the team's `frozen` setting).

A delta redraws a proposal whose target changed and an id freeze whose element
changed; a change to a proposal, a freeze or the settings sends the whole list.

## Primitives

Every primitive has `k`, and may have `layer`, `lod: [min_scale, max_scale]`
(drawn only when `min <= scale < max`; either end may be null) and `op`
(opacity 0 to 1). `scale` is screen pixels per world unit.

Paint fields on shapes: `fill`, `stroke`, `sw` (world units), `sw_px` (screen
pixels, wins over `sw`), `dash: [on, off]` (world units) and `elev` (1 to 3, a
token shadow).

| `k` | Fields | Notes |
|---|---|---|
| `rect` | `x y w h r` + paint | `r` is the corner radius. |
| `ellipse` | `cx cy rx ry` + paint | |
| `poly` | `points`, `closed` + paint | A diamond, a pen's smooth outline (filled with the stroke's paint). An open `poly` is never filled. |
| `line` | `points` + `stroke sw dash` | A polyline, never filled. |
| `path` | `d` + paint, `rule` | `d` is absolute `M L C Q Z` only; Python flattens arcs. |
| `arrow` | `d`, `stroke sw dash`, `heads` | A head is `{"at": "end"\|"start", "shape": "chevron"\|"triangle", "points": [[x,y],[x,y],[x,y]]}` or `{"at", "shape": "dot", "cx", "cy", "r"}`, always in the shaft's stroke paint. |
| `text` | `x`, `anchor`, `font` (`sans`/`mono`), `weight`, `size`, `lh`, `fill`, `box`, `lines` | Each line is `{"y", "t", "w", "dir"?}`: `y` its baseline, `w` Python's measured width at `size` (safety factor included), `dir: "rtl"` when its first strong character is right to left. `box` `[x, y, w, h]` is the room the lines were fitted to (it never clips). |
| `image` | `x y w h`, `src: {"asset": name}` or `{"still": name}`, `fallback`? | Contained in its box. `fallback` is what to draw when the asset cannot be loaded. |
| `slot` | `slot` (a kind's renderer key: `chart`, `mermaid`, `viz`, `scene3d`), `x y w h`, `ref: {id, v, doc?}`, `still` (a name or null), `fallback`, and since phases 3 and 4 `views`?, `drawn`?, `gl`? | The browser draws the content live; without the browser, the still or else `fallback`. `ref.doc` names an asset the page fetches from `assets/<name>` (`<32 hex>.json`, a chart's datasets; `.glb`, a model), immutable, so cached by name. `views` maps each of the kind's still views to its still or null (`{"iso": "E-9-v12-iso.png", "front": null, "top": null}`), in order; `still` stays the first one's. `drawn: true` says `fallback` is a faithful drawing (a chart's axes and bars, a scene's projection), not a placeholder card. `gl: true` says the live content needs a WebGL context. |
| `group` | `items`, `t: [a,b,c,d,e,f]`?, `clip: [x,y,w,h]`?, `screen: [ax, ay]`? | With `screen` the children are in screen pixels around the anchor (comment pins, claim and lock labels). A claim's label sits in a world group clipped at the claim beside it on the right, when there is one, so it never runs over that claim's label; a claim that changes redraws every claim. |

A **claim**'s dashed border is drawn `CLAIM_BORDER_OUT` units *outside* the
region, and its `bbox` is the region itself. The border is the only ink a claim
puts on the board, it is two screen pixels wide at every zoom, and the region
snaps to the marks it holds (`canvas_check.claim_snap`), so drawn on the region
its stroke lands along their outlines - which at 41% zoom read as a chart's axis
labels being cut off.

The label hangs *above* that corner, `CLAIM_LABEL_LIFT` screen pixels clear of
the border's own stroke. It used to hang just inside it, which is exactly where
a frame draws its title, and a claim snaps to the marks it holds - so on nearly
every graph render the dashed rectangle and the pill were both drawn through the
frame's own words. The clip that stops the label running over the claim beside
it is therefore a horizontal stop and nothing else: the label is screen-sized,
so its height in world units grows as the board is zoomed out, and the clip's
vertical range (`CLAIM_LABEL_ROOM` either side) is whatever cannot cut it at a
usable zoom.

The border is never drawn **through words** either (layout findings N4). The region
holds whole the words that are in world units — an arrow's label pill and a frame's
title in its band (`canvas_check.claim_snap` with the board's elements, and
`claim_edge` reports a claim that does not, with a `claim` op that does). The
words whose size depends on the zoom cannot be held by any region: a top-level
frame's title stands above it at 12 px and grows in world units as the board is
zoomed out, and another claim's label is screen-sized. For those the border is
drawn per band of zoom (`CLAIM_BAND_TOP` down to `CLAIM_BAND_FLOOR`, each
`CLAIM_BAND_STEP` wide, with `lod`) and, where a line of text sits on it, as a
`path` of straight runs that stops `CLAIM_TEXT_GAP_PX` short of the words and
starts again past them — a fieldset's legend. A band that draws the same as the
one beside it merges into it, and a claim with no words on its border is one
`rect` as before. Only the entries along the border are measured, each once per
side of its own `lod` edges, just inside each band so a `lod` edge is never read
as the band beside it. A proposal's pill counts as words too. A band where words
cover every side draws no border at all, never a zero-length run.

The claim's **label** keeps off words the same way. Per band it hangs from the
first of `CLAIM_LABEL_CORNERS` where it lands on no other line of text — above
the top-left corner (where it always hung), above the top-right, just inside the
top-right and top-left, below the bottom-left and bottom-right — measured against
the board's words, proposals' pills and the labels of the claims placed before
it, and is left out of a band where every corner is on words or the label is
wider than the whole board. Each run is a `group` with `lod`. The document
`bbox` holds every claim's label as a picture at the default size draws it, so a
whole-board picture shows it whole. The classic (v1) page engine draws claims
its own way and has none of this.

**Paint** is `null`, a literal `"#rrggbb"` (the same in both themes), a token
reference (`base.<role>`, `tone.<tone>.<role>`, `chip.<0-7|human>.<bg|fg>`,
and since phases 3 and 4 `chart.<name>` with an optional index (`chart.paper`,
`chart.cat.3`, `chart.seq.6`, `chart.on_cat.3` for a label on that fill) and
`mat.<tone>.<top|left|right|edge>` for a 3D face) or `{"hatch": <paint>}`. A renderer resolves a reference with
`palettes[theme][ref]`; an unknown one draws as `base.ink`. Nothing ever
inverts colours.

`canvas_display.paints(el)` is the only place stored colours become paints: a
stored colour equal to what the element's tone resolves to in the light theme
becomes that role's reference, any other hex stays literal, and an element from
before 0.22 goes through the legacy tables. A label on a literal fill stays
literal too, so it keeps its contrast in both themes.

**Semantic zoom** (phase 2): the bands are tokens (`lod` in `tokens.json`).
From `titles` (0.35 screen pixels per unit) up everything draws; between
`overview` (0.15) and `titles`, bodies (a card's body, table cells, timeline
ticks, captions) are replaced by skeleton bars: one `rect` per line, up to
three, 8 units tall in `base.grid`, with `lod: [0.15, 0.35]`; below `overview`
only silhouettes and top-level container titles remain. Kinds build these with
`canvas_display.LOD_BODY` (`[0.35, null]`), `LOD_LABEL` (`[0.15, null]`),
`skeleton` and `body`. Renderers need nothing new: they already honour `lod`.

**Container titles** (phase 2, the frame rule generalised): a section, a
kanban, a timeline or a graph root emits its title with `title_pair`: in its
60-unit band (20/600) with `lod: [12 / size, null]`, and, for a top-level
container only, above it with `lod: [null, 12 / size]` and `zoom` as below. A
nested container's band title draws down to the overview band.

**Frame titles** (the one zoom rule of phase 1): a frame emits its title twice. In the
band: 16/600 at `(x + 20, y + 8)`, cut to the band's width, `lod: [0.75, null]`.
Above the frame: the whole title, `lod: [null, 0.75]`,
`zoom: {"min_px": 12, "grow": "up", "bottom": y0}` and `base_ratio`. A renderer
draws it at `size_eff = max(size, min_px / scale)`; its block ends at `bottom`,
each line `lh / size * size_eff` tall with its baseline `base_ratio * size_eff`
below the line's top.

**Elements drawn before 0.22** (a 0.21 board, replayed from its log, which is
never rewritten) have no `fit` record. They draw from what was stored, with
today's rules:

- The label is broken into lines at draw time with the bundled metrics, at
  the stored text size, inside the stored `w` and `h`. When it no longer fits
  there, it is laid out in the room its kind would grow to and so runs past
  the stored box; `canvas check` reports `label_overflow` for it until it is
  refitted (`canvas refit`, or the operator's `canvas migrate --apply`, which
  stores a `fit` and grows the box).
- `rough` is not drawn: every stroke is a clean line. `font: hand` has no
  bundled face and draws as `sans` (Inter), though it is still measured as the
  wider hand font until the migration sets `font: normal`.
- A stored colour with no `tone` goes through the legacy tables (above); an
  arrow without `label_at` gets its pill placed by today's rules at draw time.
- A 0.21 `graph` or Mermaid flowchart is the shapes and arrows it was expanded
  to, not a block entry; a 0.21 Vega-Lite chart is a `slot` like any chart.

## Numbers

Every number in the document is rounded to 2 decimals half away from zero (an
integral value is written as an int, never `-0`). `dumps` is
`json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.

`fmt(v)` is how both SVG writers print a number:
`n = floor(abs(v) * 100 + 0.5)`; `"0"` when `n` is 0; else the sign, `n // 100`,
and `"." + two digits` with trailing zeros stripped when `n % 100` is not 0.
`tests/fixtures/display/fmt-vectors.json` holds the vectors both are tested on.

## The canonical SVG

Both writers produce exactly this, with no whitespace between elements:

```
<svg xmlns="http://www.w3.org/2000/svg" width="W" height="H" viewBox="x y w h">
[<defs>filters synapse-elev-N (by N), patterns synapse-hatch-K, clipPaths synapse-clip-K (order of first use)</defs>]
<rect x y width height fill="{base.canvas}"/>
<g data-layer="zones"><g data-id="E-2">items</g>...</g><g data-layer="marks">...</g><g data-layer="labels">...</g><g data-layer="overlays">...</g>
</svg>
```

- **The box** is the region asked for, else `bbox`, as `[x0, y0, x1, y1]` with
  min and max per axis and at least one unit each way. `W` and `H` scale the
  longer side to `max_px` (1024 by default), each rounded half to even, at
  least 1. `u = (x1 - x0) / W`, and `scale = 1 / u` is what `lod`, zoomed text,
  `sw_px` and screen groups use.
- All four layer groups are always written. An entry appears in a layer when
  its `bbox` meets the box (strictly) and at least one of its items in that
  layer draws at this scale.
- **Attributes.** Geometry first: `rect` is `x y width height [rx]` (rx only
  when `r > 0`); `ellipse` `cx cy rx ry`; a closed `poly` is a `<polygon points>`,
  an open one and a `line` a `<polyline points fill="none">`; `path` is
  `d [fill-rule]`. Then paint: `fill stroke [stroke-width [stroke-dasharray]
  stroke-linecap="round" stroke-linejoin="round"] [opacity] [filter]` (the
  stroke group only when there is a stroke; `stroke-width` is `sw_px / scale`,
  else `sw`, else 1). A missing paint is `none`; a hatch is
  `url(#synapse-hatch-K)`.
- **Arrows** are `<g [opacity]>` holding `<path d fill="none" stroke ...>` and
  the heads: a dot `<circle cx cy r fill stroke="none">`, a triangle
  `<polygon points fill stroke="none">`, a chevron
  `<polyline points fill="none" stroke stroke-width linecap linejoin>` (never
  dashed).
- **Text** is `<g font-size font-family font-weight fill text-anchor
  [opacity]>` with one `<text x y xml:space="preserve">` per line (on each
  `<text>`, not the group: a browser's own style for `<text>` resets
  `white-space`, so an inherited `xml:space` would collapse indentation); a line
  with `dir: "rtl"` adds `unicode-bidi="plaintext"` (never `direction`: browsers
  mirror `text-anchor` under it and resvg does not).
- **Images** are `<image x y width height preserveAspectRatio="xMidYMid meet" href [opacity]>`;
  slots are `<g data-slot="kind">` holding the still or the fallback.
- **Groups** are `<g [transform] [clip-path] [opacity]>`; a screen group's
  transform is `translate(ax ay) scale(u)` before any `matrix(...)`.
- **Escaping** drops XML-invalid control characters and escapes `& < > "`.
- **Canonical mode** (the goldens): families `Inter` and `Geist Mono`, urls
  `synapse-asset:<name>` and `synapse-still:<name>`.
- Outside canonical mode the agent's picture also rewrites emoji for resvg,
  wraps each `dir: "rtl"` line in a right-to-left isolate (U+2067 … U+2069:
  resvg ignores `unicode-bidi`, so the agent reads the line in the page's order),
  inlines assets and stills, names `font-family="Inter, sans-serif"` on the
  root, and adds id badges and the labelled grid (never display-list items). A
  badge is a fixed size in pixels while an element is a box in canvas units, so
  it hangs above its element's top edge, above an arrow label's pill and above a
  comment's pin - clearance in the same pixels it is drawn in.
  **No badge is ever drawn on the words the picture draws.**
  `canvas_render.badge_marks` measures every line of text the list will draw at
  this zoom (`text_boxes`, which walks the list the way the page's presence layer
  does, screen-anchored groups and clips included) and gives each badge the first
  corner clear of all of them and of the badges already placed: above-left first
  - where every badge used to go - then the other three corners, then inside the
  top-right one, then rings around the first. A frame starts from its band's
  top-*right* corner, the one corner a frame never draws in, because its own
  title stands in the top-left one. When no corner is clear the badge is left
  out and `look --image` says which marks carry none, because a badge over a
  label costs the reader both.
  The page uses the families `"Synapse Sans"` and `"Synapse Mono"`, each followed
  by the script faces resvg falls back to (`SCRIPT_FALLBACKS` in
  `web/src/v2/render/svgAttrs.js`: Arial Hebrew, Geeza Pro), and real urls.

## Adding to it

A new kind draws itself: its module's `emit(element, env)` returns primitives
built with the helpers in `canvas_display` (`paints`, `label`, `text_prim`,
`stroke_fields`, `card`, and for level of detail `body`, `skeleton`,
`title_pair`). Icons are paths too: `canvas_icons.emit(name, x, y, size, paint)`
is one `group` with a scale, so neither side has icon code. Nothing else changes, on either side, unless the kind
needs browser-drawn content: then it names a `slot`, and the page adds one
renderer under `web/src/v2/render/slots/`. A new primitive kind or field is
added here first, then to both writers, then to the goldens.

**The still rule** (phases 3 and 4, D17), the same in `canvas_svg.slot_node`
and the page's `svgAttrs.slotNode`: a slot draws its `still` when it has one
and either the theme is light or `drawn` is not true (the page posts light
stills only, so a dark picture draws the kind's own drawing); otherwise its
`fallback`. `look --image --view <v>` first rewrites each slot that has `views`:
`still` becomes `views[v]`, and when that is null `fallback` becomes the kind's
`draw_view(el, v)`.
