"""
Synchronize ICE AI database models using sqlacodegen-v2.

Generates SQLAlchemy 2.0 dataclass models from the ice-ai, anna-trainer,
and auto-ads databases and saves them to the python/models directory.
"""

import os
import re
import sys
from importlib.metadata import entry_points
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.schema import MetaData

# add the python directory to sys.path so we can import environments
script_dir = Path(str(__file__)).resolve().parent
python_dir = script_dir.parent / "python"
sys.path.insert(0, str(python_dir))

from environments import load_environment


# ENSURE MODELS DIRECTORY
def ensure_models_directory(models_dir: Path) -> None:
    """
    Create the models directory if it doesn't exist.
    """

    if not models_dir.exists():
        models_dir.mkdir(parents=True)
        print(f"Created models directory: {models_dir}")

    # create __init__.py if it doesn't exist
    init_file = models_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text(
            '"""SQLAlchemy models generated from ICE AI databases."""\n'
        )
        print(f"Created {init_file}")


# FIX ENUM SCHEMA
def fix_enum_schema(output_file: Path, schema: str) -> None:
    """
    Fix ENUM definitions to include the schema parameter.

    sqlacodegen-v2 generates ENUMs without the schema parameter, which causes
    errors when the enum type is defined in a non-public schema. This function
    post-processes the generated file to add the schema parameter and remove
    the incorrectly duplicated enum name from positional arguments.
    """

    content = output_file.read_text()

    # pattern to match ENUM(..., name='enum_name')
    # captures everything inside the ENUM parentheses
    enum_pattern = r"ENUM\(([^)]+)\)"

    def fix_enum_match(match: re.Match) -> str:
        """
        Fix a single ENUM definition by adding schema and removing duplicate name.
        """

        enum_content = match.group(1)

        # extract the name parameter value
        name_match = re.search(r"name='([^']+)'", enum_content)
        if not name_match:
            return match.group(0)

        enum_name = name_match.group(1)

        # split into individual arguments
        parts = [p.strip() for p in enum_content.split(",")]

        # separate positional and keyword arguments
        positional_args = []
        keyword_args = []
        for part in parts:
            if "=" in part:
                keyword_args.append(part)
            else:
                positional_args.append(part)

        # remove the first positional arg if it matches the enum name
        if positional_args and positional_args[0].strip("'\"") == enum_name:
            positional_args = positional_args[1:]

        # add schema to keyword args if not already present
        has_schema = any("schema=" in arg for arg in keyword_args)
        if not has_schema:
            keyword_args.append(f"schema='{schema}'")

        # reconstruct the ENUM call
        all_args = positional_args + keyword_args
        return f"ENUM({', '.join(all_args)})"

    new_content = re.sub(enum_pattern, fix_enum_match, content)

    if new_content != content:
        output_file.write_text(new_content)
        print(f"  Fixed ENUM schema references in {output_file.name}")


# GENERATE MODELS
def generate_models(
    database_url: str, schema: str, output_file: Path, database_name: str
) -> bool:
    """
    Generate SQLAlchemy 2.0 declarative models from a database using sqlacodegen-v2.

    Runs sqlacodegen in-process so PyCharm's debugger does not inject pydevd into
    child subprocesses (which causes ConnectionRefusedError when debugging).
    """

    print(f"\n{'='*60}")
    print(f"Generating models for {database_name}")
    print(f"{'='*60}")
    print(f"Schema: {schema}")
    print(f"Output: {output_file}")

    try:
        # load the declarative generator (not dataclasses, to avoid blank-name errors)
        generators = {
            ep.name: ep for ep in entry_points(group="sqlacodegen_v2.generators")
        }
        if "declarative" not in generators:
            raise ImportError("declarative generator not found in sqlacodegen_v2")

        generator_class = generators["declarative"].load()

        # reflect schema and write generated models
        engine = create_engine(database_url)
        metadata = MetaData()
        metadata.reflect(engine, schema, False, None)

        generator = generator_class(metadata, engine, set())
        output_file.write_text(generator.generate(), encoding="utf-8")

        print(f"Successfully generated models for {database_name}")
        return True

    except ImportError:
        print("Error: sqlacodegen-v2 is not installed.")
        print("Please install it with: pip install sqlacodegen-v2")
        return False

    except Exception as e:
        print(f"Error generating models for {database_name}:")
        print(f"  {type(e).__name__}: {e}")
        return False


# MAIN
def main() -> int:
    """
    Main entry point for the database synchronization script.
    """

    print("ICE AI Database Model Synchronization")
    print("=====================================\n")

    # load environment variables
    load_environment()

    # get database configuration from environment
    ice_ai_url = os.getenv("ICE_AI_DATABASE_URL")
    ice_ai_schema = os.getenv("ICE_AI_DATABASE_SCHEMA")
    anna_trainer_url = os.getenv("ANNA_TRAINER_DATABASE_URL")
    anna_trainer_schema = os.getenv("ANNA_TRAINER_DATABASE_SCHEMA")
    auto_ads_url = os.getenv("AUTO_ADS_DATABASE_URL")
    auto_ads_schema = os.getenv("AUTO_ADS_DATABASE_SCHEMA")

    # validate required environment variables
    missing_vars = []
    if not ice_ai_url:
        missing_vars.append("ICE_AI_DATABASE_URL")
    if not ice_ai_schema:
        missing_vars.append("ICE_AI_DATABASE_SCHEMA")
    if not anna_trainer_url:
        missing_vars.append("ANNA_TRAINER_DATABASE_URL")
    if not anna_trainer_schema:
        missing_vars.append("ANNA_TRAINER_DATABASE_SCHEMA")
    if not auto_ads_url:
        missing_vars.append("AUTO_ADS_DATABASE_URL")
    if not auto_ads_schema:
        missing_vars.append("AUTO_ADS_DATABASE_SCHEMA")

    if missing_vars:
        print("Error: Missing required environment variables:")
        for var in missing_vars:
            print(f"  - {var}")
        return 1

    # set up paths
    models_dir = python_dir / "models"
    ensure_models_directory(models_dir)

    # track success
    success = True

    # generate ice-ai models
    ice_ai_output = models_dir / "ice_ai.py"
    if generate_models(ice_ai_url, ice_ai_schema, ice_ai_output, "ice-ai"):
        fix_enum_schema(ice_ai_output, ice_ai_schema)
    else:
        success = False

    # generate anna-trainer models
    anna_trainer_output = models_dir / "anna_trainer.py"
    if generate_models(
        anna_trainer_url, anna_trainer_schema, anna_trainer_output, "anna-trainer"
    ):
        fix_enum_schema(anna_trainer_output, anna_trainer_schema)
    else:
        success = False

    # generate auto-ads models
    auto_ads_output = models_dir / "auto_ads.py"
    if generate_models(auto_ads_url, auto_ads_schema, auto_ads_output, "auto-ads"):
        fix_enum_schema(auto_ads_output, auto_ads_schema)
    else:
        success = False

    # summary
    print(f"\n{'='*60}")
    print("Summary")
    print(f"{'='*60}")

    if success:
        print("All models generated successfully!")
        print(f"\nGenerated files:")
        print(f"  - {ice_ai_output}")
        print(f"  - {anna_trainer_output}")
        print(f"  - {auto_ads_output}")
        return 0
    else:
        print("Some models failed to generate. Check the errors above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
