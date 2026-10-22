<p align="center">
  <img src="docs/assets/code-search-api-social-preview.jpg" alt="Code Search API banner" width="900">
</p>

<h1 align="center">Code Search API</h1>

<p align="center">
  <img src="docs/assets/marks/code-search-api-circle.svg" alt="" width="40" height="40">
</p>

<p align="center">
  <strong>Search code by intent, on your machine.</strong>
</p>

<p align="center">
  Local semantic code search with Ollama embeddings, language-aware chunking, SQLite vectors, and hybrid search. FastAPI for agents and tools. Optional GraphTrail blend for structure plus similarity.
</p>

<p align="center">
  <a href="#try-it-in-60-seconds">Quickstart</a> &middot; <a href="#install">Install</a> &middot; <a href="https://brigade.tools/graphtrail">GraphTrail</a>
</p>

<p align="center">
  <img src="https://shieldcn.dev/github/ci/escoffier-labs/code-search-api.svg?branch=main&workflow=ci.yml" alt="CI status">
  <img src="https://shieldcn.dev/badge/license-MIT-green.svg" alt="MIT license">
</p>

## Install

```bash
# see repo docs for full local setup with Ollama
git clone https://github.com/escoffier-labs/code-search-api.git
cd code-search-api
# follow README install for venv + Ollama model pull
```

## What it does

| | Job | What you get |
|---|---|---|
| **Chunk** | Language-aware splits | Not arbitrary line windows |
| **Embed** | Local Ollama vectors | Stored as float32 in SQLite |
| **Search** | Intent, not exact text | Code, summary, or hybrid ranking |
| **Serve** | Small FastAPI surface | Agents and CLIs query locally |


## Install and run

### Option A - install from PyPI

```bash
pipx install code-search-api          # or: pip install code-search-api
ollama pull qwen3-embedding:8b
export CODE_SEARCH_WORKSPACE=/path/to/your/code   # see .env.example for full config
code-search-api index                  # first-time index
code-search-api serve                  # http://localhost:5204
```

### Option B - Docker

```bash
docker run --rm -p 5204:5204 \
  -e CODE_SEARCH_WORKSPACE=/workspace \
  -v /path/to/your/code:/workspace:ro \
  -v code-search-data:/data \
  --add-host host.docker.internal:host-gateway \
  ghcr.io/escoffier-labs/code-search-api:latest
```

A `docker-compose.yml` is included at the repo root for a more typical deployment shape.

### Option C - run from source

```bash
git clone https://github.com/escoffier-labs/code-search-api.git
cd code-search-api
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env    # edit CODE_SEARCH_WORKSPACE to point at your repos
source .env
code-search-api index
code-search-api serve
```

### Search

```bash
curl -s -X POST http://localhost:5204/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "rate limiting middleware", "mode": "hybrid"}'
```

## MCP Server

The read-only MCP server and OpenClaw plugin live in [`mcp/`](./mcp) at
`@solomonneas/code-search-mcp` version 0.2.0. The standalone
`escoffier-labs/code-search-mcp` repository is deprecated in favor of this
directory.

```bash
cd mcp
npm install
npm run build
```

## How It Works

![Code Search API indexing and search workflow](docs/assets/index-search-workflow.svg)

Generated from [`docs/assets/workflows/index-search.json`](docs/assets/workflows/index-search.json) with `plating workflow`.

1. **Chunking**: Files are split at logical boundaries (function/class definitions, not arbitrary line counts). Python, TypeScript, JavaScript, Go, Rust, Markdown, and config files are all handled with language-specific patterns.

2. **Embedding**: Each chunk is embedded with your chosen Ollama model and stored as packed float32 BLOBs in SQLite. No vector database required.

3. **Summarization**: An LLM generates a 1-2 sentence summary per chunk describing what the code *does*, not just what it *contains*. The summary gets its own embedding vector.

4. **Hybrid search**: Queries match against both code embeddings (35% weight) and summary embeddings (65% weight). This means searching "authentication flow" finds auth code even if the word "authentication" never appears in variable names.

## Why not ripgrep, hosted code search, or a vector database?

Use ripgrep or IDE search when you know the exact symbol, string, or file. Use Code Search API when the query is behavioral, such as "authentication flow", and the matching code may not contain the same words. Use a hosted code search product when you want a managed remote index. Use a separate vector database when you need distributed storage or vector infrastructure beyond a local SQLite file.

## What code-search-api is not

Code Search API is not a hosted SaaS, not a replacement for exact text search, not a general-purpose vector database, and not a security scanner or static analyzer. It is a local FastAPI service and CLI for indexing repositories with Ollama embeddings, optional code summaries, and hybrid semantic search.

