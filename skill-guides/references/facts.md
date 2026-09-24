# Reference: facts and disputes

| Command | What |
| --- | --- |
| `fact add "<statement>" [--about S] [--attribute A] [--type T] [--source URL[@DATE]] [--post SEQ] [--ref PATH] [--valid-from D] [--valid-to D] [--supersedes F-N]` | record a fact with provenance |
| `fact support F-N [--source ...]` | stand behind another member's fact |
| `fact retire F-N ["why"] [--valid-to D]` | your fact is no longer true (the manager or operator may retire any) |
| `fact show F-N` | provenance, confidence, validity, disputes |
| `facts [--about S] [--by M] [--history] [--as-of D] [--disputed] [--all]` | list |
| `fact disputes [--all]` | open (or all) disputes |
| `fact resolve D-N --keep F-N | --keep-both | --retire-all [--reason T]` | the manager (not a party) or the operator settles |
| `contradictions [off|observe|debate|escalate] [--timeout 30m]` | the operator's switch |

Time: `valid_from`/`valid_to` say when it was true in the world;
`recorded_at`/`retired_at` say when the team believed it. Nothing is deleted.

Confidence is the number of distinct members behind a fact and its external
sources. The same words from another member become support, not a duplicate;
near-identical wording is recorded with a warning.

A dispute is two members giving different statements for the same `--about`
and `--attribute`. Modes: `off` (nothing), `observe` (default: recorded, only
the operator sees it), `debate` (both authors are told and settle it; after the
timeout it goes to the manager or the operator), `escalate` (the manager or the
operator decides at once). No mode ever limits posting. Retiring your own fact
in a dispute concedes it.

`knowledge add "<text>"` is a fact with no subject; `knowledge` shows the
operator's rules and the current facts.
