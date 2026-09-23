#!/usr/bin/env python3
"""mock_server.py: a pure-stdlib MOCK of a Video Game Control Protocol (VGCP v1) server.

It speaks the exact wire protocol in `../docs/vgcp-protocol.md` with **no Godot**, so the
drivers (control.py CLI and the TypeScript MCP server) can be validated end-to-end against a
faithful, dependency-free stand-in.

It models just enough state to be meaningful:
  * paused flag (paused-by-default), physics_frame counter, process_frame, time_scale
  * `step` advances physics_frame by `ticks`, *defers* its reply by a tiny sleep to mimic
    the real server's "reply only after the ticks elapse", then re-pauses
  * `screenshot` writes a real, valid PNG (stdlib zlib) and returns its path + dimensions;
    with no `path` it resolves the shots directory exactly as the real server does (protocol
    §4.5: `VGCP_SHOTS_DIR`, else `<temp>/vgcp_tmp`, so `TMPDIR` moves it)
  * `get_state` exposes the built-in `engine` provider plus a fake `hud` provider
  * `input` validates argument shapes and echoes the type, including the canonical
    `game_action` event type (VGCP 1.5.0), modelled as delivered to a registered action sink
  * `seed` (VGCP 1.5.0) is modelled as delivered to a registered seed target and mirrored
    back as the `hud` provider's `seed` field
  * `run_input_script` replies carry `skipped` (VGCP 1.5.1): `game_action` events in the frames
    that run are listed when the mock-only hint `mock_no_sink` (-> `no_action_sink`) or
    `mock_sink_refuses` (-> `bad_args`) is set on the `run_input_script` args; `[]` otherwise

Run:
    python3 mock_server.py [--host 127.0.0.1] [--port 38787] [--once] [--quiet]

The endpoint defaults to VGCP_HOST / VGCP_PORT, else 127.0.0.1:38787.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import tempfile
import threading
import time
import zlib
from typing import Any, Optional

PROTOCOL_NAME = "vgcp"
PROTOCOL_MAJOR = 1
PROTOCOL_VERSION = "1.5.2"
MAX_LINE = 1024 * 1024
_PREDICATE_OPS = ("eq", "ne", "lt", "le", "gt", "ge", "in", "contains", "exists", "truthy")
DEFAULT_SHOTS_SUBDIR = "vgcp_tmp"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 38787


def _shots_dir() -> str:
    """Where a `screenshot` with no `path` lands: the same rule as the real server (protocol
    §4.5): `VGCP_SHOTS_DIR` when it is set to a non-empty value, else `<temp>/vgcp_tmp`, where
    `<temp>` is `tempfile.gettempdir()` (which honours `TMPDIR`, as Rust's `std::env::temp_dir`
    does), so no temp root is hardcoded. A relative value is made absolute against the working
    directory, because the reply carries the path to a client that reads it from elsewhere."""
    dir_ = os.environ.get("VGCP_SHOTS_DIR") or os.path.join(
        tempfile.gettempdir(), DEFAULT_SHOTS_SUBDIR
    )
    return os.path.abspath(dir_)


def write_png(path: str, width: int = 8, height: int = 8) -> tuple[int, int]:
    """Write a minimal but valid RGBA PNG (a simple gradient). Returns (width, height)."""
    width = max(1, width)
    height = max(1, height)
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None) per scanline
        for x in range(width):
            r = (x * 255) // max(1, width - 1) if width > 1 else 0
            g = (y * 255) // max(1, height - 1) if height > 1 else 0
            raw += bytes((r & 0xFF, g & 0xFF, 128, 255))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    idat = zlib.compress(bytes(raw), 9)
    png = sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    return width, height


class MockState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.paused = True  # paused-by-default
        self.physics_frame = 0
        self.process_frame = 0
        self.time_scale = 1.0
        self.shot_counter = 0
        # fake game state for the `hud` provider (`seed` mirrors the last accepted `seed` command)
        self.hud = {"gold": 120, "lives": 18, "wave": 3, "seed": None}
        # VGCP 1.5.0: the real server holds an optional action sink / seed target registered by the
        # game. The mock models BOTH as present; the mock-only hints `mock_no_sink` (on `input`) and
        # `mock_no_seed_target` (on `seed`) simulate an unregistered one, and `mock_sink_refuses`
        # simulates a sink that rejects the event (unknown action name / ill-typed payload field).
        self.action_sink = True
        self.seed_target = True
        self.seed: Optional[int] = None
        # loaded input script (VGCP §4.9): parsed summary, kept for run/status
        self.script: Optional[dict] = None  # {frames, duration_frames, events}


def _err(msg_id: Any, code: str, message: str) -> dict:
    return {"id": msg_id, "ok": False, "error": {"code": code, "message": message}}


def _ok(msg_id: Any, **payload) -> dict:
    return {"id": msg_id, "ok": True, **payload}


_EVENT_TYPES = ("action", "key", "mouse_button", "mouse_move", "game_action")


def _describe_value(v: Any) -> dict:
    """Mirror the gdext server's `describe_variant` for the JSON-native types the mock handles
    (no Godot here, so just nil/bool/int/float/string/array/dict). Returns the same self-describing
    `{"type":..,"value":..}` envelope so drivers see the real `await_signal` arg shape (§4.10)."""
    if v is None:
        return {"type": "nil", "value": None}
    if isinstance(v, bool):  # before int: Python bool is a subclass of int
        return {"type": "bool", "value": v}
    if isinstance(v, int):
        return {"type": "int", "value": v}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, str):
        return {"type": "String", "value": v}
    if isinstance(v, list):
        return {"type": "Array", "value": [_describe_value(e) for e in v]}
    if isinstance(v, dict):
        return {"type": "Dictionary", "value": {str(k): _describe_value(val) for k, val in v.items()}}
    return {"type": "other", "value": str(v)}


def _validate_event(ev: Any) -> Optional[str]:
    """Return None if the event object is well-formed, else an error message."""
    if not isinstance(ev, dict):
        return "must be an object"
    t = ev.get("type")
    if t not in _EVENT_TYPES:
        return f"unknown type '{t}'"
    if t == "action" and not isinstance(ev.get("action"), str):
        return "action event requires string 'action'"
    if t == "key" and not isinstance(ev.get("keycode"), (int, float)):
        return "key event requires integer 'keycode'"
    if t in ("mouse_button", "mouse_move") and not (
        isinstance(ev.get("x"), (int, float)) and isinstance(ev.get("y"), (int, float))
    ):
        return f"{t} event requires numeric 'x' and 'y'"
    if t == "game_action":
        # Structural validation ONLY (VGCP §4.6): the vocabulary of action names and the shape of
        # each payload belong to the game's action sink, never to the server/mock.
        if not isinstance(ev.get("action"), str) or not ev.get("action"):
            return "game_action event requires non-empty string 'action'"
        payload = ev.get("payload")
        if payload is not None and not isinstance(payload, dict):
            return "game_action 'payload' must be an object when present"
    return None


def parse_input_script(root: Any) -> tuple[Optional[dict], Optional[str]]:
    """Validate a script object (mirrors the gdext server). Returns (summary, error)."""
    if not isinstance(root, dict):
        return None, "script must be a JSON object"
    ver = root.get("version")
    if ver is not None and ver != 1:
        return None, f"unsupported script version {ver} (this server speaks version 1)"
    res = root.get("resolution")
    if not (isinstance(res, list) and len(res) == 2):
        return None, "script requires 'resolution': [w, h]"
    w, h = res[0], res[1]
    if not (isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0):
        return None, "'resolution' must be [w, h] with two positive numbers"
    frames = root.get("frames")
    if not isinstance(frames, list):
        return None, "script requires 'frames': [...]"
    total_events = 0
    max_index = -1
    timeline: list[tuple[int, list]] = []
    for i, fr in enumerate(frames):
        if not isinstance(fr, dict):
            return None, f"frames[{i}] must be an object"
        idx = fr.get("frame")
        if not isinstance(idx, int) or isinstance(idx, bool):
            return None, f"frames[{i}] missing integer 'frame'"
        if idx < 0:
            return None, f"frames[{i}].frame must be >= 0 (got {idx})"
        events = fr.get("events")
        if not isinstance(events, list):
            return None, f"frames[{i}] missing 'events' array"
        for j, ev in enumerate(events):
            msg = _validate_event(ev)
            if msg:
                return None, f"frames[{i}].events[{j}]: {msg}"
            total_events += 1
        max_index = max(max_index, idx)
        timeline.append((idx, events))
    duration_frames = max_index + 1 if max_index >= 0 else 0
    return {
        "frames": len(frames),
        "duration_frames": duration_frames,
        "events": total_events,
        # Playback order, as the server sorts it: ascending frame, entries sharing an index in
        # array order (sorted() is stable). Only `run_input_script`'s `skipped` model reads it.
        "timeline": sorted(timeline, key=lambda e: e[0]),
    }, None


def _mock_skipped(summary: dict, frames_to_run: int, args: dict) -> list:
    """VGCP 1.5.1 `skipped` for a mock playback: the mock injects nothing, so only a `game_action`
    refused by the (modelled) action sink can be skipped, and only when a hint asks for it.
    `index` counts a frame's events across every entry sharing that frame index (§4.9.5)."""
    no_sink, refuses = bool(args.get("mock_no_sink")), bool(args.get("mock_sink_refuses"))
    if not (no_sink or refuses):
        return []
    out: list = []
    index_in_frame: dict[int, int] = {}
    for frame, events in summary.get("timeline", []):
        if frame >= frames_to_run:
            break
        for ev in events:
            index = index_in_frame.get(frame, 0)
            index_in_frame[frame] = index + 1
            if ev.get("type") != "game_action":
                continue
            if no_sink:
                out.append({"frame": frame, "index": index, "code": "no_action_sink",
                            "message": "no action sink registered (call register_action_sink)"})
            else:
                out.append({"frame": frame, "index": index, "code": "bad_args",
                            "message": f"action sink refused the event (action '{ev.get('action')}')"})
    return out


