"""AdLoop MCP server — FastMCP instance with all tool registrations."""

from __future__ import annotations

import functools
import logging
import os
import secrets
from typing import Callable
from urllib.parse import parse_qs, urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from adloop.config import load_config

logger = logging.getLogger("adloop")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# ── Tool classification (drives Claude's permission management) ───────────────
# readOnlyHint -> safe reads; WRITE -> mutates the ad account; DESTRUCTIVE ->
# removes/applies irreversible changes. openWorldHint -> talks to Google APIs.
_READONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

# ── Auth: authless (Parker-style) ─────────────────────────────────────────────
# No OAuth is advertised — Claude connects with no login step. If MCP_AUTH_TOKEN
# is set, TokenGateMiddleware requires it on every MCP request (via the connector
# URL ?access_token=... or an Authorization: Bearer header). Empty = fully open.
SERVER_URL = os.environ.get("SERVER_URL", "").rstrip("/")
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")


def _derive_allowed_hosts() -> list:
    """Hosts allowed through DNS-rebinding protection: SERVER_URL (scheme optional)
    + Railway's auto-injected RAILWAY_PUBLIC_DOMAIN + optional MCP_ALLOWED_HOSTS.
    Prevents 421 'Invalid Host header'."""
    candidates: list = []
    if SERVER_URL:
        _u = SERVER_URL if "//" in SERVER_URL else "https://" + SERVER_URL
        netloc = urlparse(_u).netloc
        if netloc:
            candidates.append(netloc)
    rpd = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if rpd:
        candidates.append(rpd)
    for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(","):
        if h.strip():
            candidates.append(h.strip())
    seen, out = set(), []
    for h in candidates:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


_allowed_hosts = _derive_allowed_hosts()
if _allowed_hosts:
    _transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[x for h in _allowed_hosts for x in (h, f"{h}:*")],
        allowed_origins=[x for h in _allowed_hosts for x in (f"https://{h}", f"http://{h}")],
    )
    logger.info("DNS-rebinding protection ON — allowed hosts: %s", _allowed_hosts)
else:
    _transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    logger.warning("No SERVER_URL / RAILWAY_PUBLIC_DOMAIN — DNS-rebinding protection OFF")

mcp = FastMCP(
    "AdLoop",
    instructions=(
        "AdLoop connects Google Ads and Google Analytics (GA4) data to your "
        "codebase. Use the read tools to analyze performance, and the write "
        "tools (with safety confirmation) to manage campaigns."
    ),
    transport_security=_transport_security,
)
logger.info("AdLoop configured — server_url=%s auth_gate=%s", SERVER_URL or "(unset)", bool(AUTH_TOKEN))


class TokenGateMiddleware:
    """Authless-with-a-shared-secret ASGI gate. When a token is configured, require
    it on every HTTP request (from ?access_token=/token or Bearer). Correct token →
    pass through (no auth challenge); otherwise 401."""

    def __init__(self, app, token: str):
        self._app = app
        self._token = token

    def _provided(self, scope) -> str:
        qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        val = (qs.get("access_token") or qs.get("token") or [""])[0]
        if val:
            return val
        for k, v in scope.get("headers") or []:
            if k == b"authorization":
                auth = v.decode("latin-1")
                if auth.lower().startswith("bearer "):
                    return auth[7:].strip()
        return ""

    async def __call__(self, scope, receive, send):
        if self._token and scope.get("type") == "http":
            if not secrets.compare_digest(self._provided(scope), self._token):
                from starlette.responses import JSONResponse
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self._app(scope, receive, send)

_config = load_config()


