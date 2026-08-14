import os
import random
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import or_
from sqlalchemy.orm import Session, sessionmaker

from ai_analysis import process_ai_analysis_for_listing
from common import (
    create_engine_with_retry,
    is_http_not_found,
    is_not_found_error,
    is_playwright_timeout,
    is_transient_db_error,
    pause,
    with_db_retry,
)
from listing_images import (
    cap_gallery_urls,
    delete_listing,
    delete_listing_images,
    download_and_save_listing_images,
)
from models.auto_ads import Images, ProspectListings
from models.enums import ListingSource, ListingTable, ListingType, ProspectListingStatus
from stealth_browser import (
    Browser,
    CaptchaSolveError,
    Page,
    PageUnresponsiveError,
    check_proxy_health,
    close_browser_quietly,
    configure_consecutive_captcha_rotation,
    ensure_headed_window_visible,
    goto_with_captcha_handling,
    is_navigation_timeout,
    is_proxy_network_error,
    is_target_closed_error,
    launch_stealth_chromium,
    new_stealth_page,
    reset_consecutive_captcha_count,
    sync_stealth_playwright,
)


# additional Chromium network error codes (beyond the proxy-specific set in
# stealth_browser) that mean the listing page could not be fetched at all, so
# its availability is unknown and must never be inferred from the failure
SITE_UNREACHABLE_ERROR_FRAGMENTS = (
    "ERR_NAME_NOT_RESOLVED",
    "ERR_NAME_RESOLUTION_FAILED",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_ADDRESS_UNREACHABLE",
    "ERR_CONNECTION_REFUSED",
    "ERR_NETWORK_CHANGED",
    "ERR_SOCKET_NOT_CONNECTED",
)


_PROXY_ROTATION_MAX_MINUTES = 55.0


# PROXY ROTATION CONFIG
@dataclass
class ProxyRotationConfig:
    """
    Tunable limits for proxy rotation, availability reload back-off, and
    transient database failure handling. Values are read from environment
    variables prefixed by the scraper's site name (e.g. AUTOTRADER_).
    """

    max_proxy_rotations: int
    proxy_rotation_interval_mean_minutes: float
    proxy_rotation_interval_stddev_minutes: float
    max_consecutive_setup_failures: int
    max_consecutive_db_failures: int
    max_consecutive_captchas_before_rotation: int
    availability_max_load_retries: int
    availability_load_backoff_seconds: float

    # PROXY ROTATION CONFIG FROM ENV PREFIX
    @classmethod
    def from_env_prefix(
        cls, prefix: str, **defaults: int | float
    ) -> "ProxyRotationConfig":
        """
        Build a ProxyRotationConfig from environment variables whose names start
        with the given prefix (e.g. AUTOTRADER_MAX_PROXY_ROTATIONS). Optional
        keyword defaults apply when the corresponding env var is unset.
        """

        return cls(
            max_proxy_rotations=int(os.getenv(f"{prefix}_MAX_PROXY_ROTATIONS", "20")),
            proxy_rotation_interval_mean_minutes=float(
                os.getenv(
                    f"{prefix}_PROXY_ROTATION_MEAN_MINUTES",
                    os.getenv(f"{prefix}_PROXY_ROTATION_MINUTES", "35"),
                )
            ),
            proxy_rotation_interval_stddev_minutes=float(
                os.getenv(f"{prefix}_PROXY_ROTATION_STDDEV_MINUTES", "7.5")
            ),
            max_consecutive_setup_failures=int(
                os.getenv(f"{prefix}_MAX_SETUP_FAILURES", "3")
            ),
            max_consecutive_db_failures=int(
                os.getenv(f"{prefix}_MAX_DB_FAILURES", "10")
            ),
            max_consecutive_captchas_before_rotation=int(
                os.getenv(
                    f"{prefix}_MAX_CONSECUTIVE_CAPTCHAS",
                    str(
                        defaults.get(
                            "max_consecutive_captchas_before_rotation", 0
                        )
                    ),
                )
            ),
            availability_max_load_retries=int(
                os.getenv(f"{prefix}_AVAILABILITY_LOAD_RETRIES", "4")
            ),
            availability_load_backoff_seconds=float(
                os.getenv(f"{prefix}_AVAILABILITY_LOAD_BACKOFF", "5")
            ),
        )

    # SAMPLE PROXY ROTATION INTERVAL MINUTES
    def sample_proxy_rotation_interval_minutes(
        self, *, min_minutes: float = 1.0
    ) -> float:
        """
        Draw a proxy rotation interval from a normal distribution using the
        configured mean and standard deviation, clamped to a positive minimum
        and a hard maximum of 55 minutes.
        """

        interval_minutes = random.gauss(
            self.proxy_rotation_interval_mean_minutes,
            self.proxy_rotation_interval_stddev_minutes,
        )
        return min(
            _PROXY_ROTATION_MAX_MINUTES,
            max(min_minutes, interval_minutes),
        )


