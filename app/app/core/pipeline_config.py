from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.core.image_generation import ImageGenerationProviderConfig
from app.core.model_config import LLMProviderConfig, resolve_llm_config


class PipelineDefaultsConfig(BaseModel):
    llm: LLMProviderConfig = Field(default_factory=LLMProviderConfig)
    image: ImageGenerationProviderConfig = Field(default_factory=ImageGenerationProviderConfig)


class PipelineAgentConfig(BaseModel):
    use_default_llm: bool = True
    llm: LLMProviderConfig = Field(default_factory=LLMProviderConfig)
    use_default_image: bool = True
    image: ImageGenerationProviderConfig = Field(default_factory=ImageGenerationProviderConfig)


class PipelineWorkbenchConfig(BaseModel):
    defaults: PipelineDefaultsConfig = Field(default_factory=PipelineDefaultsConfig)
    agents: dict[str, PipelineAgentConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_shape(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "defaults" in data or "agents" in data:
            return data
        agents: dict[str, dict[str, Any]] = {}
        if data.get("world_api"):
            agents["world_builder"] = {"use_default_llm": False, "llm": data["world_api"]}
            agents["script_decomposition"] = {"use_default_llm": False, "llm": data["world_api"]}
        if data.get("visual_prompt_api"):
            agents["visual_prompt_composer"] = {"use_default_llm": False, "llm": data["visual_prompt_api"]}
        if data.get("npc_api"):
            agents["npc_runtime"] = {"use_default_llm": False, "llm": data["npc_api"]}
        if data.get("image_api"):
            agents["visual_asset_generation"] = {"use_default_image": False, "image": data["image_api"]}
        return {"agents": agents}


class PipelineConfigStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or os.getenv("PIPELINE_CONFIG_PATH") or "data/pipeline_config.json")

    def load(self) -> PipelineWorkbenchConfig:
        if not self.path.exists():
            return PipelineWorkbenchConfig()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid pipeline config JSON: {self.path}") from exc
        return PipelineWorkbenchConfig.model_validate(data)

    def save(self, config: PipelineWorkbenchConfig) -> PipelineWorkbenchConfig:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(_storage_payload(config), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return config


def resolve_pipeline_llm_config(config: PipelineWorkbenchConfig, purpose: str) -> LLMProviderConfig:
    agent = config.agents.get(_agent_id_for_purpose(purpose), PipelineAgentConfig())
    default_config = resolve_llm_config(config.defaults.llm, purpose=purpose)
    if agent.use_default_llm:
        return default_config
    return resolve_llm_config(_merge_llm_config(default_config, agent.llm), purpose=purpose)


def resolve_pipeline_image_config(config: PipelineWorkbenchConfig, purpose: str = "visual_asset_generation") -> ImageGenerationProviderConfig:
    agent = config.agents.get(_agent_id_for_purpose(purpose), PipelineAgentConfig())
    if agent.use_default_image:
        return config.defaults.image
    return _merge_image_config(config.defaults.image, agent.image)


def _agent_id_for_purpose(purpose: str) -> str:
    normalized = purpose.strip().lower()
    if normalized in {"story", "story_authoring", "story_authoring_agent", "story_creator"}:
        return "story_authoring"
    if normalized in {"script", "script_decomposition", "script_decomposition_llm", "decomposition"}:
        return "script_decomposition"
    if normalized in {"world", "world_builder", "world_builder_llm"}:
        return "world_builder"
    if normalized in {"lorebook", "npc_lorebook", "npc_lorebook_creation", "npc_lorebook_creation_agent"}:
        return "npc_lorebook"
    if normalized in {"visual", "visual_prompt", "visual_prompt_llm", "prompt_composer", "visual_prompt_composer"}:
        return "visual_prompt_composer"
    if normalized in {"image", "images", "visual_asset_generation", "image_generation"}:
        return "visual_asset_generation"
    if normalized in {"npc", "npc_llm", "npc_runtime"}:
        return "npc_runtime"
    return normalized or "default"


def _merge_llm_config(base: LLMProviderConfig, override: LLMProviderConfig) -> LLMProviderConfig:
    return LLMProviderConfig(
        provider=override.provider or base.provider,
        api_key=override.api_key or base.api_key,
        api_key_env=override.api_key_env or base.api_key_env,
        base_url=override.base_url or base.base_url,
        base_url_env=override.base_url_env or base.base_url_env,
        model=override.model or base.model,
        model_env=override.model_env or base.model_env,
        temperature=override.temperature if override.temperature is not None else base.temperature,
        timeout=override.timeout if override.timeout is not None else base.timeout,
        max_retries=override.max_retries if override.max_retries is not None else base.max_retries,
        metadata={**(base.metadata or {}), **(override.metadata or {})},
    )


def _merge_image_config(base: ImageGenerationProviderConfig, override: ImageGenerationProviderConfig) -> ImageGenerationProviderConfig:
    return ImageGenerationProviderConfig(
        provider=override.provider or base.provider,
        api_base_url=override.api_base_url or base.api_base_url,
        model=override.model or base.model,
        size=override.size or base.size,
        steps=override.steps if override.steps is not None else base.steps,
        cfg_scale=override.cfg_scale if override.cfg_scale is not None else base.cfg_scale,
        seed=override.seed if override.seed is not None else base.seed,
        text_mode=override.text_mode if override.text_mode is not None else base.text_mode,
        response_format=override.response_format or base.response_format,
        api_key=override.api_key or base.api_key,
        api_key_env=override.api_key_env or base.api_key_env,
        api_key_file=override.api_key_file or base.api_key_file,
        retry_count=override.retry_count if override.retry_count is not None else base.retry_count,
        retryable_error_fragments=override.retryable_error_fragments or base.retryable_error_fragments,
        extra_body={**(base.extra_body or {}), **(override.extra_body or {})},
    )


def public_llm_config(config: LLMProviderConfig, include_secrets: bool = False) -> dict[str, str | bool | float | int | None]:
    return {
        "provider": config.provider,
        "api_key": config.api_key if include_secrets else "",
        "base_url": config.base_url,
        "model": config.model,
        "temperature": config.temperature,
        "timeout": config.timeout,
        "max_retries": config.max_retries,
        "has_api_key": bool(config.api_key or os.getenv(config.api_key_env or "")),
        "source": "pipeline_config_or_environment",
    }


def public_image_config(
    config: ImageGenerationProviderConfig,
    api_key: str = "",
    include_secrets: bool = False,
    source: str = "pipeline_config",
) -> dict[str, str | bool | float | int | None]:
    resolved_key = api_key or config.api_key or os.getenv(config.api_key_env or "")
    return {
        "provider": config.provider,
        "api_base_url": config.api_base_url,
        "model": config.model,
        "size": config.size,
        "steps": config.steps,
        "cfg_scale": config.cfg_scale,
        "seed": config.seed,
        "text_mode": config.text_mode,
        "api_key_env": config.api_key_env,
        "api_key": resolved_key if include_secrets else "",
        "has_api_key": bool(resolved_key),
        "retry_count": config.retry_count,
        "response_format": config.response_format,
        "source": source,
    }


def image_config_from_payload(data: dict[str, Any]) -> ImageGenerationProviderConfig:
    return ImageGenerationProviderConfig(
        provider=str(data.get("provider") or "stepfun"),
        api_base_url=str(data.get("api_base_url") or ""),
        model=str(data.get("model") or ""),
        size=str(data.get("size") or "1024x1024"),
        steps=data.get("steps"),
        cfg_scale=data.get("cfg_scale"),
        seed=data.get("seed"),
        text_mode=data.get("text_mode"),
        api_key=str(data.get("api_key") or ""),
        api_key_env=str(data.get("api_key_env") or "STEPFUN_API_KEY"),
        retry_count=int(data.get("retry_count") if data.get("retry_count") is not None else 3),
        response_format=str(data.get("response_format") or "b64_json"),
    )


def _storage_payload(config: PipelineWorkbenchConfig) -> dict[str, Any]:
    agents: dict[str, dict[str, Any]] = {}
    for agent_id, agent in config.agents.items():
        item: dict[str, Any] = {}
        if agent_id == "visual_asset_generation":
            item["use_default_image"] = agent.use_default_image
            if not agent.use_default_image:
                item["image"] = agent.image.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
            if not agent.use_default_llm:
                item["use_default_llm"] = False
                item["llm"] = agent.llm.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        else:
            item["use_default_llm"] = agent.use_default_llm
            if not agent.use_default_llm:
                item["llm"] = agent.llm.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
            if not agent.use_default_image:
                item["use_default_image"] = False
                item["image"] = agent.image.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        agents[agent_id] = item
    return {
        "defaults": {
            "llm": config.defaults.llm.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
            "image": config.defaults.image.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
        },
        "agents": agents,
    }
