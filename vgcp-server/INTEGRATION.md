# Integrating the Video Game Control Protocol (VGCP) server (gdext): wiring reference

This crate is the in-game server for **VGCP, the Video Game Control Protocol**, for Godot 4 games
written in Rust with gdext (the `godot` crate). It is one crate (package `vgcp-server`, lib
`vgcp_server`, `src/lib.rs`), and a game depends on it **optionally, behind a cargo feature named
`vgcp`** (`vgcp = ["dep:vgcp-server"]`), so a build without the feature neither compiles nor links
the control plane. A game never copies the server's source into its own crate.

**Activation does not use a Godot autoload.** The game's own `ExtensionLibrary::on_stage_init`
calls `vgcp_server::on_stage_init(stage)` when built with `--features vgcp`. That attaches a
`VgcpServer` named `NODE_NAME` (`"VgcpServer"`) under the root window, so it resolves at
`NODE_PATH` (`"/root/VgcpServer"`), and the game's `project.godot` stays unmodified. The server
spawns **only at `InitStage::MainLoop` and never under the editor**: `on_stage_init` returns `None`
when `Engine::is_editor_hint()` is true. The editor, `--import` and `--export-*` all run the editor
binary against the same library, and a paused tree plus a bound port there would hang an import or
export. Only the binary running the project as a game pauses the tree and binds the port.

The wire contract is [`../docs/vgcp-protocol.md`](../docs/vgcp-protocol.md), the **single source of
truth for the protocol**: this server, both drivers and the mock server all conform to it.

**Command set** (wire version **1.5.2**): `ping`, `pause`, `resume`, `step`, `set_timescale`,
`screenshot`, `input`, `get_state`, `list_providers`, the input-script commands
(`load_input_script`, `run_input_script`, `clear_input_script`, `input_script_status`),
`await_signal` (§4.10), `await_state` (§4.12), `assert` (§4.13) and `seed` (§4.14). The unpausing
commands (`step`, `run_input_script`, `await_signal`, `await_state`) also take an optional
`record_signals` argument (§4.11) that logs every emission of the watched signals during the
advance. `input` also accepts the **`game_action`** event type (§4.6.1), the canonical,
device-agnostic action record, in one-off calls and in input-script frames alike. Every
`run_input_script` reply carries `skipped: [{frame, index, code, message}]` for the scripted events
the injector refused (`[]` when none were).

**Settle timing (1.5.1, protocol §4.3).** `step` and `run_input_script` do not pause inside their
last tick, and not through a deferred call either: a deferred call runs before
`PhysicsServer2D::step`, so a kinematic body moved on the last tick would be applied a command late,
or never under `step(1)` loops. On the last tick the server sets a `settle` flag. It finishes the
settle (pause, then reply) at the top of its own `process()` in the same engine iteration, before
handling any queued request, or at the top of its next `physics_process()` if the engine runs
another physics tick first. The control node runs first among that tick's nodes, so no game node and
no physics step runs for it. A game therefore sees exactly N fully simulated ticks per `step(N)`, at
any frame rate. `await_signal` and `await_state` already pause at the top of the tick after the one
that satisfied them, so their timing is unchanged.

One thing the two settle paths do not make identical: the *node* transform of a `sync_to_physics`
body (for example an `AnimatableBody2D` platform) is copied from the physics server by a state
callback the engine delivers at the start of a tick, and only while the server is active. On the
next-tick path that callback has already run when the pause lands; on the `process()` path it runs
at the start of the first tick after the next unpause. The physics world, physics queries and every
signal are the same either way, but a node position read **while paused** (a screenshot, or a
provider field built from `get_position()` rather than from game state) can show that body one tick
behind. A provider that reports game state rather than node transforms is not affected.

---

## 0. Files in this directory

