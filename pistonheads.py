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
    locator_is_visible,
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
    parse_mileage,
    pause_with_poll,
    run_with_timeout_backoff,
    wait_for_selector_with_backoff,
    wait_with_poll,
)
from listing_images import (
    append_unique_image_url,
    cap_gallery_urls,
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
    log_loaded_source_ids,
    log_no_listings_on_page,
    log_not_available,
    log_paginating_start,
    log_repeat_page,
    log_resume_scrape,
    log_results_page,
    log_saved_listing,
    log_scrape_finished,
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
)

UNAVAILABLE_ADVERT_TEXTS = (
    "This advert is no longer available",
    "This listing is no longer available",
    "advert has been removed",
    "Page not found",
)

CONFIG = ProxyRotationConfig.from_env_prefix(
    "PISTONHEADS",
    max_consecutive_captchas_before_rotation=3,
)

# private classified listings with keyword classic, sorted newest first; kebab-case
# query params are required because camelCase sellerType/listingType are ignored
_FILTERED_SEARCH_URL = (
    "https://www.pistonheads.com/buy/search"
    "?keywords=classic"
    "&seller-type=Private"
    "&listing-type=classifieds"
    "&sort=mostRecent"
)

_SEARCH_RESULTS_LINK_SELECTOR = 'a[href*="/buy/listing/"]'
_SORT_SELECT_SELECTOR = "#srp-sort-option-select"
_MOST_RECENT_SORT_VALUE = "Date"
_VIEW_MORE_BUTTON_SELECTOR = 'button:has-text("View more")'

# cap view-more batches (~16 cards each; ~89 results today, headroom for growth)
_MAX_SEARCH_BATCHES = 20

# pistonheads allows many photos; cap gallery extraction noise
_MAX_GALLERY_IMAGES = 100

_LISTING_ID_PATTERN = re.compile(r"/buy/listing/(\d+)")
_IMAGE_URL_PATTERN = re.compile(r"https://img\.pistonheads\.com/[^\"'\\\s>]+")
_DESCRIPTION_CONTAINER_SELECTOR = 'div[class*="Description_description"]'
_READ_MORE_SUFFIX = re.compile(r"\s*Read more\s*$", re.IGNORECASE)
_DRAWER_SELECTOR = ".MuiDrawer-root"
_OVERVIEW_SPECS_BUTTON_TEXT = "Overview and specs"
_VEHICLE_HISTORY_BUTTON_TEXT = "Vehicle history"
_MOT_EXPIRY_DATE_PATTERN = re.compile(
    r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4})",
    re.IGNORECASE,
)
_POA_PRICE_PATTERN = re.compile(
    r"\b(?:POA|Price on application)\b",
    re.IGNORECASE,
)

_CONSENT_APPEAR_TIMEOUT_S = 8.0
_CONSENT_CLEAR_TIMEOUT_S = 5.0
_CONSENT_POLL_INTERVAL_S = 0.5

# human-like pacing between navigations and listing visits
_PAUSE_PAGE_MIN_S = 2.0
_PAUSE_PAGE_MAX_S = 5.0
_PAUSE_LISTING_MIN_S = 2.5
_PAUSE_LISTING_MAX_S = 6.0
_PAUSE_BATCH_MIN_S = 2.0
_PAUSE_BATCH_MAX_S = 4.0
_PAUSE_DRAWER_MIN_S = 0.8
_PAUSE_DRAWER_MAX_S = 1.5
_PAUSE_BETWEEN_LISTINGS_MIN_S = 1.5
_PAUSE_BETWEEN_LISTINGS_MAX_S = 3.5
_PAUSE_SESSION_MIN_S = 3.0
_PAUSE_SESSION_MAX_S = 6.0

_QUANTCAST_CONSENT_NOTICE_SELECTORS = (
    ".qc-cmp2-summary-section",
    ".qc-cmp2-container",
    "#qc-cmp2-ui",
)
_QUANTCAST_ACCEPT_BUTTON_SELECTORS = (
    '.qc-cmp2-summary-buttons button[mode="primary"]',
    "button#accept-btn",
)
_QUANTCAST_ACCEPT_BUTTON_NAMES = (
    "AGREE",
    "Agree",
    "Accept all",
    "Accept All",
    "I agree",
)


# ITER CONSENT SEARCH ROOTS
def _iter_consent_search_roots(page: Page):
    """
    Yield the main page and every frame that may host a cookie consent banner.
    """

    yield page

    seen_frames: set[tuple[str, str]] = set()
    for frame in page.frames:
        frame_key = (frame.url, frame.name)
        if frame_key in seen_frames:
            continue
        seen_frames.add(frame_key)
        yield frame


# LOCATOR IS VISIBLE
def _locator_is_visible(locator) -> bool:
    """Return True when a locator resolves to a visible element."""

    return locator_is_visible(locator)


