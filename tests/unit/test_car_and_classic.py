import json
from unittest.mock import MagicMock

import pytest

from car_and_classic import (
    _canonical_newest_search_url,
    _extract_gallery_images_from_inertia_html,
    _extract_gallery_images_from_structured_html,
    _find_json_array_end,
    _is_non_title_heading,
    extract_source_id,
    is_title_sold_or_under_offer,
    scrape_listings,
)
from models.auto_ads import ProspectListings
from models.enums import ListingSource, ListingType, ProspectListingStatus


def _sessionmaker_stub():
    session = MagicMock()
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    return MagicMock(return_value=session_cm), session


class TestExtractSourceId:
    """Tests for Car & Classic listing URL parsing."""

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://www.carandclassic.com/l/C2085940", "C2085940"),
            ("https://www.carandclassic.com/l/C2085940?ref=1", "C2085940"),
            ("https://www.carandclassic.com/l/C2085940#gallery", "C2085940"),
            ("/l/C99", "C99"),
            ("https://www.carandclassic.com/car/C1032137", "C1032137"),
            ("https://www.carandclassic.com/car/C1032137?ref=1", "C1032137"),
            ("https://www.carandclassic.com/la/C2078854", "C2078854"),
            (
                "https://www.carandclassic.com/auctions/2004-mercedes-benz-sl500-r230-n9v0O4",
                "n9v0O4",
            ),
            (
                "https://www.carandclassic.com/auctions/1993-rover-montego-20-lxi-2200-miles-nkN5Ng?ref=1",
                "nkN5Ng",
            ),
            (
                "https://www.carandclassic.com/make-an-offer/2015-porsche-macan-turbo-36-g2YYr8",
                "g2YYr8",
            ),
            (
                "https://www.carandclassic.com/make-an-offer/1997-mercedes-benz-c36-amg-w202-8qZRxg#gallery",
                "8qZRxg",
            ),
        ],
    )
    def test_extracts_listing_reference(self, url, expected):
        assert extract_source_id(url) == expected

    @pytest.mark.parametrize(
        "url",
        [None, "", "https://www.carandclassic.com/search", "https://example.com/listing/1"],
    )
    def test_returns_none_when_missing(self, url):
        assert extract_source_id(url) is None


class TestTitleSoldOrUnderOffer:
    """Tests for sold / under-offer title detection."""

    @pytest.mark.parametrize(
        "title",
        [
            "1965 Morris Minor - SOLD",
            "Porsche 911 already sold",
            "Ford Escort - Under Offer",
            "under  offer awaiting payment",
        ],
    )
    def test_detects_sold_or_under_offer(self, title):
        assert is_title_sold_or_under_offer(title) is True

    @pytest.mark.parametrize(
        "title",
        ["Ford Escort Mk1", "Soldering iron collection", "UNSOLD reserve"],
    )
    def test_rejects_live_titles(self, title):
        assert is_title_sold_or_under_offer(title) is False


class TestNonTitleHeading:
    """Tests for detail-page h1 values that are section headings, not vehicles."""

    @pytest.mark.parametrize("text", [None, "", "Highlights", "  GALLERY  ", "Description"])
    def test_section_headings(self, text):
        assert _is_non_title_heading(text) is True

    def test_vehicle_title_is_not_a_heading(self):
        assert _is_non_title_heading("1965 Morris Minor") is False


class TestCanonicalNewestSearchUrl:
    """Tests for pinning search results to newest-first page one."""

    def test_sets_sort_and_drops_page(self):
        url = _canonical_newest_search_url(
            "https://www.carandclassic.com/search?vehicle_type=cars&page=3&sort=price"
        )
        assert "sort=latest" in url
        assert "source=modal-sort" in url
        assert "page=" not in url
        assert "vehicle_type=cars" in url


class TestFindJsonArrayEnd:
    """Tests for slicing the embedded Inertia images array."""

    def test_simple_array(self):
        content = "[1, 2, 3] trailing"
        assert content[: _find_json_array_end(content, 0)] == "[1, 2, 3]"

    def test_nested_and_escaped_quotes(self):
        content = r'[{"src":"a]b\"c"}, [1]]'
        end = _find_json_array_end(content, 0)
        json.loads(content[:end])

    def test_returns_none_when_unclosed_or_not_array(self):
        assert _find_json_array_end("[1, 2", 0) is None
        assert _find_json_array_end("not-an-array", 0) is None


class TestExtractGalleryImagesFromHtml:
    """Tests for gallery URL extraction from listing HTML payloads."""

    def test_inertia_payload_prefers_largest_size(self):
        images = [
            {
                "xs": {"src": "https://cdn.example.com/a-xs.jpg"},
                "xxl": {"src": "https://cdn.example.com/a-xxl.jpg"},
            },
            {"md": {"src": "https://cdn.example.com/b-md.jpg"}},
        ]
        html = '"images":' + json.dumps(images, separators=(",", ":"))
        urls = _extract_gallery_images_from_inertia_html(html)
        assert urls[0] == "https://cdn.example.com/a-xxl.jpg"
        assert "https://cdn.example.com/b-md.jpg" in urls

    def test_inertia_payload_missing_returns_empty(self):
        assert _extract_gallery_images_from_inertia_html("<html></html>") == []

    def test_json_ld_car_images(self):
        html = """
        <script type="application/ld+json">
        {"@type": "Car", "image": ["https://cdn.example.com/one.jpg?w=1"]}
        </script>
        """
        urls = _extract_gallery_images_from_structured_html(html)
        assert urls == ["https://cdn.example.com/one.jpg?w=1"]


