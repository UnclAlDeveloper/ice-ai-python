import argparse
import os
import re
import shutil
import sys
from datetime import date, datetime

# parse --env before load_environment so the chosen env file is selected at import time
_pre_parser = argparse.ArgumentParser(add_help=False)
_pre_parser.add_argument("--env", choices=["dev", "prod"], default=None)
_pre_args, _remaining_argv = _pre_parser.parse_known_args()
if _pre_args.env is not None:
    os.environ["ENVIRONMENT"] = "production" if _pre_args.env == "prod" else "dev"
sys.argv[:] = [sys.argv[0]] + _remaining_argv

from environments import load_environment

load_environment()

from playwright.sync_api import Page, sync_playwright
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai_analysis import apply_ai_analysis, generate_ai_analysis
from common import (
    generate_hash_code,
    get_existing_hash_codes,
    goto_with_captcha_handling,
    is_captcha_present,
    is_http_not_found,
    is_not_found_error,
    pause,
    wait_for_captcha_solve,
)
from listing_images import delete_listing_images, download_and_save_listing_images
from models.auto_ads import ProspectListings
from models.enums import ListingSource, ProspectListingStatus

UNAVAILABLE_ADVERT_TEXT = (
    "This advert has now been removed through sale or otherwise"
)


# IS LISTING NO LONGER AVAILABLE
def is_listing_no_longer_available(page: Page) -> bool:
    """
    Return True when the listing page shows that the advert has been removed.
    """

    unavailable_banner = page.get_by_text(UNAVAILABLE_ADVERT_TEXT, exact=False)
    return unavailable_banner.count() > 0


# UPDATE NEW LISTINGS AVAILABILITY
def update_new_listings_availability(page: Page) -> None:
    """
    Visit each Car & Classic prospect listing with status New and mark any that are
    no longer available as NotAvailable.
    """

    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        new_listings = (
            session.query(ProspectListings)
            .filter(
                ProspectListings.listing_source == ListingSource.CAR_AND_CLASSIC,
                ProspectListings.status == ProspectListingStatus.NEW,
            )
            .order_by(ProspectListings.id)
            .all()
        )

        if not new_listings:
            print("No New Car & Classic listings to check for availability")
            return

        print(
            f"Checking availability of {len(new_listings)} New Car & Classic listings..."
        )

        for listing in new_listings:
            print(
                f"Checking: {listing.make_and_model} - {listing.short_description[:50]}..."
            )

            # guard each visit so one bad listing does not abort the whole sweep
            try:
                response = goto_with_captcha_handling(page, listing.url)

                if is_http_not_found(response) or is_listing_no_longer_available(page):
                    listing.status = ProspectListingStatus.NOT_AVAILABLE
                    listing.updated_at = datetime.now()
                    session.commit()
                    reason = "404" if is_http_not_found(response) else "unavailable"
                    print(f"  Marked as NotAvailable ({reason}): {listing.url}")

                    # drop the now-orphaned photos from s3 and the images table
                    delete_listing_images(listing, session)
                else:
                    print("  Still available")
            except Exception as e:
                if is_not_found_error(e):
                    listing.status = ProspectListingStatus.NOT_AVAILABLE
                    listing.updated_at = datetime.now()
                    session.commit()
                    print(f"  Marked as NotAvailable (404): {listing.url}")

                    # drop the now-orphaned photos from s3 and the images table
                    delete_listing_images(listing, session)
                else:
                    session.rollback()
                    print(f"  Error checking {listing.url}: {e}")

            pause(2.0, 8.0)

        print("Finished availability check for New listings")


ICON_TO_FIELD = {
    "driving-wheel": "drive_configuration",
    "dial": "mileage_raw",
    "fuel": "fuel_type",
    "engine": "engine_size",
    "calendar": "year",
    "vrm": "registration",
    "colour": "colour",
}


# ACCEPT COOKIES
def accept_cookies(page: Page):
    """
    Dismiss the OneTrust cookie consent banner by clicking 'Accept All' if it appears.
    """

    accept_button = page.locator("#onetrust-accept-btn-handler")
    if accept_button.is_visible():
        accept_button.click()
        page.wait_for_selector("#onetrust-banner-sdk", state="hidden")


