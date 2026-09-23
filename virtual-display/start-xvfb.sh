#!/usr/bin/env bash
# start-xvfb.sh: start a user-space virtual X display (Xvfb) for software-rendered Godot capture.
# launch_game.sh beside it runs it when the display is not up yet. To run it yourself, detach it:
#   setsid bash start-xvfb.sh :99 2560x1440x24 >/tmp/vgcp-xvfb99.log 2>&1 &
# It `exec`s into Xvfb (this script's process BECOMES the X server), so no wrapper shell is left for
# the X server's ready signal to disturb.
#
# Args: $1 = display (default :99)   $2 = WxHxDepth (default 2560x1440x24)
set -euo pipefail
DISP="${1:-:99}"
RES="${2:-2560x1440x24}"

XVFB_BIN="$(command -v Xvfb 2>/dev/null)" || {
  echo "start-xvfb.sh: Xvfb is not installed (Debian and Ubuntu: apt install xvfb)" >&2
  exit 1
}

# The X server, if SIGUSR1 is inherited as SIG_IGN, signals its parent "I'm ready", which kills a
# non-interactive wrapper shell. Reset SIGUSR1 to SIG_DFL in the X server process so it never sends
# that notification, then exec Xvfb. (-ac disables access control; -noreset keeps the server up
# after the last client disconnects; -nolisten tcp: local connections only.)
exec python3 -c 'import signal,os,sys
signal.signal(signal.SIGUSR1, signal.SIG_DFL)
os.execvp(sys.argv[1], sys.argv[1:])' \
  "$XVFB_BIN" "$DISP" -screen 0 "$RES" -nolisten tcp -noreset -ac
