import { describe, expect, it, vi } from "vitest";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { formatSearchResponse, registerCodeSearchTools } from "../src/tools/code-search.js";
import { VERSION, applySchemaStripIntercept, createServer } from "../src/index.js";
import type { CodeSearchClient } from "../src/client.js";
import { normalizeUrl } from "../src/config.js";
import type { SearchResponse } from "../src/types.js";
import pkg from "../package.json" with { type: "json" };

interface CapturedTool {
  name: string;
  handler: (args: Record<string, unknown>) => Promise<{
    content: { type: string; text: string }[];
    isError?: boolean;
  }>;
}

function makeFakeServer(): { server: unknown; tools: Map<string, CapturedTool> } {
  const tools = new Map<string, CapturedTool>();
  const server = {
    tool: (
      name: string,
      _description: string,
      _schema: unknown,
      handler: CapturedTool["handler"],
    ) => {
      tools.set(name, { name, handler });
    },
  };
  return { server, tools };
}

describe("code-search tool handlers", () => {
  it("registers only read-only tools", () => {
    const client = {} as CodeSearchClient;
    const { server, tools } = makeFakeServer();

    registerCodeSearchTools(server as never, client);

    expect([...tools.keys()].sort()).toEqual([
      "code_search_stats",
      "health",
      "list_projects",
      "search_code",
    ]);
    expect(tools.has("index")).toBe(false);
    expect(tools.has("backfill_summaries")).toBe(false);
  });

  it("passes search arguments through with safe defaults", async () => {
    const client = {
      search: vi.fn().mockResolvedValue({
        results: [],
        total_matches: 0,
        mode: "hybrid",
      }),
    } as unknown as CodeSearchClient;
    const { server, tools } = makeFakeServer();
    registerCodeSearchTools(server as never, client);

    const result = await tools.get("search_code")!.handler({
      query: "auth middleware",
      project: "api",
    });

    expect(result.isError).toBeUndefined();
    expect(client.search).toHaveBeenCalledWith({
      query: "auth middleware",
      mode: "hybrid",
      project: "api",
      limit: 10,
      min_score: 0.3,
    });
    expect(JSON.parse(result.content[0].text)).toEqual({
      results: [],
      total_matches: 0,
      mode: "hybrid",
    });
  });

  it("combines detailed and summary stats", async () => {
    const client = {
      stats: vi.fn().mockResolvedValue({
        total_chunks: 12,
        by_type: { block: 12 },
        by_project: [],
      }),
      summaryStats: vi.fn().mockResolvedValue({
        total_chunks: 12,
        summarized: 8,
        pending: 4,
        by_model: { "qwen3-coder-next:cloud": 8 },
      }),
    } as unknown as CodeSearchClient;
    const { server, tools } = makeFakeServer();
    registerCodeSearchTools(server as never, client);

    const result = await tools.get("code_search_stats")!.handler({});

    expect(result.isError).toBeUndefined();
    expect(JSON.parse(result.content[0].text)).toEqual({
      stats: {
        total_chunks: 12,
        by_type: { block: 12 },
        by_project: [],
      },
      summary_stats: {
        total_chunks: 12,
        summarized: 8,
        pending: 4,
        by_model: { "qwen3-coder-next:cloud": 8 },
      },
    });
  });

  it("surfaces client errors as MCP errors", async () => {
    const client = {
      health: vi.fn().mockRejectedValue(new Error("connection refused")),
    } as unknown as CodeSearchClient;
    const { server, tools } = makeFakeServer();
    registerCodeSearchTools(server as never, client);

    const result = await tools.get("health")!.handler({});

    expect(result.isError).toBe(true);
    expect(result.content[0].text).toContain("connection refused");
  });

  it("formats grouped search results by file", () => {
    const response: SearchResponse = {
      total_matches: 3,
      mode: "hybrid",
      results: [
        makeResult({ file_path: "src/a.ts", chunk_index: 1, score: 0.9 }),
        makeResult({ file_path: "src/b.ts", chunk_index: 0, score: 0.8 }),
        makeResult({ file_path: "src/a.ts", chunk_index: 2, score: 0.7 }),
      ],
    };

    expect(
      formatSearchResponse(response, {
        format: "by_file",
        includeContent: false,
        maxContentChars: 20,
      }),
    ).toEqual({
      total_matches: 3,
      mode: "hybrid",
      files: [
        {
          file_path: "src/a.ts",
          project: "proj",
          best_score: 0.9,
          matches: [
            {
              file_path: "src/a.ts",
              project: "proj",
              chunk_index: 1,
              chunk_type: "function",
              score: 0.9,
              code_score: 0.6,
              summary_score: 0.95,
              summary: "summary",
            },
            {
              file_path: "src/a.ts",
              project: "proj",
              chunk_index: 2,
              chunk_type: "function",
              score: 0.7,
              code_score: 0.6,
              summary_score: 0.95,
              summary: "summary",
            },
          ],
        },
        {
          file_path: "src/b.ts",
          project: "proj",
          best_score: 0.8,
          matches: [
            {
              file_path: "src/b.ts",
              project: "proj",
              chunk_index: 0,
              chunk_type: "function",
              score: 0.8,
              code_score: 0.6,
              summary_score: 0.95,
              summary: "summary",
            },
          ],
        },
      ],
    });
  });

  it("validates API URLs before the MCP server starts", () => {
    expect(normalizeUrl("http://localhost:5204/")).toBe("http://localhost:5204");
    expect(() => normalizeUrl("file:///tmp/code-search.sock")).toThrow("http or https");
    expect(() => normalizeUrl("not a url")).toThrow("Invalid CODE_SEARCH_API_URL");
  });

  it("hints at the applied min_score when compact output is empty", () => {
    const response: SearchResponse = { total_matches: 0, mode: "hybrid", results: [] };

    expect(
      formatSearchResponse(response, {
        format: "compact",
        includeContent: false,
        maxContentChars: 20,
        minScore: 0.3,
      }),
    ).toEqual({
      total_matches: 0,
      mode: "hybrid",
      applied_min_score: 0.3,
      note: "No results at or above min_score 0.3. Lower min_score to widen the search.",
      results: [],
    });
  });

  it("hints at the applied min_score when by_file output is empty", () => {
    const response: SearchResponse = { total_matches: 0, mode: "hybrid", results: [] };

    expect(
      formatSearchResponse(response, {
        format: "by_file",
        includeContent: false,
        maxContentChars: 20,
        minScore: 0.5,
      }),
    ).toEqual({
      total_matches: 0,
      mode: "hybrid",
      applied_min_score: 0.5,
      note: "No results at or above min_score 0.5. Lower min_score to widen the search.",
      files: [],
    });
  });

  it("omits the empty hint when results are present", () => {
    const response: SearchResponse = {
      total_matches: 1,
      mode: "hybrid",
      results: [makeResult({})],
    };

    const out = formatSearchResponse(response, {
      format: "compact",
      includeContent: false,
      maxContentChars: 20,
      minScore: 0.3,
    }) as Record<string, unknown>;

    expect(out.applied_min_score).toBeUndefined();
    expect(out.note).toBeUndefined();
  });
});

