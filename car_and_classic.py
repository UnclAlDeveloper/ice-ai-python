import sys
import playwright
import os

print("PYTHONPATH =", os.environ.get("PYTHONPATH"))

import json
import os
import random
import re
import time
from datetime import date, datetime
from urllib.parse import parse_qsl, urlparse

from runtime_flags import resolve_runtime_flags

is_headless = resolve_runtime_flags()

from environments import load_environment

load_environment()

from stealth_browser import (
    Page,
    PageUnresponsiveError,
    goto_with_captcha_handling,
    page_html,
    quick_locator_count,
    run_quick_page_action,
)
from sqlalchemy.orm import sessionmaker

from common import (
    create_engine_with_retry,
    generate_hash_code,
    get_existing_source_ids,
    is_http_not_found,
    is_playwright_timeout,
    parse_mileage,
    pause,
    pause_with_poll,
    run_with_timeout_backoff,
    wait_for_selector_with_backoff,
)
from listing_images import (
    append_unique_image_url,
    cap_gallery_urls,
    merge_image_url_lists,
    normalize_image_url,
)
from models.auto_ads import ProspectListings
from models.enums import ListingSource, ListingType, ProspectListingStatus
from scraper_driver import (
    ProxyRotationConfig,
    ProxySessionExpired,
    ScrapeResumeState,
    log_already_exists,
    log_already_processed,
    log_listing_error,
    log_loaded_source_ids,
    log_no_listings_on_page,
    log_not_available,
    log_paginating_start,
    log_repeat_page,
    log_resume_scrape,
    log_results_page,
    log_saved_listing,
    log_scrape_finished,
    log_sold_under_offer_card,
    log_timestamp,
    mark_listing_processed_and_check_availability,
    open_proxied_session,
    persist_listing_with_images_and_ai,
    persist_unavailable_listing_stub,
    replace_query_params,
    reraise_or_log_listing_error,
    run_consent_dismiss_loop,
    run_with_proxy_rotation,
    search_results_present,
    with_page_param,
)

UNAVAILABLE_ADVERT_TEXTS = (
    "This advert has now been removed through sale or otherwise",
)

CONFIG = ProxyRotationConfig.from_env_prefix(
    "CAR_AND_CLASSIC",
    max_consecutive_captchas_before_rotation=3,
)

# cars in the UK from private sellers, newest first; applied via query params
# because the All filters overlay needs client-side JS that Cloudflare often
# blocks on proxied asset requests (button visible, click does nothing)
_FILTERED_SEARCH_URL = (
    "https://www.carandclassic.com/search"
    "?vehicle_type=cars"
    "&country=GB"
    "&seller_type=private"
    "&sort=latest"
    "&source=modal-sort"
)

# title or h1 patterns that indicate a listing is sold or under offer
TITLE_SOLD_OR_UNDER_OFFER = re.compile(
    r"(?i)\bunder\s+offer\b|\b(?:already\s+)?sold\b"
)

# section headings that can appear as section h1 on the detail page and must
# never be treated as the vehicle make/model
_NON_TITLE_HEADINGS = frozenset(
    {
        "highlights",
        "gallery",
        "description",
        "vehicle background",
        "asking price",
        "overview",
        "specification",
        "specifications",
        "seller",
        "location",
    }
)

# LISTING PAUSE MIN S
_LISTING_PAUSE_MIN_S = 5.0

# LISTING PAUSE MAX S
_LISTING_PAUSE_MAX_S = 12.0

# SEARCH WARMUP MIN S
_SEARCH_WARMUP_MIN_S = 2.0

# SEARCH WARMUP MAX S
_SEARCH_WARMUP_MAX_S = 5.0

# cap search pagination at 20 pages (~60 cards per page, ~1200 listings total)
_MAX_SEARCH_PAGES = 20


# WARM UP SEARCH SESSION
def _warm_up_search_session(page: Page) -> None:
    """
    Scroll and idle on the search results page so the first Cloudflare session
    looks less like an immediate automation burst after filter navigation.
    """

    try:
        page.mouse.wheel(0, 500)
        pause(_SEARCH_WARMUP_MIN_S, _SEARCH_WARMUP_MAX_S)
        page.mouse.wheel(0, -250)
        pause(1.0, 2.0)
    except Exception:
        pass


# IS NON TITLE HEADING
def _is_non_title_heading(text: str | None) -> bool:
    """
    Return True when text looks like a page section heading rather than a
    vehicle make and model string.
    """

    if not text:
        return True
    return text.strip().lower() in _NON_TITLE_HEADINGS


# NORMALIZE IMAGE URL
def _normalize_image_url(url: str) -> str:
    """
    Strip query and fragment so the same photo at different CDN sizes is only
    kept once when collecting gallery image urls.
    """

    return normalize_image_url(url)


# APPEND UNIQUE IMAGE URL
def _append_unique_image_url(image_urls: list[str], seen: set[str], src: str | None) -> None:
    """
    Add an http(s) image src to image_urls when its normalised form has not
    already been collected.
    """

    append_unique_image_url(image_urls, seen, src)


# MERGE IMAGE URL LISTS
def _merge_image_url_lists(*sources: list[str]) -> list[str]:
    """
    Merge multiple image url lists, de-duplicating by normalised path and
    keeping the first url variant encountered for each photo.
    """

    return merge_image_url_lists(*sources)


# INERTIA GALLERY IMAGES MARKER
_INERTIA_GALLERY_IMAGES_MARKER = '"images":[{"xs":'

# INERTIA GALLERY IMAGE SIZES
_INERTIA_GALLERY_IMAGE_SIZES = ("xxl", "xl", "lg", "md", "sm", "xs")


