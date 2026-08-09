from __future__ import annotations

import json
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from alarmops.models import ToolDescriptor
from alarmops.settings import Settings


class McpGateway:
    def __init__(self, settings: Settings) -> None:
        self.servers = {
            "alarm": settings.alarm_mcp_url,
            "maintenance": settings.maintenance_mcp_url,
        }

    @staticmethod
    def _http_client(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout or httpx.Timeout(30),
            auth=auth,
            follow_redirects=True,
            trust_env=False,
        )

    async def discover(self) -> list[ToolDescriptor]:
        descriptors: list[ToolDescriptor] = []
        for server, url in self.servers.items():
            async with streamablehttp_client(
                url, httpx_client_factory=self._http_client
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for tool in (await session.list_tools()).tools:
                        descriptors.append(
                            ToolDescriptor(
                                server=server,
                                name=tool.name,
                                description=tool.description or "",
                                input_schema=tool.inputSchema,
                                write_operation=tool.name == "create_escalation_ticket",
                            )
                        )
        return descriptors

    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> Any:
        url = self.servers[server]
        async with streamablehttp_client(
            url, httpx_client_factory=self._http_client
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
        if result.isError:
            message = " ".join(getattr(block, "text", "") for block in result.content)
            raise RuntimeError(message or f"MCP tool {server}.{tool} failed")
        if result.structuredContent:
            return result.structuredContent.get("result", result.structuredContent)
        texts = [getattr(block, "text", "") for block in result.content]
        joined = "".join(texts)
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            return {"text": joined}