def _safe(fn: Callable) -> Callable:
    """Wrap a tool function so exceptions return structured error dicts."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RuntimeError as e:
            return {"error": str(e)}
        except Exception as e:
            err = str(e).lower()
            if "invalid_grant" in err or "revoked" in err:
                return {
                    "error": "Authentication failed — OAuth token expired or revoked.",
                    "hint": (
                        "Delete ~/.adloop/token.json and re-run any tool to "
                        "trigger re-authorization. If this keeps happening, "
                        "publish the GCP consent screen to 'In production'."
                    ),
                }
            return {"error": str(e), "tool": fn.__name__}

    return wrapper

# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def health_check() -> dict:
    """Test AdLoop connectivity — checks OAuth token, GA4 API, and Google Ads API.

    Run this first if other tools are failing. Returns status for each service
    and actionable guidance if something is broken.
    """
    from adloop.ads.client import GOOGLE_ADS_API_VERSION

    status = {
        "ga4": "unknown",
        "ads": "unknown",
        "config": "ok",
        "google_ads_api_version": GOOGLE_ADS_API_VERSION,
    }

    try:
        from google.ads.googleads.client import _DEFAULT_VERSION
        if _DEFAULT_VERSION != GOOGLE_ADS_API_VERSION:
            status["ads_version_note"] = (
                f"AdLoop is pinned to {GOOGLE_ADS_API_VERSION} but the "
                f"google-ads library defaults to {_DEFAULT_VERSION}. "
                f"A newer API version is available — update "
                f"GOOGLE_ADS_API_VERSION in ads/client.py when ready to migrate."
            )
    except ImportError:
        pass

    try:
        from adloop.ga4.reports import get_account_summaries as _ga4_test

        result = _ga4_test(_config)
        status["ga4"] = "ok"
        status["ga4_properties"] = result.get("total_properties", 0)
    except Exception as e:
        status["ga4"] = "error"
        status["ga4_error"] = str(e)

    try:
        from adloop.ads.read import list_accounts as _ads_test

        result = _ads_test(_config)
        status["ads"] = "ok"
        status["ads_accounts"] = result.get("total_accounts", 0)
    except Exception as e:
        status["ads"] = "error"
        status["ads_error"] = str(e)

    if status["ga4"] == "error" or status["ads"] == "error":
        any_error = status.get("ga4_error", "") + status.get("ads_error", "")
        if "invalid_grant" in any_error.lower() or "revoked" in any_error.lower():
            status["hint"] = (
                "OAuth token expired or revoked. Delete ~/.adloop/token.json "
                "and re-run health_check to trigger re-authorization. "
                "To prevent recurring expiry, publish the GCP consent screen "
                "from 'Testing' to 'In production'."
            )

    return status


# ---------------------------------------------------------------------------
# GA4 Read Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def get_account_summaries() -> dict:
    """Discover all GA4 properties accessible by the authenticated user.

    ALWAYS call this first when a specific GA4 property has not been mentioned.
    Returns a flat ``properties`` list — each entry contains ``property_id``,
    ``property_name``, ``account_id``, and ``account_name``.  Pass the
    ``property_id`` value directly to run_ga4_report, run_realtime_report,
    get_tracking_events, and the cross-reference tools.

    For agency accounts with many clients this will return all properties
    the authenticated Google account can access (can be 80+).
    """
    from adloop.ga4.reports import get_account_summaries as _impl

    return _impl(_config)


@mcp.tool(annotations=_READONLY)
@_safe
def run_ga4_report(
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    date_range_start: str = "7daysAgo",
    date_range_end: str = "today",
    property_id: str = "",
    limit: int = 100,
) -> dict:
    """Run a custom GA4 report with specified dimensions, metrics, and date range.

    property_id: numeric GA4 property ID (e.g. "123456789"). Call
    get_account_summaries() first to find the right ID for the client.
    Falls back to the default property_id in config if empty.

    Common dimensions: date, pagePath, sessionSource, sessionMedium,
    country, deviceCategory, eventName
    Common metrics: sessions, totalUsers, newUsers, screenPageViews,
    conversions, eventCount, bounceRate

    Date formats: "today", "yesterday", "7daysAgo", "28daysAgo",
    "90daysAgo", or "YYYY-MM-DD".
    """
    from adloop.ga4.reports import run_ga4_report as _impl

    return _impl(
        _config,
        property_id=property_id or _config.ga4.property_id,
        dimensions=dimensions,
        metrics=metrics,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        limit=limit,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def run_realtime_report(
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    property_id: str = "",
) -> dict:
    """Run a GA4 realtime report showing current active users and events.

    property_id: numeric GA4 property ID. Call get_account_summaries()
    first to find the right ID for the client. Falls back to config default.

    Useful for verifying tracking is firing after code changes.
    Common dimensions: unifiedScreenName, eventName, country, deviceCategory
    Common metrics: activeUsers, eventCount
    """
    from adloop.ga4.reports import run_realtime_report as _impl

    return _impl(
        _config,
        property_id=property_id or _config.ga4.property_id,
        dimensions=dimensions,
        metrics=metrics,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_tracking_events(
    date_range_start: str = "28daysAgo",
    date_range_end: str = "today",
    property_id: str = "",
) -> dict:
    """List all GA4 events and their volume for the given date range.

    property_id: numeric GA4 property ID. Call get_account_summaries()
    first to find the right ID for the client. Falls back to config default.

    Returns every distinct event name with its total event count, sorted
    by volume descending. Use this to audit what tracking is active.
    """
    from adloop.ga4.tracking import get_tracking_events as _impl

    return _impl(
        _config,
        property_id=property_id or _config.ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


# ---------------------------------------------------------------------------
# Google Ads Read Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def list_accounts() -> dict:
    """List all accessible Google Ads accounts.

    Returns account names, IDs, and status. Use this to discover
    which accounts are available before running performance queries.
    """
    from adloop.ads.read import list_accounts as _impl

    return _impl(_config)


@mcp.tool(annotations=_READONLY)
@_safe
def split_date_range(
    date_range_start: str,
    date_range_end: str,
    days: int = 7,
) -> dict:
    """Split an inclusive date range into consecutive, NON-overlapping windows.

    Call this before pulling week-over-week (or any multi-window) performance.
    Every AdLoop date range is inclusive on both ends, so if you reuse an end
    date as the next window's start, that day's cost, impressions, and clicks
    are counted twice — and a sum of weekly pulls will overstate the true total
    (the more windows, the larger the overcount). The windows returned here tile
    the range cleanly so summing a metric across them equals the whole-range value.

    days=7 with a Monday start gives Mon–Sun weeks. Date format: "YYYY-MM-DD".
    Returns: {"windows": [{"start", "end"}, ...], "total_windows", "days_per_window"}.
    """
    from adloop.dates import split_date_range as _impl

    windows = _impl(date_range_start, date_range_end, days=days)
    return {
        "windows": [{"start": s, "end": e} for s, e in windows],
        "total_windows": len(windows),
        "days_per_window": days,
        "note": (
            "Windows are inclusive and non-overlapping; summing a metric across "
            "them equals the metric over the full range."
        ),
    }


@mcp.tool(annotations=_READONLY)
@_safe
def get_campaign_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get campaign-level performance metrics for a date range.

    Returns: campaign name, status, type, impressions, clicks, cost,
    conversions, CPA, ROAS, CTR for each campaign.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. For
    weekly/multi-window pulls, build windows with split_date_range — adjacent
    windows that share a boundary day double-count cost, impressions, and clicks.
    """
    from adloop.ads.read import get_campaign_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_ad_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get ad-level performance data including headlines, descriptions, and metrics.

    Returns: ad type, headlines, descriptions, final URL, impressions,
    clicks, CTR, conversions, cost for each ad.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts cost, impressions, and clicks).
    """
    from adloop.ads.read import get_ad_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_keyword_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get keyword metrics including quality scores and competitive data.

    Returns: keyword text, match type, quality score, impressions,
    clicks, CTR, CPC, conversions for each keyword.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts cost, impressions, and clicks).
    """
    from adloop.ads.read import get_keyword_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_search_terms(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get search terms report — what users actually typed before clicking your ads.

    Critical for finding negative keyword opportunities and understanding user intent.
    Returns: search term, campaign, ad group, impressions, clicks, conversions.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts cost, impressions, and clicks).
    """
    from adloop.ads.read import get_search_terms as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_ad_group_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get ad group-level performance metrics for a date range.

    Returns: campaign name, ad group name, status, type, impressions, clicks,
    cost, conversions, CPA, CTR for each ad group.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts cost, impressions, and clicks).
    """
    from adloop.ads.read import get_ad_group_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_asset_group_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get asset group-level performance for Performance Max (PMax) campaigns.

    Returns: campaign name, asset group name, status, ad strength, final URLs,
    impressions, clicks, cost, conversions for each asset group.
    Ad strength values: EXCELLENT, GOOD, POOR, PENDING, UNKNOWN.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts cost, impressions, and clicks).
    """
    from adloop.ads.read import get_asset_group_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_asset_group_asset_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get per-asset performance for Performance Max (PMax) asset groups.

    Queries the asset_group_asset resource — returns each individual asset
    (text, image, video) within a PMax asset group with its performance label
    (BEST | GOOD | LOW | PENDING) and primary status.

    Use this to identify which PMax assets drive results and which to replace.
    Complements get_asset_group_performance (aggregate) with per-asset detail.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts cost, impressions, and clicks).
    """
    from adloop.ads.read import get_asset_group_asset_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_ad_group_ad_asset_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get per-asset performance metrics via ad_group_ad_asset_view.

    Returns actual impressions, clicks, cost, and conversions broken down
    by individual asset, along with performance labels (BEST | GOOD | LOW | PENDING)
    and whether the asset is pinned to a specific position.

    Works for Responsive Search Ads — use this to identify which headlines
    and descriptions drive performance and which should be replaced.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts cost, impressions, and clicks).
    """
    from adloop.ads.read import get_ad_group_ad_asset_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_product_performance(
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get product-level performance from Shopping campaigns.

    Returns: product ID, title, brand, type, category, condition,
    impressions, clicks, cost, conversions, and ROAS per product.
    Use this to identify top/underperforming products in Shopping or PMax.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Max 500
    products. Use split_date_range for week-over-week so adjacent windows don't
    share a boundary day (which double-counts cost, impressions, and clicks).
    """
    from adloop.ads.read import get_product_performance as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_negative_keywords(
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """List existing negative keywords for a campaign or all campaigns.

    Use this before adding negative keywords to check for duplicates.
    If campaign_id is empty, returns negatives across all campaigns.
    """
    from adloop.ads.read import get_negative_keywords as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def analyze_campaign_conversions(
    date_range_start: str = "",
    date_range_end: str = "",
    customer_id: str = "",
    property_id: str = "",
    campaign_name: str = "",
) -> dict:
    """Campaign clicks → GA4 conversions mapping — the real cost-per-conversion.

    Combines Google Ads campaign metrics with GA4 session/conversion data to
    reveal click-to-session ratios (GDPR indicator), compare Ads-reported vs
    GA4-reported conversions, and compute cost-per-GA4-conversion.
    Also returns non-paid channel conversion rates for comparison context.

    property_id: numeric GA4 property ID for the client. Call
    get_account_summaries() first if unsure. Falls back to config default.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts cost, impressions, and clicks).
    """
    from adloop.crossref import analyze_campaign_conversions as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        property_id=property_id or _config.ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        campaign_name=campaign_name,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def landing_page_analysis(
    date_range_start: str = "",
    date_range_end: str = "",
    customer_id: str = "",
    property_id: str = "",
) -> dict:
    """Analyze which landing pages convert and which don't.

    Combines ad final URLs with GA4 page-level data to show paid traffic
    sessions, conversion rates, bounce rates, and engagement per landing page.
    Identifies pages that get ad clicks but zero conversions and orphaned URLs.

    property_id: numeric GA4 property ID for the client. Call
    get_account_summaries() first if unsure. Falls back to config default.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts cost, impressions, and clicks).
    """
    from adloop.crossref import landing_page_analysis as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        property_id=property_id or _config.ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def attribution_check(
    date_range_start: str = "",
    date_range_end: str = "",
    customer_id: str = "",
    property_id: str = "",
    conversion_events: list[str] | None = None,
) -> dict:
    """Compare Ads-reported conversions vs GA4 — find tracking discrepancies.

    Checks whether conversions reported by Google Ads match what GA4 records,
    diagnoses GDPR consent gaps, attribution model differences, and missing
    conversion event configuration.

    property_id: numeric GA4 property ID for the client. Call
    get_account_summaries() first if unsure. Falls back to config default.
    conversion_events: optional list of GA4 event names to specifically check
    (e.g. ["sign_up", "purchase"]). If omitted, compares aggregate totals only.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts cost, impressions, and clicks).
    """
    from adloop.crossref import attribution_check as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        property_id=property_id or _config.ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        conversion_events=conversion_events,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def run_gaql(
    query: str,
    customer_id: str = "",
    format: str = "table",
) -> dict:
    """Execute an arbitrary GAQL (Google Ads Query Language) query.

    Use this for advanced queries not covered by the other tools.
    See the GAQL reference in the AdLoop cursor rules for syntax help.

    format: "table" (default, readable), "json" (structured), "csv" (exportable)
    """
    from adloop.ads.gaql import run_gaql as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        query=query,
        format=format,
    )


