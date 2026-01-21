from typing import List

from sqlalchemy import (
    CHAR,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, declarative_base, mapped_column, relationship
from sqlalchemy.orm.base import Mapped

Base = declarative_base()


class ListingSources(Base):
    __tablename__ = "listing_sources"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="listing_sources_pkey"),
        {"schema": "aa"},
    )

    id = mapped_column(SmallInteger)
    name = mapped_column(String, nullable=False)

    found_listings: Mapped[List["FoundListings"]] = relationship(
        "FoundListings", uselist=True, back_populates="listing_source"
    )
    images: Mapped[List["Images"]] = relationship(
        "Images", uselist=True, back_populates="listing_source"
    )


class ListingTables(Base):
    __tablename__ = "listing_tables"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="listing_tables_pkey"),
        {"schema": "aa"},
    )

    id = mapped_column(SmallInteger)
    name = mapped_column(String, nullable=False)

    images: Mapped[List["Images"]] = relationship(
        "Images", uselist=True, back_populates="listing_table"
    )


class FoundListings(Base):
    __tablename__ = "found_listings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["listing_source_id"],
            ["aa.listing_sources.id"],
            name="found_listings_listing_source_id_listing_sources_id_fk",
        ),
        PrimaryKeyConstraint("id", name="found_listings_pkey"),
        UniqueConstraint("hash_code", name="found_listings_hash_code_unique"),
        Index("idx_found_listings_hash_code", "hash_code"),
        {"schema": "aa"},
    )

    id = mapped_column(Integer)
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
    currency = mapped_column(CHAR(1))
    vat_status = mapped_column(String)
    location = mapped_column(String)
    specs_and_features = mapped_column(String)
    body_type = mapped_column(String)
    cab_type = mapped_column(String)
    fuel_type = mapped_column(String)
    gearbox_type = mapped_column(String)
    wheelbase = mapped_column(String)
    engine_size = mapped_column(String)
    colour = mapped_column(String)
    seats = mapped_column(Integer)
    emmission_class = mapped_column(String)
    number_of_owners = mapped_column(Integer)
    service_history = mapped_column(String)
    basic_history_check = mapped_column(String)
    mot_status = mapped_column(String)
    mot_expiry = mapped_column(Date)
    repairs_anticipated = mapped_column(String)
    repairs_anticipated_cost = mapped_column(Integer)
    recommended_selling_price_low = mapped_column(Integer)
    recommended_selling_price_high = mapped_column(Integer)

    listing_source: Mapped["ListingSources"] = relationship(
        "ListingSources", back_populates="found_listings"
    )


class Images(Base):
    __tablename__ = "images"
    __table_args__ = (
        ForeignKeyConstraint(
            ["listing_source_id"],
            ["aa.listing_sources.id"],
            name="images_listing_source_id_listing_sources_id_fk",
        ),
        ForeignKeyConstraint(
            ["listing_table_id"],
            ["aa.listing_tables.id"],
            name="images_listing_table_id_listing_tables_id_fk",
        ),
        PrimaryKeyConstraint("id", name="images_pkey"),
        {"schema": "aa"},
    )

    id = mapped_column(Integer)
    listing_table_id = mapped_column(SmallInteger, nullable=False)
    listing_source_id = mapped_column(SmallInteger, nullable=False)
    url = mapped_column(String, nullable=False)

    listing_source: Mapped["ListingSources"] = relationship(
        "ListingSources", back_populates="images"
    )
    listing_table: Mapped["ListingTables"] = relationship(
        "ListingTables", back_populates="images"
    )
