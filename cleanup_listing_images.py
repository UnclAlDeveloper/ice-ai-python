import os

from environments import load_environment

# load env vars before importing listing_images so AWSAccess can read them at class definition
load_environment()

from sqlalchemy.orm import Session

from common import create_engine_with_retry, with_db_retry
from listing_images import delete_listing_images
from models.auto_ads import ProspectListings
from models.enums import ProspectListingStatus

KEEP_STATUSES = (
    ProspectListingStatus.NEW,
    ProspectListingStatus.BOUGHT,
    ProspectListingStatus.INTERESTED,
)


# CLEANUP LISTING IMAGES
def cleanup_listing_images() -> None:
    """
    Delete every image whose prospect listing is not in an active status of
    New, Bought or Interested. The existing delete_listing_images helper is
    used per listing so each image is removed from both the S3 bucket and
    the images table inside a single transaction.
    """

    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    engine = create_engine_with_retry(database_url)

    with Session(engine) as session:
        # find every prospect listing that should have its images purged,
        # retrying a transient db hiccup at startup rather than aborting
        listings_to_clean = with_db_retry(
            lambda: (
                session.query(ProspectListings)
                .filter(ProspectListings.status.notin_(KEEP_STATUSES))
                .all()
            ),
            description="load listings for image cleanup",
        )

        print(
            f"Found {len(listings_to_clean)} prospect listing(s) with a status "
            f"outside {tuple(s.value for s in KEEP_STATUSES)}; deleting their images."
        )

        total_deleted = 0
        listings_with_deletions = 0

        # delegate to the shared helper so s3 objects and rows stay in sync
        for listing in listings_to_clean:
            deleted = delete_listing_images(listing, session)
            if deleted > 0:
                listings_with_deletions += 1
                total_deleted += deleted

        print(
            f"\nDone. Deleted {total_deleted} image(s) across "
            f"{listings_with_deletions} listing(s)."
        )


if __name__ == "__main__":
    cleanup_listing_images()