# IS QUANTCAST CONSENT VISIBLE
def _is_quantcast_consent_visible(page: Page) -> bool:
    """
    Return True when the Quantcast Choice consent banner is visible on the page
    or inside a consent iframe.
    """

    for root in _iter_consent_search_roots(page):
        for selector in _QUANTCAST_CONSENT_NOTICE_SELECTORS:
            if _locator_is_visible(root.locator(selector)):
                return True

        if _locator_is_visible(root.get_by_text("We value your privacy", exact=False)):
            return True

    return False


# IS ONETRUST CONSENT VISIBLE
def _is_onetrust_consent_visible(page: Page) -> bool:
    """
    Return True when the OneTrust cookie banner is on screen and blocking
    interaction with the page underneath.
    """

    banner = page.locator("#onetrust-banner-sdk")
    if _locator_is_visible(banner):
        return True

    accept_button = page.locator("#onetrust-accept-btn-handler")
    return _locator_is_visible(accept_button)


# IS COOKIE CONSENT VISIBLE
def _is_cookie_consent_visible(page: Page) -> bool:
    """
    Return True when either the Quantcast Choice or OneTrust cookie banner is
    visible and blocking interaction with the page underneath.
    """

    return _is_quantcast_consent_visible(page) or _is_onetrust_consent_visible(page)


# CLICK QUANTCAST CONSENT
def _click_quantcast_consent(page: Page, timeout: float = 5000) -> bool:
    """
    Click the primary accept button on the Quantcast Choice consent banner,
    checking the main page and any consent iframes. Returns True when consent
    was dismissed.
    """

    deadline = time.time() + (timeout / 1000.0)

    while time.time() < deadline:
        for root in _iter_consent_search_roots(page):
            for selector in _QUANTCAST_ACCEPT_BUTTON_SELECTORS:
                button = root.locator(selector)
                if not _locator_is_visible(button):
                    continue

                try:
                    button.first.click(timeout=2000)
                except Exception:
                    continue

                if not _is_quantcast_consent_visible(page):
                    return True

            for button_name in _QUANTCAST_ACCEPT_BUTTON_NAMES:
                button = root.get_by_role("button", name=button_name, exact=True)
                if not _locator_is_visible(button):
                    continue

                try:
                    button.first.click(timeout=2000)
                except Exception:
                    continue

                if not _is_quantcast_consent_visible(page):
                    return True

        if not _is_quantcast_consent_visible(page):
            return True

        time.sleep(_CONSENT_POLL_INTERVAL_S)

    return not _is_quantcast_consent_visible(page)


# CLICK ONETRUST COOKIES
def _click_onetrust_cookies(page: Page, timeout: float = 5000) -> bool:
    """
    Click 'Accept All' on the OneTrust banner and remove any lingering overlay.
    Returns True when consent was dismissed.
    """

    accept_button = page.locator("#onetrust-accept-btn-handler")

    try:
        accept_button.wait_for(state="visible", timeout=timeout)
    except Exception:
        return False

    accept_button.click()

    try:
        page.wait_for_selector(
            "#onetrust-banner-sdk", state="hidden", timeout=timeout
        )
    except Exception:
        pass

    return True


# REMOVE CONSENT OVERLAYS
def _remove_consent_overlays(page: Page) -> None:
    """
    Detach any lingering Quantcast or OneTrust overlays that continue to
    intercept pointer events after consent is recorded.
    """

    page.evaluate(
        """() => {
            document
                .querySelectorAll(
                    '.onetrust-pc-dark-filter, #onetrust-banner-sdk, ' +
                    '.qc-cmp2-container, #qc-cmp2-ui, .qc-cmp2-summary-section'
                )
                .forEach((el) => el.remove());
        }"""
    )


# CLICK ACCEPT COOKIES
def _click_accept_cookies(page: Page, timeout: float = 5000) -> bool:
    """
    Dismiss a visible Quantcast Choice or OneTrust banner. Returns True only
    when a banner was present and is no longer visible afterwards; returns
    False when no banner is on screen so callers can keep waiting for a late
    load.
    """

    if not _is_cookie_consent_visible(page):
        return False

    if _is_quantcast_consent_visible(page):
        _click_quantcast_consent(page, timeout=timeout)

    if _is_onetrust_consent_visible(page):
        _click_onetrust_cookies(page, timeout=timeout)

    if not _is_cookie_consent_visible(page):
        _remove_consent_overlays(page)
        return True

    return False


# WAIT FOR CONSENT BANNER ATTACHED
def _wait_for_consent_banner_attached(page: Page) -> None:
    """
    Wait briefly for either consent platform to attach its banner so a
    post-navigation accept_cookies call can catch late-loading modals.
    """

    selectors = (
        *_QUANTCAST_CONSENT_NOTICE_SELECTORS,
        "#onetrust-accept-btn-handler",
        '.qc-cmp2-summary-buttons button[mode="primary"]',
    )
    for root in _iter_consent_search_roots(page):
        for selector in selectors:
            try:
                root.locator(selector).first.wait_for(state="attached", timeout=500)
                return
            except Exception:
                pass


