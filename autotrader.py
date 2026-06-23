import os
import random
import re
import shutil
import time
from dataclasses import dataclass
from datetime import date, datetime

from runtime_flags import resolve_runtime_flags

is_headless = resolve_runtime_flags()

from environments import load_environment

load_environment()

from stealth_browser import (
    CaptchaSolveError,
    Page,
    goto_with_captcha_handling,
    is_captcha_present,
    launch_stealth_chromium,
    new_stealth_page,
    wait_for_captcha_solve,
)
from sqlalchemy.orm import sessionmaker

from common import (
    create_engine_with_retry,
    generate_hash_code,
    get_existing_source_ids,
    is_http_not_found,
    pause,
    wait_for_selector_with_backoff,
    with_db_retry,
)
from listing_images import (
    delete_listing,
    download_and_save_listing_images,
)
from ai_analysis import process_ai_analysis_for_listing
from models.auto_ads import Images, ProspectListings
from models.enums import ListingSource, ListingTable, ListingType, ProspectListingStatus
from scraper_driver import (
    ProxyRotationConfig,
    ProxySessionExpired,
    ScrapeResumeState,
    persist_unavailable_listing_stub,
    run_with_proxy_rotation,
    search_results_present,
    update_new_listings_availability,
    with_page_param,
)

UNAVAILABLE_ADVERT_TEXT = (
    "The advert you are looking for is no longer available"
)

CONFIG = ProxyRotationConfig.from_env_prefix("AUTOTRADER")

AUTOTRADER_VANS_URL = "https://www.autotrader.co.uk/vans/used-vans"
AUTOTRADER_CARS_URL = "https://www.autotrader.co.uk/cars"


# AUTOTRADER SCRAPE CONFIG
@dataclass
class AutotraderScrapeConfig:
    """
    Parameters that distinguish a vans scrape from a classics scrape while
    sharing the same proxy-rotation driver and listing-extraction logic.
    """

    landing_url: str
    listing_type: ListingType
    ai_prompt_filename: str
    select_classic_cars: bool = False


# reading a single listing involves many deliberate human-like pauses, so give
# element actions (clicks, waits, locator queries) a generous five-minute budget
# rather than Playwright's 30s default that was tripping mid-listing
LISTING_ACTION_TIMEOUT_MS = int(
    os.getenv("AUTOTRADER_ACTION_TIMEOUT_MS", str(5 * 60 * 1000))
)

# keep navigation on a short timeout so a stalled proxy exit IP still surfaces
# quickly as a timeout and triggers a rotation instead of hanging for minutes
NAVIGATION_TIMEOUT_MS = int(os.getenv("AUTOTRADER_NAVIGATION_TIMEOUT_MS", "30000"))


# reading a single listing involves many deliberate human-like pauses, so give
def is_listing_no_longer_available(page: Page) -> bool:
    """
    Return True when the listing page shows the "advert no longer available"
    banner. When an advert is pulled, Autotrader serves a client-rendered page
    carrying an atds-banner__copy paragraph whose text begins with
    UNAVAILABLE_ADVERT_TEXT before suggesting similar vehicles.
    """

    # the listing page is client-rendered, so after domcontentloaded neither the
    # banner nor the listing body exists yet; wait for whichever renders first so
    # a removed advert is not misread as available just because the check ran too
    # early
    outcome = page.locator(
        ".atds-banner__copy, [data-testid='advert-price']"
    )
    try:
        outcome.first.wait_for(state="attached", timeout=15000)
    except Exception:
        pass

    # prefer the dedicated banner element so styling-hash class changes or a
    # trailing "similar vehicles" suffix never break detection
    banner_copy = page.locator(
        ".atds-banner__copy", has_text=UNAVAILABLE_ADVERT_TEXT
    )
    if banner_copy.count() > 0:
        return True

    # fall back to scanning the rendered html in case the banner markup changes
    try:
        if UNAVAILABLE_ADVERT_TEXT in page.content():
            return True
    except Exception:
        pass

    # final fallback: a page-wide visible-text search
    unavailable_banner = page.get_by_text(UNAVAILABLE_ADVERT_TEXT, exact=False)
    return unavailable_banner.count() > 0


# CONSENT DISMISS BUTTONS
_CONSENT_DISMISS_BUTTONS = (
    ("Reject All", "button.sp_choice_type_13"),
    ("Essential Cookies Only", None),
    ("Accept All", "button.sp_choice_type_11"),
)


# IS COOKIE CONSENT VISIBLE
def _is_cookie_consent_visible(page: Page) -> bool:
    """
    Return True when the Sourcepoint consent modal is on screen and blocking
    interaction with the page underneath.
    """

    consent_container = page.locator("[id*='sp_message_container']")
    if consent_container.count() > 0 and consent_container.first.is_visible():
        return True

    consent_iframe = page.frame_locator("iframe[id*='sp_message_iframe']")
    notice = consent_iframe.locator("#notice")
    if notice.count() > 0 and notice.first.is_visible():
        return True

    for button_name, _ in _CONSENT_DISMISS_BUTTONS:
        button = consent_iframe.get_by_role("button", name=button_name, exact=True)
        if button.count() > 0 and button.first.is_visible():
            return True

    return False


# WAIT FOR COOKIE CONSENT BANNER
def _wait_for_cookie_consent_banner(page: Page, *, timeout_ms: int = 10000) -> bool:
    """
    Wait until the Sourcepoint consent modal appears, trying the outer container
    first and then the iframe notice or dismiss buttons.
    """

    try:
        page.locator("[id*='sp_message_container']").first.wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except Exception:
        pass

    consent_iframe = page.frame_locator("iframe[id*='sp_message_iframe']")
    try:
        consent_iframe.locator("#notice").first.wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except Exception:
        pass

    for button_name, css_fallback in _CONSENT_DISMISS_BUTTONS:
        button = consent_iframe.get_by_role("button", name=button_name, exact=True)
        try:
            button.first.wait_for(state="visible", timeout=2000)
            return True
        except Exception:
            if css_fallback:
                try:
                    consent_iframe.locator(css_fallback).first.wait_for(
                        state="visible", timeout=2000
                    )
                    return True
                except Exception:
                    pass

    return False