# LOGIN
def login(page: Page):
    """
    Fill in the login popup with credentials from environment variables and submit.
    """

    email = os.getenv("CAR_AND_CLASSIC_EMAIL")
    password = os.getenv("CAR_AND_CLASSIC_PASSWORD")

    # enter email address
    pause()
    page.locator('[data-testid="input-email"]').fill(email)

    # enter password (target the first matching input)
    pause()
    page.locator('[data-testid="input-password"]').first.fill(password)

    # submit the login form
    pause()
    page.locator('button[type="submit"]:has-text("Log in")').click()


# PARSE MILEAGE
def parse_mileage(raw: str) -> tuple[int | None, str]:
    """
    Split a mileage string like '138,100 Miles' into the numeric value
    as an integer and the unit string.
    """

    match = re.match(r"^([\d,]+)\s+(.+)$", raw.strip())
    if match:
        return int(match.group(1).replace(",", "")), match.group(2)
    return None, ""


# DISMISS INERTIA ERROR DIALOG
def dismiss_inertia_error_dialog(page: Page) -> bool:
    """
    Remove the Inertia.js error overlay dialog if one is open. The overlay
    renders a full-page iframe that intercepts pointer events, so any
    subsequent click on the underlying page silently times out until it is
    cleared. Returns True if a dialog was dismissed.
    """

    dialog = page.locator("dialog#inertia-error-dialog")
    if dialog.count() == 0:
        return False

    # the dialog exposes no visible close control, so detach it from the dom
    page.evaluate(
        "document.getElementById('inertia-error-dialog')?.remove()"
    )
    return True


# EXTRACT GALLERY IMAGES
def extract_gallery_images(page: Page) -> list[str]:
    """
    Collect image URLs from the Gallery section. If the last button contains a
    camera icon, click it to open the full gallery popup and collect URLs from
    there. Otherwise all images are already visible on the detail page, so
    collect URLs directly from the gallery section.
    """

    image_urls = []

    # clear any inertia error overlay that would intercept the gallery click
    dismiss_inertia_error_dialog(page)

    gallery_section = page.locator('section:has(h2:text("Gallery"))')
    if gallery_section.count() == 0:
        return image_urls

    gallery_buttons = gallery_section.locator("button")
    if gallery_buttons.count() == 0:
        return image_urls

    last_button = gallery_buttons.last
    has_camera_icon = last_button.locator('svg[data-icon="camera"]').count() > 0

    if has_camera_icon:
        # click the last button to open the full gallery popup
        last_button.click()
        pause()

        # collect all image src urls from the popup panel
        popup_images = page.locator("#panel_sheet_images img")
        for i in range(popup_images.count()):
            src = popup_images.nth(i).get_attribute("src")
            if src and src.startswith("http") and src not in image_urls:
                image_urls.append(src)

        # close the gallery popup
        close_button = page.locator('button:has(svg[data-icon="close"])').first
        if close_button.is_visible():
            close_button.click()
            pause()
    else:
        # all images are visible on the detail page already
        section_images = gallery_section.locator("img")
        for i in range(section_images.count()):
            src = section_images.nth(i).get_attribute("src")
            if src and src.startswith("http") and src not in image_urls:
                image_urls.append(src)

    return image_urls


