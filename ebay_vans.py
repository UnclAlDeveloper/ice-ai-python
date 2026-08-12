from ebay import (
    EBAY_VANS_CATEGORY_ID,
    EbayScrapeConfig,
    run_ebay,
)
from models.enums import ListingType


# EBAY VANS
def ebay_vans() -> None:
    """
    Download new eBay van listings using the shared eBay downloader and persist
    them with listing type Van.
    """

    run_ebay(
        EbayScrapeConfig(
            listing_type=ListingType.VAN,
            ai_prompt_filename="van_prompt.md",
            query="van",
            category_id=EBAY_VANS_CATEGORY_ID,
            pickup_postal_code="LS1 3AD",
            pickup_radius=100,
        )
    )


if __name__ == "__main__":
    ebay_vans()
