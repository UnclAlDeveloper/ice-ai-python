from functools import lru_cache
import json
import os
from pathlib import Path
import urllib.parse


# GET APP SETTINGS
def get_app_settings():
    """
    Load application settings from the appsettings.json file, searching upward from the home directory.
    """

    path = Path.home()
    while not Path.exists(Path(os.path.join(Path.home(), "appsettings.json"))) and path.parent != path:
        path = path.parent
    if Path.exists(Path(os.path.join(Path.home(), "appsettings.json"))):
        app_settings = json.load(open(os.path.join(Path.home(), "appsettings.json")))
    elif Path.exists(Path(os.path.join("/home", "appsettings.json"))):
        app_settings = json.load(open(os.path.join("/home", "appsettings.json")))
    else:
        app_settings = json.loads("{}")

    return app_settings


# GET ENV VARIABLE
@lru_cache(maxsize=None)
def get_env_variable(name: str) -> str:
    """
    Retrieve an environment variable, checking local environment variables first, then the MIEnvVariables section of appsettings.json.
    Supports nested settings using colon notation (e.g., "Section:Key").
    """

    # try local variables first
    if ":" not in name:
        variable_value = os.getenv(name)
        if variable_value is not None:
            return os.getenv(name)

    # try app settings if that doesn't work
    app_settings = get_app_settings()
    if ":" in name:
        variable_value = app_settings[name.split(":")[0]][name.split(":")[-1]]
    else:
        variable_value = app_settings["MIEnvVariables"][name]

    return variable_value


# DECRYPT SETTING
def decrypt_setting(text: str):
    """
    Decrypt an encrypted setting by encoding it and calling the MI API decryption endpoint.
    """

    # encode the text
    encoded_text = urllib.parse.quote(text, safe="")

    # call the API to get the decrypted text
    response = requests.get(f"https://imperium.mindlessinvesting.com/mi-api/decrypt-text&text={encoded_text}")

    return response.text

