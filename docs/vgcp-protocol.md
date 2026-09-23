# Video Game Control Protocol (VGCP) v1

> **Single source of truth** for VGCP, the Video Game Control Protocol. Every implementation in
> this repository (the in-game gdext server, the Python driver, the TypeScript MCP server and the
> mock server) MUST conform to this document. If code and this document disagree, **this document
> wins**: fix the code.
>
> Status: **v1.5.2**, wire-compatible with 1.0.0 through 1.5.1. §2.4 lists what each version
> added.

---

## 0. Design goals (why this exists)

An AI agent reasons for **seconds** per step, while a running game advances about 60 physics ticks
per second. To observe a *coherent* world we must **freeze the simulation while the agent thinks**
and advance it **a frame-exact amount** on command. VGCP gives the agent six primitives (`pause`,
`resume`, `step(n)`, `screenshot`, `input`, `get_state`), plus `ping` and `set_timescale`, and an
extensibility hook so the game can publish its own state.

How VGCP differs from off-the-shelf Godot MCP servers:

| Property | Typical editor-plugin Godot MCP server | **VGCP** |
|---|---|---|
| Default state | game runs in real time | **paused by default**: the agent only ever observes a frozen frame |
| Time advance | `wait <ms>` (races the game) | **`step(n)` = exactly N physics ticks**, then an automatic re-pause |
| Capture timing | grabbed inside `_process`, no draw barrier (stale or torn frame) | **capture after a forced frame draw** (`frame_post_draw` / `force_draw`) |
| Transport | editor plugin + `user://` file polling | **direct localhost TCP NDJSON**, owned by the running game |
| Language | GDScript-only bridge | **gdext-native** (Rust) server; state providers can be Rust or GDScript |

---

## 1. Transport & framing

- **Transport:** TCP over loopback. Default endpoint **`127.0.0.1:38787`**. The endpoint is
  configurable: the reference server reads `VGCP_ADDR`, and the reference clients read `VGCP_HOST`
  and `VGCP_PORT`. The port is configuration, not part of the wire format. (38787 avoids 8787,
  which other local development servers commonly use.)
- **Framing:** **NDJSON** (newline-delimited JSON). Each message is **one** UTF-8 JSON object
  encoded with **no embedded literal newlines** (compact / minified), terminated by a single `\n`
  (`0x0A`). A bare `\r` before the `\n` is tolerated on read and never emitted on write. Empty
  lines (just `\n`) are ignored.
- **Encoding:** UTF-8. Numbers are JSON numbers; booleans are JSON booleans.
- **Max frame size:** a server MUST reject (error `frame_too_large`) any request line
  longer than **1 MiB** before parsing, and SHOULD close the connection after replying.
- **Half-duplex per request (recommended):** a client SHOULD keep **at most one request
  in flight per connection** and read until it sees the response with the matching `id`.
  The server processes requests from one connection **in order**, but `step` defers its
  reply (see §4.3), so a pipelining client must still match by `id`.
- **Connections:** multiple concurrent connections are allowed; each is serviced by its
  own reader, and **all** commands are funnelled onto the game's **main thread** for
  execution (the socket threads never touch the engine). A connection stays open until
  the client closes it or sends EOF.
- **Liveness:** there is no server-initiated greeting and no heartbeat in v1. Use `ping`.
- **Startup binding (fail fast, no port zombies):** the server **binds its loopback port on the
  main thread in `ready()`** before the accept loop ever runs; the accept loop then services the
  already-bound listener on a background thread. If the bind **fails** (port already in use), the
  game logs a clear error (`godot_error!`) and **quits** (`get_tree().quit()`) rather than run on
  with no control port, and it never prints the misleading "listening on …" line. A failed client
  connection therefore cleanly means **"no live server"**, not a half-alive game holding a dead
  port.

### Why loopback + no auth (v1)
The channel exposes input injection and arbitrary state: it is a **debug** facility.
v1 binds **loopback only** and is **unauthenticated**, matching the threat model of a
single-developer local loop. **Do not expose the port off `localhost`.** An optional
shared-token handshake is reserved for v2 (see §7).

---

## 2. Message envelopes

### 2.1 Request

```json
{"id": 1, "cmd": "pause", "args": {}}
```

| Field | Type | Req? | Notes |
|---|---|---|---|
| `id` | integer or string | optional | Echoed verbatim in the response. If omitted, the server echoes `null`. Clients SHOULD send a unique monotonic integer so they can match the (possibly delayed) reply. |
| `cmd` | string | **required** | One of the commands in §4. Unknown → error `unknown_cmd`. |
| `args` | object | optional | Command arguments. Omitted = `{}`. |

### 2.2 Response: success

```json
{"id": 1, "ok": true, "paused": true}
```

- Always contains `id` (echo) and `ok: true`. The remaining keys are the command's payload
  (flat and command-specific; see §4).

### 2.3 Response: error

```json
{"id": 1, "ok": false, "error": {"code": "bad_args", "message": "step.ticks must be >= 1"}}
```

- `ok: false` + an `error` object with a stable machine `code` (§6) and a human `message`.
- The `id` is echoed when it could be parsed; for unparseable input it is `null`.

### 2.4 Protocol identity & versioning

- Protocol name: **`vgcp`**. Version is **semver `MAJOR.MINOR.PATCH`** with an
  integer **`MAJOR`** that clients gate on.
- **v1** = `protocol_major: 1`, `version: "1.5.2"`.
- **Breaking** wire changes (renaming or removing a field or command, changing a type) bump
  **MAJOR**. Additive changes (new command, new optional arg, new payload field) bump
  **MINOR**. The `ping` response carries both (§4.1). A client MUST refuse a server whose
  `protocol_major` it does not support.
- **1.1.0 (additive over 1.0.0):** four new commands: `load_input_script`,
  `run_input_script`, `clear_input_script`, `input_script_status` (§4.9). A client built for
  1.0.0 is unaffected (same `protocol_major`, all prior commands and payloads unchanged); a 1.0.0
  server simply returns `unknown_cmd` for the new commands.
- **1.2.0 (additive over 1.1.0):** one new command, `await_signal` (§4.10). Same
  `protocol_major`; all prior commands and payloads unchanged; an older server returns
  `unknown_cmd`.
- **1.3.0 (additive over 1.2.0):** a new optional `record_signals` arg on the unpausing commands
  (`step` / `run_input_script` / `await_signal`) and a new optional `recorded` reply field (§4.11).
  Same `protocol_major`; clients that don't send it are unaffected, and an older server ignores it.
- **1.4.0 (additive over 1.3.0):** two new commands, `await_state` (§4.12) and `assert` (§4.13):
  state-predicate primitives for assembling deterministic scripted tests. Same `protocol_major`;
  an older server returns `unknown_cmd`.
- **1.5.0 (additive over 1.4.0):** a new `input` event type, **`game_action`** (§4.6), the
  device-agnostic *canonical action record*, delivered to a game-registered **action sink**; a new
  command, **`seed`** (§4.14), delivered to a game-registered **seed target**; and the four
  registration hooks that publish them (`register_action_sink` / `unregister_action_sink` /
  `register_seed_target` / `unregister_seed_target`, §4.6), which mirror `register_state_provider`.
  Two new error codes (`no_action_sink`, `no_seed_target`, §6). Same `protocol_major`; a client that
  never sends the new event or command is unaffected, and an older server answers `bad_args`
  (unknown input type) / `unknown_cmd`.