# ACCEPT COOKIES
def accept_cookies(
    page: Page,
    *,
    wait_for_banner: bool = False,
    timeout: float = 15000,
) -> None:
    """
    Dismiss the Quantcast Choice or OneTrust cookie banner by accepting all.
    Pass wait_for_banner=True immediately after a navigation so a late-loading
    banner on a listing detail page can be handled; otherwise only act when
    the banner is already on screen.
    """

    click_timeout = min(timeout, 5000.0)
    run_consent_dismiss_loop(
        is_visible=lambda: _is_cookie_consent_visible(page),
        click_dismiss=lambda: _click_accept_cookies(page, timeout=click_timeout),
        wait_attached=lambda: _wait_for_consent_banner_attached(page),
        wait_for_banner=wait_for_banner,
        timeout=timeout,
        appear_timeout_s=_CONSENT_APPEAR_TIMEOUT_S,
        clear_timeout_s=_CONSENT_CLEAR_TIMEOUT_S,
    )


# POLL COOKIE CONSENT
def _poll_cookie_consent(page: Page) -> None:
    """
    Check once for a consent banner and dismiss it when present.
    """

    if _is_cookie_consent_visible(page):
        accept_cookies(page)


# PAUSE FOR PAGE
def pause_for_page(
    page: Page,
    min_seconds: float = _PAUSE_PAGE_MIN_S,
    max_seconds: float = _PAUSE_PAGE_MAX_S,
) -> None:
    """
    Wait like pause(), but poll for a late Quantcast or OneTrust cookie banner
    during longer delays so it can be dismissed before it blocks interactions.
    """

    pause_with_poll(
        lambda: _poll_cookie_consent(page),
        min_seconds=min_seconds,
        max_seconds=max_seconds,
        poll_interval=_CONSENT_POLL_INTERVAL_S,
    )


# CANONICAL FILTERED SEARCH URL
def _canonical_filtered_search_url(url: str) -> str:
    """
    Return the page-one search url with classic/private/classifieds/mostRecent
    query parameters preserved and pagination params removed.
    """

    return replace_query_params(
        url,
        drop=("offset", "page", "p", "limit"),
        set_params={
            "keywords": "classic",
            "seller-type": "Private",
            "listing-type": "classifieds",
            "sort": "mostRecent",
        },
        prefer_keys=[
            "keywords",
            "seller-type",
            "listing-type",
            "sort",
        ],
    )


# IS SORTED BY MOST RECENT
def _is_sorted_by_most_recent(page: Page) -> bool:
    """
    Return True when the search results sort select shows Most recent (Date).
    """

    sort_select = page.locator(_SORT_SELECT_SELECTOR)
    if quick_locator_count(sort_select, description="sort select") == 0:
        params = dict(parse_qsl(urlparse(page.url).query, keep_blank_values=True))
        return params.get("sort") == "mostRecent"

    try:
        value = sort_select.first.input_value()
    except Exception:
        return False

    return value == _MOST_RECENT_SORT_VALUE


# APPLY MOST RECENT SORT
def _apply_most_recent_sort(page: Page) -> None:
    """
    Set the search results sort order to Most recent via the native select
    control when it is not already selected.
    """

    if _is_sorted_by_most_recent(page):
        return

    sort_select = page.locator(_SORT_SELECT_SELECTOR)
    wait_for_selector_with_backoff(
        page,
        _SORT_SELECT_SELECTOR,
        state="attached",
        description="sort select",
    )
    sort_select.first.select_option(_MOST_RECENT_SORT_VALUE)
    wait_for_selector_with_backoff(
        page,
        _SEARCH_RESULTS_LINK_SELECTOR,
        state="attached",
        description="search result links",
    )
    pause_for_page(page)


# LOAD SEARCH BATCH
def _load_search_batch(page: Page, search_url: str, batch_number: int) -> None:
    """
    Open the filtered search url and click View more until the requested batch
    number is reached. Batch one is the first page of results without extra clicks.
    """

    goto_with_captcha_handling(page, search_url)
    accept_cookies(page, wait_for_banner=True, timeout=8000)
    pause_for_page(page)
    _apply_most_recent_sort(page)

    for _ in range(max(batch_number - 1, 0)):
        if not _click_view_more(page):
            break
        pause_for_page(page, min_seconds=_PAUSE_BATCH_MIN_S, max_seconds=_PAUSE_BATCH_MAX_S)


# CLICK VIEW MORE
def _click_view_more(page: Page) -> bool:
    """
    Click the View more button when present and wait for additional listing
    cards to attach. Returns False when no button is visible.
    """

    view_more = page.locator(_VIEW_MORE_BUTTON_SELECTOR)
    if quick_locator_count(view_more, description="view more button") == 0:
        return False

    before_count = quick_locator_count(
        page.locator(_SEARCH_RESULTS_LINK_SELECTOR),
        description="listing links before view more",
    )
    view_more.first.click()
    deadline = time.time() + 15.0
    while time.time() < deadline:
        after_count = quick_locator_count(
            page.locator(_SEARCH_RESULTS_LINK_SELECTOR),
            description="listing links after view more",
        )
        if after_count > before_count:
            return True
        time.sleep(0.5)

    return True