# FIND JSON ARRAY END
def _find_json_array_end(content: str, start: int) -> int | None:
    """
    Return the index one past the closing bracket of a JSON array that starts
    at start, or None when the array is not well-formed.
    """

    if start >= len(content) or content[start] != "[":
        return None

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(content)):
        character = content[index]
        if in_string:
            if escape:
                escape = False
            elif character == "\\":
                escape = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


# EXTRACT GALLERY IMAGES FROM INERTIA HTML
def _extract_gallery_images_from_inertia_html(content: str) -> list[str]:
    """
    Parse the embedded Inertia gallery payload from listing page HTML. Car &
    Classic embeds a responsive images array (xs through xxl) with every photo
    for the listing; this is more complete than the visible gallery thumbnails.
    """

    marker_index = content.find(_INERTIA_GALLERY_IMAGES_MARKER)
    if marker_index < 0:
        return []

    array_start = marker_index + len('"images":')
    array_end = _find_json_array_end(content, array_start)
    if array_end is None:
        return []

    try:
        images_data = json.loads(content[array_start:array_end])
    except json.JSONDecodeError:
        return []

    image_urls: list[str] = []
    seen: set[str] = set()
    for entry in images_data:
        if not isinstance(entry, dict):
            continue

        # pick the largest available responsive size for each photo
        src = None
        for size in _INERTIA_GALLERY_IMAGE_SIZES:
            size_entry = entry.get(size)
            if isinstance(size_entry, dict):
                src = size_entry.get("src")
                if src:
                    break
        if src:
            _append_unique_image_url(image_urls, seen, src.replace("\\/", "/"))

    return image_urls


# EXTRACT GALLERY IMAGES FROM STRUCTURED HTML
def _extract_gallery_images_from_structured_html(content: str) -> list[str]:
    """
    Collect gallery image URLs from JSON-LD and Open Graph meta tags when DOM
    extraction and the Inertia gallery payload are unavailable.
    """

    image_urls: list[str] = []
    seen: set[str] = set()

    for match in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        content,
        re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue

        objects: list[object]
        if isinstance(data, list):
            objects = data
        elif isinstance(data, dict) and isinstance(data.get("@graph"), list):
            objects = data["@graph"]
        else:
            objects = [data]

        for obj in objects:
            if not isinstance(obj, dict):
                continue
            obj_type = obj.get("@type", "")
            types = obj_type if isinstance(obj_type, list) else [obj_type]
            if not any(item in ("Car", "Product", "Vehicle") for item in types):
                continue

            images = obj.get("image", [])
            if isinstance(images, str):
                images = [images]
            for url in images:
                _append_unique_image_url(image_urls, seen, str(url))

    if image_urls:
        return image_urls

    for match in re.finditer(
        r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::url)?|twitter:image:src)["\'][^>]+content=["\']([^"\']+)["\']',
        content,
        re.IGNORECASE,
    ):
        _append_unique_image_url(image_urls, seen, match.group(1).replace("&amp;", "&"))

    for match in re.finditer(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image(?::url)?|twitter:image:src)["\']',
        content,
        re.IGNORECASE,
    ):
        _append_unique_image_url(image_urls, seen, match.group(1).replace("&amp;", "&"))

    return image_urls


# COLLECT GALLERY SECTION IMAGES
def _collect_gallery_section_images(
    gallery_section,
    image_urls: list[str],
    seen: set[str],
) -> None:
    """
    Append image src urls from img elements inside the gallery section.
    """

    section_images = gallery_section.locator("img")
    section_image_count = quick_locator_count(
        section_images, description="gallery section images"
    )
    for index in range(section_image_count):
        src = section_images.nth(index).get_attribute("src")
        _append_unique_image_url(image_urls, seen, src)


# EXTRACT GALLERY IMAGES FROM DOM
def _extract_gallery_images_from_dom(page: Page) -> list[str]:
    """
    Collect image URLs from the Gallery section via Playwright locators. Visible
    thumbnails are always collected; when a camera icon opens a full gallery
    sheet, additional URLs are merged from the popup when present.
    """

    image_urls: list[str] = []
    seen: set[str] = set()

    gallery_section = page.locator('section:has(h2:text("Gallery"))')
    if quick_locator_count(gallery_section, description="gallery section") == 0:
        return image_urls

    gallery_buttons = gallery_section.locator("button")
    if quick_locator_count(gallery_buttons, description="gallery buttons") == 0:
        _collect_gallery_section_images(gallery_section, image_urls, seen)
        return image_urls

    last_button = gallery_buttons.last
    has_camera_icon = (
        quick_locator_count(
            last_button.locator('svg[data-icon="camera"]'),
            description="gallery camera icon",
        )
        > 0
    )

    # always collect the thumbnails already rendered in the gallery grid
    _collect_gallery_section_images(gallery_section, image_urls, seen)

    if has_camera_icon:
        # try to open the full gallery sheet for any photos not yet visible
        last_button.click()
        pause(0.5, 1)

        for popup_selector in (
            "#panel_sheet_images img",
            ".pswp img",
            "[role='dialog'] img",
        ):
            popup_images = page.locator(popup_selector)
            popup_image_count = quick_locator_count(
                popup_images,
                description=f"gallery popup images ({popup_selector})",
            )
            for index in range(popup_image_count):
                src = popup_images.nth(index).get_attribute("src")
                _append_unique_image_url(image_urls, seen, src)

        close_button = page.locator('button:has(svg[data-icon="close"])').first
        if close_button.is_visible():
            close_button.click()
            pause(0.5, 1)

    return image_urls

