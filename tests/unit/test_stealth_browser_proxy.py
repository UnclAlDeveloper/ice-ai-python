import pytest

from stealth_browser import _with_session_id, parse_decodo_port_range


class TestParseDecodoPortRange:
    """Tests for Decodo proxy port range parsing."""

    def test_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("DECODO_PORT_RANGE", raising=False)
        assert parse_decodo_port_range() == []

    def test_single_port(self, monkeypatch):
        monkeypatch.setenv("DECODO_PORT_RANGE", "10001")
        assert parse_decodo_port_range() == [10001]

    def test_inclusive_range(self, monkeypatch):
        monkeypatch.setenv("DECODO_PORT_RANGE", "10001-10003")
        assert parse_decodo_port_range() == [10001, 10002, 10003]

    def test_inverted_range_raises(self, monkeypatch):
        monkeypatch.setenv("DECODO_PORT_RANGE", "10010-10001")
        with pytest.raises(ValueError, match="Invalid DECODO_PORT_RANGE"):
            parse_decodo_port_range()


class TestWithSessionId:
    """Tests for Decodo sticky-session username embedding."""

    def test_inserts_before_sessionduration(self):
        username = "user-country-gb-sessionduration-10"
        result = _with_session_id(username, "abc123")
        assert result == "user-country-gb-session-abc123-sessionduration-10"

    def test_noop_when_session_already_present(self):
        username = "user-session-existing"
        assert _with_session_id(username, "newid") == username

    def test_appends_when_no_duration_segment(self):
        username = "user-country-gb"
        assert _with_session_id(username, "abc123") == "user-country-gb-session-abc123"
