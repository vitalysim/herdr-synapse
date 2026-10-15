# Let the leader recruit the team as needed

Synapse 0.23.0 adds an optional way to start a team: the human defines the task,
leader, project/knowledge location and staffing limits; the leader researches the
task and recruits the workers it actually needs. This adds to manual creation,
not replaces it. Existing teams are not enrolled or changed automatically.

## 1. Operator: inspect the native inventory

Use a running Herdr session and an up-to-date Synapse notifier. Run the setup as
the human in a Herdr shell, not by impersonating the operator from an agent.
Replace the path below with an existing trusted project:

```sh
project_dir=/absolute/path/to/your/project
herdr-synapse available codex --readiness --cwd "$project_dir"
```

The example below approves `codex:openai`. Use it only if the inventory reports
that provider; otherwise choose a reported `HARNESS:PROVIDER` route. You may
repeat `--allow-provider` for approved Claude Code, Codex or OpenCode routes.
The manager may use another of those three harnesses; the approved worker pool
does not have to match the manager's harness.

Discovery is metadata-only: it lists installed tools, native models and available
authentication/quota evidence, not a guarantee that every listed model can run.
Never copy credentials between tools. Worker turns use the native program's
account and may consume subscription quota or incur charges.

## 2. Operator: start only the leader

This example deliberately uses `--permissions native` instead of Synapse's
default YOLO launch flags. Native mode keeps each harness's own policy; it is not
an additional sandbox. Choose an external sandbox if the task needs isolation.

```sh
herdr-synapse create task-team --new --spawn lead:codex \
    --leader role:lead --project "$project_dir" --permissions native \
    --charter 'Add a CSV export to the existing reports page without changing existing report behavior.' \
    --brief 'lead=Research the task and knowledge first. Plan the work, choose needed roles and native models, and create same-team workers with staff add as needed within the approved pool. Assign clear Missions and acceptance evidence, verify results, and report completion or blockers. Do not wait for me to enumerate workers.' \
    --autonomous --allow-provider codex:openai --max-workers 4 \
    --acceptance 'CSV export works, existing reports are unchanged, tests pass, and review evidence is recorded.'
```

The leader's designation, Mission and staffing policy are saved before briefing.
The task is the charter; the required Mission explicitly tells the manager to
staff without waiting for you to name every worker. Four is a concurrency ceiling
for automatically recruited workers, not a desired team size. The manager and
manually added members do not consume those four slots.

The project/knowledge root is `--project`. This team gets
`$project_dir/.herdr-synapse/task-team/`, including `knowledge.md`, `members/`,
and `artifacts/`. Read existing project documentation too; the project location
does not automatically import every document. Unknown or edited documents are
held for the existing explicit operator review/confirmation, never overwritten
or silently adopted.

## 3. Manager: research, then recruit only what the task needs

These commands run from the verified manager's own agent shell. The CLI derives
identity and team from the real terminal; do not set environment variables to
pretend to be the human. Read the Mission, task, knowledge and existing work first:

```sh
herdr-synapse orient
herdr-synapse skill get manager
herdr-synapse staff status
herdr-synapse work list
```

Research the code and documentation before choosing roles. For this task the
manager might need an implementer now, and an independent reviewer once there is
a change to review. A smaller task may need no worker at all. Avoid speculative
workers, duplicate assignments and several workers editing the same files.

Inspect the approved harness's inventory in the project's actual directory:

```sh
project_dir=/absolute/path/to/your/project
herdr-synapse available codex --readiness --cwd "$project_dir"
```

Select a configured model ID reported there, on an approved provider, not a model
name copied from someone else's machine. Replace the value below before running:

```sh
worker_model=REPLACE_WITH_A_REPORTED_NATIVE_MODEL_ID
herdr-synapse staff add implementer codex --model "$worker_model" \
    --mission 'Implement CSV export in the existing reports page. Preserve existing report behavior; add regression tests and record changed files and test results.' \
    --acceptance 'Exported CSV has correct headers and escaping; existing and new tests pass with evidence.' \
    --request-id csv-export-implementer-1
```

The response identifies the member and assigned work item. `staff add` creates
the same-team worker, saves its Mission, assigns its work, then briefs it. If you
already planned an unowned work item, add `--work-id W-N` using its actual ID.
Use the reported member/work names, not guessed IDs. Optional `--profile` and
`--effort` values must be supported by the native program and approved context.

Keep the request ID and exact arguments. If the command times out or its result
is uncertain, repeat that same request; never invent a new ID to force another
launch. An ambiguous attempt retains its slot for inspection, not blind retry.

## 4. Manager: observe, verify and adapt

Use the existing board and work system, not a second scheduler:

```sh
herdr-synapse work next
herdr-synapse work list
herdr-synapse who
herdr-synapse board --new
```

Read each result and its evidence against acceptance before treating it as done.
Recruit an independent reviewer with another `staff add` only when useful; give
it a distinct review scope, explicit model and fresh request ID. Use work review
when the work item has assigned reviewers. After an owned worker is idle/done and
has no active work, `staff retire ACTUAL_MEMBER_NAME` queues its exit. This keeps
the pane, conversation, history and artifacts; the next add frees the slot only
after positive evidence that the exact worker stopped. `staff status` is read-only.

Revise the plan when evidence warrants it. One in-pool fallback is available for
a settled failed work attempt (`--work-id`, `--fallback-for`, `--reason`); quota
failures, more retries, changed provider/account context or outside-pool needs
require an operator decision. Do not launch recursive teams or use raw shell
commands to bypass staffing gates. Never edit the human's Rules or instructions.

Report what was delivered, the verification evidence and any remaining limits
to the human. Do not declare completion merely because workers stopped:

```sh
herdr-synapse post --to human --kind done \
    'CSV export is implemented and independently reviewed. Evidence is recorded on the completed work items; no outstanding acceptance items remain.'
```

That is an example completion message, not a claim about an actual run. If work
is blocked, post a truthful `--kind blocked` instead.

## Human controls and model-access limits

You still choose the team manually if you prefer, add members, select models and
profiles, change Missions, or use existing restore/resume/swap controls. You may
inspect the enrolled team or pause further automatic launches without stopping
running work:

```sh
herdr-synapse --team task-team staff status
herdr-synapse --team task-team staff pause
herdr-synapse --team task-team staff resume
herdr-synapse --team task-team staff configure --max-workers 2
```

Only the operator can undo an operator pause or configure the pool/worker limit.
The limit is not a spending, elapsed-time, total-start or native-subagent cap;
there is no default expiry. Staffing authority is same-team coordination, not
operator delegation or an OS security boundary.

Discovery never spends an inference turn. Only an operator-consented
`model-check ... --yes` does so. The current safe adapter supports Claude Code
2.1.299+ without custom profiles/project-scoped settings. Codex and OpenCode
checks explicitly return `verification_unsupported` without inference; their
inventory, ordinary model selection and worker launches remain available.
Listings/logins and a successful one-time check never guarantee future quota.

See the [CLI contract](cli.md#optional-autonomous-staffing) for all command
arguments, ownership fences and readiness semantics.
