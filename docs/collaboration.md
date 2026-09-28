# Collaboration on the canvas

Canvas v2 phase 5 (`.local/prd/canvas-v2-phase5.md`). The canvas is shared by the
operator and her team's agents. The operator in person is the lead: nothing an
agent does changes her marks unseen, the collaboration layer never refuses her,
and only she decides. Agents contribute in their own lanes; what they would do
elsewhere waits as a proposal. The code is `herdr_team/canvas_collab.py` (the
gate, proposals, freezes, settings, undo and checkpoints) and
`herdr_team/canvas_presence.py` (presence).

## Who is who

| Actor | In code | May |
|---|---|---|
| The lead | `canvas_collab.is_lead(author)`: `is_human and operator` (the writable page, the console, a trusted shell) | everything; never refused by the collaboration layer; the only one who accepts, rejects, freezes, thaws, restores, changes the settings or forces an undo |
| A delegate | `is_member and operator` (an operator grant) | agents' work like the lead; her marks follow `human_edits` like anyone's; bound by freezes; exempt from locks and lanes |
| The manager | `is_member and manager` | agents' elements live; exempt from lanes |
| A member | `is_member` | its own elements and its lane; everything else is a proposal |

## The review gate

Every op runs as it always did. For anyone but the lead, and for every
*proposable* op (the core `move`, `restyle`, `edit`, `delete`, `refit`,
`patch`, `place`, `pin`, `unpin`, and every kind's create op unless its
`OpSpec.proposable` is False, as a comment's is), the gate then looks at the
op's element changes and decides:

- **live**: the changes fold into the canvas as before;
- **propose**: none of them reach the canvas; they become one proposal record;
- **refuse**: the op is refused with a code, like any refusal.

An op refused only for authority (`element_not_yours`) runs once more with the
author's authority raised for that op alone (D2); what that run produced can
only become a proposal, or live under `human_edits: live` for the operator's
marks. The `locked` and `frozen` rules check it as the real author, and a rule
never sees the raised author.

A change is *primary* unless it is an arrow rerouted or a comment following its
element; the rules judge primary changes.

### The rules (`register_rule`, in order)

| Order | Rule | Verdict |
|---|---|---|
| 10 | `busy` | the element the operator is editing now, on any of her fresh pages: refuse `element_busy` (retry after 5 s) |
| 20 | `locked` | a raised run touching a lock: refuse `canvas_locked` |
| 30 | `frozen` | a frozen element, what it holds, or a new mark in a frozen region: propose, or refuse `frozen` under `frozen: refuse` |
| 40 | `stale_base` | an aimed element the operator changed after the batch's `base`: refuse `stale_base` (it yields to a proposal, which then carries a base note) |
| 50 | `human_made` | the operator's marks, or a new mark in her container or on her top-level marks: propose, or live under `human_edits: live` (the event says `touched_human`) |
| 60 | `peer` | a raised run changing another member's marks: propose |
| 70 | `foreign_lane` | a plain member's new mark whose centre lies in another author's claim or home: propose |

Combining: any refusal wins (except `stale_base`, which yields to a proposal);
else any proposal makes the whole op one proposal (ops are never split); else
live. A raised run that would be live for any other reason than her own
`human_edits: live` is a proposal.

### Adding a rule

A rule is a pure function of the `Review` (the real author, the op, the changes
with their before and after values, the settings, freezes, locks, lanes, the
operator's territory and presence, the stale entries) returning a `Verdict` or
None:

```python
from herdr_team.canvas_collab import Verdict, register_rule

def pinned(review):
    hits = [c.id for c in review.changes if c.primary and c.before and (c.before.get("pin") or {}).get("by") == "human"]
    return Verdict("propose", "pinned", "{} was placed by the operator".format(hits[0]), tuple(hits)) if hits else None

register_rule("pinned", pinned, 55)
```

Add its rows to `tests/fixtures/collab/matrix.json`; nothing else changes.

## Base and stale edits

A batch may carry `base`, the version its author last read (a number, or
`"last"`: the author's look cursor). For each op, the elements it aims at
(`id`, `ids`) that changed after `base` are read from the log once per batch
(at most 5,000 events back). Changed by the author only: nothing. By another
agent: the op applies with a `stale_base` warning saying what changed
(`E-9 changed since v405 by beta (v410): moved by c0r+3`). By the operator, or
too far back to tell: refused `stale_base`, with each change's `diff_lines`
(from the author's own last value when it made or changed the element after
`base`) and the current versions. The lead's own batches only warn. A `base`
newer than the canvas is refused `op_invalid`.

## Lanes

An author's lane is its active claims plus its home. A live op inside one of
the author's claims renews it once less than half its five minutes is left. A
plain member's new marks (not comments or arrows) outside every lane claim
their area (`auto: true`); the batch grows that one claim, and so does a later
batch drawing within 200 units of an automatic claim of the author's (up to
2400 a side, never over another author's lane); a fourth claim releases the
oldest automatic one first.

## Proposals

`P-n`, target `proposal`. The record holds its author, batch, op, intent,
reasons, `base` and `base_note`, the `targets`, `created` and `deleted` ids,
the `changes` (each with the element's `updated_seq` when it was made as `was`,
and the whole after-value), and a `summary` (`diff_lines` per change, at most
12 lines). The ids it would create are reserved. A new proposal by the same
author on the same targets supersedes the older. At most 20 open per author
and 200 in all (`proposal_limit`). The scene carries the open ones and the 20
newest decided; decided ones drop their values.

- `accept {id, note?}` (the lead) replays the stored after-values through the
  normal pipeline (unbinding, rerouting, comments following). A proposal whose
  targets changed since it was made is outdated and refused
  `proposal_outdated`. New elements keep their proposer as author and gain
  `accepted: P-n`. Undoing the accept batch takes it back.
- `reject {id, note?}` (the lead) and `withdraw {id}` (the proposer or the lead)
  set the status; neither touches the canvas.
- A later op in the same batch that names what only a proposal holds is refused
  `in_proposal`.
- The proposer reads the outcome in `look` (`decided:`), once.

## Freezes and settings

`freeze {region | ids, label}` (the lead) makes `X-n` (target `freeze`); an id
freeze covers its elements and what they hold, and its outline follows them. It
binds delegates too; comments are never frozen. `thaw {id}` or `thaw {ids}`.

`settings {human_edits, frozen}` (the lead) is canvas state (target `setting`,
id `collab`), logged and undoable. `human_edits`: `propose` (the default) or
`live`; `frozen`: `propose` (the default) or `refuse`. The scene carries
`settings.collab`.

## Undo and revert

`undo {batch}` or `undo {author, since?}` (every not-undone batch of that
author after `since`, its own undo batches aside, newest first). An element is
written back only when every change to it after that batch came from batches
reverted in the same op; otherwise it is skipped, and the result's `undo`
lists `{id, by, seq}` and the warning says `3 of 7 reverted; E-12 edited by the
operator later (v415)`. `force: true` (the lead) writes back anyway. A member
undoes its own batches; the manager and delegates any agent's; the lead
anything.

An undo is not proposable, but for anyone but the lead (delegates too) a freeze
binds it like any op: a write-back to a frozen mark, or one that would put a
mark back into a frozen region, is skipped as `{id, reason: "frozen", freeze}`
in either `frozen` setting, and an undo that would touch the element the
operator is editing on any of her pages is refused `element_busy`. What an undo
leaves (skipped, frozen, or not the undoer's to change) stays on the batch as
`left`: `undo {batch}` again tries those keys once more (refused, saying why,
when nothing would change), and the lead's `force` writes them back, by batch or
by `{author, since, force}`. Open proposals the reverted batches made are
withdrawn. "N of M reverted" counts marks and settings, not the automatic
claims that came with them.

## Checkpoints

`checkpoint {label}` saves the elements as `whiteboard/checkpoints/V-n.json`
(at most 8 MiB); the lead keeps 30 named, each other author 3, and there are 10
automatic ones: before a batch of 30 ops or more from anyone but the lead,
before a `restore`, and before `canvas clear` (which keeps `checkpoints/`, the
records, and a copy of the assets their elements name). `restore {id}` (the
lead) applies the difference to the snapshot as one op, comments excluded; undo
it like any batch.

## Comments

A comment on an element stores `anchor: [u, v]` (fractions of the element's
box, top-right `[1, 0]` by default) or `[u, v, dx, dy]` (a reply, 16 right and
16 down of what it answers). An op that changes the element's box moves the
comment's `point` with it. When the element goes, the comment keeps its point
and gains `was_on`. `look` derives `answered`: an open comment with a reply
from a member it mentions.

## Presence

Files, never the log (`whiteboard/presence/`, at most 4 KiB each, atomic):

- `<member>.json`: written for a verified member by its draws (`drawing`, 90 s),
  its looks (`reading`, 60 s) and `canvas focus` (60 to 3600 s);
- `human-<page>.json`: the operator's pages, through `POST /presence` only
  (writable sessions, 8 a second, 30 s).

A record is fresh while `at + ttl_s > now` and it is not `away`; everything is
validated again on read, and files older than ten minutes are removed. A
member whose roster status is `left`, `missing` or `failed` is not present
whatever its file says, and removing a member deletes its file. The operator
keeps at most 8 page files; a new page id removes the oldest. A `look` without a
region is `reading` with no region (no halo round the whole board). It feeds
the `busy` rule, the `operator_selected` warning, `look` (`operator:` and
`here:` lines, `--region operator`) and every member's apply result
(`operator: {viewport, selection, editing, pointing_at, age_s}`). The page's
stream sends it as `presence`. Presence is a view, never authority.

## Readback

`look` adds `presence`, `proposals` (open, each with `outdated`), `decided`,
`freezes`, `settings` and `checkpoints` (the newest five) to its JSON, and these
lines after `locks:`, each only when it has something to say:

```
operator: viewing c10r4:c60r30 · selected E-4, E-7 · editing E-4 · pointing at E-9 · 4s ago
here: beta drawing c80r4:c120r20 "pricing table" (3s ago)
proposals (2 open): P-3 by you: move E-4 (the operator's), waiting; P-5 by beta: patch E-9 (yours), outdated
decided: P-2 accepted by the operator; P-1 rejected by the operator: "keep it red"
frozen: X-5 c0r0:c40r20 "final layout" (your changes become proposals)
settings: the operator's marks: proposals · frozen: proposals
checkpoints: V-3 "before pricing rework" v400; V-4 auto v431
```

Element lines gain ` [frozen]` and ` [proposal P-3]`; `look --proposals`
prints each open proposal in full. The display list draws ghosts and freezes
(`docs/display-list.md`). The v1 (Excalidraw) page shows none of it: review
proposals on the v2 page (`?engine=v2`) or with `canvas accept|reject`.

## Files

```
whiteboard/presence/<name>.json, human-<page>.json    presence (never in events.jsonl)
whiteboard/checkpoints/V-n.json                       checkpoint snapshots (kept by clear, removed by purge)
tests/fixtures/collab/                                the page's fixtures (python3 -m herdr_team.canvas_collab --write-fixtures)
tests/fixtures/canvas_scenes/collab.json              the multi-author golden scene
```
