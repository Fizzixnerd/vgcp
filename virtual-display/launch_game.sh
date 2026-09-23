#!/usr/bin/env bash
# launch_game.sh: launch a Godot game built with the server for the Video Game Control Protocol
# (VGCP), windowed, for autonomous control. A thin layer over start-xvfb.sh, run-game.sh and
# gfx-env.sh beside it: those own the display, GPU and isolation logic; this one adds the toolchain
# checks and the pid and VGCP endpoint reporting a control loop expects. It needs no git and no
# particular directory layout.
#
# Two lanes (docs/virtual-display.md has the detail):
#   DEFAULT: the isolated lane. Windowed on a private Xvfb display (:99), rendered in SOFTWARE (Mesa
#     lavapipe), walled off from any live session (WAYLAND_DISPLAY stripped, a private
#     XDG_RUNTIME_DIR, --display-driver x11), so nothing pops onto a desktop. A hardware Vulkan
#     driver cannot present to Xvfb (no DRI3), so this lane is software by design, and it is the
#     only lane that works on a machine with no GPU. Software rasterisation is CPU-bound.
#   A real session (opt-in): VGCP_DISPLAY=:0 renders on a live session through the local GPU. Fast,
#     but the window is VISIBLE on that desktop. It needs a usable render node, a hardware Vulkan
#     driver and a live display.
#
# Usage:
#   launch_game.sh [--virtual] [--port N] [--display :N] [--scene S] [--reclaim] [--] [godot args...]
#     --virtual    force the isolated Xvfb software lane (already the default unless VGCP_DISPLAY
#                  points at a real session)
#     --port N     the VGCP port to expose (exported as VGCP_ADDR and VGCP_PORT; default 38787)
#     --display :N the Xvfb display of the isolated lane (default :99)
#     --scene S    boot this scene (e.g. res://main.tscn) instead of the project's main scene
#     --reclaim    if a game already holds --port, kill it and launch a fresh one. The default is to
#                  fail fast: a held port is an error, since the holder may be a concurrent game.
#   Any other argument, and everything after `--`, is passed to Godot.
#
#   Parallel runs: give each game its OWN --port and --display, so they share no socket or
#   framebuffer (separate checkouts isolate files, not ports or displays). The game log is keyed by
#   port, /tmp/vgcp-game-<port>.log; the Xvfb log by display, /tmp/vgcp-xvfb<N>.log.
#
#   Arguments for the GAME (Godot's user arguments) go after a SECOND `--`: the first ends this
#   launcher's flags, the second is Godot's user-argument separator. The scene is inserted before
#   them, so both orders work:
#     launch_game.sh --port 39011 --display :125 -- -- --my-game-flag
#     launch_game.sh --port 39011 --display :125 --scene res://main.tscn -- -- --my-game-flag
#
# The Godot project is VGCP_GODOT_PROJECT, else the current directory if it holds project.godot,
# else its godot/ subdirectory; it is exported so run-game.sh boots the same one. The Godot binary is
# GODOT4_BIN (a path or a command name), else godot4 on PATH, else ~/.local/bin/godot4, else godot on
# PATH; it is exported, resolved, as GODOT4_BIN.
#
# On success it prints the endpoint and how to stop the game on stderr, and the game pid as the last
# line of stdout, then leaves Godot running. When it started the Xvfb, stderr also carries the line
# `[launch] XVFB_PID=<pid>`. On any error it stops what it started (the game, and that Xvfb) and
# exits 1. Screenshots need a real renderer: under --headless every capture fails.

set -uo pipefail

HERE="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
if [ -n "${VGCP_GODOT_PROJECT:-}" ]; then
  GODOT_PROJECT="$VGCP_GODOT_PROJECT"
elif [ -f "$PWD/project.godot" ]; then
  GODOT_PROJECT="$PWD"
else
  GODOT_PROJECT="$PWD/godot"
fi
export VGCP_GODOT_PROJECT="$GODOT_PROJECT"
PORT="${VGCP_PORT:-38787}"
XVFB_DISPLAY="${VGCP_XVFB_DISPLAY:-:99}"
FORCE_VIRTUAL=0
SCENE=""
RECLAIM=0
EXTRA=()
TAIL=()          # Godot arguments after the launcher's `--`; kept AFTER the scene positional