describe("server version and schema transport", () => {
  it("derives VERSION from package.json", () => {
    expect(VERSION).toBe(pkg.version);
  });

  it("strips the draft-07 $schema from every tool's inputSchema on the wire", async () => {
    const server = createServer();
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    applySchemaStripIntercept(serverTransport);

    const client = new Client({ name: "schema-strip-test", version: "0.0.0" });
    await server.connect(serverTransport);
    await client.connect(clientTransport);

    try {
      const { tools } = await client.listTools();
      expect(tools.length).toBeGreaterThan(0);
      for (const tool of tools) {
        expect(tool.inputSchema).toBeDefined();
        expect("$schema" in (tool.inputSchema as Record<string, unknown>)).toBe(false);
        if (tool.outputSchema) {
          expect("$schema" in (tool.outputSchema as Record<string, unknown>)).toBe(false);
        }
      }
    } finally {
      await client.close();
      await server.close();
    }
  });
});

function makeResult(overrides: Partial<SearchResponse["results"][number]> = {}) {
  return {
    score: 0.9,
    code_score: 0.6,
    summary_score: 0.95,
    file_path: "src/a.ts",
    project: "proj",
    chunk_index: 0,
    chunk_type: "function",
    summary: "summary",
    content: "const value = 'long content';",
    ...overrides,
  };
}
