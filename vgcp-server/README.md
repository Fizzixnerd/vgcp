# vgcp-server

The in-game server for **VGCP, the Video Game Control Protocol**, for Godot 4 games written in Rust
with gdext (the `godot` crate). It is a `Node`, `VgcpServer`, that the game spawns under the root
window from its own `on_stage_init` (no autoload), and it opens a localhost TCP port that speaks
VGCP v1.5.2 as newline-delimited JSON ([`../docs/vgcp-protocol.md`](../docs/vgcp-protocol.md)).

A client (an AI agent, a test runner or a script) can then **pause the game by default**, advance
it an exact number of physics ticks (`step(n)`), capture screenshots **after a real frame draw**,
inject input, replay input scripts, wait for signals and state predicates, fix the run's seed, and
query **state providers the game registers**, all without frames advancing while the client
thinks.

**Requirements:** gdext `godot = "=0.5.5"` with the `api-4-7` feature, so Godot 4.7 or newer at
runtime; Rust 1.94 or newer (edition 2024). The server uses nothing beyond `godot` and the
standard library.

## Using it

Add it as an optional dependency behind a `vgcp` feature, so a build without the feature neither
compiles nor links it:

```toml
[features]
vgcp = ["dep:vgcp-server"]

[dependencies]
godot = { version = "=0.5.5", features = ["api-4-7"] }
vgcp-server = { git = "https://github.com/Fizzixnerd/vgcp", tag = "v0.1.0", optional = true }
```

Then call `vgcp_server::on_stage_init(stage)` from your `ExtensionLibrary::on_stage_init` under
`#[cfg(feature = "vgcp")]`, and register your game's state provider, action sink and seed target
with the node at `vgcp_server::NODE_PATH`. [`INTEGRATION.md`](INTEGRATION.md) has every step, and
[`lib_registration.rs.snippet`](lib_registration.rs.snippet) has the two edits in one place.

**The game and this crate must share one `godot-core`.** gdext registers every class in one
registry inside `godot-core`, and a second copy would be a second registry in which `VgcpServer`
silently never registers. Take `godot` from crates.io with the same requirement as above (Cargo
then unifies the two), and enable no `api-*` feature other than `api-4-7`. A game that takes gdext
from git must add a `[patch.crates-io]` entry for `godot` that points at the same revision
([`INTEGRATION.md`](INTEGRATION.md) §1).
`cargo tree --features vgcp -i godot-core` must print exactly one `godot-core`.

## Files

| File | What |
|---|---|
| `Cargo.toml` | the crate: package `vgcp-server`, lib `vgcp_server`, `godot = "=0.5.5"` with `api-4-7` (the game must use the same) |
| `Cargo.lock` | the crate's own lockfile, for building and testing it on its own |
| `src/lib.rs` | the whole server (one `Node` class, `VgcpServer`) plus its public API: `NODE_NAME` (`"VgcpServer"`), `NODE_PATH` (`"/root/VgcpServer"`) and `on_stage_init(stage) -> Option<Gd<VgcpServer>>` |
| `lib_registration.rs.snippet` | the consumer snippet: the optional dependency and feature, and the one-line `vgcp_server::on_stage_init(stage)` call |
| `INTEGRATION.md` | the wiring reference and the API-verification status |

## Design (one screen)
- **Spawned from `on_stage_init`, `PROCESS_MODE_ALWAYS`**, so it keeps servicing the socket while
  the rest of the SceneTree is paused. **Never under the editor** (`Engine::is_editor_hint()`:
  `vgcp_server::on_stage_init` returns `None`). The editor, `--import` and `--export-*` load the
  same library, and a paused tree plus a bound port there would hang them.
- **Bind on the main thread in `ready()`.** The `TcpListener` is bound up front in `ready()`. If
  the port cannot be bound (already in use), the server logs a clear `godot_error!` and calls
  `get_tree().quit()`: a `vgcp` build **terminates itself** rather than run silently with a dead
  background acceptor and no control port, and it never prints a misleading "listening on …" line.