# conservative sold phrases in the first description paragraph only
DESCRIPTION_SOLD_PHRASE = re.compile(
    r"(?i)\b(?:now sold|already sold|has been sold|is sold)\b"
)


# IS TITLE SOLD OR UNDER OFFER
def is_title_sold_or_under_offer(title: str) -> bool:
    """
    Return True when a search-card title or listing h1 clearly indicates the
    vehicle is sold or under offer.
    """

    return bool(TITLE_SOLD_OR_UNDER_OFFER.search(title))


# GET ADVERT TYPE
def _get_advert_type(page: Page) -> str | None:
    """
    Read the Advert type value from the Advert Details section, if present.
    """

    details = page.locator('section:has(h2:text("Advert Details"))')
    if quick_locator_count(details, description="advert details section") == 0:
        return None

    match = re.search(
        r"Advert type:\s*([^\n]+)",
        details.first.inner_text(),
    )
    return match.group(1).strip() if match else None


# HAS DESCRIPTION SOLD PHRASE
def _has_description_sold_phrase(page: Page) -> bool:
    """
    Return True when the first description paragraph contains an explicit sold
    phrase such as 'now sold' or 'already sold'.
    """

    desc_article = page.locator('article:has(h2:text("Description"))')
    if quick_locator_count(desc_article, description="description section") == 0:
        return False

    first_para = desc_article.locator("p").first
    if quick_locator_count(first_para, description="description paragraph") == 0:
        return False

    return bool(DESCRIPTION_SOLD_PHRASE.search(first_para.inner_text()))


# LISTING DETAIL OUTCOME LOCATOR
def _listing_detail_outcome_locator(page: Page):
    """
    Locator for whichever element confirms a Car & Classic detail page has
    finished rendering: the removed-advert banner, asking-price header, or h1.
    """

    removed_banner = page.get_by_text(
        UNAVAILABLE_ADVERT_TEXTS[0], exact=False
    )
    for text in UNAVAILABLE_ADVERT_TEXTS[1:]:
        removed_banner = removed_banner.or_(
            page.get_by_text(text, exact=False)
        )
    price_header = page.locator('header:has(span:text("Asking price"))')
    listing_h1 = page.locator("section h1")
    return removed_banner.or_(price_header).or_(listing_h1)


# WAIT FOR LISTING DETAIL PAGE
def wait_for_listing_detail_page(page: Page) -> None:
    """
    Wait until a Car & Classic listing detail page has finished rendering.
    Removed adverts never show section h1, so waiting for h1 alone would time
    out on unavailable listings instead of recognising the removed-advert banner.
    """

    outcome = _listing_detail_outcome_locator(page)

    def wait_for_outcome(timeout_ms: int) -> None:
        outcome.first.wait_for(state="attached", timeout=timeout_ms)

    run_with_timeout_backoff(
        wait_for_outcome,
        description="listing detail page",
    )


# IS LISTING NO LONGER AVAILABLE
def is_listing_no_longer_available(page: Page) -> bool:
    """
    Return True when the listing page shows the advert has been sold, is under
    offer, or has been removed. Car & Classic detail pages are client-rendered,
    so this waits for a removed-advert banner, asking-price header, or h1 to
    appear before deciding, ensuring a slow render is never misread as available.
    """

    return run_quick_page_action(
        page,
        lambda: _check_listing_no_longer_available(page),
        description="listing availability check",
        timeout_ms=30_000,
    )


# CHECK LISTING NO LONGER AVAILABLE
def _check_listing_no_longer_available(page: Page) -> bool:
    """
    Evaluate whether the current listing detail page is unavailable, using short
    timeouts on each locator probe so a wedged renderer cannot block for minutes.
    """

    # wait for whichever outcome renders first so a removed advert ends the wait
    # immediately rather than paying the full timeout, while a live listing is
    # confirmed by its asking-price header or at least its h1 title
    outcome = _listing_detail_outcome_locator(page)
    price_header = page.locator('header:has(span:text("Asking price"))')
    try:
        outcome.first.wait_for(state="attached", timeout=15000)
    except Exception:
        pass

    for text in UNAVAILABLE_ADVERT_TEXTS:
        if quick_locator_count(
            page.get_by_text(text, exact=False),
            description="unavailable advert banner",
        ) > 0:
            return True

    # fall back to scanning the rendered html in case the banner markup changes;
    # page_html respects the action timeout unlike page.content()
    try:
        content = page_html(page)
        if any(text in content for text in UNAVAILABLE_ADVERT_TEXTS):
            return True
    except Exception:
        pass

    h1 = page.locator("section h1").first
    if quick_locator_count(h1, description="listing h1") > 0:
        h1_text = h1.text_content() or ""
        if is_title_sold_or_under_offer(h1_text):
            return True

    if _has_description_sold_phrase(page):
        return True

    # a For Sale classified with no asking price is no longer purchasable
    if (
        quick_locator_count(price_header, description="asking price header") == 0
        and _get_advert_type(page) == "For Sale"
    ):
        return True

    return False


# UNAVAILABLE BANNER IN PAGE HTML
def _unavailable_banner_in_page_html(page: Page) -> bool:
    """
    Return True when unavailable-advert banner text is present in the page html.
    Used after a Playwright timeout to avoid re-running full availability probes
    against a possibly wedged renderer.
    """

    try:
        content = run_quick_page_action(
            page,
            lambda: page_html(page),
            description="page html read",
            timeout_ms=10_000,
        )
    except PageUnresponsiveError:
        return False

    return any(text in content for text in UNAVAILABLE_ADVERT_TEXTS)