# SCRAPE RESUME STATE
@dataclass
class ScrapeResumeState:
    """
    Bookkeeping for where a listings scrape was interrupted so a proxy rotation
    can resume on the same search-results page and skip listings already
    processed in the current pass.
    """

    search_url: str | None = None
    page_number: int = 1
    processed_ids: set[str] = field(default_factory=set)


# LOG TIMESTAMP
def log_timestamp() -> None:
    """Print the current local date and time on its own line."""

    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


# LOG LOADED SOURCE IDS
def log_loaded_source_ids(count: int) -> None:
    """Print how many listing source ids were loaded from the database."""

    print(f"Loaded {count} existing source ids from database")


# LOG RESUME SCRAPE
def log_resume_scrape(page_number: int, processed_count: int) -> None:
    """Print where a listings scrape will resume after proxy rotation."""

    print(
        f"Resuming listings scrape at page {page_number} "
        f"({processed_count} listings already processed)"
    )


# LOG PAGINATING START
def log_paginating_start(source_label: str) -> None:
    """Print the start of a paginated listings sweep."""

    print(f"Paginating to load all {source_label} listings...\n")


# LOG NO LISTINGS ON PAGE
def log_no_listings_on_page(page_number: int) -> None:
    """Print that a results page is empty and pagination should stop."""

    print(
        f"No listings on results page {page_number}; reached the end "
        f"of the result set"
    )


# LOG REPEAT PAGE
def log_repeat_page(page_number: int) -> None:
    """Print that a results page duplicates the previous page."""

    print(
        f"Results page {page_number} repeats the previous page; "
        f"reached the end of the result set"
    )


# LOG RESULTS PAGE
def log_results_page(page_number: int, listing_count: int) -> None:
    """Print a results-page header with its listing count."""

    print(f"\n--- Page {page_number} ({listing_count} listings) ---")


# LOG ALREADY PROCESSED
def log_already_processed(index: int, title: str) -> None:
    """Print that a listing card was already handled in this run."""

    print(f"  [{index}] {title} — already processed this run, skipping")


# LOG ALREADY EXISTS
def log_already_exists(index: int, title: str) -> None:
    """Print that a listing is already stored in the database."""

    print(f"  [{index}] {title} — already exists, skipping")


# LOG PROCESSING NEW LISTING
def log_processing_new_listing(
    index: int,
    title: str,
    listing_url: str,
    source_id: str | None = None,
) -> None:
    """
    Announce a new listing before navigating to its detail page so the log
    matches the browser and is not mistaken for a stuck results page.
    """

    log_timestamp()
    if source_id is not None:
        print(
            f"  [{index}] Processing new listing (source_id: {source_id}): "
            f"{title}"
        )
    else:
        print(f"  [{index}] Processing new listing: {title}")
    print(f"      {listing_url}")


# LOG NOT AVAILABLE
def log_not_available(index: int, title: str, reason: str) -> None:
    """Print that a listing is no longer available and will be skipped."""

    print(f"  [{index}] {title} — NotAvailable ({reason}), skipping")


