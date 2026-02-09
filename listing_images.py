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


# DOWNLOAD AND SAVE LISTING IMAGES
def download_and_save_listing_images(
    image_urls: list[str],
    page: Page,
    prospect_listing: ProspectListings,
    session: "Session",
    listing_source: ListingSource,
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
    aws_access = AWSAccess(
        bucket_name=os.getenv("AUTO_ADS_BUCKET"),
        media_dir="images",
        sub_directory_name="prospects",
    )

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
                listing_source=listing_source,
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
