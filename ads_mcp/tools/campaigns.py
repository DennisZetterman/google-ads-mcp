# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Write tools for campaigns: pause/enable, change daily budget, create paused.

Every tool follows the propose-and-confirm workflow in
``ads_mcp.propose_and_confirm``: the first call (``validate_only=True``, the
default) sends the request to the Google Ads API with ``validate_only`` set,
so nothing is changed, and returns the proposed changes plus a checksum. The
change is only committed when the tool is called again with
``validate_only=False``, the exact same parameters and that checksum.
"""

from enum import Enum
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import propose_and_confirm as pac
import ads_mcp.utils as utils

campaigns_mcp = FastMCP("campaigns")

_MICROS = 1_000_000
# Google Ads requires budget amounts in multiples of 10 000 micros (0.01).
_BUDGET_STEP_MICROS = 10_000


class CampaignStatus(str, Enum):
    """Statuses that set_campaign_status can move a campaign to."""

    ENABLED = "ENABLED"
    PAUSED = "PAUSED"


class BiddingStrategy(str, Enum):
    """Bidding strategies supported by create_paused_campaign."""

    MAXIMIZE_CLICKS = "MAXIMIZE_CLICKS"
    MAXIMIZE_CONVERSIONS = "MAXIMIZE_CONVERSIONS"
    MANUAL_CPC = "MANUAL_CPC"


def _clean_customer_id(customer_id: str | int) -> str:
    cleaned = utils.clean_customer_id(customer_id)
    if not cleaned:
        raise ToolError("Error: 'customer_id' must contain digits.")
    return cleaned


def _clean_campaign_id(campaign_id: str | int) -> str:
    cleaned = utils.clean_customer_id(utils.extract_id(str(campaign_id)))
    if not cleaned:
        raise ToolError("Error: 'campaign_id' must contain digits.")
    return cleaned


def _enum_value(value: Any) -> str:
    return value.value if isinstance(value, Enum) else str(value).upper()


def _to_micros(amount: float, field_name: str) -> int:
    """Converts an amount in account currency to micros, rounded to 0.01."""
    try:
        amount_f = float(amount)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"Error: '{field_name}' must be a number.") from exc
    if amount_f <= 0:
        raise ToolError(f"Error: '{field_name}' must be greater than 0.")
    steps = round(amount_f * _MICROS / _BUDGET_STEP_MICROS)
    if steps <= 0:
        raise ToolError(f"Error: '{field_name}' must be at least 0.01.")
    return steps * _BUDGET_STEP_MICROS


def _format_money(micros: int, currency: str = "") -> str:
    text = f"{micros / _MICROS:.2f}"
    return f"{text} {currency}".strip()


def _search_one(ga_client: Any, customer_id: str, query: str) -> Any | None:
    """Runs a GAQL query and returns the first row, or None."""
    ga_service = utils.get_googleads_service_from_client(
        ga_client, "GoogleAdsService"
    )
    for row in ga_service.search(customer_id=customer_id, query=query):
        return row
    return None


# ---------------------------------------------------------------------------
# 1. Pause / enable a campaign
# ---------------------------------------------------------------------------


@campaigns_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def set_campaign_status(
    customer_id: str | int,
    campaign_id: str | int,
    status: CampaignStatus,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Pauses or enables an existing campaign.

    Propose & Confirm Workflow (`validate_only` and `checksum`):
    1. Call `set_campaign_status` with `validate_only=True` (the default). The request is validated by the Google Ads API without changing anything, and the tool returns `change_items` (current and new status) and a `checksum`.
    2. Present the `change_items` to the user and wait for explicit user approval.
    3. Only after user confirmation, call `set_campaign_status` with `validate_only=False` along with the exact same parameters and the `checksum` returned by the previous `validate_only=True` call.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated, e.g. '123-456-7890').
        campaign_id: The ID of the campaign (e.g. '987654321').
        status: The new status. Must be 'ENABLED' or 'PAUSED'.
        login_customer_id: Optional manager (MCC) customer ID to use as the login-customer-id header.
        validate_only: If True (default), only validates and returns the proposed change and a `checksum`. If False, requires `checksum` from a prior `validate_only=True` call and commits the change.
        checksum: Checksum returned by a prior `validate_only=True` call. Required when `validate_only=False`.
    """
    cid = _clean_customer_id(customer_id)
    camp_id = _clean_campaign_id(campaign_id)
    status_upper = _enum_value(status)
    if status_upper not in CampaignStatus.__members__:
        raise ToolError(
            f"Error: Invalid status '{status}'. Must be ENABLED or PAUSED."
        )

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    resource_name = f"customers/{cid}/campaigns/{camp_id}"

    current = _search_one(
        ga_client,
        cid,
        "SELECT campaign.id, campaign.name, campaign.status "
        f"FROM campaign WHERE campaign.id = {camp_id}",
    )
    if current is None:
        raise ToolError(
            f"Error: Campaign {camp_id} was not found in customer {cid}."
        )
    current_status = utils.format_output_value(current.campaign.status)
    campaign_name = current.campaign.name

    campaign_service = utils.get_googleads_service_from_client(
        ga_client, "CampaignService"
    )
    op = ga_client.get_type("CampaignOperation")
    op.update.resource_name = resource_name
    op.update.status = getattr(ga_client.enums.CampaignStatusEnum, status_upper)
    op.update_mask.paths.append("status")

    request = ga_client.get_type("MutateCampaignsRequest")
    request.customer_id = cid
    request.operations.append(op)

    operation_payload = {
        "tool": "set_campaign_status",
        "campaign_id": camp_id,
        "status": status_upper,
    }
    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable="Campaign",
        primary_key_readable=f"{campaign_name} ({camp_id})",
        fields={"Status": status_upper},
        original_fields={"Status": current_status},
    )

    def _validate() -> None:
        request.validate_only = True
        campaign_service.mutate_campaigns(request=request)

    def _commit() -> str:
        request.validate_only = False
        campaign_service.mutate_campaigns(request=request)
        verb = "enabled" if status_upper == "ENABLED" else "paused"
        return (
            f"Successfully {verb} campaign '{campaign_name}' (ID: {camp_id}). "
            f"Status changed from {current_status} to {status_upper}."
        )

    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=operation_payload,
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to change campaign status",
    )


