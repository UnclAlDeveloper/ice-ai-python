import os
from typing import Generator, Optional

from environments import load_environment

load_environment()

from ebay_rest import API, Error


# GET EBAY API
def get_ebay_api(marketplace: str = "GB") -> API:
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

    # determine environment from client_id (SBX = sandbox, PRD = production)
    is_sandbox = "SBX" in client_id

    # configure application credentials
    application = {
        "app_id": client_id,
        "cert_id": client_secret,
        "dev_id": dev_id,
        "redirect_uri": "http://www.mindlessinvesting.com:8005/auto-ads-ebay-redirect",
    }

    # configure marketplace header for UK
    if marketplace == "GB":
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
    elif marketplace == "US":
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
        raise ValueError(f"Unsupported marketplace: {marketplace}")

    # user configuration for application-only access
    user = {
        "email_or_username": "TESTUSER_Graeme_Test",
        "password": "ice-ai9EB",
        "refresh_token": "",
        "refresh_token_expiry": "",
    }

    # create and return the api instance
    api = API(application=application, user=user, header=header)

    return api


# PRINT CATEGORY SUGGESTIONS
def print_category_suggestions(
    query: str,
    marketplace: str = "GB",
) -> list[dict]:
    """
    Get suggested eBay categories for a given search query using the Taxonomy API.
    Prints and returns a list of category suggestions sorted by relevance.
    """

    # initialize the api
    api = get_ebay_api(marketplace)
    marketplace_id = f"EBAY_{marketplace}"

    # get the category tree id for the marketplace
    tree_response = api.commerce_taxonomy_get_default_category_tree_id(marketplace_id)
    category_tree_id = tree_response.get("category_tree_id")

    if not category_tree_id:
        raise ValueError(f"Could not get category tree ID for marketplace: {marketplace_id}")

    # call the category suggestions api
    response = api.commerce_taxonomy_get_category_suggestions(
        category_tree_id=category_tree_id,
        q=query,
    )

    # extract the suggestions
    suggestions = response.get("category_suggestions", [])

    # print the suggestions
    print(f"\nCategory suggestions for '{query}':")
    print("-" * 50)
    for i, suggestion in enumerate(suggestions, 1):
        category = suggestion.get("category", {})
        category_id = category.get("category_id", "N/A")
        category_name = category.get("category_name", "N/A")

        # build the category path from ancestors
        ancestors = suggestion.get("category_tree_node_ancestors", [])
        path_parts = [a.get("category_name", "") for a in reversed(ancestors)]
        path_parts.append(category_name)
        full_path = " > ".join(path_parts)

        print(f"{i}. {category_name} (ID: {category_id})")
        print(f"   Path: {full_path}")

    return suggestions


# SEARCH VAN LISTINGS
def search_van_listings(
    api: API,
    query: str = "van",
    category_id = "122202", # vans and pickups
    limit: Optional[int] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
) -> Generator[dict, None, None]:
    """
    Search for van listings on eBay using the Browse API.
    Yields individual listing records as dictionaries.
    """

    # build filter string for additional constraints
    filters = []
    if min_price is not None or max_price is not None:
        price_min = min_price if min_price is not None else 0
        price_max = max_price if max_price is not None else ""
        filters.append(f"price:[{price_min}..{price_max}]")

    # prepare search parameters - q parameter is required by the api
    search_params = {
        "q": query,
        "category_ids": category_id,
    }

    # add filter string if there are any filters
    if filters:
        search_params["filter"] = ",".join(filters)

    if limit is not None:
        search_params["limit"] = limit

    # execute search and yield results
    for record in api.buy_browse_search(**search_params):
        if "record" in record:
            yield record["record"]
        elif "total" in record:
            # metadata record with totals
            print(f"Total records available: {record['total'].get('records_available', 'unknown')}")
            print(f"Total records yielded: {record['total'].get('records_yielded', 'unknown')}")


# DOWNLOAD ALL VAN LISTINGS
def download_all_van_listings(
    marketplace: str = "GB",
    query: str = "van",
    category_id: str = "122202",
    limit: Optional[int] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
) -> list[dict]:
    """
    Download all van listings from eBay for the specified marketplace.
    Returns a list of listing dictionaries.
    """

    print(f"Initializing eBay API for marketplace: {marketplace}")
    api = get_ebay_api(marketplace)

    print(f"Searching for '{query}' listings in {query} category (ID: {category_id})...")
    listings = []

    try:
        for listing in search_van_listings(
            api,
            query=query,
            category_id=category_id,
            limit=limit,
            min_price=min_price,
            max_price=max_price,
        ):
            listings.append(listing)

            # print progress every 100 listings
            if len(listings) % 100 == 0:
                print(f"Downloaded {len(listings)} listings...")

    except Error as error:
        print(f"eBay API Error {error.number}: {error.reason}")
        if error.detail:
            print(f"Detail: {error.detail}")
        raise

    print(f"Download complete. Total listings: {len(listings)}")
    return listings


# PRINT LISTING SUMMARY
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


def main():
    """
    Main entry point for downloading van listings from eBay.
    """

    print("=" * 60)
    print("eBay Van Listings Downloader")
    print("=" * 60)

    try:
        # download listings with a limit for testing
        listings = download_all_van_listings(
            marketplace="GB",
            limit=50,  # limit for testing, set to None for all
        )

        # print summaries for first 10 listings
        print("\n" + "=" * 60)
        print("Sample Listings:")
        print("=" * 60 + "\n")

        for listing in listings[:10]:
            print_listing_summary(listing)

    except Error as error:
        print(f"\nFailed to download listings: {error}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
