import os

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from common import create_engine_with_retry
from environments import load_environment
from models.auto_ads import ProspectListings


@pytest.fixture(scope="session")
def auto_ads_engine():
    """Engine for the shared development auto-ads database from .env.dev."""

    load_environment()
    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    if not database_url:
        pytest.skip("AUTO_ADS_DATABASE_URL is not set")

    engine = create_engine_with_retry(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("development auto-ads database is unavailable")

    yield engine
    engine.dispose()


@pytest.fixture
def auto_ads_session(auto_ads_engine):
    """SQLAlchemy session that deletes prospect rows created during the test."""

    SessionLocal = sessionmaker(bind=auto_ads_engine)
    session = SessionLocal()
    created_ids: list[int] = []
    try:
        yield session, created_ids
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        if created_ids:
            session.rollback()
            session.query(ProspectListings).filter(
                ProspectListings.id.in_(created_ids)
            ).delete(synchronize_session=False)
            session.commit()
        session.close()
