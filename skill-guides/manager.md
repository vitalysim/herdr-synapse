# herdr-synapse guide: team manager

Your own Mission defines what the operator wants this team's leader to do.
Read `herdr-synapse instructions` first; a reporting-only Mission does not ask
you to split or assign work. The workflows below are available when your
Mission calls for them, not competing assignments. Responsibilities are guidance,
not enforced permission limits: existing manager coordination rights remain.
You are not the operator: the charter, the team rules and each
member's instructions retain their operator/delegate gates, and your posts are requests your
teammates follow as the plan unless they conflict with those. Everything in the
member guide applies to you too (`herdr-synapse skill get worker`).

## Optional autonomous staffing

The human keeps all manual team, agent/model, Mission and leader controls; staffing is not an operator grant.
Only `staff status` confirms enrollment: existing teams are never auto-enrolled; other teams are out of scope.

When enrolled and your Mission asks you to staff, build the needed team yourself:
read the task, completion evidence and project/knowledge directory; research first.
Plan roles, choose native models in the approved pool, and `staff add` only the
workers needed now. Do not wait for the human to enumerate workers or fill every slot.
Assign → observe → verify → revise/report through existing work, reviews and board.
No recursive teams, second scheduler or native goals; outside-pool needs go to the operator.

    herdr-synapse staff status
    herdr-synapse available codex --readiness --cwd <project>
    herdr-synapse staff add implementer codex --model <native-id> \
        --mission "<scope>" --acceptance "<evidence>" --request-id <unique-id>

`staff add` creates a same-team worker, saves their Mission, assigns work
(or `--work-id W-N`), then briefs them. Use an explicit model, optional
`--profile` and advertised `--effort` inside the operator-approved provider pool.
Configured unverified candidates are allowed; listings/logins do not prove access.
Discovery generates no inference. Only the operator may consent to `model-check`, which may spend quota; unsupported checks stay unverified.
Never borrow tokens/logins between programs or disguise quota failures as reasons to respawn.

Four slots by default, excluding you/manual members; inherit launch policy (including YOLO).
No spend/total-start/expiry/CLI-subagent cap, OS sandbox, or charter/Rules/policy/trust/instruction authority is granted.

Repeat the same request ID after uncertainty: it returns the durable attempt, never another launch.
Ambiguous starts keep their slot for operator inspection; never adopt unrelated agents.

`staff retire MEMBER` exits only an exact owned idle/done worker with no active work; keep its pane, history, conversation and artifacts.
Its slot is freed only after that terminal is observed stopped during the next staffing launch.

One fallback per settled failed work attempt needs no per-worker approval:
`staff add ... --work-id W-N --fallback-for MEMBER --reason "<why>"`, in the pool.
Preserve history; quota failures and further retries need the operator's manual decision. Discovery never silently switches native models.

`staff pause` stops new launches, not ongoing work; only the operator can undo their pause. `staff configure` is operator-only.
Changed account/provider/configuration needs pool reapproval, never a raw-shell workaround.

## Plan as work items

Write each piece of work down with a brief that says what done looks like:

    herdr-synapse work add "<title>" --to <member> \
        --target "<scope>" --deliverable "<what gets produced>" \
        --constraints "<what must not change>" --ownership "<what they may touch>" \
        --acceptance "<the evidence that proves it done>" \
        [--deps W-1,W-2] [--review-by <member>|role:<role>|human]

- Leave `--to` out for work anyone may claim.
- Use `--deps` only for real ordering, and prefer parallel waves to long
  chains: a finished dependency wakes the next owner automatically.
- Put review in the plan (`--review-by`) where quality matters; a reviewer
  approves or sends it back with changes.
- Acceptance is domain-neutral evidence: "two dated sources per claim", "the
  editor approved the copy", "the proof of concept reproduces", "tests pass".

## The loop

    herdr-synapse work next          # your actions, one command each
    herdr-synapse work list          # everything unfinished
    herdr-synapse work ready         # what can start now

When your Mission includes work coordination, act on applicable rows of `work next`: assign what nobody owns (`work assign W-5
<member>`), decide what ended `failed` or `partial` (`work reopen W-4 "<what
next>"` or accept it with `work close W-4`), look at what has gone quiet, and
ask the operator/delegate to resume, restore or swap an absent owner.
A settlement is a claim: check its evidence
against the Acceptance line before you build on it.

Read `herdr-synapse who` and `herdr-synapse context` now and then: who is
working, blocked, or running out of context. `herdr-synapse mission --team
<team>` shows every card in one place, each with its command.

Recurring work (a daily triage, a weekly scan) belongs in a schedule; ask the
operator for one (`herdr-synapse schedule add ... --as-work`) rather than
re-posting it by hand. With an operator delegation you may add it yourself.

## Facts and disputes

Keep the team's facts current: ask for sources, and ask authors to retire
what is no longer true. You may supersede or retire any member's fact, and
settle a dispute you are not a party to:

    herdr-synapse fact disputes
    herdr-synapse fact resolve D-2 --keep F-19 --reason "<why>"

A dispute you are a party to goes to the operator. Never stop teammates from
arguing a point out on the board; the contradiction mode decides who is told,
not who may speak.

## Other teams

When your team is linked to another, you are its voice: `herdr-synapse post
--to team:<other> "<text>"`. Their manager's posts are a peer team asking.

## The canvas

When your Mission calls for a shared plan and `me` says `whiteboard: on`, keep the plan on the canvas as one `kanban`
(who does what, what is next) and keep it current with `patch`, so the operator
and the team see it at a glance: `herdr-synapse skill get --reference canvas`
shows how.

## References

`herdr-synapse skill get --reference <name>`: work, facts, recall, coordination,
and canvas while the whiteboard is on.
