import os
import random
import time

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


def pause(min_seconds: float = 1.0, max_seconds: float = 3.0):
    """
    Wait a random amount of time to simulate human browsing behavior.
    """

    delay = random.uniform(min_seconds, max_seconds)
    time.sleep(delay)


def extract_and_print_listing(list_item):
    """
    Extract and print a single listing's information.
    """

    # extract individual fields from the listing
    title_link = list_item.locator('a[data-testid="search-listing-title"]')
    if title_link.count() == 0:
        return False

    # get the van name (text content of the link, excluding hidden span)
    van_name = title_link.evaluate("el => el.childNodes[0].textContent").strip()

    # get the listing url from the href attribute
    listing_url = title_link.get_attribute("href")
    if listing_url and not listing_url.startswith("http"):
        listing_url = "https://www.autotrader.co.uk" + listing_url

    # get subtitle
    subtitle = list_item.locator('p[data-testid="search-listing-subtitle"]')
    short_description = subtitle.inner_text() if subtitle.count() > 0 else ""

    # get mileage
    mileage = list_item.locator('li[data-testid="mileage"]')
    mileage_text = mileage.inner_text() if mileage.count() > 0 else ""
    mileage_unit = mileage_text.split(" ")[-1] if " " in mileage_text else None
    mileage_value_text = mileage_text.split(" ")[0] if " " in mileage_text else mileage_text
    mileage_value = int(mileage_value_text.replace(",", "")) if mileage_value_text else None

    # get year
    year = list_item.locator('li[data-testid="registered_year"]')
    year_text = year.inner_text() if year.count() > 0 else ""
    year_value = int(year_text.split(" ")[0]) if " " in year_text else int(year_text) if year_text.isdigit() else None
    reg_text = year_text.split("(")[-1].replace(")", "") if "(" in year_text else None

    # get price - find span that starts with £ symbol (class names are unstable)
    price_text = list_item.evaluate(
        """(element) => {
            const spans = element.querySelectorAll('span');
            for (const span of spans) {
                const text = span.textContent.trim();
                if (text.startsWith('£') || text.startsWith('€') || text.startsWith('$')) {
                    return text;
                }
            }
            return '';
        }"""
    )
    currency_symbol = price_text[0] if price_text else None
    price_value = int(price_text[1:].replace(",", "").split(" ")[0]) if " " in price_text else  int(price_text[1:].replace(",", "")) if price_text else None
    vat_status = " ".join(price_text.split(" ")[1:]).strip() if " " in price_text else None

    # get location
    location = list_item.locator('span[data-testid="search-listing-location"]')
    location_text = location.inner_text() if location.count() > 0 else ""
    location_city = location_text.split("(")[0].strip() if "(" in location_text else location_text.strip() if location_text else None

    # print the extracted info
    print(van_name)
    if short_description:
        print(short_description)
    if mileage_text:
        print(f"{mileage_value:,}", mileage_unit)
    if year_text:
        print(year_value, reg_text)
    if price_text:
        print(currency_symbol, price_value, vat_status)
    if location_text:
        print(location_city)
    if listing_url:
        print(listing_url)

    print("\n------\n")

    return True


def main():
    """
    Navigate to autotrader.co.uk using Playwright with visible browser.
    """

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

        # load email from .env file
        load_dotenv()
        email = os.getenv("AUTOTRADER_EMAIL")
        if not email:
            raise ValueError("AUTOTRADER_EMAIL not found in .env file")

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
        printed_listing_ids = set()
        scroll_attempts = 0
        max_scroll_attempts = 100  # prevent infinite scrolling

        while scroll_attempts < max_scroll_attempts:
            # scroll down by one page height
            page.evaluate("window.scrollBy(0, window.innerHeight)")
            pause(min_seconds=1.0, max_seconds=2.0)  # pause between scrolls

            # wait a bit for content to load
            page.wait_for_timeout(1000)

            # find all currently visible list items
            list_items = page.locator('li[data-testid^="id-"]').all()

            # extract and print any new listings
            for list_item in list_items:
                # get the listing ID from the data-testid attribute
                listing_id = list_item.get_attribute("data-testid")
                if listing_id and listing_id not in printed_listing_ids:
                    # check if this listing has a title (is fully loaded)
                    title_link = list_item.locator(
                        'a[data-testid="search-listing-title"]'
                    )
                    if title_link.count() > 0:
                        extract_and_print_listing(list_item)
                        printed_listing_ids.add(listing_id)

            # check if we've reached the bottom
            current_height = page.evaluate("document.body.scrollHeight")
            current_scroll = page.evaluate("window.scrollY + window.innerHeight")
            if current_scroll >= current_height:
                # reached the bottom of the page
                break
            scroll_attempts += 1

        print(f"\nFinished scrolling after {scroll_attempts} scroll operations")
        print(f"Found {len(printed_listing_ids)} listings total")

        print("Press Enter to close the browser...")
        input()
        browser.close()


if __name__ == "__main__":
    main()
