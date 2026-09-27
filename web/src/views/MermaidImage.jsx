// A Mermaid diagram shown as an <img> of the SVG Mermaid produced in strict mode.
import React, { useEffect, useState } from "react";
import { naturalSVG, renderMermaid, svgToDataURL } from "../canvas/renderers.js";

export default function MermaidImage({ source, alt }) {
  const [url, setUrl] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let live = true;
    setError(null);
    if (!source) {
      setUrl(null);
      return undefined;
    }
    renderMermaid(source)
      .then((svg) => live && setUrl(svgToDataURL(naturalSVG(svg))))
      .catch((err) => live && setError(String(err && err.message ? err.message : err).slice(0, 200)));
    return () => {
      live = false;
    };
  }, [source]);
  if (error) return <div className="render-error">could not draw: {error}</div>;
  if (!url) return <div className="muted">drawing…</div>;
  return <img className="diagram" src={url} alt={alt} />;
}
