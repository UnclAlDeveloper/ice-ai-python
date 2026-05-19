import os
import tempfile
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from playwright.sync_api import Page

from AWSAccess import AWSAccess
from common import generate_image_hash, pause
from models.auto_ads import Images, ProspectListings
from models.enums import ListingSource, ListingTable

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


# GET PROSPECT IMAGES AWS ACCESS
def _get_prospect_images_aws_access() -> AWSAccess:
    """
    Build an AWSAccess client pointed at the prospect listings image directory.
    Centralised here so save and delete operations share identical bucket and
    path configuration.
    """

    return AWSAccess(
        bucket_name=os.getenv("AUTO_ADS_BUCKET"),
        media_dir="images",
        sub_directory_name="prospects",
    )


# DOWNLOAD AND SAVE LISTING IMAGES
def download_and_save_listing_images(
    image_urls: list[str],
    page: Page,
    prospect_listing: ProspectListings,
    session: "Session",
    temp_dir_prefix: str = "listing_images_",
) -> Optional[str]:
    """
    Fetch each image URL via HTTP, save to a temporary directory and S3, and create
    Images database records. Returns the path to the temporary directory, or None
    if no images were saved.
    """

    if not image_urls:
        return None

    # create temporary directory for storing images
    temp_dir = tempfile.mkdtemp(prefix=temp_dir_prefix)

    # use aws access for saving and retrieving images from s3
    aws_access = _get_prospect_images_aws_access()

    saved_count = 0
    for index, img_url in enumerate(image_urls):
        try:
            # pause between downloading images
            if index > 0:
                pause(0.5, 1.5)

            # fetch the image via http request
            response = page.request.get(img_url)
            if response.status != 200:
                print(f"Failed to fetch image {index}: HTTP {response.status}")
                continue

            image_bytes = response.body()

            # skip if no valid image data
            if not image_bytes:
                continue

            # determine file extension from content type or url
            content_type = response.headers.get("content-type", "")
            if "jpeg" in content_type or "jpg" in content_type:
                extension = "jpg"
            elif "png" in content_type:
                extension = "png"
            elif "webp" in content_type:
                extension = "webp"
            elif "gif" in content_type:
                extension = "gif"
            else:
                # try to extract from url
                url_lower = img_url.lower()
                if ".jpg" in url_lower or ".jpeg" in url_lower:
                    extension = "jpg"
                elif ".png" in url_lower:
                    extension = "png"
                elif ".webp" in url_lower:
                    extension = "webp"
                elif ".gif" in url_lower:
                    extension = "gif"
                else:
                    extension = "jpg"  # default to jpg

            # generate a 16-character random hash for the filename
            image_hash = generate_image_hash()

            # save to temporary directory
            temp_file_path = os.path.join(temp_dir, f"{image_hash}.{extension}")
            with open(temp_file_path, "wb") as f:
                f.write(image_bytes)

            # save to s3
            aws_access.save_media(image_hash, extension, image_bytes)

            # get the s3 url
            s3_url = aws_access.get_media_url(image_hash, extension)

            # create images database record
            image_record = Images(
                listing_table=ListingTable.PROSPECT,
                listing_id=prospect_listing.id,
                url=s3_url,
                is_primary=(index == 0),
                created_at=datetime.now(),
            )
            session.add(image_record)
            saved_count += 1

        except Exception as e:
            print(f"Error extracting image {index}: {e}")
            continue

    # commit all image records
    session.commit()

    if saved_count > 0:
        print(
            f"Saved {saved_count} images for listing {prospect_listing.id} "
            f"to S3 and temp directory: {temp_dir}"
        )
        return temp_dir

    # clean up empty temp dir when no images were saved
    if os.path.isdir(temp_dir):
        os.rmdir(temp_dir)
    return None


# DELETE LISTING IMAGES
def delete_listing_images(
    prospect_listing: ProspectListings,
    session: "Session",
) -> int:
    """
    Remove every image associated with a prospect listing from both S3 and the
    Images table. Call this when a listing transitions to a status where its
    photos are no longer needed (e.g. NotAvailable or NotInterested) so that
    the bucket does not accumulate orphaned media. Returns the number of image
    records that were deleted.
    """

    # find every image row that belongs to this prospect listing
    image_records = (
        session.query(Images)
        .filter(
            Images.listing_id == prospect_listing.id,
            Images.listing_table == ListingTable.PROSPECT,
        )
        .all()
    )

    if not image_records:
        return 0

    aws_access = _get_prospect_images_aws_access()

    deleted_count = 0
    for image_record in image_records:
        try:
            # derive the s3 hash name and extension back out of the stored url
            filename = image_record.url.rsplit("/", 1)[-1]
            name, ext = os.path.splitext(filename)
            ext = ext.lstrip(".")

            # remove the underlying object from s3; ignore_if_not_exists keeps
            # the database cleanup robust even when the bucket entry is gone
            aws_access.remove_media(name, ext, ignore_if_not_exists=True)

            session.delete(image_record)
            deleted_count += 1
        except Exception as e:
            print(
                f"Error deleting image {image_record.id} for listing "
                f"{prospect_listing.id}: {e}"
            )
            continue

    # commit the row deletions in a single transaction
    session.commit()

    if deleted_count > 0:
        print(
            f"Deleted {deleted_count} images for listing {prospect_listing.id} "
            f"from S3 and database"
        )

    return deleted_count
