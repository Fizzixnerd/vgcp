// vgcp-client.ts: TCP/NDJSON client for the Video Game Control Protocol (VGCP v1).
// See ../../docs/vgcp-protocol.md. Mirrors control.py's VgcpClient.

import net from "node:net";

export const PROTOCOL_MAJOR = 1;

/** Default endpoint when neither the constructor options nor VGCP_HOST / VGCP_PORT set one. */
export const DEFAULT_HOST = "127.0.0.1";
export const DEFAULT_PORT = 38787;

export interface VgcpResponse {
  id: number | string | null;
  ok: boolean;
  error?: { code: string; message: string };
  [key: string]: unknown;
}

export class VgcpError extends Error {
  constructor(
    public code: string,
    message: string,
    public response: VgcpResponse | null,
  ) {
    super(`${code}: ${message}`);
    this.name = "VgcpError";
  }
}

interface Pending {
  resolve: (r: VgcpResponse) => void;
  reject: (e: Error) => void;
  timer: NodeJS.Timeout;
}

/** Synchronous-feeling, id-matched VGCP client over a single persistent TCP connection. */
export class VgcpClient {
  private host: string;
  private port: number;
  private defaultTimeoutMs: number;
  private sock: net.Socket | null = null;
  private buffer = "";
  private nextId = 0;
  private pending = new Map<number, Pending>();
  private connectPromise: Promise<void> | null = null;

  constructor(opts: { host?: string; port?: number; timeoutMs?: number } = {}) {
    this.host = opts.host ?? process.env.VGCP_HOST ?? DEFAULT_HOST;
    this.port = opts.port ?? Number(process.env.VGCP_PORT ?? DEFAULT_PORT);
    this.defaultTimeoutMs = opts.timeoutMs ?? 10_000;
  }

  private connect(): Promise<void> {
    if (this.sock && !this.sock.destroyed) return Promise.resolve();
    if (this.connectPromise) return this.connectPromise;

    this.connectPromise = new Promise<void>((resolve, reject) => {
      const sock = net.createConnection({ host: this.host, port: this.port }, () => {
        resolve();
      });
      sock.setEncoding("utf8");
      sock.on("data", (chunk: string) => this.onData(chunk));
      sock.on("error", (err) => {
        this.failAll(err);
        reject(err);
      });
      sock.on("close", () => {
        this.failAll(new Error("connection closed"));
        this.sock = null;
        this.connectPromise = null;
      });
      this.sock = sock;
    });
    return this.connectPromise;
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    let nl: number;
    // eslint-disable-next-line no-cond-assign
    while ((nl = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, nl).replace(/\r$/, "");
      this.buffer = this.buffer.slice(nl + 1);
      if (line.trim() === "") continue;
      let resp: VgcpResponse;
      try {
        resp = JSON.parse(line) as VgcpResponse;
      } catch {
        continue; // ignore malformed line
      }
      const id = resp.id;
      if (typeof id === "number" && this.pending.has(id)) {
        const p = this.pending.get(id)!;
        this.pending.delete(id);
        clearTimeout(p.timer);
        p.resolve(resp);
      }
    }
  }

  private failAll(err: Error): void {
    for (const [, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(err);
    }
    this.pending.clear();
  }

  /** Send one command; resolves with the matching-id response (errors surface as ok=false). */
  async request(
    cmd: string,
    args?: Record<string, unknown>,
    opts: { timeoutMs?: number } = {},
  ): Promise<VgcpResponse> {
    await this.connect();
    const id = ++this.nextId;
    const payload: Record<string, unknown> = { id, cmd };
    if (args && Object.keys(args).length > 0) payload.args = args;
    const line = JSON.stringify(payload) + "\n";

    // `step`, `run_input_script`, `await_signal` and `await_state` defer their reply until the
    // ticks/frames elapse, so widen the timeout for them.
    let timeoutMs = opts.timeoutMs ?? this.defaultTimeoutMs;
    if (cmd === "step") {
      const ticks = Number((args?.ticks as number) ?? 1);
      timeoutMs = Math.max(timeoutMs, (ticks / 60) * 4 * 1000 + 5000);
    } else if (cmd === "run_input_script") {
      const frames = Number((args?.max_frames as number) ?? 600);
      timeoutMs = Math.max(timeoutMs, (frames / 60) * 4 * 1000 + 5000);
    } else if (cmd === "await_signal" || cmd === "await_state") {
      const ticks = Number((args?.timeout_ticks as number) ?? 600);
      timeoutMs = Math.max(timeoutMs, (ticks / 60) * 4 * 1000 + 5000);
    }

    return new Promise<VgcpResponse>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new VgcpError("timeout", `no response to '${cmd}' within ${timeoutMs}ms`, null));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.sock!.write(line, (err) => {
        if (err) {
          this.pending.delete(id);
          clearTimeout(timer);
          reject(err);
        }
      });
    });
  }

  /**
   * Liveness + version handshake. The protocol conformance checklist (§9) requires a client to
   * **gate on `protocol_major`**, so this throws `VgcpError("protocol_mismatch")` when the
   * server's major differs from {@link PROTOCOL_MAJOR}. Prefer this over `request("ping")` for
   * the first call; the MCP `game_ping` tool uses it.
   */
  async ping(opts: { timeoutMs?: number } = {}): Promise<VgcpResponse> {
    const resp = await this.request("ping", undefined, opts);
    if (resp.ok && resp.protocol_major !== PROTOCOL_MAJOR) {
      throw new VgcpError(
        "protocol_mismatch",
        `server speaks protocol_major ${String(resp.protocol_major)}, client supports ${PROTOCOL_MAJOR}`,
        resp,
      );
    }
    return resp;
  }

  /** Like request(), but throws VgcpError when the server returns ok=false. */
  async require(cmd: string, args?: Record<string, unknown>): Promise<VgcpResponse> {
    const resp = await this.request(cmd, args);
    if (!resp.ok) {
      const e = resp.error ?? { code: "error", message: "unknown" };
      throw new VgcpError(e.code, e.message, resp);
    }
    return resp;
  }

  close(): void {
    if (this.sock && !this.sock.destroyed) this.sock.end();
    this.sock = null;
    this.connectPromise = null;
  }
}
