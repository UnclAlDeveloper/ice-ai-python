"""Helpers for opt-in live scrape tests against real listing sites."""

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from models.auto_ads import ProspectListings
from models.enums import ListingSource, ListingType
from stealth_browser import sync_stealth_playwright


MAX_LISTINGS_TO_TRY = 6


# REQUIRE PROXY
def require_proxy() -> None:
    """
    Skip the calling test when Decodo is not configured.
    """

    if not os.getenv("DECODO_SERVER"):
        pytest.skip("DECODO_SERVER is not set")


# REQUIRE LOCATOR
def require_locator(page, selector: str, *, description: str, min_count: int = 1):
    """
    Assert that a CSS selector matches at least min_count elements on the page.
    """

    locator = page.locator(selector)
    count = locator.count()
    assert count >= min_count, (
        f"{description}: expected {selector!r} count >= {min_count}, got {count} "
        f"(url={page.url})"
    )
    return locator


# ASSERT EXPECTED PROSPECT FIELDS
def assert_expected_prospect_fields(
    listing: ProspectListings,
    *,
    source: ListingSource,
    listing_type: ListingType,
) -> None:
    """
    Assert that a live-extracted prospect has the fields a scrape must produce.
    """

    assert listing.listing_source == source
    assert listing.listing_type == listing_type
    assert listing.make_and_model, "make_and_model was empty"
    assert listing.short_description, "short_description was empty"
    assert listing.url, "url was empty"
    assert listing.source_id, "source_id was empty"
    assert listing.hash_code, "hash_code was empty"
    if listing.asking_price is not None:
        assert listing.asking_price >= 0
        assert listing.currency_symbol in {"£", "$", "€", "", " "}
    if listing.year is not None:
        assert 1900 <= listing.year <= 2027
    if listing.mileage is not None:
        assert listing.mileage >= 0


# SESSIONMAKER STUB
def sessionmaker_stub():
    """
    Return a sessionmaker stand-in so live extracts do not write to the database.
    """

    session = MagicMock()
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    return MagicMock(return_value=session_cm)


# PATCH AUTOTRADER PERSIST
@contextmanager
def patch_autotrader_persist():
    """
    Keep Autotrader page extraction but skip database, image download, and AI.
    """

    def fake_persist(_session, prospect_listing, **_kwargs):
        return prospect_listing

    with (
        patch("autotrader.persist_listing_with_images_and_ai", side_effect=fake_persist),
        patch("autotrader.create_engine_with_retry", return_value=MagicMock()),
        patch("autotrader.sessionmaker", return_value=sessionmaker_stub()),
    ):
        yield


# LIVE SESSION
@contextmanager
def live_session(open_session):
    """
    Launch the source's production browser session and always close it.
    """

    with sync_stealth_playwright() as playwright:
        browser, page = open_session(playwright)
        try:
            yield page
        finally:
            try:
                browser.close()
            except Exception:
                pass