# LOG SOLD UNDER OFFER CARD
def log_sold_under_offer_card(index: int, title: str) -> None:
    """Print that a search-card title indicates sold or under offer."""

    print(f"  [{index}] {title} — sold/under offer (card title), skipping")


# LOG SAVED LISTING
def log_saved_listing(
    index: int,
    title: str,
    *,
    make_and_model: str | None = None,
    year: int | None = None,
    location: str | None = None,
    image_count: int | None = None,
) -> None:
    """
    Print a multi-line summary of extracted listing details. Call this after
    fields (and gallery url count) are known, and before image download / AI
    analysis, so long-running work is attributed to a visible listing.
    """

    print(f"  [{index}] {title}")
    if make_and_model is not None:
        print(f"      make_and_model: {make_and_model}")
    if year is not None:
        print(f"      year: {year}")
    if location is not None:
        print(f"      location: {location}")
    if image_count is not None:
        print(f"      images: {image_count}")


# LOG PARTIAL SAVE
def log_partial_save(
    index: int, saved_count: int, total_count: int, listing_id: int
) -> None:
    """Print that a listing save was incomplete and is being rolled back."""

    print(
        f"  [{index}] Partial save "
        f"({saved_count}/{total_count} images) — "
        f"deleting listing {listing_id}"
    )


# LOG LISTING ERROR
def log_listing_error(index: int, title: str, error: Exception | str) -> None:
    """Print that a single listing failed and will be skipped."""

    print(f"  [{index}] {title} — error, skipping: {error}")


# LOG SKIPPING
def log_skipping(index: int, title: str, reason: str) -> None:
    """Print a generic skip reason for a listing card."""

    print(f"  [{index}] {title} — {reason}")


# LOG SCRAPE FINISHED
def log_scrape_finished(
    page_count: int, new_count: int, processed_count: int | None = None
) -> None:
    """Print the end-of-run summary for a listings scrape."""

    print(
        f"\nFinished — {page_count} page(s) scraped, "
        f"{new_count} new listing(s)."
    )
    if processed_count is not None:
        print(f"Processed {processed_count} listings")


# WITH PAGE PARAM
def with_page_param(url: str, page_number: int) -> str:
    """
    Return a search-results url with its page query parameter set to
    page_number. Page one omits the parameter so the canonical first-page url
    (including sort and filter query params) is preserved for later pagination
    and proxy-resume navigations.
    """

    return replace_query_params(
        url,
        drop=("page",),
        set_params={"page": str(page_number)} if page_number > 1 else None,
    )


# REPLACE QUERY PARAMS
def replace_query_params(
    url: str,
    *,
    drop: tuple[str, ...] | set[str] = (),
    set_params: dict[str, str] | None = None,
    prefer_keys: list[str] | None = None,
) -> str:
    """
    Return url with selected query keys removed and optional replacements set.
    When prefer_keys is given those keys are emitted first in that order.
    """

    drop_keys = set(drop)
    parsed = urlparse(url)
    existing = {
        key: value
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in drop_keys
    }
    if set_params:
        existing.update(set_params)

    if prefer_keys:
        final_pairs = [
            (key, existing[key]) for key in prefer_keys if key in existing
        ]
        for key, value in existing.items():
            if key not in prefer_keys:
                final_pairs.append((key, value))
    else:
        final_pairs = list(existing.items())

    return urlunparse(parsed._replace(query=urlencode(final_pairs)))


# SEARCH RESULTS PRESENT
def search_results_present(
    page: Page, listing_selector: str, *, timeout_ms: int = 15000
) -> bool:
    """
    Return True when the current search-results page renders at least one
    listing card matching listing_selector within the timeout, and False when
    none appear. Paging past the final results page yields a page with no
    cards, so the caller uses a False return to detect the end of the result
    set and stop paginating.
    """

    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass

    try:
        page.locator(listing_selector).first.wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except Exception:
        return False


# PROXY SESSION EXPIRED
class ProxySessionExpired(Exception):
    """
    Raised between listings once the proxy rotation interval has elapsed, so the
    caller can relaunch on a fresh Decodo port without ever interrupting an
    in-progress listing download.
    """