_SEARCH_RESULTS_CARD_SELECTOR = '[data-testid="card-listing"]'


ICON_TO_FIELD = {
    "driving-wheel": "drive_configuration",
    "dial": "mileage_raw",
    "fuel": "fuel_type",
    "engine": "engine_size",
    "calendar": "year",
    "vrm": "registration",
    "colour": "colour",
}


# CONSENT APPEAR TIMEOUT S
_CONSENT_APPEAR_TIMEOUT_S = 8.0

# CONSENT CLEAR TIMEOUT S
_CONSENT_CLEAR_TIMEOUT_S = 5.0

# CONSENT POLL INTERVAL S
_CONSENT_POLL_INTERVAL_S = 0.5


# IS COOKIE CONSENT VISIBLE
def _is_cookie_consent_visible(page: Page) -> bool:
    """
    Return True when the OneTrust cookie banner is on screen and blocking
    interaction with the page underneath.
    """

    banner = page.locator("#onetrust-banner-sdk")
    if quick_locator_count(banner, description="cookie banner") > 0:
        try:
            if banner.first.is_visible():
                return True
        except Exception:
            pass

    accept_button = page.locator("#onetrust-accept-btn-handler")
    if quick_locator_count(accept_button, description="cookie accept button") > 0:
        try:
            if accept_button.first.is_visible():
                return True
        except Exception:
            pass

    return False


# CLICK ACCEPT COOKIES
def _click_accept_cookies(page: Page, timeout: float = 5000) -> bool:
    """
    Click 'Accept All' on the OneTrust banner and remove any lingering overlay.
    Returns True when consent was dismissed.
    """

    accept_button = page.locator("#onetrust-accept-btn-handler")

    # wait for the banner to render; bail out quietly if it never appears
    try:
        accept_button.wait_for(state="visible", timeout=timeout)
    except Exception:
        return False

    accept_button.click()

    # wait for the banner to be dismissed once consent is recorded
    try:
        page.wait_for_selector(
            "#onetrust-banner-sdk", state="hidden", timeout=timeout
        )
    except Exception:
        pass

    # the dark filter overlay sometimes lingers after the banner closes and
    # intercepts pointer events on subsequent clicks, so detach any remnants
    page.evaluate(
        """() => {
            document
                .querySelectorAll('.onetrust-pc-dark-filter, #onetrust-banner-sdk')
                .forEach((el) => el.remove());
        }"""
    )

    return True


# ACCEPT COOKIES
def accept_cookies(
    page: Page,
    *,
    wait_for_banner: bool = False,
    timeout: float = 15000,
) -> None:
    """
    Dismiss the OneTrust cookie consent banner by clicking 'Accept All'. Pass
    wait_for_banner=True immediately after a navigation so a late-loading banner
    can be handled; otherwise only act when the banner is already on screen so
    repeated calls do not probe for a new banner to appear.
    """

    click_timeout = min(timeout, 5000.0)

    def wait_attached() -> None:
        try:
            page.locator("#onetrust-accept-btn-handler").wait_for(
                state="attached", timeout=1000
            )
        except Exception:
            pass

    run_consent_dismiss_loop(
        is_visible=lambda: _is_cookie_consent_visible(page),
        click_dismiss=lambda: _click_accept_cookies(page, timeout=click_timeout),
        wait_attached=wait_attached,
        wait_for_banner=wait_for_banner,
        timeout=timeout,
        appear_timeout_s=_CONSENT_APPEAR_TIMEOUT_S,
        clear_timeout_s=_CONSENT_CLEAR_TIMEOUT_S,
    )


# POLL COOKIE CONSENT
def _poll_cookie_consent(page: Page) -> None:
    """
    Check once for a OneTrust consent banner and dismiss it when present.
    """

    if _is_cookie_consent_visible(page):
        accept_cookies(page)


# PAUSE FOR PAGE
def pause_for_page(
    page: Page, min_seconds: float = 1.0, max_seconds: float = 3.0
) -> None:
    """
    Wait like pause(), but poll for a late OneTrust cookie banner during longer
    delays so it can be dismissed before it blocks interactions.
    """

    pause_with_poll(
        lambda: _poll_cookie_consent(page),
        min_seconds=min_seconds,
        max_seconds=max_seconds,
        poll_interval=_CONSENT_POLL_INTERVAL_S,
    )


# DISMISS BLOCKING OVERLAYS
def _dismiss_blocking_overlays(page: Page, *, cookie_timeout: float = 3000) -> None:
    """
    Clear cookie-consent and inertia error overlays that intercept pointer
    events on the search results page before opening sort or filter controls.
    """

    accept_cookies(page, wait_for_banner=False, timeout=cookie_timeout)
    dismiss_inertia_error_dialog(page)


_NEWEST_LISTED_SORT_APPLIED_SELECTOR = (
    'button:has(svg[data-icon="clock"]):has(span:text-is("Newest listed"))'
)
_SORT_MENU_BUTTON_SELECTOR = 'button:has(svg[data-icon="sort"])'


# IS SORTED BY NEWEST
def _is_sorted_by_newest(page: Page) -> bool:
    """
    Return True when the results page is sorted by newest listed, either from
    the sort=latest query parameter or from the sort control showing a clock
    icon with the Newest listed label after the selection is applied.
    """

    params = dict(parse_qsl(urlparse(page.url).query, keep_blank_values=True))
    if params.get("sort") == "latest":
        return True

    return run_quick_page_action(
        page,
        lambda: page.locator(_NEWEST_LISTED_SORT_APPLIED_SELECTOR).count() > 0,
        description="newest-listed sort check",
    )


