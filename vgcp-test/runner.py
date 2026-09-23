#!/usr/bin/env python3
"""runner.py: replay one test script (a linear list of protocol commands) against a running game.

Part of the test system for the Video Game Control Protocol (VGCP; the contract is
`../docs/vgcp-protocol.md`). A VGCP script is a JSON object
`{vgcp_script: 1, meta: {...}, steps: [{cmd, args, label?, expect?}]}`, usually kept in a game's
`tests/vgcp/` directory. Each step is ONE VGCP command; steps run **in order** and the script
**stops on the first failing step**. Input scripts keep their own format: `run_input_script` is
just one possible step.

A step **passes** iff the reply is `ok` AND the per-command success condition holds:
`assert`->`passed`, `await_signal`->`fired`, `await_state`->`held`; any other command, just `ok`.
If the step carries an optional `expect` dict, that OVERRIDES the default condition: the step passes
iff every `expect` key shallow-matches the reply (so a test can assert a *negative* outcome, e.g.
`"expect": {"held": false, "timed_out": true}`).

Two rules sit OUTSIDE that override (VGCP 1.5.1):
  * **skipped**: a `run_input_script` step whose reply lists `skipped` events (the server could not
    inject them, protocol §4.9.5) FAILS unless its `expect` names `skipped`, in which case
    equality decides. A script that silently stopped driving the game must never pass by accident.
  * **chunk**: a `step` step may carry a sibling `"chunk": k` (an int >= 1, never sent on the
    wire): the runner advances `args.ticks` as `ceil(ticks / k)` wire steps of at most `k` ticks and
    merges their replies into one (`ticks` summed; `recorded` concatenated with each entry's `frame`
    offset by the ticks already run, so frames stay relative to the whole step). This is how one
    script compares the same window driven one tick at a time and in bigger steps.

Determinism: the game is paused between every VGCP call, so wall-clock and socket timing never
change the result.

Usage:
    runner.py <script.vgcp.json>        # exit 0 if all steps pass, else 1; prints a JSON result
Connection: --host/--port or env VGCP_HOST / VGCP_PORT.
Importable:  from runner import load_script, run_script
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

# Reuse the stdlib VGCP client.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "vgcp-mcp"))
from control import VgcpClient  # noqa: E402


def _default_pass(cmd: str, reply: dict) -> bool:
    """Per-command success condition (beyond `ok`, already checked)."""
    if cmd == "assert":
        return bool(reply.get("passed"))
    if cmd == "await_signal":
        return bool(reply.get("fired"))
    if cmd == "await_state":
        return bool(reply.get("held"))
    return True


def _step_passed(cmd: str, reply: dict, expect: Optional[dict]) -> bool:
    if not reply.get("ok", False):
        return False
    # VGCP 1.5.1: refused scripted events fail the step unless the test expects them. Checked
    # before (and independently of) the `expect` override below.
    if cmd == "run_input_script" and reply.get("skipped") and "skipped" not in (expect or {}):
        return False
    if expect:  # explicit expectation overrides the default condition (allows negative outcomes)
        return all(reply.get(k) == v for k, v in expect.items())
    return _default_pass(cmd, reply)


def chunk_plan(ticks: int, chunk: int) -> list[int]:
    """The wire step sizes for `ticks` advanced in chunks of at most `chunk`."""
    full, rest = divmod(ticks, chunk)
    return [chunk] * full + ([rest] if rest else [])


def merge_chunk_replies(replies: list[dict], sizes: list[int]) -> dict:
    """Merge the replies of consecutive wire `step`s into one step reply. `recorded` entries keep
    their order and have `frame` offset by the ticks run before their chunk. Stops at the first
    reply that is not ok or was cancelled (that reply's fields win)."""
    merged: dict[str, Any] = {}
    recorded: list[dict] = []
    have_recorded = False
    truncated = False
    done = 0
    for reply, size in zip(replies, sizes):
        if "recorded" in reply:
            have_recorded = True
            for e in reply.get("recorded") or []:
                e = dict(e)
                if isinstance(e.get("frame"), (int, float)):
                    e["frame"] = e["frame"] + done
                recorded.append(e)
        truncated = truncated or bool(reply.get("recorded_truncated"))
        merged.update({k: v for k, v in reply.items() if k not in ("recorded", "recorded_truncated")})
        done += int(reply.get("ticks", size)) if reply.get("ok") else 0
        if not reply.get("ok") or reply.get("cancelled"):
            break
    merged["ticks"] = done
    if have_recorded:
        merged["recorded"] = recorded
    if truncated:
        merged["recorded_truncated"] = True
    return merged


def chunked_step(client: VgcpClient, args: dict, chunk: Any) -> dict:
    """Advance a `step`'s `args.ticks` in wire steps of at most `chunk` ticks; one merged reply.
    A malformed chunk or ticks comes back as an error reply (the step then fails)."""
    ticks = args.get("ticks", 1)
    if isinstance(chunk, bool) or not isinstance(chunk, int) or chunk < 1:
        return {"ok": False, "error": {"code": "bad_script", "message": f"step chunk must be an int >= 1, got {chunk!r}"}}
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 1:
        return {"ok": False, "error": {"code": "bad_script", "message": f"a chunked step needs int ticks >= 1, got {ticks!r}"}}
    sizes = chunk_plan(ticks, chunk)
    replies: list[dict] = []
    for size in sizes:
        reply = client.request("step", dict(args, ticks=size), raise_on_error=False)
        replies.append(reply)
        if not reply.get("ok") or reply.get("cancelled"):
            break
    return merge_chunk_replies(replies, sizes)


def load_script(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        script = json.load(f)
    if not isinstance(script, dict) or not isinstance(script.get("steps"), list):
        raise ValueError(f"{path}: not a VGCP script (missing a 'steps' array)")
    return script


def run_script(client: VgcpClient, script: dict) -> dict:
    """Run the steps in order; stop on the first failure. Returns a structured result:
    `{passed, steps:[{idx,label,cmd,ok,passed,reply}], failed_step?}`."""
    steps_out: list[dict[str, Any]] = []
    failed_step: Optional[int] = None
    for idx, step in enumerate(script.get("steps", [])):
        cmd = step.get("cmd", "")
        args = step.get("args") or {}
        label = step.get("label", cmd)
        if "chunk" in step:
            if cmd != "step":
                reply = {"ok": False, "error": {"code": "bad_script",
                                                "message": f"'chunk' applies to step only, not {cmd!r}"}}
            else:
                reply = chunked_step(client, args, step["chunk"])
        else:
            reply = client.request(cmd, args, raise_on_error=False)
        passed = _step_passed(cmd, reply, step.get("expect"))
        steps_out.append({"idx": idx, "label": label, "cmd": cmd,
                          "ok": reply.get("ok", False), "passed": passed, "reply": reply})
        if not passed:
            failed_step = idx
            break
    result: dict[str, Any] = {"passed": failed_step is None, "steps": steps_out}
    if failed_step is not None:
        result["failed_step"] = failed_step
    return result


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run a VGCP script against a running game.")
    ap.add_argument("script", help="path to a *.vgcp.json script")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--timeout", type=float, default=10.0)
    ns = ap.parse_args(argv)
    script = load_script(ns.script)
    with VgcpClient(host=ns.host, port=ns.port, timeout=ns.timeout) as c:
        result = run_script(c, script)
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
