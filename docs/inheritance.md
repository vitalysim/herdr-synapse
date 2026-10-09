# What a new team inherits

## The contract

**What Synapse writes into `<project>/.herdr-synapse/<team>/`:** `knowledge.md`,
`facts.md` (continued in `facts-2.md`, `facts-3.md`, ... for a long record),
`members/<name>.md`, `canvas.json` and the `canvas-assets/` files its marks
name — plus `board.md` and `canvas.md`, regenerated views nothing reads back.
Beside the team folders it writes `.herdr-synapse/README.md` and
`.herdr-synapse/.gitignore`.

**What it never touches:** it writes a durable record in the team's folder
only when the path is empty, or when the file
still holds exactly the bytes this team last wrote there. Anything else — a
previous team's `knowledge.md`, a file a `git pull` brought in, a hand edit, an
agent's — is left exactly as it is. A file that already holds exactly what this
team would write is left as it is too: this team records that it found it, but
finding a file is not writing it, so this team's next version of it is held
like any other until you resolve it (the first render after an upgrade from an
older plugin is the one exception, because that plugin kept no record of what
it wrote). It deletes a picture from `canvas-assets/` only when this team wrote
those bytes itself. It copies nothing on its own, and nothing a file in the
folder says becomes this team's rules, facts, board or a member's instructions
until you run an import, adopt or confirmation yourself.

`board.md` and `canvas.md` are disposable views: nothing reads them back and
they are git-ignored. A marker-bearing version is regenerated, even if another
team wrote it. A file without the marker, a symlink or an unreadable file stays
untouched; the report prints the `mv` that moves it aside. A view is never held
by the durable-record guard, and `--force` does not overwrite a foreign view.

**Automatic document sync** (`project sync auto`, the mode `create` gives a new
team) proposes a settled edit of a document this team wrote — `knowledge.md`'s
Rules or a member's instructions — and posts it once to the operator with the
complete text that would be stored and `herdr-synapse project confirm <id>`.
Nothing changes until a verified operator runs that command. It prints the
same text as it stores it. The id binds the file bytes, current project, team
instance, stored text and revision, and member identity; a change to any of
those makes the old command refuse. A newer proposal gets a new id even if
the file's bytes did not change. The marker identifies the instance that wrote
the document, but cannot establish who edited it.

This applies to existing teams too. `project sync manual` only holds and lists
edits for `instructions <name> --adopt` or `knowledge import`; switching it to
auto applies nothing. An edit with nothing to adopt (a note outside the
sections or whitespace) stays held. A full render by another team instance is
reported as a replacement and is never proposed as an edit of this team's text.

**What a hold looks like:** a file left alone is *held*. `herdr-synapse
project` lists every held file with the reason and the commands that resolve
it, `project render` prints the same lines, and the board says it once:

```
held, left exactly as it is (1):
  /repo/.herdr-synapse/alpha/knowledge.md is left exactly as it is: this team has no record of writing
  what is there now; see its Rules, and what an import would take of its facts and canvas, with
  herdr-synapse knowledge import --from /repo/.herdr-synapse/alpha --dry-run --team alpha  ; write this
  team's version, copying the file into inherited/ first, with  herdr-synapse project render --force --team alpha
```

(one line on screen; wrapped here.) Every printed `herdr-synapse` command names
the team, so it acts on the team that holds the file even in a session with
several, and every printed command shell-quotes its paths.

A held file stays as it is until you resolve it, and until then this team's
own version of that one file is not written. That is the cost of never
overwriting, and it is deliberate. A held file you remove yourself is no longer
listed, except a synced document this team wrote, in automatic mode, which is
held as gone (below).

**The commands that resolve one:**

- `herdr-synapse knowledge import --from <folder>` takes the folder's rules,
  facts and canvas into this team. It prints the Rules it would adopt, word for
  word, and asks before adopting them (`--yes` or `--json` does not ask; without a terminal
  it refuses unless given one of those); `--dry-run` shows the same text and changes
  nothing. `herdr-synapse instructions <name> --adopt` takes one member
  document, in front of a diff; `herdr-synapse canvas import --from <folder>`
  takes the board; `herdr-synapse create <team> --inherit <folder>` does the
  first at creation. Each reads the file where it is. What it adopted becomes
  this team's, so the next render may write this team's version over it —
  through the copy below.
- `herdr-synapse project render --force` writes this team's version over
  replaceable held durable records. Conflicting named canvas assets stay untouched, and disposable marker-bearing
  views regenerate without copies. `instructions <name> --discard` does it for one member
  document.
- A synced document you deleted is held as *gone* in automatic mode, because a
  deletion may be an editor's save half done: `project render --force` writes
  it again, and so does `project sync manual`.