# CLICK COOKIE CONSENT DISMISS BUTTON
def _click_cookie_consent_dismiss_button(page: Page) -> bool:
    """
    Click the least-permissive available dismiss button inside the consent iframe.
    """

    consent_iframe = page.frame_locator("iframe[id*='sp_message_iframe']")

    for button_name, css_fallback in _CONSENT_DISMISS_BUTTONS:
        button = consent_iframe.get_by_role("button", name=button_name, exact=True)
        try:
            button.first.wait_for(state="visible", timeout=5000)
            button.first.click()
            print(f"Clicked '{button_name}' on cookie consent")
            return True
        except Exception:
            if css_fallback:
                fallback = consent_iframe.locator(css_fallback)
                try:
                    fallback.first.wait_for(state="visible", timeout=2000)
                    fallback.first.click()
                    print(f"Clicked '{button_name}' on cookie consent")
                    return True
                except Exception:
                    pass

    return False


# DISMISS COOKIE CONSENT
def dismiss_cookie_consent(page: Page, *, wait_for_banner: bool = False) -> None:
    """
    Dismiss the AutoTrader cookie consent modal when it is visible inside its
    iframe. Pass wait_for_banner=True once immediately after a navigation so
    the late-loading banner can be handled; otherwise only act when the modal is
    already on screen so repeated calls do not probe for a new banner to appear.
    """

    if not _is_cookie_consent_visible(page):
        if not wait_for_banner:
            return

        # the banner often appears several seconds after domcontentloaded
        if not _wait_for_cookie_consent_banner(page):
            return

    if not _is_cookie_consent_visible(page):
        return

    if not _click_cookie_consent_dismiss_button(page):
        return

    # wait for the modal to clear so it does not intercept later clicks
    try:
        page.locator("[id*='sp_message_container']").first.wait_for(
            state="hidden", timeout=10000
        )
    except Exception:
        pass


# SELECT CLASSIC CARS FILTER
def _select_classic_cars_filter(page: Page) -> None:
    """
    Expand the "I'm looking for" facet on the cars landing page and select
    Classic cars. The option is a visually hidden checkbox with id "classic"
    inside a label, not the seller-type-style data-testid containers used on
    the vans channel.
    """

    # expand the "I'm looking for" facet group
    looking_facet = page.get_by_test_id("category-facet-group")
    if looking_facet.count() == 0:
        looking_facet = page.get_by_test_id("journey-facet-group")
    if looking_facet.count() == 0:
        looking_facet = page.locator('[data-testid$="-facet-group"]').filter(
            has_text="I'm looking for"
        )
    looking_facet.first.wait_for(state="visible")
    looking_facet.first.click()
    print("Expanded 'I'm looking for'")
    pause()

    # the classic option is input#classic inside label[for="classic"]
    classic_checkbox = page.locator("#classic")
    classic_checkbox.wait_for(state="attached")
    if classic_checkbox.is_checked():
        print("Classic cars already selected")
        pause()
        return

    classic_label = page.locator('label[for="classic"]')
    if classic_label.count() > 0:
        classic_label.first.click()
        print("Selected 'Classic cars'")
        pause()
        return

    # fall back to accessible-name locators when the id attribute changes
    classic_option = page.get_by_role("checkbox", name="Classic cars")
    if classic_option.count() == 0:
        classic_option = page.get_by_text("Classic cars", exact=True)
    classic_option.first.wait_for(state="visible")
    classic_option.first.click()
    print("Selected 'Classic cars'")
    pause()


# ENGLISH POSTCODES
_ENGLISH_POSTCODES = (
    "M1 1AE",
    "B1 1BB",
    "LS1 1UR",
    "BS1 4DJ",
    "NG1 1AA",
    "S1 1AA",
    "L1 8JQ",
    "NE1 4ST",
    "CB1 1PT",
    "OX1 1DP",
    "BN1 1GE",
    "EX1 1QA",
    "PL1 1AA",
    "YO1 7HH",
    "HU1 1UU",
    "LE1 1AA",
    "CV1 1AA",
    "ST1 1AA",
    "DE1 1AA",
    "GL1 1AA",
    "BA1 1AA",
    "SN1 1AA",
    "RG1 1AA",
    "MK9 1AA",
    "PO1 1AA",
    "SO14 1AA",
    "NR1 1AA",
    "IP1 1AA",
    "CO1 1AA",
    "CM1 1AA",
    "SS1 1AA",
    "TN1 1AA",
    "GU1 1AA",
    "RH1 1AA",
    "KT1 1AA",
    "CR0 1AA",
    "TW1 1AA",
    "HA1 1AA",
    "UB1 1AA",
    "SL1 1AA",
    "HP1 1AA",
    "LU1 1AA",
    "PE1 1AA",
    "NN1 1AA",
    "DN1 1AA",
    "HU10 6AA",
    "WF1 1AA",
    "HD1 1AA",
    "HX1 1AA",
    "BD1 1AA",
    "HG1 1AA",
    "DL1 1AA",
    "TS1 1AA",
    "SR1 1AA",
    "DH1 1AA",
    "CA1 1AA",
    "LA1 1AA",
    "FY1 1AA",
    "PR1 1AA",
    "BB1 1AA",
    "OL1 1AA",
    "SK1 1AA",
    "WA1 1AA",
    "CW1 1AA",
    "ST5 1AA",
    "TF1 1AA",
    "SY1 1AA",
    "WR1 1AA",
    "DY1 1AA",
    "WS1 1AA",
    "WV1 1AA",
    "B90 1AA",
    "CV6 1AA",
    "LE11 1AA",
    "NG7 1AA",
    "S10 1AA",
    "CH1 1AA",
    "L2 2AA",
    "M2 2AA",
    "EC1A 1BB",
    "W1A 1AA",
    "SE1 1AA",
    "N1 1AA",
    "E1 1AA",
)


