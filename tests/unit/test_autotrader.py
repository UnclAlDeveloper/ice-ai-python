from unittest.mock import MagicMock

import pytest

from autotrader import (
    UNAVAILABLE_ADVERT_TEXT,
    _ENGLISH_POSTCODES,
    _SEARCH_RESULTS_LISTING_SELECTOR,
    extract_source_id,
    is_listing_no_longer_available,
    random_english_postcode,
    read_full_prospect_listing,
    scrape_listings,
)
from models.auto_ads import ProspectListings
from models.enums import ListingSource, ListingType, ProspectListingStatus


def _locator(text="", *, count=1):
    loc = MagicMock()
    loc.count.return_value = count
    loc.inner_text.return_value = text
    loc.text_content.return_value = text
    loc.first = loc
    loc.all.return_value = []
    loc.wait_for = MagicMock()
    loc.click = MagicMock()
    return loc


def _sessionmaker_stub():
    session = MagicMock()
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    return MagicMock(return_value=session_cm), session


class TestExtractSourceId:
    """Tests for Autotrader listing URL parsing."""

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://www.autotrader.co.uk/van-details/202606113190878", "202606113190878"),
            ("https://www.autotrader.co.uk/car-details/123456789", "123456789"),
            (
                "https://www.autotrader.co.uk/van-details/202606113190878?utm=1",
                "202606113190878",
            ),
            (
                "https://www.autotrader.co.uk/van-details/202605162457488?sort=most-recent&page=1",
                "202605162457488",
            ),
            ("/car-details/99", "99"),
        ],
    )
    def test_extracts_details_id(self, url, expected):
        assert extract_source_id(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            "https://www.autotrader.co.uk/search",
            "https://www.autotrader.co.uk/bike-details/123",
            "https://www.autotrader.co.uk/van-details/",
        ],
    )
    def test_returns_none_when_missing(self, url):
        assert extract_source_id(url) is None


class TestRandomEnglishPostcode:
    """Tests for Autotrader distance-search postcodes."""

    def test_returns_a_known_english_postcode(self):
        assert random_english_postcode() in _ENGLISH_POSTCODES


class TestIsListingNoLongerAvailable:
    """Tests for Autotrader unavailable-advert detection."""

    def test_true_when_banner_copy_present(self):
        page = MagicMock()

        def locator(selector, **kwargs):
            loc = _locator()
            if kwargs.get("has_text") == UNAVAILABLE_ADVERT_TEXT:
                loc.count.return_value = 1
            else:
                loc.count.return_value = 0
            return loc

        page.locator.side_effect = locator
        page.content.return_value = ""
        page.get_by_text.return_value.count.return_value = 0

        assert is_listing_no_longer_available(page) is True

    def test_true_when_banner_text_in_html(self):
        page = MagicMock()
        page.locator.return_value = _locator(count=0)
        page.content.return_value = f"prefix {UNAVAILABLE_ADVERT_TEXT} suffix"
        page.get_by_text.return_value.count.return_value = 0

        assert is_listing_no_longer_available(page) is True

    def test_false_when_listing_is_live(self):
        page = MagicMock()
        page.locator.return_value = _locator(count=0)
        page.content.return_value = "This van is still for sale"
        page.get_by_text.return_value.count.return_value = 0

        assert is_listing_no_longer_available(page) is False


def _detail_page(*, url, h1, short_description, price):
    page = MagicMock()
    page.url = url
    locators = {
        "h1": _locator(h1),
        "h1 + div span": _locator(short_description),
        'p[class*="sc-1ph9l9h-4"]': _locator("Leeds - 2.1 miles"),
        'section[id="description"] p': _locator("A tidy private van."),
        'div:has(> p:text-is("Owners"))': _locator(count=0),
        'h3:text-is("MOT Information")': _locator(count=0),
        'h3:text-is("MOT Information") + p, h3:text-is("MOT Information") ~ p': _locator(
            count=0
        ),
    }

    def locator(selector):
        return locators.get(selector, _locator(count=0))

    page.locator.side_effect = locator

    def get_by_test_id(name):
        if name == "advert-price":
            return _locator(price, count=1 if price else 0)
        return _locator(count=0)

    page.get_by_test_id.side_effect = get_by_test_id
    return page


