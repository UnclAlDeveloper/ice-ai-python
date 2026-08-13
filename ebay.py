import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Generator, Optional, Union

from runtime_flags import resolve_runtime_flags

is_headless = resolve_runtime_flags()

from environments import load_environment
load_environment()

from pydantic import BaseModel, Field, PrivateAttr
from sqlalchemy.orm import sessionmaker

from ebay_rest import API, Error
from ebay_rest.date_time import DateTime

from common import (
    create_engine_with_retry,
    generate_hash_code,
    get_existing_source_ids,
    get_oauth_tokens,
    parse_mileage,
    pause,
)
from listing_images import MAX_GALLERY_IMAGES, cap_gallery_urls
from models.auto_ads import ProspectListings
from models.enums import ListingSource, ListingType, ProspectListingStatus
from scraper_driver import (
    ProxyRotationConfig,
    log_already_exists,
    log_listing_error,
    log_loaded_source_ids,
    log_not_available,
    log_processing_new_listing,
    log_saved_listing,
    log_scrape_finished,
    log_skipping,
    log_timestamp,
    mark_listing_processed_and_check_availability,
    persist_listing_with_images_and_ai,
    run_consent_dismiss_loop,
    update_new_listings_availability as scraper_update_new_listings_availability,
)
from stealth_browser import (
    Page,
    goto_with_captcha_handling,
    launch_stealth_chromium,
    sync_stealth_playwright,
)

EBAY_VANS_CATEGORY_ID = "122202"
EBAY_CLASSICS_CATEGORY_ID = "29751"
EBAY_NON_AUCTION_BUYING_OPTIONS = ["FIXED_PRICE", "CLASSIFIED_AD"]

CONFIG = ProxyRotationConfig.from_env_prefix("EBAY")

UNAVAILABLE_ADVERT_TEXTS = (
    "Bidding ended on",
    "This listing ended on",
    "This listing sold on",
    "This listing was ended",
)


# EBAY SCRAPE CONFIG
@dataclass
class EbayScrapeConfig:
    """
    Parameters that distinguish a vans scrape from a classics scrape while
    sharing the same eBay API search and browser enrichment pipeline.
    """

    listing_type: ListingType
    ai_prompt_filename: str
    category_id: str
    query: Optional[str] = None
    marketplace: str = "GB"
    limit: Optional[int] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    pickup_postal_code: Optional[str] = None
    pickup_radius: Optional[int] = None
    item_location_country: Optional[str] = None
    buying_options: list[str] = field(
        default_factory=lambda: list(EBAY_NON_AUCTION_BUYING_OPTIONS)
    )
    sort: Optional[str] = None
    availability_check_limit: int = 500


# PARSE DATETIME STRING
def parse_datetime_string(value: str) -> datetime:
    """
    Parse a datetime string in eBay Z format or ISO 8601 with a timezone offset.
    Always returns a UTC timezone-aware datetime.
    """

    try:
        return DateTime.from_string(value)
    except Error:
        pass

    # iso 8601 with +00:00 or a trailing Z
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# FORMAT EBAY DATETIME STRING
def format_ebay_datetime_string(value: Union[datetime, str]) -> str:
    """
    Format a datetime or string for the ebay_rest API (millisecond precision, Z suffix).
    Accepts eBay Z strings and ISO 8601 strings with a timezone offset.
    """

    if isinstance(value, str):
        dt = parse_datetime_string(value)
    else:
        dt = value

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return DateTime.to_string(dt)


# PARSE AUCTION CLOSE DATETIME
def parse_auction_close_datetime(text: str) -> Optional[datetime]:
    """
    Parse auction close datetime from eBay timer format (dd/mm, hh:mi).
    Returns a datetime or None if parsing fails. Infers year from current date.
    """

    try:
        parts = text.split(",")
        if len(parts) != 2:
            return None
        date_part = parts[0].strip()
        time_part = parts[1].strip()
        day, month = map(int, date_part.split("/"))
        hour, minute = map(int, time_part.split(":"))
        now = datetime.now()
        parsed = datetime(now.year, month, day, hour, minute, 0, 0)
        if parsed < now:
            parsed = datetime(now.year + 1, month, day, hour, minute, 0, 0)
        return parsed
    except (ValueError, IndexError):
        return None


