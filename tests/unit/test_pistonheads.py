from datetime import date
from unittest.mock import MagicMock

import pytest

from listing_images import MAX_GALLERY_IMAGES
from pistonheads import (
    _ABORTED_REQUEST_GLOBS,
    _canonical_filtered_search_url,
    _click_view_more,
    _extract_gallery_images_from_html,
    _install_request_filters,
    _parse_engine_size,
    _parse_mot_expiry_date,
    _parse_price_text,
    _quantcast_overlay_blocks_page,
    _search_page_graphql_proxy_failure,
    _search_page_graphql_rate_limited,
    _static_bundle_rate_limited,
    _raise_if_origin_rate_limited,
    extract_source_id,
    scrape_listings,
)
from models.auto_ads import ProspectListings
from models.enums import ListingSource, ListingType, ProspectListingStatus
from scraper_driver import ProxySessionExpired


def _sessionmaker_stub():
    session = MagicMock()
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    return MagicMock(return_value=session_cm), session


class TestExtractSourceId:
    """Tests for PistonHeads listing URL parsing."""

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://www.pistonheads.com/buy/listing/15667955", "15667955"),
            ("https://www.pistonheads.com/buy/listing/15667955?ref=1", "15667955"),
            ("/buy/listing/99", "99"),
        ],
    )
    def test_extracts_listing_id(self, url, expected):
        assert extract_source_id(url) == expected

    @pytest.mark.parametrize(
        "url",
        [None, "", "https://www.pistonheads.com/buy/search", "https://example.com/listing/1"],
    )
    def test_returns_none_when_missing(self, url):
        assert extract_source_id(url) is None


class TestParsePriceText:
    """Tests for visible price strings including POA."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("£19,995", (19995, "£")),
            ("$1,200", (1200, "$")),
            ("€500", (500, "€")),
            ("Guide price £8,500", (8500, "£")),
            ("POA", (None, None)),
            ("Price on application", (None, None)),
            ("", (None, None)),
            (None, (None, None)),
            ("Call for price", (None, None)),
        ],
    )
    def test_parses_or_rejects(self, text, expected):
        assert _parse_price_text(text) == expected


class TestParseEngineSize:
    """Tests for engine-size normalisation."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("5.0L", "5.0L"),
            ("2.0 L", "2.0L"),
            ("1998 cc", "1998 cc"),
            (None, None),
            ("", None),
        ],
    )
    def test_normalises_litres(self, raw, expected):
        assert _parse_engine_size(raw) == expected


class TestParseMotExpiryDate:
    """Tests for MOT expiry parsing from drawer text."""

    def test_parses_abbreviated_month(self):
        assert _parse_mot_expiry_date("12 Jun 2025") == date(2025, 6, 12)

    def test_parses_numeric_uk_date(self):
        assert _parse_mot_expiry_date("12/06/2025") == date(2025, 6, 12)

    def test_exempt_and_full_month_name_are_not_parsed(self):
        assert _parse_mot_expiry_date("MOT exempt") is None
        assert _parse_mot_expiry_date("12 June 2025") is None

    def test_invalid_calendar_date_returns_none(self):
        assert _parse_mot_expiry_date("32/13/2025") is None
        assert _parse_mot_expiry_date(None) is None


class TestSearchPageGraphqlProxyFailure:
    """Tests for classifying failed View more GraphQL requests."""

    def test_returns_tunnel_error_for_search_page(self):
        request = MagicMock()
        request.url = (
            "https://www.pistonheads.com/api/graphql?operationName=SearchPage"
            "&variables=%7B%22offset%22%3A69%7D"
        )
        request.failure = "net::ERR_TUNNEL_CONNECTION_FAILED"

        assert (
            _search_page_graphql_proxy_failure(request)
            == "net::ERR_TUNNEL_CONNECTION_FAILED"
        )

    def test_ignores_analytics_tunnel_failures(self):
        request = MagicMock()
        request.url = "https://www.google.co.uk/ads/ga-audiences?v=1"
        request.failure = "net::ERR_TUNNEL_CONNECTION_FAILED"

        assert _search_page_graphql_proxy_failure(request) is None

    def test_ignores_unrelated_graphql_failures(self):
        request = MagicMock()
        request.url = (
            "https://www.pistonheads.com/api/graphql?operationName=SearchPage"
        )
        request.failure = "net::ERR_ABORTED"

        assert _search_page_graphql_proxy_failure(request) is None


