#!/usr/bin/env python3
"""control.py: pure-stdlib driver and CLI for the Video Game Control Protocol (VGCP v1).

Speaks the wire protocol in `../docs/vgcp-protocol.md` over a localhost TCP socket using
newline-delimited JSON. No third-party dependencies: `python3` only.

Usage as a CLI:
    python3 control.py ping
    python3 control.py pause
    python3 control.py resume
    python3 control.py step --ticks 30
    python3 control.py set_timescale --value 3.0
    python3 control.py screenshot --path /tmp/s.png --downscale 0.5
    python3 control.py input --type action --action jump --pressed true
    python3 control.py input --type mouse_button --x 100 --y 200        # full click
    python3 control.py input --type key --keycode 4194305 --pressed true
    python3 control.py input --type game_action --action move_to --payload '{"x":300}'
    python3 control.py input --type game_action --action pause          # payload optional
    python3 control.py seed --value 42                                  # fix the run RNG (VGCP 1.5.0)
    python3 control.py get_state [--provider hud] [--query '{"k":1}']
    python3 control.py list_providers
    python3 control.py load-input-script --path script.json
    python3 control.py run-input-script [--path script.json] [--max-frames 60]   # reply lists `skipped` events (1.5.1)
    python3 control.py clear-input-script
    python3 control.py input-script-status
    python3 control.py await-signal --signal game_over --timeout-ticks 4000 --provider game
    python3 control.py await-signal --signal phase_changed --timeout-ticks 300 --node /root/Main
    python3 control.py assert --provider hud --path lives --op eq --value 3
    python3 control.py await-state --provider game --path phase --op eq --value lost --timeout-ticks 600
    python3 control.py raw '{"cmd":"ping"}'

Connection:
    --host / --port, or env VGCP_HOST (default 127.0.0.1) and
    VGCP_PORT (default 38787).

Exit code: 0 if the response had ok=true, 1 otherwise (or on transport error). For `assert`,
0 only when the predicate passed.

Usable as a library too:
    from control import VgcpClient
    with VgcpClient() as c:
        print(c.ping())
        c.step(ticks=10)
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from typing import Any, Optional

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 38787
PROTOCOL_MAJOR = 1


class VgcpError(RuntimeError):
    """Raised when the server returns an error envelope (ok=false)."""

    def __init__(self, code: str, message: str, response: dict):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.response = response


class VgcpClient:
    """A synchronous, one-request-in-flight VGCP client over a single TCP connection."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        timeout: float = 10.0,
    ):
        self.host = host or os.environ.get("VGCP_HOST", DEFAULT_HOST)
        self.port = port or int(os.environ.get("VGCP_PORT", DEFAULT_PORT))
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._reader = None  # file-like for line reads
        self._next_id = 0

    # -- connection lifecycle ----------------------------------------------------------
    def connect(self) -> "VgcpClient":
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        # makefile gives us robust newline framing without manual buffering.
        self._reader = self._sock.makefile("r", encoding="utf-8", newline="\n")
        return self

    def close(self) -> None:
        try:
            if self._reader is not None:
                self._reader.close()
        finally:
            if self._sock is not None:
                self._sock.close()
            self._sock = None
            self._reader = None

    def __enter__(self) -> "VgcpClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- core request/response ---------------------------------------------------------
    def request(
        self,
        cmd: str,
        args: Optional[dict] = None,
        *,
        timeout: Optional[float] = None,
        raise_on_error: bool = True,
    ) -> dict:
        """Send one command, block until the response with the matching id arrives."""
        if self._sock is None:
            self.connect()
        self._next_id += 1
        msg_id = self._next_id
        payload = {"id": msg_id, "cmd": cmd}
        if args:
            payload["args"] = args
        # Compact, single-line, newline-terminated (NDJSON). ensure_ascii keeps it 1-line-safe.
        line = json.dumps(payload, separators=(",", ":")) + "\n"

        # `step`, `run_input_script`, `await_signal` and `await_state` defer their reply until the
        # ticks/frames elapse; widen the read timeout for them (an await can run to its full tick
        # budget).
        eff_timeout = timeout
        if eff_timeout is None:
            eff_timeout = self.timeout
            if cmd == "step":
                ticks = (args or {}).get("ticks", 1)
                eff_timeout = max(self.timeout, ticks / 60.0 * 4.0 + 5.0)
            elif cmd == "run_input_script":
                frames = (args or {}).get("max_frames", 600)
                eff_timeout = max(self.timeout, frames / 60.0 * 4.0 + 5.0)
            elif cmd in ("await_signal", "await_state"):
                ticks = (args or {}).get("timeout_ticks", 600)
                eff_timeout = max(self.timeout, ticks / 60.0 * 4.0 + 5.0)

        assert self._sock is not None
        self._sock.settimeout(eff_timeout)
        self._sock.sendall(line.encode("utf-8"))

        # Read lines until we find our id (responses are matched by id, never by order).
        while True:
            raw = self._reader.readline()  # type: ignore[union-attr]
            if raw == "":
                raise VgcpError("connection_closed", "server closed the connection", {})
            raw = raw.strip()
            if not raw:
                continue
            try:
                resp = json.loads(raw)
            except json.JSONDecodeError as e:
                raise VgcpError("bad_response", f"server sent non-JSON: {raw!r} ({e})", {})
            if resp.get("id") != msg_id:
                # Out-of-order / stray frame for a different request id; keep reading.
                continue
            if raise_on_error and not resp.get("ok", False):
                err = resp.get("error", {}) or {}
                raise VgcpError(err.get("code", "error"), err.get("message", "unknown"), resp)
            return resp

    # -- typed convenience wrappers ----------------------------------------------------
    def ping(self) -> dict:
        resp = self.request("ping")
        major = resp.get("protocol_major")
        if major is not None and major != PROTOCOL_MAJOR:
            raise VgcpError(
                "protocol_mismatch",
                f"server protocol_major={major}, client supports {PROTOCOL_MAJOR}",
                resp,
            )
        return resp

    def pause(self) -> dict:
        return self.request("pause")

    def resume(self) -> dict:
        return self.request("resume")

    def step(self, ticks: int = 1, mode: str = "budget",
             record_signals: Optional[list] = None, **extra) -> dict:
        args: dict[str, Any] = {"ticks": ticks, "mode": mode}
        if record_signals is not None:
            args["record_signals"] = record_signals
        args.update(extra)  # mock-only hints (mock_recorded, ...) pass through; the server ignores them
        return self.request("step", args)

    def set_timescale(self, value: float) -> dict:
        return self.request("set_timescale", {"value": value})

    def screenshot(self, path: Optional[str] = None, downscale: float = 1.0) -> dict:
        args: dict[str, Any] = {}
        if path:
            args["path"] = path
        if downscale != 1.0:
            args["downscale"] = downscale
        return self.request("screenshot", args)

    def input(self, **kwargs) -> dict:
        return self.request("input", kwargs)

    # -- canonical game actions + seeding (VGCP §4.6 / §4.14, 1.5.0) --------------------
    def game_action(self, action: str, payload: Optional[dict] = None, **extra) -> dict:
        """Inject ONE canonical, device-agnostic game action (the record the game actually consumes).
        Delivered straight to the game's registered action sink; unlike a synthetic InputEvent it is
        NOT swallowed while the tree is paused, so `game_action` then `step(1)` always works.
        `payload` is the action's typed fields (e.g. {"x": 300} for a `move_to` action); omit for
        none. `**extra` forwards mock-only hint keys (the real server ignores them)."""
        args: dict[str, Any] = {"type": "game_action", "action": action}
        if payload is not None:
            args["payload"] = payload
        args.update(extra)
        return self.request("input", args)

    def seed(self, value: int, **extra) -> dict:
        """Fix the run RNG (VGCP §4.14). The reply is immediate (`{ok, seed}`); the game applies the
        seed at its NEXT tick, so the usual pattern is `seed(N)`, then `await_signal` on the signal
        the game emits when a run starts (e.g. `await_signal("run_started", ...)`)."""
        args: dict[str, Any] = {"seed": value}
        args.update(extra)
        return self.request("seed", args)

    def get_state(self, provider: Optional[str] = None, query: Any = None) -> dict:
        args: dict[str, Any] = {}
        if provider:
            args["provider"] = provider
        if query is not None:
            args["query"] = query
        return self.request("get_state", args)

    def list_providers(self) -> dict:
        return self.request("list_providers")

    # -- input scripts (VGCP §4.9) ------------------------------------------------------
    def load_input_script(
        self, script: Optional[dict] = None, path: Optional[str] = None
    ) -> dict:
        args: dict[str, Any] = {}
        if script is not None:
            args["script"] = script
        if path is not None:
            args["path"] = path
        return self.request("load_input_script", args)

    def run_input_script(
        self,
        script: Optional[dict] = None,
        path: Optional[str] = None,
        max_frames: Optional[int] = None,
        record_signals: Optional[list] = None,
        **extra,
    ) -> dict:
        args: dict[str, Any] = {}
        if script is not None:
            args["script"] = script
        if path is not None:
            args["path"] = path
        if max_frames is not None:
            args["max_frames"] = max_frames
        if record_signals is not None:
            args["record_signals"] = record_signals
        args.update(extra)
        return self.request("run_input_script", args)

    def clear_input_script(self) -> dict:
        return self.request("clear_input_script")

    def input_script_status(self) -> dict:
        return self.request("input_script_status")

    # -- await a Godot signal with a required tick timeout (VGCP §4.10) -----------------
    def await_signal(
        self,
        signal: str,
        timeout_ticks: int,
        *,
        node: Optional[str] = None,
        provider: Optional[str] = None,
        record_signals: Optional[list] = None,
        **extra: Any,
    ) -> dict:
        """Advance the world watching for `signal` (on the `node` NodePath or registered
        `provider`'s object), re-pausing on fire (`fired:true` + structured `args`) or after
        `timeout_ticks` physics ticks (`timed_out:true`). `timeout_ticks` is REQUIRED.
        `record_signals` (VGCP §4.11) optionally logs other signals during the wait. `**extra`
        forwards mock-only hint keys (the real server ignores them)."""
        args: dict[str, Any] = {"signal": signal, "timeout_ticks": timeout_ticks}
        if node is not None:
            args["node"] = node
        if provider is not None:
            args["provider"] = provider
        if record_signals is not None:
            args["record_signals"] = record_signals
        args.update(extra)
        return self.request("await_signal", args)

    # -- state predicates (VGCP §4.12 / §4.13) ------------------------------------------
    def assert_(
        self,
        provider: str,
        op: str,
        *,
        path: Optional[str] = None,
        value: Any = None,
        query: Any = None,
    ) -> dict:
        """Evaluate a narrow predicate against a provider's current state. Returns the reply with
        `passed` (and typed `actual`). `value` is sent only when not None."""
        args: dict[str, Any] = {"provider": provider, "op": op}
        if path is not None:
            args["path"] = path
        if value is not None:
            args["value"] = value
        if query is not None:
            args["query"] = query
        return self.request("assert", args)

    def await_state(
        self,
        provider: str,
        op: str,
        timeout_ticks: int,
        *,
        path: Optional[str] = None,
        value: Any = None,
        query: Any = None,
        record_signals: Optional[list] = None,
        **extra,
    ) -> dict:
        """Advance the world until a narrow predicate over `provider`'s state holds (`held:true`) or
        `timeout_ticks` physics ticks pass (`timed_out:true`). `timeout_ticks` is REQUIRED."""
        args: dict[str, Any] = {"provider": provider, "op": op, "timeout_ticks": timeout_ticks}
        if path is not None:
            args["path"] = path
        if value is not None:
            args["value"] = value
        if query is not None:
            args["query"] = query
        if record_signals is not None:
            args["record_signals"] = record_signals
        args.update(extra)
        return self.request("await_state", args)