def _num(x: Any) -> Optional[float]:
    """Numeric value of x (int/float, NOT bool), else None. Mirrors the server's num_to_f64."""
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _veq(a: Any, b: Any) -> bool:
    """Numeric-aware equality (mirrors the server's variant_eq)."""
    na, nb = _num(a), _num(b)
    return na == nb if (na is not None and nb is not None) else a == b


def _navigate(state: Any, path: Optional[str]) -> tuple[bool, Any]:
    """Resolve a dotted path into a state value (dict keys / list indices). Returns (resolved, value).
    Mirrors the server's navigate_path."""
    if not path:
        return True, state
    cur = state
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif isinstance(cur, list):
            try:
                i = int(seg)
            except ValueError:
                return False, None
            if 0 <= i < len(cur):
                cur = cur[i]
            else:
                return False, None
        else:
            return False, None
    return True, cur


def _eval_predicate(state: Any, path: Optional[str], op: str, value: Any) -> tuple[bool, Any]:
    """Evaluate a narrow predicate against a state value. Mirrors the gdext `eval_predicate`.
    Returns (passed, actual)."""
    resolved, actual = _navigate(state, path)
    a = actual if resolved else None
    if op == "exists":
        passed = resolved
    elif op == "truthy":
        passed = bool(a)
    elif op == "eq":
        passed = _veq(a, value)
    elif op == "ne":
        passed = not _veq(a, value)
    elif op in ("lt", "le", "gt", "ge"):
        na, nv = _num(a), _num(value)
        passed = False if (na is None or nv is None) else (
            {"lt": na < nv, "le": na <= nv, "gt": na > nv, "ge": na >= nv}[op])
    elif op == "in":
        passed = isinstance(value, list) and any(_veq(e, a) for e in value)
    elif op == "contains":
        if isinstance(a, list):
            passed = any(_veq(e, value) for e in a)
        elif isinstance(a, str) and isinstance(value, str):
            passed = value in a
        else:
            passed = False
    else:
        passed = False
    return passed, a


