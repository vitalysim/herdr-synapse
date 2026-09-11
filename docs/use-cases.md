# Herdr Synapse use cases

These recipes show practical team shapes, not fixed opinions about which harness is best at a role. They deliberately mix Claude Code, Codex and OpenCode—the three harnesses whose full Synapse workflow is live-verified. Swap their roles whenever your models, tools or project make another mix more useful.

Model names are intentionally absent from the recipes because they belong to your installed harness configuration and change over time. Synapse uses the harness default unless you add `--model <role-or-kind>=<model>[@<effort>]` to `create`, or change a member later with `herdr-synapse model`.

## Before any recipe

Start Herdr, enter the project directory, and prepare this Herdr session:

```bash
PROJECT_DIR="$(pwd -P)"

herdr-synapse daemon start
herdr-synapse skill install
herdr-synapse kinds trust claude
herdr-synapse kinds trust codex
herdr-synapse kinds trust opencode

# Optional, Claude Code only: board context on session start, prompt and Stop.
herdr-synapse hooks install claude
```

The commands below start fresh panes. To organize agents that are already running, use `prefix+t`, select them with Space, press Enter, and follow the same charter, optional team rules, folder, role, required Mission / brief and optional model pattern in the wizard; appoint or link managers from the resulting team row.

## 1. Development team with a team lead

Use this when one change needs planning, implementation and an independent review loop.

| Member | Harness | Responsibility |
| --- | --- | --- |
| `product-dev-lead` | Claude Code | Split the work, coordinate handoffs and own the final recommendation. |
| `product-dev-implementer` | Codex | Implement the bounded change and report touched files and tests. |
| `product-dev-reviewer` | OpenCode | Review behavior, edge cases and regressions independently. |

```bash
herdr-synapse create product-dev --new --use \
  --project "$PROJECT_DIR" \
  --charter "Ship one reviewed and tested change without expanding the requested scope." \
  --rules "Stay inside assigned scope. Post evidence and handoffs on the board. Do not declare completion without tests." \
  --spawn "lead:claude:$PROJECT_DIR" \
  --spawn "implementer:codex:$PROJECT_DIR" \
  --spawn "reviewer:opencode:$PROJECT_DIR" \
  --brief product-dev-lead="Turn the charter into a plan, assign work, resolve overlaps and summarize the release decision." \
  --brief product-dev-implementer="Implement the assigned slice, run focused tests and post a precise handoff." \
  --brief product-dev-reviewer="Review the implementation independently; look for regressions, missing tests and scope drift." \
  --manager product-dev-lead
```

Give the lead the initial outcome, not every implementation detail:

```bash
herdr-synapse --team product-dev post \
  --to product-dev-lead --kind request \
  "Break the charter into assignments, coordinate the team, and return a ship or no-ship recommendation with evidence."
```

The manager designation makes the lead the team's coordinator; it does not make that agent the human operator. If the lead genuinely needs to edit the charter, rules, roster or member instructions, lend that authority explicitly and revoke it afterward:

```bash
herdr-synapse --team product-dev operator grant product-dev-lead \
  --ttl 2h --note "temporary authority for this development cycle"

herdr-synapse --team product-dev operator revoke product-dev-lead
```

Follow the work with `prefix+u`, or from a shell:

```bash
herdr-synapse --team product-dev who --brief
herdr-synapse --team product-dev board --new
herdr-synapse --team product-dev context
```

## 2. Defensive vulnerability-hunting team

Use this for an authorized review where discovery, code analysis and triage must remain independent. The charter and rules should name the allowed target and prohibit destructive validation.

| Member | Harness | Responsibility |
| --- | --- | --- |
| `vuln-hunt-triage` | Codex | Manager; deduplicate findings, demand reproductions and rank impact. |
| `vuln-hunt-code-audit` | Claude Code | Trace trust boundaries, dangerous inputs and security-sensitive flows. |
| `vuln-hunt-recon` | OpenCode | Map dependencies, exposed surfaces and existing mitigations. |

```bash
herdr-synapse create vuln-hunt --new \
  --project "$PROJECT_DIR" \
  --charter "Find reproducible security defects in this repository and produce a prioritized defensive report." \
  --rules "Authorized repository only. No production access, persistence, credential use, destructive payloads or unsupported impact claims. Redact secrets." \
  --spawn "triage:codex:$PROJECT_DIR" \
  --spawn "code-audit:claude:$PROJECT_DIR" \
  --spawn "recon:opencode:$PROJECT_DIR" \
  --brief vuln-hunt-triage="Coordinate scope, challenge evidence, merge duplicates and maintain the prioritized finding list." \
  --brief vuln-hunt-code-audit="Inspect source trust boundaries and validation paths; report file-level evidence and a safe reproduction." \
  --brief vuln-hunt-recon="Inventory attack surface, dependencies and mitigations; separate confirmed facts from hypotheses." \
  --manager vuln-hunt-triage
```

Start the lanes explicitly so each member knows what it owns:

