"""Load AdLoop configuration from environment variables for remote deployment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class GoogleConfig:
    project_id: str = ""
    credentials_path: str = ""
    token_path: str = ""


@dataclass
class GA4Config:
    property_id: str = ""


@dataclass
class AdsConfig:
    developer_token: str = ""
    customer_id: str = ""
    login_customer_id: str = ""


@dataclass
class SafetyConfig:
    max_daily_budget: float = 50.0
    max_bid_increase_pct: int = 100
    require_dry_run: bool = True
    log_file: str = "/tmp/adloop-audit.log"
    blocked_operations: list[str] = field(default_factory=list)


@dataclass
class AdLoopConfig:
    google: GoogleConfig = field(default_factory=GoogleConfig)
    ga4: GA4Config = field(default_factory=GA4Config)
    ads: AdsConfig = field(default_factory=AdsConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)


def load_config(config_path: str | None = None) -> AdLoopConfig:
    """Load configuration from environment variables.

    config_path is ignored in remote mode — all settings come from env vars.
    """
    return AdLoopConfig(
        google=GoogleConfig(
            project_id=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        ),
        ga4=GA4Config(
            property_id=os.environ.get("ADLOOP_GA4_PROPERTY_ID", ""),
        ),
        ads=AdsConfig(
            developer_token=os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", ""),
            customer_id=os.environ.get("GOOGLE_ADS_CUSTOMER_ID", ""),
            login_customer_id=os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", ""),
        ),
        safety=SafetyConfig(
            max_daily_budget=float(os.environ.get("ADLOOP_MAX_DAILY_BUDGET", "50.0")),
            max_bid_increase_pct=int(os.environ.get("ADLOOP_MAX_BID_INCREASE_PCT", "100")),
            require_dry_run=os.environ.get("ADLOOP_REQUIRE_DRY_RUN", "true").lower() != "false",
            log_file="/tmp/adloop-audit.log",
        ),
    )