def _provider_value(state: "MockState", provider: str) -> Optional[dict]:
    """The mock's modelled provider state for predicate eval (engine / hud), or None if unknown."""
    if provider == "engine":
        return {"paused": state.paused, "physics_frame": state.physics_frame,
                "process_frame": state.process_frame, "time_scale": state.time_scale, "fps": 0.0}
    if provider == "hud":
        return dict(state.hud)
    return None


def _record_payload(args: dict) -> tuple[Optional[dict], Optional[tuple[str, str]]]:
    """Validate a `record_signals` arg (VGCP §4.11) and synthesize a `recorded` payload to merge
    into an unpausing command's reply. Returns (payload, error) where payload is
    `{} | {"recorded": [...], "recorded_truncated"?}` and error is `(code, message)` or None.
    Error codes mirror the gdext server (unknown provider -> `unknown_provider`, else `bad_args`).
    With no real Godot signals, the mock emits the entries named by the mock-only `mock_recorded`
    hint (the real server ignores that hint and records live emissions)."""
    specs = args.get("record_signals")
    if specs is None:
        return {}, None
    if not isinstance(specs, list):
        return None, ("bad_args", "record_signals must be an array of {node|provider, signals?}")
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict):
            return None, ("bad_args", f"record_signals[{i}] must be an object")
        node, provider = spec.get("node"), spec.get("provider")
        if (node is None) == (provider is None):
            return None, ("bad_args", f"record_signals[{i}]: exactly one of 'node' or 'provider' required")
        if provider is not None and provider not in ("hud",):
            return None, ("unknown_provider", f"record_signals[{i}]: no provider named '{provider}'")
        sigs = spec.get("signals")
        if sigs is not None and not (isinstance(sigs, list) and all(isinstance(s, str) for s in sigs)):
            return None, ("bad_args", f"record_signals[{i}].signals must be an array of names")
    recorded = [
        {"signal": e.get("signal", ""), "frame": e.get("frame", 0),
         "args": [_describe_value(a) for a in e.get("args", [])]}
        for e in args.get("mock_recorded", [])
    ]
    out: dict = {"recorded": recorded}
    if args.get("mock_recorded_truncated"):
        out["recorded_truncated"] = True
    return out, None


