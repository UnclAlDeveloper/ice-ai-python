import argparse
import os
import sys


# RESOLVE RUNTIME FLAGS
def resolve_runtime_flags() -> bool:
    """
    Parse --env and --headless before load_environment so the chosen env file
    is selected at import time. Sets ENVIRONMENT when --env is given, rewrites
    sys.argv to drop the pre-parsed flags, and returns whether the browser should
    run headless.
    """

    # parse --env before load_environment so the chosen env file is selected at import time
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--env", choices=["dev", "prod"], default=None)
    pre_parser.add_argument("--headless", choices=["true", "false"], default=None)
    pre_args, remaining_argv = pre_parser.parse_known_args()

    if pre_args.env is not None:
        os.environ["ENVIRONMENT"] = (
            "production" if pre_args.env == "prod" else "dev"
        )

    is_headless = True
    if pre_args.env is not None:
        is_headless = False if pre_args.headless == "false" else True

    sys.argv[:] = [sys.argv[0]] + remaining_argv

    return is_headless
