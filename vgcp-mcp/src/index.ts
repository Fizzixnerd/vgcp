#!/usr/bin/env node
// index.ts: MCP server exposing the Video Game Control Protocol (VGCP v1) as MCP tools.
//
// Tools (17): game_ping, game_pause, game_resume, game_step, game_set_timescale,
//        game_screenshot, game_input, game_seed, game_get_state, game_list_providers,
//        game_load_input_script, game_run_input_script, game_clear_input_script,
//        game_input_script_status, game_await_signal, game_await_state, game_assert.
// Each maps 1:1 to a VGCP command over localhost TCP/NDJSON (see ./vgcp-client.ts and
// ../../docs/vgcp-protocol.md).
//
// The server advertises itself to MCP clients as "vgcp-godot".
//
// Transport to the MCP client: stdio.
// Transport to the game: localhost TCP, env VGCP_HOST / VGCP_PORT (default 127.0.0.1:38787).

import { readFileSync } from "node:fs";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { DEFAULT_HOST, DEFAULT_PORT, VgcpClient, VgcpError, type VgcpResponse } from "./vgcp-client.js";

const client = new VgcpClient();

/** Shared `record_signals` arg (VGCP §4.11): watch specs logged during an unpausing command. */
const recordSignalsSchema = z
  .array(
    z.object({
      node: z.string().optional().describe("NodePath of the emitter, e.g. /root/Main/Player"),
      provider: z.string().optional().describe("registered provider whose object emits"),
      signals: z
        .array(z.string())
        .optional()
        .describe("signal names to log; omit to record ALL of the object's signals"),
    }),
  )
  .optional()
  .describe(
    "log signals emitted during the advance; returned in `recorded` ([{signal,frame,args}])",
  );

type ToolResult = {
  content: Array<
    | { type: "text"; text: string }
    | { type: "image"; data: string; mimeType: string }
  >;
  isError?: boolean;
};

/** Render a VGCP response as an MCP tool result (pretty JSON; ok=false -> isError). */
function asResult(resp: VgcpResponse): ToolResult {
  return {
    content: [{ type: "text", text: JSON.stringify(resp, null, 2) }],
    isError: !resp.ok,
  };
}

function errResult(err: unknown): ToolResult {
  // Preserve the VGCP error code (e.g. "protocol_mismatch", "timeout") when we have one.
  const code = err instanceof VgcpError ? err.code : "client_error";
  const message = err instanceof Error ? err.message : String(err);
  return {
    content: [{ type: "text", text: JSON.stringify({ ok: false, error: { code, message } }) }],
    isError: true,
  };
}

const server = new McpServer(
  { name: "vgcp-godot", version: "1.5.2" },
  { capabilities: { tools: {} } },
);

/** Shared predicate args (VGCP §4.12/§4.13) for `game_assert` / `game_await_state`. */
const predicateOps = ["eq", "ne", "lt", "le", "gt", "ge", "in", "contains", "exists", "truthy"] as const;
const predicateShape = {
  provider: z.string().describe("provider to read: engine, or one the game registers (e.g. hud)"),
  op: z.enum(predicateOps).describe("comparison operator"),
  path: z.string().optional().describe("dotted path into the state, e.g. phase / player.x / items.0.id"),
  value: z.unknown().optional().describe("RHS literal (ignored for exists/truthy)"),
  query: z.unknown().optional().describe("opaque value forwarded to the provider"),
};

