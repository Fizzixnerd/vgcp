# vgcp-test: record and replay game tests

The test system for the Video Game Control Protocol (VGCP). A test is a **VGCP script**: a short,
linear list of VGCP commands recorded from a real game. The harness replays every script against a
freshly launched game, with no agent or person in the loop, and prints a **triage packet** for
each script that fails, so that the few failures are all anyone has to look at.

You do not write scripts by hand. You perform the test once through a `Recorder`, which sends each
command to the running game and writes down the reply, then compiles the session into a script.

## Requirements

- Linux: the harness and the launcher use `setsid`, `ss` and `/proc`.
- `python3` (standard library only) and `git`. Tests run from a git checkout of the game.
- A game built with `vgcp-server` (`cargo build --features vgcp`) and imported once
  ([`../vgcp-server/INTEGRATION.md`](../vgcp-server/INTEGRATION.md) §1).
- A display the game can render on. The harness runs `../virtual-display/launch_game.sh`, which
  starts an Xvfb display with software Vulkan by default; see
  [`../docs/virtual-display.md`](../docs/virtual-display.md). `xdpyinfo` is optional (it lets the
  window-size check read the X screen).

## Where things are

The harness works from the **game directory**: the repository root, unless `vgcp.json` says
otherwise (below). Its Godot project is the game directory itself when it holds `project.godot`,
else `<game_dir>/godot`. Its scripts are `<game_dir>/tests/vgcp/*.vgcp.json`, each usually with a
Markdown file beside it that says what the test checks. With the Godot project at the root, add an
empty `tests/.gdignore` so Godot does not import the scripts.

| File | What |
|---|---|
| `harness.py` | find every script, launch a fresh game per test, replay it, stamp the passes, triage the failures |
| `runner.py` | replay ONE script against a game that is already running; `run_script` from Python |
| `record.py` | the `Recorder`: perform a test once and compile it into a script; `record.py --rerecord` |
| `provenance.py` | `vgcp.json`, and the `recorded_against` stamp: which commit a recording was made against |
| `selftest.py` | checks all of the above against the mock server, with no Godot |

## Quick start

Run these from the game's repository, with `VGCP` set to your copy of this repository:

```bash
VGCP=path/to/vgcp
python3 $VGCP/vgcp-test/selftest.py          # the tools against the mock server; ends with ALL PASS
python3 $VGCP/vgcp-test/harness.py --list    # the scripts it finds
python3 $VGCP/vgcp-test/harness.py --play    # launch the game, print its VGCP port, hold it until Ctrl-C
```

`--play` serves on this checkout's own port (see [Safe across worktrees](#safe-across-worktrees)),
not on 38787, the drivers' default, so name the port it prints (`VGCP=127.0.0.1:<port>`) when you
drive the game from another shell:

```bash
VGCP_PORT=<port> python3 $VGCP/vgcp-mcp/control.py screenshot
```

For a single interactive game, `harness.py --play --port-base 38787` serves on the default instead.

