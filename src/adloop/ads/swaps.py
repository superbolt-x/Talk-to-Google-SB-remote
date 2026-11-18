"""In-place copy and asset swaps — edit what is already live without new ads.

Two draft tools, both behind the usual preview -> confirm_and_apply flow:

- ``draft_update_rsa``: swap specific headlines/descriptions on an existing
  Responsive Search Ad. Uses AdService.MutateAds (update), which is what the
  Google Ads UI does when you edit an RSA: the ad keeps its ID and history
  and goes back through policy review. An Ad can be SHARED by several ad
  groups (same ad ID); the update reaches every one of them, so the preview
  lists them all.

- ``draft_swap_asset``: replace (or add, or unlink) a sitelink / callout /
  image linked at customer, campaign or ad group level. One atomic
  GoogleAdsService.Mutate: create the new asset (if needed), link it, remove
  the old LINK. The old asset itself is never removed, so it stays wherever
  else it is linked and keeps its history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


HEADLINE_MAX = 30
DESCRIPTION_MAX = 90
SITELINK_TEXT_MAX = 25
SITELINK_DESC_MAX = 35
CALLOUT_MAX = 25
IMAGE_MAX_BYTES = 5 * 1024 * 1024

ASSET_LEVELS = {"customer", "campaign", "ad_group"}
SWAPPABLE_FIELD_TYPES = {"SITELINK", "CALLOUT", "AD_IMAGE"}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without the API)
# ---------------------------------------------------------------------------


def apply_text_swaps(
    current: list[dict],
    swaps: list[dict],
    *,
    kind: str,
    max_len: int,
    min_count: int,
    max_count: int,
) -> tuple[list[dict], list[str]]:
    """Apply ``swaps`` to ``current`` and return (new_list, errors).

    current: [{"text": str, "pinned_field": str}] in the ad's order.
    swaps:   [{"old": str, "new": str}]. ``old`` empty = add ``new`` at the
             end (unpinned). The replaced entry keeps its position and pin.
    """
    errors: list[str] = []
    result = [dict(item) for item in current]

    for i, swap in enumerate(swaps or []):
        old = (swap.get("old") or "").strip()
        new = (swap.get("new") or "").strip()
        label = f"{kind} swap #{i + 1}"
        if not new:
            errors.append(f"{label}: 'new' is required")
            continue
        if len(new) > max_len:
            errors.append(
                f"{label}: '{new}' is {len(new)} chars (max {max_len})"
            )
        if not old:
            result.append({"text": new, "pinned_field": "UNSPECIFIED"})
            continue
        matches = [j for j, item in enumerate(result) if item["text"] == old]
        if not matches:
            errors.append(
                f"{label}: '{old}' is not in this ad (it may already have "
                "been swapped, or the text differs)"
            )
            continue
        if len(matches) > 1:
            errors.append(f"{label}: '{old}' appears {len(matches)} times")
            continue
        result[matches[0]]["text"] = new

    texts = [item["text"] for item in result]
    dupes = sorted({t for t in texts if texts.count(t) > 1})
    if dupes:
        errors.append(f"Duplicate {kind}s after swaps: {dupes}")
    if not min_count <= len(result) <= max_count:
        errors.append(
            f"{len(result)} {kind}s after swaps (must be {min_count}-{max_count})"
        )
    return result, errors


def validate_new_asset(field_type: str, new_asset: dict) -> list[str]:
    """Validate the ``new_asset`` payload for a given field type."""
    errors: list[str] = []
    if new_asset.get("asset_id"):
        return errors  # reuse of an existing asset; type checked against the API

    if field_type == "SITELINK":
        text = new_asset.get("link_text", "")
        if not text:
            errors.append("SITELINK needs link_text")
        elif len(text) > SITELINK_TEXT_MAX:
            errors.append(
                f"link_text '{text}' is {len(text)} chars (max {SITELINK_TEXT_MAX})"
            )
        d1, d2 = new_asset.get("description1", ""), new_asset.get("description2", "")
        if bool(d1) != bool(d2):
            errors.append("SITELINK descriptions: give both description1 and description2, or neither")
        for name, val in (("description1", d1), ("description2", d2)):
            if len(val) > SITELINK_DESC_MAX:
                errors.append(
                    f"{name} '{val}' is {len(val)} chars (max {SITELINK_DESC_MAX})"
                )
        if not new_asset.get("final_url"):
            errors.append("SITELINK needs final_url")
    elif field_type == "CALLOUT":
        text = new_asset.get("text", "")
        if not text:
            errors.append("CALLOUT needs text")
        elif len(text) > CALLOUT_MAX:
            errors.append(f"callout '{text}' is {len(text)} chars (max {CALLOUT_MAX})")
    elif field_type == "AD_IMAGE":
        if not new_asset.get("image_url"):
            errors.append(
                "AD_IMAGE needs image_url (a URL the server can download) "
                "or asset_id (an image already in the account)"
            )
    return errors


def link_resource_name(cid: str, level: str, parent_id: str, asset_id: str, field_type: str) -> str:
    if level == "customer":
        return f"customers/{cid}/customerAssets/{asset_id}~{field_type}"
    if level == "campaign":
        return f"customers/{cid}/campaignAssets/{parent_id}~{asset_id}~{field_type}"
    return f"customers/{cid}/adGroupAssets/{parent_id}~{asset_id}~{field_type}"


# ---------------------------------------------------------------------------
# API reads
# ---------------------------------------------------------------------------


def _search(config: AdLoopConfig, cid: str, query: str):
    from adloop.ads.client import get_ads_client

    client = get_ads_client(config)
    return list(client.get_service("GoogleAdsService").search(customer_id=cid, query=query))


def read_rsa(config: AdLoopConfig, cid: str, ad_id: str) -> dict:
    """Return the RSA's current text and every ad group that serves it."""
    rows = _search(
        config,
        cid,
        f"""
        SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status,
               ad_group_ad.ad.responsive_search_ad.headlines,
               ad_group_ad.ad.responsive_search_ad.descriptions,
               ad_group_ad.policy_summary.approval_status,
               ad_group.id, ad_group.name, campaign.id, campaign.name, campaign.status
        FROM ad_group_ad
        WHERE ad_group_ad.ad.id = {int(ad_id)}
        """,
    )
    if not rows:
        return {"error": f"Ad {ad_id} not found in customer {cid}"}

    first = rows[0].ad_group_ad
    if first.ad.type_.name != "RESPONSIVE_SEARCH_AD":
        return {"error": f"Ad {ad_id} is {first.ad.type_.name}, not a Responsive Search Ad"}

    rsa = first.ad.responsive_search_ad
    return {
        "headlines": [
            {"text": a.text, "pinned_field": a.pinned_field.name} for a in rsa.headlines
        ],
        "descriptions": [
            {"text": a.text, "pinned_field": a.pinned_field.name} for a in rsa.descriptions
        ],
        "served_in": [
            {
                "campaign_id": str(r.campaign.id),
                "campaign": r.campaign.name,
                "campaign_status": r.campaign.status.name,
                "ad_group_id": str(r.ad_group.id),
                "ad_group": r.ad_group.name,
                "ad_status": r.ad_group_ad.status.name,
                "approval_status": r.ad_group_ad.policy_summary.approval_status.name,
            }
            for r in rows
        ],
    }


