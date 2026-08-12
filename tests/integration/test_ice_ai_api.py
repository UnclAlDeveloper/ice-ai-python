import hashlib
import json
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def api_client(monkeypatch):
    """Async HTTP client for the FastAPI app with MCP session managers disabled."""

    monkeypatch.setattr("ice_ai_api.mcp_session_managers", [])
    from ice_ai_api import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


class TestHealthEndpoints:
    """Smoke tests for basic API availability."""

    @pytest.mark.asyncio
    async def test_health_returns_healthy(self, api_client):
        response = await api_client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy", "service": "ice-ai-api"}

    @pytest.mark.asyncio
    async def test_root_lists_endpoints(self, api_client):
        response = await api_client.get("/")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Ice AI API"
        assert "/health" in body["endpoints"]


class TestEbayMarketplaceDeletion:
    """Tests for eBay marketplace account deletion compliance endpoints."""

    @pytest.mark.asyncio
    async def test_challenge_returns_expected_hash(self, api_client, monkeypatch):
        challenge_code = "test-challenge-123"
        verification_token = "verify-token-abc"
        endpoint = "https://api.example.com/autoads-ebay-marketplace-account-deletion"

        monkeypatch.setenv("AUTO_ADS_EBAY_VERIFICATION_TOKEN", verification_token)
        monkeypatch.setenv("AUTO_ADS_EBAY_DELETION_ENDPOINT", endpoint)

        expected = hashlib.sha256(
            (challenge_code + verification_token + endpoint).encode("utf-8")
        ).hexdigest()

        response = await api_client.get(
            "/autoads-ebay-marketplace-account-deletion",
            params={"challenge_code": challenge_code},
        )

        assert response.status_code == 200
        assert response.json() == {"challengeResponse": expected}

    @pytest.mark.asyncio
    async def test_challenge_missing_code_returns_400(self, api_client):
        response = await api_client.get("/autoads-ebay-marketplace-account-deletion")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_challenge_missing_token_returns_500(self, api_client, monkeypatch):
        monkeypatch.delenv("AUTO_ADS_EBAY_VERIFICATION_TOKEN", raising=False)
        response = await api_client.get(
            "/autoads-ebay-marketplace-account-deletion",
            params={"challenge_code": "abc"},
        )
        assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_notification_acknowledges_payload(self, api_client):
        payload = {
            "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION"},
            "notification": {
                "notificationId": "notif-123",
                "eventDate": "2026-08-12T00:00:00.000Z",
                "data": {
                    "username": "seller1",
                    "userId": "user-1",
                    "eiasToken": "token-1",
                },
            },
        }

        response = await api_client.post(
            "/autoads-ebay-marketplace-account-deletion",
            json=payload,
        )

        assert response.status_code == 200
        assert response.json() == {
            "status": "acknowledged",
            "notificationId": "notif-123",
        }


class TestMcpBearerAuth:
    """Tests for MCP bearer middleware via the mounted app."""

    @pytest.mark.asyncio
    async def test_rejects_unauthenticated_mcp_request(self, api_client):
        response = await api_client.get("/mcp/synthesia/")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_accepts_valid_bearer_token(self, api_client, monkeypatch):
        monkeypatch.setenv(
            "MCP_TOKENS_JSON",
            json.dumps({"test-client": "valid-token"}),
        )

        with patch("ice_ai_api.mcp_token_store") as mock_store:
            mock_store.authenticate.return_value = "test-client"
            response = await api_client.get(
                "/mcp/synthesia/",
                headers={"Authorization": "Bearer valid-token"},
            )

        # mounted MCP app may return 404/405 for GET, but auth must not be 401
        assert response.status_code != 401