# EXTRACT LISTING DETAILS
def extract_listing_details(
    page: Page, hash_code: str
) -> tuple[ProspectListings, list[str]]:
    """
    Extract prospect listing fields from an individual listing detail page
    and return a populated ProspectListings instance along with gallery image URLs.
    """

    url = page.url
    current_datetime = datetime.now()

    # extract make_and_model from the h1
    h1_text = page.locator("section h1").first.text_content().strip()
    make_and_model = h1_text

    mileage = None
    mileage_unit = None
    fuel_type = None
    engine_size = None
    year = None
    registration = None
    colour = None
    drive_configuration = None
    location = None

    # extract fields from the spec list items using their svg data-icon attribute
    items = page.locator("section ul li")
    for i in range(items.count()):
        li = items.nth(i)
        icon = li.locator("svg[data-icon]")

        if icon.count() > 0:
            icon_name = icon.first.get_attribute("data-icon")
            value = li.text_content().strip()
            field = ICON_TO_FIELD.get(icon_name)
            if field == "mileage_raw":
                mileage, mileage_unit = parse_mileage(value)
            elif field == "year":
                try:
                    year = int(value)
                except ValueError:
                    pass
            elif field == "fuel_type":
                fuel_type = value
            elif field == "engine_size":
                engine_size = value
            elif field == "registration":
                registration = value
            elif field == "colour":
                colour = value
            elif field == "drive_configuration":
                drive_configuration = value
        else:
            # the location li uses a flag <img> instead of an svg
            img = li.locator("img")
            if img.count() > 0:
                location = li.text_content().strip()
                location = re.sub(r",?\s*United Kingdom$", "", location)

    # extract asking price and currency symbol from the price header
    asking_price = None
    currency_symbol = None
    price_header = page.locator('header:has(span:text("Asking price")) h2')
    if price_header.count() > 0:
        price_text = price_header.first.text_content().strip()
        if price_text:
            currency_symbol = price_text[0]
            price_digits = price_text[1:].replace(",", "")
            try:
                asking_price = int(price_digits)
            except ValueError:
                pass

    # extract description from the article containing "Description" heading
    short_description = None
    full_description = None
    desc_article = page.locator('article:has(h2:text("Description"))')
    if desc_article.count() > 0:
        full_text = desc_article.locator("p").first.inner_text().strip()
        full_description = full_text
        short_description = full_text.split("\n")[0].strip()

    # extract vehicle background history check summary
    basic_history_check = None
    bg_section = page.locator('section:has(h2:text("Vehicle background"))')
    if bg_section.count() > 0:
        answers = bg_section.locator("div > p:last-child")
        no_count = 0
        yes_count = 0
        for i in range(answers.count()):
            answer = answers.nth(i).text_content().strip()
            if answer == "No":
                no_count += 1
            elif answer == "Yes":
                yes_count += 1
        basic_history_check = f"{no_count} checks passed"
        if yes_count > 0:
            suffix = "s" if yes_count > 1 else ""
            basic_history_check += f", {yes_count} item{suffix} to note"

    # extract gallery images from the full gallery popup
    image_urls = extract_gallery_images(page)

    prospect_listing = ProspectListings(
        hash_code=hash_code,
        listing_source=ListingSource.CAR_AND_CLASSIC,
        status=ProspectListingStatus.NEW,
        url=url,
        make_and_model=make_and_model,
        short_description=short_description or h1_text,
        full_description=full_description,
        asking_price=asking_price,
        currency_symbol=currency_symbol,
        mileage=mileage,
        mileage_unit=mileage_unit,
        fuel_type=fuel_type,
        engine_size=engine_size,
        year=year,
        registration=registration,
        colour=colour,
        drive_configuration=drive_configuration,
        location=location,
        basic_history_check=basic_history_check,
        created_at=current_datetime,
        updated_at=current_datetime,
    )

    return prospect_listing, image_urls