# -- CLI ------------------------------------------------------------------------------------

def _parse_bool(s: str) -> bool:
    return str(s).lower() in ("1", "true", "yes", "on", "y", "t")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="control.py",
        description="Drive a game that runs a VGCP (Video Game Control Protocol) v1 server."
    )
    p.add_argument("--host", default=None, help="server host (env VGCP_HOST)")
    p.add_argument("--port", type=int, default=None, help="server port (env VGCP_PORT)")
    p.add_argument("--timeout", type=float, default=10.0, help="socket timeout seconds")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="liveness + version handshake")
    sub.add_parser("pause", help="freeze the game (paused=true)")
    sub.add_parser("resume", help="free-run (paused=false)")
    sub.add_parser("list_providers", help="list registered state providers")

    sp = sub.add_parser("step", help="advance exactly N physics ticks, then re-pause")
    sp.add_argument("ticks_pos", nargs="?", type=int, default=None, metavar="TICKS",
                    help="same as --ticks (step 30 == step --ticks 30)")
    sp.add_argument("--ticks", type=int, default=None, help="ticks to advance (default 1)")
    sp.add_argument("--mode", default="budget", choices=["budget", "blocking"])
    sp.add_argument("--record-signals", dest="record_signals", default=None,
                    help="JSON array of {node|provider, signals?} specs to log during the advance")

    sp = sub.add_parser("set_timescale", help="set Engine.time_scale (fast-forward / slow-mo)")
    sp.add_argument("value_pos", nargs="?", type=float, default=None, metavar="VALUE",
                    help="same as --value")
    sp.add_argument("--value", type=float, default=None)

    sp = sub.add_parser("screenshot", help="capture the (paused) viewport after a real draw")
    sp.add_argument(
        "--path",
        default=None,
        help="where to write the PNG; omitted, the server names one under VGCP_SHOTS_DIR "
        "(default <temp>/vgcp_tmp)",
    )
    sp.add_argument("--downscale", type=float, default=1.0)

    sp = sub.add_parser("get_state", help="query registered state providers")
    sp.add_argument("--provider", default=None)
    sp.add_argument("--query", default=None, help="JSON value forwarded to the provider")

    sp = sub.add_parser("input", help="inject one input event")
    sp.add_argument("--type", dest="itype", required=True,
                    choices=["action", "key", "mouse_button", "mouse_move", "game_action"])
    sp.add_argument("--action", default=None,
                    help="action name (type=action: an InputMap action; type=game_action: a "
                         "canonical game action such as move_to / jump / pause)")
    sp.add_argument("--payload", default=None,
                    help="type=game_action: the action's fields as a JSON object, e.g. '{\"x\":300}'")
    sp.add_argument("--pressed", default=None, help="true/false; omit on mouse_button for a full click")
    sp.add_argument("--strength", type=float, default=None)
    sp.add_argument("--keycode", type=int, default=None)
    sp.add_argument("--physical", default=None)
    sp.add_argument("--x", type=float, default=None)
    sp.add_argument("--y", type=float, default=None)
    sp.add_argument("--button", type=int, default=None)

    sp = sub.add_parser("seed", help="fix the run RNG (applied by the game at its next tick)")
    sp.add_argument("value_pos", nargs="?", type=int, default=None, metavar="SEED",
                    help="same as --value (seed 42 == seed --value 42)")
    sp.add_argument("--value", type=int, default=None, help="the integer seed")

    sp = sub.add_parser("load-input-script", aliases=["load_input_script"],
                        help="validate + store a frame-by-frame input script")
    sp.add_argument("--path", default=None, help="path to a script JSON file")
    sp.add_argument("--script", default=None, help="inline script JSON (object)")

    sp = sub.add_parser("run-input-script", aliases=["run_input_script"],
                        help="replay the loaded script (or --path/--script), then re-pause")
    sp.add_argument("--path", default=None, help="path to a script JSON file (override)")
    sp.add_argument("--script", default=None, help="inline script JSON (override)")
    sp.add_argument("--max-frames", dest="max_frames", type=int, default=None,
                    help="cap playback to this many frames")
    sp.add_argument("--record-signals", dest="record_signals", default=None,
                    help="JSON array of {node|provider, signals?} specs to log during playback")

    sub.add_parser("clear-input-script", aliases=["clear_input_script"],
                   help="drop the stored input script")
    sub.add_parser("input-script-status", aliases=["input_script_status"],
                   help="report loaded/playing status + cursor")

    sp = sub.add_parser("await-signal", aliases=["await_signal"],
                        help="advance until a Godot signal fires or a REQUIRED tick timeout elapses")
    sp.add_argument("--signal", required=True, help="signal name on the target")
    sp.add_argument("--timeout-ticks", dest="timeout_ticks", type=int, required=True,
                    help="REQUIRED: max physics ticks to wait before timing out")
    sp.add_argument("--node", default=None, help="NodePath of the emitter, e.g. /root/Main/Player")
    sp.add_argument("--provider", default=None,
                    help="registered provider whose target object emits the signal")
    sp.add_argument("--record-signals", dest="record_signals", default=None,
                    help="JSON array of {node|provider, signals?} specs to log while awaiting")

    for name, deferred in (("assert", False), ("await-state", True)):
        sp = sub.add_parser(name, help=("advance until a state predicate holds, or a tick timeout"
                                        if deferred else "check a state predicate against a provider"))
        sp.add_argument("--provider", required=True,
                        help="provider to read: engine, or one the game registers (e.g. hud)")
        sp.add_argument("--op", required=True,
                        choices=["eq", "ne", "lt", "le", "gt", "ge", "in", "contains", "exists", "truthy"])
        sp.add_argument("--path", default=None,
                        help="dotted path into the state, e.g. phase / player.x / items.0.id")
        sp.add_argument("--value", default=None, help="RHS literal (JSON, or a bare string)")
        sp.add_argument("--query", default=None, help="JSON value forwarded to the provider")
        if deferred:
            sp.add_argument("--timeout-ticks", dest="timeout_ticks", type=int, required=True,
                            help="REQUIRED: max physics ticks to wait")

    sp = sub.add_parser("raw", help="send a raw JSON request object")
    sp.add_argument("json", help='e.g. \'{"cmd":"ping"}\'')

    return p


