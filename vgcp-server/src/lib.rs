//! VGCP Server: the in-game server for the Video Game Control Protocol (VGCP), for Godot 4 games
//! written in Rust with gdext.
//!
//! Implements **VGCP v1** (the contract is `../docs/vgcp-protocol.md`): a localhost TCP / NDJSON
//! server that lets an agent or a test runner pause the game by default, advance it frame-exactly
//! with `step(n)`, capture screenshots after a real frame draw, inject input (device events and
//! canonical `game_action` records), replay input scripts, await signals and state predicates,
//! fix the run's seed, and query state providers the game registers.
//!
//! Architecture:
//!   * A gdext `Node` ([`VgcpServer`]) attached to the scene tree's root window at startup by
//!     [`on_stage_init`], running with `PROCESS_MODE_ALWAYS` so it keeps servicing the socket
//!     while the rest of the SceneTree is paused.
//!   * A background `std::net` thread accepts connections and reads NDJSON lines. It
//!     **never touches the SceneTree** — it hands each request line to the main thread
//!     over an `mpsc` channel together with a per-request reply channel.
//!   * `process()` (main thread) drains the channel and executes commands.
//!   * `physics_process()` (main thread) runs the `step` frame-budget counter and
//!     re-pauses + replies when the budget hits zero.
//!
//! ## Wiring a game to it
//!
//! 1. Depend on this crate **optionally, behind a cargo feature** (by git or by path), so a build
//!    without the feature compiles the control plane out entirely and ships no debug port:
//!
//!    ```toml
//!    [dependencies]
//!    vgcp-server = { git = "https://github.com/Fizzixnerd/vgcp", tag = "v0.1.0", optional = true }
//!    # or: vgcp-server = { path = "…/vgcp-server", optional = true }
//!
//!    [features]
//!    vgcp = ["dep:vgcp-server"]
//!    ```
//!
//! 2. Call [`on_stage_init`] from the game's `ExtensionLibrary::on_stage_init`, under that
//!    feature. It spawns the server at `InitStage::MainLoop` (never under the editor), with no
//!    `[autoload]` entry, so a featureless project is unmodified. The call also links this
//!    crate: gdext registers a dependency's classes only if the cdylib names the crate.
//!
//!    ```ignore
//!    #[cfg(feature = "vgcp")]
//!    fn on_stage_init(stage: godot::init::InitStage) {
//!        vgcp_server::on_stage_init(stage);
//!    }
//!    ```
//!
//! 3. From the game, look the node up at [`NODE_PATH`] and publish the game through
//!    `Object::call`: `register_state_provider(name, target, method)` (`target.method(query)`
//!    returns a Dictionary), `register_action_sink(target, method)` (`method(event) -> bool`,
//!    fed every `game_action`) and `register_seed_target(target, method)`
//!    (`method(seed: int) -> bool`, fed the `seed` command).
//!
//! **The game must use exactly the same `godot` crate version (and API feature) as this crate**
//! (here `godot = "=0.5.5"`, `api-4-7`): gdext registers every class in one godot-core registry,
//! and a second copy of godot-core would be a second registry in which `VgcpServer` silently
//! never registers.
//!
//! See `INTEGRATION.md` beside this crate for the integration notes, and
//! `../docs/vgcp-protocol.md` for the protocol itself.

use godot::classes::node::ProcessMode;
use godot::classes::{
    Engine, INode, Input, InputEventKey, InputEventMouseButton, InputEventMouseMotion,
    Json, Node, RenderingServer,
};
use godot::builtin::{Callable, VarArray, VarDictionary};
use godot::global::{Error, Key, MouseButton};
use godot::prelude::*;

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::mpsc::{channel, Receiver, Sender};

/// VGCP wire identity.
const PROTOCOL_NAME: &str = "vgcp";
const PROTOCOL_MAJOR: i64 = 1;
const PROTOCOL_VERSION: &str = "1.5.2";
const DEFAULT_ADDR: &str = "127.0.0.1:38787";
/// Subdirectory of the system temp dir a screenshot lands in when neither the client nor
/// `VGCP_SHOTS_DIR` names a path.
const DEFAULT_SHOTS_SUBDIR: &str = "vgcp_tmp";
/// Maximum accepted request-line length (bytes). Larger lines are rejected pre-parse.
const MAX_LINE: usize = 1024 * 1024;
/// Process priority for the control node. Negative = runs *first* among same-tick nodes
/// (Godot processes lower priority earlier). The node is added to the root window AFTER the
/// main scene (so it is the last child), but for **input-script frame alignment** it must run
/// its `physics_process` *before* the game's nodes — so it injects a frame's events before the
/// game observes that tick. See the protocol doc, "Input-script frame alignment".
const CONTROL_PROCESS_PRIORITY: i32 = -1_000_000;

/// The name the server node is given when [`on_stage_init`] attaches it to the root window.
pub const NODE_NAME: &str = "VgcpServer";
/// The server node's absolute path, where a game looks it up to register its providers.
pub const NODE_PATH: &str = "/root/VgcpServer";

/// Spawn the control plane: call this from the game's `ExtensionLibrary::on_stage_init`.
///
/// At `InitStage::MainLoop`, and never under the editor, it attaches a [`VgcpServer`] named
/// [`NODE_NAME`] to the scene tree's root window, logs it, and returns the node. At any other
/// stage, under the editor, or on failure, it returns `None`.
///
/// Spawning here rather than from an `[autoload]` entry means the game's `project.godot` needs no
/// edit, so a build without the control plane boots the bare project with no debug port.
///
/// Timing: `InitStage::MainLoop` (Godot 4.5+) is the engine's startup callback, invoked
/// from `Main::start()` AFTER the SceneTree, autoloads, and the main scene are already in
/// the tree (verified against Godot 4.6.3 `main/main.cpp`: SceneTree at ~L4263, autoloads
/// ~L4464, main scene `add_current_scene` ~L4669, then `GDExtensionManager::startup()`
/// ~L4759). So the root Window exists and adding a child here is safe and immediate.
pub fn on_stage_init(stage: godot::init::InitStage) -> Option<Gd<VgcpServer>> {
    use godot::classes::SceneTree;

    if stage != godot::init::InitStage::MainLoop {
        return None;
    }
    // NEVER spawn the control plane under the EDITOR. The editor binary is also what runs a
    // headless import (`godot4 --headless --import`) and a headless export
    // (`godot4 --headless --export-release …`), and the server's `ready()` does two things that
    // are fatal there: it calls `SceneTree::set_pause(true)` — which stalls the editor's own main
    // loop, so the import or export never finishes — and it binds the VGCP TCP port, squatting it
    // for every other tool on the box. `is_editor_hint()` is true for the editor, an import and an
    // export, and false when this same binary runs the project as a game, which is the only case
    // the control plane is for.
    if Engine::singleton().is_editor_hint() {
        return None;
    }
    // Engine is a Core-level singleton, available from this stage onward.
    let Some(main_loop) = Engine::singleton().get_main_loop() else {
        godot_error!("[VGCP] no MainLoop at startup — server not spawned");
        return None;
    };
    let Ok(tree) = main_loop.try_cast::<SceneTree>() else {
        godot_error!("[VGCP] MainLoop is not a SceneTree — server not spawned");
        return None;
    };
    // gdext 0.5.5: SceneTree::get_root() returns a non-Option `Gd<Window>` (it was
    // `Option<Gd<Window>>` through 0.5.3) — the root window always exists at this stage.
    let mut root = tree.get_root();
    // Instantiate the gdext-registered control node and parent it to the window root.
    // Its `ready()` flips PROCESS_MODE_ALWAYS, pauses the tree (pause-by-default), and
    // opens the TCP NDJSON port.
    let mut server = VgcpServer::new_alloc();
    // Name it EXPLICITLY: a child added with an empty name gets an engine-generated
    // `@VgcpServer@<n>`, and then the documented absolute path NODE_PATH (which a game's
    // registration lookup and every `node:`-addressed VGCP call use) would not resolve.
    server.set_name(NODE_NAME);
    root.add_child(&server);
    godot_print!("[VGCP] vgcp feature ON — VgcpServer attached to scene root");
    Some(server)
}

/// A request handed from a socket thread to the main thread, with a one-shot reply channel.
struct VgcpRequest {
    line: String,
    reply: Sender<String>,
}

/// A parsed, validated **input script** (VGCP §4.9): a frame-by-frame schedule of input events
/// replayed deterministically, one frame per physics tick. Reusable (a `run` clones it).
#[derive(Clone)]
struct InputScript {
    /// Authored viewport resolution in pixels, `[w, h]` (for the portability check + scaling).
    authored_w: i64,
    authored_h: i64,
    /// If true, rescale mouse coordinates to the live viewport during playback.
    scale: bool,
    /// Frames sorted ascending by relative frame index. Each entry is `(frame_index, events)`,
    /// where `events` is a `VarArray` of per-event `VarDictionary` objects in the **exact**
    /// `input`-command arg shape. Multiple entries may share a frame index (played in order).
    frames: Vec<(i64, VarArray)>,
    /// `max(frame_index) + 1` — the number of physics ticks a full play advances.
    duration_frames: i64,
    /// Total event count across all frames.
    total_events: i64,
}

/// In-flight scripted playback — the input-script analogue of `step`'s `frame_budget` /
/// `pending_step` machinery. Present only while a `run_input_script` is draining.
struct Playback {
    /// Frames to play (sorted by index), shared from the loaded/inline script.
    frames: Vec<(i64, VarArray)>,
    /// Index into `frames` of the next entry to inject.
    cursor: usize,
    /// Physics ticks elapsed so far (== the current relative frame index).
    frames_run: i64,
    /// Total ticks to advance (script duration, capped by `max_frames`).
    frames_to_run: i64,
    /// Echoed request id for the deferred reply.
    id: Variant,
    /// Reply channel for the deferred reply.
    reply: Sender<String>,
    /// `Input.use_accumulated_input` value to restore when playback ends.
    prev_accumulated: bool,
    /// Mouse-coordinate scale factors `(sx, sy)`; `None` = play 1:1.
    scale: Option<(f32, f32)>,
    /// Events the shared injector refused so far, in playback order (VGCP 1.5.1 §4.9.5). Surfaced
    /// as the reply's `skipped` on every exit path, so a dropped frame is never silent.
    skipped: Vec<SkippedEvent>,
}

/// One scripted event playback could not inject (VGCP 1.5.1 §4.9.5 `skipped[]`). Plain Rust (no
/// Godot types), so the mapping from an [`InjectError`] is covered by `cargo test`.
#[derive(Debug, Clone, PartialEq)]
struct SkippedEvent {
    /// Relative frame index of the event (§4.9.1).
    frame: i64,
    /// Position among that frame's events in playback order (entries sharing a frame index count
    /// as one run of events).
    index: i64,
    /// The wire code the `input` command would have answered for the same event (§4.6.2).
    code: &'static str,
    /// Human-readable reason.
    message: String,
}

impl SkippedEvent {
    fn new(frame: i64, index: i64, err: &InjectError) -> Self {
        let (code, message) = err.wire();
        Self {
            frame,
            index,
            code,
            message,
        }
    }

    fn to_dict(&self) -> VarDictionary {
        let mut d = VarDictionary::new();
        d.set("frame", self.frame);
        d.set("index", self.index);
        d.set("code", self.code);
        d.set("message", self.message.as_str());
        d
    }
}

/// The wire `skipped` array for a playback reply (`[]` when every event landed).
fn skipped_array(skipped: &[SkippedEvent]) -> VarArray {
    skipped.iter().map(|e| e.to_dict().to_variant()).collect()
}

/// A `step` or `run_input_script` whose last tick has run but which has not yet re-paused and
/// replied (VGCP 1.5.1 settle rule, §4.3). The last tick must be simulated in full, including the
/// physics server's step that follows the nodes' `physics_process`, so the pause cannot happen
/// inside that tick. It is finished at whichever comes first: this node's `process()` in the same
/// engine iteration (after the physics server has stepped), or the top of this node's next
/// `physics_process()` when the engine runs a further physics tick in the same iteration (this
/// node runs first, so the pause lands before any game node or the physics server runs that tick).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Settle {
    Step,
    Playback,
}

/// In-flight `await_signal` (VGCP §4.10) — the signal analogue of `step`'s deferred-reply
/// machinery. Present only while the server is advancing the world watching for one Godot signal.
struct PendingAwait {
    /// The object whose signal we connected to (kept so we can `disconnect` on finish/cancel).
    target: Gd<godot::classes::Object>,
    /// The awaited signal name (for `disconnect` + echo in the reply).
    signal: StringName,
    /// The exact `Callable` we connected. Disconnect uses *this same instance* — a `from_fn`
    /// callable only compares equal to a clone of itself (gdext), so we never rebuild it.
    callable: Callable,
    /// Shared one-shot slot the capture closure writes the signal's args into. `take()`n by the
    /// main thread's `service_await`. `Rc<RefCell<…>>` is sound here because the closure (run
    /// during the *emitter's* tick) and the poll (run during *our* earlier-priority tick) never
    /// borrow it at the same instant — both are main-thread and temporally disjoint.
    cell: Rc<RefCell<Option<VarArray>>>,
    /// `Engine.get_physics_frames()` captured at arm time — the baseline for elapsed/timeout.
    start_frame: i64,
    /// Required, non-optional tick budget: re-pause + `timed_out` once this many physics ticks
    /// pass without the signal (plus a 1-tick detection grace; see `service_await`).
    timeout_ticks: i64,
    /// Echoed request id for the deferred reply.
    id: Variant,
    /// Reply channel for the deferred reply.
    reply: Sender<String>,
}

/// How an in-flight `await_signal` ended (drives the deferred reply payload in `finish_await`).
enum AwaitEnd {
    /// The signal fired; carries its captured arguments (any arity, possibly empty).
    Fired(VarArray),
    /// The tick budget elapsed before the signal fired.
    TimedOut,
}