def _resolve_script_source(args: dict) -> tuple[Optional[Any], Optional[str]]:
    """Return (script_obj_or_None, error). 'path' reads a file; 'script' is inline."""
    path = args.get("path")
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f), None
        except OSError as e:
            return None, f"cannot read script file '{path}': {e}"
        except json.JSONDecodeError as e:
            return None, f"invalid JSON: {e}"
    if "script" in args:
        return args.get("script"), None
    return None, None


def handle(state: MockState, req: dict) -> dict:
    msg_id = req.get("id")
    cmd = req.get("cmd")
    args = req.get("args") or {}
    if cmd is None:
        return _err(msg_id, "missing_cmd", "no 'cmd' field")

    with state.lock:
        if cmd == "ping":
            min_major = args.get("min_protocol_major")
            if isinstance(min_major, (int, float)) and min_major > PROTOCOL_MAJOR:
                return _err(msg_id, "protocol_mismatch",
                            f"server major {PROTOCOL_MAJOR} < required {int(min_major)}")
            return _ok(msg_id, protocol=PROTOCOL_NAME, protocol_major=PROTOCOL_MAJOR,
                       version=PROTOCOL_VERSION, server="mock-vgcp", paused=state.paused,
                       physics_frame=state.physics_frame, time_scale=state.time_scale)

        if cmd == "pause":
            state.paused = True
            return _ok(msg_id, paused=True)

        if cmd == "resume":
            state.paused = False
            return _ok(msg_id, paused=False)

        if cmd == "step":
            ticks = args.get("ticks", 1)
            if not isinstance(ticks, (int, float)) or ticks < 1:
                return _err(msg_id, "bad_args", "step.ticks must be >= 1")
            mode = args.get("mode", "budget")
            if mode != "budget":
                return _err(msg_id, "unsupported_mode", f"mode '{mode}' not supported")
            rec, rerr = _record_payload(args)
            if rerr:
                return _err(msg_id, rerr[0], rerr[1])
            ticks = int(ticks)
            state.paused = False
            # Mimic the real server deferring its reply until the ticks elapse.
            time.sleep(min(0.2, ticks * 0.001))
            state.physics_frame += ticks
            state.process_frame += ticks
            state.paused = True
            return _ok(msg_id, ticks=ticks, physics_frame=state.physics_frame, paused=True, **rec)

        if cmd == "set_timescale":
            value = args.get("value")
            if not isinstance(value, (int, float)) or value <= 0:
                return _err(msg_id, "bad_args", "value must be > 0")
            state.time_scale = float(value)
            return _ok(msg_id, time_scale=state.time_scale)

        if cmd == "screenshot":
            path = args.get("path")
            if not path:
                state.shot_counter += 1
                path = os.path.join(_shots_dir(), f"shot-{state.shot_counter}.png")
            downscale = args.get("downscale", 1.0)
            base_w, base_h = 16, 12
            if isinstance(downscale, (int, float)) and 0 < downscale < 1:
                base_w = max(1, int(base_w * downscale))
                base_h = max(1, int(base_h * downscale))
            try:
                w, h = write_png(path, base_w, base_h)
            except OSError as e:
                return _err(msg_id, "capture_failed", str(e))
            return _ok(msg_id, path=path, w=w, h=h)

        if cmd == "input":
            itype = args.get("type")
            if itype not in _EVENT_TYPES:
                return _err(msg_id, "bad_args", f"unknown input.type '{itype}'")
            if itype == "action" and not args.get("action"):
                return _err(msg_id, "bad_args", "input.action required")
            if itype in ("mouse_button", "mouse_move") and ("x" not in args or "y" not in args):
                return _err(msg_id, "bad_args", "input.x and input.y required")
            if itype == "key" and "keycode" not in args:
                return _err(msg_id, "bad_args", "input.keycode required")
            if itype == "game_action":
                # A canonical game action (VGCP §4.6, 1.5.0). Structure first, then delivery:
                # no registered sink -> no_action_sink; a sink that returns false -> bad_args.
                msg = _validate_event(args)
                if msg:
                    return _err(msg_id, "bad_args", f"input: {msg}")
                if args.get("mock_no_sink") or not state.action_sink:
                    return _err(msg_id, "no_action_sink",
                                "no action sink registered (call register_action_sink)")
                if args.get("mock_sink_refuses"):
                    return _err(msg_id, "bad_args", "action sink refused the event")
                return _ok(msg_id, injected=itype, action=args.get("action"))
            return _ok(msg_id, injected=itype)

        if cmd == "seed":
            # VGCP §4.14 (1.5.0): fix the run RNG. Delivered to the registered seed target; the game
            # applies it at its next tick (pattern: seed -> await_signal run_started).
            value = args.get("seed")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or (
                isinstance(value, float) and not value.is_integer()
            ):
                return _err(msg_id, "bad_args", "seed.seed (int) required")
            value = int(value)
            if args.get("mock_no_seed_target") or not state.seed_target:
                return _err(msg_id, "no_seed_target",
                            "no seed target registered (call register_seed_target)")
            state.seed = value
            state.hud["seed"] = value
            return _ok(msg_id, seed=value)

        if cmd == "get_state":
            engine = {
                "paused": state.paused,
                "physics_frame": state.physics_frame,
                "process_frame": state.process_frame,
                "time_scale": state.time_scale,
                "fps": 0.0,
            }
            providers = {"engine": engine, "hud": dict(state.hud)}
            provider = args.get("provider")
            if provider is None:
                return _ok(msg_id, state=providers)
            if provider not in providers:
                return _err(msg_id, "unknown_provider", f"no provider named '{provider}'")
            return _ok(msg_id, provider=provider, state=providers[provider])

        if cmd == "list_providers":
            return _ok(msg_id, providers=["engine", "hud"])

        if cmd == "load_input_script":
            src, err = _resolve_script_source(args)
            if err:
                return _err(msg_id, "bad_args", err)
            if src is None:
                return _err(msg_id, "bad_args",
                            "load_input_script requires 'script' (object) or 'path' (file)")
            summary, perr = parse_input_script(src)
            if perr:
                return _err(msg_id, "bad_args", perr)
            state.script = summary
            return _ok(msg_id, frames=summary["frames"],
                       duration_frames=summary["duration_frames"], events=summary["events"])

        if cmd == "run_input_script":
            src, err = _resolve_script_source(args)
            if err:
                return _err(msg_id, "bad_args", err)
            if src is not None:
                summary, perr = parse_input_script(src)
                if perr:
                    return _err(msg_id, "bad_args", perr)
            elif state.script is not None:
                summary = state.script
            else:
                return _err(msg_id, "bad_args",
                            "no input script loaded (call load_input_script, or pass 'script'/'path')")
            frames_to_run = summary["duration_frames"]
            mf = args.get("max_frames")
            if mf is not None:
                if not isinstance(mf, (int, float)) or mf < 1:
                    return _err(msg_id, "bad_args", "max_frames must be >= 1")
                frames_to_run = min(frames_to_run, int(mf))
            rec, rerr = _record_payload(args)
            if rerr:
                return _err(msg_id, rerr[0], rerr[1])
            # Mimic the real server: unpause, advance frames_to_run physics ticks, re-pause.
            state.paused = False
            time.sleep(min(0.2, frames_to_run * 0.001))
            state.physics_frame += frames_to_run
            state.process_frame += frames_to_run
            state.paused = True
            return _ok(msg_id, frames_run=frames_to_run, physics_frame=state.physics_frame,
                       paused=True, completed=True,
                       skipped=_mock_skipped(summary, frames_to_run, args), **rec)

        if cmd == "clear_input_script":
            state.script = None
            return _ok(msg_id)

        if cmd == "input_script_status":
            loaded = state.script is not None
            total_frames = state.script["duration_frames"] if loaded else 0
            # The mock runs synchronously, so playback is never observed mid-flight.
            return _ok(msg_id, loaded=loaded, playing=False, current_frame=0,
                       total_frames=total_frames)

        if cmd == "await_signal":
            signal = args.get("signal")
            if not isinstance(signal, str) or not signal:
                return _err(msg_id, "bad_args",
                            "await_signal.signal (non-empty string) required")
            timeout = args.get("timeout_ticks")
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 1:
                return _err(msg_id, "bad_args",
                            "await_signal.timeout_ticks (int >= 1) required")
            timeout = int(timeout)
            node, provider = args.get("node"), args.get("provider")
            if (node is None) == (provider is None):
                return _err(msg_id, "bad_args",
                            "await_signal requires exactly one of 'node' or 'provider'")
            # The synthetic `engine` provider has no target object to connect a signal to, so it is
            # NOT an awaitable target; only real registered providers are (the mock models just
            # `hud`). This matches the gdext server, where `engine` is not in its provider map and
            # so resolve_signal_target returns unknown_provider for it.
            if provider is not None and provider not in ("hud",):
                return _err(msg_id, "unknown_provider", f"no provider named '{provider}'")
            # Mock-only deterministic trigger (the real server watches a live Godot signal): the
            # simulated signal "fires" after `mock_fire_after` ticks (default 1) carrying `mock_args`.
            # Set mock_fire_after > timeout_ticks to exercise the timeout branch. The real server
            # ignores these hint keys.
            fire_after = args.get("mock_fire_after", 1)
            if not isinstance(fire_after, (int, float)) or isinstance(fire_after, bool):
                fire_after = 1
            fire_after = int(fire_after)
            mock_args = args.get("mock_args", [])
            if not isinstance(mock_args, list):
                mock_args = []
            rec, rerr = _record_payload(args)
            if rerr:
                return _err(msg_id, rerr[0], rerr[1])
            # Mirror step/run_input_script: unpause, advance ticks, re-pause; reply is "deferred".
            # On timeout the real server advances timeout_ticks + 1 (the §4.10.2 detection grace),
            # so mirror that here for waited_ticks fidelity.
            fired = fire_after <= timeout
            waited = fire_after if fired else timeout + 1
            state.paused = False
            time.sleep(min(0.2, waited * 0.001))
            state.physics_frame += max(0, waited)
            state.process_frame += max(0, waited)
            state.paused = True
            if fired:
                return _ok(msg_id, signal=signal, fired=True,
                           args=[_describe_value(a) for a in mock_args],
                           waited_ticks=waited, physics_frame=state.physics_frame, paused=True, **rec)
            return _ok(msg_id, signal=signal, fired=False, timed_out=True,
                       waited_ticks=waited, physics_frame=state.physics_frame, paused=True, **rec)

        if cmd == "assert":
            provider = args.get("provider")
            if not isinstance(provider, str):
                return _err(msg_id, "bad_args", "assert.provider (string) required")
            op = args.get("op")
            if op not in _PREDICATE_OPS:
                return _err(msg_id, "bad_args", f"assert.op must be one of {list(_PREDICATE_OPS)}")
            pv = _provider_value(state, provider)
            if pv is None:
                return _err(msg_id, "unknown_provider", f"no provider named '{provider}'")
            path, value = args.get("path"), args.get("value")
            passed, actual = _eval_predicate(pv, path, op, value)
            out = {"passed": passed, "provider": provider, "op": op,
                   "value": value, "actual": _describe_value(actual)}
            if path is not None:
                out["path"] = path
            return _ok(msg_id, **out)

        if cmd == "await_state":
            provider = args.get("provider")
            if not isinstance(provider, str):
                return _err(msg_id, "bad_args", "await_state.provider (string) required")
            op = args.get("op")
            if op not in _PREDICATE_OPS:
                return _err(msg_id, "bad_args", f"await_state.op must be one of {list(_PREDICATE_OPS)}")
            timeout = args.get("timeout_ticks")
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 1:
                return _err(msg_id, "bad_args", "await_state.timeout_ticks (int >= 1) required")
            timeout = int(timeout)
            pv = _provider_value(state, provider)
            if pv is None:
                return _err(msg_id, "unknown_provider", f"no provider named '{provider}'")
            rec, rerr = _record_payload(args)
            if rerr:
                return _err(msg_id, rerr[0], rerr[1])
            path, value = args.get("path"), args.get("value")
            _, actual = _eval_predicate(pv, path, op, value)
            # Mock-only deterministic trigger: the mock's provider state is static, so a real "wait
            # until it changes" can't happen, so `mock_hold_after` (default 1) names the tick the
            # predicate is deemed to hold. Set it > timeout_ticks to exercise the timeout branch.
            hold_after = args.get("mock_hold_after", 1)
            if not isinstance(hold_after, (int, float)) or isinstance(hold_after, bool):
                hold_after = 1
            hold_after = int(hold_after)
            held = hold_after <= timeout
            waited = hold_after if held else timeout + 1
            state.paused = False
            time.sleep(min(0.2, waited * 0.001))
            state.physics_frame += max(0, waited)
            state.process_frame += max(0, waited)
            state.paused = True
            out = {"provider": provider, "waited_ticks": waited, "physics_frame": state.physics_frame,
                   "paused": True, "actual": _describe_value(actual), **rec}
            if held:
                out["held"] = True
            else:
                out["held"] = False
                out["timed_out"] = True
            return _ok(msg_id, **out)

        return _err(msg_id, "unknown_cmd", f"unknown cmd: {cmd}")


