from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from common import (
    generate_hash_code,
    is_http_not_found,
    is_not_found_error,
    is_transient_db_error,
    with_db_retry,
)


class TestIsNotFoundError:
    """Tests for navigation 404 classification."""

    @pytest.mark.parametrize(
        "message",
        [
            "HTTP 404 Not Found",
            "http 404 not found",
            "status code: 404",
            "status=404",
            "Page not found",
        ],
    )
    def test_accepts_explicit_404_phrasing(self, message):
        assert is_not_found_error(RuntimeError(message)) is True

    @pytest.mark.parametrize(
        "message",
        [
            "https://example.com/listing/404/details",
            "capsolver task id abc404def failed",
            "error 4040 invalid",
        ],
    )
    def test_rejects_bare_404_in_urls_or_ids(self, message):
        assert is_not_found_error(RuntimeError(message)) is False


class TestIsHttpNotFound:
    """Tests for Playwright response 404 detection."""

    def test_returns_true_for_404_status(self):
        response = MagicMock()
        response.status = 404
        assert is_http_not_found(response) is True

    def test_returns_false_for_other_status(self):
        response = MagicMock()
        response.status = 200
        assert is_http_not_found(response) is False

    def test_returns_false_for_none_response(self):
        assert is_http_not_found(None) is False


class TestIsTransientDbError:
    """Tests for transient database error classification."""

    def test_detects_transient_connection_failure(self):
        exc = OperationalError("SELECT 1", {}, Exception("could not connect to server"))
        assert is_transient_db_error(exc) is True

    def test_rejects_non_operational_error(self):
        assert is_transient_db_error(ValueError("could not connect to server")) is False

    def test_rejects_non_transient_operational_error(self):
        exc = OperationalError("SELECT 1", {}, Exception("syntax error at or near"))
        assert is_transient_db_error(exc) is False


class TestWithDbRetry:
    """Tests for database retry with exponential backoff."""

    def test_retries_transient_then_succeeds(self, monkeypatch):
        attempts = {"count": 0}
        sleeps: list[float] = []
        monkeypatch.setattr("common.time.sleep", lambda s: sleeps.append(s))

        def operation():
            attempts["count"] += 1
            if attempts["count"] < 2:
                raise OperationalError(
                    "stmt", {}, Exception("temporary failure in name resolution")
                )
            return "ok"

        result = with_db_retry(operation, attempts=3, base_delay=1.0)
        assert result == "ok"
        assert attempts["count"] == 2
        assert sleeps == [1.0]

    def test_does_not_retry_non_transient_error(self):
        def operation():
            raise OperationalError("stmt", {}, Exception("syntax error"))

        with pytest.raises(OperationalError):
            with_db_retry(operation, attempts=3)

    def test_exhausts_attempts_and_reraises(self, monkeypatch):
        monkeypatch.setattr("common.time.sleep", lambda _s: None)

        def operation():
            raise OperationalError(
                "stmt", {}, Exception("connection refused")
            )

        with pytest.raises(OperationalError):
            with_db_retry(operation, attempts=2, base_delay=0.5)


class TestGenerateHashCode:
    """Tests for listing hash generation."""

    def test_is_stable_for_same_input(self):
        assert generate_hash_code("Ford Transit 2020") == generate_hash_code(
            "Ford Transit 2020"
        )

    def test_differs_for_different_input(self):
        assert generate_hash_code("Ford Transit") != generate_hash_code("VW Crafter")

    def test_returns_sixteen_character_hex(self):
        result = generate_hash_code("test listing")
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)
