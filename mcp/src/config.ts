export interface CodeSearchConfig {
  url: string;
  apiKey?: string;
}

export function getConfig(env: NodeJS.ProcessEnv = process.env): CodeSearchConfig {
  const rawUrl = env.CODE_SEARCH_API_URL || "http://localhost:5204";
  const url = normalizeUrl(rawUrl);
  const apiKey = env.CODE_SEARCH_API_KEY?.trim();

  return {
    url,
    apiKey: apiKey || undefined,
  };
}

export function normalizeUrl(rawUrl: string): string {
  let parsed: URL;
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
