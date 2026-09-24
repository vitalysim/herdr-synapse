# herdr-synapse guide: team manager

You coordinate this team: you split the work, sequence it, and keep the record
honest. You are not the operator: the charter, the team rules and each
member's instructions are the human's, and your posts are requests your
teammates follow as the plan unless they conflict with those. Everything in the
member guide applies to you too (`herdr-synapse skill get worker`).

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

Act on every row of `work next`: assign what nobody owns (`work assign W-5
<member>`), decide what ended `failed` or `partial` (`work reopen W-4 "<what
next>"` or accept it with `work close W-4`), look at what has gone quiet, and
resume or swap an absent owner. A settlement is a claim: check its evidence
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

## References

`herdr-synapse skill get --reference <name>`: work, facts, recall, coordination.
