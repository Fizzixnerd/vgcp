#!/usr/bin/env python3
"""harness.py: run every test script locally, with no agent, and triage the failures.

Part of the test system for the Video Game Control Protocol (VGCP). It discovers
`<GAME_DIR>/tests/vgcp/*.vgcp.json` (`GAME_DIR` is `vgcp.json`'s `game_dir`, see `provenance.py`),
launches a fresh, isolated game per test with `launch_game.sh` from `../virtual-display/` (into
`meta.scene` when the script names one, else the project's main scene), runs the script with
`runner.py`, and reports pass or fail per test. On a PASS it stamps a per-checkout "last green"
record; on a FAIL it prints a structured **triage packet** (which step, the reply, and
`git diff --stat` since the last green commit) so a person or an agent can tell *which layer*
broke.

Local-first and safe across worktrees:
  * the last-green record lives UNTRACKED in the per-worktree git dir
    (`$(git rev-parse --git-dir)/vgcp-test/`), so it never touches the working tree and every
    worktree or branch has its own "last green";
  * the VGCP port and the Xvfb display are derived from the worktree path, so two harnesses in two
    worktrees do not fight over a socket, a framebuffer or a `/tmp` log.

Usage:
    harness.py                 # run every test
    harness.py --test <id>     # run one test by meta.id (the triage loop)
    harness.py --list          # list discovered tests
    harness.py --stamps        # which recordings are older than HEAD, and which were made dirty
    harness.py --play [SCENE] [--mode MODE]      # ad-hoc game, held until terminated

The game's own settings come from `vgcp.json` (`provenance.CONFIG`, read at call time). With a
`mode` there, each test's `meta.mode` (one of `mode.values`, default the first) is exported as
`mode.env` for that launch, so the mode a test runs in is a property of the script, not of the
shell; with none, a `meta.mode` fails the test.

A test launch never inherits the shell's scenario: every inherited `VGCP_*` (the control plane's)
key, every key with the game's `env_prefix`, and every key `vgcp.json` names is dropped except the
lane keys (`LANE_ENV_KEYS`), then the isolation keys, `meta.env` and `meta.mode` are applied.
`meta.env` is an allow-list (`scenario_env_keys()`, the game's `scenario_env`): any other key fails
the test before launch. More launch properties a script may declare:
  * `meta.replays` (int >= 1, default 1): launch a fresh game that many times, require every replay
    to pass, and require every step's `recorded` list to be identical across replays.
  * `meta.godot_args` (allow-listed pairs `--max-fps N` / `--frame-delay N` / `--resolution WxH`):
    engine arguments passed after the launcher's `--`, e.g. to force several physics ticks per engine
    iteration, or to open the window at a given pixel size.
  * `meta.replay_godot_args` (an array of arg lists, one per replay, length == `meta.replays`,
    each validated like `meta.godot_args`): appended to `meta.godot_args` for that replay only, so
    one script replays at several window sizes and the recorded lists are compared across them.
    A replay whose args carry `--resolution WxH` is checked to have really opened at WxH (the
    screenshot reply's `w`/`h`) before its script runs.
Ad-hoc launches (`--play`, `managed_game`) still inherit the shell, so a person can steer them.

Every script also carries `meta.recorded_against`: the commit the recording was made against, and
whether code was uncommitted then (record from a commit, so that it never is). On a FAIL the
triage packet repeats it (`recorded_against.stamp`): that commit is the good endpoint for a bisect.
The harness never runs a bisect itself. A recording older than HEAD is normal: staleness is
REPORTED (`harness.py --stamps`), never failed, and never blocks a merge.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import glob
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "vgcp-mcp"))
from control import VgcpClient, VgcpError  # noqa: E402
from runner import load_script, run_script  # noqa: E402
import provenance  # noqa: E402  (the recorded_against stamp and vgcp.json)

# Where things are. The game (its Godot project and its `tests/vgcp/` scripts) is in GAME_DIR,
# relative to the root of the checkout the harness runs in; provenance owns it (vgcp.json's
# `game_dir`; `VGCP_GAME_DIR` overrides it). The launcher is the one beside these tools, in
# `virtual-display/`, by its absolute path. Both are read at call time, so a caller may point
# LAUNCHER at another launcher.
GAME_DIR = provenance.GAME_DIR
LAUNCHER = os.path.normpath(os.path.join(_HERE, "..", "virtual-display", "launch_game.sh"))


def _git(*args: str, cwd: Optional[str] = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout.strip()


def repo_root() -> str:
    """The checkout the harness runs in: the current directory's. The game directory is always
    joined to it, never to the directory `vgcp.json` was found in (a tool may run these tools
    from another checkout, inside a worktree of the commit under test)."""
    return _git("rev-parse", "--show-toplevel") or os.getcwd()


def godot_project(root: str) -> str:
    """The Godot project of the game in `root`: `<GAME_DIR>` when it holds `project.godot`, else
    `<GAME_DIR>/godot`."""
    game = os.path.normpath(os.path.join(root, GAME_DIR))
    return game if os.path.isfile(os.path.join(game, "project.godot")) else os.path.join(game, "godot")


# ---- per-worktree, untracked state: last-green record, lane pin, profiles -------------------
def _state_dir() -> str:
    """`<git-dir>/vgcp-test/`, created on first use. The harness keeps per-checkout state there, so
    it refuses to run outside a git checkout rather than invent a `.git` of its own."""
    gitdir = _git("rev-parse", "--git-dir")
    if not gitdir:
        raise RuntimeError(f"not inside a git checkout ({os.getcwd()}): the harness keeps its "
                           "per-checkout state in <git-dir>/vgcp-test/, so run it from the game's "
                           "repository")
    d = os.path.join(os.path.abspath(gitdir), "vgcp-test")
    os.makedirs(d, exist_ok=True)
    return d


def _last_green_path() -> str:
    return os.path.join(_state_dir(), "last-verified.json")


def _load_last_green() -> dict:
    p = _last_green_path()
    try:
        return json.load(open(p)) if os.path.exists(p) else {}
    except (OSError, ValueError):
        return {}


def _stamp_last_green(test_id: str) -> None:
    rec = _load_last_green()
    rec[test_id] = {"commit": _git("rev-parse", "HEAD", cwd=repo_root()),
                    "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    json.dump(rec, open(_last_green_path(), "w"), indent=2)


# ---- worktree-derived runtime isolation -------------------------------------------------
def _bases() -> tuple[int, int]:
    """A VGCP port + Xvfb display number derived from the worktree path, so concurrent worktrees
    pick non-colliding bases. Override with --port-base / --display-base."""
    # A lane may pin its bases once per worktree: $(git rev-parse --git-dir)/vgcp-test/lane.json
    # = {"port_base": N, "display_base": M}. Untracked, next to the last-green record; lets every harness /
    # managed_game call in that worktree stay lane-safe without flags or env.
    lane = os.path.join(os.path.dirname(_last_green_path()), "lane.json")
    try:
        with open(lane) as f:
            d = json.load(f)
        return int(d["port_base"]), int(d["display_base"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    h = int(hashlib.sha1(repo_root().encode()).hexdigest(), 16)
    return 38800 + (h % 150), 90 + (h % 8)  # port 38800..38949, display :90..:97


# ---- launch / drive / kill --------------------------------------------------------------
# Signal-safe teardown registry. Every game launch_game.sh starts for us is a setsid PROCESS-GROUP
# LEADER (pgid == pid), so we reap it by GROUP, killing godot plus any child it spawned. Every
# Xvfb the launcher starts for us is reported on its stderr as `[launch] XVFB_PID=<pid>`; we reap
# only those (reused displays don't report one, so a concurrent worktree's Xvfb is never touched).
# A killed harness (SIGTERM/SIGINT/SIGHUP) or any abnormal exit runs `_reap_all` via the signal
# handlers + atexit below, so it never leaks an orphaned godot or Xvfb that would pile up and pin
# the CPU.
_LIVE_GAME_PIDS: set[int] = set()   # setsid game-group leaders we launched
_XVFB_PIDS: set[int] = set()        # Xvfb procs launch_game.sh started for us


def _term_then_kill(target: int, *, group: bool) -> None:
    """SIGTERM, wait briefly for exit, then SIGKILL a pid or a process group. Idempotent."""
    send = (lambda s: os.killpg(target, s)) if group else (lambda s: os.kill(target, s))
    try:
        send(signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(20):                       # up to ~2s for a graceful shutdown
        time.sleep(0.1)
        try:
            send(0)                            # probe: alive?
        except ProcessLookupError:
            return
    try:
        send(signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# The game's keys come from vgcp.json (provenance.CONFIG), read at call time, never copied into a
# constant here. Everything else in the shell (`PATH`, `HOME`, `TMPDIR`, the graphics variables
# `gfx-env.sh` sets) is inherited untouched, so the game and the server still write their
# temporary files where `TMPDIR` says.
def _mode_config() -> Optional[dict]:
    """vgcp.json's `mode` (`{"env", "values"}`), or None when the game has no boot mode."""
    return provenance.CONFIG.get("mode")


