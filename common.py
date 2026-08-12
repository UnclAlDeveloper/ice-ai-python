import functools
import hashlib
import os
import random
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Callable, Optional, TypeVar

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from models.ice_ai import OauthTokens

T = TypeVar("T")

# message fragments seen on transient database connection failures (DNS hiccups,
# server still starting, momentary network drops) that are worth retrying rather
# than aborting; matched case-insensitively against the exception text
TRANSIENT_DB_ERROR_FRAGMENTS = (
    "temporary failure in name resolution",
    "could not translate host name",
    "name or service not known",
    "could not connect to server",
    "connection refused",
    "server closed the connection unexpectedly",
    "the database system is starting up",
    "connection timed out",
    "could not receive data from server",
    "no route to host",
    "network is unreachable",
)

# default retry budget for transient database failures: five attempts with
# exponential backoff (1s, 2s, 4s, 8s) between them
DB_RETRY_ATTEMPTS = int(os.getenv("DB_RETRY_ATTEMPTS", "5"))
DB_RETRY_BASE_DELAY = float(os.getenv("DB_RETRY_BASE_DELAY", "1.0"))

# escalating per-attempt timeouts (in milliseconds) for slow page/network
# operations: a quick first try, then progressively longer waits (30s, 1m, 2m,
# 4m) before finally giving up, so a transiently slow proxy exit or slow-loading
# media is given more time rather than failing hard at a single 30s deadline
TIMEOUT_BACKOFF_MS = (30000, 60000, 120000, 240000)


# IS PLAYWRIGHT TIMEOUT
def is_playwright_timeout(exc: BaseException) -> bool:
    """
    Return True when an exception is a Playwright timeout ('Timeout NNNNms
    exceeded'), which means an operation simply ran out of time and may succeed
    if retried with a longer deadline rather than being a hard failure.
    """

    message = str(exc)
    return "Timeout" in message and "exceeded" in message


# RUN WITH TIMEOUT BACKOFF
def run_with_timeout_backoff(
    operation: Callable[[int], T],
    *,
    description: str = "operation",
    backoff: tuple[int, ...] = TIMEOUT_BACKOFF_MS,
) -> T:
    """
    Run a Playwright operation that accepts a timeout (in milliseconds), retrying
    it with progressively longer timeouts (30s, 1m, 2m, 4m by default) whenever
    it times out. The operation callable is given the timeout to use for each
    attempt. Only genuine timeouts are retried; any other error propagates
    immediately because waiting longer would not help. The final timeout error
    is re-raised once the whole backoff sequence is exhausted.
    """

    last_error: Optional[BaseException] = None
    for index, timeout_ms in enumerate(backoff):
        try:
            return operation(timeout_ms)
        except Exception as e:
            # a non-timeout failure will not be cured by a longer deadline
            if not is_playwright_timeout(e):
                raise

            last_error = e

            # log and escalate while longer timeouts remain in the budget
            if index < len(backoff) - 1:
                next_timeout = backoff[index + 1]
                print(
                    f"  {description} timed out after {timeout_ms / 1000:.0f}s; "
                    f"retrying with a {next_timeout / 1000:.0f}s timeout..."
                )

    # exhausted the backoff sequence while still timing out
    assert last_error is not None
    raise last_error


# WAIT FOR SELECTOR WITH BACKOFF
def wait_for_selector_with_backoff(
    page,
    selector: str,
    *,
    state: str = "attached",
    description: Optional[str] = None,
    backoff: tuple[int, ...] = TIMEOUT_BACKOFF_MS,
):
    """
    Wait for a selector to reach the given state, escalating the timeout (30s,
    1m, 2m, 4m by default) on each attempt so a slow page render gets more time
    before failing. A thin wrapper over run_with_timeout_backoff for the very
    common page.wait_for_selector call.
    """

    return run_with_timeout_backoff(
        lambda timeout_ms: page.wait_for_selector(
            selector, state=state, timeout=timeout_ms
        ),
        description=description or f"wait for {selector}",
        backoff=backoff,
    )


