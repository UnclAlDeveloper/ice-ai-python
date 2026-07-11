import os
import random
import re
import shutil
import time
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from runtime_flags import resolve_runtime_flags

is_headless = resolve_runtime_flags()

from environments import load_environment

load_environment()

from stealth_browser import (
    CaptchaSolveError,
    Page,
    goto_with_captcha_handling,
    launch_stealth_chromium,
    new_stealth_page,
)
from sqlalchemy.orm import sessionmaker

from ai_analysis import process_ai_analysis_for_listing
from common import (
    create_engine_with_retry,
    generate_hash_code,
    get_existing_source_ids,
    is_http_not_found,
    is_playwright_timeout,
    pause,
    run_with_timeout_backoff,
    wait_for_selector_with_backoff,
    with_db_retry,
)
from listing_images import (
    delete_listing,
    download_and_save_listing_images,
)
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

UNAVAILABLE_ADVERT_TEXTS = (
    "This advert has now been removed through sale or otherwise",
)

CONFIG = ProxyRotationConfig.from_env_prefix("CAR_AND_CLASSIC")

# title or h1 patterns that indicate a listing is sold or under offer
TITLE_SOLD_OR_UNDER_OFFER = re.compile(
    r"(?i)\bunder\s+offer\b|\b(?:already\s+)?sold\b"
)

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
    if details.count() == 0:
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
    if desc_article.count() == 0:
        return False

    first_para = desc_article.locator("p").first
    if first_para.count() == 0:
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
        if page.get_by_text(text, exact=False).count() > 0:
            return True

    # fall back to scanning the rendered html in case the banner markup changes
    try:
        content = page.content()
        if any(text in content for text in UNAVAILABLE_ADVERT_TEXTS):
            return True
    except Exception:
        pass

    h1 = page.locator("section h1").first
    if h1.count() > 0:
        h1_text = h1.text_content() or ""
        if is_title_sold_or_under_offer(h1_text):
            return True

    if _has_description_sold_phrase(page):
        return True

    # a For Sale classified with no asking price is no longer purchasable
    if (
        price_header.count() == 0
        and _get_advert_type(page) == "For Sale"
    ):
        return True

    return False


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
    if banner.count() > 0:
        try:
            if banner.first.is_visible():
                return True
        except Exception:
            pass

    accept_button = page.locator("#onetrust-accept-btn-handler")
    if accept_button.count() > 0:
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

    appear_deadline = time.time() + (
        min(timeout / 1000, _CONSENT_APPEAR_TIMEOUT_S) if wait_for_banner else 0.0
    )
    dismiss_deadline = time.time() + (
        timeout / 1000 if wait_for_banner else 5.0
    )
    click_timeout = min(timeout, 5000.0)

    while time.time() < dismiss_deadline:
        if wait_for_banner or _is_cookie_consent_visible(page):
            if _click_accept_cookies(page, timeout=click_timeout):
                clear_deadline = time.time() + _CONSENT_CLEAR_TIMEOUT_S
                while time.time() < clear_deadline:
                    if not _is_cookie_consent_visible(page):
                        return
                    time.sleep(0.25)
                return

            time.sleep(0.25)
            continue

        if wait_for_banner and time.time() < appear_deadline:
            try:
                page.locator("#onetrust-accept-btn-handler").wait_for(
                    state="attached", timeout=1000
                )
            except Exception:
                pass
            time.sleep(0.25)
            continue

        return


# POLL COOKIE CONSENT
def _poll_cookie_consent(page: Page) -> None:
    """
    Check once for a OneTrust consent banner and dismiss it when present.
    """

    if _is_cookie_consent_visible(page):
        accept_cookies(page)


# PAUSE DELAY SECONDS
def _pause_delay_seconds(min_seconds: float, max_seconds: float) -> float:
    """Return a human-like delay using the same gamma distribution as pause()."""

    if max_seconds <= min_seconds:
        return max(min_seconds, 0.0)

    scale = (max_seconds - min_seconds) / 1.8
    shape = ((min_seconds * 0.9) / scale) + 1.0
    return min_seconds * 0.1 + random.gammavariate(shape, scale)


