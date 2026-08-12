from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from ebay import (
    EBAY_CLASSICS_CATEGORY_ID,
    EBAY_VANS_CATEGORY_ID,
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
    format_ebay_datetime_string,
    is_listing_no_longer_available,
    parse_asking_price,
    parse_auction_close_datetime,
    parse_datetime_string,
    run_ebay,
)
from models.enums import ListingSource, ListingType, ProspectListingStatus


class TestEbayScrapeConfig:
    """Tests for vans vs classics scrape configuration."""

    def test_vans_config_values(self):
        config = EbayScrapeConfig(
            listing_type=ListingType.VAN,
            ai_prompt_filename="van_prompt.md",
            query="van",
            category_id=EBAY_VANS_CATEGORY_ID,
            pickup_postal_code="LS1 3AD",
            pickup_radius=100,
        )

        assert config.listing_type == ListingType.VAN
        assert config.ai_prompt_filename == "van_prompt.md"
        assert config.category_id == EBAY_VANS_CATEGORY_ID

    def test_classics_config_values(self):
        config = EbayScrapeConfig(
            listing_type=ListingType.CLASSIC,
            ai_prompt_filename="classic_car_prompt.md",
            query="classic car",
            category_id=EBAY_CLASSICS_CATEGORY_ID,
        )

        assert config.listing_type == ListingType.CLASSIC
        assert config.ai_prompt_filename == "classic_car_prompt.md"
        assert config.category_id == EBAY_CLASSICS_CATEGORY_ID

    def test_downloader_from_config_applies_fields(self):
        config = EbayScrapeConfig(
            listing_type=ListingType.CLASSIC,
            ai_prompt_filename="classic_car_prompt.md",
            query="classic car",
            category_id=EBAY_CLASSICS_CATEGORY_ID,
            pickup_postal_code="LS1 3AD",
            pickup_radius=50,
        )
        downloader = EbayDownloader.from_config(config)

        assert downloader.listing_type == ListingType.CLASSIC
        assert downloader.ai_prompt_filename == "classic_car_prompt.md"
        assert downloader.query == "classic car"
        assert downloader.category_id == EBAY_CLASSICS_CATEGORY_ID
        assert downloader.pickup_radius == 50


class TestBuildSearchFilters:
    """Tests for Browse API filter construction."""

    def test_price_filter_min_only(self):
        filters = build_search_filters(min_price=1000.0)
        assert filters == ["price:[1000.0..]"]

    def test_price_filter_min_and_max(self):
        filters = build_search_filters(min_price=1000.0, max_price=5000.0)
        assert filters == ["price:[1000.0..5000.0]"]

    def test_pickup_filters_when_both_set(self):
        filters = build_search_filters(
            pickup_postal_code="LS1 3AD",
            pickup_radius=100,
        )
        assert "pickupPostalCode:LS1 3AD" in filters
        assert "pickupRadius:100" in filters
        assert "pickupCountry:GB" in filters

    def test_raises_when_only_one_pickup_param(self):
        with pytest.raises(ValueError, match="both pickup_postal_code"):
            build_search_filters(pickup_postal_code="LS1 3AD")

        with pytest.raises(ValueError, match="both pickup_postal_code"):
            build_search_filters(pickup_radius=100)


class TestEbayDatetime:
    """Tests for eBay datetime parsing and formatting."""

    def test_round_trip_z_format(self):
        value = "2024-06-15T10:30:00.000Z"
        parsed = parse_datetime_string(value)
        assert format_ebay_datetime_string(parsed) == value

    def test_parses_iso_offset(self):
        parsed = parse_datetime_string("2024-06-15T10:30:00+01:00")
        assert parsed.tzinfo is not None

    def test_malformed_input_raises(self):
        with pytest.raises(Exception):
            parse_datetime_string("not-a-date")


