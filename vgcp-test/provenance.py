#!/usr/bin/env python3
"""provenance.py: which commit a test recording was made against, and the per-game config.

Part of the test system for the Video Game Control Protocol (VGCP). A `*.vgcp.json` script is a
recording: its `expect` blocks came from a real game, running real code, at one commit. When a
script later fails, the first question is not which step broke but whether the recording was ever
green on this code. So every script carries a stamp:

    "recorded_against": {
      "commit": "<40-hex>",          # the commit the recording was made against
      "dirty": false                 # were there uncommitted changes that could affect it?
    }

The stamp is where a search for the change that broke the script starts (the good endpoint of a
`git bisect`), not a value to compare against. A stamp older than HEAD is normal: it is reported
(`harness.py --stamps`), never failed, and never blocks a merge.

**Record from a commit.** The commit hash is the only provenance a recording has, so commit the
code first, then record: at a clean HEAD, or in a worktree made from the commit under test.
`record.py --rerecord` refuses a dirty tree before it launches anything. `Recorder.save()` on a
dirty tree still writes the recording, so no session's work is thrown away, but stamps it
`dirty: true` and says so; `harness.py --stamps` lists every such stamp until the script is
re-recorded from a commit.

**What "dirty" means:** a tracked change, or an untracked file that is not ignored, under one of
the pathspecs `affects_recording()` returns. Those are `vgcp.json`'s `record_paths` when it sets
them (typically the game's source, the VGCP server crate compiled into it, and the tools that
launch, drive and record it), else the whole checkout except the recorder's own outputs under
`<GAME_DIR>/tests/vgcp/`. `VGCP_GAME_DIR` moves only that default: `record_paths` are literal. A
change to a document, or to a tool that cannot change what the game does, should not make a
recording dirty, and neither should the recorder's own outputs, so a game lists only the paths
that matter. If git cannot answer, the tree counts as dirty: unknown is not clean. Every git
command here is read-only and runs with `--no-optional-locks`, so it does not even refresh the
index under a commit running in the same checkout.

**The per-game config, `vgcp.json`.** The game's names and paths are not in this code. They come
from one optional file, `vgcp.json`, at the top of a git checkout. It is looked up once, at import,
without running git: first at the top of the checkout these tools are in (a tool that runs them
inside a worktree of an older commit, which may have no `vgcp.json`, still reads the current
settings), then at the top of the current directory's checkout (a VGCP clone or copy beside a game
has none of its own, so it finds the game's). Every key is optional: `game_dir`, `env_prefix`,
`scenario_env`, `mode` (`{"env", "values"}`), `profile_env` and `record_paths`. An unknown key or a
value of the wrong type raises ValueError naming the file. The game directory is always joined to
the root of the checkout the tools run in (the current directory's), never to the directory the
file was found in.
"""

from __future__ import annotations

import glob
import json
import os
import posixpath
import subprocess
import sys
from typing import Any, Callable, Optional

# ---- the per-game config, vgcp.json ----------------------------------------------------------
CONFIG_NAME = "vgcp.json"
_REAL_HERE = os.path.dirname(os.path.realpath(__file__))
_STR_KEYS = ("game_dir", "env_prefix", "profile_env")
_TYPES = {**{k: "a string" for k in _STR_KEYS},
          **{k: "a list of strings" for k in ("scenario_env", "record_paths")},
          "mode": '{"env": "<variable>", "values": ["<mode>", ...]}'}


def _checkout_top(start: str) -> Optional[str]:
    """The first directory at or above `start` that holds a `.git` entry (a directory in a clone, a
    file in a linked worktree or a submodule), or None. Never runs git: under `git bisect run`,
    GIT_DIR is exported, and with it git answers for the wrong directory."""
    d = os.path.realpath(start)
    while True:
        if os.path.lexists(os.path.join(d, ".git")):
            return d
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def find_config(tools_dir: Optional[str] = None, cwd: Optional[str] = None) -> Optional[str]:
    """The `vgcp.json` for this run, or None: the one at the top of the checkout these tools are in,
    else the one at the top of the current directory's checkout."""
    for start in (tools_dir or _REAL_HERE, cwd or os.getcwd()):
        top = _checkout_top(start)
        if top and os.path.isfile(os.path.join(top, CONFIG_NAME)):
            return os.path.join(top, CONFIG_NAME)
    return None


def parse_config(path: str) -> dict:
    """`vgcp.json`, checked: every key optional, none unknown, each of its type. A problem raises
    ValueError naming the file, so a malformed file stops every tool at import."""
    def bad(problem: str) -> ValueError:
        return ValueError(f"vgcp.json at {path}: {problem}")

    def is_strs(v: Any) -> bool:
        return isinstance(v, list) and all(isinstance(x, str) for x in v)

    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise bad(str(e)) from e
    if not isinstance(cfg, dict):
        raise bad(f"must be a JSON object, got {type(cfg).__name__}")
    unknown = sorted(set(cfg) - set(_TYPES))
    if unknown:
        raise bad(f"unknown key(s) {unknown} (known: {', '.join(_TYPES)})")
    for k, v in cfg.items():
        ok = (isinstance(v, str) if k in _STR_KEYS else is_strs(v) if k != "mode" else
              isinstance(v, dict) and set(v) == {"env", "values"} and isinstance(v["env"], str)
              and is_strs(v["values"]) and v["values"] != [])    # values[0] is the default mode
        if not ok:
            raise bad(f"{k} must be {_TYPES[k]}, got {v!r}")
    return cfg


