# Implementation Plan: code-search operator CLI (reference toolkit)

Date: 2026-06-27
Status: ready to execute
Executor: a fresh agent or engineer with no prior context. Execute task by task, top to bottom. Tick each `- [ ]` as you go. Do not skip the "run it, watch it fail" steps.

## Goal

Add a real operator CLI (`code-search`) to the `@solomonneas/code-search-mcp` package, sharing the existing `CodeSearchClient` core with the MCP server, so the same tool serves agents (MCP) and operators/cron/CI (CLI).

## Architecture

The package already has the right shape: `src/client.ts` (`CodeSearchClient`) is the core, `src/index.ts`'s `createServer()` is the MCP adapter. This plan adds a third thin consumer of the same client: a CLI. The server-start logic is extracted into an exported `serve()`; two tiny bin entrypoints (`cli.ts`, `mcp-bin.ts`) wrap it. No new runtime dependencies, no new repos, no behavior change to the MCP server or the OpenClaw plugin (`index.mjs` is self-contained and untouched).

```
src/client.ts        core (HTTP client; unchanged)
src/config.ts        core (env config; unchanged)
src/tools/*          MCP tool registration + formatSearchResponse (unchanged, reused by CLI)
src/index.ts         library: createServer(), serve(), schema-strip helpers (refactored)
src/mcp-bin.ts       NEW bin: starts the MCP server (back-compat for `code-search-mcp`)
src/cli.ts           NEW bin: operator CLI (`code-search`), and `code-search mcp` delegates to serve()
```

Key tech: TypeScript ESM, tsup (build), vitest (test). Node >= 20.

## Pinned decisions (do not relitigate; do not forward to the reader)

1. **Package `name` stays `@solomonneas/code-search-mcp` in this PR.** The user-facing command `code-search` ships now via the `bin` map. Renaming the npm package to `code-search` (+ `npm deprecate` of the old name) is a breaking, outward, publish-time action that bundles with the pending `@solomonneas` -> `escoffier-labs` org migration (the repo URL already points at `escoffier-labs`). It is explicitly out of scope here and listed as a separate gated follow-up at the end.
2. **CLI is read-only.** Commands: `search`, `projects`, `stats`, `health`, `mcp`, `help`. No `index`/`backfill`/`summarize` command. Indexing lives in the Python `code-search` / `code-search-api` repos, and the MCP read-only contract is enforced by an existing test. The audit's suggested `code-search index --summarize` is rejected for this reason.
3. **No CLI framework dependency.** A small hand-rolled parser keeps the dependency surface (`@modelcontextprotocol/sdk`, `zod`) unchanged. The parser is fully covered by tests.
4. **Two bin files, no basename magic.** `code-search` -> `dist/cli.js`, `code-search-mcp` -> `dist/mcp-bin.js`. Existing MCP clients that launch the `code-search-mcp` bin keep identical behavior. Anything launching the file path `dist/index.js` directly must switch to `dist/mcp-bin.js` (or `dist/cli.js mcp`); see the migration note in Task 5.
5. **Exit codes:** `0` success; `1` runtime error (API unreachable, search failed) and also `health` when status is not `ok` (so cron can gate on it); `2` usage error (unknown command/flag, bad value).

## File map

- Modify: `src/index.ts` (extract `serve()`, remove the auto-run entrypoint block)
- Create: `src/mcp-bin.ts` (MCP server bin)
- Create: `src/cli.ts` (operator CLI: `parseArgs`, `run`, renderers, bin tail)
- Create: `tests/cli.test.ts` (parser + run() behavior, no network)
- Modify: `package.json` (`bin`, `scripts.start`)
- Modify: `tsup.config.ts` (entrypoints, `splitting: false`)
- Modify: `README.md` (CLI usage section + bin migration note)

---

### Task 1: Extract `serve()` and make `index.ts` a pure library

**Files:**
- Modify: `src/index.ts`
- Test: `tests/code-search.test.ts` (existing; must stay green)

- [x] Replace the `main()` / `isEntrypoint` / auto-run block at the bottom of `src/index.ts` with an exported `serve()`. The final state of `src/index.ts` from line 47 onward is exactly:

```ts
export async function serve(): Promise<void> {
  const server = createServer();
  const transport = new StdioServerTransport();
  applySchemaStripIntercept(transport);
  await server.connect(transport);
}
```

Delete the old `async function main()`, the `isEntrypoint` IIFE, and the `if (isEntrypoint) { main()... }` block. Keep all existing exports above it (`VERSION`, `createServer`, `stripDraftSchema`, `applySchemaStripIntercept`). The `realpathSync` / `pathToFileURL` imports are no longer used by `index.ts`; remove them from the import block (lines 1-2).

- [x] Run the existing suite, expect still green (no test depended on the auto-run): `npm test` - expect PASS (same test count as before).
- [x] Commit: `git add -A && git commit -m "refactor: extract serve() so index.ts is a pure library"`

---

### Task 2: MCP server bin (back-compat entrypoint)

**Files:**
- Create: `src/mcp-bin.ts`

- [x] Create `src/mcp-bin.ts` with the full contents:

```ts
import { serve } from "./index.js";

serve().catch((error: unknown) => {
  const msg = error instanceof Error ? error.message : String(error);
  console.error(`code-search-mcp fatal: ${msg}`);
  process.exit(1);
});
```

- [x] Typecheck: `npm run typecheck` - expect PASS.
- [x] Commit: `git add -A && git commit -m "feat: add code-search-mcp server bin"`

---

### Task 3: Operator CLI (`src/cli.ts`) + tests

**Files:**
- Create: `tests/cli.test.ts`
- Create: `src/cli.ts`

- [x] Write the failing test first. Create `tests/cli.test.ts`:

```ts
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
```

- [x] Run it, watch it fail: `npx vitest run tests/cli.test.ts` - expect FAIL, "Cannot find module '../src/cli.js'" (file not created yet).
- [x] Implement `src/cli.ts` with the full contents:

```ts
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
```

- [x] Run to green: `npx vitest run tests/cli.test.ts` - expect PASS (all cases).
- [x] Run the full suite to confirm nothing regressed: `npm test` - expect PASS.
- [x] Commit: `git add -A && git commit -m "feat: add code-search operator CLI sharing the client core"`

---

### Task 4: Wire bins, build entries, scripts

**Files:**
- Modify: `package.json`
- Modify: `tsup.config.ts`

- [x] In `package.json`, replace the `bin` block with:

```json
  "bin": {
    "code-search": "dist/cli.js",
    "code-search-mcp": "dist/mcp-bin.js"
  },
```

- [x] In `package.json` `scripts`, change `start` from `"node dist/index.js"` to:

```json
    "start": "node dist/cli.js mcp",
```

- [x] In `tsup.config.ts`, set the entry array and disable splitting so each bin is self-contained. Final file:

```ts
import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/index.ts", "src/cli.ts", "src/mcp-bin.ts"],
  format: ["esm", "cjs"],
  target: "node20",
  outDir: "dist",
  clean: true,
  splitting: false,
  sourcemap: true,
  dts: true,
  banner: {
    js: "#!/usr/bin/env node",
  },
});
```

- [x] Build: `npm run build` - expect success and `dist/cli.js`, `dist/mcp-bin.js`, `dist/index.js` present (`ls dist/cli.js dist/mcp-bin.js`).
- [x] Smoke the CLI without the API: `node dist/cli.js --version` - expect it prints `0.2.0`.
- [x] Smoke help routing: `node dist/cli.js help | head -1` - expect `code-search - semantic search over a local code-search-api index`.
- [x] Smoke against the live API (code-search-api runs on :5204): `node dist/cli.js health` - expect `status: ok` and exit 0 (`echo $?` -> 0). If the API is down, expect a clear "Unable to reach code-search-api" error and exit 1; that is correct behavior, not a task failure.
- [x] Smoke a real search: `node dist/cli.js search "config loading" --limit 3` - expect grouped file output.
- [x] Commit: `git add -A && git commit -m "build: expose code-search and code-search-mcp bins"`

---

### Task 5: README CLI section + bin migration note

**Files:**
- Modify: `README.md`

- [x] Add a `## CLI` section to `README.md` (place it after the MCP usage section). Insert this markdown verbatim:

```markdown
## CLI

The same package ships an operator CLI for shells, cron, and CI. It is read-only and talks to the same local `code-search-api`.

```bash
npx @solomonneas/code-search-mcp@latest search "where is auth configured" --limit 5
# or, installed globally, simply:
code-search search "where is auth configured"
code-search projects
code-search stats
code-search health        # exit 1 if the API is not ok (cron-friendly)
code-search --json stats  # raw JSON for piping
```

Run `code-search help` for the full flag list. Configure with `CODE_SEARCH_API_URL` (default `http://localhost:5204`) and optional `CODE_SEARCH_API_KEY`.

### Starting the MCP server

`code-search mcp` (or the back-compat `code-search-mcp` bin) starts the stdio MCP server. If a launcher referenced the file path `dist/index.js` directly, point it at `dist/mcp-bin.js` (or `dist/cli.js mcp`); launchers that use the `code-search-mcp` bin name need no change.
```

- [x] Commit: `git add -A && git commit -m "docs: document the code-search CLI and bin migration"`

---

## Verification checklist (run after all tasks)

- [x] `npm run typecheck` - PASS
- [x] `npm test` - PASS (existing MCP tests + new CLI tests)
- [x] `npm run build` - PASS
- [x] `node dist/cli.js health` - reachable, exit 0 (with API up)
- [x] `node dist/mcp-bin.js` - starts and blocks on stdio (Ctrl-C to exit); confirms back-compat entrypoint

## Out of scope (separate, gated follow-ups)

- **npm package rename** `@solomonneas/code-search-mcp` -> `code-search` (+ `npm deprecate` of the old name) and the `@solomonneas` -> `escoffier-labs` scope move. This is a breaking publish; do it as one step during the org migration, not here. Requires explicit owner go.
- **`npm publish`** of this feature as a minor version bump. Publishing is owner-gated.
- Templating this core/cli/mcp split onto the next toolkit (`n8n-ops`), then the network-ops pair (`adguardctl`, `librenmsctl`).
