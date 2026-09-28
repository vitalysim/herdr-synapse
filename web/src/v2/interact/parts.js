// The text editor's target for an entry with inline parts (canvas-v2-phase2.md 6.2 `parts`,
// 6.3 W-a and W-e). Pure, so it is testable without a board.

// What an editor opens on for an entry (canvas-v2-phase2.md 6.3 W-a, W-e): the part asked for when
// it has an editor; else the entry's own text, then its first editable part; for an entry a tool
// just made (`created`), its first editable part first (a new card's title), then its own text.
export function editTargetOf(entry, part = null, { created = false } = {}) {
  if (!entry) return null;
  const parts = Array.isArray(entry.parts) ? entry.parts.filter((p) => p && typeof p.part === "string" && p.edit && p.edit.box) : [];
  if (part) {
    const found = parts.find((p) => p.part === part);
    return found ? { part: found.part, spec: found.edit } : null;
  }
  // An entry's own edit may name one of its parts (a card's is its title): then it edits that part.
  const named = entry.edit && typeof entry.edit.part === "string" ? parts.find((p) => p.part === entry.edit.part) : null;
  const own = named ? { part: named.part, spec: named.edit } : entry.edit && entry.edit.box ? { part: null, spec: entry.edit } : null;
  const first = parts.length ? { part: parts[0].part, spec: parts[0].edit } : null;
  return created ? first || own : own || first;
}

// While a part is edited, the entry is drawn without the text inside the part's box, so the typing
// replaces only that cell or line (text primitives carry no part, so their anchor decides).
export function withoutPartText(entry, box) {
  const [x, y, w, h] = box;
  const inside = (p) => {
    const px = Number(p.x);
    const py = Array.isArray(p.lines) && p.lines.length ? Number(p.lines[0].y) : Number(p.y);
    return px >= x - 1 && px <= x + w + 1 && py >= y - 1 && py <= y + h + 1;
  };
  return (entry.items || []).filter((p) => p && !(p.k === "text" && inside(p)));
}
