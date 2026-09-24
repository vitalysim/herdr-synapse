# Feature team

Ship one reviewed change: a lead who splits the work, an implementer, and a
reviewer who approves each piece against its acceptance criteria.

## Charter

Ship the change described in the brief without expanding its scope: each
piece is a work item with acceptance criteria, implemented, tested, and
approved by the reviewer.

## Rules

- Stay inside the assigned scope; propose anything else on the board.
- Every piece of work names its acceptance evidence (tests, behaviour, output).
- Decisions worth keeping are recorded as facts about the component.

## Settings

- manager: lead
- contradictions: observe
- acceptance: warn
- review_by: role:reviewer

## Roles

- lead: claude — splits and sequences the work, owns the final recommendation
- implementer: codex — implements the assigned pieces with tests
- reviewer: opencode — reviews each piece for behaviour, edge cases and regressions

## Vocabulary

- Component: a part of the system
- Decision: a design choice and why it was made
- Bug: a defect found along the way
