from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from alarmops.models import Alarm, Asset


class Contract(BaseModel):
    model_config = ConfigDict(extra="allow")


class ToolMeta(Contract):
    trace_id: str
    status_code: int
    duration_ms: float


class AssetSearchResult(Contract):
    query: str
    count: int
    results: list[Asset]
    metadata: ToolMeta | None = Field(default=None, alias="_meta")


class AssetMetadataResult(Asset):
    manufacturer: str
    commissioned: str
    design_pressure_mpa: float
    owner: str
    metadata: ToolMeta | None = Field(default=None, alias="_meta")


class AlarmListResult(Contract):
    data: list[Alarm]
    items: list[Alarm]
    total: int
    page: int
    page_size: int
    metadata: ToolMeta | None = Field(default=None, alias="_meta")


class AlarmSummaryResult(Contract):
    total_alarms: int
    critical_count: int
    active_count: int
    average_ack_delay_seconds: float
    breakdown: list[dict[str, Any]]
    metadata: ToolMeta | None = Field(default=None, alias="_meta")


class RationalizationCandidate(Contract):
    asset_id: str
    alarm_name: str
    occurrences: int
    reason: str
    recommended_review: str


class RationalizationResult(Contract):
    candidate_count: int
    candidates: list[RationalizationCandidate]
    metadata: ToolMeta | None = Field(default=None, alias="_meta")


class PriorityResult(Contract):
    alarm_id: str
    priority_score: int
    priority_band: str
    factors: list[str]
    metadata: ToolMeta | None = Field(default=None, alias="_meta")


class RecommendationResult(Contract):
    alarm_id: str
    asset_id: str
    actions: list[str]
    disclaimer: str
    metadata: ToolMeta | None = Field(default=None, alias="_meta")


class CalculationResult(Contract):
    calculation_id: str
    calculation_type: str
    result: float
    unit: str
    row_count: int
    metadata: ToolMeta | None = Field(default=None, alias="_meta")


class MaintenanceRecord(Contract):
    work_order_id: str
    completed_at: str
    finding: str
    action: str


class MaintenanceHistoryResult(Contract):
    asset_id: str
    count: int
    records: list[MaintenanceRecord]


class WorkOrder(Contract):
    work_order_id: str
    asset_id: str
    status: str
    priority: str
    title: str


class WorkOrderResult(Contract):
    asset_id: str
    count: int
    work_orders: list[WorkOrder]


class TicketFields(Contract):
    asset_id: str
    title: str
    description: str
    priority: str


class TicketDraftResult(Contract):
    status: Literal["draft"]
    requires_human_approval: bool
    ticket: TicketFields


class TicketCreatedResult(Contract):
    status: Literal["created"]
    ticket_id: str
    run_id: str
    approved_by: str
    created_at: str
    input_digest: str