# RANDOM ENGLISH POSTCODE
def random_english_postcode() -> str:
    """
    Return a randomly chosen valid English postcode for Autotrader distance
    searches.
    """

    return random.choice(_ENGLISH_POSTCODES)


# FILL LANDING POSTCODE
def _fill_landing_postcode(page: Page, postcode: str) -> None:
    """
    Enter a postcode on the Autotrader landing search form and wait until the
    React-controlled field accepts the value. The postcode input is often not
    ready immediately after cookie consent, so a single fill() can look
    successful while the form state stays empty and More options refreshes
    without opening the filter panel.
    """

    # wait until the postcode input is hydrated and interactable
    page.wait_for_function(
        """
        () => {
            const input = document.getElementById('postcode');
            return input && !input.disabled && input.offsetParent !== null;
        }
        """,
        timeout=15000,
    )
    pause(1.0, 2.0)

    postcode_input = page.locator("#postcode")
    max_attempts = 5

    for _ in range(max_attempts):
        # retry fill until the controlled input reports the expected value
        postcode_input.fill(postcode)
        pause(0.5, 1.0)
        if postcode_input.input_value() == postcode:
            print(f"Entered postcode: {postcode}")
            return

    raise RuntimeError(
        f"Failed to fill landing-page postcode after {max_attempts} attempts"
    )


# APPLY SEARCH FILTERS
def apply_search_filters(page: Page, *, select_classic_cars: bool = False) -> str:
    """
    On an Autotrader landing page, expand More options, optionally filter to
    classic cars, restrict to private sellers, set distance to National, sort
    by most recent, and run the search. Assumes the page is already on the
    configured landing URL and cookie consent has been handled. Returns the
    results-page URL.
    """

    # dismiss only if the banner is already visible; do not wait for a new one
    dismiss_cookie_consent(page)
    pause()

    # fill the landing-page postcode before expanding filters
    _fill_landing_postcode(page, random_english_postcode())
    pause()

    # the used-vans landing page exposes "More options" as a plain button with
    # no data-testid, so fall back to matching it by its accessible name
    more_options = page.get_by_test_id("more-options-button")
    if more_options.count() == 0:
        more_options = page.get_by_role("button", name="More options")
    more_options.first.wait_for(state="visible")
    more_options.first.click()
    print("Clicked 'More options'")
    pause()

    if select_classic_cars:
        _select_classic_cars_filter(page)

    # seller type: private sellers only
    seller_facet = page.get_by_test_id("seller_type-facet-group")
    seller_facet.wait_for(state="visible")
    seller_facet.click()
    print("Expanded 'Seller type'")
    pause()

    trade_checkbox = page.locator("#seller_type-trade_sellers-checkbox")
    if trade_checkbox.is_checked():
        page.get_by_test_id("seller_type-trade_sellers-container").click()
        print("Unchecked trade sellers")
        pause()

    private_checkbox = page.locator("#seller_type-private_sellers-checkbox")
    if not private_checkbox.is_checked():
        page.get_by_test_id("seller_type-private_sellers-container").click()
        print("Selected private sellers")
        pause()

    # distance from you: national (empty option value)
    distance_facet = page.get_by_test_id("distance-facet-group")
    distance_facet.click()
    print("Expanded 'Distance from you'")
    pause()

    page.locator("select#distance").select_option(value="")
    print("Set distance to National")
    pause()

    # sort: most recent
    sort_facet = page.get_by_test_id("sort-facet-group")
    sort_facet.click()
    print("Expanded 'Sort'")
    pause()

    most_recent = page.get_by_test_id("most-recent-radio-testid")
    most_recent.wait_for(state="visible")
    most_recent.click()
    print("Selected 'Most recent' sort option")
    pause()

    search_button = page.get_by_test_id("search-apply-button")
    search_button.wait_for(state="visible")
    search_button.click()
    print("Clicked 'Search' to apply filters")
    pause()

    wait_for_selector_with_backoff(
        page,
        'li[data-testid^="id-"]',
        state="attached",
        description="search results grid",
    )
    search_url = page.url
    print(f"Navigated to search results: {search_url}")
    pause()

    return search_url


# CONFIGURE SEARCH
def configure_search(page: Page, config: AutotraderScrapeConfig) -> str:
    """
    Navigate to the configured Autotrader landing page, dismiss cookie consent
    once, and apply the standard search filters. Returns the results-page URL so
    proxy rotation can resume on the same search.
    """

    goto_with_captcha_handling(page, config.landing_url)
    print(f"Navigated to {config.landing_url}")
    pause()

    dismiss_cookie_consent(page, wait_for_banner=True)
    pause()

    return apply_search_filters(
        page, select_classic_cars=config.select_classic_cars
    )


# EXTRACT SOURCE ID
def extract_source_id(url: str | None) -> str | None:
    """
    Extract the Autotrader listing reference (e.g. '202606113190878') from a
    listing URL such as 'https://www.autotrader.co.uk/van-details/202606113190878'.
    Returns None when the URL does not contain a van-details or car-details segment.
    """

    match = re.search(r"/(?:van|car)-details/(\d+)", url or "")
    return match.group(1) if match else None


