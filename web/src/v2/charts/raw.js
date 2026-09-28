// engine "echarts-raw" (canvas-v2-phase3-4.md 2.8): the option lives in the chart's doc asset
// ({"v": 1, "option": {...}}), already sanitised by Python on write. The page sanitises it again
// with the same allow table before anything draws it.
import { sanitize } from "./sanitize.js";

/** The drawable raw option from its doc, or throws with the refusal. */
export function rawOption(doc) {
  const { option, refused } = sanitize(doc && typeof doc === "object" ? doc.option : null);
  if (refused) throw new Error(`raw option refused: ${refused}`);
  return option;
}
