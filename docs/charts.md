# Charts

Canvas v2 phase 3. The binding plan is `.local/prd/canvas-v2-phase3-4.md`
section 2; this page is the contract as built. Agents learn the op from the
`canvas-charts` skill reference and `herdr-synapse canvas catalog charts`.

## What happens to a chart op

A `chart` op is a block (`Kind.block`, inline parts): its spec is its
settings, so `patch set`, an upsert by `id`, `if_version` and `look --block`
work as for every block. Exactly one of three engines:

| Engine | Given | Who draws it |
| --- | --- | --- |
| `echarts` | `type` (the flat spec) | Python compiles it; the page draws the ECharts option; the agent's picture is Python's own drawing |
| `vega-lite` | `spec` (raw Vega-Lite) | the page through `vega-interpreter`; a plain single view is also drawn by Python |
| `echarts-raw` | `echarts` (a raw option) | the page, after sanitising it; the agent's picture is the gist card |

For a flat chart, Python (`herdr_team/canvas_charts/`) does everything that
needs judgement, once, when the op runs:

1. **Load** (`Block.load`, before the canvas lock): read the file under
   `artifacts/` through `FetchIO` (`.csv`, `.tsv`, `.json`, 5 MB, 100,000
   rows) or take the inline rows (500, 32 KB), type every column
   (`quantitative`, `temporal`, `nominal`; `ordinal` only when declared; a
   line or area refuses a nominal x, which would be ranked by value and read
   back as a false trend, and names the `types` repair),
   check every channel against its column (a missing field names the columns
   and the nearest one; a wrong type says what the channel takes), filter,
   aggregate, order and cap (the tail into "Other", long series thinned by
   LTTB, many points sampled with an even stride), and compute the model, the
   doc and exact statistics over the whole filtered table.
2. **Frame** (`_frame.py`): the plot rectangle, the legend, the value ticks
   (Heckbert's nice numbers), and the category labels' rung (horizontal, every
   2nd, 45°, 90°, every 3rd, truncated), measured with the bundled fonts. A
   frame that cannot be made readable says so (`legible: false`, the check
   `chart_labels`). `w`/`h` are minimums: a chart grows to its type's legible
   minimum (`chart_grew`).
3. **Option** (`_option.py` and the type): the complete ECharts option,
   theme-neutral, following the frame verbatim (grid in px, explicit
   min/max/interval, label rotation and interval).
4. **Drawing** (`_draw.py` and the type): display-list primitives in the
   slot's `fallback` (`drawn: true`), from the same frame.
5. **Gist** (`_gist.py` and the type): the chart in words.

The data is a snapshot: a later edit of the file changes nothing drawn until
`patch {relayout: "full"}` or the same op again re-reads it. A patch that only
changes how the chart looks (`title`, `caption`, `labels`, `legend`,
`highlight`, `annotations`, `horizontal`, `smooth`, `points`, `palette`,
`orient`, `inner`, `shading`, `wireframe`) and a resize re-frame the stored
model with no file read.

## The stored element

```json
{"id": "E-51", "type": "chart", "kv": 2, "alias": "rev", "text": "Revenue by region, 2026",
 "settings": {"type": "bar", "data": "rev.csv", "x": "month", "y": "revenue", "color": "region", "format": {"y": "compact"}, "units": {"y": "$"}},
 "engine": "echarts", "chart": {"type": "bar", "gl": false},
 "source": {"data": "rev.csv", "sha256": "…", "rows": 36, "kept": 36, "columns": [["month", "temporal"], ["region", "nominal"], ["revenue", "quantitative"]]},
 "model": {}, "stats": {}, "resolved": {}, "chart_frame": {}, "option": {},
 "doc_asset": "3f…a1.json", "spec_asset": "9c…07.vl.json", "data_key": "…",
 "gist": ["x=month (12, 2026-01…2026-12) · y=revenue · color=region (3) · rev.csv 36 rows", "…"]}
```

- `model` (16 KB or less) is what Python draws, checks and re-frames;
  `stats` the exact numbers the gist is written from; `resolved` the spec with
  the defaults the data decided (the aggregate, units found in the data).
- `chart_frame` is the fit (`canvas_charts._frame.Frame.to_json`). It is not
  called `frame`: that field is an element's enclosing frame.
- `option` (16 KB or less) is the ECharts option; `doc_asset` the page's
  datasets; `spec_asset` a small Vega-Lite spec over the model for the frozen
  v1 page (a text card of the gist for a type Vega-Lite has no mark for).
- A flat chart keeps its file only in `settings` and `source`; a Vega-Lite
  chart keeps `data` at the top level too, as v1 charts did.
- A v1 chart element (a `spec_asset` and a `data`) is read as a `vega-lite`
  chart (`Kind.upgrade`, applied by `canvas_kinds.upgraded` when the state is
  folded); the log is never rewritten.

## The option contract (Python to page)

The option is complete except for markers the page resolves
(`web/src/v2/charts/resolve.js`):

- any string matching `^(chart|tone|base|mat)\.[a-z0-9_.]+$` is a token
  reference, resolved from the display list's palette of the theme drawn;
- `{"$doc": "<name>"}` is the doc asset's `refs[name]` (`{"$doc": "datasets"}`
  is its `datasets`);
