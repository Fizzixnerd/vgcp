// mcp_selftest.mjs: end-to-end test of the TypeScript MCP server for the Video Game Control
// Protocol (VGCP) against the Python mock.
//
// Flow (NO Godot):
//   mock_server.py  <--TCP/NDJSON-->  dist/index.js (MCP server)  <--stdio MCP-->  this client
//
// Validates that each MCP tool round-trips the VGCP wire protocol correctly. Temporary files go
// under os.tmpdir() (honours TMPDIR) and are removed at the end.
// Run:  npm run build && node test/mcp_selftest.mjs   (or via ../selftest.sh)

import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const mockPath = path.join(root, "mock_server.py");
const serverPath = path.join(root, "dist", "index.js");

let failures = 0;
function check(name, cond, detail = "") {
  const ok = !!cond;
  if (!ok) failures++;
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  -- " + detail : ""}`);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function waitForPort(port, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tryOnce = () => {
      const s = net.createConnection({ host: "127.0.0.1", port }, () => {
        s.end();
        resolve();
      });
      s.on("error", () => {
        s.destroy();
        if (Date.now() > deadline) reject(new Error(`mock not up on :${port}`));
        else setTimeout(tryOnce, 100);
      });
    };
    tryOnce();
  });
}

/** Parse the JSON VgcpResponse out of an MCP tool result's text content. */
function parseResult(res) {
  const text = (res.content ?? []).find((c) => c.type === "text")?.text ?? "{}";
  return JSON.parse(text);
}

