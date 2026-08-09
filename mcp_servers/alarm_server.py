from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from mcp.server.fastmcp import FastMCP

from alarmops.alarm_client import AlarmApiClient
from alarmops.mcp_contracts import (
    AlarmListResult,
    AlarmSummaryResult,
    AssetMetadataResult,
    AssetSearchResult,
    CalculationResult,
    PriorityResult,
    RationalizationResult,
    RecommendationResult,
)
from alarmops.settings import get_settings

settings = get_settings()
client = AlarmApiClient(settings)
mcp = FastMCP(
    "Alarm Intelligence MCP",
    instructions="Read-only tools for enterprise alarm and asset intelligence.",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "9001")),
)


def _range(days: int) -> dict[str, str]:
    end = datetime.now(UTC)
    return {"start_time": (end - timedelta(days=days)).isoformat(), "end_time": end.isoformat()}


@mcp.tool()
async def search_assets(query: str, limit: int = 5) -> AssetSearchResult:
    """Resolve a human asset name or identifier to authoritative asset records."""
    result = await client.request("GET", "/assets/search", params={"query": query, "limit": limit})
    return AssetSearchResult.model_validate(result)


@mcp.tool()
async def get_asset_metadata(asset_id: str) -> AssetMetadataResult:
    """Return authoritative asset criticality, ownership, design, and location metadata."""
    return AssetMetadataResult.model_validate(
        await client.request("GET", f"/assets/{asset_id}/metadata")
    )


@mcp.tool()
async def get_alarms(asset_id: str, lookback_days: int = 90) -> AlarmListResult:
    """List an asset's alarms over a bounded lookback period."""
    time_range = _range(lookback_days)
    return AlarmListResult.model_validate(
        await client.request(
            "GET",
            "/alarms",
            params={
                "asset_id": asset_id,
                **time_range,
                "page_size": 200,
                "sort_order": "desc",
            },
        )
    )


@mcp.tool()
async def summarize_alarms(
    asset_id: str, lookback_days: int = 90, severities: list[str] | None = None
) -> AlarmSummaryResult:
    """Aggregate count, recurrence, criticality, and acknowledgement delay for an asset."""
    return AlarmSummaryResult.model_validate(
        await client.request(
            "POST",
            "/alarms/summary",
            json={
                "asset_ids": [asset_id],
                "time_range": _range(lookback_days),
                "severity": severities or [],
                "group_by": ["alarm_name", "severity"],
                "kpis": ["alarm_count", "recurring_rate", "avg_ack_delay"],
            },
        )
    )


@mcp.tool()
async def find_rationalization_candidates(
    asset_id: str, lookback_days: int = 90, recurrence_threshold: int = 5
) -> RationalizationResult:
    """Identify recurring alarms that merit engineering rationalization review."""
    return RationalizationResult.model_validate(
        await client.request(
            "POST",
            "/alarms/rationalization-candidates",
            json={
                "asset_ids": [asset_id],
                "time_range": _range(lookback_days),
                "recurrence_threshold": recurrence_threshold,
                "stale_minutes_threshold": 180,
            },
        )
    )


@mcp.tool()
async def score_alarm_priority(alarm_id: str) -> PriorityResult:
    """Score a concrete alarm using severity, active state, and asset criticality."""
    return PriorityResult.model_validate(
        await client.request("POST", "/alarms/priority-score", json={"alarm_id": alarm_id})
    )


@mcp.tool()
async def get_operator_recommendations(alarm_id: str) -> RecommendationResult:
    """Retrieve simulator recommendations for a concrete alarm; these are advisory only."""
    return RecommendationResult.model_validate(
        await client.request(
            "POST", "/recommendations/operator-actions", json={"alarm_id": alarm_id}
        )
    )


@mcp.tool()
async def calculate_operator_response_efficiency(asset_id: str) -> CalculationResult:
    """Generate and execute the safe built-in response-efficiency calculation."""
    generated = await client.request(
        "POST",
        "/calculation-code/generate",
        json={"calculation_type": "operator_response_efficiency", "filters": {"asset_id": asset_id}},
    )
    return CalculationResult.model_validate(
        await client.request(
            "POST",
            "/calculation-code/execute",
            json={
                "calculation_id": generated["calculation_id"],
                "filters": {"asset_id": asset_id},
            },
        )
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
