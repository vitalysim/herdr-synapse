// slot "mermaid": a Mermaid diagram drawn locally by canvas/renderers.js (securityLevel strict).
import React from "react";
import { fileIdFor, renderFile } from "../../../canvas/renderers.js";
import LiveImageSlot from "./image.jsx";

const load = (team) => (element) => renderFile(team, element, fileIdFor(element, true));
const loaders = new Map();

export default function MermaidSlot(props) {
  if (!loaders.has(props.team)) loaders.set(props.team, load(props.team));
  return <LiveImageSlot {...props} load={loaders.get(props.team)} />;
}
