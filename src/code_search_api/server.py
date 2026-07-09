"""Code Search API — local semantic code search with Ollama and SQLite."""
import hashlib
import json
import os
import re
import sqlite3
import struct
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from math import sqrt
from pathlib import Path
from typing import Any, Optional

import numpy as np
import httpx
from threading import Lock

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Config
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("CODE_SEARCH_DB", "./code_index.db")).expanduser()
WORKSPACE = Path(os.environ.get("CODE_SEARCH_WORKSPACE", "./repos")).expanduser()
_reference_dir = os.environ.get("CODE_SEARCH_REFERENCE")
REFERENCE_DIR = Path(_reference_dir).expanduser() if _reference_dir else None
OLLAMA_BASE = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_EMBED_URL = f"{OLLAMA_BASE}/api/embed"
EMBED_MODEL = os.environ.get("CODE_SEARCH_EMBED_MODEL", "qwen3-embedding:8b")

# Ollama models for summaries. Non-thinking models only: the chat call caps
# num_predict at 200, and a thinking model spends that budget on reasoning
# tokens and returns empty content (qwen3-coder-next is also being retired).
SUMMARY_MODEL_PRIMARY = os.environ.get("CODE_SEARCH_SUMMARY_MODEL", "gemma4:31b-cloud")
SUMMARY_MODEL_FALLBACK = os.environ.get("CODE_SEARCH_SUMMARY_FALLBACK", "devstral-2:123b-cloud")

# Parallel summary config
SUMMARY_WORKERS = int(os.environ.get("CODE_SEARCH_SUMMARY_WORKERS", "4"))
DB_BATCH_SIZE = int(os.environ.get("CODE_SEARCH_DB_BATCH_SIZE", "100"))
CACHE_TTL_SECONDS = int(os.environ.get("CODE_SEARCH_CACHE_TTL_SECONDS", "3600"))
ALLOWED_ORIGINS = [origin.strip() for origin in os.environ.get("CODE_SEARCH_CORS_ORIGINS", "*").split(",") if origin.strip()]
CODE_SEARCH_API_KEY = os.environ.get("CODE_SEARCH_API_KEY")
ARTIFACT_KINDS = {"embedding", "summary", "summary_embedding"}
EMBED_MODEL_META_KEY = "embed_model"

index_lock = Lock()
index_job_status: dict[str, Any] = {
    "status": "idle",
    "message": "No indexing job has run yet",
    "started_at": None,
    "finished_at": None,
    "last_result": None,
}
MAX_CHUNK_CHARS = 2000
SKIP_DIRS = {"node_modules", ".git", "dist", "__pycache__", "build", ".next", ".astro", "coverage", ".turbo"}
INDEX_EXTENSIONS = {
    ".ts", ".tsx", ".py", ".astro", ".js", ".jsx", ".md", ".mdx", ".rst",
    ".css", ".html", ".json", ".sh", ".yaml", ".yml", ".toml",
}
SKIP_FILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", ".env", ".env.local"}
MAX_FILE_SIZE = 100_000  # 100KB

# Search weights
CODE_WEIGHT = 0.35
SUMMARY_WEIGHT = 0.65

# Runtime caches to speed repeated semantic queries (query embedding cache only)
query_embed_cache: OrderedDict[str, list[float]] = OrderedDict()
query_cache_time: dict[str, float] = {}
QUERY_CACHE_MAX = 256


def _evict_stale_cache_entry(cache: OrderedDict, cache_time: dict, key: Any) -> None:
    cache.pop(key, None)
    cache_time.pop(key, None)


def clear_embedding_caches() -> None:
    """Clear query embedding cache (Ollama API call cache)."""
    query_embed_cache.clear()
    query_cache_time.clear()


def _cache_get_query_embedding(query: str) -> Optional[list[float]]:
    cached_at = query_cache_time.get(query)
    if cached_at is not None and (time.time() - cached_at) > CACHE_TTL_SECONDS:
        _evict_stale_cache_entry(query_embed_cache, query_cache_time, query)
        return None

    val = query_embed_cache.get(query)
    if val is not None:
        query_embed_cache.move_to_end(query)
        query_cache_time[query] = time.time()
    return val


def _cache_set_query_embedding(query: str, emb: list[float]) -> None:
    query_embed_cache[query] = emb
    query_cache_time[query] = time.time()
    query_embed_cache.move_to_end(query)
    if len(query_embed_cache) > QUERY_CACHE_MAX:
        old_key, _ = query_embed_cache.popitem(last=False)
        query_cache_time.pop(old_key, None)


# ─── Database ───────────────────────────────────────────────────────────────

def _sqlite_cosine_sim(blob_a: bytes | None, blob_b: bytes | None) -> float | None:
    """SQLite custom function: cosine similarity between two packed float32 BLOBs.
    Uses numpy for fast vector operations via np.frombuffer (zero-copy on the blob)."""
    if blob_a is None or blob_b is None:
        return None
    if len(blob_a) == 0 or len(blob_a) != len(blob_b):
        return 0.0
    a = np.frombuffer(blob_a, dtype=np.float32)
    b = np.frombuffer(blob_b, dtype=np.float32)
    dot = np.dot(a, b)
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(dot / (na * nb))