- **1.5.1 (additive over 1.5.0):** two changes, neither of which renames or removes anything.
  1. Every `run_input_script` reply (completed, cancelled and zero-frame) carries a new
     **`skipped`** array (§4.9.5): one `{frame, index, code, message}` entry per scripted event the
     server could not inject, and `[]` when every event landed. Before 1.5.1 a refused event (for
     example a `game_action` the sink rejected) was only logged, and the reply still said
     `completed: true`.
  2. A normative **settle** rule for when `step` and `run_input_script` re-pause (§4.3, §4.9.5):
     after the last tick's physics-server step, and never after an extra simulated tick. Earlier
     servers paused *before* that step, so a kinematic body moved on the last tick was applied one
     command late, and under `step(1)` loops never. That made a world driven one tick at a time
     differ from the same world driven in larger steps. Reply payloads are unchanged. A client that
     ignores `skipped` is unaffected.
- **1.5.2 (over 1.5.1; the wire shape is unchanged):** the `x`, `y` of `mouse_button` and
  `mouse_move` (in `input` and in input-script frames) are **root-canvas coordinates** (the space
  the game draws in and its state providers report positions in), not window pixels (§4.6, §4.9.1).
  The server maps them to window pixels through the root viewport's `final_transform ×
  canvas_transform` and rounds to whole pixels before warping the pointer and building the event.
  With display stretch disabled and no canvas transform that mapping is the identity, so a script
  recorded against a 1.5.1 server replays unchanged; with stretch on (for example a window that
  scales around a fixed 1280×720 canvas) the same script now lands on the same game point at every
  window size. The input-script portability check (§4.9.2) compares `resolution` with the root
  window's **base** size (its content-scale size when set), not the window's pixel size.

---

## 3. The agent loop (normative usage)

```
default: paused = true, control node = PROCESS_MODE_ALWAYS
loop:
  screenshot                 # game is paused -> stable, freshly-drawn frame
  get_state                  # reflect registered providers while frozen
  <THINK for N seconds>      # game does NOT advance (paused)
  input ...                  # queue actions; pausable gameplay nodes don't react yet
  step {ticks: n}            # unpause -> run exactly n physics ticks -> auto re-pause
                             #   (reply arrives only after the n ticks complete)
  # back to top