**When Synapse replaces durable bytes it did not write:** on one of those commands,
including your `project confirm`. It reads the file once, writes a
copy to `<team>/inherited/<name>-<digest>.<ext>` (a member document under
`inherited/members/`), reads the copy back and compares it, and replaces the
file only if the copy matches and the file still holds what it read. If the
copy cannot be made or checked — the file is over 1 MiB, `inherited/` is not a
writable directory, a different file already has the copy's name, or the file
changed meanwhile — it changes nothing and says why; move the file aside
yourself (the line prints an `mv` to a name nothing has yet) and render again.
A copy is named in a report only after its bytes have been read back and match,
and the report says the bytes were other than the ones this team last wrote
there — not that this team never wrote them, which it cannot know. Explicit adoption or
confirmation of an unrecorded file takes this checked copy and really writes the
file even when its bytes already match the canonical document. Matching a template
alone never makes those bytes this team's recorded write.

There is a remaining race: `os.replace` cannot be conditional on the final
byte check. A writer landing between that check and the replace can have its
newer bytes replaced, while the copy contains the bytes read earlier. The
check narrows that window; it does not eliminate it. A copy can also be edited
or removed after verification, so reports re-check it and say when it no
longer holds the replaced bytes.

A confirmation records its consent before saving authoritative text. If that
first state save fails, nothing is adopted. If later bookkeeping or
notifications fail after text was saved, the command shows the saved text and
a warning; the edited file may remain held for explicit recovery. Retrying a
consumed confirmation does not store it twice.

`inherited/` is git-ignored: its copies stay on this checkout's disk and do not
travel with a clone.

**Moving the folder.** After `mv`, `herdr-synapse project set <new path>`
carries over what this team recorded about each file it wrote, for every file
whose bytes at the new path are still exactly the recorded ones, so the moved
folder is not held as a stranger's.

## Why it is built this way

The question this answers, in the owner's own words: *"If I dissolve the team
and create a new one pointing at the same knowledge path, do they inherit
everything the previous team did?"* The folder now carries the operator's rules,
each member's instructions, the team's current facts with their provenance, and
the canvas as an importable scene with its pictures, so the answer is yes — as
soon as you say so. Inheritance is never automatic: a file in a checkout the
agents can write to must not become the operator's word by itself, so the
folder is read back only by the commands above, in front of you.

An earlier design copied every file this team had not written into
`inherited/` and then overwrote it. Six review rounds found every one of its
data losses in that copy-then-overwrite step (a copy that failed, collided, was
too large, landed in session state, or was edited afterwards), and every way an
agent's forged `knowledge.md` became the operator's Rules went through an
automatic adoption at first contact. Both are gone: nothing found in place is
copied or adopted by itself. Document sync `auto` proposes settled edits of
documents this team wrote for the operator's confirmation; while an edit
settles or waits for confirmation, the file stays held.

## The folder, file by file

`<project>/.herdr-synapse/` holds one directory per team, so two teams can
share a repository.

| Path | What it is | Written | In git | Read back |
| --- | --- | --- | --- | --- |
| `README.md`, `.gitignore` | the folder's explanation and ignore rules | every render | yes | never |
| `<team>/knowledge.md` | Rules (operator authority) + Findings (generated) | every render, unless held | yes | `knowledge import`, or the operator's `project confirm` for an auto-mode proposal |
| `<team>/members/<name>.md` | one member's instructions | every render, unless held | yes | `instructions <name> --adopt`, or the operator's `project confirm` for an auto-mode proposal |
| `<team>/facts.md`, `facts-2.md`, ... | current facts with their provenance, in parts past 256 KiB | every render, unless held | yes | `knowledge import`, every part |
| `<team>/canvas.json` | the scene, which is the import payload | when the canvas moves, unless held | yes | `canvas import` |
| `<team>/canvas-assets/` | the chart data and pictures the marks name | when the set changes; a file is deleted only if this team wrote it | yes | `canvas import` |
| `<team>/canvas.md` | the readable canvas listing | when the canvas moves; a foreign file is left alone | no | never |
| `<team>/inherited/` | checked copies taken before an operator-ordered replacement, import, adopt or confirmation | only by those | no | read it directly |
| `<team>/artifacts/` | the agents' own work products | by the agents | no | `recall`, `--ref`, charts |
| `<team>/exports/` | where `/export` writes | on demand | no | never |
| `<team>/board.md` | the whole board, archive included | when the board moves; a foreign file is left alone | no | never |