/// In-flight `await_state` (VGCP §4.12) — the state-predicate analogue of `await_signal`. The
/// predicate is re-evaluated against a provider's `get_state` every physics tick; the world
/// re-pauses + replies when it holds (`held:true`) or `timeout_ticks` ticks pass (`timed_out`).
struct PendingAwaitState {
    /// Registered provider name to poll each tick (re-resolved so a freed target degrades to nil).
    provider: String,
    /// Opaque query forwarded to the provider (as in `get_state`).
    query: Variant,
    /// Dotted path into the provider's returned value (`None` = the whole value).
    path: Option<String>,
    /// Comparison operator (validated against `PREDICATE_OPS` at arm time).
    op: String,
    /// Right-hand literal (ignored by `exists`/`truthy`).
    value: Variant,
    /// `Engine.get_physics_frames()` at arm time — the elapsed/timeout baseline.
    start_frame: i64,
    /// Required, non-optional tick budget (no infinite wait).
    timeout_ticks: i64,
    /// Echoed request id for the deferred reply.
    id: Variant,
    /// Reply channel for the deferred reply.
    reply: Sender<String>,
}

/// How an in-flight `await_state` ended (drives the deferred reply in `finish_await_state`).
enum AwaitStateEnd {
    /// The predicate held; carries the matching `actual` value (typed in the reply).
    Held(Variant),
    /// The tick budget elapsed; carries the last-polled `actual` value.
    TimedOut(Variant),
}

/// The fixed comparison operators a predicate (`assert` / `await_state`) may use — a comparison
/// vocabulary, deliberately NOT an expression engine (richer logic graduates to a Python test).
const PREDICATE_OPS: &[&str] = &["eq", "ne", "lt", "le", "gt", "ge", "in", "contains", "exists", "truthy"];

/// Max number of emissions a single recording session keeps before it stops appending (a memory
/// backstop for `record_signals` over a long advance watching chatty signals). Truncation is
/// reported via `recorded_truncated:true`, never silent.
const RECORD_CAP: usize = 10_000;

/// Chronological emission log shared by every recorder closure in a [`RecordingSession`].
struct RecordLog {
    /// Each entry is a ready `{signal, frame, args}` descriptor dict (see `arm_recording`).
    entries: Vec<VarDictionary>,
    /// Set once the log hits [`RECORD_CAP`] and further emissions are dropped.
    truncated: bool,
}

/// An opt-in `record_signals` session (VGCP §4.11): while an unpausing command (`step` /
/// `run_input_script` / `await_signal`) advances the world, connected recorders append every
/// emission of the watched signals to a shared log, surfaced as `recorded` in that command's
/// reply. Mutually-exclusive ops mean at most one session is ever live.
struct RecordingSession {
    /// Connected recorders to disconnect on teardown: `(object, signal_name, callable)`.
    recorders: Vec<(Gd<godot::classes::Object>, StringName, Callable)>,
    /// Shared emission log. `Rc<RefCell<…>>` is sound for the same reason as `PendingAwait.cell`:
    /// pushes happen during the emitter's tick, the drain during teardown — never concurrently.
    log: Rc<RefCell<RecordLog>>,
}

#[derive(GodotClass)]
#[class(base=Node)]
pub struct VgcpServer {
    base: Base<Node>,
    /// Receives request lines from the socket thread(s). `None` until `ready()`.
    cmd_rx: Option<Receiver<VgcpRequest>>,
    /// Remaining physics ticks to run for the in-flight `step`. 0 = idle.
    frame_budget: i64,
    /// Ticks requested by the in-flight `step` (for the reply payload).
    step_ticks: i64,
    /// Deferred reply for the in-flight `step`: (echoed id, reply channel).
    pending_step: Option<(Variant, Sender<String>)>,
    /// Registered state providers: name -> (target object, method name).
    providers: HashMap<String, (Gd<godot::classes::Object>, StringName)>,
    /// The game's registered **action sink** (VGCP §4.6.3): `target.method(event) -> bool`,
    /// handed every `game_action` input event. Single slot — the game owns one queue. `None`
    /// until registered, which is what `no_action_sink` reports.
    action_sink: Option<(Gd<godot::classes::Object>, StringName)>,
    /// The game's registered **seed target** (VGCP §4.6.3): `target.method(seed: int) -> bool`,
    /// handed the `seed` command's value. Single slot; `None` → `no_seed_target`.
    seed_target: Option<(Gd<godot::classes::Object>, StringName)>,
    /// Monotonic counter for auto-named screenshots.
    shot_counter: u64,
    /// The currently-loaded input script (via `load_input_script`), if any. `None` until loaded.
    loaded_script: Option<InputScript>,
    /// In-flight scripted playback (`run_input_script`). `None` when idle.
    playback: Option<Playback>,
    /// In-flight `await_signal` (VGCP §4.10). `None` when idle.
    pending_await: Option<PendingAwait>,
    /// In-flight `await_state` (VGCP §4.12). `None` when idle.
    pending_await_state: Option<PendingAwaitState>,
    /// Opt-in `record_signals` session (VGCP §4.11) attached to whichever unpausing command is
    /// running. `None` when not recording. Drained into the command's reply on completion/cancel.
    recording: Option<RecordingSession>,
    /// A drained `step` / playback waiting to re-pause after the physics server's step (§4.3
    /// settle rule). `None` when nothing is settling.
    settle: Option<Settle>,
}

#[godot_api]
impl INode for VgcpServer {
    fn init(base: Base<Node>) -> Self {
        Self {
            base,
            cmd_rx: None,
            frame_budget: 0,
            step_ticks: 0,
            pending_step: None,
            providers: HashMap::new(),
            action_sink: None,
            seed_target: None,
            shot_counter: 0,
            loaded_script: None,
            playback: None,
            pending_await: None,
            pending_await_state: None,
            recording: None,
            settle: None,
        }
    }

    fn ready(&mut self) {
        // Keep running while the rest of the tree is paused.
        self.base_mut().set_process_mode(ProcessMode::ALWAYS);

        // Run FIRST among same-tick nodes so scripted input injected in `physics_process` lands
        // before the game's nodes observe that tick (input-script frame alignment).
        self.base_mut().set_process_priority(CONTROL_PROCESS_PRIORITY);
        self.base_mut()
            .set_physics_process_priority(CONTROL_PROCESS_PRIORITY);

        // Pause-by-default: the agent only ever observes a frozen world.
        let mut tree = self.base().get_tree(); // VERIFY: get_tree() -> Gd<SceneTree> (panics if not in tree; an autoload always is)
        tree.set_pause(true);

        let addr = std::env::var("VGCP_ADDR").unwrap_or_else(|_| DEFAULT_ADDR.to_string());

        // Bind on the MAIN thread so a failure is handled HERE, synchronously, instead of dying on a
        // background thread while the game keeps running with no control port — the "zombie holds the
        // port, the new game can't bind but runs anyway, and the agent silently drives the zombie"
        // trap. A vgcp build is useless without its control port, so if we cannot own it we
        // QUIT (and never print the misleading "listening" line).
        let listener = match TcpListener::bind(&addr) {
            Ok(l) => l,
            Err(e) => {
                let port = addr.rsplit(':').next().unwrap_or("PORT");
                godot_error!(
                    "[VGCP] could NOT bind {addr}: {e}. Another vgcp game (or process) \
                     already holds this port — this instance is QUITTING so it can't be mistaken \
                     for a live control server. Kill the process holding {addr} (e.g. \
                     `fuser -k {port}/tcp`) or set a free VGCP_ADDR, then relaunch."
                );
                self.base().get_tree().quit();
                return;
            }
        };

        let (tx, rx) = channel::<VgcpRequest>();
        self.cmd_rx = Some(rx);

        // Background acceptor over the already-bound listener. Uses only std::net + channel -> safe
        // off the main thread.
        std::thread::Builder::new()
            .name("vgcp-listener".into())
            .spawn(move || vgcp_serve(listener, tx))
            .ok();

        godot_print!(
            "[VGCP] VGCP Server v{PROTOCOL_VERSION} listening on {addr} (paused-by-default)"
        );
    }

    fn process(&mut self, _delta: f64) {
        // Settle first (§4.3): the physics server has stepped by now, so a `step` / playback whose
        // last tick ran this iteration re-pauses here, before any queued request can observe it.
        if let Some(settle) = self.settle.take() {
            self.finish_settle(settle);
        }
        // Drain queued requests, then execute them on the main thread (SceneTree-safe).
        let mut batch: Vec<VgcpRequest> = Vec::new();
        if let Some(rx) = self.cmd_rx.as_ref() {
            while let Ok(req) = rx.try_recv() {
                batch.push(req);
            }
        }
        for req in batch {
            self.handle_request(req.line, req.reply);
        }
    }

    fn physics_process(&mut self, _delta: f64) {
        // Settle gate (§4.3): a step / playback finished on the PREVIOUS tick and the engine is
        // running another physics tick in the same iteration (low frame rate), before our
        // `process()` could settle it. Pause now: this node runs first among the tick's nodes and
        // `set_pause(true)` takes effect immediately, so no game node and no physics-server step
        // runs for this tick. Nothing else can be in flight, so there is nothing else to service.
        if let Some(settle) = self.settle.take() {
            self.finish_settle(settle);
            return;
        }

        // Frame-budget gate (VGCP `step`, Model A): consume one tick; settle at zero.
        if self.frame_budget > 0 {
            self.frame_budget -= 1;
            if self.frame_budget == 0 {
                // Do NOT pause here. This node runs first among the tick's nodes and
                // `set_pause(true)` takes effect immediately, so an inline pause would skip the
                // game's `physics_process` on this very tick (n-1 simulated ticks). A deferred
                // call at the end of the physics frame (the 1.5.0 fix) still paused BEFORE the
                // physics server's step, so kinematic transforms set on the last tick were applied
                // a command late, and under `step(1)` loops never. Settle instead: `process()`
                // or the next tick's top, whichever comes first.
                self.settle = Some(Settle::Step);
            }
        }

        // Input-script playback gate (VGCP `run_input_script`): inject this tick's events, then
        // advance; re-pause + reply when the last frame's tick has run.
        if self.playback.is_some() {
            self.advance_playback();
        }

        // `await_signal` gate (VGCP §4.10): poll the capture slot, re-pause + reply on fire or
        // timeout. Polled AFTER playback/step so all three never re-pause in the same tick.
        if self.pending_await.is_some() {
            self.service_await();
        }

        // `await_state` gate (VGCP §4.12): re-evaluate the predicate against the provider, re-pause +
        // reply when it holds or the budget elapses. (Mutual exclusion means only one is ever live.)
        if self.pending_await_state.is_some() {
            self.service_await_state();
        }
    }
}

// ---- In-engine registration API (callable from Rust or GDScript) -----------------------

#[godot_api]
impl VgcpServer {
    /// Register a named state provider. `target.method(query)` must return a Dictionary
    /// (or any Variant that `JSON.stringify` accepts). Invoked on the main thread.
    #[func]
    fn register_state_provider(
        &mut self,
        name: StringName,
        target: Gd<godot::classes::Object>,
        method: StringName,
    ) {
        self.providers.insert(name.to_string(), (target, method));
        godot_print!("[VGCP] registered state provider '{name}'");
    }

    /// Remove a previously-registered provider.
    #[func]
    fn unregister_state_provider(&mut self, name: StringName) {
        self.providers.remove(&name.to_string());
    }

    /// Register the **action sink** (VGCP §4.6.3): every `game_action` input event — from the
    /// `input` command and from input-script frames alike — is handed to `target.method(event)`,
    /// which returns `true` if the game accepted it. Single slot: registering again replaces it.
    ///
    /// The server deliberately knows nothing about the action vocabulary; the sink owns the one
    /// deserialiser, so a `false` return is the game saying "not my action / bad
    /// payload" and becomes `bad_args` on the wire.
    #[func]
    fn register_action_sink(&mut self, target: Gd<godot::classes::Object>, method: StringName) {
        godot_print!("[VGCP] registered action sink -> {method}()");
        self.action_sink = Some((target, method));
    }

    /// Drop the registered action sink (a later `game_action` then errors `no_action_sink`).
    #[func]
    fn unregister_action_sink(&mut self) {
        self.action_sink = None;
    }

    /// Register the **seed target** (VGCP §4.6.3): the `seed` command hands its integer to
    /// `target.method(seed)`, which returns `true` if the game accepted it. Single slot.
    /// *When* the seed takes effect is the game's business (a game may apply it at the top of its
    /// next physics tick, for example), which is why `seed` never advances the world itself.
    #[func]
    fn register_seed_target(&mut self, target: Gd<godot::classes::Object>, method: StringName) {
        godot_print!("[VGCP] registered seed target -> {method}()");
        self.seed_target = Some((target, method));
    }

    /// Drop the registered seed target (a later `seed` then errors `no_seed_target`).
    #[func]
    fn unregister_seed_target(&mut self) {
        self.seed_target = None;
    }
}

// ---- Command dispatch (main thread) ----------------------------------------------------

impl VgcpServer {
    /// Finish a settling `step` / playback (§4.3): re-pause and send its deferred reply.
    fn finish_settle(&mut self, settle: Settle) {
        match settle {
            Settle::Step => self.finish_step(),
            Settle::Playback => self.finish_playback_completed(),
        }
    }

    /// End a drained `step`: pause and send the deferred reply. Called when the step settles
    /// (see `Settle`), after the last tick's physics-server step. A `resume` that cancelled the
    /// step in the meantime took `pending_step`, so this is then a no-op.
    fn finish_step(&mut self) {
        let Some((id, reply)) = self.pending_step.take() else {
            return;
        };
        self.base().get_tree().set_pause(true);
        let mut d = ok_dict(&id);
        d.set("ticks", self.step_ticks);
        d.set("physics_frame", Engine::singleton().get_physics_frames() as i64);
        d.set("paused", true);
        self.attach_recorded(&mut d);
        send_dict(&reply, d);
    }

