// An author's chip in the page chrome: their initials on their chip colour (the design's author
// chips, one per roster index; the human's is ink). Authorship is shown here and on hover, never
// by colouring what they drew.
import React from "react";
import { authorChip } from "../theme/tokens.js";

export default function AuthorChip({ scene, name, theme = "light" }) {
  const chip = authorChip(scene, name, theme);
  return (
    <span className="author-chip" style={{ background: chip.bg, color: chip.fg }} aria-hidden="true">
      {chip.initials}
    </span>
  );
}