Record a first test. This one is for a toy game whose `game` state provider reports `score`,
`lives` and `phase`, which emits `run_started` when a seeded run begins, and which accepts the
game actions `move_to {x}` and `jump`. Save it as a file under `tests/vgcp/`, for example
`tests/vgcp/record_jump_scores.py`, and run it from the repository root: `save()` takes a path
relative to the current directory and creates `tests/vgcp/` if it is missing. Anywhere else in the
checkout, the new script would itself be an uncommitted change and make the recording dirty
([below](#recorded_against-which-commit-a-recording-was-made-against)); outside the repository is
fine too.

```python
import sys
sys.path.insert(0, "path/to/vgcp/vgcp-test")
from harness import managed_game
from record import Recorder
from control import VgcpClient              # the harness puts ../vgcp-mcp on the path

with managed_game(None) as game, VgcpClient(port=game.port) as c:
    rec = Recorder(c, meta={"id": "jump-scores", "title": "a jump over the first gap scores",
                            "instructions": "tests/vgcp/jump-scores.md"})
    rec.seed(42)                                               # a fresh, deterministic run
    rec.await_signal("game", "run_started", timeout_ticks=10)  # the seed applies at the next tick
    rec.await_state("game", "eq", "playing", path="phase", timeout_ticks=300)
    rec.snapshot("game", ["score", "lives"], label_prefix="before")
    rec.game_action("move_to", {"x": 320}, label="walk to the gap")
    rec.step(60)
    rec.game_action("jump", label="jump")
    rec.step(90)
    rec.snapshot("game", ["score", "lives"], label_prefix="after")
    rec.save("tests/vgcp/jump-scores.vgcp.json")
```

Then replay it, and the whole suite:

```bash
python3 $VGCP/vgcp-test/harness.py --test jump-scores
python3 $VGCP/vgcp-test/harness.py           # every script; exit 0 only when all pass
```

## What a script is

```json
{
  "vgcp_script": 1,
  "meta": {"id": "jump-scores", "title": "...", "instructions": "tests/vgcp/jump-scores.md",
           "recorded_against": {"commit": "<40-hex>", "dirty": false}},
  "steps": [{"cmd": "seed", "args": {"seed": 42}}, {"cmd": "await_state", "args": {"...": "..."}}]
}
```

Each step is one VGCP command (`cmd`, `args`, an optional `label`). Steps run in order and stop at
the first failure. A replay is deterministic because the game is paused between every command and
a script seeds the run first. A step passes when the reply is `ok` and, for `assert`,
`await_signal` and `await_state`, when `passed`, `fired` or `held` is true. An `expect` object on
the step replaces that condition: every key must equal the reply's (so `{"held": false,
"timed_out": true}` expects a timeout). Two rules hold regardless: a `run_input_script` step whose
reply lists `skipped` events fails unless its `expect` names `skipped`; and a `step` step may carry
`"chunk": k`, which the runner sends as wire steps of at most `k` ticks and merges into one reply.

The `meta` fields:

| Field | Meaning |
|---|---|
| `id`, `title` | the test's name (`harness.py --test <id>`) and a one-line description |
| `scene` | optional: the scene to boot, e.g. `res://main.tscn`; without it, the project's main scene |
| `mode` | the game's boot mode, one of `vgcp.json`'s `mode.values` (default the first); refused when no mode is configured |
| `env` | the scenario: variables from `vgcp.json`'s `scenario_env`, with string or integer values; any other key fails the test before launch |
| `godot_args` | engine arguments after the launcher's `--`, as pairs: `--max-fps N`, `--frame-delay N`, `--resolution WxH`; nothing else is allowed |
| `replays` | launch a fresh game this many times (default 1); every replay must pass, and every step's `recorded` signal list must be identical across replays |
| `replay_godot_args` | one extra argument list per replay (as many as `replays`), validated like `godot_args` and never repeating one of its flags; a replay with `--resolution WxH` is checked to have opened at that size |
| `authored_resolution` | `[w, h]`: before each test the pointer is moved to the centre of this size (default `[1280, 720]`), where a fresh Xvfb has it |
| `instructions` | a Markdown file, relative to the game directory, that says what the test checks |
| `recorded_against` | written by `record.py`: `{"commit", "dirty"}`, below |
| `introduced_in_pr`, `modified_in_prs` | optional provenance; `compile()` fills in `0` and `[]`, and triage points at them when a checkout has no last-green record |
| `bisect` | optional metadata for a bisect tool (below); the harness ignores it |

## The launch environment

A test launch never inherits the shell's scenario. The harness starts from the shell's
environment and drops every `VGCP_*` variable, every variable with the game's `env_prefix` and
every variable `vgcp.json` names, except the four that choose the display lane
(`VGCP_XVFB_DISPLAY`, `VGCP_DISPLAY`, `VGCP_SOFTWARE`, `VGCP_DRIVER_FLAGS`). Everything else
(`PATH`, `HOME`, `TMPDIR`) is kept. It then sets the isolation keys (`VGCP_PORT`, and the game's
`profile_env` when it has one), then `meta.env`, then `meta.mode` as `mode.env`, and points
`VGCP_GODOT_PROJECT` at the game's project. `meta.env` can never set an isolation key or the mode
variable. So a stray `export` in your shell cannot change what a test does, and a game's scenario
(a map, a seed, a difficulty) reaches it only through the variables `scenario_env` lists.

Ad-hoc launches (`harness.py --play`, `managed_game(...)`) inherit the shell, so you can steer
them; record from a clean shell. `managed_game(scene, mode=..., env=..., engine_args=...)` takes
the same scenario keys and engine arguments a script declares.

## vgcp.json: the game's settings

The tools know nothing about your game until one optional file, `vgcp.json`, at the top of the
game's git checkout, tells them. A plain Godot project with its tests in `tests/vgcp/` needs none.
It is read once, at import, without running git: first at the top of the checkout the tools are
in, then at the top of the current directory's checkout. So never put a `vgcp.json` at the root of
a VGCP clone: it would win over the game's. Every key is optional; an unknown key or a wrong type
stops every tool at import, naming the file.

| Key | Meaning | Without it |
|---|---|---|
| `game_dir` | the game directory, relative to the repository root | `"."` |
| `env_prefix` | a test launch also drops every inherited variable with this prefix | only `VGCP_*` and the named keys are dropped |
| `scenario_env` | the variables a script's `meta.env` may set | a non-empty `meta.env` is refused |
| `mode` | `{"env": NAME, "values": [...]}`: `meta.mode` is exported as `NAME`; `harness.py --play --mode` takes the same values | a `meta.mode` is refused, and `save()` skips the live mode check |
| `profile_env` | each launch gets a fresh `<git-dir>/vgcp-test/profile-<port>.json`, named in this variable, so saved progress never leaks between tests | no profile file |
| `record_paths` | git pathspecs whose uncommitted changes make a recording dirty; each must name a real directory, never a symlink | the whole checkout except `<game_dir>/tests/vgcp` |

For example:

```json
{"env_prefix": "MYGAME_", "scenario_env": ["MYGAME_MAP", "MYGAME_SEED"],
 "mode": {"env": "MYGAME_MODE", "values": ["play", "debug"]}}
```

`VGCP_GAME_DIR` overrides `game_dir` for one run, and the Godot project moves with it. When the
game has a `mode` and the running game has a `game` state provider with a `mode` key, `save()`
refuses a recording whose `meta.mode` differs from it.

## Recording

| `Recorder` method | records |
|---|---|
| `seed(value)` | a `seed` step; start here |
| `await_signal(provider, signal, timeout_ticks, record_signals=None)` | a sync point; the fired args become its `expect` |
| `await_state(provider, op, value, path=..., record_signals=None)` | a sync point; prefer it to a fixed `step N` |
| `game_action(action, payload=None)` | an `input` step of type `game_action`, the device-independent action the game consumes |
| `input(**kw)` | a synthetic device event; only to test the input path itself |
| `run_input_script(script, record_signals=None, allow_skipped=False)` | a frame-by-frame input timeline; raises if the game refused events, unless `allow_skipped` |
| `step(ticks)`, `step_recording(ticks, record_signals, chunk=None)` | an exact advance; the second pins the signals that fired |
| `set_timescale(value)` | a `set_timescale` step; it never changes how many ticks a `step` advances |
| `snapshot(provider, paths)`, `snapshot_keys(provider)` | `assert` steps holding the values, or the key names, read from the game now |
| `assert_state(...)`, `assert_absent(...)` | an explicit assert; it is checked live before it is recorded |
| `bug_check(provider, path, op, value)` | the check of a bug script (below) |
| `screenshot(path=None)` | a diagnostic capture; nothing compares pixels on replay |

Record `screenshot()` with no path (the server writes into its shots directory and replies with
the absolute path) or with an absolute path: a relative one is resolved by the game process.

Pass the meta to `Recorder(c, meta=...)` or `rec.preflight(meta)` so a bad field fails before the
first tick, not after the whole drive; `compile()` and `save()` apply the harness's own rules.
`record.py --rerecord <script> [--out PATH] [--port P] [--dry-run] [--allow-dirty] [--pr N]`
replays a script's steps through a `Recorder` and captures every expectation again, keeping the
commands; a step that no longer behaves as recorded stops it, and nothing is written.

## Running

- `harness.py` runs every script; `--test <id>` runs one; `--list` lists the ids; `--stamps`
  reports stale and dirty recordings; `--play [SCENE] [--mode M]` launches the game and holds it
  until you press Ctrl-C or kill the harness pid it prints. `--port-base` and `--display-base`
  choose the first port and display.
- `runner.py <script> [--host H] [--port P]` replays one script against a game that is already
  running, and prints the result as JSON.
- From Python, `harness.managed_game(scene, ...)` launches a game with the harness's teardown
  (the game is killed by process group when the block ends, on an exception, at exit and on
  SIGTERM, SIGINT or SIGHUP), and `runner.run_script(client, script)` replays a script over a
  connected client.

## recorded_against: which commit a recording was made against

`save()` stamps `meta.recorded_against = {"commit": <HEAD>, "dirty": <bool>}`. A recording older
than HEAD is normal: `harness.py --stamps` reports it, and it never fails a test or blocks a merge.
When a script goes red, its commit is where to replay it first, and the known-good endpoint for a
bisect.

Record from a commit: commit the code, then record at a clean HEAD or in a worktree made from the
commit under test (`git worktree add --detach <path> <commit>`). The recording is **dirty** when a
tracked change, or an untracked file that is not ignored, sits under `record_paths`. A `Recorder`
warns when it is given its meta; `save()` still writes the recording but stamps `dirty: true`;
`--rerecord` refuses a dirty tree before it launches anything. If git cannot answer, the tree
counts as dirty. With the default `record_paths` (the whole checkout but
`<game_dir>/tests/vgcp/`), keep a VGCP clone outside the game's repository or ignore it, ignore
`.godot/`, build output and `__pycache__/`, and keep an uncommitted recording script under
`<game_dir>/tests/vgcp/` or outside the repository, or every recording will be dirty.

## Triage

A failing script prints a JSON packet, then where its recording came from:

```
{ "test_id", "title", "instructions",
  "failed_step": {"idx", "label", "cmd", "args"}, "reply", "replay", "replay_divergence",
  "last_verified_commit", "changed_since",
  "recorded_against": {"stamp", "stale", "note"}, "hint" }
[vgcp-test] provenance: this recording was made at 1a2b3c4d5e; replay it there before believing the failure
```

`changed_since` is `git diff --stat` from the last commit where this checkout saw the test pass.
The `hint` reads the failure: an `await_*` that timed out usually means the script lost sync; an
`assert` that failed after its syncs passed means the game behaved differently (a stale test or a
real regression); refused input events, two replays that recorded different signals, and a test
that could not run at all (a game that did not boot, a window of the wrong size, a game that died
mid-script) each get their own.

## Bug scripts

A bug script is a test that fails while a bug is there. Record it at a commit that has the bug:
the setup first, then `bug_check(...)` with the **correct** behaviour as its predicate. It raises
if the predicate already holds (the bug is not on this build), or if its path does not resolve
(unless the op is `exists` or `truthy`). `compile()` sets `meta.bisect.predicate_from` to the index
of the first bug check: every step before it is setup. The script stays red until the bug is
fixed. `meta.bisect` is metadata for a bisect tool, which can replay the script at each commit it
tests; the harness ignores it.

## Safe across worktrees

- Per-checkout state lives, untracked, in `<git-dir>/vgcp-test/`: `last-verified.json` (the last
  green commit per test), `profile-<port>.json`, and an optional `lane.json`.
- The VGCP port and the Xvfb display are derived from the checkout's path (a port base from 38800
  to 38949, a display base from :90 to :97), so harnesses in two worktrees seldom collide. Test *i*
  uses port `base + i` and display `base + i % 8`. `lane.json`, `{"port_base": N,
  "display_base": M}`, pins both bases for every run in that checkout; `--port-base` and
  `--display-base` override them for one run.
