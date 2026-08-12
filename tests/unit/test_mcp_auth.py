import json
from unittest.mock import MagicMock, patch

import pytest

from mcp_auth import McpBearerAuthMiddleware, McpTokenStore


class TestMcpTokenStore:
    """Tests for MCP bearer token loading and validation."""

    def test_authenticate_returns_label_for_valid_token(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_TOKENS_JSON",
            json.dumps({"test-client": "secret-token-123"}),
        )
        store = McpTokenStore(cache_seconds=300)

        assert store.authenticate("secret-token-123") == "test-client"
        assert store.authenticate("wrong-token") is None

    def test_invalid_json_shape_raises(self, monkeypatch):
        monkeypatch.setenv("MCP_TOKENS_JSON", json.dumps(["not", "a", "dict"]))
        store = McpTokenStore()

        with pytest.raises(ValueError, match="JSON object"):
            store.refresh(force=True)

    def test_cache_avoids_reload_until_expired(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_TOKENS_JSON",
            json.dumps({"client": "token-a"}),
        )
        store = McpTokenStore(cache_seconds=60)
        store.refresh(force=True)

        with patch.object(store, "_load_from_env_json", wraps=store._load_from_env_json) as mock_load:
            store.authenticate("token-a")
            store.authenticate("token-a")
            mock_load.assert_not_called()

    def test_force_refresh_reloads_tokens(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_TOKENS_JSON",
            json.dumps({"client": "token-a"}),
        )
        store = McpTokenStore(cache_seconds=300)
        store.refresh(force=True)

        monkeypatch.setenv(
            "MCP_TOKENS_JSON",
            json.dumps({"client": "token-b"}),
        )
        store.refresh(force=True)

        assert store.authenticate("token-b") == "client"
        assert store.authenticate("token-a") is None


class FakeRequest:
    """Minimal request object for middleware unit tests."""

    def __init__(self, path: str, headers: dict[str, str]):
        self.url = type("URL", (), {"path": path})()
        self.headers = headers
        self.state = type("State", (), {})()


async def async_noop(_request):
    return type("Response", (), {"status_code": 200})()


class TestMcpAuthMiddleware:
    """Tests for MCP bearer auth middleware in isolation."""

    @pytest.mark.asyncio
    async def test_rejects_missing_authorization(self):
        store = McpTokenStore()
        middleware = McpBearerAuthMiddleware(app=MagicMock(), token_store=store)

        request = FakeRequest(path="/mcp/synthesia/", headers={})
        response = await middleware.dispatch(request, call_next=async_noop)

        assert response.status_code == 401