    fn handle_request(&mut self, line: String, reply: Sender<String>) {
        // Parse with Godot's own JSON (no extra crates). Instance form gives us error detection.
        let mut json = Json::new_gd();
        if json.parse(&line) != Error::OK {
            return send_err(&reply, &Variant::nil(), "bad_json", "invalid JSON");
        }
        let data = json.get_data();
        let Ok(dict) = data.try_to::<VarDictionary>() else {
            return send_err(
                &reply,
                &Variant::nil(),
                "bad_json",
                "top-level value must be a JSON object",
            );
        };

        let id = dict.get("id").unwrap_or(Variant::nil());
        let cmd = match dict.get("cmd").and_then(|v| v.try_to::<GString>().ok()) {
            Some(c) => c.to_string(),
            None => return send_err(&reply, &id, "missing_cmd", "no 'cmd' field"),
        };
        let args = dict
            .get("args")
            .and_then(|v| v.try_to::<VarDictionary>().ok())
            .unwrap_or_else(VarDictionary::new);

        match cmd.as_str() {
            "ping" => self.cmd_ping(&reply, &id, &args),
            "pause" => self.cmd_pause(&reply, &id),
            "resume" => self.cmd_resume(&reply, &id),
            "step" => self.cmd_step(&reply, &id, &args, reply.clone()),
            "set_timescale" => self.cmd_set_timescale(&reply, &id, &args),
            "screenshot" => self.cmd_screenshot(&reply, &id, &args),
            "input" => self.cmd_input(&reply, &id, &args),
            "get_state" => self.cmd_get_state(&reply, &id, &args),
            "list_providers" => self.cmd_list_providers(&reply, &id),
            "load_input_script" => self.cmd_load_input_script(&reply, &id, &args),
            "run_input_script" => self.cmd_run_input_script(&reply, &id, &args, reply.clone()),
            "clear_input_script" => self.cmd_clear_input_script(&reply, &id),
            "input_script_status" => self.cmd_input_script_status(&reply, &id),
            "await_signal" => self.cmd_await_signal(&reply, &id, &args, reply.clone()),
            "await_state" => self.cmd_await_state(&reply, &id, &args, reply.clone()),
            "assert" => self.cmd_assert(&reply, &id, &args),
            "seed" => self.cmd_seed(&reply, &id, &args),
            other => send_err(&reply, &id, "unknown_cmd", &format!("unknown cmd: {other}")),
        }
    }

    fn cmd_ping(&self, reply: &Sender<String>, id: &Variant, args: &VarDictionary) {
        if let Some(min) = arg_i64(args, "min_protocol_major")
            && min > PROTOCOL_MAJOR
        {
            return send_err(
                reply,
                id,
                "protocol_mismatch",
                &format!("server speaks protocol_major {PROTOCOL_MAJOR}, client requires {min}"),
            );
        }
        let tree = self.base().get_tree();
        let eng = Engine::singleton();
        let mut d = ok_dict(id);
        d.set("protocol", PROTOCOL_NAME);
        d.set("protocol_major", PROTOCOL_MAJOR);
        d.set("version", PROTOCOL_VERSION);
        d.set("server", "gdext-vgcp");
        d.set("paused", tree.is_paused());
        d.set("physics_frame", eng.get_physics_frames() as i64);
        d.set("time_scale", eng.get_time_scale());
        send_dict(reply, d);
    }

    fn cmd_pause(&mut self, reply: &Sender<String>, id: &Variant) {
        // A `pause` mid-playback / mid-await cancels it (mirrors `resume` cancelling an in-flight
        // `step`): the waiter gets a `cancelled` reply; the world stays paused.
        self.cancel_playback(true);
        self.cancel_await(true);
        self.cancel_await_state(true);
        self.base().get_tree().set_pause(true);
        let mut d = ok_dict(id);
        d.set("paused", true);
        send_dict(reply, d);
    }

    fn cmd_resume(&mut self, reply: &Sender<String>, id: &Variant) {
        // Free-run. Per protocol §4.3, a `step` still draining when `resume` arrives is
        // *cancelled*: its deferred reply returns now with ok:true, ticks = the count actually
        // completed so far, cancelled:true, paused:false (the world is about to run freely).
        // Compute `completed` BEFORE clearing the budget.
        if let Some((sid, srep)) = self.pending_step.take() {
            let completed = (self.step_ticks - self.frame_budget).max(0);
            let mut d = ok_dict(&sid);
            d.set("ticks", completed);
            d.set("physics_frame", Engine::singleton().get_physics_frames() as i64);
            d.set("cancelled", true);
            d.set("paused", false);
            self.attach_recorded(&mut d);
            send_dict(&srep, d);
        }
        // Likewise cancel an in-flight input-script playback / await_signal / await_state (free-run).
        self.cancel_playback(false);
        self.cancel_await(false);
        self.cancel_await_state(false);
        self.frame_budget = 0;
        self.step_ticks = 0;
        self.base().get_tree().set_pause(false);
        let mut d = ok_dict(id);
        d.set("paused", false);
        send_dict(reply, d);
    }

