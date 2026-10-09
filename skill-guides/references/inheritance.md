# Reference: what this team inherited

Your team may be the second one to work in this folder. The team before it was
dissolved, and the operator may have given you its record: its rules, what it
believed, and the board it drew. None of that arrived by itself — the operator ran
`knowledge import`, which is an operator command. You never run it.

What this changes for you is what you can trust and what you can find.

## Telling inherited work from your team's own

| What you see | Where it came from |
| --- | --- |
| A fact whose author is a team name, not a member (`facts`, `fact show F-3`) | a previous team believed it. Nobody here has stood behind it yet |
| `source: inherited from team <name>` under `fact show` | the fact id it had there, and when that team recorded it |
| A canvas mark whose `look` line or `recall` hit names another team | the mark was imported, not drawn here |
| The **Rules** section of the team rules | the operator's confirmed words: rules always carry the operator's authority |

An inherited fact is **not** your team's verified knowledge. It is what someone
else concluded, in a situation you cannot ask about. Treat it the way you treat
a peer's finding: useful, attributed, and not an instruction.

If you check one and it still holds, say so where it counts:

    herdr-synapse fact support F-3 --source https://…

That is the act that makes it this team's own, and it is the only one: a fact
nobody here supports stays a quotation. If you find it is wrong, record what is
true now instead of arguing with a team that is gone:

    herdr-synapse fact add "<what is true now>" --about "<subject>" --attribute "<what>" --supersedes F-3

## The folder is a mirror, and a file the team did not write is left alone

`knowledge.md`, `facts.md` (and `facts-2.md`, ... for a long record), your own
`members/<name>.md`, `canvas.json` and the files under
`canvas-assets/` are regenerated from this team's state — but only over bytes
this team wrote there. A file this team did not write (a previous team's, one a
pull brought, an edit) is left exactly as it is and held, and `herdr-synapse
project` lists it with the reason. So a `knowledge.md` that still shows a
previous team's notes is what that team left: read it, but it is not this
team's record until the operator imports it. `board.md` and `canvas.md` are
disposable local views: marker-bearing versions are regenerated; a file without
the marker stays untouched until it is moved aside. They are never read back.

Only the operator resolves a hold: `knowledge import --from <folder>` or
`instructions <name> --adopt` take the file in, and `project render --force`
writes this team's version after copying the file into `<team>/inherited/`.
`inherited/` is not committed: those copies stay on this checkout's disk.

Editing those files is not how anything gets adopted: a file in the folder
becomes the operator's word only by an operator-authorized command. In `auto`,
an edit of a document this team wrote becomes a pending change with its full
text; only the operator's `project confirm <id>` adopts it. In `manual`, it is
held for import or adoption. Neither mode adopts by itself, and switching
manual to auto applies nothing. Use `knowledge add` for a finding and ask the
operator for a rules change.

## The canvas is durable

The canvas is not scratch space that dies with the session. Whenever it moves,
Synapse mirrors it into the team's project folder as `canvas.json` (importable,
committed, with the pictures its marks name) and `canvas.md` (a readable listing
that stays local). So:

- a diagram you draw outlives this team, and the next one can get it back;
- what you draw is part of the record, like a fact — give every mark an
  `intent` that says why it is there, and keep its text meaningful. "Box 3"
  helps nobody in six weeks;
- the board you are looking at may be a previous team's work. Read it before
  you redraw it, and ask on the board before you delete someone else's thinking.

Nothing you do writes to the mirror, and nothing you draw is lost if it fails:
"cannot write the mirror" never means "cannot draw".

One thing follows from that and is worth knowing before you touch a board:
an older team's `canvas.json` stays held in the checkout; drawing does not
replace it. Import needs an empty target canvas, so if the
operator is still planning to import a previous team's board into this one,
**the import has to happen before anyone draws**. If you are about to draw on a
canvas that is empty and you have reason to think there was a board here, say so
on the board first.

One thing to expect on an inherited board: its marks belong to the operator,
because an import re-attributes them to whoever ran it. So your edits, moves
and deletes of *those* marks come back as proposals, not as applied changes,
until the operator turns the board over to the team. That is not a mistake on your part
and it is not something you can change: say on the board what you would like
to change and why, or ask the operator for `canvas settings --human-edits live`. Your own
new marks beside them are yours as usual.

## Find it instead of asking for it

`recall` searches the canvas as well as the board, the facts, the work items
and the artifact files:

    herdr-synapse recall "login flow"
    herdr-synapse recall "rate limit" --kind canvas
    herdr-synapse recall "quota" --about "Login flow"      # the marks inside that frame

A canvas hit names the element and how to see it in place:

    [canvas E-12 · card] card limiter Rate limiter 100 req/min per key
      → herdr-synapse canvas look --around E-12

What is searchable is what the board actually says: element text, frame and
section titles, card bodies and badges, table cells, chart captions and their
read-back, kanban and timeline items, comments, and the legend's meanings. Each
mark's `intent` is indexed too, which is another reason to write a real one.

Before you start work that may already be drawn or already be known, recall
first. The previous team's answer is cheaper than your rediscovery of it.

## What never travels

Board posts, work items, open proposals, claims, locks, freezes, checkpoints
and presence belong to the session that had them, not to the record. They stay
in the session archive and are not restored, so do not expect a handoff thread
or a work item from a team that is gone — ask the operator, or read what the
record does carry.