# IS SITE UNREACHABLE ERROR
def is_site_unreachable_error(exc: BaseException) -> bool:
    """
    Return True when an exception means the listing page could not be fetched at
    all: a proxy transport failure, a navigation timeout, a DNS failure or any
    "site can't be reached" network error. A listing's availability can never be
    inferred from these, so the caller retries with back-off and ultimately
    raises rather than marking the listing NotAvailable.
    """

    if is_proxy_network_error(exc) or is_navigation_timeout(exc):
        return True

    message = str(exc)
    return any(
        fragment in message for fragment in SITE_UNREACHABLE_ERROR_FRAGMENTS
    )


# LOAD LISTING WITH BACKOFF
def load_listing_with_backoff(
    page: Page,
    url: str,
    *,
    max_retries: int,
    backoff_seconds: float,
):
    """
    Navigate to a listing url for an availability check, retrying transient
    "site can't be reached" network/proxy failures with exponential back-off.
    Returns the navigation response on success. A genuine 404 is raised
    immediately because the advert really is gone, and a network failure that
    survives every retry is re-raised so the caller can rotate the proxy or
    abort rather than guess the listing's availability.
    """

    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            return goto_with_captcha_handling(page, url)
        except CaptchaSolveError:
            raise
        except Exception as e:
            # a genuine 404 means the advert is gone, so stop retrying
            if is_not_found_error(e):
                raise

            # only transport-level failures are worth retrying; any other error
            # is a real page/parse problem the caller should handle directly
            if not is_site_unreachable_error(e) and not is_playwright_timeout(e):
                raise

            last_error = e
            if attempt < max_retries:
                # exponential back-off gives a flapping proxy or connection time
                # to recover before the next attempt
                delay = backoff_seconds * (2 ** (attempt - 1))
                failure = (
                    "Page probe timed out"
                    if is_playwright_timeout(e)
                    else "Site unreachable"
                )
                print(
                    f"  {failure} (attempt {attempt}/"
                    f"{max_retries}): {e}; retrying in "
                    f"{delay:.0f}s..."
                )
                time.sleep(delay)

    # every retry failed: surface the error so availability is never guessed
    raise last_error if last_error is not None else RuntimeError(
        f"Failed to load {url} for availability check"
    )


# PERSIST UNAVAILABLE LISTING STUB
def persist_unavailable_listing_stub(
    session,
    *,
    listing_source: ListingSource,
    listing_type: ListingType,
    hash_code: str,
    source_id: str | None,
    url: str,
    make_and_model: str,
    short_description: str | None = None,
) -> None:
    """
    Save a minimal NotAvailable prospect row so future scrapes skip this
    source_id without downloading images or running AI analysis.
    """

    current_datetime = datetime.now()

    prospect_listing = ProspectListings(
        hash_code=hash_code,
        source_id=source_id,
        listing_source=listing_source,
        listing_type=listing_type,
        status=ProspectListingStatus.NOT_AVAILABLE,
        url=url,
        make_and_model=make_and_model,
        short_description=short_description or make_and_model,
        created_at=current_datetime,
        updated_at=current_datetime,
        status_checked_at=current_datetime,
    )

    def persist():
        session.add(prospect_listing)
        session.commit()

    with_db_retry(persist, description="persist unavailable listing stub")


