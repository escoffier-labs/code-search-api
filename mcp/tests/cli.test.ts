import { describe, expect, it, vi } from "vitest";
import { UsageError, parseArgs, run, type CliDeps } from "../src/cli.js";
import type { CodeSearchClient } from "../src/client.js";

function capture(client: Partial<CodeSearchClient>, serve = vi.fn().mockResolvedValue(undefined)) {
  const out: string[] = [];
  const err: string[] = [];
  const deps: CliDeps = {
    out: (s) => out.push(s),
    err: (s) => err.push(s),
    makeClient: () => client as CodeSearchClient,
    serve,
  };
  return { out, err, deps, serve };
}

describe("parseArgs", () => {
  it("parses a multi-word search query with defaults", () => {
    expect(parseArgs(["search", "auth", "middleware"])).toEqual({
      kind: "search",
      json: false,
      query: "auth middleware",
      mode: "hybrid",
      project: undefined,
      limit: 10,
      minScore: 0.3,
      maxContent: 240,
      includeContent: true,
    });
  });

  it("parses search options", () => {
    expect(
      parseArgs(["search", "x", "--mode", "code", "--limit", "5", "--min-score", "0.5", "--project", "api", "--no-content", "--json"]),
    ).toEqual({
      kind: "search",
      json: true,
      query: "x",
      mode: "code",
      project: "api",
      limit: 5,
      minScore: 0.5,
      maxContent: 240,
      includeContent: false,
    });
  });

  it("routes simple commands and --json", () => {
    expect(parseArgs(["projects", "--json"])).toEqual({ kind: "projects", json: true });
    expect(parseArgs(["stats"])).toEqual({ kind: "stats", json: false });
    expect(parseArgs(["health"])).toEqual({ kind: "health", json: false });
    expect(parseArgs(["mcp"])).toEqual({ kind: "mcp" });
    expect(parseArgs(["--version"])).toEqual({ kind: "version" });
    expect(parseArgs([])).toEqual({ kind: "help" });
  });

  it("rejects bad input with UsageError", () => {
    expect(() => parseArgs(["search"])).toThrow(UsageError);
    expect(() => parseArgs(["bogus"])).toThrow(UsageError);
    expect(() => parseArgs(["search", "x", "--limit", "999"])).toThrow(UsageError);
    expect(() => parseArgs(["search", "x", "--mode", "fuzzy"])).toThrow(UsageError);
    expect(() => parseArgs(["projects", "extra"])).toThrow(UsageError);
  });
});

describe("run", () => {
  it("prints human search output and exits 0", async () => {
    const client = {
      search: vi.fn().mockResolvedValue({
        total_matches: 1,
        mode: "hybrid",
        results: [
          { score: 0.91, code_score: 0.6, summary_score: 0.95, file_path: "src/a.ts", project: "proj", chunk_index: 0, chunk_type: "function", summary: "does a thing", content: "const a = 1;" },
        ],
      }),
    };
    const { out, deps } = capture(client);
    const code = await run(["search", "a", "thing"], deps);
    expect(code).toBe(0);
    expect(client.search).toHaveBeenCalledWith({ query: "a thing", mode: "hybrid", project: undefined, limit: 10, min_score: 0.3 });
    const text = out.join("\n");
    expect(text).toContain("proj/src/a.ts");
    expect(text).toContain("does a thing");
  });

  it("emits raw JSON with --json", async () => {
    const resp = { total_matches: 0, mode: "hybrid", results: [] };
    const client = { search: vi.fn().mockResolvedValue(resp) };
    const { out, deps } = capture(client);
    const code = await run(["search", "x", "--json"], deps);
    expect(code).toBe(0);
    expect(JSON.parse(out.join("\n"))).toEqual(resp);
  });

  it("returns exit 1 when health is not ok", async () => {
    const client = { health: vi.fn().mockResolvedValue({ status: "degraded", version: "1.0.0", error: "indexing" }) };
    const { out, deps } = capture(client);
    expect(await run(["health"], deps)).toBe(1);
    expect(out.join("\n")).toContain("degraded");
  });

  it("returns exit 1 and prints the error on client failure", async () => {
    const client = { listProjects: vi.fn().mockRejectedValue(new Error("connection refused")) };
    const { err, deps } = capture(client);
    expect(await run(["projects"], deps)).toBe(1);
    expect(err.join("\n")).toContain("connection refused");
  });

  it("returns exit 2 and prints help on usage error", async () => {
    const { err, deps } = capture({});
    expect(await run(["bogus"], deps)).toBe(2);
    expect(err.join("\n")).toContain("Usage:");
  });

  it("delegates `mcp` to serve()", async () => {
    const { deps, serve } = capture({});
    expect(await run(["mcp"], deps)).toBe(0);
    expect(serve).toHaveBeenCalledOnce();
  });
});