# APPLY NEWEST LISTED SORT
def _apply_newest_listed_sort(page: Page) -> None:
    """
    Open the sort overlay and choose Newest listed, dismissing any cookie or
    inertia overlays first. When the control already shows Newest listed with
    the clock icon, the selection is left unchanged.
    """

    for attempt in range(2):
        _dismiss_blocking_overlays(page)
        pause_for_page(page)

        if _is_sorted_by_newest(page):
            return

        page.locator(_SORT_MENU_BUTTON_SELECTOR).first.click()
        wait_for_selector_with_backoff(
            page,
            'button:has(span:text-is("Newest listed"))',
            state="attached",
            description="sort overlay",
        )

        pause_for_page(page)
        page.locator('button:has(span:text-is("Newest listed"))').click()
        wait_for_selector_with_backoff(
            page,
            '[data-testid="card-listing"]',
            state="attached",
            description="search results grid",
        )

        if _is_sorted_by_newest(page):
            return

        print(
            "Newest listed sort did not apply "
            f"(attempt {attempt + 1}/2); retrying after clearing overlays"
        )

    raise RuntimeError(
        "Failed to apply Newest listed sort after clearing cookie overlays"
    )


# CANONICAL NEWEST SEARCH URL
def _canonical_newest_search_url(url: str) -> str:
    """
    Return the page-one search url with sort=latest and source=modal-sort set,
    preserving every other filter query parameter from url.
    """

    return replace_query_params(
        url,
        drop=("sort", "source", "page"),
        set_params={"sort": "latest", "source": "modal-sort"},
    )


# EXTRACT SOURCE ID
def extract_source_id(url: str | None) -> str | None:
    """
    Extract the Car & Classic listing reference from a listing URL. Classified
    ads store a C-prefixed id in /l/, /la/, or /car/ (e.g. 'C1032137').
    Auction and make-an-offer pages use the trailing slug token (e.g. 'n9v0O4'
    from .../auctions/2004-mercedes-benz-sl500-r230-n9v0O4). Returns None when
    the URL does not contain a recognised listing id.
    """

    if not url:
        return None

    # classified ads store the c-prefixed id in /l/, /la/ or /car/
    match = re.search(r"/(?:la|l|car)/([^/?#]+)", url)
    if match:
        return match.group(1)

    # auctions and make-an-offer pages use the trailing slug token as the id
    match = re.search(
        r"/(?:auctions|make-an-offer)/[^/?#]*-([A-Za-z0-9]+)(?:[/?#]|$)",
        url,
    )
    return match.group(1) if match else None


# DISMISS INERTIA ERROR DIALOG
def dismiss_inertia_error_dialog(page: Page) -> bool:
    """
    Remove the Inertia.js error overlay dialog if one is open. The overlay
    renders a full-page iframe that intercepts pointer events, so any
    subsequent click on the underlying page silently times out until it is
    cleared. Returns True if a dialog was dismissed.
    """

    dialog = page.locator("dialog#inertia-error-dialog")
    if quick_locator_count(dialog, description="inertia error dialog") == 0:
        return False

    # the dialog exposes no visible close control, so detach it from the dom
    page.evaluate(
        "document.getElementById('inertia-error-dialog')?.remove()"
    )
    return True


# EXTRACT GALLERY IMAGES
def extract_gallery_images(page: Page) -> list[str]:
    """
    Collect image URLs for a listing from every available source and merge
    them. Car & Classic uses several gallery layouts, so this combines the
    embedded Inertia payload, DOM gallery thumbnails and popup sheet, and
    JSON-LD / Open Graph fallbacks. URLs are de-duplicated by path (ignoring
    CDN size query params) and capped at the site photo limit.
    """

    # clear any overlays that would intercept the gallery click
    _poll_cookie_consent(page)
    dismiss_inertia_error_dialog(page)

    page_content = ""
    try:
        page_content = page_html(page)
    except Exception:
        pass

    inertia_urls = (
        _extract_gallery_images_from_inertia_html(page_content)
        if page_content
        else []
    )
    dom_urls = _extract_gallery_images_from_dom(page)
    structured_urls = (
        _extract_gallery_images_from_structured_html(page_content)
        if page_content
        else []
    )

    image_urls = _merge_image_url_lists(
        inertia_urls,
        dom_urls,
        structured_urls,
    )

    return cap_gallery_urls(image_urls)


# EXTRACT LISTING DETAILS
def extract_listing_details(
    page: Page,
    hash_code: str,
    source_id: str | None,
    fallback_title: str | None = None,
) -> tuple[ProspectListings, list[str]]:
    """
    Extract prospect listing fields from an individual listing detail page
    and return a populated ProspectListings instance along with gallery image URLs.
    fallback_title is the search-card title, used when the detail-page h1 is a
    section heading such as Highlights rather than the vehicle name.
    """

    return run_quick_page_action(
        page,
        lambda: _extract_listing_details_impl(
            page, hash_code, source_id, fallback_title
        ),
        description="listing field extraction",
        timeout_ms=90_000,
    )


