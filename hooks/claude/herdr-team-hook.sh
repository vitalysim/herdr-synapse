#!/bin/sh
# herdr-team Claude Code hook shim (plan 9.4).
# HERDR_TEAM_HOOK_VERSION=1
#
# Installed by `herdr-team hooks install claude` as ~/.claude/hooks/herdr-team-hook.sh
# with the absolute CLI path baked in below. Registered three times in
# ~/.claude/settings.json: SessionStart, UserPromptSubmit, Stop.
#
# Rules: never `set -e`; every CLI call ends in `|| true`; the only exit 2
# is on the explicit stop branch (a UserPromptSubmit exit 2 would erase the
# human's prompt). Anything unexpected exits 0 so a broken team setup can
# never break the agent. All JSON parsing happens in the Python CLI
# (`herdr-team hook-input <action>`), which reads the hook's stdin and
# prints the context (session-start, prompt-submit) or, on stop, exits 7
# to ask for a block. HERDR_TEAM_HOOKS=off (launch-time env) disables
# everything; `herdr-team mute` is the live switch.

HERDR_TEAM_CLI='@@HERDR_TEAM_CLI@@'
action=${1:-}

[ "${HERDR_ENV:-}" = 1 ] || exit 0
[ -n "${HERDR_PANE_ID:-}" ] || exit 0
[ -n "${HERDR_SOCKET_PATH:-}" ] || exit 0
[ "${HERDR_TEAM_HOOKS:-}" != off ] || exit 0
# Cursor's agent also speaks the Claude hook protocol; the team hooks are Claude-only.
[ -z "${CURSOR_VERSION:-}" ] || exit 0

case "$action" in
    session-start|prompt-submit|stop) ;;
    *) exit 0 ;;
esac

# Log only to a per-user location: HERDR_TEAM_HOOK_LOG, else the plugin state dir
# (0700, created by the plugin) when it exists, else nowhere. Never a fixed name
# in a shared temp dir, which another local user could pre-create as a symlink.
log=${HERDR_TEAM_HOOK_LOG:-}
if [ -z "$log" ]; then
    state=${HERDR_TEAM_STATE_DIR:-${XDG_STATE_HOME:-${HOME:-/nonexistent}/.local/state}/herdr/plugins/herdr-team}
    if [ -d "$state" ] && [ ! -L "$state/hooks-claude.log" ]; then
        log=$state/hooks-claude.log
    else
        log=/dev/null
    fi
fi
if [ "$log" != /dev/null ] && [ -L "$log" ]; then
    log=/dev/null
fi
if ! { : >>"$log"; } 2>/dev/null; then
    log=/dev/null
fi

cli=$HERDR_TEAM_CLI
if [ ! -x "$cli" ]; then
    cli=$(command -v herdr-team 2>/dev/null) || cli=""
fi
if [ -z "$cli" ]; then
    printf '%s herdr-team CLI not found (%s); hook %s skipped\n' "$(date '+%Y-%m-%dT%H:%M:%S' 2>/dev/null)" "$HERDR_TEAM_CLI" "$action" >>"$log" 2>/dev/null || true
    exit 0
fi

case "$action" in
    session-start|prompt-submit)
        # Stdout goes to Claude as context; nothing here may exit 2.
        "$cli" hook-input "$action" 2>>"$log" || true
        exit 0
        ;;
    stop)
        message=$("$cli" hook-input stop 2>>"$log")
        rc=$?
        if [ "$rc" -eq 7 ] && [ -n "$message" ]; then
            printf '%s\n' "$message" >&2
            exit 2
        fi
        exit 0
        ;;
esac
exit 0
