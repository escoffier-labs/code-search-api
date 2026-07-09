"""Incremental indexing tests for file metadata and embed model guards."""

from __future__ import annotations

import importlib
import os
import sys
from contextlib import closing
from pathlib import Path

import pytest


@pytest.fixture()
def server_module(tmp_path):
    db_path = tmp_path / "test_index.db"
    workspace = tmp_path / "repos"
    workspace.mkdir()

    env_keys = (
        "CODE_SEARCH_DB",
        "CODE_SEARCH_WORKSPACE",
        "CODE_SEARCH_EMBED_MODEL",
        "CODE_SEARCH_SUMMARY_MODEL",
        "CODE_SEARCH_SUMMARY_FALLBACK",
        "CODE_SEARCH_ALLOW_MODEL_CHANGE",
    )
    prev_env = {key: os.environ.get(key) for key in env_keys}
    os.environ["CODE_SEARCH_DB"] = str(db_path)
    os.environ["CODE_SEARCH_WORKSPACE"] = str(workspace)
    os.environ["CODE_SEARCH_EMBED_MODEL"] = "test-embed"
    os.environ["CODE_SEARCH_SUMMARY_MODEL"] = "test-summary"
    os.environ["CODE_SEARCH_SUMMARY_FALLBACK"] = "test-summary-fallback"
    os.environ.pop("CODE_SEARCH_ALLOW_MODEL_CHANGE", None)

    sys.modules.pop("code_search_api.server", None)
    module = importlib.import_module("code_search_api.server")
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


def _install_fake_embed(monkeypatch, module):
    calls: list[str] = []

    def fake_embed_text(text: str):
        calls.append(text)
        return [float(len(calls)), 1.0]

    monkeypatch.setattr(module, "embed_text", fake_embed_text)
    return calls


def test_unchanged_file_skips_second_index_without_chunking(server_module, monkeypatch):
    _write_project_file(server_module, "demo/app.py", "def unchanged():\n    return 1\n")
    _install_fake_embed(monkeypatch, server_module)

    first = server_module.perform_index(summarize=False)
    assert first["files_new"] == 1

    original_chunk_file = server_module.chunk_file
    chunk_calls = {"count": 0}

    def counted_chunk_file(content: str, file_path: str):
        chunk_calls["count"] += 1
        return original_chunk_file(content, file_path)

    monkeypatch.setattr(server_module, "chunk_file", counted_chunk_file)
    second = server_module.perform_index(summarize=False)

    assert second["files_skipped"] == 1
    assert second["files_new"] == 0
    assert second["files_changed"] == 0
    assert second["embedded"] == 0
    assert chunk_calls["count"] == 0


def test_touched_identical_file_refreshes_mtime_without_rechunk(server_module, monkeypatch):
    path = _write_project_file(server_module, "demo/app.py", "def touched():\n    return 1\n")
    _install_fake_embed(monkeypatch, server_module)

    server_module.perform_index(summarize=False)
    with closing(server_module.get_conn()) as conn:
        before = conn.execute(
            "SELECT file_hash, mtime, indexed_at FROM files WHERE file_path = ?",
            ("demo/app.py",),
        ).fetchone()

    new_mtime = path.stat().st_mtime + 10.0
    os.utime(path, (new_mtime, new_mtime))

    original_chunk_file = server_module.chunk_file
    chunk_calls = {"count": 0}

    def counted_chunk_file(content: str, file_path: str):
        chunk_calls["count"] += 1
        return original_chunk_file(content, file_path)

    monkeypatch.setattr(server_module, "chunk_file", counted_chunk_file)
    result = server_module.perform_index(summarize=False)

    with closing(server_module.get_conn()) as conn:
        after = conn.execute(
            "SELECT file_hash, mtime, indexed_at FROM files WHERE file_path = ?",
            ("demo/app.py",),
        ).fetchone()

    assert result["files_skipped"] == 1
    assert result["files_refreshed"] == 1
    assert chunk_calls["count"] == 0
    assert after["file_hash"] == before["file_hash"]
    assert after["mtime"] == path.stat().st_mtime
    assert after["indexed_at"] >= before["indexed_at"]


