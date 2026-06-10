# code-search-mcp

[![npm version](https://img.shields.io/npm/v/@solomonneas/code-search-mcp.svg)](https://www.npmjs.com/package/@solomonneas/code-search-mcp)
[![license](https://img.shields.io/npm/l/@solomonneas/code-search-mcp.svg)](./LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-blue)](https://modelcontextprotocol.io)

Read-only MCP server for [code-search-api](https://github.com/escoffier-labs/code-search-api). It lets Claude Desktop, Claude Code, OpenClaw, Hermes Agent, Codex CLI, and any MCP-compatible client query a local codebase by intent through a running code-search-api service.

`code-search-mcp` talks to the FastAPI service over HTTP and uses stdio for MCP transport. It does not index, delete, backfill, or mutate the code-search-api database.

## Tools

- `search_code` - semantic search over the indexed workspace. Supports `mode` (`hybrid`, `code`, `summary`), `project`, `limit`, and `min_score`.
- `list_projects` - project names and chunk, embedding, and summary counts from `/api/projects`.
- `code_search_stats` - chunk type, per-project coverage, and summary model coverage from `/api/stats` and `/api/summary-stats`.
- `health` - readiness and index counters from `/health`.

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

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

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

### Claude Code

```bash
claude mcp add code-search \
  --env CODE_SEARCH_API_URL=http://localhost:5204 \
  --env CODE_SEARCH_API_KEY=your-api-key-here \
  -- code-search-mcp
```

Add `--scope user` to make it available from any directory instead of only the current project.

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

### Hermes Agent

[Hermes Agent](https://github.com/NousResearch/hermes-agent) reads MCP config from `~/.hermes/config.yaml` under the `mcp_servers` key. Add an entry:

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

Then reload MCP from inside a Hermes session:

```
/reload-mcp
```

### Codex CLI

[Codex CLI](https://github.com/openai/codex) registers MCP servers via `codex mcp add`:

```bash
codex mcp add code-search \
  --env CODE_SEARCH_API_URL=http://localhost:5204 \
  --env CODE_SEARCH_API_KEY=your-api-key-here \
  -- code-search-mcp
```

Or, when running from a source checkout:

```bash
codex mcp add code-search \
  --env CODE_SEARCH_API_URL=http://localhost:5204 \
  --env CODE_SEARCH_API_KEY=your-api-key-here \
  -- node /absolute/path/to/code-search-api/mcp/dist/index.js
```

Codex writes the entry to `~/.codex/config.toml` under `[mcp_servers.code-search]`. Verify with:

```bash
codex mcp list
```

## Development

```bash
npm install
npm run typecheck
npm test
npm run build
```

## License

MIT