# IS TRANSIENT DB ERROR
def is_transient_db_error(exc: BaseException) -> bool:
    """
    Return True when an exception is a database OperationalError caused by a
    transient connection problem (e.g. a momentary DNS resolution failure on
    WSL2) rather than a genuine query or schema error. Callers use this to
    decide when retrying the same operation is worthwhile.
    """

    if not isinstance(exc, OperationalError):
        return False

    message = str(exc).lower()
    return any(fragment in message for fragment in TRANSIENT_DB_ERROR_FRAGMENTS)


# WITH DB RETRY
def with_db_retry(
    operation: Callable[[], T],
    *,
    attempts: int = DB_RETRY_ATTEMPTS,
    base_delay: float = DB_RETRY_BASE_DELAY,
    description: str = "database operation",
) -> T:
    """
    Run a database unit of work, retrying transient connection failures with
    exponential backoff. Non-transient OperationalErrors (and any other
    exception) propagate immediately, as does a transient error once the retry
    budget is exhausted, so genuine faults are never silently swallowed.
    """

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except OperationalError as e:
            # only retry transient failures, and only while attempts remain
            if attempt >= attempts or not is_transient_db_error(e):
                raise

            delay = base_delay * 2 ** (attempt - 1)
            print(
                f"Transient database error during {description} "
                f"(attempt {attempt}/{attempts}); retrying in {delay:.0f}s..."
            )
            time.sleep(delay)

    # unreachable: the loop either returns a value or raises on the last attempt
    raise RuntimeError(f"with_db_retry exhausted attempts for {description}")


# DB RETRY
def db_retry(
    func: Optional[Callable[..., T]] = None,
    *,
    attempts: int = DB_RETRY_ATTEMPTS,
    base_delay: float = DB_RETRY_BASE_DELAY,
) -> Callable:
    """
    Decorator that wraps a function performing a single database unit of work in
    with_db_retry, so transient connection failures are retried with backoff.
    Usable both bare (@db_retry) and with arguments (@db_retry(attempts=3)).
    """

    def decorator(target: Callable[..., T]) -> Callable[..., T]:

        @functools.wraps(target)
        def wrapper(*args, **kwargs) -> T:
            return with_db_retry(
                lambda: target(*args, **kwargs),
                attempts=attempts,
                base_delay=base_delay,
                description=target.__name__,
            )

        return wrapper

    # support both @db_retry and @db_retry(...) call styles
    if func is not None:
        return decorator(func)
    return decorator


# CREATE ENGINE WITH RETRY
def create_engine_with_retry(database_url: str, **kwargs):
    """
    Create a SQLAlchemy engine with pool_pre_ping enabled so stale pooled
    connections are detected and replaced transparently. This is a drop-in
    replacement for create_engine at the scrapers' connection points.
    """

    kwargs.setdefault("pool_pre_ping", True)
    return create_engine(database_url, **kwargs)


# PAUSE DELAY SECONDS
def pause_delay_seconds(min_seconds: float, max_seconds: float) -> float:
    """
    Return a human-like delay without sleeping. The delay uses the same gamma
    distribution parameterisation as pause().
    """

    if max_seconds <= min_seconds:
        return max(min_seconds, 0.0)

    scale = (max_seconds - min_seconds) / 1.8
    shape = ((min_seconds * 0.9) / scale) + 1.0
    return min_seconds * 0.1 + random.gammavariate(shape, scale)


# PAUSE
def pause(min_seconds: float = 1.0, max_seconds: float = 3.0):
    """
    Wait a random amount of time to simulate human browsing behaviour. The
    delay is drawn from a gamma distribution parameterised so that its mode
    equals min_seconds and its mean equals (min_seconds + max_seconds) / 2,
    giving a peak at the lower bound with a long tail toward longer pauses.
    """

    time.sleep(pause_delay_seconds(min_seconds, max_seconds))


# PAUSE WITH POLL
def pause_with_poll(
    poll: Callable[[], None] | None = None,
    *,
    min_seconds: float = 1.0,
    max_seconds: float = 3.0,
    poll_interval: float = 0.5,
) -> None:
    """
    Wait using pause_delay_seconds(), optionally calling poll between sleeps so
    scrapers can dismiss late cookie banners during longer delays.
    """

    delay = pause_delay_seconds(min_seconds, max_seconds)
    deadline = time.time() + delay

    while time.time() < deadline:
        if poll is not None:
            poll()
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))


