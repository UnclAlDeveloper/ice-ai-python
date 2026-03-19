import os
import re
import shutil
import tempfile
from datetime import date
from typing import TYPE_CHECKING, Optional

from environments import load_environment
load_environment()

from google import genai

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
from google.genai import types
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from AWSAccess import AWSAccess
from environments import load_environment
from models.auto_ads import Images, ProspectListings
from models.enums import ListingTable


# CONVERT FOUND LISTING TO MARKDOWN
def convert_prospect_listing_to_markdown(prospect_listing: ProspectListings) -> str:
    """
    Convert a ProspectListings object to a markdown formatted string for use in
    ai analysis prompts.
    """

    # helper function to format optional values
    def format_value(value, default="N/A"):
        return str(value) if value is not None else default

    # build markdown sections
    markdown_parts = []

    # header with make and model
    markdown_parts.append(
        f"# {format_value(prospect_listing.make_and_model, 'Unknown')}"
    )

    # short description
    markdown_parts.append(
        f"## {format_value(prospect_listing.short_description, 'No description')}"
    )
    markdown_parts.append("")

    # overview section
    markdown_parts.append("## Overview")

    # mileage with unit
    mileage_str = format_value(prospect_listing.mileage)
    if prospect_listing.mileage_unit:
        mileage_str += f" {prospect_listing.mileage_unit}"
    markdown_parts.append(f"- Mileage: {mileage_str}")

    markdown_parts.append(f"- Year: {format_value(prospect_listing.year)}")
    markdown_parts.append(
        f"- Registration: {format_value(prospect_listing.registration)}"
    )
    markdown_parts.append(f"- Body type: {format_value(prospect_listing.body_type)}")
    markdown_parts.append(f"- Cab type: {format_value(prospect_listing.cab_type)}")
    markdown_parts.append(f"- Wheelbase: {format_value(prospect_listing.wheelbase)}")
    markdown_parts.append(f"- Engine: {format_value(prospect_listing.engine_size)}")
    markdown_parts.append(
        f"- Emission class: {format_value(prospect_listing.emission_class)}"
    )
    markdown_parts.append(f"- Gearbox: {format_value(prospect_listing.gearbox_type)}")
    markdown_parts.append(f"- Fuel type: {format_value(prospect_listing.fuel_type)}")
    markdown_parts.append(f"- Seats: {format_value(prospect_listing.seats)}")
    markdown_parts.append(f"- Colour: {format_value(prospect_listing.colour)}")
    markdown_parts.append("")

    # specs and features section
    if prospect_listing.specs_and_features:
        markdown_parts.append(prospect_listing.specs_and_features)
        markdown_parts.append("")

    # description section
    markdown_parts.append("## Description")
    markdown_parts.append(
        format_value(prospect_listing.full_description, "No description available")
    )
    markdown_parts.append("")

    # history section
    markdown_parts.append("## History")
    markdown_parts.append(
        f"- Owners: {format_value(prospect_listing.number_of_owners)}"
    )

    # service history subsection
    markdown_parts.append("### Service history:")
    markdown_parts.append(
        format_value(prospect_listing.service_history, "Not available")
    )
    markdown_parts.append("")

    # basic checks subsection
    markdown_parts.append("### Basic checks (out of 5)")
    markdown_parts.append(
        format_value(prospect_listing.basic_history_check, "Not available")
    )
    markdown_parts.append("")

    # mot status subsection
    markdown_parts.append("### MOT status")
    mot_info = format_value(prospect_listing.mot_status, "Not available")
    markdown_parts.append(mot_info)

    return "\n".join(markdown_parts)