def _input_args(ns: argparse.Namespace) -> dict:
    args: dict[str, Any] = {"type": ns.itype}
    if ns.itype == "action":
        args["action"] = ns.action
        if ns.pressed is not None:
            args["pressed"] = _parse_bool(ns.pressed)
        if ns.strength is not None:
            args["strength"] = ns.strength
    elif ns.itype == "key":
        args["keycode"] = ns.keycode
        if ns.pressed is not None:
            args["pressed"] = _parse_bool(ns.pressed)
        if ns.physical is not None:
            args["physical"] = _parse_bool(ns.physical)
    elif ns.itype == "mouse_button":
        args["x"] = ns.x
        args["y"] = ns.y
        if ns.button is not None:
            args["button"] = ns.button
        if ns.pressed is not None:
            args["pressed"] = _parse_bool(ns.pressed)
    elif ns.itype == "mouse_move":
        args["x"] = ns.x
        args["y"] = ns.y
    elif ns.itype == "game_action":
        args["action"] = ns.action
        if ns.payload is not None:
            payload = json.loads(ns.payload)
            if not isinstance(payload, dict):
                raise ValueError("--payload must be a JSON object, e.g. '{\"x\":300}'")
            args["payload"] = payload
    return args


def _add_record_signals(args: dict, ns: argparse.Namespace) -> None:
    """Parse the --record-signals JSON (an array of watch specs) into args, if given."""
    rs = getattr(ns, "record_signals", None)
    if rs:
        args["record_signals"] = json.loads(rs)


