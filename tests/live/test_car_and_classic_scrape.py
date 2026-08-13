import pytest
from stealth_browser import quick_locator_count

from car_and_classic import (
    _SEARCH_RESULTS_CARD_SELECTOR,
    _apply_search_filters,
    _open_session,
    _snapshot_listing_candidates,
    extract_listing_details,
    generate_hash_code,
    goto_with_captcha_handling,
    is_listing_no_longer_available,
    is_title_sold_or_under_offer,
    pause_for_page,
    wait_for_listing_detail_page,
)
from models.enums import ListingSource, ListingType
from tests.live.helpers import (
    MAX_LISTINGS_TO_TRY,
    assert_expected_prospect_fields,
    live_session,
    require_locator,
    require_proxy,
)


@pytest.mark.live
def test_car_and_classic_search_and_listing_elements():
    """
    Load Car & Classic private UK search results, visit a live listing, and
    check that card/detail locators and extracted fields still match the scraper.
    """

    require_proxy()

    with live_session(_open_session) as page:
        search_url = _apply_search_filters(page)
        assert "carandclassic.com" in search_url

        require_locator(
            page,
            _SEARCH_RESULTS_CARD_SELECTOR,
            description="Car & Classic search cards",
        )

        new_vehicles_grid = page.locator("div.lg\\:grid-cols-3.grid.grid-cols-1")
        use_new_section = (
            quick_locator_count(new_vehicles_grid, description="new vehicles grid") > 0
        )
        candidates = _snapshot_listing_candidates(page, use_new_section=use_new_section)
        assert candidates, "search results contained no listing cards"

        last_error = None
        tried = 0
        for _card_id, listing_url, source_id, title in candidates:
            if tried >= MAX_LISTINGS_TO_TRY:
                break
            if not source_id or is_title_sold_or_under_offer(title):
                continue
            tried += 1

            goto_with_captcha_handling(page, listing_url)
            wait_for_listing_detail_page(page)
            pause_for_page(page)
            if is_listing_no_longer_available(page):
                continue

            require_locator(
                page,
                "section h1",
                description="Car & Classic listing title",
            )

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
                source=ListingSource.CAR_AND_CLASSIC,
                listing_type=ListingType.CLASSIC,
            )
            assert listing.source_id == source_id
            assert listing.asking_price is not None
            assert listing.currency_symbol == "£"
            assert isinstance(image_urls, list)
            return

        detail = f" last error: {last_error}" if last_error else ""
        pytest.fail(
            "could not extract a live Car & Classic listing from search cards."
            f"{detail}"
        )
