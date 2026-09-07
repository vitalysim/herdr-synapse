#!/bin/sh
# Wrapper for the console plugin pane.
#
# On macOS a fresh plugin pane closes when its process exits, so a console
# crash would take the pane with it and hide the error. Run the console; on a
# non-zero exit print the relaunch hint and wait for a key before the pane
# closes.

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 1

"$root/bin/herdr-synapse" console "$@"
rc=$?

if [ "$rc" -ne 0 ]; then
    printf '\nherdr-synapse console exited with status %s.\n' "$rc"
    printf 'Relaunch with: herdr-synapse ui console   (or: herdr plugin action invoke herdr-synapse.console)\n'
    printf 'Logs: herdr-synapse doctor; herdr plugin log list --plugin herdr-synapse\n'
    printf 'Press any key to close this pane.\n'
    if [ -t 0 ]; then
        saved=$(stty -g 2>/dev/null)
        stty -icanon -echo min 1 time 0 2>/dev/null
        dd bs=1 count=1 >/dev/null 2>&1
        [ -n "$saved" ] && stty "$saved" 2>/dev/null
    else
        read -r _ignored
    fi
fi

exit "$rc"