# WAIT WITH POLL
def wait_with_poll(
    action: Callable[[int], None],
    *,
    poll: Callable[[], None] | None = None,
    timeout_ms: int = 30000,
    poll_interval: float = 0.5,
    slice_timeout_ms: int = 1000,
) -> None:
    """
    Retry action in short timeout slices until it succeeds or timeout_ms elapses,
    optionally calling poll between attempts so late cookie banners can be
    dismissed during the wait. action receives the remaining timeout for the
    current slice in milliseconds.
    """

    deadline = time.time() + timeout_ms / 1000
    last_error: Exception | None = None

    while time.time() < deadline:
        if poll is not None:
            poll()
        try:
            remaining_ms = max(100, int((deadline - time.time()) * 1000))
            action(min(slice_timeout_ms, remaining_ms))
            return
        except Exception as exc:
            last_error = exc

        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))

    if last_error is not None:
        raise last_error

    raise TimeoutError(f"Timed out after {timeout_ms}ms waiting with poll")


# PARSE MILEAGE
def parse_mileage(raw: str | None) -> tuple[int | None, str | None]:
    """
    Split a mileage string such as '81,000 mi' or '138,100 Miles' into the
    numeric value and unit. Returns (None, None) when the value cannot be
    parsed.
    """

    if not raw:
        return None, None

    match = re.match(r"^([\d,]+)\s*(.+)$", raw.strip())
    if not match:
        return None, None

    return int(match.group(1).replace(",", "")), match.group(2).strip()


# IS HTTP NOT FOUND
def is_http_not_found(response) -> bool:
    """
    Return True when a Playwright navigation response indicates HTTP 404.
    """

    return response is not None and response.status == 404


# IS NOT FOUND ERROR
def is_not_found_error(exc: BaseException) -> bool:
    """
    Return True when an exception from navigation indicates the page was not
    found. Only explicit HTTP-404 phrasing is matched, so a '404' that merely
    appears inside a listing URL or captcha-solver task id embedded in the
    exception message cannot misclassify a listing as not found.
    """

    message = str(exc).lower()

    # match 404 only when phrased as an http status, never as a bare substring
    if re.search(r"\b(?:http\s+)?404\s+not\s+found\b", message):
        return True
    if re.search(r"\bstatus(?:\s+code)?\s*[:=]?\s*404\b", message):
        return True
    return "page not found" in message


# GET EXISTING HASH CODES
@db_retry
def get_existing_hash_codes(listing_source: str) -> set[str]:
    """
    Query database for existing hash_codes in prospect_listings filtered by
    listing_source. Rows of every status are included because the hash_code
    unique constraint is table-wide, so a listing that is e.g. NotAvailable
    would still block an insert of the same hash.
    """

    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    schema = os.getenv("AUTO_ADS_DATABASE_SCHEMA", "aa")

    engine = create_engine_with_retry(database_url)
    with engine.connect() as conn:
        result = conn.execute(
            text(
                f"SELECT hash_code FROM {schema}.prospect_listings "
                f"WHERE listing_source = :listing_source"
            ),
            {"listing_source": listing_source},
        )
        return {row[0] for row in result}


# GET EXISTING SOURCE IDS
@db_retry
def get_existing_source_ids(listing_source: str) -> set[str]:
    """
    Query database for existing source_ids in prospect_listings filtered by
    listing_source. The source_id is the site's own listing reference (e.g.
    'C2085940' for Car & Classic), so it identifies a listing unambiguously
    where titles, and therefore title-based hash codes, can collide. Rows of
    every status are included so previously seen listings are never re-added.
    """

    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    schema = os.getenv("AUTO_ADS_DATABASE_SCHEMA", "aa")

    engine = create_engine_with_retry(database_url)
    with engine.connect() as conn:
        result = conn.execute(
            text(
                f"SELECT source_id FROM {schema}.prospect_listings "
                f"WHERE listing_source = :listing_source "
                f"AND source_id IS NOT NULL"
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
