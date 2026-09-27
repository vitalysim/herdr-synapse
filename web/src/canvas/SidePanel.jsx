// The canvas side panel: what the human can do beyond Excalidraw's own tools. Every
// action is an operation (or /send) the server checks; nothing here edits the scene.
import React, { useEffect, useState, useSyncExternalStore } from "react";
import { activeClaims } from "../sceneStore.js";

const who = (name) => (name === "human" ? "the operator" : name);

function useScene(store) {
  return useSyncExternalStore(
    (fn) => store.subscribe(fn),
    () => store.get(),
  );
}

function Section({ title, children, count }) {
  const [open, setOpen] = useState(true);
  return (
    <section className="panel-section">
      <button type="button" className="panel-heading" onClick={() => setOpen((v) => !v)}>
        <span>{open ? "▾" : "▸"}</span> {title}
        {count ? <span className="count">{count}</span> : null}
      </button>
      {open ? <div className="panel-body">{children}</div> : null}
    </section>
  );
}

function SelectionActions({ teamRow, writable, selection, actions }) {
  const [to, setTo] = useState("");
  const [note, setNote] = useState("");
  const [lockLabel, setLockLabel] = useState("");
  const [comment, setComment] = useState("");
  const members = (teamRow && teamRow.members) || [];
  useEffect(() => {
    if (!to && members.length) setTo(members[0].name);
  }, [members, to]);
  const count = selection.filter((id) => id.startsWith("E-")).length;
  return (
    <Section title={count ? `Selection (${count})` : "Selection"}>
      {count ? (
        <>
          <button type="button" onClick={actions.copyText}>Copy text form</button>
          {writable ? (
            <>
              <div className="field-row">
                <label htmlFor="send-to">Send to</label>
                <select id="send-to" value={to} onChange={(event) => setTo(event.target.value)}>
                  {members.map((member) => (
                    <option key={member.name} value={member.name}>
                      {member.name}
                    </option>
                  ))}
                </select>
              </div>
              <textarea placeholder="Note (optional): what to do with it" value={note} maxLength={1000} onChange={(event) => setNote(event.target.value)} />
              <button type="button" disabled={!to} onClick={() => actions.send(to, note).then(() => setNote(""))}>
                Send to {to || "…"}
              </button>
              <div className="field-row">
                <input placeholder="lock label" value={lockLabel} maxLength={200} onChange={(event) => setLockLabel(event.target.value)} />
                <button type="button" onClick={() => actions.lock(lockLabel).then(() => setLockLabel(""))}>Lock this area</button>
              </div>
            </>
          ) : null}
        </>
      ) : (
        <p className="muted">Select marks to send them to a member, copy their text form or lock their area.</p>
      )}
      {writable ? (
        <>
          <textarea
            placeholder={count ? "Comment on the selection; @name wakes a member" : "Comment in the middle of the view; @name wakes a member"}
            value={comment}
            maxLength={1000}
            onChange={(event) => setComment(event.target.value)}
          />
          <button type="button" disabled={!comment.trim()} onClick={() => actions.comment(comment).then(() => setComment(""))}>
            Comment
          </button>
        </>
      ) : null}
    </Section>
  );
}

function CommentThread({ scene, id, writable, actions }) {
  const [reply, setReply] = useState("");
  const root = scene.elements.find((el) => el.id === id);
  if (!root || root.type !== "comment") return null;
  const top = root.reply_to ? scene.elements.find((el) => el.id === root.reply_to) || root : root;
  const replies = scene.elements.filter((el) => el.type === "comment" && el.reply_to === top.id);
  const target = top.on ? scene.elements.find((el) => el.id === top.on) : null;
  return (
    <Section title={`Comment ${top.id}${top.resolved ? " (resolved)" : ""}`}>
      {target ? (
        <p className="muted">
          on <button type="button" className="link" onClick={() => actions.focus(target.id)}>{target.id}</button> {target.text ? `“${target.text}”` : target.type}
        </p>
      ) : null}
      {[top, ...replies].map((item) => (
        <div key={item.id} className="comment">
          <div className="comment-head">
            {who(item.author)} · {item.id}
            {item.mentions && item.mentions.length ? <span className="muted"> → {item.mentions.map((m) => `@${m}`).join(" ")}</span> : null}
          </div>
          <div className="comment-text">{item.text}</div>
        </div>
      ))}
      {writable ? (
        <>
          <textarea placeholder="Reply" value={reply} maxLength={1000} onChange={(event) => setReply(event.target.value)} />
          <div className="field-row">
            <button type="button" disabled={!reply.trim()} onClick={() => actions.comment(reply, top.id).then(() => setReply(""))}>Reply</button>
            {!top.resolved ? <button type="button" onClick={() => actions.resolve(top.id)}>Resolve</button> : null}
          </div>
        </>
      ) : null}
    </Section>
  );
}