# BUILD SEARCH FILTERS
def build_search_filters(
    *,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    pickup_postal_code: Optional[str] = None,
    pickup_radius: Optional[int] = None,
    item_location_country: Optional[str] = None,
    buying_options: Optional[list[str]] = None,
) -> list[str]:
    """
    Build eBay Browse API filter strings for price, location, buying format,
    local pickup constraints, and private sellers only. Raises ValueError when
    only one pickup parameter is provided.
    """

    filters: list[str] = ["sellerAccountTypes:{INDIVIDUAL}"]
    if min_price is not None or max_price is not None:
        price_min = min_price if min_price is not None else 0
        price_max = max_price if max_price is not None else ""
        filters.append(f"price:[{price_min}..{price_max}]")

    if item_location_country is not None:
        filters.append(f"itemLocationCountry:{item_location_country}")

    resolved_buying_options = (
        buying_options
        if buying_options is not None
        else EBAY_NON_AUCTION_BUYING_OPTIONS
    )
    if resolved_buying_options:
        options = "|".join(resolved_buying_options)
        filters.append(f"buyingOptions:{{{options}}}")

    if pickup_postal_code is not None and pickup_radius is not None:
        filters.append(f"pickupPostalCode:{pickup_postal_code}")
        filters.append(f"pickupRadius:{pickup_radius}")
        filters.append("pickupCountry:GB")
        filters.append("pickupRadiusUnit:mi")
        filters.append("deliveryOptions:{SELLER_ARRANGED_LOCAL_PICKUP}")
    elif pickup_postal_code is not None or pickup_radius is not None:
        raise ValueError(
            "For local pickup searches, both pickup_postal_code and pickup_radius "
            "must be provided"
        )

    return filters


# PARSE ASKING PRICE
def parse_asking_price(price_value: object) -> Optional[int]:
    """
    Convert an eBay Browse API price value to an integer pounds/dollars amount.
    Classified and incomplete Browse records may omit a price; those become None.
    """

    if price_value is None or price_value == "":
        return None
    if isinstance(price_value, str):
        return int(float(price_value.replace(",", "")))
    return int(float(price_value))


# CURRENCY SYMBOL FROM CODE
def currency_symbol_from_code(currency: str) -> str:
    """
    Map an ISO currency code from the Browse API to a display symbol.
    """

    if currency == "GBP":
        return "£"
    if currency == "USD":
        return "$"
    return ""


# BUILD MAKE AND MODEL
def build_make_and_model(
    details: dict,
    *,
    title: str,
) -> str:
    """
    Build make_and_model from extracted item specifics, falling back to the title.
    """

    make = details.get("make") or ""
    model = details.get("model") or ""
    if make or model:
        return f"{make} {model}".strip()
    if make:
        return make
    if model:
        return model
    return title


# IS AUCTION LISTING
def is_auction_listing(listing: dict, *, details: Optional[dict] = None) -> bool:
    """
    Return True when the Browse API record or scraped page details indicate an
    auction listing rather than fixed price or classified ad.
    """

    buying_options = listing.get("buying_options") or listing.get("buyingOptions") or []
    if "AUCTION" in buying_options:
        return True
    if details is not None and details.get("auction_closes") is not None:
        return True
    return False


# EXTRACT SOURCE ID
def extract_source_id(url: str | None) -> str | None:
    """
    Extract the eBay item number from a listing URL such as
    https://www.ebay.co.uk/itm/800028133156 or
    https://www.ebay.co.uk/itm/some-title/800028133156. Returns None when the
    URL does not contain a numeric /itm/ id.
    """

    match = re.search(r"/itm/(?:[^/?#]+/)?(\d+)", url or "")
    return match.group(1) if match else None