## Embedding Models

The embedding model is the most important choice. It determines search quality.

**Recommended: `qwen3-embedding:8b`** (what this project was built on)

| Model | Params | VRAM | Quality | Speed | Best For |
|-------|--------|------|---------|-------|----------|
| **qwen3-embedding:8b** | 8B | ~6 GB | ★★★★★ | ★★★☆☆ | Best overall. Strong code + multilingual understanding. **Recommended.** |
| qwen3-embedding:4b | 4B | ~3 GB | ★★★★☆ | ★★★★☆ | Good balance if VRAM is tight |
| qwen3-embedding:0.6b | 0.6B | ~500 MB | ★★★☆☆ | ★★★★★ | Laptop/low-resource environments |
| nomic-embed-text | 137M | ~300 MB | ★★★☆☆ | ★★★★★ | Lightweight, fast, proven. Good starter model. |
| mxbai-embed-large | 335M | ~700 MB | ★★★½☆ | ★★★★☆ | Strong English performance |
| bge-m3 | 567M | ~1 GB | ★★★★☆ | ★★★★☆ | Excellent multilingual support |
| snowflake-arctic-embed2 | 568M | ~1 GB | ★★★★☆ | ★★★★☆ | Strong multilingual, good scaling |
| nomic-embed-text-v2-moe | MoE | ~500 MB | ★★★★☆ | ★★★★☆ | Multilingual MoE, efficient |

Pull your chosen model:

```bash
ollama pull qwen3-embedding:8b    # recommended
# or
ollama pull nomic-embed-text      # lightweight alternative
```

Set it in `.env`:

```
CODE_SEARCH_EMBED_MODEL=qwen3-embedding:8b
```

> **Note:** Changing the embedding model after indexing requires a full re-index since vector dimensions and similarity spaces differ between models.
> The indexer refuses accidental model changes. Set `CODE_SEARCH_ALLOW_MODEL_CHANGE=1` only when you intend to re-stamp the database and re-index with the configured embedding model.

## Summary Models

Summaries are what make hybrid search work. The summarizer reads each code chunk and writes a 1-2 sentence description of what it *does*. That summary gets its own embedding, so you can find code by describing behavior.

Summary generation goes through a small provider adapter (`src/code_search_api/summary_provider.py`). Pick a lane with `CODE_SEARCH_SUMMARY_PROVIDER`:

| Provider | Runs | Default model | Notes |
|----------|------|---------------|-------|
| `agy` (default) | `agy -p` subprocess (Antigravity CLI, Gemini) | `gemini-3.8-flash-low` | Uses the CLI's own subscription login. No API key is read. |
| `codex` | `codex exec` subprocess (ChatGPT login) | `gpt-6-luna` | Heavier per-call overhead than `agy`, so batches matter more. |
| `ollama` | Local Ollama chat model | `qwen3:14b` | Free but needs a capable local model. Ollama cloud models (`:cloud`) are rejected. |
| `none` | nothing | n/a | Disables summary generation. Search still works on code embeddings. |

For `agy` and `codex`, several chunks go into one call (`CODE_SEARCH_SUMMARY_BATCH_SIZE`, default 8) and the model replies with a JSON array. A smoke run of 3 real files produced 3 summaries and embeddings from one `agy` call in about 5 seconds.

### Daily cap

`CODE_SEARCH_SUMMARY_DAILY_CAP` (default `500`) limits how many chunks are sent to a subscription lane per local day, so a large backlog cannot burn quota unattended. `0` disables generation and a negative value removes the cap. The counter lives in the `meta` table, reserved chunks count even when a call fails, and `GET /api/summary-stats` reports `provider`, `model`, `daily_cap`, and `used_today`. `POST /api/backfill-summaries` and `code-search-api summarize` stop and return `cap_reached: true` once the day's budget is spent; run them again after midnight to continue. The cap does not apply to the local `ollama` provider.

### Local models (`ollama` provider)

You need at least a 14B+ parameter model to get useful code summaries. Anything smaller will hallucinate function names and produce generic descriptions that don't help search.

| Model | Params | VRAM | Quality | Notes |
|-------|--------|------|---------|-------|
| qwen3:32b | 32B | ~20 GB | ★★★★☆ | Best local option if you have the VRAM |
| qwen3:14b | 14B | ~10 GB | ★★★½☆ | Minimum viable for code summaries |
| codellama:34b | 34B | ~22 GB | ★★★★☆ | Strong code understanding |
| deepseek-coder-v2:16b | 16B | ~11 GB | ★★★½☆ | Decent code summaries |

**Models to avoid for summarization:**

