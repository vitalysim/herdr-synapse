// The hand tool: Surface pans on a left drag while panMode is set (the hand tool, or space
// held), so the board starts no gesture of its own.
export function panModeOf(tool, spaceHeld) {
  return tool === "hand" || Boolean(spaceHeld);
}