# EXTRACT LISTING DETAILS IMPL
def _extract_listing_details_impl(
    page: Page,
    hash_code: str,
    source_id: str | None,
    fallback_title: str | None = None,
) -> tuple[ProspectListings, list[str]]:
    """
    Populate a ProspectListings row and gallery image urls from the current
    listing detail page.
    """

    url = page.url
    current_datetime = datetime.now()

    # prefer a real vehicle title over section headings like "Highlights" that
    # can appear as the first section h1 when the page layout shifts
    h1_text = page.locator("section h1").first.text_content().strip()
    if _is_non_title_heading(h1_text):
        make_and_model = (fallback_title or "").strip() or h1_text
        if fallback_title:
            print(
                f"  Detail h1 was '{h1_text}'; using card title "
                f"'{make_and_model}' instead"
            )
    else:
        make_and_model = h1_text

    mileage = None
    mileage_unit = None
    fuel_type = None
    engine_size = None
    year = None
    registration = None
    colour = None
    drive_configuration = None
    location = None

    # extract fields from the spec list items using their svg data-icon attribute
    items = page.locator("section ul li")
    item_count = quick_locator_count(items, description="listing spec items")
    for i in range(item_count):
        li = items.nth(i)
        icon = li.locator("svg[data-icon]")

        if quick_locator_count(icon, description="spec list icon") > 0:
            icon_name = icon.first.get_attribute("data-icon")
            value = li.text_content().strip()
            field = ICON_TO_FIELD.get(icon_name)
            if field == "mileage_raw":
                mileage, mileage_unit = parse_mileage(value)
            elif field == "year":
                try:
                    year = int(value)
                except ValueError:
                    pass
            elif field == "fuel_type":
                fuel_type = value
            elif field == "engine_size":
                engine_size = value
            elif field == "registration":
                registration = value
            elif field == "colour":
                colour = value
            elif field == "drive_configuration":
                drive_configuration = value
        else:
            # the location li uses a flag <img> instead of an svg
            img = li.locator("img")
            if quick_locator_count(img, description="location flag image") > 0:
                location = li.text_content().strip()
                location = re.sub(r",?\s*United Kingdom$", "", location)

    # extract asking price and currency symbol from the price header
    asking_price = None
    currency_symbol = None
    price_header = page.locator('header:has(span:text("Asking price")) h2')
    if quick_locator_count(price_header, description="asking price header") > 0:
        price_text = price_header.first.text_content().strip()
        if price_text:
            currency_symbol = price_text[0]
            price_digits = price_text[1:].replace(",", "")
            try:
                asking_price = int(price_digits)
            except ValueError:
                pass

    # extract description from the article containing "Description" heading
    short_description = None
    full_description = None
    desc_article = page.locator('article:has(h2:text("Description"))')
    if quick_locator_count(desc_article, description="description article") > 0:
        full_text = desc_article.locator("p").first.inner_text().strip()
        full_description = full_text
        short_description = full_text.split("\n")[0].strip()

    # extract vehicle background history check summary
    basic_history_check = None
    bg_section = page.locator('section:has(h2:text("Vehicle background"))')
    if quick_locator_count(bg_section, description="vehicle background section") > 0:
        answers = bg_section.locator("div > p:last-child")
        no_count = 0
        yes_count = 0
        answer_count = quick_locator_count(
            answers, description="vehicle background answers"
        )
        for i in range(answer_count):
            answer = answers.nth(i).text_content().strip()
            if answer == "No":
                no_count += 1
            elif answer == "Yes":
                yes_count += 1
        basic_history_check = f"{no_count} checks passed"
        if yes_count > 0:
            suffix = "s" if yes_count > 1 else ""
            basic_history_check += f", {yes_count} item{suffix} to note"

    # extract gallery images from the full gallery popup
    image_urls = extract_gallery_images(page)

    prospect_listing = ProspectListings(
        hash_code=hash_code,
        source_id=source_id,
        listing_source=ListingSource.CAR_AND_CLASSIC,
        listing_type=ListingType.CLASSIC,
        status=ProspectListingStatus.NEW,
        url=url,
        make_and_model=make_and_model,
        short_description=short_description or make_and_model,
        full_description=full_description,
        asking_price=asking_price,
        currency_symbol=currency_symbol,
        mileage=mileage,
        mileage_unit=mileage_unit,
        fuel_type=fuel_type,
        engine_size=engine_size,
        year=year,
        registration=registration,
        colour=colour,
        drive_configuration=drive_configuration,
        location=location,
        basic_history_check=basic_history_check,
        created_at=current_datetime,
        updated_at=current_datetime,
    )

    return prospect_listing, image_urls


# SNAPSHOT LISTING CANDIDATES
def _snapshot_listing_candidates(
    page: Page, *, use_new_section: bool
) -> list[tuple[str, str, str | None, str]]:
    """
    Collect every listing card on the current results page before navigating to
    any detail page, returning (card_id, listing_url, source_id, title) tuples.
    """

    if use_new_section:
        articles = page.locator("div.lg\\:grid-cols-3.grid.grid-cols-1").locator(
            _SEARCH_RESULTS_CARD_SELECTOR
        )
    else:
        articles = page.locator(_SEARCH_RESULTS_CARD_SELECTOR)

    listing_candidates: list[tuple[str, str, str | None, str]] = []
    article_count = quick_locator_count(
        articles, description="search result cards"
    )
    for i in range(article_count):
        card = articles.nth(i)
        title = card.locator("h2").text_content().strip()
        href = card.locator("a").first.get_attribute("href")
        source_id = extract_source_id(href)
        listing_url = (
            href
            if href.startswith("http")
            else f"https://www.carandclassic.com{href}"
        )
        card_id = source_id or listing_url
        listing_candidates.append((card_id, listing_url, source_id, title))

    return listing_candidates


