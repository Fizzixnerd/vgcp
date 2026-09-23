#!/usr/bin/env bash
# gfx-env.sh: the environment for running Godot (Forward+ or Mobile, on Vulkan) on the virtual
# display, for a game served over the Video Game Control Protocol (VGCP). SOURCE it
# (`. gfx-env.sh`) before starting Godot; run-game.sh beside it does. It selects a lane from the
# caller's variables; it does not probe the hardware. docs/virtual-display.md has the setup and the
# troubleshooting.
#
# Reads:
#   VGCP_DISPLAY       set (e.g. :0): render on that real session with its GPU, VISIBLE there.
#                      Unset (the default): the isolated software lane on an Xvfb display.
#   VGCP_XVFB_DISPLAY  the Xvfb display of the isolated lane (default :99).
#   VGCP_SOFTWARE      1 forces software Vulkan (Mesa lavapipe). The isolated lane sets it to 1.
#   VGCP_ADDR, VGCP_HOST, VGCP_PORT   kept when set, else 127.0.0.1:38787.
#
# Sets (its output):
#   DISPLAY                    the session or Xvfb display Godot opens.
#   VGCP_DRIVER_FLAGS          extra Godot flags run-game.sh passes: on the isolated lane
#                              "--display-driver x11 --audio-driver Dummy", else empty.
#   VGCP_SOFTWARE              as above.
#   VGCP_ADDR, VGCP_HOST, VGCP_PORT   the endpoint the server binds and the clients use.
#   XDG_RUNTIME_DIR            isolated lane only: a private ${TMPDIR:-/tmp}/vgcp-xdg-<uid>.
#   WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS   unset on the isolated lane.
#   VK_ICD_FILENAMES, VK_DRIVER_FILES           the Vulkan driver the loader must use.
#   LIBGL_ALWAYS_SOFTWARE, GALLIUM_DRIVER       software GL, or unset for a real GPU.
#   LD_LIBRARY_PATH            the system library directory first.
#
# The isolated lane renders with Mesa lavapipe (software Vulkan): a hardware Vulkan driver cannot
# present to Xvfb (no DRI3), so there the Vulkan device is llvmpipe by design, and no GPU is needed.
# `vulkaninfo --summary` on that display names llvmpipe. On a real session (VGCP_DISPLAY) it names
# the hardware device, when the machine has a usable render node (/dev/dri/renderD*) and a hardware
# Vulkan driver.

# ---- Display isolation and renderer (read before changing) ------------------------------------
# The isolated lane renders on a DEDICATED Xvfb display, in software, walled off from any live
# login or remote desktop session. Two things matter:
#  1. ISOLATION: a remote desktop session exports DISPLAY=:0, WAYLAND_DISPLAY=wayland-0 and
#     XDG_RUNTIME_DIR=/run/user/<uid>. Godot's X11 driver FALLS BACK TO WAYLAND when X11 init fails,
#     so even with DISPLAY=:99 it can reach the session's compositor and open a window on that
#     desktop. So: strip WAYLAND_DISPLAY, XDG_RUNTIME_DIR and the session bus, and force
#     --display-driver x11.
#  2. RENDERER: a hardware Vulkan driver cannot present to Xvfb (no DRI3; Godot reports "None of the
#     devices supports both graphics and present queues" and its X11 display driver fails). So the
#     isolated lane uses SOFTWARE Vulkan (lavapipe), which presents to Xvfb. GPU rendering needs a
#     real session (VGCP_DISPLAY=:0).
if [ -n "${VGCP_DISPLAY:-}" ]; then
  # A real session: its display, its GPU, visible there.
  export DISPLAY="$VGCP_DISPLAY"
  export VGCP_DRIVER_FLAGS=""
