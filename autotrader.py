import os
import re
import shutil
from datetime import date, datetime
from typing import Optional

from environments import load_environment

load_environment()

from playwright.sync_api import Page, sync_playwright
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from common import generate_hash_code, get_existing_hash_codes, pause
from listing_images import download_and_save_listing_images
from resell_analysis import (
    apply_resell_analysis,
    generate_resell_analysis,
    process_resell_analysis_for_listing,
)
from models.auto_ads import ProspectListings
from models.enums import ListingSource, ProspectListingStatus


# GET SPECS AND FEATURES
def get_specs_and_features(page: Page) -> Optional[str]:
    """
    Click the 'View all spec and features' button, expand all accordion sections,
    and extract all specs and features into markdown"""

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
    popup = page.locator("div.ppa-enabled")
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
                    category_name = re.sub(r"\d+$", "", category_name).strip()
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
                    category_name = re.sub(r"\d+$", "", category_name).strip()
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


# SAVE GALLERY IMAGES
def save_gallery_images(
    page: Page, prospect_listing: ProspectListings, session
) -> Optional[str]:
    """
    Open the full gallery view, load all images (via carousel or scrolling),
    fetch them via HTTP, save to S3 and temporary directory, and create Images database records.
    Returns the path to the temporary directory containing the images, or None if no images were saved.
    """

    # click the gallery button to open full gallery view
    gallery_button = page.locator(
        'section[name="gallery"] button:has(span:text("Gallery"))'
    )
    if gallery_button.count() == 0:
        print("Gallery button not found, skipping image extraction")
        return None

    gallery_button.click()
    pause(1.0, 2.0)

    # collect unique image urls
    image_urls = []
    seen_urls = set()

    # try scroll mode first: scroll down the gallery to load all images
    # wait for gallery container to be visible
    page.wait_for_load_state("networkidle")
    pause(1.0, 2.0)

    # find the scrollable container in the gallery (look for common scrollable elements)
    scrollable_container = page.evaluate(
        """
        () => {
            // find elements with overflow scroll or auto that have significant height
            const elements = document.querySelectorAll('*');
            for (const el of elements) {
                const style = window.getComputedStyle(el);
                const overflowY = style.overflowY;
                if ((overflowY === 'scroll' || overflowY === 'auto') && 
                    el.scrollHeight > el.clientHeight &&
                    el.clientHeight > 200) {
                    return true;
                }
            }
            return false;
        }
    """
    )

    max_scroll_attempts = 20
    scroll_attempts = 0
    previous_image_count = 0

    while scroll_attempts < max_scroll_attempts:
        # collect current images before scrolling
        img_elements = page.locator("img").all()
        current_image_count = len(img_elements)

        # scroll the scrollable container or window
        page.evaluate(
            """
            () => {
                // find and scroll the scrollable container
                const elements = document.querySelectorAll('*');
                for (const el of elements) {
                    const style = window.getComputedStyle(el);
                    const overflowY = style.overflowY;
                    if ((overflowY === 'scroll' || overflowY === 'auto') && 
                        el.scrollHeight > el.clientHeight &&
                        el.clientHeight > 200) {
                        el.scrollBy(0, window.innerHeight);
                        return;
                    }
                }
                // fallback to window scroll
                window.scrollBy(0, window.innerHeight);
            }
        """
        )
        pause(0.5, 1.0)

        # wait for potential new images to load
        page.wait_for_timeout(500)

        # check if we've reached the bottom of the scrollable container
        at_bottom = page.evaluate(
            """
            () => {
                // check scrollable container first
                const elements = document.querySelectorAll('*');
                for (const el of elements) {
                    const style = window.getComputedStyle(el);
                    const overflowY = style.overflowY;
                    if ((overflowY === 'scroll' || overflowY === 'auto') && 
                        el.scrollHeight > el.clientHeight &&
                        el.clientHeight > 200) {
                        return el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
                    }
                }
                // fallback to window check
                return window.scrollY + window.innerHeight >= document.body.scrollHeight;
            }
        """
        )

        # also check if no new images loaded
        img_elements_after = page.locator("img").all()
        new_image_count = len(img_elements_after)
        if at_bottom and new_image_count == previous_image_count:
            break

        previous_image_count = new_image_count
        scroll_attempts += 1

    # pause to ensure all images are fully loaded
    pause(1.0, 2.0)

    # locate all img elements in the gallery
    img_elements = page.locator("img").all()

    for img_element in img_elements:
        try:
            src = img_element.get_attribute("src")
            if src and src not in seen_urls and src.startswith("http"):
                seen_urls.add(src)
                image_urls.append(src)
        except Exception:
            continue

    # if only one image found, try carousel mode instead
    if len(image_urls) <= 1:
        carousel_next_button = page.locator('button[data-testid="carousel-next-icon"]')

        if carousel_next_button.count() > 0:
            # carousel mode: click through to collect all images
            max_carousel_clicks = 50
            carousel_clicks = 0

            while carousel_clicks < max_carousel_clicks:
                # collect current image url
                img_elements = page.locator("img").all()
                for img_element in img_elements:
                    try:
                        src = img_element.get_attribute("src")
                        if src and src not in seen_urls and src.startswith("http"):
                            seen_urls.add(src)
                            image_urls.append(src)
                    except Exception:
                        continue

                # check if next button is still available and enabled
                if carousel_next_button.count() == 0:
                    break

                # check if button is disabled (reached end of carousel)
                is_disabled = carousel_next_button.get_attribute("disabled")
                if is_disabled is not None:
                    break

                # click next to advance carousel
                try:
                    carousel_next_button.click()
                    pause(0.3, 0.6)
                    carousel_clicks += 1
                except Exception:
                    break

    # download images, save to temp dir and s3, create database records
    temp_dir = download_and_save_listing_images(
        image_urls,
        page,
        prospect_listing,
        session,
        ListingSource.AUTOTRADER,
        temp_dir_prefix="autotrader_images_",
    )

    # pause before closing gallery
    pause(1.0, 2.0)

    # click the close button to return to listing (works for both carousel and scroll modes)
    close_button = page.get_by_test_id("gallery-close")
    if close_button.count() > 0:
        close_button.click()
        pause(0.5, 1.0)

    return temp_dir


