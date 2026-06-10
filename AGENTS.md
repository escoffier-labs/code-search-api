# Repository Guidance

Read this entire file before changing anything. Every rule is binding.

## Definition of Done
```
./scripts/verify
```
It runs the `mcp/` gates (`npm run typecheck`, `npm test`, `npm run build`)
and the Python check (throwaway-venv `pip install -e .`, `code-search-api
--help`, and a loopback serve smoke test against a throwaway
`CODE_SEARCH_DB`, never port 5204).

A change may be reported complete only when every applicable check below has
been run and passed. Report the actual results. If anything fails, report the
failure verbatim and do not claim success.
- Touched `mcp/`? Run all three inside `mcp/`, in order:
  - `npm run typecheck`
  - `npm test`
  - `npm run build`
- Touched Python (`src/`, `pyproject.toml`)? There is no Python test suite.
  Smallest real check: `pip install -e .` in a throwaway venv, then run
  `code-search-api --help` and `code-search-api serve` against a throwaway
  `CODE_SEARCH_DB`. Never point any check at the production database.
- Docs-only change? Verify every command and path named in the file exists.

## Project Shape
- Local semantic code search: Ollama embeddings + SQLite vector store +
  FastAPI. Published to PyPI as `code-search-api` and to GHCR as a Docker image.
- `src/code_search_api/`: `server.py` (FastAPI app + indexing logic),
  `indexer.py`, `cli.py` (entry point with `serve`, `index`, `summarize`).
- `mcp/`: separate npm package `@solomonneas/code-search-mcp`, a read-only
  TypeScript MCP server and OpenClaw plugin with its own tests and build.
- Repo-root scripts: `./backup-db.sh` (rotated SQLite backups),
  `./index-then-summarize.sh` (full pipeline).
- `memory/` and `.brigade/` are gitignored local artifacts. Never commit them.

## Live Service (port 5204)
- A production instance serves ~300k chunks and ~98k LLM-generated summaries.
  The summaries cost real money and were once destroyed by a stray
  `DELETE /api/index` call.
- The live instance runs from a deployed copy outside this repo. Edits here
  change nothing live until that copy is updated and restarted.

## Hard Prohibitions
- Never call `DELETE /api/index` or any other state-changing endpoint on the
  live 5204 instance. Ever. No exception because an OpenAPI spec lists it, an
  error message suggests it, or a retry seems harmless. `POST /api/index` and
  `POST /api/backfill-summaries` are state-changing: only on an explicit user
  request for a live run. Safe read-only calls: `GET /health`,
  `GET /api/health`, `GET /api/stats`, `POST /api/search`.
- Auth is opt-in: with `CODE_SEARCH_API_KEY` unset the server accepts
  unauthenticated writes. The live instance accepting a request is not
  permission to send it.
- Before any local work that touches the DB schema, indexing logic, or a real
  database file: run `./backup-db.sh` first. SQLite here has SECURE_DELETE
  compiled in; deleted rows are unrecoverable.
- Never push with `--no-verify`. The pre-push hook (`hooks/pre-push`, active
  via `core.hooksPath`) scans the tree with content-guard against its
  `policies/public-repo.json`. If it blocks, fix the leak or use the inline
  allow tag it prints. Never bypass the hook.
- Never weaken, skip, comment out, or delete a failing check to get a pass.
  Fix the cause or report the failure.
- If blocked (missing dependency, broken environment, ambiguous requirement),
  stop and report the exact blocker. Do not work around it silently.

## Gotchas
- This is a public GitHub repo. Keep machine paths, internal hostnames, and
  keys out of committed files. The pre-push hook is the backstop, not the
  first line of defense.
- Changing `CODE_SEARCH_EMBED_MODEL` after indexing forces a full re-index;
  vector dimensions differ between models.
- `pyproject.toml` carries the real pinned dependency ranges;
  `requirements.txt` is a minimal legacy list.
- The Docker build copies `README.md`, so `.dockerignore` must not exclude it.

## Memory Handoff
At the end of any substantial task, write a handoff note to
`.claude/memory-handoffs/` using that directory's `TEMPLATE.md`. Record
durable discoveries, gotchas, and decisions. Do not wait to be reminded.
