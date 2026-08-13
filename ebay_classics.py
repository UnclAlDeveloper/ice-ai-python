from ebay import (
    EBAY_CLASSICS_CATEGORY_ID,
    EbayScrapeConfig,
    run_ebay,
)
from models.enums import ListingType


# EBAY CLASSICS
def ebay_classics() -> None:
    """
    Download new eBay classic car listings using the shared eBay downloader and
    persist them with listing type Classic.
    """

    run_ebay(
        EbayScrapeConfig(
            listing_type=ListingType.CLASSIC,
            ai_prompt_filename="classic_car_prompt.md",
            category_id=EBAY_CLASSICS_CATEGORY_ID,
            item_location_country="GB",
            buying_options=["FIXED_PRICE", "CLASSIFIED_AD"],
            sort="newlyListed",
        )
    )


if __name__ == "__main__":
    ebay_classics()