# EXTRACT SOURCE ID
def extract_source_id(url: str | None) -> str | None:
    """
    Extract the PistonHeads listing reference from a URL such as
    https://www.pistonheads.com/buy/listing/15667955. Returns None when the
    URL does not contain a listing id segment.
    """

    match = _LISTING_ID_PATTERN.search(url or "")
    return match.group(1) if match else None


# NORMALIZE IMAGE URL
def _normalize_image_url(url: str) -> str:
    """
    Strip query parameters and prefer Fullsize CDN paths so the same photo at
    different responsive sizes is only kept once.
    """

    return normalize_image_url(
        url,
        path_replacements=(("/LargeSize/", "/Fullsize/"),),
    )


# EXTRACT GALLERY IMAGES FROM HTML
def _extract_gallery_images_from_html(content: str) -> list[str]:
    """
    Collect listing gallery image urls embedded in the rendered page HTML,
    de-duplicating by normalised path and excluding static site assets.
    """

    image_urls: list[str] = []
    seen: set[str] = set()

    for match in _IMAGE_URL_PATTERN.finditer(content):
        raw_url = match.group(0)
        if "/static-assets/" in raw_url:
            continue

        append_unique_image_url(
            image_urls,
            seen,
            raw_url,
            path_replacements=(("/LargeSize/", "/Fullsize/"),),
            store_normalized=True,
        )

    return cap_gallery_urls(image_urls, max_images=_MAX_GALLERY_IMAGES)


# EXTRACT GALLERY IMAGES
def extract_gallery_images(page: Page) -> list[str]:
    """
    Collect image URLs for a listing from the rendered detail page HTML and,
    when available, the full-screen gallery opened via Show all photos.
    """

    _poll_cookie_consent(page)

    try:
        content = page_html(page)
    except Exception:
        content = ""

    image_urls = _extract_gallery_images_from_html(content)

    photos_button = page.locator('button:has-text("Show all photos")')
    if quick_locator_count(photos_button, description="show all photos button") > 0:
        try:
            photos_button.first.click()
            pause_for_page(page, min_seconds=_PAUSE_BATCH_MIN_S, max_seconds=_PAUSE_BATCH_MAX_S)
            gallery_content = page_html(page)
            image_urls = _extract_gallery_images_from_html(
                gallery_content + "\n" + content
            )
        except Exception:
            pass

    return image_urls


# EXTRACT LISTING DESCRIPTION
def _extract_listing_description(page: Page) -> tuple[str | None, str | None]:
    """
    Extract short and full description text from the dedicated listing
    description container rather than the surrounding page chrome.
    """

    description_locator = page.locator(_DESCRIPTION_CONTAINER_SELECTOR)
    if quick_locator_count(description_locator, description="description container") == 0:
        return None, None

    full_text = description_locator.first.inner_text().strip()
    full_text = _READ_MORE_SUFFIX.sub("", full_text).strip()
    if not full_text:
        return None, None

    short_description = full_text.split("\n")[0].strip() or None
    return short_description, full_text


# CLOSE OPEN DRAWER
def _close_open_drawer(page: Page) -> None:
    """
    Close the listing detail drawer when one is open so another section can be
    opened without its backdrop intercepting clicks.
    """

    drawer = page.locator(_DRAWER_SELECTOR)
    if quick_locator_count(drawer, description="listing drawer") == 0:
        return

    close_button = page.locator('button[aria-label="Close"]')
    if quick_locator_count(close_button, description="drawer close button") > 0:
        try:
            close_button.first.click(timeout=2000)
            pause_for_page(
                page,
                min_seconds=_PAUSE_DRAWER_MIN_S,
                max_seconds=_PAUSE_DRAWER_MAX_S,
            )
            return
        except Exception:
            pass

    page.keyboard.press("Escape")
    pause_for_page(
        page,
        min_seconds=_PAUSE_DRAWER_MIN_S,
        max_seconds=_PAUSE_DRAWER_MAX_S,
    )


# OPEN DRAWER
def _open_drawer(page: Page, button_text: str) -> bool:
    """
    Open a listing detail drawer such as Overview and specs or Vehicle history.
    Returns True when the drawer becomes visible.
    """

    _close_open_drawer(page)
    _poll_cookie_consent(page)

    button = page.locator(f'button:has-text("{button_text}")').first
    if quick_locator_count(button, description=f"{button_text} button") == 0:
        return False

    button.click()
    try:
        page.locator(_DRAWER_SELECTOR).first.wait_for(state="visible", timeout=5000)
    except Exception:
        return False

    pause_for_page(
        page,
        min_seconds=_PAUSE_DRAWER_MIN_S,
        max_seconds=_PAUSE_DRAWER_MAX_S,
    )
    return True


