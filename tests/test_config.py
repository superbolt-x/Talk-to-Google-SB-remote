"""Tests for config loading.

In remote deployment mode ``load_config`` reads settings **only from
environment variables** and ignores any config file path. These tests exercise
that contract.
"""

import pytest

from adloop.config import AdLoopConfig, load_config

# Every env var load_config reads — cleared before each test so results don't
# depend on the runner's ambient environment.
_CONFIG_ENV_VARS = [
    "GOOGLE_CLOUD_PROJECT",
    "ADLOOP_GA4_PROPERTY_ID",
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CUSTOMER_ID",
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    "ADLOOP_MAX_DAILY_BUDGET",
    "ADLOOP_MAX_BID_INCREASE_PCT",
    "ADLOOP_REQUIRE_DRY_RUN",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in _CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestLoadConfig:
    def test_returns_defaults_when_env_unset(self):
        config = load_config()
        assert isinstance(config, AdLoopConfig)
        assert config.safety.max_daily_budget == 50.0
        assert config.safety.require_dry_run is True
        assert config.ads.customer_id == ""

    def test_loads_from_env(self, monkeypatch):
        monkeypatch.setenv("ADLOOP_MAX_DAILY_BUDGET", "25.0")
        monkeypatch.setenv("ADLOOP_REQUIRE_DRY_RUN", "false")
        monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", "123-456-7890")
        config = load_config()
        assert config.safety.max_daily_budget == 25.0
        assert config.safety.require_dry_run is False
        assert config.ads.customer_id == "123-456-7890"

    def test_ga4_property_id_loaded_from_env(self, monkeypatch):
        monkeypatch.setenv("ADLOOP_GA4_PROPERTY_ID", "properties/123")
        config = load_config()
        assert config.ga4.property_id == "properties/123"

    def test_ga4_property_id_defaults_empty_for_per_call_switching(self):
        # Intentionally unset: this account switches between multiple GA4
        # properties, so there is no default — the GA4 tools take property_id
        # as a per-call argument. A default here would defeat that.
        config = load_config()
        assert config.ga4.property_id == ""

    def test_config_path_is_ignored(self, tmp_path):
        # Remote mode reads only env vars; a config file must NOT override them.
        config_file = tmp_path / "config.yaml"
        config_file.write_text("safety:\n  max_daily_budget: 25.0\n")
        config = load_config(str(config_file))
        assert config.safety.max_daily_budget == 50.0  # default, not YAML's 25.0