def test_changed_file_rechunks(server_module, monkeypatch):
    path = _write_project_file(server_module, "demo/app.py", "def changed():\n    return 1\n")
    _install_fake_embed(monkeypatch, server_module)
    server_module.perform_index(summarize=False)

    original_chunk_file = server_module.chunk_file
    chunk_calls = {"count": 0}

    def counted_chunk_file(content: str, file_path: str):
        chunk_calls["count"] += 1
        return original_chunk_file(content, file_path)

    path.write_text("def changed():\n    return 2\n", encoding="utf-8")
    monkeypatch.setattr(server_module, "chunk_file", counted_chunk_file)
    result = server_module.perform_index(summarize=False)

    assert result["files_changed"] == 1
    assert result["files_new"] == 0
    assert result["files_skipped"] == 0
    assert chunk_calls["count"] == 1


def test_deleted_file_prunes_chunks_and_files(server_module, monkeypatch):
    path = _write_project_file(server_module, "demo/app.py", "def doomed():\n    return 1\n")
    _install_fake_embed(monkeypatch, server_module)
    server_module.perform_index(summarize=False)

    path.unlink()
    result = server_module.perform_index(summarize=False)

    assert result["cleanup"]["orphan_files_count"] == 1
    assert result["cleanup"]["orphan_chunks_removed"] == 1
    with closing(server_module.get_conn()) as conn:
        chunk_count = conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"]
        file_count = conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"]
    assert chunk_count == 0
    assert file_count == 0


def test_embed_model_mismatch_refuses_index(server_module, monkeypatch):
    _write_project_file(server_module, "demo/app.py", "def model_guard():\n    return 1\n")
    _install_fake_embed(monkeypatch, server_module)
    server_module.perform_index(summarize=False)

    server_module.EMBED_MODEL = "other-embed"

    with pytest.raises(RuntimeError) as excinfo:
        server_module.perform_index(summarize=False)

    message = str(excinfo.value)
    assert "stored embed model 'test-embed'" in message
    assert "configured embed model 'other-embed'" in message
    assert "CODE_SEARCH_ALLOW_MODEL_CHANGE=1" in message


def test_embed_model_override_restamps_and_proceeds(server_module, monkeypatch):
    _write_project_file(server_module, "demo/app.py", "def model_override():\n    return 1\n")
    _install_fake_embed(monkeypatch, server_module)
    server_module.perform_index(summarize=False)

    server_module.EMBED_MODEL = "other-embed"
    monkeypatch.setenv("CODE_SEARCH_ALLOW_MODEL_CHANGE", "1")
    result = server_module.perform_index(summarize=False)

    assert result["model_changed"] is True
    assert result["embedded"] == 1
    with closing(server_module.get_conn()) as conn:
        stored = conn.execute(
            "SELECT value FROM meta WHERE key = 'embed_model'"
        ).fetchone()["value"]
    assert stored == "other-embed"


def test_workspace_change_does_not_prune_out_of_scope_projects(server_module, monkeypatch, tmp_path):
    """A scan scoped to a different workspace must never delete other projects."""
    _write_project_file(server_module, "demo/app.py", "def keep():\n    return 1\n")
    _install_fake_embed(monkeypatch, server_module)
    server_module.perform_index(summarize=False)

    roster = tmp_path / "roster"
    (roster / "newproj").mkdir(parents=True)
    (roster / "newproj" / "new.py").write_text("def fresh():\n    return 2\n", encoding="utf-8")
    monkeypatch.setattr(server_module, "WORKSPACE", roster)

    result = server_module.perform_index(summarize=False)

    assert result["cleanup"]["orphan_files_count"] == 0
    assert result["cleanup"]["orphan_chunks_removed"] == 0
    with closing(server_module.get_conn()) as conn:
        demo_chunks = conn.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE file_path LIKE 'demo/%'"
        ).fetchone()["c"]
    assert demo_chunks == 1