# GET SPECS AND FEATURES
def get_specs_and_features(page: Page) -> str | None:
    """
    Click the 'View all spec and features' button, expand all accordion sections,
    and extract all specs and features into markdown"""

    # try to find and click the "View all spec and features" button
    view_all_button = page.get_by_test_id("view-all-spec-and-features-signpost")
    if view_all_button.count() == 0:
        return None

    try:
        view_all_button.click()
        pause(1.0, 2.0)
    except Exception:
        return None

    # wait for the popup to appear
    popup = page.locator("div.ppa-enabled")
    if popup.count() == 0:
        return None

    # expand all collapsed accordion sections
    collapsed_buttons = popup.locator('button[aria-expanded="false"]')
    collapsed_count = collapsed_buttons.count()

    for i in range(collapsed_count):
        try:
            # re-query to handle dynamic DOM changes
            collapsed_buttons = popup.locator('button[aria-expanded="false"]')
            if collapsed_buttons.count() > 0:
                collapsed_buttons.first.click()
                pause(1.0, 3.0)
        except Exception:
            pass

    markdown_parts = []

    # process feature accordions
    feature_sections = popup.locator('section[data-testid="feature-accordion"]')
    feature_count = feature_sections.count()

    if feature_count > 0:
        markdown_parts.append("## All Features\n")

        for i in range(feature_count):
            section = feature_sections.nth(i)

            # get category name from button text
            category_button = section.locator("button").first
            if category_button.count() > 0:
                # get the category name (first span text, excluding the count badge)
                category_span = category_button.locator("span").first
                if category_span.count() > 0:
                    category_name = category_span.inner_text().strip()
                    # remove trailing number if present (the count badge)
                    category_name = re.sub(r"\d+$", "", category_name).strip()
                    markdown_parts.append(f"### {category_name}\n")

            # get all feature items
            list_items = section.locator("li")
            for j in range(list_items.count()):
                item = list_items.nth(j)
                spans = item.locator("span")

                # features have a single span with the feature name
                if spans.count() == 1:
                    feature_text = spans.first.inner_text().strip()
                    markdown_parts.append(f"- {feature_text}")

            markdown_parts.append("")

    # process spec accordions
    spec_sections = popup.locator('section[data-testid="spec-accordion"]')
    spec_count = spec_sections.count()

    if spec_count > 0:
        markdown_parts.append("## Specs\n")

        for i in range(spec_count):
            section = spec_sections.nth(i)

            # get category name from button text
            category_button = section.locator("button").first
            if category_button.count() > 0:
                # get the category name (first span text, excluding the count badge)
                category_span = category_button.locator("span").first
                if category_span.count() > 0:
                    category_name = category_span.inner_text().strip()
                    # remove trailing number if present (the count badge)
                    category_name = re.sub(r"\d+$", "", category_name).strip()
                    markdown_parts.append(f"### {category_name}\n")

            # get all spec items
            list_items = section.locator("li")
            for j in range(list_items.count()):
                item = list_items.nth(j)
                spans = item.locator("span")

                # specs have two spans: name and value
                if spans.count() >= 2:
                    spec_name = spans.nth(0).inner_text().strip()
                    spec_value = spans.nth(1).inner_text().strip()
                    markdown_parts.append(f"- {spec_name}: {spec_value}")

            markdown_parts.append("")

    # pause before closing the popup
    pause(2.0, 10.0)

    # close the popup using the Back button
    try:
        back_button = page.locator(
            'section[data-testid="spec-feats-modal-back-button"] '
            'button[aria-label="Close"]'
        )
        if back_button.count() > 0:
            back_button.click()
            pause(0.5, 1.0)
    except Exception:
        pass

    if not markdown_parts:
        return None

    return "\n".join(markdown_parts).strip()


# GET BASIC HISTORY CHECK
def get_basic_history_check(page: Page) -> str | None:
    """
    Extract basic vehicle history check information from the vehicle history section.
    """

    # look for the vehicle history section
    history_section = page.locator('section[id="vehicle-history"]')
    if history_section.count() == 0:
        return None

    # try to find the summary text (e.g., "5 checks passed")
    summary_elem = history_section.locator('p:has-text("checks passed")').first
    if summary_elem.count() == 0:
        return None

    summary_text = summary_elem.inner_text().strip()

    # if 5 checks passed, return that
    if summary_text == "5 checks passed":
        return summary_text

    # otherwise, examine each individual check to find failures
    check_labels = [
        "Not recorded as stolen",
        "Not recorded as scrapped",
        "Not imported from another country",
        "Not exported out of the UK",
        "Never been written off",
    ]

    failed_checks = []

    for label in check_labels:
        # find the button containing this check label
        check_button = history_section.locator(f'button:has-text("{label}")').first
        if check_button.count() > 0:
            # look at the svg path to determine pass or fail
            # checkmark path contains "13.439" or similar curved path
            # x path contains different coordinates
            svg_path = check_button.locator("svg path").first
            if svg_path.count() > 0:
                path_d = svg_path.get_attribute("d")
                # checkmark paths typically contain "13.439" or "11.299"
                # if the path doesn't contain these, it's likely an X (fail)
                if path_d and "13.439" not in path_d and "11.299" not in path_d:
                    # derive the failure description from the label
                    if "stolen" in label.lower():
                        failed_checks.append("Recorded as stolen")
                    elif "scrapped" in label.lower():
                        failed_checks.append("Recorded as scrapped")
                    elif "imported" in label.lower():
                        failed_checks.append("Imported from another country")
                    elif "exported" in label.lower():
                        failed_checks.append("Exported out of the UK")
                    elif "written off" in label.lower():
                        failed_checks.append("Has been written off")

    # build the result string
    if failed_checks:
        result = (
            summary_text + "\n" + "\n".join(f"* {check}" for check in failed_checks)
        )
        return result

    return summary_text


# GET OVERVIEW VALUE
def get_overview_value(page: Page, icon_name: str) -> str | None:
    """
    Extract value from overview section by icon data-gui attribute.
    """

    icon = page.locator(f'svg[data-gui="atds-icon-{icon_name}"]')
    if icon.count() > 0:
        # navigate up to the parent container and get the value paragraph
        parent = icon.locator("xpath=ancestor::div[contains(@class, 'sc-tqnfbs-1')]")
        if parent.count() > 0:
            value_elem = parent.locator("p").last
            if value_elem.count() > 0:
                return value_elem.inner_text()
    return None


