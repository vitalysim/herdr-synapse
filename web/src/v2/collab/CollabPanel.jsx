// The collaboration panel (canvas-v2-phase5.md 12.4), under the side panel: open proposals, the
// agents at work (with "Revert all by <name>"), checkpoints, frozen areas and the two settings.
// Writable pages (the operator in person) get the actions; read-only pages get the lists. Every
// action is an op the server checks; the panel only calls back.
import React, { useState } from "react";
import { authorChip } from "../../theme/tokens.js";
import { DEFAULT_SETTINGS, MAX_LABEL_CHARS } from "./ops.js";
import { revertChoices } from "./revert.js";

const who = (name) => (name === "human" ? "the operator" : name);

function Chip({ scene, name, theme }) {
  const chip = authorChip(scene, name, theme);
  return (
    <span className="author-chip" style={{ background: chip.bg, color: chip.fg }} aria-hidden="true">
      {chip.initials}
    </span>
  );
}

function Section({ title, count, children, name }) {
  const [open, setOpen] = useState(true);
  return (
    <section className="panel-section" data-section={name}>
      <button type="button" className="panel-heading" onClick={() => setOpen((v) => !v)}>
        <span>{open ? "▾" : "▸"}</span> {title}
        {count ? <span className="count">{count}</span> : null}
      </button>
      {open ? <div className="panel-body">{children}</div> : null}
    </section>
  );
}

function Segmented({ label, value, options, onPick, disabled, name }) {
  return (
    <div className="cv2-setting" data-setting={name}>
      <div className="cv2-setting-label">{label}</div>
      <div className="cv2-segmented" role="radiogroup" aria-label={label}>
        {options.map(([key, text]) => (
          <button
            key={key}
            type="button"
            role="radio"
            aria-checked={value === key}
            className={value === key ? "on" : ""}
            disabled={disabled}
            data-value={key}
            onClick={() => value !== key && onPick(key)}
          >
            {text}
          </button>
        ))}
      </div>
    </div>
  );
}

// The proposals the panel lists: the scene's open ones, oldest first, each with what the display
// list says about it now (outdated flips there when a target changes).
export function openProposals(scene, dlProposals = new Map()) {
  const rows = ((scene && scene.proposals) || []).filter((p) => p && p.status === "open");
  const byId = new Map(rows.map((p) => [p.id, p]));
  for (const [id, entry] of dlProposals) if (!byId.has(id)) byId.set(id, { id, status: "open", ...entry });
  const num = (id) => Number(String(id).replace(/^\D+/, "")) || 0;
  return [...byId.values()]
    .map((p) => {
      const live = dlProposals.get(p.id);
      return { ...p, outdated: live ? Boolean(live.outdated) : Boolean(p.outdated), summary: (live && live.summary) || p.summary || [] };
    })
    .sort((a, b) => (Number(a.seq) || num(a.id)) - (Number(b.seq) || num(b.id)) || num(a.id) - num(b.id));
}

// The agents: every member author in the scene, plus any member present who has not drawn yet.
export function agentRows(scene, presence) {
  const members = new Map();
  for (const [name, info] of Object.entries((scene && scene.authors) || {})) {
    if (info && info.kind === "member") members.set(name, { name, agent: info.agent || null, index: info.index ?? 0, here: null });
  }
  for (const m of (presence && presence.members) || []) {
    const row = members.get(m.name) || { name: m.name, agent: m.agent, index: 1e9, here: null };
    row.here = m;
    members.set(m.name, row);
  }
  return [...members.values()].sort((a, b) => a.index - b.index || (a.name < b.name ? -1 : 1));
}