# PAUSE FOR PAGE
def pause_for_page(
    page: Page, min_seconds: float = 1.0, max_seconds: float = 3.0
) -> None:
    """
    Wait like pause(), but poll for a late OneTrust cookie banner during longer
    delays so it can be dismissed before it blocks interactions.
    """

    delay = _pause_delay_seconds(min_seconds, max_seconds)
    deadline = time.time() + delay

    while time.time() < deadline:
        _poll_cookie_consent(page)
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(_CONSENT_POLL_INTERVAL_S, remaining))


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

    return page.locator(_NEWEST_LISTED_SORT_APPLIED_SELECTOR).count() > 0


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

    parsed = urlparse(url)
    pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in ("sort", "source", "page")
    ]
    pairs.append(("sort", "latest"))
    pairs.append(("source", "modal-sort"))

    return urlunparse(parsed._replace(query=urlencode(pairs)))


# EXTRACT SOURCE ID
def extract_source_id(url: str | None) -> str | None:
    """
    Extract the Car & Classic listing reference (e.g. 'C2085940') from a
    listing URL such as 'https://www.carandclassic.com/l/C2085940'. Returns
    None when the URL does not contain a /l/ reference segment.
    """

    match = re.search(r"/l/([^/?#]+)", url or "")
    return match.group(1) if match else None


# PARSE MILEAGE
def parse_mileage(raw: str) -> tuple[int | None, str]:
    """
    Split a mileage string like '138,100 Miles' into the numeric value
    as an integer and the unit string.
    """

    match = re.match(r"^([\d,]+)\s+(.+)$", raw.strip())
    if match:
        return int(match.group(1).replace(",", "")), match.group(2)
    return None, ""


# DISMISS INERTIA ERROR DIALOG
def dismiss_inertia_error_dialog(page: Page) -> bool:
    """
    Remove the Inertia.js error overlay dialog if one is open. The overlay
    renders a full-page iframe that intercepts pointer events, so any
    subsequent click on the underlying page silently times out until it is
    cleared. Returns True if a dialog was dismissed.
    """

    dialog = page.locator("dialog#inertia-error-dialog")
    if dialog.count() == 0:
        return False

    # the dialog exposes no visible close control, so detach it from the dom
    page.evaluate(
        "document.getElementById('inertia-error-dialog')?.remove()"
    )
    return True


# EXTRACT GALLERY IMAGES
def extract_gallery_images(page: Page) -> list[str]:
    """
    Collect image URLs from the Gallery section. If the last button contains a
    camera icon, click it to open the full gallery popup and collect URLs from
    there. Otherwise all images are already visible on the detail page, so
    collect URLs directly from the gallery section.
    """

    image_urls = []

    # clear any overlays that would intercept the gallery click
    _poll_cookie_consent(page)
    dismiss_inertia_error_dialog(page)

    gallery_section = page.locator('section:has(h2:text("Gallery"))')
    if gallery_section.count() == 0:
        return image_urls

    gallery_buttons = gallery_section.locator("button")
    if gallery_buttons.count() == 0:
        return image_urls

    last_button = gallery_buttons.last
    has_camera_icon = last_button.locator('svg[data-icon="camera"]').count() > 0

    if has_camera_icon:
        # click the last button to open the full gallery popup
        last_button.click()
        pause(0.5, 1)

        # collect all image src urls from the popup panel
        popup_images = page.locator("#panel_sheet_images img")
        for i in range(popup_images.count()):
            src = popup_images.nth(i).get_attribute("src")
            if src and src.startswith("http") and src not in image_urls:
                image_urls.append(src)

        # close the gallery popup
        close_button = page.locator('button:has(svg[data-icon="close"])').first
        if close_button.is_visible():
            close_button.click()
            pause(0.5, 1)
    else:
        # all images are visible on the detail page already
        section_images = gallery_section.locator("img")
        for i in range(section_images.count()):
            src = section_images.nth(i).get_attribute("src")
            if src and src.startswith("http") and src not in image_urls:
                image_urls.append(src)

    return image_urls


