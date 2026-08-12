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
            query="classic car",
            category_id=EBAY_CLASSICS_CATEGORY_ID,
            pickup_postal_code="LS1 3AD",
            pickup_radius=100,
        )
    )


if __name__ == "__main__":
    ebay_classics()
