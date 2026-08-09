from __future__ import annotations

import logging
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from alarmops.settings import Settings

logger = logging.getLogger(__name__)


class AlarmApiClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.alarm_api_base_url.rstrip("/")
        self.token = settings.alarm_api_token.get_secret_value()

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.1, max=1),
        reraise=True,
    )
    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        trace_id = kwargs.pop("trace_id", str(uuid4()))
        headers = {
            "Authorization": f"Bearer {self.token}",
            "trace_id": trace_id,
            "x-client-id": "alarm-intelligence-mcp",
            **kwargs.pop("headers", {}),
        }
        started = perf_counter()
        async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
            response = await client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        response.raise_for_status()
        payload = response.json()
        duration_ms = round((perf_counter() - started) * 1000, 2)
        logger.info(
            "alarm_api_call",
            extra={
                "trace_id": trace_id,
                "method": method,
                "path": path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        if isinstance(payload, dict):
            payload["_meta"] = {
                "trace_id": response.headers.get("x-trace-id", trace_id),
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            }
        return payload
