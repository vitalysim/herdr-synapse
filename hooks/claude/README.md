# Claude Code hook shim

The hooks implementer writes `herdr-team-hook.sh` here (plan section 9.4).
`herdr-team hooks install claude` copies it to `~/.claude/hooks/` with the
absolute CLI path baked in and registers three entries in `~/.claude/settings.json`.