# RUN CONSENT DISMISS LOOP
def run_consent_dismiss_loop(
    *,
    is_visible: Callable[[], bool],
    click_dismiss: Callable[[], bool],
    wait_attached: Callable[[], None] | None = None,
    wait_for_banner: bool = False,
    timeout: float = 15000,
    appear_timeout_s: float = 8.0,
    clear_timeout_s: float = 5.0,
    no_wait_dismiss_timeout_s: float = 5.0,
    dismiss_timeout_s: float | None = None,
) -> None:
    """
    Shared cookie-consent dismiss loop used by site scrapers. Site-specific
    visibility checks and click handlers are injected so Sourcepoint, OneTrust,
    and Quantcast implementations can share the same appear/clear deadlines.
    """

    appear_deadline = time.time() + (
        min(timeout / 1000, appear_timeout_s) if wait_for_banner else 0.0
    )
    if dismiss_timeout_s is not None:
        dismiss_window_s = dismiss_timeout_s if wait_for_banner else no_wait_dismiss_timeout_s
    else:
        dismiss_window_s = (
            timeout / 1000 if wait_for_banner else no_wait_dismiss_timeout_s
        )
    dismiss_deadline = time.time() + dismiss_window_s

    while time.time() < dismiss_deadline:
        if wait_for_banner or is_visible():
            if click_dismiss():
                clear_deadline = time.time() + clear_timeout_s
                while time.time() < clear_deadline:
                    if not is_visible():
                        return
                    time.sleep(0.25)
                return

            time.sleep(0.25)
            continue

        if wait_for_banner and time.time() < appear_deadline:
            if wait_attached is not None:
                wait_attached()
            time.sleep(0.25)
            continue

        return


# OPEN PROXIED SESSION
def open_proxied_session(
    playwright,
    *,
    headless: bool,
    use_proxy: bool = True,
    landing_url: str | None = None,
    on_ready: Callable[[Page], None] | None = None,
    default_timeout_ms: int | None = None,
    default_navigation_timeout_ms: int | None = None,
) -> tuple[Browser, Page]:
    """
    Launch a fresh Chromium session with optional proxy, timeouts, landing
    navigation, and a site-specific on_ready hook (e.g. accept cookies).
    """

    browser = launch_stealth_chromium(
        playwright, headless=headless, use_proxy=use_proxy
    )
    page = new_stealth_page(browser)

    if default_timeout_ms is not None:
        page.set_default_timeout(default_timeout_ms)
    if default_navigation_timeout_ms is not None:
        page.set_default_navigation_timeout(default_navigation_timeout_ms)

    if landing_url is not None:
        goto_with_captcha_handling(page, landing_url)

    if getattr(browser, "_ice_headed_display", False):
        ensure_headed_window_visible(page)

    if on_ready is not None:
        on_ready(page)

    return browser, page


# PERSIST LISTING WITH IMAGES AND AI
def persist_listing_with_images_and_ai(
    session: Session,
    prospect_listing: ProspectListings,
    *,
    ai_prompt_filename: str,
    listing_url: str,
    image_urls: list[str] | None = None,
    page: Page | None = None,
    temp_dir_prefix: str = "listing_images_",
    page_hook: Callable[[Page], None] | None = None,
    download_images: Callable[
        [Session, ProspectListings], tuple[str | None, int]
    ]
    | None = None,
    before_ai: Callable[[], None] | None = None,
    after_ai: Callable[[], None] | None = None,
    on_after_persist: Callable[[ProspectListings], None] | None = None,
    log_index: int | None = None,
    refresh_listing: bool = True,
    expunge_listing: bool = False,
) -> ProspectListings:
    """
    Commit a prospect listing, download its images, run AI analysis, and clean
    up the temp image directory. Raises PageUnresponsiveError when images were
    expected but only a partial set was saved, after discarding the listing.
    """

    def persist_listing():
        session.add(prospect_listing)
        session.flush()
        session.commit()
        if refresh_listing and not expunge_listing:
            session.refresh(prospect_listing)

    with_db_retry(persist_listing, description="persist prospect listing")

    if on_after_persist is not None:
        on_after_persist(prospect_listing)

    temp_image_dir: str | None = None
    expected_image_count = 0

    if download_images is not None:
        temp_image_dir, expected_image_count = download_images(
            session, prospect_listing
        )
    elif image_urls is not None and page is not None:
        image_urls = cap_gallery_urls(image_urls)
        expected_image_count = len(image_urls)
        temp_image_dir = download_and_save_listing_images(
            image_urls,
            page,
            prospect_listing,
            session,
            temp_dir_prefix=temp_dir_prefix,
            page_hook=page_hook,
        )
    else:
        raise ValueError(
            "persist_listing_with_images_and_ai requires image_urls+page "
            "or download_images"
        )

    saved_image_count = (
        session.query(Images)
        .filter(
            Images.listing_id == prospect_listing.id,
            Images.listing_table == ListingTable.PROSPECT,
        )
        .count()
    )

    if expected_image_count > 0 and saved_image_count < expected_image_count:
        if log_index is not None:
            log_partial_save(
                log_index,
                saved_image_count,
                expected_image_count,
                prospect_listing.id,
            )
        else:
            print(
                f"  Partial save ({saved_image_count}/{expected_image_count} images) — "
                f"deleting listing {prospect_listing.id}"
            )
        if temp_image_dir and os.path.isdir(temp_image_dir):
            shutil.rmtree(temp_image_dir)
        delete_listing(prospect_listing, session)
        raise PageUnresponsiveError(
            f"partial image download for {listing_url} "
            f"({saved_image_count}/{expected_image_count} images saved)"
        )

    if before_ai is not None:
        before_ai()

    prospect_listing = process_ai_analysis_for_listing(
        ai_prompt_filename,
        prospect_listing,
        session,
        temp_image_dir,
    )

    if after_ai is not None:
        after_ai()

    if temp_image_dir and os.path.isdir(temp_image_dir):
        shutil.rmtree(temp_image_dir)

    if refresh_listing:
        session.refresh(prospect_listing)
    if expunge_listing:
        session.expunge(prospect_listing)

    return prospect_listing


