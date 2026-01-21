import hashlib
import os
import random
import re
import time
from datetime import date, datetime
from typing import Optional

from playwright.sync_api import Page, sync_playwright
from sqlalchemy import create_engine, text

# from AWSAccess import AWSAccess
from environments import load_environment
from models.auto_ads import FoundListings
from models.enums import ListingSource

# aws_access = AWSAccess(
#     bucket_name=os.getenv("AUTO_ADS_BUCKET"), sub_directory_name="images"
# )


def pause(min_seconds: float = 1.0, max_seconds: float = 3.0):
    """
    Wait a random amount of time to simulate human browsing behavior.
    """

    delay = random.uniform(min_seconds, max_seconds)
    time.sleep(delay)


# GET EXISTING HASH CODES
def get_existing_hash_codes() -> set[str]:
    """
    Query database for all existing hash_codes in found_listings table.
    """

    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    schema = os.getenv("AUTO_ADS_DATABASE_SCHEMA", "aa")

    engine = create_engine(database_url)
    with engine.connect() as conn:
        result = conn.execute(text(f"SELECT hash_code FROM {schema}.found_listings"))
        return {row[0] for row in result}


# GENERATE HASH CODE
def generate_hash_code(short_description: str) -> str:
    """
    Generate a 16-character hash code from the short description using MD5.
    """

    return hashlib.md5(short_description.encode()).hexdigest()[:16]


# GET SPECS AND FEATURES
def get_specs_and_features(page: Page) -> Optional[str]:
    """
    Click the 'View all spec and features' button, expand all accordion sections,
    and extract all specs and features into markdown format.
    """

    # try to find and click the "View all spec and features" button
    view_all_button = page.get_by_test_id("view-all-spec-and-features-signpost")
    if view_all_button.count() == 0:
        return None

    try:
        view_all_button.click()
        pause(1.0, 2.0)
    except Exception:
        return None

    # wait for the popup to appear
    popup = page.locator('div.ppa-enabled')
    if popup.count() == 0:
        return None

    # expand all collapsed accordion sections
    collapsed_buttons = popup.locator('button[aria-expanded="false"]')
    collapsed_count = collapsed_buttons.count()

    for i in range(collapsed_count):
        try:
            # re-query to handle dynamic DOM changes
            collapsed_buttons = popup.locator('button[aria-expanded="false"]')
            if collapsed_buttons.count() > 0:
                collapsed_buttons.first.click()
                pause(1.0, 3.0)
        except Exception:
            pass

    markdown_parts = []

    # process feature accordions
    feature_sections = popup.locator('section[data-testid="feature-accordion"]')
    feature_count = feature_sections.count()

    if feature_count > 0:
        markdown_parts.append("## All Features\n")

        for i in range(feature_count):
            section = feature_sections.nth(i)

            # get category name from button text
            category_button = section.locator("button").first
            if category_button.count() > 0:
                # get the category name (first span text, excluding the count badge)
                category_span = category_button.locator("span").first
                if category_span.count() > 0:
                    category_name = category_span.inner_text().strip()
                    # remove trailing number if present (the count badge)
                    category_name = re.sub(r'\d+$', '', category_name).strip()
                    markdown_parts.append(f"### {category_name}\n")

            # get all feature items
            list_items = section.locator("li")
            for j in range(list_items.count()):
                item = list_items.nth(j)
                spans = item.locator("span")

                # features have a single span with the feature name
                if spans.count() == 1:
                    feature_text = spans.first.inner_text().strip()
                    markdown_parts.append(f"- {feature_text}")

            markdown_parts.append("")

    # process spec accordions
    spec_sections = popup.locator('section[data-testid="spec-accordion"]')
    spec_count = spec_sections.count()

    if spec_count > 0:
        markdown_parts.append("## Specs\n")

        for i in range(spec_count):
            section = spec_sections.nth(i)

            # get category name from button text
            category_button = section.locator("button").first
            if category_button.count() > 0:
                # get the category name (first span text, excluding the count badge)
                category_span = category_button.locator("span").first
                if category_span.count() > 0:
                    category_name = category_span.inner_text().strip()
                    # remove trailing number if present (the count badge)
                    category_name = re.sub(r'\d+$', '', category_name).strip()
                    markdown_parts.append(f"### {category_name}\n")

            # get all spec items
            list_items = section.locator("li")
            for j in range(list_items.count()):
                item = list_items.nth(j)
                spans = item.locator("span")

                # specs have two spans: name and value
                if spans.count() >= 2:
                    spec_name = spans.nth(0).inner_text().strip()
                    spec_value = spans.nth(1).inner_text().strip()
                    markdown_parts.append(f"- {spec_name}: {spec_value}")

            markdown_parts.append("")

    # pause before closing the popup
    pause(2.0, 10.0)

    # close the popup using the Back button
    try:
        back_button = page.locator(
            'section[data-testid="spec-feats-modal-back-button"] '
            'button[aria-label="Close"]'
        )
        if back_button.count() > 0:
            back_button.click()
            pause(0.5, 1.0)
    except Exception:
        pass

    if not markdown_parts:
        return None

    return "\n".join(markdown_parts).strip()