server.registerTool(
  "game_ping",
  {
    description:
      "Liveness + protocol-version handshake with the running game. Returns paused " +
      "state, current physics_frame, time_scale, and the VGCP protocol version. Call this first. " +
      "Gates on protocol_major: errors 'protocol_mismatch' if the server major differs.",
    inputSchema: {},
  },
  async (): Promise<ToolResult> => {
    try {
      // client.ping() enforces the §9 protocol_major gate (throws on mismatch).
      return asResult(await client.ping());
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_pause",
  {
    description:
      "Freeze the game (SceneTree.paused = true). Observe the game only while it is paused; " +
      "the game starts paused by default.",
    inputSchema: {},
  },
  async (): Promise<ToolResult> => {
    try {
      return asResult(await client.request("pause"));
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_resume",
  {
    description: "Let the game free-run in real time (SceneTree.paused = false) until the next pause/step.",
    inputSchema: {},
  },
  async (): Promise<ToolResult> => {
    try {
      return asResult(await client.request("resume"));
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_step",
  {
    description:
      "Advance the simulation by exactly N physics ticks, then auto re-pause. This is the " +
      "frame-exact way to move time forward between observations. The reply arrives only after " +
      "the ticks have elapsed.",
    inputSchema: {
      ticks: z.number().int().min(1).default(1).describe("number of physics ticks to advance"),
      mode: z.enum(["budget", "blocking"]).default("budget").describe("v1 supports 'budget'"),
      record_signals: recordSignalsSchema,
    },
  },
  async ({ ticks, mode, record_signals }): Promise<ToolResult> => {
    try {
      const args: Record<string, unknown> = { ticks, mode };
      if (record_signals !== undefined) args.record_signals = record_signals;
      return asResult(await client.request("step", args));
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_set_timescale",
  {
    description:
      "Set Engine.time_scale (fast-forward >1 / slow-mo <1). NOT a freeze; use game_pause to stop.",
    inputSchema: {
      value: z.number().positive().describe("time scale multiplier (> 0)"),
    },
  },
  async ({ value }): Promise<ToolResult> => {
    try {
      return asResult(await client.request("set_timescale", { value }));
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_screenshot",
  {
    description:
      "Capture the (paused) game viewport to a PNG after forcing a real frame draw, and return " +
      "its path + dimensions. Set return_image=true to also embed the PNG inline (costs tokens; " +
      "prefer opening the PNG at the returned path).",
    inputSchema: {
      path: z.string().optional().describe(
        "absolute path to write; omit to let the server name one under VGCP_SHOTS_DIR " +
          "(default <temp>/vgcp_tmp)",
      ),
      downscale: z.number().gt(0).lte(1).optional().describe("scale factor 0<d<=1 to shrink"),
      return_image: z.boolean().optional().describe("also return the PNG as inline image content"),
    },
  },
  async ({ path, downscale, return_image }): Promise<ToolResult> => {
    try {
      const args: Record<string, unknown> = {};
      if (path) args.path = path;
      if (downscale !== undefined) args.downscale = downscale;
      const resp = await client.request("screenshot", args);
      const result = asResult(resp);
      if (return_image && resp.ok && typeof resp.path === "string") {
        try {
          const data = readFileSync(resp.path).toString("base64");
          result.content.push({ type: "image", data, mimeType: "image/png" });
        } catch {
          /* fall through: path is still returned as text */
        }
      }
      return result;
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_input",
  {
    description:
      "Inject one input event. type='game_action' is the CANONICAL, device-agnostic record the " +
      "game actually consumes (name in 'action', typed fields in 'payload', e.g. " +
      "move_to {x:300}). It goes straight to the game's action sink, so unlike the " +
      "synthetic device events it is NOT swallowed while the tree is paused. The device types " +
      "'action' (mapped InputMap action), 'key', 'mouse_button' (omit 'pressed' for a full " +
      "click) and 'mouse_move' exercise the real input pipeline. Inject while paused, then " +
      "game_step so the game reacts.",
    inputSchema: {
      type: z.enum(["action", "key", "mouse_button", "mouse_move", "game_action"]),
      action: z
        .string()
        .optional()
        .describe("action name: an InputMap action (type=action) or a canonical game action " +
          "such as move_to / jump / pause (type=game_action)"),
      payload: z
        .record(z.string(), z.unknown())
        .optional()
        .describe("the game action's typed fields (type=game_action), e.g. {\"x\": 300}"),
      pressed: z.boolean().optional().describe("press(true)/release(false); omit on mouse_button for a click"),
      strength: z.number().min(0).max(1).optional().describe("action strength (type=action)"),
      keycode: z.number().int().optional().describe("Godot Key ordinal (type=key)"),
      physical: z.boolean().optional().describe("use physical keycode (type=key)"),
      x: z.number().optional().describe("root-canvas x (mouse_*; VGCP 1.5.2: the game's own drawing space, mapped to window pixels by the server)"),
      y: z.number().optional().describe("root-canvas y (mouse_*; VGCP 1.5.2: the game's own drawing space, mapped to window pixels by the server)"),
      button: z.number().int().optional().describe("MouseButton ordinal: 1=LEFT,2=RIGHT,3=MIDDLE"),
    },
  },
  async (args): Promise<ToolResult> => {
    try {
      const payload: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(args)) {
        if (v !== undefined) payload[k] = v;
      }
      return asResult(await client.request("input", payload));
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_seed",
  {
    description:
      "Fix the run's RNG seed (VGCP §4.14) so a run is reproducible. The reply is immediate; the " +
      "game applies the seed at its NEXT tick, typically by starting a fresh run, so the usual " +
      "pattern is game_seed, then game_await_signal on the signal the game emits when a run " +
      "starts (e.g. 'run_started'). Errors 'no_seed_target' if the game registered none.",
    inputSchema: {
      value: z.number().int().describe("the integer seed to apply"),
    },
  },
  async ({ value }): Promise<ToolResult> => {
    try {
      return asResult(await client.request("seed", { seed: value }));
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_get_state",
  {
    description:
      "Query registered game state providers as JSON. Omit 'provider' to get all (the built-in " +
      "engine provider plus those the game registers, e.g. hud). 'query' is forwarded to the provider.",
    inputSchema: {
      provider: z.string().optional().describe("a single provider name, e.g. 'hud'"),
      query: z.unknown().optional().describe("opaque value forwarded to the provider"),
    },
  },
  async ({ provider, query }): Promise<ToolResult> => {
    try {
      const args: Record<string, unknown> = {};
      if (provider) args.provider = provider;
      if (query !== undefined) args.query = query;
      return asResult(await client.request("get_state", args));
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_list_providers",
  {
    description: "List the names of all registered state providers (always includes 'engine').",
    inputSchema: {},
  },
  async (): Promise<ToolResult> => {
    try {
      return asResult(await client.request("list_providers"));
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_load_input_script",
  {
    description:
      "Validate + store a frame-by-frame input script for deterministic replay (1 frame = 1 " +
      "physics tick). Provide the script inline ('script' object) OR a file path ('path'; lifts " +
      "the 1 MiB line cap). Returns frame/event counts. See examples/sample-input-script.json.",
    inputSchema: {
      script: z.record(z.string(), z.unknown()).optional().describe("the script object (inline)"),
      path: z.string().optional().describe("path to a script JSON file on disk"),
    },
  },
  async ({ script, path }): Promise<ToolResult> => {
    try {
      const args: Record<string, unknown> = {};
      if (script !== undefined) args.script = script;
      if (path !== undefined) args.path = path;
      return asResult(await client.request("load_input_script", args));
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_run_input_script",
  {
    description:
      "Replay the loaded input script (or an inline 'script'/'path' override), one frame per " +
      "physics tick, then auto re-pause. Like game_step the reply is deferred until playback " +
      "completes ({frames_run, completed:true, skipped}). 'skipped' (VGCP 1.5.1) lists every " +
      "scripted event the server could not inject as {frame, index, code, message}; it is [] " +
      "when every event landed, and a non-empty list means the script silently stopped driving " +
      "part of the game. A game_pause/game_resume mid-playback cancels it (completed:false, " +
      "cancelled:true). 'max_frames' caps the length.",
    inputSchema: {
      script: z.record(z.string(), z.unknown()).optional().describe("inline script override"),
      path: z.string().optional().describe("script file override"),
      max_frames: z.number().int().min(1).optional().describe("cap playback to N frames"),
      record_signals: recordSignalsSchema,
    },
  },
  async ({ script, path, max_frames, record_signals }): Promise<ToolResult> => {
    try {
      const args: Record<string, unknown> = {};
      if (script !== undefined) args.script = script;
      if (path !== undefined) args.path = path;
      if (max_frames !== undefined) args.max_frames = max_frames;
      if (record_signals !== undefined) args.record_signals = record_signals;
      return asResult(await client.request("run_input_script", args));
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_clear_input_script",
  {
    description: "Drop the stored input script (does not affect an in-flight playback).",
    inputSchema: {},
  },
  async (): Promise<ToolResult> => {
    try {
      return asResult(await client.request("clear_input_script"));
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_input_script_status",
  {
    description:
      "Report input-script status: {loaded, playing, current_frame, total_frames}.",
    inputSchema: {},
  },
  async (): Promise<ToolResult> => {
    try {
      return asResult(await client.request("input_script_status"));
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_await_signal",
  {
    description:
      "Advance the game watching for ONE Godot signal, with a REQUIRED tick timeout (the world " +
      "never advances forever). Name the emitter with 'node' (a NodePath like /root/Main/Player) OR " +
      "'provider' (a registered state provider, e.g. 'game'), plus the 'signal' name and " +
      "'timeout_ticks'. Like game_step the reply is DEFERRED: it arrives when the signal fires " +
      "({fired:true, args, waited_ticks}) or the budget elapses ({fired:false, timed_out:true}). " +
      "A game_pause/game_resume mid-await cancels it ({cancelled:true}). The signal's 'args' come " +
      "back as typed, structured descriptors ([{type,value}, ...]), never opaque strings.",
    inputSchema: {
      signal: z.string().describe("signal name on the target object"),
      timeout_ticks: z
        .number()
        .int()
        .min(1)
        .describe("REQUIRED max physics ticks to wait before timing out"),
      node: z.string().optional().describe("NodePath of the emitter, e.g. /root/Main/Player"),
      provider: z
        .string()
        .optional()
        .describe("registered provider whose target object emits the signal"),
      record_signals: recordSignalsSchema,
    },
  },
  async ({ signal, timeout_ticks, node, provider, record_signals }): Promise<ToolResult> => {
    try {
      const args: Record<string, unknown> = { signal, timeout_ticks };
      if (node !== undefined) args.node = node;
      if (provider !== undefined) args.provider = provider;
      if (record_signals !== undefined) args.record_signals = record_signals;
      return asResult(await client.request("await_signal", args));
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_assert",
  {
    description:
      "Check a narrow predicate against a provider's current (paused) state. Reply has `passed` " +
      "(bool) and a typed `actual`. `ok` is true even when the assertion is false (the command ran; " +
      "inspect `passed`). op is one of eq/ne/lt/le/gt/ge/in/contains/exists/truthy; `path` is a " +
      "dotted key into the state (e.g. 'phase', 'player.x', 'items.0.id').",
    inputSchema: predicateShape,
  },
  async ({ provider, op, path, value, query }): Promise<ToolResult> => {
    try {
      const args: Record<string, unknown> = { provider, op };
      if (path !== undefined) args.path = path;
      if (value !== undefined) args.value = value;
      if (query !== undefined) args.query = query;
      return asResult(await client.request("assert", args));
    } catch (e) {
      return errResult(e);
    }
  },
);

server.registerTool(
  "game_await_state",
  {
    description:
      "Advance the game until a narrow predicate over a provider's state holds (`held:true`) or a " +
      "REQUIRED tick timeout elapses (`held:false, timed_out:true`). Deferred reply like " +
      "game_await_signal; cancellable by game_pause/game_resume; supports record_signals. Same " +
      "predicate vocabulary as game_assert.",
    inputSchema: {
      ...predicateShape,
      timeout_ticks: z.number().int().min(1).describe("REQUIRED max physics ticks to wait"),
      record_signals: recordSignalsSchema,
    },
  },
  async ({ provider, op, path, value, query, timeout_ticks, record_signals }): Promise<ToolResult> => {
    try {
      const args: Record<string, unknown> = { provider, op, timeout_ticks };
      if (path !== undefined) args.path = path;
      if (value !== undefined) args.value = value;
      if (query !== undefined) args.query = query;
      if (record_signals !== undefined) args.record_signals = record_signals;
      return asResult(await client.request("await_state", args));
    } catch (e) {
      return errResult(e);
    }
  },
);

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  // MCP servers must not write to stdout (it is the JSON-RPC channel). Log to stderr.
  process.stderr.write(
    `[vgcp-godot] MCP server up; VGCP target ` +
      `${process.env.VGCP_HOST ?? DEFAULT_HOST}:${process.env.VGCP_PORT ?? String(DEFAULT_PORT)}\n`,
  );
}

main().catch((err) => {
  process.stderr.write(`fatal: ${err instanceof Error ? err.stack : String(err)}\n`);
  process.exit(1);
});
