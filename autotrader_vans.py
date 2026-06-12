from autotrader import AUTOTRADER_VANS_URL, AutotraderScrapeConfig, run_autotrader
from models.enums import ListingType


# AUTOTRADER VANS
def autotrader_vans() -> None:
    """
    Scrape new Autotrader van listings using the shared proxy-rotation driver and
    persist them with listing type Van.
    """

    run_autotrader(
        AutotraderScrapeConfig(
            landing_url=AUTOTRADER_VANS_URL,
            listing_type=ListingType.VAN,
            ai_prompt_filename="van_prompt.md",
        )
    )


if __name__ == "__main__":
    autotrader_vans()
