#!/usr/bin/env python3
"""record.py: record a session with a running game and compile it into a test script.

Part of the test system for the Video Game Control Protocol (VGCP). Instead of writing a
`*.vgcp.json` by hand, perform the test once through a `Recorder` (a thin wrapper over the
standard-library `VgcpClient`). Every action is logged as a script step, with two compile tricks
that make the result robust and cheap:

  * **sync points**: advancing with `await_signal` / `await_state` records a step the replay can
    resync on (it waits for the same event or state, not a brittle fixed `step N`); a fired
    signal's args are captured as an `expect`, so the replay checks them too.
  * **captured asserts**: `snapshot(provider, paths)` reads the live state NOW and emits
    `assert ... eq <captured>` steps, so expected values are never written by hand: they come from
    the game.

Then `save(path, meta)` writes the `*.vgcp.json` (run it with `runner.py` or `harness.py`).

Usage, as a library in a short recording script. The usual pattern seeds the run first, so the
replay draws the same random numbers the recording did, and drives the game with **game actions**
(`game_action`) rather than synthetic device input. For a toy game whose `game` state provider
reports `score`, `lives` and `phase`, which emits `run_started` when a seeded run begins, and whose
actions include `move_to {x}` and `jump`:

    from record import Recorder
    from control import VgcpClient
    with VgcpClient(port=38787) as c:
        rec = Recorder(c, meta={"id": "jump-scores", "title": "a jump over the first gap scores",
                                "instructions": "tests/vgcp/jump-scores.md"})
        rec.seed(42)                                                     # a fresh, deterministic run
        rec.await_signal("game", "run_started", timeout_ticks=10)        # applied at the next tick
        rec.await_state("game", "eq", "playing", path="phase", timeout_ticks=300, label="playing")
        rec.snapshot("game", ["score", "lives"], label_prefix="before")  # -> assert score == <now>, ...
        rec.game_action("move_to", {"x": 320}, label="walk to the gap")
        rec.step(60)
        rec.game_action("jump", label="jump")
        rec.step(90)
        rec.snapshot("game", ["score", "lives"], label_prefix="after")   # -> assert score == <now>, ...
        rec.save("tests/vgcp/jump-scores.vgcp.json")

`meta.mode` (one of `vgcp.json`'s `mode.values`, default the first) is the boot mode the harness
will launch the replay in (it exports it as `mode.env`); record in the same mode you replay in.
`save()` refuses to write a script whose `meta.mode` disagrees with the mode the game being
recorded is actually in (its `game` provider's `mode`), so a debug-mode session can never be filed
as a play-mode test. A game with no `mode` in `vgcp.json` has no `meta.mode` to check.
`meta.scene` is optional: without it the harness boots the project's main scene.

Signal records are captured, never hand-written: `step_recording`, `run_input_script`,
`await_signal` and `await_state` all take `record_signals=` and store the reply's `recorded` list as
the step's `expect`. `step_recording(..., chunk=k)` drives the window in wire steps of at most `k`
ticks (the runner's `chunk`). `snapshot_keys(provider)` captures the provider's key SHAPE as
`exists` asserts. `run_input_script` raises when the reply lists `skipped` events (VGCP 1.5.1) unless
`allow_skipped=True`, which captures them as `expect.skipped`: a refusal during a recording is almost
always a broken script, and capturing it silently would freeze the bug into the test.
`compile()` validates `meta.replays`, `meta.godot_args` and `meta.replay_godot_args` the way the
harness will.

`save()` stamps the recording with the code it was made against (`provenance.py`):
`meta.recorded_against = {"commit": <HEAD>, "dirty": <were there uncommitted changes>}`. The stamp is
`git bisect`'s good endpoint when the script later goes red, never a value to compare against: a
recording older than HEAD is normal and is never a failure. `save(..., stamp=False)` skips it (there
is nothing to stamp outside a checkout anyway).

**Record from a commit.** Commit the code, then record, at a clean HEAD or in a worktree made from
the commit under test, so the hash alone says what the recording was made against. "Dirty" is an
uncommitted change under one of `vgcp.json`'s `record_paths` (`provenance.affects_recording()`),
else anywhere in the checkout but the scripts under `<game_dir>/tests/vgcp/`. A `Recorder` warns
about a dirty tree when it is given its meta (`preflight()`), before the first driven tick, and
`save()` still writes the recording (a session's work is never thrown away) but stamps it
`dirty: true`. `--rerecord` refuses a dirty tree before it launches anything (`--allow-dirty`
overrides, and the stamp then says so; `--dry-run` is exempt because it writes nothing, so there is
no stamp to get wrong).

Validate metadata BEFORE driving: pass the meta to the constructor, `Recorder(c, meta={...})`, or
call `rec.preflight(meta)` first, so a disallowed `meta.env` key (for example the game's
`profile_env`, an isolation key the harness sets: never copy launch settings into `meta.env`) fails
before the first driven tick rather than at `save()` after the whole drive. `save(path)` then uses
that meta.

Bug scripts: `bug_check(provider, path, op, value)` is the one recorded assert that must FAIL live.
Record a bug script at a commit that has the bug: the setup first (seed, `run_started`, game
actions, `await_*` syncs), then `bug_check(...)` with the CORRECT behaviour as its predicate. It
raises if the predicate already holds (the bug is not on this build), and `compile()` sets
`meta.bisect.predicate_from` to the first bug check's step index unless the meta already sets it.
The saved script fails at the bug check while the bug is there and passes once it is fixed.
`meta.bisect` is metadata for a bisect tool; the harness ignores it.

Absence: `assert_absent(provider, path, op, value)` records an assert whose predicate must be FALSE
(`expect: {"passed": false}`), and every recorded assert is checked live first, so a recording
never files an assert that already fails. "X did not happen before Y" is
`await_state(... Y ..., record_signals=["X"])` (or `await_signal`): the reply's `held`/`fired` and the
`recorded` list (empty when X never fired) are both saved as the step's `expect`.

Re-record by replay:

    record.py --rerecord <script> [--out PATH] [--pr N] [--port PORT] [--dry-run] [--allow-dirty]

launches a game the way the harness replays the script (`managed_game` with its `meta.scene`,
`meta.mode`, `meta.env`, `meta.godot_args`; or attaches to `--port`), sends its steps again through a
Recorder, and re-captures every expectation from the game: `recorded` signal lists, fired args,
`held` / `fired`, `skipped` lists, and the values of `eq` asserts. Commands, args, labels and chunks
are kept; a step that cannot be re-captured the same way (an assert expected to pass that now fails,
an await that no longer holds) stops the re-record with the step index, and nothing is written. `--pr`
appends to `meta.modified_in_prs`. Review the diff before committing: a re-record is a legitimate
behaviour change only when the change under test is meant to change behaviour.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "vgcp-mcp"))
from control import VgcpClient  # noqa: E402
from runner import chunked_step  # noqa: E402
import provenance  # noqa: E402  (the recorded_against stamp)


class SkippedEventsError(RuntimeError):
    """A recorded `run_input_script` reply listed `skipped` events and `allow_skipped` was off."""


def _nav(state: Any, path: str) -> Any:
    """Resolve a dotted path (dict keys / list indices) into a state value, or None."""
    cur = state
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


class Recorder:
    """Drives the VGCP and records each call as a VGCP-script step. The agent performs the test
    through this; `compile()`/`save()` emit the deterministic replay."""

    def __init__(self, client: VgcpClient, meta: Optional[dict] = None):
        self.client = client
        self.steps: list[dict[str, Any]] = []
        self.meta: Optional[dict] = None
        self.bisect_from: Optional[int] = None   # the first bug_check's step index (bug scripts)
        if meta is not None:
            self.preflight(meta)

    @staticmethod
    def validate_meta(meta: dict) -> dict:
        """The harness's rules for `meta` (mode, env allow-list, godot_args, replays), with the
        default mode filled in when the game has one. Raises ValueError naming the offending
        field. No scene is filled in: without one the harness boots the project's main scene."""
        from harness import (godot_args, meta_mode, replay_count,  # the harness's own rules
                             replay_godot_args, scenario_env)

        meta = dict(meta)
        mode, err = meta_mode(meta)
        if err:
            raise ValueError(err)
        if mode is not None:
            meta["mode"] = mode                  # harness.py exports this as vgcp.json's mode.env
        for _, err in (scenario_env(meta), godot_args(meta), replay_count(meta)):
            if err:
                raise ValueError(err)
        _, err = replay_godot_args(meta, replay_count(meta)[0])
        if err:
            raise ValueError(err)
        return meta

    def preflight(self, meta: dict) -> dict:
        """Validate `meta` now, before any driven tick, and keep it for `save()`. Also the moment
        to say the tree is dirty: before the session, not after it."""
        self.meta = self.validate_meta(meta)
        warn_if_dirty()
        return self.meta

    def _add(self, cmd: str, args: dict, label: Optional[str] = None,
             expect: Optional[dict] = None) -> dict:
        step: dict[str, Any] = {"cmd": cmd, "args": args}
        if label:
            step["label"] = label
        if expect:
            step["expect"] = expect
        self.steps.append(step)
        return step

    # -- pass-through actions (recorded verbatim) --------------------------------------
    def step(self, ticks: int = 1, label: Optional[str] = None) -> dict:
        r = self.client.step(ticks=ticks)
        self._add("step", {"ticks": ticks}, label)
        return r

    def input(self, *, label: Optional[str] = None, **kw) -> dict:
        r = self.client.input(**kw)
        self._add("input", dict(kw), label)
        return r

    def game_action(self, action: str, payload: Optional[dict] = None,
                    label: Optional[str] = None) -> dict:
        """Perform ONE canonical game action (VGCP §4.6): the device-agnostic record the game
        actually consumes. Recorded as an ordinary `input` step, so the replay takes the same path.
        Prefer this over synthetic device input in tests: it is not swallowed while paused, and it
        survives a change of input device or key binding."""
        r = self.client.game_action(action, payload)
        args: dict[str, Any] = {"type": "game_action", "action": action}
        if payload is not None:
            args["payload"] = payload
        self._add("input", args, label)
        return r

    def seed(self, value: int, label: Optional[str] = None) -> dict:
        """Fix the run RNG (VGCP §4.14) and record it as the script's first step. The game applies
        the seed at its next tick, so follow with `await_signal("game", "run_started", …)`."""
        r = self.client.seed(value)
        self._add("seed", {"seed": value}, label)
        return r

    def set_timescale(self, value: float, label: Optional[str] = None) -> dict:
        """Set Engine.time_scale (recorded verbatim). Steps stay frame-exact either way: this only
        changes how fast a *resumed* game runs, never how many ticks a `step` advances."""
        r = self.client.set_timescale(value)
        self._add("set_timescale", {"value": value}, label)
        return r

    def screenshot(self, path: Optional[str] = None, label: Optional[str] = None) -> dict:
        """Capture the paused viewport (recorded verbatim). DIAGNOSTIC ONLY: a screenshot step
        proves nothing on replay (nothing compares the pixels); assert on state predicates instead.
        Useful in a recorded script as a breadcrumb for triage."""
        r = self.client.screenshot(path=path)
        args: dict[str, Any] = {}
        if path is not None:
            args["path"] = path
        self._add("screenshot", args, label)
        return r

    def run_input_script(self, script: dict, *, max_frames: Optional[int] = None,
                         record_signals: Optional[list] = None, allow_skipped: bool = False,
                         label: Optional[str] = None) -> dict:
        """Run an input script to completion (recorded verbatim). With `record_signals` (VGCP
        §4.11) the signals that fired during the script's frames are captured as
        `expect: {recorded: [...]}`, so the replay verifies the same signals, args and frames: the
        expectation comes from the game, never from a hand-written list.

        VGCP 1.5.1: if the reply lists `skipped` events this raises `SkippedEventsError` (nothing is
        recorded) unless `allow_skipped=True`, which captures the list as `expect.skipped`, so the
        replay requires exactly those refusals."""
        r = self.client.run_input_script(script=script, max_frames=max_frames,
                                         record_signals=record_signals)
        skipped = r.get("skipped") or []
        if skipped and not allow_skipped:
            raise SkippedEventsError(
                f"run_input_script skipped {len(skipped)} event(s): {skipped}; fix the script, or "
                "pass allow_skipped=True if the refusal is what this test is about")
        args: dict[str, Any] = {"script": script}
        if max_frames is not None:
            args["max_frames"] = max_frames
        expect: dict[str, Any] = {}
        if record_signals is not None:
            args["record_signals"] = record_signals
            expect["recorded"] = r.get("recorded", [])
        if allow_skipped:
            expect["skipped"] = skipped
        self._add("run_input_script", args, label, expect or None)
        return r

    def step_recording(self, ticks: int, record_signals: list, *, chunk: Optional[int] = None,
                       label: Optional[str] = None) -> dict:
        """Advance `ticks` while logging `record_signals`, captured as `expect: {recorded: [...]}`:
        the way to pin "nothing fired" (an empty list) as well as "exactly these fired".

        `chunk=k` drives the window as wire steps of at most `k` ticks and records the runner's
        sibling `"chunk": k`, so the replay drives it the same way; the recorded frames are
        relative to the whole window either way (runner.merge_chunk_replies)."""
        args: dict[str, Any] = {"ticks": ticks, "record_signals": record_signals}
        if chunk is None:
            r = self.client.step(ticks=ticks, record_signals=record_signals)
        else:
            r = chunked_step(self.client, args, chunk)
            if not r.get("ok"):
                raise RuntimeError(f"chunked step failed: {r}")
        step = self._add("step", args, label, {"recorded": r.get("recorded", [])})
        if chunk is not None:
            step["chunk"] = chunk
        return r

    # -- sync points (recorded as resync-able awaits) ----------------------------------
    def await_signal(self, provider: str, signal: str, timeout_ticks: int,
                     label: Optional[str] = None, capture_args: bool = True, *,
                     record_signals: Optional[list] = None) -> dict:
        """Sync on a signal. The fired args are captured as `expect.args`. With `record_signals`
        the signals logged during the wait are captured as `expect.recorded` too (and `fired`, since
        an `expect` replaces the runner's default fired check)."""
        r = self.client.await_signal(signal, timeout_ticks, provider=provider,
                                     record_signals=record_signals)
        args: dict[str, Any] = {"provider": provider, "signal": signal,
                                "timeout_ticks": timeout_ticks}
        expect: dict[str, Any] = {}
        # Capture the fired args as an expect so the replay verifies them too.
        if capture_args and r.get("fired") and r.get("args") is not None:
            expect["args"] = r["args"]
        if record_signals is not None:
            args["record_signals"] = record_signals
            expect["fired"] = bool(r.get("fired"))
            expect["recorded"] = r.get("recorded", [])
        self._add("await_signal", args, label, expect or None)
        return r

    def await_state(self, provider: str, op: str, value: Any = None, *, path: Optional[str] = None,
                    timeout_ticks: int = 4000, label: Optional[str] = None,
                    record_signals: Optional[list] = None) -> dict:
        """Sync on a state predicate. With `record_signals` the signals logged during the wait are
        captured as `expect.recorded` (plus `held`, which an `expect` would otherwise skip)."""
        r = self.client.await_state(provider, op, timeout_ticks, path=path, value=value,
                                    record_signals=record_signals)
        args: dict[str, Any] = {"provider": provider, "op": op, "timeout_ticks": timeout_ticks}
        if path is not None:
            args["path"] = path
        if value is not None:
            args["value"] = value
        expect = None
        if record_signals is not None:
            args["record_signals"] = record_signals
            expect = {"held": bool(r.get("held")), "recorded": r.get("recorded", [])}
        self._add("await_state", args, label, expect)
        return r

    # -- checkpoints (asserts) ---------------------------------------------------------
    def assert_state(self, provider: str, path: str, op: str, value: Any = None,
                     label: Optional[str] = None, *, expect_passed: bool = True) -> dict:
        """Record an assert. It is evaluated live first: a predicate whose live result is not
        `expect_passed` raises instead of freezing a failing step into the script. With
        `expect_passed=False` the step carries `expect: {"passed": false}` (an absence assert)."""
        r = self.client.assert_(provider, op, path=path, value=value)
        if bool(r.get("passed")) != expect_passed:
            raise AssertionError(
                f"live assert {provider}.{path} {op} {value!r} passed={r.get('passed')!r}, "
                f"expected {expect_passed}: actual {r.get('actual')!r}")
        args: dict[str, Any] = {"provider": provider, "op": op, "path": path}
        if value is not None or op not in ("exists", "truthy"):
            args["value"] = value
        self._add("assert", args, label, None if expect_passed else {"passed": False})
        return r

    def assert_absent(self, provider: str, path: str, op: str = "exists", value: Any = None,
                      label: Optional[str] = None) -> dict:
        """Record that a predicate does NOT hold now (`expect: {"passed": false}`), e.g.
        `assert_absent("game", "powerup")` or `assert_absent("game", "phase", "eq", "lost")`."""
        return self.assert_state(provider, path, op, value, label, expect_passed=False)

    def bug_check(self, provider: str, path: str, op: str, value: Any = None,
                  label: Optional[str] = None) -> dict:
        """Record the bug check of a BUG SCRIPT: an assert of the CORRECT behaviour that fails now,
        because this build has the bug. It is checked live like every assert, but the other way
        round: it raises if the predicate already holds (the bug is not on this build, so the script
        would prove nothing). The step carries no `expect`, so it passes once the bug is fixed. The
        first bug check's index becomes `meta.bisect.predicate_from` at `compile()` (unless the meta
        sets it): every step before it is setup."""
        r = self.client.assert_(provider, op, path=path, value=value)
        # A path that does not resolve reads as actual null and fails every comparison (protocol
        # §4.12.1), so a typo would record a "bug check" that fails at every commit, and a bisect
        # could never find where the bug began. Unless the op is about presence, the path must
        # resolve on this (buggy) build.
        if op not in ("exists", "truthy") and (r.get("actual") is None
                                                 or (isinstance(r.get("actual"), dict) and r["actual"].get("type") == "nil")):
            raise AssertionError(
                f"bug_check {provider}.{path}: the path does not resolve on this build (actual "
                f"{r.get('actual')!r}); a bug check must read a value that exists and is wrong. Check "
                "the path with get_state first")
        if r.get("passed"):
            raise AssertionError(
                f"bug_check {provider}.{path} {op} {value!r} already holds live (actual "
                f"{r.get('actual')!r}): the bug is not on this build. Record a bug script at the "
                "commit that has the bug")
        if self.bisect_from is None:
            self.bisect_from = len(self.steps)
        args: dict[str, Any] = {"provider": provider, "op": op, "path": path}
        if value is not None or op not in ("exists", "truthy"):
            args["value"] = value
        self._add("assert", args, label or f"bug check: {provider}.{path} {op} {value!r}")
        return r

    def snapshot(self, provider: str, paths: list[str], *, op: str = "eq",
                 label_prefix: Optional[str] = None, query: Any = None) -> list[Any]:
        """Auto-compile the CURRENT state into `assert` steps: read each path and emit
        `assert provider.path <op> <captured-value>`. Returns the captured values."""
        state = self.client.get_state(provider=provider, query=query)["state"]
        captured = []
        for p in paths:
            val = _nav(state, p)
            captured.append(val)
            label = f"{label_prefix}: {p} {op} {val}" if label_prefix else None
            self._add("assert", {"provider": provider, "op": op, "path": p, "value": val}, label)
        return captured

    def snapshot_keys(self, provider: str, path: Optional[str] = None, *, depth: int = 1,
                      label_prefix: Optional[str] = None) -> list[str]:
        """Auto-compile the provider's key SHAPE (not its values) into `exists` asserts: every key
        of the dict at `path` (the whole state when None), and, `depth` dicts deeper, every key of
        each nested dict. Lists are not descended (their length is state, not shape). Returns
        the asserted paths. Keys containing a '.' cannot be addressed by a dotted path and are
        skipped."""
        state = self.client.get_state(provider=provider)["state"]
        root = _nav(state, path) if path else state
        if not isinstance(root, dict):
            raise ValueError(f"snapshot_keys: {provider}.{path or '<root>'} is not an object")
        paths: list[str] = []

        def walk(node: dict, prefix: Optional[str], level: int) -> None:
            for key in sorted(node):
                if "." in key:
                    continue
                full = f"{prefix}.{key}" if prefix else key
                paths.append(full)
                if level < depth and isinstance(node[key], dict):
                    walk(node[key], full, level + 1)

        walk(root, path, 0)
        for p in paths:
            label = f"{label_prefix}: {p} exists" if label_prefix else None
            self._add("assert", {"provider": provider, "op": "exists", "path": p}, label)
        return paths

    # -- emit --------------------------------------------------------------------------
    def compile(self, meta: Optional[dict] = None) -> dict:
        if meta is None:
            if self.meta is None:
                raise ValueError("compile/save need meta: pass it here, to Recorder(meta=...) or to preflight()")
            meta = self.meta
        meta = self.validate_meta(meta)
        if self.bisect_from is not None:
            bis = dict(meta.get("bisect") or {})
            bis.setdefault("predicate_from", self.bisect_from)
            meta["bisect"] = bis
        meta.setdefault("introduced_in_pr", 0)
        meta.setdefault("modified_in_prs", [])
        return {"vgcp_script": 1, "meta": meta, "steps": self.steps}

    def _live_mode(self) -> Optional[str]:
        """The mode the game under record is actually in, or None when there is no `game`
        provider (the mock, a scene without one): nothing to check then."""
        try:
            if "game" not in (self.client.list_providers().get("providers") or []):
                return None
            return self.client.get_state(provider="game")["state"].get("mode")
        except (OSError, KeyError, TypeError, ValueError):
            return None

    def save(self, path: str, meta: Optional[dict] = None, *, stamp: bool = True) -> dict:
        script = self.compile(meta)
        # Expectations come from the game, and so does the launch that produced them. Recording a
        # debug-mode session into a `meta.mode: play` script would replay in the wrong mode.
        live = self._live_mode() if "mode" in script["meta"] else None
        if live is not None and live != script["meta"]["mode"]:
            raise ValueError(
                f"refusing to save: this game is in {live!r} mode but meta.mode is "
                f"{script['meta']['mode']!r}: record in the mode the test will replay in"
            )
        # ...and so does the CODE that produced them: stamp the commit this recording was made
        # against and whether code was uncommitted at the time. Read-only against git. A game's
        # first recording usually names a `tests/vgcp/` that does not exist yet; it is created
        # first, since the stamp asks git from that directory and the session must not be lost.
        _make_parent(path)
        if stamp:
            script["meta"] = provenance.stamp_meta(script["meta"], path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(script, f, indent=2)
            f.write("\n")
        return script


def _make_parent(path: str) -> None:
    """Create the directory a script is about to be written into, if it is missing."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


# -- re-record by replay ------------------------------------------------------------------------
class RerecordError(RuntimeError):
    """A step could not be re-captured the way it was recorded; nothing is written."""


def rerecord_steps(client: VgcpClient, script: dict) -> Recorder:
    """Send `script`'s steps again through a Recorder and re-capture their expectations."""
    rec = Recorder(client, meta=script.get("meta", {}))
    for i, st in enumerate(script.get("steps", [])):
        cmd, a, label = st.get("cmd"), dict(st.get("args") or {}), st.get("label")
        exp = st.get("expect") or {}
        n0 = len(rec.steps)
        try:
            if cmd == "step" and "record_signals" in a:
                rec.step_recording(a["ticks"], a["record_signals"], chunk=st.get("chunk"), label=label)
            elif cmd == "run_input_script":
                rec.run_input_script(a["script"], max_frames=a.get("max_frames"),
                                     record_signals=a.get("record_signals"),
                                     allow_skipped="skipped" in exp, label=label)
            elif cmd == "await_signal":
                r = rec.await_signal(a["provider"], a["signal"], a["timeout_ticks"], label,
                                     capture_args="args" in exp, record_signals=a.get("record_signals"))
                want = exp.get("fired", True)
                if bool(r.get("fired")) != want:
                    raise RerecordError(f"await_signal {a['signal']} fired={r.get('fired')}, recorded {want}")
                if "record_signals" not in a and exp and "args" not in exp:
                    rec.steps[-1]["expect"] = exp  # a hand-written negative expectation stays as written
            elif cmd == "await_state":
                r = rec.await_state(a["provider"], a["op"], a.get("value"), path=a.get("path"),
                                    timeout_ticks=a["timeout_ticks"], label=label,
                                    record_signals=a.get("record_signals"))
                want = exp.get("held", True)
                if bool(r.get("held")) != want:
                    raise RerecordError(f"await_state {a.get('path')} {a['op']} held={r.get('held')}, recorded {want}")
                if "record_signals" not in a and exp:
                    rec.steps[-1]["expect"] = exp
            elif cmd == "assert":
                want = exp.get("passed", True)
                if a["op"] == "eq" and want:
                    val = _nav(client.get_state(provider=a["provider"], query=a.get("query"))["state"], a["path"])
                    rec.assert_state(a["provider"], a["path"], "eq", val, label)
                    if val != a.get("value"):
                        print(f"[rerecord] step {i} ({label or a['path']}): {a.get('value')!r} -> {val!r}", file=sys.stderr)
                else:
                    rec.assert_state(a["provider"], a["path"], a["op"], a.get("value"), label, expect_passed=want)
            else:
                # seed / step / input / set_timescale / screenshot / pause / resume: sent and kept verbatim
                r = client.request(cmd, a, raise_on_error=False)
                if not r.get("ok", False) and exp.get("ok", True):
                    raise RerecordError(f"{cmd} failed: {r.get('error')}")
                rec._add(cmd, a, label, exp or None)
        except (AssertionError, SkippedEventsError, KeyError) as e:
            raise RerecordError(f"step {i} ({label or cmd}): {e}") from e
        except RerecordError as e:
            raise RerecordError(f"step {i} ({label or cmd}): {e}") from e
        for k in ("chunk",):
            if k in st and len(rec.steps) > n0:
                rec.steps[-1][k] = st[k]
    return rec


def code_root() -> Optional[str]:
    """The checkout whose game a recording runs: the current directory's, the one the harness
    launches the game from (these tools may live in another checkout, or in none)."""
    return provenance.repo_root(os.getcwd())


_warned_dirty = False


def warn_if_dirty() -> None:
    """One stderr warning per process when the code checkout is dirty. A `Recorder` is also
    used just to drive, so this warns and never refuses; `save()` is what stamps."""
    global _warned_dirty
    if _warned_dirty:
        return
    try:
        root = code_root()
        dirty = provenance.dirty_paths(root) if root else []
    except provenance.GitError:
        return
    if dirty:
        _warned_dirty = True
        shown = ", ".join(dirty[:5]) + (f", … ({len(dirty)} paths)" if len(dirty) > 5 else "")
        print(f"[record] WARNING: uncommitted changes could affect a recording ({shown}). A script "
              f"saved from this session will be stamped dirty: true. {provenance.RULE}.",
              file=sys.stderr)


def dirty_refusal(script_path: str, code: Optional[str] = None) -> Optional[str]:
    """Why a re-recording written to `script_path` must not start, or None when it may: the rule is
    to record from a commit, and it is checked BEFORE the game launches so nothing is lost.
    Two checkouts are asked, because `--out` may point anywhere: the one whose game runs (`code`,
    default the current directory's) and the one the script is written into. Outside a git checkout
    there is nothing to be dirty against; a git that cannot answer is a refusal, since nothing has
    been recorded yet and unknown is not clean."""
    roots = []
    for r in (code if code is not None else code_root(), provenance.repo_root(script_path)):
        if r and r not in roots:
            roots.append(r)
    dirty: list[str] = []
    for r in roots:
        try:
            dirty += provenance.dirty_paths(r)
        except provenance.GitError as e:
            return (f"git could not say whether {r} is clean ({e}). {provenance.RULE}. "
                    "--allow-dirty records anyway and stamps dirty: true.")
    if not dirty:
        return None
    shown = ", ".join(dirty[:5]) + (f", … ({len(dirty)} paths)" if len(dirty) > 5 else "")
    return (f"uncommitted changes could affect the recording ({shown}). {provenance.RULE}. "
            "--allow-dirty records anyway and stamps dirty: true.")


def _rerecord_main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="record.py", description="Re-record a VGCP script by replaying it.")
    ap.add_argument("--rerecord", required=True, metavar="SCRIPT", help="the *.vgcp.json to re-record")
    ap.add_argument("--out", default=None, help="write here instead of over SCRIPT")
    ap.add_argument("--pr", type=int, default=None, help="append this PR to meta.modified_in_prs")
    ap.add_argument("--port", type=int, default=None, help="attach to a game already on this port (no launch)")
    ap.add_argument("--dry-run", action="store_true", help="re-capture and print the diff summary; write nothing")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="re-record although code is uncommitted; the stamp will say dirty: true")
    ns = ap.parse_args(argv)
    script = json.load(open(ns.rerecord, encoding="utf-8"))
    meta = Recorder.validate_meta(script.get("meta", {}))  # preflight: fail before any launch
    refusal = dirty_refusal(ns.out or ns.rerecord)
    if refusal and not (ns.allow_dirty or ns.dry_run):   # a dry run writes no stamp
        print(f"[rerecord] refused, nothing launched: {refusal}", file=sys.stderr)
        return 2

    def run(port: int) -> Recorder:
        with VgcpClient(port=port) as c:
            return rerecord_steps(c, script)

    try:
        if ns.port is not None:
            rec = run(ns.port)
        else:
            from harness import managed_game, replay_count, replay_godot_args, scenario_env

            env, _ = scenario_env(meta)
            # the first replay's engine args: meta.godot_args plus meta.replay_godot_args[0]
            first_extra = replay_godot_args(meta, replay_count(meta)[0])[0][0]
            with managed_game(meta.get("scene"), mode=meta.get("mode"), env=env,
                              engine_args=list(meta.get("godot_args") or []) + first_extra) as game:
                rec = run(game.port)
    except RerecordError as e:
        print(f"[rerecord] stopped, nothing written: {e}", file=sys.stderr)
        return 1
    new_meta = dict(script.get("meta", {}))
    # A re-recording never inherits the old recording's stamp: if this one cannot be stamped (an
    # `--out` outside any checkout), it carries none, not a claim about another commit.
    new_meta.pop("recorded_against", None)
    if ns.pr is not None:
        prs = list(new_meta.get("modified_in_prs") or [])
        if ns.pr not in prs:
            prs.append(ns.pr)
        new_meta["modified_in_prs"] = prs
    changed = sum(1 for o, n in zip(script.get("steps", []), rec.steps) if o != n)
    print(f"[rerecord] {len(rec.steps)} steps, {changed} changed", file=sys.stderr)
    if ns.dry_run:
        return 0
    out = ns.out or ns.rerecord
    compiled = rec.compile(new_meta)
    compiled["meta"] = {**new_meta, **{k: v for k, v in compiled["meta"].items() if k not in new_meta}}
    # A re-recording is a new recording: it is stamped against the commit it was re-captured on,
    # not against the one the old expectations came from.
    _make_parent(out)
    compiled["meta"] = provenance.stamp_meta(compiled["meta"], out)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(compiled, f, indent=2)
        f.write("\n")
    print(out)
    return 0


if __name__ == "__main__":
    if any(a == "--bug-check" or a.startswith("--bug-check=") for a in sys.argv[1:]):
        # Not an option here: say so plainly, rather than argparse's "--rerecord is required".
        print("record.py has no --bug-check option; a bug script is replayed like any other script, "
              "with harness.py or runner.py, and fails while the bug is there", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(_rerecord_main())