CONFIG_PATH = find_config()
CONFIG: dict = parse_config(CONFIG_PATH) if CONFIG_PATH else {}

# The game's directory, relative to the root of the checkout the tools run in. The ONE place the
# harness, the recorder and this file learn where the game's Godot project and `tests/vgcp/` are:
# `VGCP_GAME_DIR`, else vgcp.json's `game_dir`, else the checkout's root.
GAME_DIR = os.environ.get("VGCP_GAME_DIR") or CONFIG.get("game_dir") or "."


def affects_recording() -> list[str]:
    """The git pathspecs whose uncommitted changes can change what a recording records, read at call
    time: vgcp.json's `record_paths`, else the whole checkout except the recorder's own outputs
    (`<GAME_DIR>/tests/vgcp`). Git does not look through a symlink, so a pathspec must name a REAL
    directory, never a link to one."""
    if CONFIG.get("record_paths"):
        return list(CONFIG["record_paths"])
    return [".", ":(exclude)" + posixpath.normpath(posixpath.join(GAME_DIR, "tests", "vgcp"))]


RULE = ("record from a commit: commit the code first, then record, at a clean HEAD or in a "
        "worktree made from the commit under test (git worktree add --detach <path> <commit>)")


class GitError(RuntimeError):
    """A read-only git command failed (or git is not there at all)."""


def git(args: list[str], cwd: Optional[str] = None) -> str:
    """Run a READ-ONLY git command and return its stdout. Never writes: no objects, no refs, and,
    with `--no-optional-locks`, no index either (a plain `git status` refreshes the index under
    `index.lock`, which can make a commit running in the same checkout fail)."""
    try:
        # `surrogateescape`: `status -z` prints path names as raw bytes, and a path need not be valid
        # UTF-8. They round-trip through str unchanged instead of raising half-way through a save.
        p = subprocess.run(["git", "--no-optional-locks", *args], cwd=cwd, capture_output=True,
                           text=True, errors="surrogateescape")
    except OSError as e:  # no git on PATH
        raise GitError(str(e)) from e
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args)} -> {p.returncode}: {p.stderr.strip()}")
    return p.stdout


GitRunner = Callable[..., str]


# ---- the repository -----------------------------------------------------------------------
def repo_root(path: str, run: Optional[GitRunner] = None) -> Optional[str]:
    """The work tree containing `path` (a file or a directory), or None when there is none. A path
    whose directory does not exist yet (a first recording's `tests/vgcp/`) belongs to the work tree
    of its nearest existing ancestor."""
    run = run or git
    d = os.path.abspath(path)
    while not os.path.isdir(d) and os.path.dirname(d) != d:
        d = os.path.dirname(d)
    try:
        root = run(["rev-parse", "--show-toplevel"], cwd=d).strip()
    except GitError:
        return None
    return root or None


def head_commit(root: str, run: Optional[GitRunner] = None) -> Optional[str]:
    """HEAD's full sha, or None in a repository without one (an unborn branch)."""
    run = run or git
    try:
        sha = run(["rev-parse", "HEAD"], cwd=root).strip()
    except GitError:
        return None
    return sha or None


# ---- is the tree dirty? --------------------------------------------------------------------
def dirty_paths(root: str, run: Optional[GitRunner] = None) -> list[str]:
    """The uncommitted paths that could affect a recording: tracked changes (staged or not) and
    untracked, unignored files under `affects_recording()`. Empty means the commit alone says what
    the recording was made against. One read-only `git status`; a pathspec that matches nothing (a
    checkout without one of the configured directories) is not an error."""
    run = run or git
    out = run(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *affects_recording()],
              cwd=root)
    paths, skip = [], False
    for entry in out.split("\0"):
        if skip:                      # the second path of a rename/copy entry: "R  new\0old"
            skip = False
            continue
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        skip = entry[0] in "RC" or entry[1] in "RC"   # staged (X) or worktree (Y) rename/copy
    return paths