| File | Purpose |
|---|---|
| `Cargo.toml` | the crate: package `vgcp-server`, lib `vgcp_server`, `godot = "=0.5.5"` with `api-4-7` |
| `Cargo.lock` | the crate's own lockfile, for building and testing it on its own |
| `src/lib.rs` | **the server itself** (one `Node` class, `VgcpServer`) and the crate's public API: `NODE_NAME`, `NODE_PATH`, `on_stage_init` |
| `lib_registration.rs.snippet` | the consumer snippet: the optional dependency and feature, and the one-line `on_stage_init` call |
| `INTEGRATION.md` | this file (wiring and API-verification reference) |
| `README.md` | overview and design |

**The crate's API** (in `src/lib.rs`):

- `pub const NODE_NAME: &str = "VgcpServer";` the name the spawned node is given.
- `pub const NODE_PATH: &str = "/root/VgcpServer";` where a game looks the node up to register itself.
- `pub fn on_stage_init(stage: godot::init::InitStage) -> Option<Gd<VgcpServer>>`: at
  `InitStage::MainLoop`, and never under the editor, it attaches a `VgcpServer` named `NODE_NAME`
  to the scene tree's root window, logs
  `[VGCP] vgcp feature ON — VgcpServer attached to scene root`, and returns the node. At any other
  stage, under the editor, or on failure it returns `None`; the two failures log
  `[VGCP] no MainLoop at startup — server not spawned` and
  `[VGCP] MainLoop is not a SceneTree — server not spawned`.

---

## 1. Wiring a game to the crate

The steps below are what any gdext game does. You do **not** copy any `.rs` file into the game.
`lib_registration.rs.snippet` has the Cargo and `lib.rs` edits in one place.

An example layout:

```
my-game/
├─ godot/
│  ├─ project.godot                 ← unmodified: no [autoload] entry
│  └─ my_game.gdextension
└─ rust/
   ├─ Cargo.toml                    ← optional vgcp-server dependency; vgcp = ["dep:vgcp-server"]
   └─ src/lib.rs                    ← on_stage_init calls vgcp_server::on_stage_init(stage)
```

1. **Cargo.toml: an optional dependency behind a feature.** The server needs **no extra crates**
   beyond `godot` (it uses only `std::net`, `std::sync::mpsc`, `std::thread` and `std::fs`).
   Depend on it by git, pinned to a release tag (Cargo finds the package by name inside the
   repository), or by path to a local checkout:

   ```toml
   [lib]
   crate-type = ["cdylib"]

   [features]
   vgcp = ["dep:vgcp-server"]

   [dependencies]
   godot = { version = "=0.5.5", features = ["api-4-7"] }
   vgcp-server = { git = "https://github.com/Fizzixnerd/vgcp", tag = "v0.2.0", optional = true }
   # or, from a local checkout:
   # vgcp-server = { path = "../../vgcp/vgcp-server", optional = true }
   ```
   Commit the game's `Cargo.lock`.

   **One `godot-core`.** gdext registers every class in one registry inside the `godot-core`
   crate. If the game and this crate linked two copies of `godot-core`, there would be two
   registries, and `VgcpServer` would silently never register. Cargo keeps a single copy only when
   both crates take `godot` from the **same source** with **compatible versions**, so:

   - Use `godot = { version = "=0.5.5", features = ["api-4-7"] }` from crates.io, the same
     requirement as this crate.
   - Enable `api-4-7` or no `api-*` feature at all. Cargo unifies features, so this crate's
     `api-4-7` applies to the whole build, and gdext refuses two `api-*` features at once. The game
     then needs Godot 4.7 or newer at runtime.
   - A game that takes gdext **from git** must also redirect this crate's crates.io `godot` to the
     same revision with a `[patch.crates-io]` entry. Without it Cargo builds both copies, and
     nothing warns:

     ```toml
     [dependencies]
     godot = { git = "https://github.com/godot-rust/gdext", rev = "<rev>", features = ["api-4-7"] }

     [patch.crates-io]
     godot = { git = "https://github.com/godot-rust/gdext", rev = "<rev>" }
     ```
     The patched crate must still satisfy this crate's `=0.5.5` requirement. Cargo ignores a patch
     whose version does not match (it warns that the patch was not used), so for another gdext
     version depend on a local copy of this crate by path, with its `godot` requirement changed.

   Check the result with `cargo tree --features vgcp -i godot-core`: it must print exactly one
   `godot-core`.

