from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from ebay import (
    EBAY_CLASSICS_CATEGORY_ID,
    EBAY_NON_AUCTION_BUYING_OPTIONS,
    EbayDownloader,
    EbayScrapeConfig,
    _click_ebay_cookie_consent_dismiss,
    _is_ebay_cookie_consent_visible,
    accept_ebay_cookie_consent_if_present,
    build_make_and_model,
    build_prospect_listing,
    build_search_filters,
    currency_symbol_from_code,
    dismiss_ebay_cookie_consent,
    extract_source_id,
    format_ebay_datetime_string,
    is_auction_listing,
    is_listing_no_longer_available,
    parse_asking_price,
    parse_auction_close_datetime,
    parse_datetime_string,
)
from models.enums import ListingSource, ListingType, ProspectListingStatus


NOW = datetime(2026, 1, 1, 12, 0, 0)


def _browse_listing(**overrides):
    listing = {
        "title": "1965 Morris Minor",
        "item_web_url": "https://www.ebay.co.uk/itm/123",
        "item_id": "v1|123456|0",
        "price": {"value": "4,500.00", "currency": "GBP"},
        "item_location": {"city": "Leeds"},
    }
    listing.update(overrides)
    return listing


def _van_config(**overrides):
    values = dict(
        listing_type=ListingType.VAN,
        ai_prompt_filename="van_prompt.md",
        query="van",
        category_id="122202",
    )
    values.update(overrides)
    return EbayScrapeConfig(**values)


class FrozenDateTime(datetime):
    """datetime subclass with a fixed now() for auction year inference."""

    frozen_now = datetime(2026, 8, 12, 12, 0, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.frozen_now


class TestExtractSourceId:
    """Tests for eBay listing URL parsing."""

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://www.ebay.co.uk/itm/800028133156", "800028133156"),
            (
                "https://www.ebay.co.uk/itm/800028133156?_skw=van&hash=itemba45648724:g:3rcAAeSwKRRqCM9b",
                "800028133156",
            ),
            ("https://www.ebay.co.uk/itm/some-title/800028133156", "800028133156"),
            ("/itm/123", "123"),
        ],
    )
    def test_extracts_item_number(self, url, expected):
        assert extract_source_id(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            "https://www.ebay.co.uk/sch/i.html",
            "https://www.ebay.co.uk/itm/pytest/not-numeric",
        ],
    )
    def test_returns_none_when_missing(self, url):
        assert extract_source_id(url) is None


class TestEbayDownloaderFromConfig:
    """Tests for copying scrape config onto the downloader."""

    def test_from_config_copies_search_and_pickup_fields(self):
        config = EbayScrapeConfig(
            listing_type=ListingType.CLASSIC,
            ai_prompt_filename="classic_car_prompt.md",
            category_id=EBAY_CLASSICS_CATEGORY_ID,
            item_location_country="GB",
            buying_options=["FIXED_PRICE", "CLASSIFIED_AD"],
            sort="newlyListed",
            min_price=1000.0,
            max_price=9000.0,
            limit=25,
        )
        downloader = EbayDownloader.from_config(config)

        assert downloader.listing_type == ListingType.CLASSIC
        assert downloader.query is None
        assert downloader.category_id == EBAY_CLASSICS_CATEGORY_ID
        assert downloader.pickup_postal_code is None
        assert downloader.pickup_radius is None
        assert downloader.item_location_country == "GB"
        assert downloader.buying_options == ["FIXED_PRICE", "CLASSIFIED_AD"]
        assert downloader.sort == "newlyListed"
        assert downloader.min_price == 1000.0
        assert downloader.max_price == 9000.0
        assert downloader.limit == 25