    /// A reason string if ANY unpausing / deferred-reply operation is in flight, else `None`. The
    /// single mutual-exclusion gate for `step` / `run_input_script` / `await_signal` / `await_state`
    /// (only one may advance the world at a time).
    fn in_flight(&self) -> Option<&'static str> {
        if self.settle.is_some() {
            Some("a step or input script is settling")
        } else if self.frame_budget > 0 || self.pending_step.is_some() {
            Some("a step is in progress")
        } else if self.playback.is_some() {
            Some("an input script is playing")
        } else if self.pending_await.is_some() {
            Some("an await_signal is in progress")
        } else if self.pending_await_state.is_some() {
            Some("an await_state is in progress")
        } else {
            None
        }
    }

    fn cmd_step(
        &mut self,
        reply: &Sender<String>,
        id: &Variant,
        args: &VarDictionary,
        owned_reply: Sender<String>,
    ) {
        if let Some(why) = self.in_flight() {
            return send_err(reply, id, "bad_args", why);
        }
        let ticks = arg_i64(args, "ticks").unwrap_or(1);
        if ticks < 1 {
            return send_err(reply, id, "bad_args", "step.ticks must be >= 1");
        }
        if let Some(mode) = arg_str(args, "mode")
            && mode != "budget"
        {
            return send_err(
                reply,
                id,
                "unsupported_mode",
                &format!("step.mode '{mode}' not supported (v1 budget-only)"),
            );
        }
        // Optional signal recording for the advance window (VGCP §4.11). Arm last (nothing fallible
        // follows) so an error needs no rollback.
        if let Err((code, msg)) = self.arm_recording(args) {
            return send_err(reply, id, code, &msg);
        }
        // Unpause and arm the budget; reply is deferred until physics_process drains it.
        self.step_ticks = ticks;
        self.frame_budget = ticks;
        self.pending_step = Some((id.clone(), owned_reply));
        self.base().get_tree().set_pause(false);
        // No immediate reply.
    }

    fn cmd_set_timescale(&self, reply: &Sender<String>, id: &Variant, args: &VarDictionary) {
        let Some(value) = arg_f64(args, "value") else {
            return send_err(reply, id, "bad_args", "set_timescale.value (float) required");
        };
        if value <= 0.0 {
            return send_err(
                reply,
                id,
                "bad_args",
                "value must be > 0 (use pause to stop)",
            );
        }
        Engine::singleton().set_time_scale(value);
        let mut d = ok_dict(id);
        d.set("time_scale", value);
        send_dict(reply, d);
    }

    fn cmd_screenshot(&mut self, reply: &Sender<String>, id: &Variant, args: &VarDictionary) {
        // Force a fresh, fully-composited frame so the capture matches "now", even while paused.
        // That force_draw() yields a fresh frame mid-pause is empirical (the engine does not
        // document it); live runs confirm it.
        RenderingServer::singleton().force_draw();

        // get_viewport() -> Option<Gd<Viewport>> in gdext 0.5.3. Godot 4.6 only marks an object
        // return non-null when its JSON meta == "required"; Node.get_viewport's return has no such
        // meta (verified against the Godot 4.6.3 extension_api.json dump + gdext codegen
        // type_conversions.rs:310 `is_nullable = meta.is_none_or(|m| m != "required")`).
        let Some(viewport) = self.base().get_viewport() else {
            return send_err(reply, id, "capture_failed", "no viewport");
        };
        let Some(texture) = viewport.get_texture() else {
            return send_err(reply, id, "capture_failed", "viewport has no texture");
        };
        let Some(mut img) = texture.get_image() else {
            return send_err(reply, id, "capture_failed", "could not read viewport image");
        };

        let downscale = arg_f64(args, "downscale").unwrap_or(1.0);
        if downscale > 0.0 && downscale < 1.0 {
            let w = (img.get_width() as f64 * downscale).max(1.0) as i32;
            let h = (img.get_height() as f64 * downscale).max(1.0) as i32;
            img.resize(w, h);
        }

        let path = match arg_str(args, "path") {
            Some(p) => p,
            None => {
                let dir = shots_dir();
                let _ = std::fs::create_dir_all(&dir);
                self.shot_counter += 1;
                dir.join(format!("shot-{}.png", self.shot_counter))
                    .to_string_lossy()
                    .into_owned()
            }
        };

        // Image::save_png takes an OS path (or res://, user://) and answers with an Error. The
        // path from `shots_dir` is always absolute; a `path` the CLIENT sent is passed on
        // verbatim (protocol §4.5) — a relative one resolves against this process's cwd, which
        // is the client's problem to know, not ours to rewrite.
        let err = img.save_png(&path);
        if err != Error::OK {
            return send_err(
                reply,
                id,
                "capture_failed",
                &format!("save_png failed: {err:?} (path {path})"),
            );
        }
        let mut d = ok_dict(id);
        d.set("path", path.as_str());
        d.set("w", img.get_width() as i64);
        d.set("h", img.get_height() as i64);
        send_dict(reply, d);
    }

    fn cmd_input(&self, reply: &Sender<String>, id: &Variant, args: &VarDictionary) {
        // The per-event injection lives in the shared `inject_one_event` helper so the `input`
        // command and scripted playback inject through one code path. The action sink is passed
        // in (rather than looked up inside) so that same free fn stays callable from the
        // borrow-restricted playback loop.
        match inject_one_event(args, self.action_sink.as_ref()) {
            Ok(kind) => {
                let mut d = ok_dict(id);
                d.set("injected", kind);
                // VGCP §4.6: a game_action reply echoes the action name, so a transcript reads.
                if kind == "game_action"
                    && let Some(action) = arg_str(args, "action")
                {
                    d.set("action", action);
                }
                send_dict(reply, d);
            }
            Err(e) => {
                let (code, msg) = e.wire();
                send_err(reply, id, code, &msg);
            }
        }
    }

    /// `seed` (VGCP §4.14) — hand an integer seed to the game's registered seed target. Immediate
    /// (never deferred): the command only delivers the value, and the game applies it when it
    /// chooses (typically at its next tick), so the documented pattern is `seed`, then
    /// `await_signal` on the game's own run-start signal (for example `run_started`).
    fn cmd_seed(&self, reply: &Sender<String>, id: &Variant, args: &VarDictionary) {
        let Some(seed) = args.get("seed").and_then(|v| coerce_seed(&v)) else {
            return send_err(reply, id, "bad_args", "seed.seed (int) required");
        };
        let Some((target, method)) = self.seed_target.as_ref() else {
            return send_err(
                reply,
                id,
                "no_seed_target",
                "no seed target registered (see VGCP §4.6.3 register_seed_target)",
            );
        };
        let mut obj = target.clone();
        if !obj.is_instance_valid() {
            return send_err(reply, id, "internal", "the registered seed target was freed");
        }
        if !obj
            .call(method, &[seed.to_variant()])
            .try_to::<bool>()
            .unwrap_or(false)
        {
            return send_err(reply, id, "bad_args", "seed target refused the seed");
        }
        let mut d = ok_dict(id);
        d.set("seed", seed);
        send_dict(reply, d);
    }

    fn cmd_get_state(&self, reply: &Sender<String>, id: &Variant, args: &VarDictionary) {
        let query = args.get("query").unwrap_or(Variant::nil());
        match arg_str(args, "provider") {
            Some(name) => {
                // Both arms must be the same type. engine_state() is a VarDictionary; the provider
                // arm yields a Variant — so normalise the engine arm to Variant via to_variant().
                let state: Variant = if name == "engine" {
                    self.engine_state().to_variant()
                } else if let Some((target, method)) = self.providers.get(&name) {
                    match call_provider(target, method, &query) {
                        Some(v) => v,
                        None => {
                            return send_err(
                                reply,
                                id,
                                "internal",
                                &format!("provider '{name}' target was freed"),
                            )
                        }
                    }
                } else {
                    return send_err(
                        reply,
                        id,
                        "unknown_provider",
                        &format!("no provider named '{name}'"),
                    );
                };
                let mut d = ok_dict(id);
                d.set("provider", name.as_str());
                // Owned Variant: only &Variant implements AsArg<Variant> in 0.5.3, so pass a ref.
                d.set("state", &state);
                send_dict(reply, d);
            }
            None => {
                // Aggregate: engine + every provider, each under its name.
                let mut state = VarDictionary::new();
                // set() takes `impl AsArg<Variant>`; owned VarDictionary/VarArray/Variant do NOT
                // implement it (only &Dictionary / &Array / &Variant do), so bind + pass refs.
                let engine = self.engine_state();
                state.set("engine", &engine);
                for (name, (target, method)) in &self.providers {
                    if let Some(v) = call_provider(target, method, &query) {
                        state.set(name.as_str(), &v);
                    }
                }
                let mut d = ok_dict(id);
                d.set("state", &state);
                send_dict(reply, d);
            }
        }
    }

    fn cmd_list_providers(&self, reply: &Sender<String>, id: &Variant) {
        let names: VarArray = std::iter::once("engine".to_variant())
            .chain(self.providers.keys().map(|k| k.to_variant()))
            .collect();
        let mut d = ok_dict(id);
        d.set("providers", &names); // owned VarArray -> pass &Array (AsArg<Variant>)
        send_dict(reply, d);
    }

    // ---- Input scripts (VGCP §4.9) ------------------------------------------------------

    /// `load_input_script` — validate + store a frame-by-frame input script for later replay.
    /// Source is either inline `args.script` (an object) or `args.path` (a JSON file on disk,
    /// which lifts the 1 MiB line cap for large scripts).
    fn cmd_load_input_script(&mut self, reply: &Sender<String>, id: &Variant, args: &VarDictionary) {
        let root = match resolve_script_source(args) {
            Ok(Some(d)) => d,
            Ok(None) => {
                return send_err(
                    reply,
                    id,
                    "bad_args",
                    "load_input_script requires 'script' (object) or 'path' (file)",
                )
            }
            Err(e) => return send_err(reply, id, "bad_args", &e),
        };
        match parse_input_script(&root) {
            Ok(script) => {
                let frames = script.frames.len() as i64;
                let duration_frames = script.duration_frames;
                let events = script.total_events;
                self.loaded_script = Some(script);
                let mut d = ok_dict(id);
                d.set("frames", frames);
                d.set("duration_frames", duration_frames);
                d.set("events", events);
                send_dict(reply, d);
            }
            Err(e) => send_err(reply, id, "bad_args", &e),
        }
    }

    /// `run_input_script` — replay the loaded script (or an inline `script`/`path` override),
    /// one frame per physics tick. Like `step`, this unpauses, drains in `physics_process`, then
    /// re-pauses and sends a DEFERRED reply. Optional `max_frames` caps the playback length.
    fn cmd_run_input_script(
        &mut self,
        reply: &Sender<String>,
        id: &Variant,
        args: &VarDictionary,
        owned_reply: Sender<String>,
    ) {
        if let Some(why) = self.in_flight() {
            return send_err(reply, id, "bad_args", why);
        }
        // Resolve the script: inline override (`script`/`path`) else the loaded one.
        let parsed = match resolve_script_source(args) {
            Ok(Some(root)) => match parse_input_script(&root) {
                Ok(s) => s,
                Err(e) => return send_err(reply, id, "bad_args", &e),
            },
            Ok(None) => match &self.loaded_script {
                Some(s) => s.clone(),
                None => {
                    return send_err(
                        reply,
                        id,
                        "bad_args",
                        "no input script loaded (call load_input_script, or pass 'script'/'path')",
                    )
                }
            },
            Err(e) => return send_err(reply, id, "bad_args", &e),
        };

        // Optional max_frames cap.
        let mut frames_to_run = parsed.duration_frames;
        if let Some(mf) = arg_i64(args, "max_frames") {
            if mf < 1 {
                return send_err(reply, id, "bad_args", "max_frames must be >= 1");
            }
            frames_to_run = frames_to_run.min(mf);
        }

        // Resolution-portability check (protocol §4.9.2): warn on a mismatch; rescale only if asked.
        let (live_w, live_h) = self.live_viewport_size();
        let resolution_differs = parsed.authored_w > 0
            && parsed.authored_h > 0
            && live_w > 0
            && live_h > 0
            && (live_w != parsed.authored_w || live_h != parsed.authored_h);
        if resolution_differs {
            godot_warn!(
                "[VGCP] input script authored at {}x{} but live viewport is {}x{}; {}",
                parsed.authored_w,
                parsed.authored_h,
                live_w,
                live_h,
                if parsed.scale {
                    "rescaling mouse coordinates"
                } else {
                    "playing 1:1 (set \"scale\":true to rescale)"
                }
            );
        }
        let scale = if parsed.scale && resolution_differs {
            Some((
                live_w as f32 / parsed.authored_w as f32,
                live_h as f32 / parsed.authored_h as f32,
            ))
        } else {
            None
        };

        // Empty (or capped-to-zero) script: nothing to advance — reply immediately (no recording:
        // 0 ticks advance, so there is nothing to record).
        if frames_to_run < 1 {
            let mut d = ok_dict(id);
            d.set("frames_run", 0);
            d.set("physics_frame", Engine::singleton().get_physics_frames() as i64);
            d.set("paused", self.base().get_tree().is_paused());
            d.set("completed", true);
            d.set("skipped", &VarArray::new());
            return send_dict(reply, d);
        }

        // Optional signal recording for the playback window (VGCP §4.11). Arm before playback (the
        // remaining steps are infallible), so an error needs no rollback.
        if let Err((code, msg)) = self.arm_recording(args) {
            return send_err(reply, id, code, &msg);
        }

        // Arm playback. Turn OFF accumulated input so each `parse_input_event` dispatches
        // immediately + synchronously (no cross-tick buffering, no mouse-motion coalescing),
        // giving deterministic same-tick frame alignment. Restored when playback ends.
        let mut input = Input::singleton();
        let prev_accumulated = input.is_using_accumulated_input();
        input.set_use_accumulated_input(false);

        self.playback = Some(Playback {
            frames: parsed.frames,
            cursor: 0,
            frames_run: 0,
            frames_to_run,
            id: id.clone(),
            reply: owned_reply,
            prev_accumulated,
            scale,
            skipped: Vec::new(),
        });
        self.base().get_tree().set_pause(false);
        // No immediate reply — physics_process/advance_playback sends it when done.
    }

    /// `clear_input_script` — drop the stored script (does not affect an in-flight playback).
    fn cmd_clear_input_script(&mut self, reply: &Sender<String>, id: &Variant) {
        self.loaded_script = None;
        send_dict(reply, ok_dict(id));
    }

    /// `input_script_status` — report whether a script is loaded / playing and the cursor.
    fn cmd_input_script_status(&self, reply: &Sender<String>, id: &Variant) {
        let playing = self.playback.is_some();
        let current_frame = self.playback.as_ref().map(|p| p.frames_run).unwrap_or(0);
        let total_frames = if let Some(p) = &self.playback {
            p.frames_to_run
        } else if let Some(s) = &self.loaded_script {
            s.duration_frames
        } else {
            0
        };
        let mut d = ok_dict(id);
        d.set("loaded", self.loaded_script.is_some());
        d.set("playing", playing);
        d.set("current_frame", current_frame);
        d.set("total_frames", total_frames);
        send_dict(reply, d);
    }

    /// Advance one tick of scripted playback: inject the current frame's events (so the game's
    /// nodes — processed AFTER this node — observe them this tick), then re-pause + reply at end.
    fn advance_playback(&mut self) {
        let finished;
        {
            // Disjoint field borrows: the playback cursor is borrowed mutably, the action sink
            // immutably, so a `game_action` frame reaches the game through the same injector as
            // the `input` command (VGCP §4.9.1).
            let sink = self.action_sink.as_ref();
            let Some(pb) = self.playback.as_mut() else {
                return;
            };
            if pb.frames_run >= pb.frames_to_run {
                // Every frame has run and the deferred finish is pending: never over-run.
                return;
            }
            let f = pb.frames_run;
            // Position among this frame's events, across every entry sharing the index (§4.9.5).
            let mut index: i64 = 0;
            // Inject every event scheduled at relative frame index `f` (gaps just advance).
            while pb.cursor < pb.frames.len() && pb.frames[pb.cursor].0 == f {
                let events = pb.frames[pb.cursor].1.clone();
                let scale = pb.scale;
                pb.cursor += 1;
                for ev_v in events.iter_shared() {
                    let result = match ev_v.try_to::<VarDictionary>() {
                        Ok(ev) => inject_playback_event(&ev, scale, sink).map(|_| ()),
                        // Unreachable after `parse_input_script`, but never drop one silently.
                        Err(_) => Err(InjectError::Internal("event is not an object".to_string())),
                    };
                    if let Err(e) = result {
                        godot_warn!(
                            "[VGCP] playback frame {f}: skipped bad event ({})",
                            e.message()
                        );
                        pb.skipped.push(SkippedEvent::new(f, index, &e));
                    }
                    index += 1;
                }
            }
            pb.frames_run += 1;
            finished = pb.frames_run >= pb.frames_to_run;
        }
        // Force-dispatch any agile/buffered events so they are visible to the game THIS tick.
        Input::singleton().flush_buffered_events();
        if finished {
            // Same rule as `step` (see `physics_process` and `Settle`): an inline pause would skip
            // the game's `physics_process` on the last frame's tick, and a pause before the
            // physics server's step would leave that tick's kinematic transforms unapplied.
            // Settle after the step instead.
            self.settle = Some(Settle::Playback);
        }
    }

    /// Successful end of playback: restore input mode, re-pause, send the deferred `completed` reply.
    /// Called when the playback settles (see `Settle`); a `pause`/`resume` that cancelled the
    /// playback in the meantime took `playback`, so this is then a no-op.
    fn finish_playback_completed(&mut self) {
        let Some(pb) = self.playback.take() else {
            return;
        };
        Input::singleton().set_use_accumulated_input(pb.prev_accumulated);
        self.base().get_tree().set_pause(true);
        let mut d = ok_dict(&pb.id);
        d.set("frames_run", pb.frames_run);
        d.set("physics_frame", Engine::singleton().get_physics_frames() as i64);
        d.set("paused", true);
        d.set("completed", true);
        d.set("skipped", &skipped_array(&pb.skipped));
        self.attach_recorded(&mut d);
        send_dict(&pb.reply, d);
    }

    /// Cancel an in-flight playback (from `pause`/`resume`): restore input mode and send the
    /// deferred reply with `cancelled: true, completed: false`. `paused` reflects the new state.
    fn cancel_playback(&mut self, paused: bool) {
        let Some(pb) = self.playback.take() else {
            return;
        };
        Input::singleton().set_use_accumulated_input(pb.prev_accumulated);
        let mut d = ok_dict(&pb.id);
        d.set("frames_run", pb.frames_run);
        d.set("physics_frame", Engine::singleton().get_physics_frames() as i64);
        d.set("paused", paused);
        d.set("completed", false);
        d.set("cancelled", true);
        d.set("skipped", &skipped_array(&pb.skipped));
        self.attach_recorded(&mut d);
        send_dict(&pb.reply, d);
    }

    // ---- await_signal (VGCP §4.10) ------------------------------------------------------

    /// `await_signal` — advance the world watching for one Godot signal, with a REQUIRED tick
    /// timeout. Connects an arity-agnostic capture callable to `signal` on the target (selected
    /// by `node` path or registered `provider`), unpauses, and — like `step` — DEFERS the reply
    /// until the signal fires (`fired:true` + structured `args`) or `timeout_ticks` physics ticks
    /// elapse (`fired:false, timed_out:true`). Re-pauses either way; cancellable by `pause`/`resume`.
    fn cmd_await_signal(
        &mut self,
        reply: &Sender<String>,
        id: &Variant,
        args: &VarDictionary,
        owned_reply: Sender<String>,
    ) {
        // Mutual exclusion with the other deferred-reply operations.
        if let Some(why) = self.in_flight() {
            return send_err(reply, id, "bad_args", why);
        }

        // Required: a non-empty signal name.
        let Some(signal) = arg_str(args, "signal").filter(|s| !s.is_empty()) else {
            return send_err(reply, id, "bad_args", "await_signal.signal (non-empty string) required");
        };
        // Required + non-optional: a positive tick timeout (the whole point of this command).
        let Some(timeout_ticks) = arg_i64(args, "timeout_ticks") else {
            return send_err(reply, id, "bad_args", "await_signal.timeout_ticks (int >= 1) required");
        };
        if timeout_ticks < 1 {
            return send_err(reply, id, "bad_args", "await_signal.timeout_ticks must be >= 1");
        }

        // Resolve the target object (exactly one of `node` / `provider`).
        let target = match self.resolve_signal_target(args) {
            Ok(t) => t,
            Err((code, msg)) => return send_err(reply, id, code, &msg),
        };

        // Fail fast if the object has no such signal: a clear error is better than a silent await
        // that never fires.
        let signal_name = StringName::from(signal.as_str());
        if !target.has_signal(&signal_name) {
            return send_err(reply, id, "bad_args", &format!("target has no signal '{signal}'"));
        }

        // Capture slot + the closure that fills it. `from_fn` is a *local* (single-thread) callable
        // taking the signal's args as `&[&Variant]` of ANY arity, so it handles `game_over(bool)`,
        // `wave_started(int)`, a no-arg signal, etc. with no compile-time arity match. The closure
        // only writes the slot (no engine/self access) so it can never re-enter a `bind`/`bind_mut`.
        let cell: Rc<RefCell<Option<VarArray>>> = Rc::new(RefCell::new(None));
        let cell_for_closure = cell.clone();
        let callable = Callable::from_fn("vgcp_await_signal", move |sig_args: &[&Variant]| {
            let mut slot = cell_for_closure.borrow_mut();
            if slot.is_none() {
                // First emission wins. Capture the raw args; they are described (typed/structured)
                // later, on the main thread, in `finish_await`.
                *slot = Some(sig_args.iter().map(|v| (*v).clone()).collect());
            }
            Variant::nil()
        });

        // Connect. On failure, do NOT arm — report synchronously.
        let mut target_mut = target.clone();
        let err = target_mut.connect(&signal_name, &callable);
        if err != Error::OK {
            return send_err(
                reply,
                id,
                "internal",
                &format!("connect to signal '{signal}' failed: {err:?}"),
            );
        }

        // Optional signal recording for the await window (VGCP §4.11). Armed AFTER our own capture
        // connection — if it fails, roll that connection back so nothing is left dangling.
        if let Err((code, msg)) = self.arm_recording(args) {
            target_mut.disconnect(&signal_name, &callable);
            return send_err(reply, id, code, &msg);
        }

        let start_frame = Engine::singleton().get_physics_frames() as i64;
        self.pending_await = Some(PendingAwait {
            target,
            signal: signal_name,
            callable,
            cell,
            start_frame,
            timeout_ticks,
            id: id.clone(),
            reply: owned_reply,
        });
        // Unpause so the world advances and the signal has a chance to fire. Reply is deferred.
        self.base().get_tree().set_pause(false);
    }

    /// Resolve the object whose signal `await_signal` should watch from the command args: exactly
    /// one of `node` / `provider`. Thin wrapper over [`resolve_target`].
    fn resolve_signal_target(
        &self,
        args: &VarDictionary,
    ) -> Result<Gd<godot::classes::Object>, (&'static str, String)> {
        self.resolve_target(arg_str(args, "node"), arg_str(args, "provider"))
    }

    /// Resolve a target object from exactly one of `node` (a NodePath resolved from this node, e.g.
    /// "/root/Game") or `provider` (a registered state provider's target object — reuses the
    /// `get_state` registration). Shared by `await_signal` and `record_signals`. `Err((code, msg))`.
    fn resolve_target(
        &self,
        node: Option<String>,
        provider: Option<String>,
    ) -> Result<Gd<godot::classes::Object>, (&'static str, String)> {
        match (node, provider) {
            (Some(_), Some(_)) => Err((
                "bad_args",
                "exactly one of 'node' or 'provider' is required, not both".to_string(),
            )),
            (None, None) => Err((
                "bad_args",
                "a target is required: 'node' (a NodePath) or 'provider' (a registered provider)"
                    .to_string(),
            )),
            (Some(path), None) => {
                let np = NodePath::from(path.as_str());
                match self.base().get_node_or_null(&np) {
                    Some(n) => Ok(n.upcast::<godot::classes::Object>()),
                    None => Err(("bad_args", format!("node '{path}' not found"))),
                }
            }
            (None, Some(name)) => match self.providers.get(&name) {
                Some((target, _method)) if target.clone().is_instance_valid() => Ok(target.clone()),
                Some(_) => Err(("internal", format!("provider '{name}' target was freed"))),
                None => Err(("unknown_provider", format!("no provider named '{name}'"))),
            },
        }
    }

    // ---- record_signals (VGCP §4.11) ----------------------------------------------------

    /// Arm a `record_signals` session from the command args, if requested. `record_signals` is an
    /// array of watch specs `{node|provider, signals?}`: a target (one of node/provider) plus an
    /// optional list of signal names (omitted = record **all** of that object's signals). Connects
    /// one recorder per (object, signal) for the advance's duration. Returns `Err((code, msg))` on
    /// a malformed spec / unresolved target / unknown signal — the caller MUST abort the command
    /// (no recorders are left connected on the error path). A no-op when `record_signals` is absent.
    fn arm_recording(&mut self, args: &VarDictionary) -> Result<(), (&'static str, String)> {
        let Some(specs_v) = args.get("record_signals") else {
            return Ok(());
        };
        let specs = specs_v.try_to::<VarArray>().map_err(|_| {
            (
                "bad_args",
                "record_signals must be an array of {node|provider, signals?}".to_string(),
            )
        })?;

        let log: Rc<RefCell<RecordLog>> = Rc::new(RefCell::new(RecordLog {
            entries: Vec::new(),
            truncated: false,
        }));
        let mut recorders: Vec<(Gd<godot::classes::Object>, StringName, Callable)> = Vec::new();

        // Connect all recorders; on ANY failure roll back the partial set so nothing is left
        // dangling (a connected recorder firing into an orphaned log after the command aborted).
        if let Err(e) = self.build_recorders(&specs, &log, &mut recorders) {
            Self::disconnect_recorders(&recorders);
            return Err(e);
        }

        // Store the session even with ZERO recorders (an empty / degenerate `record_signals`, e.g.
        // `[]` or `[{"provider":"game","signals":[]}]`): the command still attaches `recorded: []`,
        // matching the mock and §4.11.2 — the arg was supplied, so the field is present.
        self.recording = Some(RecordingSession { recorders, log });
        Ok(())
    }

    /// Resolve + connect one recorder per (object, signal) for the watch specs, appending each to
    /// `recorders`. Returns `Err` (leaving the caller to roll back) on a malformed spec, unresolved
    /// target, unknown signal, or failed connect.
    fn build_recorders(
        &self,
        specs: &VarArray,
        log: &Rc<RefCell<RecordLog>>,
        recorders: &mut Vec<(Gd<godot::classes::Object>, StringName, Callable)>,
    ) -> Result<(), (&'static str, String)> {
        let start_frame = Engine::singleton().get_physics_frames() as i64;
        // Dedup `(object, signal)` across all specs so redundant specs (the same provider twice, a
        // `node` + the equivalent `provider`, or a repeated name) don't connect two recorders to
        // one signal and log every emission twice. Keyed by instance id + signal name.
        let mut seen: HashSet<(i64, String)> = HashSet::new();
        for (i, spec_v) in specs.iter_shared().enumerate() {
            let spec = spec_v
                .try_to::<VarDictionary>()
                .map_err(|_| ("bad_args", format!("record_signals[{i}] must be an object")))?;
            let target = self
                .resolve_target(arg_str(&spec, "node"), arg_str(&spec, "provider"))
                .map_err(|(c, m)| (c, format!("record_signals[{i}]: {m}")))?;

            // Signal names: an explicit list, or ALL of the object's signals when omitted.
            let names: Vec<StringName> = match spec.get("signals") {
                Some(v) => {
                    let arr = v.try_to::<VarArray>().map_err(|_| {
                        (
                            "bad_args",
                            format!("record_signals[{i}].signals must be an array of names"),
                        )
                    })?;
                    let mut ns = Vec::new();
                    for nv in arr.iter_shared() {
                        let n = nv.try_to::<GString>().map_err(|_| {
                            (
                                "bad_args",
                                format!("record_signals[{i}].signals entries must be strings"),
                            )
                        })?;
                        ns.push(StringName::from(n.to_string().as_str()));
                    }
                    ns
                }
                None => target
                    .get_signal_list()
                    .iter_shared()
                    .filter_map(|d| d.get("name").and_then(|nv| variant_to_string_name(&nv)))
                    .collect(),
            };

            for name in names {
                // Skip a (object, signal) we are already recording (cross-spec or intra-list dup).
                if !seen.insert((target.instance_id().to_i64(), name.to_string())) {
                    continue;
                }
                if !target.has_signal(&name) {
                    return Err((
                        "bad_args",
                        format!("record_signals[{i}]: target has no signal '{name}'"),
                    ));
                }
                let log_c = log.clone();
                let sig_name = name.to_string();
                let callable = Callable::from_fn("vgcp_record_signal", move |sig_args: &[&Variant]| {
                    // Build the entry BEFORE borrowing the shared log, so the borrow covers only the
                    // cap-check + push (describe_args never touches the log, but keeping it out of
                    // the borrow scope is the robust invariant).
                    let frame = Engine::singleton().get_physics_frames() as i64 - start_frame;
                    let captured: VarArray = sig_args.iter().map(|v| (*v).clone()).collect();
                    let described = describe_args(&captured);
                    let mut l = log_c.borrow_mut();
                    if l.entries.len() >= RECORD_CAP {
                        l.truncated = true;
                        return Variant::nil();
                    }
                    let mut entry = VarDictionary::new();
                    entry.set("signal", sig_name.as_str());
                    entry.set("frame", frame);
                    entry.set("args", &described);
                    l.entries.push(entry);
                    Variant::nil()
                });
                let mut t = target.clone();
                if t.connect(&name, &callable) != Error::OK {
                    return Err((
                        "internal",
                        format!("record_signals[{i}]: connect to '{name}' failed"),
                    ));
                }
                recorders.push((target.clone(), name, callable));
            }
        }
        Ok(())
    }

    /// Disconnect a set of recorders, guarded against a freed target / already-removed connection.
    /// Shared by the `arm_recording` rollback path and `drain_recording`.
    fn disconnect_recorders(recorders: &[(Gd<godot::classes::Object>, StringName, Callable)]) {
        for (target, name, callable) in recorders {
            let mut t = target.clone();
            if t.is_instance_valid() && t.is_connected(name, callable) {
                t.disconnect(name, callable);
            }
        }
    }

    /// Tear down the recording session (if any): disconnect every recorder and return the
    /// chronological emission log plus whether it was truncated. Called at every reply site of an
    /// unpausing command so a `recorded` payload is attached and no connection leaks.
    fn drain_recording(&mut self) -> Option<(VarArray, bool)> {
        let session = self.recording.take()?;
        Self::disconnect_recorders(&session.recorders);
        let log = session.log.borrow();
        let arr: VarArray = log.entries.iter().map(|d| d.to_variant()).collect();
        Some((arr, log.truncated))
    }

    /// Attach a drained `recorded` (+ `recorded_truncated`) payload to a reply dict, if a recording
    /// session was active. Centralises the §4.11 teardown done at each unpausing-command reply site.
    fn attach_recorded(&mut self, d: &mut VarDictionary) {
        if let Some((recorded, truncated)) = self.drain_recording() {
            d.set("recorded", &recorded);
            if truncated {
                d.set("recorded_truncated", true);
            }
        }
    }

    /// One physics tick of an in-flight `await_signal`: check the capture slot FIRST (so a signal
    /// emitted on the budget-final tick still wins over the timeout), then enforce the budget.
    fn service_await(&mut self) {
        // Fire path: the closure captured an emission since the last poll.
        let fired = self
            .pending_await
            .as_ref()
            .and_then(|a| a.cell.borrow_mut().take());
        if let Some(args) = fired {
            return self.finish_await(AwaitEnd::Fired(args));
        }
        // Timeout path. `> timeout_ticks` (not `>=`) grants a 1-tick detection grace: the capture
        // closure runs DURING the emitter's tick, after our earlier-priority poll, so a fire on
        // tick `timeout_ticks` is only visible here on tick `timeout_ticks + 1`.
        let timed_out = {
            let a = self.pending_await.as_ref().expect("is_some checked by caller");
            (Engine::singleton().get_physics_frames() as i64 - a.start_frame) > a.timeout_ticks
        };
        if timed_out {
            self.finish_await(AwaitEnd::TimedOut);
        }
    }

    /// End an in-flight `await_signal` (fire or timeout): disconnect, re-pause, send the deferred
    /// reply. `fired:true` carries the signal's `args` as **typed, structured** descriptors
    /// (never an opaque blob — see `describe_variant`); `timed_out:true` marks a budget expiry.
    fn finish_await(&mut self, end: AwaitEnd) {
        let Some(mut pending) = self.pending_await.take() else {
            return;
        };
        Self::disconnect_await(&mut pending);
        self.base().get_tree().set_pause(true);
        let frame = Engine::singleton().get_physics_frames() as i64;
        let sig = pending.signal.to_string();
        let mut d = ok_dict(&pending.id);
        d.set("signal", sig.as_str());
        d.set("waited_ticks", (frame - pending.start_frame).max(0));
        d.set("physics_frame", frame);
        d.set("paused", true);
        match end {
            AwaitEnd::Fired(args) => {
                d.set("fired", true);
                d.set("args", &describe_args(&args)); // owned VarArray -> &Array is AsArg<Variant>
            }
            AwaitEnd::TimedOut => {
                d.set("fired", false);
                d.set("timed_out", true);
            }
        }
        self.attach_recorded(&mut d);
        send_dict(&pending.reply, d);
    }

    /// Cancel an in-flight `await_signal` (from `pause`/`resume`): disconnect and send the deferred
    /// reply. `paused` reflects the new tree state. If the signal *actually fired* before this
    /// `pause`/`resume` drained (an emission captured but not yet polled by `service_await`), report
    /// it as `fired:true` rather than dropping it to `cancelled:false` — `service_await` would have
    /// reported it next tick, and telling the agent `fired:false` for an event that genuinely
    /// happened is a lie. Only a truly empty slot yields `cancelled:true`.
    fn cancel_await(&mut self, paused: bool) {
        let Some(mut pending) = self.pending_await.take() else {
            return;
        };
        let captured = pending.cell.borrow_mut().take();
        Self::disconnect_await(&mut pending);
        let frame = Engine::singleton().get_physics_frames() as i64;
        let sig = pending.signal.to_string();
        let mut d = ok_dict(&pending.id);
        d.set("signal", sig.as_str());
        d.set("waited_ticks", (frame - pending.start_frame).max(0));
        d.set("physics_frame", frame);
        d.set("paused", paused);
        match captured {
            Some(args) => {
                d.set("fired", true);
                d.set("args", &describe_args(&args));
            }
            None => {
                d.set("fired", false);
                d.set("cancelled", true);
            }
        }
        self.attach_recorded(&mut d);
        send_dict(&pending.reply, d);
    }

    /// Disconnect the capture callable from the awaited signal, guarded against a freed target /
    /// an already-removed connection. Associated (no `&self`) so it runs while `pending_await` is
    /// taken. Uses the SAME `Callable` instance that was connected — a `from_fn` callable only
    /// compares equal to itself/its clones, so disconnect-by-identity is exact.
    fn disconnect_await(pending: &mut PendingAwait) {
        let mut target = pending.target.clone();
        if target.is_instance_valid() && target.is_connected(&pending.signal, &pending.callable) {
            target.disconnect(&pending.signal, &pending.callable);
        }
    }

    // ---- await_state + assert (state predicates, VGCP §4.12 / §4.13) ---------------------

    /// Fetch a provider's `get_state` value for predicate evaluation: the built-in `engine` provider
    /// or a registered one. Mirrors `cmd_get_state`'s single-provider arm. `Err((code, msg))` on an
    /// unknown / freed provider.
    fn provider_state(
        &self,
        provider: &str,
        query: &Variant,
    ) -> Result<Variant, (&'static str, String)> {
        if provider == "engine" {
            Ok(self.engine_state().to_variant())
        } else if let Some((target, method)) = self.providers.get(provider) {
            call_provider(target, method, query)
                .ok_or_else(|| ("internal", format!("provider '{provider}' target was freed")))
        } else {
            Err(("unknown_provider", format!("no provider named '{provider}'")))
        }
    }

    /// `assert` (VGCP §4.13) — evaluate a narrow predicate against a provider's current (paused)
    /// state. `ok:true` always (the command RAN); `passed` carries the result and `actual` the typed
    /// value found — consistent with `await_signal`'s fired/timed_out. No game advance.
    fn cmd_assert(&self, reply: &Sender<String>, id: &Variant, args: &VarDictionary) {
        let Some(provider) = arg_str(args, "provider") else {
            return send_err(reply, id, "bad_args", "assert.provider (string) required");
        };
        let Some(op) = arg_str(args, "op").filter(|o| PREDICATE_OPS.contains(&o.as_str())) else {
            return send_err(reply, id, "bad_args", &format!("assert.op must be one of {PREDICATE_OPS:?}"));
        };
        let path = arg_str(args, "path");
        let value = args.get("value").unwrap_or_else(Variant::nil);
        let query = args.get("query").unwrap_or_else(Variant::nil);
        let state = match self.provider_state(&provider, &query) {
            Ok(s) => s,
            Err((code, msg)) => return send_err(reply, id, code, &msg),
        };
        let (passed, actual) = eval_predicate(&state, path.as_deref(), &op, &value);
        let mut d = ok_dict(id);
        d.set("passed", passed);
        d.set("provider", provider.as_str());
        if let Some(p) = &path {
            d.set("path", p.as_str());
        }
        d.set("op", op.as_str());
        d.set("value", &value);
        d.set("actual", &describe_variant(&actual, 0).to_variant());
        send_dict(reply, d);
    }

    /// `await_state` (VGCP §4.12) — advance the world re-evaluating a predicate against a provider
    /// every physics tick, with a REQUIRED tick timeout. Deferred reply like `await_signal`:
    /// re-pauses + replies `held:true` (+ typed `actual`) when it holds, or `held:false,
    /// timed_out:true` at the budget. Cancellable by `pause`/`resume`; supports `record_signals`.
    fn cmd_await_state(
        &mut self,
        reply: &Sender<String>,
        id: &Variant,
        args: &VarDictionary,
        owned_reply: Sender<String>,
    ) {
        if let Some(why) = self.in_flight() {
            return send_err(reply, id, "bad_args", why);
        }
        let Some(provider) = arg_str(args, "provider") else {
            return send_err(reply, id, "bad_args", "await_state.provider (string) required");
        };
        let Some(op) = arg_str(args, "op").filter(|o| PREDICATE_OPS.contains(&o.as_str())) else {
            return send_err(reply, id, "bad_args", &format!("await_state.op must be one of {PREDICATE_OPS:?}"));
        };
        let Some(timeout_ticks) = arg_i64(args, "timeout_ticks") else {
            return send_err(reply, id, "bad_args", "await_state.timeout_ticks (int >= 1) required");
        };
        if timeout_ticks < 1 {
            return send_err(reply, id, "bad_args", "await_state.timeout_ticks must be >= 1");
        }
        let query = args.get("query").unwrap_or_else(Variant::nil);
        // Validate the provider exists now → a clear `unknown_provider` instead of a silent timeout.
        if let Err((code, msg)) = self.provider_state(&provider, &query) {
            return send_err(reply, id, code, &msg);
        }
        // Optional signal recording over the await window (§4.11). Arm last (nothing fallible follows).
        if let Err((code, msg)) = self.arm_recording(args) {
            return send_err(reply, id, code, &msg);
        }
        let start_frame = Engine::singleton().get_physics_frames() as i64;
        self.pending_await_state = Some(PendingAwaitState {
            provider,
            query,
            path: arg_str(args, "path"),
            op,
            value: args.get("value").unwrap_or_else(Variant::nil),
            start_frame,
            timeout_ticks,
            id: id.clone(),
            reply: owned_reply,
        });
        // Unpause so the world advances toward the predicate. Reply is deferred.
        self.base().get_tree().set_pause(false);
    }

    /// One physics tick of an in-flight `await_state`: re-evaluate the predicate; finish `Held` when
    /// it holds, else enforce the budget (the §4.10.2 `> timeout_ticks` 1-tick grace).
    fn service_await_state(&mut self) {
        // Clone the predicate inputs out so we don't hold a borrow of `pending_await_state` while
        // calling `provider_state` (which borrows `self`).
        let (provider, query, path, op, value, start, timeout) = {
            let a = self.pending_await_state.as_ref().expect("is_some checked by caller");
            (a.provider.clone(), a.query.clone(), a.path.clone(), a.op.clone(),
             a.value.clone(), a.start_frame, a.timeout_ticks)
        };
        let state = self.provider_state(&provider, &query).unwrap_or_else(|_| Variant::nil());
        let (held, actual) = eval_predicate(&state, path.as_deref(), &op, &value);
        if held {
            return self.finish_await_state(AwaitStateEnd::Held(actual));
        }
        if (Engine::singleton().get_physics_frames() as i64 - start) > timeout {
            self.finish_await_state(AwaitStateEnd::TimedOut(actual));
        }
    }

    /// End an in-flight `await_state` (held or timeout): re-pause, send the deferred reply with a
    /// typed `actual`.
    fn finish_await_state(&mut self, end: AwaitStateEnd) {
        let Some(pending) = self.pending_await_state.take() else {
            return;
        };
        self.base().get_tree().set_pause(true);
        let frame = Engine::singleton().get_physics_frames() as i64;
        let mut d = ok_dict(&pending.id);
        d.set("provider", pending.provider.as_str());
        d.set("waited_ticks", (frame - pending.start_frame).max(0));
        d.set("physics_frame", frame);
        d.set("paused", true);
        let actual = match end {
            AwaitStateEnd::Held(a) => {
                d.set("held", true);
                a
            }
            AwaitStateEnd::TimedOut(a) => {
                d.set("held", false);
                d.set("timed_out", true);
                a
            }
        };
        d.set("actual", &describe_variant(&actual, 0).to_variant());
        self.attach_recorded(&mut d);
        send_dict(&pending.reply, d);
    }

    /// Cancel an in-flight `await_state` (from `pause`/`resume`): one final re-evaluation so a
    /// predicate that became true right before the cancel is reported `held:true` rather than
    /// dropped (mirrors `cancel_await`); otherwise `cancelled:true`. `paused` reflects the new state.
    fn cancel_await_state(&mut self, paused: bool) {
        let Some(pending) = self.pending_await_state.take() else {
            return;
        };
        let state = self
            .provider_state(&pending.provider, &pending.query)
            .unwrap_or_else(|_| Variant::nil());
        let (held, actual) = eval_predicate(&state, pending.path.as_deref(), &pending.op, &pending.value);
        let frame = Engine::singleton().get_physics_frames() as i64;
        let mut d = ok_dict(&pending.id);
        d.set("provider", pending.provider.as_str());
        d.set("waited_ticks", (frame - pending.start_frame).max(0));
        d.set("physics_frame", frame);
        d.set("paused", paused);
        if held {
            d.set("held", true);
        } else {
            d.set("held", false);
            d.set("cancelled", true);
        }
        d.set("actual", &describe_variant(&actual, 0).to_variant());
        self.attach_recorded(&mut d);
        send_dict(&pending.reply, d);
    }

    /// The live **base** size an input script's `resolution` is compared with (VGCP 1.5.2
    /// §4.9.2): the root window's content-scale size when display stretch sets one (the authored
    /// viewport, whatever the window's pixel size), else the visible rect; `(0, 0)` if unavailable.
    fn live_viewport_size(&self) -> (i64, i64) {
        let base = self.base().get_tree().get_root().get_content_scale_size();
        if base.x > 0 && base.y > 0 {
            return (base.x as i64, base.y as i64);
        }
        if let Some(vp) = self.base().get_viewport() {
            let size = vp.get_visible_rect().size;
            (size.x as i64, size.y as i64)
        } else {
            (0, 0)
        }
    }

    fn engine_state(&self) -> VarDictionary {
        let tree = self.base().get_tree();
        let eng = Engine::singleton();
        let mut d = VarDictionary::new();
        d.set("paused", tree.is_paused());
        d.set("physics_frame", eng.get_physics_frames() as i64);
        d.set("process_frame", eng.get_process_frames() as i64);
        d.set("time_scale", eng.get_time_scale());
        d.set("fps", eng.get_frames_per_second()); // -> f64 (confirmed docs.rs 0.5.3)
        d
    }
}

