# vgcp-mcp: drivers for the Video Game Control Protocol

The Video Game Control Protocol (VGCP) lets a program drive a running game: pause it, advance
it an exact number of physics ticks, take screenshots, inject input, read game state, wait for
signals and check state predicates. A VGCP server runs inside the game and speaks
newline-delimited JSON over a localhost TCP socket. The protocol is specified in
[`../docs/vgcp-protocol.md`](../docs/vgcp-protocol.md); this directory holds the client side,
for protocol version **1.5.2**.

There are two interchangeable drivers:

1. **`control.py`**, a Python command-line tool and importable client. It uses only the
   standard library, so there is nothing to install. Start here.
2. **An MCP server** (`src/`, Node 22 or later) that exposes each VGCP command as an MCP tool
   (`game_ping`, `game_step`, `game_screenshot`, `game_input`, ...), so an MCP client such as
   an AI coding assistant can drive the game directly. It advertises itself as `vgcp-godot`.

A **mock server** (`mock_server.py`) implements the protocol with no game engine, so both
drivers can be tried and tested before a game exists.

## Files

| Path | What |
|---|---|
| `control.py` | Python CLI and `VgcpClient` library (standard library only) |
| `mock_server.py` | mock VGCP server with no engine; writes real PNGs; used by the self-tests |
| `selftest.py` | Python driver against the mock |
| `selftest.sh` | runs `selftest.py`, then (if `node` is present) type-checks, builds and runs the MCP self-test |
| `src/vgcp-client.ts` | TCP/NDJSON client in TypeScript (a port of `control.py`'s client) |
| `src/index.ts` | the MCP server (17 `game_*` tools) |
| `test/mcp_selftest.mjs` | starts the MCP server and the mock, and drives every tool through a real MCP client |
| `examples/sample-input-script.json` | an input script with mouse, key and action events |
| `examples/game-action-script.json` | an input script whose frames carry `game_action` events |
| `examples/mcp.json` | an MCP client configuration that registers the server as `vgcp-godot` |
| `package.json`, `package-lock.json`, `tsconfig.json` | the MCP server's build |

## Try it against the mock

No game or engine is needed:

```bash
python3 mock_server.py &                 # listens on 127.0.0.1:38787
# wait for the mock to listen: control.py connects once and does not retry
for _ in $(seq 50); do python3 control.py ping >/dev/null 2>&1 && break; sleep 0.2; done
python3 control.py ping
python3 control.py step --ticks 10
python3 control.py get_state
python3 control.py assert --provider hud --path lives --op eq --value 18
kill %1
```

## Self-test

```bash
bash selftest.sh
```

It starts the mock on a free port chosen by the operating system, so it never collides with a
running game, and ends with `OVERALL: ALL PASS` when everything passes. Temporary files go under
the system temporary directory (`TMPDIR` moves it) and are removed afterwards. It runs two layers:

- **Python:** `control.py` against the mock: `ping`, `pause`, `resume`, `step`,
  `set_timescale`, `screenshot` (a real PNG, with and without a `path`), `input` (action, key,
  click), `get_state` (all, one, unknown), `list_providers`; input scripts (load, run, status,
  clear, the `max_frames` cap, malformed scripts, `game_action` frames, the `skipped` list);
  `game_action` (accepted and echoed, malformed events, no action sink, a sink that refuses);
  `seed` (accepted and observable, non-integer values, no seed target); `await_signal` (fired,
  typed arguments, timeout, argument and target errors); `record_signals`; `await_state` and
  `assert` (held, timed out, passed, argument errors); and the error envelopes `unknown_cmd`,
  `bad_args` and `bad_json`.
- **TypeScript (if `node` is on `PATH`):** `tsc --noEmit`, a build, then the real MCP server
  driven by a real MCP client over stdio. It lists the tools and calls every `game_*` tool
  through to the mock, including an inline image result, the input-script tools,
  `game_await_signal`, `game_await_state`, `game_assert`, the `game_action` form of
  `game_input` with its typed `payload`, and `game_seed`.

## The Python driver

```bash
python3 control.py ping
python3 control.py pause | resume | list_providers
python3 control.py step --ticks 30                     # advance exactly 30 physics ticks, then re-pause
python3 control.py set_timescale --value 3.0
python3 control.py screenshot --path /tmp/f.png --downscale 0.5
python3 control.py screenshot                          # no --path: the server writes into VGCP_SHOTS_DIR (default <temp>/vgcp_tmp)
python3 control.py get_state [--provider hud] [--query '{"k":1}']
# device input: goes through the engine's input pipeline
python3 control.py input --type action --action jump --pressed true
python3 control.py input --type mouse_button --x 640 --y 360       # full click
# canonical game actions and seeding (protocol §4.6.1, §4.14)
python3 control.py input --type game_action --action move_to --payload '{"x":300}'
python3 control.py input --type game_action --action jump          # payload optional
python3 control.py seed --value 42
# input scripts (§4.9): upload and deterministically replay an input timeline
python3 control.py load-input-script --path examples/sample-input-script.json
python3 control.py run-input-script [--path s.json] [--max-frames 60]
python3 control.py input-script-status
python3 control.py clear-input-script
# await_signal (§4.10): advance until a signal fires, with a required tick timeout
python3 control.py await-signal --signal game_over --timeout-ticks 6000 --provider game
python3 control.py await-signal --signal phase_changed --timeout-ticks 600 --node /root/Main
# state predicates: assert (§4.13) checks the current paused state; await-state (§4.12)
# advances until the predicate holds. ops: eq ne lt le gt ge in contains exists truthy
python3 control.py assert --provider game --path phase --op eq --value lost
python3 control.py await-state --provider game --path score --op ge --value 100 --timeout-ticks 4000
python3 control.py raw '{"cmd":"ping"}'
```

The endpoint comes from `--host` / `--port`, else `VGCP_HOST` / `VGCP_PORT`, else
`127.0.0.1:38787`. The exit code is `0` when the reply has `ok: true` and `1` otherwise; for
`assert` it is `0` only when the predicate passed, so the CLI works as a test gate.
`run-input-script` also prints a warning on stderr when the reply lists `skipped` events.

As a library:

```python
from control import VgcpClient

with VgcpClient() as c:          # or VgcpClient(host="127.0.0.1", port=38787)
    c.pause()
    c.game_action("move_to", {"x": 300})
    c.step(ticks=60)
    print(c.get_state(provider="hud"))
    print(c.assert_("hud", "gt", path="lives", value=0)["passed"])
```

## The MCP server

```bash
npm ci
npm run typecheck      # tsc --noEmit
npm run build          # writes dist/
npm run selftest       # end-to-end against the mock
```

Register it with any MCP client that runs stdio servers. With Claude Code, for example:

```bash
claude mcp add vgcp-godot -e VGCP_PORT=38787 -- node <path-to>/vgcp-mcp/dist/index.js
```

Or use a project configuration like [`examples/mcp.json`](examples/mcp.json):

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

The path in `args` is relative to the directory the client starts in; give an absolute path
otherwise. `dist/` is build output and is not committed, so run `npm run build` after every
update and restart the client so it reloads the tool list.

The tools map one to one to VGCP commands:

| Tool | Command |
|---|---|
| `game_ping` | `ping` (fails with `protocol_mismatch` if the server's major version differs) |
| `game_pause`, `game_resume` | `pause`, `resume` |
| `game_step` | `step` |
| `game_set_timescale` | `set_timescale` |
| `game_screenshot` | `screenshot`; returns `{path, w, h}`, and with `return_image: true` also the PNG inline |
| `game_input` | `input`, including `type: "game_action"` |
| `game_seed` | `seed` |
| `game_get_state`, `game_list_providers` | `get_state`, `list_providers` |
| `game_load_input_script`, `game_run_input_script`, `game_clear_input_script`, `game_input_script_status` | the input-script commands |
| `game_await_signal` | `await_signal` |
| `game_await_state`, `game_assert` | `await_state`, `assert` |

## Protocol versions

| Version | Adds |
|---|---|
| 1.1.0 | input scripts: `load_input_script`, `run_input_script`, `clear_input_script`, `input_script_status` (§4.9) |
| 1.2.0 | `await_signal`: advance until a named signal fires or a required tick timeout elapses; signal arguments come back as typed `{type, value}` descriptors (§4.10) |
| 1.3.0 | `record_signals` on `step`, `run_input_script` and `await_signal`: a chronological `recorded` list of the watched signals' emissions (§4.11) |
| 1.4.0 | `await_state` and `assert`: state predicates `{provider, op, path?, value?, query?}` (§4.12, §4.13) |
| 1.5.0 | the `game_action` input event, delivered to the game's registered action sink and not swallowed while paused (§4.6.1); the `seed` command, delivered to the registered seed target (§4.14); errors `no_action_sink` and `no_seed_target` |
| 1.5.1 | `skipped` in every `run_input_script` reply, one `{frame, index, code, message}` entry per event the server could not inject; `step` and `run_input_script` settle after the last tick's physics step (§4.3, §4.9.5) |
| 1.5.2 | `mouse_move` and `mouse_button` coordinates are root-canvas coordinates, which the server maps to window pixels, so a script drives the same game point at any window size (§4.6) |

## What the self-test proves, and what it does not

- **Proven:** the wire protocol (envelopes, framing, error codes) and both drivers,
  end to end, against `mock_server.py`.
- **Not proven:** the Rust server. `mock_server.py` is an independent implementation of the
  protocol, not generated from the server, so the two could diverge. A passing self-test says
  nothing about whether the `vgcp-server` crate ([`../vgcp-server/`](../vgcp-server/)) builds
  or runs. That takes a build of the crate and the smoke test against a real game in
  [`../vgcp-server/INTEGRATION.md`](../vgcp-server/INTEGRATION.md) §6.
- **Known, intentional differences between the mock and the server.** Both conform to the
  protocol; they are not byte-identical:

  | Field or behaviour | Mock | Server |
  |---|---|---|
  | `ping.server` | `"mock-vgcp"` | `"gdext-vgcp"` |
  | `engine.fps` while paused | always `0.0` | the real `Engine.get_frames_per_second()` |
  | `step` reply delay | a short `time.sleep` | the actual N physics ticks elapsing |
  | `screenshot` PNG | a tiny synthetic gradient (stdlib `zlib`) | a real viewport capture |
  | a line over 1 MiB | checked after reading; keeps the connection | capped read; **closes** the connection |
  | providers | the built-in `engine` plus a fake `hud` (`gold`, `lives`, `wave`, `seed`) | the built-in `engine` plus whatever the game registers |
  | action sink and seed target | always registered (the hints `mock_no_sink`, `mock_sink_refuses` and `mock_no_seed_target` simulate the failures); an empty `action` string is refused structurally | whatever the game registered with `register_action_sink` / `register_seed_target`; an empty `action` reaches the sink, which refuses it (same `bad_args` code, different message) |
  | `run_input_script` `skipped` (1.5.1) | only `game_action` events, and only when a `mock_sink_refuses` or `mock_no_sink` hint is on the `run_input_script` args (every `game_action` in the frames run is then listed) | every event the injector refused, whatever its type, with the sink's real verdict per event |
  | settle timing (1.5.1) | nothing to settle (no physics) | pauses after the last tick's physics step, never an extra tick |
  | `seed` | stored and mirrored as `hud.seed` | handed to the game, which applies it at its next tick |
  | root-canvas mapping (1.5.2) | none (no rendering) | root-canvas coordinates mapped to window pixels |

  The mock accepts extra `mock_*` hint keys on some commands to reach branches it cannot reach
  by itself (a signal firing, a predicate holding, a refusing sink). The server ignores them.

## License

Licensed under either of the MIT license or the Apache License, Version 2.0, at your option
(`MIT OR Apache-2.0`). See the license files at the root of the repository.
