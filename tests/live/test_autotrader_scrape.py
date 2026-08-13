import pytest

from autotrader import (
    AUTOTRADER_VANS_URL,
    AutotraderScrapeConfig,
    _SEARCH_RESULTS_LISTING_SELECTOR,
    _open_session,
    configure_search,
    extract_source_id,
    goto_with_captcha_handling,
    is_listing_no_longer_available,
    pause_for_page,
    read_full_prospect_listing,
)
from models.enums import ListingSource, ListingType
from tests.live.helpers import (
    MAX_LISTINGS_TO_TRY,
    assert_expected_prospect_fields,
    live_session,
    patch_autotrader_persist,
    require_locator,
    require_proxy,
)


def _snapshot_search_cards(page) -> list[tuple[str, str, str | None, str]]:
    cards: list[tuple[str, str, str | None, str]] = []
    for list_item in page.locator(_SEARCH_RESULTS_LISTING_SELECTOR).all():
        title_link = list_item.locator('a[data-testid="search-listing-title"]')
        if title_link.count() == 0:
            continue
        subtitle = list_item.locator('p[data-testid="search-listing-subtitle"]')
        short_description = subtitle.inner_text() if subtitle.count() > 0 else ""
        href = title_link.get_attribute("href") or ""
        listing_url = (
            href if href.startswith("http") else f"https://www.autotrader.co.uk{href}"
        )
        source_id = extract_source_id(listing_url)
        cards.append((listing_url, source_id, short_description, title_link.inner_text().strip()))
        if len(cards) >= MAX_LISTINGS_TO_TRY:
            break
    return cards


@pytest.mark.live
def test_autotrader_search_and_listing_elements():
    """
    Load Autotrader van search results, visit a live listing, and check that
    search cards, detail locators, and extracted fields still match the scraper.
    """

    require_proxy()
    config = AutotraderScrapeConfig(
        landing_url=AUTOTRADER_VANS_URL,
        listing_type=ListingType.VAN,
        ai_prompt_filename="van_prompt.md",
    )

    with live_session(_open_session) as page:
        search_url = configure_search(page, config)
        assert "autotrader.co.uk" in search_url

        require_locator(
            page,
            _SEARCH_RESULTS_LISTING_SELECTOR,
            description="Autotrader search result cards",
        )
        require_locator(
            page,
            'a[data-testid="search-listing-title"]',
            description="Autotrader search card titles",
        )

        cards = _snapshot_search_cards(page)
        assert cards, "search results contained no listing cards with titles"

        last_error = None
        with patch_autotrader_persist():
            for listing_url, source_id, short_description, title in cards:
                if not source_id:
                    continue
                goto_with_captcha_handling(page, listing_url)
                page.wait_for_load_state("domcontentloaded")
                pause_for_page(page)
                if is_listing_no_longer_available(page):
                    continue

                price = page.get_by_test_id("advert-price")
                if price.count() == 0:
                    continue
                price_text = price.inner_text().strip()
                if not price_text or price_text == "AUCTION":
                    continue

                require_locator(page, "h1", description="Autotrader listing title")
                assert page.get_by_test_id("advert-price").count() > 0

                try:
                    listing, _image_count = read_full_prospect_listing(
                        page,
                        short_description,
                        ListingType.VAN,
                        "van_prompt.md",
                        log_title=title,
                    )
                except Exception as error:
                    last_error = error
                    continue

                if listing is None:
                    continue

                assert_expected_prospect_fields(
                    listing,
                    source=ListingSource.AUTOTRADER,
                    listing_type=ListingType.VAN,
                )
                assert listing.source_id == source_id
                assert listing.asking_price is not None
                assert listing.currency_symbol == "£"
                return

        detail = f" last error: {last_error}" if last_error else ""
        pytest.fail(
            "could not extract a live Autotrader van listing from the first "
            f"{len(cards)} search cards.{detail}"
        )
