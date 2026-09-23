# The virtual display

A Video Game Control Protocol (VGCP) screenshot is a frame Godot has just drawn, so the game needs
a real renderer and a window to draw into. The scripts in [`virtual-display/`](../virtual-display/)
give it both on a machine with no monitor and no GPU: Godot runs windowed on Xvfb, a virtual X
display, and renders with Mesa's lavapipe, a software Vulkan driver. Screenshots are real frames.

- `godot --headless` uses a dummy renderer, which draws no frame, so `screenshot` fails with
  `capture_failed` ("could not read viewport image").
- A hardware Vulkan driver cannot present to Xvfb, which has no DRI3: Godot logs `None of the
  devices supports both graphics and present queues`. Lavapipe can, so this lane renders on the CPU.
- Godot falls back to Wayland when its X11 driver fails, so `gfx-env.sh` unsets `WAYLAND_DISPLAY`
  and the session bus, sets a private `XDG_RUNTIME_DIR` and passes `--display-driver x11`. The
  window stays off any desktop.

Tested on Ubuntu x86_64 with Godot's Vulkan renderers (Forward+, Mobile) and its Compatibility
renderer (OpenGL, through Mesa's llvmpipe). The scripts expect the Debian and Ubuntu file layout.

## Install

```bash
sudo apt install xvfb mesa-vulkan-drivers libvulkan1 vulkan-tools python3 iproute2 \
  libxcursor1 libxinerama1 libxi6 libxrandr2 libxkbcommon0
```

`mesa-vulkan-drivers` holds lavapipe, `iproute2` provides `ss`, and the `lib*` packages are what
Godot loads to open an X11 window. `vulkaninfo --summary | grep deviceName` should list
`llvmpipe`. The Compatibility renderer also needs `libgl1-mesa-dri`, which `xvfb` recommends.

You also need a standard (non-.NET) build of Godot 4.7 or newer. The launcher runs `GODOT4_BIN`
(a path or a command name) if set, else the first of `godot4` on `PATH`, `~/.local/bin/godot4`
and `godot` on `PATH`.

## Quick start

Build the game with the server and import its project once, so Godot registers the extension
([`vgcp-server/INTEGRATION.md`](../vgcp-server/INTEGRATION.md) §1). For a game laid out as
`my-game/godot/` and `my-game/rust/`:

```bash
VGCP=path/to/vgcp                                # a clone of this repository
cd my-game
( cd rust && cargo build --features vgcp )
godot4 --headless --path godot --import           # once; the import needs no display
bash "$VGCP/virtual-display/launch_game.sh"       # starts Xvfb and the game, then returns
python3 "$VGCP/vgcp-mcp/control.py" ping          # "paused": true
python3 "$VGCP/vgcp-mcp/control.py" screenshot    # the reply's "path" names the PNG
```

The first import of a project with a GDExtension can end with `Aborted (core dumped)` (exit status
134) after it has done its work. Check that `godot/.godot/extension_list.cfg` names your
`.gdextension`; importing again exits cleanly.

Stop it with the `kill` line the launcher prints. A screenshot with no path goes to
`VGCP_SHOTS_DIR`, which the launcher sets to `${TMPDIR:-/tmp}/vgcp_tmp` unless it is already set.

## The launcher

`launch_game.sh` runs the other three scripts: `start-xvfb.sh` (the display), `gfx-env.sh` (the
environment; its header lists every variable it reads and sets) and `run-game.sh` (Godot,
windowed). It has no `--help`; its header comment is the usage.

- `--port N`: the VGCP port, exported as `VGCP_ADDR` and `VGCP_PORT` (default `VGCP_PORT`, else
  38787).
- `--display :N`: the Xvfb display (default `VGCP_XVFB_DISPLAY`, else `:99`), started at
  2560x1440, or reused if `/tmp/.X<N>-lock` exists.
- `--scene S`: boot scene `S`, such as `res://main.tscn`, instead of the main scene.
- `--reclaim`: if a Godot game already holds the port, kill it and launch a fresh one.
- `--virtual`: use the isolated lane even when `VGCP_DISPLAY` is set.
- Any other argument, and everything after `--`, goes to Godot; a second `--` starts the game's
  own arguments: `launch_game.sh --port 39011 -- --resolution 1600x900 -- --my-flag`.

The project is `VGCP_GODOT_PROJECT`, else the current directory if it holds `project.godot`, else
its `godot/` subdirectory. The game's pid is the only line on stdout; the rest goes to stderr,
including `[launch] XVFB_PID=<pid>` when the launcher started the Xvfb. It waits about 12 seconds
for the server's `listening on` log line. On any error it stops what it started (the game, and the
Xvfb if it started one) and exits 1. Logs go to `/tmp/vgcp-game-<port>.log` and
`/tmp/vgcp-xvfb<N>.log`. A held port is an error unless `--reclaim` applies, since the holder may be
another run's game. Parallel runs each need their own `--port` and `--display`; separate checkouts
do not separate them. In a script:

