// What a screen reader hears for a scene (canvas-v2-phase3-4.md 5): its title, its object count and
// the labels it shows, from the element the page already holds (the server's gist is not stored on
// it). Small, so the first chunk's placeholder has it too.
const MAX_LABELS = 8;

export function sceneLabel(element) {
  const title = typeof element?.text === "string" && element.text ? element.text : typeof element?.title === "string" ? element.title : "";
  const objects = Array.isArray(element?.objects) ? element.objects : [];
  const labels = objects.map((o) => (o && typeof o.label === "string" ? o.label : o && o.shape === "text3d" && typeof o.text === "string" ? o.text : "")).filter(Boolean);
  const more = labels.length > MAX_LABELS ? `, +${labels.length - MAX_LABELS} more` : "";
  const named = labels.length ? `: ${labels.slice(0, MAX_LABELS).join(", ")}${more}` : "";
  return `3D scene${title ? ` "${title}"` : ""}, ${objects.length} object${objects.length === 1 ? "" : "s"}${named}`;
}
