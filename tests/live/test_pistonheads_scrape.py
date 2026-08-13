import pytest

from models.enums import ListingSource, ListingType
from pistonheads import (
    _SEARCH_RESULTS_LINK_SELECTOR,
    _apply_search_filters,
    _open_session,
    _snapshot_listing_candidates,
    extract_listing_details,
    generate_hash_code,
    goto_with_captcha_handling,
    is_listing_no_longer_available,
    pause_for_page,
    wait_for_listing_detail_page,
)
from tests.live.helpers import (
    MAX_LISTINGS_TO_TRY,
    assert_expected_prospect_fields,
    live_session,
    require_locator,
    require_proxy,
)


@pytest.mark.live
def test_pistonheads_search_and_listing_elements():
    """
    Load PistonHeads private classified search results, visit a live listing,
    and check that card/detail locators and extracted fields still match the scraper.
    """

    require_proxy()

    with live_session(_open_session) as page:
        search_url = _apply_search_filters(page)
        assert "pistonheads.com" in search_url

        require_locator(
            page,
            _SEARCH_RESULTS_LINK_SELECTOR,
            description="PistonHeads search listing links",
        )

        candidates = _snapshot_listing_candidates(page)
        assert candidates, "search results contained no listing links"

        last_error = None
        tried = 0
        for _card_id, listing_url, source_id, title in candidates:
            if tried >= MAX_LISTINGS_TO_TRY:
                break
            if not source_id:
                continue
            tried += 1

            goto_with_captcha_handling(page, listing_url)
            wait_for_listing_detail_page(page)
            pause_for_page(page)
            if is_listing_no_longer_available(page):
                continue

            require_locator(page, "h1", description="PistonHeads listing title")

            hash_code = generate_hash_code(f"{title}|{source_id}")
            try:
                listing, image_urls = extract_listing_details(
                    page, hash_code, source_id, fallback_title=title
                )
            except Exception as error:
                last_error = error
                continue

            assert_expected_prospect_fields(
                listing,
                source=ListingSource.PISTONHEADS,
                listing_type=ListingType.CLASSIC,
            )
            assert listing.source_id == source_id
            assert isinstance(image_urls, list)
            if listing.asking_price is not None:
                assert listing.currency_symbol == "£"
            return

        detail = f" last error: {last_error}" if last_error else ""
        pytest.fail(
            "could not extract a live PistonHeads listing from search cards."
            f"{detail}"
        )
