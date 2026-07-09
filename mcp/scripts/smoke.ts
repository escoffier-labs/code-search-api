import path from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const entry = path.join(rootDir, "dist", "index.js");
const query = process.env.CODE_SEARCH_SMOKE_QUERY || "FastAPI semantic search endpoint";

const transport = new StdioClientTransport({
  command: "node",
  args: [entry],
  env: {
    CODE_SEARCH_API_URL: process.env.CODE_SEARCH_API_URL || "http://localhost:5204",
    CODE_SEARCH_API_KEY: process.env.CODE_SEARCH_API_KEY || "",
  },
});

const client = new Client({ name: "code-search-mcp-smoke", version: "0.0.0" });

try {
  await client.connect(transport);
  const tools = await client.listTools();
  const toolNames = tools.tools.map((tool) => tool.name).sort();
  const requiredTools = ["code_search_stats", "health", "list_projects", "search_code"];
  for (const tool of requiredTools) {
    if (!toolNames.includes(tool)) {
      throw new Error(`Missing tool: ${tool}`);
    }
  }

  const health = await client.callTool({ name: "health", arguments: {} });
  const healthPayload = parseToolPayload(health);
  if (healthPayload.status !== "ok") {
    throw new Error(`code-search-api health is not ok: ${JSON.stringify(healthPayload)}`);
  }

  const projects = await client.callTool({ name: "list_projects", arguments: {} });
  const projectPayload = parseToolPayload(projects);
  if (!Array.isArray(projectPayload.projects)) {
    throw new Error("list_projects did not return a projects array");
  }

  const search = await client.callTool({
    name: "search_code",
    arguments: {
      query,
      mode: "hybrid",
      limit: 3,
      response_format: "compact",
      max_content_chars: 120,
    },
  });
  const searchPayload = parseToolPayload(search);
  if (!Array.isArray(searchPayload.results) || searchPayload.results.length === 0) {
    throw new Error("search_code returned no results");
  }

  console.log(
    JSON.stringify(
      {
        ok: true,
        tools: toolNames,
        health: {
          status: healthPayload.status,
          version: healthPayload.version,
          chunks: healthPayload.chunks,
        },
        projects: projectPayload.projects.length,
        first_result: {
          file_path: searchPayload.results[0].file_path,
          project: searchPayload.results[0].project,
          score: searchPayload.results[0].score,
        },
      },
      null,
      2,
    ),
  );
} finally {
  await client.close();
}

function parseToolPayload(result: unknown): Record<string, any> {
  const content = (result as { content?: Array<{ type: string; text?: string }> }).content;
  const text = content?.find((item) => item.type === "text")?.text;
  if (!text) throw new Error("Tool returned no text content");
  return JSON.parse(text) as Record<string, any>;
}
