"""Google Ads read tools — campaign, ad, keyword, and search term performance."""
# test premier commit 

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def list_accounts(config: AdLoopConfig) -> dict:
    """List all accessible Google Ads accounts."""
    from adloop.ads.gaql import execute_query

    mcc_id = config.ads.login_customer_id
    if mcc_id:
        query = """
            SELECT customer_client.id, customer_client.descriptive_name,
                   customer_client.status, customer_client.manager
            FROM customer_client
        """
        rows = execute_query(config, mcc_id, query)
    else:
        query = """
            SELECT customer.id, customer.descriptive_name,
                   customer.status, customer.manager
            FROM customer
            LIMIT 1
        """
        rows = execute_query(config, config.ads.customer_id, query)

    return {"accounts": rows, "total_accounts": len(rows)}


def get_campaign_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get campaign-level performance metrics for the given date range."""
    from adloop.ads.gaql import execute_query

    date_clause = _date_clause(date_range_start, date_range_end)

    query = f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.advertising_channel_type, campaign.bidding_strategy_type,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value,
               metrics.ctr, metrics.average_cpc
        FROM campaign
        WHERE campaign.status != 'REMOVED'
          AND metrics.cost_micros > 0
          {date_clause}
        ORDER BY metrics.cost_micros DESC
    """

    rows = execute_query(config, customer_id, query)
    _enrich_cost_fields(rows)

    return {"campaigns": rows, "total_campaigns": len(rows)}


def get_ad_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get ad-level performance data including headlines, descriptions, and metrics."""
    from adloop.ads.gaql import execute_query

    date_clause = _date_clause(date_range_start, date_range_end)

    query = f"""
        SELECT campaign.name, ad_group.name,
               ad_group_ad.ad.id, ad_group_ad.ad.type,
               ad_group_ad.ad.responsive_search_ad.headlines,
               ad_group_ad.ad.responsive_search_ad.descriptions,
               ad_group_ad.ad.final_urls,
               ad_group_ad.status,
               metrics.impressions, metrics.clicks, metrics.ctr,
               metrics.conversions, metrics.cost_micros
        FROM ad_group_ad
        WHERE ad_group_ad.status != 'REMOVED'
          AND metrics.cost_micros > 0
          {date_clause}
        ORDER BY metrics.cost_micros DESC
    """

    rows = execute_query(config, customer_id, query)
    _enrich_cost_fields(rows)

    return {"ads": rows, "total_ads": len(rows)}


def get_keyword_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get keyword metrics including quality scores and competitive data."""
    from adloop.ads.gaql import execute_query

    date_clause = _date_clause(date_range_start, date_range_end)

    query = f"""
        SELECT campaign.name, ad_group.name,
               ad_group_criterion.keyword.text,
               ad_group_criterion.keyword.match_type,
               ad_group_criterion.quality_info.quality_score,
               metrics.impressions, metrics.clicks, metrics.ctr,
               metrics.average_cpc, metrics.cost_micros,
               metrics.conversions
        FROM keyword_view
        WHERE ad_group_criterion.status != 'REMOVED'
          AND metrics.cost_micros > 0
          {date_clause}
        ORDER BY metrics.cost_micros DESC
    """

    rows = execute_query(config, customer_id, query)
    _enrich_cost_fields(rows)

    return {"keywords": rows, "total_keywords": len(rows)}