def read_asset_link(
    config: AdLoopConfig, cid: str, level: str, parent_id: str, asset_id: str, field_type: str
) -> dict | None:
    """Return the live link of ``asset_id`` at that level, or None."""
    table = {"customer": "customer_asset", "campaign": "campaign_asset", "ad_group": "ad_group_asset"}[level]
    where = f"{table}.field_type = '{field_type}' AND asset.id = {int(asset_id)} AND {table}.status != 'REMOVED'"
    parent_field = ""
    if level == "campaign":
        where += f" AND campaign.id = {int(parent_id)}"
        parent_field = "campaign.id, "
    elif level == "ad_group":
        where += f" AND ad_group.id = {int(parent_id)}"
        parent_field = "ad_group.id, "
    rows = _search(
        config,
        cid,
        f"""
        SELECT {parent_field}{table}.resource_name, {table}.status, asset.id, asset.type, asset.name,
               asset.sitelink_asset.link_text, asset.sitelink_asset.description1,
               asset.sitelink_asset.description2, asset.callout_asset.callout_text,
               asset.final_urls
        FROM {table} WHERE {where}
        """,
    )
    if not rows:
        return None
    a = rows[0].asset
    return {
        "asset_id": str(a.id),
        "type": a.type_.name,
        "name": a.name,
        "link_text": a.sitelink_asset.link_text,
        "description1": a.sitelink_asset.description1,
        "description2": a.sitelink_asset.description2,
        "callout_text": a.callout_asset.callout_text,
        "final_urls": list(a.final_urls),
    }


