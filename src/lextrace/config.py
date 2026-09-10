"""Application metadata and ingestion-only environment configuration."""

import os

APP_TITLE = "LexTrace"
COURTLISTENER_API_BASE_URL = "https://www.courtlistener.com/api/rest/v4/"
COURTLISTENER_TIMEOUT = 30.0


class ConfigurationError(Exception):
    """A safe configuration message containing no supplied values."""


def courtlistener_token() -> str:
    """Read credentials lazily, without affecting CLI help or the health API."""
    token = os.environ.get("COURTLISTENER_API_TOKEN", "").strip()
    if not token:
        raise ConfigurationError("Set COURTLISTENER_API_TOKEN before ingesting a case.")
    if not token.isascii() or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token
    ):
        raise ConfigurationError("COURTLISTENER_API_TOKEN has an invalid format.")
    return token
