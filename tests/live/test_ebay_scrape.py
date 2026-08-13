from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from common import generate_hash_code
from ebay import (
    EBAY_VANS_CATEGORY_ID,
    EbayDownloader,
    EbayScrapeConfig,
    build_prospect_listing,
    extract_source_id,
    launch_stealth_chromium,
    sync_stealth_playwright,
)
from models.enums import ListingSource, ListingType
from tests.live.helpers import (
    MAX_LISTINGS_TO_TRY,
    assert_expected_prospect_fields,
)


@pytest.mark.live
def test_ebay_search_and_listing_elements():
    """
    Search the eBay Browse API, visit a live listing page, and check that API
    records plus page-extracted fields still match the scraper.
    """

    config = EbayScrapeConfig(
        listing_type=ListingType.VAN,
        ai_prompt_filename="van_prompt.md",
        query="van",
        category_id=EBAY_VANS_CATEGORY_ID,
        pickup_postal_code="LS1 3AD",
        pickup_radius=100,
        limit=10,
    )
    downloader = EbayDownloader.from_config(config)

    records = []
    for record in downloader.search_listings():
        records.append(record)
        if len(records) >= 10:
            break

    assert records, "eBay Browse API returned no van listings"
    sample = records[0]
    assert sample.get("title"), "Browse API record missing title"
    assert sample.get("item_id"), "Browse API record missing item_id"
    assert sample.get("item_web_url"), "Browse API record missing item_web_url"
    assert sample.get("price", {}).get("value"), "Browse API record missing price"

    last_error = None
    tried = 0
    with sync_stealth_playwright() as playwright:
        browser = launch_stealth_chromium(playwright, headless=True, use_proxy=False)
        try:
            downloader._page = browser.new_page()
            with patch("ebay.pause", return_value=None):
                for listing in records:
                    if tried >= MAX_LISTINGS_TO_TRY:
                        break
                    url = listing.get("item_web_url")
                    title = listing.get("title") or ""
                    if not url:
                        continue
                    tried += 1
                    try:
                        details, image_urls, make_and_model = downloader._visit_listing_page(
                            url,
                            title=title,
                        )
                    except ValueError as error:
                        last_error = error
                        continue
                    except Exception as error:
                        last_error = error
                        continue

                    assert make_and_model
                    assert isinstance(details, dict)
                    prospect = build_prospect_listing(
                        listing=listing,
                        details=details,
                        listing_type=ListingType.VAN,
                        hash_code=generate_hash_code(title),
                        make_and_model=make_and_model,
                        current_datetime=datetime.now(timezone.utc),
                    )
                    assert_expected_prospect_fields(
                        prospect,
                        source=ListingSource.EBAY,
                        listing_type=ListingType.VAN,
                    )
                    assert prospect.source_id == (
                        extract_source_id(url) or listing.get("item_id")
                    )
                    assert isinstance(image_urls, list)
                    return
        finally:
            try:
                browser.close()
            except Exception:
                pass

    detail = f" last error: {last_error}" if last_error else ""
    pytest.fail(
        "could not extract a live private eBay van listing from Browse API "
        f"results.{detail}"
    )