2. **lib.rs: one call, no `mod` line.** Keep exactly one `#[gdextension] unsafe impl
   ExtensionLibrary`, and give it:

   ```rust
   use godot::prelude::*;

   struct MyGame;

   #[gdextension]
   unsafe impl ExtensionLibrary for MyGame {
       // Spawning here, not from an [autoload] entry, keeps a featureless project unmodified. The
       // call is also what links the crate: gdext registers a dependency's classes only if the
       // cdylib names the crate.
       #[cfg(feature = "vgcp")]
       fn on_stage_init(stage: godot::init::InitStage) {
           vgcp_server::on_stage_init(stage);
       }
   }
   ```
   `InitStage::MainLoop` (Godot 4.5+) runs after the SceneTree, the autoloads and the main scene
   are in the tree, so the server is added **after** the main scene: a node in that scene registers
   with it deferred from its `ready()` (§2).

3. **`.gdextension`**, if the project does not have one yet, for example `godot/my_game.gdextension`:

   ```ini
   [configuration]
   entry_symbol = "gdext_rust_init"
   compatibility_minimum = 4.7
   reloadable = true

   [libraries]
   linux.debug.x86_64     = "res://../rust/target/debug/libmy_game.so"
   linux.release.x86_64   = "res://../rust/target/release/libmy_game.so"
   windows.debug.x86_64   = "res://../rust/target/debug/my_game.dll"
   windows.release.x86_64 = "res://../rust/target/release/my_game.dll"
   macos.debug            = "res://../rust/target/debug/libmy_game.dylib"
   macos.release          = "res://../rust/target/release/libmy_game.dylib"
   ```

4. **Build and run.** In `rust/`, run `cargo build --features vgcp` (a build without the feature
   compiles the server out). Import the project once so Godot registers the extension
   (`godot --headless --path godot --import`), then run it windowed
   (`VGCP_ADDR=127.0.0.1:38787 godot --path godot`). On boot you should see
   `[VGCP] vgcp feature ON — VgcpServer attached to scene root`, then
   `[VGCP] VGCP Server v1.5.2 listening on 127.0.0.1:38787 (paused-by-default)`.
   The first import can end with `Aborted (core dumped)` (exit status 134) after it has done its
   work: check that `godot/.godot/extension_list.cfg` names your `.gdextension`, and import again
   for a clean exit.

5. **The crate's own checks** run in this directory: `cargo clippy --all-targets -- -D warnings`
   and `cargo test`. A game's clippy run does not lint this crate.

**Environment.** Every environment variable the server reads carries the `VGCP_` prefix:

- `VGCP_ADDR`: the address the server binds. Unset, it is `127.0.0.1:38787`, the protocol's default
  (protocol §1), which the drivers also use.
- `VGCP_SHOTS_DIR`: the directory a `screenshot` with no `path` writes into. Unset, it is
  `<std::env::temp_dir()>/vgcp_tmp`, so `TMPDIR` moves it. A `path` the client sends is used
  verbatim.

> **Bind on the main thread (no silent background failure):** the server binds its TCP port in
> `ready()` on the **main thread**. If it cannot bind (port in use) it logs a clear `godot_error!`
> and calls `get_tree().quit()`: a `vgcp` build **terminates itself** rather than run with no
> control port, and it never prints the misleading `listening on …` line. The accept loop then runs
> on a background thread over the already-bound listener.

---

## 2. Registering game state providers

`get_state` is extensible: the game registers `(name, target, method)` triples, and the server
calls `target.method(query)` on the main thread and serialises the returned `Dictionary`.

