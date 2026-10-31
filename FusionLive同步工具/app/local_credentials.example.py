"""Copy to local_credentials.py and configure credentials in the environment."""

import os


def get_credentials():
    return (
        os.environ.get("FUSIONLIVE_USERNAME", ""),
        os.environ.get("FUSIONLIVE_PASSWORD", ""),
    )
