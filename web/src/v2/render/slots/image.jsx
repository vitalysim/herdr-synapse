// A slot the browser draws into an image (chart, mermaid): the shared part. While the live picture
// loads, or when it fails, the slot shows what a written picture shows (its still, else its
// fallback card). A picture that drew is rasterised once per (id, v) and handed to onStill, so
// agents' PNGs get the same picture next time.
//
// Chart and Mermaid renderers (canvas/renderers.js) colour with the light tokens, so the live
// picture sits on a light paper card in both themes; nothing is ever colour-inverted.
import React, { useEffect, useState } from "react";
import { rasterize } from "../../../canvas/renderers.js";

export default function LiveImageSlot({ prim, entryId, version, element, paper, edge, fallback, onStill, load }) {
  const [url, setUrl] = useState(null);
  const x = Number(prim.x);
  const y = Number(prim.y);
  const w = Number(prim.w);
  const h = Number(prim.h);

  useEffect(() => {
    let live = true;
    setUrl(null);
    if (!element || !load) return undefined;
    load(element, w, h)
      .then((result) => {
        if (!live || !result || result.failed || !result.dataURL) return;
        setUrl(result.dataURL);
        if (onStill) {
          rasterize(result.dataURL, w, h)
            .then((blob) => onStill(entryId, version, blob))
            .catch(() => undefined);
        }
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
    // The picture depends on the element's version, not on the object's identity.
  }, [entryId, version, element ? element.updated_seq : null, w, h, load]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!url) return fallback;
  return (
    <g data-slot={prim.slot}>
      <rect x={x} y={y} width={w} height={h} rx={8} fill={paper} stroke={edge} strokeWidth={1} />
      <image x={x} y={y} width={w} height={h} preserveAspectRatio="xMidYMid meet" href={url} />
    </g>
  );
}