class TestSearchPageGraphqlRateLimited:
    """Tests for classifying HTTP 429 on View more GraphQL responses."""

    def test_detects_search_page_429(self):
        response = MagicMock()
        response.url = (
            "https://www.pistonheads.com/api/graphql?operationName=SearchPage"
            "&variables=%7B%22offset%22%3A16%7D"
        )
        response.status = 429

        assert _search_page_graphql_rate_limited(response) is True

    def test_ignores_successful_search_page(self):
        response = MagicMock()
        response.url = (
            "https://www.pistonheads.com/api/graphql?operationName=SearchPage"
        )
        response.status = 200

        assert _search_page_graphql_rate_limited(response) is False

    def test_ignores_429_on_unrelated_url(self):
        response = MagicMock()
        response.url = "https://www.pistonheads.com/auth/profile"
        response.status = 429

        assert _search_page_graphql_rate_limited(response) is False


class TestStaticBundleRateLimited:
    """Tests for 429s on Next.js search/runtime JS that View more needs."""

    def test_detects_search_chunk_429(self):
        response = MagicMock()
        response.url = (
            "https://www.pistonheads.com/_next/static/chunks/pages/"
            "search-4630beeb1b903c9d.js"
        )
        response.status = 429

        assert _static_bundle_rate_limited(response).endswith(
            "search-4630beeb1b903c9d.js"
        )

    def test_detects_webpack_chunk_429(self):
        response = MagicMock()
        response.url = (
            "https://www.pistonheads.com/_next/static/chunks/"
            "6044-2646418a0daa9fc3.js?dpl=1"
        )
        response.status = 429

        assert _static_bundle_rate_limited(response).endswith(
            "6044-2646418a0daa9fc3.js"
        )

    def test_ignores_svg_and_favicon_429s(self):
        response = MagicMock()
        response.url = (
            "https://www.pistonheads.com/_next/static/media/coupe.79bded64.svg"
        )
        response.status = 429

        assert _static_bundle_rate_limited(response) is None

    def test_ignores_successful_js(self):
        response = MagicMock()
        response.url = (
            "https://www.pistonheads.com/_next/static/chunks/pages/search.js"
        )
        response.status = 200

        assert _static_bundle_rate_limited(response) is None


class TestRaiseIfOriginRateLimited:
    """Tests for rotating when a session has already 429'd search JS."""

    def test_raises_when_reasons_were_recorded(self):
        page = MagicMock()
        page._ice_ph_rate_limit_reasons = [
            "https://www.pistonheads.com/_next/static/chunks/pages/search.js"
        ]

        with pytest.raises(ProxySessionExpired, match="essential assets"):
            _raise_if_origin_rate_limited(page)

    def test_noops_on_plain_mock_page(self):
        _raise_if_origin_rate_limited(MagicMock())


def _view_more_page(*, on_click=None):
    page = MagicMock()
    listeners = {}

    def on(event, handler):
        listeners[event] = handler

    def remove_listener(event, handler):
        if listeners.get(event) is handler:
            listeners.pop(event, None)

    page.on.side_effect = on
    page.remove_listener.side_effect = remove_listener

    button = MagicMock()
    button.first.wait_for = MagicMock()
    button.first.scroll_into_view_if_needed = MagicMock()

    def click(timeout=None):
        if on_click is not None:
            on_click(listeners)

    button.first.click.side_effect = click
    page.locator.return_value = button
    page._listeners = listeners
    return page


