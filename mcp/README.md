<p align="center">
  <img src="docs/assets/code-search-mcp-social-preview.jpg" alt="code-search-mcp banner" width="900">
</p>

<h1 align="center">code-search-mcp</h1>

<p align="center">
  <strong>Read-only MCP server that lets any MCP client query a local codebase by intent through code-search-api.</strong>
</p>

<p align="center">
  <img src="https://shieldcn.dev/github/ci/escoffier-labs/code-search-api.svg?branch=main&workflow=ci.yml" alt="CI status">
  <img src="https://shieldcn.dev/npm/@solomonneas/code-search-mcp.svg" alt="npm version">
  <img src="https://shieldcn.dev/badge/language-TypeScript-blue.svg" alt="TypeScript">
  <img src="https://shieldcn.dev/badge/license-MIT-green.svg" alt="MIT license">
</p>

Run intent search over your local code index without giving the MCP server write access to the index.

<!-- proof: inspector tools/list + one real query recording lands here; spec in the plating gallery -->

## What it does

`code-search-mcp` is a read-only MCP server and shell CLI for querying a running [code-search-api](https://github.com/escoffier-labs/code-search-api) service by intent. It exists so MCP-compatible desktop, CLI, and gateway clients can search a local semantic code index through a small stdio server instead of calling the HTTP API directly. It differs from a standalone indexer because this package only exposes search, project listing, stats, and health checks over read-only HTTP endpoints, while code-search-api owns indexing, embeddings, summaries, and storage. The MCP server uses stdio for transport, and the same package also ships a read-only `code-search` command for scripts, shells, cron, and CI.


## Install

```bash
npm install -g @solomonneas/code-search-mcp
```

Or from source:

```bash
git clone https://github.com/escoffier-labs/code-search-api.git
cd code-search-api/mcp
npm install
npm run build
```

## Tools

- `search_code` - semantic search over the indexed workspace. Supports `mode` (`hybrid`, `code`, `summary`), `project`, `limit`, `min_score`, `response_format`, `include_content`, and `max_content_chars`.
- `list_projects` - project names and chunk, embedding, and summary counts from `/api/projects`.
- `code_search_stats` - chunk type, per-project coverage, and summary model coverage from `/api/stats` and `/api/summary-stats`.
- `health` - readiness and index counters from `/health`.

`search_code` response formats:

| Format | Description |
|--------|-------------|
| `raw` | The unmodified code-search-api `/api/search` response. This is the default. |
| `compact` | Keeps scores, file path, project, chunk metadata, summary, and optional trimmed content preview. |
| `by_file` | Groups compact matches by `file_path` and surfaces each file's best score. |

Example prompts:

- "Find the FastAPI route that handles semantic code search."
- "Where is API key authentication enforced?"
- "List likely files involved in summary backfills, grouped by file."
- "Search only the `code-search-api` project for embedding cache logic."

## Configuration

Start code-search-api first:

```bash
code-search-api serve
```

Set these environment variables in your MCP client config:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CODE_SEARCH_API_URL` | no | `http://localhost:5204` | Base URL for the running code-search-api service |
| `CODE_SEARCH_API_KEY` | no | - | Optional API key sent as `X-API-Key` when the FastAPI service requires it |

### Generic MCP JSON config

Add a server entry to an MCP client config:

```json
{
  "mcpServers": {
    "code-search": {
      "command": "code-search-mcp",
      "env": {
        "CODE_SEARCH_API_URL": "http://localhost:5204",
        "CODE_SEARCH_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

### Launcher argument shape

For clients that expose a shell registration command, map the server command and environment variables to that command's syntax:

```text
server name: code-search
command: code-search-mcp
env: CODE_SEARCH_API_URL=http://localhost:5204
env: CODE_SEARCH_API_KEY=your-api-key-here
```

Add a user or global scope flag if your launcher supports one.

### OpenClaw

If you're running from a source checkout instead of the npm-installed binary, point `command`/`args` at the built `dist/index.js`:

```bash
openclaw mcp set code-search '{
  "command": "node",
  "args": ["/absolute/path/to/code-search-api/mcp/dist/index.js"],
  "env": {
    "CODE_SEARCH_API_URL": "http://localhost:5204",
    "CODE_SEARCH_API_KEY": "your-api-key-here"
  }
}'
```

Or, with the global npm install:

```bash
openclaw mcp set code-search '{
  "command": "code-search-mcp",
  "env": {
    "CODE_SEARCH_API_URL": "http://localhost:5204",
    "CODE_SEARCH_API_KEY": "your-api-key-here"
  }
}'
```

Then restart the OpenClaw gateway so the new server is picked up:

```bash
systemctl --user restart openclaw-gateway
openclaw mcp list   # confirm "code-search" is registered
```

### YAML MCP config

For clients that read YAML config under an `mcp_servers` key, add an entry:

```yaml
mcp_servers:
  code-search:
    command: "code-search-mcp"
    env:
      CODE_SEARCH_API_URL: "http://localhost:5204"
      CODE_SEARCH_API_KEY: "your-api-key-here"
