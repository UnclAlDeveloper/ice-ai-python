from update_select_lists import parse_section


SAMPLE_MARKDOWN = """## Other section
- ignored

## Features
### Exterior
- Alloy wheels
- Tow bar

### Interior
- Air conditioning

## Another section
- also ignored
"""


class TestParseSection:
    """Tests for specs_and_features markdown section parsing."""

    def test_parses_subsections_and_items(self):
        result = parse_section(SAMPLE_MARKDOWN, "Features")

        assert result == {
            "Exterior": ["Alloy wheels", "Tow bar"],
            "Interior": ["Air conditioning"],
        }

    def test_missing_section_returns_empty_dict(self):
        assert parse_section(SAMPLE_MARKDOWN, "Missing") == {}
