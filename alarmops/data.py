from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from alarmops.models import Alarm, Asset, Severity

ASSETS = [
    Asset(asset_id="AST-BFP-101", asset_name="Boiler Feed Pump 101", site="NorthPlant", unit="Unit 1", asset_type="centrifugal_pump", criticality="A", tags=["boiler-feed", "high-pressure"]),
    Asset(asset_id="AST-BFP-102", asset_name="Boiler Feed Pump 102", site="NorthPlant", unit="Unit 1", asset_type="centrifugal_pump", criticality="A", tags=["boiler-feed", "standby"]),
    Asset(asset_id="AST-CMP-201", asset_name="Process Air Compressor 201", site="SouthPlant", unit="Unit 2", asset_type="compressor", criticality="A", tags=["compressor"]),
    Asset(asset_id="AST-CMP-202", asset_name="Process Air Compressor 202", site="SouthPlant", unit="Unit 2", asset_type="compressor", criticality="B", tags=["compressor"]),
    Asset(asset_id="AST-MTR-501", asset_name="Cooling Water Motor 501", site="EastRefinery", unit="Unit 5", asset_type="motor", criticality="B", tags=["motor"]),
]


def alarms(now: datetime | None = None) -> list[Alarm]:
    anchor = now or datetime.now(UTC)
    rows: list[Alarm] = []
    for index, days in enumerate([3, 9, 16, 27, 41, 58, 76], 1):
        rows.append(Alarm(alarm_id=f"ALM-BFP101-{index:03d}", asset_id="AST-BFP-101", asset_name="Boiler Feed Pump 101", site="NorthPlant", unit="Unit 1", alarm_name="Discharge Pressure Low", alarm_type="process", severity="critical" if index in {2, 6} else "high", status="active" if index == 1 else "cleared", start_time=anchor - timedelta(days=days), acknowledgement_delay_seconds=80 + index * 19, message="Discharge pressure below 8.2 MPa"))
    for index, days in enumerate([5, 32, 63], 1):
        rows.append(Alarm(alarm_id=f"ALM-BFP101-VIB-{index:03d}", asset_id="AST-BFP-101", asset_name="Boiler Feed Pump 101", site="NorthPlant", unit="Unit 1", alarm_name="Drive-end Vibration High", alarm_type="device", severity="high", status="acknowledged" if index == 1 else "cleared", start_time=anchor - timedelta(days=days), acknowledgement_delay_seconds=140 + index * 20, message="Vibration above 6.8 mm/s RMS"))
    extras: list[tuple[str, str, str, str, str, Severity]] = [
        ("AST-BFP-102", "Boiler Feed Pump 102", "NorthPlant", "Unit 1", "Seal Leak", "high"),
        ("AST-CMP-201", "Process Air Compressor 201", "SouthPlant", "Unit 2", "Stage 2 Temperature High", "critical"),
        ("AST-CMP-202", "Process Air Compressor 202", "SouthPlant", "Unit 2", "Suction Pressure Low", "medium"),
        ("AST-MTR-501", "Cooling Water Motor 501", "EastRefinery", "Unit 5", "Winding Temperature High", "critical"),
    ]
    for index, item in enumerate(extras, 1):
        asset_id, asset_name, site, unit, name, severity = item
        rows.append(Alarm(alarm_id=f"ALM-OTHER-{index:03d}", asset_id=asset_id, asset_name=asset_name, site=site, unit=unit, alarm_name=name, alarm_type="device", severity=severity, status="active", start_time=anchor - timedelta(days=index), acknowledgement_delay_seconds=40 + index * 30, message=f"{name} threshold exceeded"))
    return rows


MAINTENANCE: dict[str, list[dict[str, Any]]] = {
    "AST-BFP-101": [
        {"work_order_id": "WO-8421", "completed_at": "2026-06-18T11:30:00Z", "finding": "Suction strainer differential pressure was 34% above baseline.", "action": "Strainer cleaned."},
        {"work_order_id": "WO-8177", "completed_at": "2026-04-07T09:10:00Z", "finding": "Drive-end vibration increased after alignment work.", "action": "Laser alignment corrected."},
        {"work_order_id": "WO-7902", "completed_at": "2026-01-14T16:45:00Z", "finding": "Pressure transmitter impulse line intermittently restricted.", "action": "Line flushed and transmitter recalibrated."},
    ]
}

WORK_ORDERS = [{"work_order_id": "WO-8613", "asset_id": "AST-BFP-101", "status": "planned", "priority": "P2", "title": "Inspect suction path and pressure transmitter"}]