// ---- Provider invocation ---------------------------------------------------------------

/// Call `target.method(query)` if the target is still alive; returns its Variant result.
fn call_provider(
    target: &Gd<godot::classes::Object>,
    method: &StringName,
    query: &Variant,
) -> Option<Variant> {
    let mut obj = target.clone();
    if !obj.is_instance_valid() {
        return None;
    }
    Some(obj.call(method, std::slice::from_ref(query)))
}

// ---- Input injection (shared by `input` and script playback) ---------------------------

/// Why one event could not be injected. `game_action` (VGCP §4.6.2) needs more than "bad args":
/// an absent sink is a *setup* problem (`no_action_sink`), a freed one an `internal` error, and
/// only a structural/vocabulary rejection is `bad_args`.
enum InjectError {
    BadArgs(String),
    NoActionSink,
    Internal(String),
}

impl InjectError {
    /// The wire (error code, message) pair for this failure.
    fn wire(&self) -> (&'static str, String) {
        match self {
            InjectError::BadArgs(m) => ("bad_args", m.clone()),
            InjectError::NoActionSink => (
                "no_action_sink",
                "no action sink registered (see VGCP §4.6.3 register_action_sink)".to_string(),
            ),
            InjectError::Internal(m) => ("internal", m.clone()),
        }
    }

