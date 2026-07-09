import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { CodeSearchClient } from "../client.js";
import type { SearchMode, SearchResponse, SearchResult } from "../types.js";
import { fail, ok } from "./_util.js";

type SearchResponseFormat = "raw" | "compact" | "by_file";
type CompactSearchResult = {
  file_path: string;
  project: string;
  chunk_index: number;
  chunk_type: string;
  score: number;
  code_score: number;
  summary_score: number | null;
  summary: string | null;
  content?: string;
};

export function registerCodeSearchTools(server: McpServer, client: CodeSearchClient): void {
  server.tool(
    "search_code",
    "Search the indexed local codebase by developer intent using hybrid semantic search. Read-only.",
    {
      query: z.string().min(1).describe("Natural-language description of the code, behavior, symbol, or workflow to find."),
      mode: z
        .enum(["hybrid", "code", "summary"])
        .default("hybrid")
        .describe("Search mode. hybrid combines code and summary vectors, code searches code embeddings, summary searches summary embeddings."),
      project: z.string().min(1).optional().describe("Optional exact project filter from list_projects."),
      limit: z.number().int().min(1).max(50).default(10).describe("Maximum results to return, 1 to 50."),
      min_score: z.number().min(0).max(1).default(0.3).describe("Minimum similarity score, 0 to 1."),
      response_format: z
        .enum(["raw", "compact", "by_file"])
        .default("raw")
        .describe("Output shape. raw returns the code-search-api response, compact trims each hit, by_file groups hits by file."),
      include_content: z
        .boolean()
        .default(true)
        .describe("Include the content preview in compact and by_file output."),
      max_content_chars: z
        .number()
        .int()
        .min(0)
        .max(500)
        .default(240)
        .describe("Maximum content preview characters in compact and by_file output."),
    },
    async ({
      query,
      mode = "hybrid",
      project,
      limit = 10,
      min_score = 0.3,
      response_format = "raw",
      include_content = true,
      max_content_chars = 240,
    }) => {
      try {
        const response = await client.search({
          query,
          mode: mode as SearchMode,
          project,
          limit,
          min_score,
        });
        return ok(
          formatSearchResponse(response, {
            format: response_format as SearchResponseFormat,
            includeContent: include_content,
            maxContentChars: max_content_chars,
            minScore: min_score,
          }),
        );
      } catch (error) {
        return fail(error);
      }
    },
  );

  server.tool(
    "list_projects",
    "List projects currently present in the code-search-api index with chunk, embedding, and summary counts. Read-only.",
    {},
    async () => {
      try {
        return ok(await client.listProjects());
      } catch (error) {
        return fail(error);
      }
    },
  );

  server.tool(
    "code_search_stats",
    "Return code-search-api index health statistics, including chunk coverage and summary model coverage. Read-only.",
    {},
    async () => {
      try {
        const [stats, summaryStats] = await Promise.all([client.stats(), client.summaryStats()]);
        return ok({ stats, summary_stats: summaryStats });
      } catch (error) {
        return fail(error);
      }
    },
  );

  server.tool(
    "health",
    "Check whether the code-search-api service is reachable and report index readiness counters. Read-only.",
    {},
    async () => {
      try {
        return ok(await client.health());
      } catch (error) {
        return fail(error);
      }
    },
  );
}

export function formatSearchResponse(
  response: SearchResponse,
  options: {
    format: SearchResponseFormat;
    includeContent: boolean;
    maxContentChars: number;
    minScore?: number;
  },
): unknown {
  if (options.format === "raw") return response;

  const compactResults = response.results.map((result) => compactResult(result, options));
  if (options.format === "compact") {
    return {
      total_matches: response.total_matches,
      mode: response.mode,
      ...emptyHint(compactResults.length, options.minScore),
      results: compactResults,
    };
  }

  const files = new Map<
    string,
    { file_path: string; project: string; best_score: number; matches: CompactSearchResult[] }
  >();
  for (const result of compactResults) {
    const key = result.file_path;
    const existing = files.get(key);
    if (existing) {
      existing.best_score = Math.max(existing.best_score, result.score);
      existing.matches.push(result);
    } else {
      files.set(key, {
        file_path: result.file_path,
        project: result.project,
        best_score: result.score,
        matches: [result],
      });
    }
  }

  const grouped = [...files.values()].sort((a, b) => b.best_score - a.best_score);
  return {
    total_matches: response.total_matches,
    mode: response.mode,
    ...emptyHint(grouped.length, options.minScore),
    files: grouped,
  };
}

// When the compact/by_file projection is empty, surface the applied min_score
// and total_matches so callers know the floor filtered everything out and can
// retry with a lower min_score.
function emptyHint(count: number, minScore?: number): Record<string, unknown> {
  if (count > 0 || minScore === undefined) return {};
  return {
    applied_min_score: minScore,
    note: `No results at or above min_score ${minScore}. Lower min_score to widen the search.`,
  };
}

function compactResult(
  result: SearchResult,
  options: {
    includeContent: boolean;
    maxContentChars: number;
  },
): CompactSearchResult {
  const compact: CompactSearchResult = {
    file_path: result.file_path,
    project: result.project,
    chunk_index: result.chunk_index,
    chunk_type: result.chunk_type,
    score: result.score,
    code_score: result.code_score,
    summary_score: result.summary_score,
    summary: result.summary,
  };

  if (options.includeContent) {
    compact.content = truncate(result.content, options.maxContentChars);
  }

  return compact;
}

function truncate(value: string, maxChars: number): string {
  if (maxChars <= 0) return "";
  if (value.length <= maxChars) return value;
  return `${value.slice(0, Math.max(0, maxChars - 3)).trimEnd()}...`;
}
