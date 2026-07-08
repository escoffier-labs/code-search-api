"""Artifact cache tests for indexing and CLI backfill."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest


@pytest.fixture()
def server_module(tmp_path):
    db_path = tmp_path / "test_index.db"
    workspace = tmp_path / "repos"
    workspace.mkdir()

    prev_env = {
        key: os.environ.get(key)
        for key in (
            "CODE_SEARCH_DB",
            "CODE_SEARCH_WORKSPACE",
            "CODE_SEARCH_EMBED_MODEL",
            "CODE_SEARCH_SUMMARY_MODEL",
            "CODE_SEARCH_SUMMARY_FALLBACK",
        )
    }
    os.environ["CODE_SEARCH_DB"] = str(db_path)
    os.environ["CODE_SEARCH_WORKSPACE"] = str(workspace)
    os.environ["CODE_SEARCH_EMBED_MODEL"] = "test-embed"
    os.environ["CODE_SEARCH_SUMMARY_MODEL"] = "test-summary"
    os.environ["CODE_SEARCH_SUMMARY_FALLBACK"] = "test-summary-fallback"

    sys.modules.pop("code_search_api.server", None)
    module = importlib.import_module("code_search_api.server")
    module.SUMMARY_WORKERS = 1
    module.init_db()
    module.migrate_db()

    try:
        yield module
    finally:
        sys.modules.pop("code_search_api.server", None)
        for key, value in prev_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write_project_file(module, rel_path: str, content: str) -> Path:
    file_path = module.WORKSPACE / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return file_path


def _chunk_hash(module, rel_path: str, content: str, index: int = 0) -> str:
    chunk_content = module.chunk_file(content, rel_path)[index][0]
    return hashlib.md5(chunk_content.encode()).hexdigest()


def test_perform_index_writes_artifacts_through_to_cache(server_module, monkeypatch):
    content = "def hello():\n    return 'world'\n"
    _write_project_file(server_module, "demo/app.py", content)
    calls = {"embed": [], "summary": []}

    def fake_embed_text(text: str):
        calls["embed"].append(text)
        return [float(len(calls["embed"])), 2.0]

    def fake_summarize_chunk(chunk: str, file_path: str):
        calls["summary"].append((chunk, file_path))
        return ("greets the caller", "test-summary")

    monkeypatch.setattr(server_module, "embed_text", fake_embed_text)
    monkeypatch.setattr(server_module, "summarize_chunk", fake_summarize_chunk)

    result = server_module.perform_index(summarize=True)

    assert result["embedded"] == 1
    assert result["summarized"] == 1
    assert len(calls["embed"]) == 2
    assert len(calls["summary"]) == 1

    content_hash = _chunk_hash(server_module, "demo/app.py", content)
    with closing(server_module.get_conn()) as conn:
        rows = conn.execute(
            "SELECT content_hash, model, kind, value FROM artifact_cache ORDER BY kind"
        ).fetchall()

    assert [(row["content_hash"], row["model"], row["kind"]) for row in rows] == [
        (content_hash, "test-embed", "embedding"),
        (content_hash, "test-summary", "summary"),
        (content_hash, "test-embed", "summary_embedding"),
    ]
    assert rows[0]["value"] == server_module.pack_embedding([1.0, 2.0])
    assert rows[1]["value"] == b"greets the caller"
    assert rows[2]["value"] == server_module.pack_embedding([2.0, 2.0])


def test_perform_index_cache_hit_skips_artifact_calls(server_module, monkeypatch):
    content = "def cached():\n    return 42\n"
    _write_project_file(server_module, "demo/app.py", content)
    content_hash = _chunk_hash(server_module, "demo/app.py", content)

    with closing(server_module.get_conn()) as conn:
        conn.executemany(
            """
            INSERT INTO artifact_cache (content_hash, model, kind, value, created_at)
            VALUES (?, ?, ?, ?, 1.0)
            """,
            [
                (
                    content_hash,
                    "test-embed",
                    "embedding",
                    server_module.pack_embedding([1.0, 0.0]),
                ),
                (content_hash, "test-summary", "summary", b"cached summary"),
                (
                    content_hash,
                    "test-embed",
                    "summary_embedding",
                    server_module.pack_embedding([0.0, 1.0]),
                ),
            ],
        )
        conn.commit()

    def fail_embed_text(text: str):
        raise AssertionError(f"unexpected embed call for {text!r}")

    def fail_summarize_chunk(chunk: str, file_path: str):
        raise AssertionError(f"unexpected summary call for {file_path}")

    monkeypatch.setattr(server_module, "embed_text", fail_embed_text)
    monkeypatch.setattr(server_module, "summarize_chunk", fail_summarize_chunk)

    result = server_module.perform_index(summarize=True)

    assert result["embedded"] == 1
    assert result["summarized"] == 1
    with closing(server_module.get_conn()) as conn:
        row = conn.execute(
            "SELECT embedding, summary, summary_embedding, summary_model FROM chunks"
        ).fetchone()
    assert row["embedding"] == server_module.pack_embedding([1.0, 0.0])
    assert row["summary"] == "cached summary"
    assert row["summary_embedding"] == server_module.pack_embedding([0.0, 1.0])
    assert row["summary_model"] == "test-summary"


def test_chunk_index_shift_reuses_cached_embedding_for_same_content(server_module, monkeypatch):
    preamble = "# preamble\n" + "# filler\n" * 180
    def_a = "def alpha():\n" + "    value = 'a'\n" * 70 + "    return value\n"
    def_b = "def beta():\n" + "    value = 'b'\n" * 70 + "    return value\n"
    initial_content = preamble + "\n" + def_a + "\n" + def_b
    shifted_content = preamble + "\n" + def_b
    rel_path = "demo/app.py"
    _write_project_file(server_module, rel_path, initial_content)

    calls = {"embed": 0}

    def fake_embed_text(text: str):
        calls["embed"] += 1
        return [float(calls["embed"]), 9.0]

    def fake_summarize_chunk(chunk: str, file_path: str):
        raise AssertionError("summaries are disabled in this test")

    monkeypatch.setattr(server_module, "embed_text", fake_embed_text)
    monkeypatch.setattr(server_module, "summarize_chunk", fake_summarize_chunk)
    first = server_module.perform_index(summarize=False)
    assert first["embedded"] == 3
    assert calls["embed"] == 3

    initial_beta_hash = _chunk_hash(server_module, rel_path, initial_content, index=2)
    shifted_beta_hash = _chunk_hash(server_module, rel_path, shifted_content, index=1)
    assert shifted_beta_hash == initial_beta_hash

    _write_project_file(server_module, rel_path, shifted_content)
    calls["embed"] = 0

    second = server_module.perform_index(summarize=False)

    assert second["embedded"] == 1
    assert calls["embed"] == 0
    with closing(server_module.get_conn()) as conn:
        row = conn.execute(
            "SELECT chunk_index, content_hash, embedding FROM chunks WHERE chunk_index = 1"
        ).fetchone()
    assert row["content_hash"] == shifted_beta_hash
    assert row["embedding"] == server_module.pack_embedding([3.0, 9.0])


def test_cache_backfill_cli_populates_cache_from_existing_chunks(
    server_module, capsys
):
    emb = server_module.pack_embedding([1.0, 2.0])
    sum_emb = server_module.pack_embedding([3.0, 4.0])
    with closing(server_module.get_conn()) as conn:
        conn.execute(
            """
            INSERT INTO chunks (
                file_path, project, chunk_index, content, content_hash,
                embedding, summary, summary_embedding, chunk_type,
                summary_model, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "demo/app.py",
                "demo",
                0,
                "File: demo/app.py\n\ndef cached():\n    pass\n",
                "abc123",
                emb,
                "cached summary",
                sum_emb,
                "function",
                "test-summary",
                1.0,
            ),
        )
        conn.commit()

    from code_search_api import cli

    assert cli.main(["cache", "backfill"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "embedding": 1,
        "summary": 1,
        "summary_embedding": 1,
        "total": 3,
    }

    assert cli.main(["cache", "backfill"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total"] == 0

    with closing(server_module.get_conn()) as conn:
        rows = conn.execute(
            "SELECT model, kind, value FROM artifact_cache ORDER BY kind"
        ).fetchall()
    assert [(row["model"], row["kind"]) for row in rows] == [
        ("test-embed", "embedding"),
        ("test-summary", "summary"),
        ("test-embed", "summary_embedding"),
    ]
    assert rows[0]["value"] == emb
    assert rows[1]["value"] == b"cached summary"
    assert rows[2]["value"] == sum_emb