# SCRAPE LISTINGS
def scrape_listings(
    page: Page,
    search_url: str,
    deadline: float | None = None,
    resume: ScrapeResumeState | None = None,
):
    """
    Walk up to _MAX_SEARCH_PAGES of Car & Classic search results and save new
    listings. Each results page is loaded via the page= query parameter on the canonical
    search url (including sort=latest), listing cards are snapshotted, then each
    candidate is visited directly without returning to the grid between items.
    When deadline (a time.monotonic value) is given, raises ProxySessionExpired
    between listings once it is reached so the proxy can be rotated without
    interrupting a partially-downloaded listing. When resume carries a
    search_url from an interrupted run, continues from the saved results page
    number rather than starting over from page one.
    """

    existing_source_ids = get_existing_source_ids(ListingSource.CAR_AND_CLASSIC)
    log_loaded_source_ids(len(existing_source_ids))

    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    engine = create_engine_with_retry(database_url)
    SessionLocal = sessionmaker(bind=engine)

    new_count = 0
    processed_ids = resume.processed_ids if resume is not None else set()
    page_number = resume.page_number if resume is not None else 1
    previous_page_listing_ids: set[str] = set()
    use_new_section: bool | None = None

    if resume is not None and resume.search_url is not None:
        search_url = with_page_param(resume.search_url, 1)
        log_resume_scrape(page_number, len(processed_ids))

    log_paginating_start("Car & Classic")

    while True:
        page_url = with_page_param(search_url, page_number)
        goto_with_captcha_handling(page, page_url)
        accept_cookies(page, wait_for_banner=True, timeout=8000)
        pause_for_page(page, min_seconds=2.0, max_seconds=4.0)

        if not search_results_present(page, _SEARCH_RESULTS_CARD_SELECTOR):
            log_no_listings_on_page(page_number)
            break

        if use_new_section is None:
            new_vehicles_grid = page.locator(
                "div.lg\\:grid-cols-3.grid.grid-cols-1"
            )
            use_new_section = (
                quick_locator_count(
                    new_vehicles_grid, description="new vehicles grid"
                )
                > 0
            )
            if use_new_section:
                print("New vehicles section detected — processing only new listings")

        listing_candidates = _snapshot_listing_candidates(
            page, use_new_section=use_new_section
        )
        page_listing_ids = {card_id for card_id, _, _, _ in listing_candidates}

        if page_listing_ids and page_listing_ids == previous_page_listing_ids:
            log_repeat_page(page_number)
            break
        previous_page_listing_ids = page_listing_ids

        log_results_page(page_number, len(listing_candidates))

        for index, (card_id, listing_url, source_id, title) in enumerate(
            listing_candidates, start=1
        ):
            if card_id in processed_ids:
                log_already_processed(index, title)
                continue

            # record position before each listing so proxy rotation can resume here
            if resume is not None:
                resume.search_url = search_url
                resume.page_number = page_number
                resume.processed_ids = processed_ids

            # rotate the proxy between listings, never mid-download, once the
            # session has outlived the rotation interval; raised before any
            # navigation so no listing is left partially saved
            if deadline is not None and time.monotonic() >= deadline:
                raise ProxySessionExpired(
                    "proxy rotation interval elapsed during listing scrape"
                )

            if source_id is not None and source_id in existing_source_ids:
                log_already_exists(index, title)
                processed_ids.add(card_id)
                continue

            # include the source id in the hash input so two different
            # listings with the same title cannot violate the table-wide
            # hash_code unique constraint
            hash_code = generate_hash_code(
                f"{title}|{source_id}" if source_id else title
            )

            # skip sold/under-offer listings from the card title without a
            # detail-page visit when the marker is already visible
            if is_title_sold_or_under_offer(title):
                log_sold_under_offer_card(index, title)
                with SessionLocal() as session:
                    persist_unavailable_listing_stub(
                        session,
                        listing_source=ListingSource.CAR_AND_CLASSIC,
                        listing_type=ListingType.CLASSIC,
                        hash_code=hash_code,
                        source_id=source_id,
                        url=listing_url,
                        make_and_model=title,
                        short_description=title,
                    )
                if source_id is not None:
                    existing_source_ids.add(source_id)
                processed_ids.add(card_id)
                continue

            # process each listing inside a guard so one bad listing cannot
            # abort the whole sweep; every candidate url comes from the snapshot
            # so a failure just moves on to the next without re-reading the page
            log_timestamp()

            try:
                # navigate to the listing via its href instead of clicking the
                # card, because the anchor is overlaid by a sibling that
                # intercepts pointer events; goto_with_captcha_handling also
                # pauses for any captcha shown in place of the detail page
                response = goto_with_captcha_handling(page, listing_url)
                accept_cookies(page, wait_for_banner=True, timeout=8000)
                wait_for_listing_detail_page(page)
                pause_for_page(
                    page,
                    min_seconds=_LISTING_PAUSE_MIN_S,
                    max_seconds=_LISTING_PAUSE_MAX_S,
                )

                if is_http_not_found(response) or is_listing_no_longer_available(
                    page
                ):
                    reason = (
                        "404"
                        if is_http_not_found(response)
                        else "unavailable"
                    )
                    log_not_available(index, title, reason)
                    h1 = page.locator("section h1").first
                    make_and_model = (
                        h1.text_content().strip()
                        if quick_locator_count(h1, description="listing h1") > 0
                        else title
                    )
                    with SessionLocal() as session:
                        persist_unavailable_listing_stub(
                            session,
                            listing_source=ListingSource.CAR_AND_CLASSIC,
                            listing_type=ListingType.CLASSIC,
                            hash_code=hash_code,
                            source_id=source_id,
                            url=listing_url,
                            make_and_model=make_and_model,
                            short_description=title,
                        )
                    if source_id is not None:
                        existing_source_ids.add(source_id)
                    processed_ids.add(card_id)
                    continue

                prospect_listing, image_urls = extract_listing_details(
                    page, hash_code, source_id, fallback_title=title
                )
                prospect_listing.status_checked_at = datetime.now()
                new_count += 1

                # save to database and download images
                with SessionLocal() as session:
                    def on_after_persist(saved: ProspectListings) -> None:
                        log_saved_listing(
                            index,
                            title,
                            make_and_model=saved.make_and_model,
                            year=saved.year,
                            location=saved.location,
                            image_count=len(image_urls),
                        )

                    try:
                        persist_listing_with_images_and_ai(
                            session,
                            prospect_listing,
                            ai_prompt_filename="classic_car_prompt.md",
                            listing_url=listing_url,
                            image_urls=image_urls,
                            page=page,
                            temp_dir_prefix="car_and_classic_images_",
                            on_after_persist=on_after_persist,
                            log_index=index,
                        )
                    except PageUnresponsiveError:
                        new_count -= 1
                        raise

                mark_listing_processed_and_check_availability(
                    page,
                    source_id=source_id,
                    card_id=card_id,
                    existing_source_ids=existing_source_ids,
                    processed_ids=processed_ids,
                    listing_source=ListingSource.CAR_AND_CLASSIC,
                    listing_type=ListingType.CLASSIC,
                    is_unavailable_fn=is_listing_no_longer_available,
                    config=CONFIG,
                    deadline=deadline,
                )

            except Exception as e:
                if is_playwright_timeout(e) and _unavailable_banner_in_page_html(page):
                    log_not_available(index, title, "unavailable")
                    with SessionLocal() as session:
                        persist_unavailable_listing_stub(
                            session,
                            listing_source=ListingSource.CAR_AND_CLASSIC,
                            listing_type=ListingType.CLASSIC,
                            hash_code=hash_code,
                            source_id=source_id,
                            url=listing_url,
                            make_and_model=title,
                            short_description=title,
                        )
                    if source_id is not None:
                        existing_source_ids.add(source_id)
                    processed_ids.add(card_id)
                    continue

                reraise_or_log_listing_error(index, title, listing_url, e)

        # new-vehicles section has no pagination, so stop after one pass
        if use_new_section:
            break

        if page_number >= _MAX_SEARCH_PAGES:
            print(
                f"Reached max search pages ({_MAX_SEARCH_PAGES}); "
                "stopping pagination"
            )
            break

        page_number += 1
        if resume is not None:
            resume.page_number = page_number
            resume.processed_ids = processed_ids
        pause_for_page(page, min_seconds=1.0, max_seconds=2.0)

    log_scrape_finished(page_number, new_count, len(processed_ids))


