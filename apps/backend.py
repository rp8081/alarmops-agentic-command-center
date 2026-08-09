from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from langgraph.types import Command

from alarmops.gateway import McpGateway
from alarmops.graph import AlarmOpsGraph
from alarmops.llm import AnswerGenerator
from alarmops.models import ApprovalDecision, ChatRequest
from alarmops.rag import HybridRagIndex
from alarmops.settings import Settings, get_settings
from alarmops.telemetry import TelemetryStore

ROOT = Path(__file__).resolve().parents[1]
request_windows: dict[str, deque[datetime]] = defaultdict(deque)


def _ndjson(payload: Any) -> bytes:
    return (json.dumps(payload, default=str) + "\n").encode()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    document_path = settings.document_path if settings.document_path.is_absolute() else ROOT / settings.document_path
    index = HybridRagIndex.build(
        document_path,
        embedding_model_name=settings.rag_embedding_model,
        embedding_device=settings.rag_embedding_device,
        semantic_weight=settings.rag_semantic_weight,
        semantic_threshold=settings.rag_semantic_threshold,
    )
    index_path = settings.rag_index_path if settings.rag_index_path.is_absolute() else ROOT / settings.rag_index_path
    index.save(index_path)
    telemetry_path = settings.telemetry_db_path if settings.telemetry_db_path.is_absolute() else ROOT / settings.telemetry_db_path
    telemetry = TelemetryStore(telemetry_path)
    app.state.settings = settings
    app.state.rag = index
    app.state.telemetry = telemetry
    app.state.graph = AlarmOpsGraph(McpGateway(settings), index, AnswerGenerator(settings), telemetry)
    app.state.conversations = {}
    yield


app = FastAPI(title="AlarmOps Agentic Command Center", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origin_list, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def protect_demo(request: Request, call_next: Any) -> Any:
    settings: Settings = get_settings()
    if request.url.path.startswith("/api/"):
        if settings.require_access_code and request.headers.get("x-demo-code") != settings.demo_access_code.get_secret_value():
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "valid X-Demo-Code required"}, status_code=401)
        key = request.client.host if request.client else "unknown"
        now = datetime.now(UTC)
        while request_windows[key] and request_windows[key][0] < now - timedelta(minutes=1):
            request_windows[key].popleft()
        if len(request_windows[key]) >= settings.requests_per_minute:
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "demo rate limit exceeded"}, status_code=429)
        request_windows[key].append(now)
    return await call_next(request)


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "alarmops-backend", "rag": app.state.rag.diagnostics()}


@app.get("/api/config")
async def config() -> dict[str, Any]:
    return app.state.settings.safe_summary()


@app.get("/api/graph")
async def graph() -> dict[str, Any]:
    return AlarmOpsGraph.topology()


@app.get("/api/tools")
async def tools() -> list[dict[str, Any]]:
    return [item.model_dump() for item in await app.state.graph.gateway.discover()]


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    if request.failure_mode != "none" and not app.state.settings.enable_demo_failures:
        raise HTTPException(403, "failure injection is disabled")
    run_id = uuid4().hex
    conversation_id = request.conversation_id or uuid4().hex
    telemetry: TelemetryStore = app.state.telemetry
    telemetry.start(run_id, request.query)
    config = {"configurable": {"thread_id": run_id}}
    initial = {
        "run_id": run_id,
        **request.model_dump(),
        "conversation_id": conversation_id,
        "conversation_context": app.state.conversations.get(conversation_id, {}),
    }

    async def events() -> AsyncIterator[bytes]:
        yield _ndjson(
            {"type": "run_started", "run_id": run_id, "conversation_id": conversation_id}
        )
        try:
            async for update in app.state.graph.compiled.astream(initial, config, stream_mode="updates"):
                for node, delta in update.items():
                    if node == "__interrupt__":
                        payload = delta[0].value if isinstance(delta, tuple | list) else delta
                        yield _ndjson({"type": "approval_required", "run_id": run_id, "payload": payload})
                    else:
                        yield _ndjson({"type": "node", "run_id": run_id, "node": node, "data": delta})
            state = app.state.graph.compiled.get_state(config).values
            if state.get("status") in {"completed", "degraded", "completed_without_ticket"}:
                telemetry.finish(run_id, state["status"], state, state.get("answer", {}).get("confidence"))
                app.state.conversations[conversation_id] = {
                    "asset": state.get("asset", {}),
                    "answer": state.get("answer", {}),
                }
                yield _ndjson({"type": "final", "run_id": run_id, "data": state})
        except Exception as error:
            telemetry.finish(run_id, "failed", {"error": str(error)}, "low")
            yield _ndjson({"type": "error", "run_id": run_id, "error": str(error)})

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.post("/api/runs/{run_id}/resume")
async def resume(run_id: str, decision: ApprovalDecision) -> dict[str, Any]:
    config = {"configurable": {"thread_id": run_id}}
    if not app.state.graph.compiled.get_state(config).values:
        raise HTTPException(404, "run not found")
    app.state.telemetry.approval(run_id, decision.decision, decision.approved_by)
    state = await app.state.graph.compiled.ainvoke(Command(resume=decision.model_dump()), config)
    app.state.telemetry.finish(run_id, state["status"], state, state.get("answer", {}).get("confidence"))
    if state.get("conversation_id"):
        app.state.conversations[state["conversation_id"]] = {
            "asset": state.get("asset", {}),
            "answer": state.get("answer", {}),
        }
    return state


@app.get("/api/runs")
async def runs() -> list[dict[str, Any]]:
    return app.state.telemetry.runs()


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str) -> dict[str, Any]:
    result = app.state.telemetry.run(run_id)
    if not result:
        raise HTTPException(404, "run not found")
    return result


@app.get("/api/metrics")
async def metrics() -> dict[str, Any]:
    return app.state.telemetry.metrics()


@app.get("/api/rag/search")
async def rag_search(query: str, limit: int = 4) -> dict[str, Any]:
    return {"diagnostics": app.state.rag.diagnostics(), "results": [item.model_dump() for item in app.state.rag.search(query, limit)]}


@app.get("/api/evaluations")
async def evaluations() -> Any:
    path = ROOT / "reports" / "evaluation_summary.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "not_run"}