# RERAISE OR LOG LISTING ERROR
def reraise_or_log_listing_error(
    index: int,
    title: str,
    listing_url: str,
    error: Exception,
    *,
    promote_timeouts: bool = True,
) -> None:
    """
    Re-raise rotation-worthy listing errors; otherwise log and allow the scrape
    loop to continue with the next candidate. When promote_timeouts is True,
    Playwright timeouts become PageUnresponsiveError so the proxy can rotate.
    """

    if isinstance(
        error, (CaptchaSolveError, PageUnresponsiveError, ProxySessionExpired)
    ):
        raise error
    if is_proxy_network_error(error):
        raise error
    if promote_timeouts and is_playwright_timeout(error):
        raise PageUnresponsiveError(
            f"listing page timed out for {listing_url}"
        ) from error

    log_listing_error(index, title, error)


# MARK LISTING PROCESSED AND CHECK AVAILABILITY
def mark_listing_processed_and_check_availability(
    page: Page,
    *,
    source_id: str | None,
    card_id: str,
    existing_source_ids: set[str],
    processed_ids: set[str],
    listing_source: ListingSource,
    listing_type: ListingType,
    is_unavailable_fn: Callable[[Page], bool],
    config: "ProxyRotationConfig",
    deadline: float | None,
) -> None:
    """
    Record a successfully saved listing as processed and run a randomized
    availability sweep of other NEW listings from the same source.
    """

    log_timestamp()

    if source_id is not None:
        existing_source_ids.add(source_id)
    processed_ids.add(card_id)

    update_new_listings_availability(
        page,
        listing_source=listing_source,
        is_unavailable_fn=is_unavailable_fn,
        limit=random.randint(1, 4),
        config=config,
        deadline=deadline,
        listing_type=listing_type,
    )