def env_prefixes() -> tuple[str, ...]:
    """The prefixes a test launch scrubs: the control plane's (`VGCP_`) and the game's `env_prefix`."""
    prefix = provenance.CONFIG.get("env_prefix")
    return ("VGCP_", prefix) if prefix else ("VGCP_",)


def scenario_env_keys() -> tuple[str, ...]:
    """The keys a test's `meta.env` may set: the scenario a script declares, vgcp.json's
    `scenario_env`. The boot mode is `meta.mode`'s and the isolation keys are the harness's; a typo
    or any other key fails the test before launch rather than silently changing (or not changing)
    the scenario."""
    return tuple(provenance.CONFIG.get("scenario_env") or ())


def isolation_keys() -> tuple[str, ...]:
    """Keys only the harness sets; never overridable (not even by an ad-hoc `managed_game(env=...)`):
    the VGCP endpoint, and the game's `profile_env` and `mode.env` when it has them."""
    game = (provenance.CONFIG.get("profile_env"), (_mode_config() or {}).get("env"))
    return ("VGCP_PORT", "VGCP_ADDR", "VGCP_HOST", *(k for k in game if k))


# Inherited keys a TEST launch keeps: they choose the render lane (display / software Vulkan /
# driver flags), not the scenario. Every other inherited `VGCP_*` key, and every game key, is dropped.
LANE_ENV_KEYS = ("VGCP_XVFB_DISPLAY", "VGCP_DISPLAY", "VGCP_SOFTWARE",
                 "VGCP_DRIVER_FLAGS")