# SAVE GALLERY IMAGES
def save_gallery_images(
    page: Page, prospect_listing: ProspectListings, session
) -> tuple[str | None, int]:
    """
    Open the full gallery view, load all images (via carousel or scrolling),
    fetch them via HTTP, save to S3 and temporary directory, and create Images database records.
    Returns the path to the temporary directory containing the images and the
    number of image URLs collected, or (None, 0) if no images were saved.
    """

    # click the gallery button to open full gallery view
    gallery_button = page.locator(
        'section[name="gallery"] button:has(span:text("Gallery"))'
    )
    if gallery_button.count() == 0:
        print("Gallery button not found, skipping image extraction")
        return None, 0

    gallery_button.click()
    pause(1.0, 2.0)

    # collect unique image urls
    image_urls = []
    seen_urls = set()

    # let the gallery start loading, but treat full network idle as best-effort:
    # the page's ad/tracking traffic can keep the network busy indefinitely, so
    # never block (or fail) the whole listing waiting for it — the scroll loop
    # below loads and collects images on its own regardless
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        print("  Gallery did not reach network idle; proceeding to load images")
    pause(1.0, 2.0)

    # find the scrollable container in the gallery (look for common scrollable elements)
    scrollable_container = page.evaluate(
        """
        () => {
            // find elements with overflow scroll or auto that have significant height
            const elements = document.querySelectorAll('*');
            for (const el of elements) {
                const style = window.getComputedStyle(el);
                const overflowY = style.overflowY;
                if ((overflowY === 'scroll' || overflowY === 'auto') && 
                    el.scrollHeight > el.clientHeight &&
                    el.clientHeight > 200) {
                    return true;
                }
            }
            return false;
        }
    """
    )

    max_scroll_attempts = 20
    scroll_attempts = 0
    previous_image_count = 0

    while scroll_attempts < max_scroll_attempts:
        # collect current images before scrolling
        img_elements = page.locator("img").all()
        current_image_count = len(img_elements)

        # scroll the scrollable container or window
        page.evaluate(
            """
            () => {
                // find and scroll the scrollable container
                const elements = document.querySelectorAll('*');
                for (const el of elements) {
                    const style = window.getComputedStyle(el);
                    const overflowY = style.overflowY;
                    if ((overflowY === 'scroll' || overflowY === 'auto') && 
                        el.scrollHeight > el.clientHeight &&
                        el.clientHeight > 200) {
                        el.scrollBy(0, window.innerHeight);
                        return;
                    }
                }
                // fallback to window scroll
                window.scrollBy(0, window.innerHeight);
            }
        """
        )
        pause(0.5, 1.0)

        # wait for potential new images to load
        page.wait_for_timeout(500)

        # check if we've reached the bottom of the scrollable container
        at_bottom = page.evaluate(
            """
            () => {
                // check scrollable container first
                const elements = document.querySelectorAll('*');
                for (const el of elements) {
                    const style = window.getComputedStyle(el);
                    const overflowY = style.overflowY;
                    if ((overflowY === 'scroll' || overflowY === 'auto') && 
                        el.scrollHeight > el.clientHeight &&
                        el.clientHeight > 200) {
                        return el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
                    }
                }
                // fallback to window check
                return window.scrollY + window.innerHeight >= document.body.scrollHeight;
            }
        """
        )

        # also check if no new images loaded
        img_elements_after = page.locator("img").all()
        new_image_count = len(img_elements_after)
        if at_bottom and new_image_count == previous_image_count:
            break

        previous_image_count = new_image_count
        scroll_attempts += 1

    # pause to ensure all images are fully loaded
    pause(1.0, 2.0)

    # locate all img elements in the gallery
    img_elements = page.locator("img").all()

    for img_element in img_elements:
        try:
            src = img_element.get_attribute("src")
            if src and src not in seen_urls and src.startswith("http"):
                seen_urls.add(src)
                image_urls.append(src)
        except Exception:
            continue

    # if only one image found, try carousel mode instead
    if len(image_urls) <= 1:
        carousel_next_button = page.locator('button[data-testid="carousel-next-icon"]')

        if carousel_next_button.count() > 0:
            # carousel mode: click through to collect all images
            max_carousel_clicks = 50
            carousel_clicks = 0

            while carousel_clicks < max_carousel_clicks:
                # collect current image url
                img_elements = page.locator("img").all()
                for img_element in img_elements:
                    try:
                        src = img_element.get_attribute("src")
                        if src and src not in seen_urls and src.startswith("http"):
                            seen_urls.add(src)
                            image_urls.append(src)
                    except Exception:
                        continue

                # check if next button is still available and enabled
                if carousel_next_button.count() == 0:
                    break

                # check if button is disabled (reached end of carousel)
                is_disabled = carousel_next_button.get_attribute("disabled")
                if is_disabled is not None:
                    break

                # click next to advance carousel
                try:
                    carousel_next_button.click()
                    pause(0.3, 0.6)
                    carousel_clicks += 1
                except Exception:
                    break

    # download images, save to temp dir and s3, create database records
    temp_dir = download_and_save_listing_images(
        image_urls,
        page,
        prospect_listing,
        session,
        temp_dir_prefix="autotrader_images_",
    )

    # pause before closing gallery
    pause(1.0, 2.0)

    # click the close button to return to listing (works for both carousel and scroll modes)
    close_button = page.get_by_test_id("gallery-close")
    if close_button.count() > 0:
        close_button.click()
        pause(0.5, 1.0)

    return temp_dir, len(image_urls)