# UPDATE NEW LISTINGS AVAILABILITY
def update_new_listings_availability(
    page: Page,
    *,
    listing_source: ListingSource,
    is_unavailable_fn: Callable[[Page], bool],
    limit: int,
    config: ProxyRotationConfig,
    deadline: float | None = None,
    listing_type: ListingType | None = None,
) -> None:
    """
    Visit up to limit prospect listings with status New for the given source
    that have not been checked within the past 24 hours, mark any that are no
    longer available as NotAvailable, and stamp status_checked_at on every
    completed check. When limit is zero or negative, returns immediately without
    doing any work. When deadline (a time.monotonic value) is given, raises
    ProxySessionExpired between listings once it is reached so the proxy can be
    rotated without interrupting a listing.
    """

    if limit <= 0:
        return

    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    engine = create_engine_with_retry(database_url)
    SessionLocal = sessionmaker(bind=engine)

    source_label = listing_source.value
    if listing_type is not None:
        source_label = f"{source_label} {listing_type.value}"

    with SessionLocal() as session:
        checked_before = datetime.now() - timedelta(hours=24)

        # retry the initial load so a transient db hiccup at sweep start does
        # not abort the whole availability check
        def load_new_listings():
            query = session.query(ProspectListings).filter(
                ProspectListings.listing_source == listing_source,
                ProspectListings.status == ProspectListingStatus.NEW,
                or_(
                    ProspectListings.status_checked_at.is_(None),
                    ProspectListings.status_checked_at < checked_before,
                ),
            )
            if listing_type is not None:
                query = query.filter(ProspectListings.listing_type == listing_type)

            return (
                query.order_by(ProspectListings.status_checked_at.asc().nullsfirst())
                .limit(limit)
                .all()
            )

        new_listings = with_db_retry(
            load_new_listings,
            description="load New listings for availability check",
        )

        if not new_listings:
            print(f"No New {source_label} listings to check for availability")
            return

        print(f"Checking availability of {len(new_listings)} New {source_label} listings...")

        for listing in new_listings:
            # rotate the proxy between listings, never mid-check, once the
            # session has outlived the rotation interval
            if deadline is not None and time.monotonic() >= deadline:
                raise ProxySessionExpired(
                    "proxy rotation interval elapsed during availability check"
                )

            print(
                f"Checking: {listing.make_and_model} - {listing.short_description[:50]}..."
            )

            # guard each visit so one bad listing does not abort the whole sweep
            try:
                # reload with back-off so a transient proxy/network blip never
                # gets misread as the advert being gone
                response = load_listing_with_backoff(
                    page,
                    listing.url,
                    max_retries=config.availability_max_load_retries,
                    backoff_seconds=config.availability_load_backoff_seconds,
                )

                if is_http_not_found(response) or is_unavailable_fn(page):
                    listing.status = ProspectListingStatus.NOT_AVAILABLE
                    listing.updated_at = datetime.now()
                    listing.status_checked_at = datetime.now()
                    session.commit()
                    reason = "404" if is_http_not_found(response) else "unavailable"
                    print(f"  Marked as NotAvailable ({reason}): {listing.url}")

                    # drop the now-orphaned photos from s3 and the images table
                    delete_listing_images(listing, session)
                else:
                    listing.status_checked_at = datetime.now()
                    session.commit()
                    print("  Still available")
            except CaptchaSolveError:
                # an unsolved challenge will block every remaining listing on
                # this exit ip too, so bubble up for a proxy rotation instead
                # of misclassifying this listing as unavailable
                session.rollback()
                raise
            except Exception as e:
                if is_not_found_error(e):
                    listing.status = ProspectListingStatus.NOT_AVAILABLE
                    listing.updated_at = datetime.now()
                    listing.status_checked_at = datetime.now()
                    session.commit()
                    print(f"  Marked as NotAvailable (404): {listing.url}")

                    # drop the now-orphaned photos from s3 and the images table
                    delete_listing_images(listing, session)
                elif is_site_unreachable_error(e) or is_playwright_timeout(e):
                    # the page could not be fetched even after back-off retries;
                    # availability is unknown, so never mark NotAvailable. bubble
                    # up so the outer loop rotates the proxy or aborts the run.
                    session.rollback()
                    reason = (
                        "probe timed out"
                        if is_playwright_timeout(e)
                        else "site unreachable"
                    )
                    print(
                        f"  {reason.title()} after "
                        f"{config.availability_max_load_retries} attempts; aborting "
                        f"availability check: {e}"
                    )
                    raise
                else:
                    session.rollback()
                    print(f"  Error checking {listing.url}: {e}")

            pause(1.0, 3.0)

        print("Finished availability check for New listings")


