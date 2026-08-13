import pytest

from stealth_browser import (
    _greased_brand_list_for_major,
    _navigation_clear_after_captcha_solve,
    _playwright_cookie_entries,
    _sec_ch_ua_for_major,
    _stealth_extra_http_headers,
    extract_turnstile_sitekey_from_html,
)


class TestStealthExtraHttpHeaders:
    """Tests for persistent-context client hint headers."""

    def test_includes_windows_platform_hint(self, monkeypatch):
        monkeypatch.setattr(
            "stealth_browser.STEALTH_SEC_CH_UA",
            '"Chromium";v="151", "Google Chrome";v="151"',
        )
        headers = _stealth_extra_http_headers()
        assert headers["sec-ch-ua-mobile"] == "?0"
        assert headers["sec-ch-ua-platform"] == '"Windows"'


class TestGreasedClientHints:
    """Tests for deterministic Sec-CH-UA GREASE generation."""

    def test_brand_list_has_three_entries(self):
        brands = _greased_brand_list_for_major("151")
        brand_names = {brand for brand, _ in brands}
        assert len(brands) == 3
        assert brand_names == {
            "Chromium",
            "Google Chrome",
            next(name for name in brand_names if name.startswith("Not")),
        }

    def test_grease_brand_is_deterministic(self):
        first = _sec_ch_ua_for_major("151")
        second = _sec_ch_ua_for_major("151")
        assert first == second
        assert "Google Chrome" in first
        assert "Chromium" in first

    def test_major_versions_differ(self):
        assert _sec_ch_ua_for_major("150") != _sec_ch_ua_for_major("151")


class TestTurnstileSitekeyExtraction:
    """Tests for Turnstile sitekey parsing from HTML."""

    def test_extracts_data_sitekey_attribute(self):
        html = '<div class="cf-turnstile" data-sitekey="0xABC123"></div>'
        result = extract_turnstile_sitekey_from_html(html)
        assert result == {"sitekey": "0xABC123"}

    def test_extracts_iframe_k_parameter(self):
        html = (
            '<iframe src="https://challenges.cloudflare.com/turnstile?k=0xDEADBEEF">'
            "</iframe>"
        )
        result = extract_turnstile_sitekey_from_html(html)
        assert result == {"sitekey": "0xDEADBEEF"}

    def test_returns_none_when_missing(self):
        assert extract_turnstile_sitekey_from_html("<html></html>") is None


class TestPlaywrightCookieEntries:
    """Tests for Cloudflare cookie application metadata."""

    def test_cf_clearance_is_secure_and_httponly(self, monkeypatch):
        page = type(
            "Page",
            (),
            {
                "url": "https://www.carandclassic.com/search",
                "context": object(),
            },
        )()
        entries = _playwright_cookie_entries(
            page,
            {"cf_clearance": "token-value"},
        )
        assert len(entries) == 1
        assert entries[0]["name"] == "cf_clearance"
        assert entries[0]["domain"] == "www.carandclassic.com"
        assert entries[0]["secure"] is True
        assert entries[0]["httpOnly"] is True


class TestNavigationClearAfterCaptchaSolve:
    """Tests for post-solve navigation short-circuit."""

    def test_returns_response_when_captcha_cleared(self, monkeypatch):
        page = type("Page", (), {"is_closed": lambda self: False})()
        monkeypatch.setattr(
            "stealth_browser.is_captcha_present",
            lambda _page: False,
        )
        monkeypatch.setattr(
            "stealth_browser.reset_consecutive_captcha_count",
            lambda: None,
        )
        response = object()
        assert _navigation_clear_after_captcha_solve(page, response) is response

    def test_returns_none_when_captcha_still_present(self, monkeypatch):
        page = type("Page", (), {"is_closed": lambda self: False})()
        monkeypatch.setattr(
            "stealth_browser.is_captcha_present",
            lambda _page: True,
        )
        assert _navigation_clear_after_captcha_solve(page, object()) is None
