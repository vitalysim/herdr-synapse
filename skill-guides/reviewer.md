# herdr-synapse guide: reviewer

Work items can name you, or your role, as their reviewer. When the owner
settles one successfully it waits for you (`in_review`) and you receive a
request to review it. Everything in the member guide applies to you too.

## How to review

1. `herdr-synapse work show W-4` — read the brief, especially Acceptance.
2. Open each deliverable and check each piece of evidence against Acceptance.
   Verify rather than trust: a claim with no evidence is not evidence.
3. Decide:
   - `herdr-synapse work review W-4 --approve [--note "<remark>"]`
   - `herdr-synapse work review W-4 --changes "<exactly what to fix>"`

Rules:

- Review the work against its brief, not against what you would have done.
- Changes must be specific and checkable ("the second headline breaks the
  voice guide: no exclamation marks"), never "improve it".
- You never review your own work, and you never make the fixes yourself
  unless you were asked to: send it back.
- What you learned while reviewing that others should know becomes a fact
  (`herdr-synapse fact add ... --source ...`).

`herdr-synapse work next` lists the items waiting for you.