def get_search_terms(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get search terms report — what users actually typed before clicking ads."""
    from adloop.ads.gaql import execute_query

    if date_range_start and date_range_end:
        from adloop.dates import validate_iso_date_range

        validate_iso_date_range(date_range_start, date_range_end)
        query = f"""
            SELECT search_term_view.search_term,
                   campaign.name, ad_group.name,
                   metrics.impressions, metrics.clicks,
                   metrics.cost_micros, metrics.conversions
            FROM search_term_view
            WHERE metrics.cost_micros > 0
              AND segments.date BETWEEN '{date_range_start}' AND '{date_range_end}'
            ORDER BY metrics.clicks DESC
            LIMIT 200
        """
    else:
        query = """
            SELECT search_term_view.search_term,
                   campaign.name, ad_group.name,
                   metrics.impressions, metrics.clicks,
                   metrics.cost_micros, metrics.conversions
            FROM search_term_view
            WHERE metrics.cost_micros > 0
              AND segments.date DURING LAST_30_DAYS
            ORDER BY metrics.clicks DESC
            LIMIT 200
        """

    rows = execute_query(config, customer_id, query)
    _enrich_cost_fields(rows)

    return {"search_terms": rows, "total_search_terms": len(rows)}


def get_ad_group_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get ad group-level performance metrics for the given date range."""
    from adloop.ads.gaql import execute_query

    date_clause = _date_clause(date_range_start, date_range_end)

    query = f"""
        SELECT campaign.id, campaign.name,
               ad_group.id, ad_group.name, ad_group.status, ad_group.type,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value,
               metrics.ctr, metrics.average_cpc
        FROM ad_group
        WHERE ad_group.status != 'REMOVED'
          AND metrics.cost_micros > 0
          {date_clause}
        ORDER BY metrics.cost_micros DESC
    """

    rows = execute_query(config, customer_id, query)
    _enrich_cost_fields(rows)

    return {"ad_groups": rows, "total_ad_groups": len(rows)}


def get_asset_group_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get asset group-level performance for Performance Max campaigns."""
    from adloop.ads.gaql import execute_query

    date_clause = _date_clause(date_range_start, date_range_end)

    query = f"""
        SELECT campaign.id, campaign.name,
               asset_group.id, asset_group.name, asset_group.status,
               asset_group.ad_strength,
               asset_group.final_urls,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value, metrics.ctr
        FROM asset_group
        WHERE asset_group.status != 'REMOVED'
          AND metrics.cost_micros > 0
          {date_clause}
        ORDER BY metrics.cost_micros DESC
    """

    rows = execute_query(config, customer_id, query)
    _enrich_cost_fields(rows)

    return {"asset_groups": rows, "total_asset_groups": len(rows)}


def get_asset_group_asset_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get per-asset performance for Performance Max campaigns via asset_group_asset.

    Returns metrics broken down by individual asset within a PMax asset group,
    along with performance_label (BEST | GOOD | LOW | PENDING | UNSPECIFIED)
    and primary_status for each asset.
    """
    from adloop.ads.gaql import execute_query

    date_clause = _date_clause(date_range_start, date_range_end)

    query = f"""
        SELECT campaign.id, campaign.name,
               asset_group.id, asset_group.name,
               asset_group_asset.field_type,
               asset_group_asset.performance_label,
               asset_group_asset.primary_status,
               asset_group_asset.status,
               asset.id, asset.name, asset.type,
               asset.text_asset.text,
               asset.image_asset.full_size.url,
               asset.youtube_video_asset.youtube_video_id,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value, metrics.ctr
        FROM asset_group_asset
        WHERE asset_group_asset.status != 'REMOVED'
          AND metrics.cost_micros > 0
          {date_clause}
        ORDER BY metrics.impressions DESC
    """

    rows = execute_query(config, customer_id, query)
    _enrich_cost_fields(rows)

    return {"assets": rows, "total_assets": len(rows)}


def get_ad_group_ad_asset_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get per-asset performance for RSA ads via ad_group_ad_asset_view.

    Returns actual metrics (impressions, clicks, cost) broken down by individual
    asset, along with the performance label (BEST | GOOD | LOW | PENDING).
    Works for Responsive Search Ads and other ad types that use assets.
    """
    from adloop.ads.gaql import execute_query

    date_clause = _date_clause(date_range_start, date_range_end)

    query = f"""
        SELECT campaign.id, campaign.name,
               ad_group.id, ad_group.name,
               ad_group_ad.ad.id,
               asset.id, asset.name, asset.type,
               asset.text_asset.text,
               asset.image_asset.full_size.url,
               asset.youtube_video_asset.youtube_video_id,
               ad_group_ad_asset_view.field_type,
               ad_group_ad_asset_view.performance_label,
               ad_group_ad_asset_view.enabled,
               ad_group_ad_asset_view.pinned_field,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.ctr
        FROM ad_group_ad_asset_view
        WHERE ad_group_ad_asset_view.enabled = TRUE
          AND metrics.cost_micros > 0
          {date_clause}
        ORDER BY metrics.impressions DESC
    """

    rows = execute_query(config, customer_id, query)
    _enrich_cost_fields(rows)

    return {"assets": rows, "total_assets": len(rows)}


def get_product_performance(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get product-level performance via shopping_performance_view."""
    from adloop.ads.gaql import execute_query

    date_clause = _date_clause(date_range_start, date_range_end)

    query = f"""
        SELECT campaign.id, campaign.name,
               ad_group.id, ad_group.name,
               segments.product_item_id,
               segments.product_title,
               segments.product_brand,
               segments.product_type_l1,
               segments.product_category_level1,
               segments.product_condition,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value, metrics.ctr
        FROM shopping_performance_view
        WHERE metrics.cost_micros > 0
          {date_clause}
        ORDER BY metrics.cost_micros DESC
        LIMIT 500
    """

    rows = execute_query(config, customer_id, query)
    _enrich_cost_fields(rows)

    return {"products": rows, "total_products": len(rows)}


def get_negative_keywords(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    campaign_id: str = "",
) -> dict:
    """List negative keywords for a campaign or all campaigns."""
    from adloop.ads.gaql import execute_query

    campaign_filter = ""
    if campaign_id:
        campaign_filter = f"AND campaign.id = {campaign_id}"

    query = f"""
        SELECT campaign.id, campaign.name,
               campaign_criterion.keyword.text,
               campaign_criterion.keyword.match_type,
               campaign_criterion.negative,
               campaign_criterion.criterion_id
        FROM campaign_criterion
        WHERE campaign_criterion.negative = TRUE
          AND campaign_criterion.status != 'REMOVED'
          {campaign_filter}
        ORDER BY campaign.name
    """

    rows = execute_query(config, customer_id, query)
    return {"negative_keywords": rows, "total_negative_keywords": len(rows)}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _date_clause(start: str, end: str) -> str:
    """Build a GAQL date WHERE fragment for an INCLUSIVE [start, end] range.

    GAQL's BETWEEN is inclusive on both ends, so '2026-06-01' AND '2026-06-07'
    covers seven days. When tiling adjacent windows for week-over-week analysis,
    advance the next start by one day — reusing an end date as the next start
    double-counts that day's metrics. Use ``adloop.dates.split_date_range`` to
    generate safe non-overlapping windows. Empty start/end falls back to
    LAST_30_DAYS.
    """
    from adloop.dates import validate_iso_date_range

    if start and end:
        validate_iso_date_range(start, end)
        return f"AND segments.date BETWEEN '{start}' AND '{end}'"
    return "AND segments.date DURING LAST_30_DAYS"


def _enrich_cost_fields(rows: list[dict]) -> None:
    """Add human-readable cost and CPA fields computed from cost_micros."""
    for row in rows:
        cost_micros = row.get("metrics.cost_micros", 0) or 0
        row["metrics.cost"] = round(cost_micros / 1_000_000, 2)

        conversions = row.get("metrics.conversions", 0) or 0
        if conversions > 0:
            row["metrics.cpa"] = round(cost_micros / 1_000_000 / conversions, 2)

        avg_cpc_micros = row.get("metrics.average_cpc", 0) or 0
        if avg_cpc_micros:
            row["metrics.average_cpc_eur"] = round(avg_cpc_micros / 1_000_000, 2)
