"""Google API authentication — OAuth 2.0 and service account support."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

    from adloop.config import AdLoopConfig

# Request all scopes in a single OAuth flow so one token works for both
# GA4 and Google Ads. Without this, separate tokens would constantly
# overwrite each other at the same token_path.
_ALL_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/analytics.edit",
    "https://www.googleapis.com/auth/adwords",
]

_GA4_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/analytics.edit",
]

_ADS_SCOPES = [
    "https://www.googleapis.com/auth/adwords",
]


def get_ga4_credentials(config: AdLoopConfig) -> Credentials:
    """Return authenticated credentials for GA4 APIs."""
    creds_path = Path(config.google.credentials_path).expanduser()

    if creds_path.exists():
        import json

        with open(creds_path) as f:
            creds_info = json.load(f)

        if creds_info.get("type") == "service_account":
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(
                str(creds_path),
                scopes=_GA4_SCOPES,
            )

        return _oauth_flow(config)

    import google.auth

    credentials, _ = google.auth.default(scopes=_GA4_SCOPES)
    return credentials


def get_ads_credentials(config: AdLoopConfig) -> Credentials:
    """Return authenticated credentials for Google Ads API."""
    creds_path = Path(config.google.credentials_path).expanduser()

    if creds_path.exists():
        import json

        with open(creds_path) as f:
            creds_info = json.load(f)

        if creds_info.get("type") == "service_account":
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(
                str(creds_path),
                scopes=_ADS_SCOPES,
            )

        return _oauth_flow(config)

    import google.auth

    credentials, _ = google.auth.default(scopes=_ADS_SCOPES)
    return credentials


def _oauth_flow(config: AdLoopConfig) -> Credentials:
    """Run OAuth Desktop flow requesting all scopes (GA4 + Ads).

    Uses a single token file for all scopes to avoid conflicts between
    GA4 and Ads auth sharing the same token_path.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials as OAuthCredentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = Path(config.google.token_path).expanduser()
    creds_path = Path(config.google.credentials_path).expanduser()

    creds = None
    if token_path.exists():
        creds = OAuthCredentials.from_authorized_user_file(
            str(token_path), _ALL_SCOPES
        )

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            err_str = str(exc).lower()
            if "revoked" in err_str or "invalid_grant" in err_str:
                token_path.unlink(missing_ok=True)
                raise RuntimeError(
                    "OAuth token has been revoked or expired. "
                    "This typically happens when the Google Cloud consent screen "
                    "is in 'Testing' mode (tokens expire after 7 days). "
                    "Fix: (1) re-run any AdLoop tool to trigger re-authorization, "
                    "(2) publish the consent screen to 'In production' in Google "
                    "Cloud Console to prevent future expiry."
                ) from exc
            raise
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(creds_path), _ALL_SCOPES
        )
        
        auth_url, _ = flow.authorization_url(prompt="consent")
        
        print("\n🔐 Google Authorization Required")
        print("Open this URL in your browser:\n")
        print(auth_url)
        print()
        
        creds = flow.run_local_server(
            port=0,
            open_browser=False  # 👈 prevents silent failure
        )

    token_path.parent.mkdir(parents=True, exist_ok=True)
    with open(token_path, "w") as f:
        f.write(creds.to_json())

    return creds
