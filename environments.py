import os
import re
import tempfile
from pathlib import Path

from dotenv import load_dotenv


# NORMALIZE TEMP ENVIRONMENT
def _normalize_temp_environment() -> None:
    """
    Replace unusable TMPDIR/TEMP/TMP values with a real temp directory for this
    OS. Windows PyCharm often injects C:\\Users\\...\\AppData\\Local\\Temp into
    remote Linux runs; Playwright then fails mkdtemp for playwright-artifacts-*.
    """

    # python's gettempdir already skips non-existent candidates, so it stays valid
    # even when TEMP points at a Windows path that does not exist on Linux
    fallback_temp_dir = tempfile.gettempdir()
    for name in ("TMPDIR", "TEMP", "TMP"):
        value = os.getenv(name, "").strip()
        if not value:
            os.environ[name] = fallback_temp_dir
            continue

        # drop drive-letter paths inherited from a Windows host into Linux
        if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", value):
            os.environ[name] = fallback_temp_dir
            continue

        if not os.path.isdir(value):
            os.environ[name] = fallback_temp_dir


# LOAD ENVIRONMENT
def load_environment():
    """
    Load environment variables from the shared `.env` file first, then layer the
    environment-specific file (`.env.dev` for dev/staging, `.env.prod` for
    production) on top so its values override or extend the base set.

    Reads `ENVIRONMENT` (default `dev`); accepts `dev`, `staging`, `production`.
    """

    # get environment from env var, default to dev
    env_name = os.getenv("ENVIRONMENT", "dev").lower()

    # validate environment name
    valid_environments = {"dev", "staging", "production"}
    if env_name not in valid_environments:
        raise ValueError(
            f"Invalid environment value: {env_name}. Must be one of {valid_environments}"
        )

    # use .env.dev for dev and staging, .env.prod for production
    if env_name in {"dev", "staging"}:
        override_filename = ".env.dev"
    else:
        override_filename = ".env.prod"

    # path to parent directory (one level up from this file's package)
    current_file = Path(__file__).resolve()
    parent_dir = current_file.parent.parent
    base_env_file = parent_dir / ".env"
    override_env_file = parent_dir / override_filename

    # load the shared base first; its values become defaults for the override step
    if base_env_file.exists():
        load_dotenv(base_env_file, override=True)
        print(f"Loaded base environment variables from {base_env_file}")
    else:
        print(f"Warning: base env file {base_env_file} not found")

    # layer the per-environment file on top so its keys win and any extra keys are added
    if override_env_file.exists():
        load_dotenv(override_env_file, override=True)
        print(
            f"Loaded override environment variables from {override_env_file} "
            f"(environment: {env_name})"
        )
    else:
        print(
            f"Warning: override env file {override_env_file} not found. "
            "Using base/system environment variables only."
        )

    # scrub windows temp paths before playwright or other tools inherit them
    _normalize_temp_environment()