class TestBuildSearchFilters:
    """Tests for Browse API filter construction, including incomplete inputs."""

    def test_no_filters_returns_private_seller_and_non_auction_filters(self):
        assert build_search_filters() == [
            "sellerAccountTypes:{INDIVIDUAL}",
            "buyingOptions:{FIXED_PRICE|CLASSIFIED_AD}",
        ]

    def test_price_filter_min_only(self):
        assert build_search_filters(min_price=1000.0) == [
            "sellerAccountTypes:{INDIVIDUAL}",
            "price:[1000.0..]",
            "buyingOptions:{FIXED_PRICE|CLASSIFIED_AD}",
        ]

    def test_price_filter_max_only_defaults_min_to_zero(self):
        assert build_search_filters(max_price=5000.0) == [
            "sellerAccountTypes:{INDIVIDUAL}",
            "price:[0..5000.0]",
            "buyingOptions:{FIXED_PRICE|CLASSIFIED_AD}",
        ]

    def test_price_filter_min_and_max(self):
        assert build_search_filters(min_price=1000.0, max_price=5000.0) == [
            "sellerAccountTypes:{INDIVIDUAL}",
            "price:[1000.0..5000.0]",
            "buyingOptions:{FIXED_PRICE|CLASSIFIED_AD}",
        ]

    def test_item_location_country_filter(self):
        assert build_search_filters(item_location_country="GB") == [
            "sellerAccountTypes:{INDIVIDUAL}",
            "itemLocationCountry:GB",
            "buyingOptions:{FIXED_PRICE|CLASSIFIED_AD}",
        ]

    def test_buying_options_filter(self):
        assert build_search_filters(
            buying_options=["FIXED_PRICE", "CLASSIFIED_AD"]
        ) == [
            "sellerAccountTypes:{INDIVIDUAL}",
            "buyingOptions:{FIXED_PRICE|CLASSIFIED_AD}",
        ]

    def test_uk_classics_filters_without_pickup(self):
        assert build_search_filters(
            item_location_country="GB",
            buying_options=["FIXED_PRICE", "CLASSIFIED_AD"],
        ) == [
            "sellerAccountTypes:{INDIVIDUAL}",
            "itemLocationCountry:GB",
            "buyingOptions:{FIXED_PRICE|CLASSIFIED_AD}",
        ]

    def test_pickup_filters_when_both_set(self):
        filters = build_search_filters(
            pickup_postal_code="LS1 3AD",
            pickup_radius=100,
        )
        assert filters == [
            "sellerAccountTypes:{INDIVIDUAL}",
            "buyingOptions:{FIXED_PRICE|CLASSIFIED_AD}",
            "pickupPostalCode:LS1 3AD",
            "pickupRadius:100",
            "pickupCountry:GB",
            "pickupRadiusUnit:mi",
            "deliveryOptions:{SELLER_ARRANGED_LOCAL_PICKUP}",
        ]

    def test_price_and_pickup_can_be_combined(self):
        filters = build_search_filters(
            min_price=500.0,
            pickup_postal_code="LS1 3AD",
            pickup_radius=50,
        )
        assert filters[0] == "sellerAccountTypes:{INDIVIDUAL}"
        assert filters[1] == "price:[500.0..]"
        assert "pickupPostalCode:LS1 3AD" in filters

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"pickup_postal_code": "LS1 3AD"},
            {"pickup_radius": 100},
        ],
    )
    def test_raises_when_only_one_pickup_param(self, kwargs):
        with pytest.raises(ValueError, match="both pickup_postal_code"):
            build_search_filters(**kwargs)


class TestEbayDatetime:
    """Tests for eBay datetime parsing and formatting."""

    def test_round_trip_z_format(self):
        value = "2024-06-15T10:30:00.000Z"
        parsed = parse_datetime_string(value)
        assert format_ebay_datetime_string(parsed) == value

    def test_parses_iso_offset_as_utc(self):
        parsed = parse_datetime_string("2024-06-15T10:30:00+01:00")
        assert parsed.tzinfo is not None
        assert parsed.hour == 9
        assert parsed.utcoffset().total_seconds() == 0

    def test_naive_iso_is_treated_as_utc(self):
        parsed = parse_datetime_string("2024-06-15T10:30:00")
        assert parsed.tzinfo is not None
        assert parsed.hour == 10

    def test_malformed_input_raises(self):
        with pytest.raises(Exception):
            parse_datetime_string("not-a-date")