# APPEND DRAWER SECTION TO MARKDOWN
def _append_drawer_section_to_markdown(
    markdown_parts: list[str],
    section_name: str,
    heading,
) -> None:
    """
    Append one drawer section heading and its following dl or ul items to
    markdown_parts in the same list style used by the Autotrader specs export.
    """

    section_items: list[str] = []
    definition_list = heading.locator("xpath=following-sibling::dl[1]")
    if quick_locator_count(definition_list, description="drawer section dl") > 0:
        labels = definition_list.locator("dt")
        values = definition_list.locator("dd")
        pair_count = min(
            quick_locator_count(labels, description="drawer section dt"),
            quick_locator_count(values, description="drawer section dd"),
        )
        for index in range(pair_count):
            label = labels.nth(index).text_content().strip()
            value = values.nth(index).text_content().strip()
            if label and value:
                section_items.append(f"- {label}: {value}")

    feature_list = heading.locator("xpath=following-sibling::ul[1]")
    if quick_locator_count(feature_list, description="drawer section ul") > 0:
        list_items = feature_list.locator("li")
        item_count = quick_locator_count(
            list_items, description="drawer section li"
        )
        for index in range(item_count):
            feature_text = list_items.nth(index).text_content().strip()
            if feature_text:
                section_items.append(f"- {feature_text}")

    if not section_items:
        return

    markdown_parts.append(f"## {section_name}\n")
    markdown_parts.extend(section_items)
    markdown_parts.append("")


# EXTRACT SPECS AND FEATURES
def _extract_specs_and_features(page: Page) -> str | None:
    """
    Open the Overview and specs drawer and export every specs/features section
    into markdown similar to the Autotrader specs_and_features field.
    """

    if not _open_drawer(page, _OVERVIEW_SPECS_BUTTON_TEXT):
        return None

    drawer = page.locator(_DRAWER_SELECTOR).first
    markdown_parts: list[str] = []
    headings = drawer.locator("h3")
    heading_count = quick_locator_count(
        headings, description="overview drawer headings"
    )

    for index in range(heading_count):
        heading = headings.nth(index)
        section_name = heading.text_content().strip()
        if not section_name:
            continue

        _append_drawer_section_to_markdown(
            markdown_parts,
            section_name,
            heading,
        )

    _close_open_drawer(page)

    if not markdown_parts:
        return None

    return "\n".join(markdown_parts).strip()