def read_asset_type(config: AdLoopConfig, cid: str, asset_id: str) -> str | None:
    rows = _search(config, cid, f"SELECT asset.id, asset.type FROM asset WHERE asset.id = {int(asset_id)}")
    return rows[0].asset.type_.name if rows else None


# ---------------------------------------------------------------------------
# Draft tools
# ---------------------------------------------------------------------------


def draft_update_rsa(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    ad_id: str = "",
    headline_swaps: list[dict] | None = None,
    description_swaps: list[dict] | None = None,
) -> dict:
    """Draft in-place headline/description swaps on an existing RSA."""
    from adloop.ads.client import normalize_customer_id
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_responsive_search_ad", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    if not ad_id or not str(ad_id).isdigit():
        return {"error": "ad_id is required (the numeric ad ID, not adGroupId~adId)"}
    if not headline_swaps and not description_swaps:
        return {"error": "Give at least one headline_swaps or description_swaps entry"}

    cid = normalize_customer_id(customer_id)
    current = read_rsa(config, cid, ad_id)
    if "error" in current:
        return current

    new_h, err_h = apply_text_swaps(
        current["headlines"], headline_swaps or [],
        kind="headline", max_len=HEADLINE_MAX, min_count=3, max_count=15,
    )
    new_d, err_d = apply_text_swaps(
        current["descriptions"], description_swaps or [],
        kind="description", max_len=DESCRIPTION_MAX, min_count=2, max_count=4,
    )
    if err_h or err_d:
        return {"error": "Validation failed", "details": err_h + err_d}

    served = current["served_in"]
    warnings = []
    live = [s for s in served if s["ad_status"] != "REMOVED" and s["campaign_status"] != "REMOVED"]
    if len(live) > 1:
        warnings.append(
            f"This ad is shared by {len(live)} live ad groups. The update changes "
            "the ad in ALL of them (listed in served_in) — check that none is a "
            "test arm that must stay unchanged."
        )
    warnings.append("The ad keeps its ID and history but goes back through policy review.")

    plan = ChangePlan(
        operation="update_responsive_search_ad",
        entity_type="ad",
        entity_id=str(ad_id),
        customer_id=customer_id,
        changes={
            "ad_id": str(ad_id),
            "headline_swaps": headline_swaps or [],
            "description_swaps": description_swaps or [],
            "before": {"headlines": current["headlines"], "descriptions": current["descriptions"]},
            "after": {"headlines": new_h, "descriptions": new_d},
            "served_in": served,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = warnings
    return preview


def draft_swap_asset(
    config: AdLoopConfig,
    *,
    customer_id: str = "",
    level: str = "",
    parent_id: str = "",
    field_type: str = "",
    old_asset_id: str = "",
    new_asset: dict | None = None,
) -> dict:
    """Draft replacing/adding/unlinking an asset at customer/campaign/ad group level."""
    from adloop.ads.client import normalize_customer_id
    from adloop.safety.guards import SafetyViolation, check_blocked_operation
    from adloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("swap_asset", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    field_type = (field_type or "").upper()
    new_asset = dict(new_asset or {})
    errors = []
    if level not in ASSET_LEVELS:
        errors.append(f"level must be one of {sorted(ASSET_LEVELS)}")
    if level in ("campaign", "ad_group") and not str(parent_id).isdigit():
        errors.append(f"parent_id (the {level} ID) is required for level '{level}'")
    if field_type not in SWAPPABLE_FIELD_TYPES:
        errors.append(f"field_type must be one of {sorted(SWAPPABLE_FIELD_TYPES)}")
    if not old_asset_id and not new_asset:
        errors.append("Give old_asset_id (to unlink), new_asset (to add), or both (to swap)")
    if old_asset_id and not str(old_asset_id).isdigit():
        errors.append("old_asset_id must be the numeric asset ID")
    if new_asset:
        errors += validate_new_asset(field_type, new_asset)
    if errors:
        return {"error": "Validation failed", "details": errors}

    cid = normalize_customer_id(customer_id)

    old = None
    if old_asset_id:
        old = read_asset_link(config, cid, level, parent_id, old_asset_id, field_type)
        if old is None:
            return {
                "error": f"No live {field_type} link for asset {old_asset_id} at "
                f"{level} {parent_id or ''}".strip()
                + ". Check the level: an asset linked to the campaign is not "
                "unlinked from an ad group, and vice versa."
            }

    if new_asset.get("asset_id"):
        asset_type = read_asset_type(config, cid, new_asset["asset_id"])
        expected = {"SITELINK": "SITELINK", "CALLOUT": "CALLOUT", "AD_IMAGE": "IMAGE"}[field_type]
        if asset_type != expected:
            return {"error": f"Asset {new_asset['asset_id']} is {asset_type}, expected {expected}"}

    if new_asset.get("image_url"):
        size_err = _check_image_url(new_asset["image_url"])
        if size_err:
            return {"error": size_err}

    action = "swap" if old and new_asset else ("unlink" if old else "add")
    plan = ChangePlan(
        operation="swap_asset",
        entity_type=f"{level}_asset",
        entity_id=str(parent_id or cid),
        customer_id=customer_id,
        changes={
            "action": action,
            "level": level,
            "parent_id": str(parent_id or ""),
            "field_type": field_type,
            "old": old,
            "new": new_asset or None,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = [
        "Only the link is removed; the old asset stays wherever else it is linked."
    ] if old else []
    return preview


def _check_image_url(url: str) -> str | None:
    data, err = _download(url)
    if err:
        return err
    if len(data) > IMAGE_MAX_BYTES:
        return f"Image is {len(data) / 1e6:.1f} MB (max 5 MB)"
    return None


def _download(url: str) -> tuple[bytes, str | None]:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - user-supplied asset URL
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read(IMAGE_MAX_BYTES + 1)
    except Exception as e:  # pragma: no cover - network
        return b"", f"Could not download image_url: {e}"
    if not ctype.startswith("image/"):
        return b"", (
            f"image_url returned '{ctype}', not an image. Drive 'view' links are "
            "HTML pages; use a direct download URL or an existing asset_id."
        )
    return data, None


# ---------------------------------------------------------------------------
# Apply (called from write._execute_plan)
# ---------------------------------------------------------------------------


def google_ads_error_text(exc: Exception) -> str:
    """Readable message from a GoogleAdsException (each error's code + message)."""
    failure = getattr(exc, "failure", None)
    if failure is not None and getattr(failure, "errors", None):
        parts = []
        for err in failure.errors:
            code = next(
                (f"{k}={getattr(getattr(err.error_code, k), 'name', v)}"
                 for k, v in type(err.error_code).to_dict(err.error_code).items() if v),
                "",
            )
            field = ".".join(
                el.field_name for el in getattr(err.location, "field_path_elements", [])
            )
            parts.append(f"{code}: {err.message}" + (f" (field: {field})" if field else ""))
        return "; ".join(parts)
    raw = str(exc)
    return raw[:500] + "..." if len(raw) > 500 else raw


def apply_update_rsa(client, cid: str, changes: dict, *, config=None, validate_only: bool = False) -> dict:
    """AdService.MutateAds update of headlines + descriptions, pins kept."""
    if config is not None:
        live = read_rsa(config, cid, changes["ad_id"])
        if "error" in live:
            raise ValueError(live["error"])
        before = changes["before"]
        if live["headlines"] != before["headlines"] or live["descriptions"] != before["descriptions"]:
            raise ValueError(
                "The ad changed since this plan was drafted. Draft it again so "
                "the swaps apply to the current text."
            )

    service = client.get_service("AdService")
    operation = client.get_type("AdOperation")
    ad = operation.update
    ad.resource_name = service.ad_path(cid, changes["ad_id"])
    pin_enum = client.enums.ServedAssetFieldTypeEnum

    for key, target in (("headlines", ad.responsive_search_ad.headlines),
                        ("descriptions", ad.responsive_search_ad.descriptions)):
        for item in changes["after"][key]:
            asset = client.get_type("AdTextAsset")
            asset.text = item["text"]
            if item.get("pinned_field") not in (None, "", "UNSPECIFIED", "UNKNOWN"):
                asset.pinned_field = getattr(pin_enum, item["pinned_field"])
            target.append(asset)

    operation.update_mask.paths.extend(
        ["responsive_search_ad.headlines", "responsive_search_ad.descriptions"]
    )
    request = client.get_type("MutateAdsRequest")
    request.customer_id = cid
    request.operations.append(operation)
    request.validate_only = validate_only
    response = service.mutate_ads(request=request)
    if validate_only:
        return {"validated": True}
    return {"resource_name": response.results[0].resource_name}


def apply_swap_asset(client, cid: str, changes: dict, *, config=None, validate_only: bool = False) -> dict:
    """One atomic GoogleAdsService.Mutate: create asset, link it, unlink the old one."""
    level, parent_id, field_type = changes["level"], changes["parent_id"], changes["field_type"]
    new, old = changes.get("new"), changes.get("old")
    gas = client.get_service("GoogleAdsService")
    field_enum = getattr(client.enums.AssetFieldTypeEnum, field_type)
    ops = []

    new_asset_rn = None
    if new and new.get("asset_id"):
        new_asset_rn = client.get_service("AssetService").asset_path(cid, new["asset_id"])
    elif new:
        new_asset_rn = f"customers/{cid}/assets/-1"
        op = client.get_type("MutateOperation")
        asset = op.asset_operation.create
        asset.resource_name = new_asset_rn
        if field_type == "SITELINK":
            asset.sitelink_asset.link_text = new["link_text"]
            if new.get("description1"):
                asset.sitelink_asset.description1 = new["description1"]
                asset.sitelink_asset.description2 = new["description2"]
            asset.final_urls.append(new["final_url"])
        elif field_type == "CALLOUT":
            asset.callout_asset.callout_text = new["text"]
        elif field_type == "AD_IMAGE":
            data, err = _download(new["image_url"])
            if err:
                raise ValueError(err)
            asset.type_ = client.enums.AssetTypeEnum.IMAGE
            asset.image_asset.data = data
            asset.name = new.get("name") or new["image_url"].rsplit("/", 1)[-1][:100]
        ops.append(op)

    if new_asset_rn:
        op = client.get_type("MutateOperation")
        if level == "customer":
            link = op.customer_asset_operation.create
        elif level == "campaign":
            link = op.campaign_asset_operation.create
            link.campaign = client.get_service("CampaignService").campaign_path(cid, parent_id)
        else:
            link = op.ad_group_asset_operation.create
            link.ad_group = client.get_service("AdGroupService").ad_group_path(cid, parent_id)
        link.asset = new_asset_rn
        link.field_type = field_enum
        ops.append(op)

    if old:
        op = client.get_type("MutateOperation")
        rn = link_resource_name(cid, level, parent_id, old["asset_id"], field_type)
        if level == "customer":
            op.customer_asset_operation.remove = rn
        elif level == "campaign":
            op.campaign_asset_operation.remove = rn
        else:
            op.ad_group_asset_operation.remove = rn
        ops.append(op)

    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = cid
    request.mutate_operations.extend(ops)
    request.validate_only = validate_only
    response = gas.mutate(request=request)
    if validate_only:
        return {"validated": True, "operations": len(ops)}
    names = []
    for r in response.mutate_operation_responses:
        for attr in ("asset_result", "customer_asset_result", "campaign_asset_result", "ad_group_asset_result"):
            res = getattr(r, attr, None)
            if res and res.resource_name:
                names.append(res.resource_name)
    return {"resource_names": names}
