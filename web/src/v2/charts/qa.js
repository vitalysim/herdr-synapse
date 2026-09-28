// window.__synapseV2.charts(): what every chart on the page drew (canvas-v2-phase3-4.md 2.10, K1).
//   [{id, engine, type, rendered, labelOverlaps, texts, failed}]
// `rendered` is true once the chart's own picture (ECharts, echarts-gl or Vega) is on the slot;
// `failed` is the reason it is showing its fallback instead, else null. labelOverlaps counts
// intersecting text boxes in the static render (labels.js); null when unknown (Vega, GL). `texts`
// lists the labels that render drew (null when unknown), for canvas_qa's frame comparison.
//
// index.js exports this as QA for render/qa.js registerQA (3D-WEB, WW-5). Until a registry exists,
// the first chart to report also attaches it to the hook object the Board installed.
const states = new Map();

/** The QA rows, by id. */
export function chartsQA() {
  return [...states.values()].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)).map((s) => ({ ...s }));
}

function attach() {
  if (typeof window === "undefined") return;
  const hook = window.__synapseV2;
  if (hook && typeof hook === "object" && typeof hook.charts !== "function") hook.charts = chartsQA;
}

/** Records one chart's state (a slot calls it as it loads, draws or fails). */
export function reportChart(id, fields) {
  if (!id) return;
  const prev = states.get(id) || { id, engine: null, type: null, rendered: false, labelOverlaps: null, texts: null, failed: null };
  states.set(id, { ...prev, ...fields, id });
  attach();
}

/** Forgets a chart (its slot unmounted). */
export function forgetChart(id) {
  states.delete(id);
}

/** Test hook: forgets every chart. */
export function resetChartsQA() {
  states.clear();
}
