"""Auth tests for mutating vs read-only endpoints.

Covers the security contract:
- Mutating endpoints fail closed (403) when CODE_SEARCH_API_KEY is unset.
- A valid key supplied via header authenticates mutating endpoints.
- The API key is accepted only via request headers, never a query parameter.
- Read-only endpoints stay open when no key is configured.

These tests never touch the production database. They point CODE_SEARCH_DB at a
throwaway file under a temp dir before the server module is imported.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

API_KEY = "test-secret-key"


@pytest.fixture()
def server_module():
    """Import the server with a throwaway DB and a fresh module state."""
    tmpdir = tempfile.mkdtemp(prefix="code-search-test-")
    db_path = Path(tmpdir) / "test_index.db"
    prev_db = os.environ.get("CODE_SEARCH_DB")
    os.environ["CODE_SEARCH_DB"] = str(db_path)

    # Drop any cached import so DB_PATH picks up the throwaway path.
    sys.modules.pop("code_search_api.server", None)
    module = importlib.import_module("code_search_api.server")

    try:
        yield module
    finally:
        sys.modules.pop("code_search_api.server", None)
        if prev_db is None:
            os.environ.pop("CODE_SEARCH_DB", None)
        else:
            os.environ["CODE_SEARCH_DB"] = prev_db


def _client(module) -> TestClient:
    return TestClient(module.app)


def test_mutating_route_unset_key_returns_403(server_module):
    """With no key configured, POST /api/index must fail closed with 403."""
    server_module.CODE_SEARCH_API_KEY = None
    with _client(server_module) as client:
        resp = client.post("/api/index")
    assert resp.status_code == 403
    assert "CODE_SEARCH_API_KEY" in resp.json()["detail"]


def test_mutating_route_unset_key_backfill_returns_403(server_module):
    """The same fail-closed behavior applies to POST /api/backfill-summaries."""
    server_module.CODE_SEARCH_API_KEY = None
    with _client(server_module) as client:
        resp = client.post("/api/backfill-summaries")
    assert resp.status_code == 403


def test_mutating_route_header_auth_passes(server_module):
    """A valid X-API-Key header authenticates a mutating endpoint.

    backfill-summaries runs against the empty throwaway DB, so a passing auth
    check yields a normal 200 (no chunks found), not a 401/403.
    """
    server_module.CODE_SEARCH_API_KEY = API_KEY
    with _client(server_module) as client:
        resp = client.post(
            "/api/backfill-summaries", headers={"X-API-Key": API_KEY}
        )
    assert resp.status_code not in (401, 403)


def test_mutating_route_bearer_auth_passes(server_module):
    """Authorization: Bearer <key> also authenticates a mutating endpoint."""
    server_module.CODE_SEARCH_API_KEY = API_KEY
    with _client(server_module) as client:
        resp = client.post(
            "/api/backfill-summaries",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
    assert resp.status_code not in (401, 403)


def test_mutating_route_wrong_key_returns_401(server_module):
    """A configured key with a wrong supplied value returns 401."""
    server_module.CODE_SEARCH_API_KEY = API_KEY
    with _client(server_module) as client:
        resp = client.post(
            "/api/backfill-summaries", headers={"X-API-Key": "wrong"}
        )
    assert resp.status_code == 401


def test_query_param_key_is_not_accepted(server_module):
    """The key must not be accepted via query parameter (URL/log leak)."""
    server_module.CODE_SEARCH_API_KEY = API_KEY
    with _client(server_module) as client:
        resp = client.post(f"/api/backfill-summaries?token={API_KEY}")
        resp_named = client.post(f"/api/backfill-summaries?api_key={API_KEY}")
    assert resp.status_code == 401
    assert resp_named.status_code == 401


def test_readonly_route_open_when_key_unset(server_module):
    """Read-only endpoints stay open when no key is configured."""
    server_module.CODE_SEARCH_API_KEY = None
    with _client(server_module) as client:
        resp = client.get("/api/projects")
    # Auth must not block; the handler returns its normal payload.
    assert resp.status_code == 200
    assert "projects" in resp.json()


def test_readonly_route_requires_key_when_configured(server_module):
    """When a key is set, read-only protected routes still validate it."""
    server_module.CODE_SEARCH_API_KEY = API_KEY
    with _client(server_module) as client:
        no_key = client.get("/api/projects")
        good_key = client.get("/api/projects", headers={"X-API-Key": API_KEY})
    assert no_key.status_code == 401
    assert good_key.status_code == 200
