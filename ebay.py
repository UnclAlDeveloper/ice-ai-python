import os
from environments import load_environment
load_environment()

import shutil
from datetime import datetime
from typing import Generator, Optional

from pydantic import BaseModel, PrivateAttr
from playwright.sync_api import sync_playwright
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from common import generate_hash_code, get_existing_hash_codes, pause
from listing_images import download_and_save_listing_images
from models.auto_ads import ProspectListings
from ai_analysis import process_ai_analysis_for_listing
from models.enums import ListingSource, ProspectListingStatus

from ebay_rest import API, Error


# EBAY DOWNLOADER
class EbayDownloader(BaseModel):
    """
    Download and process van listings from eBay. Holds configuration as model fields
    and browser/API state as private attributes. Use as a context for consistent
    marketplace, search, and filter settings across operations.
    """

    marketplace: str = "GB"
    query: str = "van"
    category_id: str = "122202"
    limit: Optional[int] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    pickup_postal_code: Optional[str] = None
    pickup_radius: Optional[int] = None

    _api: Optional[API] = PrivateAttr(default=None)
    _browser: Optional[object] = PrivateAttr(default=None)
    _page: Optional[object] = PrivateAttr(default=None)

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

        if not all([client_id, dev_id, client_secret]):
            raise ValueError(
                "Missing required eBay credentials. Ensure AUTO_ADS_EBAY_CLIENT_ID, "
                "AUTO_ADS_EBAY_DEV_ID, and AUTO_ADS_EBAY_CLIENT_SECRET are set."
            )

        # configure application credentials
        application = {
            "app_id": client_id,
            "cert_id": client_secret,
            "dev_id": dev_id,
            "redirect_uri": "http://www.mindlessinvesting.com:8005/auto-ads-ebay-redirect",
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

        # user configuration for application-only access
        user = {
            "email_or_username": os.getenv("AUTO_ADS_EBAY_USERNAME"),
            "password": os.getenv("AUTO_ADS_EBAY_PASSWORD"),
            "refresh_token": "",
            "refresh_token_expiry": "",
        }

        # create and return the api instance
        api = API(application=application, user=user, header=header)

        return api

    def print_category_suggestions(self) -> list[dict]:
        """
        Get suggested eBay categories for the configured query using the Taxonomy API.
        Prints and returns a list of category suggestions sorted by relevance.
        """

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

        # extract the suggestions
        suggestions = response.get("category_suggestions", [])

        # print the suggestions
        print(f"\nCategory suggestions for '{self.query}':")
        print("-" * 50)
        for i, suggestion in enumerate(suggestions, 1):
            category = suggestion.get("category", {})
            category_id_val = category.get("category_id", "N/A")
            category_name = category.get("category_name", "N/A")

            # build the category path from ancestors
            ancestors = suggestion.get("category_tree_node_ancestors", [])
            path_parts = [a.get("category_name", "") for a in reversed(ancestors)]
            path_parts.append(category_name)
            full_path = " > ".join(path_parts)

            print(f"{i}. {category_name} (ID: {category_id_val})")
            print(f"   Path: {full_path}")

        return suggestions

    def search_van_listings(self) -> Generator[dict, None, None]:
        """
        Search for van listings on eBay using the Browse API.
        Yields individual listing records as dictionaries.
        Handles pagination automatically to fetch all available listings.

        For local pickup searches, both pickup parameters must be provided together:
        - pickup_postal_code: The postal/zip code for local pickup location
        - pickup_radius: The search radius in miles from the postal code
        """

        api = self._get_api()

        # build filter string for additional constraints
        filters = []
        if self.min_price is not None or self.max_price is not None:
            price_min = self.min_price if self.min_price is not None else 0
            price_max = self.max_price if self.max_price is not None else ""
            filters.append(f"price:[{price_min}..{price_max}]")

        # add local pickup filters if both required parameters are provided
        if self.pickup_postal_code is not None and self.pickup_radius is not None:
            filters.append(f"pickupPostalCode:{self.pickup_postal_code}")
            filters.append(f"pickupRadius:{self.pickup_radius}")
            filters.append("pickupCountry:GB")
            filters.append("pickupRadiusUnit:mi")
            filters.append("deliveryOptions:{SELLER_ARRANGED_LOCAL_PICKUP}")
        elif self.pickup_postal_code is not None or self.pickup_radius is not None:
            raise ValueError(
                "For local pickup searches, both pickup_postal_code and pickup_radius must be provided"
            )

        # prepare search parameters - q parameter is required by the api
        search_params = {
            "q": self.query,
            "category_ids": self.category_id,
        }

        # add filter string if there are any filters
        if filters:
            search_params["filter"] = ",".join(filters)

        # only set limit if specified - the API handles pagination automatically
        if self.limit is not None:
            search_params["limit"] = self.limit

        # execute search and yield results
        # the API generator handles pagination automatically, so we just iterate through it
        records_yielded = 0
        for record in api.buy_browse_search(**search_params):
            if "record" in record:
                records_yielded += 1
                yield record["record"]
            elif "total" in record:
                # metadata record with totals
                total_info = record.get("total", {})
                print(f"Total records available: {total_info.get('records_available', 'unknown')}")

        print(f"Total records yielded: {records_yielded}")

    def _accept_cookie_consent_if_present(self) -> None:
        """
        Click 'Accept all' on the eBay GDPR cookie banner if visible, then wait
        until the banner has disappeared. No-op if the banner is not present.
        """

        accept_btn = self._page.query_selector("#gdpr-banner-accept")
        if accept_btn:
            accept_btn.click()
            self._page.wait_for_selector("#gdpr-banner", state="hidden", timeout=10000)

    def _extract_listing_details(self) -> dict:
        """
        Extract the seller description and item specifics from the current eBay listing page.
        Returns a dictionary with full_description and item specifics fields.
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
            "auction_closes": None,
        }

        if self._page is None:
            return details

        page = self._page

        try:
            # extract the seller description from iframe or direct content
            description_iframe = page.query_selector('iframe#desc_ifr')
            if description_iframe:
                # get content from iframe
                frame = description_iframe.content_frame()
                if frame:
                    body = frame.query_selector('body')
                    if body:
                        details["full_description"] = body.inner_text().strip()
            else:
                # try direct description container
                desc_container = page.query_selector('[data-testid="ux-layout-section-module__content"]')
                if desc_container:
                    details["full_description"] = desc_container.inner_text().strip()

            # extract item specifics - try multiple layouts (legacy and evo)
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

            # extract auction close time from timer module if present (auctions only)
            timer_el = page.query_selector("span.ux-timer__time-left")
            if timer_el:
                time_left_text = timer_el.inner_text().strip()
                if time_left_text:
                    parsed = self._parse_auction_close_datetime(time_left_text)
                    if parsed:
                        details["auction_closes"] = parsed

        except Exception as e:
            print(f"  Warning: Could not extract some listing details: {e}")

        return details

    # _EXTRACT_SELLER_TYPE
    def _extract_seller_type(self) -> Optional[str]:
        """
        Extract the seller type (Private or Business) from the current eBay listing page.
        Returns "Private" if the seller is a private seller, "Business" if a business,
        or None if the seller type cannot be determined.
        """

        if self._page is None:
            return None

        page = self._page

        try:
            # look for the seller type span within the seller card
            seller_type_elements = page.query_selector_all(
                '.x-sellercard-atf__about-seller-item span.ux-textspans.ux-textspans--SECONDARY'
            )
            
            for element in seller_type_elements:
                text = element.inner_text().strip()
                # check if this element contains "Private" or "Business"
                if text == "Private":
                    return "Private"
                elif text == "Business":
                    return "Business"
            
            # alternative selector: direct search for the text
            seller_info_section = page.query_selector('.x-sellercard-atf__about-seller')
            if seller_info_section:
                all_text = seller_info_section.inner_text()
                if "Private" in all_text:
                    return "Private"
                elif "Business" in all_text:
                    return "Business"

        except Exception as e:
            print(f"  Warning: Could not extract seller type: {e}")

        return None

    # _PARSE_AUCTION_CLOSE_DATETIME
    def _parse_auction_close_datetime(self, text: str) -> Optional[datetime]:
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

    def _extract_listing_images(self) -> list[str]:
        """
        Iterate through the eBay listing page to collect all image URLs. Clicks each
        thumbnail in the filmstrip to ensure lazy-loaded images are available, then
        collects the full-size URL from the carousel (data-zoom-src or src).
        """

        if self._page is None:
            return []

        page = self._page
        image_urls: list[str] = []

        try:
            # wait for photos container to be visible
            photos_container = page.query_selector('[data-testid="x-photos-min-view"]')
            if not photos_container:
                return []

            # get all thumbnail buttons in the grid
            thumb_buttons = page.query_selector_all(
                'div.ux-image-grid button.ux-image-grid-item'
            )
            if not thumb_buttons:
                return []

            # iterate through each thumbnail by clicking it
            for idx in range(len(thumb_buttons)):
                # click the thumbnail for this index
                thumb = page.query_selector(
                    f'button.ux-image-grid-item[data-idx="{idx}"]'
                )
                if thumb:
                    thumb.click()
                    pause(0.3, 0.6)

                # get current active image from carousel (full-size preferred)
                active_img = page.query_selector(
                    'div.ux-image-carousel-item.active img'
                )
                if active_img:
                    url = active_img.get_attribute("data-zoom-src") or active_img.get_attribute("src")
                    if url and url not in image_urls:
                        image_urls.append(url)

            # fallback: if no urls from carousel, collect from grid thumbnails
            if not image_urls:
                grid_imgs = page.query_selector_all('div.ux-image-grid img[src]')
                for img in grid_imgs:
                    src = img.get_attribute("src")
                    if src:
                        # upgrade to full-size (s-l140 -> s-l1600)
                        full_url = src.replace("/s-l140.webp", "/s-l1600.webp")
                        if full_url not in image_urls:
                            image_urls.append(full_url)

        except Exception as e:
            print(f"  Warning: Could not extract listing images: {e}")

        return image_urls

    def download_all_van_listings(self) -> list[dict]:
        """
        Download all van listings from eBay for the configured marketplace.
        Returns a list of listing dictionaries. Each listing's URL is visited
        in a headless browser to extract full description and item specifics,
        then the prospect listing is saved to the database.
        Skips listings that have already been processed based on hash code.

        For local pickup searches, both pickup parameters must be provided together:
        - pickup_postal_code: The postal/zip code for local pickup location
        - pickup_radius: The search radius in miles from the postal code
        """

        # load existing hash codes from database at startup
        existing_hash_codes = get_existing_hash_codes(ListingSource.EBAY)
        print(f"Loaded {len(existing_hash_codes)} existing hash codes from database")

        # initialize database session for prospect_listing records
        database_url = os.getenv("AUTO_ADS_DATABASE_URL")
        engine = create_engine(database_url)
        SessionLocal = sessionmaker(bind=engine)

        print(f"Initializing eBay API for marketplace: {self.marketplace}")
        self._get_api()

        print(f"Searching for '{self.query}' listings in {self.query} category (ID: {self.category_id})...")
        listings = []

        try:
            with sync_playwright() as p:
                # launch browser once for all listings
                self._browser = p.chromium.launch(headless=False)
                self._page = self._browser.new_page()

                with SessionLocal() as session:
                    for listing in self.search_van_listings():
                        # generate hash code from listing title
                        title = listing.get("title", "")
                        hash_code = generate_hash_code(title)

                        # check if already processed - skip if duplicate found
                        if hash_code in existing_hash_codes:
                            print(
                                f"Found existing listing (hash: {hash_code}), "
                                "continuing..."
                            )
                            continue

                        listings.append(listing)

                        # create prospect_listing
                        item_web_url = listing.get("item_web_url")
                        if item_web_url:
                            # extract price value and currency from listing
                            price_info = listing.get("price", {})
                            asking_price_raw = price_info.get("value")
                            currency = price_info.get("currency", "")

                            # convert asking_price to int, handling string format with commas and decimals
                            if isinstance(asking_price_raw, str):
                                # remove commas and convert to float, then int
                                asking_price = int(float(asking_price_raw.replace(",", "")))
                            else:
                                asking_price = int(float(asking_price_raw))

                            # determine currency_symbol based on currency
                            if currency == "GBP":
                                currency_symbol = "£"
                            elif currency == "USD":
                                currency_symbol = "$"
                            else:
                                currency_symbol = ""

                            # extract location from listing
                            item_location = listing.get("item_location", {})
                            location = item_location.get("city", "")

                            # set created_at and updated_at to current datetime
                            current_datetime = datetime.now()

                            # visit the listing url and extract details
                            title_short = title[:50] if title else "Unknown"
                            print(f"Visiting listing {len(listings)}: {title_short}...")

                            image_urls: list[str] = []
                            try:
                                self._page.goto(item_web_url, wait_until="domcontentloaded")
                                pause(0.1, 0.5)
                                self._accept_cookie_consent_if_present()

                                # check seller type and skip if not private
                                seller_type = self._extract_seller_type()
                                if seller_type != "Private" or seller_type is None:
                                    print(f"  Skipping listing: seller is {seller_type} (not Private)")
                                    continue

                                # extract full description and item specifics from page
                                details = self._extract_listing_details()

                                # iterate through listing page to collect all van images
                                image_urls = self._extract_listing_images()

                                # build make_and_model from extracted make and model
                                make = details.get("make") or ""
                                model = details.get("model") or ""
                                if make or model:
                                    make_and_model = f"{make} {model}".strip()
                                elif make:
                                    make_and_model = make
                                elif model:
                                    make_and_model = model
                                else:
                                    # fallback to title if no make/model found
                                    make_and_model = title

                            except Exception as e:
                                print(f"  Error visiting {item_web_url}: {e}")
                                details = {}
                                make_and_model = title

                            # create ProspectListings instance with extracted details
                            prospect_listing = ProspectListings(
                                hash_code=hash_code,
                                listing_source=ListingSource.EBAY,
                                status=ProspectListingStatus.NEW,
                                short_description=title,
                                full_description=details.get("full_description"),
                                url=item_web_url,
                                asking_price=asking_price,
                                currency_symbol=currency_symbol,
                                location=location,
                                make_and_model=make_and_model,
                                body_type=details.get("body_type"),
                                engine_size=details.get("engine_size"),
                                fuel_type=details.get("fuel_type"),
                                gearbox_type=details.get("transmission"),
                                year=details.get("year"),
                                colour=details.get("colour"),
                                auction_closes=details.get("auction_closes"),
                                created_at=current_datetime,
                                updated_at=current_datetime,
                            )

                            # save prospect listing to database
                            session.add(prospect_listing)
                            session.commit()
                            session.refresh(prospect_listing)

                            # download images after id is known, save to temp dir and s3
                            temp_image_dir = None
                            if image_urls:
                                temp_image_dir = download_and_save_listing_images(
                                    image_urls,
                                    self._page,
                                    prospect_listing,
                                    session,
                                    temp_dir_prefix="ebay_images_",
                                )

                            # generate and apply ai analysis using temp image directory
                            prospect_listing = process_ai_analysis_for_listing(
                                prospect_listing, session, temp_image_dir
                            )

                            # clean up temp directory after use
                            if temp_image_dir and os.path.isdir(temp_image_dir):
                                shutil.rmtree(temp_image_dir)

                        # add hash code to existing set to avoid duplicates in this run
                        existing_hash_codes.add(hash_code)

                        # print progress every 100 listings
                        if len(listings) % 100 == 0:
                            print(f"Downloaded {len(listings)} listings...")

                # close browser when done
                self._browser.close()
                self._browser = None
                self._page = None

        except Error as error:
            print(f"eBay API Error {error.number}: {error.reason}")
            if error.detail:
                print(f"Detail: {error.detail}")
            raise

        print(f"Download complete. Total listings: {len(listings)}")
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


# MAIN
def main():
    """
    Main entry point for downloading van listings from eBay.
    """

    print("=" * 60)
    print("eBay Van Listings Downloader")
    print("=" * 60)

    try:
        # download all listings (set limit=None to get all results)
        downloader = EbayDownloader(
            marketplace="GB",
            limit=None,  # set to None to fetch all available listings
            pickup_postal_code="LS1 3AD",
            pickup_radius=100,
        )
        listings = downloader.download_all_van_listings()

        # print summaries for first 10 listings
        print("\n" + "=" * 60)
        print("Sample Listings:")
        print("=" * 60 + "\n")

        for listing in listings[:10]:
            EbayDownloader.print_listing_summary(listing)

    except Error as error:
        print(f"\nFailed to download listings: {error}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