while [ $# -gt 0 ]; do
  case "$1" in
    --virtual) FORCE_VIRTUAL=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --display) XVFB_DISPLAY="$2"; shift 2 ;;
    --scene) SCENE="$2"; shift 2 ;;
    --reclaim) RECLAIM=1; shift ;;
    --) shift; TAIL=("$@"); break ;;
    *) EXTRA+=("$1"); shift ;;
  esac
done

# A scene to boot (e.g. res://main.tscn) is a Godot POSITIONAL and must come before any trailing
# `--` user arguments, or Godot takes the scene as a user argument and boots the project's main
# scene instead. The VGCP server spawns under the root window either way.
[ -n "$SCENE" ] && EXTRA+=("$SCENE")
EXTRA+=(${TAIL[@]+"${TAIL[@]}"})

# Per-run resource paths, so PARALLEL instances do not clobber each other: the game log is keyed by
# VGCP port, the Xvfb lock and log by display number. Logs stay in /tmp (not TMPDIR), so every
# shell names the same file.
DNUM="${XVFB_DISPLAY#:}"
GAME_LOG="/tmp/vgcp-game-${PORT}.log"
XVFB_LOG="/tmp/vgcp-xvfb${DNUM}.log"

# The Xvfb this launcher started, if any. Every error exit stops it (and only it: a reused display
# belongs to whoever started it), so a refused launch leaves no display behind.
XVFB_PID=""
note() { printf '[launch] %s\n' "$*" >&2; }
die()  {
  printf '[launch] ERROR: %s\n' "$*" >&2
  [ -n "$XVFB_PID" ] && kill "$XVFB_PID" 2>/dev/null
  exit 1
}

# The pid listening on a TCP port (own processes), or empty. Used to clear a stale game holding the
# VGCP port (a stale game holds the port, the new one cannot bind, and an agent would drive the
# stale one) and to verify the new game really owns the port.
port_holder_pid() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2; }

# --- 1. Toolchain checks -------------------------------------------------------------------
# The Godot binary: GODOT4_BIN when set (never a silent fallback past it), else the first found.
resolve_godot() {
  local c p
  if [ -n "${GODOT4_BIN:-}" ]; then
    set -- "$GODOT4_BIN"
  else
    set -- godot4 "$HOME/.local/bin/godot4" godot
  fi
  for c in "$@"; do
    p="$(command -v -- "$c" 2>/dev/null)" || continue
    case "$p" in
      /*) ;;
      */*) p="$PWD/$p" ;;
      *) continue ;;             # a shell builtin, function or alias, not a file
    esac
    printf '%s\n' "$p"
    return 0
  done
  return 1
}
if [ -n "${GODOT4_BIN:-}" ]; then
  GODOT_BIN="$(resolve_godot)" || die "GODOT4_BIN=$GODOT4_BIN is not an executable file or a command on PATH."
else
  GODOT_BIN="$(resolve_godot)" || die \
"no Godot binary: set GODOT4_BIN, or put godot4 or godot on PATH, or install ~/.local/bin/godot4
   (a standard, non-.NET Godot 4 build; see docs/virtual-display.md)."
fi
export GODOT4_BIN="$GODOT_BIN"

[ -f "$GODOT_PROJECT/project.godot" ] || die \
"no Godot project at $GODOT_PROJECT (set VGCP_GODOT_PROJECT, or run from the game's directory: the
   launcher uses the current directory when it holds project.godot, else its godot/ subdirectory)."

[ -x "$HERE/run-game.sh" ] || die "missing $HERE/run-game.sh (the display helpers belong beside this launcher)."

# --- 2. Choose a lane ----------------------------------------------------------------------
# Default to the isolated lane; render on a real session only when the caller points VGCP_DISPLAY
# at one (e.g. VGCP_DISPLAY=:0). This keys off VGCP_DISPLAY, NOT the ambient DISPLAY: an inherited
# DISPLAY=:0 must never silently render on someone's desktop.
USE_VIRTUAL=1
if [ -n "${VGCP_DISPLAY:-}" ] && [ "$FORCE_VIRTUAL" = "0" ]; then
  USE_VIRTUAL=0