# OPEN SESSION
def _open_session(playwright) -> tuple:
    """
    Launch a fresh proxied Chromium session on the Car & Classic search page and
    accept cookies. Each launch advances to the next Decodo port via
    round-robin, so calling this again after a dead sticky session yields a new
    exit IP. Returns the browser and its page.
    """

    def on_ready(page: Page) -> None:
        pause_for_page(page, min_seconds=2.0, max_seconds=5.0)
        accept_cookies(page, wait_for_banner=True)

    return open_proxied_session(
        playwright,
        headless=is_headless,
        landing_url="https://www.carandclassic.com/search",
        on_ready=on_ready,
    )


# APPLY SEARCH FILTERS
def _apply_search_filters(page: Page) -> str:
    """
    Load the Cars / United Kingdom / Private seller search results sorted by
    Newest listed. Filters are set via query parameters on the search URL
    rather than the All filters overlay, which depends on hydrated client JS
    that often fails to load behind Cloudflare on proxied sessions. Returns
    the canonical page-one search url including sort=latest.
    """

    _dismiss_blocking_overlays(page, cookie_timeout=15000)
    pause_for_page(page)

    # navigate to the pre-filtered search url (cars, GB, private, newest)
    goto_with_captcha_handling(page, _FILTERED_SEARCH_URL)
    accept_cookies(page, wait_for_banner=True, timeout=8000)
    wait_for_selector_with_backoff(
        page,
        '[data-testid="card-listing"]',
        state="attached",
        description="search results grid",
    )

    # sort=latest is already on the url; only open the sort control if the
    # page did not pick it up (e.g. a soft redirect dropped the param)
    if not _is_sorted_by_newest(page):
        _apply_newest_listed_sort(page)

    _warm_up_search_session(page)

    return _canonical_newest_search_url(page.url)


# CAR AND CLASSIC
def car_and_classic():
    """
    Scrape new Car & Classic search listings inside a proxy-rotation loop.
    When a Decodo sticky session expires mid-run (surfacing as navigation
    timeouts) the browser is relaunched on a fresh proxy port and the sweep
    resumes on the same search-results page (page= query param) where it was
    interrupted; already-saved listings are skipped because scrape_listings
    reloads the existing source ids from the database on each pass. When a
    captcha is shown on three consecutive navigations the current exit IP is
    rotated automatically even if CapSolver cleared the earlier challenges.
    """

    def setup(page: Page, resume: ScrapeResumeState) -> None:
        resume.search_url = _apply_search_filters(page)

    def scrape(page: Page, deadline: float, resume: ScrapeResumeState) -> None:
        scrape_listings(page, resume.search_url, deadline, resume)

    run_with_proxy_rotation(
        config=CONFIG,
        open_session=_open_session,
        setup=setup,
        scrape=scrape,
    )


if __name__ == "__main__":
    car_and_classic()
