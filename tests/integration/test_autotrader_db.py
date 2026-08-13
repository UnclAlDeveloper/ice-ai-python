from datetime import datetime, timezone

import pytest

from common import generate_hash_code, get_existing_source_ids
from models.auto_ads import ProspectListings
from models.enums import ListingSource, ListingType, ProspectListingStatus
from scraper_driver import persist_unavailable_listing_stub

from tests.integration.db_helpers import insert_prospect, unique_test_source_id

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _autotrader_listing(source_id: str, **overrides) -> ProspectListings:
    title = f"Pytest Autotrader listing {source_id}"
    values = dict(
        hash_code=generate_hash_code(title),
        source_id=source_id,
        listing_source=ListingSource.AUTOTRADER,
        listing_type=ListingType.VAN,
        status=ProspectListingStatus.NEW,
        make_and_model="Ford Transit",
        short_description=title,
        url=f"https://www.autotrader.co.uk/van-details/{source_id}",
        asking_price=12995,
        currency_symbol="£",
        vat_status="Inc VAT",
        location="Leeds",
        year=2007,
        registration="57 reg",
        mileage=81000,
        mileage_unit="miles",
        seats=3,
        created_at=NOW,
        updated_at=NOW,
        status_checked_at=NOW,
    )
    values.update(overrides)
    return ProspectListings(**values)


class TestAutotraderProspectPersistence:
    """Insert Autotrader prospects into the development database and read them back."""

    def test_mapped_listing_round_trips_required_columns(self, auto_ads_session):
        session, created_ids = auto_ads_session
        source_id = unique_test_source_id("pytest-autotrader")
        saved = insert_prospect(session, created_ids, _autotrader_listing(source_id))
        loaded = session.get(ProspectListings, saved.id)

        assert loaded.source_id == source_id
        assert loaded.listing_source == ListingSource.AUTOTRADER
        assert loaded.listing_type == ListingType.VAN
        assert loaded.asking_price == 12995
        assert loaded.currency_symbol == "£"
        assert loaded.vat_status == "Inc VAT"
        assert loaded.registration == "57 reg"
        assert loaded.seats == 3

    def test_sparse_listing_persists_with_null_optional_fields(self, auto_ads_session):
        session, created_ids = auto_ads_session
        source_id = unique_test_source_id("pytest-autotrader")
        saved = insert_prospect(
            session,
            created_ids,
            _autotrader_listing(
                source_id,
                asking_price=None,
                currency_symbol=None,
                vat_status=None,
                year=None,
                registration=None,
                mileage=None,
                mileage_unit=None,
                seats=None,
                location=None,
            ),
        )
        loaded = session.get(ProspectListings, saved.id)

        assert loaded.source_id == source_id
        assert loaded.year is None
        assert loaded.mileage is None
        assert loaded.asking_price is None

    def test_get_existing_source_ids_includes_committed_row(self, auto_ads_session):
        session, created_ids = auto_ads_session
        source_id = unique_test_source_id("pytest-autotrader")
        insert_prospect(session, created_ids, _autotrader_listing(source_id))

        assert source_id in get_existing_source_ids(ListingSource.AUTOTRADER)

    def test_unavailable_stub_is_skipped_on_later_scrapes(self, auto_ads_session):
        session, created_ids = auto_ads_session
        source_id = unique_test_source_id("pytest-autotrader")
        title = f"Pytest Autotrader listing {source_id}"
        persist_unavailable_listing_stub(
            session,
            listing_source=ListingSource.AUTOTRADER,
            listing_type=ListingType.VAN,
            hash_code=generate_hash_code(title),
            source_id=source_id,
            url=f"https://www.autotrader.co.uk/van-details/{source_id}",
            make_and_model="Ford Transit",
            short_description=title,
        )
        loaded = (
            session.query(ProspectListings)
            .filter(ProspectListings.source_id == source_id)
            .one()
        )
        created_ids.append(loaded.id)

        assert loaded.status == ProspectListingStatus.NOT_AVAILABLE
        assert source_id in get_existing_source_ids(ListingSource.AUTOTRADER)
