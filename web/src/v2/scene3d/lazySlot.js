// The scene3d slot component as the page's first chunk sees it: a transparent placeholder at the slot
// box until slot.jsx (the placeholder's behaviour: registering with the shared renderer, entering,
// Save view) has loaded with the first scene on screen. The SVG beneath shows the still or the
// drawing meanwhile, so nothing is lost while it loads (canvas-v2-phase3-4.md 3.7, 9.2).
import { createElement, useEffect, useState } from "react";
import { sceneLabel } from "./aria.js";
import { noteMounted, noteUnmounted } from "./qa.js";

let Loaded = null;
let loading = null;

/** Loads slot.jsx once; resolves to its component. */
export function loadScene3DSlot() {
  if (!loading) {
    loading = import("./slot.jsx").then((m) => {
      Loaded = m.default;
      return Loaded;
    });
    loading.catch(() => {
      loading = null;
    });
  }
  return loading;
}

export default function Scene3DSlot(props) {
  const [Impl, setImpl] = useState(() => Loaded);
  const { prim, entryId, version, team } = props;
  const key = `${team}:${entryId}`;
  useEffect(() => {
    if (Impl) return undefined;
    let live = true;
    noteMounted(key, entryId, version);
    loadScene3DSlot()
      .then((C) => {
        if (live) setImpl(() => C);
      })
      .catch((err) => console.warn(`[synapse v2] ${entryId}: the 3D slot did not load (${err && err.message}); the scene shows its still`));
    return () => {
      live = false;
      noteUnmounted(key);
    };
  }, [Impl, key]); // eslint-disable-line react-hooks/exhaustive-deps
  if (Impl) return createElement(Impl, props);
  return createElement("div", {
    className: "sv2-scene3d",
    "data-slot": "scene3d",
    "data-id": entryId,
    "data-drawn": "0",
    role: "img",
    "aria-label": sceneLabel(props.element),
    style: { left: prim.x, top: prim.y, width: Number(prim.w), height: Number(prim.h), pointerEvents: "none" },
  });
}