fi

if [ "$USE_VIRTUAL" = "1" ]; then
  unset VGCP_DISPLAY                          # so gfx-env.sh takes the isolated software lane
  export VGCP_XVFB_DISPLAY="$XVFB_DISPLAY"    # the display that lane renders on
  command -v Xvfb >/dev/null 2>&1 || die \
"Xvfb is not installed (Debian and Ubuntu: the xvfb package; see docs/virtual-display.md)."
  if [ -e "/tmp/.X${DNUM}-lock" ]; then
    note "reusing Xvfb already on ${XVFB_DISPLAY}"
  else
    # 2560x1440: X clamps a warped pointer to the SCREEN, so a window larger than the screen reads a
    # scripted pointer past the edge as the edge (a 1600x900 window on a 1280x720 screen read x=1500
    # as 1279), and a replay at another window size diverges. The screen must be at least as large
    # as any --resolution a test asks for. A 1280x720 window is still centred on it, so a fresh
    # pointer still starts at the window's centre.
    note "starting Xvfb on ${XVFB_DISPLAY} (2560x1440x24)"
    bash "$HERE/start-xvfb.sh" "$XVFB_DISPLAY" 2560x1440x24 </dev/null >"$XVFB_LOG" 2>&1 &
    XVFB_PID=$!
    # A stable, machine-readable line, so a long-lived owner (e.g. the VGCP test harness) can reap
    # the Xvfb this launcher started, and only that one (a reused display prints no such line). It
    # is on stderr, so stdout's contract (the bare game pid on the last line) is untouched.
    printf '[launch] XVFB_PID=%s\n' "$XVFB_PID" >&2
    sleep 1
  fi
  note "lane: isolated Xvfb ${XVFB_DISPLAY}, SOFTWARE (lavapipe), Wayland stripped, --display-driver x11"
else
  note "lane: VGCP_DISPLAY=$VGCP_DISPLAY (the GPU on a real session, VISIBLE there)"
fi

# --- 3. Pre-flight: the VGCP port must be free ---------------------------------------------
# If the port is held, fail fast by default (never kill it): the holder may be a CONCURRENT game
# (parallel tests, another checkout on a colliding port), and killing it would corrupt that run.
# Only --reclaim kills a GAME on the port: a fresh game on a well-known port, for a single
# interactive lane. The holder counts as a game when its command line contains "godot" or the
# Godot binary's name. (A new game that cannot bind quits by itself either way: see step 4.)
HOLDER="$(port_holder_pid "$PORT")"
if [ -n "$HOLDER" ]; then
  CMD="$(tr '\0' ' ' < "/proc/$HOLDER/cmdline" 2>/dev/null || true)"
  IS_GAME=0
  if printf '%s' "$CMD" | grep -qi 'godot' || printf '%s' "$CMD" | grep -qF -- "$(basename "$GODOT_BIN")"; then
    IS_GAME=1
  fi
  if [ "$RECLAIM" = "1" ] && [ "$IS_GAME" = "1" ]; then
    note "--reclaim: port $PORT held by a game (pid $HOLDER); killing it for a fresh launch"
    # Kill the group if it is a setsid-led game (-$HOLDER is its group); else the pid alone.
    kill -9 -- "-$HOLDER" 2>/dev/null || kill -9 "$HOLDER" 2>/dev/null || true
    for _ in $(seq 1 20); do [ -z "$(port_holder_pid "$PORT")" ] && break; sleep 0.2; done
    [ -n "$(port_holder_pid "$PORT")" ] && die "port $PORT still held after killing pid $HOLDER"
  else
    SUFFIX=""
    [ "$IS_GAME" = "1" ] && SUFFIX=" (it looks like a Godot game: if it is stale, re-run with --reclaim, or find it with ss -ltnp 'sport = :$PORT' and kill that pid)"
    die "VGCP port $PORT already in use by pid $HOLDER${SUFFIX}. Each game needs its OWN --port: pick a free one, or stop that process."
  fi
fi