# PARSE MOT EXPIRY DATE
def _parse_mot_expiry_date(raw: str | None) -> date | None:
    """
    Parse a PistonHeads MOT expiry value such as 12 Jun 2025 into a date.
    """

    if not raw:
        return None

    cleaned = raw.strip()
    if "exempt" in cleaned.lower():
        return None

    month_match = _MOT_EXPIRY_DATE_PATTERN.search(cleaned)
    if month_match:
        for pattern in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(month_match.group(1), pattern).date()
            except ValueError:
                continue

    numeric_match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", cleaned)
    if numeric_match:
        day = int(numeric_match.group(1))
        month = int(numeric_match.group(2))
        year = int(numeric_match.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            return None

    return None


# EXTRACT VEHICLE HISTORY MOT
def _extract_vehicle_history_mot(page: Page) -> tuple[str | None, date | None]:
    """
    Open the Vehicle history drawer and read the MOT status and expiry date.
    """

    if not _open_drawer(page, _VEHICLE_HISTORY_BUTTON_TEXT):
        return None, None

    drawer = page.locator(_DRAWER_SELECTOR).first
    mot_status = None
    mot_expiry = None

    if quick_locator_count(
        drawer.get_by_text("MOT Exempt", exact=False),
        description="mot exempt label",
    ) > 0:
        _close_open_drawer(page)
        return "MOT Exempt", None

    expiry_label = drawer.locator('dt:has-text("MOT expiry")')
    if quick_locator_count(expiry_label, description="mot expiry label") > 0:
        expiry_value = expiry_label.first.locator("xpath=following-sibling::dd[1]")
        if quick_locator_count(expiry_value, description="mot expiry value") > 0:
            expiry_text = expiry_value.first.text_content().strip()
            mot_expiry = _parse_mot_expiry_date(expiry_text)
            if expiry_text:
                mot_status = f"MOT expires {expiry_text}"

    if mot_status is None:
        mot_heading = drawer.locator('h3:has-text("MOT")')
        if quick_locator_count(mot_heading, description="mot heading") > 0:
            mot_status = mot_heading.first.text_content().strip() or None

    _close_open_drawer(page)
    return mot_status, mot_expiry


# PARSE SPEC VALUE
def _parse_spec_pairs(page: Page) -> dict[str, str]:
    """
    Read the overview specification block on a listing detail page, returning
    lower-case label to value mappings from adjacent dt/dd pairs.
    """

    labels = page.locator("dl dt")
    values = page.locator("dl dd")
    label_count = quick_locator_count(labels, description="spec labels")
    value_count = quick_locator_count(values, description="spec values")
    count = min(label_count, value_count)

    specs: dict[str, str] = {}
    for index in range(count):
        label = labels.nth(index).text_content().strip().lower()
        value = values.nth(index).text_content().strip()
        if label:
            specs[label] = value

    return specs


# PARSE PRICE TEXT
def _parse_price_text(price_text: str | None) -> tuple[int | None, str | None]:
    """
    Parse a visible price string such as £19,995 or POA into amount and symbol.
    """

    if not price_text:
        return None, None

    cleaned = price_text.strip()
    if _POA_PRICE_PATTERN.search(cleaned):
        return None, None

    match = re.search(r"([£$€])([\d,]+)", cleaned)
    if not match:
        return None, None

    return int(match.group(2).replace(",", "")), match.group(1)


# PARSE ENGINE SIZE
def _parse_engine_size(raw: str | None) -> str | None:
    """
    Normalise an engine size value such as 5.0L into a litres string.
    """

    if not raw:
        return None

    match = re.search(r"([\d.]+)\s*L", raw, re.IGNORECASE)
    if match:
        return f"{match.group(1)}L"

    return raw.strip() or None


# WAIT FOR LISTING DETAIL PAGE
def wait_for_listing_detail_page(page: Page) -> None:
    """
    Wait until a PistonHeads listing detail page has rendered its title or an
    unavailable banner, polling for late cookie consent between attempts so a
    modal that loads after the shell does not block extraction.
    """

    outcome = page.locator("h1").or_(
        page.get_by_text(UNAVAILABLE_ADVERT_TEXTS[0], exact=False)
    )

    def wait_for_outcome(timeout_ms: int) -> None:
        def wait_slice(slice_ms: int) -> None:
            outcome.first.wait_for(state="attached", timeout=slice_ms)

        wait_with_poll(
            wait_slice,
            poll=lambda: _poll_cookie_consent(page),
            timeout_ms=timeout_ms,
            poll_interval=_CONSENT_POLL_INTERVAL_S,
        )

    run_with_timeout_backoff(
        wait_for_outcome,
        description="listing detail page",
    )
    accept_cookies(page, wait_for_banner=True, timeout=10000)


# IS LISTING NO LONGER AVAILABLE
def is_listing_no_longer_available(page: Page) -> bool:
    """
    Return True when the listing page shows the advert has been removed or is
    otherwise no longer purchasable.
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
    Evaluate whether the current listing detail page is unavailable using banner
    text, HTTP-style not-found copy, or missing price on a live layout.
    """

    for text in UNAVAILABLE_ADVERT_TEXTS:
        if quick_locator_count(
            page.get_by_text(text, exact=False),
            description="unavailable advert banner",
        ) > 0:
            return True

    try:
        content = page_html(page)
        lowered = content.lower()
        if any(text.lower() in lowered for text in UNAVAILABLE_ADVERT_TEXTS):
            return True
    except Exception:
        pass

    return False


# EXTRACT LISTING DETAILS
def extract_listing_details(
    page: Page,
    hash_code: str,
    source_id: str | None,
    fallback_title: str | None = None,
) -> tuple[ProspectListings, list[str]]:
    """
    Extract prospect listing fields from an individual listing detail page and
    return a populated ProspectListings instance along with gallery image URLs.
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

    accept_cookies(page, wait_for_banner=True, timeout=10000)

    url = page.url
    current_datetime = datetime.now()

    h1 = page.locator("h1").first
    h1_text = (
        h1.text_content().strip()
        if quick_locator_count(h1, description="listing h1") > 0
        else ""
    )
    make_and_model = h1_text or (fallback_title or "").strip() or "Unknown"

    specs = _parse_spec_pairs(page)
    mileage, mileage_unit = parse_mileage(specs.get("mileage"))
    engine_size = _parse_engine_size(specs.get("engine"))
    fuel_type = specs.get("fuel")
    gearbox_type = specs.get("gearbox")
    number_of_owners = None

    owners_raw = specs.get("prev owners") or specs.get("previous owners")
    if owners_raw:
        owners_match = re.search(r"\d+", owners_raw)
        if owners_match:
            number_of_owners = int(owners_match.group(0))

    year = None
    year_match = re.search(r"\b(19|20)\d{2}\b", make_and_model)
    if year_match:
        year = int(year_match.group(0))

    asking_price = None
    currency_symbol = None
    price_locator = page.locator("span:has-text('£')").first
    if quick_locator_count(price_locator, description="listing price") > 0:
        asking_price, currency_symbol = _parse_price_text(
            price_locator.text_content()
        )

    short_description = make_and_model
    full_description = None
    description_short, description_full = _extract_listing_description(page)
    if description_full:
        full_description = description_full
        short_description = description_short or make_and_model

    specs_and_features = _extract_specs_and_features(page)
    mot_status, mot_expiry = _extract_vehicle_history_mot(page)

    location = None
    location_match = re.search(
        r"([A-Za-z .'-]+,\s*United Kingdom)",
        page.locator("body").inner_text(),
    )
    if location_match:
        location = location_match.group(1)

    body_type = None
    body_match = re.search(
        r"\b(Convertible|Coupe|Estate|SUV|Hatchback|Saloon|MPV|Pick Up|Other)\b",
        page.locator("body").inner_text(),
        re.IGNORECASE,
    )
    if body_match:
        body_type = body_match.group(1).title()

    image_urls = extract_gallery_images(page)

    prospect_listing = ProspectListings(
        hash_code=hash_code,
        source_id=source_id,
        listing_source=ListingSource.PISTONHEADS,
        listing_type=ListingType.CLASSIC,
        status=ProspectListingStatus.NEW,
        url=url,
        make_and_model=make_and_model,
        short_description=short_description,
        full_description=full_description,
        asking_price=asking_price,
        currency_symbol=currency_symbol,
        mileage=mileage,
        mileage_unit=mileage_unit,
        fuel_type=fuel_type,
        gearbox_type=gearbox_type,
        body_type=body_type,
        engine_size=engine_size,
        year=year,
        location=location,
        number_of_owners=number_of_owners,
        specs_and_features=specs_and_features,
        mot_status=mot_status,
        mot_expiry=mot_expiry,
        created_at=current_datetime,
        updated_at=current_datetime,
    )

    return prospect_listing, image_urls


# SNAPSHOT LISTING CANDIDATES
def _snapshot_listing_candidates(page: Page) -> list[tuple[str, str, str | None, str]]:
    """
    Collect every listing card link on the current results page before navigating
    to any detail page, returning (card_id, listing_url, source_id, title) tuples.
    """

    links = page.locator(_SEARCH_RESULTS_LINK_SELECTOR)
    link_count = quick_locator_count(links, description="search result links")
    listing_candidates: list[tuple[str, str, str | None, str]] = []
    seen_source_ids: set[str] = set()

    for index in range(link_count):
        link = links.nth(index)
        href = link.get_attribute("href")
        source_id = extract_source_id(href)
        if source_id is None or source_id in seen_source_ids:
            continue

        seen_source_ids.add(source_id)
        card_info = link.evaluate(
            """el => {
                const root = el.closest('div');
                const heading = root?.querySelector('h2,h3,h4')?.textContent?.trim();
                return { heading: heading || '' };
            }"""
        )
        title = card_info.get("heading") or f"Listing {source_id}"
        listing_url = (
            href
            if href.startswith("http")
            else f"https://www.pistonheads.com{href}"
        )
        listing_candidates.append((source_id, listing_url, source_id, title))

    return listing_candidates


# APPLY SEARCH FILTERS
def _apply_search_filters(page: Page) -> str:
    """
    Load the classic/private/classifieds search results sorted by Most recent.
    Filters are applied via kebab-case query parameters on the search URL and
    the native sort select, avoiding the filter overlay that depends on client
    hydration behind Cloudflare on proxied sessions. Returns the canonical
    page-one search url.
    """

    goto_with_captcha_handling(page, _FILTERED_SEARCH_URL)
    accept_cookies(page, wait_for_banner=True, timeout=8000)
    pause_for_page(page)
    _apply_most_recent_sort(page)
    wait_for_selector_with_backoff(
        page,
        _SEARCH_RESULTS_LINK_SELECTOR,
        state="attached",
        description="search result links",
    )

    return _canonical_filtered_search_url(page.url)


# SCRAPE LISTINGS
def scrape_listings(
    page: Page,
    search_url: str,
    deadline: float | None = None,
    resume: ScrapeResumeState | None = None,
):
    """
    Walk up to _MAX_SEARCH_BATCHES of PistonHeads search result batches and
    save new listings. Each batch loads the filtered search url, optionally
    clicks View more to reach the saved batch number, snapshots cards, then
    visits each candidate directly without returning to the grid between items.
    """

    existing_source_ids = get_existing_source_ids(ListingSource.PISTONHEADS)
    log_loaded_source_ids(len(existing_source_ids))

    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    engine = create_engine_with_retry(database_url)
    SessionLocal = sessionmaker(bind=engine)

    new_count = 0
    processed_ids = resume.processed_ids if resume is not None else set()
    batch_number = resume.page_number if resume is not None else 1
    previous_batch_listing_ids: set[str] = set()

    if resume is not None and resume.search_url is not None:
        search_url = _canonical_filtered_search_url(resume.search_url)
        log_resume_scrape(batch_number, len(processed_ids))

    log_paginating_start("PistonHeads")

    while True:
        _load_search_batch(page, search_url, batch_number)
        accept_cookies(page, wait_for_banner=True, timeout=8000)
        pause_for_page(page)

        if not search_results_present(page, _SEARCH_RESULTS_LINK_SELECTOR):
            log_no_listings_on_page(batch_number)
            break

        listing_candidates = _snapshot_listing_candidates(page)
        batch_listing_ids = {card_id for card_id, _, _, _ in listing_candidates}

        if batch_listing_ids and batch_listing_ids == previous_batch_listing_ids:
            log_repeat_page(batch_number)
            break

        previous_batch_listing_ids = batch_listing_ids
        log_results_page(batch_number, len(listing_candidates))

        for index, (card_id, listing_url, source_id, title) in enumerate(
            listing_candidates, start=1
        ):
            if card_id in processed_ids:
                log_already_processed(index, title)
                continue

            if resume is not None:
                resume.search_url = search_url
                resume.page_number = batch_number
                resume.processed_ids = processed_ids

            if deadline is not None and time.monotonic() >= deadline:
                raise ProxySessionExpired(
                    "proxy rotation interval elapsed during listing scrape"
                )

            if source_id is not None and source_id in existing_source_ids:
                log_already_exists(index, title)
                processed_ids.add(card_id)
                continue

            hash_code = generate_hash_code(
                f"{title}|{source_id}" if source_id else title
            )

            log_timestamp()

            try:
                response = goto_with_captcha_handling(page, listing_url)
                accept_cookies(page, wait_for_banner=True, timeout=15000)
                wait_for_listing_detail_page(page)
                pause_for_page(
                    page,
                    min_seconds=_PAUSE_LISTING_MIN_S,
                    max_seconds=_PAUSE_LISTING_MAX_S,
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
                    with SessionLocal() as session:
                        persist_unavailable_listing_stub(
                            session,
                            listing_source=ListingSource.PISTONHEADS,
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

                prospect_listing, image_urls = extract_listing_details(
                    page, hash_code, source_id, fallback_title=title
                )
                prospect_listing.status_checked_at = datetime.now()
                new_count += 1

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

                    def before_ai() -> None:
                        _poll_cookie_consent(page)

                    try:
                        persist_listing_with_images_and_ai(
                            session,
                            prospect_listing,
                            ai_prompt_filename="classic_car_prompt.md",
                            listing_url=listing_url,
                            image_urls=image_urls,
                            page=page,
                            temp_dir_prefix="pistonheads_images_",
                            page_hook=_poll_cookie_consent,
                            before_ai=before_ai,
                            after_ai=before_ai,
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
                    listing_source=ListingSource.PISTONHEADS,
                    listing_type=ListingType.CLASSIC,
                    is_unavailable_fn=is_listing_no_longer_available,
                    config=CONFIG,
                    deadline=deadline,
                )

            except Exception as e:
                reraise_or_log_listing_error(index, title, listing_url, e)

            pause_for_page(
                page,
                min_seconds=_PAUSE_BETWEEN_LISTINGS_MIN_S,
                max_seconds=_PAUSE_BETWEEN_LISTINGS_MAX_S,
            )

        if batch_number >= _MAX_SEARCH_BATCHES:
            print(
                f"Reached max search batches ({_MAX_SEARCH_BATCHES}); "
                "stopping pagination"
            )
            break

        # the next loop reloads search and clicks View more batch_number times
        probe_batch = batch_number + 1
        _load_search_batch(page, search_url, probe_batch)
        if not search_results_present(page, _SEARCH_RESULTS_LINK_SELECTOR):
            break

        probe_ids = {
            card_id
            for card_id, _, _, _ in _snapshot_listing_candidates(page)
        }
        if not probe_ids or probe_ids == batch_listing_ids:
            break

        batch_number = probe_batch
        if resume is not None:
            resume.page_number = batch_number
            resume.processed_ids = processed_ids
        pause_for_page(page, min_seconds=_PAUSE_BATCH_MIN_S, max_seconds=_PAUSE_BATCH_MAX_S)

    log_scrape_finished(batch_number, new_count, len(processed_ids))


# OPEN SESSION
def _open_session(playwright) -> tuple:
    """
    Launch a fresh proxied Chromium session on the PistonHeads search page and
    accept cookies. Returns the browser and its page.
    """

    def on_ready(page: Page) -> None:
        pause_for_page(
            page,
            min_seconds=_PAUSE_SESSION_MIN_S,
            max_seconds=_PAUSE_SESSION_MAX_S,
        )
        accept_cookies(page, wait_for_banner=True, timeout=10000)

    return open_proxied_session(
        playwright,
        headless=is_headless,
        landing_url="https://www.pistonheads.com/buy/search",
        on_ready=on_ready,
    )


# PISTONHEADS
def pistonheads() -> None:
    """
    Scrape new PistonHeads private classified listings matching keyword classic
    inside a proxy-rotation loop. When a sticky session expires mid-run the
    browser is relaunched on a fresh proxy port and the sweep resumes on the
    same results batch where it was interrupted.
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
    pistonheads()