# READ FULL FOUND LISTING
def read_full_prospect_listing(
    page: Page, expected_short_description: str
) -> Optional[ProspectListings]:
    """
    Extract full listing details from the detail page and return a ProspectListings instance.
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
    if short_description != expected_short_description:
        raise Exception(
            f"Expected short description {expected_short_description}, got {short_description}"
        )

    # generate hash code from short description
    hash_code = generate_hash_code(short_description)

    # get price from data-testid="advert-price"
    price_elem = page.get_by_test_id("advert-price")
    price_text = price_elem.inner_text() if price_elem.count() > 0 else ""

    # check that the record has a price and is not an AUCTION
    if not price_text:
        print(f"No price found for listing: {make_and_model} - {short_description}")
        return None
    if not price_text or price_text == "AUCTION":
        print(f"Indicates AUCTION listing: {make_and_model} - {short_description}")
        return None

    # parse price components
    currency_symbol = price_text[0] if price_text else None
    asking_price = None
    vat_status = None

    # remove currency symbol and parse
    price_parts = price_text[1:].split(" ", 1)
    price_value_text = price_parts[0].replace(",", "")
    asking_price = int(price_value_text)
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
            owners_text = owners_value.inner_text()
            if owners_text.isnumeric():
                try:
                    number_of_owners = int(owners_text)
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

    # create and return ProspectListings instance
    prospect_listing = ProspectListings(
        hash_code=hash_code,
        listing_source=ListingSource.AUTOTRADER,
        status=ProspectListingStatus.NEW,
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
        currency_symbol=currency_symbol,
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
        emission_class=emission_class,
        number_of_owners=number_of_owners,
        service_history=service_history,
        basic_history_check=basic_history_check,
        specs_and_features=specs_and_features,
        mot_status=mot_status,
        mot_expiry=mot_expiry,
    )

    # insert into database and populate id field
    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        session.add(prospect_listing)
        session.flush()
        session.commit()

        # save gallery images after listing is committed
        temp_image_dir = save_gallery_images(page, prospect_listing, session)

        # generate and apply resell analysis using temp image directory
        prospect_listing = process_resell_analysis_for_listing(
            prospect_listing, session, temp_image_dir
        )

        # clean up temp directory after use
        if temp_image_dir and os.path.isdir(temp_image_dir):
            shutil.rmtree(temp_image_dir)

        # detach the object from the session so it can be used outside the session context
        session.expunge(prospect_listing)

    print(f"Extracted listing: {make_and_model} - {short_description[:50]}...")

    return prospect_listing


# REGENERATE ALL RESELL ANALYSES
def regenerate_all_resell_analyses():
    """
    Iterate through all rows in prospect_listings table and regenerate the
    resell analysis for each one using the AI model.
    """

    # load environment variables
    load_environment()

    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        # query all prospect listings
        prospect_listings = session.query(ProspectListings).all()
        total_count = len(prospect_listings)
        print(f"Found {total_count} prospect listings to process")

        for index, prospect_listing in enumerate(prospect_listings, start=1):
            try:
                print(
                    f"Processing {index}/{total_count}: {prospect_listing.make_and_model} "
                    f"(ID: {prospect_listing.id})"
                )

                # generate resell analysis (no images available for existing listings)
                resell_analysis = generate_resell_analysis(prospect_listing, None)

                # apply the analysis to update the AI fields
                apply_resell_analysis(prospect_listing, resell_analysis)

                # commit changes for this listing
                session.commit()
                print(f"  Successfully updated listing {prospect_listing.id}")

            except Exception as e:
                print(f"  Error processing listing {prospect_listing.id}: {e}")
                session.rollback()
                continue

    print(f"Finished processing {total_count} prospect listings")


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

        # wait for email input, handling possible 'are you human' verification
        email_input = page.get_by_test_id("enter-email-input")
        try:
            email_input.wait_for(state="visible", timeout=10000)
        except Exception:
            # email input didn't appear quickly, likely an 'are you human' check
            print(
                "Waiting for 'are you human' verification to be completed manually..."
            )
            email_input.wait_for(state="visible", timeout=300000)
            print("'Are you human' verification completed")
        email_input.fill(email)
        print(f"Entered email address: {email}")
        pause()

        # click the Continue button
        continue_button = page.get_by_test_id("create-username-continue-button")
        continue_button.click()
        print("Clicked 'Continue' button")
        pause()

        # wait for either password form or home page (verification code bypasses password)
        print(
            "Waiting for next step (password, captcha, or email verification code)..."
        )
        password_input = page.get_by_test_id("password-entry-password-input")
        home_indicator = page.get_by_test_id("header-saved-icon")
        next_step = password_input.or_(home_indicator)
        next_step.wait_for(state="visible", timeout=300000)

        if password_input.is_visible():
            # normal password flow
            print("Password form appeared")

            # load password from environment variable
            password = os.getenv("AUTOTRADER_PASSWORD")
            if not password:
                raise ValueError(
                    "AUTOTRADER_PASSWORD not found in environment variables"
                )

            # fill in the password field
            password_input.fill(password)
            print("Entered password")
            pause()

            # click the sign in button
            sign_in_button = page.get_by_test_id("password-entry-sign-in-button")
            sign_in_button.click()
            print("Clicked 'Sign in' button")
            pause()
        else:
            # home page header is visible but a focus-locked email verification code
            # modal may be blocking it — wait for any such modal to be dismissed first
            focus_lock_modal = page.locator('[data-focus-lock-disabled="false"]')
            if focus_lock_modal.count() > 0:
                print(
                    "Email verification code required. "
                    "Please enter the code sent to your email in the browser."
                )
                focus_lock_modal.wait_for(state="hidden", timeout=300000)
                print("Email verification code entered, proceeding to home page")
            else:
                print("Reached home page")

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
        prospect_listings = []
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
                                f"Found existing listing (hash: {hash_code}), skipping..."
                            )
                            continue

                        # click to navigate to detail page
                        title_link.click()
                        page.wait_for_load_state("domcontentloaded")
                        pause()

                        # extract full listing details
                        prospect_listing = read_full_prospect_listing(
                            page, short_description
                        )
                        if prospect_listing is None:
                            print(f"skipping...")
                            continue

                        prospect_listings.append(prospect_listing)

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
                        if hash_code not in existing_hash_codes:
                            existing_hash_codes.add(hash_code)

            # check if we've reached the bottom
            current_height = page.evaluate("document.body.scrollHeight")
            current_scroll = page.evaluate("window.scrollY + window.innerHeight")
            if current_scroll >= current_height:
                # reached the bottom of the page
                break
            scroll_attempts += 1

        print(f"\nFinished scrolling after {scroll_attempts} scroll operations")
        print(f"Processed {len(processed_listing_ids)} listings")
        print(f"Found {len(prospect_listings)} new listings")
        browser.close()


if __name__ == "__main__":
    main()
