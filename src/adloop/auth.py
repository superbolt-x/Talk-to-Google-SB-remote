"""Google API authentication — env-var based credentials for remote deployment."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

_ALL_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/analytics.edit",
    "https://www.googleapis.com/auth/adwords",
]


def _credentials_from_env() -> Credentials:
    """Build OAuth2 credentials from environment variables.

    Requires GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET,
    GOOGLE_ADS_REFRESH_TOKEN. No browser or file I/O needed.
    """
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_ADS_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=_ALL_SCOPES,
    )


def get_ga4_credentials(config) -> Credentials:  # type: ignore[return]
    """Return authenticated credentials for GA4 APIs."""
    return _credentials_from_env()


def get_ads_credentials(config) -> Credentials:  # type: ignore[return]
    """Return authenticated credentials for Google Ads API."""
    return _credentials_from_env()