# GENERATE AI ANALYSIS
def generate_ai_analysis(
    prompt_filename: str, prospect_listing: ProspectListings, temp_image_dir: Optional[str] = None
) -> str:
    """
    Generate ai analysis for a found listing using Google Gemini API.
    Reads the ai prompt template, converts the listing to markdown,
    and sends it to Gemini for analysis along with any images from the temp directory.
    """

    # get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    prompt_path = os.path.join(script_dir, prompt_filename)

    # read the prompt template
    with open(prompt_path, "r", encoding="utf-8") as f:
        prompt = f.read()

    # substitute {date} placeholder with current date
    prompt = prompt.replace("{date}", date.today().strftime("%Y-%m-%d"))

    # convert found listing to markdown
    listing_markdown = convert_prospect_listing_to_markdown(prospect_listing)

    # append the listing markdown to the prompt
    full_prompt = prompt + "\n\n" + listing_markdown

    # get API key and model name from environment variables
    api_key = os.getenv("AUTO_ADS_GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("AUTO_ADS_GOOGLE_API_KEY not found in environment variables")

    model_name = os.getenv("GEMINI_MODEL_NAME")
    if not model_name:
        raise ValueError("GEMINI_MODEL_NAME not found in environment variables")

    # configure the Gemini client with API key
    client = genai.Client(api_key=api_key)

    # build the contents list with the text prompt
    contents = [full_prompt]

    # add images from temp directory if provided
    if temp_image_dir and os.path.isdir(temp_image_dir):
        # map file extensions to mime types
        extension_to_mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }

        # iterate through image files in the temp directory
        for filename in sorted(os.listdir(temp_image_dir)):
            file_path = os.path.join(temp_image_dir, filename)

            # skip if not a file
            if not os.path.isfile(file_path):
                continue

            # get file extension and determine mime type
            _, ext = os.path.splitext(filename.lower())
            mime_type = extension_to_mime.get(ext)

            # skip unsupported file types
            if not mime_type:
                continue

            # read the image bytes and create a Part
            with open(file_path, "rb") as f:
                image_bytes = f.read()

            image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
            contents.append(image_part)

    # generate the content
    response = client.models.generate_content(model=model_name, contents=contents)

    return response.text