# GET BASIC HISTORY CHECK
def get_basic_history_check(page: Page) -> Optional[str]:
    """
    Extract basic vehicle history check information from the vehicle history section.
    """

    # look for the vehicle history section
    history_section = page.locator('section[id="vehicle-history"]')
    if history_section.count() == 0:
        return None

    # try to find the summary text (e.g., "5 checks passed")
    summary_elem = history_section.locator('p:has-text("checks passed")').first
    if summary_elem.count() == 0:
        return None

    summary_text = summary_elem.inner_text().strip()

    # if 5 checks passed, return that
    if summary_text == "5 checks passed":
        return summary_text

    # otherwise, examine each individual check to find failures
    check_labels = [
        "Not recorded as stolen",
        "Not recorded as scrapped",
        "Not imported from another country",
        "Not exported out of the UK",
        "Never been written off",
    ]

    failed_checks = []

    for label in check_labels:
        # find the button containing this check label
        check_button = history_section.locator(f'button:has-text("{label}")').first
        if check_button.count() > 0:
            # look at the svg path to determine pass or fail
            # checkmark path contains "13.439" or similar curved path
            # x path contains different coordinates
            svg_path = check_button.locator("svg path").first
            if svg_path.count() > 0:
                path_d = svg_path.get_attribute("d")
                # checkmark paths typically contain "13.439" or "11.299"
                # if the path doesn't contain these, it's likely an X (fail)
                if path_d and "13.439" not in path_d and "11.299" not in path_d:
                    # derive the failure description from the label
                    if "stolen" in label.lower():
                        failed_checks.append("Recorded as stolen")
                    elif "scrapped" in label.lower():
                        failed_checks.append("Recorded as scrapped")
                    elif "imported" in label.lower():
                        failed_checks.append("Imported from another country")
                    elif "exported" in label.lower():
                        failed_checks.append("Exported out of the UK")
                    elif "written off" in label.lower():
                        failed_checks.append("Has been written off")

    # build the result string
    if failed_checks:
        result = (
            summary_text + "\n" + "\n".join(f"* {check}" for check in failed_checks)
        )
        return result

    return summary_text


# GET OVERVIEW VALUE
def get_overview_value(page: Page, icon_name: str) -> Optional[str]:
    """
    Extract value from overview section by icon data-gui attribute.
    """

    icon = page.locator(f'svg[data-gui="atds-icon-{icon_name}"]')
    if icon.count() > 0:
        # navigate up to the parent container and get the value paragraph
        parent = icon.locator("xpath=ancestor::div[contains(@class, 'sc-tqnfbs-1')]")
        if parent.count() > 0:
            value_elem = parent.locator("p").last
            if value_elem.count() > 0:
                return value_elem.inner_text()
    return None


