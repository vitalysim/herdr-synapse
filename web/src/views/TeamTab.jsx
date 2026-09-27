// The Team tab: views Synapse generates from the team's own state (contract 15), read-only.
import React, { useEffect, useMemo, useState } from "react";
import { getJSON, teamPath } from "../api.js";
import MermaidImage from "./MermaidImage.jsx";
import { topologySource, workGraphSource } from "./mermaidViews.js";

const LANE_TITLES = { needs_you: "Needs you", blocked: "Blocked", working: "Working", done: "Done", idle: "Idle" };
const who = (name) => (name === "human" ? "the operator" : name || "");

function FactMap({ facts }) {
  const subjects = (facts && facts.subjects) || [];
  const disputes = new Map(((facts && facts.disputes) || []).map((d) => [d.id, d]));
  if (!subjects.length) return <p className="muted">No facts recorded yet.</p>;
  return (
    <div className="fact-map">
      {subjects.map((subject) => (
        <div key={subject.about} className="fact-subject">
          <h4>{subject.about}</h4>
          <ul>
            {(subject.facts || []).map((fact) => {
              const open = (fact.disputes || []).filter((id) => disputes.get(id) && disputes.get(id).open);
              return (
                <li key={fact.id} className={`fact ${fact.status}${open.length ? " disputed" : ""}`}>
                  <span className="fact-id">{fact.id}</span> {fact.attribute ? <b>{fact.attribute}: </b> : null}
                  {fact.statement}
                  <span className="muted">
                    {" "}· {who(fact.author)} · support {fact.support ?? 0}
                    {fact.status !== "current" ? ` · ${fact.status}` : ""}
                    {fact.superseded_by ? ` by ${fact.superseded_by}` : ""}
                  </span>
                  {open.map((id) => (
                    <span key={id} className="badge red">{id} {disputes.get(id).mode}</span>
                  ))}
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </div>
  );
}

function Timeline({ items }) {
  const list = (items || []).slice().reverse();
  if (!list.length) return <p className="muted">The board is empty.</p>;
  return (
    <ol className="timeline">
      {list.map((item) => (
        <li key={item.seq} className={`kind-${item.kind}`}>
          <span className="muted">#{item.seq} {item.ts ? new Date(item.ts).toLocaleTimeString() : ""}</span>{" "}
          <b>{who(item.from)}</b>
          {item.to && item.to.length ? <span className="muted"> → {item.to.map(who).join(", ")}</span> : null}{" "}
          <span className="badge">{item.event || item.kind}</span> {item.text}
        </li>
      ))}
    </ol>
  );
}

function Lanes({ lanes }) {
  const names = Object.keys(LANE_TITLES).filter((lane) => lanes && lanes[lane]);
  if (!names.length) return <p className="muted">Nothing in the lanes.</p>;
  return (
    <div className="lanes">
      {names.map((lane) => (
        <div key={lane} className="lane">
          <h4>
            {LANE_TITLES[lane]} <span className="count">{lanes[lane].length}</span>
          </h4>
          {lanes[lane].map((card, i) => (
            <div key={`${lane}-${i}`} className="lane-card">
              <div className="lane-title">{card.title}</div>
              {card.detail ? <div className="muted">{card.detail}</div> : null}
              {card.who ? <div className="muted">{who(card.who)}</div> : null}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

export default function TeamTab({ team, bus, visible }) {
  const [views, setViews] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    if (!team || !visible) return undefined;
    let live = true;
    getJSON(teamPath(team, "views"))
      .then((data) => live && setViews(data))
      .catch((err) => live && setError(err.message));
    return () => {
      live = false;
    };
  }, [team, visible]);
  useEffect(() => bus.subscribe("views", (data) => data && data.team === team && setViews(data.views)), [bus, team]);
  const workSource = useMemo(() => (views ? workGraphSource(views.work) : null), [views]);
  const topoSource = useMemo(() => (views ? topologySource(views.topology) : null), [views]);
  if (!team) return <div className="empty">No team has the whiteboard on.</div>;
  if (error && !views) return <div className="empty">Could not load the team views: {error}</div>;
  if (!views) return <div className="empty">Loading…</div>;
  return (
    <div className="team-tab">
      <section className="card wide">
        <h3>Work graph</h3>
        {workSource ? <MermaidImage source={workSource} alt="work graph" /> : <p className="muted">No work items.</p>}
      </section>
      <section className="card">
        <h3>Topology</h3>
        {topoSource ? <MermaidImage source={topoSource} alt="team topology" /> : null}
      </section>
      <section className="card">
        <h3>Fact map</h3>
        <FactMap facts={views.facts} />
      </section>
      <section className="card wide">
        <h3>Lanes</h3>
        <Lanes lanes={views.lanes} />
      </section>
      <section className="card wide">
        <h3>Timeline</h3>
        <Timeline items={views.timeline} />
      </section>
      <p className="muted small">generated {views.generated_at}</p>
    </div>
  );
}
