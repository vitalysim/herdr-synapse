# Claude Code hook shim

The hooks implementer writes `herdr-synapse-hook.sh` here (plan section 9.4).
`herdr-synapse hooks install claude` copies it to `~/.claude/hooks/` with the
absolute CLI path baked in and registers three entries in `~/.claude/settings.json`.
