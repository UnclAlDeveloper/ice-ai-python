import hashlib
import os
import random
import secrets
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from playwright.sync_api import Page
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from models.ice_ai import OauthTokens

CAPTCHA_URL_FRAGMENTS = (
    "challenges.cloudflare.com",
    "/cdn-cgi/challenge-platform",
    "captcha",
    "datadome",
    "perimeterx",
    "px-captcha",
    "/_Incapsula_Resource",
)

CAPTCHA_TITLE_FRAGMENTS = (
    "just a moment",
    "attention required",
    "verify you are human",
    "are you human",
    "access denied",
    "you have been blocked",
)

CAPTCHA_TEXT_FRAGMENTS = (
    "Verify you are human",
    "Press & Hold",
    "Please verify you are a human",
    "Are you human",
    "unusual traffic",
)


# PAUSE
def pause(min_seconds: float = 1.0, max_seconds: float = 3.0):
    """
    Wait a random amount of time to simulate human browsing behaviour. The
    delay is drawn from a gamma distribution parameterised so that its mode
    equals min_seconds and its mean equals (min_seconds + max_seconds) / 2,
    giving a peak at the lower bound with a long tail toward longer pauses.
    """

    # fall back to a fixed delay when the range is degenerate or invalid
    if max_seconds <= min_seconds:
        time.sleep(max(min_seconds, 0.0))
        return

    # derive gamma shape and scale from the requested mode and mean:
    #   mode = (shape - 1) * scale = min_seconds
    #   mean = shape * scale       = (min_seconds + max_seconds) / 2
    # subtracting the two equations gives scale, and the shape follows
    scale = (max_seconds - min_seconds) / 1.8
    shape = ((min_seconds * 0.9) / scale) + 1.0

    delay = min_seconds * 0.1 + random.gammavariate(shape, scale)
    time.sleep(delay)


# IS HTTP NOT FOUND
def is_http_not_found(response) -> bool:
    """
    Return True when a Playwright navigation response indicates HTTP 404.
    """

    return response is not None and response.status == 404


# IS NOT FOUND ERROR
def is_not_found_error(exc: BaseException) -> bool:
    """
    Return True when an exception from navigation indicates the page was not found.
    """

    message = str(exc).lower()
    return "404" in message or "not found" in message


# IS CAPTCHA PRESENT
def is_captcha_present(page: Page) -> bool:
    """
    Detect whether the current page is showing a captcha or anti-bot challenge
    such as a Cloudflare interstitial, DataDome 'Press & Hold' challenge, or an
    embedded hCaptcha/reCAPTCHA widget.
    """

    try:
        # url-based detection covers cloudflare/datadome/perimeterx redirects
        current_url = (page.url or "").lower()
        if any(fragment in current_url for fragment in CAPTCHA_URL_FRAGMENTS):
            return True

        # title-based detection catches the typical interstitial titles
        title = (page.title() or "").lower()
        if any(fragment in title for fragment in CAPTCHA_TITLE_FRAGMENTS):
            return True

        # iframe-based detection catches embedded challenge widgets
        challenge_iframe = page.locator(
            'iframe[src*="challenges.cloudflare.com"], '
            'iframe[src*="recaptcha"], '
            'iframe[src*="hcaptcha"], '
            'iframe[src*="datadome"], '
            'iframe[src*="perimeterx"], '
            'iframe[src*="captcha-delivery"]'
        )
        if challenge_iframe.count() > 0:
            return True

        # visible-text detection as a final fallback for provider-agnostic prompts
        for fragment in CAPTCHA_TEXT_FRAGMENTS:
            if page.get_by_text(fragment, exact=False).count() > 0:
                return True
    except Exception:
        # if any probe fails (e.g. detached frame) assume no captcha so the
        # caller can decide how to handle the underlying error
        return False

    return False


# WAIT FOR CAPTCHA SOLVE
def wait_for_captcha_solve(page: Page) -> None:
    """
    Pause execution while the user manually solves a captcha in the visible
    browser window. Returns only once the captcha is no longer detected.
    In non-interactive contexts (no controlling TTY on stdin, e.g. the
    scheduler subprocess) raises RuntimeError immediately so the job fails
    fast and the scheduler can move on, rather than blocking forever on
    input() waiting for a human that will never arrive.
    """

    if not is_captcha_present(page):
        return

    # try to record the offending url for diagnostics in either branch
    current_url = ""
    try:
        current_url = page.url or ""
    except Exception:
        pass

    # fail fast when nobody can solve the challenge interactively; isatty()
    # returns False under apscheduler subprocesses and most container runtimes
    if not sys.stdin.isatty():
        raise RuntimeError(
            f"CAPTCHA encountered at {current_url!r} in a non-interactive run; "
            "aborting because no human is available to solve it."
        )

    # show a clearly delimited prompt so it stands out in the console
    print()
    print("=" * 70)
    print("CAPTCHA detected — manual intervention required.")
    if current_url:
        print(f"  Current URL: {current_url}")
    print("Solve the challenge in the browser window, then press Enter here.")
    print("=" * 70)
    input()

    # loop until the captcha is truly gone, in case the user pressed Enter early
    while is_captcha_present(page):
        input("Captcha still detected — solve it and press Enter to retry...")


