from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api import shared
from app.core.image_generation import resolve_image_api_key
from app.core.model_config import LLMProviderConfig
from app.core.pipeline_config import (
    PipelineWorkbenchConfig,
    public_image_config,
    public_llm_config,
    resolve_pipeline_image_config,
    resolve_pipeline_llm_config,
)

router = APIRouter(tags=["orchestration"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config/effective")
async def effective_config(include_secrets: bool = False) -> dict[str, dict]:
    config = shared.load_pipeline_config()
    image_config = resolve_pipeline_image_config(config, "visual_asset_generation")
    image_api_key = resolve_image_api_key(image_config)
    script_api = public_llm_config(
        resolve_pipeline_llm_config(config, "script_decomposition"),
        include_secrets=include_secrets,
    )
    world_api = public_llm_config(
        resolve_pipeline_llm_config(config, "world_builder"),
        include_secrets=include_secrets,
    )
    visual_prompt_api = public_llm_config(
        resolve_pipeline_llm_config(config, "visual_prompt"),
        include_secrets=include_secrets,
    )
    npc_api = public_llm_config(
        resolve_pipeline_llm_config(config, "npc"),
        include_secrets=include_secrets,
    )
    image_api = public_image_config(
        image_config,
        api_key=image_api_key,
        include_secrets=include_secrets,
    )
    agent_payload = shared.public_agent_config(config, include_secrets=include_secrets)
    return {
        "defaults": {
            "llm": public_llm_config(config.defaults.llm, include_secrets=include_secrets),
            "image": public_image_config(config.defaults.image, include_secrets=include_secrets, source="pipeline_default"),
        },
        "agents": agent_payload,
        "script_decomposition_api": script_api,
        "world_api": world_api,
        "visual_prompt_api": visual_prompt_api,
        "npc_api": npc_api,
        "image_api": image_api,
    }


@router.put("/config")
async def save_pipeline_config(payload: dict) -> dict[str, dict]:
    try:
        config = shared.pipeline_config_from_payload(payload)
        shared.active_pipeline_config_store().save(config)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await effective_config(include_secrets=True)


PipelineConfigStore = shared.PipelineConfigStore
pipeline_config_store = shared.pipeline_config_store
PipelineWorkbenchConfig = PipelineWorkbenchConfig
LLMProviderConfig = LLMProviderConfig
