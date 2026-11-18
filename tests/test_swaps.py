"""Tests for in-place RSA swaps and asset swaps (no API calls)."""

from unittest.mock import patch

import pytest

from adloop.ads import swaps
from adloop.config import AdLoopConfig, SafetyConfig


def _items(*texts, pins=None):
    pins = pins or {}
    return [{"text": t, "pinned_field": pins.get(t, "UNSPECIFIED")} for t in texts]


HEADLINES = _items("Erie Home", "Check Your Zip", "Metal Roof Replacement", pins={"Erie Home": "HEADLINE_1"})


class TestApplyTextSwaps:
    def _run(self, current, swap_list, **kw):
        args = dict(kind="headline", max_len=30, min_count=3, max_count=15)
        args.update(kw)
        return swaps.apply_text_swaps(current, swap_list, **args)

    def test_swap_keeps_position_and_pin(self):
        new, errors = self._run(HEADLINES, [{"old": "Erie Home", "new": "Erie Home Roofs"}])
        assert errors == []
        assert new[0] == {"text": "Erie Home Roofs", "pinned_field": "HEADLINE_1"}
        assert [h["text"] for h in new[1:]] == ["Check Your Zip", "Metal Roof Replacement"]

    def test_does_not_mutate_input(self):
        self._run(HEADLINES, [{"old": "Erie Home", "new": "Other"}])
        assert HEADLINES[0]["text"] == "Erie Home"

    def test_old_text_missing(self):
        _, errors = self._run(HEADLINES, [{"old": "Not There", "new": "X"}])
        assert any("is not in this ad" in e for e in errors)

    def test_too_long(self):
        _, errors = self._run(HEADLINES, [{"old": "Check Your Zip", "new": "x" * 31}])
        assert any("max 30" in e for e in errors)

    def test_duplicate_after_swap(self):
        _, errors = self._run(HEADLINES, [{"old": "Check Your Zip", "new": "Erie Home"}])
        assert any("Duplicate" in e for e in errors)

    def test_add_when_old_empty(self):
        new, errors = self._run(HEADLINES, [{"old": "", "new": "Book an Erie Home Consult"}])
        assert errors == []
        assert new[-1] == {"text": "Book an Erie Home Consult", "pinned_field": "UNSPECIFIED"}

    def test_count_limit(self):
        full = _items(*[f"H{i}" for i in range(15)])
        _, errors = self._run(full, [{"old": "", "new": "One more"}])
        assert any("must be 3-15" in e for e in errors)


class TestValidateNewAsset:
    def test_sitelink_ok(self):
        assert swaps.validate_new_asset("SITELINK", {
            "link_text": "Erie Home Financing",
            "description1": "Same-day financing decisions",
            "description2": "No payments for 90 days",
            "final_url": "https://roof.eriehome.com/x",
        }) == []

    def test_sitelink_limits(self):
        errors = swaps.validate_new_asset("SITELINK", {
            "link_text": "x" * 26, "description1": "y" * 36, "description2": "ok", "final_url": "u",
        })
        assert any("max 25" in e for e in errors)
        assert any("max 35" in e for e in errors)

    def test_sitelink_needs_both_descriptions(self):
        errors = swaps.validate_new_asset("SITELINK", {"link_text": "a", "description1": "b", "final_url": "u"})
        assert any("both" in e for e in errors)

    def test_existing_asset_id_skips_content_checks(self):
        assert swaps.validate_new_asset("AD_IMAGE", {"asset_id": "123"}) == []

    def test_image_needs_url(self):
        assert swaps.validate_new_asset("AD_IMAGE", {}) != []


class TestLinkResourceName:
    @pytest.mark.parametrize("level,parent,expected", [
        ("customer", "", "customers/1/customerAssets/9~SITELINK"),
        ("campaign", "5", "customers/1/campaignAssets/5~9~SITELINK"),
        ("ad_group", "7", "customers/1/adGroupAssets/7~9~SITELINK"),
    ])
    def test_names(self, level, parent, expected):
        assert swaps.link_resource_name("1", level, parent, "9", "SITELINK") == expected


@pytest.fixture
def config():
    return AdLoopConfig(safety=SafetyConfig())


