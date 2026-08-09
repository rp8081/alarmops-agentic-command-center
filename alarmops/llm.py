from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from alarmops.models import GroundedAnswer
from alarmops.settings import Settings

SYSTEM_PROMPT = """You are AlarmOps, an industrial alarm decision-support assistant.
Use only the supplied tool evidence and procedure citations. Never invent identifiers, measurements,
causes, or completed actions. Separate correlation from causation. If evidence is missing, say so.
Never instruct the user to bypass an interlock or claim to have controlled plant equipment.
Return valid JSON with: executive_summary, findings, likely_contributors, recommended_actions,
caveats, citation_ids, confidence. confidence must be low, medium, or high."""


class AnswerGenerator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def runtime_info(self, deterministic: bool) -> dict[str, Any]:
        key_configured = bool(self.settings.llm_api_key.get_secret_value())
        reviewer_mode = deterministic or self.settings.llm_mode == "deterministic" or not key_configured
        provider = "groq" if "api.groq.com" in self.settings.llm_api_base else "openai-compatible"
        return {
            "mode": "deterministic" if reviewer_mode else "llm",
            "provider": None if reviewer_mode else provider,
            "model": "built-in-reviewer" if reviewer_mode else self.settings.llm_model,
            "api_base": None if reviewer_mode else self.settings.llm_api_base,
            "key_configured": key_configured,
        }

    async def generate(self, query: str, context: dict[str, Any], deterministic: bool) -> GroundedAnswer:
        if self.runtime_info(deterministic)["mode"] == "deterministic":
            return self._deterministic(context)
        client = AsyncOpenAI(
            api_key=self.settings.llm_api_key.get_secret_value(),
            base_url=self.settings.llm_api_base,
            timeout=45,
            max_retries=2,
        )
        completion = await client.chat.completions.create(
            model=self.settings.llm_model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"query": query, "evidence": context}, default=str)},
            ],
        )
        payload = json.loads(completion.choices[0].message.content or "{}")
        return GroundedAnswer.model_validate(payload)

    @staticmethod
    def _deterministic(context: dict[str, Any]) -> GroundedAnswer:
        summary = context.get("alarm", {}).get("summary", {})
        maintenance = context.get("maintenance", {}).get("history", {}).get("records", [])
        citations = context.get("citations", [])
        total = summary.get("total_alarms", 0)
        critical = summary.get("critical_count", 0)
        findings = [f"The selected window contains {total} matching alarms, including {critical} critical events."]
        if maintenance:
            findings.append(f"Maintenance history contains {len(maintenance)} relevant completed records.")
        return GroundedAnswer(
            executive_summary="Recurring high-severity alarms warrant prompt verification and a documented engineering review; the evidence does not by itself prove a single root cause.",
            findings=findings,
            likely_contributors=[
                "A suction-path restriction is plausible because a prior strainer finding exists.",
                "Instrument impulse-line restriction is also plausible from maintenance history.",
            ],
            recommended_actions=[
                "Validate discharge pressure against the redundant indicator.",
                "Inspect suction strainer differential pressure, valve position, and transmitter impulse line.",
                "Have alarm engineering review the setpoint, deadband, and recurrence pattern.",
            ],
            caveats=["Tool evidence is decision support, not proof of causation or permission to operate equipment."],
            citation_ids=[item["citation_id"] for item in citations],
            confidence="low" if not citations else "high" if summary else "medium",
        )