def get_conn() -> sqlite3.Connection:
    # timeout: concurrent writers (index job + summary backfill) must wait for
    # the WAL write lock instead of failing with "database is locked".
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.create_function("cosine_sim", 2, _sqlite_cosine_sim, deterministic=True)
    return conn


def init_db() -> None:
    with closing(get_conn()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                project TEXT NOT NULL,
                chunk_index INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                embedding BLOB,
                summary TEXT,
                summary_embedding BLOB,
                chunk_type TEXT DEFAULT 'block',
                summary_model TEXT,
                created_at REAL NOT NULL,
                UNIQUE(file_path, chunk_index)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(content_hash)")
        conn.commit()


def migrate_db() -> None:
    """Add new columns if they don't exist (safe for existing DBs)."""
    with closing(get_conn()) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
        if "summary" not in cols:
            conn.execute("ALTER TABLE chunks ADD COLUMN summary TEXT")
        if "summary_embedding" not in cols:
            conn.execute("ALTER TABLE chunks ADD COLUMN summary_embedding BLOB")
        if "chunk_type" not in cols:
            conn.execute("ALTER TABLE chunks ADD COLUMN chunk_type TEXT DEFAULT 'block'")
        if "summary_model" not in cols:
            conn.execute("ALTER TABLE chunks ADD COLUMN summary_model TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS artifact_cache (
                content_hash TEXT NOT NULL,
                model TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('embedding', 'summary', 'summary_embedding')),
                value BLOB NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (content_hash, model, kind)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                file_path TEXT PRIMARY KEY,
                file_hash TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                indexed_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()


def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO meta (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


def _allow_embed_model_change() -> bool:
    return os.environ.get("CODE_SEARCH_ALLOW_MODEL_CHANGE") == "1"


def _ensure_embed_model(conn: sqlite3.Connection) -> bool:
    """Stamp the DB's embedding model and refuse accidental model changes."""
    stored_model = _get_meta(conn, EMBED_MODEL_META_KEY)
    if stored_model is None:
        _set_meta(conn, EMBED_MODEL_META_KEY, EMBED_MODEL)
        return False
    if stored_model == EMBED_MODEL:
        return False
    if not _allow_embed_model_change():
        raise RuntimeError(
            "Refusing to index with configured embed model "
            f"'{EMBED_MODEL}' because the database has stored embed model "
            f"'{stored_model}'. Set CODE_SEARCH_ALLOW_MODEL_CHANGE=1 to "
            "re-stamp the database and re-index with the configured model."
        )
    _set_meta(conn, EMBED_MODEL_META_KEY, EMBED_MODEL)
    return True


def _file_hash_from_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _upsert_file_metadata(
    conn: sqlite3.Connection,
    file_path: str,
    file_hash: str,
    size: int,
    mtime: float,
) -> None:
    conn.execute(
        """
        INSERT INTO files (file_path, file_hash, size, mtime, indexed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(file_path) DO UPDATE SET
            file_hash=excluded.file_hash,
            size=excluded.size,
            mtime=excluded.mtime,
            indexed_at=excluded.indexed_at
        """,
        (file_path, file_hash, size, mtime, time.time()),
    )


def _artifact_cache_get(
    conn: sqlite3.Connection,
    content_hash: str,
    model: str,
    kind: str,
) -> bytes | None:
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"Unknown artifact kind: {kind}")
    row = conn.execute(
        "SELECT value FROM artifact_cache WHERE content_hash = ? AND model = ? AND kind = ?",
        (content_hash, model, kind),
    ).fetchone()
    return row["value"] if row else None


def _artifact_cache_upsert(
    conn: sqlite3.Connection,
    content_hash: str,
    model: str,
    kind: str,
    value: bytes,
) -> None:
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"Unknown artifact kind: {kind}")
    conn.execute(
        """
        INSERT INTO artifact_cache (content_hash, model, kind, value, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(content_hash, model, kind) DO UPDATE SET
            value=excluded.value,
            created_at=excluded.created_at
        """,
        (content_hash, model, kind, value, time.time()),
    )


def _summary_model_candidates() -> list[str]:
    return list(dict.fromkeys([SUMMARY_MODEL_PRIMARY, SUMMARY_MODEL_FALLBACK]))


def _get_cached_summary(
    conn: sqlite3.Connection,
    content_hash: str,
) -> tuple[str, str] | None:
    for model in _summary_model_candidates():
        value = _artifact_cache_get(conn, content_hash, model, "summary")
        if value is not None:
            return (value.decode("utf-8"), model)
    return None


def _embedding_blob_for_content(
    conn: sqlite3.Connection,
    content_hash: str,
    content: str,
    kind: str,
) -> bytes | None:
    cached = _artifact_cache_get(conn, content_hash, EMBED_MODEL, kind)
    if cached is not None:
        return cached

    emb = embed_text(content)
    if not emb:
        return None

    emb_blob = pack_embedding(emb)
    _artifact_cache_upsert(conn, content_hash, EMBED_MODEL, kind, emb_blob)
    return emb_blob


def _summary_artifacts_for_content(
    conn: sqlite3.Connection,
    content_hash: str,
    content: str,
    file_path: str,
) -> tuple[str, bytes | None, str] | None:
    cached = _get_cached_summary(conn, content_hash)
    if cached is not None:
        summary, provider = cached
    else:
        result = summarize_chunk(content, file_path)
        if not result:
            return None
        summary, provider = result
        _artifact_cache_upsert(conn, content_hash, provider, "summary", summary.encode("utf-8"))

    sum_emb_blob = _embedding_blob_for_content(conn, content_hash, summary, "summary_embedding")
    return (summary, sum_emb_blob, provider)