    /// One-line form for the playback log (bad script events are logged and skipped, §4.9.1).
    fn message(&self) -> String {
        self.wire().1
    }
}

impl From<&str> for InjectError {
    fn from(m: &str) -> Self {
        InjectError::BadArgs(m.to_string())
    }
}

/// Inject exactly ONE input event described by `ev` — a `VarDictionary` in the **exact**
/// `input`-command arg shape (`{"type": "action"|"key"|"mouse_button"|"mouse_move"|"game_action",
/// ...}`). Returns the event `type` (for the reply echo) on success, or an `InjectError`.
/// The single code path used by both `cmd_input` and `run_input_script` playback.
///
/// (Factored as a free fn — it touches only the `Input` singleton and the caller-supplied sink,
/// not `self` — which both keeps it callable from the borrow-restricted playback loop and matches
/// the spec's intent of one shared injector. `sink` is the registered action sink, §4.6.3; it is
/// only consulted by the `game_action` arm.)
fn inject_one_event(
    ev: &VarDictionary,
    sink: Option<&(Gd<godot::classes::Object>, StringName)>,
) -> Result<&'static str, InjectError> {
    let Some(kind) = arg_str(ev, "type") else {
        return Err("input.type required".into());
    };
    let mut input = Input::singleton();
    match kind.as_str() {
        "action" => {
            let Some(action) = arg_str(ev, "action") else {
                return Err("input.action required".into());
            };
            let pressed = arg_bool(ev, "pressed").unwrap_or(true);
            if pressed {
                if let Some(strength) = arg_f64(ev, "strength") {
                    // CONFIRMED via cargo check (gdext 0.5.3): ExActionPress::strength takes f32.
                    input
                        .action_press_ex(action.as_str())
                        .strength(strength as f32)
                        .done();
                } else {
                    input.action_press(action.as_str());
                }
            } else {
                input.action_release(action.as_str());
            }
            Ok("action")
        }
        "key" => {
            let Some(keycode) = arg_i64(ev, "keycode") else {
                return Err("input.keycode (int) required".into());
            };
            let pressed = arg_bool(ev, "pressed").unwrap_or(true);
            let physical = arg_bool(ev, "physical").unwrap_or(false);
            let mut key_ev = InputEventKey::new_gd();
            key_ev.set_pressed(pressed);
            let key = Key::from_ord(keycode as i32);
            if physical {
                key_ev.set_physical_keycode(key);
            } else {
                key_ev.set_keycode(key);
            }
            // &Gd<InputEventKey> coerces to AsArg<Gd<InputEvent>> via the blanket impl in
            // gdext as_arg.rs:147 (impl<T,Base> AsArg<Gd<Base>> for &Gd<T>). Confirmed.
            input.parse_input_event(&key_ev);
            Ok("key")
        }
        "mouse_button" => {
            let Some(x) = arg_f64(ev, "x") else {
                return Err("input.x required".into());
            };
            let Some(y) = arg_f64(ev, "y") else {
                return Err("input.y required".into());
            };
            let button = MouseButton::from_ord(arg_i64(ev, "button").unwrap_or(1) as i32);
            let pos = canvas_to_window(root_canvas_to_window(), Vector2::new(x as f32, y as f32));
            let make = |pressed: bool| {
                let mut mb = InputEventMouseButton::new_gd();
                mb.set_button_index(button);
                mb.set_position(pos);
                mb.set_pressed(pressed);
                mb
            };
            match arg_bool(ev, "pressed") {
                Some(p) => {
                    let mb = make(p);
                    input.parse_input_event(&mb);
                }
                None => {
                    // pressed omitted/null -> a full click (press then release).
                    let down = make(true);
                    input.parse_input_event(&down);
                    let up = make(false);
                    input.parse_input_event(&up);
                }
            }
            Ok("mouse_button")
        }
        "mouse_move" => {
            let Some(x) = arg_f64(ev, "x") else {
                return Err("input.x required".into());
            };
            let Some(y) = arg_f64(ev, "y") else {
                return Err("input.y required".into());
            };
            let pos = canvas_to_window(root_canvas_to_window(), Vector2::new(x as f32, y as f32));
            input.warp_mouse(pos);
            let mut mm = InputEventMouseMotion::new_gd();
            mm.set_position(pos);
            input.parse_input_event(&mm);
            Ok("mouse_move")
        }
        // VGCP §4.6.1 — the canonical, device-agnostic action record. It never touches `Input`:
        // it goes straight to the game's own queue (so it is NOT subject to the paused-input
        // trap) and the game consumes it on the next tick.
        "game_action" => {
            let Some(action) = arg_str(ev, "action") else {
                return Err("game_action event requires string 'action'".into());
            };
            if !payload_ok(ev) {
                return Err("game_action.payload must be an object".into());
            }
            let Some((target, method)) = sink else {
                return Err(InjectError::NoActionSink);
            };
            let mut obj = target.clone();
            if !obj.is_instance_valid() {
                return Err(InjectError::Internal(
                    "the registered action sink was freed".to_string(),
                ));
            }
            // The sink owns the ONE deserialiser: `false` = unknown action name or an ill-typed
            // payload field. Anything but a `true` bool is a refusal.
            if !obj
                .call(method, &[ev.to_variant()])
                .try_to::<bool>()
                .unwrap_or(false)
            {
                return Err(InjectError::BadArgs(format!(
                    "action sink refused the event (action '{action}')"
                )));
            }
            Ok("game_action")
        }
        other => Err(InjectError::BadArgs(format!(
            "unknown input.type '{other}'"
        ))),
    }
}

/// The root viewport's canvas → window-pixel transform (VGCP 1.5.2 §4.6):
/// `final_transform × canvas_transform`. The identity when stretch is off and the canvas is not
/// moved, and also when there is no root (never in a running game).
fn root_canvas_to_window() -> Transform2D {
    let Some(main_loop) = Engine::singleton().get_main_loop() else {
        return Transform2D::IDENTITY;
    };
    let Ok(tree) = main_loop.try_cast::<godot::classes::SceneTree>() else {
        return Transform2D::IDENTITY;
    };
    let root = tree.get_root();
    root.get_final_transform() * root.get_canvas_transform()
}

/// Map a root-canvas point to the whole window pixel the pointer is warped to (VGCP 1.5.2 §4.6).
/// Rounded, because the window system delivers the pointer in whole pixels: warping to a fraction
/// would read back truncated, a pixel short of the scripted point. Engine-free, so `cargo test`
/// covers it.
fn canvas_to_window(xform: Transform2D, p: Vector2) -> Vector2 {
    (xform * p).round()
}

/// `payload` is optional on a `game_action` (§4.6.1): absent or `null` means `{}`; anything that
/// is not a Dictionary is an error. Shared by the injector and `validate_event`.
fn payload_ok(ev: &VarDictionary) -> bool {
    match ev.get("payload") {
        None => true,
        Some(v) if v.is_nil() => true,
        Some(v) => v.try_to::<VarDictionary>().is_ok(),
    }
}

/// Coerce a `seed` argument (VGCP §4.14): a JSON integer, or a whole-valued JSON float (JSON has
/// one number type, so `42` may decode either way). A bool must never silently become 0/1, and a
/// fraction is not a seed.
fn coerce_seed(v: &Variant) -> Option<i64> {
    match v.get_type() {
        VariantType::INT => v.try_to::<i64>().ok(),
        VariantType::FLOAT => v.try_to::<f64>().ok().and_then(whole_f64_to_i64),
        _ => None,
    }
}