**From Rust.** The example below is a `GameState` node in the main scene. `on_stage_init` adds the
server **after** the main scene, so the node registers deferred from its `ready()`. The registration
and the methods it names sit in a `#[cfg(feature = "vgcp")]` `#[godot_api(secondary)]` block, so a
build without the `vgcp` feature exports none of them. A secondary block needs the class's primary
`#[godot_api]` block, and signals can only be declared in the primary one.

```rust
use godot::prelude::*;

#[derive(GodotClass)]
#[class(base = Node, init)]
pub struct GameState {
    score: i64,
    lives: i64,
    phase: GString,
    player_x: f64,
    actions: Vec<PlayerAction>,
    pending_seed: Option<u64>,
    base: Base<Node>,
}

// The primary block. Signals must be declared here.
#[godot_api]
impl GameState {
    #[signal]
    fn game_over(won: bool);
}

#[godot_api]
impl INode for GameState {
    fn ready(&mut self) {
        #[cfg(feature = "vgcp")]
        self.run_deferred(|this: &mut Self| this.register_vgcp());
    }
}

#[cfg(feature = "vgcp")]
#[godot_api(secondary)]
impl GameState {
    fn register_vgcp(&mut self) {
        // on_stage_init names the node NODE_NAME before adding it under the root window (an
        // unnamed child would get an engine-generated `@VgcpServer@<n>`), so it resolves here.
        let Some(mut server) = self
            .base()
            .try_get_node_as::<vgcp_server::VgcpServer>(vgcp_server::NODE_PATH)
        else {
            godot_error!("no VgcpServer at {}", vgcp_server::NODE_PATH);
            return;
        };
        let me = self.to_gd().upcast::<Object>();
        server.call(
            "register_state_provider",
            &["game".to_variant(), me.to_variant(), "vgcp_state".to_variant()],
        );
        // The action sink and the seed target, §2a.
        server.call("register_action_sink", &[me.to_variant(), "vgcp_action".to_variant()]);
        server.call("register_seed_target", &[me.to_variant(), "vgcp_seed".to_variant()]);
    }

    #[func]
    fn vgcp_state(&self, _query: Variant) -> VarDictionary {
        vdict! {
            "score" => self.score,
            "lives" => self.lives,
            "phase" => &self.phase,
            "player" => &vdict! { "x" => self.player_x },
        }
    }

    // vgcp_action and vgcp_seed: see §2a.
}
```

**From GDScript** (for example thin glue in a scene script). A build without the `vgcp` feature
has no server, so look it up with `get_node_or_null`:

```gdscript
func _ready() -> void:
    _register_vgcp.call_deferred()  # the server is added after the main scene

func _register_vgcp() -> void:
    var server := get_node_or_null("/root/VgcpServer")
    if server:
        server.register_state_provider("game", self, "vgcp_state")

func vgcp_state(_query) -> Dictionary:
    return {"score": score, "lives": lives, "phase": phase}
```

