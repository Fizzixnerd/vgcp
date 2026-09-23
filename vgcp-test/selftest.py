#!/usr/bin/env python3
"""selftest.py: check the test tools against the mock server, with no Godot.

Part of the test system for the Video Game Control Protocol (VGCP). It exercises `run_script` (an
all-pass script, stop on the first failure, the `expect` override with a timeout as the EXPECTED
outcome, an error step) and the record -> compile -> replay round trip, including the recorder
verbs `seed`, `game_action` and `set_timescale`. It also covers: the `skipped` rule, step `chunk`
merging, the recorder's `allow_skipped`, `record_signals` on awaits and `snapshot_keys`; the launch
environment scrub and the `meta.env` allow-list; `meta.godot_args`, `meta.replays` and
`meta.replay_godot_args` validation (with `--resolution WxH`); replay divergence detection; the
launcher the harness runs (the one in `../virtual-display/`, which needs no git; it is run only in
a shape that fails before it touches Godot or a display); and the `recorded_against` stamp with its
rule, record from a commit (what counts as dirty, asked of a real throwaway repository; the
`--rerecord` refusal; the triage lines; and, with a fake git, the proof that provenance only ever
runs read-only git commands). The game's names come from `vgcp.json`: every check runs under a
neutral fixture config (`DEMO_CONFIG`, set together with `provenance.GAME_DIR` and
`harness.GAME_DIR`), never under a file the checkout ships, and the config itself is checked with
no file, with bad files, and for where it is looked up.

Needs python3 and git; the mock server is `../vgcp-mcp/mock_server.py`.
Run:  python3 selftest.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "vgcp-mcp"))
from control import VgcpClient  # noqa: E402
from runner import chunk_plan, merge_chunk_replies, run_script  # noqa: E402
import record  # noqa: E402
from record import Recorder, SkippedEventsError  # noqa: E402
import harness  # noqa: E402
import provenance  # noqa: E402

_fail = 0

# A neutral game's vgcp.json. Checks run under it (or under no config at all), never under the file
# this checkout ships, so they say what the tools do for any game.
DEMO_CONFIG = {
    "game_dir": "game",
    "env_prefix": "DEMO_",
    "scenario_env": ["DEMO_MAP", "DEMO_SEED"],
    "mode": {"env": "DEMO_MODE", "values": ["play", "debug"]},
    "profile_env": "DEMO_PROFILE",
    "record_paths": ["game/godot", "game/rust", "vgcp/vgcp-server/src", "vgcp/vgcp-server/Cargo.toml",
                     "vgcp/vgcp-test", "vgcp/launcher"],
}


@contextlib.contextmanager
def use_config(cfg: dict):
    """Run under `cfg` as the vgcp.json: `provenance.CONFIG`, `provenance.GAME_DIR` and
    `harness.GAME_DIR` together, restored afterwards."""
    saved = provenance.CONFIG, provenance.GAME_DIR, harness.GAME_DIR
    game_dir = cfg.get("game_dir") or "."
    provenance.CONFIG, provenance.GAME_DIR, harness.GAME_DIR = cfg, game_dir, game_dir
    try:
        yield
    finally:
        provenance.CONFIG, provenance.GAME_DIR, harness.GAME_DIR = saved


def _git_sh(root: str, *args: str) -> None:
    """A git command in a throwaway repository, with no hooks, no signing and a fixed identity."""
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                   cwd=root, check=True, capture_output=True)


def _put(root: str, rel: str, body: str = "x\n") -> None:
    os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
    with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
        f.write(body)


def check(name: str, cond: bool, detail: str = "") -> None:
    global _fail
    ok = bool(cond)
    if not ok:
        _fail += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait(port: int, t: float = 5.0) -> None:
    deadline = time.time() + t
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("mock did not come up")


class _HintingClient:
    """Wraps a client so every `run_input_script` carries mock-only hint args (the Recorder's own
    call has no hint parameter)."""

    def __init__(self, inner: VgcpClient, hints: dict):
        self.inner, self.hints = inner, hints

    def run_input_script(self, **kw):
        return self.inner.run_input_script(**kw, **self.hints)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _offline_checks() -> None:
    """Pure checks of the harness rules (no mock, no Godot)."""
    check("chunk_plan splits a window into chunks of at most k",
          chunk_plan(5, 2) == [2, 2, 1] and chunk_plan(16, 16) == [16] and chunk_plan(3, 16) == [3])
    merged = merge_chunk_replies(
        [{"ok": True, "ticks": 2, "recorded": [{"signal": "a", "frame": 2}]},
         {"ok": True, "ticks": 2, "recorded": [{"signal": "b", "frame": 1}], "recorded_truncated": True}],
        [2, 2])
    check("merge_chunk_replies offsets frames and ors truncation",
          merged["ticks"] == 4 and [e["frame"] for e in merged["recorded"]] == [2, 3]
          and merged.get("recorded_truncated") is True, str(merged))

    # the launch environment: scrub + allow-list, with the game's keys from the (fixture) vgcp.json
    shell = {"PATH": "/bin", "TMPDIR": "/scratch", "DEMO_MAP": "res://maps/one.json",
             "DEMO_SEED": "7", "DEMO_MODE": "debug", "DEMO_OTHER": "1", "VGCP_SOFTWARE": "1",
             "VGCP_XVFB_DISPLAY": ":5", "VGCP_SHOTS_DIR": "/somewhere/else"}
    env = harness.launch_env(shell, 40000, "/tmp/p.json", "play", {"DEMO_SEED": "9"}, scrub=True)
    check("a test launch drops inherited scenario keys and every key with the game's env_prefix",
          "DEMO_MAP" not in env and "DEMO_OTHER" not in env and env.get("DEMO_SEED") == "9", str(env))
    check("...drops inherited control-plane keys that are not lane keys",
          "VGCP_SHOTS_DIR" not in env, str(env))
    check("...keeps the lane keys, PATH and TMPDIR",
          env.get("VGCP_SOFTWARE") == "1" and env.get("VGCP_XVFB_DISPLAY") == ":5"
          and env.get("PATH") == "/bin" and env.get("TMPDIR") == "/scratch")
    check("...applies isolation keys (profile_env), meta.env and meta.mode (mode.env)",
          env["VGCP_PORT"] == "40000" and env["DEMO_PROFILE"] == "/tmp/p.json"
          and env["DEMO_SEED"] == "9" and env["DEMO_MODE"] == "play", str(env))
    adhoc = harness.launch_env(shell, 1, "/tmp/p.json", None, {"VGCP_PORT": "9", "DEMO_PROFILE": "x"},
                               scrub=False)
    check("an ad-hoc launch inherits the shell and never lets env steal the port or the profile",
          adhoc["DEMO_MAP"] == shell["DEMO_MAP"] and adhoc["VGCP_PORT"] == "1"
          and adhoc["DEMO_PROFILE"] == "/tmp/p.json" and adhoc["DEMO_MODE"] == "debug", str(adhoc))
    unprefixed = {"scenario_env": ["LEVEL_FILE"], "mode": {"env": "BOOT_MODE", "values": ["a"]},
                  "profile_env": "SAVE_FILE"}
    with use_config(unprefixed):
        env = harness.launch_env({"LEVEL_FILE": "1", "BOOT_MODE": "b", "SAVE_FILE": "s", "HOME": "/h"},
                                 1, None, None, None, scrub=True)
    check("a key vgcp.json names is scrubbed even with no env_prefix, and no profile is set without "
          "a profile path", env == {"HOME": "/h", "VGCP_PORT": "1"}, str(env))
    ok_env, err = harness.scenario_env({"env": {"DEMO_MAP": "a", "DEMO_SEED": 7}})
    check("meta.env accepts the scenario_env keys", err is None and ok_env == {"DEMO_MAP": "a", "DEMO_SEED": "7"},
          str(err))
    for bad in ("DEMO_MODE", "DEMO_PROFILE", "DEMO_MAPP", "PATH"):
        _, err = harness.scenario_env({"env": {bad: "x"}})
        check(f"meta.env rejects {bad} with a message naming it", err is not None and bad in err, str(err))
    _, err = harness.scenario_env({"env": {"DEMO_MODE": "x"}})
    check("...and says the mode variable is meta.mode's", "meta.mode" in (err or ""), str(err))
    check("meta_mode: absent is the first value, a listed value passes, another is refused",
          harness.meta_mode({}) == ("play", None) and harness.meta_mode({"mode": "debug"}) == ("debug", None)
          and harness.meta_mode({"mode": "developer"})[1] is not None)

    # meta.godot_args / meta.replays
    for good in ([], ["--max-fps", "10"], ["--max-fps", "10", "--frame-delay", "0"]):
        args, err = harness.godot_args({"godot_args": good})
        check(f"godot_args accepts {good}", err is None and args == good, str(err))
    for bad in (["--max-fps"], ["--headless", "1"], ["--max-fps", "ten"], ["--max-fps", "-1"], "x"):
        _, err = harness.godot_args({"godot_args": bad})
        check(f"godot_args rejects {bad!r}", err is not None, str(err))
    # --resolution WxH, and one extra arg list per replay
    for good in (["--resolution", "1600x900"], ["--max-fps", "10", "--resolution", "2560x1080"]):
        args, err = harness.godot_args({"godot_args": good})
        check(f"godot_args accepts {good}", err is None and args == good, str(err))
    for bad in (["--resolution", "1600"], ["--resolution", "1600x"], ["--resolution", "0x900"],
                ["--resolution", "1600X900"], ["--resolution", "-1x900"], ["--resolution", "16.5x9"],
                ["--resolution", "1600x900", "--resolution", "1280x720"]):
        _, err = harness.godot_args({"godot_args": bad})
        check(f"godot_args rejects {bad!r}", err is not None, str(err))
    sizes = [["--resolution", "1280x720"], ["--resolution", "1600x900"]]
    lists, err = harness.replay_godot_args({"replays": 2, "replay_godot_args": sizes}, 2)
    check("replay_godot_args accepts one allow-listed list per replay", err is None and lists == sizes, str(err))
    lists, err = harness.replay_godot_args({}, 3)
    check("replay_godot_args absent means an empty list per replay", err is None and lists == [[], [], []], str(err))
    for why, meta, n in (
        ("a length mismatch", {"replay_godot_args": sizes}, 3),
        ("a non-list", {"replay_godot_args": "--resolution 1280x720"}, 1),
        ("a non-list entry", {"replay_godot_args": ["--resolution", "1280x720"]}, 2),
        ("a non-allow-listed flag", {"replay_godot_args": [["--headless", "1"], []]}, 2),
        ("a malformed resolution", {"replay_godot_args": [["--resolution", "big"]]}, 1),
        ("a flag already in godot_args", {"godot_args": ["--resolution", "1280x720"],
                                          "replay_godot_args": [["--resolution", "1600x900"]]}, 1),
    ):
        _, err = harness.replay_godot_args(meta, n)
        check(f"replay_godot_args rejects {why} before launch", err is not None, str(err))
    check("requested_resolution reads WxH from a validated list",
          harness.requested_resolution(["--max-fps", "5", "--resolution", "1920x1080"]) == (1920, 1080)
          and harness.requested_resolution(["--max-fps", "5"]) is None)
    real_screen = harness._x_screen_size
    try:
        harness._x_screen_size = lambda display: (1280, 720)
        err = harness.window_size_error(None, ["--resolution", "1600x900"], 0, 140)
        check("a --resolution larger than the lane X screen fails before the script runs",
              err is not None and "larger than the X screen" in err, str(err))
    finally:
        harness._x_screen_size = real_screen
    check("replays defaults to 1 and rejects 0 / bool",
          harness.replay_count({}) == (1, None) and harness.replay_count({"replays": 0})[1]
          and harness.replay_count({"replays": True})[1] and harness.replay_count({"replays": 2}) == (2, None))
    same = [{"steps": [{"idx": 0, "label": "a", "reply": {"recorded": [1]}}]}] * 2
    diff = same[:1] + [{"steps": [{"idx": 0, "label": "a", "reply": {"recorded": [2]}}]}]
    check("replay_divergence: None when identical, names replay and step when not",
          harness.replay_divergence(same) is None
          and harness.replay_divergence(diff)["replay"] == 2
          and harness.replay_divergence(diff)["step"] == 0)

    _provenance_checks()
    _config_checks()

    _launcher_checks()


def _launcher_checks() -> None:
    """The launcher the harness runs: the one beside these tools, which needs no git. It is run only
    in a shape that dies before any display work, so this starts no Xvfb and no Godot."""
    sibling = os.path.normpath(os.path.join(_HERE, "..", "virtual-display", "launch_game.sh"))
    check("the harness's launcher is virtual-display/launch_game.sh beside these tools, and it exists",
          harness.LAUNCHER == sibling and os.path.isfile(sibling), harness.LAUNCHER)
    # A copy of virtual-display/ outside any git checkout (git may not look above the copy), with a
    # Godot binary that exists (`true`) and no Godot project. Its Xvfb and game helpers are stubs
    # that fail, so even a launcher that got past the project check would start nothing real.
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="vgcp-launcher-"))
    try:
        vd = os.path.join(tmp, "virtual-display")
        shutil.copytree(os.path.dirname(sibling), vd)
        for helper in ("start-xvfb.sh", "run-game.sh"):
            with open(os.path.join(vd, helper), "w", encoding="utf-8") as f:
                f.write("#!/usr/bin/env bash\nexit 1\n")
            os.chmod(os.path.join(vd, helper), 0o755)
        game = os.path.join(tmp, "game")
        os.makedirs(game)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("VGCP_", "GIT_")) and k != "GODOT4_BIN"}
        env.update(GODOT4_BIN=shutil.which("true") or "/bin/true", GIT_CEILING_DIRECTORIES=os.path.dirname(tmp))
        missing = os.path.join(tmp, "no-such-project")
        seen = []
        # VGCP_GODOT_PROJECT naming a missing project, then unset: the current directory's godot/
        for extra, want in (({"VGCP_GODOT_PROJECT": missing}, missing), ({}, os.path.join(game, "godot"))):
            out = subprocess.run(["bash", os.path.join(vd, "launch_game.sh"), "--port", "1", "--display", ":1"],
                                 cwd=game, env={**env, **extra}, capture_output=True, text=True, timeout=10)
            seen.append((out.returncode, out.stderr.strip()[:300]))
            ok = (out.returncode != 0 and f"no Godot project at {want} " in out.stderr
                  and "XVFB_PID" not in out.stderr)
            if not ok:
                break
        check("run from a copy outside git with no Godot project, the launcher exits non-zero naming the "
              "project it looked for (VGCP_GODOT_PROJECT, else the current directory's godot/), with no "
              "XVFB_PID line", len(seen) == 2 and ok, str(seen))
        _held_port_check(vd, game, env, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _held_port_check(vd: str, game: str, env: dict, tmp: str) -> None:
    """A held port is refused AFTER the launcher has started its Xvfb, so the refusal must stop it.
    The display is a stand-in (`start-xvfb.sh` execs `sleep`, and a stub `Xvfb` is on PATH), so no
    X server starts; the port is held by this process."""
    with open(os.path.join(vd, "start-xvfb.sh"), "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env bash\nexec sleep 30\n")
    stubs = os.path.join(tmp, "bin")
    os.makedirs(stubs)
    with open(os.path.join(stubs, "Xvfb"), "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(os.path.join(stubs, "Xvfb"), 0o755)
    os.makedirs(os.path.join(game, "godot"), exist_ok=True)
    open(os.path.join(game, "godot", "project.godot"), "w", encoding="utf-8").close()
    # a display with no lock (so the launcher starts one) and no log (which the launcher writes)
    disp = next(n for n in range(190, 1000) if not os.path.exists(f"/tmp/.X{n}-lock")
                and not os.path.exists(f"/tmp/vgcp-xvfb{n}.log"))

    def gone(pid: int) -> bool:          # exited, a zombie, or dead and being reaped
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                return f.read().rsplit(") ", 1)[1].startswith(("Z", "X"))
        except OSError:
            return True

    xpid = None
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            out = subprocess.run(
                ["bash", os.path.join(vd, "launch_game.sh"), "--port", str(port), "--display", f":{disp}"],
                cwd=game, env={**env, "PATH": stubs + os.pathsep + env.get("PATH", "")},
                capture_output=True, text=True, timeout=20)
            lines = [l for l in out.stderr.splitlines() if l.startswith("[launch] XVFB_PID=")]
            xpid = int(lines[0].split("=", 1)[1]) if lines else None
            deadline = time.time() + 3
            while xpid and not gone(xpid) and time.time() < deadline:
                time.sleep(0.1)
            check("a held port is refused (exit 1) after the launcher started its Xvfb, and the "
                  "refusal stops that Xvfb", out.returncode == 1 and "already in use" in out.stderr
                  and xpid is not None and gone(xpid), f"rc={out.returncode} xvfb={xpid} "
                  f"{out.stderr.strip()[-300:]}")
        finally:
            if xpid and not gone(xpid):
                try:
                    os.kill(xpid, 9)
                except ProcessLookupError:
                    pass
            try:
                os.unlink(f"/tmp/vgcp-xvfb{disp}.log")
            except OSError:
                pass


def _provenance_checks() -> None:
    """The `recorded_against` stamp and its rule. A FAKE git proves the code never reaches for a
    git verb that writes (no `add`, no `stash`, no `bisect`); a REAL throwaway repository proves
    what counts as dirty, because that is a question about git's own answers."""
    class FakeGit:
        """Answers the three read-only questions provenance asks; anything else is a failure."""

        def __init__(self, root: str, status: str = ""):
            self.root, self.status, self.verbs = root, status, []

        def __call__(self, args, cwd=None):
            self.verbs.append(args[0])
            if args[:2] == ["rev-parse", "--show-toplevel"]:
                return self.root + "\n"
            if args[:2] == ["rev-parse", "HEAD"]:
                return "a" * 40 + "\n"
            if args[0] == "status":
                self.status_args = list(args)
                if self.status is None:
                    raise provenance.GitError("git status -> 128: index file corrupt")
                return self.status
            raise AssertionError(f"unexpected git call: {args}")

    with tempfile.TemporaryDirectory() as tmp:
        s1 = os.path.join(tmp, "one.vgcp.json")
        s2 = os.path.join(tmp, "two.vgcp.json")
        clean = provenance.make_stamp(s1, "one", run=FakeGit(tmp), quiet=True)
        check("a clean tree stamps commit + dirty:false, and nothing else",
              clean == {"commit": "a" * 40, "dirty": False}, str(clean))

        g = FakeGit(tmp, " M rust/src/game.rs\0?? godot/data/new.json\0"
                         "R  tools/vgcp-test/b.py\0tools/vgcp-test/a.py\0"
                         " R tools/vgcp-mcp/d.py\0tools/vgcp-mcp/c.py\0")
        d2 = provenance.make_stamp(s2, "two", run=g, quiet=True)
        check("a dirty tree stamps commit + dirty:true, with no diff, link or store",
              d2 == {"commit": "a" * 40, "dirty": True}
              and os.listdir(tmp) == [], f"{d2} {os.listdir(tmp)}")
        check("dirty_paths reads -z porcelain: one path per entry, a rename's old name skipped "
              "whether the rename is staged (X) or in the worktree (Y)",
              provenance.dirty_paths(tmp, run=g)
              == ["rust/src/game.rs", "godot/data/new.json", "tools/vgcp-test/b.py",
                  "tools/vgcp-mcp/d.py"])
        broken = provenance.make_stamp(s2, "two", run=FakeGit(tmp, None), quiet=True)
        check("a git that cannot answer stamps dirty:true and never raises (unknown is not clean, "
              "and a recording is not lost to it)", broken == {"commit": "a" * 40, "dirty": True})
        check("...and asks only about the paths that can affect a recording",
              g.status_args[g.status_args.index("--") + 1:] == DEMO_CONFIG["record_paths"]
              and "--untracked-files=all" in g.status_args, str(g.status_args))
        check("provenance never runs a git verb that writes",
              set(g.verbs) <= {"rev-parse", "status", "log"}, str(sorted(set(g.verbs))))
        import inspect
        check("...and every real call carries --no-optional-locks, so `status` does not even "
              "refresh the index", '"--no-optional-locks"' in inspect.getsource(provenance.git))

        # save() stamps what it writes, and stamp=False opts out.
        class _NoProvider:
            def list_providers(self):
                return {"providers": []}

        rec = Recorder(_NoProvider(), meta={"id": "one", "title": "t"})
        rec.steps.append({"cmd": "step", "args": {"ticks": 1}})
        real_git = provenance.git
        try:
            provenance.git = FakeGit(tmp)
            saved = rec.save(s1)
            provenance.git = FakeGit(tmp, " M rust/src/game.rs\0")
            saved_dirty = rec.save(s2)
        finally:
            provenance.git = real_git
        on_disk = json.load(open(s1))["meta"]
        check("save() stamps the recording it writes",
              saved["meta"]["recorded_against"] == {"commit": "a" * 40, "dirty": False}
              and on_disk["recorded_against"] == saved["meta"]["recorded_against"], str(on_disk))
        check("save() on a dirty tree still writes the recording, stamped dirty:true",
              saved_dirty["meta"]["recorded_against"] == {"commit": "a" * 40, "dirty": True}
              and json.load(open(s2))["meta"]["recorded_against"]["dirty"] is True)
        check("the stamp sits after the PR fields",
              list(provenance.with_stamp(
                  {"id": "x", "introduced_in_pr": 1, "modified_in_prs": [], "env": {}},
                  {"commit": "c", "dirty": False}))
              == ["id", "introduced_in_pr", "modified_in_prs", "recorded_against", "env"])
        check("no checkout -> no stamp, and the recording is still written",
              provenance.with_stamp({"id": "x"}, None) == {"id": "x"})

        # Triage: a stale stamp is INFORMATION. The packet carries the raw stamp (the good endpoint a
        # bisect takes) and prints no command.
        meta = {"id": "one", "recorded_against": {"commit": "b" * 40, "dirty": False}}
        note = provenance.triage_note(s1, meta, head="c" * 40)
        check("a stale recording is reported with its commit, and the note carries the raw stamp",
              note["stale"] is True and "replay it there" in note["note"]
              and note["stamp"] == meta["recorded_against"] and "dirty" not in note
              and "bisect" not in note and "bisect_note" not in note, str(note))
        real_last_green = harness._load_last_green
        try:
            harness._load_last_green = lambda: {}      # no last-green file in this checkout's git dir
            packet = harness.triage_packet(s1, {"meta": meta, "steps": [{"cmd": "step", "args": {}}]},
                                           {"passed": False, "failed_step": 0, "steps": []})
        finally:
            harness._load_last_green = real_last_green
        check("the triage packet still carries meta.recorded_against (recorded_against.stamp)",
              (packet.get("recorded_against") or {}).get("stamp") == meta["recorded_against"],
              str(packet.get("recorded_against")))
        fresh = provenance.triage_note(s1, {"id": "one", "recorded_against": {"commit": "c" * 40}},
                                       head="c" * 40)
        check("a recording made at HEAD says the failure is in the working tree",
              fresh["stale"] is False and "not in history" in fresh["note"], str(fresh))
        dirty_note = provenance.triage_note(s2, {"id": "two", "recorded_against": d2}, head="c" * 40)
        check("a dirty-tree recording says its commit is approximate, and offers no restore",
              "DIRTY" in dirty_note["note"] and "approximately" in dirty_note["dirty"]
              and "restore" not in dirty_note and "diff" not in dirty_note, str(dirty_note))
        old = provenance.triage_note(
            s2, {"id": "two", "recorded_against": {"commit": "b" * 40, "dirty": True,
                                                   "diff": "diff-store/deadbeef.diff",
                                                   "diff_link": "diff-store/by-test/two.diff"}},
            head="c" * 40)
        check("an older stamp that still carries diff keys is read without raising",
              "approximately" in old["dirty"] and "restore" not in old, str(old))
        check("triage never raises, even with no stamp at all",
              harness._provenance_note(s1, {"id": "one"})["stamp"] is None)

    # What counts as dirty is a question about git's own answers, so ask a real one. The layout is
    # DEMO_CONFIG's: the game in game/, the VGCP tools under vgcp/ (the launcher in vgcp/launcher/),
    # and a link to the launcher's directory, the way another directory can link to it.
    with tempfile.TemporaryDirectory() as tmp:
        def sh(*args):
            _git_sh(tmp, *args)

        def put(rel, body="x\n"):
            _put(tmp, rel, body)

        game = DEMO_CONFIG["game_dir"]
        launcher = "vgcp/launcher/launch_game.sh"
        lib = f"{game}/rust/src/lib.rs"
        new_file = f"{game}/godot/data/new.json"
        tool = "vgcp/vgcp-test/t.py"
        # the VGCP server crate the game compiles in: its source is the game's code, its README a doc
        server_src = "vgcp/vgcp-server/src/lib.rs"
        server_readme = "vgcp/vgcp-server/README.md"
        sh("init", "-q")
        for rel in (f"{game}/godot/data/one.json", lib, tool, server_src, server_readme,
                    f"{game}/tools/gen.py", launcher, "docs/notes.md"):
            put(rel)
        os.makedirs(os.path.join(tmp, "links"))
        os.symlink(os.path.join("..", "vgcp", "launcher"), os.path.join(tmp, "links", "launcher"))
        put(f"{game}/tests/vgcp/one.vgcp.json", json.dumps({
            "vgcp_script": 1, "steps": [],
            "meta": {"id": "one", "title": "t", "instructions": "one.md",
                     "recorded_against": {"commit": "0" * 40, "dirty": False}}}))
        # no trailing slash on the ignore pattern: a worktree's rust/target is often a SYMLINK to the
        # main checkout's, and a directory-only pattern ("rust/target/") leaves a symlink untracked
        put(".gitignore", f"/{game}/rust/target\n")
        sh("add", "-A")
        sh("commit", "-q", "-m", "c")
        script = os.path.join(tmp, game, "tests", "vgcp", "two.vgcp.json")
        check("real git: a fresh commit is clean", provenance.dirty_paths(tmp) == []
              and record.dirty_refusal(script, code=tmp) is None)
        first = os.path.join(tmp, game, "tests", "vgcp", "new", "first.vgcp.json")
        check("real git: a path under a directory not created yet belongs to its ancestor's "
              "checkout", os.path.realpath(provenance.repo_root(first) or "/") == os.path.realpath(tmp),
              str(provenance.repo_root(first)))
        rec_first = Recorder(_NoProvider(), meta={"id": "first", "title": "t"})
        rec_first.steps.append({"cmd": "step", "args": {"ticks": 1}})
        first_stamp = rec_first.save(first)["meta"].get("recorded_against") or {}
        check("real git: save() creates a missing tests/vgcp/ and stamps the commit, clean",
              os.path.isfile(first) and len(first_stamp.get("commit") or "") == 40
              and first_stamp.get("dirty") is False, str(first_stamp))
        put("docs/notes.md", "edited\n")
        put(f"{game}/docs/reports/log.jsonl")
        put(f"{game}/tests/vgcp/two.vgcp.json")
        put(f"{game}/tests/vgcp/INDEX.md")
        put(f"{game}/tools/gen.py", "edited\n")
        put("other/tool.py")
        put(server_readme, "edited\n")
        with tempfile.TemporaryDirectory() as elsewhere:
            os.symlink(elsewhere, os.path.join(tmp, game, "rust", "target"))
            check("real git: docs (the server crate's README included), logs, the recorder's "
                  "own outputs, tools that cannot touch a game and a symlinked rust/target do NOT "
                  "make a recording dirty (a second recording stays clean)",
                  provenance.dirty_paths(tmp) == [], str(provenance.dirty_paths(tmp)))
        os.unlink(os.path.join(tmp, game, "rust", "target"))
        put(new_file)
        check("real git: an UNTRACKED file under the game's godot/ does (git diff alone would miss it)",
              provenance.dirty_paths(tmp) == [new_file], str(provenance.dirty_paths(tmp)))
        put(lib, "changed\n")
        sh("add", lib)
        put(tool, "changed\n")
        put(server_src, "changed\n")
        # edited THROUGH the link: git reports the real path, and only a pathspec naming the real
        # directory sees it (git does not follow a symlink)
        put("links/launcher/launch_game.sh", "changed\n")
        check("real git: staged and unstaged changes under the game's rust/, the VGCP server "
              "crate's src/, vgcp/vgcp-test/ and the launcher, edited through a link, do",
              sorted(provenance.dirty_paths(tmp))
              == sorted([new_file, lib, tool, server_src, launcher]),
              str(provenance.dirty_paths(tmp)))
        with use_config(dict(DEMO_CONFIG, record_paths=["links/launcher"])):
            through_link = provenance.dirty_paths(tmp)
        check("real git: a record_paths entry naming the LINK sees nothing edited through it, so each "
              "must name a real directory", through_link == [], str(through_link))
        with use_config({"game_dir": game}):
            default = sorted(provenance.dirty_paths(tmp))
        check("real git: with no record_paths, everything but <game_dir>/tests/vgcp counts",
              launcher in default and "docs/notes.md" in default and "other/tool.py" in default
              and not any(p.startswith(f"{game}/tests/vgcp/") for p in default), str(default))
        why = record.dirty_refusal(script, code=tmp)
        check("dirty_refusal names the paths, the rule and the override",
              why is not None and lib in why and "record from a commit" in why
              and "--allow-dirty" in why, str(why))
        with tempfile.TemporaryDirectory() as outside:
            out = os.path.join(outside, "z.vgcp.json")
            check("...and asks the CODE checkout too, so --out outside it is no way round",
                  provenance.repo_root(out) is None
                  and lib in (record.dirty_refusal(out, code=tmp) or ""))
        check("a git that cannot answer is a refusal, not a pass (nothing is recorded yet)",
              "could not say" in (record.dirty_refusal(script, code=os.path.join(tmp, "gone")) or ""))
        cwd = os.getcwd()
        try:
            os.chdir(os.path.join(tmp, "docs"))
            code = record.code_root()
        finally:
            os.chdir(cwd)
        check("record.code_root() is the current directory's checkout, not the tools' one",
              code is not None and os.path.realpath(code) == os.path.realpath(tmp), str(code))

        # The CLI wiring, not just the helper: refused with exit 2 before anything connects;
        # --allow-dirty gets past the refusal; a re-recording never keeps the old stamp.
        one = os.path.join(tmp, game, "tests", "vgcp", "one.vgcp.json")

        class _Reached(Exception):
            pass

        class _FakeClient:
            def __init__(self, port=None):
                raise _Reached()

        real_client = record.VgcpClient
        try:
            record.VgcpClient = _FakeClient
            rc = record._rerecord_main(["--rerecord", one, "--port", "1"])
            check("record.py --rerecord on a dirty tree exits 2 and never connects", rc == 2, str(rc))
            reached = False
            try:
                record._rerecord_main(["--rerecord", one, "--port", "1", "--allow-dirty"])
            except _Reached:
                reached = True
            check("...and --allow-dirty gets past the refusal", reached)
        finally:
            record.VgcpClient = real_client

