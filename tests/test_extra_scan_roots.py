"""Extra scan root parsing, namespacing, and prune scoping tests."""

from __future__ import annotations

import importlib
import os
import sys
from contextlib import closing
from pathlib import Path

import pytest


@pytest.fixture()
def make_server_module(tmp_path):
    """Factory fixture: build the server module with a given env overlay."""
    created = []

    env_keys = (
        "CODE_SEARCH_DB",
        "CODE_SEARCH_WORKSPACE",
        "CODE_SEARCH_EXTRA_SCAN_ROOTS",
        "CODE_SEARCH_EMBED_MODEL",
        "CODE_SEARCH_SUMMARY_MODEL",
        "CODE_SEARCH_SUMMARY_FALLBACK",
        "CODE_SEARCH_ALLOW_MODEL_CHANGE",
    )
    prev_env = {key: os.environ.get(key) for key in env_keys}

    def _make(workspace: str, extra_roots: str):
        os.environ["CODE_SEARCH_DB"] = str(tmp_path / "test_index.db")
        os.environ["CODE_SEARCH_WORKSPACE"] = workspace
        os.environ["CODE_SEARCH_EXTRA_SCAN_ROOTS"] = extra_roots
        os.environ["CODE_SEARCH_EMBED_MODEL"] = "test-embed"
        os.environ["CODE_SEARCH_SUMMARY_MODEL"] = "test-summary"
        os.environ["CODE_SEARCH_SUMMARY_FALLBACK"] = "test-summary-fallback"
        os.environ.pop("CODE_SEARCH_ALLOW_MODEL_CHANGE", None)

        sys.modules.pop("code_search_api.server", None)
        module = importlib.import_module("code_search_api.server")
        module.init_db()
        module.migrate_db()
        created.append(module)
        return module

    try:
        yield _make
    finally:
        sys.modules.pop("code_search_api.server", None)
        for key, value in prev_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _install_fake_embed(monkeypatch, module):
    monkeypatch.setattr(module, "embed_text", lambda text: [1.0, 1.0])


def _write(base: Path, rel: str, content: str) -> Path:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_parse_extra_scan_roots_entries(make_server_module, tmp_path):
    module = make_server_module(
        str(tmp_path / "ws"),
        f"repos={tmp_path / 'farm'}{os.pathsep}{tmp_path / 'bare'}{os.pathsep}bad id!={tmp_path / 'odd'}",
    )
    roots = module._parse_extra_scan_roots()
    assert roots == [
        ("repos", tmp_path / "farm"),
        ("bare", tmp_path / "bare"),
        ("bad-id", tmp_path / "odd"),
    ]


def test_extra_root_projects_are_namespaced(make_server_module, monkeypatch, tmp_path):
    farm = tmp_path / "farm"
    _write(farm, "proj/app.py", "def farmed():\n    return 1\n")
    ws = tmp_path / "ws"
    _write(ws, "plain/app.py", "def plain():\n    return 1\n")

    module = make_server_module(str(ws), f"repos={farm}")
    _install_fake_embed(monkeypatch, module)
    result = module.perform_index(summarize=False)

    assert result["files_new"] == 2
    with closing(module.get_conn()) as conn:
        rows = {
            (row["project"], row["file_path"])
            for row in conn.execute("SELECT project, file_path FROM chunks").fetchall()
        }
    assert ("repos/proj", "repos/proj/app.py") in rows
    assert ("plain", "plain/app.py") in rows


def test_empty_workspace_scans_only_extra_roots(make_server_module, monkeypatch, tmp_path):
    farm = tmp_path / "farm"
    _write(farm, "proj/app.py", "def only_farm():\n    return 1\n")

    module = make_server_module("", f"repos={farm}")
    assert module.WORKSPACE is None
    _install_fake_embed(monkeypatch, module)
    result = module.perform_index(summarize=False)

    assert result["files_new"] == 1
    assert [project for project, _ in module.scan_project_dirs()] == ["repos/proj"]


def test_prune_scope_covers_namespaced_projects_only(make_server_module, monkeypatch, tmp_path):
    farm = tmp_path / "farm"
    doomed = _write(farm, "proj/gone.py", "def doomed():\n    return 1\n")

    module = make_server_module("", f"repos={farm}")
    _install_fake_embed(monkeypatch, module)
    module.perform_index(summarize=False)

    # Rows under the same root id but for a project dir that is not on disk
    # are out of scope and must survive the prune.
    with closing(module.get_conn()) as conn:
        conn.execute(
            "INSERT INTO chunks (file_path, project, chunk_index, content, content_hash, created_at) "
            "VALUES ('repos/other/keep.py', 'repos/other', 0, 'x', 'hash', 0)"
        )
        conn.commit()

    doomed.unlink()
    result = module.perform_index(summarize=False)

    assert result["cleanup"]["orphan_files_count"] == 1
    with closing(module.get_conn()) as conn:
        remaining = {
            row["file_path"]
            for row in conn.execute("SELECT file_path FROM chunks").fetchall()
        }
    assert remaining == {"repos/other/keep.py"}
