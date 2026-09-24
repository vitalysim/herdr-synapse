# Security hunt

Hunt for vulnerabilities in an authorised target: a lead who scopes and
triages, a hunter who investigates, and a validator who reproduces each
finding before it counts.

## Charter

Find and validate security issues in the authorised target named in the
brief. A finding counts only when the validator has reproduced it and the
report states impact, preconditions and a minimal proof of concept.

## Rules

- Stay inside the authorised scope; anything outside it goes to the operator.
- Never test against production systems or third parties.
- Each suspected issue is a work item; each confirmed fact about the target is a fact with its evidence.
- Proofs of concept live in artifacts/ and are minimal.

## Settings

- manager: lead
- contradictions: debate
- acceptance: require
- review_by: role:validator

## Roles

- lead: claude — scope, triage, and the final report
- hunter: codex — investigates and writes proofs of concept
- validator: opencode — reproduces findings independently

## Vocabulary

- Target: a system or component in scope
- Finding: a suspected or confirmed security issue
- ProofOfConcept: the minimal reproduction of a finding
- Component: a part of the target