# READ FULL FOUND LISTING
def read_full_found_listing(page: Page) -> FoundListings:
    """
    Extract full listing details from the detail page and return a FoundListings instance.
    """

    # get the current url
    url = page.url

    # get make and model from h1
    h1_elem = page.locator("h1").first
    make_and_model = h1_elem.inner_text() if h1_elem.count() > 0 else ""

    # get short description from span after h1
    short_desc_elem = page.locator("h1 + div span").first
    short_description = (
        short_desc_elem.inner_text() if short_desc_elem.count() > 0 else ""
    )

    # generate hash code from short description
    hash_code = generate_hash_code(short_description)

    # get price from data-testid="advert-price"
    price_elem = page.get_by_test_id("advert-price")
    price_text = price_elem.inner_text() if price_elem.count() > 0 else ""

    # parse price components
    currency = price_text[0] if price_text else None
    asking_price = None
    vat_status = None

    if price_text:
        # remove currency symbol and parse
        price_parts = price_text[1:].split(" ", 1)
        price_value_text = price_parts[0].replace(",", "")
        try:
            asking_price = int(price_value_text)
        except ValueError:
            asking_price = None
        # get vat status (everything after the price number)
        if len(price_parts) > 1:
            vat_status = price_parts[1].strip()

    # get location from contact seller section
    location_elem = page.locator('p[class*="sc-1ph9l9h-4"]').first
    location_text = location_elem.inner_text() if location_elem.count() > 0 else ""
    # extract just the city name (before the dash)
    location = location_text.split(" - ")[0].strip() if location_text else None

    # get full description by clicking expand button if available
    full_description = None
    desc_elem = page.locator('section[id="description"] p').first
    if desc_elem.count() > 0:
        full_description = desc_elem.inner_text()

    # try to expand description for full text
    expand_btn = page.get_by_test_id("description-signpost")
    if expand_btn.count() > 0:
        try:
            expand_btn.click()
            pause(1.0, 3.0)
            # get expanded description
            expanded_desc = page.locator('section[id="description"] p').first
            if expanded_desc.count() > 0:
                full_description = expanded_desc.inner_text()
            # click back to return to main listing page
            pause(2.0, 5.0)
            back_button = page.locator(
                'section[data-testid="description-modal-back-button"] '
                'button[aria-label="Close"]'
            )
            if back_button.count() > 0:
                back_button.click()
                pause(1.0, 2.0)
        except Exception:
            pass  # keep the short description if expand fails

    # extract overview fields using helper function
    mileage_text = get_overview_value(page, "mileage")
    mileage = None
    mileage_unit = None
    if mileage_text:
        parts = mileage_text.replace(",", "").split(" ")
        try:
            mileage = int(parts[0])
            mileage_unit = parts[1] if len(parts) > 1 else None  # 'm' for miles
        except (ValueError, IndexError):
            pass

    # get year and registration
    reg_text = get_overview_value(page, "registration")
    year = None
    registration = None
    if reg_text:
        # format: "2007 (57 reg)"
        parts = reg_text.split(" ")
        try:
            year = int(parts[0])
        except ValueError:
            pass
        if "(" in reg_text:
            registration = reg_text.split("(")[-1].replace(")", "").strip()

    # get other overview fields
    body_type = get_overview_value(page, "body-type")
    cab_type = get_overview_value(page, "cab-type")
    wheelbase = get_overview_value(page, "wheelbase")
    engine_size = get_overview_value(page, "engine")
    emission_class = get_overview_value(page, "emission-class")
    gearbox_type = get_overview_value(page, "gearbox")
    fuel_type = get_overview_value(page, "fuel-type")
    colour = get_overview_value(page, "body-colour")

    # get seats as integer
    seats_text = get_overview_value(page, "seats")
    seats = None
    if seats_text:
        try:
            seats = int(seats_text)
        except ValueError:
            pass

    # get basic vehicle history check
    basic_history_check = get_basic_history_check(page)

    # get specs and features from popup
    specs_and_features = get_specs_and_features(page)

    # get number of owners
    number_of_owners = None
    owners_div = page.locator('div:has(> p:text-is("Owners"))').first
    if owners_div.count() > 0:
        owners_value = owners_div.locator("p").nth(1)
        if owners_value.count() > 0:
            try:
                number_of_owners = int(owners_value.inner_text())
            except ValueError:
                pass

    # get service history
    service_history = get_overview_value(page, "service-history")

    # get mot status and expiry
    mot_status = None
    mot_expiry = None
    mot_heading = page.locator('h3:text-is("MOT Information")').first
    if mot_heading.count() > 0:
        # get the next sibling p element
        mot_status_elem = page.locator(
            'h3:text-is("MOT Information") + p, ' 'h3:text-is("MOT Information") ~ p'
        ).first
        if mot_status_elem.count() > 0:
            mot_status = mot_status_elem.inner_text()
            # extract date from mot_status if present (UK format dd/mm/yyyy)
            if mot_status:
                date_match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", mot_status)
                if date_match:
                    day = int(date_match.group(1))
                    month = int(date_match.group(2))
                    year_val = int(date_match.group(3))
                    try:
                        mot_expiry = date(year_val, month, day)
                    except ValueError:
                        pass

    # set created_at and updated_at to current datetime
    current_datetime = datetime.now()

    # create and return FoundListings instance
    found_listing = FoundListings(
        hash_code=hash_code,
        listing_source_id=ListingSource.AUTOTRADER,
        make_and_model=make_and_model,
        short_description=short_description,
        url=url,
        asking_price=asking_price,
        created_at=current_datetime,
        updated_at=current_datetime,
        full_description=full_description,
        mileage=mileage,
        mileage_unit=mileage_unit,
        year=year,
        registration=registration,
        currency=currency,
        vat_status=vat_status,
        location=location,
        body_type=body_type,
        cab_type=cab_type,
        fuel_type=fuel_type,
        gearbox_type=gearbox_type,
        wheelbase=wheelbase,
        engine_size=engine_size,
        colour=colour,
        seats=seats,
        emmission_class=emission_class,
        number_of_owners=number_of_owners,
        service_history=service_history,
        basic_history_check=basic_history_check,
        specs_and_features=specs_and_features,
        mot_status=mot_status,
        mot_expiry=mot_expiry,
    )

    print(f"Extracted listing: {make_and_model} - {short_description[:50]}...")

    return found_listing


