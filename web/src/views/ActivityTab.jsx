// The Activity tab: one card per watched agent (and the selected team's members), read from
// what each harness already writes. Tool names and short arguments only, never output.
import React, { useEffect, useState } from "react";
import { getJSON } from "../api.js";

const ICONS = { run: "▶", edit: "✎", read: "⌕", search: "⌕", web: "🌐", agent: "◆", mcp: "⚙", tool: "•" };

function Card({ card }) {
  const [showPrompt, setShowPrompt] = useState(false);
  const plan = card.plan;
  return (
    <div className={`activity-card state-${card.state}`}>
      <div className="activity-head">
        <span className={`state-dot ${card.state}`} title={card.state} />
        <b>{card.name || card.pane_id}</b>
        <span className="muted">
          {" "}· {[card.kind, card.profile].filter(Boolean).join("/")} · {card.pane_id}
          {card.team ? ` · ${card.team}` : ""}
          {card.watched ? " · watched" : ""}
        </span>
      </div>
      {card.headline ? <div className="headline">{card.headline}</div> : null}
      {card.context && card.context.percent != null ? <div className="muted">context {Math.round(card.context.percent)}%</div> : null}
      {plan && plan.steps && plan.steps.length ? (
        <ol className="plan">
          {plan.steps.map((step, i) => (
            <li key={i} className={step.status}>
              {step.status === "completed" ? "✓ " : ""}
              {step.text}
            </li>
          ))}
        </ol>
      ) : null}
      {card.actions && card.actions.length ? (
        <ul className="actions">
          {card.actions.map((action, i) => (
            <li key={i}>
              <span className="icon">{ICONS[action.icon] || "•"}</span> <code>{action.text}</code>
            </li>
          ))}
        </ul>
      ) : null}
      {card.files && card.files.length ? (
        <div className="files">
          {card.files.slice(0, 8).map((file) => (
            <code key={file}>{file}</code>
          ))}
        </div>
      ) : null}
      {card.last_prompt ? (
        <button type="button" className="link" onClick={() => setShowPrompt((v) => !v)}>
          {showPrompt ? "hide the last prompt" : "last prompt"}
        </button>
      ) : null}
      {showPrompt ? <div className="prompt">{card.last_prompt}</div> : null}
      {card.reader_error ? <div className="muted">activity: {card.reader_error}</div> : null}
    </div>
  );
}

export default function ActivityTab({ team, bus, visible }) {
  const [cards, setCards] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    if (!visible) return undefined;
    let live = true;
    getJSON(`/api/activity${team ? `?team=${encodeURIComponent(team)}` : ""}`)
      .then((data) => live && setCards(data.cards || []))
      .catch((err) => live && setError(err.message));
    return () => {
      live = false;
    };
  }, [team, visible]);
  useEffect(() => bus.subscribe("activity", (data) => data && setCards(data.cards || [])), [bus]);
  if (error && !cards) return <div className="empty">Could not load activity: {error}</div>;
  if (!cards) return <div className="empty">Loading…</div>;
  if (!cards.length) {
    return (
      <div className="empty">
        Nobody is watched. Flag an agent with <code>o</code> in the team picker, or <code>herdr-synapse watch &lt;pane&gt;</code>.
      </div>
    );
  }
  return (
    <div className="activity-grid">
      {cards.map((card) => (
        <Card key={card.key || card.terminal_id || card.pane_id} card={card} />
      ))}
    </div>
  );
}
