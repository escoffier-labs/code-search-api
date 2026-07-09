import { realpathSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { CodeSearchClient } from "./client.js";
import { getConfig } from "./config.js";
import { serve } from "./index.js";
import { formatSearchResponse } from "./tools/code-search.js";
import type {
  HealthResponse,
  ProjectsResponse,
  SearchMode,
  SearchResponse,
  StatsResponse,
  SummaryStatsResponse,
} from "./types.js";
import pkg from "../package.json" with { type: "json" };

export class UsageError extends Error {}

export type Parsed =
  | { kind: "help" }
  | { kind: "version" }
  | { kind: "mcp" }
  | { kind: "projects"; json: boolean }
  | { kind: "stats"; json: boolean }
  | { kind: "health"; json: boolean }
  | {
      kind: "search";
      json: boolean;
      query: string;
      mode: SearchMode;
      project: string | undefined;
      limit: number;
      minScore: number;
      maxContent: number;
      includeContent: boolean;
    };

export const HELP = `code-search - semantic search over a local code-search-api index

Usage:
  code-search <command> [options]

Commands:
  search <query>     Semantic search over the indexed codebase
  projects           List indexed projects
  stats              Index and summary-coverage statistics
  health             Check that code-search-api is reachable (exit 1 if not ok)
  mcp                Start the MCP server over stdio
  help               Show this help

Global options:
  --json             Emit raw JSON instead of human-readable text
  --version, -v      Print version
  --help, -h         Show help

search options:
  --mode <m>         hybrid | code | summary        (default hybrid)
  --project <name>   Restrict to one project
  --limit <n>        Max results, 1-50              (default 10)
  --min-score <n>    Minimum similarity, 0-1        (default 0.3)
  --max-content <n>  Content preview chars, 0-500   (default 240)
  --no-content       Omit content previews

Environment:
  CODE_SEARCH_API_URL   API base URL (default http://localhost:5204)
  CODE_SEARCH_API_KEY   Optional API key (sent as X-API-Key)`;

function takeFlag(args: string[], name: string): boolean {
  const i = args.indexOf(name);
  if (i === -1) return false;
  args.splice(i, 1);
  return true;
}

function ensureNoExtra(args: string[]): void {
  if (args.length) throw new UsageError(`Unexpected arguments: ${args.join(" ")}`);
}

function requireValue(v: string | undefined, name: string): string {
  if (v === undefined || v.startsWith("--")) throw new UsageError(`${name} requires a value`);
  return v;
}

function requireInt(v: string | undefined, name: string, min: number, max: number): number {
  const n = Number(requireValue(v, name));
  if (!Number.isInteger(n) || n < min || n > max) {
    throw new UsageError(`${name} must be an integer in [${min}, ${max}]`);
  }
  return n;
}

function requireNum(v: string | undefined, name: string, min: number, max: number): number {
  const n = Number(requireValue(v, name));
  if (!Number.isFinite(n) || n < min || n > max) {
    throw new UsageError(`${name} must be a number in [${min}, ${max}]`);
  }
  return n;
}

function requireEnum<T extends string>(v: string | undefined, allowed: readonly T[], name: string): T {
  const s = requireValue(v, name);
  if (!(allowed as readonly string[]).includes(s)) {
    throw new UsageError(`${name} must be one of: ${allowed.join(", ")}`);
  }
  return s as T;
}

function parseSearch(args: string[], json: boolean): Parsed {
  let mode: SearchMode = "hybrid";
  let project: string | undefined;
  let limit = 10;
  let minScore = 0.3;
  let maxContent = 240;
  let includeContent = true;
  const positionals: string[] = [];

  while (args.length) {
    const a = args.shift() as string;
    switch (a) {
      case "--mode":
        mode = requireEnum(args.shift(), ["hybrid", "code", "summary"] as const, "--mode");
        break;
      case "--project":
        project = requireValue(args.shift(), "--project");
        break;
      case "--limit":
        limit = requireInt(args.shift(), "--limit", 1, 50);
        break;
      case "--min-score":
        minScore = requireNum(args.shift(), "--min-score", 0, 1);
        break;
      case "--max-content":
        maxContent = requireInt(args.shift(), "--max-content", 0, 500);
        break;
      case "--no-content":
        includeContent = false;
        break;
      default:
        if (a.startsWith("--")) throw new UsageError(`Unknown option: ${a}`);
        positionals.push(a);
    }
  }

  const query = positionals.join(" ").trim();
  if (!query) throw new UsageError("search requires a query");
  return { kind: "search", json, query, mode, project, limit, minScore, maxContent, includeContent };
}

export function parseArgs(argv: string[]): Parsed {
  const args = [...argv];
  if (args.includes("-h") || args.includes("--help")) return { kind: "help" };
  if (args.includes("-v") || args.includes("--version")) return { kind: "version" };

  const cmd = args.shift();
  if (!cmd || cmd === "help") return { kind: "help" };

  const json = takeFlag(args, "--json");
  switch (cmd) {
    case "mcp":
      return { kind: "mcp" };
    case "projects":
      ensureNoExtra(args);
      return { kind: "projects", json };
    case "stats":
      ensureNoExtra(args);
      return { kind: "stats", json };
    case "health":
      ensureNoExtra(args);
      return { kind: "health", json };
    case "search":
      return parseSearch(args, json);
    default:
      throw new UsageError(`Unknown command: ${cmd}`);
  }
}

type GroupedFiles = {
  total_matches: number;
  mode: string;
  files: Array<{
    file_path: string;
    project: string;
    best_score: number;
    matches: Array<{
      chunk_index: number;
      chunk_type: string;
      score: number;
      summary: string | null;
      content?: string;
    }>;
  }>;
};

function renderSearch(
  resp: SearchResponse,
  opts: { includeContent: boolean; maxContentChars: number; minScore: number },
): string {
  if (resp.results.length === 0) {
    return `No matches (mode=${resp.mode}, ${resp.total_matches} total before the min-score filter).`;
  }
  const grouped = formatSearchResponse(resp, {
    format: "by_file",
    includeContent: opts.includeContent,
    maxContentChars: opts.maxContentChars,
    minScore: opts.minScore,
  }) as GroupedFiles;

  const lines: string[] = [`${grouped.files.length} file(s), ${resp.total_matches} match(es), mode=${resp.mode}`];
  for (const f of grouped.files) {
    lines.push("");
    lines.push(`${f.best_score.toFixed(3)}  ${f.project}/${f.file_path}`);
    for (const m of f.matches) {
      lines.push(`  - chunk ${m.chunk_index} [${m.chunk_type}] score ${m.score.toFixed(3)}`);
      if (m.summary) lines.push(`    ${m.summary}`);
      if (opts.includeContent && m.content) {
        for (const cl of m.content.split("\n")) lines.push(`    | ${cl}`);
      }
    }
  }
  return lines.join("\n");
}

function renderProjects(p: ProjectsResponse): string {
  if (!p.projects.length) return "No projects indexed.";
  const lines = [`${p.projects.length} project(s):`];
  for (const pr of [...p.projects].sort((a, b) => b.chunks - a.chunks)) {
    lines.push(`  ${pr.project}  chunks=${pr.chunks} embedded=${pr.embedded} summarized=${pr.summarized}`);
  }
  return lines.join("\n");
}

function renderStats(stats: StatsResponse, summary: SummaryStatsResponse): string {
  const lines = [
    `Total chunks: ${stats.total_chunks}`,
    `Summarized: ${summary.summarized}/${summary.total_chunks} (pending ${summary.pending})`,
    "By type:",
  ];
  for (const [t, n] of Object.entries(stats.by_type)) lines.push(`  ${t}: ${n}`);
  lines.push("By project:");
  for (const pr of stats.by_project) lines.push(`  ${pr.project}: ${pr.summarized}/${pr.total} (${pr.pct}%)`);
  if (Object.keys(summary.by_model).length) {
    lines.push("Summary models:");
    for (const [m, n] of Object.entries(summary.by_model)) lines.push(`  ${m}: ${n}`);
  }
  return lines.join("\n");
}

function renderHealth(h: HealthResponse): string {
  const lines = [`status: ${h.status}`, `version: ${h.version}`];
  if (h.chunks !== undefined) lines.push(`chunks: ${h.chunks}`);
  if (h.embedded !== undefined) lines.push(`embedded: ${h.embedded}`);
  if (h.summarized !== undefined) lines.push(`summarized: ${h.summarized}`);
  if (h.error) lines.push(`error: ${h.error}`);
  return lines.join("\n");
}

export interface CliDeps {
  out: (s: string) => void;
  err: (s: string) => void;
  makeClient: () => CodeSearchClient;
  serve: () => Promise<void>;
}

export async function run(argv: string[], deps: CliDeps): Promise<number> {
  let parsed: Parsed;
  try {
    parsed = parseArgs(argv);
  } catch (error) {
    deps.err(error instanceof Error ? error.message : String(error));
    deps.err("");
    deps.err(HELP);
    return 2;
  }

  if (parsed.kind === "help") {
    deps.out(HELP);
    return 0;
  }
  if (parsed.kind === "version") {
    deps.out(pkg.version);
    return 0;
  }
  if (parsed.kind === "mcp") {
    await deps.serve();
    return 0;
  }

  const client = deps.makeClient();
  try {
    switch (parsed.kind) {
      case "search": {
        const resp = await client.search({
          query: parsed.query,
          mode: parsed.mode,
          project: parsed.project,
          limit: parsed.limit,
          min_score: parsed.minScore,
        });
        deps.out(
          parsed.json
            ? JSON.stringify(resp, null, 2)
            : renderSearch(resp, {
                includeContent: parsed.includeContent,
                maxContentChars: parsed.maxContent,
                minScore: parsed.minScore,
              }),
        );
        return 0;
      }
      case "projects": {
        const p = await client.listProjects();
        deps.out(parsed.json ? JSON.stringify(p, null, 2) : renderProjects(p));
        return 0;
      }
      case "stats": {
        const [stats, summary] = await Promise.all([client.stats(), client.summaryStats()]);
        deps.out(parsed.json ? JSON.stringify({ stats, summary_stats: summary }, null, 2) : renderStats(stats, summary));
        return 0;
      }
      case "health": {
        const h = await client.health();
        deps.out(parsed.json ? JSON.stringify(h, null, 2) : renderHealth(h));
        return h.status === "ok" ? 0 : 1;
      }
    }
  } catch (error) {
    deps.err(error instanceof Error ? error.message : String(error));
    return 1;
  }
  return 0;
}

// True when this module is the process entrypoint. process.argv[1] is often a
// symlink (npm installs the bin as a link); resolve it before comparing.
const isEntrypoint = (() => {
  const arg = process.argv[1];
  if (typeof arg !== "string") return false;
  try {
    return import.meta.url === pathToFileURL(realpathSync(arg)).href;
  } catch {
    return false;
  }
})();

if (isEntrypoint) {
  run(process.argv.slice(2), {
    out: (s) => process.stdout.write(`${s}\n`),
    err: (s) => process.stderr.write(`${s}\n`),
    makeClient: () => new CodeSearchClient(getConfig()),
    serve,
  })
    .then((code) => {
      process.exitCode = code;
    })
    .catch((error: unknown) => {
      process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
      process.exitCode = 1;
    });
}
