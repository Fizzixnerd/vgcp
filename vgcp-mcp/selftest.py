#!/usr/bin/env python3
"""selftest.py: validate the Video Game Control Protocol (VGCP) wire protocol with the Python
CLI/client against the mock.

NO Godot, NO third-party deps. Temporary files go under tempfile.gettempdir() (honours TMPDIR)
and are removed at the end. Starts mock_server.py on a free port, then drives it with
control.py's VgcpClient, asserting the round-trips for ping/pause/resume/step/screenshot/
input/get_state/list_providers/set_timescale, the canonical `game_action` input event and the
`seed` command (VGCP 1.5.0), and the error envelopes.

Run:  python3 selftest.py
Exit: 0 on all-pass, 1 otherwise.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from control import VgcpClient, VgcpError  # noqa: E402

_failures = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _failures
    if not cond:
        _failures += 1
    status = "PASS" if cond else "FAIL"
    print(f"  {status}  {name}" + (f"  -- {detail}" if detail else ""))


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"mock did not come up on :{port}")


def main() -> int:
    port = free_port()
    print(f"[py-selftest] mock port {port}")
    # The mock resolves a pathless `screenshot` the way the real server does (protocol §4.5), so
    # give it a shots directory of its own and assert the reply lands inside it.
    shots_dir = tempfile.mkdtemp(prefix="vgcp-selftest-shots-")
    # Explicit-path screenshots go to a scratch directory of their own under the temp root.
    work_dir = tempfile.mkdtemp(prefix="vgcp-py-selftest-")
    mock = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "mock_server.py"), "--port", str(port), "--quiet"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "VGCP_SHOTS_DIR": shots_dir},
    )
    try:
        wait_for_port(port)
        with VgcpClient(host="127.0.0.1", port=port, timeout=5.0) as c:
            # ping
            ping = c.ping()
            check("ping ok", ping.get("ok") is True)
            check("ping protocol_major=1", ping.get("protocol_major") == 1, str(ping.get("protocol_major")))
            check("ping version 1.5.2", ping.get("version") == "1.5.2", str(ping.get("version")))
            check("ping paused-by-default", ping.get("paused") is True)

            # pause / resume
            check("pause", c.pause().get("paused") is True)
            check("resume", c.resume().get("paused") is False)

            # step advances physics_frame by exactly ticks and re-pauses
            before = c.ping()["physics_frame"]
            step = c.step(ticks=30)
            check("step ok + ticks echoed", step.get("ok") is True and step.get("ticks") == 30)
            check("step advanced exactly 30", step.get("physics_frame") == before + 30,
                  f"{before} -> {step.get('physics_frame')}")
            check("step re-paused", step.get("paused") is True)

            # set_timescale
            ts = c.set_timescale(3.0)
            check("set_timescale", ts.get("ok") is True and ts.get("time_scale") == 3.0)

            # screenshot writes a real PNG
            shot_path = os.path.join(work_dir, "vgcp-py-selftest.png")
            shot = c.screenshot(path=shot_path, downscale=0.5)
            check("screenshot ok + path", shot.get("ok") is True and shot.get("path") == shot_path)
            png_ok = False
            if os.path.exists(shot_path):
                with open(shot_path, "rb") as f:
                    png_ok = f.read(8) == b"\x89PNG\r\n\x1a\n"
            check("screenshot produced a valid PNG", png_ok)

            # ...and with NO path, into the shots directory VGCP_SHOTS_DIR names (§4.5).
            shot_default = c.screenshot()
            default_path = shot_default.get("path") or ""
            check("pathless screenshot lands in VGCP_SHOTS_DIR",
                  shot_default.get("ok") is True
                  and os.path.dirname(default_path) == shots_dir,
                  f"{default_path!r} vs dir {shots_dir!r}")
            check("pathless screenshot path is absolute", os.path.isabs(default_path),
                  default_path)
            default_png_ok = False
            if os.path.exists(default_path):
                with open(default_path, "rb") as f:
                    default_png_ok = f.read(8) == b"\x89PNG\r\n\x1a\n"
            check("pathless screenshot produced a valid PNG", default_png_ok)

            # input variants
            check("input action", c.input(type="action", action="jump", pressed=True).get("injected") == "action")
            check("input mouse_button click", c.input(type="mouse_button", x=100, y=200).get("injected") == "mouse_button")
            check("input key", c.input(type="key", keycode=4194305, pressed=True).get("injected") == "key")

            # ---- canonical game actions (VGCP §4.6, 1.5.0) ----
            ga = c.game_action("move_to", {"x": 300})
            check("input game_action ok + type echoed", ga.get("ok") is True and ga.get("injected") == "game_action", f"{ga}")
            check("input game_action echoes the action name", ga.get("action") == "move_to", f"{ga}")
            check("input game_action without a payload",
                  c.game_action("pause").get("injected") == "game_action")
            for label, payload, code in [
                ("missing action", {"type": "game_action"}, "bad_args"),
                ("non-string action", {"type": "game_action", "action": 7}, "bad_args"),
                ("payload not an object",
                 {"type": "game_action", "action": "move_to", "payload": [1, 2]}, "bad_args"),
            ]:
                r = c.request("input", payload, raise_on_error=False)
                check(f"game_action {label} -> {code}",
                      r.get("ok") is False and r["error"]["code"] == code, f"{r}")
            # delivery failures: no registered sink, and a sink that refuses the event
            no_sink = c.request("input", {"type": "game_action", "action": "pause",
                                          "mock_no_sink": True}, raise_on_error=False)
            check("game_action with no registered sink -> no_action_sink",
                  no_sink.get("ok") is False and no_sink["error"]["code"] == "no_action_sink", f"{no_sink}")
            refused = c.request("input", {"type": "game_action", "action": "frobnicate",
                                          "mock_sink_refuses": True}, raise_on_error=False)
            check("game_action refused by the sink -> bad_args",
                  refused.get("ok") is False and refused["error"]["code"] == "bad_args", f"{refused}")

            # ---- seed (VGCP §4.14, 1.5.0) ----
            sd = c.seed(42)
            check("seed ok + echoed", sd.get("ok") is True and sd.get("seed") == 42, f"{sd}")
            check("seed is observable in the game state",
                  c.get_state(provider="hud")["state"]["seed"] == 42)
            for label, payload in [("string", {"seed": "x"}), ("bool", {"seed": True}),
                                   ("fractional", {"seed": 1.5}), ("missing", {})]:
                r = c.request("seed", payload, raise_on_error=False)
                check(f"seed {label} -> bad_args",
                      r.get("ok") is False and r["error"]["code"] == "bad_args", f"{r}")
            no_tgt = c.request("seed", {"seed": 7, "mock_no_seed_target": True}, raise_on_error=False)
            check("seed with no registered target -> no_seed_target",
                  no_tgt.get("ok") is False and no_tgt["error"]["code"] == "no_seed_target", f"{no_tgt}")

            # get_state (all / single / unknown)
            all_state = c.get_state()
            check("get_state all has engine+hud",
                  all_state.get("ok") is True and "engine" in all_state["state"] and "hud" in all_state["state"])
            hud = c.get_state(provider="hud")
            check("get_state provider=hud", hud.get("ok") is True and hud["state"]["gold"] == 120)

            # error envelopes
            try:
                c.get_state(provider="does_not_exist")
                check("unknown provider raises", False)
            except VgcpError as e:
                check("unknown provider -> error code", e.code == "unknown_provider", e.code)

            bad = c.request("frobnicate", raise_on_error=False)
            check("unknown_cmd error envelope", bad.get("ok") is False and bad["error"]["code"] == "unknown_cmd")

            bad2 = c.request("step", {"ticks": 0}, raise_on_error=False)
            check("step ticks<1 -> bad_args", bad2.get("ok") is False and bad2["error"]["code"] == "bad_args")

            # list_providers
            lp = c.list_providers()
            check("list_providers", lp.get("ok") is True and "engine" in lp["providers"] and "hud" in lp["providers"])

            # ---- input scripts (VGCP §4.9) ----
            sample = {
                "version": 1,
                "resolution": [1280, 720],
                "scale": False,
                "frames": [
                    {"frame": 0, "events": [{"type": "mouse_move", "x": 640, "y": 360}]},
                    {"frame": 3, "events": [
                        {"type": "mouse_button", "x": 200, "y": 300, "button": 1, "pressed": True},
                        {"type": "mouse_button", "x": 200, "y": 300, "button": 1, "pressed": False},
                    ]},
                    {"frame": 10, "events": [{"type": "key", "keycode": 32, "pressed": True}]},
                    {"frame": 14, "events": [{"type": "action", "action": "jump", "pressed": True}]},
                ],
            }
            status0 = c.input_script_status()
            check("status before load: not loaded/playing",
                  status0.get("ok") is True and status0.get("loaded") is False
                  and status0.get("playing") is False)

            loaded = c.load_input_script(script=sample)
            check("load_input_script counts",
                  loaded.get("ok") is True and loaded.get("frames") == 4
                  and loaded.get("duration_frames") == 15 and loaded.get("events") == 5,
                  f"{loaded}")

            status1 = c.input_script_status()
            check("status after load: loaded, total_frames=15",
                  status1.get("loaded") is True and status1.get("total_frames") == 15)

            before_run = c.ping()["physics_frame"]
            run = c.run_input_script()
            check("run_input_script completed", run.get("ok") is True and run.get("completed") is True)
            check("run advanced exactly duration_frames",
                  run.get("frames_run") == 15 and run.get("physics_frame") == before_run + 15,
                  f"{before_run} -> {run.get('physics_frame')} (frames_run={run.get('frames_run')})")
            check("run re-paused", run.get("paused") is True)
            check("run skipped is [] on a clean script (1.5.1)", run.get("skipped") == [], f"{run}")

            # ---- skipped[] (VGCP 1.5.1 §4.9.5): refused game_action frames are reported ----
            ga_script = {"resolution": [1280, 720], "frames": [
                {"frame": 0, "events": [{"type": "mouse_move", "x": 1, "y": 1},
                                        {"type": "game_action", "action": "frobnicate"}]},
                {"frame": 0, "events": [{"type": "game_action", "action": "pause"}]},
                {"frame": 4, "events": [{"type": "game_action", "action": "move_to",
                                         "payload": {"x": 3}}]},
            ]}
            refused_run = c.request("run_input_script", {"script": ga_script,
                                                         "mock_sink_refuses": True},
                                    raise_on_error=False)
            check("refused game_action frames: completed true, one skipped entry each",
                  refused_run.get("completed") is True
                  and [(e["frame"], e["index"], e["code"]) for e in refused_run.get("skipped", [])]
                  == [(0, 1, "bad_args"), (0, 2, "bad_args"), (4, 0, "bad_args")], f"{refused_run}")
            check("skipped entries carry a message naming the action",
                  all(isinstance(e.get("message"), str) and e["message"]
                      for e in refused_run.get("skipped", []))
                  and "frobnicate" in refused_run["skipped"][0]["message"], f"{refused_run}")
            capped = c.request("run_input_script", {"script": ga_script, "mock_no_sink": True,
                                                    "max_frames": 1}, raise_on_error=False)
            check("no sink: only frames that ran are skipped, as no_action_sink",
                  [(e["frame"], e["code"]) for e in capped.get("skipped", [])]
                  == [(0, "no_action_sink"), (0, "no_action_sink")], f"{capped}")

            run_cap = c.run_input_script(max_frames=5)
            check("run_input_script max_frames cap", run_cap.get("frames_run") == 5, f"{run_cap}")

            # inline override (run without loading; resolution differs is fine for the mock)
            run_inline = c.run_input_script(script=sample, max_frames=2)
            check("run inline override", run_inline.get("ok") is True and run_inline.get("frames_run") == 2)

            # malformed script -> bad_args
            bad_script = c.request("load_input_script",
                                   {"script": {"frames": [{"frame": 0, "events": []}]}},
                                   raise_on_error=False)
            check("load malformed (no resolution) -> bad_args",
                  bad_script.get("ok") is False and bad_script["error"]["code"] == "bad_args")

            # a game_action frame is a valid script event (same injector as `input`, VGCP §4.9.1)
            ga_script = c.load_input_script(script={
                "resolution": [1280, 720],
                "frames": [
                    {"frame": 0, "events": [{"type": "game_action", "action": "move_to",
                                             "payload": {"x": 300}}]},
                    {"frame": 5, "events": [{"type": "game_action", "action": "pause"}]},
                ],
            })
            check("load_input_script counts game_action frames",
                  ga_script.get("ok") is True and ga_script.get("frames") == 2
                  and ga_script.get("events") == 2 and ga_script.get("duration_frames") == 6,
                  f"{ga_script}")
            ga_bad = c.request("load_input_script",
                               {"script": {"resolution": [800, 600],
                                           "frames": [{"frame": 0,
                                                       "events": [{"type": "game_action"}]}]}},
                               raise_on_error=False)
            check("script game_action with no action -> bad_args",
                  ga_bad.get("ok") is False and ga_bad["error"]["code"] == "bad_args", f"{ga_bad}")

            bad_event = c.request("load_input_script",
                                  {"script": {"resolution": [800, 600],
                                              "frames": [{"frame": 0, "events": [{"type": "nope"}]}]}},
                                  raise_on_error=False)
            check("load bad event type -> bad_args",
                  bad_event.get("ok") is False and bad_event["error"]["code"] == "bad_args")

            # run with nothing loaded -> bad_args (clear first)
            cleared = c.clear_input_script()
            check("clear_input_script ok", cleared.get("ok") is True)
            status2 = c.input_script_status()
            check("status after clear: not loaded", status2.get("loaded") is False)
            no_script = c.request("run_input_script", {}, raise_on_error=False)
            check("run with no script loaded -> bad_args",
                  no_script.get("ok") is False and no_script["error"]["code"] == "bad_args")

            # ---- await_signal (VGCP §4.10) ----
            # fired branch (mock fires after `mock_fire_after` ticks) + STRUCTURED args
            aw = c.await_signal("game_over", timeout_ticks=50, provider="hud",
                                mock_fire_after=1, mock_args=[False])
            check("await_signal fired",
                  aw.get("ok") is True and aw.get("fired") is True
                  and aw.get("signal") == "game_over" and aw.get("paused") is True, f"{aw}")
            check("await_signal args are typed descriptors ({type,value}), not opaque",
                  aw.get("args") == [{"type": "bool", "value": False}], f"{aw.get('args')}")

            aw_int = c.await_signal("phase_changed", timeout_ticks=50, node="/root/Main",
                                    mock_fire_after=2, mock_args=[3])
            check("await_signal fired with int arg + waited_ticks",
                  aw_int.get("fired") is True
                  and aw_int.get("args") == [{"type": "int", "value": 3}]
                  and aw_int.get("waited_ticks") == 2, f"{aw_int}")

            # timeout branch (simulated fire scheduled past the budget)
            aw_to = c.await_signal("never", timeout_ticks=4, provider="hud", mock_fire_after=999)
            check("await_signal timed_out",
                  aw_to.get("ok") is True and aw_to.get("fired") is False
                  and aw_to.get("timed_out") is True, f"{aw_to}")

            # error envelopes: required args + target selection
            for label, payload, code in [
                ("missing timeout_ticks", {"signal": "x", "provider": "hud"}, "bad_args"),
                ("timeout_ticks<1", {"signal": "x", "timeout_ticks": 0, "provider": "hud"}, "bad_args"),
                ("missing signal", {"timeout_ticks": 5, "provider": "hud"}, "bad_args"),
                ("no target", {"signal": "x", "timeout_ticks": 5}, "bad_args"),
                ("both targets", {"signal": "x", "timeout_ticks": 5,
                                  "node": "/root/Main", "provider": "hud"}, "bad_args"),
                ("unknown provider", {"signal": "x", "timeout_ticks": 5,
                                      "provider": "nope"}, "unknown_provider"),
            ]:
                r = c.request("await_signal", payload, raise_on_error=False)
                check(f"await_signal {label} -> {code}",
                      r.get("ok") is False and r["error"]["code"] == code, f"{r}")

            # ---- record_signals (VGCP §4.11) ----
            # record over a step: the mock synthesizes emissions from the mock_recorded hint.
            rec_step = c.step(
                ticks=5,
                record_signals=[{"provider": "hud", "signals": ["phase_changed"]}],
                mock_recorded=[{"signal": "phase_changed", "frame": 1, "args": [1]},
                               {"signal": "game_over", "frame": 4, "args": [False]}],
            )
            check("step.recorded present, chronological, typed args",
                  rec_step.get("ok") is True and isinstance(rec_step.get("recorded"), list)
                  and rec_step["recorded"][0] == {"signal": "phase_changed", "frame": 1,
                                                  "args": [{"type": "int", "value": 1}]}
                  and rec_step["recorded"][1]["args"] == [{"type": "bool", "value": False}],
                  f"{rec_step.get('recorded')}")

            # record over an await_signal (record arg rides alongside the await).
            rec_aw = c.await_signal(
                "game_over", timeout_ticks=50, provider="hud", mock_fire_after=1, mock_args=[True],
                record_signals=[{"node": "/root/Main"}],
                mock_recorded=[{"signal": "child_entered_tree", "frame": 0, "args": ["x"]}],
            )
            check("await_signal carries recorded",
                  rec_aw.get("fired") is True and isinstance(rec_aw.get("recorded"), list)
                  and rec_aw["recorded"][0]["signal"] == "child_entered_tree", f"{rec_aw.get('recorded')}")

            # truncation marker
            rec_tr = c.step(ticks=1, record_signals=[{"provider": "hud"}],
                            mock_recorded=[], mock_recorded_truncated=True)
            check("recorded_truncated marker", rec_tr.get("recorded_truncated") is True)

            # a present-but-empty record_signals still returns recorded:[] (field present), matching
            # the server (which stores a session even with zero recorders).
            rec_empty = c.step(ticks=1, record_signals=[])
            check("empty record_signals -> recorded:[] present",
                  rec_empty.get("ok") is True and rec_empty.get("recorded") == [], f"{rec_empty}")

            # malformed record specs abort the command (no advance) with the right code
            for label, specs, code in [
                ("both targets", [{"node": "/root/Main", "provider": "hud"}], "bad_args"),
                ("non-array", "notarray", "bad_args"),
                ("unknown provider", [{"provider": "engine"}], "unknown_provider"),
                ("signals not array", [{"provider": "hud", "signals": "x"}], "bad_args"),
            ]:
                r = c.request("step", {"ticks": 5, "record_signals": specs}, raise_on_error=False)
                check(f"record_signals {label} -> {code}",
                      r.get("ok") is False and r["error"]["code"] == code, f"{r}")

            # ---- assert + await_state (VGCP §4.12 / §4.13) ----
            # mock hud is a static {"gold":120,"lives":18,"wave":3} -> deterministic predicates.
            for label, op, path, value, want in [
                ("eq int", "eq", "lives", 18, True),
                ("eq int false", "eq", "lives", 17, False),
                ("gt", "gt", "gold", 100, True),
                ("lt false", "lt", "lives", 0, False),
                ("ge boundary", "ge", "wave", 3, True),
                ("exists", "exists", "gold", None, True),
                ("exists missing", "exists", "nope", None, False),
                ("truthy", "truthy", "gold", None, True),
                ("in", "in", "wave", [1, 2, 3], True),
            ]:
                r = c.assert_("hud", op, path=path, value=value)
                check(f"assert hud.{path} {label}",
                      r.get("ok") is True and r.get("passed") is want, f"{r}")
            # actual comes back as a typed descriptor
            ra = c.assert_("hud", "eq", path="lives", value=18)
            check("assert actual is typed descriptor",
                  ra.get("actual") == {"type": "int", "value": 18}, f"{ra.get('actual')}")
            # assert error envelopes
            for label, payload, code in [
                ("unknown provider", {"provider": "nope", "op": "eq", "value": 1}, "unknown_provider"),
                ("bad op", {"provider": "hud", "op": "frobnicate"}, "bad_args"),
                ("missing provider", {"op": "eq", "value": 1}, "bad_args"),
            ]:
                r = c.request("assert", payload, raise_on_error=False)
                check(f"assert {label} -> {code}",
                      r.get("ok") is False and r["error"]["code"] == code, f"{r}")

            # await_state: held (mock default fires at tick 1) + actual
            aw = c.await_state("hud", "eq", timeout_ticks=50, path="lives", value=18)
            check("await_state held + typed actual",
                  aw.get("ok") is True and aw.get("held") is True
                  and aw.get("actual") == {"type": "int", "value": 18}, f"{aw}")
            # await_state timeout (mock hold scheduled past the budget)
            aw_to = c.await_state("hud", "eq", timeout_ticks=4, path="lives", value=18, mock_hold_after=999)
            check("await_state timed_out",
                  aw_to.get("ok") is True and aw_to.get("held") is False
                  and aw_to.get("timed_out") is True, f"{aw_to}")
            # await_state errors: bad timeout, unknown provider
            for label, payload, code in [
                ("timeout<1", {"provider": "hud", "op": "eq", "path": "lives", "value": 18,
                               "timeout_ticks": 0}, "bad_args"),
                ("unknown provider", {"provider": "nope", "op": "eq", "value": 1,
                                      "timeout_ticks": 5}, "unknown_provider"),
            ]:
                r = c.request("await_state", payload, raise_on_error=False)
                check(f"await_state {label} -> {code}",
                      r.get("ok") is False and r["error"]["code"] == code, f"{r}")
            # await_state carries record_signals too
            aw_rec = c.await_state("hud", "eq", timeout_ticks=50, path="lives", value=18,
                                   record_signals=[{"provider": "hud", "signals": ["x"]}],
                                   mock_recorded=[{"signal": "x", "frame": 1, "args": [1]}])
            check("await_state carries recorded",
                  aw_rec.get("held") is True and isinstance(aw_rec.get("recorded"), list)
                  and aw_rec["recorded"][0]["signal"] == "x", f"{aw_rec.get('recorded')}")

            # raw bad JSON -> bad_json
            c._sock.sendall(b"this is not json\n")  # type: ignore[union-attr]
            raw_line = c._reader.readline()  # type: ignore[union-attr]
            import json as _json
            raw_resp = _json.loads(raw_line)
            check("malformed line -> bad_json", raw_resp.get("ok") is False and raw_resp["error"]["code"] == "bad_json")
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=2)
        except subprocess.TimeoutExpired:
            mock.kill()
        shutil.rmtree(shots_dir, ignore_errors=True)
        shutil.rmtree(work_dir, ignore_errors=True)

    print(f"\n[py-selftest] {'ALL PASS' if _failures == 0 else str(_failures) + ' FAILURE(S)'}")
    return 0 if _failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