def _patch_scrape(monkeypatch, *, existing_ids, candidates, persist_calls, stub_calls):
    factory, _session = _sessionmaker_stub()
    monkeypatch.setattr(
        "car_and_classic.get_existing_source_ids", lambda _source: set(existing_ids)
    )
    monkeypatch.setattr("car_and_classic.create_engine_with_retry", lambda _url: MagicMock())
    monkeypatch.setattr("car_and_classic.sessionmaker", lambda **_kwargs: factory)
    monkeypatch.setattr(
        "car_and_classic.goto_with_captcha_handling",
        lambda *_a, **_k: MagicMock(status=200),
    )
    monkeypatch.setattr("car_and_classic.accept_cookies", lambda *_a, **_k: None)
    monkeypatch.setattr("car_and_classic.pause_for_page", lambda *_a, **_k: None)
    monkeypatch.setattr("car_and_classic.wait_for_listing_detail_page", lambda *_a, **_k: None)
    monkeypatch.setattr("car_and_classic.search_results_present", lambda *_a, **_k: True)
    monkeypatch.setattr("car_and_classic.quick_locator_count", lambda *_a, **_k: 1)
    monkeypatch.setattr(
        "car_and_classic._snapshot_listing_candidates", lambda *_a, **_k: candidates
    )
    monkeypatch.setattr("car_and_classic.is_http_not_found", lambda _response: False)
    monkeypatch.setattr(
        "car_and_classic.mark_listing_processed_and_check_availability",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "car_and_classic.persist_unavailable_listing_stub",
        lambda *_a, **kwargs: stub_calls.append(kwargs),
    )

    def fake_persist(_session, prospect_listing, **_kwargs):
        persist_calls.append(prospect_listing)
        return prospect_listing

    monkeypatch.setattr("car_and_classic.persist_listing_with_images_and_ai", fake_persist)

    def fake_extract(_page, hash_code, source_id, fallback_title=None):
        listing = ProspectListings(
            hash_code=hash_code,
            source_id=source_id,
            listing_source=ListingSource.CAR_AND_CLASSIC,
            listing_type=ListingType.CLASSIC,
            status=ProspectListingStatus.NEW,
            make_and_model=fallback_title or "Morris Minor",
            short_description=fallback_title or "Morris Minor",
            url=f"https://www.carandclassic.com/l/{source_id}",
        )
        return listing, ["https://cdn.example.com/a.jpg"]

    monkeypatch.setattr("car_and_classic.extract_listing_details", fake_extract)
    return MagicMock()


class TestScrapeListings:
    """Tests for Car & Classic skip/save decisions with page I/O faked."""

    def test_skips_existing_source_id(self, monkeypatch):
        persist_calls = []
        stub_calls = []
        page = _patch_scrape(
            monkeypatch,
            existing_ids={"C2085940"},
            candidates=[
                (
                    "C2085940",
                    "https://www.carandclassic.com/l/C2085940",
                    "C2085940",
                    "1965 Morris Minor",
                )
            ],
            persist_calls=persist_calls,
            stub_calls=stub_calls,
        )

        scrape_listings(page, "https://www.carandclassic.com/search")

        assert persist_calls == []
        assert stub_calls == []

    def test_persists_unavailable_stub_from_sold_card_title(self, monkeypatch):
        persist_calls = []
        stub_calls = []
        page = _patch_scrape(
            monkeypatch,
            existing_ids=set(),
            candidates=[
                (
                    "C1",
                    "https://www.carandclassic.com/l/C1",
                    "C1",
                    "1965 Morris Minor - SOLD",
                )
            ],
            persist_calls=persist_calls,
            stub_calls=stub_calls,
        )

        scrape_listings(page, "https://www.carandclassic.com/search")

        assert persist_calls == []
        assert len(stub_calls) == 1
        assert stub_calls[0]["source_id"] == "C1"
        assert stub_calls[0]["listing_source"] == ListingSource.CAR_AND_CLASSIC

    def test_persists_new_listing(self, monkeypatch):
        persist_calls = []
        stub_calls = []
        page = _patch_scrape(
            monkeypatch,
            existing_ids=set(),
            candidates=[
                (
                    "C2",
                    "https://www.carandclassic.com/l/C2",
                    "C2",
                    "1965 Morris Minor",
                )
            ],
            persist_calls=persist_calls,
            stub_calls=stub_calls,
        )
        monkeypatch.setattr(
            "car_and_classic.is_listing_no_longer_available", lambda _page: False
        )

        scrape_listings(page, "https://www.carandclassic.com/search")

        assert stub_calls == []
        assert len(persist_calls) == 1
        assert persist_calls[0].source_id == "C2"
        assert persist_calls[0].listing_source == ListingSource.CAR_AND_CLASSIC
