// window.__synapseV2.collab() (canvas-v2-phase5.md 12.7, I-8): counts read from what the page
// actually drew, for tools/canvas_qa.py --page --engine v2 to compare with the display list.
//   halos           halo rects in the presence layer
//   operator_marks  the operator's other pages drawn (viewport rects and cursor dots)
//   proposals       P- entries drawn
//   outdated        drawn P- entries the list calls outdated
//   frozen          drawn entries the list marks frozen (elements a freeze covers)
//   freezes         X- freeze overlays drawn
//   review_open     whether a review card is open
//   queue           the review queue's length (0 when it is closed)
import { registerQA } from "../render/index.js";

function drawnIds(root) {
  if (!root || typeof root.querySelectorAll !== "function") return new Set();
  return new Set([...root.querySelectorAll("g[data-layer] > g[data-id]")].map((g) => g.getAttribute("data-id")));
}

export function collabCounts({ root, halos, dl, reviewOpen, queue }) {
  const drawn = drawnIds(root);
  const byId = new Map(((dl && dl.entries) || []).map((e) => [e.id, e]));
  let proposals = 0;
  let outdated = 0;
  let frozen = 0;
  let freezes = 0;
  for (const id of drawn) {
    const e = byId.get(id);
    if (!e) continue;
    if (/^P-/.test(id)) {
      proposals += 1;
      if (e.proposal && e.proposal.outdated) outdated += 1;
    } else if (e.kind === "freeze") freezes += 1;
    else if (e.frozen) frozen += 1;
  }
  const layer = halos || null;
  const count = (sel) => (layer && typeof layer.querySelectorAll === "function" ? layer.querySelectorAll(sel).length : 0);
  return {
    halos: count(".cv2-halo"),
    operator_marks: count(".cv2-operator-view") + count(".cv2-operator-cursor"),
    proposals,
    outdated,
    frozen,
    freezes,
    review_open: Boolean(reviewOpen),
    queue: Number(queue) || 0,
  };
}

// Registers the hook; `read()` returns the live inputs. Returns unregister().
export function installCollabQA(read) {
  return registerQA("collab", () => collabCounts(read()));
}
