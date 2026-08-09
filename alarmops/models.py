from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Severity = Literal["low", "medium", "high", "critical"]


class TimeRange(BaseModel):
    start_time: datetime
    end_time: datetime

    @model_validator(mode="after")
    def ordered(self) -> TimeRange:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class Asset(BaseModel):
    asset_id: str
    asset_name: str
    site: str
    unit: str
    asset_type: str
    criticality: Literal["A", "B", "C"]
    tags: list[str] = Field(default_factory=list)


class Alarm(BaseModel):
    alarm_id: str
    asset_id: str
    asset_name: str
    site: str
    unit: str
    alarm_name: str
    alarm_type: str
    severity: Severity
    status: Literal["active", "acknowledged", "cleared", "shelved"]
    start_time: datetime
    acknowledgement_delay_seconds: int
    message: str


class SummaryRequest(BaseModel):
    asset_ids: list[str] = Field(default_factory=list)
    site: str | None = None
    unit: str | None = None
    time_range: TimeRange
    severity: list[Severity] = Field(default_factory=list)
    alarm_types: list[str] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=lambda: ["alarm_name"])
    kpis: list[str] = Field(default_factory=lambda: ["alarm_count"])


class TrendsRequest(BaseModel):
    asset_ids: list[str] = Field(default_factory=list)
    site: str | None = None
    unit: str | None = None
    time_range: TimeRange
    bucket: Literal["hourly", "daily", "weekly"] = "daily"
    metrics: list[str] = Field(default_factory=lambda: ["alarm_count"])


class CorrelationRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    time_range: TimeRange
    correlation_method: Literal["cooccurrence", "sequence"] = "cooccurrence"
    lag_window_minutes: int = Field(default=15, ge=1, le=1440)
    severity_threshold: Severity = "medium"
    min_support: int = Field(default=1, ge=1)


class FloodRequest(BaseModel):
    unit: str
    time_range: TimeRange
    threshold_count: int = Field(default=10, ge=1)
    rolling_window_minutes: int = Field(default=10, ge=1, le=1440)


class RationalizationRequest(BaseModel):
    asset_ids: list[str] = Field(default_factory=list)
    site: str | None = None
    unit: str | None = None
    time_range: TimeRange
    recurrence_threshold: int = Field(default=5, ge=1)
    stale_minutes_threshold: int = Field(default=180, ge=1)


class PriorityRequest(BaseModel):
    alarm_id: str


class RecommendationRequest(BaseModel):
    alarm_id: str
    include_related: bool = True
    include_asset_context: bool = True
    include_historical_pattern: bool = True


class CalculationGenerate(BaseModel):
    calculation_type: Literal[
        "alarm_flood_index",
        "critical_alarm_density",
        "operator_response_efficiency",
        "nuisance_alarm_score",
    ]
    filters: dict[str, Any] = Field(default_factory=dict)


class CalculationExecute(BaseModel):
    calculation_id: str
    filters: dict[str, Any] = Field(default_factory=dict)


class ToolDescriptor(BaseModel):
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    write_operation: bool = False


class Plan(BaseModel):
    intent: str
    asset_query: str
    lookback_days: int = Field(default=90, ge=1, le=730)
    severities: list[str] = Field(default_factory=lambda: ["high", "critical"])
    draft_ticket: bool = False
    execute_ticket: bool = False


class Citation(BaseModel):
    citation_id: str
    document_id: str
    title: str
    section: str
    score: float
    excerpt: str
    source_path: str


class GroundedAnswer(BaseModel):
    executive_summary: str
    findings: list[str]
    likely_contributors: list[str]
    recommended_actions: list[str]
    caveats: list[str]
    citation_ids: list[str]
    confidence: Literal["low", "medium", "high"]


class ChatRequest(BaseModel):
    query: str = Field(min_length=3, max_length=2000)
    conversation_id: str | None = None
    failure_mode: Literal["none", "alarm_timeout", "maintenance_timeout", "rag_no_results"] = "none"
    deterministic: bool = False


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    approved_by: str = Field(
        default="hackathon-reviewer", min_length=2, max_length=100, pattern=r"^[A-Za-z0-9 ._@-]+$"
    )
