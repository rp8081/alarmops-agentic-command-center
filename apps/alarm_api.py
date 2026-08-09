from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime
from statistics import mean
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from alarmops.data import ASSETS, alarms
from alarmops.models import (
    CalculationExecute,
    CalculationGenerate,
    CorrelationRequest,
    FloodRequest,
    PriorityRequest,
    RationalizationRequest,
    RecommendationRequest,
    SummaryRequest,
    TrendsRequest,
)
from alarmops.settings import get_settings

app = FastAPI(title="Alarm API Simulator", version="1.0.0")
_CALCULATIONS: dict[str, CalculationGenerate] = {}
_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


@app.middleware("http")
async def auth_and_trace(request: Request, call_next: Any) -> JSONResponse:
    if request.url.path != "/health":
        expected = get_settings().alarm_api_token.get_secret_value()
        if request.headers.get("authorization") != f"Bearer {expected}":
            return JSONResponse({"detail": "invalid bearer token"}, status_code=401)
    trace_id = request.headers.get("trace_id") or request.headers.get("x-trace-id") or str(uuid4())
    response = await call_next(request)
    response.headers["x-trace-id"] = trace_id
    return response


def _filtered(
    *,
    asset_ids: list[str] | None = None,
    site: str | None = None,
    unit: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    severities: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    rows = [row.model_dump(mode="json") for row in alarms()]
    if asset_ids:
        rows = [row for row in rows if row["asset_id"] in asset_ids]
    if site:
        rows = [row for row in rows if row["site"].casefold() == site.casefold()]
    if unit:
        rows = [row for row in rows if row["unit"].casefold() == unit.casefold()]
    if severities:
        rows = [row for row in rows if row["severity"] in severities]
    if start:
        rows = [row for row in rows if datetime.fromisoformat(row["start_time"]) >= start]
    if end:
        rows = [row for row in rows if datetime.fromisoformat(row["start_time"]) <= end]
    return rows


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "alarm-api", "version": "1.0.0"}


@app.get("/assets/search")
async def search_assets(query: str = "", limit: int = Query(10, ge=1, le=100)) -> dict[str, Any]:
    terms = query.casefold().split()
    matches = []
    for asset in ASSETS:
        value = " ".join([asset.asset_id, asset.asset_name, asset.asset_type, *asset.tags]).casefold()
        if not terms or all(term in value for term in terms):
            matches.append(asset.model_dump())
    return {"query": query, "count": len(matches[:limit]), "results": matches[:limit]}


@app.get("/assets/{asset_id}/metadata")
async def asset_metadata(asset_id: str) -> dict[str, Any]:
    asset = next((item for item in ASSETS if item.asset_id == asset_id), None)
    if not asset:
        raise HTTPException(404, "asset not found")
    return {
        **asset.model_dump(),
        "manufacturer": "Apex Rotating Equipment",
        "commissioned": "2021-03-12",
        "design_pressure_mpa": 10.5,
        "owner": "Utilities Operations",
    }


@app.get("/alarms")
async def list_alarms(
    asset_id: str | None = None,
    site: str | None = None,
    unit: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    sort_by: str = "start_time",
    sort_order: str = "desc",
) -> dict[str, Any]:
    rows = _filtered(
        asset_ids=[asset_id] if asset_id else None,
        site=site,
        unit=unit,
        start=start_time,
        end=end_time,
        severities=[severity] if severity else None,
    )
    if status:
        rows = [row for row in rows if row["status"] == status]
    safe_sort = sort_by if sort_by in {"start_time", "severity", "alarm_id"} else "start_time"
    rows.sort(key=lambda row: str(row[safe_sort]), reverse=sort_order == "desc")
    start_index = (page - 1) * page_size
    data = rows[start_index : start_index + page_size]
    return {"data": data, "items": data, "total": len(rows), "page": page, "page_size": page_size}


@app.get("/alarms/{alarm_id}")
async def get_alarm(alarm_id: str) -> dict[str, Any]:
    row = next((item for item in alarms() if item.alarm_id == alarm_id), None)
    if not row:
        raise HTTPException(404, "alarm not found")
    return row.model_dump(mode="json")


@app.post("/alarms/summary")
async def summarize(request: SummaryRequest) -> dict[str, Any]:
    rows = _filtered(
        asset_ids=request.asset_ids,
        site=request.site,
        unit=request.unit,
        start=request.time_range.start_time,
        end=request.time_range.end_time,
        severities=request.severity,
    )
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(str(row.get(key, "")) for key in request.group_by)].append(row)
    breakdown = []
    for keys, values in grouped.items():
        item: dict[str, Any] = dict(zip(request.group_by, keys, strict=False))
        item.update(
            alarm_count=len(values),
            critical_count=sum(row["severity"] == "critical" for row in values),
            recurring_rate=round(len(values) / max(len(rows), 1), 3),
            avg_ack_delay=round(mean(row["acknowledgement_delay_seconds"] for row in values), 1),
        )
        breakdown.append(item)
    return {
        "total_alarms": len(rows),
        "critical_count": sum(row["severity"] == "critical" for row in rows),
        "active_count": sum(row["status"] == "active" for row in rows),
        "average_ack_delay_seconds": round(mean([row["acknowledgement_delay_seconds"] for row in rows]) if rows else 0, 1),
        "breakdown": breakdown,
        "data": breakdown,
    }


