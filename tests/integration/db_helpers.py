"""Shared helpers for development-database integration tests."""

import uuid

from models.auto_ads import ProspectListings


# UNIQUE TEST SOURCE ID
def unique_test_source_id(prefix: str) -> str:
    """Return a source_id that will not collide with real listings."""

    return f"{prefix}-{uuid.uuid4()}"


# INSERT PROSPECT
def insert_prospect(session, created_ids, prospect: ProspectListings):
    """Commit a prospect row and record its id for fixture teardown."""

    session.add(prospect)
    session.flush()
    session.commit()
    session.refresh(prospect)
    created_ids.append(prospect.id)
    return prospect
