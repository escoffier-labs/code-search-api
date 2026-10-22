"""Summary provider adapter and daily cap tests. No real model is called."""

from __future__ import annotations

import importlib
import json
import os
import sys
import types

import pytest

from code_search_api import summary_provider


def test_parse_batch_response_aligns_by_id_and_tolerates_noise():
    text = 'sure\n[{"id": 2, "summary": "Second."}, {"id": 1, "summary": " First\\n one. "}, {"id": 9, "summary": "x"}]\n'
    assert summary_provider.parse_batch_response(text, 3) == ["First one.", "Second.", None]


@pytest.mark.parametrize("text", ["", "no json here", "[not json]", '{"id": 1}'])
def test_parse_batch_response_bad_input_returns_nones(text):
    assert summary_provider.parse_batch_response(text, 2) == [None, None]


def test_build_batch_prompt_numbers_items_and_truncates():
    prompt = summary_provider.build_batch_prompt([("a.py", "x" * 5000), ("b.ts", "const b = 1")])
    assert "### ITEM 1: `a.py` (Python)" in prompt
    assert "### ITEM 2: `b.ts` (TypeScript)" in prompt
    assert "x" * summary_provider.MAX_PROMPT_CHUNK_CHARS in prompt
    assert "x" * (summary_provider.MAX_PROMPT_CHUNK_CHARS + 1) not in prompt


def test_ollama_cloud_models_are_rejected():
    with pytest.raises(summary_provider.SummaryProviderError):
        summary_provider.summarize_items("ollama", "example-model:cloud", [("a.py", "x = 1")])


def test_agy_provider_runs_one_subprocess_for_the_batch(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        reply = json.dumps([{"id": 1, "summary": "One."}, {"id": 2, "summary": "Two."}])
        return types.SimpleNamespace(returncode=0, stdout=reply, stderr="")

    monkeypatch.setattr(summary_provider.subprocess, "run", fake_run)
    out = summary_provider.summarize_items("agy", "gemini-3.8-flash-low", [("a.py", "a"), ("b.py", "b")])
    assert out == ["One.", "Two."]
    assert len(calls) == 1 and calls[0][0] == "agy" and "--model" in calls[0]


def test_cli_failure_raises(monkeypatch):
    monkeypatch.setattr(
        summary_provider.subprocess,
        "run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(summary_provider.SummaryProviderError):
        summary_provider.summarize_items("codex", "gpt-6-luna", [("a.py", "a")])


@pytest.fixture()
def server_module(tmp_path):
    keys = (
        "CODE_SEARCH_DB", "CODE_SEARCH_WORKSPACE", "CODE_SEARCH_SUMMARY_PROVIDER",
        "CODE_SEARCH_SUMMARY_MODEL", "CODE_SEARCH_SUMMARY_FALLBACK",
        "CODE_SEARCH_SUMMARY_DAILY_CAP", "CODE_SEARCH_SUMMARY_BATCH_SIZE",
    )
    prev = {k: os.environ.get(k) for k in keys}
    os.environ["CODE_SEARCH_DB"] = str(tmp_path / "t.db")
    os.environ["CODE_SEARCH_WORKSPACE"] = str(tmp_path / "repos")
    (tmp_path / "repos").mkdir()
    os.environ["CODE_SEARCH_SUMMARY_PROVIDER"] = "agy"
    os.environ["CODE_SEARCH_SUMMARY_MODEL"] = "test-model"
    os.environ.pop("CODE_SEARCH_SUMMARY_FALLBACK", None)
    os.environ["CODE_SEARCH_SUMMARY_DAILY_CAP"] = "5"
    os.environ["CODE_SEARCH_SUMMARY_BATCH_SIZE"] = "2"
    sys.modules.pop("code_search_api.server", None)
    module = importlib.import_module("code_search_api.server")
    module.SUMMARY_WORKERS = 1
    module.init_db()
    module.migrate_db()
    try:
        yield module
    finally:
        sys.modules.pop("code_search_api.server", None)
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _fake_provider(monkeypatch, module, calls):
    def fake(provider, model, items, **kwargs):
        calls.append((provider, model, len(items)))
        return [f"summary of {path}" for path, _ in items]

    monkeypatch.setattr(module.summary_provider, "summarize_items", fake)


def test_prefetch_batches_and_fills_cache(server_module, monkeypatch):
    calls = []
    _fake_provider(monkeypatch, server_module, calls)
    items = [(f"h{i}", f"code {i}", f"f{i}.py") for i in range(3)]
    server_module.prefetch_summaries(items)
    assert sorted(c[2] for c in calls) == [1, 2]
    with server_module.closing(server_module.get_conn()) as conn:
        assert server_module._get_cached_summary(conn, "h2") == ("summary of f2.py", "test-model")
        assert server_module.summary_usage_today(conn) == 3
    # second pass is fully cached, no new calls and no new usage
    server_module.prefetch_summaries(items)
    assert len(calls) == 2


def test_daily_cap_stops_calls(server_module, monkeypatch):
    calls = []
    _fake_provider(monkeypatch, server_module, calls)
    items = [(f"h{i}", f"code {i}", f"f{i}.py") for i in range(8)]
    server_module.prefetch_summaries(items)
    with server_module.closing(server_module.get_conn()) as conn:
        assert server_module.summary_usage_today(conn) == 5
        assert server_module.summary_quota_remaining(conn) == 0
    assert sum(c[2] for c in calls) == 5
    assert server_module.summarize_chunk("x = 1", "z.py") is None
    result = server_module.backfill_summaries(limit=10)
    assert result["cap_reached"] is True and result["chunks_found"] == 0


def test_provider_none_disables_generation(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "SUMMARY_PROVIDER", "none")
    calls = []
    _fake_provider(monkeypatch, server_module, calls)
    assert server_module.summarize_chunk("x = 1", "z.py") is None
    assert calls == []


def test_fallback_model_covers_failed_primary(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "SUMMARY_MODEL_FALLBACK", "backup-model")

    def fake(provider, model, items, **kwargs):
        if model == "test-model":
            raise summary_provider.SummaryProviderError("primary down")
        return ["ok"] * len(items)

    monkeypatch.setattr(server_module.summary_provider, "summarize_items", fake)
    assert server_module.summarize_chunk("x = 1", "z.py") == ("ok", "backup-model")
