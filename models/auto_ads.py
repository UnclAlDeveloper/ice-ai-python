from typing import List

from sqlalchemy import Boolean, CHAR, Column, Date, DateTime, ForeignKeyConstraint, Index, Integer, PrimaryKeyConstraint, Sequence, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, declarative_base, mapped_column, relationship
from sqlalchemy.orm.base import Mapped

Base = declarative_base()


class ListingSources(Base):
    __tablename__ = 'listing_sources'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='listing_sources_pkey'),
        {'schema': 'aa'}
    )

    id = mapped_column(SmallInteger)
    name = mapped_column(String, nullable=False)

    images: Mapped[List['Images']] = relationship('Images', uselist=True, back_populates='listing_source')
    prospect_listings: Mapped[List['ProspectListings']] = relationship('ProspectListings', uselist=True, back_populates='listing_source')


class ListingTables(Base):
    __tablename__ = 'listing_tables'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='listing_tables_pkey'),
        {'schema': 'aa'}
    )

    id = mapped_column(SmallInteger)
    name = mapped_column(String, nullable=False)

    images: Mapped[List['Images']] = relationship('Images', uselist=True, back_populates='listing_table')


class Images(Base):
    __tablename__ = 'images'
    __table_args__ = (
        ForeignKeyConstraint(['listing_source_id'], ['aa.listing_sources.id'], name='images_listing_source_id_listing_sources_id_fk'),
        ForeignKeyConstraint(['listing_table_id'], ['aa.listing_tables.id'], name='images_listing_table_id_listing_tables_id_fk'),
        PrimaryKeyConstraint('id', name='images_pkey'),
        Index('idx_images_listing_table_id_listing_id', 'listing_table_id', 'listing_id'),
        {'schema': 'aa'}
    )

    id = mapped_column(Integer)
    listing_table_id = mapped_column(SmallInteger, nullable=False)
    listing_source_id = mapped_column(SmallInteger, nullable=False)
    url = mapped_column(String, nullable=False)
    listing_id = mapped_column(Integer, nullable=False)
    is_primary = mapped_column(Boolean)
    created_at = mapped_column(DateTime(True))

    listing_source: Mapped['ListingSources'] = relationship('ListingSources', back_populates='images')
    listing_table: Mapped['ListingTables'] = relationship('ListingTables', back_populates='images')


class ProspectListings(Base):
    __tablename__ = 'prospect_listings'
    __table_args__ = (
        ForeignKeyConstraint(['listing_source_id'], ['aa.listing_sources.id'], name='prospect_listings_listing_source_id_listing_sources_id_fk'),
        PrimaryKeyConstraint('id', name='prospect_listings_pkey'),
        UniqueConstraint('hash_code', name='prospect_listings_hash_code_unique'),
        Index('idx_prospect_listings_hash_code', 'hash_code'),
        {'schema': 'aa'}
    )

    id = mapped_column(Integer, Sequence('prospect_listings_id_seq', schema='aa'))
    hash_code = mapped_column(CHAR(16), nullable=False)
    listing_source_id = mapped_column(SmallInteger, nullable=False)
    make_and_model = mapped_column(String, nullable=False)
    short_description = mapped_column(String, nullable=False)
    url = mapped_column(String, nullable=False)
    asking_price = mapped_column(Integer, nullable=False)
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
    status_code = mapped_column(CHAR(1))
    ai_listing_summary = mapped_column(String)
    ai_resell_overview = mapped_column(String)
    ai_resell_notes = mapped_column(String)
    ai_buy_price_low = mapped_column(Integer)
    ai_buy_price_high = mapped_column(Integer)

    listing_source: Mapped['ListingSources'] = relationship('ListingSources', back_populates='prospect_listings')
