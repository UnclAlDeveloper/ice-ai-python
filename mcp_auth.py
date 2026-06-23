"""Bearer-token authentication for internet-facing MCP endpoints."""

import hmac
import json
import logging
import os
import time
from typing import Callable, Optional

import boto3
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp


logger = logging.getLogger(__name__)

DEFAULT_CACHE_SECONDS = 300


# MCP TOKEN STORE
class McpTokenStore:
    """Loads and caches valid MCP bearer tokens from env or Secrets Manager."""

    def __init__(self, cache_seconds: int = DEFAULT_CACHE_SECONDS):
        self._cache_seconds = cache_seconds
        self._tokens_by_label: dict[str, str] = {}
        self._loaded_at: float = 0.0

    def _load_from_env_json(self) -> Optional[dict[str, str]]:
        """Parse MCP_TOKENS_JSON when set for local development."""

        raw = os.getenv("MCP_TOKENS_JSON", "").strip()
        if not raw:
            return None

        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("MCP_TOKENS_JSON must decode to a JSON object")

        tokens: dict[str, str] = {}
        for label, token in parsed.items():
            if not isinstance(label, str) or not isinstance(token, str):
                raise ValueError("MCP_TOKENS_JSON keys and values must be strings")
            token = token.strip()
            if token:
                tokens[label] = token
        return tokens

    def _load_from_secrets_manager(self) -> dict[str, str]:
        """Fetch the token map from AWS Secrets Manager."""

        secret_name = os.getenv("MCP_TOKENS_SECRET_NAME", "ice-ai/mcp-tokens").strip()
        if not secret_name:
            raise ValueError("MCP_TOKENS_SECRET_NAME is not configured")

        region = os.getenv("AWS_REGION_NAME", os.getenv("AWS_REGION", "eu-west-2"))
        client = boto3.client("secretsmanager", region_name=region)
        response = client.get_secret_value(SecretId=secret_name)
        secret_string = response.get("SecretString", "").strip()
        if not secret_string:
            raise ValueError(f"Secret {secret_name} has no SecretString value")

        parsed = json.loads(secret_string)
        if not isinstance(parsed, dict):
            raise ValueError(f"Secret {secret_name} must contain a JSON object")

        tokens: dict[str, str] = {}
        for label, token in parsed.items():
            if not isinstance(label, str) or not isinstance(token, str):
                raise ValueError(f"Secret {secret_name} keys and values must be strings")
            token = token.strip()
            if token:
                tokens[label] = token
        return tokens

    def refresh(self, force: bool = False) -> None:
        """Reload tokens when the cache has expired or force is true."""

        now = time.monotonic()
        if not force and self._tokens_by_label and (now - self._loaded_at) < self._cache_seconds:
            return

        env_tokens = self._load_from_env_json()
        if env_tokens is not None:
            self._tokens_by_label = env_tokens
        else:
            self._tokens_by_label = self._load_from_secrets_manager()

        self._loaded_at = now
        logger.info("Loaded %s MCP bearer token(s)", len(self._tokens_by_label))

    def authenticate(self, bearer_token: str) -> Optional[str]:
        """Return the token label when bearer_token is valid, otherwise None."""

        self.refresh()
        for label, valid_token in self._tokens_by_label.items():
            if hmac.compare_digest(bearer_token, valid_token):
                return label
        return None


# MCP BEARER AUTH MIDDLEWARE
class McpBearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject unauthenticated requests to /mcp/* using static bearer tokens."""

    def __init__(self, app: ASGIApp, token_store: McpTokenStore):
        super().__init__(app)
        self._token_store = token_store

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not request.url.path.startswith("/mcp/"):
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        bearer_token = auth_header[7:].strip()
        if not bearer_token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        label = self._token_store.authenticate(bearer_token)
        if label is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        request.state.mcp_token_label = label
        return await call_next(request)
