from __future__ import annotations

import operator
import re
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from alarmops.gateway import McpGateway
from alarmops.llm import AnswerGenerator
from alarmops.models import Plan
from alarmops.rag import HybridRagIndex
from alarmops.telemetry import SpanTimer, TelemetryStore
from mcp_servers.maintenance_server import make_approval_reference


class GraphState(TypedDict, total=False):
    run_id: str
    conversation_id: str
    conversation_context: dict[str, Any]
    query: str
    failure_mode: str
    deterministic: bool
    tools: list[dict[str, Any]]
    plan: dict[str, Any]
    asset: dict[str, Any]
    alarm: dict[str, Any]
    maintenance: dict[str, Any]
    citations: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    errors: Annotated[list[str], operator.add]
    answer: dict[str, Any]
    generation: dict[str, Any]
    verification: dict[str, Any]
    retry_count: int
    ticket_draft: dict[str, Any]
    ticket: dict[str, Any]
    approval: dict[str, Any]
    status: str


class AlarmOpsGraph:
    def __init__(
        self,
        gateway: McpGateway,
        rag: HybridRagIndex,
        generator: AnswerGenerator,
        telemetry: TelemetryStore,
    ) -> None:
        self.gateway = gateway
        self.rag = rag
        self.generator = generator
        self.telemetry = telemetry
        self.compiled = self._build()

    async def _tool_call(
        self, state: GraphState, server: str, tool: str, arguments: dict[str, Any]
    ) -> Any:
        timer = SpanTimer()
        safe_arguments = {
            key: "[redacted]" if key in {"approval_reference", "description"} else value
            for key, value in arguments.items()
        }
        try:
            result = await self.gateway.call(server, tool, arguments)
            self.telemetry.span(
                state["run_id"],
                f"mcp:{server}.{tool}",
                "ok",
                timer.milliseconds,
                {"arguments": safe_arguments, "outcome": "structured_result"},
            )
            return result
        except Exception as error:
            self.telemetry.span(
                state["run_id"],
                f"mcp:{server}.{tool}",
                "error",
                timer.milliseconds,
                {"arguments": safe_arguments, "error": str(error)},
            )
            raise

    async def _observed(self, state: GraphState, node: str, operation: Any) -> dict[str, Any]:
        timer = SpanTimer()
        try:
            result: dict[str, Any] = await operation()
            self.telemetry.span(
                state["run_id"],
                node,
                "ok",
                timer.milliseconds,
                {
                    "output_keys": sorted(result),
                    "error_count": len(result.get("errors", [])),
                    "citation_count": len(result.get("citations", [])),
                },
            )
            return result
        except Exception as error:
            self.telemetry.span(state["run_id"], node, "error", timer.milliseconds, {"error": str(error)})
            return {"errors": [f"{node}: {error}"]}

    async def intake(self, state: GraphState) -> dict[str, Any]:
        query = re.sub(r"\s+", " ", state["query"]).strip()
        return {"query": query, "status": "running", "retry_count": 0, "errors": []}

    async def discover_tools(self, state: GraphState) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            tools = await self.gateway.discover()
            return {"tools": [tool.model_dump() for tool in tools]}

        return await self._observed(state, "discover_tools", operation)

    async def plan(self, state: GraphState) -> dict[str, Any]:
        query = state["query"].casefold()
        asset_query = state["query"]
        for needle, value in {
            "pump 101": "Boiler Feed Pump 101",
            "bfp-101": "Boiler Feed Pump 101",
            "pump 102": "Boiler Feed Pump 102",
            "bfp-102": "Boiler Feed Pump 102",
            "compressor 201": "Process Air Compressor 201",
            "compressor 202": "Process Air Compressor 202",
            "motor 501": "Cooling Water Motor 501",
        }.items():
            if needle in query:
                asset_query = value
                break
        previous_asset = state.get("conversation_context", {}).get("asset", {})
        if previous_asset and (
            any(phrase in query for phrase in ("that asset", "same asset", "this pump"))
            or re.search(r"\bit\b", query)
        ):
            asset_query = previous_asset.get("asset_name", asset_query)
        days_match = re.search(r"(\d{1,3})\s*days?", query)
        days = int(days_match.group(1)) if days_match else 90
        execute_ticket = any(
            phrase in query
            for phrase in (
                "create ticket",
                "open ticket",
                "raise ticket",
                "create an escalation",
                "open an escalation",
            )
        )
        plan = Plan(
            intent="investigate recurring alarms and recommend grounded actions",
            asset_query=asset_query,
            lookback_days=max(1, min(days, 730)),
            severities=["high", "critical"],
            draft_ticket=execute_ticket or "draft" in query or "escalat" in query,
            execute_ticket=execute_ticket,
        )
        return {"plan": plan.model_dump()}

    async def resolve_asset(self, state: GraphState) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            result = await self._tool_call(
                state,
                "alarm",
                "search_assets",
                {"query": state["plan"]["asset_query"], "limit": 5},
            )
            matches = result.get("results", [])
            if not matches:
                raise ValueError("No authoritative asset matched the query")
            metadata = await self._tool_call(
                state, "alarm", "get_asset_metadata", {"asset_id": matches[0]["asset_id"]}
            )
            return {"asset": metadata}

        return await self._observed(state, "resolve_asset", operation)

    async def alarm_worker(self, state: GraphState) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            if state.get("failure_mode") == "alarm_timeout":
                raise TimeoutError("injected alarm MCP timeout")
            asset_id = state["asset"]["asset_id"]
            days = state["plan"]["lookback_days"]
            detail = await self._tool_call(
                state, "alarm", "get_alarms", {"asset_id": asset_id, "lookback_days": days}
            )
            summary = await self._tool_call(
                state,
                "alarm",
                "summarize_alarms",
                {
                    "asset_id": asset_id,
                    "lookback_days": days,
                    "severities": state["plan"]["severities"],
                },
            )
            candidates = await self._tool_call(
                state,
                "alarm",
                "find_rationalization_candidates",
                {"asset_id": asset_id, "lookback_days": days, "recurrence_threshold": 5},
            )
            return {"alarm": {"detail": detail, "summary": summary, "rationalization": candidates}}

        return await self._observed(state, "alarm_worker", operation)

    async def maintenance_worker(self, state: GraphState) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            if state.get("failure_mode") == "maintenance_timeout":
                raise TimeoutError("injected maintenance MCP timeout")
            asset_id = state["asset"]["asset_id"]
            history = await self._tool_call(
                state,
                "maintenance",
                "get_maintenance_history",
                {"asset_id": asset_id, "limit": 10},
            )
            work_orders = await self._tool_call(
                state,
                "maintenance",
                "get_open_work_orders",
                {"asset_id": asset_id},
            )
            return {"maintenance": {"history": history, "work_orders": work_orders}}

        return await self._observed(state, "maintenance_worker", operation)

    async def rag_worker(self, state: GraphState) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            if state.get("failure_mode") == "rag_no_results":
                return {"citations": []}
            query = f"{state['query']} {state['plan']['asset_query']} operator response maintenance"
            return {"citations": [item.model_dump() for item in self.rag.search(query)]}

        return await self._observed(state, "rag_worker", operation)

    async def merge(self, state: GraphState) -> dict[str, Any]:
        evidence: list[dict[str, Any]] = []
        asset_id = state.get("asset", {}).get("asset_id", "unresolved")
        if state.get("alarm"):
            evidence.append({"id": "EV-ALARM-1", "source": "Alarm Intelligence MCP", "asset_id": asset_id, "claim": "Alarm chronology and aggregates retrieved", "data": state["alarm"]})
        if state.get("maintenance"):
            evidence.append({"id": "EV-MAINT-1", "source": "Maintenance Operations MCP", "asset_id": asset_id, "claim": "Maintenance history and open work retrieved", "data": state["maintenance"]})
        evidence.extend({"id": item["citation_id"], "source": "Trusted RAG", "claim": item["section"], "data": item} for item in state.get("citations", []))
        return {"evidence": evidence}

    async def synthesize(self, state: GraphState) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            context = {
                "asset": state.get("asset"),
                "alarm": state.get("alarm"),
                "maintenance": state.get("maintenance"),
                "citations": state.get("citations", []),
                "errors": state.get("errors", []),
                "previous_turn": state.get("conversation_context", {}),
            }
            deterministic = state.get("deterministic", False)
            generation = self.generator.runtime_info(deterministic)
            answer = await self.generator.generate(state["query"], context, deterministic)
            result: dict[str, Any] = {
                "answer": answer.model_dump(),
                "generation": generation,
            }
            if state["plan"].get("draft_ticket"):
                result["ticket_draft"] = await self._tool_call(
                    state,
                    "maintenance",
                    "draft_escalation_ticket",
                    {"asset_id": state["asset"]["asset_id"], "title": "Recurring high-severity alarm investigation", "description": answer.executive_summary, "priority": "P2"},
                )
            return result

        return await self._observed(state, "synthesize", operation)

    async def verify(self, state: GraphState) -> dict[str, Any]:
        checks = {
            "has_alarm_evidence": bool(state.get("alarm")),
            "has_citations": bool(state.get("citations")),
            "citations_resolve": set(state.get("answer", {}).get("citation_ids", [])) <= {item["citation_id"] for item in state.get("citations", [])},
            "answer_has_caveat": bool(state.get("answer", {}).get("caveats")),
        }
        score = sum(checks.values()) / len(checks)
        return {"verification": {"checks": checks, "score": score, "passed": score == 1.0}}

    def route_after_verify(self, state: GraphState) -> Literal["retry", "approval", "finalize"]:
        if (
            not state["verification"]["passed"]
            and state.get("retry_count", 0) < 1
            and state.get("failure_mode", "none") == "none"
        ):
            return "retry"
        if state["plan"].get("execute_ticket") and state.get("ticket_draft"):
            return "approval"
        return "finalize"

    async def rag_rewrite(self, state: GraphState) -> dict[str, Any]:
        expanded = f"{state['plan']['asset_query']} low discharge pressure alarm rationalization verification maintenance operator"
        return {"citations": [item.model_dump() for item in self.rag.search(expanded, limit=6)], "retry_count": state.get("retry_count", 0) + 1}

    async def approval(self, state: GraphState) -> dict[str, Any]:
        decision = interrupt({"type": "human_approval", "message": "Review the escalation draft before the write-capable MCP tool is called.", "draft": state["ticket_draft"], "run_id": state["run_id"]})
        if decision.get("decision") != "approve":
            return {"approval": decision, "status": "completed_without_ticket"}
        approved_by = decision.get("approved_by", "reviewer")
        draft = state["ticket_draft"]["ticket"]
        reference = make_approval_reference(state["run_id"], approved_by)
        ticket = await self._tool_call(
            state,
            "maintenance",
            "create_escalation_ticket",
            {**draft, "approval_reference": reference},
        )
        return {"approval": decision, "ticket": ticket}

    async def finalize(self, state: GraphState) -> dict[str, Any]:
        degraded = (
            bool(state.get("errors"))
            or not state.get("verification", {}).get("passed", False)
            or state.get("failure_mode", "none") != "none"
        )
        return {"status": "degraded" if degraded else "completed"}

    def _build(self) -> Any:
        graph = StateGraph(GraphState)
        for name in ("intake", "discover_tools", "plan", "resolve_asset", "alarm_worker", "maintenance_worker", "rag_worker", "merge", "synthesize", "verify", "rag_rewrite", "approval", "finalize"):
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "intake")
        graph.add_edge("intake", "discover_tools")
        graph.add_edge("discover_tools", "plan")
        graph.add_edge("plan", "resolve_asset")
        for worker in ("alarm_worker", "maintenance_worker", "rag_worker"):
            graph.add_edge("resolve_asset", worker)
        graph.add_edge(["alarm_worker", "maintenance_worker", "rag_worker"], "merge")
        graph.add_edge("merge", "synthesize")
        graph.add_edge("synthesize", "verify")
        graph.add_conditional_edges("verify", self.route_after_verify, {"retry": "rag_rewrite", "approval": "approval", "finalize": "finalize"})
        graph.add_edge("rag_rewrite", "synthesize")
        graph.add_edge("approval", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=MemorySaver())

    @staticmethod
    def topology() -> dict[str, Any]:
        return {
            "architecture": "Plan-and-Execute Orchestrator-Worker with Evaluator-Optimizer and HITL",
            "nodes": ["intake", "discover_tools", "plan", "resolve_asset", "alarm_worker", "maintenance_worker", "rag_worker", "merge", "synthesize", "verify", "rag_rewrite", "approval", "finalize"],
            "parallel_group": ["alarm_worker", "maintenance_worker", "rag_worker"],
            "write_boundary": "approval -> create_escalation_ticket",
        }


__all__ = ["AlarmOpsGraph", "GraphState"]