# RUN WITH PROXY ROTATION
def run_with_proxy_rotation(
    *,
    config: ProxyRotationConfig,
    open_session: Callable[[Any], tuple],
    setup: Callable[[Page, ScrapeResumeState], None],
    scrape: Callable[[Page, float, ScrapeResumeState], None],
) -> None:
    """
    Run a listings scrape inside a proxy-rotation loop. When a Decodo sticky
    session expires mid-run the browser is relaunched on a fresh proxy port and
    the sweep resumes from ScrapeResumeState; already-saved listings are skipped
    because each scrape pass reloads existing source ids from the database.
    """

    scrape_resume = ScrapeResumeState()

    configure_consecutive_captcha_rotation(
        config.max_consecutive_captchas_before_rotation
    )

    with sync_stealth_playwright() as p:
        check_proxy_health(p)

        browser = None
        rotations = 0
        consecutive_setup_failures = 0
        consecutive_db_failures = 0

        try:
            while True:
                established = False
                rotation_interval_minutes = (
                    config.sample_proxy_rotation_interval_minutes()
                )
                try:
                    browser, page = open_session(p)
                    reset_consecutive_captcha_count()

                    deadline = (
                        time.monotonic()
                        + rotation_interval_minutes * 60
                    )

                    if scrape_resume.search_url is None:
                        setup(page, scrape_resume)
                    else:
                        print("Resuming listings scrape after proxy rotation")

                    established = True
                    consecutive_setup_failures = 0

                    scrape(page, deadline, scrape_resume)
                    break
                except ProxySessionExpired as e:
                    if rotations >= config.max_proxy_rotations:
                        raise

                    rotations += 1
                    print(
                        f"\nScheduled proxy rotation after "
                        f"{rotation_interval_minutes:.1f} min ({e}); relaunching "
                        f"on a new Decodo port and resuming "
                        f"(rotation {rotations}/{config.max_proxy_rotations})...\n"
                    )

                    if browser is not None:
                        close_browser_quietly(browser, force_kill_first=True)
                        browser = None
                except Exception as e:
                    if is_transient_db_error(e):
                        consecutive_db_failures += 1
                        if consecutive_db_failures >= config.max_consecutive_db_failures:
                            raise RuntimeError(
                                f"Database unreachable after "
                                f"{consecutive_db_failures} consecutive attempts "
                                f"({e}). Aborting."
                            ) from e

                        delay = min(60.0, 5.0 * consecutive_db_failures)
                        print(
                            f"\nTransient database error ({e}); waiting "
                            f"{delay:.0f}s and resuming from the same position "
                            f"(db retry {consecutive_db_failures}/"
                            f"{config.max_consecutive_db_failures})...\n"
                        )

                        if browser is not None:
                            close_browser_quietly(browser)
                            browser = None

                        time.sleep(delay)
                        continue

                    consecutive_db_failures = 0

                    if (
                        not (
                            is_proxy_network_error(e)
                            or is_navigation_timeout(e)
                            or is_target_closed_error(e)
                            or is_playwright_timeout(e)
                            or isinstance(e, CaptchaSolveError)
                            or isinstance(e, PageUnresponsiveError)
                        )
                        or rotations >= config.max_proxy_rotations
                    ):
                        raise

                    if not established:
                        consecutive_setup_failures += 1
                        if (
                            consecutive_setup_failures
                            >= config.max_consecutive_setup_failures
                        ):
                            raise RuntimeError(
                                f"Proxy failed to open the site on "
                                f"{consecutive_setup_failures} consecutive sessions "
                                f"({e}). This usually means the Decodo proxy is out "
                                "of bandwidth, suspended or misconfigured, or its "
                                "exit IPs are blocked outright; rotating ports will "
                                "not help. Aborting."
                            ) from e

                    rotations += 1
                    print(
                        f"\nProxy session appears dead or blocked ({e}); rotating "
                        f"to a new Decodo port and resuming "
                        f"(rotation {rotations}/{config.max_proxy_rotations})...\n"
                    )

                    if browser is not None:
                        close_browser_quietly(browser, force_kill_first=True)
                        browser = None

            if sys.stdin.isatty():
                page.wait_for_event("close", timeout=0)
        finally:
            close_browser_quietly(browser)
