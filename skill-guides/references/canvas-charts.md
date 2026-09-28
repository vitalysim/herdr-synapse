# Reference: canvas charts

Served with `canvas` while the canvas is on. A `chart` is one element: you name
a type and the columns, the canvas checks them against the data, draws it (the
page with ECharts; your picture with the same axes), and reads it back in
words. Chart when the point is a comparison, a trend, a share or a flow; a
`table` is better for a few exact numbers people will look up.

## The op

`chart {id, title, type, data | rows, <channels>, aggregate, filter, sort, top,
stack, horizontal, labels, legend, format, units, types, highlight,
annotations, caption, w, h}`

- `data: "rev.csv"`: a `.csv`, `.tsv` or `.json` under `artifacts/` (5 MB,
  100,000 rows). `rows: [{...}]`: inline objects (500 rows, 32 KB), or
  `columns` plus rows as arrays. The data is read when the op runs; a later
  file edit changes nothing until `patch {relayout: "full"}` or the same op again.
- Columns are typed from their values: numbers (`1,234`, `12%`, `$3.2M`),
  dates (`2026`, `2026-03`, `2026-03-04`, `2026-Q2`, `2026-W05`), else text.
  `types: {"day": "ordinal"}` keeps a column's own order (a line needs it).
- `aggregate`: `sum` (the default when rows repeat), `mean`, `median`, `min`,
  `max`, `count` (needs no measure), `none`.
- `filter: [{"field": "region", "in": ["EMEA"]}, {"field": "revenue", ">": 0}]`
  (`in`, `not_in`, `=`, `!=`, `>`, `>=`, `<`, `<=`, `between [a, b]`, `null`).
- `top: 8` keeps the biggest categories and series, the rest become "Other"
  (`other: false` drops them). `sort`: `x`, `-x`, `y`, `-y`, `none`.
- `format: {"y": "compact"}` (`compact`, `percent`, `integer`, `0.0`, `date`,
  `month`, `year`); `units: {"y": "$"}` (a prefix for `$ £ € ¥`, else a suffix).
- `highlight: ["EMEA"]` paints those series or categories, the rest dimmed.
  `annotations: [{"x": "2026-08", "text": "launch"}, {"y": 1000000}]`.
- `w`, `h` are minimums: made or moved too small, a chart grows (`chart_grew`).

| type | channels (required first) | options |
|---|---|---|
| `bar` (`column`) | x, y; color | stack true/percent, horizontal |
| `line` · `area` | x (time, number or ordered), y; color | smooth, points; area: stack |
| `scatter` (`bubble`) | x, y numbers; color, size, label | trend: linear |
| `pie` · `donut` | category, value | donut: inner 0.3-0.8 |
| `heatmap` | x, y, value | palette: sequential/diverging |
| `histogram` | x; color | bins |
| `box` | y; x (the groups) | whiskers 1.5iqr/minmax, horizontal |
| `funnel` | category, value | horizontal |
| `treemap` | path (1-3 columns), value | depth |
| `sankey` | source, target, value | orient |
| `bar3d` · `scatter3d` · `surface` | x, y, z (see `canvas-3d`) | |

`herdr-synapse canvas catalog charts [--type sankey]` lists every type with
its channels, options, caps and an example op.

## Reading it back

The reply and `look` print the chart's gist: the channels and the source, then
the extremes with their categories, trends (`EMEA ↑ +38%`), shares, and what
the fit did (`frame: x labels rotated 45°`). `look --full` adds the columns,
the model's first rows and the source's sha; `look --block <id>` prints the
spec; `look --image` draws the chart itself. A refusal names the field and the
repair: `field "revenu" is not a column of rev.csv; columns: month (temporal,
12 values), region (nominal, 3), revenue (quantitative). Did you mean
"revenue"?`. A wrong type says what the channel takes and suggests `count`.

## Examples

A grouped bar chart over `artifacts/rev.csv`:

```json
{"op": "chart", "id": "rev", "intent": "compare regions by month", "title": "Revenue by region, 2026", "type": "bar",
 "data": "rev.csv", "x": "month", "y": "revenue", "color": "region", "format": {"y": "compact"}, "units": {"y": "$"}, "at": "c0r140"}
```

A line over inline rows, with an annotation:

```json
{"op": "chart", "id": "p95", "intent": "latency after the cache change", "title": "p95 latency (ms)", "type": "line",
 "rows": [{"day": "2026-09-01", "p95": 412}, {"day": "2026-09-02", "p95": 405}, {"day": "2026-09-03", "p95": 398},
          {"day": "2026-09-04", "p95": 251}, {"day": "2026-09-05", "p95": 240}, {"day": "2026-09-06", "p95": 236}],
 "x": "day", "y": "p95", "units": {"y": "ms"}, "annotations": [{"x": "2026-09-04", "text": "cache on"}], "below": "rev"}
```

A donut with the tail folded into Other:

```json
{"op": "chart", "id": "mix", "intent": "traffic share by channel", "title": "Visits by channel", "type": "donut",
 "data": "traffic.json", "category": "channel", "value": "visits", "top": 5, "right_of": "p95"}
```

Change it, then re-read the file (`if_version` is the chart's `v` from `look`):

```json
{"ops": [
 {"op": "patch", "id": "rev", "if_version": 12, "intent": "show the trend, not the totals", "set": {"type": "line", "color": "region"}},
 {"op": "patch", "id": "rev", "intent": "the finance export was refreshed", "relayout": "full"}]}
```

`patch set` changes any field; `set: {"color": null}` removes one. A move or
resize re-fits the axes with no file read.

## Escape hatches

- `spec`: a raw Vega-Lite spec (facets, layers), with `data` a file under
  `artifacts/` or `spec.data.values`; no URL anywhere. It reads back as its
  mark and fields; a plain single view is also drawn in your picture.
- `echarts`: a raw ECharts option for what no type covers (radar, gauge,
  parallel, candlestick, calendar ...). Titles, toolboxes, links, images,
  functions and CSS are stripped and listed (`chart_escape`); anything that
  loads (`https:`, `data:`, `javascript:`) is refused. It reads back as its
  series; your picture shows that summary, not a drawing.

```json
{"op": "chart", "id": "skills", "intent": "who covers what", "title": "Skill coverage", "below": "mix",
 "echarts": {"radar": {"indicator": [{"name": "Rust", "max": 5}, {"name": "Python", "max": 5}, {"name": "Web", "max": 5}, {"name": "Infra", "max": 5}]},
             "series": [{"type": "radar", "data": [{"name": "alpha", "value": [4, 3, 5, 2]}, {"name": "beta", "value": [2, 5, 3, 4]}]}]}}
```

## Checks

`chart_labels` (labels unreadable: the fix resizes, keeps the top 20, or lays
bars on their side), `chart_crowded` (more than 10 series: `top: 8`),
`chart_pie_slices` (more than 7 slices), `chart_contrast` (a paint under 3:1),
and the notes `chart_capped` (data cut to draw) and `chart_escape`.
