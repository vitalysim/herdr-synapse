// The Diagrams tab: every diagram, chart and live visual on the team's canvas in one gallery,
// with its source. Pictures render locally exactly as on the canvas.
import React, { useEffect, useState, useSyncExternalStore } from "react";
import { getJSON, teamPath } from "../api.js";
import { renderFile, fileIdFor } from "../canvas/renderers.js";
import VizFrame from "../viz/VizFrame.jsx";

const KINDS = ["mermaid", "chart", "viz", "svg"];
const who = (name) => (name === "human" ? "the operator" : name);

function Picture({ team, element, vizOn }) {
  const [url, setUrl] = useState(null);
  useEffect(() => {
    let live = true;
    const fileId = fileIdFor(element, vizOn);
    if (!fileId) return undefined;
    renderFile(team, element, fileId).then((file) => live && setUrl(file.dataURL));
    return () => {
      live = false;
    };
  }, [team, element, vizOn]);
  if (element.type === "viz" && vizOn) {
    return (
      <div className="gallery-viz" style={{ aspectRatio: `${element.w || 4} / ${element.h || 3}` }}>
        <VizFrame team={team} element={element} />
      </div>
    );
  }
  return url ? <img className="diagram" src={url} alt={element.text || element.id} /> : <div className="muted">drawing…</div>;
}

function Source({ team, element }) {
  const [text, setText] = useState(null);
  useEffect(() => {
    let live = true;
    if (element.type === "mermaid") setText(element.source || "");
    else if (element.type === "chart" && element.spec_asset) {
      getJSON(teamPath(team, `assets/${element.spec_asset}`))
        .then((spec) => live && setText(JSON.stringify(spec, null, 2)))
        .catch((err) => live && setText(`could not load the spec: ${err.message}`));
    } else if (element.type === "viz") {
      setText(`live visual, libraries: ${(element.libs || []).join(", ") || "none"}${element.data_path ? `, data: ${element.data_path}` : ""}`);
    } else {
      setText("");
    }
    return () => {
      live = false;
    };
  }, [team, element]);
  return <pre className="source">{text}</pre>;
}

export default function DiagramsTab({ team, teamRow, store }) {
  const scene = useSyncExternalStore(
    (fn) => store.subscribe(fn),
    () => store.get(),
  );
  const [open, setOpen] = useState(() => new Set());
  const items = scene.elements.filter((el) => KINDS.includes(el.type)).slice().reverse();
  if (!team) return <div className="empty">No team has the whiteboard on.</div>;
  if (!items.length) return <div className="empty">No diagrams, charts or live visuals on this canvas yet.</div>;
  return (
    <div className="gallery">
      {items.map((element) => (
        <figure key={element.id} className="gallery-item">
          <Picture team={team} element={element} vizOn={Boolean(teamRow && teamRow.viz)} />
          <figcaption>
            <b>{element.text || element.id}</b>
            <span className="muted">
              {" "}· {element.id} {element.type}
              {element.diagram ? ` (${element.diagram})` : ""} · {who(element.author)} · v{element.updated_seq}
            </span>
            <button
              type="button"
              className="link"
              onClick={() =>
                setOpen((prev) => {
                  const next = new Set(prev);
                  if (next.has(element.id)) next.delete(element.id);
                  else next.add(element.id);
                  return next;
                })
              }
            >
              {open.has(element.id) ? "hide source" : "source"}
            </button>
            {element.intent ? <div className="muted">{element.intent}</div> : null}
          </figcaption>
          {open.has(element.id) ? <Source team={team} element={element} /> : null}
        </figure>
      ))}
    </div>
  );
}