# EXTRACT LISTING DETAILS
def extract_listing_details(
    page: Page, hash_code: str, source_id: str | None
) -> tuple[ProspectListings, list[str]]:
    """
    Extract prospect listing fields from an individual listing detail page
    and return a populated ProspectListings instance along with gallery image URLs.
    """

    url = page.url
    current_datetime = datetime.now()

    # extract make_and_model from the h1
    h1_text = page.locator("section h1").first.text_content().strip()
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
    for i in range(items.count()):
        li = items.nth(i)
        icon = li.locator("svg[data-icon]")

        if icon.count() > 0:
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
            if img.count() > 0:
                location = li.text_content().strip()
                location = re.sub(r",?\s*United Kingdom$", "", location)

    # extract asking price and currency symbol from the price header
    asking_price = None
    currency_symbol = None
    price_header = page.locator('header:has(span:text("Asking price")) h2')
    if price_header.count() > 0:
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
    if desc_article.count() > 0:
        full_text = desc_article.locator("p").first.inner_text().strip()
        full_description = full_text
        short_description = full_text.split("\n")[0].strip()

    # extract vehicle background history check summary
    basic_history_check = None
    bg_section = page.locator('section:has(h2:text("Vehicle background"))')
    if bg_section.count() > 0:
        answers = bg_section.locator("div > p:last-child")
        no_count = 0
        yes_count = 0
        for i in range(answers.count()):
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
        short_description=short_description or h1_text,
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
    for i in range(articles.count()):
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
    Walk every page of Car & Classic search results and save new listings.
    Each results page is loaded via the page= query parameter on the canonical
    search url (including sort=latest), listing cards are snapshotted, then each
    candidate is visited directly without returning to the grid between items.
    When deadline (a time.monotonic value) is given, raises ProxySessionExpired
    between listings once it is reached so the proxy can be rotated without
    interrupting a partially-downloaded listing. When resume carries a
    search_url from an interrupted run, continues from the saved results page
    number rather than starting over from page one.
    """

    existing_source_ids = get_existing_source_ids(ListingSource.CAR_AND_CLASSIC)
    print(f"Loaded {len(existing_source_ids)} existing source ids from database")

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
        print(
            f"Resuming listings scrape at page {page_number} "
            f"({len(processed_ids)} listings already processed)"
        )

    print("Paginating to load all Car & Classic listings...\n")

    while True:
        page_url = with_page_param(search_url, page_number)
        goto_with_captcha_handling(page, page_url)
        accept_cookies(page, wait_for_banner=True, timeout=8000)
        pause_for_page(page)

        if not search_results_present(page, _SEARCH_RESULTS_CARD_SELECTOR):
            print(
                f"No listings on results page {page_number}; reached the end "
                f"of the result set"
            )
            break

        if use_new_section is None:
            new_vehicles_grid = page.locator(
                "div.lg\\:grid-cols-3.grid.grid-cols-1"
            )
            use_new_section = new_vehicles_grid.count() > 0
            if use_new_section:
                print("New vehicles section detected — processing only new listings")

        listing_candidates = _snapshot_listing_candidates(
            page, use_new_section=use_new_section
        )
        page_listing_ids = {card_id for card_id, _, _, _ in listing_candidates}

        if page_listing_ids and page_listing_ids == previous_page_listing_ids:
            print(
                f"Results page {page_number} repeats the previous page; "
                f"reached the end of the result set"
            )
            break
        previous_page_listing_ids = page_listing_ids

        print(
            f"\n--- Page {page_number} ({len(listing_candidates)} listings) ---"
        )

        for index, (card_id, listing_url, source_id, title) in enumerate(
            listing_candidates, start=1
        ):
            if card_id in processed_ids:
                print(
                    f"  [{index}] {title} — already processed this run, skipping"
                )
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
                print(f"  [{index}] {title} — already exists, skipping")
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
                print(
                    f"  [{index}] {title} — sold/under offer (card title), "
                    f"skipping"
                )
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
            try:
                # navigate to the listing via its href instead of clicking the
                # card, because the anchor is overlaid by a sibling that
                # intercepts pointer events; goto_with_captcha_handling also
                # pauses for any captcha shown in place of the detail page
                response = goto_with_captcha_handling(page, listing_url)
                accept_cookies(page, wait_for_banner=True, timeout=8000)
                wait_for_listing_detail_page(page)
                pause_for_page(page)

                if is_http_not_found(response) or is_listing_no_longer_available(
                    page
                ):
                    reason = (
                        "404"
                        if is_http_not_found(response)
                        else "unavailable"
                    )
                    print(
                        f"  [{index}] {title} — NotAvailable ({reason}), "
                        f"skipping"
                    )
                    h1 = page.locator("section h1").first
                    make_and_model = (
                        h1.text_content().strip()
                        if h1.count() > 0
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
                    page, hash_code, source_id
                )
                prospect_listing.status_checked_at = datetime.now()
                new_count += 1
                print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                print(f"  [{index}] {title}")
                print(f"      make_and_model: {prospect_listing.make_and_model}")
                print(f"      year: {prospect_listing.year}")
                print(f"      location: {prospect_listing.location}")
                print(f"      images: {len(image_urls)}")

                # save to database and download images
                with SessionLocal() as session:
                    # retry the initial persist so a transient db connection
                    # failure (e.g. a momentary dns hiccup) does not drop the
                    # listing; nothing is committed until this block succeeds
                    def persist_listing():
                        session.add(prospect_listing)
                        session.flush()
                        session.commit()
                        session.refresh(prospect_listing)

                    with_db_retry(
                        persist_listing, description="persist prospect listing"
                    )

                    temp_image_dir = download_and_save_listing_images(
                        image_urls,
                        page,
                        prospect_listing,
                        session,
                        temp_dir_prefix="car_and_classic_images_",
                    )

                    # count the images that actually persisted so a partial
                    # save (e.g. a proxy that died mid-download) can be detected
                    saved_image_count = (
                        session.query(Images)
                        .filter(
                            Images.listing_id == prospect_listing.id,
                            Images.listing_table == ListingTable.PROSPECT,
                        )
                        .count()
                    )

                    # a shortfall means some images failed to download; discard
                    # the whole listing (row, images and s3 objects) so it is not
                    # kept incomplete and gets re-fetched cleanly on a later pass
                    if image_urls and saved_image_count < len(image_urls):
                        print(
                            f"      Partial save "
                            f"({saved_image_count}/{len(image_urls)} images) — "
                            f"deleting listing {prospect_listing.id}"
                        )
                        if temp_image_dir and os.path.isdir(temp_image_dir):
                            shutil.rmtree(temp_image_dir)
                        delete_listing(prospect_listing, session)
                        new_count -= 1
                        raise RuntimeError(
                            f"partial image download for {listing_url} "
                            f"({saved_image_count}/{len(image_urls)} images saved)"
                        )

                    process_ai_analysis_for_listing(
                        "classic_car_prompt.md",
                        prospect_listing,
                        session,
                        temp_image_dir,
                    )

                    if temp_image_dir and os.path.isdir(temp_image_dir):
                        shutil.rmtree(temp_image_dir)

                if source_id is not None:
                    existing_source_ids.add(source_id)
                processed_ids.add(card_id)

                # after each newly saved listing, run a tiny randomized availability sweep
                update_new_listings_availability(
                    page,
                    listing_source=ListingSource.CAR_AND_CLASSIC,
                    is_unavailable_fn=is_listing_no_longer_available,
                    limit=random.randint(0, 2),
                    config=CONFIG,
                    deadline=deadline,
                )
            except CaptchaSolveError:
                # an unsolved challenge will block the rest of the sweep on
                # this exit ip too, so bubble up for a proxy rotation
                raise
            except Exception as e:
                # a removed advert has no section h1; if a wait timed out but
                # the unavailable banner is present, save a stub rather than
                # burning the full backoff budget and skipping without a record
                if is_playwright_timeout(e) and is_listing_no_longer_available(
                    page
                ):
                    print(
                        f"  [{index}] {title} — NotAvailable (unavailable), "
                        f"skipping"
                    )
                    h1 = page.locator("section h1").first
                    make_and_model = (
                        h1.text_content().strip()
                        if h1.count() > 0
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

                print(f"  [{index}] {title} — error, skipping: {e}")

        # new-vehicles section has no pagination, so stop after one pass
        if use_new_section:
            break

        page_number += 1
        if resume is not None:
            resume.page_number = page_number
            resume.processed_ids = processed_ids
        pause_for_page(page, min_seconds=1.0, max_seconds=2.0)

    print(f"\nFinished — {page_number} page(s) scraped, {new_count} new listing(s).")


# OPEN SESSION
def _open_session(playwright) -> tuple:
    """
    Launch a fresh proxied Chromium session on the Car & Classic search page and
    accept cookies. Each launch advances to the next Decodo port via
    round-robin, so calling this again after a dead sticky session yields a new
    exit IP. Returns the browser and its page.
    """

    browser = launch_stealth_chromium(playwright, headless=is_headless, use_proxy=True)
    page = new_stealth_page(browser)

    # land on the search page, solving any captcha that interrupts the load
    goto_with_captcha_handling(page, "https://www.carandclassic.com/search")

    pause_for_page(page)
    accept_cookies(page, wait_for_banner=True)

    return browser, page


# APPLY SEARCH FILTERS
def _apply_search_filters(page: Page) -> str:
    """
    Open the All filters overlay on the search page, select Cars, United
    Kingdom, Advert and Private seller type, apply the filters, then sort the
    results by Newest listed so the grid is ready to scrape. Returns the
    canonical page-one search url including sort=latest.
    """

    _dismiss_blocking_overlays(page, cookie_timeout=15000)
    pause_for_page(page)

    # open the all filters overlay
    pause_for_page(page)
    page.locator('button:has(span:text-is("All filters"))').first.click()
    wait_for_selector_with_backoff(
        page,
        'section:has(h2:text-is("Category"))',
        state="attached",
        description="all filters overlay",
    )

    # select category, country, listing type and seller type
    pause_for_page(page)
    page.locator(
        'section:has(h2:text-is("Category")) button:has(span:text-is("Cars"))'
    ).click()

    pause_for_page(page)
    page.locator(
        'section:has(h2:text-is("Country")) '
        'button:has(span:text-is("United Kingdom"))'
    ).click()

    pause_for_page(page)
    page.locator(
        'section:has(h2:text-is("Listing type")) '
        'button:has(span:text-is("Advert"))'
    ).click()

    pause_for_page(page)
    page.locator(
        'section:has(h2:text-is("Seller type")) '
        'button:has(span:text-is("Private"))'
    ).click()

    # apply the selected filters and wait for the results grid
    pause_for_page(page)
    page.locator('button:has-text("Show"):has-text("results")').click()
    wait_for_selector_with_backoff(
        page,
        '[data-testid="card-listing"]',
        state="attached",
        description="search results grid",
    )

    _apply_newest_listed_sort(page)

    return _canonical_newest_search_url(page.url)


# CAR AND CLASSIC
def car_and_classic():
    """
    Scrape new Car & Classic search listings inside a proxy-rotation loop.
    When a Decodo sticky session expires mid-run (surfacing as navigation
    timeouts) the browser is relaunched on a fresh proxy port and the sweep
    resumes on the same search-results page (page= query param) where it was
    interrupted; already-saved listings are skipped because scrape_listings
    reloads the existing source ids from the database on each pass.
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
