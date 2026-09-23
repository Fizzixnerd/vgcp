# VGCP: the Video Game Control Protocol

VGCP, the Video Game Control Protocol, lets a program drive a running game the way a debugger
drives a process. The game starts **paused**. A client, such as an AI agent, an automated test or
a script, then advances it an exact number of physics ticks, takes a screenshot of a freshly
drawn frame, sends input and queries the game's state. Nothing moves while the client thinks.

The protocol is newline-delimited JSON over a loopback TCP socket. This repository holds the
protocol document, a server for Godot 4 games written in Rust with gdext, and client drivers in
Python and TypeScript, a test system that records a test once and replays it with no agent, and
helpers for taking real screenshots on a machine with no display. It is v0.2; the protocol itself
is at version **1.5.2**.

## Why

An agent takes seconds to decide on its next move, and a game runs about 60 physics ticks a
second. Tools that let the game run in real time and then wait some milliseconds race it: the
frame the agent sees is not the frame it acts on. VGCP removes the race:

- **Paused by default.** The client only ever observes a frozen world.
- **Frame-exact `step(n)`.** Advance exactly N physics ticks, then pause again.
- **Capture after a real draw.** A screenshot forces a frame draw first, so it is never stale,
  even while paused.
- **In the game, not the editor.** The server is a node inside the running game, reached over
  direct TCP, not an editor plugin polling files.
- **The game's own state and vocabulary.** The game registers state providers (`get_state`),
  an action sink for device-independent actions (`input` with `game_action`) and a seed target
  (`seed`). Clients can wait for a signal (`await_signal`), log signals over an advance
  (`record_signals`), check or wait for a state predicate (`assert`, `await_state`) and replay
  input scripts frame by frame.

```
  AI agent (MCP client) --stdio--> MCP server (vgcp-mcp) --+
                                                           +-- TCP, NDJSON --> game with VgcpServer
  test or script ----------------> control.py (vgcp-mcp) --+   127.0.0.1:38787 (vgcp-server)
```

## What is in v0.2

| Path | What |
|---|---|
| [`docs/vgcp-protocol.md`](docs/vgcp-protocol.md) | the protocol, version 1.5.2: every command, field and error code. It is the single source of truth; every implementation here conforms to it. |
| [`vgcp-server/`](vgcp-server/) | the `vgcp-server` crate: the in-game server, one `VgcpServer` node, for Godot 4.7 or newer and gdext (the `godot` crate) 0.5.5 |
| [`vgcp-mcp/`](vgcp-mcp/) | the drivers: `control.py`, a Python CLI and library that needs only the standard library; `mock_server.py`, a VGCP server with no game engine, for trying and testing clients; and an MCP server in TypeScript that exposes each command as a `game_*` tool and advertises itself as `vgcp-godot` |
| [`vgcp-test/`](vgcp-test/) | the test system, in Python with only the standard library: record a test by playing it once through a `Recorder`, replay every recorded script against a freshly launched game with no agent, and get a triage packet for each failure |
| [`virtual-display/`](virtual-display/) | shell scripts that launch a game windowed on a private Xvfb display with software Vulkan, so screenshots are real frames on a machine with no monitor or GPU |
| [`docs/virtual-display.md`](docs/virtual-display.md) | how to install, use and troubleshoot that display |

## Quick start, with no Godot

You need `python3` and `git`; `node` 22 or newer adds the MCP server to the self-test.

```bash
python3 vgcp-test/selftest.py         # the test system against the mock; ends with ALL PASS
cd vgcp-mcp
bash selftest.sh                      # both drivers against the mock; ends with OVERALL: ALL PASS
```

Or drive the mock by hand:

```bash
cd vgcp-mcp
python3 mock_server.py &              # a stand-in game on 127.0.0.1:38787
# wait for the mock to listen: control.py connects once and does not retry
for _ in $(seq 50); do python3 control.py ping >/dev/null 2>&1 && break; sleep 0.2; done
python3 control.py ping
python3 control.py step --ticks 10    # advance exactly 10 physics ticks, then pause again
python3 control.py get_state
python3 control.py screenshot         # writes a PNG and prints its path
kill %1
```

