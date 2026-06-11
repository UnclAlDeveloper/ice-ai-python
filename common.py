import hashlib
import os
import random
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from models.ice_ai import OauthTokens


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
def get_existing_hash_codes(listing_source: str) -> set[str]:
    """
    Query database for existing hash_codes in prospect_listings filtered by
    listing_source. Rows of every status are included because the hash_code
    unique constraint is table-wide, so a listing that is e.g. NotAvailable
    would still block an insert of the same hash.
    """

    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    schema = os.getenv("AUTO_ADS_DATABASE_SCHEMA", "aa")

    engine = create_engine(database_url)
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

    engine = create_engine(database_url)
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