# ---- making a stamp -----------------------------------------------------------------------
def make_stamp(script_path: str, test_id: str, *, run: Optional[GitRunner] = None,
               quiet: bool = False) -> Optional[dict]:
    """The stamp for a recording being written to `script_path` right now: HEAD, and whether
    anything that could affect the recording was uncommitted. None when there is no repository to
    stamp against: the recording is still written, it just cannot say what it was made against."""
    run = run or git
    root = repo_root(script_path, run=run)
    commit = head_commit(root, run=run) if root else None
    if not commit:
        if not quiet:
            print(f"[provenance] note: {os.path.basename(script_path)} has no recorded_against "
                  "stamp (no git checkout here)", file=sys.stderr)
        return None
    try:
        dirty = dirty_paths(root, run=run)
    except GitError as e:
        # Unknown is not clean, and a recording is never lost to a git hiccup: stamp it dirty.
        dirty = [f"(git status failed: {e})"]
    if dirty and not quiet:
        shown = ", ".join(dirty[:5]) + (f", … ({len(dirty)} paths)" if len(dirty) > 5 else "")
        print(f"[provenance] WARNING {test_id}: recorded against {commit[:10]} plus UNCOMMITTED "
              f"changes ({shown}). The stamp says dirty: true, and the commit no longer says what "
              f"this recording was made against. {RULE}; then re-record "
              f"(record.py --rerecord <script>).", file=sys.stderr)
    return {"commit": commit, "dirty": bool(dirty)}


def with_stamp(meta: dict, stamp: Optional[dict]) -> dict:
    """`meta` with `recorded_against` set, placed right after the PR fields it belongs with, so the
    key order of a backfilled script and of a freshly recorded one match. No stamp: unchanged."""
    if not stamp:
        return dict(meta)
    keys = [k for k in meta if k != "recorded_against"]
    anchor = "modified_in_prs" if "modified_in_prs" in keys else (keys[-1] if keys else None)
    out: dict[str, Any] = {}
    for k in keys:
        out[k] = meta[k]
        if k == anchor:
            out["recorded_against"] = stamp
    if anchor is None:
        out["recorded_against"] = stamp
    return out


def stamp_meta(meta: dict, script_path: str, *, run: Optional[GitRunner] = None,
               quiet: bool = False) -> dict:
    """Return `meta` with `recorded_against` set for a recording written to `script_path`."""
    stamp = make_stamp(script_path, str(meta.get("id") or "recording"), run=run, quiet=quiet)
    return with_stamp(meta, stamp)


# ---- reading a stamp back (triage) ---------------------------------------------------------
def triage_note(script_path: str, meta: dict, head: Optional[str] = None,
                run: Optional[GitRunner] = None) -> dict:
    """What the harness reports about a RED script's provenance. Never a verdict: a stale stamp is
    information, never a failure."""
    run = run or git
    stamp = (meta or {}).get("recorded_against") or {}
    commit = stamp.get("commit")
    if not commit:
        return {"stamp": None,
                "note": ("this recording carries no recorded_against stamp, so the commit it was "
                         "green at is unknown; meta.introduced_in_pr / meta.modified_in_prs are the "
                         "only provenance it has")}
    if head is None:
        root = repo_root(script_path, run=run)
        head = head_commit(root, run=run) if root else None
    out: dict[str, Any] = {
        "stamp": stamp,
        "stale": bool(head and head != commit),
        "note": (f"this recording was made at {commit[:10]}"
                 + (" on a DIRTY tree" if stamp.get("dirty") else "")
                 + "; replay it there before believing the failure"),
    }
    if head == commit:
        out["note"] = (f"this recording was made at HEAD ({commit[:10]})"
                       + (" on a DIRTY tree" if stamp.get("dirty") else "")
                       + "; the failure is in the working tree, not in history")
    if stamp.get("dirty"):
        out["dirty"] = ("this recording was made with uncommitted changes that were never kept, so "
                        f"{commit[:10]} is only approximately the code it was green on; if it "
                        "replays green anywhere, re-record it from a commit")
    return out


# ---- reporting (harness.py --stamps) -------------------------------------------------------
def _scripts(root: str) -> list[str]:
    return sorted(glob.glob(os.path.join(root, GAME_DIR, "tests", "vgcp", "*.vgcp.json")))


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def report_stamps(root: str) -> int:
    head = head_commit(root)
    stale = fresh = unstamped = dirty = 0
    for path in _scripts(root):
        meta = _load(path).get("meta", {})
        st = meta.get("recorded_against") or {}
        if not st.get("commit"):
            unstamped += 1
            print(f"  {str(meta.get('id')):52s} (no stamp)")
            continue
        if st.get("dirty"):
            dirty += 1
        if st["commit"] == head:
            fresh += 1
            if st.get("dirty"):
                print(f"  {str(meta.get('id')):52s} recorded at HEAD  [DIRTY tree]")
        else:
            stale += 1
            print(f"  {str(meta.get('id')):52s} recorded at {st['commit'][:10]}"
                  + ("  [DIRTY tree]" if st.get("dirty") else ""))
    print(f"\n[provenance] HEAD {head[:10] if head else '?'}: {fresh} recorded at HEAD, {stale} "
          f"older, {unstamped} unstamped, {dirty} made on a dirty tree.")
    print("[provenance] A stale recording is REPORTED, never failed: its commit is where a bisect "
          "starts, not a comparison. Nothing here blocks a merge.")
    if dirty:
        print(f"[provenance] {dirty} recording(s) made on a dirty tree: their commit is only "
              f"approximately what they were made against. {RULE}; then "
              "record.py --rerecord <script>.")
    return 0
