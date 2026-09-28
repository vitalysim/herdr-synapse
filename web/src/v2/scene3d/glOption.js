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