# A stand-in for the launcher: it records what it was run with and starts nothing, so `_launch` can be
# checked with no display and no game (it prints no pid, so `_launch` returns None).
FAKE_LAUNCHER = """#!/usr/bin/env bash
python3 - "$@" <<'PY'
import json, os, sys
with open({log!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps({{"args": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
PY
"""


def _last_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1] if lines else ""


def _config_checks() -> None:
    """vgcp.json: where it is looked up (never with git), what a bad one does, and what the tools do
    with no config at all and with the fixture."""
    with tempfile.TemporaryDirectory() as t:
        t = os.path.realpath(t)
        outside_git = provenance._checkout_top(t) is None

        # ---- lookup: the tools' checkout first, then the current directory's; no git -------------
        tools_repo, game_repo, loose = (os.path.join(t, n) for n in ("tools-repo", "game-repo", "loose"))
        tools = os.path.join(tools_repo, "vgcp", "vgcp-test")
        os.makedirs(os.path.join(tools_repo, ".git"))
        os.makedirs(tools)
        os.makedirs(os.path.join(game_repo, "sub"))
        os.makedirs(os.path.join(loose, "vgcp-test"))
        _put(game_repo, ".git", "gitdir: /nonexistent/.git/worktrees/x\n")   # a linked worktree's .git
        _put(game_repo, "vgcp.json", "{}\n")
        link = os.path.join(t, "tools-link")
        os.symlink(tools, link)
        real_run = provenance.subprocess.run

        def no_git(*a, **k):
            raise AssertionError(f"find_config ran a process: {a}")

        try:
            provenance.subprocess.run = no_git
            cwd = os.path.join(game_repo, "sub")
            found_cwd = provenance.find_config(tools, cwd)
            _put(tools_repo, "vgcp.json", "{}\n")
            found_tools = provenance.find_config(tools, cwd)
            found_link = provenance.find_config(link, cwd)
            found_loose = provenance.find_config(os.path.join(loose, "vgcp-test"), cwd)
            _put(tools_repo, "vgcp/.git", "gitdir: ../.git/modules/vgcp\n")    # VGCP as a submodule
            found_sub = provenance.find_config(tools, cwd)
            found_none = provenance.find_config(os.path.join(loose, "vgcp-test"), loose)
            ran_git = False
        except AssertionError:
            ran_git = True
        finally:
            provenance.subprocess.run = real_run
        game_cfg, tools_cfg = os.path.join(game_repo, "vgcp.json"), os.path.join(tools_repo, "vgcp.json")
        check("find_config runs no git (git bisect run's GIT_DIR cannot mislead it)", not ran_git)
        if not ran_git:
            check("find_config: the tools' checkout has none -> the current directory's checkout's",
                  found_cwd == game_cfg, str(found_cwd))
            check("find_config: the tools' checkout's vgcp.json wins, also when the tools are reached "
                  "through a link", found_tools == tools_cfg and found_link == tools_cfg,
                  f"{found_tools} {found_link}")
            check("find_config: tools outside any checkout, or in a submodule without one, use the "
                  "current directory's", found_loose == game_cfg and found_sub == game_cfg,
                  f"{found_loose} {found_sub}")
            check("find_config: no checkout anywhere -> no config",
                  found_none is None or not outside_git, str(found_none))

        # ---- bad files: every one raises ValueError naming the file --------------------------------
        def parse(name: str, body: str) -> tuple[str, Optional[dict], str]:
            path = os.path.join(t, "cfg", name, "vgcp.json")
            _put(t, os.path.relpath(path, t), body)
            try:
                return path, provenance.parse_config(path), ""
            except ValueError as e:
                return path, None, str(e)

        for why, body, needle in (
            ("an unknown key", {"game_dir": "g", "scenario": []}, "scenario"),
            ("a string where a list goes", {"scenario_env": "DEMO_MAP"}, "scenario_env"),
            ("a list of non-strings", {"record_paths": ["game", 3]}, "record_paths"),
            ("a non-string env_prefix", {"env_prefix": 1}, "env_prefix"),
            ("a mode without values", {"mode": {"env": "DEMO_MODE"}}, "mode"),
            ("a mode with an empty values list", {"mode": {"env": "DEMO_MODE", "values": []}}, "mode"),
            ("a file that is not an object", [], "object"),
        ):
            path, cfg, err = parse(why.replace(" ", "-"), json.dumps(body))
            check(f"parse_config refuses {why} with a ValueError naming the file and the key",
                  cfg is None and path in err and needle in err, err or str(cfg))
        path, cfg, err = parse("malformed", "{")
        check("parse_config refuses malformed JSON, naming the file", cfg is None and path in err, err)
        _, cfg, err = parse("demo", json.dumps(DEMO_CONFIG))
        check("the fixture config parses to itself", cfg == DEMO_CONFIG, err)
        _, cfg, err = parse("empty", "{}")
        check("an empty config is valid (every key is optional)", cfg == {}, err)
        if outside_git:
            copy = os.path.join(t, "copy")
            os.makedirs(copy)
            shutil.copy(os.path.join(_HERE, "provenance.py"), copy)
            os.makedirs(os.path.join(t, "bad-game", ".git"))
            _put(t, "bad-game/vgcp.json", '{"game_dir": "g", "bogus": 1}\n')
            p = subprocess.run([sys.executable, "-c", "import provenance"], cwd=os.path.join(t, "bad-game"),
                               env=dict(os.environ, PYTHONPATH=copy), capture_output=True, text=True,
                               timeout=30)
            check("a malformed vgcp.json stops the import, naming the file",
                  p.returncode != 0 and os.path.join(t, "bad-game", "vgcp.json") in p.stderr
                  and "bogus" in p.stderr, _last_line(p.stderr))

        # ---- no config: only VGCP_* scrubbed, meta.env and meta.mode refused, no profile ----------
        with use_config({}):
            env = harness.launch_env({"DEMO_MAP": "x", "VGCP_SHOTS_DIR": "y", "VGCP_SOFTWARE": "1"},
                                     1, None, "play", None, scrub=True)
            check("no config: a test launch scrubs only VGCP_* (lane keys kept) and sets no mode "
                  "or profile", env == {"DEMO_MAP": "x", "VGCP_SOFTWARE": "1", "VGCP_PORT": "1"}, str(env))
            _, err = harness.scenario_env({"env": {"DEMO_MAP": "x"}})
            check("no config: a non-empty meta.env is refused", err is not None and "DEMO_MAP" in err, str(err))
            check("no config: an empty meta.env is fine", harness.scenario_env({"env": {}}) == ({}, None))
            check("no config: no meta.mode means no mode; a meta.mode is refused",
                  harness.meta_mode({}) == (None, None) and harness.meta_mode({"mode": "play"})[1] is not None)
            meta = Recorder.validate_meta({"id": "x", "title": "t"})
            check("no config: validate_meta fills in neither a mode nor a scene",
                  "mode" not in meta and "scene" not in meta, str(meta))
            try:
                Recorder.validate_meta({"id": "x", "mode": "play"})
                refused = False
            except ValueError:
                refused = True
            check("no config: compile() refuses a meta.mode", refused)
            check("no config: the default dirty rule is the whole checkout but tests/vgcp",
                  provenance.affects_recording() == [".", ":(exclude)tests/vgcp"],
                  str(provenance.affects_recording()))

            class _NoCalls:
                def __getattr__(self, name):
                    raise AssertionError(f"save() asked the game for {name}")

            out = os.path.join(t, "no-mode.vgcp.json")
            try:
                saved = Recorder(_NoCalls()).save(out, {"id": "x", "title": "t"}, stamp=False)
                ok, detail = "mode" not in saved["meta"] and os.path.exists(out), str(saved["meta"])
            except AssertionError as e:
                ok, detail = False, str(e)
            check("no config: save() writes with no mode and never asks the game for its live mode",
                  ok, detail)
            try:
                with harness.managed_game(None, mode="play"):
                    pass
                refused = False
            except ValueError:
                refused = True
            check("no config: managed_game refuses a mode before launching anything", refused)
            err_out = io.StringIO()
            try:
                with contextlib.redirect_stderr(err_out):
                    harness.main(["--play", "--mode", "play"])
                code = 0
            except SystemExit as e:
                code = e.code
            check("no config: harness.py --mode is refused", code == 2 and "configures no mode" in err_out.getvalue(),
                  _last_line(err_out.getvalue()))
        err_out = io.StringIO()
        try:
            with contextlib.redirect_stderr(err_out):
                harness.main(["--play", "--mode", "banana"])
            code = 0
        except SystemExit as e:
            code = e.code
        check("harness.py --mode takes only mode.values", code == 2 and "banana" in err_out.getvalue(),
              _last_line(err_out.getvalue()))

        # ---- launch: the game directory is the CURRENT checkout's; --scene only when set ----------
        repo = os.path.join(t, "launch-repo")
        os.makedirs(repo)
        _git_sh(repo, "init", "-q")
        log = os.path.join(t, "launches.jsonl")
        fake = os.path.join(t, "fake_launch.sh")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE_LAUNCHER.format(log=log))

        def launches() -> list[dict]:
            if not os.path.exists(log):
                return []
            with open(log, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]

        cwd, real_launcher = os.getcwd(), harness.LAUNCHER
        try:
            os.chdir(repo)
            harness.LAUNCHER = fake                # the harness runs LAUNCHER as it finds it at launch
            with use_config({}):
                harness._launch(None, 1, 1, None, None, scrub=True)
                _put(repo, "project.godot", "")
                harness._launch(None, 1, 1, None, None, scrub=True)
                os.unlink(os.path.join(repo, "project.godot"))
                script = os.path.join(t, "moded.vgcp.json")
                _put(t, "moded.vgcp.json", json.dumps({"vgcp_script": 1, "steps": [],
                                                        "meta": {"id": "m", "mode": "play"}}))
                moded = harness.run_one(script, 1, 1)
                _put(t, "moded.vgcp.json", json.dumps({"vgcp_script": 1, "steps": [],
                                                        "meta": {"id": "m", "env": {"DEMO_MAP": "x"}}}))
                envd = harness.run_one(script, 1, 1)
            with use_config(DEMO_CONFIG):
                harness._launch("res://x.tscn", 2, 2, "debug", {"DEMO_MAP": "m"}, scrub=True)
                _put(repo, "game/project.godot", "")
                harness._launch("", 3, 3, "play", None, scrub=True)
            gitdir = os.path.realpath(os.path.join(repo, ".git"))
        finally:
            os.chdir(cwd)
            harness.LAUNCHER = real_launcher
        runs = launches()
        check("the fake launcher ran four times (and run_one refused two scripts before launching)",
              len(runs) == 4, str([r["args"] for r in runs]))
        if len(runs) == 4:
            a, b, c, d = runs

            def proj(r):
                return os.path.realpath(r["env"].get("VGCP_GODOT_PROJECT", ""))

            check("no config: no meta.scene -> no --scene (Godot boots the project's main scene)",
                  a["args"] == ["--port", "1", "--display", ":1"], str(a["args"]))
            check("no config: no profile variable is set",
                  not any(v.endswith("profile-1.json") for v in a["env"].values()), "")
            check("godot_project: <checkout>/godot, or the checkout itself when it holds project.godot",
                  proj(a) == os.path.join(repo, "godot") and proj(b) == repo, f"{proj(a)} {proj(b)}")
            check("a scene is passed as --scene; profile_env and mode.env are set from the config",
                  c["args"] == ["--port", "2", "--display", ":2", "--scene", "res://x.tscn"]
                  and os.path.realpath(c["env"].get("DEMO_PROFILE", "")) == os.path.join(gitdir, "vgcp-test",
                                                                                         "profile-2.json")
                  and c["env"].get("DEMO_MODE") == "debug" and c["env"].get("DEMO_MAP") == "m", str(c["args"]))
            check("game_dir is joined to the CURRENT directory's checkout (never the tools' one): "
                  "<game_dir>/godot, or <game_dir> when it holds project.godot; an empty scene is none",
                  proj(c) == os.path.join(repo, "game", "godot") and proj(d) == os.path.join(repo, "game")
                  and "--scene" not in d["args"], f"{proj(c)} {proj(d)} {d['args']}")
        check("no config: run_one refuses a meta.mode and a meta.env as bad_meta, before launching",
              moded.get("error_kind") == "bad_meta" and envd.get("error_kind") == "bad_meta",
              f"{moded} {envd}")

        # ---- outside a git checkout the harness refuses instead of inventing a .git -----------------
        if outside_git:
            nogit = os.path.join(t, "no-git")
            os.makedirs(nogit)
            err_out = io.StringIO()
            try:
                os.chdir(nogit)
                with contextlib.redirect_stderr(err_out):
                    rc = harness.main(["--test", "x"])
            finally:
                os.chdir(cwd)
            check("outside a git checkout harness.py refuses (exit 2) and creates no .git",
                  rc == 2 and "git checkout" in err_out.getvalue()
                  and not os.path.exists(os.path.join(nogit, ".git")), _last_line(err_out.getvalue()))


