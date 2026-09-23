#!/usr/bin/env bash
# run-game.sh: run a Godot project WINDOWED on the virtual display (Xvfb), so a screenshot over the
# Video Game Control Protocol (VGCP) captures a real rendered frame. It sources gfx-env.sh beside
# it, which picks the lane: by default the isolated Xvfb lane, rendered in SOFTWARE (Mesa lavapipe;
# gfx-env.sh sets VGCP_SOFTWARE=1 there, since a hardware Vulkan driver cannot present to Xvfb);
# VGCP_DISPLAY=:0 renders on a real session instead.
# launch_game.sh beside it is the usual way in: it starts the Xvfb, checks the port and reports the
# pid. To run this directly, start the Xvfb first (start-xvfb.sh), then run it detached, e.g.:
#   setsid bash run-game.sh </dev/null >/tmp/game.log 2>&1 &
#
# The project is VGCP_GODOT_PROJECT, else the current directory if it holds project.godot, else its
# godot/ subdirectory (the same rule as launch_game.sh, which exports the one it checked). The Godot
# binary is GODOT4_BIN, else godot4 on PATH (launch_game.sh exports the one it resolved).
# Arguments are passed through to Godot (e.g. --resolution 1280x720, --max-fps 0, --verbose).
#
# NOT --headless: its dummy renderer draws no frame, so every capture fails. A real Vulkan frame is
# what the screenshot path reads (get_viewport().get_texture().get_image()); on Xvfb it is rendered
# in software, which presents there fine.
set -euo pipefail
HERE="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
if [ -n "${VGCP_GODOT_PROJECT:-}" ]; then
  GODOT_PROJECT="$VGCP_GODOT_PROJECT"
elif [ -f "$PWD/project.godot" ]; then
  GODOT_PROJECT="$PWD"
else
  GODOT_PROJECT="$PWD/godot"
fi

# shellcheck source=/dev/null
. "$HERE/gfx-env.sh"

exec "${GODOT4_BIN:-godot4}" --path "$GODOT_PROJECT" ${VGCP_DRIVER_FLAGS:-} -w "$@"