class TestClickViewMore:
    """Tests for View more clicks that append cards or die at the proxy."""

    def test_returns_true_when_listing_count_increases(self, monkeypatch):
        monkeypatch.setattr("pistonheads._VIEW_MORE_WAIT_S", 0)
        monkeypatch.setattr("pistonheads._poll_cookie_consent", lambda _page: None)
        counts = iter([18, 36])
        monkeypatch.setattr(
            "pistonheads.quick_locator_count",
            lambda *_a, **_k: next(counts, 36),
        )

        assert _click_view_more(_view_more_page()) is True

    def test_returns_false_when_count_stays_the_same(self, monkeypatch):
        monkeypatch.setattr("pistonheads._VIEW_MORE_WAIT_S", 0)
        monkeypatch.setattr("pistonheads._poll_cookie_consent", lambda _page: None)
        monkeypatch.setattr(
            "pistonheads.quick_locator_count", lambda *_a, **_k: 18
        )

        assert _click_view_more(_view_more_page()) is False

    def test_raises_when_search_page_graphql_tunnel_fails(self, monkeypatch):
        monkeypatch.setattr("pistonheads._VIEW_MORE_WAIT_S", 0)
        monkeypatch.setattr("pistonheads._poll_cookie_consent", lambda _page: None)
        monkeypatch.setattr(
            "pistonheads.quick_locator_count", lambda *_a, **_k: 69
        )

        def on_click(listeners):
            request = MagicMock()
            request.url = (
                "https://www.pistonheads.com/api/graphql?operationName=SearchPage"
                "&variables=%7B%22offset%22%3A69%7D"
            )
            request.failure = "net::ERR_TUNNEL_CONNECTION_FAILED"
            listeners["requestfailed"](request)

        with pytest.raises(ProxySessionExpired, match="GraphQL fetch failed"):
            _click_view_more(_view_more_page(on_click=on_click))

    def test_retries_after_graphql_429_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("pistonheads._VIEW_MORE_WAIT_S", 0)
        monkeypatch.setattr("pistonheads._VIEW_MORE_RATE_LIMIT_BACKOFF_S", 0)
        monkeypatch.setattr("pistonheads._poll_cookie_consent", lambda _page: None)
        counts = iter([16, 16, 33])
        monkeypatch.setattr(
            "pistonheads.quick_locator_count",
            lambda *_a, **_k: next(counts, 33),
        )
        clicks = {"n": 0}

        def on_click(listeners):
            clicks["n"] += 1
            if clicks["n"] == 1:
                response = MagicMock()
                response.url = (
                    "https://www.pistonheads.com/api/graphql"
                    "?operationName=SearchPage&variables=%7B%22offset%22%3A16%7D"
                )
                response.status = 429
                listeners["response"](response)

        assert _click_view_more(_view_more_page(on_click=on_click)) is True
        assert clicks["n"] == 2

    def test_raises_when_graphql_429_persists(self, monkeypatch):
        monkeypatch.setattr("pistonheads._VIEW_MORE_WAIT_S", 0)
        monkeypatch.setattr("pistonheads._VIEW_MORE_RATE_LIMIT_RETRIES", 0)
        monkeypatch.setattr("pistonheads._poll_cookie_consent", lambda _page: None)
        monkeypatch.setattr(
            "pistonheads.quick_locator_count", lambda *_a, **_k: 16
        )

        def on_click(listeners):
            response = MagicMock()
            response.url = (
                "https://www.pistonheads.com/api/graphql?operationName=SearchPage"
            )
            response.status = 429
            listeners["response"](response)

        with pytest.raises(ProxySessionExpired, match="rate limited"):
            _click_view_more(_view_more_page(on_click=on_click))

    def test_raises_when_search_js_bundle_is_429(self, monkeypatch):
        monkeypatch.setattr("pistonheads._VIEW_MORE_WAIT_S", 0)
        monkeypatch.setattr("pistonheads._poll_cookie_consent", lambda _page: None)
        monkeypatch.setattr(
            "pistonheads.quick_locator_count", lambda *_a, **_k: 16
        )

        def on_click(listeners):
            response = MagicMock()
            response.url = (
                "https://www.pistonheads.com/_next/static/chunks/pages/"
                "search-4630beeb1b903c9d.js"
            )
            response.status = 429
            listeners["response"](response)

        with pytest.raises(ProxySessionExpired, match="essential assets"):
            _click_view_more(_view_more_page(on_click=on_click))

    def test_ignores_analytics_failures_when_listings_do_not_increase(
        self, monkeypatch
    ):
        monkeypatch.setattr("pistonheads._VIEW_MORE_WAIT_S", 0)
        monkeypatch.setattr("pistonheads._poll_cookie_consent", lambda _page: None)
        monkeypatch.setattr(
            "pistonheads.quick_locator_count", lambda *_a, **_k: 18
        )

        def on_click(listeners):
            request = MagicMock()
            request.url = "https://region1.analytics.google.com/g/collect?v=2"
            request.failure = "net::ERR_TUNNEL_CONNECTION_FAILED"
            listeners["requestfailed"](request)

        assert _click_view_more(_view_more_page(on_click=on_click)) is False

    def test_removes_request_listeners(self, monkeypatch):
        monkeypatch.setattr("pistonheads._VIEW_MORE_WAIT_S", 0)
        monkeypatch.setattr("pistonheads._poll_cookie_consent", lambda _page: None)
        monkeypatch.setattr(
            "pistonheads.quick_locator_count", lambda *_a, **_k: 18
        )
        page = _view_more_page()

        _click_view_more(page)

        assert "requestfailed" not in page._listeners
        assert "response" not in page._listeners

    def test_retries_when_consent_overlay_intercepts_click(self, monkeypatch):
        monkeypatch.setattr("pistonheads._VIEW_MORE_WAIT_S", 0)
        monkeypatch.setattr("pistonheads._poll_cookie_consent", lambda _page: None)
        monkeypatch.setattr("pistonheads._ensure_consent_cleared", lambda _page: None)
        counts = iter([16, 33])
        monkeypatch.setattr(
            "pistonheads.quick_locator_count",
            lambda *_a, **_k: next(counts, 33),
        )
        clicks = {"n": 0}

        def on_click(_listeners):
            clicks["n"] += 1
            if clicks["n"] == 1:
                raise RuntimeError("intercepts pointer events")

        monkeypatch.setattr(
            "pistonheads._is_cookie_consent_visible",
            lambda _page: clicks["n"] == 1,
        )

        assert _click_view_more(_view_more_page(on_click=on_click)) is True
        assert clicks["n"] == 2

    def test_retries_when_consent_overlay_blocks_new_listings(self, monkeypatch):
        monkeypatch.setattr("pistonheads._VIEW_MORE_WAIT_S", 0)
        monkeypatch.setattr("pistonheads._poll_cookie_consent", lambda _page: None)
        monkeypatch.setattr("pistonheads._ensure_consent_cleared", lambda _page: None)
        counts = iter([16, 16, 33])
        monkeypatch.setattr(
            "pistonheads.quick_locator_count",
            lambda *_a, **_k: next(counts, 33),
        )
        clicks = {"n": 0}

        def on_click(_listeners):
            clicks["n"] += 1

        monkeypatch.setattr(
            "pistonheads._is_cookie_consent_visible",
            lambda _page: clicks["n"] == 1,
        )

        assert _click_view_more(_view_more_page(on_click=on_click)) is True
        assert clicks["n"] == 2


