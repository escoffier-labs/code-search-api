const DEFAULT_URL = "http://localhost:5204";
const REQUEST_TIMEOUT_MS = 30_000;

function resolveConfig(pluginConfig = {}) {
  const url = normalizeUrl(String(pluginConfig.url || process.env.CODE_SEARCH_API_URL || DEFAULT_URL));
  const apiKeyEnv = String(pluginConfig.apiKeyEnv || "CODE_SEARCH_API_KEY");
  const apiKey = String(pluginConfig.apiKey || process.env[apiKeyEnv] || process.env.CODE_SEARCH_API_KEY || "").trim();
  return { url, apiKey: apiKey || undefined };
}

function normalizeUrl(rawUrl) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error(`Invalid CODE_SEARCH_API_URL: ${rawUrl}`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error(`CODE_SEARCH_API_URL must use http or https: ${rawUrl}`);
  }
  return parsed.toString().replace(/\/+$/, "");
}

function textResult(payload) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    details: payload,
  };
}

async function request(config, path, options = {}) {
  const headers = { Accept: "application/json" };
  if (config.apiKey) headers["X-API-Key"] = config.apiKey;
  if (options.body) headers["Content-Type"] = "application/json";

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    let response;
    try {
      response = await fetch(`${config.url}${path}`, {
        ...options,
        headers: { ...headers, ...(options.headers || {}) },
        signal: controller.signal,
      });
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") {
        throw new Error(`Request to ${path} timed out after ${REQUEST_TIMEOUT_MS}ms`);
      }
      const msg = error instanceof Error ? error.message : String(error);
      throw new Error(`Unable to reach code-search-api at ${config.url}. Is it running? ${msg}`);
    }
    const text = await response.text();
    if (!response.ok) {
      const detail = text ? `: ${text.slice(0, 500)}` : "";
      if (response.status === 401 || response.status === 403) {
        throw new Error(`code-search-api rejected the request. Set CODE_SEARCH_API_KEY for ${config.url}${detail}`);
      }
      throw new Error(`code-search-api HTTP ${response.status} for ${path}${detail}`);
    }
    return text ? JSON.parse(text) : undefined;
  } finally {
    clearTimeout(timeout);
  }
}

function makeTool(config, tool) {
  return {
    ...tool,
    execute: async (_toolCallId, params = {}) => {
      const payload = await tool.run(params, config);
      return textResult(payload);
    },
  };
}

const searchParameters = {
  type: "object",
  additionalProperties: false,
  required: ["query"],
  properties: {
    query: {
      type: "string",
      minLength: 1,
      description: "Natural-language description of the code, behavior, symbol, or workflow to find.",
    },
    mode: {
      type: "string",
      enum: ["hybrid", "code", "summary"],
      default: "hybrid",
      description: "Search mode.",
    },
    project: {
      type: "string",
      minLength: 1,
      description: "Optional exact project filter from list_projects.",
    },
    limit: {
      type: "integer",
      minimum: 1,
      maximum: 50,
      default: 10,
      description: "Maximum results to return.",
    },
    min_score: {
      type: "number",
      minimum: 0,
      maximum: 1,
      default: 0.3,
      description: "Minimum similarity score.",
    },
    response_format: {
      type: "string",
      enum: ["raw", "compact", "by_file"],
      default: "raw",
      description: "Output shape. raw returns the code-search-api response, compact trims each hit, by_file groups hits by file.",
    },
    include_content: {
      type: "boolean",
      default: true,
      description: "Include the content preview in compact and by_file output.",
    },
    max_content_chars: {
      type: "integer",
      minimum: 0,
      maximum: 500,
      default: 240,
      description: "Maximum content preview characters in compact and by_file output.",
    },
  },
};

const emptyParameters = {
  type: "object",
  additionalProperties: false,
  properties: {},
};

const tools = [
  {
    name: "search_code",
    label: "Search Code",
    description: "Search the indexed local codebase by developer intent using hybrid semantic search. Read-only.",
    parameters: searchParameters,
    run: async (params, config) => {
      const response = await request(config, "/api/search", {
        method: "POST",
        body: JSON.stringify({
          query: String(params.query),
          mode: params.mode || "hybrid",
          project: params.project,
          limit: params.limit || 10,
          min_score: params.min_score ?? 0.3,
        }),
      });
      return formatSearchResponse(response, {
        format: params.response_format || "raw",
        includeContent: params.include_content ?? true,
        maxContentChars: params.max_content_chars ?? 240,
        minScore: params.min_score ?? 0.3,
      });
    },
  },
  {
    name: "list_projects",
    label: "List Projects",
    description: "List projects currently present in the code-search-api index. Read-only.",
    parameters: emptyParameters,
    run: async (_params, config) => await request(config, "/api/projects"),
  },
  {
    name: "code_search_stats",
    label: "Code Search Stats",
    description: "Return code-search-api index health statistics. Read-only.",
    parameters: emptyParameters,
    run: async (_params, config) => {
      const [stats, summaryStats] = await Promise.all([
        request(config, "/api/stats"),
        request(config, "/api/summary-stats"),
      ]);
      return { stats, summary_stats: summaryStats };
    },
  },
  {
    name: "health",
    label: "Health",
    description: "Check whether the code-search-api service is reachable. Read-only.",
    parameters: emptyParameters,
    run: async (_params, config) => await request(config, "/health"),
  },
];

export default {
  id: "code-search",
  name: "Code Search",
  description: "Read-only tools for querying a local code-search-api semantic index by intent.",
  configSchema: {
    type: "object",
    additionalProperties: false,
    properties: {
      url: { type: "string", format: "uri", default: DEFAULT_URL },
      apiKey: { type: "string" },
      apiKeyEnv: { type: "string", default: "CODE_SEARCH_API_KEY" },
    },
  },
  register(api) {
    if (api.registrationMode !== "full") return;
    const config = resolveConfig(api.pluginConfig);
    for (const tool of tools) {
      api.registerTool(makeTool(config, tool));
    }
  },
};

function formatSearchResponse(response, options) {
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

  const files = new Map();
  for (const result of compactResults) {
    const existing = files.get(result.file_path);
    if (existing) {
      existing.best_score = Math.max(existing.best_score, result.score);
      existing.matches.push(result);
    } else {
      files.set(result.file_path, {
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
function emptyHint(count, minScore) {
  if (count > 0 || minScore === undefined) return {};
  return {
    applied_min_score: minScore,
    note: `No results at or above min_score ${minScore}. Lower min_score to widen the search.`,
  };
}

function compactResult(result, options) {
  const compact = {
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

function truncate(value, maxChars) {
  if (maxChars <= 0) return "";
  if (value.length <= maxChars) return value;
  return `${value.slice(0, Math.max(0, maxChars - 3)).trimEnd()}...`;
}
