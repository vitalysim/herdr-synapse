// The text editor overlay (canvas-v2-phase1.md 4.4): a <textarea> in Surface's screen-space HTML
// layer, placed over the entry's `edit` box in the entry's own font at the current zoom, with
// no padding, border or background, so the text sits where the drawn text was.
//
// Enter adds a line (it commits for `wrap: "line"`), Cmd/Ctrl-Enter and blur commit, Esc
// cancels, and nothing commits during IME composition. `box` wraps grow downward; `auto`
// (a free text) grows sideways up to AUTO_MAX_W world units.
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { PAGE_FAMILIES } from "../render/index.js";

export const AUTO_MAX_W = 600;
// The drawn text's own families, so the typing looks like the label it replaces.
const FAMILY = PAGE_FAMILIES;

// Where the editor goes on screen: the camera maps world to screen as (world - [x, y]) * scale.
export function editorRect(box, camera) {
  const s = camera.scale;
  return { left: (box[0] - camera.x) * s, top: (box[1] - camera.y) * s, width: box[2] * s, height: box[3] * s };
}

export default function TextEditor({ spec, part = null, camera, color, onCommit, onCancel }) {
  const ref = useRef(null);
  const done = useRef(false);
  const composing = useRef(false);
  const [value, setValue] = useState(spec.value ?? "");
  const [content, setContent] = useState({ w: 0, h: 0 });
  const s = camera.scale;
  const size = spec.size || 20;
  const lh = spec.lh || size * 1.25;
  const wrap = spec.wrap || "box";

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    node.focus({ preventScroll: true });
    node.select();
  }, []);

  // Measure what the text needs, in screen px, after every change.
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const prev = node.style.height;
    node.style.height = "0px";
    const h = node.scrollHeight;
    node.style.height = prev;
    setContent((c) => (c.h === h && c.w === node.scrollWidth ? c : { w: node.scrollWidth, h }));
  }, [value, s]);

  const finish = (commit) => {
    if (done.current) return;
    done.current = true;
    if (commit) onCommit(ref.current ? ref.current.value : value);
    else onCancel();
  };

  const rect = editorRect(spec.box, camera);
  const minH = lh * s;
  let { left, top, width } = rect;
  let height = Math.max(minH, content.h);
  if (wrap === "box") {
    // Centred in the box while the text fits, as it is drawn; then it grows downward.
    top += Math.max(0, (rect.height - height) / 2);
  } else if (wrap === "auto") {
    width = Math.min(AUTO_MAX_W * s, Math.max(rect.width, content.w + size * s));
    if (spec.align === "center") left = rect.left + (rect.width - width) / 2;
  } else if (wrap === "line") {
    height = minH;
    top += Math.max(0, (rect.height - height) / 2);
  }

  const style = {
    position: "absolute",
    left,
    top,
    width: Math.max(width, size * s),
    height,
    font: `${spec.weight || 500} ${size * s}px ${FAMILY[spec.font] || FAMILY.sans}`,
    lineHeight: `${lh * s}px`,
    textAlign: spec.align === "center" ? "center" : "start",
    color: color || "inherit",
    whiteSpace: wrap === "auto" || wrap === "line" ? "pre" : "pre-wrap",
    overflowWrap: "break-word",
  };

  return (
    <textarea
      ref={ref}
      className="v2-text-editor"
      data-part={part || undefined}
      style={style}
      value={value}
      spellCheck={false}
      aria-label={spec.label || "Edit text"}
      onChange={(event) => setValue(event.target.value)}
      onCompositionStart={() => {
        composing.current = true;
      }}
      onCompositionEnd={() => {
        composing.current = false;
      }}
      onPointerDown={(event) => event.stopPropagation()}
      onKeyDown={(event) => {
        event.stopPropagation();
        if (composing.current || event.nativeEvent.isComposing) return;
        if (event.key === "Escape") {
          event.preventDefault();
          finish(false);
        } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey || wrap === "line")) {
          event.preventDefault();
          finish(true);
        }
      }}
      onBlur={() => finish(true)}
    />
  );
}