```bash
GAME_PID=$(bash "$VGCP/virtual-display/launch_game.sh" --port 39011 --display :125 2>launch.err) \
  || { cat launch.err; exit 1; }
XVFB_PID=$(sed -n 's/^\[launch\] XVFB_PID=//p' launch.err)
VGCP_PORT=39011 python3 "$VGCP/vgcp-mcp/control.py" ping
kill -- "-$GAME_PID" $XVFB_PID
```

## A real display instead

`VGCP_DISPLAY=:0 bash "$VGCP/virtual-display/launch_game.sh"` renders on the session at `:0`
with its GPU: fast, but visible on that desktop. No Xvfb, no driver flags. With a render node
(`/dev/dri/renderD*`), `gfx-env.sh` points the Vulkan loader at the first hardware driver
installed (radeon, intel, nvidia, virtio, nouveau); otherwise the loader chooses.
`VGCP_SOFTWARE=1` forces lavapipe. The launcher reads `VGCP_DISPLAY`, never an inherited
`DISPLAY`, so a shell with `DISPLAY=:0` still gets the isolated lane.

## Teardown

Stop what you started, by pid: `kill -- -<game pid>` stops the game's process group, and
`kill <Xvfb pid>` the display if the launcher started it (a reused one belongs to whoever started
it). Never `pkill godot`, `pkill -f godot` or `pkill Xvfb`: they stop other runs' games and
displays, and a `-f` pattern can match your own shell.

## Troubleshooting

- **`the control server did not come up`**: read the game log it names. With no `[VGCP]` line in
  it, the project was never imported (its `.godot/extension_list.cfg` must name your
  `.gdextension`; see the note under [Quick start](#quick-start)) or the game was built without
  `--features vgcp`.
- **`X11 Display is not available` in the log, after `reusing Xvfb already on :N`**: the lock
  `/tmp/.X<N>-lock` outlived its Xvfb. Use another `--display`, or delete it once its pid is gone.
- **`capture_failed`, "could not read viewport image"**: the game ran with `--headless`, whose
  dummy renderer draws no frame. Start it through the launcher; on this lane the log's device line
  names `llvmpipe`.
- **`None of the devices supports both graphics and present queues`**: leave `VGCP_SOFTWARE` unset
  or `1` on the isolated lane, and install `mesa-vulkan-drivers` if the log starts with
  `[gfx-env] no lavapipe Vulkan driver`.
- **A window on a desktop**: `VGCP_DISPLAY` is set; unset it or pass `--virtual`. Start Godot
  through the launcher or `run-game.sh`, since the isolation lives in `gfx-env.sh`.
- **`VGCP port N already in use by pid P`**: pick a free `--port`, or pass `--reclaim` if the
  holder is a stale game of yours. `ss -ltnp 'sport = :N'` names the holder.
- **`Condition "status < 0" is true. Returning: ERR_CANT_OPEN`** after ALSA lines: no sound card.
  The isolated lane passes `--audio-driver Dummy`; on a real session, pass
  `-- --audio-driver Dummy`.
- **Slow first frames**: lavapipe compiles shaders on the CPU first, then runs steadily; its frame
  rate falls as resolution and drawing work rise.
- **Pointer input lands at the window's edge**: X clamps the pointer to the screen, so keep the
  window (`--resolution`) within the 2560x1440 Xvfb screen.