def main():
    """
    Navigate to autotrader.co.uk using Playwright with visible browser.
    """

    # load environment variables based on app type and environment setting
    load_environment()

    # load existing hash codes from database at startup
    existing_hash_codes = get_existing_hash_codes()
    print(f"Loaded {len(existing_hash_codes)} existing hash codes from database")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto("https://www.autotrader.co.uk")
        print("Navigated to autotrader.co.uk")
        pause()

        # dismiss the cookie consent modal (it's inside an iframe)
        consent_iframe = page.frame_locator("iframe[id*='sp_message_iframe']")

        # try to find and click "Reject All" button
        reject_all_button = consent_iframe.get_by_role("button", name="Reject All")
        if reject_all_button.count() > 0:
            reject_all_button.click()
            print("Clicked 'Reject All' on cookie consent")
        else:
            # try "Essential cookies only" button
            essential_button = consent_iframe.get_by_role(
                "button", name="Essential cookies only"
            )
            if essential_button.count() > 0:
                essential_button.click()
                print("Clicked 'Essential cookies only' on cookie consent")
            else:
                # get all buttons and click the middle one (should be 3 buttons)
                all_buttons = consent_iframe.get_by_role("button")
                button_count = all_buttons.count()
                if button_count >= 3:
                    # click the middle button (index 1)
                    all_buttons.nth(1).click()
                    print("Clicked middle button on cookie consent")
                else:
                    print(
                        f"Warning: Found {button_count} buttons, expected 3. Clicking first button."
                    )
                    all_buttons.first.click()

        pause()

        # click the sign in button to navigate to the sign in screen
        sign_in_button = page.get_by_role("button", name="Sign in")
        sign_in_button.click()
        print("Clicked 'Sign in' button")
        pause()

        # load email from environment variables
        email = os.getenv("AUTOTRADER_EMAIL")
        if not email:
            raise ValueError("AUTOTRADER_EMAIL not found in environment variables")

        # wait for the email input field to appear and fill it
        email_input = page.get_by_test_id("enter-email-input")
        email_input.wait_for(state="visible")
        email_input.fill(email)
        print(f"Entered email address: {email}")
        pause()

        # click the Continue button
        continue_button = page.get_by_test_id("create-username-continue-button")
        continue_button.click()
        print("Clicked 'Continue' button")
        pause()

        # wait for captcha to be filled in manually
        print("Waiting for captcha to be filled in...")
        # wait for the password form to appear (this indicates captcha was completed)
        password_input = page.get_by_test_id("password-entry-password-input")
        password_input.wait_for(
            state="visible", timeout=300000
        )  # 5 minute timeout for manual captcha
        print("Password form appeared, captcha completed")

        # load password from environment variable
        password = os.getenv("AUTOTRADER_PASSWORD")
        if not password:
            raise ValueError("AUTOTRADER_PASSWORD not found in environment variables")

        # fill in the password field
        password_input.fill(password)
        print("Entered password")
        pause()

        # click the Sign in button
        sign_in_button = page.get_by_test_id("password-entry-sign-in-button")
        sign_in_button.click()
        print("Clicked 'Sign in' button")
        pause()

        # click the Saved button
        saved_button = page.get_by_test_id("header-saved-icon")
        saved_button.click()
        print("Clicked 'Saved' button")
        pause()

        # click the Searches tab
        searches_tab = page.get_by_test_id("nav-strip-searches-link")
        searches_tab.click()
        print("Clicked 'Searches' tab")
        pause()

        # click the "All Vans" saved search
        all_vans_link = page.get_by_test_id("saved-advert-title-link").filter(
            has_text="All Vans"
        )
        all_vans_link.click()
        print("Clicked 'All Vans' saved search")
        pause()

        # click the "Filter and sort" button
        filter_button = page.get_by_test_id("search-filter-toggle")
        filter_button.wait_for(state="visible")
        filter_button.click()
        print("Clicked 'Filter and sort' button")
        pause()

        # expand the Sort menu item
        sort_menu = page.locator("#sort").get_by_test_id("sort-facet-group")
        sort_menu.wait_for(state="visible")
        sort_menu.click()
        print("Expanded 'Sort' menu")
        pause()

        # select "Most recent" option
        most_recent_option = page.get_by_test_id("most-recent-radio-testid")
        most_recent_option.wait_for(state="visible")
        most_recent_option.click()
        print("Selected 'Most recent' sort option")
        pause()

        # click the Search button to apply filters
        search_button = page.get_by_test_id("search-apply-button")
        search_button.wait_for(state="visible")
        search_button.click()
        print("Clicked 'Search' button to apply filters")
        pause()

        # scroll down the page one viewport at a time to load all listings
        print("Scrolling to load all van listings...\n")
        processed_listing_ids = set()
        found_listings = []
        scroll_attempts = 0
        max_scroll_attempts = 10  # prevent infinite scrolling
        stop_processing = False

        while scroll_attempts < max_scroll_attempts and not stop_processing:
            # scroll down by one page height
            page.evaluate("window.scrollBy(0, window.innerHeight)")
            pause(min_seconds=1.0, max_seconds=2.0)  # pause between scrolls

            # wait a bit for content to load
            page.wait_for_timeout(1000)

            # find all currently visible list items
            list_items = page.locator('li[data-testid^="id-"]').all()

            # process any new listings
            for list_item in list_items:
                if stop_processing:
                    break

                # get the listing id from the data-testid attribute
                listing_id = list_item.get_attribute("data-testid")
                if listing_id and listing_id not in processed_listing_ids:
                    # check if this listing has a title (is fully loaded)
                    title_link = list_item.locator(
                        'a[data-testid="search-listing-title"]'
                    )
                    if title_link.count() > 0:
                        # extract short_description and calculate hash_code
                        subtitle = list_item.locator(
                            'p[data-testid="search-listing-subtitle"]'
                        )
                        short_description = (
                            subtitle.inner_text() if subtitle.count() > 0 else ""
                        )
                        hash_code = generate_hash_code(short_description)

                        # check if already processed - stop if duplicate found
                        if hash_code in existing_hash_codes:
                            print(
                                f"Found existing listing (hash: {hash_code}), "
                                "stopping..."
                            )
                            stop_processing = True
                            break

                        # click to navigate to detail page
                        title_link.click()
                        page.wait_for_load_state("domcontentloaded")
                        pause()

                        # extract full listing details
                        found_listing = read_full_found_listing(page)
                        found_listings.append(found_listing)

                        # pause before navigating back
                        pause(2.0, 10.0)

                        # navigate back using "Back to results" link
                        back_button = page.locator(
                            'a[data-testid="back-to-search-link"]'
                        )
                        back_button.click()
                        page.wait_for_load_state("domcontentloaded")
                        pause()

                        processed_listing_ids.add(listing_id)

            # check if we've reached the bottom
            current_height = page.evaluate("document.body.scrollHeight")
            current_scroll = page.evaluate("window.scrollY + window.innerHeight")
            if current_scroll >= current_height:
                # reached the bottom of the page
                break
            scroll_attempts += 1

        print(f"\nFinished scrolling after {scroll_attempts} scroll operations")
        print(f"Processed {len(processed_listing_ids)} listings")
        print(f"Found {len(found_listings)} new listings")

        print("Press Enter to close the browser...")
        input()
        browser.close()


if __name__ == "__main__":
    main()