A game typically registers **one** provider named `game` with the facts a client reasons about
(for example `score`, `lives`, `phase` and the player's position), so the client works with
meaning rather than raw node dumps. The built-in `engine` provider (`paused`, `physics_frame`,
`process_frame`, `time_scale`, `fps`) is always present, so `get_state` works before any
registration.

---

## 2a. Registering the action sink and the seed target (1.5.0)

`get_state` lets a client **read** the game. The two hooks added in VGCP 1.5.0 let it **drive** the
game in the game's own vocabulary, and they are shaped exactly like `register_state_provider`: the
server stores a single `(target, method)` slot and calls it on the main thread. It knows nothing
about what the actions *are*; the game owns the one deserialiser.

| hook | method signature | used by |
|---|---|---|
| `register_action_sink(target, method)` | `fn(event: Dictionary) -> bool` | `input` events of type `game_action` (§4.6.1) and `game_action` frames inside an input script |
| `register_seed_target(target, method)` | `fn(seed: int) -> bool` | the `seed` command (§4.14) |

`unregister_action_sink()` and `unregister_seed_target()` drop them; a later `game_action` then
fails with `no_action_sink`, and a later `seed` with `no_seed_target`.

The §2 example registers all three surfaces in one place, `register_vgcp()`. The two methods the
hooks name go in the same gated secondary block:

```rust
/// A `game_action` record parsed into the game's own action type.
enum PlayerAction {
    MoveTo { x: f64 },
    Jump,
}

impl PlayerAction {
    /// The game's one deserialiser: `{"type": "game_action", "action": …, "payload"?: {…}}`.
    fn from_dict(event: &VarDictionary) -> Result<Self, String> {
        let action = event
            .get("action")
            .and_then(|v| v.try_to::<GString>().ok())
            .ok_or("missing action")?;
        let payload = event
            .get("payload")
            .and_then(|v| v.try_to::<VarDictionary>().ok())
            .unwrap_or_default();
        match action.to_string().as_str() {
            "move_to" => {
                let x = payload.get("x").ok_or("move_to needs x")?;
                let x = x
                    .try_to::<f64>()
                    .or_else(|_| x.try_to::<i64>().map(|i| i as f64))
                    .map_err(|_| "x must be a number")?;
                Ok(Self::MoveTo { x })
            }
            "jump" => Ok(Self::Jump),
            other => Err(format!("unknown action '{other}'")),
        }
    }
}

// Inside `#[cfg(feature = "vgcp")] #[godot_api(secondary)] impl GameState`:

/// `false` means malformed or unknown; the server replies `bad_args`. Only queue the action here;
/// the game applies it on its next tick.
#[func]
fn vgcp_action(&mut self, event: VarDictionary) -> bool {
    match PlayerAction::from_dict(&event) {
        Ok(action) => {
            self.actions.push(action);
            true
        }
        Err(e) => {
            godot_warn!("rejected a VGCP game_action: {e}");
            false
        }
    }
}

/// Stored here and applied at the top of the next physics tick, which restarts the run.
#[func]
fn vgcp_seed(&mut self, seed: i64) -> bool {
    self.pending_seed = Some(seed as u64);
    true
}
```

A successful boot logs all three:

```
[VGCP] registered state provider 'game'
[VGCP] registered action sink -> vgcp_action()
[VGCP] registered seed target -> vgcp_seed()
```

**Re-entrancy note.** The server calls the sink and the seed target from its own `process()` or
`physics_process()`, that is, from the control node's frame, never from inside the game node's own
`bind_mut()`. Both methods can therefore take `&mut self` safely. Keep them **cheap and
non-reentrant**: push onto a queue or set a pending field, and return. Doing gameplay work inside
the sink would run it outside the game's own tick order and break determinism, and if it touched a
child that calls back into the game node, it would panic on aliasing.

---

## 3. Thread-safety contract (why this is correct)

- The background `vgcp-listener` and `vgcp-conn` threads use **only `std::net` and `mpsc`**. They
  never construct or touch a `Gd<T>` or the SceneTree, which satisfies gdext's hard rule (`Gd<T>`
  is `!Send` and `!Sync`; never touch the SceneTree off the main thread).
- Requests cross the boundary as a `String` and a `Sender<String>` (both `Send`).
- **All** engine calls happen in `process()` or `physics_process()` (main thread).
- `godot_print!` and `godot_error!` are main-thread only; background threads log with `eprintln!`.

---

## 4. API verification status (gdext 0.5.3 / Godot 4.6, then 0.5.5 / 4.7)

The API calls below were verified against the gdext `v0.5.3` source (`godot-core/src/...`) and
`docs.rs/godot/0.5.3`. The crate now builds against gdext 0.5.5 with `api-4-7` (Godot 4.7). One
break since 0.5.3 is known: `SceneTree::get_root()` now returns `Gd<Window>` rather than
`Option<Gd<Window>>`. Check any other signature that matters against the 0.5.5 documentation.

### Confirmed (source- or docs-verified)
- `#[derive(GodotClass)]`, `#[class(base=Node)]`, `impl INode { fn init(base: Base<Node>) -> Self; fn ready(&mut self); fn process(&mut self, delta: f64); fn physics_process(&mut self, delta: f64) }`: canonical `f64` delta (itest and docs.rs `INode`).
- `self.base()` / `self.base_mut()` deref to `Node` methods (itest `base_test.rs`, `call_deferred_test.rs`).
- `Node::get_tree() -> Gd<SceneTree>` (panics if outside the tree; the spawned node always is inside it); `get_tree_or_null()` is the `Option` form.
- `Node::set_process_mode(ProcessMode)`; `godot::classes::node::ProcessMode::ALWAYS`.
- `SceneTree::set_pause(&mut self, bool)`, `is_paused() -> bool`.
- `Node::get_viewport() -> Option<Gd<Viewport>>` is **nullable** (the screenshot path must `let Some(viewport) = … else { … }`). Godot 4.6 marks an object return non-null only when its JSON `meta == "required"`, and this method's return has no such meta. **Verified against the Godot 4.6.3 `extension_api.json` dump (`return_value.meta == null`) and gdext codegen `type_conversions.rs:310` (`is_nullable = meta.is_none_or(|m| m != "required")`).** `Viewport::get_texture() -> Option<Gd<ViewportTexture>>` and `Texture2D::get_image() -> Option<Gd<Image>>` follow the same rule.
- `Image::save_png(impl AsArg<GString>) -> Error`; `get_width()/get_height() -> i32`; `resize(i32, i32)`.
- `RenderingServer::singleton()`, `force_draw()`.
- `Input::singleton()`, `action_press(impl AsArg<StringName>)`, `action_release(...)`, `parse_input_event(impl AsArg<Gd<InputEvent>>)`, `warp_mouse(Vector2)`.
- `&Gd<Derived>` → `AsArg<Gd<Base>>` (so `parse_input_event(&ev)` compiles): `as_arg.rs:147`.
- `&String`/`&str` → `AsArg<GString>`/`AsArg<StringName>` (so `json.parse(&line)` and `save_png(&path)` compile): `as_arg.rs:24`.
- `InputEventKey::new_gd()` with `set_keycode(Key)`, `set_physical_keycode(Key)`, `set_pressed(bool)`; `InputEventMouseButton` with `set_button_index(MouseButton)`, `set_position(Vector2)`, `set_pressed(bool)`; `InputEventMouseMotion::new_gd()` with `set_position(Vector2)`.
- `godot::global::{Key, MouseButton, Error}`; `EngineEnum::from_ord(i32) -> Self`.
- `Engine::singleton()`, `set_time_scale(f64)`, `get_time_scale() -> f64`, `get_physics_frames()/get_process_frames() -> u64`, `get_frames_per_second() -> f64`.
- `Json::new_gd()`, `parse(impl AsArg<GString>) -> Error`, `get_data() -> Variant`, and the **static** `Json::stringify(&Variant) -> GString` (compact and single-line, which suits NDJSON).
- Builtins: `VarDictionary` (= `Dictionary<Variant,Variant>`) `::new()`, `.get("k") -> Option<Variant>`, `.set(key: impl AsArg<K>, value: impl AsArg<V>)` (`dictionary.rs:315`; **see the `AsArg<Variant>` caveat below**); `VarArray` (= `Array<Variant>`) collectible from a `Variant` iterator (`FromIterator`, `array.rs:1390`); `Variant::try_to::<T>()`, `Variant::nil()`, `value.to_variant()`.
- `Gd::is_instance_valid()` (`gd.rs:308`); `Object::call(impl AsArg<StringName>, &[Variant]) -> Variant` (itest `func_test.rs`).
- `#[func]` methods accepting `StringName` and `Gd<Object>` parameters.

#### `Dictionary::set` and `AsArg<Variant>`: owned collection and Variant values need `&`
For a `VarDictionary` both key and value are typed `Variant`, so each must be `AsArg<Variant>`.
In 0.5.3 (`meta/args/as_arg.rs`) that trait is implemented for:
- **ByValue** types **by value**: `i32`/`i64`/`f64`/`bool`, `&str`, `String`, enums, `Vector2`, … (the blanket `impl<T: ToGodot<Pass=ByValue>> AsArg<Variant> for T`, line 793). So `d.set("ok", true)`, `d.set("server", "gdext-vgcp")` and `d.set("w", x as i64)` are fine **as is**.
- **By reference only**: `&Variant` (line 122; an owned `Variant` has `Pass = ByVariant` and is **excluded** from the ByValue blanket), and `&GString`/`&StringName`/`&Array<T>`/`&Dictionary<K,V>`/`&Gd<T>` (the `impl_asarg_variant_for_ref!` macro, lines 819 to 827).

Consequence: **owned** `Variant`, `VarDictionary` and `VarArray` values do **not** compile when passed by value to `set`. Pass a reference (`d.set("error", &err)`, `d.set("state", &state)`, `d.set("providers", &names)`, `d.set("id", id)` where `id: &Variant`), or wrap the value with `godot::meta::owned_into_arg(v)`. This server passes references throughout (see `ok_dict`, `send_err`, `cmd_get_state`, `cmd_list_providers`). It is the most common operation in the file.

### Items that needed a build or a live run
1. **`RenderingServer::force_draw()` produces a fresh capture mid-pause.** The engine does not
   document this; it is empirical, and live runs confirm it. If a stale frame ever appears while
   paused, switch to capturing on the next idle frame: set a `capture_pending` flag in
   `screenshot`, and read the viewport image and reply in the following `process()` (which runs
   after the draw), or connect a one-shot `RenderingServer::frame_post_draw` signal through a
   `Callable`.
2. **`ExActionPress::strength` argument type (resolved by `cargo check`):** it takes **`f32`**, not
   `f64`. The server calls `.strength(strength as f32).done()`.

The first compile against gdext 0.5.3 and Godot 4.6 needed only two fixes: `strength: f32` (above)
and dropping an unused `Image` import. Every other API call matched the source as written.

---

## 5. Headless caveat (screenshots)

`--headless` uses the dummy renderer, which draws no frame, so `screenshot` **fails** with
`capture_failed` ("could not read viewport image"). Run the game **windowed** on a real display,
or on a **virtual display** such as Xvfb with a software Vulkan driver (Mesa lavapipe). A hardware
Vulkan driver generally cannot present to Xvfb, so a virtual display needs the software driver.
[`../virtual-display/launch_game.sh`](../virtual-display/launch_game.sh) sets all of this up and
launches the game; [`../docs/virtual-display.md`](../docs/virtual-display.md) lists the packages to
install and how to troubleshoot.

---

## 6. Smoke test once the toolchain is installed

Run these from the directory that holds `vgcp-server/` and `vgcp-mcp/`, with `GAME` set to a game
laid out as in §1:

```bash
GAME=path/to/my-game

# 1. Build, import once, and run the game windowed (on a real or virtual display).
( cd "$GAME/rust" && cargo build --features vgcp )
godot --headless --path "$GAME/godot" --import    # a first import may abort after its work (§1, step 4)
VGCP_ADDR=127.0.0.1:38787 godot --path "$GAME/godot" &

# 2. Wait for the server to listen: control.py connects once and does not retry.
for _ in $(seq 150); do python3 vgcp-mcp/control.py ping >/dev/null 2>&1 && break; sleep 0.2; done

# 3. Drive it with the stdlib-only Python driver (no MCP, no dependencies):
python3 vgcp-mcp/control.py ping
python3 vgcp-mcp/control.py screenshot --path /tmp/s.png
python3 vgcp-mcp/control.py step --ticks 30
python3 vgcp-mcp/control.py get_state
```

Without Godot, validate the wire protocol and both drivers against the mock server:
`bash vgcp-mcp/selftest.sh` (see [`../vgcp-mcp/README.md`](../vgcp-mcp/README.md)).