# GOTO WITH CAPTCHA HANDLING
def goto_with_captcha_handling(
    page: Page, url: str, max_retries: int = 3
) -> Optional[object]:
    """
    Navigate to a url, transparently pausing for the user to solve any captcha
    that interrupts the navigation. Returns the Playwright response from the
    final successful page.goto call.
    """

    attempt = 0
    last_error: Optional[Exception] = None

    while attempt < max_retries:
        attempt += 1
        try:
            response = page.goto(url)
            page.wait_for_load_state("domcontentloaded")

            # if a captcha appeared after a successful load, wait then retry
            if is_captcha_present(page):
                wait_for_captcha_solve(page)
                continue

            return response
        except Exception as e:
            last_error = e

            # a mid-flight challenge redirect typically surfaces as ERR_ABORTED;
            # if a captcha is now on the page, pause for the user and retry
            if is_captcha_present(page):
                wait_for_captcha_solve(page)
                continue

            # unrelated failure — let the caller decide
            raise

    # exhausted retries while still seeing a captcha
    if last_error is not None:
        raise last_error
    raise RuntimeError(
        f"Failed to navigate to {url} after {max_retries} captcha retries"
    )


# GET EXISTING HASH CODES
def get_existing_hash_codes(listing_source: str) -> set[str]:
    """
    Query database for existing hash_codes in prospect_listings filtered by
    listing_source and restricted to rows with status 'New' or 'Viewed'.
    """

    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    schema = os.getenv("AUTO_ADS_DATABASE_SCHEMA", "aa")

    engine = create_engine(database_url)
    with engine.connect() as conn:
        result = conn.execute(
            text(
                f"SELECT hash_code FROM {schema}.prospect_listings "
                f"WHERE listing_source = :listing_source "
                f"AND status IN ('New', 'Viewed')"
            ),
            {"listing_source": listing_source},
        )
        return {row[0] for row in result}


# GENERATE HASH CODE
def generate_hash_code(short_description: str) -> str:
    """
    Generate a 16-character hash code from the short description using MD5.
    """

    return hashlib.md5(short_description.encode()).hexdigest()[:16]


# GENERATE IMAGE HASH
def generate_image_hash() -> str:
    """
    Generate a 16-character random hex string for use as an image filename.
    """

    return secrets.token_hex(8)


# GET OAUTH TOKENS
def get_oauth_tokens(provider: str) -> Optional[OauthTokens]:
    """
    Read OAuth token data for a provider from the ia.oauth_tokens table.
    Returns an OauthTokens instance, or None if no row exists.
    """

    database_url = os.getenv("ICE_AI_DATABASE_URL")

    engine = create_engine(database_url)
    with Session(engine) as session:
        return session.get(OauthTokens, provider)


# SAVE OAUTH TOKENS
def save_oauth_tokens(
    provider: str,
    account_id: Optional[str] = None,
    access_token: Optional[str] = None,
    access_token_expiry: Optional[datetime] = None,
    refresh_token: Optional[str] = None,
    refresh_token_expiry: Optional[datetime] = None,
) -> None:
    """
    Upsert OAuth token data for a provider into the ia.oauth_tokens table.
    Only non-None fields are updated so callers can update individual
    columns without overwriting others.
    """

    database_url = os.getenv("ICE_AI_DATABASE_URL")

    engine = create_engine(database_url)
    with Session(engine) as session:
        existing = session.get(OauthTokens, provider)

        if existing is None:
            existing = OauthTokens(provider=provider)
            session.add(existing)

        # only overwrite fields that the caller explicitly provided
        if account_id is not None:
            existing.account_id = account_id
        if access_token is not None:
            existing.access_token = access_token
        if access_token_expiry is not None:
            existing.access_token_expiry = access_token_expiry
        if refresh_token is not None:
            existing.refresh_token = refresh_token
        if refresh_token_expiry is not None:
            existing.refresh_token_expiry = refresh_token_expiry

        existing.updated_at = datetime.now(timezone.utc)
        session.commit()


# DELETE OAUTH TOKENS
def delete_oauth_tokens(provider: str) -> None:
    """
    Remove the OAuth token row for a provider from the ia.oauth_tokens table.
    No-op if the row does not exist.
    """

    database_url = os.getenv("ICE_AI_DATABASE_URL")

    engine = create_engine(database_url)
    with Session(engine) as session:
        existing = session.get(OauthTokens, provider)
        if existing is not None:
            session.delete(existing)
            session.commit()