async function main() {
  const port = await freePort();
  console.log(`[mcp-selftest] mock port ${port}`);

  const mock = spawn("python3", [mockPath, "--port", String(port), "--quiet"], {
    stdio: ["ignore", "inherit", "inherit"],
  });
  mock.on("error", (e) => {
    console.error("failed to spawn mock:", e);
    process.exit(2);
  });
  await waitForPort(port);

  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [serverPath],
    env: { ...process.env, VGCP_HOST: "127.0.0.1", VGCP_PORT: String(port) },
    stderr: "inherit",
  });
  const client = new Client({ name: "mcp-selftest", version: "1.0.0" });
  await client.connect(transport);
  // Explicit-path screenshots go to a scratch directory of their own under the temp root.
  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), "vgcp-mcp-selftest-"));

  try {
    // tools/list
    const tools = await client.listTools();
    const names = tools.tools.map((t) => t.name).sort();
    const expected = [
      "game_get_state", "game_input", "game_list_providers", "game_pause",
      "game_ping", "game_resume", "game_screenshot", "game_set_timescale", "game_step",
      "game_load_input_script", "game_run_input_script", "game_clear_input_script",
      "game_input_script_status", "game_await_signal", "game_await_state", "game_assert",
      "game_seed",
    ];
    check("tools/list exposes all 17 tools", expected.every((n) => names.includes(n)),
      names.join(","));

    // game_ping
    const ping = parseResult(await client.callTool({ name: "game_ping", arguments: {} }));
    check("game_ping ok", ping.ok === true);
    check("game_ping protocol_major=1", ping.protocol_major === 1, String(ping.protocol_major));
    check("game_ping paused-by-default", ping.paused === true);

    // game_pause / resume
    check("game_pause", parseResult(await client.callTool({ name: "game_pause", arguments: {} })).paused === true);
    check("game_resume", parseResult(await client.callTool({ name: "game_resume", arguments: {} })).paused === false);

    // game_step advances physics_frame by exactly ticks and re-pauses
    const before = parseResult(await client.callTool({ name: "game_ping", arguments: {} })).physics_frame;
    const step = parseResult(await client.callTool({ name: "game_step", arguments: { ticks: 30 } }));
    check("game_step ok + ticks echoed", step.ok === true && step.ticks === 30);
    check("game_step advanced exactly 30", step.physics_frame === before + 30,
      `${before} -> ${step.physics_frame}`);
    check("game_step re-paused", step.paused === true);

    // game_set_timescale
    const ts = parseResult(await client.callTool({ name: "game_set_timescale", arguments: { value: 3 } }));
    check("game_set_timescale", ts.ok === true && ts.time_scale === 3);

    // game_screenshot writes a real PNG
    const shotPath = path.join(workDir, "vgcp-mcp-selftest.png");
    const shot = parseResult(await client.callTool({
      name: "game_screenshot", arguments: { path: shotPath, downscale: 0.5 },
    }));
    check("game_screenshot ok + path", shot.ok === true && shot.path === shotPath);
    const buf = fs.existsSync(shotPath) ? fs.readFileSync(shotPath) : Buffer.alloc(0);
    const isPng = buf.length > 8 && buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47;
    check("game_screenshot produced a valid PNG file", isPng, `${buf.length} bytes`);

    // game_screenshot return_image -> inline image content present
    const shotImg = await client.callTool({
      name: "game_screenshot", arguments: { path: shotPath, return_image: true },
    });
    check("game_screenshot return_image embeds image content",
      (shotImg.content ?? []).some((c) => c.type === "image" && c.mimeType === "image/png"));

    // game_input (action + click)
    check("game_input action",
      parseResult(await client.callTool({
        name: "game_input", arguments: { type: "action", action: "jump", pressed: true },
      })).injected === "action");
    check("game_input mouse_button click",
      parseResult(await client.callTool({
        name: "game_input", arguments: { type: "mouse_button", x: 100, y: 200 },
      })).injected === "mouse_button");

    // game_input game_action (VGCP §4.6, 1.5.0): canonical action + typed payload through the
    // MCP boundary (the zod schema declares `payload`, so it is NOT stripped).
    const ga = parseResult(await client.callTool({
      name: "game_input",
      arguments: { type: "game_action", action: "move_to", payload: { x: 300 } },
    }));
    check("game_input game_action injected + action echoed",
      ga.ok === true && ga.injected === "game_action" && ga.action === "move_to",
      JSON.stringify(ga));
    const gaBad = parseResult(await client.callTool({
      name: "game_input", arguments: { type: "game_action" },
    }));
    check("game_input game_action without an action -> bad_args",
      gaBad.ok === false && gaBad.error.code === "bad_args", JSON.stringify(gaBad));

    // game_seed (VGCP §4.14, 1.5.0)
    const sd = parseResult(await client.callTool({ name: "game_seed", arguments: { value: 42 } }));
    check("game_seed ok + seed echoed", sd.ok === true && sd.seed === 42, JSON.stringify(sd));
    const seededHud = parseResult(await client.callTool({
      name: "game_get_state", arguments: { provider: "hud" },
    }));
    check("game_seed is observable in the game state", seededHud.state.seed === 42,
      JSON.stringify(seededHud.state));

    // game_get_state (all + single + unknown)
    const all = parseResult(await client.callTool({ name: "game_get_state", arguments: {} }));
    check("game_get_state all has engine+hud",
      all.ok === true && all.state && all.state.engine && all.state.hud);
    const hud = parseResult(await client.callTool({ name: "game_get_state", arguments: { provider: "hud" } }));
    check("game_get_state provider=hud", hud.ok === true && hud.state.gold === 120);
    const bad = parseResult(await client.callTool({ name: "game_get_state", arguments: { provider: "nope" } }));
    check("game_get_state unknown provider -> error", bad.ok === false && bad.error.code === "unknown_provider");

    // game_list_providers
    const lp = parseResult(await client.callTool({ name: "game_list_providers", arguments: {} }));
    check("game_list_providers", lp.ok === true && lp.providers.includes("engine") && lp.providers.includes("hud"));

    // ---- input scripts (VGCP §4.9) ----
    const sample = {
      version: 1,
      resolution: [1280, 720],
      scale: false,
      frames: [
        { frame: 0, events: [{ type: "mouse_move", x: 640, y: 360 }] },
        { frame: 3, events: [
          { type: "mouse_button", x: 200, y: 300, button: 1, pressed: true },
          { type: "mouse_button", x: 200, y: 300, button: 1, pressed: false },
        ] },
        { frame: 10, events: [{ type: "key", keycode: 32, pressed: true }] },
        { frame: 14, events: [{ type: "action", action: "jump", pressed: true }] },
      ],
    };
    const loaded = parseResult(await client.callTool({
      name: "game_load_input_script", arguments: { script: sample },
    }));
    check("game_load_input_script counts",
      loaded.ok === true && loaded.frames === 4 && loaded.duration_frames === 15 && loaded.events === 5,
      JSON.stringify(loaded));

    const st1 = parseResult(await client.callTool({ name: "game_input_script_status", arguments: {} }));
    check("game_input_script_status loaded", st1.ok === true && st1.loaded === true && st1.total_frames === 15);

    const beforeRun = parseResult(await client.callTool({ name: "game_ping", arguments: {} })).physics_frame;
    const run = parseResult(await client.callTool({ name: "game_run_input_script", arguments: {} }));
    check("game_run_input_script completed",
      run.ok === true && run.completed === true && run.frames_run === 15
      && run.physics_frame === beforeRun + 15 && run.paused === true,
      `${beforeRun} -> ${run.physics_frame} (frames_run=${run.frames_run})`);
    // VGCP 1.5.1: every run_input_script reply carries skipped ([] when every event landed). The
    // refusal cases need the mock's hint args, which the tool's zod schema strips, so they are
    // covered by selftest.py against the same mock.
    check("game_run_input_script skipped is [] (1.5.1)",
      Array.isArray(run.skipped) && run.skipped.length === 0, JSON.stringify(run.skipped));

    const runCap = parseResult(await client.callTool({
      name: "game_run_input_script", arguments: { max_frames: 5 },
    }));
    check("game_run_input_script max_frames cap", runCap.ok === true && runCap.frames_run === 5);

    const badScript = parseResult(await client.callTool({
      name: "game_load_input_script", arguments: { script: { frames: [] } },
    }));
    check("game_load_input_script malformed -> error",
      badScript.ok === false && badScript.error.code === "bad_args");

    const cleared = parseResult(await client.callTool({ name: "game_clear_input_script", arguments: {} }));
    check("game_clear_input_script ok", cleared.ok === true);
    const st2 = parseResult(await client.callTool({ name: "game_input_script_status", arguments: {} }));
    check("game_input_script_status after clear", st2.loaded === false);

    // ---- await_signal (VGCP §4.10) ----
    // The typed MCP tool's zod schema strips unknown keys, so it can't forward the mock's
    // `mock_*` hints; the mock fires after its default 1 tick. (The fire/timeout/structured-arg
    // matrix is exercised in the Python selftest, which sends hints directly.) Here we prove the
    // tool plumbs through: a fired reply with a structured args array, plus the error path.
    const aw = parseResult(await client.callTool({
      name: "game_await_signal",
      arguments: { signal: "game_over", timeout_ticks: 50, provider: "hud" },
    }));
    check("game_await_signal fired (default mock) + re-paused",
      aw.ok === true && aw.fired === true && aw.signal === "game_over" && aw.paused === true,
      JSON.stringify(aw));
    check("game_await_signal args is a structured array", Array.isArray(aw.args),
      JSON.stringify(aw.args));
    const awBad = parseResult(await client.callTool({
      name: "game_await_signal", arguments: { signal: "x", timeout_ticks: 5, provider: "nope" },
    }));
    check("game_await_signal unknown provider -> error",
      awBad.ok === false && awBad.error.code === "unknown_provider");

    // ---- record_signals (VGCP §4.11) ----
    // The typed tool forwards record_signals (it IS in the schema), but strips the mock_recorded
    // hint, so the mock returns an empty recorded array: enough to prove the field plumbs through.
    const recStep = parseResult(await client.callTool({
      name: "game_step",
      arguments: { ticks: 3, record_signals: [{ provider: "hud", signals: ["phase_changed"] }] },
    }));
    check("game_step record_signals -> recorded array present",
      recStep.ok === true && Array.isArray(recStep.recorded), JSON.stringify(recStep.recorded));
    const recBad = parseResult(await client.callTool({
      name: "game_step", arguments: { ticks: 3, record_signals: [{ provider: "engine" }] },
    }));
    check("game_step record_signals unknown provider -> error",
      recBad.ok === false && recBad.error.code === "unknown_provider");

    // ---- assert + await_state (VGCP §4.12 / §4.13) ----
    const asPass = parseResult(await client.callTool({
      name: "game_assert", arguments: { provider: "hud", op: "eq", path: "lives", value: 18 },
    }));
    check("game_assert passed + typed actual",
      asPass.ok === true && asPass.passed === true
      && JSON.stringify(asPass.actual) === JSON.stringify({ type: "int", value: 18 }),
      JSON.stringify(asPass));
    const asFail = parseResult(await client.callTool({
      name: "game_assert", arguments: { provider: "hud", op: "lt", path: "lives", value: 0 },
    }));
    check("game_assert false -> ok:true,passed:false", asFail.ok === true && asFail.passed === false);
    const asBad = parseResult(await client.callTool({
      name: "game_assert", arguments: { provider: "nope", op: "eq", value: 1 },
    }));
    check("game_assert unknown provider -> error",
      asBad.ok === false && asBad.error.code === "unknown_provider");
    const awSt = parseResult(await client.callTool({
      name: "game_await_state", arguments: { provider: "hud", op: "eq", path: "lives", value: 18, timeout_ticks: 50 },
    }));
    check("game_await_state held", awSt.ok === true && awSt.held === true, JSON.stringify(awSt));
  } finally {
    await client.close();
    mock.kill("SIGTERM");
    fs.rmSync(workDir, { recursive: true, force: true });
  }

  console.log(`\n[mcp-selftest] ${failures === 0 ? "ALL PASS" : failures + " FAILURE(S)"}`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error(e);
  process.exit(2);
});