def backfill_artifact_cache_from_chunks() -> dict[str, int]:
    """Populate artifact_cache from existing chunk artifact columns."""
    counts = {"embedding": 0, "summary": 0, "summary_embedding": 0}
    with closing(get_conn()) as conn:
        rows = conn.execute(
            """
            SELECT content_hash, embedding, summary, summary_embedding, summary_model
            FROM chunks
            WHERE embedding IS NOT NULL
               OR summary IS NOT NULL
               OR summary_embedding IS NOT NULL
            """
        ).fetchall()

        for row in rows:
            if row["embedding"] is not None:
                cursor = conn.execute(
                    """
                    INSERT INTO artifact_cache (content_hash, model, kind, value, created_at)
                    VALUES (?, ?, 'embedding', ?, ?)
                    ON CONFLICT(content_hash, model, kind) DO NOTHING
                    """,
                    (row["content_hash"], EMBED_MODEL, row["embedding"], time.time()),
                )
                counts["embedding"] += cursor.rowcount

            if row["summary"] is not None:
                summary_model = row["summary_model"] or SUMMARY_MODEL_PRIMARY
                cursor = conn.execute(
                    """
                    INSERT INTO artifact_cache (content_hash, model, kind, value, created_at)
                    VALUES (?, ?, 'summary', ?, ?)
                    ON CONFLICT(content_hash, model, kind) DO NOTHING
                    """,
                    (
                        row["content_hash"],
                        summary_model,
                        row["summary"].encode("utf-8"),
                        time.time(),
                    ),
                )
                counts["summary"] += cursor.rowcount

            if row["summary_embedding"] is not None:
                cursor = conn.execute(
                    """
                    INSERT INTO artifact_cache (content_hash, model, kind, value, created_at)
                    VALUES (?, ?, 'summary_embedding', ?, ?)
                    ON CONFLICT(content_hash, model, kind) DO NOTHING
                    """,
                    (row["content_hash"], EMBED_MODEL, row["summary_embedding"], time.time()),
                )
                counts["summary_embedding"] += cursor.rowcount

        conn.commit()

    counts["total"] = sum(counts.values())
    return counts


# ─── Ollama helpers ─────────────────────────────────────────────────────────

def embed_text(text: str) -> list[float] | None:
    try:
        resp = httpx.post(OLLAMA_EMBED_URL, json={"model": EMBED_MODEL, "input": text}, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        # /api/embed returns {"embeddings": [[...]]}
        embeddings = data.get("embeddings")
        if embeddings and len(embeddings) > 0:
            return embeddings[0]
        # Fallback for /api/embeddings format
        return data.get("embedding")
    except Exception:
        return None


def _build_summary_prompt(content: str, file_path: str) -> str:
    """Build the summary prompt for a chunk."""
    ext = Path(file_path).suffix
    lang = {
        ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript/React",
        ".js": "JavaScript", ".jsx": "JavaScript/React", ".astro": "Astro",
        ".css": "CSS", ".html": "HTML", ".sh": "Bash", ".md": "Markdown",
        ".json": "JSON config", ".yaml": "YAML config", ".yml": "YAML config",
        ".toml": "TOML config",
    }.get(ext, "code")

    truncated = content[:3000] if len(content) > 3000 else content
    return f"""You are indexing code for a semantic search engine. Write a 1-2 sentence summary that would help a developer FIND this code when searching.

Focus on: what it DOES (not what it contains), key function/class/component names, technologies used, and the problem it solves.
Bad: "This file contains CSS styles for card layouts."
Good: "Styles the SOC project threat actor cards with animated flow diagrams and responsive grid layout."

File: `{file_path}` ({lang})

```
{truncated}
```

Summary (1-2 sentences, no markdown):"""


def _summarize_via_ollama_model(prompt: str, model: str | None = None) -> str | None:
    """Summarize via the configured Ollama chat model."""
    model = model or SUMMARY_MODEL_PRIMARY
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"num_predict": 200, "temperature": 0.3},
            },
            timeout=90.0,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("message", {}).get("content", "").strip()
        return content if content else None
    except Exception as e:
        print(f"Ollama summarization failed for {model}: {e}")
        return None


