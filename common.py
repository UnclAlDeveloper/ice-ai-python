import hashlib
import os
import random
import secrets
import time

from sqlalchemy import create_engine, text


# PAUSE
def pause(min_seconds: float = 1.0, max_seconds: float = 3.0):
    """
    Wait a random amount of time to simulate human browsing behavior.
    """

    delay = random.uniform(min_seconds, max_seconds)
    time.sleep(delay)


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
