from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ai_analysis import process_ai_analysis_for_listing
from autotrader import extract_source_id
from models.enums import ListingSource, ListingType, ProspectListingStatus
from scraper_driver import load_listing_with_backoff, persist_unavailable_listing_stub


SAMPLE_AI_MARKDOWN = """# Overview
Good van.

# Price ranges
**Low buy price:** £10,000
**High buy price:** £12,000
"""


def _run_new_listing_pipeline(
    session,
    *,
    url: str,
    ai_markdown: str,
) -> SimpleNamespace:
    """Simulate a thin Autotrader listing save path with external I/O mocked."""

    source_id = extract_source_id(url)
    listing = SimpleNamespace(
        id=1,
        hash_code="hash123",
        source_id=source_id,
        listing_source=ListingSource.AUTOTRADER,
        listing_type=ListingType.VAN,
        status=ProspectListingStatus.NEW,
        url=url,
        make_and_model="Ford Transit",
        short_description="Ford Transit LWB",
        ai_resell_overview=None,
        ai_work_and_repairs=None,
        ai_resell_notes=None,
        ai_value_add_improvements=None,
        ai_campervan_conversion=None,
        ai_target_market=None,
        ai_buy_price_low=None,
        ai_buy_price_high=None,
        ai_repair_cost=None,
        ai_sell_price_low=None,
        ai_sell_price_high=None,
    )

    session.add(listing)
    session.commit()

    with patch("listing_images.download_and_save_listing_images") as mock_images:
        mock_images.return_value = "/tmp/listing_images_test"
        temp_dir = mock_images(
            ["http://img/1.jpg"],
            MagicMock(),
            listing,
            session,
        )

    with patch("ai_analysis.generate_ai_analysis", return_value=ai_markdown):
        listing = process_ai_analysis_for_listing("van_prompt.md", listing, session, temp_dir)

    return listing, temp_dir


class TestScrapeListingPipeline:
    """Integration-style tests for a mocked Autotrader listing workflow."""

    def test_new_listing_persists_images_and_applies_ai(self, mock_db_session):
        url = "https://www.autotrader.co.uk/van-details/202606113190878"

        listing, temp_dir = _run_new_listing_pipeline(
            mock_db_session,
            url=url,
            ai_markdown=SAMPLE_AI_MARKDOWN,
        )

        assert extract_source_id(url) == "202606113190878"
        assert temp_dir == "/tmp/listing_images_test"
        assert listing.ai_resell_overview == "Good van."
        assert listing.ai_buy_price_low == 10000
        assert listing.ai_buy_price_high == 12000
        mock_db_session.add.assert_called()
        mock_db_session.commit.assert_called()

    @patch("scraper_driver.goto_with_captcha_handling")
    @patch("scraper_driver.time.sleep")
    def test_unreachable_does_not_persist_not_available_stub(
        self, _mock_sleep, mock_goto, mock_db_session, mock_page
    ):
        mock_goto.side_effect = RuntimeError("ERR_CONNECTION_REFUSED")

        with pytest.raises(RuntimeError, match="ERR_CONNECTION_REFUSED"):
            load_listing_with_backoff(
                mock_page,
                "https://www.autotrader.co.uk/van-details/202606113190878",
                max_retries=2,
                backoff_seconds=0.1,
            )

        mock_db_session.add.assert_not_called()

    @patch("scraper_driver.with_db_retry", side_effect=lambda op, **_: op())
    def test_genuine_not_found_persists_unavailable_stub(self, _mock_retry, mock_db_session):
        persist_unavailable_listing_stub(
            mock_db_session,
            listing_source=ListingSource.AUTOTRADER,
            listing_type=ListingType.VAN,
            hash_code="hash-gone",
            source_id="202606113190878",
            url="https://www.autotrader.co.uk/van-details/202606113190878",
            make_and_model="Ford Transit",
        )

        listing = mock_db_session.add.call_args[0][0]
        assert listing.status == ProspectListingStatus.NOT_AVAILABLE
        assert listing.source_id == "202606113190878"

    @patch("scraper_driver.goto_with_captcha_handling")
    def test_not_found_navigation_triggers_stub_not_retry_loop(
        self, mock_goto, mock_db_session, mock_page
    ):
        mock_goto.side_effect = RuntimeError("HTTP 404 Not Found")

        with pytest.raises(RuntimeError, match="404"):
            load_listing_with_backoff(
                mock_page,
                "https://www.autotrader.co.uk/van-details/999",
                max_retries=3,
                backoff_seconds=0.1,
            )

        assert mock_goto.call_count == 1
        mock_db_session.add.assert_not_called()