[`vgcp-mcp/README.md`](vgcp-mcp/README.md) lists every command of the CLI and the library, and
the known differences between the mock and the real server.

## Add the server to a gdext game

Requirements: Godot 4.7 or newer, gdext `godot = "=0.5.5"` with the `api-4-7` feature, and
Rust 1.94 or newer (edition 2024).

1. Depend on the crate behind a `vgcp` feature, so a build without the feature neither compiles
   nor links it:

   ```toml
   [features]
   vgcp = ["dep:vgcp-server"]

   [dependencies]
   godot = { version = "=0.5.5", features = ["api-4-7"] }
   vgcp-server = { git = "https://github.com/Fizzixnerd/vgcp", tag = "v0.2.0", optional = true }
   ```

2. Spawn the server from your extension's entry point:

   ```rust
   #[gdextension]
   unsafe impl ExtensionLibrary for MyGame {
       #[cfg(feature = "vgcp")]
       fn on_stage_init(stage: godot::init::InitStage) {
           vgcp_server::on_stage_init(stage);
       }
   }
   ```

3. Register what the game publishes with the node at `vgcp_server::NODE_PATH`
   (`/root/VgcpServer`): a state provider such as `game` returning `score`, `lives`, `phase` and
   `player.x`, an action sink for actions such as `move_to {x}` and `jump`, and a seed target.

4. Build with `cargo build --features vgcp`, import the project once, and run the game
   windowed. The server listens on `127.0.0.1:38787`; set `VGCP_ADDR` to change it.

**One `godot-core`.** gdext registers every class in a single registry inside the `godot-core`
crate. If your game and `vgcp-server` linked two copies of it, `VgcpServer` would silently never
register. Take `godot` from crates.io with the same `=0.5.5` requirement and enable no `api-*`
feature other than `api-4-7`; Cargo then builds one copy. If your game takes gdext from git, add
a `[patch.crates-io]` entry for `godot` that points at the same revision. Check with:

```bash
cargo tree --features vgcp -i godot-core     # must print exactly one godot-core
```

[`vgcp-server/INTEGRATION.md`](vgcp-server/INTEGRATION.md) has every step in full: the
dependency and the one-`godot-core` rules (§1), state providers in Rust and GDScript (§2), the
action sink and seed target (§2a), and a smoke test (§6).