```bash
herdr-synapse --team vuln-hunt post \
  --to vuln-hunt-code-audit --kind request \
  "Audit input-to-sink paths and report only findings with file and line evidence."

herdr-synapse --team vuln-hunt post \
  --to vuln-hunt-recon --kind request \
  "Map externally reachable surfaces, security-sensitive dependencies and current mitigations."

herdr-synapse --team vuln-hunt post \
  --to vuln-hunt-triage --kind request \
  "Set severity criteria, challenge every reproduction and maintain one deduplicated queue."
```

Members can preserve concise, attributed evidence with `knowledge add`; the manager should use board replies for challenges so the reasoning remains threaded. Export the final audit trail without losing rotated board segments:

```bash
herdr-synapse --team vuln-hunt export vuln-hunt-report.md --format md
```

## 3. Research and decision team

Use this when the result must distinguish sources, experiments, inference and recommendation rather than merely produce a long summary.

| Member | Harness | Responsibility |
| --- | --- | --- |
| `research-lab-coordinator` | OpenCode | Manager; frame questions, merge evidence and expose uncertainty. |
| `research-lab-source-review` | Claude Code | Collect and assess primary sources supplied or available to the agent. |
| `research-lab-verifier` | Codex | Reproduce technical claims against code, data or a small experiment. |

```bash
herdr-synapse create research-lab --new \
  --project "$PROJECT_DIR" \
  --charter "Answer the research question with traceable evidence, explicit uncertainty and an actionable recommendation." \
  --rules "Prefer primary evidence. Label inference. Record contradictions. Never invent a citation or claim a test that was not run." \
  --spawn "coordinator:opencode:$PROJECT_DIR" \
  --spawn "source-review:claude:$PROJECT_DIR" \
  --spawn "verifier:codex:$PROJECT_DIR" \
  --brief research-lab-coordinator="Decompose the question, assign evidence gaps, reconcile contradictions and write the decision memo." \
  --brief research-lab-source-review="Find and assess the strongest available sources; quote sparingly and preserve references." \
  --brief research-lab-verifier="Test technical claims against the repository or reproducible experiments and report exact methods." \
  --manager research-lab-coordinator
```

Post the question and acceptance criteria to the coordinator. Attach a local brief with `--file research-brief.md` when one exists.

```bash
herdr-synapse --team research-lab post \
  --to research-lab-coordinator --kind request \
  "Produce a decision memo: answer, supporting evidence, contradictory evidence, confidence, risks and next experiment."
```

Synapse launches supported Claude Code, Codex and OpenCode members without approval prompts by default, but it cannot grant tools, credentials or access the harness does not already have. Use a trusted repository or external sandbox and configure browsing and secrets according to the research scope.

## 4. Independent review swarm

Not every team needs a manager. For a high-risk change, three independent reviewers can inspect the same diff through different lenses while the human owns synthesis.

```bash
herdr-synapse create review-swarm --new \
  --project "$PROJECT_DIR" \
  --charter "Review the current change independently and report actionable findings with evidence." \
  --rules "Do not modify files. Do not copy another reviewer's conclusion. Mark no-finding reviews explicitly." \
  --spawn "correctness:codex:$PROJECT_DIR" \
  --spawn "security:claude:$PROJECT_DIR" \
  --spawn "operability:opencode:$PROJECT_DIR" \
  --brief review-swarm-correctness="Check logic, invariants, error paths and regression coverage." \
  --brief review-swarm-security="Check trust boundaries, unsafe inputs, secret handling and privilege changes." \
  --brief review-swarm-operability="Check user workflow, diagnostics, recovery, documentation and maintenance cost."

herdr-synapse --team review-swarm post --kind request \
  "Review your assigned lens independently. Post findings with severity and evidence, then post done."
```

A human broadcast queues every member for an idle-gated nudge. Use separate named posts instead when reviewers need different source material.

## 5. Link specialist teams into an organization

Keep implementation and security on separate boards, then link their managers for bounded cross-team requests:

```bash
herdr-synapse link product-dev vuln-hunt \
  --note "security review and remediation handoffs"

herdr-synapse --team product-dev post \
  --to team:vuln-hunt --kind request \
  "Please review the authentication change and return prioritized findings with reproductions."
```

The request is mirrored on both boards, addressed to the receiving manager, and returns a read receipt. Replies stay threaded across the link. In the team console, `/filter teams` shows only cross-team traffic; in `prefix+t`, press `v` for the ASCII organization topology and `c` to manage links.

Use links for team-level consultation, not as a way to merge every board into one noisy feed. Both teams need a manager, and links stay inside one Herdr session.

## More team shapes

| Goal | Suggested roles | Useful Synapse feature |
| --- | --- | --- |
| Release readiness | release lead, test runner, documentation reviewer | Manager handoffs, blocking operator asks, board export. |
| Large refactor | architecture owner, migration implementer, invariant reviewer | Per-member instructions, context gauges, compact and re-brief. |
| Incident diagnosis | incident lead, evidence collector, reproducer | Urgent named posts, attachments, threaded findings. |
| Dependency upgrade | upgrade implementer, compatibility tester, changelog reviewer | Separate briefs, exact session resume, independent review. |
| Competing prototypes | coordinator plus one implementer per approach | Isolated roles, artifacts folder, explicit comparison criteria. |

The harness mix is a choice, not a requirement. The durable part is the team contract: one charter, non-overlapping briefs, a named manager only when coordination benefits from one, and evidence posted to the shared board.
