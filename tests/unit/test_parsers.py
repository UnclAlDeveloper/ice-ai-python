import pytest

from autotrader import extract_source_id as autotrader_extract_source_id
from car_and_classic import (
    _normalize_image_url as cac_normalize_image_url,
    extract_source_id as cac_extract_source_id,
    is_title_sold_or_under_offer,
    parse_mileage as cac_parse_mileage,
)
from common import parse_mileage as common_parse_mileage
from ebay import format_ebay_datetime_string, parse_datetime_string
from listing_images import (
    cap_gallery_urls,
    merge_image_url_lists,
    normalize_image_url,
)
from pistonheads import (
    _normalize_image_url as ph_normalize_image_url,
    _parse_price_text,
    extract_source_id as ph_extract_source_id,
    parse_mileage as ph_parse_mileage,
)
from scraper_driver import replace_query_params


class TestAutotraderExtractSourceId:
    """Tests for Autotrader listing URL parsing."""

    def test_extracts_van_details_id(self):
        url = "https://www.autotrader.co.uk/van-details/202606113190878"
        assert autotrader_extract_source_id(url) == "202606113190878"

    def test_extracts_car_details_id(self):
        url = "https://www.autotrader.co.uk/car-details/123456789"
        assert autotrader_extract_source_id(url) == "123456789"

    def test_returns_none_for_unrelated_url(self):
        assert autotrader_extract_source_id("https://www.autotrader.co.uk/search") is None


class TestCarAndClassicParsers:
    """Tests for Car and Classic URL and text helpers."""

    def test_extract_source_id(self):
        url = "https://www.carandclassic.com/l/C2085940"
        assert cac_extract_source_id(url) == "C2085940"

    def test_is_title_sold_or_under_offer(self):
        assert is_title_sold_or_under_offer("1965 Morris Minor - SOLD") is True
        assert is_title_sold_or_under_offer("Porsche 911 - Under Offer") is True
        assert is_title_sold_or_under_offer("Ford Escort Mk1") is False

    def test_parse_mileage(self):
        assert cac_parse_mileage("138,100 Miles") == (138100, "Miles")
        assert cac_parse_mileage("invalid") == (None, None)

    def test_normalize_image_url_strips_query(self):
        url = "https://cdn.example.com/photo.jpg?w=800#thumb"
        assert cac_normalize_image_url(url) == "https://cdn.example.com/photo.jpg"


class TestPistonHeadsParsers:
    """Tests for PistonHeads URL and text helpers."""

    def test_extract_source_id(self):
        url = "https://www.pistonheads.com/buy/listing/15667955"
        assert ph_extract_source_id(url) == "15667955"

    def test_parse_mileage(self):
        assert ph_parse_mileage("81,000 mi") == (81000, "mi")
        assert ph_parse_mileage(None) == (None, None)

    def test_parse_price_text(self):
        assert _parse_price_text("£19,995") == (19995, "£")
        assert _parse_price_text("POA") == (None, None)
        assert _parse_price_text("Price on application") == (None, None)

    def test_normalize_image_url_prefers_fullsize(self):
        url = "https://cdn.example.com/LargeSize/photo.jpg?size=large"
        assert ph_normalize_image_url(url) == "https://cdn.example.com/Fullsize/photo.jpg"


class TestSharedScraperHelpers:
    """Tests for helpers shared across listing scrapers."""

    def test_common_parse_mileage(self):
        assert common_parse_mileage("81,000 mi") == (81000, "mi")
        assert common_parse_mileage(None) == (None, None)

    def test_normalize_and_merge_image_urls(self):
        merged = merge_image_url_lists(
            ["https://cdn.example.com/a.jpg?w=1"],
            ["https://cdn.example.com/a.jpg?w=2", "https://cdn.example.com/b.jpg"],
        )
        assert merged == [
            "https://cdn.example.com/a.jpg?w=1",
            "https://cdn.example.com/b.jpg",
        ]
        assert (
            normalize_image_url(
                "https://cdn.example.com/LargeSize/a.jpg?x=1",
                path_replacements=(("/LargeSize/", "/Fullsize/"),),
            )
            == "https://cdn.example.com/Fullsize/a.jpg"
        )

    def test_cap_gallery_urls(self):
        assert len(cap_gallery_urls([f"u{i}" for i in range(5)], max_images=3)) == 3

    def test_replace_query_params(self):
        url = replace_query_params(
            "https://example.com/search?page=2&keep=1",
            drop=("page",),
            set_params={"sort": "latest"},
            prefer_keys=["sort", "keep"],
        )
        assert url == "https://example.com/search?sort=latest&keep=1"


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