/// A float is a valid integer seed only if it is finite, whole, and in `i64` range.
/// (Godot-free, so plain `cargo test` covers it.)
fn whole_f64_to_i64(f: f64) -> Option<i64> {
    if f.is_finite() && f.fract() == 0.0 && f >= -(2f64.powi(63)) && f < 2f64.powi(63) {
        Some(f as i64)
    } else {
        None
    }
}

/// Inject one playback event, applying mouse-coordinate scaling (protocol §4.9.2) to
/// `mouse_button`/`mouse_move` events only. Non-mouse events and the `scale == None` case go
/// straight through `inject_one_event` untouched.
fn inject_playback_event(
    ev: &VarDictionary,
    scale: Option<(f32, f32)>,
    sink: Option<&(Gd<godot::classes::Object>, StringName)>,
) -> Result<&'static str, InjectError> {
    if let Some((sx, sy)) = scale
        && let Some(kind) = arg_str(ev, "type")
        && (kind == "mouse_button" || kind == "mouse_move")
    {
        // Shallow-duplicate so the stored script is never mutated; override x/y only.
        let mut scaled = ev.duplicate_shallow();
        if let Some(x) = arg_f64(ev, "x") {
            scaled.set("x", x * sx as f64);
        }
        if let Some(y) = arg_f64(ev, "y") {
            scaled.set("y", y * sy as f64);
        }
        return inject_one_event(&scaled, sink);
    }
    inject_one_event(ev, sink)
}

// ---- Input-script parsing + validation -------------------------------------------------

/// Resolve a script source from command args: `path` (read+parse a JSON file off disk) or
/// `script` (inline object). `Ok(None)` = neither key present; `Err` = a present source was bad.
fn resolve_script_source(args: &VarDictionary) -> Result<Option<VarDictionary>, String> {
    if let Some(path) = arg_str(args, "path") {
        let text = std::fs::read_to_string(&path)
            .map_err(|e| format!("cannot read script file '{path}': {e}"))?;
        return parse_json_object(&text).map(Some);
    }
    if let Some(v) = args.get("script") {
        return match v.try_to::<VarDictionary>() {
            Ok(obj) => Ok(Some(obj)),
            Err(_) => Err("'script' must be a JSON object".to_string()),
        };
    }
    Ok(None)
}

/// Parse `text` as a JSON object (top-level `{...}`) using Godot's own JSON parser.
fn parse_json_object(text: &str) -> Result<VarDictionary, String> {
    let mut json = Json::new_gd();
    if json.parse(text) != Error::OK {
        return Err(format!("invalid JSON: {}", json.get_error_message()));
    }
    json.get_data()
        .try_to::<VarDictionary>()
        .map_err(|_| "top-level value must be a JSON object".to_string())
}

/// Validate + normalize an input-script object into an [`InputScript`]. Errors carry a precise,
/// human-readable location so a malformed script yields a clear `bad_args`.
fn parse_input_script(root: &VarDictionary) -> Result<InputScript, String> {
    // version (optional; must be 1 if present).
    if let Some(v) = arg_i64(root, "version")
        && v != 1
    {
        return Err(format!(
            "unsupported script version {v} (this server speaks version 1)"
        ));
    }

    // resolution: required [w, h] of two positive numbers.
    let Some(res) = root.get("resolution").and_then(|v| v.try_to::<VarArray>().ok()) else {
        return Err("script requires 'resolution': [w, h]".to_string());
    };
    if res.len() != 2 {
        return Err("'resolution' must be [w, h] (two numbers)".to_string());
    }
    let rw = res.get(0).and_then(num_to_f64);
    let rh = res.get(1).and_then(num_to_f64);
    let (authored_w, authored_h) = match (rw, rh) {
        (Some(w), Some(h)) if w > 0.0 && h > 0.0 => (w as i64, h as i64),
        _ => return Err("'resolution' must be [w, h] with two positive numbers".to_string()),
    };

    let scale = arg_bool(root, "scale").unwrap_or(false);

    // frames: required array.
    let Some(frames_arr) = root.get("frames").and_then(|v| v.try_to::<VarArray>().ok()) else {
        return Err("script requires 'frames': [...]".to_string());
    };

    let mut frames: Vec<(i64, VarArray)> = Vec::new();
    let mut total_events = 0i64;
    let mut max_index = -1i64;
    for (i, fv) in frames_arr.iter_shared().enumerate() {
        let Ok(fd) = fv.try_to::<VarDictionary>() else {
            return Err(format!("frames[{i}] must be an object"));
        };
        let Some(idx) = arg_i64(&fd, "frame") else {
            return Err(format!("frames[{i}] missing integer 'frame'"));
        };
        if idx < 0 {
            return Err(format!("frames[{i}].frame must be >= 0 (got {idx})"));
        }
        let Some(events) = fd.get("events").and_then(|v| v.try_to::<VarArray>().ok()) else {
            return Err(format!("frames[{i}] missing 'events' array"));
        };
        for (j, ev_v) in events.iter_shared().enumerate() {
            let Ok(ev) = ev_v.try_to::<VarDictionary>() else {
                return Err(format!("frames[{i}].events[{j}] must be an object"));
            };
            if let Err(msg) = validate_event(&ev) {
                return Err(format!("frames[{i}].events[{j}]: {msg}"));
            }
            total_events += 1;
        }
        max_index = max_index.max(idx);
        frames.push((idx, events));
    }

    // Stable sort by frame index: preserves authored order for equal indices (and within a frame).
    frames.sort_by_key(|(idx, _)| *idx);
    let duration_frames = if max_index >= 0 { max_index + 1 } else { 0 };

    Ok(InputScript {
        authored_w,
        authored_h,
        scale,
        frames,
        duration_frames,
        total_events,
    })
}

/// Structural validation of a single event object (same required fields as `inject_one_event`,
/// but without injecting) so a script is rejected at load time with a precise message.
fn validate_event(ev: &VarDictionary) -> Result<(), String> {
    let Some(kind) = arg_str(ev, "type") else {
        return Err("missing 'type'".to_string());
    };
    match kind.as_str() {
        "action" => {
            if arg_str(ev, "action").is_none() {
                return Err("action event requires string 'action'".to_string());
            }
        }
        "key" => {
            if arg_i64(ev, "keycode").is_none() {
                return Err("key event requires integer 'keycode'".to_string());
            }
        }
        "mouse_button" | "mouse_move" => {
            if arg_f64(ev, "x").is_none() || arg_f64(ev, "y").is_none() {
                return Err(format!("{kind} event requires numeric 'x' and 'y'"));
            }
        }
        // Structure only (§4.6.2): the action vocabulary belongs to the sink, so a script that
        // names an action this build does not know still LOADS — it is refused at playback and
        // logged there, exactly like any other event the injector rejects.
        "game_action" => {
            if arg_str(ev, "action").is_none() {
                return Err("game_action event requires string 'action'".to_string());
            }
            if !payload_ok(ev) {
                return Err("game_action event 'payload' must be an object".to_string());
            }
        }
        other => return Err(format!("unknown type '{other}'")),
    }
    Ok(())
}

/// Coerce a JSON-number Variant (int or float) to f64.
fn num_to_f64(v: Variant) -> Option<f64> {
    v.try_to::<f64>()
        .ok()
        .or_else(|| v.try_to::<i64>().ok().map(|i| i as f64))
}

/// Coerce a Variant to a `StringName` (a `get_signal_list` entry's `name` is a StringName, but
/// accept a plain String too). `None` if it is neither.
fn variant_to_string_name(v: &Variant) -> Option<StringName> {
    v.try_to::<StringName>()
        .ok()
        .or_else(|| v.try_to::<GString>().ok().map(|g| StringName::from(g.to_string().as_str())))
}

// ---- Signal-argument description (VGCP §4.10) -------------------------------------------

/// Max recursion depth for [`describe_variant`] on nested Array/Dictionary signal args. Godot
/// containers are reference types and CAN be made self-referential, which would otherwise recurse
/// until a stack-overflow abort (uncatchable). Past the cap we emit a sentinel instead of recursing.
const DESCRIBE_MAX_DEPTH: u32 = 64;

/// Describe a whole signal-argument list as a `VarArray` of typed descriptors (see
/// [`describe_variant`]). Shared by the fire and fire-during-cancel reply paths.
fn describe_args(args: &VarArray) -> VarArray {
    args.iter_shared()
        .map(|a| describe_variant(&a, 0).to_variant())
        .collect()
}

/// Convert one signal-argument `Variant` into a **typed, structured, JSON-able** descriptor, so
/// the agent receives usable data — never an opaque blob. Every descriptor is
/// `{"type": <name>, "value": <json>}`:
///   * scalars (bool/int/float/String) → native JSON value;
///   * math types (Vector2/Vector2i/Vector3/Color/Rect2) → expanded named components;
///   * live objects → `{class, instance_id, name?, path?}` instead of a handle;
///   * Array / Dictionary → recurse (nested objects/vectors are expanded too), bounded by
///     [`DESCRIBE_MAX_DEPTH`].
///
/// Exotic types (RID/Callable/packed arrays/transforms) — and a **freed** object handle, which
/// gdext refuses to convert to a `Gd` so it never reaches the `Object` arm — fall back to a
/// human-readable `stringify` string rather than being dropped. `try_to` is type-strict in gdext,
/// so the probe order only needs integer-vector variants before their float siblings for clarity.
fn describe_variant(v: &Variant, depth: u32) -> VarDictionary {
    let mut d = VarDictionary::new();

    if depth >= DESCRIBE_MAX_DEPTH {
        // Self-referential container guard: stop before a stack-overflow abort.
        d.set("type", "other");
        d.set("value", "<max depth>");
        return d;
    }

    if v.is_nil() {
        d.set("type", "nil");
        d.set("value", &Variant::nil());
        return d;
    }
    if let Ok(p) = v.try_to::<Vector2i>() {
        let mut o = VarDictionary::new();
        o.set("x", p.x as i64);
        o.set("y", p.y as i64);
        d.set("type", "Vector2i");
        d.set("value", &o);
        return d;
    }
    if let Ok(p) = v.try_to::<Vector2>() {
        let mut o = VarDictionary::new();
        o.set("x", p.x as f64);
        o.set("y", p.y as f64);
        d.set("type", "Vector2");
        d.set("value", &o);
        return d;
    }
    if let Ok(p) = v.try_to::<Vector3i>() {
        let mut o = VarDictionary::new();
        o.set("x", p.x as i64);
        o.set("y", p.y as i64);
        o.set("z", p.z as i64);
        d.set("type", "Vector3i");
        d.set("value", &o);
        return d;
    }
    if let Ok(p) = v.try_to::<Vector3>() {
        let mut o = VarDictionary::new();
        o.set("x", p.x as f64);
        o.set("y", p.y as f64);
        o.set("z", p.z as f64);
        d.set("type", "Vector3");
        d.set("value", &o);
        return d;
    }
    if let Ok(c) = v.try_to::<Color>() {
        let mut o = VarDictionary::new();
        o.set("r", c.r as f64);
        o.set("g", c.g as f64);
        o.set("b", c.b as f64);
        o.set("a", c.a as f64);
        d.set("type", "Color");
        d.set("value", &o);
        return d;
    }
    if let Ok(r) = v.try_to::<Rect2>() {
        let mut pos = VarDictionary::new();
        pos.set("x", r.position.x as f64);
        pos.set("y", r.position.y as f64);
        let mut sz = VarDictionary::new();
        sz.set("x", r.size.x as f64);
        sz.set("y", r.size.y as f64);
        let mut o = VarDictionary::new();
        o.set("position", &pos);
        o.set("size", &sz);
        d.set("type", "Rect2");
        d.set("value", &o);
        return d;
    }
    if let Ok(obj) = v.try_to::<Gd<godot::classes::Object>>() {
        d.set("type", "Object");
        d.set("value", &describe_object(obj));
        return d;
    }
    if let Ok(arr) = v.try_to::<VarArray>() {
        let described: VarArray = arr
            .iter_shared()
            .map(|e| describe_variant(&e, depth + 1).to_variant())
            .collect();
        d.set("type", "Array");
        d.set("value", &described);
        return d;
    }
    if let Ok(dict) = v.try_to::<VarDictionary>() {
        let mut o = VarDictionary::new();
        for (k, val) in dict.iter_shared() {
            let key = k.stringify().to_string();
            o.set(key.as_str(), &describe_variant(&val, depth + 1).to_variant());
        }
        d.set("type", "Dictionary");
        d.set("value", &o);
        return d;
    }
    // Strict probes for the JSON-native scalars (int vs float are distinguished by gdext).
    if let Ok(b) = v.try_to::<bool>() {
        d.set("type", "bool");
        d.set("value", b);
        return d;
    }
    if let Ok(i) = v.try_to::<i64>() {
        d.set("type", "int");
        d.set("value", i);
        return d;
    }
    if let Ok(f) = v.try_to::<f64>() {
        d.set("type", "float");
        d.set("value", f);
        return d;
    }
    if let Ok(s) = v.try_to::<GString>() {
        d.set("type", "String");
        d.set("value", s.to_string().as_str());
        return d;
    }
    // Exotic (RID/Callable/Signal/packed arrays/transforms): a human string, not a silent drop.
    d.set("type", "other");
    d.set("value", v.stringify().to_string().as_str());
    d
}

/// Identity descriptor for a (live) `Object`-typed signal arg: class + instance id, plus node name
/// and (if in the tree) path. The `{freed:true}` arm is defensive only — gdext's `try_to::<Gd<_>>`
/// rejects an already-freed object before this is reached (it surfaces in `describe_variant`'s
/// `other` arm instead), so in practice this fn always sees a live object.
fn describe_object(obj: Gd<godot::classes::Object>) -> VarDictionary {
    let mut o = VarDictionary::new();
    if !obj.is_instance_valid() {
        o.set("freed", true);
        return o;
    }
    o.set("class", obj.get_class().to_string().as_str());
    o.set("instance_id", obj.instance_id().to_i64());
    if let Ok(node) = obj.clone().try_cast::<Node>() {
        o.set("name", node.get_name().to_string().as_str());
        if node.is_inside_tree() {
            o.set("path", node.get_path().to_string().as_str());
        }
    }
    o
}