# BUILD PROSPECT LISTING
def build_prospect_listing(
    *,
    listing: dict,
    details: dict,
    listing_type: ListingType,
    hash_code: str,
    make_and_model: str,
    current_datetime: datetime,
) -> ProspectListings:
    """
    Build a ProspectListings row from a Browse API record and page-extracted details.
    """

    title = listing.get("title", "")
    item_web_url = listing.get("item_web_url", "")
    price_info = listing.get("price") or {}
    asking_price_raw = price_info.get("value")
    currency = price_info.get("currency", "")
    item_location = listing.get("item_location") or {}
    location = item_location.get("city", "")
    source_id = extract_source_id(item_web_url) or listing.get("item_id")

    return ProspectListings(
        hash_code=hash_code,
        source_id=source_id,
        listing_source=ListingSource.EBAY,
        listing_type=listing_type,
        status=ProspectListingStatus.NEW,
        short_description=title,
        full_description=details.get("full_description"),
        url=item_web_url,
        asking_price=parse_asking_price(asking_price_raw),
        currency_symbol=currency_symbol_from_code(currency),
        location=location,
        make_and_model=make_and_model,
        body_type=details.get("body_type"),
        engine_size=details.get("engine_size"),
        fuel_type=details.get("fuel_type"),
        gearbox_type=details.get("transmission"),
        year=details.get("year"),
        colour=details.get("colour"),
        mileage=details.get("mileage"),
        mileage_unit=details.get("mileage_unit"),
        auction_closes=details.get("auction_closes"),
        created_at=current_datetime,
        updated_at=current_datetime,
    )


# IS EBAY COOKIE CONSENT VISIBLE
def _is_ebay_cookie_consent_visible(page: Page) -> bool:
    """
    Return True when the eBay GDPR cookie banner or its accept button is present.
    """

    if page.query_selector("#gdpr-banner-accept") is not None:
        return True
    return page.query_selector("#gdpr-banner") is not None


# CLICK EBAY COOKIE CONSENT DISMISS
def _click_ebay_cookie_consent_dismiss(page: Page) -> bool:
    """
    Click 'Accept all' on the eBay GDPR cookie banner when the button is present.
    """

    accept_btn = page.query_selector("#gdpr-banner-accept")
    if accept_btn is None:
        return False

    accept_btn.click()
    return True


# DISMISS EBAY COOKIE CONSENT
def dismiss_ebay_cookie_consent(page: Page, *, wait_for_banner: bool = False) -> None:
    """
    Dismiss the eBay GDPR cookie banner using the shared consent dismiss loop.
    """

    run_consent_dismiss_loop(
        is_visible=lambda: _is_ebay_cookie_consent_visible(page),
        click_dismiss=lambda: _click_ebay_cookie_consent_dismiss(page),
        wait_attached=lambda: page.wait_for_selector(
            "#gdpr-banner-accept", state="attached", timeout=5000
        ),
        wait_for_banner=wait_for_banner,
        timeout=10000,
    )


# ACCEPT EBAY COOKIE CONSENT IF PRESENT
def accept_ebay_cookie_consent_if_present(page: Page) -> None:
    """
    Dismiss the eBay GDPR cookie banner when visible. Kept for compatibility with
    older call sites; prefer dismiss_ebay_cookie_consent.
    """

    dismiss_ebay_cookie_consent(page, wait_for_banner=False)


# IS LISTING NO LONGER AVAILABLE
def is_listing_no_longer_available(page: Page) -> bool:
    """
    Return True when the listing page shows that bidding or the listing has ended.
    """

    for text in UNAVAILABLE_ADVERT_TEXTS:
        if page.get_by_text(text, exact=False).count() > 0:
            return True
    return False