```

Or, when running from a source checkout instead of the global npm install:

```yaml
mcp_servers:
  code-search:
    command: "node"
    args: ["/absolute/path/to/code-search-api/mcp/dist/index.js"]
    env:
      CODE_SEARCH_API_URL: "http://localhost:5204"
      CODE_SEARCH_API_KEY: "your-api-key-here"
```

Some YAML-driven clients expose a reload command after config changes:

```
/reload-mcp
```

## CLI

The same package ships a read-only **search tool**, `code-search`, for shells, cron, and CI. It talks to the same local `code-search-api`.

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

## Why not grep, GitHub code search, or IDE search?

**grep/ripgrep:** use them when you know the exact token, symbol, or file path. `code-search-mcp` is for intent searches where a summary, related wording, or cross-file match is more useful than exact text.

**GitHub code search:** use it for hosted repositories, review links, and searches that benefit from GitHub's index. `code-search-mcp` talks to a local code-search-api service, so it can query whatever that service indexed without depending on a hosted repository search surface.

**IDE search:** use it while editing in one workspace. `code-search-mcp` is useful when an MCP client, script, or gateway needs the same search capability outside the editor.

## What code-search-mcp is not

`code-search-mcp` is not an indexer, crawler, backfill runner, embedding worker, or database maintenance tool. It does not delete, mutate, rebuild, or write to the code-search-api index. It is also not a hosted search product or a replacement for exact text search when exact text is the right tool.


## Development

```bash
npm install
npm run typecheck
npm test
npm run build
npm run smoke       # requires a live code-search-api service
npm run pack:dry-run
```

## Release

The release script verifies the package, optionally smoke-tests against a live service, publishes to npm, packs the exact npm artifact into `/tmp`, extracts it, and publishes that extracted package to ClawHub with source provenance pointing at this repo.

```bash
scripts/release.sh --publish
```

Set `SKIP_SMOKE=1` if no local code-search-api service is available during release.

## Changelog

See [CHANGELOG.md](./CHANGELOG.md) for notable changes, including the draft-07
`$schema` strip fix, the move under
`escoffier-labs/code-search-api/tree/main/mcp`, and the `scripts/verify`
entrypoint.

## HTTP mode

Run a single process for concurrent MCP clients from the repository root:

```sh
node mcp/dist/mcp-bin.js --http
```

Stdio remains the default. Set `CODE_SEARCH_API_URL` and the optional
`CODE_SEARCH_API_KEY` as usual, plus an independent random bearer token
(for example, generate one with `openssl rand -hex 32`).

| Variable | Default | Purpose |
| --- | --- | --- |
| `CODE_SEARCH_MCP_HTTP_TOKEN` | Required | Independent bearer token |
| `CODE_SEARCH_MCP_HTTP_HOST` | IPv4 loopback | Listener address |
| `CODE_SEARCH_MCP_HTTP_PORT` | `18819` | Listener port, or `0` for a free port |

Startup refuses a blank token or one matching the upstream API key or URL
credentials. Token comparison uses SHA-256 and constant-time comparison.
`/mcp` authenticates every POST, GET, and DELETE request. DELETE terminates an
MCP session only. The four tools remain read-only with fixed upstream routes.
There is no destructive API passthrough or arbitrary-URL fetch tool.
`GET /healthz` returns only `{"status":"ok"}` without authentication.

Host and Origin checks run before authentication. Bodies are limited to 1 MiB,
request and header timeouts are 30 and 10 seconds, and the listener caps sessions
at 128. Sessions idle for 30 minutes expire on a one-minute timer. Active tool
requests are excluded from expiry. SIGTERM and SIGINT close sessions and the
listener. Clients must initialize again after expiry or restart.

Each session owns a fresh MCP server and API client. There are no module-level
stats or project caches, pending approvals, deferred tool sets, write flags, or
shared request queues. Each upstream request retains its own 30-second timeout.
All clients holding the bearer token share the configured upstream privileges.
Keep the default loopback binding, or provide a trusted TLS proxy or tunnel
with network access controls and an allowed Host header for remote access.

### Claude Code

Add an HTTP entry under `mcpServers`:

```json
{
  "mcpServers": {
    "code-search": {
      "type": "http",
      "url": "http://localhost:18819/mcp",
      "headersHelper": "/path/to/helper CODE_SEARCH_MCP_HTTP_TOKEN"
    }
  }
}
```

The executable helper reads the named environment variable and prints
`{"Authorization":"Bearer ..."}` to stdout. Keep the token in the helper's
protected environment rather than in the MCP configuration.

### Codex

Export the same token in the environment that launches Codex:

```toml
[mcp_servers.code_search]
url = "http://localhost:18819/mcp"
bearer_token_env_var = "CODE_SEARCH_MCP_HTTP_TOKEN"
```

### systemd user service

The example [unit](../docs/systemd/code-search-mcp-http.service) uses generic
installation paths. Build the repository at the configured location and create
`%h/.config/code-search-mcp/http.env` with mode `0600`. Include the upstream
configuration and `CODE_SEARCH_MCP_HTTP_TOKEN` in that file. The unit supplies
`NoNewPrivileges=true`, `PrivateTmp=true`, and `UMask=0077`.

## License

MIT