# APPLY AI ANALYSIS
def apply_ai_analysis(
    prospect_listing: ProspectListings, ai_analysis: str
) -> ProspectListings:
    """
    Parse the markdown ai analysis and populate the AI-generated fields
    on the prospect_listing object. Returns the updated prospect_listing.
    """

    # helper to extract price as integer from string like "£27,500"
    def parse_price(price_str: str) -> Optional[int]:
        if not price_str:
            return None
        # remove currency symbol, commas, and whitespace
        cleaned = re.sub(r"[£,\s]", "", price_str)
        try:
            return int(cleaned)
        except ValueError:
            return None

    # helper to extract section content between headers
    def extract_section(content: str, header: str) -> Optional[str]:
        # match header with optional leading whitespace, and capture content until the next header
        # the next header can also have leading whitespace on the line
        pattern = rf"^\s*#\s*{header}\s*\n(.*?)(?=^\s*#\s|\Z)"
        match = re.search(pattern, content, re.DOTALL | re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip()
        return None

    # strip markdown code fence if present
    analysis = ai_analysis.strip()
    if analysis.startswith("```markdown"):
        analysis = analysis[len("```markdown") :].strip()
    elif analysis.startswith("```"):
        analysis = analysis[3:].strip()
    if analysis.endswith("```"):
        analysis = analysis[:-3].strip()

    # extract overview section
    overview = extract_section(analysis, "Overview")
    prospect_listing.ai_resell_overview = overview if overview else None

    # extract repair costs table
    repair_costs = extract_section(analysis, "Repair costs")
    prospect_listing.ai_work_and_repairs = repair_costs if repair_costs else None

    # extract notes section
    notes = extract_section(analysis, "Notes")
    prospect_listing.ai_resell_notes = notes if notes else None

    # extract value added improvements section
    value_add = extract_section(analysis, "Value added improvements")
    prospect_listing.ai_value_add_improvements = value_add if value_add else None

    # extract campervan conversion section
    campervan_conversion = extract_section(analysis, "Campervan Conversion")
    prospect_listing.ai_campervan_conversion = campervan_conversion if campervan_conversion else None

    # extract market section
    market = extract_section(analysis, "Market")
    prospect_listing.ai_target_market = market if market else None

    # extract price ranges section
    price_section = extract_section(analysis, "Price ranges")
    if price_section:
        # parse each price line
        low_buy_match = re.search(
            r"\*{0,2}Low buy price:?\*{0,2}:?\s*(£[\d,]+)", price_section, re.IGNORECASE
        )
        high_buy_match = re.search(
            r"\*{0,2}High buy price:?\*{0,2}:?\s*(£[\d,]+)", price_section, re.IGNORECASE
        )
        repair_cost_match = re.search(
            r"\*{0,2}Expected repair cost:?\*{0,2}:?\s*(£[\d,]+)", price_section, re.IGNORECASE
        )
        low_sell_match = re.search(
            r"\*{0,2}Low sell price:?\*{0,2}:?\s*(£[\d,]+)", price_section, re.IGNORECASE
        )
        high_sell_match = re.search(
            r"\*{0,2}High sell price:?\*{0,2}:?\s*(£[\d,]+)", price_section, re.IGNORECASE
        )
        prospect_listing.ai_buy_price_low = parse_price(low_buy_match.group(1)) if low_buy_match else None
        prospect_listing.ai_buy_price_high = parse_price(high_buy_match.group(1)) if high_buy_match else None
        prospect_listing.ai_repair_cost = parse_price(repair_cost_match.group(1)) if repair_cost_match else None
        prospect_listing.ai_sell_price_low = parse_price(low_sell_match.group(1)) if low_sell_match else None
        prospect_listing.ai_sell_price_high = parse_price(high_sell_match.group(1)) if high_sell_match else None
    else:
        prospect_listing.ai_buy_price_low = None
        prospect_listing.ai_buy_price_high = None
        prospect_listing.ai_repair_cost = None
        prospect_listing.ai_sell_price_low = None
        prospect_listing.ai_sell_price_high = None

    return prospect_listing


# PROCESS AI ANALYSIS FOR LISTING
def process_ai_analysis_for_listing(
    prompt_filename: str,
    prospect_listing: ProspectListings,
    session: "Session",
    temp_image_dir: Optional[str] = None,
) -> ProspectListings:
    """
    Generate ai analysis, apply it to the prospect listing, flush and commit
    the session, and clean up the temp image directory. Returns the updated listing.
    Use this after saving a prospect listing and its images to run the full
    ai analysis workflow.
    """

    # generate and apply ai analysis using temp image directory
    try:
        ai_analysis = generate_ai_analysis(prompt_filename, prospect_listing, temp_image_dir)
        prospect_listing = apply_ai_analysis(prospect_listing, ai_analysis)

        # resave prospect listing to database with ai fields
        session.add(prospect_listing)
        session.flush()
        session.commit()
    except Exception as e:
        pass

    return prospect_listing


# REGENERATE ALL AI ANALYSES
def regenerate_all_ai_analyses(prompt_filename: str, listing_source: str, skip_rows: int = 0):
    """
    Iterate through all prospect_listings for a given listing source and
    regenerate the ai analysis for each one using the AI model. Rows are
    ordered by id and the first skip_rows rows are skipped.
    """

    load_environment()

    database_url = os.getenv("AUTO_ADS_DATABASE_URL")
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        prospect_listings = (
            session.query(ProspectListings)
            .filter(ProspectListings.listing_source == listing_source)
            .order_by(ProspectListings.id)
            .offset(skip_rows)
            .all()
        )
        total_count = len(prospect_listings)
        print(f"Found {total_count} prospect listings to process")

        aws_access = AWSAccess(
            bucket_name=os.getenv("AUTO_ADS_BUCKET"),
            media_dir="images",
            sub_directory_name="prospects",
        )

        for index, prospect_listing in enumerate(prospect_listings, start=1):
            temp_image_dir = None
            try:
                print(
                    f"Processing {index}/{total_count}: {prospect_listing.make_and_model} "
                    f"(ID: {prospect_listing.id})"
                )

                # download images from s3 into a temporary directory
                image_records = (
                    session.query(Images)
                    .filter(
                        Images.listing_id == prospect_listing.id,
                        Images.listing_table == ListingTable.PROSPECT,
                    )
                    .all()
                )

                if image_records:
                    temp_image_dir = tempfile.mkdtemp(prefix="ai_analysis_images_")
                    for img in image_records:
                        # extract hash name and extension from the s3 url
                        filename = img.url.rsplit("/", 1)[-1]
                        name, ext = os.path.splitext(filename)
                        ext = ext.lstrip(".")
                        local_path = os.path.join(temp_image_dir, filename)
                        aws_access.download_media_to_file(name, local_path, ext)

                ai_analysis = generate_ai_analysis(
                    prompt_filename, prospect_listing, temp_image_dir
                )
                apply_ai_analysis(prospect_listing, ai_analysis)

                session.commit()
                print(f"  Successfully updated listing {prospect_listing.id}")

            except Exception as e:
                print(f"  Error processing listing {prospect_listing.id}: {e}")
                session.rollback()
                continue
            finally:
                if temp_image_dir and os.path.isdir(temp_image_dir):
                    shutil.rmtree(temp_image_dir)

    print(f"Finished processing {total_count} prospect listings")


if __name__ == "__main__":
    regenerate_all_ai_analyses("van_prompt.md", "ManualEntry", 0)
