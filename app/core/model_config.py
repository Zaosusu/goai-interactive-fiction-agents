from __future__ import annotations

import os

from pydantic import BaseModel, Field


class LLMProviderConfig(BaseModel):
    provider: str = "openai_compatible"
    api_key: str = ""
    api_key_env: str = ""
    base_url: str = ""
    base_url_env: str = ""
    model: str = ""
    model_env: str = ""
    temperature: float = 0.75
    timeout: float | None = None
    max_retries: int | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


def resolve_llm_config(config: LLMProviderConfig | None, purpose: str) -> LLMProviderConfig:
    prefix = _purpose_prefix(purpose)
    source = config or LLMProviderConfig()
    normalized_purpose = purpose.strip().lower()
    return LLMProviderConfig(
        provider=source.provider or _env(prefix, "PROVIDER") or os.getenv("LLM_PROVIDER") or "openai_compatible",
        api_key=source.api_key or _env(prefix, "API_KEY") or os.getenv("LLM_API_KEY") or "",
        api_key_env=source.api_key_env,
        base_url=source.base_url or _env(prefix, "BASE_URL") or os.getenv("LLM_BASE_URL") or "",
        base_url_env=source.base_url_env,
        model=source.model or _env(prefix, "MODEL") or os.getenv("LLM_MODEL") or "qwen3.5-flash",
        model_env=source.model_env,
        temperature=source.temperature,
        timeout=source.timeout if source.timeout is not None else _purpose_timeout(normalized_purpose),
        max_retries=source.max_retries if source.max_retries is not None else _purpose_max_retries(normalized_purpose),
        metadata={**source.metadata, "purpose": purpose},
    )


def _purpose_prefix(purpose: str) -> str:
    normalized = purpose.strip().upper().replace("-", "_")
    if normalized in {"NPC", "NPC_LLM"}:
        return "NPC_LLM"
    if normalized in {"WORLD", "WORLD_BUILDER", "WORLD_BUILDER_LLM"}:
        return "WORLD_BUILDER_LLM"
    if normalized in {"VISUAL_PROMPT", "VISUAL_PROMPT_LLM", "PROMPT_COMPOSER"}:
        return "VISUAL_PROMPT_LLM"
    return normalized or "LLM"


def _env(prefix: str, suffix: str) -> str:
    return os.getenv(f"{prefix}_{suffix}") or ""


def _purpose_timeout(purpose: str) -> float:
    if purpose in {"world", "world_builder", "world_builder_llm"}:
        return float(os.getenv("WORLD_BUILDER_LLM_TIMEOUT") or os.getenv("LLM_TIMEOUT") or 900)
    if purpose in {"visual_prompt", "visual_prompt_llm", "prompt_composer"}:
        return float(os.getenv("VISUAL_PROMPT_LLM_TIMEOUT") or os.getenv("LLM_TIMEOUT") or 120)
    if purpose in {"npc", "npc_llm"}:
        return float(os.getenv("NPC_LLM_TIMEOUT") or os.getenv("LLM_TIMEOUT") or 90)
    return float(os.getenv("LLM_TIMEOUT") or 120)


def _purpose_max_retries(purpose: str) -> int:
    if purpose in {"world", "world_builder", "world_builder_llm"}:
        return int(os.getenv("WORLD_BUILDER_LLM_MAX_RETRIES") or os.getenv("LLM_MAX_RETRIES") or 1)
    return int(os.getenv("LLM_MAX_RETRIES") or 0)