# READ FULL FOUND LISTING
def read_full_prospect_listing(
    page: Page,
    expected_short_description: str,
    listing_type: ListingType,
    ai_prompt_filename: str,
) -> ProspectListings | None:
    """
    Extract full listing details from the detail page and return a ProspectListings
    instance with the configured listing type and AI prompt.
    """

    # get the current url
    url = page.url

    # get make and model from h1
    h1_elem = page.locator("h1").first
    make_and_model = h1_elem.inner_text() if h1_elem.count() > 0 else ""

    # get short description from span after h1
    short_desc_elem = page.locator("h1 + div span").first
    short_description = (
        short_desc_elem.inner_text() if short_desc_elem.count() > 0 else ""
    )
    if short_description != expected_short_description:
        raise Exception(
            f"Expected short description {expected_short_description}, got {short_description}"
        )

    # derive the site's own listing reference and a hash that includes it so
    # two different listings with the same subtitle cannot collide
    source_id = extract_source_id(url)
    hash_code = generate_hash_code(
        f"{short_description}|{source_id}" if source_id else short_description
    )

    # get price from data-testid="advert-price"
    price_elem = page.get_by_test_id("advert-price")
    price_text = price_elem.inner_text() if price_elem.count() > 0 else ""

    # check that the record has a price and is not an AUCTION
    if not price_text:
        print(f"No price found for listing: {make_and_model} - {short_description}")
        return None
    if not price_text or price_text == "AUCTION":
        print(f"Indicates AUCTION listing: {make_and_model} - {short_description}")
        return None

    # parse price components
    currency_symbol = price_text[0] if price_text else None
    asking_price = None
    vat_status = None

    # remove currency symbol and parse
    price_parts = price_text[1:].split(" ", 1)
    price_value_text = price_parts[0].replace(",", "")
    asking_price = int(price_value_text)
    # get vat status (everything after the price number)
    if len(price_parts) > 1:
        vat_status = price_parts[1].strip()

    # get location from contact seller section
    location_elem = page.locator('p[class*="sc-1ph9l9h-4"]').first
    location_text = location_elem.inner_text() if location_elem.count() > 0 else ""
    # extract just the city name (before the dash)
    location = location_text.split(" - ")[0].strip() if location_text else None

    # get full description by clicking expand button if available
    full_description = None
    desc_elem = page.locator('section[id="description"] p').first
    if desc_elem.count() > 0:
        full_description = desc_elem.inner_text()

    # try to expand description for full text
    expand_btn = page.get_by_test_id("description-signpost")
    if expand_btn.count() > 0:
        try:
            expand_btn.click()
            pause(1.0, 3.0)
            # get expanded description
            expanded_desc = page.locator('section[id="description"] p').first
            if expanded_desc.count() > 0:
                full_description = expanded_desc.inner_text()
            # click back to return to main listing page
            pause(2.0, 5.0)
            back_button = page.locator(
                'section[data-testid="description-modal-back-button"] '
                'button[aria-label="Close"]'
            )
            if back_button.count() > 0:
                back_button.click()
                pause(1.0, 2.0)
        except Exception:
            pass  # keep the short description if expand fails

    # extract overview fields using helper function
    mileage_text = get_overview_value(page, "mileage")
    mileage = None
    mileage_unit = None
    if mileage_text:
        parts = mileage_text.replace(",", "").split(" ")
        try:
            mileage = int(parts[0])
            mileage_unit = parts[1] if len(parts) > 1 else None  # 'm' for miles
        except (ValueError, IndexError):
            pass

    # get year and registration
    reg_text = get_overview_value(page, "registration")
    year = None
    registration = None
    if reg_text:
        # format: "2007 (57 reg)"
        parts = reg_text.split(" ")
        try:
            year = int(parts[0])
        except ValueError:
            pass
        if "(" in reg_text:
            registration = reg_text.split("(")[-1].replace(")", "").strip()

    # get other overview fields
    body_type = get_overview_value(page, "body-type")
    cab_type = get_overview_value(page, "cab-type")
    wheelbase = get_overview_value(page, "wheelbase")
    engine_size = get_overview_value(page, "engine")
    emission_class = get_overview_value(page, "emission-class")
    gearbox_type = get_overview_value(page, "gearbox")
    fuel_type = get_overview_value(page, "fuel-type")
    colour = get_overview_value(page, "body-colour")

    # get seats as integer
    seats_text = get_overview_value(page, "seats")
    seats = None
    if seats_text:
        try:
            seats = int(seats_text)
        except ValueError:
            pass

    # get basic vehicle history check
    basic_history_check = get_basic_history_check(page)

    # get specs and features from popup
    specs_and_features = get_specs_and_features(page)

    # get number of owners
    number_of_owners = None
    owners_div = page.locator('div:has(> p:text-is("Owners"))').first
    if owners_div.count() > 0:
        owners_value = owners_div.locator("p").nth(1)
        if owners_value.count() > 0:
            owners_text = owners_value.inner_text()
            if owners_text.isnumeric():
                try:
                    number_of_owners = int(owners_text)
                except ValueError:
                    pass

    # get service history
    service_history = get_overview_value(page, "service-history")

    # get mot status and expiry
    mot_status = None
    mot_expiry = None
    mot_heading = page.locator('h3:text-is("MOT Information")').first
    if mot_heading.count() > 0:
        # get the next sibling p element
        mot_status_elem = page.locator(
            'h3:text-is("MOT Information") + p, ' 'h3:text-is("MOT Information") ~ p'
        ).first
        if mot_status_elem.count() > 0:
            mot_status = mot_status_elem.inner_text()
            # extract date from mot_status if present (UK format dd/mm/yyyy)
            if mot_status:
                date_match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", mot_status)
                if date_match:
                    day = int(date_match.group(1))
                    month = int(date_match.group(2))
                    year_val = int(date_match.group(3))
                    try:
                        mot_expiry = date(year_val, month, day)
                    except ValueError:
                        pass

    # set created_at and updated_at to current datetime
    current_datetime = datetime.now()

    # create and return ProspectListings instance
    prospect_listing = ProspectListings(
        hash_code=hash_code,
        source_id=source_id,
        listing_source=ListingSource.AUTOTRADER,
        listing_type=listing_type,
        status=ProspectListingStatus.NEW,
        make_and_model=make_and_model,
        short_description=short_description,
        url=url,
        asking_price=asking_price,
        created_at=current_datetime,
        updated_at=current_datetime,
        status_checked_at=current_datetime,
        full_description=full_description,
        mileage=mileage,
        mileage_unit=mileage_unit,
        year=year,
        registration=registration,
        currency_symbol=currency_symbol,
        vat_status=vat_status,
        location=location,
        body_type=body_type,
        cab_type=cab_type,
        fuel_type=fuel_type,
        gearbox_type=gearbox_type,
        wheelbase=wheelbase,
        engine_size=engine_size,
        colour=colour,
        seats=seats,
        emission_class=emission_class,
        number_of_owners=number_of_owners,
        service_history=service_history,
        basic_history_check=basic_history_check,
        specs_and_features=specs_and_features,
        mot_status=mot_status,
        mot_expiry=mot_expiry,
    )

    # insert into database and populate id field
    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    engine = create_engine_with_retry(database_url)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        # retry the initial persist so a transient db connection failure does
        # not drop the listing; nothing is committed until this block succeeds
        def persist_listing():
            session.add(prospect_listing)
            session.flush()
            session.commit()

        with_db_retry(persist_listing, description="persist prospect listing")

        # save gallery images after listing is committed
        temp_image_dir, image_url_count = save_gallery_images(
            page, prospect_listing, session
        )

        # count the images that actually persisted so a partial save can be detected
        saved_image_count = (
            session.query(Images)
            .filter(
                Images.listing_id == prospect_listing.id,
                Images.listing_table == ListingTable.PROSPECT,
            )
            .count()
        )

        # a shortfall means some images failed to download; discard the whole
        # listing so it is not kept incomplete and gets re-fetched on a later pass
        if image_url_count > 0 and saved_image_count < image_url_count:
            print(
                f"  Partial save ({saved_image_count}/{image_url_count} images) — "
                f"deleting listing {prospect_listing.id}"
            )
            if temp_image_dir and os.path.isdir(temp_image_dir):
                shutil.rmtree(temp_image_dir)
            delete_listing(prospect_listing, session)
            raise RuntimeError(
                f"partial image download for {url} "
                f"({saved_image_count}/{image_url_count} images saved)"
            )

        # generate and apply ai analysis using temp image directory
        prospect_listing = process_ai_analysis_for_listing(
            ai_prompt_filename, prospect_listing, session, temp_image_dir
        )

        # clean up temp directory after use
        if temp_image_dir and os.path.isdir(temp_image_dir):
            shutil.rmtree(temp_image_dir)

        # detach the object from the session so it can be used outside the session context
        session.expunge(prospect_listing)

    print(f"Extracted listing: {make_and_model} - {short_description[:50]}...")

    return prospect_listing