def _patch_read_persist(monkeypatch, persist_calls):
    factory, _session = _sessionmaker_stub()
    monkeypatch.setattr("autotrader.create_engine_with_retry", lambda _url: MagicMock())
    monkeypatch.setattr("autotrader.sessionmaker", lambda **_kwargs: factory)
    monkeypatch.setattr("autotrader.pause_for_page", lambda *_a, **_k: None)
    monkeypatch.setattr("autotrader.get_basic_history_check", lambda _page: None)
    monkeypatch.setattr("autotrader.get_specs_and_features", lambda _page: None)
    overview = {
        "mileage": "81,000 miles",
        "registration": "2007 (57 reg)",
        "body-type": "Panel Van",
        "engine": "2.2",
        "gearbox": "Manual",
        "fuel-type": "Diesel",
        "body-colour": "White",
        "seats": "3",
    }
    monkeypatch.setattr(
        "autotrader.get_overview_value",
        lambda _page, name: overview.get(name),
    )

    def fake_persist(_session, prospect_listing, **_kwargs):
        persist_calls.append(prospect_listing)
        return prospect_listing

    monkeypatch.setattr("autotrader.persist_listing_with_images_and_ai", fake_persist)


class TestReadFullProspectListing:
    """Tests for Autotrader detail-page mapping and skip rules."""

    def test_skips_when_price_missing(self, monkeypatch):
        persist_calls = []
        _patch_read_persist(monkeypatch, persist_calls)
        page = _detail_page(
            url="https://www.autotrader.co.uk/van-details/111",
            h1="Ford Transit",
            short_description="LWB",
            price="",
        )

        listing, image_count = read_full_prospect_listing(
            page, "LWB", ListingType.VAN, "van_prompt.md"
        )

        assert listing is None
        assert image_count == 0
        assert persist_calls == []

    def test_skips_auction_listings(self, monkeypatch):
        persist_calls = []
        _patch_read_persist(monkeypatch, persist_calls)
        page = _detail_page(
            url="https://www.autotrader.co.uk/van-details/111",
            h1="Ford Transit",
            short_description="LWB",
            price="AUCTION",
        )

        listing, image_count = read_full_prospect_listing(
            page, "LWB", ListingType.VAN, "van_prompt.md"
        )

        assert listing is None
        assert image_count == 0
        assert persist_calls == []

    def test_raises_when_short_description_does_not_match(self, monkeypatch):
        persist_calls = []
        _patch_read_persist(monkeypatch, persist_calls)
        page = _detail_page(
            url="https://www.autotrader.co.uk/van-details/111",
            h1="Ford Transit",
            short_description="Other subtitle",
            price="£12,995",
        )

        with pytest.raises(Exception, match="Expected short description"):
            read_full_prospect_listing(
                page, "LWB", ListingType.VAN, "van_prompt.md"
            )

        assert persist_calls == []

    def test_maps_price_vat_registration_and_location(self, monkeypatch):
        persist_calls = []
        _patch_read_persist(monkeypatch, persist_calls)
        page = _detail_page(
            url="https://www.autotrader.co.uk/van-details/202606113190878",
            h1="Ford Transit",
            short_description="LWB 350",
            price="£12,995 Inc VAT",
        )

        listing, _image_count = read_full_prospect_listing(
            page, "LWB 350", ListingType.VAN, "van_prompt.md"
        )

        assert listing is persist_calls[0]
        assert listing.source_id == "202606113190878"
        assert listing.listing_source == ListingSource.AUTOTRADER
        assert listing.listing_type == ListingType.VAN
        assert listing.status == ProspectListingStatus.NEW
        assert listing.asking_price == 12995
        assert listing.currency_symbol == "£"
        assert listing.vat_status == "Inc VAT"
        assert listing.location == "Leeds"
        assert listing.year == 2007
        assert listing.registration == "57 reg"
        assert listing.mileage == 81000
        assert listing.seats == 3
        assert listing.make_and_model == "Ford Transit"


def _search_card(*, listing_id, href, title, subtitle):
    item = MagicMock()
    item.get_attribute.return_value = listing_id
    title_link = _locator(title)
    title_link.get_attribute.return_value = href
    subtitle_el = _locator(subtitle)

    def locator(selector):
        if "search-listing-title" in selector:
            return title_link
        if "search-listing-subtitle" in selector:
            return subtitle_el
        return _locator(count=0)

    item.locator.side_effect = locator
    return item