- `"$sans"` and `"$mono"` as a `fontFamily` are the page's fonts;
- `{"$fmt": "<format>|<unit>"}` as a formatter is one of the fixed functions in
  `charts/format.js`, which mirrors `_format.fmt_number` exactly
  (`tests/fixtures/charts/format-vectors.json`).

Fixed keys: `animation: false`, `tooltip.renderMode: "richText"` with
`confine: true`, `aria.label.description` (the gist), `grid` in px with
`containLabel: false`, explicit axis bounds; never `toolbox`, `title`,
`graphic` or a function. The left axis's title (the measure and its unit,
`p95 (ms)`; never a doubled unit) is that axis's `name`, left-aligned at the
axis line and `_frame.NAME_GAP` above the plot, where the agent's drawing puts
it too. A bubble size channel is one hidden `visualMap` per series that keeps
the series' colour (`_option.size_maps`): a map setting only `symbolSize`
would take the page theme's colour ramp. Colours that depend only on how the chart looks live
in the option, never in the doc, so a visual patch keeps the doc asset.

The doc asset (`<32 hex>.json`, 2 MB or less) is
`{"v": 1, "chart", "source", "datasets": [{"id", "dimensions", "source"}], "refs": {...}}`,
at page resolution (2,000 points a series for a line, 5,000 for a scatter).
`tests/fixtures/charts/options/<type>.json` holds one `{"element", "doc"}`
per type (`python3 -m herdr_team.canvas_charts._fixtures --write`).

## The raw ECharts escape hatch

`_sanitize.sanitize(option)` applies `assets/canvas/echarts-allow.json`
(generated from the Python tables by `python3 -m
herdr_team.canvas_charts._sanitize --write`): allowed top-level keys and
series types only; `toolbox`, `title`, `graphic`, `extraCssText`, `link`,
dataset `transform`, a non-string `formatter` and image paints are stripped
(listed as `stripped`, the note `chart_escape`); any string that loads
(`http:`, `https:`, `//`, `data:`, `javascript:`, `blob:`, `file:`,
`image://`, `url(`) refuses the op; 64 KB, depth 32, 20,000 numbers, strings of
1,000 characters. The clean option is stored as a doc asset `{"v": 1,
"option"}`; the page runs the same table again before `setOption`.
`tests/fixtures/charts/sanitize-vectors.json` is the hostile corpus both
sanitisers are held to.

## Checks

| Code | Fires when | Fix |
| --- | --- | --- |
| `chart_labels` | the frame could not make the labels readable | `move` to a size where they are, `patch set {top: 20}` over 30 categories, or `set {horizontal: true}` for a vertical bar |
| `chart_crowded` | more than 10 series, or a legend of more than 8 rows | `patch set {top: 8}` |
| `chart_pie_slices` | a pie or donut of more than 7 slices, or a labelled slice under 2 % | `set {top: 6}`, or `set {type: "bar"}` past 12 |
| `chart_contrast` | a highlight or a raw option's colour under 3:1 on the paper | `set {highlight: null}` |
| `chart_capped` | the data was capped or sampled to draw (a note) | none |
| `chart_escape` | a raw option lost keys (a note) | none |

## Adding a chart type

One module in `herdr_team/canvas_charts/` exporting `CHARTS` (and an `ORDER`):
a `ChartType` with its channels, options (and `option_rules` for a new one),
hooks (`normalize`, `model`, `stats`, `doc`, `frame`, `option`, `draw`,
`gist`, `vegalite`, `checks`, `min_box`), the ECharts modules it needs and one
example op. The chart op takes its fields, the catalog, the MCP op table and
`docs/reference.md` list it, and conformance runs over it with no other file
changed (`tests/test_charts_registry.py` proves it with a `lollipop`). A type
that needs an ECharts module the page does not bundle fails
`tests/test_web_contract.py` against `web/dist/charts.json`, not a user. Then
regenerate the fixtures (`_fixtures --write`) and `reference_docs --write`.

Tokens: the chart paints are `herdr_team/canvas_tokens.json` `chart`
(`paper`, `ink`, `muted`, `axis`, `gridline`, `cat` 10, `seq` 9, `div` 9,
`highlight`, `dim`) per theme, plus `chart.on_<ramp>.<i>` (derived: the paper
or ink, whichever reads better on that fill) for labels on marks. Every
categorical paint is at least 3:1 on the paper, and every label on a fill at
least 4.5:1, in both themes (`tests/test_canvas_theme.py`).