# `meta.godot_args` flags a script may pass to the engine, each followed by its value: a non-negative
# integer, or `WxH` (two positive integers) for `--resolution` (the window's pixel size).
GODOT_ARG_FLAGS = ("--max-fps", "--frame-delay", "--resolution")
_RESOLUTION_FLAG = "--resolution"


def _valid_arg_value(flag: str, value: str) -> bool:
    if flag == _RESOLUTION_FLAG:
        w, sep, h = value.partition("x")
        return bool(sep) and w.isdigit() and h.isdigit() and int(w) > 0 and int(h) > 0
    return value.isdigit()


def _validate_arg_list(raw: Any, where: str) -> tuple[list[str], Optional[str]]:
    """One engine arg list: a flat list of allow-listed `FLAG VALUE` pairs, each flag at most once."""
    if not isinstance(raw, list) or not all(isinstance(a, str) for a in raw):
        return [], f"{where} must be an array of strings, got {raw!r}"
    if len(raw) % 2:
        return [], f"{where} must be FLAG VALUE pairs, got {raw!r}"
    seen = set()
    for flag, value in zip(raw[::2], raw[1::2]):
        if flag not in GODOT_ARG_FLAGS:
            return [], f"{where} flag '{flag}' is not allow-listed (allowed: {', '.join(GODOT_ARG_FLAGS)})"
        if flag in seen:
            return [], f"{where} names {flag} twice"
        seen.add(flag)
        if not _valid_arg_value(flag, value):
            want = "WxH (two positive integers)" if flag == _RESOLUTION_FLAG else "a non-negative integer"
            return [], f"{where} {flag} needs {want}, got {value!r}"
    return list(raw), None


def meta_mode(meta: dict) -> tuple[Optional[str], Optional[str]]:
    """`meta.mode`, validated against vgcp.json's `mode.values`: `(mode, error)`. Absent, it is the
    first value. A game with no `mode` configured has no boot mode: the mode is None, and a
    `meta.mode` is an error."""
    cfg = _mode_config()
    if not cfg:
        if "mode" in meta:
            return None, f"meta.mode is {meta['mode']!r}, but vgcp.json configures no mode"
        return None, None
    values = cfg["values"]
    mode = meta.get("mode", values[0])
    if mode not in values:
        return None, f"meta.mode must be {'|'.join(values)}, got {mode!r}"
    return mode, None


def scenario_env(meta: dict) -> tuple[dict, Optional[str]]:
    """The environment a test's `meta.env` declares, validated against `scenario_env_keys()`.
    Returns `(env, error)`; a non-None error names the offending key and the test must not launch."""
    raw = meta.get("env")
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, f"meta.env must be an object, got {raw!r}"
    allowed = scenario_env_keys()
    out = {}
    for k, v in raw.items():
        if k not in allowed:
            why = ("the boot mode is meta.mode's" if k == (_mode_config() or {}).get("env") else
                   "it is the harness's isolation key" if k in isolation_keys() else
                   f"allowed: {', '.join(allowed)}" if allowed else
                   "vgcp.json names no scenario_env keys")
            return {}, f"meta.env key '{k}' is not allow-listed ({why})"
        if not isinstance(v, (str, int)) or isinstance(v, bool):
            return {}, f"meta.env['{k}'] must be a string or an integer, got {v!r}"
        out[k] = str(v)
    return out, None


def godot_args(meta: dict) -> tuple[list[str], Optional[str]]:
    """`meta.godot_args`, validated: a flat list of allow-listed `FLAG N` pairs. `(args, error)`."""
    raw = meta.get("godot_args")
    if raw is None:
        return [], None
    return _validate_arg_list(raw, "meta.godot_args")


def replay_godot_args(meta: dict, replays: int) -> tuple[list[list[str]], Optional[str]]:
    """`meta.replay_godot_args`, validated against `meta.replays` and `meta.godot_args`: one
    extra engine arg list per replay. `(lists, error)`; absent means `replays` empty lists."""
    raw = meta.get("replay_godot_args")
    if raw is None:
        return [[] for _ in range(replays)], None
    if not isinstance(raw, list):
        return [], f"meta.replay_godot_args must be an array of arg lists, got {raw!r}"
    if len(raw) != replays:
        return [], (f"meta.replay_godot_args has {len(raw)} arg lists but meta.replays is {replays} "
                    "(one list per replay)")
    base, _ = godot_args(meta)
    out = []
    for n, item in enumerate(raw, start=1):
        args, err = _validate_arg_list(item, f"meta.replay_godot_args[{n - 1}]")
        if err:
            return [], err
        clash = set(args[::2]) & set(base[::2])
        if clash:
            return [], (f"meta.replay_godot_args[{n - 1}] repeats {', '.join(sorted(clash))} "
                        "from meta.godot_args")
        out.append(args)
    return out, None