# SEARCH RESULTS LISTING SELECTOR
_SEARCH_RESULTS_LISTING_SELECTOR = 'li[data-testid^="id-"]'


# WAIT FOR SEARCH RESULTS READY
def _wait_for_search_results_ready(page: Page) -> None:
    """
    Wait until the search-results page has finished navigating and at least one
    listing card is visible. Cookie consent on a fresh proxy session can reload
    the page after domcontentloaded, so callers should use this before reading
    the listing cards.
    """

    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.locator(_SEARCH_RESULTS_LISTING_SELECTOR).first.wait_for(
        state="visible", timeout=30000
    )


# SCRAPE LISTINGS
def scrape_listings(
    page: Page,
    search_url: str,
    listing_type: ListingType,
    ai_prompt_filename: str,
    deadline: float | None = None,
    resume: ScrapeResumeState | None = None,
) -> None:
    """
    Walk every page of Autotrader search results and save new listings of the
    given type. Autotrader's desktop search lazy-loads results in ~25-card
    chunks via infinite scroll, so this paginates through the underlying
    page=1, page=2, ... urls instead, stopping once a page renders no cards.
    When deadline (a time.monotonic value) is given, raises ProxySessionExpired
    between listings once it is reached so the proxy can be rotated without
    interrupting a partially-downloaded listing. When resume carries a
    search_url from an interrupted run, continues from the saved results page
    number rather than starting over from page one.
    """

    existing_source_ids = get_existing_source_ids(ListingSource.AUTOTRADER)
    print(f"Loaded {len(existing_source_ids)} existing source ids from database")

    processed_ids = resume.processed_ids if resume is not None else set()
    page_number = resume.page_number if resume is not None else 1
    prospect_listings = []
    previous_page_listing_ids: set[str] = set()
    first_page_load = True

    if resume is not None and resume.search_url is not None:
        search_url = resume.search_url
        print(
            f"Resuming listings scrape from results page {page_number} "
            f"({len(processed_ids)} listings already processed)"
        )

    print(f"Paginating to load all {listing_type.value.lower()} listings...\n")

    while True:
        # walk the underlying page parameter; the desktop ui hides pagination
        # behind infinite scroll, but each page url still resolves its chunk
        page_url = with_page_param(search_url, page_number)
        goto_with_captcha_handling(page, page_url)

        # the first load of a (possibly rotated) session has no stored consent,
        # so wait for the late cookie banner; later pages only need a cheap
        # dismissal if one is somehow still on screen
        dismiss_cookie_consent(page, wait_for_banner=first_page_load)
        first_page_load = False
        pause()

        # an empty page means we have walked past the final results page, so
        # the entire result set has been processed
        if not search_results_present(page, _SEARCH_RESULTS_LISTING_SELECTOR):
            print(
                f"No listings on results page {page_number}; reached the end "
                f"of the result set"
            )
            break

        # snapshot every listing card on this page before navigating to any
        # detail page so the element handles are not invalidated mid-loop
        listing_candidates: list[tuple[str, str, str | None, str]] = []
        page_listing_ids: set[str] = set()
        list_items = page.locator(_SEARCH_RESULTS_LISTING_SELECTOR).all()
        for list_item in list_items:
            listing_id = list_item.get_attribute("data-testid")
            if not listing_id:
                continue
            page_listing_ids.add(listing_id)

            title_link = list_item.locator('a[data-testid="search-listing-title"]')
            if title_link.count() == 0:
                continue

            subtitle = list_item.locator('p[data-testid="search-listing-subtitle"]')
            short_description = (
                subtitle.inner_text() if subtitle.count() > 0 else ""
            )

            href = title_link.get_attribute("href")
            listing_url = (
                href
                if href and href.startswith("http")
                else f"https://www.autotrader.co.uk{href}"
            )
            source_id = extract_source_id(listing_url)
            listing_candidates.append(
                (listing_id, listing_url, source_id, short_description)
            )

        # autotrader clamps an out-of-range page back to the last valid one, so
        # an identical card set to the previous page means there are no more
        if page_listing_ids and page_listing_ids == previous_page_listing_ids:
            print(
                f"Results page {page_number} repeats the previous page; "
                f"reached the end of the result set"
            )
            break
        previous_page_listing_ids = page_listing_ids

        print(
            f"\nResults page {page_number}: {len(listing_candidates)} listings"
        )

        # process every new listing on this page from the snapshot
        for listing_id, listing_url, source_id, short_description in (
            listing_candidates
        ):
            if listing_id in processed_ids:
                continue

            # record the current page before each listing so a proxy rotation
            # resumes on this page; already-saved listings are skipped on resume
            # because existing source ids are reloaded from the database
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
                print(
                    f"Found existing listing (source_id: {source_id}), skipping..."
                )
                processed_ids.add(listing_id)
                continue

            # a new listing: announce it so the on-screen single detail view
            # matches the log and is not mistaken for a stuck results page
            print(
                f"\nProcessing new listing (source_id: {source_id}): "
                f"{short_description[:60]}\n  {listing_url}"
            )

            # process each listing inside a guard so one bad listing cannot
            # abort the whole sweep; every candidate url comes from the snapshot
            # so a failure just moves on to the next without re-reading the page
            try:
                # navigate to the detail page via href, pausing for any captcha
                # that appears in place of the detail page
                response = goto_with_captcha_handling(page, listing_url)
                page.wait_for_load_state("domcontentloaded")
                if is_captcha_present(page):
                    wait_for_captcha_solve(page)
                pause()

                if is_http_not_found(response) or is_listing_no_longer_available(
                    page
                ):
                    reason = (
                        "404"
                        if is_http_not_found(response)
                        else "unavailable"
                    )
                    print(
                        f"  NotAvailable ({reason}), saving stub and skipping"
                    )
                    hash_code = generate_hash_code(
                        f"{short_description}|{source_id}"
                        if source_id
                        else short_description
                    )
                    h1_elem = page.locator("h1").first
                    make_and_model = (
                        h1_elem.inner_text()
                        if h1_elem.count() > 0
                        else short_description
                    )
                    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
                    engine = create_engine_with_retry(database_url)
                    SessionLocal = sessionmaker(bind=engine)
                    with SessionLocal() as session:
                        persist_unavailable_listing_stub(
                            session,
                            listing_source=ListingSource.AUTOTRADER,
                            listing_type=listing_type,
                            hash_code=hash_code,
                            source_id=source_id,
                            url=listing_url,
                            make_and_model=make_and_model,
                            short_description=short_description,
                        )
                    if source_id is not None:
                        existing_source_ids.add(source_id)
                    processed_ids.add(listing_id)
                    continue

                prospect_listing = read_full_prospect_listing(
                    page,
                    short_description,
                    listing_type,
                    ai_prompt_filename,
                )
                if prospect_listing is None:
                    print("skipping...")
                    processed_ids.add(listing_id)
                    continue

                prospect_listings.append(prospect_listing)
                processed_ids.add(listing_id)
                if source_id is not None:
                    existing_source_ids.add(source_id)

                # after each newly saved listing, run a randomized availability sweep
                update_new_listings_availability(
                    page,
                    listing_source=ListingSource.AUTOTRADER,
                    is_unavailable_fn=is_listing_no_longer_available,
                    limit=random.randint(5, 20),
                    config=CONFIG,
                    deadline=deadline,
                    listing_type=listing_type,
                )

                pause(2.0, 10.0)
            except CaptchaSolveError:
                # an unsolved challenge will block the rest of the sweep on
                # this exit ip too, so bubble up for a proxy rotation
                raise
            except Exception as e:
                print(f"  Error processing listing, skipping: {e}")

        # advance to the next results page
        page_number += 1
        if resume is not None:
            resume.page_number = page_number
            resume.processed_ids = processed_ids
        pause(min_seconds=1.0, max_seconds=2.0)

    print(f"\nFinished after walking {page_number} results page(s)")
    print(f"Processed {len(processed_ids)} listings")
    print(f"Found {len(prospect_listings)} new listings")