- **A background `std::net` thread** only **accepts** on that already-bound listener and reads
  NDJSON lines; it **never touches the SceneTree**. Each request crosses to the **main thread**
  through an `mpsc` channel with a per-request reply channel (gdext rule: `Gd<T>` is `!Send`, and
  engine calls are main-thread only).
- **`process()`** (main thread) drains and executes commands.
- **`physics_process()`** runs the `step` budget: unpause, count exactly N physics ticks, settle
  (re-pause after the last tick's physics-server step, in `process()` or at the top of the next
  tick, whichever comes first; protocol §4.3), then send the deferred `step` reply.
- **Pause by default:** `get_tree().set_pause(true)` in `ready()`.
- **Screenshot:** `RenderingServer::force_draw()`, then
  `get_viewport().get_texture()→get_image()→save_png()`, so the PNG is a fresh frame even while
  paused. A `path` the client sends is used **verbatim** (absolute or not). With no `path`, the PNG
  goes into `VGCP_SHOTS_DIR`, or `<temp>/vgcp_tmp` when that is unset (`std::env::temp_dir()`,
  which honours `TMPDIR`), and the reply carries the absolute path; a relative `VGCP_SHOTS_DIR` is
  resolved against the process's working directory first.
- **Input:** `Input::action_press/release`, and synthetic `InputEventKey`,
  `InputEventMouseButton` and `InputEventMouseMotion` events through `parse_input_event`.
- **`get_state` extensibility:** the game calls `register_state_provider(name, target, method)`;
  `get_state` invokes `target.method(query)` and serialises the returned `Dictionary`. A built-in
  `engine` provider is always present.
- **State predicates:** `assert` evaluates a narrow predicate `{provider, op, path?, value?,
  query?}` against the current paused state and replies `{ok:true, passed, actual, …}` (a false
  assertion keeps `ok:true`); `await_state` re-evaluates the same predicate every physics tick,
  advancing until it holds (`held:true`) or `timeout_ticks` elapses. The operators are a comparison
  vocabulary (`eq/ne/lt/le/gt/ge/in/contains/exists/truthy`), not an expression engine.
- **`record_signals`:** an optional argument on the unpausing commands (`step`,
  `run_input_script`, `await_signal`, `await_state`) that logs every emission of the watched
  signals during the advance into a chronological `recorded` array.

## Endpoint and environment

- `VGCP_ADDR` sets the address the server binds. Unset, it is `127.0.0.1:38787`, the protocol's
  default, which the drivers also use.
- `VGCP_SHOTS_DIR` sets the directory pathless screenshots land in (default `<temp>/vgcp_tmp`).

The port is loopback only and unauthenticated: it is a debug facility, so never expose it off
`localhost`, and ship release builds without the `vgcp` feature.

## Build status
`src/lib.rs` builds against gdext 0.5.5 with `api-4-7` (Godot 4.7), and it has been exercised
end-to-end on a display against a running game (ping, step, screenshot, get_state and
await_signal). The gdext API calls were first verified against the gdext 0.5.3 source and
docs.rs; [`INTEGRATION.md`](INTEGRATION.md) §4 has the confirmed list.

The crate's own checks run in this directory:

```bash
cargo build --locked
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
```

## Validate the protocol without Godot
The wire contract is also checked end-to-end against a mock: see [`../vgcp-mcp/`](../vgcp-mcp/)
(`selftest.sh`). The mock is an **independent** Python implementation of VGCP (not this server,
and not generated from it), so it validates **the protocol and the two drivers**, **not** this
Rust server. This server is checked only by a real build and a run against a game
([`INTEGRATION.md`](INTEGRATION.md) §6). The drivers' README lists the known, intentional
differences between the mock and this server (for example `ping.server` is `"mock-vgcp"` rather
than `"gdext-vgcp"`).

## License
MIT OR Apache-2.0.
