import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import or_
from sqlalchemy.orm import sessionmaker

from common import (
    create_engine_with_retry,
    is_http_not_found,
    is_not_found_error,
    is_transient_db_error,
    pause,
    with_db_retry,
)
from listing_images import delete_listing_images
from models.auto_ads import ProspectListings
from models.enums import ListingSource, ListingType, ProspectListingStatus
from stealth_browser import (
    CaptchaSolveError,
    Page,
    check_proxy_health,
    goto_with_captcha_handling,
    is_navigation_timeout,
    is_proxy_network_error,
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


# PROXY ROTATION CONFIG
@dataclass
class ProxyRotationConfig:
    """
    Tunable limits for proxy rotation, availability reload back-off, and
    transient database failure handling. Values are read from environment
    variables prefixed by the scraper's site name (e.g. AUTOTRADER_).
    """

    max_proxy_rotations: int
    proxy_rotation_interval_minutes: float
    max_consecutive_setup_failures: int
    max_consecutive_db_failures: int
    availability_max_load_retries: int
    availability_load_backoff_seconds: float

    # PROXY ROTATION CONFIG FROM ENV PREFIX
    @classmethod
    def from_env_prefix(cls, prefix: str) -> "ProxyRotationConfig":
        """
        Build a ProxyRotationConfig from environment variables whose names start
        with the given prefix (e.g. AUTOTRADER_MAX_PROXY_ROTATIONS).
        """

        return cls(
            max_proxy_rotations=int(os.getenv(f"{prefix}_MAX_PROXY_ROTATIONS", "20")),
            proxy_rotation_interval_minutes=float(
                os.getenv(f"{prefix}_PROXY_ROTATION_MINUTES", "25")
            ),
            max_consecutive_setup_failures=int(
                os.getenv(f"{prefix}_MAX_SETUP_FAILURES", "3")
            ),
            max_consecutive_db_failures=int(
                os.getenv(f"{prefix}_MAX_DB_FAILURES", "10")
            ),
            availability_max_load_retries=int(
                os.getenv(f"{prefix}_AVAILABILITY_LOAD_RETRIES", "4")
            ),
            availability_load_backoff_seconds=float(
                os.getenv(f"{prefix}_AVAILABILITY_LOAD_BACKOFF", "5")
            ),
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


# WITH PAGE PARAM
def with_page_param(url: str, page_number: int) -> str:
    """
    Return a search-results url with its page query parameter set to
    page_number. Page one omits the parameter so the canonical first-page url
    (including sort and filter query params) is preserved for later pagination
    and proxy-resume navigations.
    """

    parsed = urlparse(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "page"
    ]
    if page_number > 1:
        query.append(("page", str(page_number)))

    return urlunparse(parsed._replace(query=urlencode(query)))


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
            if not is_site_unreachable_error(e):
                raise

            last_error = e
            if attempt < max_retries:
                # exponential back-off gives a flapping proxy or connection time
                # to recover before the next attempt
                delay = backoff_seconds * (2 ** (attempt - 1))
                print(
                    f"  Site unreachable (attempt {attempt}/"
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
                elif is_site_unreachable_error(e):
                    # the page could not be fetched even after back-off retries;
                    # availability is unknown, so never mark NotAvailable. bubble
                    # up so the outer loop rotates the proxy or aborts the run.
                    session.rollback()
                    print(
                        f"  Site still unreachable after "
                        f"{config.availability_max_load_retries} attempts; aborting "
                        f"availability check: {e}"
                    )
                    raise
                else:
                    session.rollback()
                    print(f"  Error checking {listing.url}: {e}")

            pause(2.0, 8.0)

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

    with sync_stealth_playwright() as p:
        check_proxy_health(p)

        browser = None
        rotations = 0
        consecutive_setup_failures = 0
        consecutive_db_failures = 0

        try:
            while True:
                established = False
                try:
                    browser, page = open_session(p)

                    deadline = (
                        time.monotonic()
                        + config.proxy_rotation_interval_minutes * 60
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
                        f"{config.proxy_rotation_interval_minutes} min ({e}); relaunching "
                        f"on a new Decodo port and resuming "
                        f"(rotation {rotations}/{config.max_proxy_rotations})...\n"
                    )

                    if browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            pass
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
                            try:
                                browser.close()
                            except Exception:
                                pass
                            browser = None

                        time.sleep(delay)
                        continue

                    consecutive_db_failures = 0

                    if (
                        not (
                            is_proxy_network_error(e)
                            or is_navigation_timeout(e)
                            or isinstance(e, CaptchaSolveError)
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
                        try:
                            browser.close()
                        except Exception:
                            pass
                        browser = None

            if sys.stdin.isatty():
                page.wait_for_event("close", timeout=0)
        finally:
            if browser is not None:
                browser.close()