// ---- State predicates (VGCP §4.12 / §4.13) ----------------------------------------------

/// Evaluate a narrow predicate (`assert` / `await_state`) against a provider's state `Variant`.
/// Returns `(passed, actual)` — `actual` is the value the `path` resolved to (nil if it didn't). A
/// comparison vocabulary only (see [`PREDICATE_OPS`]); NOT an expression engine.
fn eval_predicate(state: &Variant, path: Option<&str>, op: &str, value: &Variant) -> (bool, Variant) {
    let resolved = navigate_path(state, path);
    let actual = resolved.clone().unwrap_or_else(Variant::nil);
    let passed = match op {
        "exists" => resolved.is_some(),
        "truthy" => actual.booleanize(),
        "eq" => variant_eq(&actual, value),
        "ne" => !variant_eq(&actual, value),
        "lt" | "le" | "gt" | "ge" => {
            match (num_to_f64(actual.clone()), num_to_f64(value.clone())) {
                (Some(a), Some(b)) => match op {
                    "lt" => a < b,
                    "le" => a <= b,
                    "gt" => a > b,
                    _ => a >= b, // "ge"
                },
                _ => false, // non-numeric operand → ordering is false, never an error
            }
        }
        "in" => value
            .try_to::<VarArray>()
            .map(|arr| arr.iter_shared().any(|e| variant_eq(&e, &actual)))
            .unwrap_or(false),
        "contains" => {
            if let Ok(arr) = actual.try_to::<VarArray>() {
                arr.iter_shared().any(|e| variant_eq(&e, value))
            } else if let (Ok(s), Ok(sub)) = (actual.try_to::<GString>(), value.try_to::<GString>()) {
                s.to_string().contains(&sub.to_string())
            } else {
                false
            }
        }
        _ => false, // unreachable: ops are validated against PREDICATE_OPS upstream
    };
    (passed, actual)
}

/// Resolve a dotted `path` (e.g. `"state"`, `"enemies.0.hp"`) into a state `Variant` — dict keys and
/// array indices. `None`/empty path = the whole value. `None` if any segment fails to resolve.
fn navigate_path(state: &Variant, path: Option<&str>) -> Option<Variant> {
    let path = path.unwrap_or("");
    if path.is_empty() {
        return Some(state.clone());
    }
    let mut cur = state.clone();
    for seg in path.split('.') {
        cur = if let Ok(dict) = cur.try_to::<VarDictionary>() {
            dict.get(seg)?
        } else if let Ok(arr) = cur.try_to::<VarArray>() {
            arr.get(seg.parse::<usize>().ok()?)?
        } else {
            return None;
        };
    }
    Some(cur)
}

/// Numeric-aware Variant equality for predicates: compare numerically when both sides are numbers
/// (so an int `0` matches a JSON `0` or `0.0`), else fall back to Godot Variant equality (strings,
/// bools, …).
fn variant_eq(a: &Variant, b: &Variant) -> bool {
    match (num_to_f64(a.clone()), num_to_f64(b.clone())) {
        (Some(x), Some(y)) => x == y,
        _ => a == b,
    }
}

// ---- Response helpers ------------------------------------------------------------------

fn ok_dict(id: &Variant) -> VarDictionary {
    let mut d = VarDictionary::new();
    // id is already &Variant; only &Variant impls AsArg<Variant> in 0.5.3 (owned Variant has
    // Pass=ByVariant and is excluded from the ByValue blanket), so pass the borrow directly.
    d.set("id", id);
    d.set("ok", true);
    d
}

fn send_dict(reply: &Sender<String>, d: VarDictionary) {
    // Json::stringify produces compact, single-line JSON (no embedded newlines). Perfect for NDJSON.
    let _ = reply.send(Json::stringify(&d.to_variant()).to_string());
}

fn send_err(reply: &Sender<String>, id: &Variant, code: &str, message: &str) {
    let mut err = VarDictionary::new();
    err.set("code", code);
    err.set("message", message);
    let mut d = VarDictionary::new();
    d.set("id", id); // id: &Variant already (AsArg<Variant>); no clone
    d.set("ok", false);
    d.set("error", &err); // owned VarDictionary -> pass &Dictionary (AsArg<Variant>)
    let _ = reply.send(Json::stringify(&d.to_variant()).to_string());
}

// ---- Argument extraction (JSON numbers may decode as int or float) ---------------------

fn arg_i64(args: &VarDictionary, key: &str) -> Option<i64> {
    args.get(key).and_then(|v| {
        v.try_to::<i64>()
            .ok()
            .or_else(|| v.try_to::<f64>().ok().map(|f| f as i64))
    })
}

fn arg_f64(args: &VarDictionary, key: &str) -> Option<f64> {
    args.get(key).and_then(|v| {
        v.try_to::<f64>()
            .ok()
            .or_else(|| v.try_to::<i64>().ok().map(|i| i as f64))
    })
}

fn arg_bool(args: &VarDictionary, key: &str) -> Option<bool> {
    args.get(key).and_then(|v| v.try_to::<bool>().ok())
}

fn arg_str(args: &VarDictionary, key: &str) -> Option<String> {
    args.get(key)
        .and_then(|v| v.try_to::<GString>().ok())
        .map(|g| g.to_string())
}

/// Where a screenshot lands when the client sends no `path`: `VGCP_SHOTS_DIR` if it is set to a
/// non-empty value, else `<temp>/vgcp_tmp`. `std::env::temp_dir` already honours `TMPDIR`, so no
/// temp root is hardcoded. The directory is always made **absolute** before it is used, because
/// the reply carries the path and the agent reading it has its own working directory — a relative
/// `VGCP_SHOTS_DIR` would otherwise resolve against the *game process's* cwd. A `path` the client
/// *does* send is used verbatim, absolute or not.
fn shots_dir() -> PathBuf {
    shots_dir_from(
        std::env::var("VGCP_SHOTS_DIR").ok(),
        std::env::current_dir().ok(),
    )
}

/// The pure half of [`shots_dir`]: the value of `VGCP_SHOTS_DIR` (if any) and the working
/// directory a relative one is resolved against -> the directory. With no usable working
/// directory a relative value falls back to the temp dir, so the result is absolute either way.
fn shots_dir_from(var: Option<String>, cwd: Option<PathBuf>) -> PathBuf {
    match var {
        Some(dir) if !dir.is_empty() => {
            let dir = PathBuf::from(dir);
            if dir.is_absolute() {
                dir
            } else {
                cwd.unwrap_or_else(std::env::temp_dir).join(dir)
            }
        }
        _ => std::env::temp_dir().join(DEFAULT_SHOTS_SUBDIR),
    }
}

// ---- Socket server (background thread; never touches the SceneTree) --------------------

fn vgcp_serve(listener: TcpListener, tx: Sender<VgcpRequest>) {
    // The listener is already bound on the main thread (see `ready`); this thread only accepts.
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let tx = tx.clone();
                std::thread::Builder::new()
                    .name("vgcp-conn".into())
                    .spawn(move || vgcp_handle_conn(stream, tx))
                    .ok();
            }
            Err(_) => continue,
        }
    }
}

fn vgcp_handle_conn(stream: TcpStream, tx: Sender<VgcpRequest>) {
    let read_half = match stream.try_clone() {
        Ok(s) => s,
        Err(_) => return,
    };
    let mut reader = BufReader::new(read_half);
    let mut writer = stream;
    let mut line = String::new();

    loop {
        line.clear();
        // Bound the read at MAX_LINE+1 bytes so an oversized line cannot exhaust memory *before*
        // we reject it (protocol §1: a server MUST reject a >1 MiB line before parsing). Reading
        // one byte past the cap lets us distinguish "exactly MAX_LINE then newline" (accept) from
        // "no newline within the cap" (overflow). `Take<&mut BufReader>` is itself `BufRead`, so
        // `read_line` still works; `by_ref()` preserves the underlying buffer across iterations.
        let n = match reader.by_ref().take(MAX_LINE as u64 + 1).read_line(&mut line) {
            Ok(0) => break, // EOF
            Ok(n) => n,
            Err(_) => break,
        };
        // Hit the cap without a terminating newline -> the line exceeds 1 MiB. Reject pre-parse
        // (we never allocate beyond the cap) and close: we cannot reliably resync framing past an
        // unbounded line, and the doc says a server SHOULD close after replying frame_too_large.
        if n > MAX_LINE && !line.ends_with('\n') {
            let _ = writeln!(
                writer,
                "{{\"id\":null,\"ok\":false,\"error\":{{\"code\":\"frame_too_large\",\"message\":\"request exceeds 1 MiB\"}}}}"
            );
            break;
        }
        let trimmed = line.trim_end_matches(['\r', '\n']);
        if trimmed.is_empty() {
            continue;
        }
        let (rtx, rrx) = channel::<String>();
        if tx
            .send(VgcpRequest {
                line: trimmed.to_string(),
                reply: rtx,
            })
            .is_err()
        {
            break; // main-thread receiver gone -> server shutting down
        }
        match rrx.recv() {
            Ok(resp) => {
                if writeln!(writer, "{resp}").is_err() {
                    break;
                }
            }
            Err(_) => break, // reply channel dropped without a response
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{InjectError, SkippedEvent, canvas_to_window, shots_dir_from, whole_f64_to_i64};
    use godot::builtin::{Transform2D, Vector2};

    /// A screenshot with no client `path` goes to `VGCP_SHOTS_DIR` when that is set, and otherwise
    /// to `<temp>/vgcp_tmp` — where `<temp>` is `std::env::temp_dir()`, which honours `TMPDIR`.
    /// Whatever the source, the directory comes back **absolute**, because the reply carries the
    /// path to a client that reads it from a different working directory.
    #[test]
    fn shots_dir_defaults_under_the_temp_dir() {
        let cwd = || Some(std::path::PathBuf::from("/work/dir"));
        assert_eq!(
            shots_dir_from(Some("/shots/here".to_string()), cwd()),
            std::path::PathBuf::from("/shots/here")
        );
        // A relative value is resolved against the working directory, never handed back as is.
        assert_eq!(
            shots_dir_from(Some("relative/shots".to_string()), cwd()),
            std::path::PathBuf::from("/work/dir/relative/shots")
        );
        // ...and with no usable working directory, against the temp dir — still absolute.
        assert_eq!(
            shots_dir_from(Some("relative/shots".to_string()), None),
            std::env::temp_dir().join("relative/shots")
        );
        for unset in [None, Some(String::new())] {
            assert_eq!(
                shots_dir_from(unset, cwd()),
                std::env::temp_dir().join("vgcp_tmp")
            );
            assert!(shots_dir_from(None, cwd()).is_absolute());
        }
    }

    /// VGCP 1.5.2: at the identity (stretch off, canvas unmoved) a mouse coordinate is its own
    /// window pixel, so every pre-1.5.2 script at 1280×720 is unchanged; a scaled, centred canvas
    /// maps through scale then offset and rounds to a whole pixel.
    #[test]
    fn canvas_coordinates_map_to_whole_window_pixels() {
        let id = Transform2D::IDENTITY;
        for p in [Vector2::new(0.0, 0.0), Vector2::new(640.0, 360.0), Vector2::new(1279.0, 719.0)] {
            assert_eq!(canvas_to_window(id, p), p);
        }
        assert_eq!(canvas_to_window(id, Vector2::new(640.0, 534.667)), Vector2::new(640.0, 535.0));
        // 1.25× (a 1600×900 window): 333 → 416.25 → 416.
        let s125 = Transform2D::from_cols(Vector2::new(1.25, 0.0), Vector2::new(0.0, 1.25), Vector2::ZERO);
        assert_eq!(canvas_to_window(s125, Vector2::new(333.0, 101.0)), Vector2::new(416.0, 126.0));
        // 1.5× with the canvas centred 213 px in (a 2560×1080 window): final × canvas.
        let stretch = Transform2D::from_cols(Vector2::new(1.5, 0.0), Vector2::new(0.0, 1.5), Vector2::ZERO);
        let canvas = Transform2D::from_cols(Vector2::RIGHT, Vector2::DOWN, Vector2::new(213.0, 0.0));
        assert_eq!(canvas_to_window(stretch * canvas, Vector2::new(0.0, 0.0)), Vector2::new(320.0, 0.0));
        assert_eq!(canvas_to_window(stretch * canvas, Vector2::new(333.0, 333.0)), Vector2::new(819.0, 500.0));
    }

    /// A skipped playback event carries the same wire code the `input` command would have answered
    /// for that event (VGCP 1.5.1 §4.9.5), plus its frame and in-frame index.
    #[test]
    fn skipped_entries_use_the_input_wire_codes() {
        let refused = SkippedEvent::new(
            3,
            1,
            &InjectError::BadArgs("action sink refused the event (action 'x')".to_string()),
        );
        assert_eq!(refused.frame, 3);
        assert_eq!(refused.index, 1);
        assert_eq!(refused.code, "bad_args");
        assert_eq!(refused.message, "action sink refused the event (action 'x')");
        assert_eq!(SkippedEvent::new(0, 0, &InjectError::NoActionSink).code, "no_action_sink");
        let freed = SkippedEvent::new(0, 2, &InjectError::Internal("freed".to_string()));
        assert_eq!((freed.code, freed.message.as_str()), ("internal", "freed"));
    }

    /// `seed` takes an *integer* (VGCP §4.14). JSON has one number type, so `42` may arrive as a
    /// float — that is a seed. `42.5` is not, and neither is anything non-finite. (The bool and
    /// string rejections live in `coerce_seed`, which needs a live Variant and so is covered by
    /// the mock/live self-tests rather than here.)
    #[test]
    fn whole_floats_are_seeds_fractions_are_not() {
        assert_eq!(whole_f64_to_i64(42.0), Some(42));
        assert_eq!(whole_f64_to_i64(0.0), Some(0));
        assert_eq!(whole_f64_to_i64(-7.0), Some(-7));
        assert_eq!(whole_f64_to_i64(42.5), None);
        assert_eq!(whole_f64_to_i64(-0.25), None);
        assert_eq!(whole_f64_to_i64(f64::NAN), None);
        assert_eq!(whole_f64_to_i64(f64::INFINITY), None);
        // Out of i64 range: 2^63 exactly, and beyond.
        assert_eq!(whole_f64_to_i64(9.223_372_036_854_776e18), None);
        assert_eq!(whole_f64_to_i64(-1e30), None);
    }
}