def main() -> int:
    with use_config(DEMO_CONFIG):
        _main()
    print(f"\n[vgcp-test runner selftest] {'ALL PASS' if _fail == 0 else str(_fail) + ' FAIL'}")
    return 0 if _fail == 0 else 1


def _main() -> None:
    _offline_checks()
    port = _free_port()
    mock = subprocess.Popen(
        [sys.executable, os.path.join(_HERE, "..", "vgcp-mcp", "mock_server.py"),
         "--port", str(port), "--quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait(port)
        with VgcpClient(port=port, timeout=5.0) as c:
            # all steps pass (mock hud = {gold:120, lives:18, wave:3})
            r = run_script(c, {"steps": [
                {"cmd": "await_state", "args": {"provider": "hud", "op": "eq", "path": "lives",
                                                "value": 18, "timeout_ticks": 50}, "label": "lives==18"},
                {"cmd": "assert", "args": {"provider": "hud", "op": "gt", "path": "gold", "value": 100}},
                {"cmd": "step", "args": {"ticks": 3}},
            ]})
            check("all-pass script passes", r["passed"] is True and "failed_step" not in r, str(r["passed"]))
            check("all 3 steps recorded", len(r["steps"]) == 3)

            # a failing assert stops the script
            r2 = run_script(c, {"steps": [
                {"cmd": "assert", "args": {"provider": "hud", "op": "eq", "path": "lives", "value": 18}},
                {"cmd": "assert", "args": {"provider": "hud", "op": "eq", "path": "lives", "value": 99}},
                {"cmd": "assert", "args": {"provider": "hud", "op": "exists", "path": "gold"}},
            ]})
            check("failing assert -> not passed, failed_step=1",
                  r2["passed"] is False and r2.get("failed_step") == 1, str(r2.get("failed_step")))
            check("stops on failure (2 of 3 steps run)", len(r2["steps"]) == 2)

            # `expect` override: a timeout is the EXPECTED outcome (mock holds past the budget)
            r3 = run_script(c, {"steps": [
                {"cmd": "await_state",
                 "args": {"provider": "hud", "op": "eq", "path": "lives", "value": 18,
                          "timeout_ticks": 3, "mock_hold_after": 999},
                 "expect": {"held": False, "timed_out": True}, "label": "expect a timeout"},
            ]})
            check("expect override passes on the expected timeout", r3["passed"] is True, str(r3["passed"]))

            # an error reply (unknown cmd) fails the step
            r4 = run_script(c, {"steps": [{"cmd": "frobnicate", "args": {}}]})
            check("error step fails", r4["passed"] is False and r4.get("failed_step") == 0)

            # record-and-compile round trip: record a session, compile, REPLAY -> passes.
            # snapshot() auto-captures the live state into assert steps (no hand-written values).
            rec = Recorder(c)
            rec.seed(42, label="deterministic run")
            rec.await_state("hud", "eq", 18, path="lives", timeout_ticks=50, label="lives==18")
            # the mock mirrors the accepted seed into hud.seed, so the snapshot captures 42 from
            # REALITY (never hand-written) and the replay re-verifies it after its own seed step
            rec.snapshot("hud", ["lives", "gold", "wave", "seed"], label_prefix="hud")
            rec.game_action("pause", label="canonical action")
            rec.set_timescale(2.0)
            rec.step(1)
            compiled = rec.compile({"id": "rt", "title": "roundtrip", "scene": "res://main.tscn",
                                    "instructions": "x.md", "authored_resolution": [1280, 720]})
            check("snapshot auto-asserts captured the real value (lives==18)",
                  any(s["cmd"] == "assert" and s["args"].get("path") == "lives"
                      and s["args"].get("value") == 18 for s in compiled["steps"]))
            check("Recorder.seed captured the seed step first",
                  compiled["steps"][0] == {"cmd": "seed", "args": {"seed": 42},
                                           "label": "deterministic run"},
                  str(compiled["steps"][0]))
            check("Recorder.seed round-trips through the state (hud.seed == 42)",
                  any(s["cmd"] == "assert" and s["args"].get("path") == "seed"
                      and s["args"].get("value") == 42 for s in compiled["steps"]))
            check("Recorder.game_action compiles to an `input` step (no new step kind)",
                  any(s["cmd"] == "input" and s["args"] == {"type": "game_action", "action": "pause"}
                      for s in compiled["steps"]), str(compiled["steps"]))
            check("Recorder.set_timescale recorded verbatim",
                  any(s["cmd"] == "set_timescale" and s["args"] == {"value": 2.0}
                      for s in compiled["steps"]))
            rr = run_script(c, compiled)
            check("recorded -> compiled -> replayed script passes",
                  rr["passed"] is True, str(rr.get("failed_step")))

            # a `seed` step against a game with no registered seed target fails the script there
            r5 = run_script(c, {"steps": [
                {"cmd": "seed", "args": {"seed": 42, "mock_no_seed_target": True}},
                {"cmd": "assert", "args": {"provider": "hud", "op": "exists", "path": "gold"}},
            ]})
            check("a seed step with no seed target fails at that step",
                  r5["passed"] is False and r5.get("failed_step") == 0
                  and r5["steps"][0]["reply"]["error"]["code"] == "no_seed_target", str(r5))

            # meta.mode: the harness exports it as vgcp.json's mode.env for the replay launch,
            # so with a mode configured compile() must always produce one and never a bogus one.
            check("compile() defaults meta.mode to the first of mode.values", compiled["meta"]["mode"] == "play",
                  str(compiled["meta"].get("mode")))
            dev = Recorder(c).compile({"id": "rt-dev", "title": "dev", "mode": "debug",
                                       "instructions": "x.md"})
            check("compile() keeps an explicit meta.mode", dev["meta"]["mode"] == "debug",
                  str(dev["meta"].get("mode")))
            try:
                Recorder(c).compile({"id": "rt-bad", "title": "bad", "mode": "banana"})
                bad_rejected = False
            except ValueError:
                bad_rejected = True
            check("compile() rejects an unknown meta.mode", bad_rejected)

            # ---- VGCP 1.5.1: the skipped rule ----
            refusing = {"resolution": [1280, 720], "frames": [
                {"frame": 0, "events": [{"type": "game_action", "action": "no_such_action"}]}]}
            base = {"cmd": "run_input_script", "args": {"script": refusing, "mock_sink_refuses": True}}
            r6 = run_script(c, {"steps": [dict(base)]})
            check("a run_input_script step with skipped events fails", r6["passed"] is False
                  and r6.get("failed_step") == 0, str(r6["steps"][0]["reply"]))
            r7 = run_script(c, {"steps": [dict(base, expect={"completed": True})]})
            check("...even when expect names other keys", r7["passed"] is False, str(r7["passed"]))
            want = r6["steps"][0]["reply"]["skipped"]
            r8 = run_script(c, {"steps": [dict(base, expect={"skipped": want})]})
            check("...and passes when expect.skipped matches", r8["passed"] is True, str(r8["passed"]))
            r9 = run_script(c, {"steps": [dict(base, expect={"skipped": []})]})
            check("...and fails when expect.skipped differs", r9["passed"] is False, str(r9["passed"]))
            clean = run_script(c, {"steps": [{"cmd": "run_input_script", "args": {"script": refusing}}]})
            check("a clean run_input_script step (skipped []) passes", clean["passed"] is True)

            # ---- step chunk: merged replies with frame offsets ----
            chunked = run_script(c, {"steps": [{
                "cmd": "step", "chunk": 2,
                "args": {"ticks": 5, "record_signals": [{"provider": "hud"}],
                         "mock_recorded": [{"signal": "tick", "frame": 1, "args": []}]},
                "expect": {"ticks": 5, "recorded": [
                    {"signal": "tick", "frame": 1, "args": []},
                    {"signal": "tick", "frame": 3, "args": []},
                    {"signal": "tick", "frame": 5, "args": []}]}}]})
            check("a chunked step merges replies (ticks summed, frames offset)",
                  chunked["passed"] is True, str(chunked["steps"][0]["reply"]))
            bad_chunk = run_script(c, {"steps": [{"cmd": "assert", "chunk": 2, "args": {}}]})
            check("chunk on a non-step command fails the step", bad_chunk["passed"] is False)
            zero_chunk = run_script(c, {"steps": [{"cmd": "step", "chunk": 0, "args": {"ticks": 3}}]})
            check("chunk 0 fails the step", zero_chunk["passed"] is False)

            # ---- Recorder additions ----
            rec3 = Recorder(_HintingClient(c, {"mock_sink_refuses": True}))
            try:
                rec3.run_input_script(refusing, label="refused")
                raised = False
            except SkippedEventsError:
                raised = True
            check("Recorder.run_input_script raises on skipped events", raised and rec3.steps == [])
            rec3.run_input_script(refusing, allow_skipped=True, label="refused on purpose")
            check("allow_skipped captures expect.skipped",
                  rec3.steps[-1]["expect"]["skipped"] and
                  rec3.steps[-1]["expect"]["skipped"][0]["code"] == "bad_args", str(rec3.steps[-1]))
            rec3.client = c
            rec3.step_recording(4, [{"provider": "hud"}], chunk=3, label="chunked")
            check("step_recording(chunk=) records the runner's sibling chunk",
                  rec3.steps[-1]["chunk"] == 3 and "chunk" not in rec3.steps[-1]["args"]
                  and rec3.steps[-1]["expect"] == {"recorded": []}, str(rec3.steps[-1]))
            rec3.await_state("hud", "eq", 18, path="lives", timeout_ticks=5,
                             record_signals=[{"provider": "hud"}])
            check("await_state(record_signals=) captures held + recorded",
                  rec3.steps[-1]["expect"] == {"held": True, "recorded": []}, str(rec3.steps[-1]))
            keys = rec3.snapshot_keys("engine", label_prefix="shape")
            check("snapshot_keys emits one exists assert per key",
                  set(keys) >= {"paused", "physics_frame"} and all(
                      s["args"]["op"] == "exists" for s in rec3.steps[-len(keys):]), str(keys))
            # (step 0, the allowed refusal, needs the mock hint the Recorder never records)
            replay3 = run_script(c, {"steps": rec3.steps[1:]})
            check("the recorded additions replay green", replay3["passed"] is True,
                  str(replay3.get("failed_step")))
            try:
                Recorder(c).compile({"id": "x", "env": {"DEMO_MAPP": "x"}})
                env_rejected = False
            except ValueError as e:
                env_rejected = "DEMO_MAPP" in str(e)
            check("compile() rejects a non-allow-listed meta.env key", env_rejected)

            # save() only cross-checks meta.mode against a live `game` provider; the mock has
            # none, so the round trip still writes.
            saved_dir = tempfile.mkdtemp(prefix="vgcp-selftest-")
            saved_path = os.path.join(saved_dir, "roundtrip.vgcp.json")
            try:
                rec2 = Recorder(c)
                rec2.snapshot("hud", ["lives"], label_prefix="hud")
                # `stamp=False`: this round trip is about record -> save -> read back; save()'s
                # stamping is covered above, against a fake git and a throwaway repository.
                saved = rec2.save(saved_path, {"id": "rt-save", "title": "save",
                                               "instructions": "x.md"}, stamp=False)
                check("save() round-trips with no game provider",
                      os.path.exists(saved_path) and saved["meta"]["mode"] == "play")
            finally:
                shutil.rmtree(saved_dir, ignore_errors=True)
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=2)
        except subprocess.TimeoutExpired:
            mock.kill()


if __name__ == "__main__":
    raise SystemExit(main())