class TestParseAuctionCloseDatetime:
    """Tests for auction timer parsing and year inference."""

    def test_parses_valid_timer_text(self):
        parsed = parse_auction_close_datetime("15/06, 14:30")
        assert parsed is not None
        assert (parsed.month, parsed.day, parsed.hour, parsed.minute) == (6, 15, 14, 30)

    def test_strips_surrounding_whitespace(self):
        parsed = parse_auction_close_datetime(" 15/06 , 14:30 ")
        assert parsed is not None
        assert parsed.day == 15
        assert parsed.minute == 30

    @pytest.mark.parametrize(
        "text",
        ["invalid", "15/06", "15-06, 14:30", "", "32/13, 14:30"],
    )
    def test_malformed_returns_none(self, text):
        assert parse_auction_close_datetime(text) is None

    def test_year_rolls_forward_when_date_in_past(self, monkeypatch):
        FrozenDateTime.frozen_now = datetime(2026, 8, 12, 12, 0, 0)
        monkeypatch.setattr("ebay.datetime", FrozenDateTime)

        parsed = parse_auction_close_datetime("15/06, 14:30")
        assert parsed.year == 2027

    def test_year_stays_current_when_date_still_ahead(self, monkeypatch):
        FrozenDateTime.frozen_now = datetime(2026, 8, 12, 12, 0, 0)
        monkeypatch.setattr("ebay.datetime", FrozenDateTime)

        parsed = parse_auction_close_datetime("15/12, 14:30")
        assert parsed.year == 2026

    def test_impossible_calendar_date_returns_none(self, monkeypatch):
        FrozenDateTime.frozen_now = datetime(2026, 8, 12, 12, 0, 0)
        monkeypatch.setattr("ebay.datetime", FrozenDateTime)

        assert parse_auction_close_datetime("29/02, 10:00") is None


