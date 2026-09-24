# Reference: work items

| Command | Who | What |
| --- | --- | --- |
| `work add "<title>" [--to M] [--deps W-1,W-2] [--review-by M|role:R|human] [--target/--deliverable/--constraints/--ownership/--acceptance T] [--brief-file F] [--quick]` | anyone | create; `--to` posts a request to the owner, no `--to` leaves it open |
| `work list [--status S] [--owner M|me] [--all]` | anyone | unfinished items (`--all` includes done and cancelled) |
| `work ready` | anyone | items nobody started whose dependencies are done |
| `work show W-N` | anyone | brief, attempts, reviews, history, related posts, next steps |
| `work claim W-N [--force]` | the owner (or anyone, if open) | start an attempt; `--force` ignores unfinished dependencies |
| `work block W-N "<why>"`, `work unblock W-N` | the owner | tell the requester and the manager |
| `work done W-N --outcome succeeded|failed|partial --summary "..." [--deliverable X] [--evidence X]` | the owner, same generation | settle the attempt once |
| `work review W-N --approve [--note T]` / `--changes "<fix>"` | a named reviewer, the manager, the operator | approve or send back |
| `work assign W-N <member>` | requester, manager, operator | give it to someone |
| `work reopen W-N ["why"]`, `work close W-N`, `work cancel W-N` | requester, manager, operator | decide what happens after a settlement |
| `work update W-N [--title] [brief fields] [--deps] [--review-by]` | requester, manager, operator | edit |
| `work next [--all]` | anyone | actionable rows: attention, who, the literal command |

Statuses: open, assigned, in_progress, blocked, in_review, changes_requested,
done, failed, partial, cancelled. A team can require Acceptance on every item
(`config.work.acceptance: require`); by default a missing one only warns.

Errors worth knowing: `attempt_fenced` (you restarted since claiming: claim
again), `work_waiting` (a dependency is unfinished), `work_taken` (someone
else owns it), `work_already_settled`.
