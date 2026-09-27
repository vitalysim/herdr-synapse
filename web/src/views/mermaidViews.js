// Team views as Mermaid flowcharts. Every label is agent- or member-written text, so it is
// escaped into Mermaid's quoted-label syntax (entity codes, no markup) and the diagram is
// rendered with securityLevel "strict" and shown as an <img>, where no script can run.
// Status colours are tones from the design tokens.
import { mermaidClass } from "../theme/tokens.js";

export function label(text, max = 60) {
  const clean = String(text ?? "")
    .replace(/\s+/g, " ")
    .trim();
  const clipped = clean.length > max ? `${clean.slice(0, max - 1)}…` : clean;
  return clipped.replace(/[#"<>`&]/g, (ch) => `#${ch.charCodeAt(0)};`);
}

const STATUS_CLASS = {
  open: "open", assigned: "open", in_progress: "active", blocked: "blocked", in_review: "review", changes_requested: "review",
  done: "done", failed: "failed", partial: "review", cancelled: "cancelled",
};

export function workGraphSource(work) {
  const nodes = (work && work.nodes) || [];
  if (!nodes.length) return null;
  const ids = new Map(nodes.map((node, i) => [node.id, `w${i}`]));
  const lines = ["flowchart LR"];
  for (const node of nodes) {
    const parts = [`${node.id} ${node.title || ""}`];
    const meta = [node.status, node.owner, node.attempt > 1 ? `attempt ${node.attempt}` : null, node.review ? "review gate" : null].filter(Boolean);
    lines.push(`  ${ids.get(node.id)}["${label(parts[0], 48)}<br/>${label(meta.join(" · "), 48)}"]`);
    lines.push(`  class ${ids.get(node.id)} ${STATUS_CLASS[node.status] || "open"}`);
  }
  for (const edge of (work && work.edges) || []) {
    if (ids.has(edge.from) && ids.has(edge.to)) lines.push(`  ${ids.get(edge.from)} --> ${ids.get(edge.to)}`);
  }
  lines.push(
    `  classDef open ${mermaidClass("neutral", "outline")}`,
    `  classDef active ${mermaidClass("info")}`,
    `  classDef blocked ${mermaidClass("danger")}`,
    `  classDef review ${mermaidClass("warning")}`,
    `  classDef done ${mermaidClass("success")}`,
    `  classDef failed ${mermaidClass("danger", "solid")}`,
    `  classDef cancelled ${mermaidClass("neutral")},stroke-dasharray:4 3`,
  );
  return lines.join("\n");
}

export function topologySource(topology) {
  if (!topology) return null;
  const lines = ["flowchart TB", `  team(["team ${label(topology.team)}"])`];
  const members = topology.members || [];
  members.forEach((member, i) => {
    const kind = [member.kind, member.profile].filter(Boolean).join("/");
    const state = [member.status, member.agent_status].filter(Boolean).join(", ");
    lines.push(`  m${i}["${label(member.name, 40)}<br/>${label(member.role || "", 30)} · ${label(kind, 30)}<br/>${label(state, 40)}"]`);
    lines.push(member.name === topology.manager ? `  team ==> m${i}` : `  team --> m${i}`);
    if (member.name === topology.manager) lines.push(`  class m${i} manager`);
  });
  (topology.links || []).forEach((link, i) => {
    lines.push(`  l${i}[/"linked: ${label(link.team, 30)}<br/>${label(link.state || "", 20)}"/]`);
    lines.push(`  team -.- l${i}`);
  });
  lines.push(`  classDef manager ${mermaidClass("accent")}`);
  return lines.join("\n");
}