```

Because the agent **only ever observes a paused world**, there is no race where a frame
slips between "capture" and "pause". Input is injected *while paused* and only takes
effect during the following `step`.

---

## 4. Commands

All commands are listed with their `args` and success payload. Every command can also
return an error envelope (§2.3).

### 4.1 `ping`: liveness + version handshake
- **args:** `{}` (optional `min_protocol_major: int`; the server errors `protocol_mismatch`
  if it cannot satisfy it).
- **payload:**
  ```json
  {"ok": true, "protocol": "vgcp", "protocol_major": 1, "version": "1.5.2",
   "server": "gdext-vgcp", "paused": true, "physics_frame": 0, "time_scale": 1.0}
  ```
- Side-effect-free. Use as the first call to confirm the channel and the version.

### 4.2 `pause` / `resume`
- **`pause`**: `args: {}` → `{"ok": true, "paused": true}`. Sets `SceneTree.paused = true`.
- **`resume`**: `args: {}` → `{"ok": true, "paused": false}`. **Free-run**: clears any
  pending `step` budget and lets the game run in real time until the next `pause`/`step`. If a
  `step` is still draining when `resume` arrives, that step is **cancelled** (see §4.3 for the
  reply its waiter receives). The same cancellation applies to an in-flight `run_input_script`
  (§4.9.5) or `await_signal` (§4.10.2); a `pause` mid-flight cancels those two as well.

### 4.3 `step`: advance exactly N physics ticks, then auto-pause
- **args:**
  | key | type | default | notes |
  |---|---|---|---|
  | `ticks` | integer ≥ 1 | `1` | number of **physics** ticks to advance |
  | `mode` | `"budget"` \| `"blocking"` | `"budget"` | `budget` = unpause, count ticks in the ALWAYS node, re-pause. `blocking` is reserved (RL-style lockstep, §7); a v1 server MAY return `unsupported_mode`. |
- **Reply timing:** the response is **deferred until the N ticks have elapsed** (the game
  is paused again). A client MUST wait for the matching `id`. Recommended client timeout:
  `max(5s, ticks / physics_tps * 4)`.
- **payload:**
  ```json
  {"ok": true, "ticks": 30, "physics_frame": 130, "paused": true}
  ```
  `physics_frame` is `Engine.get_physics_frames()` *after* the step.
- A `step` while already free-running (after `resume`) still re-pauses at the end.
- **Settle: when the re-pause lands** *(normative since 1.5.1)*. "Exactly N ticks" means N ticks
  **simulated in full**: each of the N ticks runs the game's physics callbacks **and** the physics
  server's step that follows them. The server therefore re-pauses *after* the last tick's
  physics-server step, not inside that tick. It MUST NOT let a further tick run. In Godot the
  control node marks the step as *settling* on the last budgeted tick and finishes it (pause, then
  the reply) at whichever comes first:
  1. its own `process()` in the same engine iteration, before it handles any queued request. This
     runs after the physics server has stepped.
  2. the start of its `physics_process()` on the next tick, when the engine runs several physics
     ticks in one iteration (low frame rate). The control node runs first among that tick's nodes,
     and the pause takes effect before any game node or the physics server runs.

  The world an agent observes after `step(1)` ×N is therefore the same world as after `step(N)`,
  including kinematic bodies whose transforms the physics server applies in its step. The reply is
  sent when the step settles, so a request queued behind it always sees a paused, settled world.
- **Cancellation (resume/step during a draining step).** Only one `step` may be in flight; a
  second `step` while one is draining errors `bad_args` ("a step is already in progress"). A
  **`resume`**, however, **cancels** the in-flight step: the server frees the budget, unpauses,
  and sends the *original* step's deferred reply immediately as
  `{"ok": true, "ticks": <completed>, "physics_frame": <now>, "cancelled": true, "paused": false}`,
  where `<completed>` is the number of ticks that actually ran before the cancel. The
  `cancelled: true` field (additive; absent on a normal completion) lets a client distinguish a
  full step from a cancelled one. Clients waiting on a step `id` MUST therefore also accept a
  `paused:false` / `cancelled:true` reply.

### 4.4 `set_timescale`: fast-forward / slow-mo
- **args:** `{"value": float}` (`value > 0`; `value == 0` is rejected with `bad_args`: use `pause`
  to stop). Multiplies `delta` (`Engine.time_scale`). It is **not** a freeze. A fixed-tick game
  that ignores `delta` is unaffected by it: for such a game `step(n)` is the only clock.
- **payload:** `{"ok": true, "time_scale": 3.0}`.

### 4.5 `screenshot`: capture the (paused) viewport after a real draw
- **args:**
  | key | type | default | notes |
  |---|---|---|---|
  | `path` | string | server shots dir | path to write the PNG. Used **verbatim**, absolute or not. If omitted, the server writes into its shots directory (below) and returns the absolute path. |
  | `downscale` | float in `(0,1]` | `1.0` | scale factor applied before save (e.g. `0.5` → half size) to cut tokens when the agent reads the image. |
- **Capture contract:** the server forces or awaits a frame draw **before** reading the
  viewport texture (`RenderingServer.force_draw()` from Rust, or `await
  RenderingServer.frame_post_draw`), so the PNG reflects the current paused state, never
  a stale or half-composited frame.
- **Shots directory (no `path` sent):** the server writes into **`VGCP_SHOTS_DIR`** when that
  environment variable is set, and otherwise into **`<temp>/vgcp_tmp`**, where `<temp>` is the
  platform temp directory (`std::env::temp_dir()`, so `TMPDIR` moves it; nothing hardcodes a temp
  root). The directory is created if missing, and the reply carries the **absolute** path of the
  file written: a relative `VGCP_SHOTS_DIR` is resolved against the server process's working
  directory first, so a client that reads the reply from a different directory still finds the
  PNG. A `path` the client *does* send is never rewritten or joined onto this directory.
- **payload:**
  ```json
  {"ok": true, "path": "/tmp/vgcp_tmp/shot-7.png", "w": 960, "h": 540}
  ```
- Errors: `capture_failed` (no viewport texture, or a save error). **Headless caveat:** under
  `--headless` the capture is **blank**. Run windowed on a real or virtual display (for example
  Xvfb with a software Vulkan driver).

### 4.6 `input`: inject input (one event per message)
`args.type` selects the form. Inject **while paused**, then `step` to let the game react.

| `type` | args | meaning |
|---|---|---|
| `"action"` | `action: string`, `pressed: bool`, `strength?: float (0..1, default 1.0)` | mapped action via `Input.action_press/action_release`. **Most reliable.** |
| `"key"` | `keycode: int`, `pressed: bool`, `physical?: bool (default false)` | synthetic `InputEventKey` via `Input.parse_input_event`. `keycode`/`physical_keycode` is a Godot `Key` ordinal. |
| `"mouse_button"` | `x: float`, `y: float`, `button?: int (default 1=LEFT)`, `pressed?: bool\|null` | synthetic `InputEventMouseButton`. If `pressed` is `null`/omitted, the server emits **press then release** (a click). `button` is a Godot `MouseButton` ordinal (LEFT=1, RIGHT=2, MIDDLE=3). |
| `"mouse_move"` | `x: float`, `y: float` | `Input.warp_mouse` + an `InputEventMouseMotion` at `(x,y)` (for hover-dependent UI). |
| `"game_action"` *(1.5.0)* | `action: string`, `payload?: object` | **Not** a synthetic device event: the **canonical, device-agnostic action record**, handed straight to the game's registered **action sink** (below). See §4.6.1. |

*(1.5.2)* **Mouse coordinates are root-canvas coordinates.** `x`, `y` on `mouse_button` and
`mouse_move` are in the root viewport's canvas space, the space the game draws in and reports
positions in. The server maps a point `p` to window pixels as `round((final_transform ×
canvas_transform) · p)` (both transforms read from the root viewport at injection time) and uses
that whole-pixel position for `Input.warp_mouse` and for the synthetic event's `position`. With
display stretch disabled and an identity canvas transform the mapping is the identity (window
pixels, as before 1.5.2). With stretch on, a script therefore drives the same game point at every
window size, which is what lets one recording replay identically in a differently sized window.
The rounding matters: a windowing system delivers the pointer in whole pixels, so an unrounded
mapping would read back a fraction of a pixel away from the scripted point.

- **payload:** `{"ok": true, "injected": "action"}` (echoes the `type`). For `game_action` the
  reply also echoes the action name: `{"ok": true, "injected": "game_action", "action": "jump"}`.
- **Gotchas (designed around):** synthetic events are processed on the next input flush; buttons
  that track press state want press and release on **separate ticks**, so prefer `action` + a
  domain method, or inject then `step` ≥ 2. While paused, *pausable* nodes receive no `_input`,
  which is exactly why we inject, then step.

#### 4.6.1 `game_action` (added in 1.5.0): the canonical action record

The four device event types above describe a **device**: a key, a button, a pointer.
`game_action` describes what the *game* was asked to do, independently of which device asked: the
record a replay, a test and a telemetry stream can all agree on.

```json
{"type": "game_action", "action": "move_to", "payload": {"x": 300}}
```

| field | type | req? | notes |
|---|---|:---:|---|
| `action` | string | **required** | The action's canonical `snake_case` name. The *vocabulary is the game's*, not the protocol's: the server does no name checking (§4.6.2). |
| `payload` | object | optional | The action's parameters. **Omitted ⇔ `{}`.** `null` is accepted and means the same. Anything other than an object or null → `bad_args`. |

- **Delivery.** The server calls `sink_target.call(sink_method, [event])` on the **main thread**
  with the event dictionary exactly as received, and the sink returns a **bool**: `true` =
  accepted (queued by the game), `false` = rejected. No sink registered → error
  **`no_action_sink`**; a freed sink target → `internal`.
- **NOT subject to the paused-input trap.** A synthetic `key`/`mouse_button` is buffered by the
  engine, and pausable nodes get no `_input` while paused (§4.6 gotchas), which is why device
  injection needs inject-then-`step`≥2. A `game_action` never touches `Input`: it lands in the
  **game's own queue immediately**, while paused, and is consumed on the next tick. Inject, then
  `step 1`, and the action has been dispatched.
- **Same object in an input script.** A `game_action` event is a valid `frames[].events[]` entry
  (§4.9.1) and is injected through the *same* injector, so a scripted `game_action` at frame `F`
  is queued before the game's tick-`F` `_physics_process`, i.e. consumed on tick `F`. Example
  script: [`vgcp-mcp/examples/game-action-script.json`](../vgcp-mcp/examples/game-action-script.json).

##### 4.6.2 Errors

| condition | code |
|---|---|
| `action` missing or not a string; `payload` present and not an object | `bad_args` |
| the sink returned `false` (unknown action name, or an ill-typed payload field) | `bad_args` (`"action sink refused the event"`) |
| no action sink registered | `no_action_sink` |
| the registered sink target has been freed | `internal` |

The server deliberately **does not know the action vocabulary**: keeping the one deserialiser in
the game means there is never a second, drifting copy of it.

##### 4.6.3 Registration API: action sink and seed target (in-engine)

Alongside `register_state_provider` (§4.7), the control node exposes:

```
register_action_sink(target: Object, method: StringName) -> void      # method(event: Dictionary) -> bool
unregister_action_sink() -> void
register_seed_target(target: Object, method: StringName) -> void      # method(seed: int) -> bool
unregister_seed_target() -> void
```

Both are single-slot (a second `register_*` replaces the first) and both are invoked on the main
thread. A game registers all three surfaces (provider, sink, seed target) in one place; see
[`vgcp-server/INTEGRATION.md`](../vgcp-server/INTEGRATION.md).

### 4.7 `get_state`: query registered state providers (extensibility hook)
The game registers **named state providers** with the server. A provider is a
`(name, target_object, method)` triple; `get_state` calls `target.method(query)` and
expects a JSON-able object (a Godot `Dictionary`).

- **args:**
  | key | type | default | notes |
  |---|---|---|---|
  | `provider` | string | *all* | name of a single provider. If omitted, the server returns the merged map of **every** provider (each under its name) plus the built-in `engine` provider. |
  | `query` | any | `null` | opaque value forwarded to the provider (e.g. `{"group":"enemies"}`). |
- **payload (single provider):**
  ```json
  {"ok": true, "provider": "game", "state": {"score": 1200, "lives": 3, "phase": "playing", "player": {"x": 300.0}}}
  ```
- **payload (all providers):**
  ```json
  {"ok": true, "state": {"engine": {"paused": true, "physics_frame": 130, "process_frame": 131, "time_scale": 1.0, "fps": 0.0},
                          "game": {"score": 1200, "lives": 3, "phase": "playing"}}}
  ```
- Unknown `provider` → error `unknown_provider`.

#### Built-in `engine` provider (always present)
`{"paused": bool, "physics_frame": int, "process_frame": int, "time_scale": float, "fps": float}`.
This lets `get_state` work before the game registers anything.

#### Registration API (in-engine; see the server's INTEGRATION.md)
The control node exposes a registration method callable from Rust **or** GDScript:
```
register_state_provider(name: StringName, target: Object, method: StringName) -> void
unregister_state_provider(name: StringName) -> void
```
*(1.5.0)* Two more registration hooks live next to these, `register_action_sink` and
`register_seed_target` (§4.6.3), with the same shape and the same main-thread call convention.

The `method` is invoked as `target.call(method, [query])` on the **main thread**, and
must return a `Dictionary` (or any Variant that `JSON.stringify` accepts). A game typically
registers one provider named `game` that exposes the facts a client reasons about (for example
`score`, `lives`, `phase` and the player's position), so the agent reasons over meaning rather
than raw node dumps.

### 4.8 `list_providers`: enumerate registered providers
- **args:** `{}` → `{"ok": true, "providers": ["engine", "game"]}`.

### 4.9 Input scripts (added in 1.1.0): upload and deterministically replay an input timeline

An **input script** is a precise, frame-by-frame timeline of input events the server replays
against the running game, locked to **physics ticks** (the same unit `step(n)` advances). This
lets an agent automate a longer, *known* input path in one shot instead of re-deciding every
tick. Determinism comes from the tick lock plus a documented injection point (§4.9.3).

#### 4.9.1 Script format (canonical)

A script is one JSON object:

```json
{
  "version": 1,
  "resolution": [1280, 720],
  "scale": false,
  "frames": [
    { "frame": 0,  "events": [ {"type":"mouse_move","x":640,"y":360} ] },
    { "frame": 3,  "events": [ {"type":"mouse_button","x":200,"y":300,"button":1,"pressed":true},
                               {"type":"mouse_button","x":200,"y":300,"button":1,"pressed":false} ] },
    { "frame": 10, "events": [ {"type":"action","action":"jump","pressed":true},
                               {"type":"key","keycode":32,"pressed":true} ] }
  ]
}
```

| field | type | req? | meaning |
|---|---|:---:|---|
| `version` | int | optional | Script schema version. Must be `1` if present (else `bad_args`). |
| `resolution` | `[w, h]` | **required** | Two positive numbers: the viewport size, in **pixels**, the script was authored at. Used for the portability check and optional scaling (§4.9.2). |
| `scale` | bool | optional (default `false`) | If `true`, rescale every mouse coordinate to the live viewport (§4.9.2). |
| `frames` | array | **required** | The schedule. Each entry is `{frame, events}`. |
| `frames[].frame` | int ≥ 0 | **required** | **Relative** offset from playback start: frame `0` is the first played tick. Gaps are allowed (a tick with no events just advances). Multiple entries MAY share an index; they play in array order. |
| `frames[].events` | array | **required** | Zero or more events for that tick, played in array order. |

- **Units = root-canvas pixels** *(defined in 1.5.2)*: the same space as the `input` command's
  mouse events (§4.6), the game's own drawing space, mapped to window pixels by the server. With
  display stretch off and no canvas transform these are window pixels. Not percentages.
- **Each event object is byte-for-byte the `input` command's `args`** (§4.6): `{"type": "action"
  | "key" | "mouse_button" | "mouse_move" | "game_action", ...}` with the identical fields. The
  server reuses the *exact* same injection code path for scripts and for one-off `input` calls.
- *(1.5.0)* **`game_action` frames.** `{"type":"game_action","action":"jump"}` is a valid frame
  event: it goes through the same injector, so it is handed to the action sink at the start of the
  control node's tick-`F` `physics_process`, and the game consumes it during its own tick-`F` step.
  That is the §4.9.3 guarantee, without the accumulated-input machinery (a `game_action` never
  touches `Input`). A `game_action` whose sink is absent or which the sink refuses is **logged and
  skipped**, exactly like any other bad playback event, and since 1.5.1 it is also listed in the
  reply's `skipped` array (§4.9.5); `load_input_script` still rejects a structurally invalid one
  (missing or non-string `action`, non-object `payload`) with `bad_args`. Sample:
  [`vgcp-mcp/examples/game-action-script.json`](../vgcp-mcp/examples/game-action-script.json).
- **`duration_frames`** = `max(frame) + 1` = the number of physics ticks a full play advances.

#### 4.9.2 Resolution portability (warn, play 1:1, opt-in scaling)

Default playback is **1:1 pixels**. If the live **base** size differs from the script's
`resolution`, the server emits a `godot_warn!` (the author is expected to keep a script per
resolution) but **still plays 1:1**. Set top-level `"scale": true` to instead rescale every
mouse coordinate by `(live_w/authored_w, live_h/authored_h)`. **Scaling applies to
`mouse_button`/`mouse_move` events only** (key and action events carry no coordinates); it is
applied inside playback, just before the shared injector, and never mutates the stored script.

*(1.5.2)* The live base size is the root window's content-scale size when one is set (display
stretch on: the authored viewport, e.g. 1280×720, whatever the window's pixel size), else the root
viewport's visible rect. Because mouse coordinates are canvas coordinates (§4.6), a script authored
at the base size plays 1:1 in a window of any pixel size without a warning.

#### 4.9.3 Frame alignment (the timing, and why)

**Guarantee:** an event scheduled for frame `F` is observed by the game's nodes **on physics
tick `F`** of playback (frame `0` = the first played tick).

How it is achieved (and why it is deterministic):

1. **Tick lock.** Playback runs in the control node's `physics_process`, the same place `step`
   counts ticks. One frame index = one physics tick. `run_input_script` unpauses, and each
   physics tick the server injects that tick's events, then advances; after the last frame's
   tick it re-pauses (exactly like `step`'s budget).
2. **Inject before the game sees the tick.** Godot normally **buffers** events parsed via
   `Input.parse_input_event` and dispatches them at the *next* engine input flush, which is why
   the one-off `input` command needs an inject-then-`step` (§4.6 gotchas). For scripts we remove
   that latency two ways, during playback only:
   - `Input.set_use_accumulated_input(false)` for the duration of playback, so each
     `parse_input_event` **dispatches immediately and synchronously** (updates `is_action_pressed`
     state, fires `_input`/`_unhandled_input`, moves the mouse) instead of buffering; we also call
     `Input.flush_buffered_events()` after each frame's events to drain any agile-flush buffer.
     The previous `use_accumulated_input` value is restored when playback ends.
   - The control node sets a very low `process_priority` / `physics_process_priority` in
     `ready()` so its `physics_process` runs **first** among same-tick nodes. (It is added to the
     root window *after* the main scene, so by default it would run *last*; the low priority moves
     it first.) Injecting at the start of the control node's tick-`F` `physics_process` therefore
     lands the events **before** the game's nodes run their tick-`F` `_physics_process`.
3. **Net effect.** Game logic that polls input (`is_action_pressed`, mouse position) in
   `_physics_process`, and handlers that consume `_unhandled_input`, both see frame `F`'s input on
   tick `F`. *Caveat (documented):* with accumulated input off, input callbacks are delivered
   synchronously from within the physics step rather than the normal idle input phase. For
   polling-based gameplay (the common case) this is irrelevant; for event-handler gameplay the
   event still arrives on the correct tick, before that tick's physics, which is the guarantee.

This was verified against the gdext 0.5.3 API (`Input::{parse_input_event, set_use_accumulated_input,
flush_buffered_events, warp_mouse}`, `Node::set_physics_process_priority`) and confirmed on a live
run (the server starts, plays and re-pauses; replies and cancellation as below).

#### 4.9.4 `load_input_script`: validate + store a script

- **args:** exactly one source:
  | key | type | notes |
  |---|---|---|
  | `script` | object | the script inline (subject to the 1 MiB request-line cap). |
  | `path` | string | a path to a `.json` file on disk; the server reads and parses it, which **lifts the 1 MiB cap** (use for large scripts). |
- Validates the structure (§4.9.1); a malformed script → `bad_args` with a precise location
  (e.g. `frames[2].events[0]: action event requires string 'action'`). On success it stores the
  parsed script on the server (replacing any previous one).
- **payload:** `{"ok": true, "frames": <int>, "duration_frames": <int>, "events": <int>}`:
  `frames` = number of frame entries, `duration_frames` = `max(frame)+1`, `events` = total event
  count.

#### 4.9.5 `run_input_script`: replay, then auto re-pause (deferred reply)

- **args (all optional):**
  | key | type | notes |
  |---|---|---|
  | `script` / `path` | object / string | an **inline override**: play this instead of the loaded script (same source rules as `load_input_script`). If neither is given, the previously loaded script is played. |
  | `max_frames` | int ≥ 1 | cap playback to this many frames (`min(duration_frames, max_frames)`). |
- Like `step`, this **unpauses, drains tick by tick injecting each frame's events, then
  re-pauses**, and the reply is **deferred** until playback completes. A client MUST wait for the
  matching `id` (recommended timeout `max(timeout, max_frames/tps*4 + 5s)`).
- **payload (completed):**
  `{"ok": true, "frames_run": <int>, "physics_frame": <int>, "paused": true, "completed": true}`.
  `frames_run` is the number of ticks advanced (authoritative); `physics_frame` is
  `Engine.get_physics_frames()` after playback (note: the engine tick counter advances even while
  the tree is paused, so this is *not* `before + frames_run`).
- **`skipped`** *(1.5.1; always present)*: an array with one entry per scripted event that could
  not be injected during the frames that ran, in playback order, and `[]` when every event landed:
  ```json
  "skipped": [{"frame": 0, "index": 1, "code": "bad_args",
               "message": "action sink refused the event (action 'no_such_action')"}]
  ```
  `frame` is the relative frame index (§4.9.1). `index` is the event's position among that frame's
  events in playback order (entries that share a frame index count as one run of events). `code` is
  the error code the same event would have got from the `input` command (§4.6.2): `bad_args`,
  `no_action_sink` or `internal`. `message` is the human-readable reason. Playback still continues
  past a skipped event, so the reply is still `completed: true`. A test runner SHOULD treat a
  non-empty `skipped` as a failure unless the test expects it. Only *injection* failures are
  listed. An event the game accepted and later refused in its own logic (for example an action
  the game ignores in its current state and reports through a signal of its own) was delivered, so
  it is not skipped.
- **Settle** *(normative since 1.5.1)*: playback re-pauses after the **last frame's** tick has been
  simulated in full, including the physics server's step, and never after an extra tick. The rule
  and the two ways of meeting it are the same as for `step` (§4.3). Restoring
  `use_accumulated_input` and sending the reply happen when playback settles.
- **Cancellation.** A `pause` **or** `resume` arriving mid-playback **cancels** it (mirroring how
  `resume` cancels an in-flight `step`): the playback's deferred reply returns immediately as
  `{"ok": true, "frames_run": <ran-so-far>, "physics_frame": <now>, "completed": false,
  "cancelled": true, "paused": <true if pause / false if resume>, "skipped": [...]}`. Clients waiting on a
  `run_input_script` id MUST accept this `cancelled`/`completed:false` shape.
- A `run_input_script` while a `step` (or another playback) is draining → `bad_args`.
- An empty script (or `max_frames` resolving to 0 frames) replies immediately with
  `frames_run: 0, completed: true, skipped: []` without unpausing.

#### 4.9.6 `clear_input_script`: drop the stored script
- **args:** `{}` → `{"ok": true}`. Does not affect an in-flight playback.

#### 4.9.7 `input_script_status`: report load and playback state
- **args:** `{}` →
  `{"ok": true, "loaded": <bool>, "playing": <bool>, "current_frame": <int>, "total_frames": <int>}`.
  `loaded` = a script is stored; `playing` = a playback is in flight; `current_frame` = the
  in-flight playback's tick cursor (0 when idle); `total_frames` = the playing script's capped
  length while playing, else the loaded script's `duration_frames`, else 0.

### 4.10 `await_signal` (added in 1.2.0): advance until a Godot signal fires, with a required timeout

An agent often wants to run the game *until something happens* (the player reaches a goal, the
game ends, an enemy spawns) rather than guess a fixed `step(n)`. `await_signal` does exactly that:
it **unpauses, advances the world watching for one named Godot signal, and re-pauses the instant
that signal fires** (or a **required** tick timeout elapses). Like `step` it **defers its reply**
until the wait resolves. The no-hang guarantee is structural: the budget is measured in **physics
ticks** (the unit `step(n)` advances) and the timeout is **not optional**, so the world never
advances forever.

#### 4.10.1 args

| key | type | req? | notes |
|---|---|:---:|---|
| `signal` | string | **required** | The signal name on the target object (e.g. `game_over`). A name the target has no signal for → `bad_args` (fail fast, rather than a silent await that never fires). |
| `timeout_ticks` | int ≥ 1 | **required** | Max physics ticks to advance before giving up. **Not optional**: there is no infinite await. |
| `node` | string | one-of | A `NodePath` to the emitter, resolved from the control node (absolute paths like `/root/Game` work). |
| `provider` | string | one-of | Name of a registered state provider (§4.7); the server watches *that provider's target object* (reusing the `get_state` registration, e.g. `provider:"game"`). |

Exactly **one** of `node` / `provider` is required (both, or neither → `bad_args`).

#### 4.10.2 behaviour

- **Unpause → watch → re-pause.** The server connects an internal capture callable to `signal`,
  unpauses, and each physics tick checks whether it fired. On fire it disconnects, re-pauses, and
  replies `fired:true` with the signal's args (§4.10.5). If `timeout_ticks` ticks pass first it
  disconnects, re-pauses, and replies `fired:false, timed_out:true`. **Either way the tree ends paused.**
- **Detection latency / 1-tick grace.** The capture runs during the *emitter's* tick, which (by
  control-node priority) is after the server's per-tick poll, so a signal emitted on tick *T* is
  observed on tick *T+1*. The server grants a **1-tick detection grace** past `timeout_ticks` so a
  signal firing on the final budgeted tick still counts as `fired` (not `timed_out`); a clean
  timeout therefore reports `waited_ticks == timeout_ticks + 1`. `waited_ticks` is always the actual
  physics ticks advanced (`physics_frame` is the engine counter after re-pause, not `before + waited`).
- **Mutual exclusion.** An `await_signal` while a `step` / input-script / another `await_signal` is
  draining → `bad_args`.
- **Cancellation.** A `pause` **or** `resume` arriving mid-await **cancels** it (mirroring how
  `resume` cancels a `step`): the deferred reply returns immediately as
  `{"ok":true,"signal":…,"fired":false,"cancelled":true,"waited_ticks":…,"physics_frame":…,"paused":<true if pause / false if resume>}`.
  The capture is disconnected on **every** exit path (fire, timeout, cancel, freed target), so no
  stale connection lingers. Clients waiting on an `await_signal` id MUST accept this
  `cancelled`/`fired:false` shape.

#### 4.10.3 payload: fired

```json
{"ok": true, "signal": "game_over", "fired": true,
 "args": [{"type": "bool", "value": false}],
 "waited_ticks": 235, "physics_frame": 3719, "paused": true}