# SCRAPE LISTINGS
def scrape_listings(page: Page):
    """
    Iterate through all pages of search results. For each article, generate a
    hash code from the h2 title and skip if already in the database. Otherwise
    navigate to the detail page, extract a ProspectListings record, save it to
    the database, and download images to local temp and S3. Advances through
    pages via the next-page link.
    """

    existing_hash_codes = get_existing_hash_codes(ListingSource.CAR_AND_CLASSIC)
    print(f"Loaded {len(existing_hash_codes)} existing hash codes from database")

    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine)

    page_number = 1
    new_count = 0

    # check if a "new vehicles" grid section is present above the main listings
    new_vehicles_grid = page.locator("div.lg\\:grid-cols-3.grid.grid-cols-1")
    use_new_section = new_vehicles_grid.count() > 0

    if use_new_section:
        print("New vehicles section detected — processing only new listings")

    # remember the search-results url so we can recover after a per-listing failure
    search_url = page.url

    while True:
        pause()

        # scope articles to the new-vehicles grid if present, otherwise all
        if use_new_section:
            articles = new_vehicles_grid.locator('[data-testid="card-listing"]')
        else:
            articles = page.locator('[data-testid="card-listing"]')

        count = articles.count()
        print(f"\n--- Page {page_number} ({count} listings) ---")

        for i in range(count):
            # re-query articles after each navigation back
            if use_new_section:
                new_vehicles_grid = page.locator(
                    "div.lg\\:grid-cols-3.grid.grid-cols-1"
                )
                articles = new_vehicles_grid.locator(
                    '[data-testid="card-listing"]'
                )
            else:
                articles = page.locator('[data-testid="card-listing"]')

            title = articles.nth(i).locator("h2").text_content().strip()

            hash_code = generate_hash_code(title)
            if hash_code in existing_hash_codes:
                print(f"  [{i + 1}] {title} — already exists, skipping")
                continue

            # process each listing inside a guard so one bad listing cannot
            # abort the whole sweep; on failure we re-navigate to the search
            # results so the next iteration starts from a known good state
            try:
                # navigate to the listing via its href instead of clicking the
                # card, because the anchor is overlaid by a sibling that
                # intercepts pointer events; goto_with_captcha_handling also
                # pauses for any captcha shown in place of the detail page
                href = articles.nth(i).locator("a").first.get_attribute("href")
                listing_url = (
                    href
                    if href.startswith("http")
                    else f"https://www.carandclassic.com{href}"
                )
                goto_with_captcha_handling(page, listing_url)
                page.wait_for_selector(
                    "section h1", state="visible", timeout=15000
                )
                pause()

                prospect_listing, image_urls = extract_listing_details(
                    page, hash_code
                )
                new_count += 1
                print(f"  [{i + 1}] {title}")
                print(f"      make_and_model: {prospect_listing.make_and_model}")
                print(f"      year: {prospect_listing.year}")
                print(f"      location: {prospect_listing.location}")
                print(f"      images: {len(image_urls)}")

                # save to database and download images
                with SessionLocal() as session:
                    session.add(prospect_listing)
                    session.flush()
                    session.commit()
                    session.refresh(prospect_listing)

                    temp_image_dir = download_and_save_listing_images(
                        image_urls,
                        page,
                        prospect_listing,
                        session,
                        temp_dir_prefix="car_and_classic_images_",
                    )

                    ai_analysis = generate_ai_analysis(
                        "classic_car_prompt.md", prospect_listing, temp_image_dir
                    )
                    apply_ai_analysis(prospect_listing, ai_analysis)

                    session.add(prospect_listing)
                    session.flush()
                    session.commit()

                    if temp_image_dir and os.path.isdir(temp_image_dir):
                        shutil.rmtree(temp_image_dir)

                existing_hash_codes.add(hash_code)

                # navigate back to the search results, handling any captcha
                # that may interpose on the back-navigation
                page.go_back()
                if is_captcha_present(page):
                    wait_for_captcha_solve(page)
                page.wait_for_selector(
                    '[data-testid="card-listing"]',
                    state="attached",
                    timeout=15000,
                )
                pause()
            except Exception as e:
                print(f"  [{i + 1}] {title} — error, skipping: {e}")

                # re-load the search results so the next iteration can proceed
                try:
                    goto_with_captcha_handling(page, search_url)
                    page.wait_for_selector(
                        '[data-testid="card-listing"]',
                        state="attached",
                        timeout=15000,
                    )
                    pause()
                except Exception as recovery_error:
                    print(
                        f"      Failed to recover to search results: "
                        f"{recovery_error}"
                    )
                    raise

        # new-vehicles section has no pagination, so stop after one pass
        if use_new_section:
            break

        # check for a next-page link
        next_button = page.locator("a[data-next-page]")
        if next_button.is_visible():
            next_button.click()
            page.wait_for_selector(
                '[data-testid="card-listing"]', state="attached", timeout=15000
            )
            page_number += 1
        else:
            break

    print(f"\nFinished — {page_number} page(s) scraped, {new_count} new listing(s).")


# CAR AND CLASSIC
def car_and_classic():
    """
    Open a Playwright browser in non-headless mode, navigate to carandclassic.com,
    accept cookies, and log in using stored credentials.
    """

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        goto_with_captcha_handling(page, "https://www.carandclassic.com")

        pause()
        accept_cookies(page)

        # click the login button to reach the login screen
        pause()
        page.locator('[data-testid="nav-login"]').click()

        login(page)

        # wait for login to complete before navigating
        page.wait_for_load_state("load")

        update_new_listings_availability(page)

        # navigate to saved searches
        pause()
        goto_with_captcha_handling(
            page, "https://www.carandclassic.com/account/saved"
        )
        page.wait_for_load_state("load")

        # click the first saved search link
        pause()
        saved_search_link = page.locator(".grid a").first
        saved_search_link.wait_for(state="visible")
        saved_search_link.click()

        # vue uses client-side routing so load events don't fire; wait for content
        page.wait_for_selector(
            '[data-testid="card-listing"]', state="attached", timeout=15000
        )

        scrape_listings(page)

        # in interactive runs, keep the browser open until the user closes it
        # so they can inspect state; in non-interactive runs (e.g. the scheduler
        # subprocess) close immediately so the process exits and the next
        # scheduled run is not blocked by max_instances=1
        if sys.stdin.isatty():
            page.wait_for_event("close", timeout=0)
        browser.close()


if __name__ == "__main__":
    car_and_classic()
