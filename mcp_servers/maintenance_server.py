from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

from alarmops.data import MAINTENANCE, WORK_ORDERS
from alarmops.mcp_contracts import (
    MaintenanceHistoryResult,
    TicketCreatedResult,
    TicketDraftResult,
    TicketFields,
    WorkOrderResult,
)
from alarmops.settings import get_settings

settings = get_settings()
mcp = FastMCP(
    "Maintenance Operations MCP",
    instructions="Maintenance history and approval-gated escalation ticket tools.",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "9002")),
)


def _connect() -> sqlite3.Connection:
    path = Path(settings.ticket_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS tickets (ticket_id TEXT PRIMARY KEY, asset_id TEXT, title TEXT, "
        "description TEXT, priority TEXT, approved_by TEXT, created_at TEXT)"
    )
    return connection


def make_approval_reference(run_id: str, approved_by: str) -> str:
    message = f"{run_id}:{approved_by}"
    signature = hmac.new(
        settings.approval_secret.get_secret_value().encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    return f"{message}:{signature}"


def _verify(reference: str) -> tuple[str, str]:
    try:
        run_id, approved_by, signature = reference.split(":", 2)
    except ValueError as error:
        raise ValueError("malformed approval reference") from error
    expected = make_approval_reference(run_id, approved_by).rsplit(":", 1)[1]
    if not hmac.compare_digest(signature, expected):
        raise PermissionError("invalid approval signature")
    return run_id, approved_by


@mcp.tool()
def get_maintenance_history(asset_id: str, limit: int = 10) -> MaintenanceHistoryResult:
    """Return completed maintenance findings for an asset."""
    records = MAINTENANCE.get(asset_id, [])[:limit]
    return MaintenanceHistoryResult.model_validate(
        {"asset_id": asset_id, "count": len(records), "records": records}
    )


@mcp.tool()
def get_open_work_orders(asset_id: str) -> WorkOrderResult:
    """Return planned and in-progress work orders for an asset."""
    records = [row for row in WORK_ORDERS if row["asset_id"] == asset_id]
    return WorkOrderResult.model_validate(
        {"asset_id": asset_id, "count": len(records), "work_orders": records}
    )


@mcp.tool()
def draft_escalation_ticket(
    asset_id: str, title: str, description: str, priority: str = "P2"
) -> TicketDraftResult:
    """Validate and preview an escalation without writing it."""
    return TicketDraftResult(
        status="draft",
        requires_human_approval=True,
        ticket=TicketFields(
            asset_id=asset_id,
            title=title[:160],
            description=description[:2000],
            priority=priority,
        ),
    )


@mcp.tool()
def create_escalation_ticket(
    asset_id: str,
    title: str,
    description: str,
    approval_reference: str,
    priority: str = "P2",
) -> TicketCreatedResult:
    """Write an escalation only when a signed human-approval reference is supplied."""
    run_id, approved_by = _verify(approval_reference)
    ticket_id = f"OPS-{uuid4().hex[:8].upper()}"
    created_at = datetime.now(UTC).isoformat()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, asset_id, title, description, priority, approved_by, created_at),
        )
    return TicketCreatedResult(
        status="created",
        ticket_id=ticket_id,
        run_id=run_id,
        approved_by=approved_by,
        created_at=created_at,
        input_digest=hashlib.sha256(
            json.dumps([asset_id, title, description]).encode()
        ).hexdigest(),
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