```

#### 4.10.4 payload: timed out

```json
{"ok": true, "signal": "game_over", "fired": false, "timed_out": true,
 "waited_ticks": 6, "physics_frame": 1228, "paused": true}
```

#### 4.10.5 Signal arguments are **typed and structured** (never opaque)

The emitted signal's arguments come back in `args` as an **array of self-describing descriptors**,
so the agent gets data it can *use* instead of a stringified blob (Godot's `JSON.stringify` would
otherwise mangle a `Vector2` into `"(x, y)"` and an object into an opaque handle). Each descriptor
is `{"type": "<GodotType>", "value": <json>}`:

| Godot arg | `type` | `value` |
|---|---|---|
| `null` | `"nil"` | `null` |
| bool / int / float | `"bool"` / `"int"` / `"float"` | the native JSON scalar |
| String | `"String"` | the text |
| Vector2 / Vector2i / Vector3 / Vector3i | `"Vector2"` … `"Vector3i"` | `{"x":…, "y":…(, "z":…)}` |
| Color | `"Color"` | `{"r":…, "g":…, "b":…, "a":…}` |
| Rect2 | `"Rect2"` | `{"position": {x,y}, "size": {x,y}}` |
| Object / Node (live) | `"Object"` | `{"class":…, "instance_id":…, "name"?:…, "path"?:…}` |
| Array | `"Array"` | array of descriptors (recursed) |
| Dictionary | `"Dictionary"` | object of described values (recursed; keys stringified) |
| anything else (RID, Callable, packed array, transform…), **and a freed object handle**, which the engine refuses to convert to a live object | `"other"` | a human-readable `stringify` string (not dropped) |

So `lives_changed(3)` → `args: [{"type":"int","value":3}]`; an `enemy_spawned(who: Node, at: Vector2)`
would yield `[{"type":"Object","value":{"class":"Enemy",…}}, {"type":"Vector2","value":{"x":…,"y":…}}]`.

- **Errors:** `bad_args` (missing or empty `signal`, missing or `<1` `timeout_ticks`, no or both
  target keys, or the target has no such signal), `unknown_provider` (named provider not
  registered), `internal` (provider target was freed, or the engine `connect` failed).

### 4.11 `record_signals` (added in 1.3.0): log signals emitted during an advance

A debugging companion to the **unpausing** commands. Pass an optional **`record_signals`** arg to
`step`, `run_input_script`, `await_signal` (or `await_state`, since 1.4.0), and the server logs
every emission of the watched signals *during that advance window* and returns them in the reply's
**`recorded`** array. It answers "what fired while I wasn't watching?" without re-deciding every
tick. It reuses `await_signal`'s capture machinery (one connected recorder per watched signal,
disconnected on every exit path).

#### 4.11.1 arg: `record_signals`
An array of **watch specs**, each:

| key | type | req? | notes |
|---|---|:---:|---|
| `node` / `provider` | string | one-of | the emitter, resolved exactly like `await_signal` (§4.10.1): a NodePath, or a registered provider's object. |
| `signals` | `[string]` | optional | the signal names to record on that object. **Omitted = record ALL of the object's signals** (via `get_signal_list`, including inherited engine signals like `child_entered_tree`). |

Exactly one of `node`/`provider` per spec. A malformed spec, an unresolved target or an unknown
signal → `bad_args`, and the **whole command aborts before advancing** (no partial recording, no
recorders left connected).

#### 4.11.2 reply: `recorded`
A **chronological** array of emissions, each:

```json
{"signal": "<name>", "frame": <int>, "args": [ <typed descriptor §4.10.5>, … ]}
```

`frame` is the physics tick **relative to the start of the advance** (`1` = first advanced tick).
The server captures the engine physics-frame counter at arm time (while still paused), so the first
advanced tick reads `1`, the same baseline as `await_signal`'s `waited_ticks` (§4.10.2).
`args` are the §4.10.5 typed, structured descriptors. `recorded` is attached to the command's
normal **and** timeout **and** cancelled reply. At most **10000** emissions are kept; if exceeded,
the array is truncated and `recorded_truncated: true` is set (never a silent drop).

#### 4.11.3 example (record over a `step`)
```jsonc
{"id":9,"cmd":"step","args":{"ticks":300,"record_signals":[{"provider":"game","signals":["score_changed","life_lost"]}]}}
//  {"id":9,"ok":true,"ticks":300,"physics_frame":…,"paused":true,
//   "recorded":[{"signal":"score_changed","frame":1,"args":[{"type":"int","value":100}]},
//               {"signal":"life_lost","frame":210,"args":[{"type":"int","value":2}]}]}
```

Use it on `await_signal` to learn what *else* fired while waiting for the one signal, on `step` to
audit a fixed window, or on `run_input_script` to see what a scripted input path triggered.

### 4.12 `await_state` (added in 1.4.0): advance until a state predicate holds, with a required timeout

The state analogue of `await_signal`: instead of watching a signal, it **advances the world,
re-evaluating a narrow predicate over a `get_state` provider every physics tick**, and re-pauses the
instant the predicate holds, or after a **required** tick timeout. Deferred reply; cancellable by
`pause`/`resume`; supports `record_signals`. Use it to sync a scripted test on *state* (for example
"until `phase` equals `game_over`") when no signal marks the moment.

#### 4.12.1 the predicate (shared with `assert`, §4.13)
A **comparison**, deliberately not an expression engine:

| key | type | req? | notes |
|---|---|:---:|---|
| `provider` | string | **required** | A registered `get_state` provider (for example `game`, or the built-in `engine`). |
| `op` | string | **required** | One of `eq, ne, lt, le, gt, ge, in, contains, exists, truthy`. |
| `path` | string | optional | Dotted keys/indices into the provider's value, e.g. `phase`, `lives`, `player.x`, `enemies.0.hp`. Omitted = the whole value. |
| `value` | any | optional | The right-hand literal (ignored by `exists`/`truthy`). |
| `query` | any | optional | Opaque value forwarded to the provider (as in `get_state`). |

Operators: `eq`/`ne` (numeric-aware: `8` matches `8.0`; else Variant equality); `lt`/`le`/`gt`/`ge`
(numeric, false on a non-numeric side); `in` (`actual` ∈ the `value` array); `contains` (`actual` is
an array containing `value`, or a string containing the `value` substring); `exists` (the `path`
resolved); `truthy` (`actual` is truthy). A `path` that doesn't resolve → `actual = null`.

#### 4.12.2 args + reply
- **args:** the predicate (§4.12.1) **plus** `timeout_ticks` (int ≥ 1, **required**) and optional
  `record_signals` (§4.11).
- **held:** `{"ok":true, "provider":…, "held":true, "actual": <typed §4.10.5 descriptor>,
  "waited_ticks":…, "physics_frame":…, "paused":true}`.
- **timed out:** `{… "held":false, "timed_out":true, "actual":…, …}`.
- **cancelled** (`pause`/`resume` mid-await): `{… "held":false, "cancelled":true, …}` (a final
  re-evaluation runs first, so a predicate that became true right before the cancel still reports
  `held:true`). Re-uses the §4.10.2 1-tick detection grace.
- **Errors:** `bad_args` (missing or bad `provider`/`op`/`timeout_ticks`), `unknown_provider`.

### 4.13 `assert` (added in 1.4.0): check a state predicate now

Evaluate the **same predicate** (§4.12.1) against the current (paused) state **immediately**, with
no game advance. The assertion primitive for scripted tests.

- **args:** the predicate (§4.12.1): `provider`, `op`, `path?`, `value?`, `query?`. No `timeout_ticks`.
- **payload:** `{"ok":true, "passed": <bool>, "provider":…, "path"?:…, "op":…, "value":…,
  "actual": <typed §4.10.5 descriptor>}`. **`ok` is `true` even when the assertion is false**: the
  command *ran*, and `passed` carries the result (mirroring `await_signal`'s `fired`). A driver or
  CLI maps `passed:false` to a test failure.
- **Errors:** `bad_args` (missing or bad `provider`/`op`), `unknown_provider`.

### 4.14 `seed` (added in 1.5.0): fix the run's RNG seed

Determinism has two halves: the **action stream** (`game_action`, §4.6.1) and the **seed**. `seed`
hands an integer to the game's registered **seed target** so a run can be reproduced exactly.

- **args:** `{"seed": <int>}`, **required**, a JSON integer (a whole-valued float such as `42.0` is
  accepted; a fraction, a bool or a string → `bad_args`).
- **payload:** `{"ok": true, "seed": 42}`, echoing the value handed over.
- **Immediate, not deferred.** The command does **not** advance the world: it returns as soon as the
  seed target has accepted it. *When* the seed takes effect is the game's business; a game might,
  for example, apply it at the top of its next physics tick and restart the run. The documented
  pattern is therefore:

  ```jsonc
  {"id":1,"cmd":"seed","args":{"seed":42}}                                         // {"ok":true,"seed":42}
  {"id":2,"cmd":"await_signal","args":{"provider":"game","signal":"run_started","timeout_ticks":10}}
  //   {"ok":true,"signal":"run_started","fired":true,"args":[{"type":"int","value":42}], …}
  ```

  Seed, then sync on the run's own announcement, rather than guess a `step`.
- **Delivery.** `seed_target.call(seed_method, [seed])` on the main thread; the target returns a
  **bool** (`false` → `bad_args`).
- **Errors:** `bad_args` (missing or non-integer `seed`, or the target refused), **`no_seed_target`**
  (nothing registered), `internal` (the registered target was freed).

Registration is §4.6.3's `register_seed_target(target, method)`.

---

## 5. Command summary table

| `cmd` | args (→ defaults) | success payload (besides `id`,`ok`) | deferred reply? |
|---|---|---|:---:|
| `ping` | `min_protocol_major?` | `protocol, protocol_major, version, server, paused, physics_frame, time_scale` | no |
| `pause` | (none) | `paused:true` | no |
| `resume` | (none) | `paused:false` | no |
| `step` ² | `ticks=1, mode="budget"` | `ticks, physics_frame, paused:true` | **yes** |
| `set_timescale` | `value` | `time_scale` | no |
| `screenshot` | `path?, downscale=1.0` | `path, w, h` | no¹ |
| `input` | `type, …` (`type:"game_action"` → `action`, `payload?`) | `injected` (+ `action` for `game_action`) | no |
| `get_state` | `provider?, query?` | `state` (and `provider` if single) | no |
| `list_providers` | (none) | `providers` | no |
| `load_input_script` | `script?` \| `path?` | `frames, duration_frames, events` | no |
| `run_input_script` ² | `script?` \| `path?`, `max_frames?` | `frames_run, physics_frame, paused:true, completed, skipped` (or `cancelled` on cancel) | **yes** |
| `clear_input_script` | (none) | (none; `ok` only) | no |
| `input_script_status` | (none) | `loaded, playing, current_frame, total_frames` | no |
| `await_signal` ² | `signal, timeout_ticks, node?` \| `provider?` | `signal, fired, args` (fired) / `timed_out` (timeout) / `cancelled` (cancel), `waited_ticks, physics_frame, paused` | **yes** |
| `await_state` ² | `provider, op, timeout_ticks, path?, value?, query?` | `provider, held, actual, waited_ticks, physics_frame, paused` (`timed_out`/`cancelled` variants) | **yes** |
| `assert` | `provider, op, path?, value?, query?` | `passed, provider, path?, op, value, actual` | no |
| `seed` | `seed` | `seed` | no |

¹ `screenshot` forces a synchronous draw before replying; it does not span game ticks.

² The four **unpausing** commands (`step`, `run_input_script`, `await_signal`, `await_state`) also
accept an optional `record_signals` arg and add a `recorded` (+ `recorded_truncated`) field to
their reply; see §4.11.

---

## 6. Error codes (stable)

| `code` | meaning |
|---|---|
| `bad_json` | request line was not valid JSON |
| `frame_too_large` | request line exceeded the 1 MiB cap |
| `missing_cmd` | no `cmd` field |
| `unknown_cmd` | `cmd` not recognised |
| `bad_args` | argument missing, of the wrong type, or out of range |
| `unsupported_mode` | `step.mode` not supported by this server (e.g. `blocking` on a v1 budget-only server) |
| `unknown_provider` | `get_state.provider` not registered |
| `no_action_sink` | an `input` of type `game_action` arrived but the game registered no action sink (§4.6.3) |
| `no_seed_target` | a `seed` command arrived but the game registered no seed target (§4.6.3) |
| `capture_failed` | screenshot could not capture or save (no viewport texture, save error, headless blank) |
| `protocol_mismatch` | client `min_protocol_major` cannot be satisfied |
| `internal` | unexpected server-side error (message has detail) |

A server SHOULD keep serving after any error (it never crashes the game on a bad request).

---

## 7. Reserved for v2 (non-normative)

- **`step{mode:"blocking"}`**: Model B lockstep. The main thread parks inside
  `_physics_process` on a socket read, so the engine structurally cannot advance until the
  agent replies (frame-exact, zero-race; RL-style throughput). v1 servers may answer
  `unsupported_mode`.
- **Auth**: an optional `{"cmd":"hello","args":{"token":"…"}}` handshake gating further
  commands, for non-loopback use.
- **Subscriptions**: server-pushed `get_state` deltas or an event stream (would relax the
  "one request in flight" rule; needs an `event` envelope with no `id`).
- **`eval`**: a guarded `Expression`-based ad-hoc query, as some editor bridges offer. Omitted from v1
  on purpose (arbitrary execution); providers cover the safe cases.
- **Data-predicate awaits**: `await_signal` resolving only when the emitted args satisfy a
  predicate (e.g. `lives_changed` where `value == 0`). **Deliberately deferred** until a concrete
  need: a general predicate language risks being dead weight, and "await by name, then re-await if
  the args aren't what you wanted" already covers most cases client-side. If built, keep it a
  *narrow* equality/`in` match on the structured args (§4.10.5), not an expression engine (that
  would be `eval` by the back door).
- **Signals inside input scripts**: input-script steps that `await` a signal, destructure its
  (typed) args, and bind them for use by later steps, a small data-flow layer over the timeline.
  Powerful (reactive scripts), but a large surface: it turns the declarative §4.9.1 schema into a
  mini-language (bindings, references, conditionals). Worth it only once scripts genuinely need to
  branch on runtime data; until then, an agent composing `run_input_script` + `await_signal` +
  `get_state` in its own loop gets the same reach with no new grammar.

(Signal recording on the unpausing commands was on this list and shipped in 1.3.0 as
`record_signals`, §4.11.)

---

## 8. Worked example session (one line per message)

```jsonc
// → request                                                  ← response
{"id":1,"cmd":"ping"}
//        {"id":1,"ok":true,"protocol":"vgcp","protocol_major":1,"version":"1.5.2","server":"gdext-vgcp","paused":true,"physics_frame":0,"time_scale":1.0}
{"id":2,"cmd":"screenshot","args":{"path":"/tmp/s.png","downscale":0.5}}
//        {"id":2,"ok":true,"path":"/tmp/s.png","w":960,"h":540}
{"id":3,"cmd":"get_state","args":{"provider":"game"}}
//        {"id":3,"ok":true,"provider":"game","state":{"score":1200,"lives":3,"phase":"playing"}}
{"id":4,"cmd":"input","args":{"type":"action","action":"jump","pressed":true}}
//        {"id":4,"ok":true,"injected":"action"}
{"id":5,"cmd":"step","args":{"ticks":30}}
//        (~0.5s later) {"id":5,"ok":true,"ticks":30,"physics_frame":30,"paused":true}
{"id":6,"cmd":"await_signal","args":{"signal":"goal_reached","provider":"game","timeout_ticks":4000}}
//        (when the player reaches the goal) {"id":6,"ok":true,"signal":"goal_reached","fired":true,"args":[{"type":"int","value":1}],"waited_ticks":812,"physics_frame":842,"paused":true}
{"id":7,"cmd":"seed","args":{"seed":42}}
//        {"id":7,"ok":true,"seed":42}
{"id":8,"cmd":"await_signal","args":{"signal":"run_started","provider":"game","timeout_ticks":10}}
//        {"id":8,"ok":true,"signal":"run_started","fired":true,"args":[{"type":"int","value":42}],"waited_ticks":1,"physics_frame":843,"paused":true}
{"id":9,"cmd":"input","args":{"type":"game_action","action":"move_to","payload":{"x":300}}}
//        {"id":9,"ok":true,"injected":"game_action","action":"move_to"}
{"id":10,"cmd":"step","args":{"ticks":1}}
//        {"id":10,"ok":true,"ticks":1,"physics_frame":844,"paused":true}
```

---

## 9. Conformance checklist (for any implementation)

A conforming **server** MUST:
1. Bind loopback, speak NDJSON, enforce the 1 MiB cap.
2. Echo `id`; emit the §2.2/§2.3 envelopes; never crash on bad input.
3. Default to paused; keep the control node `PROCESS_MODE_ALWAYS`.
4. Implement `ping, pause, resume, step(budget), set_timescale, screenshot, input,
   get_state, list_providers` with the exact payloads of §4.
5. Defer the `step` reply until the ticks complete and the tree is re-paused.
6. Force or await a draw before reading the viewport in `screenshot`.
7. Provide the built-in `engine` provider and the `register_state_provider` hook.
8. *(1.1.0)* Implement the input-script commands (§4.9): `load_input_script`,
   `run_input_script` (deferred reply; cancellable by `pause`/`resume`), `clear_input_script`,
   `input_script_status`. Replay locked to physics ticks with the §4.9.3 frame alignment, reusing
   the §4.6 `input` injection code path.
9. *(1.2.0)* Implement `await_signal` (§4.10): deferred reply; required `timeout_ticks`;
   `node`/`provider` target selection; fail fast on an unknown signal; disconnect on every exit
   path; cancellable by `pause`/`resume`; and return the signal's args as **typed, structured**
   descriptors (§4.10.5), never an opaque blob.
10. *(1.3.0)* Accept the optional `record_signals` arg on `step`/`run_input_script`/`await_signal`
    (§4.11): validate and connect recorders before advancing (abort the whole command on a bad
    spec, leaving nothing connected), then attach a chronological `recorded` array (typed args,
    relative `frame`, `recorded_truncated` past the cap) to the command's reply on every exit path,
    and disconnect all recorders.
11. *(1.4.0)* Implement `await_state` (§4.12; deferred, required `timeout_ticks`, cancellable,
    `record_signals`-capable) and `assert` (§4.13; immediate, `ok:true`+`passed`) over the shared
    narrow predicate (§4.12.1), returning `actual` as a typed §4.10.5 descriptor.
12. *(1.5.0)* Implement the `game_action` input event (§4.6.1) and the `seed` command (§4.14), plus
    the four registration hooks of §4.6.3 (`register_action_sink` / `unregister_action_sink` /
    `register_seed_target` / `unregister_seed_target`, single-slot, main-thread, mirroring
    `register_state_provider`). Specifically: accept `game_action` in **both** `input` and
    input-script frames through the **one** shared injector (structural validation only, never a
    server-side action vocabulary), echo `action` in the `input` reply, map a `false` sink return to
    `bad_args`, an absent sink to **`no_action_sink`** and a freed sink to `internal`, and log and
    skip a bad `game_action` during playback (reported in `skipped` since 1.5.1, item 13); answer
    `seed` immediately (never deferred) with `{"seed": <int>}`, rejecting a non-integer with
    `bad_args`, an absent target with **`no_seed_target`** and a freed target with `internal`.
13. *(1.5.1)* Attach `skipped: [{frame, index, code, message}]` to **every** `run_input_script`
    reply (completed, cancelled, zero-frame; `[]` when clean), with one entry per event the shared
    injector refused during playback (§4.9.5). Settle `step` and `run_input_script` after the last
    tick's physics-server step, never simulating an extra tick, even when the engine runs several
    physics ticks in one iteration (§4.3).
14. *(1.5.2)* Treat `mouse_button` / `mouse_move` `x`, `y` as root-canvas coordinates: map them to
    window pixels with the root viewport's `final_transform × canvas_transform`, round to whole
    pixels, then warp and build the event (§4.6). Compare an input script's `resolution` with the
    root window's base size (§4.9.2).

A conforming **client/driver** MUST:
1. Send minified single-line JSON + `\n`; read line by line; match responses by `id`.
2. Treat `step` as long-running (wait for the matching `id`, generous timeout).
3. Gate on `protocol_major` from `ping`.

This repository ships a **mock server** ([`vgcp-mcp/mock_server.py`](../vgcp-mcp/mock_server.py))
that implements this checklist with **no Godot**, so both drivers can be validated end-to-end.