def serve_conn(state: MockState, conn: socket.socket) -> None:
    reader = conn.makefile("r", encoding="utf-8", newline="\n")
    try:
        for raw in reader:
            line = raw.strip()
            if not line:
                continue
            if len(line) > MAX_LINE:
                resp = _err(None, "frame_too_large", "request exceeds 1 MiB")
            else:
                try:
                    req = json.loads(line)
                    if not isinstance(req, dict):
                        resp = _err(None, "bad_json", "top-level value must be an object")
                    else:
                        resp = handle(state, req)
                except json.JSONDecodeError:
                    resp = _err(None, "bad_json", "invalid JSON")
            conn.sendall((json.dumps(resp, separators=(",", ":")) + "\n").encode("utf-8"))
    except OSError:
        pass
    finally:
        try:
            reader.close()
        finally:
            conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Mock Video Game Control Protocol (VGCP v1) server; no Godot needed.")
    ap.add_argument("--host", default=os.environ.get("VGCP_HOST", DEFAULT_HOST))
    ap.add_argument("--port", type=int, default=int(os.environ.get("VGCP_PORT", DEFAULT_PORT)))
    ap.add_argument("--once", action="store_true", help="serve a single connection then exit")
    ap.add_argument("--quiet", action="store_true")
    ns = ap.parse_args(argv)

    state = MockState()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((ns.host, ns.port))
    srv.listen(8)
    if not ns.quiet:
        print(f"[mock-vgcp] listening on {ns.host}:{ns.port} (paused-by-default)", flush=True)

    try:
        if ns.once:
            conn, _ = srv.accept()
            serve_conn(state, conn)
        else:
            while True:
                conn, _ = srv.accept()
                threading.Thread(target=serve_conn, args=(state, conn), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
