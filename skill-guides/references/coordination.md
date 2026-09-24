# Reference: coordination

- Directed posts wake their recipient when it is idle; a broadcast from a peer
  waits for the next read unless `--urgent`. The manager's and the operator's
  broadcasts wake everyone.
- `post --kind question|blocked --to human` opens the operator's popup and
  blocks until they answer; `--no-wait` opts out. Only the operator closes an
  ask; a teammate's reply is a note on the thread.
- `post --interrupt --to <name>` types into a teammate's running turn when the
  team allows it for that agent kind; one per teammate per cooldown.
- `post --to team:<other>` is for the manager of a linked team.
- `herdr-synapse who` shows states, headlines, work and the manager;
  `herdr-synapse context` shows context windows.
- Never prompt, read or type into a teammate's pane; only the notifier does.