@app.post("/alarms/trends")
async def trends(request: TrendsRequest) -> dict[str, Any]:
    rows = _filtered(
        asset_ids=request.asset_ids,
        site=request.site,
        unit=request.unit,
        start=request.time_range.start_time,
        end=request.time_range.end_time,
    )
    buckets = Counter(str(row["start_time"])[:10] for row in rows)
    return {"bucket": request.bucket, "series": [{"period": key, "alarm_count": value} for key, value in sorted(buckets.items())]}


@app.post("/alarms/correlation")
async def correlation(request: CorrelationRequest) -> dict[str, Any]:
    rows = _filtered(asset_ids=request.asset_ids, start=request.time_range.start_time, end=request.time_range.end_time)
    rows = [row for row in rows if _SEVERITY_RANK[row["severity"]] >= _SEVERITY_RANK[request.severity_threshold]]
    counts = Counter(row["asset_id"] for row in rows)
    pairs = [
        {"source_asset_id": left, "target_asset_id": right, "support": min(counts[left], counts[right]), "method": request.correlation_method}
        for index, left in enumerate(request.asset_ids)
        for right in request.asset_ids[index + 1 :]
        if min(counts[left], counts[right]) >= request.min_support
    ]
    return {"correlations": pairs, "total_events": len(rows), "lag_window_minutes": request.lag_window_minutes}


@app.post("/alarms/flood-analysis")
async def flood(request: FloodRequest) -> dict[str, Any]:
    rows = _filtered(unit=request.unit, start=request.time_range.start_time, end=request.time_range.end_time)
    windows: list[dict[str, Any]] = []
    if len(rows) >= request.threshold_count:
        windows.append({"start": request.time_range.start_time.isoformat(), "end": request.time_range.end_time.isoformat(), "count": len(rows)})
    return {"is_flood": bool(windows), "flood_windows": windows, "windows": windows, "threshold_count": request.threshold_count}


@app.post("/alarms/rationalization-candidates")
async def rationalization(request: RationalizationRequest) -> dict[str, Any]:
    rows = _filtered(
        asset_ids=request.asset_ids,
        site=request.site,
        unit=request.unit,
        start=request.time_range.start_time,
        end=request.time_range.end_time,
    )
    groups = Counter((row["asset_id"], row["alarm_name"]) for row in rows)
    candidates = [
        {"asset_id": asset_id, "alarm_name": name, "occurrences": count, "reason": "recurring above configured threshold", "recommended_review": "check setpoint, deadband, suppression, and root cause"}
        for (asset_id, name), count in groups.items()
        if count >= request.recurrence_threshold
    ]
    return {"candidate_count": len(candidates), "candidates": candidates, "data": candidates}


@app.post("/alarms/priority-score")
async def priority_score(request: PriorityRequest) -> dict[str, Any]:
    alarm = next((row for row in alarms() if row.alarm_id == request.alarm_id), None)
    if not alarm:
        raise HTTPException(404, "alarm not found")
    score = _SEVERITY_RANK[alarm.severity] * 20 + (15 if alarm.status == "active" else 0)
    return {"alarm_id": alarm.alarm_id, "priority_score": min(score, 100), "priority_band": "P1" if score >= 80 else "P2", "factors": [alarm.severity, alarm.status, "asset criticality"]}


@app.post("/recommendations/operator-actions")
async def recommendations(request: RecommendationRequest) -> dict[str, Any]:
    alarm = next((row for row in alarms() if row.alarm_id == request.alarm_id), None)
    if not alarm:
        raise HTTPException(404, "alarm not found")
    actions = [
        "Confirm the reading against the redundant instrument before intervening.",
        "Check suction strainer differential pressure and upstream valve position.",
        "If pressure continues to fall, start the standby pump under the approved procedure.",
    ]
    return {"alarm_id": alarm.alarm_id, "asset_id": alarm.asset_id, "actions": actions, "disclaimer": "Decision support only; operator remains accountable."}


@app.post("/calculation-code/generate")
async def generate_calculation(request: CalculationGenerate) -> dict[str, Any]:
    calculation_id = f"CALC-{uuid4().hex[:8].upper()}"
    _CALCULATIONS[calculation_id] = request
    return {"calculation_id": calculation_id, "calculation_type": request.calculation_type, "status": "validated", "code_preview": "safe_builtin_metric(filters)"}


@app.post("/calculation-code/execute")
async def execute_calculation(request: CalculationExecute) -> dict[str, Any]:
    definition = _CALCULATIONS.get(request.calculation_id)
    if not definition:
        raise HTTPException(404, "calculation not found")
    relevant = _filtered(unit=request.filters.get("unit"))
    value = round(mean([row["acknowledgement_delay_seconds"] for row in relevant]) if relevant else 0, 2)
    return {"calculation_id": request.calculation_id, "calculation_type": definition.calculation_type, "result": value, "unit": "seconds", "row_count": len(relevant)}


@app.get("/analytics/kpi-definitions")
async def kpi_definitions(x_client_id: str | None = Header(None)) -> dict[str, Any]:
    return {"client_id": x_client_id, "definitions": [{"name": "alarm_count", "description": "Number of alarm occurrences"}, {"name": "avg_ack_delay", "description": "Mean time to acknowledge in seconds"}, {"name": "recurring_rate", "description": "Share of events belonging to a repeated alarm signature"}]}
