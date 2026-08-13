from __future__ import annotations

from fastapi import APIRouter

from app.agents.creator_assistant.routes import create_router as create_creator_router
from app.agents.story_authoring.routes import create_router as create_story_authoring_router
from app.api import shared
from app.client.routes import router as client_router
from app.content.routes import router as content_router
from app.core.model_config import LLMProviderConfig
from app.core.pipeline_config import PipelineConfigStore
from app.agents.npc_lorebook import NpcLorebookCreationAgent
from app.orchestration.routes import router as orchestration_router
from app.pipeline.routes import (
    cancel_script_decomposition_import_job,
    cancel_visual_asset_generation_job,
    router as pipeline_router,
)
from app.player_experience.routes import router as player_experience_router

router = APIRouter(prefix="/api")
router.include_router(orchestration_router)
router.include_router(create_creator_router(resolve_llm_config=shared.pipeline_llm_config))
router.include_router(create_story_authoring_router(resolve_llm_config=shared.pipeline_llm_config))
router.include_router(player_experience_router)
router.include_router(pipeline_router)
router.include_router(content_router)
router.include_router(client_router)

# Compatibility aliases for existing tests and transitional imports.
pipeline_config_store = shared.pipeline_config_store
visual_asset_generation_jobs = shared.visual_asset_generation_jobs
visual_asset_generation_tasks = shared.visual_asset_generation_tasks
script_decomposition_jobs = shared.script_decomposition_jobs
script_decomposition_tasks = shared.script_decomposition_tasks

__all__ = [
    "LLMProviderConfig",
    "NpcLorebookCreationAgent",
    "PipelineConfigStore",
    "cancel_script_decomposition_import_job",
    "cancel_visual_asset_generation_job",
    "pipeline_config_store",
    "router",
    "script_decomposition_jobs",
    "script_decomposition_tasks",
    "visual_asset_generation_jobs",
    "visual_asset_generation_tasks",
]