# --- 4. Launch through run-game.sh (it sources gfx-env.sh: isolation, renderer, VGCP) -------
export VGCP_ADDR="127.0.0.1:${PORT}"
export VGCP_PORT="$PORT"
export VGCP_HOST="${VGCP_HOST:-127.0.0.1}"
# Where a `screenshot` with no path lands. Resolved ONCE here and exported, so this script, a test
# harness and the server all name the same directory. A game started without this launcher gets the
# same value, because the server computes the same default itself: `${TMPDIR:-/tmp}` is what Rust's
# std::env::temp_dir() reads.
export VGCP_SHOTS_DIR="${VGCP_SHOTS_DIR:-${TMPDIR:-/tmp}/vgcp_tmp}"
mkdir -p "$VGCP_SHOTS_DIR" 2>/dev/null || die "cannot create VGCP_SHOTS_DIR=$VGCP_SHOTS_DIR"
note "VGCP endpoint: $VGCP_ADDR"
note "project: $GODOT_PROJECT"
note "shots dir: $VGCP_SHOTS_DIR"
note "launching: run-game.sh ${EXTRA[*]-}  (windowed; run-game.sh adds -w and the driver flags)"

# run-game.sh execs Godot, so $! is the game pid; its output goes to the game log.
# `setsid` starts the game as its OWN session and process-group LEADER (its group id is its pid),
# detached from this launcher's group. So a caller can `kill -- -$GAME_PID` to stop Godot and any
# helper children at once, and a stray signal to the launcher's group never reaches the game.
# setsid does not fork when the caller is not a group leader (true of a background job in a
# non-interactive script, where job control is off), so $! is the real game pid and group id.
setsid bash "$HERE/run-game.sh" "${EXTRA[@]}" </dev/null >"$GAME_LOG" 2>&1 &
GAME_PID=$!

# Wait for THIS game's control server to bind the port, not a fixed sleep. The VGCP server logs
#   "[VGCP] VGCP Server v<version> listening on <addr> (paused-by-default)"   when it has bound, or
#   "[VGCP] could NOT bind <addr>: <error>. ..."                               and then quits.
READY=0
for _ in $(seq 1 60); do                                   # up to about 12 s
  kill -0 "$GAME_PID" 2>/dev/null || break                 # the game exited (e.g. it could not bind)
  grep -q "listening on .*:${PORT}\b" "$GAME_LOG" 2>/dev/null && { READY=1; break; }
  grep -q "could NOT bind" "$GAME_LOG" 2>/dev/null && break
  sleep 0.2
done
if [ "$READY" != "1" ]; then
  # Kill the game's group (it is a setsid leader, so -$GAME_PID is its group); else the pid alone.
  # die() then stops the Xvfb this launcher started.
  kill -9 -- "-$GAME_PID" 2>/dev/null || kill -9 "$GAME_PID" 2>/dev/null || true
  if grep -q "could NOT bind" "$GAME_LOG" 2>/dev/null; then
    die "the control server could not bind $VGCP_ADDR (port in use). See $GAME_LOG."
  fi
  die "the control server did not come up on $VGCP_ADDR: check $GAME_LOG (a project never imported, or a game built without the VGCP server, has no control server)."
fi
# Defence in depth: confirm the launched game owns the port, not some other process.
OWNER="$(port_holder_pid "$PORT")"
[ -n "$OWNER" ] && [ "$OWNER" != "$GAME_PID" ] && \
  note "WARNING: port $PORT owned by pid $OWNER, not the launched game $GAME_PID"

CONTROL="$(cd "$HERE/.." && pwd)/vgcp-mcp/control.py"
cat >&2 <<EOF
[launch] game running.
[launch]   game PID : $GAME_PID
[launch]   VGCP     : $VGCP_ADDR
[launch]   Xvfb PID : ${XVFB_PID:-<reused / real session>}
[launch]   log      : $GAME_LOG
[launch] Drive it:  VGCP_PORT=$PORT python3 $CONTROL ping
[launch] Stop it:   kill -- -$GAME_PID${XVFB_PID:+ $XVFB_PID}   # -$GAME_PID is the game's whole process group
EOF
echo "$GAME_PID"