def requested_resolution(args: list[str]) -> Optional[tuple[int, int]]:
    """The `(w, h)` a validated engine arg list opens the window at, or None."""
    for flag, value in zip(args[::2], args[1::2]):
        if flag == _RESOLUTION_FLAG:
            w, _, h = value.partition("x")
            return int(w), int(h)
    return None


def _x_screen_size(display: int) -> Optional[tuple[int, int]]:
    """The virtual X screen's `(w, h)` on `:display` via `xdpyinfo`, or None when it cannot be read
    (no xdpyinfo, no server, or a GPU lane that renders elsewhere)."""
    if os.environ.get("VGCP_DISPLAY"):
        return None
    try:
        out = subprocess.run(["xdpyinfo", "-display", f":{display}"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.strip().startswith("dimensions:"):
            w, _, h = line.split()[1].partition("x")
            if w.isdigit() and h.isdigit():
                return int(w), int(h)
    return None


def window_size_error(c: VgcpClient, args: list[str], port: int,
                      display: Optional[int] = None) -> Optional[str]:
    """If `args` ask for `--resolution WxH`, check the game really opened at WxH from a screenshot
    reply's `w`/`h` (the capture is the whole window), and that the lane's X screen is at least that
    large: X clamps a warped pointer to the screen, so a reused, older, smaller Xvfb would make a
    device-route replay diverge silently near the right or bottom edge. None when both hold, or
    nothing was asked."""
    want = requested_resolution(args)
    if want is None:
        return None
    screen = _x_screen_size(display) if display is not None else None
    if screen is not None and (screen[0] < want[0] or screen[1] < want[1]):
        return (f"window size check: --resolution {want[0]}x{want[1]} is larger than the X screen "
                f"on :{display} ({screen[0]}x{screen[1]}); X clamps the pointer to the screen. Kill "
                "that Xvfb (by the pid in its /tmp/.X<N>-lock) so the launcher starts a 2560x1440 one")
    path = os.path.join(os.path.dirname(_last_green_path()), f"window-check-{port}.png")
    reply = c.screenshot(path=path)
    got = (reply.get("w"), reply.get("h"))
    with contextlib.suppress(OSError):
        os.unlink(path)
    if got != want:
        return (f"window size check: --resolution {want[0]}x{want[1]} opened a {got[0]}x{got[1]} "
                "window (a screen smaller than the window? the lane Xvfb is 2560x1440)")
    return None


def replay_count(meta: dict) -> tuple[int, Optional[str]]:
    """`meta.replays` (default 1), validated. `(count, error)`."""
    raw = meta.get("replays", 1)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return 1, f"meta.replays must be an int >= 1, got {raw!r}"
    return raw, None


def launch_env(base: dict, port: int, profile: Optional[str], mode: Optional[str],
               extra: Optional[dict], *, scrub: bool) -> dict:
    """The environment one game launch gets. `scrub=True` (test runs) drops every inherited
    `VGCP_*` (the control plane's) key, every key with the game's `env_prefix` and every key
    vgcp.json names, except `LANE_ENV_KEYS`; `scrub=False` (ad-hoc) inherits the shell. Everything
    else (`PATH`, `TMPDIR`, the graphics variables) is inherited either way. Then the isolation
    keys (the port; `profile` as `profile_env` when the game has one), `extra` (never over an
    isolation key) and `mode` (as `mode.env` when the game has one) are applied, in that order."""
    prefixes, iso = env_prefixes(), isolation_keys()
    named = set(scenario_env_keys()) | set(iso)
    env = {k: v for k, v in base.items()
           if not (scrub and (k.startswith(prefixes) or k in named) and k not in LANE_ENV_KEYS)}
    env["VGCP_PORT"] = str(port)
    profile_key = provenance.CONFIG.get("profile_env")
    if profile_key and profile is not None:
        env[profile_key] = profile
    for k, v in (extra or {}).items():
        if k in iso:
            print(f"[vgcp-test] env may not override '{k}': ignored", file=sys.stderr)
            continue
        env[k] = str(v)
    mode_key = (_mode_config() or {}).get("env")
    if mode is not None and mode_key:
        env[mode_key] = mode
    return env


def _launch(scene: Optional[str], port: int, display: int, mode: Optional[str] = None,
            env_extra: Optional[dict] = None, *, scrub: bool = False,
            engine_args: Optional[list[str]] = None) -> Optional[int]:
    root = repo_root()
    launcher = LAUNCHER                          # read now: a caller may point it elsewhere
    # Per-test profile isolation, when the game names a `profile_env`: the file that variable
    # names (where the game keeps its saved progress) is pointed at a FRESH per-lane file in the
    # per-worktree git dir; otherwise progress saved by one test leaks into the next (and between
    # parallel worktree lanes). Same principle as the last-green record above.
    profile = None
    if provenance.CONFIG.get("profile_env"):
        profile = os.path.join(_state_dir(), f"profile-{port}.json")
        if os.path.exists(profile):
            os.unlink(profile)
    # A test launch (scrub=True) starts from the shell's environment minus every VGCP_* and game
    # key but the lane keys, so a stray scenario key in the shell cannot change which scenario a
    # script boots, and a stray VGCP_SHOTS_DIR cannot move its screenshots. The declared
    # environment (meta.env) is applied next, never over the harness's isolation keys, and
    # `meta.mode` last: a test never depends on who ran it. `mode=None` means "inherit the shell"
    # and is only ever used ad-hoc (--play / managed_game without a mode). A Godot user argument
    # could override the mode inside the game, but the harness passes none.
    env = launch_env(dict(os.environ), port, profile, mode, env_extra, scrub=scrub)
    # The Godot project of the same game whose scripts `discover()` found, in the checkout the
    # harness runs in; this keeps the two together when VGCP_GAME_DIR moves them.
    env["VGCP_GODOT_PROJECT"] = godot_project(root)
    cmd = ["bash", launcher, "--port", str(port), "--display", f":{display}"]
    if scene:                            # none: Godot boots the project's main scene
        cmd += ["--scene", scene]
    if engine_args:
        cmd += ["--", *engine_args]      # engine args only; never a second `--` (user args)
    out = subprocess.run(cmd, cwd=root, capture_output=True, text=True, env=env)
    pids = [int(l) for l in out.stdout.strip().splitlines() if l.strip().isdigit()]
    game = pids[-1] if pids else None
    if game:
        _LIVE_GAME_PIDS.add(game)
    # Reap the Xvfb WE started (not reused ones) at harness exit: parse the launcher's stable line.
    # Only from a launch that succeeded: a failed launch has already stopped the Xvfb it started,
    # and its pid could belong to another process by the time the harness exits.
    for line in (out.stderr.splitlines() if game else []):
        s = line.strip()
        if s.startswith("[launch] XVFB_PID="):
            try:
                _XVFB_PIDS.add(int(s.split("=", 1)[1]))
            except ValueError:
                pass
    return game


def _wait_ready(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with VgcpClient(host="127.0.0.1", port=port, timeout=1.5) as c:
                if c.ping().get("ok"):
                    return True
        except OSError:
            time.sleep(0.3)
    return False


def _kill(pid: Optional[int]) -> None:
    """Tear down one game by its process GROUP (godot + children). Falls back to a single-pid kill
    if the game is NOT its own group leader: then setsid did not take (e.g. an older launcher) and
    the game still shares OUR process group, where a killpg would kill the harness itself."""
    if not pid:
        return
    _LIVE_GAME_PIDS.discard(pid)
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    if pgid == pid:                            # setsid leader → safe to group-kill
        _term_then_kill(pgid, group=True)
    else:                                      # shares our group → only kill the pid
        _term_then_kill(pid, group=False)


def _reap_all() -> None:
    """Tear down every game + Xvfb this harness started. Safe to call repeatedly / from a signal."""
    for pid in list(_LIVE_GAME_PIDS):
        _kill(pid)
    for xpid in list(_XVFB_PIDS):
        _XVFB_PIDS.discard(xpid)
        _term_then_kill(xpid, group=False)


def _install_teardown() -> None:
    """Reap on normal exit (atexit) AND on the signals that used to leak orphans. The handler reaps,
    restores the default disposition, and re-raises so the exit status still reflects the signal.
    (SIGKILL cannot be trapped; only a periodic reaper outside the harness covers that gap.)"""
    atexit.register(_reap_all)

    def _handler(signum, _frame):
        _reap_all()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, _handler)


# ---- ad-hoc entry point ------------------------------------------------------------------
@contextlib.contextmanager
def managed_game(scene: Optional[str], port: Optional[int] = None, display: Optional[int] = None,
                 mode: Optional[str] = None, env: Optional[dict] = None,
                 engine_args: Optional[list[str]] = None):
    """Launch a game FOR AD-HOC DRIVING with the same teardown guarantees as a test run:
    registered in the reap registry, killed by process group on context exit, atexit, or
    SIGTERM/SIGINT/SIGHUP. Prefer it to running `launch_game.sh` by hand: a game started by hand
    is easy to leave running, and cleaning up with `pkill -f "godot4 --path"` also matches any
    shell whose command line contains that text, including the one running the pkill.

        sys.path.insert(0, "path/to/vgcp/vgcp-test")
        from harness import managed_game
        from control import VgcpClient
        with managed_game("res://test/foo.tscn") as game:
            with VgcpClient(port=game.port) as c:
                ...

    `scene` None boots the project's main scene. Port/display default to the per-worktree bases
    (parallel-lane safe). Note: when the game names a `profile_env` in vgcp.json, the launch env
    points it at a fresh per-lane file, same as test runs.

    `mode` is one of vgcp.json's `mode.values`; it is exported as `mode.env` for
    this launch. The default None inherits whatever the shell has. A game with no `mode`
    configured takes no `mode`. `env` takes the same scenario keys a test declares as `meta.env`,
    so an ad-hoc session can be launched exactly the way the recorded script will be replayed;
    `mode` wins over an `env` `mode.env`. Unlike a test run, the launch INHERITS the shell's game
    and VGCP_* keys (only test runs are scrubbed), so record from a clean shell.
    `engine_args` takes the same allow-listed pairs as `meta.godot_args` (e.g. ["--max-fps", "10"]).
    """
    import types

    if mode is not None and not _mode_config():
        raise ValueError(f"mode {mode!r} given, but vgcp.json configures no mode")
    _install_teardown()
    base_port, base_disp = _bases()
    use_port = port if port is not None else base_port
    use_disp = display if display is not None else base_disp
    checked_args, args_err = godot_args({"godot_args": engine_args})
    if args_err:
        raise ValueError(args_err)
    pid = _launch(scene, use_port, use_disp, mode, env, engine_args=checked_args)
    if not pid or not _wait_ready(use_port):
        _kill(pid)
        raise RuntimeError(
            f"game did not come up on port {use_port} (see /tmp/vgcp-game-{use_port}.log)"
        )
    try:
        # The same reset as run_one: an ad-hoc or recording session on a reused Xvfb starts from
        # the pointer a fresh display (and therefore the replay) has, so a recording cannot capture
        # a pointer position its replay never sees.
        try:
            with VgcpClient(host="127.0.0.1", port=use_port, timeout=10.0) as c:
                _centre_pointer(c, {})
        except Exception as exc:  # never fail a launch over the reset
            print(f"[vgcp-test] note: pointer not centred ({exc})", flush=True)
        yield types.SimpleNamespace(port=use_port, pid=pid, display=use_disp, scene=scene,
                                    mode=mode)
    finally:
        _kill(pid)


def _wait_port_free(port: int, timeout: float = 10.0) -> None:
    """Wait until nothing accepts connections on `port` (the previous replay's game is gone)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.3):
                pass
        except OSError:
            return
        time.sleep(0.2)


def replay_divergence(results: list[dict]) -> Optional[dict]:
    """The first step whose `recorded` list differs between replay 1 and a later replay, or None.
    Only steps whose reply carries `recorded` are compared."""
    first = results[0].get("steps", [])
    for n, other in enumerate(results[1:], start=2):
        for a, b in zip(first, other.get("steps", [])):
            ra, rb = a.get("reply", {}), b.get("reply", {})
            if ("recorded" in ra or "recorded" in rb) and ra.get("recorded") != rb.get("recorded"):
                return {"replay": n, "step": a.get("idx"), "label": a.get("label"),
                        "replay_1_args": results[0].get("replay_args", []),
                        f"replay_{n}_args": other.get("replay_args", []),
                        "replay_1_recorded": ra.get("recorded"),
                        f"replay_{n}_recorded": rb.get("recorded")}
    return None


def run_one(test_path: str, port: int, display: int) -> dict:
    """Launch a fresh game per replay and run one script: `{passed, steps, failed_step?, error?,
    error_kind?, replay, replay_args}`. `error_kind` names why a run could not reach a verdict, so a
    caller can tell an infrastructure fault from a failed step without parsing the message (a
    bisect step, for one, can map it to git bisect's skip): `bad_meta` (a launch property the
    harness refuses), `no_boot`
    (the game did not come up on the port), `size` (the window-size check), `control_plane_gone`
    (the game went away mid-script) and `replay_divergence` (two replays recorded different
    signals). A failed step carries `failed_step` and no `error_kind`."""
    script = load_script(test_path)
    meta = script.get("meta", {})
    scene = meta.get("scene")            # none: Godot boots the project's main scene
    # `meta.mode` selects the game's boot mode for THIS launch (default the first of vgcp.json's
    # `mode.values`). It is always passed explicitly, so a shell in another mode can never change
    # the mode a test runs in.
    mode, mode_err = meta_mode(meta)
    if mode_err:
        return {"passed": False, "steps": [], "error_kind": "bad_meta", "error": mode_err}
    env, env_err = scenario_env(meta)
    args, args_err = godot_args(meta)
    replays, replays_err = replay_count(meta)
    per_replay, per_replay_err = ([], None) if replays_err else replay_godot_args(meta, replays)
    for err in (env_err, args_err, replays_err, per_replay_err):
        if err:
            return {"passed": False, "steps": [], "error_kind": "bad_meta", "error": err}
    results: list[dict] = []
    for n in range(1, replays + 1):
        if n > 1:
            _wait_port_free(port)
        launch_args = args + per_replay[n - 1]
        pid = _launch(scene, port, display, mode, env, scrub=True, engine_args=launch_args)
        try:
            if not _wait_ready(port):
                result = {"passed": False, "steps": [], "error_kind": "no_boot",
                          "error": f"game did not come up on the VGCP port (see /tmp/vgcp-game-{port}.log)"}
            else:
                try:
                    with VgcpClient(host="127.0.0.1", port=port, timeout=30.0) as c:
                        size_err = window_size_error(c, launch_args, port, display)
                        if size_err:
                            result = {"passed": False, "steps": [], "error_kind": "size",
                                      "error": size_err}
                        else:
                            _centre_pointer(c, meta)
                            result = run_script(c, script)
                except (VgcpError, OSError) as exc:
                    # The game went away mid-script (a crash, an OOM kill, a machine under enough
                    # load that it was reaped). That is ONE test's failure, with a triage packet
                    # like any other, never the end of the suite: an unhandled error here would
                    # abort the run, losing every remaining test AND the summary line.
                    result = {"passed": False, "steps": [], "error_kind": "control_plane_gone",
                              "error": f"the game's control plane went away mid-script ({exc}); "
                                       "the game process most likely died: re-run this test on "
                                       "an idle machine before believing a code cause"}
        finally:
            _kill(pid)
        result["replay"] = n
        result["replay_args"] = launch_args
        results.append(result)
        if not result.get("passed"):
            if replays > 1:
                result["error_context"] = f"replay {n} of {replays} failed"
            return result
    if replays > 1:
        divergence = replay_divergence(results)
        if divergence:
            last = dict(results[-1], passed=False, replay_divergence=divergence,
                        error_kind="replay_divergence",
                        error=(f"replay {divergence['replay']} step {divergence['step']} "
                               f"({divergence['label']}) recorded a different signal list "
                               "than replay 1"))
            return last
        return dict(results[-1], replays=replays)
    return results[0]


def _centre_pointer(c: VgcpClient, meta: dict) -> None:
    """Put the pointer back where a FRESH Xvfb has it: the centre of the authored resolution.

    Test i runs on display `base + i % 8`, and a reused Xvfb keeps the pointer wherever the last
    test on it warped it (a `mouse_move`). A game that reads the pointer on its first tick (a
    player that follows the mouse, say) would otherwise start from wherever the test eight slots
    earlier left it, so adding a test could break an unrelated one. The warp is sent while the tree
    is paused, before the script's first step, so the first poll reads the centre exactly as it
    does on a fresh display."""
    w, h = (meta.get("authored_resolution") or [1280, 720])[:2]
    try:
        c.input(type="mouse_move", x=w / 2, y=h / 2)
    except Exception as exc:  # a scene without mouse input support must still run its script
        print(f"[vgcp-test] note: pointer not centred ({exc})", flush=True)


# ---- triage packet ----------------------------------------------------------------------
def triage_packet(test_path: str, script: dict, result: dict) -> dict:
    meta = script.get("meta", {})
    tid = meta.get("id")
    last = _load_last_green().get(tid, {}).get("commit")
    fs = result.get("failed_step")
    steps = result.get("steps", [])
    step = steps[fs] if (fs is not None and fs < len(steps)) else {}
    reply = step.get("reply", {})
    cmd = step.get("cmd")
    src_args = script["steps"][fs].get("args") if (fs is not None and fs < len(script["steps"])) else None

    if result.get("replay_divergence"):
        hint = ("two replays of the same script recorded different signals -> the run is not "
                "deterministic (a wall-clock dependency, an unseeded RNG draw, or state carried "
                "across launches); compare the two recorded lists in replay_divergence")
    elif result.get("error"):
        hint = f"the game/test could not run: {result['error']}"
    elif cmd == "run_input_script" and reply.get("skipped"):
        hint = ("the input script's reply lists SKIPPED events -> the server could not inject them "
                "(a refused game_action: wrong name, bad payload, or no sink), so the script "
                "silently stopped driving part of the game; fix the script, or add expect.skipped "
                "if the refusal is the point of the test")
    elif cmd in ("await_signal", "await_state") and reply.get("timed_out"):
        hint = ("an await TIMED OUT -> the script likely desynced (inputs/timing drifted); re-derive "
                "the inputs/sync points with the VGCP and fix the script")
    elif cmd == "assert" and reply.get("passed") is False:
        hint = ("an ASSERT failed though earlier syncs passed -> the game reached the checkpoint but "
                "behaved differently; the instructions may be stale, or it's a real regression "
                "(inspect changed_since)")
    else:
        hint = "a step returned an error or an unexpected reply -> inspect the reply"

    if last:
        diff = _git("diff", "--stat", f"{last}..HEAD", cwd=repo_root())
        changed_since = diff or f"(no committed changes since the last green commit {last[:10]})"
    else:
        changed_since = ("(no local 'last green' baseline in this worktree; use "
                         "meta.modified_in_prs / introduced_in_pr to pick a baseline)")
    return {
        "test_id": tid, "title": meta.get("title"), "instructions": _instructions_path(meta),
        "failed_step": {"idx": fs, "label": step.get("label"), "cmd": cmd, "args": src_args},
        "reply": reply,
        "replay": result.get("replay"),
        "replay_divergence": result.get("replay_divergence"),
        "last_verified_commit": last,
        "changed_since": changed_since,
        # Where this recording came from: the commit it was made against is `git bisect`'s good
        # endpoint. Reported, never a verdict: a stale stamp never fails a test.
        "recorded_against": _provenance_note(test_path, meta),
        "hint": hint,
    }


def _instructions_path(meta):
    """`meta.instructions` as a path from the repository root, where a packet's reader stands. A script
    names it relative to its game directory (`tests/vgcp/<id>.md`), the way `scene` is relative to the
    Godot project, so a game can move without its recordings changing."""
    p = meta.get("instructions")
    if not p or os.path.isabs(p) or os.path.exists(os.path.join(repo_root(), p)):
        return p
    return os.path.normpath(os.path.join(GAME_DIR, p))


def _provenance_note(test_path: str, meta: dict) -> dict:
    """`provenance.triage_note`, but a triage packet is written while something is already wrong:
    it never raises and never turns into a failure of its own."""
    try:
        return provenance.triage_note(test_path, meta)
    except Exception as exc:  # no git, no HEAD: report it and move on
        return {"stamp": (meta or {}).get("recorded_against"),
                "note": f"(provenance unavailable: {exc})"}


def print_provenance(packet: dict) -> None:
    """What a human needs on a red script: where the recording came from, and whether its tree was
    dirty. The stamp itself (`meta.recorded_against`) is in the packet printed above it, as
    `recorded_against.stamp`."""
    note = packet.get("recorded_against") or {}
    if note.get("note"):
        print(f"[vgcp-test] provenance: {note['note']}")
    if note.get("dirty"):
        print(f"[vgcp-test] provenance: {note['dirty']}")


# ---- main -------------------------------------------------------------------------------
def discover() -> list[str]:
    return sorted(glob.glob(os.path.join(repo_root(), GAME_DIR, "tests", "vgcp", "*.vgcp.json")))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run + triage VGCP-script tests locally (no agent).")
    ap.add_argument("--test", default=None, help="run a single test by meta.id")
    ap.add_argument("--list", action="store_true", help="list discovered tests and exit")
    ap.add_argument("--stamps", action="store_true",
                    help="report which recordings are stale vs HEAD and which were made on a dirty "
                         "tree, and exit. Stale is reported, never failed: it never blocks a merge.")
    ap.add_argument("--port-base", type=int, default=None,
                    help="the VGCP port of the first test, or of the --play game (default: derived "
                         "from the checkout path, or read from lane.json)")
    ap.add_argument("--display-base", type=int, default=None,
                    help="the Xvfb display number of the first test, or of the --play game "
                         "(default: derived like the port)")
    ap.add_argument(
        "--play",
        metavar="SCENE",
        nargs="?",
        const="",
        default=None,
        help="ad-hoc mode: launch SCENE (default: the project's main scene) with registered "
        "teardown, print the VGCP endpoint, and hold until terminated (Ctrl-C / SIGTERM), then "
        "reap the game. No tests run.",
    )
    mode_cfg = _mode_config()
    ap.add_argument(
        "--mode",
        choices=tuple(mode_cfg["values"]) if mode_cfg else None,
        default=None,
        help=(f"with --play: the game's boot mode (exported as {mode_cfg['env']}). Default: inherit "
              "the shell. Test runs ignore this: each script's meta.mode decides."
              if mode_cfg else "refused: vgcp.json configures no mode"),
    )
    ns = ap.parse_args(argv)
    if ns.mode is not None and not mode_cfg:
        ap.error("--mode: vgcp.json configures no mode for this game")

    if ns.play is not None or not (ns.list or ns.stamps):
        try:
            _state_dir()
        except RuntimeError as exc:
            print(f"[vgcp-test] {exc}", file=sys.stderr)
            return 2

    if ns.play is not None:
        with managed_game(ns.play or None, port=ns.port_base, display=ns.display_base,
                          mode=ns.mode) as game:
            print(
                f"[vgcp-play] {game.scene or '<main scene>'} pid={game.pid} VGCP=127.0.0.1:{game.port} "
                f"display=:{game.display} mode={game.mode or '<inherited>'} "
                f"(Ctrl-C or kill {os.getpid()} to tear down)",
                flush=True,
            )
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
        return 0

    if ns.stamps:
        return provenance.report_stamps(repo_root())

    tests = discover()
    if ns.list:
        for t in tests:
            print(load_script(t).get("meta", {}).get("id", os.path.basename(t)))
        return 0
    if ns.test:
        tests = [t for t in tests if load_script(t).get("meta", {}).get("id") == ns.test]
        if not tests:
            print(f"no test with id '{ns.test}'", file=sys.stderr)
            return 2
    if not tests:
        print(f"no tests found under {GAME_DIR}/tests/vgcp/*.vgcp.json", file=sys.stderr)
        return 2

    base_port, base_disp = _bases()
    if ns.port_base is not None:
        base_port = ns.port_base
    if ns.display_base is not None:
        base_disp = ns.display_base

    _install_teardown()   # from here on, a killed/aborted harness reaps its games + Xvfb (no orphans)
    failures = 0
    for i, path in enumerate(tests):
        script = load_script(path)
        tid = script.get("meta", {}).get("id", os.path.basename(path))
        # Sequential, but allocate a distinct port/display per test so this is parallel-ready.
        port, disp = base_port + i, base_disp + (i % 8)
        print(f"[vgcp-test] {tid} ...", flush=True)
        try:
            result = run_one(path, port, disp)
        except Exception as exc:                       # noqa: BLE001 (the suite's backstop)
            # Nothing one test does may cost the other tests their result. Whatever went wrong is
            # reported as this test's failure, with the traceback, and the run carries on to the
            # summary line, which is the number people read.
            result = {"passed": False, "steps": [],
                      "error": f"the harness itself raised while running this test: {exc!r}",
                      "traceback": traceback.format_exc()}
        if result.get("passed"):
            _stamp_last_green(tid)
            extra = (f" ({result['replays']} replays, identical recorded lists)"
                     if result.get("replays", 1) > 1 else "")
            print(f"[vgcp-test] PASS {tid}{extra}")
        else:
            failures += 1
            print(f"[vgcp-test] FAIL {tid}")
            packet = triage_packet(path, script, result)
            print(json.dumps(packet, indent=2))
            print_provenance(packet)

    print(f"\n[vgcp-test] {len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
