from datetime import date, datetime, timezone

import pytest

from common import generate_hash_code, get_existing_source_ids
from models.auto_ads import ProspectListings
from models.enums import ListingSource, ListingType, ProspectListingStatus
from scraper_driver import persist_unavailable_listing_stub

from tests.integration.db_helpers import insert_prospect, unique_test_source_id

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _pistonheads_listing(source_id: str, **overrides) -> ProspectListings:
    title = f"Pytest PistonHeads listing {source_id}"
    values = dict(
        hash_code=generate_hash_code(title),
        source_id=source_id,
        listing_source=ListingSource.PISTONHEADS,
        listing_type=ListingType.CLASSIC,
        status=ProspectListingStatus.NEW,
        make_and_model="1965 Jaguar E-Type",
        short_description=title,
        url=f"https://www.pistonheads.com/buy/listing/{source_id}",
        asking_price=19995,
        currency_symbol="£",
        year=1965,
        mileage=81000,
        mileage_unit="mi",
        engine_size="5.0L",
        mot_status="MOT expires 12 Jun 2025",
        mot_expiry=date(2025, 6, 12),
        created_at=NOW,
        updated_at=NOW,
        status_checked_at=NOW,
    )
    values.update(overrides)
    return ProspectListings(**values)


class TestPistonHeadsProspectPersistence:
    """Insert PistonHeads prospects into the development database and read them back."""

    def test_mapped_listing_round_trips_required_columns(self, auto_ads_session):
        session, created_ids = auto_ads_session
        source_id = unique_test_source_id("pytest-ph")
        saved = insert_prospect(session, created_ids, _pistonheads_listing(source_id))
        loaded = session.get(ProspectListings, saved.id)

        assert loaded.source_id == source_id
        assert loaded.listing_source == ListingSource.PISTONHEADS
        assert loaded.listing_type == ListingType.CLASSIC
        assert loaded.asking_price == 19995
        assert loaded.currency_symbol == "£"
        assert loaded.engine_size == "5.0L"
        assert loaded.mot_expiry == date(2025, 6, 12)

    def test_sparse_listing_persists_with_null_optional_fields(self, auto_ads_session):
        session, created_ids = auto_ads_session
        source_id = unique_test_source_id("pytest-ph")
        saved = insert_prospect(
            session,
            created_ids,
            _pistonheads_listing(
                source_id,
                asking_price=None,
                currency_symbol=None,
                year=None,
                mileage=None,
                mileage_unit=None,
                engine_size=None,
                mot_status=None,
                mot_expiry=None,
            ),
        )
        loaded = session.get(ProspectListings, saved.id)

        assert loaded.source_id == source_id
        assert loaded.asking_price is None
        assert loaded.mot_expiry is None

    def test_get_existing_source_ids_includes_committed_row(self, auto_ads_session):
        session, created_ids = auto_ads_session
        source_id = unique_test_source_id("pytest-ph")
        insert_prospect(session, created_ids, _pistonheads_listing(source_id))

        assert source_id in get_existing_source_ids(ListingSource.PISTONHEADS)

    def test_unavailable_stub_is_skipped_on_later_scrapes(self, auto_ads_session):
        session, created_ids = auto_ads_session
        source_id = unique_test_source_id("pytest-ph")
        title = f"Pytest PistonHeads listing {source_id}"
        persist_unavailable_listing_stub(
            session,
            listing_source=ListingSource.PISTONHEADS,
            listing_type=ListingType.CLASSIC,
            hash_code=generate_hash_code(title),
            source_id=source_id,
            url=f"https://www.pistonheads.com/buy/listing/{source_id}",
            make_and_model="Jaguar E-Type",
            short_description=title,
        )
        loaded = (
            session.query(ProspectListings)
            .filter(ProspectListings.source_id == source_id)
            .one()
        )
        created_ids.append(loaded.id)

        assert loaded.status == ProspectListingStatus.NOT_AVAILABLE
        assert source_id in get_existing_source_ids(ListingSource.PISTONHEADS)