function History({ scene, store, writable, actions }) {
  const events = store.recentEvents();
  const intents = new Map();
  for (const event of events) {
    if (event.batch && event.intent && !intents.has(event.batch)) intents.set(event.batch, event.intent);
  }
  const batches = Object.entries(scene.batches || {})
    .map(([id, batch]) => ({ id, ...batch }))
    .sort((a, b) => (b.last_seq || 0) - (a.last_seq || 0))
    .slice(0, 30);
  if (!batches.length) return <p className="muted">Nothing drawn yet.</p>;
  return (
    <ul className="history">
      {batches.map((batch) => (
        <li key={batch.id} className={batch.undone ? "undone" : ""}>
          <span className="swatch" style={{ background: scene.authors?.[batch.author]?.color || "#1e1e1e" }} />
          <span className="history-main">
            <b>{who(batch.author)}</b> {batch.id} v{batch.first_seq}
            {batch.last_seq !== batch.first_seq ? `–${batch.last_seq}` : ""}
            {intents.has(batch.id) ? <span className="muted"> — {intents.get(batch.id)}</span> : null}
          </span>
          {writable && !batch.undone ? (
            <button type="button" className="small" title="Undo this batch" onClick={() => actions.undo(batch.id)}>undo</button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

export default function SidePanel({ teamRow, writable, store, selection, hidden, actions, onClose }) {
  const scene = useScene(store);
  const claims = activeClaims(scene);
  const openComments = scene.elements.filter((el) => el.type === "comment" && !el.resolved && !el.reply_to);
  const authors = Object.entries(scene.authors || {}).sort((a, b) => (a[1].index ?? 0) - (b[1].index ?? 0));
  const commentId = selection.find((id) => id.startsWith("C-"));
  return (
    <aside className="side-panel">
      <div className="panel-top">
        <b>Canvas</b>
        <span className="muted"> v{scene.version}</span>
        <button type="button" className="close" onClick={onClose} aria-label="Close the panel">×</button>
      </div>
      {commentId ? <CommentThread scene={scene} id={commentId} writable={writable} actions={actions} /> : null}
      <SelectionActions teamRow={teamRow} writable={writable} selection={selection} actions={actions} />
      <Section title="Legend" count={scene.legend.length}>
        {scene.legend.length ? (
          <ul className="legend">
            {scene.legend.map((entry) => (
              <li key={entry.id}>
                {/^E-\d+$/.test(entry.symbol || "") ? (
                  <button type="button" className="link" onClick={() => actions.focus(entry.symbol)}>{entry.symbol}</button>
                ) : (
                  <b>{entry.symbol}</b>
                )}{" "}
                = {entry.meaning} <span className="muted">({who(entry.author)})</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No conventions recorded yet. Agents add them with the legend operation.</p>
        )}
      </Section>
      <Section title="Open comments" count={openComments.length}>
        {openComments.length ? (
          <ul className="plain">
            {openComments.map((item) => (
              <li key={item.id}>
                <button type="button" className="link" onClick={() => actions.focus(item.id)}>{item.id}</button> {who(item.author)}: {item.text.slice(0, 120)}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">None.</p>
        )}
      </Section>
      <Section title="Claims and locks" count={claims.length + scene.locks.length}>
        {claims.map((claim) => (
          <div key={claim.id} className="claim-row">
            <span className="swatch dashed" style={{ borderColor: scene.authors?.[claim.author]?.color || "#868e96" }} />
            {claim.id} {who(claim.author)}: {claim.label}
          </div>
        ))}
        {scene.locks.map((lock) => (
          <div key={lock.id} className="claim-row">
            <span className="swatch hatched" />
            {lock.id} {lock.label || "locked"}
            {writable ? <button type="button" className="small" onClick={() => actions.unlock(lock.id)}>unlock</button> : null}
          </div>
        ))}
        {!claims.length && !scene.locks.length ? <p className="muted">Nobody is holding a region.</p> : null}
      </Section>
      <Section title="History">
        <History scene={scene} store={store} writable={writable} actions={actions} />
      </Section>
      <Section title="Layers" count={authors.length}>
        {authors.map(([name, author]) => (
          <label key={name} className="layer-row">
            <input type="checkbox" checked={!hidden.has(name)} onChange={() => actions.toggleAuthor(name)} />
            <span className="swatch" style={{ background: author.color }} />
            {who(name)}
            {author.agent ? <span className="muted"> · {author.agent}</span> : null}
          </label>
        ))}
        {!authors.length ? <p className="muted">No authors yet.</p> : null}
      </Section>
    </aside>
  );
}