class TestQuantcastOverlayBlocksPage:
    """Tests for treating a live Quantcast CMP node as a blocking overlay."""

    def test_true_only_when_evaluate_returns_true(self):
        page = MagicMock()
        page.evaluate.return_value = True
        assert _quantcast_overlay_blocks_page(page) is True

        page.evaluate.return_value = False
        assert _quantcast_overlay_blocks_page(page) is False

        page.evaluate.return_value = MagicMock()
        assert _quantcast_overlay_blocks_page(page) is False


class TestInstallRequestFilters:
    """Tests for aborting Next.js prefetch that 429s the origin."""

    def test_registers_abort_routes(self):
        page = MagicMock()

        _install_request_filters(page)

        assert page.route.call_count == len(_ABORTED_REQUEST_GLOBS)
        patterns = [call.args[0] for call in page.route.call_args_list]
        assert "**/pistonheads.com/_next/data/**" in patterns
        assert "**/pistonheads.com/auth/profile*" in patterns
        page.on.assert_called()
        assert page._ice_ph_rate_limit_reasons == []


class TestCanonicalFilteredSearchUrl:
    """Tests for pinning search results to private classifieds, most recent."""

    def test_sets_filters_and_drops_pagination(self):
        url = _canonical_filtered_search_url(
            "https://www.pistonheads.com/buy/search?offset=16&seller-type=Trade"
        )
        assert "seller-type=Private" in url
        assert "listing-type=classifieds" in url
        assert "sort=mostRecent" in url
        assert "offset=" not in url


class TestExtractGalleryImagesFromHtml:
    """Tests for PistonHeads CDN URL collection."""

    def test_prefers_fullsize_and_skips_static_assets(self):
        html = """
        <img src="https://img.pistonheads.com/LargeSize/photo.jpg?w=1">
        <img src="https://img.pistonheads.com/static-assets/logo.png">
        <img src="https://img.pistonheads.com/Fullsize/photo.jpg">
        """
        urls = _extract_gallery_images_from_html(html)
        assert urls == ["https://img.pistonheads.com/Fullsize/photo.jpg"]

    def test_caps_gallery_at_max_images(self):
        html = "\n".join(
            f'<img src="https://img.pistonheads.com/Fullsize/photo{i}.jpg">'
            for i in range(MAX_GALLERY_IMAGES + 20)
        )
        urls = _extract_gallery_images_from_html(html)
        assert len(urls) == MAX_GALLERY_IMAGES