# EBAY IS UNAVAILABLE
def _ebay_is_unavailable(page: Page) -> bool:
    """
    Accept cookie consent when needed, then detect ended eBay listing pages.
    """

    dismiss_ebay_cookie_consent(page)
    return is_listing_no_longer_available(page)


# EBAY DOWNLOADER
class EbayDownloader(BaseModel):
    """
    Download and process eBay listings via the Browse API and browser enrichment.
    Holds configuration as model fields and browser/API state as private attributes.
    """

    listing_type: ListingType
    ai_prompt_filename: str
    marketplace: str = "GB"
    query: Optional[str] = None
    category_id: str = EBAY_VANS_CATEGORY_ID
    limit: Optional[int] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    pickup_postal_code: Optional[str] = None
    pickup_radius: Optional[int] = None
    item_location_country: Optional[str] = None
    buying_options: list[str] = Field(
        default_factory=lambda: list(EBAY_NON_AUCTION_BUYING_OPTIONS)
    )
    sort: Optional[str] = None
    availability_check_limit: int = 500

    _api: Optional[API] = PrivateAttr(default=None)
    _browser: Optional[object] = PrivateAttr(default=None)
    _page: Optional[object] = PrivateAttr(default=None)

    # FROM CONFIG
    @classmethod
    def from_config(cls, config: EbayScrapeConfig) -> "EbayDownloader":
        """
        Build an EbayDownloader from a scrape config dataclass.
        """

        return cls(
            listing_type=config.listing_type,
            ai_prompt_filename=config.ai_prompt_filename,
            marketplace=config.marketplace,
            query=config.query,
            category_id=config.category_id,
            limit=config.limit,
            min_price=config.min_price,
            max_price=config.max_price,
            pickup_postal_code=config.pickup_postal_code,
            pickup_radius=config.pickup_radius,
            item_location_country=config.item_location_country,
            buying_options=config.buying_options,
            sort=config.sort,
            availability_check_limit=config.availability_check_limit,
        )

    def _get_api(self) -> API:
        """
        Return cached eBay API instance, creating it with current marketplace if needed.
        """

        if self._api is None:
            self._api = self._create_api()
        return self._api

    def _create_api(self) -> API:
        """
        Create and return an eBay API instance configured with credentials from environment variables.
        Uses the Browse API for searching listings without requiring user authentication.
        """

        # load credentials from environment variables
        client_id = os.getenv("AUTO_ADS_EBAY_CLIENT_ID")
        dev_id = os.getenv("AUTO_ADS_EBAY_DEV_ID")
        client_secret = os.getenv("AUTO_ADS_EBAY_CLIENT_SECRET")
        redirect_uri = os.getenv("AUTO_ADS_EBAY_REDIRECT_URL")

        if not all([client_id, dev_id, client_secret, redirect_uri]):
            raise ValueError(
                "Missing required eBay credentials. Ensure AUTO_ADS_EBAY_CLIENT_ID, "
                "AUTO_ADS_EBAY_DEV_ID, AUTO_ADS_EBAY_CLIENT_SECRET, and "
                "AUTO_ADS_EBAY_REDIRECT_URL are set."
            )

        # configure application credentials
        application = {
            "app_id": client_id,
            "cert_id": client_secret,
            "dev_id": dev_id,
            "redirect_uri": redirect_uri,
        }

        # configure marketplace header for UK
        if self.marketplace == "GB":
            header = {
                "accept_language": "en-GB",
                "affiliate_campaign_id": "",
                "affiliate_reference_id": "",
                "content_language": "en-GB",
                "country": "GB",
                "currency": "GBP",
                "device_id": "",
                "marketplace_id": "EBAY_GB",
                "zip": "SW1A 1AA",
            }
        elif self.marketplace == "US":
            header = {
                "accept_language": "en-US",
                "affiliate_campaign_id": "",
                "affiliate_reference_id": "",
                "content_language": "en-US",
                "country": "US",
                "currency": "USD",
                "device_id": "",
                "marketplace_id": "EBAY_US",
                "zip": "20500",
            }
        else:
            raise ValueError(f"Unsupported marketplace: {self.marketplace}")

        # load refresh token from the database if available
        tokens = get_oauth_tokens("ebay")
        refresh_token = tokens.refresh_token if tokens else ""
        refresh_token_expiry = ""
        if tokens and tokens.refresh_token_expiry:
            refresh_token_expiry = format_ebay_datetime_string(tokens.refresh_token_expiry)

        # user configuration with tokens from database
        user = {
            "email_or_username": os.getenv("AUTO_ADS_EBAY_USERNAME"),
            "password": os.getenv("AUTO_ADS_EBAY_PASSWORD"),
            "refresh_token": refresh_token or "",
            "refresh_token_expiry": refresh_token_expiry,
        }

        return API(application=application, user=user, header=header)

    def print_category_suggestions(self) -> list[dict]:
        """
        Get suggested eBay categories for the configured query using the Taxonomy API.
        Prints and returns a list of category suggestions sorted by relevance.
        """

        if not self.query:
            raise ValueError("A query is required to request category suggestions")

        api = self._get_api()
        marketplace_id = f"EBAY_{self.marketplace}"

        # get the category tree id for the marketplace
        tree_response = api.commerce_taxonomy_get_default_category_tree_id(marketplace_id)
        category_tree_id = tree_response.get("category_tree_id")

        if not category_tree_id:
            raise ValueError(f"Could not get category tree ID for marketplace: {marketplace_id}")

        # call the category suggestions api
        response = api.commerce_taxonomy_get_category_suggestions(
            category_tree_id=category_tree_id,
            q=self.query,
        )

        suggestions = response.get("category_suggestions", [])

        print(f"\nCategory suggestions for '{self.query}':")
        print("-" * 50)
        for i, suggestion in enumerate(suggestions, 1):
            category = suggestion.get("category", {})
            category_id_val = category.get("category_id", "N/A")
            category_name = category.get("category_name", "N/A")

            ancestors = suggestion.get("category_tree_node_ancestors", [])
            path_parts = [a.get("category_name", "") for a in reversed(ancestors)]
            path_parts.append(category_name)
            full_path = " > ".join(path_parts)

            print(f"{i}. {category_name} (ID: {category_id_val})")
            print(f"   Path: {full_path}")

        return suggestions

    def search_listings(self) -> Generator[dict, None, None]:
        """
        Search for listings on eBay using the Browse API.
        Yields individual listing records as dictionaries and handles pagination.
        """

        api = self._get_api()
        filters = build_search_filters(
            min_price=self.min_price,
            max_price=self.max_price,
            pickup_postal_code=self.pickup_postal_code,
            pickup_radius=self.pickup_radius,
            item_location_country=self.item_location_country,
            buying_options=self.buying_options,
        )

        search_params: dict = {
            "category_ids": self.category_id,
        }
        if self.query:
            search_params["q"] = self.query
        if filters:
            search_params["filter"] = ",".join(filters)
        if self.limit is not None:
            search_params["limit"] = self.limit
        if self.sort is not None:
            search_params["sort"] = self.sort

        records_yielded = 0
        for record in api.buy_browse_search(**search_params):
            if "record" in record:
                records_yielded += 1
                yield record["record"]
            elif "total" in record:
                total_info = record.get("total", {})
                print(f"Total records available: {total_info.get('records_available', 'unknown')}")

        print(f"Total records yielded: {records_yielded}")

    def _extract_listing_details(self) -> dict:
        """
        Extract the seller description and item specifics from the current eBay listing page.
        """

        details = {
            "full_description": None,
            "body_type": None,
            "engine_size": None,
            "make": None,
            "model": None,
            "fuel_type": None,
            "transmission": None,
            "year": None,
            "colour": None,
            "mileage": None,
            "mileage_unit": None,
            "auction_closes": None,
        }

        if self._page is None:
            return details

        page = self._page

        try:
            description_iframe = page.query_selector("iframe#desc_ifr")
            if description_iframe:
                frame = description_iframe.content_frame()
                if frame:
                    body = frame.query_selector("body")
                    if body:
                        details["full_description"] = body.inner_text().strip()
            else:
                desc_container = page.query_selector(
                    '[data-testid="ux-layout-section-module__content"]'
                )
                if desc_container:
                    details["full_description"] = desc_container.inner_text().strip()

            label_value_pairs = []
            for section_selector in (
                ".ux-layout-section-evo.ux-layout-section--features",
                '[data-testid="ux-layout-section-evo__item-specifics"]',
                ".ux-layout-section-evo__item-specifics",
            ):
                sections = page.query_selector_all(section_selector)
                for section in sections:
                    for dl in section.query_selector_all("dl.ux-labels-values"):
                        label_value_pairs.append(dl)
                if label_value_pairs:
                    break

            specs_mapping = {
                "Body Type": "body_type",
                "Engine Size": "engine_size",
                "Make": "make",
                "Manufacturer": "make",
                "Model": "model",
                "Fuel Type": "fuel_type",
                "Transmission": "transmission",
                "Model Year": "year",
                "Colour": "colour",
            }

            for dl in label_value_pairs:
                label_el = dl.query_selector(".ux-labels-values__labels-content")
                value_el = dl.query_selector(".ux-labels-values__values-content")
                if label_el and value_el:
                    label_text = label_el.inner_text().strip()
                    value_text = value_el.inner_text().strip()
                    if label_text in specs_mapping:
                        key = specs_mapping[label_text]
                        if key == "year":
                            try:
                                details[key] = int(value_text)
                            except ValueError:
                                pass
                        else:
                            details[key] = value_text
                    elif label_text == "Mileage":
                        mileage, mileage_unit = parse_mileage(value_text)
                        details["mileage"] = mileage
                        details["mileage_unit"] = mileage_unit

            timer_el = page.query_selector("span.ux-timer__time-left")
            if timer_el:
                time_left_text = timer_el.inner_text().strip()
                if time_left_text:
                    parsed = parse_auction_close_datetime(time_left_text)
                    if parsed:
                        details["auction_closes"] = parsed

        except Exception as e:
            print(f"  Warning: Could not extract some listing details: {e}")

        return details

    # EXTRACT SELLER TYPE
    def _extract_seller_type(self) -> Optional[str]:
        """
        Extract the seller type (Private or Business) from the current eBay listing page.
        """

        if self._page is None:
            return None

        page = self._page

        try:
            seller_type_elements = page.query_selector_all(
                ".x-sellercard-atf__about-seller-item span.ux-textspans.ux-textspans--SECONDARY"
            )

            for element in seller_type_elements:
                text = element.inner_text().strip()
                if text == "Private":
                    return "Private"
                if text == "Business":
                    return "Business"

            seller_info_section = page.query_selector(".x-sellercard-atf__about-seller")
            if seller_info_section:
                all_text = seller_info_section.inner_text()
                if "Private" in all_text:
                    return "Private"
                if "Business" in all_text:
                    return "Business"

        except Exception as e:
            print(f"  Warning: Could not extract seller type: {e}")

        return None

    def _extract_listing_images(self) -> list[str]:
        """
        Iterate through the eBay listing page to collect image URLs, stopping
        once MAX_GALLERY_IMAGES have been found.
        """

        if self._page is None:
            return []

        page = self._page
        image_urls: list[str] = []

        try:
            photos_container = page.query_selector('[data-testid="x-photos-min-view"]')
            if not photos_container:
                return []

            thumb_buttons = page.query_selector_all(
                "div.ux-image-grid button.ux-image-grid-item"
            )
            if not thumb_buttons:
                return []

            for idx in range(len(thumb_buttons)):
                if len(image_urls) >= MAX_GALLERY_IMAGES:
                    break

                thumb = page.query_selector(
                    f'button.ux-image-grid-item[data-idx="{idx}"]'
                )
                if thumb:
                    thumb.click()
                    pause(0.3, 0.6)

                active_img = page.query_selector(
                    "div.ux-image-carousel-item.active img"
                )
                if active_img:
                    url = active_img.get_attribute("data-zoom-src") or active_img.get_attribute(
                        "src"
                    )
                    if url and url not in image_urls:
                        image_urls.append(url)

            if not image_urls:
                grid_imgs = page.query_selector_all("div.ux-image-grid img[src]")
                for img in grid_imgs:
                    if len(image_urls) >= MAX_GALLERY_IMAGES:
                        break
                    src = img.get_attribute("src")
                    if src:
                        full_url = src.replace("/s-l140.webp", "/s-l1600.webp")
                        if full_url not in image_urls:
                            image_urls.append(full_url)

        except Exception as e:
            print(f"  Warning: Could not extract listing images: {e}")

        return cap_gallery_urls(image_urls)

    def _visit_listing_page(
        self, listing_url: str, *, title: str
    ) -> tuple[dict, list[str], str]:
        """
        Navigate to a listing, dismiss consent, validate seller type, and extract
        page details and image urls. Raises on navigation or validation failure.
        """

        goto_with_captcha_handling(self._page, listing_url)
        pause(0.1, 0.5)
        dismiss_ebay_cookie_consent(self._page, wait_for_banner=True)

        seller_type = self._extract_seller_type()
        if seller_type != "Private":
            raise ValueError(f"seller is {seller_type} (not Private)")

        if is_listing_no_longer_available(self._page):
            raise ValueError("listing unavailable")

        details = self._extract_listing_details()
        image_urls = self._extract_listing_images()
        make_and_model = build_make_and_model(details, title=title)
        return details, image_urls, make_and_model

    def download_all_listings(self) -> list[dict]:
        """
        Download all listings from eBay for the configured marketplace and listing type.
        Each listing URL is visited in a browser to extract details, then persisted.
        """

        existing_source_ids = get_existing_source_ids(ListingSource.EBAY)
        log_loaded_source_ids(len(existing_source_ids))

        database_url = os.getenv("AUTO_ADS_DATABASE_URL")
        engine = create_engine_with_retry(database_url)
        SessionLocal = sessionmaker(bind=engine)

        print(f"Initializing eBay API for marketplace: {self.marketplace}")
        self._get_api()

        query_label = f"'{self.query}' " if self.query else ""
        sort_label = f", sort={self.sort}" if self.sort else ""
        print(
            f"Searching for {query_label}listings in category "
            f"(ID: {self.category_id}) for {self.listing_type.value}{sort_label}..."
        )
        listings: list[dict] = []
        saved_count = 0
        processed_ids: set[str] = set()
        listing_index = 0

        try:
            with sync_stealth_playwright() as p:
                self._browser = launch_stealth_chromium(
                    p, headless=is_headless, use_proxy=False
                )
                self._page = self._browser.new_page()

                scraper_update_new_listings_availability(
                    self._page,
                    listing_source=ListingSource.EBAY,
                    is_unavailable_fn=_ebay_is_unavailable,
                    limit=self.availability_check_limit,
                    config=CONFIG,
                    listing_type=self.listing_type,
                )

                with SessionLocal() as session:
                    for listing in self.search_listings():
                        listing_index += 1
                        title = listing.get("title", "")
                        item_web_url = listing.get("item_web_url")
                        source_id = (
                            extract_source_id(item_web_url) or listing.get("item_id")
                        )
                        hash_code = generate_hash_code(title)
                        title_short = title[:50] if title else "Unknown"
                        card_id = source_id or hash_code

                        if source_id is not None and source_id in existing_source_ids:
                            log_already_exists(listing_index, title_short)
                            continue

                        if not item_web_url:
                            log_skipping(listing_index, title_short, "missing listing url")
                            continue

                        if is_auction_listing(listing):
                            log_skipping(listing_index, title_short, "auction listing")
                            continue

                        log_processing_new_listing(
                            listing_index,
                            title_short,
                            item_web_url,
                            source_id=source_id,
                        )

                        try:
                            details, image_urls, make_and_model = self._visit_listing_page(
                                item_web_url,
                                title=title,
                            )
                        except ValueError as error:
                            message = str(error)
                            if message == "listing unavailable":
                                log_not_available(listing_index, title_short, "unavailable")
                            elif "not Private" in message:
                                log_skipping(listing_index, title_short, message)
                            else:
                                log_listing_error(listing_index, title_short, error)
                            continue
                        except Exception as error:
                            log_listing_error(listing_index, title_short, error)
                            continue

                        if is_auction_listing(listing, details=details):
                            log_skipping(listing_index, title_short, "auction listing")
                            continue

                        current_datetime = datetime.now()
                        prospect_listing = build_prospect_listing(
                            listing=listing,
                            details=details,
                            listing_type=self.listing_type,
                            hash_code=hash_code,
                            make_and_model=make_and_model,
                            current_datetime=current_datetime,
                        )

                        def on_after_persist(saved: ProspectListings) -> None:
                            log_saved_listing(
                                listing_index,
                                title_short,
                                make_and_model=saved.make_and_model,
                                year=saved.year,
                                location=saved.location,
                                image_count=len(image_urls),
                            )

                        persist_listing_with_images_and_ai(
                            session,
                            prospect_listing,
                            ai_prompt_filename=self.ai_prompt_filename,
                            listing_url=item_web_url,
                            image_urls=image_urls,
                            page=self._page,
                            temp_dir_prefix="ebay_images_",
                            on_after_persist=on_after_persist,
                            log_index=listing_index,
                        )

                        log_timestamp()

                        listings.append(listing)
                        saved_count += 1

                        mark_listing_processed_and_check_availability(
                            self._page,
                            source_id=source_id,
                            card_id=card_id,
                            existing_source_ids=existing_source_ids,
                            processed_ids=processed_ids,
                            listing_source=ListingSource.EBAY,
                            listing_type=self.listing_type,
                            is_unavailable_fn=_ebay_is_unavailable,
                            config=CONFIG,
                            deadline=None,
                        )

                        if saved_count % 100 == 0:
                            print(f"Downloaded {saved_count} listings...")

                self._browser.close()
                self._browser = None
                self._page = None

        except Error as error:
            print(f"eBay API Error {error.number}: {error.reason}")
            if error.detail:
                print(f"Detail: {error.detail}")
            raise

        log_scrape_finished(1, saved_count, processed_count=listing_index)
        return listings

    @staticmethod
    def print_listing_summary(listing: dict) -> None:
        """
        Print a summary of a single listing to the console.
        """

        item_id = listing.get("item_id", "N/A")
        title = listing.get("title", "N/A")
        price_info = listing.get("price", {})
        price = f"{price_info.get('currency', '')} {price_info.get('value', 'N/A')}"
        condition = listing.get("condition", "N/A")
        location = listing.get("item_location", {}).get("city", "N/A")

        print(f"ID: {item_id}")
        print(f"Title: {title}")
        print(f"Price: {price}")
        print(f"Condition: {condition}")
        print(f"Location: {location}")
        print("-" * 50)


# RUN EBAY
def run_ebay(config: EbayScrapeConfig) -> list[dict]:
    """
    Run an eBay listings download for the given vans or classics configuration.
    """

    downloader = EbayDownloader.from_config(config)
    return downloader.download_all_listings()