def _json_or_str(s: Optional[str]) -> Any:
    """A --value / --query argument: parse as JSON, falling back to the raw string (so `--value lost`
    is the string "lost", `--value 8` is the int 8, `--value true` is a bool)."""
    if s is None:
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return s


def _predicate_args(ns: argparse.Namespace) -> dict:
    """Build wire args shared by `assert` / `await-state` from --provider/--op/--path/--value/--query."""
    args: dict[str, Any] = {"provider": ns.provider, "op": ns.op}
    if ns.path is not None:
        args["path"] = ns.path
    if ns.value is not None:
        args["value"] = _json_or_str(ns.value)
    if ns.query is not None:
        args["query"] = _json_or_str(ns.query)
    return args


def _script_args(ns: argparse.Namespace, *, with_max: bool) -> dict:
    """Build wire args for load/run-input-script from --path / --script / --max-frames."""
    args: dict[str, Any] = {}
    if getattr(ns, "path", None):
        args["path"] = ns.path
    if getattr(ns, "script", None):
        args["script"] = json.loads(ns.script)
    if with_max and getattr(ns, "max_frames", None) is not None:
        args["max_frames"] = ns.max_frames
    if with_max:  # run-input-script (not load) supports record_signals
        _add_record_signals(args, ns)
    return args


