from sqlalchemy import Boolean, CHAR, Column, Date, DateTime, Index, Integer, PrimaryKeyConstraint, SmallInteger, String, Text, UniqueConstraint
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
    listing_table = mapped_column(ENUM('Prospect', 'Resale', 'SaleItem', name='listing_table', schema='aa'), nullable=False)
    is_primary = mapped_column(Boolean)
    created_at = mapped_column(DateTime(True))


class Lookups(Base):
    __tablename__ = 'lookups'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='lookups_pkey'),
        UniqueConstraint('lookup_type', 'code', name='lookups_type_code_unique'),
        Index('idx_lookups_type_code_unique', 'lookup_type', 'code'),
        {'schema': 'aa'}
    )

    id = mapped_column(Integer)
    lookup_type = mapped_column(String, nullable=False)
    code = mapped_column(String, nullable=False)
    value = mapped_column(String)
    description = mapped_column(String)
    extra = mapped_column(String)


class ProspectListings(Base):
    __tablename__ = 'prospect_listings'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='prospect_listings_pkey'),
        UniqueConstraint('listing_source', 'source_id', name='prospect_listings_listing_source_source_id_unique'),
        Index('idx_prospect_listings_hash_code', 'hash_code'),
        Index('idx_prospect_listings_source_id', 'source_id'),
        {'schema': 'aa'}
    )

    id = mapped_column(Integer)
    hash_code = mapped_column(CHAR(16), nullable=False)
    make_and_model = mapped_column(String, nullable=False)
    short_description = mapped_column(String, nullable=False)
    url = mapped_column(String, nullable=False)
    listing_source = mapped_column(ENUM('Autotrader', 'Car&Classic', 'eBay', 'Facebook', 'Gumtree', 'ManualEntry', 'OnlyVans', 'Pistonheads', name='listing_source', schema='aa'), nullable=False)
    status = mapped_column(ENUM('New', 'NotAvailable', 'Viewed', 'NotInterested', 'Interested', 'Bought', 'Sold', name='prospect_listing_status', schema='aa'), nullable=False)
    source_id = mapped_column(String, nullable=False)
    listing_type = mapped_column(ENUM('Car', 'Van', 'Classic', 'Item', 'Boat', 'Yacht', name='listing_type', schema='aa'), nullable=False)
    created_at = mapped_column(DateTime(True))
    updated_at = mapped_column(DateTime(True))
    full_description = mapped_column(String)
    mileage = mapped_column(Integer)
    mileage_unit = mapped_column(String)
    year = mapped_column(Integer)
    registration = mapped_column(String)
    currency_symbol = mapped_column(CHAR(1))
    asking_price = mapped_column(Integer)
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
    ai_campervan_conversion = mapped_column(String)
    auction_closes = mapped_column(DateTime(True))
    ads_est_buy_price = mapped_column(Integer)
    ads_est_sell_price = mapped_column(Integer)
    ai_target_market = mapped_column(String)
    drive_configuration = mapped_column(String)
    ai_value_add_improvements = mapped_column(String)
    tax_status = mapped_column(String)
    tax_due_date = mapped_column(Date)
    co2_emissions = mapped_column(Integer)
    marked_for_export = mapped_column(Boolean)
    date_of_last_v5c_issued = mapped_column(Date)
    month_of_first_registration = mapped_column(String)
    type_approval = mapped_column(String)
    revenue_weight = mapped_column(Integer)
    status_checked_at = mapped_column(DateTime(True))


class ResaleListings(Base):
    __tablename__ = 'resale_listings'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='resale_listings_pkey'),
        {'schema': 'aa'}
    )

    id = mapped_column(Integer)
    listing_source = mapped_column(ENUM('Autotrader', 'Car&Classic', 'eBay', 'Facebook', 'Gumtree', 'ManualEntry', 'OnlyVans', 'Pistonheads', name='listing_source', schema='aa'), nullable=False)
    status = mapped_column(ENUM('Bought', 'Sold', name='resale_listing_status', schema='aa'), nullable=False)
    make_and_model = mapped_column(String, nullable=False)
    short_description = mapped_column(String, nullable=False)
    listing_type = mapped_column(ENUM('Car', 'Van', 'Classic', 'Item', 'Boat', 'Yacht', name='listing_type', schema='aa'), nullable=False)
    created_at = mapped_column(DateTime(True))
    updated_at = mapped_column(DateTime(True))
    full_description = mapped_column(String)
    mileage = mapped_column(Integer)
    mileage_unit = mapped_column(String)
    year = mapped_column(Integer)
    registration = mapped_column(String)
    currency_symbol = mapped_column(CHAR(1))
    asking_price = mapped_column(Integer)
    vat_status = mapped_column(String)
    location = mapped_column(String)
    drive_configuration = mapped_column(String)
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
    auction_closes = mapped_column(DateTime(True))
    specs_and_features = mapped_column(String)
    tax_status = mapped_column(String)
    tax_due_date = mapped_column(Date)
    co2_emissions = mapped_column(Integer)
    marked_for_export = mapped_column(Boolean)
    date_of_last_v5c_issued = mapped_column(Date)
    month_of_first_registration = mapped_column(String)
    type_approval = mapped_column(String)
    revenue_weight = mapped_column(Integer)
    ai_sell_price_low = mapped_column(Integer)
    ai_sell_price_high = mapped_column(Integer)
    ads_price = mapped_column(Integer)
    eBayUrl = mapped_column(String)
    facebookUrl = mapped_column(String)
    prospect_id = mapped_column(Integer)
    ebay_category_id = mapped_column(String)
    ebay_item_id = mapped_column(String)


class SaleItems(Base):
    __tablename__ = 'sale_items'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='sale_items_pkey'),
        {'schema': 'aa'}
    )

    id = mapped_column(Integer)
    status = mapped_column(ENUM('Inventory', 'Sold', name='sale_listing_status', schema='aa'), nullable=False)
    title = mapped_column(String, nullable=False)
    created_at = mapped_column(DateTime(True))
    updated_at = mapped_column(DateTime(True))
    description = mapped_column(String)
    asking_price = mapped_column(Integer)
    currency_symbol = mapped_column(CHAR(1))
    location = mapped_column(String)
    ebay_category_id = mapped_column(String)
    ebay_item_id = mapped_column(String)
    eBayUrl = mapped_column(String)
    facebookUrl = mapped_column(String)


class SavedSearches(Base):
    __tablename__ = 'saved_searches'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='saved_searches_pkey'),
        UniqueConstraint('user_id', 'query', name='saved_searches_user_id_query_unique'),
        Index('idx_saved_searches_user_id_last_used_at', 'user_id', 'last_used_at'),
        {'schema': 'aa'}
    )

    id = mapped_column(Integer)
    user_id = mapped_column(String, nullable=False)
    query = mapped_column(Text, nullable=False)
    last_used_at = mapped_column(DateTime(True), nullable=False)