class TestParseAuctionCloseDatetime:
    """Tests for auction timer parsing."""

    def test_parses_valid_timer_text(self):
        parsed = parse_auction_close_datetime("15/06, 14:30")
        assert parsed is not None
        assert parsed.month == 6
        assert parsed.day == 15
        assert parsed.hour == 14
        assert parsed.minute == 30

    def test_malformed_returns_none(self):
        assert parse_auction_close_datetime("invalid") is None
        assert parse_auction_close_datetime("15/06") is None

    @patch("ebay.datetime")
    def test_year_rolls_forward_when_date_in_past(self, mock_datetime):
        mock_datetime.now.return_value = datetime(2026, 8, 12)
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        parsed = parse_auction_close_datetime("15/06, 14:30")
        assert parsed.year == 2027


class TestEbayPageHelpers:
    """Tests for cookie consent and availability detection."""

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

    def test_click_ebay_cookie_consent_dismiss(self):
        page = MagicMock()
        accept_btn = MagicMock()
        page.query_selector.return_value = accept_btn

        assert _click_ebay_cookie_consent_dismiss(page) is True
        accept_btn.click.assert_called_once()

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


class TestProspectMapping:
    """Tests for Browse API record to ProspectListings mapping."""

    def test_build_prospect_listing_maps_core_fields(self):
        listing = {
            "title": "1965 Morris Minor",
            "item_web_url": "https://www.ebay.co.uk/itm/123",
            "item_id": "v1|123456|0",
            "price": {"value": "4,500.00", "currency": "GBP"},
            "item_location": {"city": "Leeds"},
        }
        details = {
            "full_description": "Lovely classic",
            "make": "Morris",
            "model": "Minor",
            "year": 1965,
            "fuel_type": "Petrol",
            "transmission": "Manual",
            "colour": "Blue",
        }
        now = datetime(2026, 1, 1, 12, 0, 0)

        prospect = build_prospect_listing(
            listing=listing,
            details=details,
            listing_type=ListingType.CLASSIC,
            hash_code="abc123",
            make_and_model="Morris Minor",
            current_datetime=now,
        )

        assert prospect.hash_code == "abc123"
        assert prospect.source_id == "v1|123456|0"
        assert prospect.listing_source == ListingSource.EBAY
        assert prospect.listing_type == ListingType.CLASSIC
        assert prospect.status == ProspectListingStatus.NEW
        assert prospect.asking_price == 4500
        assert prospect.currency_symbol == "£"
        assert prospect.location == "Leeds"
        assert prospect.make_and_model == "Morris Minor"
        assert prospect.year == 1965

    def test_build_prospect_listing_maps_mileage(self):
        listing = {
            "title": "Ford Transit",
            "item_web_url": "https://www.ebay.co.uk/itm/456",
            "item_id": "v1|456|0",
            "price": {"value": "5000", "currency": "GBP"},
            "item_location": {"city": "Leeds"},
        }
        details = {
            "mileage": 81000,
            "mileage_unit": "mi",
        }
        now = datetime(2026, 1, 1, 12, 0, 0)

        prospect = build_prospect_listing(
            listing=listing,
            details=details,
            listing_type=ListingType.VAN,
            hash_code="def456",
            make_and_model="Ford Transit",
            current_datetime=now,
        )

        assert prospect.mileage == 81000
        assert prospect.mileage_unit == "mi"

    def test_parse_asking_price_and_currency_symbol(self):
        assert parse_asking_price("12,345.67") == 12345
        assert currency_symbol_from_code("GBP") == "£"
        assert currency_symbol_from_code("EUR") == ""

    def test_build_make_and_model_falls_back_to_title(self):
        assert (
            build_make_and_model({}, title="Untitled listing")
            == "Untitled listing"
        )


class TestRunEbay:
    """Tests for run_ebay wiring."""

    @patch.object(EbayDownloader, "from_config")
    def test_run_ebay_delegates_to_downloader(self, mock_from_config):
        config = EbayScrapeConfig(
            listing_type=ListingType.VAN,
            ai_prompt_filename="van_prompt.md",
            query="van",
            category_id=EBAY_VANS_CATEGORY_ID,
        )
        mock_downloader = MagicMock()
        mock_downloader.download_all_listings.return_value = []
        mock_from_config.return_value = mock_downloader

        result = run_ebay(config)

        mock_from_config.assert_called_once_with(config)
        mock_downloader.download_all_listings.assert_called_once()
        assert result == []
