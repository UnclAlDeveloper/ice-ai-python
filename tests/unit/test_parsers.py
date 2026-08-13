from common import parse_mileage as common_parse_mileage
from listing_images import (
    MAX_GALLERY_IMAGES,
    cap_gallery_urls,
    merge_image_url_lists,
    normalize_image_url,
)
from scraper_driver import replace_query_params


class TestSharedScraperHelpers:
    """Tests for helpers shared across listing scrapers."""

    def test_common_parse_mileage(self):
        assert common_parse_mileage("81,000 mi") == (81000, "mi")
        assert common_parse_mileage("138,100 Miles") == (138100, "Miles")
        assert common_parse_mileage(None) == (None, None)
        assert common_parse_mileage("") == (None, None)
        assert common_parse_mileage("invalid") == (None, None)

    def test_normalize_and_merge_image_urls(self):
        merged = merge_image_url_lists(
            ["https://cdn.example.com/a.jpg?w=1"],
            ["https://cdn.example.com/a.jpg?w=2", "https://cdn.example.com/b.jpg"],
        )
        assert merged == [
            "https://cdn.example.com/a.jpg?w=1",
            "https://cdn.example.com/b.jpg",
        ]
        assert (
            normalize_image_url(
                "https://cdn.example.com/LargeSize/a.jpg?x=1",
                path_replacements=(("/LargeSize/", "/Fullsize/"),),
            )
            == "https://cdn.example.com/Fullsize/a.jpg"
        )

    def test_cap_gallery_urls(self):
        assert len(cap_gallery_urls([f"u{i}" for i in range(5)], max_images=3)) == 3
        uncapped = [f"u{i}" for i in range(MAX_GALLERY_IMAGES - 1)]
        assert cap_gallery_urls(uncapped) == uncapped
        assert (
            len(cap_gallery_urls([f"u{i}" for i in range(MAX_GALLERY_IMAGES + 20)]))
            == MAX_GALLERY_IMAGES
        )
        assert MAX_GALLERY_IMAGES == 30

    def test_replace_query_params(self):
        url = replace_query_params(
            "https://example.com/search?page=2&keep=1",
            drop=("page",),
            set_params={"sort": "latest"},
            prefer_keys=["sort", "keep"],
        )
        assert url == "https://example.com/search?sort=latest&keep=1"