def _summarize_via_ollama_local(prompt: str) -> str | None:
    """Summarize via a local Ollama model."""
    resp = httpx.post(
        f"{OLLAMA_BASE}/api/generate",
        json={
            "model": SUMMARY_MODEL_PRIMARY,
            "prompt": prompt + "\n\nSummary:",
            "stream": False,
            "options": {"num_predict": 150, "temperature": 0.3},
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def _truncate_summary(summary: str) -> str:
    """Truncate summary to 250 chars at sentence boundary."""
    if len(summary) > 250:
        dot_pos = summary.rfind(".", 0, 250)
        if dot_pos > 50:
            return summary[:dot_pos + 1]
        return summary[:250].rstrip() + "..."
    return summary


def summarize_chunk(content: str, file_path: str) -> tuple[str, str] | None:
    """Generate a 1-2 sentence summary using configured Ollama models with fallback.
    Returns (summary, model_name) or None."""
    prompt = _build_summary_prompt(content, file_path)

        # Try the primary model first, then fall back if needed.
    for model in [SUMMARY_MODEL_PRIMARY, SUMMARY_MODEL_FALLBACK]:
        try:
            summary = _summarize_via_ollama_model(prompt, model)
            if summary:
                return (_truncate_summary(summary), model)
        except Exception as e:
            print(f"{model} failed for {file_path}: {e}")

    return None


def pack_embedding(emb: list[float]) -> bytes:
    return struct.pack(f"{len(emb)}f", *emb)


def unpack_embedding(data: bytes) -> list[float]:
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sqrt(sum(x * x for x in a))
    nb = sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ─── Code-aware chunking ───────────────────────────────────────────────────

# Patterns for splitting at logical boundaries
PY_BOUNDARY = re.compile(r'^(class |def |async def )', re.MULTILINE)
TS_BOUNDARY = re.compile(
    r'^(export |function |class |const \w+ ?= ?\(|const \w+ ?= ?async|interface |type |enum )',
    re.MULTILINE,
)
GENERIC_BOUNDARY = re.compile(r'^(#{1,3} )', re.MULTILINE)  # Markdown headers


def detect_chunk_type(content: str, ext: str) -> str:
    """Classify what kind of code block this is."""
    stripped = content.strip()
    if ext in (".py",):
        if stripped.startswith("class "):
            return "class"
        if stripped.startswith(("def ", "async def ")):
            return "function"
        if stripped.startswith(("import ", "from ")):
            return "imports"
    elif ext in (".ts", ".tsx", ".js", ".jsx"):
        if "class " in stripped[:50]:
            return "class"
        if any(kw in stripped[:80] for kw in ("function ", "=> {", "=> (", "const ", "export default function")):
            return "function"
        if stripped.startswith(("import ", "export {")):
            return "imports"
        if "interface " in stripped[:50] or "type " in stripped[:50]:
            return "type"
    elif ext in (".md",):
        return "documentation"
    elif ext in (".json", ".yaml", ".yml", ".toml"):
        return "config"
    elif ext in (".css",):
        return "styles"
    elif ext in (".html", ".astro"):
        return "template"
    elif ext in (".sh",):
        return "script"
    return "block"


def split_at_boundaries(content: str, ext: str) -> list[tuple[str, str]]:
    """Split content at language-aware boundaries. Returns [(chunk_content, chunk_type)]."""
    if ext in (".py",):
        pattern = PY_BOUNDARY
    elif ext in (".ts", ".tsx", ".js", ".jsx"):
        pattern = TS_BOUNDARY
    elif ext in (".md",):
        pattern = GENERIC_BOUNDARY
    else:
        # Non-code files: don't try to split semantically
        return [(content, detect_chunk_type(content, ext))]

    # Find all boundary positions
    matches = list(pattern.finditer(content))

    if not matches:
        return [(content, detect_chunk_type(content, ext))]

    chunks = []

    # Content before first boundary (imports, module-level code)
    if matches[0].start() > 0:
        preamble = content[: matches[0].start()].rstrip()
        if preamble.strip():
            chunks.append((preamble, detect_chunk_type(preamble, ext)))

    # Each boundary to the next
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        chunk = content[start:end].rstrip()
        if chunk.strip():
            chunks.append((chunk, detect_chunk_type(chunk, ext)))

    return chunks


def chunk_file(content: str, file_path: str) -> list[tuple[str, str]]:
    """Split file into chunks with type detection. Returns [(chunk_content, chunk_type)]."""
    ext = Path(file_path).suffix
    header = f"File: {file_path}\n\n"

    # Small files stay whole
    if len(content) <= MAX_CHUNK_CHARS:
        ctype = detect_chunk_type(content, ext)
        return [(header + content, ctype)]

    # Try language-aware splitting
    raw_chunks = split_at_boundaries(content, ext)

    # Post-process: merge tiny chunks, split huge ones
    result = []
    for chunk_content, chunk_type in raw_chunks:
        full = (header if not result else f"File: {file_path} (continued)\n\n") + chunk_content

        if len(full) <= MAX_CHUNK_CHARS:
            result.append((full, chunk_type))
        else:
            # Chunk is too big, fall back to line splitting
            lines = chunk_content.split("\n")
            current = header if not result else f"File: {file_path} (continued)\n\n"
            for line in lines:
                if len(current) + len(line) + 1 > MAX_CHUNK_CHARS and current.strip():
                    result.append((current, chunk_type))
                    current = f"File: {file_path} (continued)\n\n"
                current += line + "\n"
            if current.strip():
                result.append((current, chunk_type))

    # Merge very small adjacent chunks of the same type
    merged = []
    for chunk_content, chunk_type in result:
        if merged and len(merged[-1][0]) + len(chunk_content) <= MAX_CHUNK_CHARS and merged[-1][1] == chunk_type:
            merged[-1] = (merged[-1][0] + "\n" + chunk_content, chunk_type)
        else:
            merged.append((chunk_content, chunk_type))

    return merged if merged else [(header + content[:MAX_CHUNK_CHARS], "block")]


# ─── File collection ────────────────────────────────────────────────────────

def should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")


def scan_project_dirs() -> list[tuple[str, Path, Path]]:
    """Returns list of (project, project_dir, base_dir) under the scan roots."""
    projects = []
    scan_roots = [WORKSPACE]
    if REFERENCE_DIR and REFERENCE_DIR.exists():
        scan_roots.append(REFERENCE_DIR)
    for base_dir in scan_roots:
        if not base_dir.exists():
            continue
        for project_dir in sorted(base_dir.iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith("."):
                continue
            projects.append((project_dir.name, project_dir, base_dir))
    return projects


def collect_files() -> list[tuple[str, str, str]]:
    """Returns list of (project, relative_path, absolute_path)."""
    files = []
    for project, project_dir, base_dir in scan_project_dirs():
            for root, dirs, filenames in os.walk(project_dir):
                dirs[:] = [d for d in dirs if not should_skip_dir(d)]
                for fname in filenames:
                    if fname in SKIP_FILES:
                        continue
                    fpath = Path(root) / fname
                    if fpath.suffix not in INDEX_EXTENSIONS:
                        continue
                    try:
                        fsize = fpath.stat().st_size
                    except OSError:
                        continue  # broken symlink or permission error
                    if fsize > MAX_FILE_SIZE:
                        continue
                    rel = str(fpath.relative_to(base_dir))
                    files.append((project, rel, str(fpath)))
    return files


# ─── FastAPI app ────────────────────────────────────────────────────────────

app = FastAPI(title="Code Search API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)




def _supplied_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str | None:
    """Extract the API key from request headers only.

    Accepts ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``. The key is
    never read from a query parameter, which would leak it into URLs and logs.
    """
    if x_api_key:
        return x_api_key
    if authorization:
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() == "bearer" and credentials:
            return credentials.strip()
    return None


def require_api_key(supplied: str | None = Depends(_supplied_api_key)) -> None:
    """Auth for read-only endpoints: open when no key is configured."""
    if not CODE_SEARCH_API_KEY:
        return
    if supplied != CODE_SEARCH_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_api_key_strict(supplied: str | None = Depends(_supplied_api_key)) -> None:
    """Auth for mutating endpoints: fail closed when no key is configured."""
    if not CODE_SEARCH_API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Set CODE_SEARCH_API_KEY to use mutating endpoints",
        )
    if supplied != CODE_SEARCH_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


protected_api = APIRouter(dependencies=[Depends(require_api_key)])
mutating_api = APIRouter(dependencies=[Depends(require_api_key_strict)])


def _index_job_already_running_response() -> dict[str, Any]:
    return {
        "status": "indexing",
        "message": "Indexing already running in background",
        "job": index_job_status,
    }


def _run_index_job(summarize: bool) -> None:
    try:
        index_job_status.update({
            "status": "indexing",
            "message": "Indexing started in background",
            "started_at": time.time(),
            "finished_at": None,
        })
        result = perform_index(summarize=summarize)
        index_job_status.update({
            "status": "completed",
            "message": "Indexing completed",
            "finished_at": time.time(),
            "last_result": result,
        })
    except Exception as exc:
        index_job_status.update({
            "status": "failed",
            "message": f"Indexing failed: {exc}",
            "finished_at": time.time(),
            "last_result": None,
        })
    finally:
        index_lock.release()


@app.on_event("startup")
def startup():
    try:
        init_db()
        migrate_db()
        if not CODE_SEARCH_API_KEY:
            print("WARNING: CODE_SEARCH_API_KEY is not set. Allowing unauthenticated requests for backwards compatibility.")
    except Exception as e:
        print(f"Startup DB init failed: {e}")


@app.get("/health")
def health_alias():
    return health()

@app.get("/api/health")
def health():
    try:
        with closing(get_conn()) as conn:
            count = conn.execute("SELECT COUNT(*) as c FROM chunks").fetchone()["c"]
            embedded = conn.execute("SELECT COUNT(*) as c FROM chunks WHERE embedding IS NOT NULL").fetchone()["c"]
            summarized = conn.execute("SELECT COUNT(*) as c FROM chunks WHERE summary IS NOT NULL").fetchone()["c"]
            summary_embedded = conn.execute(
                "SELECT COUNT(*) as c FROM chunks WHERE summary_embedding IS NOT NULL"
            ).fetchone()["c"]
            file_count = conn.execute("SELECT COUNT(*) as c FROM files").fetchone()["c"]
            stored_embed_model = _get_meta(conn, EMBED_MODEL_META_KEY)
        return {
            "status": "ok",
            "version": "2.0.1",
            "chunks": count,
            "files": file_count,
            "embedded": embedded,
            "summarized": summarized,
            "summary_embedded": summary_embedded,
            "stored_embed_model": stored_embed_model,
            "configured_embed_model": EMBED_MODEL,
            "query_cache_size": len(query_embed_cache),
        }
    except Exception as e:
        return {"status": "degraded", "version": "2.0.1", "error": str(e)}


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=50)
    min_score: float = Field(default=0.3, ge=0.0, le=1.0)
    project: Optional[str] = None
    mode: str = Field(default="hybrid", pattern="^(hybrid|code|summary)$")


@protected_api.post("/api/search")
def search(payload: SearchRequest) -> dict[str, Any]:
    """Semantic search with hybrid code + summary scoring (computed in SQLite)."""
    query_emb = _cache_get_query_embedding(payload.query)
    if query_emb is None:
        query_emb = embed_text(payload.query)
        if query_emb is None:
            raise HTTPException(status_code=503, detail="Ollama unavailable for embeddings")
        _cache_set_query_embedding(payload.query, query_emb)

    query_blob = pack_embedding(query_emb)

    with closing(get_conn()) as conn:
        where_clauses = ["embedding IS NOT NULL"]
        params: dict[str, Any] = {"qblob": query_blob}
        if payload.project:
            where_clauses.append("project = :project")
            params["project"] = payload.project

        where = " AND ".join(where_clauses)

        # Build final score expression from pre-computed CTE columns
        if payload.mode == "code":
            final_score = "cs"
        elif payload.mode == "summary":
            final_score = "COALESCE(ss, 0.0)"
        else:
            # hybrid: weighted combination
            final_score = f"{CODE_WEIGHT} * cs + {SUMMARY_WEIGHT} * COALESCE(ss, 0.0)"

        params["min_score"] = payload.min_score
        params["lim"] = payload.limit

        # CTE computes cosine_sim once per row; outer query filters/sorts on derived score
        sql = f"""
            WITH scored AS (
                SELECT
                    file_path, project, chunk_index, chunk_type, summary,
                    substr(content, 1, 500) as content_preview,
                    cosine_sim(embedding, :qblob) as cs,
                    cosine_sim(summary_embedding, :qblob) as ss
                FROM chunks
                WHERE {where}
            )
            SELECT *, ({final_score}) as score
            FROM scored
            WHERE ({final_score}) >= :min_score
            ORDER BY ({final_score}) DESC
            LIMIT :lim
        """

        rows = conn.execute(sql, params).fetchall()

    results = []
    for row in rows:
        results.append({
            "score": round(row["score"], 4) if row["score"] is not None else 0.0,
            "code_score": round(row["cs"], 4) if row["cs"] is not None else 0.0,
            "summary_score": round(row["ss"], 4) if row["ss"] is not None else None,
            "file_path": row["file_path"],
            "project": row["project"],
            "chunk_index": row["chunk_index"],
            "chunk_type": row["chunk_type"] or "block",
            "summary": row["summary"],
            "content": row["content_preview"],
        })

    return {"results": results, "total_matches": len(results), "mode": payload.mode}


def perform_index(summarize: bool = True) -> dict[str, Any]:
    """Crawl all repos, index with code-aware chunking + optional LLM summaries."""
    files = collect_files()
    new_chunks = 0
    skipped = 0
    files_new = 0
    files_changed = 0
    files_skipped = 0
    files_refreshed = 0
    embedded = 0
    summarized = 0
    failed = 0
    model_changed = False
    t0 = time.time()

    with closing(get_conn()) as conn:
        model_changed = _ensure_embed_model(conn)
        conn.commit()

        existing = {
            (row["file_path"], row["chunk_index"]): row["content_hash"]
            for row in conn.execute("SELECT file_path, chunk_index, content_hash FROM chunks").fetchall()
        }
        tracked_files = {
            row["file_path"]: row
            for row in conn.execute(
                "SELECT file_path, file_hash, size, mtime, indexed_at FROM files"
            ).fetchall()
        }

        current_file_paths = {rel_path for _, rel_path, _ in files}
        orphan_chunks_removed = 0
        orphan_files_count = 0
        stale_tail_chunks_pruned = 0

        all_db_files = {row[0] for row in conn.execute("SELECT DISTINCT file_path FROM chunks").fetchall()}
        all_db_files.update(tracked_files.keys())
        # Prune only within project directories present in this scan. A scan is
        # scoped by CODE_SEARCH_WORKSPACE; files from projects outside the
        # scanned roots are not "deleted", they are simply out of scope, and
        # removing them would wipe the rest of the index whenever the
        # workspace changes. Directories (not collected files) define the
        # scope so a project whose files were all deleted still prunes.
        scanned_projects = {project for project, _, _ in scan_project_dirs()}
        deleted_files = {
            path
            for path in all_db_files - current_file_paths
            if path.split("/", 1)[0] in scanned_projects
        }
        for deleted_file in deleted_files:
            cursor = conn.execute("DELETE FROM chunks WHERE file_path = ?", (deleted_file,))
            orphan_chunks_removed += cursor.rowcount
            conn.execute("DELETE FROM files WHERE file_path = ?", (deleted_file,))
            orphan_files_count += 1
        conn.commit()

        pending_summaries = []
        pending_upserts: list[tuple[Any, ...]] = []

        def flush_upserts() -> None:
            nonlocal new_chunks
            if not pending_upserts:
                return
            conn.executemany(
                """
                INSERT INTO chunks (file_path, project, chunk_index, content, content_hash,
                                   embedding, summary, summary_embedding, chunk_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                ON CONFLICT(file_path, chunk_index) DO UPDATE SET
                    content=excluded.content, content_hash=excluded.content_hash,
                    embedding=excluded.embedding,
                    summary=NULL, summary_embedding=NULL, summary_model=NULL,
                    chunk_type=excluded.chunk_type, created_at=excluded.created_at
                """,
                pending_upserts,
            )
            conn.commit()
            new_chunks += len(pending_upserts)
            pending_upserts.clear()

        for project, rel_path, abs_path in files:
            path = Path(abs_path)
            try:
                stat = path.stat()
            except OSError:
                continue

            stored_file = tracked_files.get(rel_path)
            if (
                not model_changed
                and stored_file
                and stored_file["size"] == stat.st_size
                and stored_file["mtime"] == stat.st_mtime
            ):
                files_skipped += 1
                continue

            try:
                data = path.read_bytes()
            except Exception:
                continue

            file_hash = _file_hash_from_bytes(data)
            if not model_changed and stored_file and stored_file["file_hash"] == file_hash:
                _upsert_file_metadata(conn, rel_path, file_hash, stat.st_size, stat.st_mtime)
                conn.commit()
                files_skipped += 1
                files_refreshed += 1
                continue

            if stored_file:
                files_changed += 1
            else:
                files_new += 1

            content = data.decode("utf-8", errors="replace")
            chunks = chunk_file(content, rel_path)
            cursor = conn.execute(
                "DELETE FROM chunks WHERE file_path = ? AND chunk_index >= ?",
                (rel_path, len(chunks)),
            )
            stale_tail_chunks_pruned += cursor.rowcount

            for i, (chunk_content, chunk_type) in enumerate(chunks):
                chunk_hash = hashlib.md5(chunk_content.encode()).hexdigest()
                key = (rel_path, i)

                if not model_changed and key in existing and existing[key] == chunk_hash:
                    skipped += 1
                    continue

                emb_blob = _embedding_blob_for_content(conn, chunk_hash, chunk_content, "embedding")
                pending_upserts.append((
                    rel_path,
                    project,
                    i,
                    chunk_content,
                    chunk_hash,
                    emb_blob,
                    chunk_type,
                    time.time(),
                ))

                if len(pending_upserts) >= DB_BATCH_SIZE:
                    flush_upserts()

                if emb_blob:
                    embedded += 1
                    if summarize:
                        pending_summaries.append((rel_path, i, chunk_hash, chunk_content))
                else:
                    failed += 1

            _upsert_file_metadata(conn, rel_path, file_hash, stat.st_size, stat.st_mtime)

        flush_upserts()
        conn.commit()

        if summarize and pending_summaries:
            print(f"Pass 2: Summarizing {len(pending_summaries)} chunks with {SUMMARY_WORKERS} workers...")

            def _summarize_and_embed(item):
                rel_path, chunk_idx, chunk_hash, chunk_content = item
                try:
                    with closing(get_conn()) as worker_conn:
                        result = _summary_artifacts_for_content(
                            worker_conn, chunk_hash, chunk_content, rel_path
                        )
                        worker_conn.commit()
                    if result:
                        summary, sum_emb_blob, provider = result
                        return (summary, sum_emb_blob, provider, rel_path, chunk_idx)
                except Exception as e:
                    print(f"Summary failed for {rel_path}[{chunk_idx}]: {e}")
                return None

            pending_summary_updates: list[tuple[Any, ...]] = []

            def flush_summary_updates() -> None:
                nonlocal summarized
                if not pending_summary_updates:
                    return
                conn.executemany(
                    "UPDATE chunks SET summary = ?, summary_embedding = ?, summary_model = ? WHERE file_path = ? AND chunk_index = ?",
                    pending_summary_updates,
                )
                conn.commit()
                summarized += len(pending_summary_updates)
                pending_summary_updates.clear()

            with ThreadPoolExecutor(max_workers=SUMMARY_WORKERS) as executor:
                futures = {executor.submit(_summarize_and_embed, item): item for item in pending_summaries}
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        pending_summary_updates.append(result)
                        if len(pending_summary_updates) >= DB_BATCH_SIZE:
                            flush_summary_updates()
                    else:
                        failed += 1

            flush_summary_updates()

    print(
        f"Files: {files_new} new, {files_changed} changed, "
        f"{files_skipped} skipped ({files_refreshed} metadata refreshed)"
    )
    print(
        f"Cleanup: removed {orphan_chunks_removed} orphan chunks from {orphan_files_count} deleted files, "
        f"{stale_tail_chunks_pruned} stale tail chunks"
    )

    duration = round(time.time() - t0, 1)
    clear_embedding_caches()
    return {
        "files_found": len(files),
        "files_new": files_new,
        "files_changed": files_changed,
        "files_skipped": files_skipped,
        "files_refreshed": files_refreshed,
        "model_changed": model_changed,
        "new_chunks": new_chunks,
        "skipped_unchanged": skipped,
        "embedded": embedded,
        "summarized": summarized,
        "failed": failed,
        "duration_seconds": duration,
        "cleanup": {
            "orphan_chunks_removed": orphan_chunks_removed,
            "orphan_files_count": orphan_files_count,
            "stale_tail_chunks_pruned": stale_tail_chunks_pruned,
        },
    }


@mutating_api.post("/api/index")
def index_all(background_tasks: BackgroundTasks, summarize: bool = True) -> dict[str, Any]:
    if not index_lock.acquire(blocking=False):
        return _index_job_already_running_response()

    # Once the background task is scheduled, _run_index_job releases the lock in
    # its finally block. But if anything between acquiring the lock and handing
    # off to the background task raises, that finally never runs, so release the
    # lock here to avoid leaking it forever.
    try:
        index_job_status.update({
            "status": "indexing",
            "message": "Indexing started in background",
            "started_at": time.time(),
            "finished_at": None,
            "last_result": None,
        })
        background_tasks.add_task(_run_index_job, summarize)
    except BaseException:
        index_lock.release()
        raise

    return {
        "status": "indexing",
        "message": "Indexing started in background",
        "job": index_job_status,
    }


@mutating_api.post("/api/backfill-summaries")
def backfill_summaries(limit: int = 100, project: Optional[str] = None) -> dict[str, Any]:
    """Backfill summaries for chunks that have code embeddings but no summary yet."""
    with closing(get_conn()) as conn:
        where = "WHERE embedding IS NOT NULL AND summary IS NULL"
        params: list[Any] = []
        if project:
            where += " AND project = ?"
            params.append(project)
        params.append(limit)

        rows = conn.execute(
            f"SELECT id, file_path, content, content_hash FROM chunks {where} LIMIT ?", params
        ).fetchall()

        updated = 0
        failed = 0
        t0 = time.time()

        def _backfill_one(row):
            try:
                with closing(get_conn()) as worker_conn:
                    result = _summary_artifacts_for_content(
                        worker_conn,
                        row["content_hash"],
                        row["content"],
                        row["file_path"],
                    )
                    worker_conn.commit()
                if result:
                    summary, sum_emb_blob, provider = result
                    return (summary, sum_emb_blob, provider, row["id"])
            except Exception as e:
                print(f"Backfill failed for {row['file_path']}: {e}")
            return None

        pending_updates: list[tuple[Any, ...]] = []

        def flush_updates() -> None:
            nonlocal updated
            if not pending_updates:
                return
            conn.executemany(
                "UPDATE chunks SET summary = ?, summary_embedding = ?, summary_model = ? WHERE id = ?",
                pending_updates,
            )
            conn.commit()
            updated += len(pending_updates)
            pending_updates.clear()

        with ThreadPoolExecutor(max_workers=SUMMARY_WORKERS) as executor:
            futures = {executor.submit(_backfill_one, dict(row)): row for row in rows}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    pending_updates.append(result)
                    if len(pending_updates) >= DB_BATCH_SIZE:
                        flush_updates()
                else:
                    failed += 1

        flush_updates()

    clear_embedding_caches()
    duration = round(time.time() - t0, 1)
    return {
        "chunks_found": len(rows),
        "summaries_added": updated,
        "failed": failed,
        "duration_seconds": duration,
    }




def _summarize_via_ollama_model_with_metrics(prompt: str, model: str, num_predict: int = 500) -> tuple[str | None, float]:
    """Summarize via a specific Ollama model. Returns (summary, duration_ms)."""
    t0 = time.time()
    try:
        import json
        resp = httpx.post(
            f"{OLLAMA_BASE}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"num_predict": num_predict, "temperature": 0.3},
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        
        # Some Ollama setups return NDJSON-style chunks even with stream=False
        # Accumulate all content parts defensively
        content_parts = []
        for line in resp.text.strip().split('\n'):
            if line:
                try:
                    chunk = json.loads(line)
                    msg = chunk.get("message", {})
                    # Skip thinking content, only use final content
                    text = msg.get("content", "")
                    if text and not msg.get("thinking"):
                        content_parts.append(text)
                except json.JSONDecodeError:
                    continue
        
        full_content = "".join(content_parts).strip()
        duration_ms = round((time.time() - t0) * 1000, 1)
        return (full_content, duration_ms)
    except Exception as e:
        duration_ms = round((time.time() - t0) * 1000, 1)
        return (f"ERROR: {e}", duration_ms)


@protected_api.get("/api/projects")
def list_projects() -> dict[str, Any]:
    with closing(get_conn()) as conn:
        rows = conn.execute(
            """SELECT project,
                      COUNT(*) as chunks,
                      COUNT(embedding) as embedded,
                      COUNT(summary) as summarized
               FROM chunks GROUP BY project ORDER BY project"""
        ).fetchall()
    return {"projects": [dict(r) for r in rows]}


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    """Detailed stats about chunk types and coverage."""
    with closing(get_conn()) as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM chunks").fetchone()["c"]
        by_type = conn.execute(
            "SELECT chunk_type, COUNT(*) as c FROM chunks GROUP BY chunk_type ORDER BY c DESC"
        ).fetchall()
        by_project = conn.execute(
            """SELECT project,
                      COUNT(*) as total,
                      COUNT(summary) as summarized,
                      ROUND(100.0 * COUNT(summary) / COUNT(*), 1) as pct
               FROM chunks GROUP BY project ORDER BY project"""
        ).fetchall()
    return {
        "total_chunks": total,
        "by_type": {r["chunk_type"] or "block": r["c"] for r in by_type},
        "by_project": [dict(r) for r in by_project],
    }


@protected_api.get("/api/summary-stats")
def summary_stats() -> dict[str, Any]:
    """Stats on which models produced summaries."""
    with closing(get_conn()) as conn:
        by_model = conn.execute(
            "SELECT summary_model, COUNT(*) as c FROM chunks WHERE summary IS NOT NULL GROUP BY summary_model ORDER BY c DESC"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) as c FROM chunks").fetchone()["c"]
        summarized = conn.execute("SELECT COUNT(*) as c FROM chunks WHERE summary IS NOT NULL").fetchone()["c"]
        pending = total - summarized
    return {
        "total_chunks": total,
        "summarized": summarized,
        "pending": pending,
        "by_model": {(r["summary_model"] or "unknown"): r["c"] for r in by_model},
    }


app.include_router(protected_api)
app.include_router(mutating_api)
