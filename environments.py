import os
from pathlib import Path

from dotenv import load_dotenv


def load_environment():
    """
    Load environment variables from the appropriate .env file based on app type and environment.
    
    Determines app type (ice-ai or uncl-al) by checking ICE_AI_ENVIRONMENT and UNCL_AL_ENVIRONMENT
    variables. Loads .env.{prefix}.dev for both dev and staging environments, and
    .env.{prefix}.production for production. Files are loaded from the UncorrelatedAlpha
    directory level (parent directory).
    """

    # determine app type and get environment value
    ice_ai_env = os.getenv("ICE_AI_ENVIRONMENT")
    uncl_al_env = os.getenv("UNCL_AL_ENVIRONMENT")

    if ice_ai_env:
        app_prefix = "ice-ai"
        env_name = ice_ai_env.lower()
    elif uncl_al_env:
        app_prefix = "uncl-al"
        env_name = uncl_al_env.lower()
    else:
        # default to ice-ai dev if neither is set
        app_prefix = "ice-ai"
        env_name = "dev"
        print("Warning: Neither ICE_AI_ENVIRONMENT nor UNCL_AL_ENVIRONMENT is set. Defaulting to ice-ai dev.")

    # validate environment name
    valid_environments = {"dev", "staging", "production"}
    if env_name not in valid_environments:
        raise ValueError(
            f"Invalid environment value: {env_name}. Must be one of {valid_environments}"
        )

    # determine which file to load - use .dev for both dev and staging
    if env_name in {"dev", "staging"}:
        env_suffix = "dev"
    else:
        env_suffix = "production"

    # get path to parent directory (UncorrelatedAlpha)
    current_file = Path(__file__).resolve()
    parent_dir = current_file.parent.parent

    # construct path to environment file
    env_file = parent_dir / f".env.{app_prefix}.{env_suffix}"

    # load the environment file
    if env_file.exists():
        load_dotenv(env_file, override=True)
        print(f"Loaded environment variables from {env_file} (app: {app_prefix}, environment: {env_name})")
    else:
        print(f"Warning: {env_file} not found. Using system environment variables only.")
