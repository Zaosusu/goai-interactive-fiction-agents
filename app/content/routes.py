from __future__ import annotations

import traceback
import sys
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from app.agents.npc_lorebook import NpcLorebookArtifact, NpcLorebookCreationAgent, NpcLorebookCreationError
from app.agents.project_intake import analyze_project_integration
from app.api import shared
from app.api.dependencies import reset_agent
from app.worlds.sandbox.models import (
    ExperienceFeedbackRequest,
    ExperienceLearningProfile,
    ProjectIntakeRequest,
    ProjectIntegrationAnalysis,
    SandboxWorldConfig,
    WorldSummary,
    WorldTemplateSummary,
)

router = APIRouter(tags=["content"])


@router.get("/worlds", response_model=list[WorldSummary])
async def list_worlds() -> list[WorldSummary]:
    return shared.world_store.list_worlds()


@router.get("/world-templates", response_model=list[WorldTemplateSummary])
async def get_world_templates() -> list[WorldTemplateSummary]:
    return shared.template_store.list()


@router.get("/experience/profile", response_model=ExperienceLearningProfile)
async def get_experience_profile() -> ExperienceLearningProfile:
    return shared.experience_agent.profile()


@router.post("/experience/feedback", response_model=ExperienceLearningProfile)
async def submit_experience_feedback(feedback: ExperienceFeedbackRequest) -> ExperienceLearningProfile:
    return shared.experience_agent.record(feedback)


@router.post("/projects/analyze", response_model=ProjectIntegrationAnalysis)
async def analyze_project(request: ProjectIntakeRequest) -> ProjectIntegrationAnalysis:
    return analyze_project_integration(request)


@router.post("/world-templates", response_model=WorldTemplateSummary)
async def create_world_template(template: WorldTemplateSummary) -> WorldTemplateSummary:
    return shared.template_store.save(template)


@router.put("/world-templates/{template_id}", response_model=WorldTemplateSummary)
async def update_world_template(template_id: str, template: WorldTemplateSummary) -> WorldTemplateSummary:
    template.id = template_id
    return shared.template_store.save(template)


@router.delete("/world-templates/{template_id}")
async def delete_world_template(template_id: str) -> dict[str, str]:
    shared.template_store.delete(template_id)
    return {"status": "deleted", "template_id": template_id}


@router.post("/worlds", response_model=SandboxWorldConfig)
async def create_world(config: SandboxWorldConfig | None = None) -> SandboxWorldConfig:
    if config is None:
        return shared.world_store.create_default()
    try:
        return shared.world_store.save(config)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": str(exc),
                "type": type(exc).__name__,
                "trace": traceback.format_exc(limit=6),
            },
        ) from exc


@router.get("/worlds/{world_id}", response_model=SandboxWorldConfig)
async def get_world(world_id: str) -> SandboxWorldConfig:
    try:
        return shared.world_store.load(world_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/worlds/{world_id}", response_model=SandboxWorldConfig)
async def save_world(world_id: str, config: SandboxWorldConfig) -> SandboxWorldConfig:
    config.world_id = world_id
    try:
        saved = shared.world_store.save(config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reset_agent(saved.world_id)
    return saved


@router.post("/worlds/{world_id}/lorebook/generate", response_model=SandboxWorldConfig)
async def generate_world_lorebook(world_id: str) -> SandboxWorldConfig:
    try:
        world = shared.world_store.load(world_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    lorebook_agent_cls = _lorebook_agent_class()
    lorebook_agent = lorebook_agent_cls()
    try:
        lorebook = await lorebook_agent.create(world, shared.pipeline_llm_config("npc_lorebook"), strict=False)
    except NpcLorebookCreationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    created_at = shared.now_iso()
    lorebook = lorebook.model_copy(update={"metadata": {**(lorebook.metadata or {}), "created_at": created_at}})
    metadata = {
        **(world.metadata or {}),
        "npc_lorebook": lorebook.model_dump(),
        "npc_lorebook_generation": {
            "agent": "NpcLorebookCreationAgent",
            "created_at": created_at,
            "created_by": lorebook.metadata.get("created_by", ""),
            "entry_count": len(lorebook.entries),
            "fallback_used": bool(lorebook.metadata.get("creation_agent_failed", False)),
            "error": lorebook.metadata.get("creation_agent_error", ""),
        },
    }
    metadata["npc_lorebook_versions"] = shared.append_lorebook_version(metadata, lorebook)
    try:
        saved = shared.world_store.save(world.model_copy(update={"metadata": metadata}))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reset_agent(saved.world_id)
    return saved


@router.post("/worlds/{world_id}/lorebook/select/{version_id}", response_model=SandboxWorldConfig)
async def select_world_lorebook(world_id: str, version_id: str) -> SandboxWorldConfig:
    try:
        world = shared.world_store.load(world_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    metadata = dict(world.metadata or {})
    versions = shared.lorebook_versions(metadata)
    selected = next((item for item in versions if str(item.get("version_id") or "") == version_id), None)
    if not selected:
        raise HTTPException(status_code=404, detail=f"lorebook version not found: {version_id}")
    lorebook = selected.get("artifact")
    if not isinstance(lorebook, dict):
        raise HTTPException(status_code=400, detail="lorebook version has no artifact")
    try:
        artifact = NpcLorebookArtifact.model_validate(lorebook)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid lorebook artifact: {exc}") from exc
    metadata["npc_lorebook"] = artifact.model_dump()
    metadata["npc_lorebook_generation"] = {
        "agent": selected.get("agent") or "NpcLorebookCreationAgent",
        "created_at": selected.get("created_at") or artifact.metadata.get("created_at", ""),
        "created_by": artifact.metadata.get("created_by", ""),
        "entry_count": len(artifact.entries),
        "fallback_used": bool(artifact.metadata.get("creation_agent_failed", False)),
        "error": artifact.metadata.get("creation_agent_error", ""),
        "selected_from_version": selected.get("version_id") or version_id,
    }
    metadata["npc_lorebook_versions"] = [
        {**item, "is_active": str(item.get("version_id") or "") == version_id} for item in versions
    ]
    try:
        saved = shared.world_store.save(world.model_copy(update={"metadata": metadata}))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reset_agent(saved.world_id)
    return saved


@router.delete("/worlds/{world_id}")
async def delete_world(world_id: str) -> dict[str, str]:
    shared.world_store.delete(world_id)
    return {"status": "deleted", "world_id": world_id}


def _lorebook_agent_class():
    api_routes = sys.modules.get("app.api.routes")
    if api_routes is not None:
        return getattr(api_routes, "NpcLorebookCreationAgent", NpcLorebookCreationAgent)
    return NpcLorebookCreationAgent
