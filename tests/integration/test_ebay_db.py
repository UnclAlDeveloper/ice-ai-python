import uuid
from datetime import datetime, timezone

import pytest

from common import generate_hash_code, get_existing_source_ids
from ebay import build_prospect_listing
from models.auto_ads import ProspectListings
from models.enums import ListingSource, ListingType, ProspectListingStatus

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _unique_source_id() -> str:
    return f"pytest-ebay-{uuid.uuid4()}"


def _browse_listing(*, source_id: str, **overrides):
    listing = {
        "title": f"Pytest eBay listing {source_id}",
        "item_web_url": f"https://www.ebay.co.uk/itm/pytest/{source_id}",
        "item_id": source_id,
        "price": {"value": "4,500.00", "currency": "GBP"},
        "item_location": {"city": "Leeds"},
    }
    listing.update(overrides)
    return listing


def _insert_prospect(session, created_ids, prospect):
    session.add(prospect)
    session.flush()
    session.commit()
    session.refresh(prospect)
    created_ids.append(prospect.id)
    return prospect


class TestEbayProspectPersistence:
    """Insert mapped eBay prospects into the development database and read them back."""

    def test_mapped_listing_round_trips_required_columns(self, auto_ads_session):
        session, created_ids = auto_ads_session
        source_id = _unique_source_id()
        listing = _browse_listing(source_id=source_id)
        details = {
            "full_description": "Inserted by pytest; safe to delete.",
            "year": 1965,
            "fuel_type": "Petrol",
            "transmission": "Manual",
            "colour": "Blue",
            "mileage": 81000,
            "mileage_unit": "mi",
            "auction_closes": NOW,
        }
        prospect = build_prospect_listing(
            listing=listing,
            details=details,
            listing_type=ListingType.CLASSIC,
            hash_code=generate_hash_code(listing["title"]),
            make_and_model="Morris Minor",
            current_datetime=NOW,
        )

        saved = _insert_prospect(session, created_ids, prospect)
        loaded = session.get(ProspectListings, saved.id)

        assert loaded is not None
        assert loaded.source_id == source_id
        assert loaded.listing_source == ListingSource.EBAY
        assert loaded.listing_type == ListingType.CLASSIC
        assert loaded.status == ProspectListingStatus.NEW
        assert loaded.asking_price == 4500
        assert loaded.currency_symbol == "£"
        assert loaded.location == "Leeds"
        assert loaded.make_and_model == "Morris Minor"
        assert loaded.year == 1965
        assert loaded.mileage == 81000
        assert loaded.mileage_unit == "mi"
        assert loaded.gearbox_type == "Manual"
        assert loaded.auction_closes is not None

    def test_sparse_listing_persists_with_null_optional_fields(self, auto_ads_session):
        session, created_ids = auto_ads_session
        source_id = _unique_source_id()
        listing = _browse_listing(source_id=source_id, item_location={})
        prospect = build_prospect_listing(
            listing=listing,
            details={},
            listing_type=ListingType.VAN,
            hash_code=generate_hash_code(listing["title"]),
            make_and_model="Ford Transit",
            current_datetime=NOW,
        )

        saved = _insert_prospect(session, created_ids, prospect)
        loaded = session.get(ProspectListings, saved.id)

        assert loaded.source_id == source_id
        assert loaded.year is None
        assert loaded.mileage is None
        assert loaded.full_description is None
        assert loaded.location in ("", None)
        assert loaded.listing_type == ListingType.VAN

    def test_get_existing_source_ids_includes_committed_ebay_row(self, auto_ads_session):
        session, created_ids = auto_ads_session
        source_id = _unique_source_id()
        listing = _browse_listing(source_id=source_id)
        prospect = build_prospect_listing(
            listing=listing,
            details={},
            listing_type=ListingType.VAN,
            hash_code=generate_hash_code(listing["title"]),
            make_and_model="Ford Transit",
            current_datetime=NOW,
        )
        _insert_prospect(session, created_ids, prospect)

        existing = get_existing_source_ids(ListingSource.EBAY)

        assert source_id in existing

    def test_get_existing_source_ids_excludes_null_source_id(self, auto_ads_session):
        session, created_ids = auto_ads_session
        listing = _browse_listing(source_id=_unique_source_id())
        del listing["item_id"]
        prospect = build_prospect_listing(
            listing=listing,
            details={},
            listing_type=ListingType.VAN,
            hash_code=generate_hash_code(listing["title"] + "-null-source"),
            make_and_model="Ford Transit",
            current_datetime=NOW,
        )
        assert prospect.source_id is None

        saved = _insert_prospect(session, created_ids, prospect)
        existing = get_existing_source_ids(ListingSource.EBAY)

        assert saved.source_id is None
        assert None not in existing

    def test_usd_currency_symbol_fits_char_column(self, auto_ads_session):
        session, created_ids = auto_ads_session
        source_id = _unique_source_id()
        listing = _browse_listing(
            source_id=source_id,
            price={"value": "1200", "currency": "USD"},
        )
        prospect = build_prospect_listing(
            listing=listing,
            details={},
            listing_type=ListingType.VAN,
            hash_code=generate_hash_code(listing["title"]),
            make_and_model="Ford Transit",
            current_datetime=NOW,
        )

        saved = _insert_prospect(session, created_ids, prospect)
        loaded = session.get(ProspectListings, saved.id)

        assert loaded.currency_symbol == "$"
        assert loaded.asking_price == 1200

    def test_unknown_currency_empty_symbol_round_trips(self, auto_ads_session):
        session, created_ids = auto_ads_session
        source_id = _unique_source_id()
        listing = _browse_listing(
            source_id=source_id,
            price={"value": "3000", "currency": "EUR"},
        )
        prospect = build_prospect_listing(
            listing=listing,
            details={},
            listing_type=ListingType.VAN,
            hash_code=generate_hash_code(listing["title"]),
            make_and_model="Ford Transit",
            current_datetime=NOW,
        )
        assert prospect.currency_symbol == ""

        saved = _insert_prospect(session, created_ids, prospect)
        loaded = session.get(ProspectListings, saved.id)

        assert loaded.currency_symbol in ("", " ")
        assert loaded.asking_price == 3000