class TestParseAskingPrice:
    """Tests for Browse API price conversion."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("12,345.67", 12345),
            ("4500", 4500),
            ("4,500.00", 4500),
            (4500, 4500),
            (4500.99, 4500),
            (0, 0),
            (None, None),
            ("", None),
        ],
    )
    def test_parses_numeric_values(self, value, expected):
        assert parse_asking_price(value) == expected

    @pytest.mark.parametrize("value", ["POA", "not-a-price"])
    def test_unparseable_values_raise(self, value):
        with pytest.raises((TypeError, ValueError)):
            parse_asking_price(value)


class TestCurrencySymbolFromCode:
    """Tests for ISO currency code to display symbol mapping."""

    @pytest.mark.parametrize(
        "code, symbol",
        [
            ("GBP", "£"),
            ("USD", "$"),
            ("EUR", ""),
            ("", ""),
            ("AUD", ""),
        ],
    )
    def test_known_and_unknown_codes(self, code, symbol):
        assert currency_symbol_from_code(code) == symbol


class TestBuildMakeAndModel:
    """Tests for make/model assembly from item specifics."""

    def test_joins_make_and_model(self):
        assert build_make_and_model(
            {"make": "Morris", "model": "Minor"},
            title="ignored",
        ) == "Morris Minor"

    def test_make_only(self):
        assert build_make_and_model({"make": "Ford"}, title="ignored") == "Ford"

    def test_model_only(self):
        assert build_make_and_model({"model": "Transit"}, title="ignored") == "Transit"

    def test_falls_back_to_title_when_both_missing(self):
        assert build_make_and_model({}, title="Untitled listing") == "Untitled listing"

    def test_empty_strings_are_treated_as_missing(self):
        assert (
            build_make_and_model({"make": "", "model": None}, title="Fallback")
            == "Fallback"
        )


class TestIsAuctionListing:
    """Tests for auction detection from Browse API records and scraped details."""

    def test_detects_auction_from_browse_api_buying_options(self):
        listing = _browse_listing(buyingOptions=["AUCTION"])

        assert is_auction_listing(listing) is True

    def test_detects_auction_from_scraped_close_time(self):
        listing = _browse_listing()

        assert is_auction_listing(listing, details={"auction_closes": NOW}) is True

    def test_fixed_price_listing_is_not_auction(self):
        listing = _browse_listing(buyingOptions=["FIXED_PRICE"])

        assert is_auction_listing(listing) is False

    def test_classified_ad_listing_is_not_auction(self):
        listing = _browse_listing(buyingOptions=["CLASSIFIED_AD"])

        assert is_auction_listing(listing) is False


class TestBuildProspectListing:
    """Tests for Browse API record to ProspectListings mapping."""

    def test_maps_core_fields(self):
        prospect = build_prospect_listing(
            listing=_browse_listing(),
            details={
                "full_description": "Lovely classic",
                "make": "Morris",
                "model": "Minor",
                "year": 1965,
                "fuel_type": "Petrol",
                "transmission": "Manual",
                "colour": "Blue",
                "body_type": "Saloon",
                "engine_size": "1.0",
                "auction_closes": NOW,
            },
            listing_type=ListingType.CLASSIC,
            hash_code="abc123def4567890",
            make_and_model="Morris Minor",
            current_datetime=NOW,
        )

        assert prospect.hash_code == "abc123def4567890"
        assert prospect.source_id == "123"
        assert prospect.listing_source == ListingSource.EBAY
        assert prospect.listing_type == ListingType.CLASSIC
        assert prospect.status == ProspectListingStatus.NEW
        assert prospect.asking_price == 4500
        assert prospect.currency_symbol == "£"
        assert prospect.location == "Leeds"
        assert prospect.make_and_model == "Morris Minor"
        assert prospect.year == 1965
        assert prospect.gearbox_type == "Manual"
        assert prospect.body_type == "Saloon"
        assert prospect.engine_size == "1.0"
        assert prospect.auction_closes == NOW
        assert prospect.created_at == NOW
        assert prospect.updated_at == NOW

    def test_maps_mileage(self):
        prospect = build_prospect_listing(
            listing=_browse_listing(
                title="Ford Transit",
                item_id="v1|456|0",
                price={"value": "5000", "currency": "GBP"},
            ),
            details={"mileage": 81000, "mileage_unit": "mi"},
            listing_type=ListingType.VAN,
            hash_code="def4567890abcdef",
            make_and_model="Ford Transit",
            current_datetime=NOW,
        )

        assert prospect.mileage == 81000
        assert prospect.mileage_unit == "mi"

    def test_missing_optional_fields_are_none(self):
        prospect = build_prospect_listing(
            listing=_browse_listing(item_location={}),
            details={},
            listing_type=ListingType.VAN,
            hash_code="aaaaaaaaaaaaaaaa",
            make_and_model="Unknown",
            current_datetime=NOW,
        )

        assert prospect.location == ""
        assert prospect.year is None
        assert prospect.mileage is None
        assert prospect.full_description is None
        assert prospect.auction_closes is None

    def test_prefers_numeric_itm_id_over_api_item_id(self):
        prospect = build_prospect_listing(
            listing=_browse_listing(
                item_web_url="https://www.ebay.co.uk/itm/800028133156?_skw=van",
                item_id="v1|800028133156|0",
            ),
            details={},
            listing_type=ListingType.VAN,
            hash_code="bbbbbbbbbbbbbbbb",
            make_and_model="Unknown",
            current_datetime=NOW,
        )
        assert prospect.source_id == "800028133156"

    def test_falls_back_to_item_id_when_url_has_no_numeric_id(self):
        prospect = build_prospect_listing(
            listing=_browse_listing(
                item_web_url="https://www.ebay.co.uk/itm/pytest/legacy",
                item_id="v1|123456|0",
            ),
            details={},
            listing_type=ListingType.VAN,
            hash_code="bbbbbbbbbbbbbbbb",
            make_and_model="Unknown",
            current_datetime=NOW,
        )
        assert prospect.source_id == "v1|123456|0"

    def test_missing_item_id_and_non_numeric_url_leaves_source_id_none(self):
        listing = _browse_listing(
            item_web_url="https://www.ebay.co.uk/itm/pytest/legacy",
        )
        del listing["item_id"]
        prospect = build_prospect_listing(
            listing=listing,
            details={},
            listing_type=ListingType.VAN,
            hash_code="bbbbbbbbbbbbbbbb",
            make_and_model="Unknown",
            current_datetime=NOW,
        )
        assert prospect.source_id is None

    def test_usd_maps_dollar_symbol(self):
        prospect = build_prospect_listing(
            listing=_browse_listing(price={"value": "1200", "currency": "USD"}),
            details={},
            listing_type=ListingType.VAN,
            hash_code="cccccccccccccccc",
            make_and_model="Unknown",
            current_datetime=NOW,
        )
        assert prospect.currency_symbol == "$"
        assert prospect.asking_price == 1200

    def test_missing_price_defaults_to_none(self):
        listing = _browse_listing()
        del listing["price"]
        prospect = build_prospect_listing(
            listing=listing,
            details={},
            listing_type=ListingType.VAN,
            hash_code="dddddddddddddddd",
            make_and_model="Unknown",
            current_datetime=NOW,
        )
        assert prospect.asking_price is None
        assert prospect.currency_symbol == ""


class TestEbayPageHelpers:
    """Tests for cookie consent, availability, and seller-type detection."""

    def test_is_listing_no_longer_available_true(self):
        page = MagicMock()
        page.get_by_text.return_value.count.return_value = 1

        assert is_listing_no_longer_available(page) is True

    def test_is_listing_no_longer_available_false(self):
        page = MagicMock()
        page.get_by_text.return_value.count.return_value = 0

        assert is_listing_no_longer_available(page) is False

    def test_is_ebay_cookie_consent_visible_when_accept_present(self):
        page = MagicMock()
        page.query_selector.side_effect = lambda selector: (
            MagicMock() if selector == "#gdpr-banner-accept" else None
        )

        assert _is_ebay_cookie_consent_visible(page) is True

    def test_is_ebay_cookie_consent_visible_when_only_banner_present(self):
        page = MagicMock()
        page.query_selector.side_effect = lambda selector: (
            MagicMock() if selector == "#gdpr-banner" else None
        )

        assert _is_ebay_cookie_consent_visible(page) is True

    def test_click_ebay_cookie_consent_dismiss(self):
        page = MagicMock()
        accept_btn = MagicMock()
        page.query_selector.return_value = accept_btn

        assert _click_ebay_cookie_consent_dismiss(page) is True
        accept_btn.click.assert_called_once()

    def test_click_ebay_cookie_consent_dismiss_when_missing(self):
        page = MagicMock()
        page.query_selector.return_value = None

        assert _click_ebay_cookie_consent_dismiss(page) is False

    @patch("ebay.run_consent_dismiss_loop")
    def test_dismiss_ebay_cookie_consent_uses_shared_loop(self, mock_loop):
        page = MagicMock()

        dismiss_ebay_cookie_consent(page, wait_for_banner=True)

        mock_loop.assert_called_once()
        assert mock_loop.call_args.kwargs["wait_for_banner"] is True

    @patch("ebay.dismiss_ebay_cookie_consent")
    def test_accept_ebay_cookie_consent_delegates_to_dismiss(self, mock_dismiss):
        page = MagicMock()

        accept_ebay_cookie_consent_if_present(page)

        mock_dismiss.assert_called_once_with(page, wait_for_banner=False)

    def test_extract_seller_type_private(self):
        downloader = EbayDownloader(
            listing_type=ListingType.VAN,
            ai_prompt_filename="van_prompt.md",
        )
        page = MagicMock()
        element = MagicMock()
        element.inner_text.return_value = "Private"
        page.query_selector_all.return_value = [element]
        downloader._page = page

        assert downloader._extract_seller_type() == "Private"

    def test_extract_seller_type_business(self):
        downloader = EbayDownloader(
            listing_type=ListingType.VAN,
            ai_prompt_filename="van_prompt.md",
        )
        page = MagicMock()
        element = MagicMock()
        element.inner_text.return_value = "Business"
        page.query_selector_all.return_value = [element]
        downloader._page = page

        assert downloader._extract_seller_type() == "Business"

    def test_extract_seller_type_from_about_seller_fallback(self):
        downloader = EbayDownloader(
            listing_type=ListingType.VAN,
            ai_prompt_filename="van_prompt.md",
        )
        page = MagicMock()
        page.query_selector_all.return_value = []
        section = MagicMock()
        section.inner_text.return_value = "Member since 2019\nPrivate seller"
        page.query_selector.return_value = section
        downloader._page = page

        assert downloader._extract_seller_type() == "Private"

    def test_extract_seller_type_none_when_unknown(self):
        downloader = EbayDownloader(
            listing_type=ListingType.VAN,
            ai_prompt_filename="van_prompt.md",
        )
        page = MagicMock()
        page.query_selector_all.return_value = []
        page.query_selector.return_value = None
        downloader._page = page

        assert downloader._extract_seller_type() is None

    def test_extract_seller_type_none_when_page_missing(self):
        downloader = EbayDownloader(
            listing_type=ListingType.VAN,
            ai_prompt_filename="van_prompt.md",
        )
        assert downloader._extract_seller_type() is None


class TestCreateApi:
    """Tests for credential and marketplace validation before the eBay client is built."""

    def test_missing_credentials_raise(self, monkeypatch):
        for name in (
            "AUTO_ADS_EBAY_CLIENT_ID",
            "AUTO_ADS_EBAY_DEV_ID",
            "AUTO_ADS_EBAY_CLIENT_SECRET",
            "AUTO_ADS_EBAY_REDIRECT_URL",
        ):
            monkeypatch.delenv(name, raising=False)

        downloader = EbayDownloader(
            listing_type=ListingType.VAN,
            ai_prompt_filename="van_prompt.md",
        )
        with pytest.raises(ValueError, match="Missing required eBay credentials"):
            downloader._create_api()

    def test_unsupported_marketplace_raises(self, monkeypatch):
        monkeypatch.setenv("AUTO_ADS_EBAY_CLIENT_ID", "id")
        monkeypatch.setenv("AUTO_ADS_EBAY_DEV_ID", "dev")
        monkeypatch.setenv("AUTO_ADS_EBAY_CLIENT_SECRET", "secret")
        monkeypatch.setenv("AUTO_ADS_EBAY_REDIRECT_URL", "redirect")
        monkeypatch.setattr("ebay.get_oauth_tokens", lambda _provider: None)

        downloader = EbayDownloader(
            listing_type=ListingType.VAN,
            ai_prompt_filename="van_prompt.md",
            marketplace="DE",
        )
        with pytest.raises(ValueError, match="Unsupported marketplace"):
            downloader._create_api()


@contextmanager
def _patched_download_io(monkeypatch, *, existing_ids=None):
    persist_calls = []

    session = MagicMock()
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    session_factory = MagicMock(return_value=session_cm)

    playwright_cm = MagicMock()
    playwright_cm.__enter__.return_value = MagicMock()
    playwright_cm.__exit__.return_value = False
    browser = MagicMock()
    browser.new_page.return_value = MagicMock()

    monkeypatch.setattr(
        "ebay.get_existing_source_ids", lambda _source: set(existing_ids or [])
    )
    monkeypatch.setattr("ebay.create_engine_with_retry", lambda _url: MagicMock())
    monkeypatch.setattr("ebay.sessionmaker", lambda **_kwargs: session_factory)
    monkeypatch.setattr("ebay.sync_stealth_playwright", lambda: playwright_cm)
    monkeypatch.setattr("ebay.launch_stealth_chromium", lambda *_args, **_kwargs: browser)
    monkeypatch.setattr("ebay.scraper_update_new_listings_availability", lambda *_a, **_k: None)
    monkeypatch.setattr("ebay.mark_listing_processed_and_check_availability", lambda *_a, **_k: None)

    def fake_persist(_session, prospect_listing, **_kwargs):
        persist_calls.append(prospect_listing)
        return prospect_listing

    monkeypatch.setattr("ebay.persist_listing_with_images_and_ai", fake_persist)

    yield persist_calls


def _run_download(monkeypatch, *, listings, existing_ids=None, visit=None):
    visit_mock = visit or MagicMock(
        side_effect=AssertionError("listing page should not be visited")
    )
    with _patched_download_io(monkeypatch, existing_ids=existing_ids) as persist_calls:
        downloader = EbayDownloader.from_config(_van_config())
        with (
            patch.object(EbayDownloader, "_get_api", return_value=MagicMock()),
            patch.object(EbayDownloader, "search_listings", return_value=iter(listings)),
            patch.object(EbayDownloader, "_visit_listing_page", visit_mock),
        ):
            result = downloader.download_all_listings()
    return result, persist_calls, visit_mock


class TestDownloadAllListings:
    """Tests for skip/save decisions in the download loop, with I/O faked."""

    def test_skips_existing_source_id_without_visiting(self, monkeypatch):
        listing = _browse_listing(
            item_web_url="https://www.ebay.co.uk/itm/800028133156",
            item_id="v1|800028133156|0",
        )

        result, persist_calls, visit_mock = _run_download(
            monkeypatch,
            listings=[listing],
            existing_ids={"800028133156"},
        )

        assert result == []
        assert persist_calls == []
        visit_mock.assert_not_called()

    def test_skips_listing_with_missing_url(self, monkeypatch):
        listing = _browse_listing(item_id="v1|new|0")
        listing["item_web_url"] = None

        result, persist_calls, visit_mock = _run_download(
            monkeypatch,
            listings=[listing],
            existing_ids=set(),
        )

        assert result == []
        assert persist_calls == []
        visit_mock.assert_not_called()

    def test_skips_non_private_seller(self, monkeypatch):
        listing = _browse_listing(item_id="v1|business|0")
        visit_mock = MagicMock(side_effect=ValueError("seller is Business (not Private)"))

        result, persist_calls, _visit = _run_download(
            monkeypatch,
            listings=[listing],
            existing_ids=set(),
            visit=visit_mock,
        )

        assert result == []
        assert persist_calls == []

    def test_skips_unavailable_listing(self, monkeypatch):
        listing = _browse_listing(item_id="v1|ended|0")
        visit_mock = MagicMock(side_effect=ValueError("listing unavailable"))

        result, persist_calls, _visit = _run_download(
            monkeypatch,
            listings=[listing],
            existing_ids=set(),
            visit=visit_mock,
        )

        assert result == []
        assert persist_calls == []

    def test_skips_auction_listing_from_browse_api(self, monkeypatch):
        listing = _browse_listing(
            item_id="v1|auction|0",
            buyingOptions=["AUCTION"],
        )

        result, persist_calls, visit_mock = _run_download(
            monkeypatch,
            listings=[listing],
            existing_ids=set(),
        )

        assert result == []
        assert persist_calls == []
        visit_mock.assert_not_called()

    def test_skips_auction_listing_detected_on_page(self, monkeypatch):
        listing = _browse_listing(item_id="v1|auction-page|0")
        visit_mock = MagicMock(
            return_value=({"auction_closes": NOW}, ["https://i.ebayimg.com/1.jpg"], "Ford Transit")
        )

        result, persist_calls, _visit = _run_download(
            monkeypatch,
            listings=[listing],
            existing_ids=set(),
            visit=visit_mock,
        )

        assert result == []
        assert persist_calls == []

    def test_persists_new_private_listing(self, monkeypatch):
        listing = _browse_listing(
            item_web_url="https://www.ebay.co.uk/itm/800028133156",
            item_id="v1|800028133156|0",
        )
        details = {"year": 2018, "mileage": 90000, "mileage_unit": "mi"}
        visit_mock = MagicMock(
            return_value=(details, ["https://i.ebayimg.com/1.jpg"], "Ford Transit")
        )

        result, persist_calls, _visit = _run_download(
            monkeypatch,
            listings=[listing],
            existing_ids=set(),
            visit=visit_mock,
        )

        assert result == [listing]
        assert len(persist_calls) == 1
        saved = persist_calls[0]
        assert saved.source_id == "800028133156"
        assert saved.listing_source == ListingSource.EBAY
        assert saved.make_and_model == "Ford Transit"
        assert saved.year == 2018
        assert saved.asking_price == 4500