def _merge_positionals(parser: argparse.ArgumentParser, ns: argparse.Namespace) -> argparse.Namespace:
    """step / seed / set_timescale accept their one value as a positional or a flag, since both
    forms are natural to type. Giving both with different values is an error."""
    for pos, flag, default, required in (("ticks_pos", "ticks", 1, False), ("value_pos", "value", None, True)):
        if not hasattr(ns, pos):
            continue
        p, f = getattr(ns, pos), getattr(ns, flag)
        if p is not None and f is not None and p != f:
            parser.error(f"{ns.cmd}: positional {p} and --{flag} {f} disagree; give one")
        v = f if f is not None else p
        if v is None:
            if required:
                parser.error(f"{ns.cmd}: a value is required (e.g. `{ns.cmd} 42` or `{ns.cmd} --{flag} 42`)")
            v = default
        setattr(ns, flag, v)
    return ns


def parse_cli(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = build_parser()
    return _merge_positionals(parser, parser.parse_args(argv))


def main(argv: Optional[list[str]] = None) -> int:
    ns = parse_cli(argv)
    client = VgcpClient(host=ns.host, port=ns.port, timeout=ns.timeout)
    try:
        client.connect()
    except OSError as e:
        print(json.dumps({"ok": False, "error": {"code": "connect_failed", "message": str(e)}}))
        return 1

    # Subcommand names may be hyphenated (e.g. load-input-script); wire commands use underscores.
    wire_cmd = ns.cmd.replace("-", "_")
    try:
        if ns.cmd == "raw":
            obj = json.loads(ns.json)
            resp = client.request(obj.get("cmd", ""), obj.get("args"), raise_on_error=False)
        elif wire_cmd == "load_input_script":
            resp = client.request("load_input_script", _script_args(ns, with_max=False),
                                  raise_on_error=False)
        elif wire_cmd == "run_input_script":
            resp = client.request("run_input_script", _script_args(ns, with_max=True),
                                  raise_on_error=False)
        elif wire_cmd == "await_signal":
            a = {"signal": ns.signal, "timeout_ticks": ns.timeout_ticks}
            if ns.node:
                a["node"] = ns.node
            if ns.provider:
                a["provider"] = ns.provider
            _add_record_signals(a, ns)
            resp = client.request("await_signal", a, raise_on_error=False)
        elif wire_cmd == "assert":
            resp = client.request("assert", _predicate_args(ns), raise_on_error=False)
        elif wire_cmd == "await_state":
            a = _predicate_args(ns)
            a["timeout_ticks"] = ns.timeout_ticks
            resp = client.request("await_state", a, raise_on_error=False)
        elif ns.cmd == "input":
            resp = client.request("input", _input_args(ns), raise_on_error=False)
        elif ns.cmd == "step":
            a = {"ticks": ns.ticks, "mode": ns.mode}
            _add_record_signals(a, ns)
            resp = client.request("step", a, raise_on_error=False)
        elif ns.cmd == "seed":
            resp = client.request("seed", {"seed": ns.value}, raise_on_error=False)
        elif ns.cmd == "set_timescale":
            resp = client.request("set_timescale", {"value": ns.value}, raise_on_error=False)
        elif ns.cmd == "screenshot":
            a: dict[str, Any] = {}
            if ns.path:
                a["path"] = ns.path
            if ns.downscale != 1.0:
                a["downscale"] = ns.downscale
            resp = client.request("screenshot", a, raise_on_error=False)
        elif ns.cmd == "get_state":
            a = {}
            if ns.provider:
                a["provider"] = ns.provider
            if ns.query is not None:
                a["query"] = json.loads(ns.query)
            resp = client.request("get_state", a, raise_on_error=False)
        else:
            # ping / pause / resume / list_providers / clear-input-script / input-script-status
            resp = client.request(wire_cmd, raise_on_error=False)
    except (VgcpError, OSError, ValueError) as e:
        print(json.dumps({"ok": False, "error": {"code": "client_error", "message": str(e)}}))
        return 1
    finally:
        client.close()

    print(json.dumps(resp, indent=2, sort_keys=True))
    # VGCP 1.5.1: a playback that dropped events still says completed:true. Say so on stderr, so
    # whoever reads the CLI output cannot miss it (stdout stays the plain JSON reply).
    if wire_cmd == "run_input_script" and resp.get("skipped"):
        n = len(resp["skipped"])
        print(f"[control] WARNING: run_input_script skipped {n} event{'s' if n != 1 else ''} "
              "(see 'skipped' in the reply)", file=sys.stderr)
    # `assert` is ok:true even when the predicate is false; the exit code reflects `passed`, so the
    # CLI is usable as a test gate. (await_state stays ok-based; `held:false` is still a valid reply.)
    if wire_cmd == "assert" and resp.get("ok", False):
        return 0 if resp.get("passed", False) else 1
    return 0 if resp.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