`facts.md`, `canvas.json` and `canvas-assets/` are committed on purpose: they
are the inheritance channel, so a checkout has to carry them. `board.md` and
`canvas.md` are not, because they churn and nothing reads them back. A 25-mark
board with no pictures adds tens of kilobytes to a checkout (measured: 17.6 KB
of `canvas.json` for 25 text marks); pasted pictures or chart images add up to
1 MiB each, 8 MiB in all, and a picture this team mirrored is pruned as soon as
no mark names it any more. A picture this team has no record of writing stays
where it is and is reported once, until a mark names it again or you remove it.

**Import before you draw.** A previous team's `canvas.json` is held, so the
first mark a new team draws does not replace it: the canvas mirror reports the
hold and writes nothing there until you choose. `canvas import --from <folder>`
brings the board in (it is refused while this team's board holds marks; the
refusal names the `canvas checkpoint` and `whiteboard clear` a replay needs),
and the next mirror copies the old file into `inherited/` before writing this
team's board over it.

Two things write the canvas mirror: the notifier, whenever the canvas moves,
and `herdr-synapse project render`, which brings the whole folder up to date
without a daemon.

Caps: the Markdown files truncate at 64 KiB, except `facts.md`, which never
truncates: past 256 KiB it continues in `facts-2.md`, `facts-3.md`, ..., each
under 256 KiB, and part 1 says how many parts there are and how many facts they
hold. `knowledge import` reads every part, and if one is missing or unreadable
it says which, with the count the record declares and the count it read, rather
than reporting the short count as the whole record. `canvas.json` does not
truncate either, because half a scene looks importable and is not: over 1 MiB it
is replaced by a valid JSON pointer (`"elements": null`) naming the scene file
and the two commands that move a board that size, and `canvas.md` says so in one
line.

**What never travels** is what belonged to a conversation rather than to the
record: board posts, work items, open proposals, claims, locks, freezes,
checkpoints, presence, the recall index and the canvas event log. They stay in
the session archive, readable, and are not restored. A post is addressed to
members who no longer exist and a work item is owned by one of them; restoring
either would be a team talking to ghosts.

## What `knowledge import` adopts

```bash
herdr-synapse knowledge import --from beta [--dry-run] [--yes]
                               [--no-rules] [--no-facts] [--no-canvas]
                               [--keep-authors] [--team-can-edit]
                               [--skip-unknown] [--skip-missing]
```

`--from` takes a team dissolved in this session, a team's folder in a project
directory (`<project>/.herdr-synapse/<team>`), or an archived team directory.
Operator only, and every step is independent: a step that refuses reports its
code and leaves the earlier steps standing. When the folder is this team's own,
each file a step adopted from may then be replaced by this team's version, after
the copy described in the contract.

1. **Rules.** From the source's Rules section (or an archived team's
   `rules.md`), stored as this team's rules. The text is printed in full, in
   `--dry-run` and in the real run, and the real run asks before adopting it
   (`--yes` or `--json` does not ask; with no terminal and neither flag the whole
   import refuses with `confirmation_required` and changes nothing). Refused
   when this team already has rules, unless `--yes`.
2. **Facts.** Every *current* fact: from the dissolved team's own record when
   it is in this session's archive, else from the `facts.md` the mirror wrote.
   Each arrives attributed to the source team (`by_kind: inherited`), with the
   source's own sources plus one record naming the team and the fact id it had
   there. Retired and superseded facts do not travel. Dispute records do not travel,
   so inherited current facts arrive without a live dispute. `fact support F-3` by a member who is actually here
   is what makes an inherited fact the team's own.
3. **The data files, then the canvas.** A chart's data file and a 3D model are
   read from the *importing* team's own `artifacts/`, so the import brings along
   every file the board reads (up to 8 MiB each, 32 MiB in all) and says what it
   brought; then the canvas arrives through the `import` op, as one undoable
   batch (see below).
4. **Member instructions: listed, never adopted by import.** An `instructions <name> --adopt`
   command is offered only for an eligible matching active member's document in this team's own folder.
   External-folder records are read in place: `--adopt` cannot take them because it reads only the current team's
   folder. Adoption is an operator act in front of a diff.

```
$ herdr-synapse knowledge import --from beta --yes --team gamma
inherited from beta (dissolved 2026-10-01 12:00), /…/_archive/beta-20261001T120000Z:
  rules    adopted as this team's operator Rules, 412 chars:
           | Every change needs a review before it lands.
           | …
  facts    12 of 14 current facts, attributed to beta; to make one yours,
           support it:  herdr-synapse fact support F-3
  data     metrics.csv copied into this team's artifacts/
  canvas   41 marks, 3 comments, 2 pictures, as B-1 (undo B-1 to take it back)
           these marks are yours now, so the team's changes to them arrive as
           proposals; herdr-synapse canvas settings --human-edits live lets them
           work on the board directly (import with --team-can-edit for both)
  members  beta-worker.md and beta-reviewer.md are in that folder but were not
           adopted: read them in place; instructions --adopt reads only this team's own folder
nothing in beta was changed or deleted.
```