def _patch_scrape(monkeypatch, *, existing_ids, candidates, persist_calls, stub_calls):
    factory, _session = _sessionmaker_stub()
    monkeypatch.setattr("pistonheads._MAX_SEARCH_BATCHES", 1)
    monkeypatch.setattr("pistonheads._load_search_batch", lambda *_a, **_k: 1)
    monkeypatch.setattr(
        "pistonheads._open_listing_page", lambda search_page: search_page
    )
    monkeypatch.setattr(
        "pistonheads.get_existing_source_ids", lambda _source: set(existing_ids)
    )
    monkeypatch.setattr("pistonheads.create_engine_with_retry", lambda _url: MagicMock())
    monkeypatch.setattr("pistonheads.sessionmaker", lambda **_kwargs: factory)
    monkeypatch.setattr(
        "pistonheads.goto_with_captcha_handling",
        lambda *_a, **_k: MagicMock(status=200),
    )
    monkeypatch.setattr("pistonheads.accept_cookies", lambda *_a, **_k: None)
    monkeypatch.setattr("pistonheads.pause_for_page", lambda *_a, **_k: None)
    monkeypatch.setattr("pistonheads.wait_for_listing_detail_page", lambda *_a, **_k: None)
    monkeypatch.setattr("pistonheads.search_results_present", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "pistonheads._snapshot_listing_candidates", lambda *_a, **_k: candidates
    )
    monkeypatch.setattr("pistonheads.is_http_not_found", lambda _response: False)
    monkeypatch.setattr(
        "pistonheads.mark_listing_processed_and_check_availability",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "pistonheads.persist_unavailable_listing_stub",
        lambda *_a, **kwargs: stub_calls.append(kwargs),
    )

    def fake_persist(_session, prospect_listing, **_kwargs):
        persist_calls.append(prospect_listing)
        return prospect_listing

    monkeypatch.setattr("pistonheads.persist_listing_with_images_and_ai", fake_persist)

    def fake_extract(_page, hash_code, source_id, fallback_title=None):
        listing = ProspectListings(
            hash_code=hash_code,
            source_id=source_id,
            listing_source=ListingSource.PISTONHEADS,
            listing_type=ListingType.CLASSIC,
            status=ProspectListingStatus.NEW,
            make_and_model=fallback_title or "Jaguar E-Type",
            short_description=fallback_title or "Jaguar E-Type",
            url=f"https://www.pistonheads.com/buy/listing/{source_id}",
        )
        return listing, ["https://img.pistonheads.com/Fullsize/a.jpg"]

    monkeypatch.setattr("pistonheads.extract_listing_details", fake_extract)
    return MagicMock()


class TestScrapeListings:
    """Tests for PistonHeads skip/save decisions with page I/O faked."""

    def test_skips_existing_source_id(self, monkeypatch):
        persist_calls = []
        stub_calls = []
        page = _patch_scrape(
            monkeypatch,
            existing_ids={"15667955"},
            candidates=[
                (
                    "15667955",
                    "https://www.pistonheads.com/buy/listing/15667955",
                    "15667955",
                    "Jaguar E-Type",
                )
            ],
            persist_calls=persist_calls,
            stub_calls=stub_calls,
        )

        scrape_listings(page, "https://www.pistonheads.com/buy/search")

        assert persist_calls == []
        assert stub_calls == []

    def test_persists_unavailable_stub(self, monkeypatch):
        persist_calls = []
        stub_calls = []
        page = _patch_scrape(
            monkeypatch,
            existing_ids=set(),
            candidates=[
                (
                    "99",
                    "https://www.pistonheads.com/buy/listing/99",
                    "99",
                    "Jaguar E-Type",
                )
            ],
            persist_calls=persist_calls,
            stub_calls=stub_calls,
        )
        monkeypatch.setattr(
            "pistonheads.is_listing_no_longer_available", lambda _page: True
        )

        scrape_listings(page, "https://www.pistonheads.com/buy/search")

        assert persist_calls == []
        assert len(stub_calls) == 1
        assert stub_calls[0]["source_id"] == "99"
        assert stub_calls[0]["listing_source"] == ListingSource.PISTONHEADS

    def test_persists_new_listing(self, monkeypatch):
        persist_calls = []
        stub_calls = []
        page = _patch_scrape(
            monkeypatch,
            existing_ids=set(),
            candidates=[
                (
                    "100",
                    "https://www.pistonheads.com/buy/listing/100",
                    "100",
                    "Jaguar E-Type",
                )
            ],
            persist_calls=persist_calls,
            stub_calls=stub_calls,
        )
        monkeypatch.setattr(
            "pistonheads.is_listing_no_longer_available", lambda _page: False
        )

        scrape_listings(page, "https://www.pistonheads.com/buy/search")

        assert stub_calls == []
        assert len(persist_calls) == 1
        assert persist_calls[0].source_id == "100"
        assert persist_calls[0].listing_source == ListingSource.PISTONHEADS