else
  # The isolated software lane on a dedicated Xvfb display. VGCP_XVFB_DISPLAY lets PARALLEL runs
  # each own a display, so they do not share one framebuffer (pair it with a per-run VGCP_PORT).
  export DISPLAY="${VGCP_XVFB_DISPLAY:-:99}"
  unset WAYLAND_DISPLAY                                 # no path to a session's Wayland compositor
  _xdg="${TMPDIR:-/tmp}/vgcp-xdg-$(id -u)"
  mkdir -p "$_xdg" 2>/dev/null; chmod 700 "$_xdg" 2>/dev/null
  export XDG_RUNTIME_DIR="$_xdg"                        # so a Wayland fallback finds no wayland-0
  unset DBUS_SESSION_BUS_ADDRESS                        # do not attach to a session bus
  # Force X11 (never an automatic Wayland pick) and the Dummy audio driver: the isolated lane has no
  # sound card, and without it Godot's ALSA probe logs one engine `ERROR:` per boot
  # ("Condition \"status < 0\" is true. Returning: ERR_CANT_OPEN") before it falls back to Dummy
  # anyway, a false positive in any count of errors in the game log. A real session keeps its audio.
  export VGCP_DRIVER_FLAGS="--display-driver x11 --audio-driver Dummy"
  : "${VGCP_SOFTWARE:=1}"; export VGCP_SOFTWARE        # a GPU cannot present to Xvfb: software here
fi

# The VGCP endpoint, when the caller has not set it (launch_game.sh always does). The in-game server
# reads VGCP_ADDR; the clients (control.py, the MCP server) read VGCP_HOST and VGCP_PORT.
export VGCP_ADDR="${VGCP_ADDR:-127.0.0.1:38787}"
export VGCP_HOST="${VGCP_HOST:-127.0.0.1}"
export VGCP_PORT="${VGCP_PORT:-38787}"

# The system library directory (Debian and Ubuntu layout). It goes FIRST on LD_LIBRARY_PATH, so the
# system Vulkan loader and drivers win over any other copy an inherited LD_LIBRARY_PATH carries.
SYSLIB="/usr/lib/$(uname -m)-linux-gnu"
_icd_dir=/usr/share/vulkan/icd.d
_arch="$(uname -m)"

if [ "${VGCP_SOFTWARE:-0}" = "1" ]; then
  # ---- Software: Mesa lavapipe (Vulkan) and llvmpipe (GL); no GPU ----
  # Software GL too (harmless for Forward+ on Vulkan; used if anything touches GL).
  export LIBGL_ALWAYS_SOFTWARE=1
  export GALLIUM_DRIVER=llvmpipe
  _lvp=""   # NB: a plain `ls A B | head` would abort a `set -euo pipefail` caller when A is absent.
  for _f in "$_icd_dir/lvp_icd.$_arch.json" "$_icd_dir/lvp_icd.json"; do
    if [ -f "$_f" ]; then _lvp="$_f"; break; fi
  done
  if [ -n "$_lvp" ]; then
    export VK_ICD_FILENAMES="$_lvp"
    export VK_DRIVER_FILES="$_lvp"
  else
    echo "[gfx-env] no lavapipe Vulkan driver in $_icd_dir (Debian and Ubuntu: mesa-vulkan-drivers); software rendering will fail" >&2
    unset VK_ICD_FILENAMES VK_DRIVER_FILES
  fi
else
  # ---- A real session: this machine's GPU, whatever it is ----
  # Pin the loader to a hardware Vulkan driver that exists here, or leave the loader to its own
  # enumeration when none does. A driver's JSON file existing proves nothing (mesa-vulkan-drivers
  # ships radeon, intel and nouveau files everywhere): force one only when a DRM render node is
  # present, i.e. there is hardware for it to drive.
  _hw_icd=""
  if ls /dev/dri/renderD* >/dev/null 2>&1; then
    for _c in radeon_icd intel_icd nvidia_icd virtio_icd nouveau_icd; do
      for _f in "$_icd_dir/$_c.$_arch.json" "$_icd_dir/$_c.json"; do
        [ -f "$_f" ] && { _hw_icd="$_f"; break 2; }
      done
    done
  fi
  if [ -n "$_hw_icd" ]; then
    export VK_ICD_FILENAMES="$_hw_icd"
    export VK_DRIVER_FILES="$_hw_icd"
  else
    unset VK_ICD_FILENAMES VK_DRIVER_FILES
  fi
  # Clear the software switches: either would silently put GL back on llvmpipe.
  unset LIBGL_ALWAYS_SOFTWARE GALLIUM_DRIVER
fi
if [ -d "$SYSLIB" ]; then
  export LD_LIBRARY_PATH="$SYSLIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