Two board records say what happened: `canvas_imported` to everyone, so the team
knows the board it is looking at is not its own work, and `knowledge_imported`
to you.

If `knowledge import` tells you a folder carries no rules or no facts, the folder
really holds none: Synapse writes over a file only when it holds the bytes this
team last wrote there, so a previous team's file is either still there or was
replaced by one of the commands above, including an operator's confirmation, and
its copy is in `<team>/inherited/`.

## `canvas import`

The inverse of `canvas export`, and the one way a drawing comes back.

```bash
herdr-synapse canvas import --from <path>              # a canvas.json, a team folder, an export
herdr-synapse canvas import --from-archive beta        # a team dissolved in this session
herdr-synapse canvas import --from-archive beta --list # what each archived copy holds
herdr-synapse canvas import --from <path> --dry-run    # every refusal, nothing written
herdr-synapse canvas import --from <path> --team-can-edit  # and let the team work on it
```

- The target canvas must be **empty**; an import is never a merge.
- Source element ids are kept, so every binding (an arrow's ends, a comment's
  element, a frame's children) survives; the counters are seeded so the next
  mark you draw collides with nothing.
- It is **one batch**: `canvas undo B-1` takes the whole board back.
- **Authorship is re-attributed to the importing operator by default.** Keeping
  original names retains their ownership semantics, so members' edits may become peer
  proposals. The operator can edit individual marks or undo the import batch with either choice.
  `--keep-authors` keeps the original names and is the operator's own call in person.
  Either way each mark records where it came from, which is what
  a `recall` hit and a `look` line can then say.
- **The team cannot edit the board it inherited until you say so.** This is the
  other side of re-attribution: the marks are the operator's, and the default
  `human_edits: propose` turns a member's edit, move or delete of them into a
  proposal — on a thirty-mark board a member tidying it is stopped at the
  twenty-proposal limit. `--team-can-edit` imports and sets `human_edits: live`
  in the same batch, so the team works on the board directly and you can still
  revert anything; `canvas settings --human-edits live` does it afterwards. It is
  refused together with `--keep-authors`, which asks for the opposite thing.
- A refusal names the repair: a board that is not empty, a scene this build
  cannot read, a mark whose data file is missing (`--skip-missing`), a kind this
  build does not know (`--skip-unknown`), an asset whose bytes do not match its
  name, and the over-cap pointer form, which names the export command instead.
- `--from` also takes a `whiteboard/archive/<stamp>/` folder, so a canvas lost
  to `whiteboard clear` is recoverable, and a copy under `<team>/inherited/`.

Nothing in the source is ever written to, and importing a canvas never brings a
dissolved team back to life.

## Searching an inherited board

`recall` indexes the canvas as a fifth kind, so a diagram is findable by what it
says — element text, frame and section titles, card bodies and badges, table
cells, chart captions, kanban and timeline items, comments and the legend:

```bash
herdr-synapse recall "login flow" --kind canvas
[canvas E-1 · frame] frame flow »Login« »flow« show the link shortener flow
  → herdr-synapse canvas look --around E-1
```

A hit on an inherited mark says which team it came from. The index is a cache in
the team's state directory: it rebuilds from the scene alone, and a cleared,
purged or re-imported board leaves nothing stale behind.

## What `dissolve` says now

Dissolving renames the whole team directory into the session archive; nothing is
deleted. The confirmation names what is going in there, and the result says what
is recoverable and how:

```
team beta archived to /…/_archive/beta-20261001T120000Z
recoverable from there:
  41 canvas marks (3 comments, 2 pictures)
  12 current facts
  the operator's rules (412 chars)
  287 board posts, 9 work items
inherit the rules, facts and canvas into another team:  herdr-synapse knowledge import --from beta
just the drawing:                     herdr-synapse canvas import --from-archive beta
nothing was deleted; a dissolved team is not restarted.
```

A team that drew nothing is not told about a canvas, and a count that cannot be
read omits its line rather than guess. The session archive has the board only
while that team was dissolved in *this* session — not on a fresh clone, after a
new session or on another machine. The project folder is what carries the record
there, and `knowledge import --from <folder>` reads it.

See also: [capabilities.md section 2a and 2b](capabilities.md), the command
contract in [cli.md](cli.md), and the canvas authorship model in
[collaboration.md](collaboration.md).
