from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration. Only ``LLM_API_KEY`` is required for real LLM runs."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    alarm_api_base_url: str = "http://localhost:8100"
    alarm_api_token: SecretStr = SecretStr("demo-token")
    alarm_mcp_url: str = "http://localhost:9001/mcp"
    maintenance_mcp_url: str = "http://localhost:9002/mcp"
    approval_secret: SecretStr = SecretStr("demo-approval-secret")

    llm_mode: str = Field(default="groq", pattern="^(groq|deterministic)$")
    llm_api_base: str = "https://api.groq.com/openai/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "openai/gpt-oss-120b"
    llm_reasoning_effort: str = Field(default="low", pattern="^(low|medium|high)$")

    document_path: Path = Path("rag/documents")
    rag_index_path: Path = Path("rag/index.json")
    rag_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    rag_embedding_device: str = "cpu"
    rag_semantic_weight: float = Field(default=0.55, ge=0.0, le=1.0)
    rag_semantic_threshold: float = Field(default=0.28, ge=-1.0, le=1.0)
    telemetry_db_path: Path = Path("data/telemetry.db")
    ticket_db_path: Path = Path("data/tickets.db")
    demo_access_code: SecretStr = SecretStr("alarmops-demo")
    require_access_code: bool = False
    requests_per_minute: int = 30
    enable_demo_failures: bool = True
    cors_origins: str = "http://localhost:8000,http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [part.strip() for part in self.cors_origins.split(",") if part.strip()]

    def safe_summary(self) -> dict[str, object]:
        return {
            "llm_mode": self.llm_mode,
            "llm_model": self.llm_model,
            "llm_api_base": self.llm_api_base,
            "llm_key_configured": bool(self.llm_api_key.get_secret_value()),
            "alarm_mcp_url": self.alarm_mcp_url,
            "maintenance_mcp_url": self.maintenance_mcp_url,
            "rag_embedding_model": self.rag_embedding_model,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
