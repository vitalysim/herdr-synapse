// engine "vega-lite" (a v1 element, or the raw Vega-Lite escape hatch, canvas-v2-phase3-4.md 2.8):
// drawn by canvas/renderers.js renderChart through vega-interpreter (no eval, no URL loads), as
// Phase 1 drew every chart. The spec is an asset; its data is inline or a file under artifacts/.
import { fitSVG, renderChart } from "../../canvas/renderers.js";

/** The fields renderChart reads, from the element or (upgraded elements, 2.9) its settings. */
export function vegaElement(element) {
  const settings = element && typeof element.settings === "object" && element.settings ? element.settings : {};
  const specAsset = element?.spec_asset ?? settings.spec_asset ?? null;
  const data = element?.data ?? settings.data ?? null;
  return { ...element, spec_asset: specAsset, data: data || undefined };
}

/** The chart as an SVG document fitted to w×h. */
export async function renderVega(team, element, w, h) {
  const el = vegaElement(element);
  if (!el.spec_asset) throw new Error("a Vega-Lite chart without a spec asset");
  return fitSVG(await renderChart(team, el, w, h), w, h);
}