function AgentRow({ row, scene, theme, writable, onRevert, now }) {
  const [menu, setMenu] = useState(false);
  const choices = menu ? revertChoices(scene, row.name, now) : [];
  return (
    <div className="cv2-agent" data-agent={row.name}>
      <div className="claim-row">
        <Chip scene={scene} name={row.name} theme={theme} />
        <span className="cv2-agent-name">{row.name}</span>
        {row.here ? <span className={`cv2-status cv2-status-${row.here.status}`}>{row.here.status}</span> : <span className="muted">away</span>}
        {writable ? (
          <button type="button" className="small" aria-expanded={menu} onClick={() => setMenu((v) => !v)} title={`Revert ${row.name}'s batches`}>
            revert…
          </button>
        ) : null}
      </div>
      {row.here && row.here.intent ? <div className="muted cv2-agent-intent">{row.here.intent}</div> : null}
      {menu ? (
        <div className="cv2-menu" role="menu" aria-label={`Revert all by ${row.name}`}>
          <div className="muted">Revert all by {row.name}:</div>
          {choices.map((c) => (
            <button
              key={c.key}
              type="button"
              role="menuitem"
              data-choice={c.key}
              disabled={!c.count}
              onClick={() => {
                setMenu(false);
                onRevert(row.name, c.since, c.label);
              }}
            >
              {c.label} <span className="muted">({c.count} batch{c.count === 1 ? "" : "es"})</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Checkpoints({ scene, writable, actions }) {
  const [label, setLabel] = useState("");
  const list = ((scene && scene.checkpoints) || []).slice().sort((a, b) => (b.version || 0) - (a.version || 0));
  return (
    <>
      {writable ? (
        <div className="field-row">
          <input type="text" value={label} maxLength={MAX_LABEL_CHARS} placeholder="checkpoint label" aria-label="Checkpoint label" onChange={(e) => setLabel(e.target.value)} />
          <button
            type="button"
            className="cv2-save-checkpoint"
            disabled={!label.trim()}
            onClick={() => {
              actions.saveCheckpoint(label);
              setLabel("");
            }}
          >
            Save checkpoint
          </button>
        </div>
      ) : null}
      {list.length ? (
        <ul className="plain">
          {list.map((c) => (
            <li key={c.id} className="cv2-row" data-checkpoint={c.id}>
              <span className="cv2-row-main">
                <b>{c.id}</b> {c.label} <span className="muted">v{c.version}{c.auto ? " · auto" : ""}{c.by ? ` · ${who(c.by)}` : ""}</span>
              </span>
              {writable ? (
                <span className="cv2-row-actions">
                  <button type="button" className="small" onClick={() => actions.restore(c.id)}>restore</button>
                  <button type="button" className="small" onClick={() => actions.removeCheckpoint(c.id)}>remove</button>
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">No checkpoints yet.</p>
      )}
    </>
  );
}

export default function CollabPanel({ scene, presence, dlProposals, writable, theme = "light", actions, now = Date.now() }) {
  const proposals = openProposals(scene, dlProposals);
  const agents = agentRows(scene, presence);
  const freezes = (scene && scene.freezes) || [];
  const settings = { ...DEFAULT_SETTINGS, ...((scene && scene.settings && scene.settings.collab) || {}) };
  return (
    <div className="cv2-panel side-panel" aria-label="Collaboration">
      <Section title="Proposals" count={proposals.length} name="proposals">
        {proposals.length ? (
          <ul className="plain">
            {proposals.map((p) => (
              <li key={p.id} className="cv2-proposal-row" data-proposal={p.id}>
                <button type="button" className="link cv2-proposal-open" onClick={() => actions.openProposal(p.id)}>
                  <Chip scene={scene} name={p.author} theme={theme} />
                  <b>{p.id}</b>
                </button>{" "}
                <span className="cv2-proposal-line">{(p.summary && p.summary[0]) || p.intent || p.op}</span>
                {p.outdated ? <span className="cv2-badge warn">outdated</span> : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">Nothing waiting for you.</p>
        )}
      </Section>
      <Section title="Agents" count={agents.length} name="agents">
        {agents.length ? (
          agents.map((row) => <AgentRow key={row.name} row={row} scene={scene} theme={theme} writable={writable} onRevert={actions.revert} now={now} />)
        ) : (
          <p className="muted">No agent has drawn here yet.</p>
        )}
      </Section>
      <Section title="Checkpoints" count={((scene && scene.checkpoints) || []).length} name="checkpoints">
        <Checkpoints scene={scene} writable={writable} actions={actions} />
      </Section>
      <Section title="Frozen" count={freezes.length} name="frozen">
        {freezes.length ? (
          freezes.map((f) => (
            <div key={f.id} className="cv2-row" data-freeze={f.id}>
              <span className="cv2-row-main">
                <span className="cv2-glyph" aria-hidden="true">❄</span>
                <button type="button" className="link" onClick={() => actions.focusFreeze(f.id)}>{f.id}</button> {f.label || "frozen"}
                <span className="muted"> {f.ids ? f.ids.join(", ") : "an area"}</span>
              </span>
              {writable ? (
                <span className="cv2-row-actions">
                  <button type="button" className="small" onClick={() => actions.thaw(f.id)}>thaw</button>
                </span>
              ) : null}
            </div>
          ))
        ) : (
          <p className="muted">Nothing is frozen.</p>
        )}
      </Section>
      <Section title="Settings" name="settings">
        <Segmented
          name="human_edits"
          label="Agents' changes to your marks"
          value={settings.human_edits}
          options={[["propose", "Proposals"], ["live", "Live with revert"]]}
          disabled={!writable}
          onPick={(v) => actions.setSettings({ human_edits: v })}
        />
        <Segmented
          name="frozen"
          label="Frozen areas"
          value={settings.frozen}
          options={[["propose", "Proposals"], ["refuse", "Refuse"]]}
          disabled={!writable}
          onPick={(v) => actions.setSettings({ frozen: v })}
        />
      </Section>
    </div>
  );
}
