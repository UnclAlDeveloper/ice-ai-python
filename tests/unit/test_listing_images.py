import shutil
from unittest.mock import MagicMock

from listing_images import (
    CONSECUTIVE_IMAGE_DOWNLOAD_FAILURES_BEFORE_ROTATE,
    download_and_save_listing_images,
)


def _patch_image_downloads(monkeypatch, download_side_effect):
    """Stub network, pause, and S3 so gallery download logic can be unit-tested."""

    monkeypatch.setattr(
        "listing_images._download_image_bytes", download_side_effect
    )
    monkeypatch.setattr("listing_images.pause", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "listing_images._get_prospect_images_aws_access", lambda: MagicMock()
    )
    monkeypatch.setattr("listing_images._save_media_with_retry", lambda *_a, **_k: None)
    hashes = (f"hash{i:012d}" for i in range(100))
    monkeypatch.setattr("listing_images.generate_image_hash", lambda: next(hashes))


class TestDownloadAndSaveListingImages:
    """Tests for gallery download abort behaviour when the proxy looks dead."""

    def test_three_consecutive_failures_stop_remaining_downloads(self, monkeypatch):
        attempted = []

        def fake_download(_page, img_url, **_kwargs):
            attempted.append(img_url)
            return None

        _patch_image_downloads(monkeypatch, fake_download)
        urls = [f"https://cdn.example.com/{i}.jpg" for i in range(8)]

        result = download_and_save_listing_images(
            urls, MagicMock(), MagicMock(id=1), MagicMock()
        )

        assert result is None
        assert attempted == urls[:CONSECUTIVE_IMAGE_DOWNLOAD_FAILURES_BEFORE_ROTATE]
        assert CONSECUTIVE_IMAGE_DOWNLOAD_FAILURES_BEFORE_ROTATE == 3

    def test_success_resets_consecutive_failure_streak(self, monkeypatch):
        attempted = []

        def fake_download(_page, img_url, **_kwargs):
            attempted.append(img_url)
            # fail, fail, succeed, then three failures abort the rest
            if len(attempted) in {1, 2, 4, 5, 6}:
                return None
            return (b"jpeg-bytes", "image/jpeg")

        _patch_image_downloads(monkeypatch, fake_download)
        urls = [f"https://cdn.example.com/{i}.jpg" for i in range(8)]

        result = download_and_save_listing_images(
            urls, MagicMock(), MagicMock(id=1), MagicMock()
        )

        try:
            assert result is not None
            assert attempted == urls[:6]
        finally:
            if result:
                shutil.rmtree(result, ignore_errors=True)
