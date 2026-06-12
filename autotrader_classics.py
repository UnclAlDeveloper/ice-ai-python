from autotrader import AUTOTRADER_CARS_URL, AutotraderScrapeConfig, run_autotrader
from models.enums import ListingType


# AUTOTRADER CLASSICS
def autotrader_classics() -> None:
    """
    Scrape new Autotrader classic car listings from the cars landing page using
    the shared proxy-rotation driver and persist them with listing type Classic.
    """

    run_autotrader(
        AutotraderScrapeConfig(
            landing_url=AUTOTRADER_CARS_URL,
            listing_type=ListingType.CLASSIC,
            ai_prompt_filename="classic_car_prompt.md",
            select_classic_cars=True,
        )
    )


if __name__ == "__main__":
    autotrader_classics()
