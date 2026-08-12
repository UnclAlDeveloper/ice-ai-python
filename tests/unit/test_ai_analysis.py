from ai_analysis import apply_ai_analysis


SAMPLE_ANALYSIS = """```markdown
# Overview
Strong resale potential for a well-maintained van.

# Repair costs
| Item | Cost |
| Rust repair | £500 |

# Notes
Check MOT history.

# Price ranges
**Low buy price:** £12,000
**High buy price:** £14,500
**Expected repair cost:** £1,200
**Low sell price:** £16,000
**High sell price:** £18,500
```"""


class TestApplyAiAnalysis:
    """Tests for Gemini markdown parsing into prospect listing fields."""

    def test_parses_sections_and_prices(self, prospect_listing_stub):
        result = apply_ai_analysis(prospect_listing_stub, SAMPLE_ANALYSIS)

        assert result.ai_resell_overview.startswith("Strong resale potential")
        assert "Rust repair" in result.ai_work_and_repairs
        assert result.ai_resell_notes == "Check MOT history."
        assert result.ai_buy_price_low == 12000
        assert result.ai_buy_price_high == 14500
        assert result.ai_repair_cost == 1200
        assert result.ai_sell_price_low == 16000
        assert result.ai_sell_price_high == 18500

    def test_missing_price_ranges_clears_price_fields(self, prospect_listing_stub):
        analysis = "# Overview\nNo prices here.\n"
        result = apply_ai_analysis(prospect_listing_stub, analysis)

        assert result.ai_buy_price_low is None
        assert result.ai_buy_price_high is None
        assert result.ai_repair_cost is None
        assert result.ai_sell_price_low is None
        assert result.ai_sell_price_high is None

    def test_junk_prices_become_none(self, prospect_listing_stub):
        analysis = """# Price ranges
**Low buy price:** £TBC
**High buy price:** £14,500
"""
        result = apply_ai_analysis(prospect_listing_stub, analysis)

        assert result.ai_buy_price_low is None
        assert result.ai_buy_price_high == 14500

    def test_unfenced_markdown_with_bold_labels(self, prospect_listing_stub):
        analysis = """# Overview
Brief summary.

# Price ranges
**Low buy price:** £10,000
**High buy price:** £11,000
"""
        result = apply_ai_analysis(prospect_listing_stub, analysis)

        assert result.ai_resell_overview == "Brief summary."
        assert result.ai_buy_price_low == 10000
