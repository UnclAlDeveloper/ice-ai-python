import os
import tempfile
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from stealth_browser import Page

from AWSAccess import AWSAccess
from common import (
    TIMEOUT_BACKOFF_MS,
    generate_image_hash,
    is_playwright_timeout,
    pause,
)
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


# DOWNLOAD IMAGE BYTES
def _download_image_bytes(
    page: Page, img_url: str, *, backoff: tuple[int, ...] = TIMEOUT_BACKOFF_MS
) -> Optional[tuple[bytes, str]]:
    """
    Fetch an image url through the browser's request context, retrying on
    transient network failures such as TLS socket disconnects through the proxy.
    Each attempt is given a progressively longer timeout (30s, 1m, 2m, 4m) so a
    slow-loading image gets more time before being abandoned. Returns a (bytes,
    content_type) tuple on success, or None when every attempt fails or returns
    a non-200 status / empty body.
    """

    last_error: object = None
    attempts = len(backoff)
    for index, timeout_ms in enumerate(backoff):
        try:
            response = page.request.get(img_url, timeout=timeout_ms)
            if response.status != 200:
                last_error = f"HTTP {response.status}"
            else:
                body = response.body()
                if body:
                    return body, response.headers.get("content-type", "")
                last_error = "empty body"
        except Exception as e:
            last_error = e

            # note when a slow image is getting a longer deadline next time
            if is_playwright_timeout(e) and index < attempts - 1:
                print(
                    f"  Image download timed out after {timeout_ms / 1000:.0f}s; "
                    f"retrying with a {backoff[index + 1] / 1000:.0f}s timeout..."
                )

        # back off briefly before retrying a transient failure
        if index < attempts - 1:
            pause(1.0, 3.0)

    print(f"  Failed to download image after {attempts} attempts: {last_error}")
    return None


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

            # fetch the image, retrying transient proxy/tls disconnects
            download = _download_image_bytes(page, img_url)
            if download is None:
                continue
            image_bytes, content_type = download

            # determine file extension from content type or url
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


# DELETE LISTING
def delete_listing(
    prospect_listing: ProspectListings,
    session: "Session",
) -> None:
    """
    Discard a prospect listing entirely: its images and their S3 objects (by
    reusing delete_listing_images) followed by the listing row itself. Use this
    when a listing was only partially saved so it is never persisted in an
    incomplete state and can be re-fetched cleanly on a later run.
    """

    # capture the id before deletion so it can still be logged afterwards
    listing_id = prospect_listing.id

    # remove the associated images and their s3 objects first
    delete_listing_images(prospect_listing, session)

    # then remove the listing row itself
    session.delete(prospect_listing)
    session.commit()
    print(f"Deleted listing {listing_id} and its images")


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