# ---------------------------------------------------------------------------
# 2. Change daily budget
# ---------------------------------------------------------------------------


@campaigns_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def update_campaign_daily_budget(
    customer_id: str | int,
    campaign_id: str | int,
    daily_budget: float,
    allow_shared_budget: bool = False,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Changes the daily budget of the budget attached to a campaign.

    The amount is given in the account's currency (e.g. 250 means 250 SEK for a
    SEK account) and rounded to two decimals. If the campaign uses a shared
    budget, every campaign on that budget is affected, so the tool refuses
    unless `allow_shared_budget=True`.

    Propose & Confirm Workflow (`validate_only` and `checksum`):
    1. Call `update_campaign_daily_budget` with `validate_only=True` (the default). The request is validated by the Google Ads API without changing anything, and the tool returns `change_items` (current and new budget) and a `checksum`.
    2. Present the `change_items` to the user and wait for explicit user approval.
    3. Only after user confirmation, call `update_campaign_daily_budget` with `validate_only=False` along with the exact same parameters and the `checksum` returned by the previous `validate_only=True` call.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated, e.g. '123-456-7890').
        campaign_id: The ID of the campaign whose budget should change.
        daily_budget: New average daily budget in the account currency (e.g. 250.0).
        allow_shared_budget: Must be True to change a budget shared by several campaigns.
        login_customer_id: Optional manager (MCC) customer ID to use as the login-customer-id header.
        validate_only: If True (default), only validates and returns the proposed change and a `checksum`. If False, requires `checksum` from a prior `validate_only=True` call and commits the change.
        checksum: Checksum returned by a prior `validate_only=True` call. Required when `validate_only=False`.
    """
    cid = _clean_customer_id(customer_id)
    camp_id = _clean_campaign_id(campaign_id)
    new_micros = _to_micros(daily_budget, "daily_budget")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    current = _search_one(
        ga_client,
        cid,
        "SELECT campaign.id, campaign.name, campaign_budget.resource_name, "
        "campaign_budget.amount_micros, campaign_budget.explicitly_shared, "
        "campaign_budget.reference_count, customer.currency_code "
        f"FROM campaign WHERE campaign.id = {camp_id}",
    )
    if current is None:
        raise ToolError(
            f"Error: Campaign {camp_id} was not found in customer {cid}."
        )

    budget = current.campaign_budget
    currency = current.customer.currency_code
    campaign_name = current.campaign.name
    if not budget.resource_name:
        raise ToolError(
            f"Error: Campaign {camp_id} has no campaign budget to update."
        )
    if budget.explicitly_shared and not allow_shared_budget:
        raise ToolError(
            f"Error: Campaign {camp_id} uses a shared budget used by "
            f"{budget.reference_count} campaigns. Changing it affects all of "
            "them. Call again with allow_shared_budget=True if that is intended."
        )

    budget_service = utils.get_googleads_service_from_client(
        ga_client, "CampaignBudgetService"
    )
    op = ga_client.get_type("CampaignBudgetOperation")
    op.update.resource_name = budget.resource_name
    op.update.amount_micros = new_micros
    op.update_mask.paths.append("amount_micros")

    request = ga_client.get_type("MutateCampaignBudgetsRequest")
    request.customer_id = cid
    request.operations.append(op)

    operation_payload = {
        "tool": "update_campaign_daily_budget",
        "campaign_id": camp_id,
        "budget_resource_name": budget.resource_name,
        "amount_micros": new_micros,
        "allow_shared_budget": bool(allow_shared_budget),
    }
    fields: dict[str, Any] = {
        "Daily budget": _format_money(new_micros, currency)
    }
    original: dict[str, Any] = {
        "Daily budget": _format_money(budget.amount_micros, currency)
    }
    if budget.explicitly_shared:
        fields["Shared budget"] = (
            f"Yes, used by {budget.reference_count} campaigns"
        )
    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable="Campaign budget",
        primary_key_readable=f"{campaign_name} ({camp_id})",
        fields=fields,
        original_fields=original,
    )

    def _validate() -> None:
        request.validate_only = True
        budget_service.mutate_campaign_budgets(request=request)

    def _commit() -> str:
        request.validate_only = False
        budget_service.mutate_campaign_budgets(request=request)
        return (
            f"Successfully changed the daily budget of campaign "
            f"'{campaign_name}' (ID: {camp_id}) from "
            f"{_format_money(budget.amount_micros, currency)} to "
            f"{_format_money(new_micros, currency)}."
        )

    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=operation_payload,
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to change campaign budget",
    )


# ---------------------------------------------------------------------------
# 3. Create a campaign as paused
# ---------------------------------------------------------------------------


@campaigns_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
def create_paused_campaign(
    customer_id: str | int,
    name: str,
    daily_budget: float,
    bidding_strategy: BiddingStrategy = BiddingStrategy.MAXIMIZE_CLICKS,
    max_cpc: float | None = None,
    include_search_partners: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates a new Search campaign with its own daily budget. The campaign is always created PAUSED.

    The budget and campaign are created in one atomic request. The campaign is
    never enabled by this tool; use `set_campaign_status` once ad groups, ads
    and keywords are in place.

    Propose & Confirm Workflow (`validate_only` and `checksum`):
    1. Call `create_paused_campaign` with `validate_only=True` (the default). The request is validated by the Google Ads API without creating anything, and the tool returns `change_items` and a `checksum`.
    2. Present the `change_items` to the user and wait for explicit user approval.
    3. Only after user confirmation, call `create_paused_campaign` with `validate_only=False` along with the exact same parameters and the `checksum` returned by the previous `validate_only=True` call.

    Args:
        customer_id: Google Ads Customer ID (digits or hyphenated, e.g. '123-456-7890').
        name: Campaign name. Must be unique among the account's campaigns.
        daily_budget: Average daily budget in the account currency (e.g. 150.0).
        bidding_strategy: 'MAXIMIZE_CLICKS' (default), 'MAXIMIZE_CONVERSIONS' or 'MANUAL_CPC'.
        max_cpc: Optional max CPC bid limit in account currency. Only used with MAXIMIZE_CLICKS.
        include_search_partners: Whether to also show ads on Google search partners. Default False.
        start_date: Optional start date 'YYYY-MM-DD'.
        end_date: Optional end date 'YYYY-MM-DD'.
        login_customer_id: Optional manager (MCC) customer ID to use as the login-customer-id header.
        validate_only: If True (default), only validates and returns the proposed campaign and a `checksum`. If False, requires `checksum` from a prior `validate_only=True` call and creates the campaign.
        checksum: Checksum returned by a prior `validate_only=True` call. Required when `validate_only=False`.
    """
    cid = _clean_customer_id(customer_id)
    if not name or not name.strip():
        raise ToolError("Error: 'name' is required.")
    name = name.strip()
    budget_micros = _to_micros(daily_budget, "daily_budget")
    strategy = _enum_value(bidding_strategy)
    if strategy not in BiddingStrategy.__members__:
        valid = ", ".join(BiddingStrategy.__members__)
        raise ToolError(
            f"Error: Invalid bidding_strategy '{bidding_strategy}'. Must be one of: {valid}."
        )
    max_cpc_micros = None
    if max_cpc is not None:
        if strategy != BiddingStrategy.MAXIMIZE_CLICKS.value:
            raise ToolError(
                "Error: 'max_cpc' can only be used with MAXIMIZE_CLICKS."
            )
        max_cpc_micros = _to_micros(max_cpc, "max_cpc")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    enums = ga_client.enums

    budget_temp_rn = f"customers/{cid}/campaignBudgets/-1"

    budget_mop = ga_client.get_type("MutateOperation")
    budget = budget_mop.campaign_budget_operation.create
    budget.resource_name = budget_temp_rn
    budget.name = f"{name} – budget"
    budget.amount_micros = budget_micros
    budget.delivery_method = enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False

    campaign_mop = ga_client.get_type("MutateOperation")
    campaign = campaign_mop.campaign_operation.create
    campaign.name = name
    campaign.status = enums.CampaignStatusEnum.PAUSED
    campaign.advertising_channel_type = enums.AdvertisingChannelTypeEnum.SEARCH
    campaign.campaign_budget = budget_temp_rn
    campaign.network_settings.target_google_search = True
    campaign.network_settings.target_search_network = bool(
        include_search_partners
    )
    campaign.network_settings.target_content_network = False
    campaign.network_settings.target_partner_search_network = False
    campaign.contains_eu_political_advertising = (
        enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )

    if strategy == BiddingStrategy.MAXIMIZE_CLICKS.value:
        if max_cpc_micros:
            campaign.target_spend.cpc_bid_ceiling_micros = max_cpc_micros
        else:
            # Setting an empty message selects the strategy.
            campaign.target_spend = ga_client.get_type("TargetSpend")
    elif strategy == BiddingStrategy.MAXIMIZE_CONVERSIONS.value:
        campaign.maximize_conversions = ga_client.get_type(
            "MaximizeConversions"
        )
    else:
        campaign.manual_cpc.enhanced_cpc_enabled = False

    if start_date:
        campaign.start_date_time = f"{start_date.strip()} 00:00:00"
    if end_date:
        campaign.end_date_time = f"{end_date.strip()} 23:59:59"

    request = ga_client.get_type("MutateGoogleAdsRequest")
    request.customer_id = cid
    request.mutate_operations.extend([budget_mop, campaign_mop])

    ga_service = utils.get_googleads_service_from_client(
        ga_client, "GoogleAdsService"
    )

    operation_payload = {
        "tool": "create_paused_campaign",
        "name": name,
        "budget_micros": budget_micros,
        "bidding_strategy": strategy,
        "max_cpc_micros": max_cpc_micros,
        "include_search_partners": bool(include_search_partners),
        "start_date": start_date,
        "end_date": end_date,
    }
    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable="Campaign (SEARCH)",
        primary_key_readable=name,
        fields={
            "Name": name,
            "Status": "PAUSED",
            "Channel": "SEARCH",
            "Daily budget": _format_money(budget_micros),
            "Bidding strategy": strategy,
            "Max CPC": (
                _format_money(max_cpc_micros) if max_cpc_micros else None
            ),
            "Google Search": "Yes",
            "Search partners": "Yes" if include_search_partners else "No",
            "Display network": "No",
            "Start date": start_date,
            "End date": end_date,
        },
    )

    def _validate() -> None:
        request.validate_only = True
        ga_service.mutate(request=request)

    def _commit() -> str:
        request.validate_only = False
        response = ga_service.mutate(request=request)
        campaign_rn = ""
        for res in response.mutate_operation_responses:
            rn = res.campaign_result.resource_name
            if rn:
                campaign_rn = rn
        camp_id = utils.extract_id(campaign_rn) if campaign_rn else "unknown"
        return (
            f"Successfully created PAUSED campaign '{name}'. ID: {camp_id}. "
            "Add ad groups, ads and keywords before enabling it with "
            "set_campaign_status."
        )

    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=operation_payload,
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to create campaign",
    )
