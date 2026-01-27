from sqlalchemy import Boolean, CHAR, Column, Date, DateTime, Index, Integer, PrimaryKeyConstraint, SmallInteger, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.orm import Mapped, declarative_base, mapped_column
from sqlalchemy.orm.base import Mapped

Base = declarative_base()


class Images(Base):
    __tablename__ = 'images'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='images_pkey'),
        {'schema': 'aa'}
    )

    id = mapped_column(Integer)
    url = mapped_column(String, nullable=False)
    listing_id = mapped_column(Integer, nullable=False)
    listing_source = mapped_column(ENUM('Autotrader', 'Ebay', 'Facebook', 'Gummtree', 'OnlyVans', name='listing_source', schema='aa'), nullable=False)
    listing_table = mapped_column(ENUM('Prospect', 'Resale', name='listing_table', schema='aa'), nullable=False)
    is_primary = mapped_column(Boolean)
    created_at = mapped_column(DateTime(True))


class ProspectListings(Base):
    __tablename__ = 'prospect_listings'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='prospect_listings_pkey'),
        UniqueConstraint('hash_code', name='prospect_listings_hash_code_unique'),
        Index('idx_prospect_listings_hash_code', 'hash_code'),
        {'schema': 'aa'}
    )

    id = mapped_column(Integer)
    hash_code = mapped_column(CHAR(16), nullable=False)
    make_and_model = mapped_column(String, nullable=False)
    short_description = mapped_column(String, nullable=False)
    url = mapped_column(String, nullable=False)
    asking_price = mapped_column(Integer, nullable=False)
    listing_source = mapped_column(ENUM('Autotrader', 'Ebay', 'Facebook', 'Gummtree', 'OnlyVans', name='listing_source', schema='aa'), nullable=False)
    status = mapped_column(ENUM('New', 'Viewed', 'NotInterested', 'Interested', 'Bought', name='prospect_listing_status', schema='aa'), nullable=False)
    created_at = mapped_column(DateTime(True))
    updated_at = mapped_column(DateTime(True))
    full_description = mapped_column(String)
    mileage = mapped_column(Integer)
    mileage_unit = mapped_column(String)
    year = mapped_column(Integer)
    registration = mapped_column(String)
    currency_symbol = mapped_column(CHAR(1))
    vat_status = mapped_column(String)
    location = mapped_column(String)
    body_type = mapped_column(String)
    cab_type = mapped_column(String)
    fuel_type = mapped_column(String)
    gearbox_type = mapped_column(String)
    wheelbase = mapped_column(String)
    engine_size = mapped_column(String)
    colour = mapped_column(String)
    seats = mapped_column(Integer)
    emission_class = mapped_column(String)
    number_of_owners = mapped_column(Integer)
    service_history = mapped_column(String)
    basic_history_check = mapped_column(String)
    mot_status = mapped_column(String)
    mot_expiry = mapped_column(Date)
    ai_work_and_repairs = mapped_column(String)
    ai_repair_cost = mapped_column(Integer)
    ai_sell_price_low = mapped_column(Integer)
    ai_sell_price_high = mapped_column(Integer)
    specs_and_features = mapped_column(String)
    interest_level = mapped_column(SmallInteger)
    ai_listing_summary = mapped_column(String)
    ai_resell_overview = mapped_column(String)
    ai_resell_notes = mapped_column(String)
    ai_buy_price_low = mapped_column(Integer)
    ai_buy_price_high = mapped_column(Integer)
