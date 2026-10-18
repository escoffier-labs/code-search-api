import { randomUUID } from "node:crypto";
import { createServer, request } from "node:http";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { createHttpServer, type HttpServerHandle } from "../src/http.js";

const TOKEN = randomUUID();
const env = {
  CODE_SEARCH_API_URL: "http://localhost:1",
  CODE_SEARCH_API_KEY: "test-upstream-secret",
  CODE_SEARCH_MCP_HTTP_TOKEN: TOKEN,
  CODE_SEARCH_MCP_HTTP_PORT: "0",
  CODE_SEARCH_MCP_HTTP_HOST: undefined,
};
let http: HttpServerHandle | undefined;
const connections: { client: Client; transport: StreamableHTTPClientTransport }[] = [];
afterEach(async () => {
  for (const { client, transport } of connections.splice(0)) {
    await transport.terminateSession().catch(() => undefined);
    await client.close();
  }
  await http?.close();
  http = undefined;
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function configure(extra = {}) {
  for (const [name, value] of Object.entries({ ...env, ...extra })) vi.stubEnv(name, value);
}
async function start(extra = {}) {
  configure(extra);
  http = await createHttpServer();
  return `http://localhost:${http.port}`;
}
async function connect(url: string) {
  const transport = new StreamableHTTPClientTransport(new URL(`${url}/mcp`), {
    requestInit: { headers: { Authorization: `Bearer ${TOKEN}` } },
  });
  const client = new Client({ name: "http-test", version: "1" });
  connections.push({ client, transport });
  await client.connect(transport);
  return { client, transport };
}

describe("authenticated Streamable HTTP", () => {
  it.each([undefined, "Bearer wrong-token"])("rejects invalid authentication: %s", async (authorization) => {
    const url = await start();
    const response = await fetch(`${url}/mcp`, { method: "POST", headers: authorization ? { authorization } : {} });
    expect(response.status).toBe(401);
    expect(response.headers.get("www-authenticate")).toBe("Bearer");
    expect(await response.text()).toBe("Unauthorized");
  });

  it("exposes only liveness without authentication", async () => {
    const response = await fetch(`${await start()}/healthz`);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "ok" });
  });

  it("lists tools using the SDK client and terminates its session", async () => {
    const url = await start();
    const { client, transport } = await connect(url);
    const result = await client.listTools();
    expect(result.tools).toHaveLength(4);
    expect(result.tools.some((t) => t.name === "search_code")).toBe(true);
    const id = transport.sessionId;
    expect(id).toBeTruthy();
    await transport.terminateSession();
    const stale = await fetch(`${url}/mcp`, {
      headers: { authorization: `Bearer ${TOKEN}`, "mcp-session-id": id!, accept: "text/event-stream" },
    });
    expect(stale.status).toBe(404);
  });

  it.each([
    { origin: "https://evil.example.com" },
    { origin: "null" },
    { origin: "http://localhost.evil.example.com" },
    { host: "evil.example.com" },
    { host: "localhost@evil.example.com" },
  ])("rejects rebinding headers %j", async (headers) => {
    const url = await start();
    const status = await new Promise<number | undefined>((resolve, reject) => {
      const req = request(`${url}/mcp`, { method: "POST", headers: { ...headers, authorization: `Bearer ${TOKEN}` } }, (res) => {
        res.resume();
        res.on("end", () => resolve(res.statusCode));
      });
      req.on("error", reject);
      req.end();
    });
    expect(status).toBe(403);
  });

  it("accepts a loopback Origin", async () => {
    const url = await start();
    const response = await fetch(`${url}/mcp`, { method: "POST", headers: { origin: url } });
    expect(response.status).toBe(401);
  });

  it.each([undefined, "", "   ", env.CODE_SEARCH_API_KEY,
  ])("refuses missing, blank, or reused credentials", async (token) => {
    configure({ CODE_SEARCH_MCP_HTTP_TOKEN: token });
    await expect(createHttpServer()).rejects.toThrow(/CODE_SEARCH_MCP_HTTP_TOKEN/);
  });

  it("refuses a token shorter than 32 characters", async () => {
    configure({ CODE_SEARCH_MCP_HTTP_TOKEN: "a".repeat(31) });
    await expect(createHttpServer()).rejects.toThrow(/at least 32 characters/);
  });

  it.each(["0.0.0.0", "::", "[::]"])("refuses wildcard bind host %s", async (host) => {
    configure({ CODE_SEARCH_MCP_HTTP_HOST: host });
    await expect(createHttpServer()).rejects.toThrow(/not a wildcard/);
  });

  it("accepts a token with surrounding whitespace from a secrets file", async () => {
    const url = await start({ CODE_SEARCH_MCP_HTTP_TOKEN: `  ${TOKEN}\n` });
    const { client } = await connect(url);
    expect((await client.listTools()).tools).toHaveLength(4);
  });

  it.each(["-1", "65536", "abc", "3000suffix", "1.5"])("refuses invalid port %s", async (port) => {
    configure({ CODE_SEARCH_MCP_HTTP_PORT: port });
    await expect(createHttpServer()).rejects.toThrow(/CODE_SEARCH_MCP_HTTP_PORT/);
  });

  it("limits request bytes before JSON parsing", async () => {
    const url = await start();
    const response = await fetch(`${url}/mcp`, {
      method: "POST", headers: { authorization: `Bearer ${TOKEN}`, "content-type": "application/json" },
      body: "x".repeat(1_048_577),
    });
    expect(response.status).toBe(413);
  });

  it("limits chunked bodies without a Content-Length", async () => {
    const url = await start();
    const status = await new Promise<number | undefined>((resolve, reject) => {
      const req = request(`${url}/mcp`, {
        method: "POST", headers: { authorization: `Bearer ${TOKEN}`, "content-type": "application/json" },
      }, (res) => {
        res.resume();
        res.on("end", () => resolve(res.statusCode));
      });
      req.on("error", reject);
      req.write("x".repeat(600_000));
      req.end("x".repeat(600_000));
    });
    expect(status).toBe(413);
  });

  it("expires idle sessions even with an SSE channel open", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const url = await start();
    const { transport } = await connect(url);
    const id = transport.sessionId!;
    vi.spyOn(Date, "now").mockReturnValue(Date.now() + 31 * 60_000);
    await vi.advanceTimersByTimeAsync(60_000);
    const response = await fetch(`${url}/mcp`, {
      headers: { authorization: `Bearer ${TOKEN}`, "mcp-session-id": id, accept: "text/event-stream" },
    });
    expect(response.status).toBe(404);
  });

  it("keeps another session responsive while stats is slow", async () => {
    let statsStarted!: () => void;
    let releaseStats!: () => void;
    const started = new Promise<void>((resolve) => { statsStarted = resolve; });
    const released = new Promise<void>((resolve) => { releaseStats = resolve; });
    const paths: string[] = [];
    const upstream = createServer((req, res) => {
      paths.push(`${req.method} ${req.url}`);
      if (req.url === "/api/stats") {
        statsStarted();
        void released.then(() => res.end(JSON.stringify({ total_chunks: 0 })));
      } else {
        res.end(JSON.stringify({ status: "ok" }));
      }
    });
    await new Promise<void>((resolve) => upstream.listen(0, "127.0.0.1", resolve));
    const address = upstream.address();
    if (!address || typeof address === "string") throw new Error("No upstream port");
    try {
      const url = await start({ CODE_SEARCH_API_URL: `http://localhost:${address.port}` });
      const [a, b] = await Promise.all([connect(url), connect(url)]);
      expect(a.transport.sessionId).not.toBe(b.transport.sessionId);
      const slow = a.client.callTool({ name: "code_search_stats", arguments: {} });
      await started;
      expect((await b.client.callTool({ name: "health", arguments: {} })).isError).not.toBe(true);
      expect((await b.client.listTools()).tools).toHaveLength(4);
      releaseStats();
      expect((await slow).isError).not.toBe(true);
      await a.transport.terminateSession();
      expect((await b.client.listTools()).tools).toHaveLength(4);
      const unknown = await b.client.callTool({ name: "delete_index", arguments: { url: "https://example.com" } });
      expect(unknown.isError).toBe(true);
      expect(paths.sort()).toEqual(["GET /api/stats", "GET /api/summary-stats", "GET /health"].sort());
    } finally {
      releaseStats();
      upstream.closeAllConnections();
      await new Promise<void>((resolve, reject) => upstream.close((error) => error ? reject(error) : resolve()));
    }
  });

  it("closes active sessions and the listener idempotently", async () => {
    const url = await start();
    await connect(url);
    await Promise.all([http!.close(), http!.close()]);
    await expect(fetch(`${url}/healthz`)).rejects.toThrow();
  });
});