def _patch_scrape(monkeypatch, *, existing_ids, cards, persist_calls, stub_calls):
    present_calls = {"n": 0}

    def search_results_present(_page, _selector):
        present_calls["n"] += 1
        return present_calls["n"] == 1

    page = MagicMock()
    results = MagicMock()
    results.all.return_value = cards
    results.first.wait_for = MagicMock()

    def locator(selector, **_kwargs):
        if selector == _SEARCH_RESULTS_LISTING_SELECTOR:
            return results
        return _locator("Ford Transit", count=1)

    page.locator.side_effect = locator
    page.url = "https://www.autotrader.co.uk/van-search"

    factory, _session = _sessionmaker_stub()
    monkeypatch.setattr(
        "autotrader.get_existing_source_ids", lambda _source: set(existing_ids)
    )
    monkeypatch.setattr("autotrader.create_engine_with_retry", lambda _url: MagicMock())
    monkeypatch.setattr("autotrader.sessionmaker", lambda **_kwargs: factory)
    monkeypatch.setattr("autotrader.goto_with_captcha_handling", lambda *_a, **_k: MagicMock(status=200))
    monkeypatch.setattr("autotrader.dismiss_cookie_consent", lambda *_a, **_k: None)
    monkeypatch.setattr("autotrader.pause_for_page", lambda *_a, **_k: None)
    monkeypatch.setattr("autotrader._wait_for_search_results_ready", lambda *_a, **_k: None)
    monkeypatch.setattr("autotrader.search_results_present", search_results_present)
    monkeypatch.setattr("autotrader.is_captcha_present", lambda _page: False)
    monkeypatch.setattr("autotrader.is_http_not_found", lambda _response: False)
    monkeypatch.setattr("autotrader.update_new_listings_availability", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "autotrader.persist_unavailable_listing_stub",
        lambda *_a, **kwargs: stub_calls.append(kwargs),
    )

    def fake_read(page, short_description, listing_type, ai_prompt_filename, **_kwargs):
        listing = ProspectListings(
            hash_code="a" * 16,
            source_id=extract_source_id(page.url) or "new-id",
            listing_source=ListingSource.AUTOTRADER,
            listing_type=listing_type,
            status=ProspectListingStatus.NEW,
            make_and_model="Ford Transit",
            short_description=short_description,
            url=page.url,
        )
        persist_calls.append(listing)
        return listing, 2

    monkeypatch.setattr("autotrader.read_full_prospect_listing", fake_read)
    return page


class TestScrapeListings:
    """Tests for Autotrader skip/save decisions with page I/O faked."""

    def test_skips_existing_source_id_without_reading_detail(self, monkeypatch):
        persist_calls = []
        stub_calls = []
        href = "https://www.autotrader.co.uk/van-details/202606113190878"
        cards = [
            _search_card(
                listing_id="id-202606113190878",
                href=href,
                title="Ford Transit",
                subtitle="LWB",
            )
        ]
        page = _patch_scrape(
            monkeypatch,
            existing_ids={"202606113190878"},
            cards=cards,
            persist_calls=persist_calls,
            stub_calls=stub_calls,
        )

        scrape_listings(page, "https://www.autotrader.co.uk/van-search", ListingType.VAN, "van_prompt.md")

        assert persist_calls == []
        assert stub_calls == []

    def test_persists_unavailable_stub_when_listing_ended(self, monkeypatch):
        persist_calls = []
        stub_calls = []
        href = "https://www.autotrader.co.uk/van-details/999"
        cards = [
            _search_card(
                listing_id="id-999",
                href=href,
                title="Ford Transit",
                subtitle="LWB",
            )
        ]
        page = _patch_scrape(
            monkeypatch,
            existing_ids=set(),
            cards=cards,
            persist_calls=persist_calls,
            stub_calls=stub_calls,
        )
        monkeypatch.setattr("autotrader.is_listing_no_longer_available", lambda _page: True)

        scrape_listings(page, "https://www.autotrader.co.uk/van-search", ListingType.VAN, "van_prompt.md")

        assert persist_calls == []
        assert len(stub_calls) == 1
        assert stub_calls[0]["source_id"] == "999"
        assert stub_calls[0]["listing_source"] == ListingSource.AUTOTRADER

    def test_reads_and_counts_new_private_listing(self, monkeypatch):
        persist_calls = []
        stub_calls = []
        href = "https://www.autotrader.co.uk/van-details/111"
        cards = [
            _search_card(
                listing_id="id-111",
                href=href,
                title="Ford Transit",
                subtitle="LWB",
            )
        ]
        page = _patch_scrape(
            monkeypatch,
            existing_ids=set(),
            cards=cards,
            persist_calls=persist_calls,
            stub_calls=stub_calls,
        )
        monkeypatch.setattr("autotrader.is_listing_no_longer_available", lambda _page: False)

        scrape_listings(page, "https://www.autotrader.co.uk/van-search", ListingType.VAN, "van_prompt.md")

        assert stub_calls == []
        assert len(persist_calls) == 1
        assert persist_calls[0].listing_source == ListingSource.AUTOTRADER
