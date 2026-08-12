from unittest.mock import MagicMock, patch

import pytest

from models.enums import ListingSource, ListingType, ProspectListingStatus
from scraper_driver import (
    is_site_unreachable_error,
    load_listing_with_backoff,
    persist_unavailable_listing_stub,
    with_page_param,
)
from stealth_browser import CaptchaSolveError


class TestWithPageParam:
    """Tests for search-results pagination URL rewriting."""

    def test_page_one_omits_page_parameter(self):
        url = "https://example.com/search?sort=newest&make=ford"
        assert with_page_param(url, 1) == url

    def test_page_two_adds_page_parameter(self):
        url = "https://example.com/search?sort=newest"
        assert with_page_param(url, 2) == "https://example.com/search?sort=newest&page=2"

    def test_replaces_existing_page_parameter(self):
        url = "https://example.com/search?page=3&sort=newest"
        assert with_page_param(url, 5) == "https://example.com/search?sort=newest&page=5"

    def test_preserves_other_query_params(self):
        url = "https://example.com/search?foo=bar&page=2"
        assert with_page_param(url, 1) == "https://example.com/search?foo=bar"


class TestIsSiteUnreachableError:
    """Tests for transport-level failure classification."""

    @pytest.mark.parametrize(
        "message",
        [
            "ERR_NAME_NOT_RESOLVED",
            "ERR_CONNECTION_REFUSED",
            "net::ERR_INTERNET_DISCONNECTED",
        ],
    )
    def test_detects_unreachable_fragments(self, message):
        assert is_site_unreachable_error(RuntimeError(message)) is True

    def test_rejects_unrelated_errors(self):
        assert is_site_unreachable_error(ValueError("element not found")) is False

    @patch("scraper_driver.is_proxy_network_error", return_value=True)
    def test_detects_proxy_network_errors(self, _mock_proxy):
        assert is_site_unreachable_error(RuntimeError("proxy failed")) is True

    @patch("scraper_driver.is_navigation_timeout", return_value=True)
    def test_detects_navigation_timeouts(self, _mock_timeout):
        assert is_site_unreachable_error(RuntimeError("timeout")) is True


class TestLoadListingWithBackoff:
    """Tests for listing navigation retry behaviour."""

    @patch("scraper_driver.time.sleep")
    @patch("scraper_driver.goto_with_captcha_handling")
    def test_retries_unreachable_then_succeeds(self, mock_goto, _mock_sleep, mock_page):
        success_response = MagicMock()
        mock_goto.side_effect = [
            RuntimeError("ERR_CONNECTION_REFUSED"),
            success_response,
        ]

        result = load_listing_with_backoff(
            mock_page, "https://example.com/listing/1", max_retries=3, backoff_seconds=1.0
        )

        assert result is success_response
        assert mock_goto.call_count == 2

    @patch("scraper_driver.goto_with_captcha_handling")
    def test_raises_404_immediately_without_retry(self, mock_goto, mock_page):
        mock_goto.side_effect = RuntimeError("HTTP 404 Not Found")

        with pytest.raises(RuntimeError, match="404"):
            load_listing_with_backoff(
                mock_page, "https://example.com/gone", max_retries=3, backoff_seconds=1.0
            )

        assert mock_goto.call_count == 1

    @patch("scraper_driver.goto_with_captcha_handling")
    def test_propagates_captcha_solve_error(self, mock_goto, mock_page):
        mock_goto.side_effect = CaptchaSolveError("captcha failed")

        with pytest.raises(CaptchaSolveError):
            load_listing_with_backoff(
                mock_page, "https://example.com/listing/1", max_retries=3, backoff_seconds=1.0
            )

    @patch("scraper_driver.time.sleep")
    @patch("scraper_driver.goto_with_captcha_handling")
    def test_exhausted_retries_reraise_last_error(self, mock_goto, _mock_sleep, mock_page):
        error = RuntimeError("ERR_NAME_NOT_RESOLVED")
        mock_goto.side_effect = error

        with pytest.raises(RuntimeError, match="ERR_NAME_NOT_RESOLVED"):
            load_listing_with_backoff(
                mock_page, "https://example.com/listing/1", max_retries=2, backoff_seconds=1.0
            )

        assert mock_goto.call_count == 2


class TestPersistUnavailableListingStub:
    """Tests for minimal NotAvailable prospect row persistence."""

    @patch("scraper_driver.with_db_retry", side_effect=lambda op, **_: op())
    def test_persists_not_available_row(self, _mock_retry, mock_db_session):
        persist_unavailable_listing_stub(
            mock_db_session,
            listing_source=ListingSource.AUTOTRADER,
            listing_type=ListingType.VAN,
            hash_code="abc123",
            source_id="202606113190878",
            url="https://www.autotrader.co.uk/van-details/202606113190878",
            make_and_model="Ford Transit",
            short_description="Ford Transit LWB",
        )

        mock_db_session.add.assert_called_once()
        listing = mock_db_session.add.call_args[0][0]
        assert listing.status == ProspectListingStatus.NOT_AVAILABLE
        assert listing.source_id == "202606113190878"
        assert listing.listing_source == ListingSource.AUTOTRADER
        mock_db_session.commit.assert_called_once()