# OPEN SESSION
def _open_session(playwright) -> tuple:
    """
    Launch a fresh proxied Chromium session. Each launch advances to the next
    Decodo port via round-robin, so calling this again after a dead sticky
    session yields a new exit IP. Returns the browser and its page.
    """

    browser = launch_stealth_chromium(playwright, headless=is_headless, use_proxy=True)
    page = new_stealth_page(browser)

    # allow long per-listing actions but keep navigation short for proxy rotation
    page.set_default_timeout(LISTING_ACTION_TIMEOUT_MS)
    page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

    return browser, page


# RUN AUTOTRADER
def run_autotrader(config: AutotraderScrapeConfig) -> None:
    """
    Scrape new Autotrader search listings for the configured listing type inside
    a proxy-rotation loop. When a Decodo sticky session expires mid-run
    (surfacing as navigation timeouts) the browser is relaunched on a fresh
    proxy port and the sweep resumes at the same scroll position where it was
    interrupted; already-saved listings are skipped because scrape_listings
    reloads the existing source ids from the database on each pass.
    """

    def setup(page: Page, resume: ScrapeResumeState) -> None:
        goto_with_captcha_handling(page, config.landing_url)
        print(f"Navigated to {config.landing_url}")
        pause()
        dismiss_cookie_consent(page, wait_for_banner=True)
        pause()
        resume.search_url = apply_search_filters(
            page,
            select_classic_cars=config.select_classic_cars,
        )

    def scrape(page: Page, deadline: float, resume: ScrapeResumeState) -> None:
        scrape_listings(
            page,
            resume.search_url,
            config.listing_type,
            config.ai_prompt_filename,
            deadline,
            resume,
        )

    run_with_proxy_rotation(
        config=CONFIG,
        open_session=_open_session,
        setup=setup,
        scrape=scrape,
    )
