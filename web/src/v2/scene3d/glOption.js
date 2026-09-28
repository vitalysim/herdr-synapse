// Pure touches on a resolved GL chart option before echarts-gl reads it (glCharts.js), kept apart
// so they are tested without WebGL.

/**
 * A surface series' points as echarts-gl reads them: rows of increasing x, one row per y, with its
 * dataShape [rows, columns]. echarts-gl guesses the grid from where x goes back down, so points in
 * any other order (x outer, say) draw as torn strips. A series that sets dataShape, or whose points
 * do not fill a grid, is left as it is.
 */
export function surfaceGrid(series) {
  if (!series || series.type !== "surface" || series.dataShape || !Array.isArray(series.data)) return series;
  const pts = series.data.map((d) => (Array.isArray(d) ? d : d && Array.isArray(d.value) ? d.value : null));
  if (!pts.length || pts.some((p) => !p || p.length < 3 || !Number.isFinite(Number(p[0])) || !Number.isFinite(Number(p[1])))) return series;
  const xs = [...new Set(pts.map((p) => Number(p[0])))];
  const ys = [...new Set(pts.map((p) => Number(p[1])))];
  if (xs.length * ys.length !== pts.length) return series;
  const order = series.data.map((d, i) => [Number(pts[i][1]), Number(pts[i][0]), d]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  return { ...series, data: order.map((r) => r[2]), dataShape: [ys.length, xs.length] };
}

/**
 * The value axis's lowest label left out (QA phase34 L7): echarts-gl puts it at the floor's near corner,
 * where the first width-axis label is drawn too ("0" on "8", "−2" on "10"). The steps above it still
 * read the axis. Only a zAxis3D with a numeric min and a function formatter (or none) is changed.
 */
export function cornerLabels(option) {
  const z = option && option.zAxis3D;
  if (!z || Array.isArray(z) || typeof z !== "object") return option;
  const label = z.axisLabel || {};
  const inner = label.formatter;
  const min = Number(z.min);
  if (label.show === false || (inner != null && typeof inner !== "function") || z.min == null || !Number.isFinite(min)) return option;
  const tol = 1e-9 * Math.max(1, Math.abs(min));
  const formatter = (value, ...rest) => (Math.abs(Number(value) - min) <= tol ? "" : inner ? inner(value, ...rest) : String(value));
  return { ...option, zAxis3D: { ...z, axisLabel: { ...label, formatter } } };
}

/**
 * A category axis's thinned labels (QA phase34 low: crowded labels met at the floor's near corner):
 * the server thins them as its drawing does (every k-th shown, `axisLabel.interval` k − 1), but
 * ECharts 6 always keeps an axis's last category label, and echarts-gl draws every label it is
 * given, so the last one landed on its neighbour. Here the interval becomes a formatter that
 * leaves the off-interval labels blank. A category axis with a formatter of its own is left alone.
 */
export function categoryLabels(option) {
  let out = option;
  for (const key of ["xAxis3D", "yAxis3D"]) {
    const axis = out && out[key];
    if (!axis || Array.isArray(axis) || typeof axis !== "object" || axis.type !== "category") continue;
    const label = axis.axisLabel || {};
    const every = Number(label.interval) + 1;
    if (label.show === false || label.formatter != null || !Number.isInteger(every) || every < 2) continue;
    const formatter = (value, index) => (Number(index) % every === 0 ? String(value) : "");
    out = { ...out, [key]: { ...axis, axisLabel: { ...label, interval: 0, formatter } } };
  }
  return out;
}
