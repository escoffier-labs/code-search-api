"""Console-script entry points for code-search-api."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "code_search_api.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


def _index(args: argparse.Namespace) -> int:
    from .indexer import main as run_indexer

    run_indexer()

    if not args.then_summarize:
        return 0

    from .server import backfill_summaries

    total_added = 0
    total_failed = 0
    while True:
        result = backfill_summaries(limit=args.summary_batch)
        print(json.dumps(result, indent=2), flush=True)
        total_added += result.get("summaries_added", 0)
        total_failed += result.get("failed", 0)
        if result.get("chunks_found", 0) == 0 or (
            result.get("summaries_added", 0) == 0 and result.get("failed", 0) == 0
        ):
            break

    print(json.dumps({"summaries_added": total_added, "failed": total_failed}, indent=2), flush=True)
    return 0


def _summarize(args: argparse.Namespace) -> int:
    from .server import backfill_summaries

    total_added = 0
    total_failed = 0
    while True:
        result = backfill_summaries(limit=args.batch, project=args.project)
        print(json.dumps(result, indent=2), flush=True)
        total_added += result.get("summaries_added", 0)
        total_failed += result.get("failed", 0)
        if result.get("chunks_found", 0) == 0 or (
            result.get("summaries_added", 0) == 0 and result.get("failed", 0) == 0
        ):
            break
        if args.once:
            break

    print(json.dumps({"summaries_added": total_added, "failed": total_failed}, indent=2), flush=True)
    return 0


def _cache_backfill(args: argparse.Namespace) -> int:
    from .server import backfill_artifact_cache_from_chunks, init_db, migrate_db

    init_db()
    migrate_db()
    result = backfill_artifact_cache_from_chunks()
    print(json.dumps(result, indent=2), flush=True)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-search-api",
        description="Local semantic code search with Ollama embeddings and SQLite.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Start the FastAPI search server.")
    p_serve.add_argument("--host", default=os.environ.get("CODE_SEARCH_HOST", "127.0.0.1"))
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("CODE_SEARCH_PORT", "5204")))
    p_serve.add_argument("--reload", action="store_true", help="Enable autoreload (dev only).")
    p_serve.add_argument("--log-level", default="info")
    p_serve.set_defaults(func=_serve)

    p_index = sub.add_parser("index", help="Index the configured workspace.")
    p_index.add_argument("--then-summarize", action="store_true", help="Run summary backfill after indexing.")
    p_index.add_argument("--summary-batch", type=int, default=100)
    p_index.set_defaults(func=_index)

    p_sum = sub.add_parser("summarize", help="Backfill summaries for unsummarized chunks.")
    p_sum.add_argument("--batch", type=int, default=100)
    p_sum.add_argument("--project", default=None, help="Only summarize chunks under this project.")
    p_sum.add_argument("--once", action="store_true", help="Run a single batch and stop.")
    p_sum.set_defaults(func=_summarize)

    p_cache = sub.add_parser("cache", help="Manage the content-addressed artifact cache.")
    cache_sub = p_cache.add_subparsers(dest="cache_command", required=True)
    p_cache_backfill = cache_sub.add_parser(
        "backfill",
        help="Populate artifact_cache from existing chunk artifacts.",
    )
    p_cache_backfill.set_defaults(func=_cache_backfill)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