| Model | Why |
|-------|-----|
| Any model < 7B | Summaries will be too vague to improve search |
| Heavily quantized (Q2, Q3) | Quality degrades to the point of being worse than no summary |
| Embedding models | These can't generate text, only vectors |

Set the provider and model in `.env`:

```
CODE_SEARCH_SUMMARY_PROVIDER=agy
CODE_SEARCH_SUMMARY_MODEL=gemini-3.8-flash-low
# or, fully local
CODE_SEARCH_SUMMARY_PROVIDER=ollama
CODE_SEARCH_SUMMARY_MODEL=qwen3:32b                  # needs ~20GB VRAM
```

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | No | Liveness check |
| `GET` | `/api/health` | No | Health + index stats (chunks, embedded, summarized) |
| `POST` | `/api/search` | Yes | Semantic search with hybrid, code, or summary mode |
| `POST` | `/api/index` | Yes | Trigger background indexing run |
| `POST` | `/api/backfill-summaries` | Yes | Generate summaries for unsummarized chunks |
| `GET` | `/api/projects` | Yes | Per-project chunk and summary counts |
| `GET` | `/api/stats` | Yes | Chunk type breakdown and project coverage |
| `GET` | `/api/summary-stats` | Yes | Summary counts by model |

### Search request

```json
{
  "query": "websocket authentication middleware",
  "mode": "hybrid",
  "limit": 10,
  "min_score": 0.3,
  "project": "my-api"
}
```

**Modes:**
- `hybrid` (default): Weighted combination of code + summary similarity. Best for most searches.
- `code`: Raw code embedding match only. Use when searching for exact patterns.
- `summary`: Summary embedding match only. Use when searching by high-level intent.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CODE_SEARCH_WORKSPACE` | `./repos` | Root directory to scan for code. Set to an empty string to disable the workspace root and scan only extra roots. |
| `CODE_SEARCH_REFERENCE` | *(unset)* | Optional second directory for reference docs |
| `CODE_SEARCH_EXTRA_SCAN_ROOTS` | *(unset)* | Extra scan roots as `root_id=/abs/path` entries separated by `:` (`os.pathsep`). Projects under an extra root are namespaced `root_id/<dirname>`. |
| `CODE_SEARCH_DB` | `./code_index.db` | SQLite database path |
| `CODE_SEARCH_API_KEY` | *(unset)* | API key for protected endpoints. Unset = no auth. |
| `CODE_SEARCH_CORS_ORIGINS` | `*` | Comma-separated CORS origins |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API base URL |
| `CODE_SEARCH_EMBED_MODEL` | `qwen3-embedding:8b` | Embedding model |
| `CODE_SEARCH_ALLOW_MODEL_CHANGE` | *(unset)* | Set to `1` to allow re-stamping an existing DB after changing embedding models |
| `CODE_SEARCH_SUMMARY_PROVIDER` | `agy` | Summary lane: `agy`, `codex`, `ollama` (local only), or `none` |
| `CODE_SEARCH_SUMMARY_MODEL` | per provider (`gemini-3.8-flash-low`, `gpt-6-luna`, `qwen3:14b`) | Primary summarization model |
| `CODE_SEARCH_SUMMARY_FALLBACK` | *(unset)* | Optional second model on the same provider |
| `CODE_SEARCH_SUMMARY_BATCH_SIZE` | `8` | Chunks per CLI call for `agy` and `codex` |
| `CODE_SEARCH_SUMMARY_DAILY_CAP` | `500` | Chunks sent to a subscription lane per local day (`0` off, negative unlimited) |
| `CODE_SEARCH_SUMMARY_TIMEOUT` | `240` | Seconds before a CLI summary call is abandoned |
| `CODE_SEARCH_SUMMARY_WORKERS` | `4` | Parallel summary generation workers |
| `CODE_SEARCH_DB_BATCH_SIZE` | `100` | DB write batch size |
| `CODE_SEARCH_CACHE_TTL_SECONDS` | `3600` | Query embedding cache TTL |

## Helper Scripts

| Script | Purpose |
|--------|---------|
| `code-search-api index` | CLI indexer for first-time or batch re-indexing |
| `index-then-summarize.sh` | Full pipeline: index new chunks, then summarize |
| `backup-db.sh` | Rotated SQLite backup (configurable retention) |

## Supported Languages

Chunking is language-aware for: Python, TypeScript/TSX, JavaScript/JSX, Go, Rust, Markdown, Astro, HTML, CSS, Shell, JSON, YAML, TOML.

Other text files are indexed as flat chunks.

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com) running locally (or on a reachable host)
- An embedding model pulled in Ollama
- ~500 MB to 6 GB VRAM depending on embedding model choice

## License

MIT