**Screenshots need a rendering display.** Godot's `--headless` mode uses a dummy renderer, which
draws no frame, so `screenshot` fails there with `capture_failed` ("could not read viewport image").
Run the game windowed on a real display, or on a virtual one
([Screenshots with no display](#screenshots-with-no-display), below).

**Keep it out of release builds.** The port is loopback only and unauthenticated: it is a debug
facility. Never expose it off `localhost`, and build releases without the `vgcp` feature.

## Register the MCP server

```bash
cd vgcp-mcp
npm ci
npm run build                         # writes dist/
```

Then register `vgcp-mcp/dist/index.js` as a stdio server with your MCP client, for example with a
project configuration like [`vgcp-mcp/examples/mcp.json`](vgcp-mcp/examples/mcp.json):

```json
{
  "mcpServers": {
    "vgcp-godot": {
      "command": "node",
      "args": ["vgcp-mcp/dist/index.js"],
      "env": { "VGCP_HOST": "127.0.0.1", "VGCP_PORT": "38787" }
    }
  }
}
```

The server offers 17 tools, one per command (`game_ping`, `game_step`, `game_screenshot`,
`game_input`, `game_get_state`, ...). `dist/` is build output: rebuild after every update and
restart the client so it reloads the tools.

## Test a game

`vgcp-test/` turns VGCP into a regression suite. Play a test once through a `Recorder` and it is
saved as a VGCP script: a linear list of commands whose expected replies were captured from the
game, synced on signals and state rather than on timing. `harness.py` then replays every script in
the game's `tests/vgcp/` against a freshly launched game, with no agent, and prints a triage packet
for each failure. Each script records the commit it was made against. A game describes itself, if
it needs to, in an optional `vgcp.json`: its directory, the environment variables that set up a
scenario, a boot mode. [`vgcp-test/README.md`](vgcp-test/README.md) has the details.

```bash
cd path/to/my-game                              # a git checkout of the game
python3 path/to/vgcp/vgcp-test/harness.py --list
python3 path/to/vgcp/vgcp-test/harness.py       # replay every script; exit 0 when all pass
```

## Screenshots with no display

`virtual-display/launch_game.sh` runs a game windowed on a private Xvfb display with Mesa's
software Vulkan driver (lavapipe), so captures are real frames on a machine with no monitor or
GPU, and no window appears on anyone's desktop. It starts the display if it is not already
running, refuses a VGCP port that another process holds, waits for the server, and prints the
game's pid on standard output and a `kill` line to stop it on standard error. Set
`VGCP_DISPLAY=:0` to render on a real display instead.
[`docs/virtual-display.md`](docs/virtual-display.md) lists the packages to install and how to
troubleshoot.

```bash
cd path/to/my-game                              # holds project.godot, or godot/project.godot
bash path/to/vgcp/virtual-display/launch_game.sh --port 38787
python3 path/to/vgcp/vgcp-mcp/control.py screenshot    # prints the PNG's path
```

## Environment variables

| Variable | Read by | Meaning |
|---|---|---|
| `VGCP_ADDR` | server | the address to bind, for example `127.0.0.1:38787` |
| `VGCP_HOST`, `VGCP_PORT` | drivers, mock, launcher | where to connect or listen; default `127.0.0.1` and `38787` |
| `VGCP_SHOTS_DIR` | server | where a `screenshot` with no `path` is written; default `<temp>/vgcp_tmp`, so `TMPDIR` moves it. A `path` the client sends is used as given. |
| `VGCP_GAME_DIR` | test harness | the game directory, relative to the repository root; overrides `vgcp.json`'s `game_dir` |
| `VGCP_GODOT_PROJECT` | launcher | the Godot project to boot; default the current directory when it holds `project.godot`, else its `godot/`. The harness sets it to the game's project. |
| `GODOT4_BIN` | launcher | the Godot binary, a path or a command name; default `godot4` on `PATH`, then `~/.local/bin/godot4`, then `godot` |
| `VGCP_DISPLAY` | launcher | set (for example `:0`) to render on that real display with its GPU, where the window is visible; unset, the game runs on the private Xvfb display |
| `VGCP_XVFB_DISPLAY` | launcher | the private Xvfb display; default `:99` (`--display` sets it) |
| `VGCP_SOFTWARE` | launcher | `1` forces software Vulkan (lavapipe); the private display defaults it to `1`, since a hardware Vulkan driver cannot present to Xvfb |
| `VGCP_DRIVER_FLAGS` | set by `gfx-env.sh` | the extra Godot flags `run-game.sh` passes: `--display-driver x11 --audio-driver Dummy` on the private display, none on a real one |

## The protocol

[`docs/vgcp-protocol.md`](docs/vgcp-protocol.md) is the contract: transport and framing, the
message envelopes, every command with its arguments and replies, the error codes, a worked
example session and a conformance checklist for new implementations. Versions follow semver:
additive changes bump the minor version, and a client refuses a server whose `protocol_major`
(returned by `ping`) it does not support.

## License

Licensed under either of

- Apache License, Version 2.0 ([`LICENSE-APACHE`](LICENSE-APACHE))
- MIT license ([`LICENSE-MIT`](LICENSE-MIT))

at your option. Unless you explicitly state otherwise, any contribution intentionally submitted
for inclusion in the work by you, as defined in the Apache-2.0 license, shall be dual licensed as
above, without any additional terms or conditions.

VGCP is an independent project. It is not affiliated with or endorsed by the Godot Engine project.