SERVED = [
    {"campaign_id": "1", "campaign": "Branded", "campaign_status": "ENABLED",
     "ad_group_id": "10", "ad_group": "General Keywords", "ad_status": "ENABLED", "approval_status": "APPROVED"},
    {"campaign_id": "2", "campaign": "Branded - VBB Test", "campaign_status": "ENABLED",
     "ad_group_id": "20", "ad_group": "General Keywords", "ad_status": "ENABLED", "approval_status": "APPROVED"},
]


class TestDraftUpdateRsa:
    def _live(self):
        return {"headlines": list(HEADLINES), "descriptions": _items("D one", "D two"), "served_in": SERVED}

    def test_preview_lists_shared_ad_groups(self, config):
        with patch.object(swaps, "read_rsa", return_value=self._live()):
            out = swaps.draft_update_rsa(
                config, customer_id="123", ad_id="711584178185",
                headline_swaps=[{"old": "Check Your Zip", "new": "Book an Erie Home Consult"}],
            )
        assert out["status"] == "PENDING_CONFIRMATION"
        assert out["operation"] == "update_responsive_search_ad"
        assert len(out["changes"]["served_in"]) == 2
        assert any("shared by 2 live ad groups" in w for w in out["warnings"])
        assert out["changes"]["after"]["headlines"][1]["text"] == "Book an Erie Home Consult"

    def test_rejects_non_numeric_ad_id(self, config):
        out = swaps.draft_update_rsa(config, ad_id="10~711", headline_swaps=[{"old": "a", "new": "b"}])
        assert "error" in out

    def test_blocked_operation(self):
        cfg = AdLoopConfig(safety=SafetyConfig(blocked_operations=["update_responsive_search_ad"]))
        out = swaps.draft_update_rsa(cfg, ad_id="1", headline_swaps=[{"old": "a", "new": "b"}])
        assert "blocked" in out["error"]

    def test_validation_errors_returned(self, config):
        with patch.object(swaps, "read_rsa", return_value=self._live()):
            out = swaps.draft_update_rsa(
                config, ad_id="1", headline_swaps=[{"old": "Gone", "new": "X"}],
            )
        assert out["error"] == "Validation failed"


class TestDraftSwapAsset:
    OLD = {"asset_id": "348534990332", "type": "SITELINK", "name": "", "link_text": "Worried About Cost?",
           "description1": "", "description2": "", "callout_text": "", "final_urls": []}
    NEW = {"link_text": "Worried About Cost?", "description1": "Up to 50% off installation",
           "description2": "$0 down financing available", "final_url": "https://roof.eriehome.com/x"}

    def test_swap_preview(self, config):
        with patch.object(swaps, "read_asset_link", return_value=self.OLD):
            out = swaps.draft_swap_asset(
                config, customer_id="123", level="ad_group", parent_id="173161625674",
                field_type="sitelink", old_asset_id="348534990332", new_asset=self.NEW,
            )
        assert out["changes"]["action"] == "swap"
        assert out["changes"]["field_type"] == "SITELINK"

    def test_old_link_missing_at_level(self, config):
        with patch.object(swaps, "read_asset_link", return_value=None):
            out = swaps.draft_swap_asset(
                config, level="campaign", parent_id="5", field_type="SITELINK",
                old_asset_id="9", new_asset=self.NEW,
            )
        assert "No live SITELINK link" in out["error"]

    def test_unlink_only(self, config):
        with patch.object(swaps, "read_asset_link", return_value=self.OLD):
            out = swaps.draft_swap_asset(
                config, level="ad_group", parent_id="1", field_type="SITELINK", old_asset_id="9",
            )
        assert out["changes"]["action"] == "unlink"

    def test_needs_old_or_new(self, config):
        out = swaps.draft_swap_asset(config, level="campaign", parent_id="1", field_type="SITELINK")
        assert out["error"] == "Validation failed"

    def test_parent_required(self, config):
        out = swaps.draft_swap_asset(config, level="ad_group", field_type="CALLOUT", new_asset={"text": "x"})
        assert out["error"] == "Validation failed"

    def test_existing_asset_type_checked(self, config):
        with patch.object(swaps, "read_asset_type", return_value="SITELINK"):
            out = swaps.draft_swap_asset(
                config, level="campaign", parent_id="1", field_type="AD_IMAGE", new_asset={"asset_id": "5"},
            )
        assert "expected IMAGE" in out["error"]