# ---------------------------------------------------------------------------
# Google Ads Write Tools (Safety Layer)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_WRITE)
@_safe
def draft_campaign(
    campaign_name: str,
    daily_budget: float,
    bidding_strategy: str,
    customer_id: str = "",
    target_cpa: float = 0,
    target_roas: float = 0,
    channel_type: str = "SEARCH",
    ad_group_name: str = "",
    keywords: list[dict] | None = None,
) -> dict:
    """Draft a full campaign structure — returns a PREVIEW, does NOT create anything.

    Creates: CampaignBudget + Campaign (PAUSED) + AdGroup + optional Keywords.
    Ads are NOT included — use draft_responsive_search_ad after the campaign exists.

    bidding_strategy: MAXIMIZE_CONVERSIONS | TARGET_CPA | TARGET_ROAS |
                      MAXIMIZE_CONVERSION_VALUE | TARGET_SPEND | MANUAL_CPC
    target_cpa: required if bidding_strategy is TARGET_CPA (in account currency)
    target_roas: required if bidding_strategy is TARGET_ROAS
    keywords: list of {"text": "keyword", "match_type": "EXACT|PHRASE|BROAD"}

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_campaign as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_name=campaign_name,
        daily_budget=daily_budget,
        bidding_strategy=bidding_strategy,
        target_cpa=target_cpa,
        target_roas=target_roas,
        channel_type=channel_type,
        ad_group_name=ad_group_name,
        keywords=keywords,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_responsive_search_ad(
    ad_group_id: str,
    headlines: list[str],
    descriptions: list[str],
    final_url: str,
    customer_id: str = "",
    path1: str = "",
    path2: str = "",
) -> dict:
    """Draft a Responsive Search Ad — returns a PREVIEW, does NOT create the ad.

    Provide 3-15 headlines (max 30 chars each) and 2-4 descriptions (max 90 chars each).
    The preview shows exactly what will be created. Call confirm_and_apply to execute.
    """
    from adloop.ads.write import draft_responsive_search_ad as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        ad_group_id=ad_group_id,
        headlines=headlines,
        descriptions=descriptions,
        final_url=final_url,
        path1=path1,
        path2=path2,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_keywords(
    ad_group_id: str,
    keywords: list[dict],
    customer_id: str = "",
) -> dict:
    """Draft keyword additions — returns a PREVIEW, does NOT add keywords.

    keywords: list of {"text": "keyword phrase", "match_type": "EXACT|PHRASE|BROAD"}
    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import draft_keywords as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        ad_group_id=ad_group_id,
        keywords=keywords,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def add_negative_keywords(
    campaign_id: str,
    keywords: list[str],
    customer_id: str = "",
    match_type: str = "EXACT",
) -> dict:
    """Draft negative keyword additions — returns a PREVIEW.

    Negative keywords prevent your ads from showing for irrelevant searches.
    match_type: "EXACT", "PHRASE", or "BROAD"
    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import add_negative_keywords as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        campaign_id=campaign_id,
        keywords=keywords,
        match_type=match_type,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def pause_entity(
    entity_type: str,
    entity_id: str,
    customer_id: str = "",
) -> dict:
    """Draft pausing a campaign, ad group, ad, or keyword — returns a PREVIEW.

    entity_type: "campaign", "ad_group", "ad", or "keyword"
    entity_id format by type:
      - campaign: campaign ID (e.g. "12345678")
      - ad_group: ad group ID (e.g. "12345678")
      - ad: "adGroupId~adId" (e.g. "12345678~987654")
      - keyword: "adGroupId~criterionId" (e.g. "12345678~987654")

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import pause_entity as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def enable_entity(
    entity_type: str,
    entity_id: str,
    customer_id: str = "",
) -> dict:
    """Draft enabling a paused campaign, ad group, ad, or keyword — returns a PREVIEW.

    entity_type: "campaign", "ad_group", "ad", or "keyword"
    entity_id format by type:
      - campaign: campaign ID (e.g. "12345678")
      - ad_group: ad group ID (e.g. "12345678")
      - ad: "adGroupId~adId" (e.g. "12345678~987654")
      - keyword: "adGroupId~criterionId" (e.g. "12345678~987654")

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import enable_entity as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@mcp.tool(annotations=_DESTRUCTIVE)
@_safe
def remove_entity(
    entity_type: str,
    entity_id: str,
    customer_id: str = "",
) -> dict:
    """Draft REMOVING an entity — returns a PREVIEW. This is IRREVERSIBLE.

    entity_type: "campaign", "ad_group", "ad", "keyword", or "negative_keyword"
    entity_id: The resource ID. For keywords use "adGroupId~criterionId".
               For negative_keywords use the campaign criterion ID.

    WARNING: Removed entities cannot be re-enabled. Use pause_entity instead
    if you just want to temporarily disable something.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from adloop.ads.write import remove_entity as _impl

    return _impl(
        _config,
        customer_id=customer_id or _config.ads.customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@mcp.tool(annotations=_DESTRUCTIVE)
@_safe
def confirm_and_apply(
    plan_id: str,
    dry_run: bool = True,
) -> dict:
    """Execute a previously previewed change.

    IMPORTANT: Defaults to dry_run=True. You MUST explicitly pass dry_run=false
    to make real changes to the Google Ads account.

    The plan_id comes from a prior draft_* or pause/enable tool call.
    """
    from adloop.ads.write import confirm_and_apply as _impl

    return _impl(_config, plan_id=plan_id, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Tracking Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def validate_tracking(
    expected_events: list[str],
    property_id: str = "",
    date_range_start: str = "28daysAgo",
    date_range_end: str = "today",
) -> dict:
    """Compare tracking events found in the codebase against actual GA4 data.

    First, search the user's codebase for gtag('event', ...) or dataLayer.push
    calls and extract event names. Then pass those names here to check which
    ones actually fire in GA4.

    Returns: matched events, events missing from GA4, unexpected GA4 events,
    and auto-collected events (page_view, session_start, etc.).
    """
    from adloop.tracking import validate_tracking as _impl

    return _impl(
        _config,
        expected_events=expected_events,
        property_id=property_id or _config.ga4.property_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def generate_tracking_code(
    event_name: str,
    event_params: dict | None = None,
    trigger: str = "",
    property_id: str = "",
    check_existing: bool = True,
) -> dict:
    """Generate a GA4 event tracking JavaScript snippet.

    Produces ready-to-paste gtag code for the specified event. Includes
    recommended parameters for well-known GA4 events (sign_up, purchase, etc.).
    Optionally checks GA4 to warn if the event already fires.

    trigger: "form_submit", "button_click", or "page_load" — wraps the gtag
    call in an appropriate event listener. Empty = bare gtag call.
    """
    from adloop.tracking import generate_tracking_code as _impl

    return _impl(
        _config,
        event_name=event_name,
        event_params=event_params,
        trigger=trigger,
        property_id=property_id or _config.ga4.property_id,
        check_existing=check_existing,
    )


# ---------------------------------------------------------------------------
# Planning Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def estimate_budget(
    keywords: list[dict],
    daily_budget: float = 0,
    geo_target_id: str = "2276",
    language_id: str = "1000",
    forecast_days: int = 30,
    customer_id: str = "",
) -> dict:
    """Forecast clicks, impressions, and cost for a set of keywords.

    Uses Google Ads Keyword Planner to estimate campaign performance without
    creating anything. Essential for budget planning before launching campaigns.

    keywords: list of {"text": "keyword", "match_type": "EXACT|PHRASE|BROAD", "max_cpc": 1.50}
        max_cpc is optional (defaults to 1.00 in account currency)
    geo_target_id: geo target constant (2276=Germany, 2840=USA, 2826=UK, 2250=France)
    language_id: language constant (1000=English, 1001=German, 1002=French, 1003=Spanish)
    daily_budget: if provided, insights will show what % of traffic the budget captures
    forecast_days: forecast horizon in days (default 30)
    """
    from adloop.ads.forecast import estimate_budget as _impl

    return _impl(
        _config,
        keywords=keywords,
        daily_budget=daily_budget,
        geo_target_id=geo_target_id,
        language_id=language_id,
        forecast_days=forecast_days,
        customer_id=customer_id or _config.ads.customer_id,
    )


# ---------------------------------------------------------------------------
# HTTP server entrypoint
# ---------------------------------------------------------------------------


def main():
    """Run the MCP server.

    Transport is controlled by the MCP_TRANSPORT env var:
      - streamable-http (default) — modern HTTP, recommended for remote deployments
      - sse                       — legacy Server-Sent Events HTTP transport
      - stdio                     — local stdio transport for Claude Desktop / CLI

    HTTP env vars (ignored for stdio):
      MCP_HOST — bind address (default: 0.0.0.0)
      MCP_PORT — bind port    (default: 8000)
    """
    import uvicorn

    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    logger.info("Starting AdLoop MCP server transport=%s", transport)

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT") or os.environ.get("MCP_PORT", "8000"))

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    from contextlib import asynccontextmanager

    raw_app = mcp.streamable_http_app() if transport == "streamable-http" else mcp.sse_app()
    gated_app = TokenGateMiddleware(raw_app, token=AUTH_TOKEN)

    @asynccontextmanager
    async def lifespan(_app):
        async with raw_app.router.lifespan_context(_app):
            yield

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "transport": transport, "auth_gate": bool(AUTH_TOKEN)})

    routes = [
        Route("/health", health),
        Mount("/", app=gated_app),
    ]

    app = Starlette(lifespan=lifespan, routes=routes)
    logger.info("Listening on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
