from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, UploadFile

from app.agents.experience_learning import ExperienceLearningAgent
from app.agents.npc_lorebook import NpcLorebookArtifact
from app.agents.script_decomposition import (
    ScriptDecompositionArtifactStore,
    ScriptGraphCompiler,
    ScriptGraphStore,
)
from app.agents.visual_asset_generation import VisualAssetArtifactStore, VisualAssetGenerationAgent
from app.core.image_generation import ImageGenerationProviderConfig, resolve_image_api_key
from app.core.model_config import LLMProviderConfig
from app.core.pipeline_config import (
    PipelineAgentConfig,
    PipelineConfigStore,
    PipelineDefaultsConfig,
    PipelineWorkbenchConfig,
    image_config_from_payload,
    public_image_config,
    public_llm_config,
    resolve_pipeline_image_config,
    resolve_pipeline_llm_config,
)
from app.worlds.sandbox.importer import extract_document_text
from app.worlds.sandbox.models import VisualAssetGenerationResult, VisualAssetPlan, VisualAssetRequest
from app.worlds.sandbox.store import SandboxWorldStore
from app.worlds.sandbox.template_store import WorldTemplateStore


world_store = SandboxWorldStore()
template_store = WorldTemplateStore()
experience_agent = ExperienceLearningAgent()
visual_asset_agent = VisualAssetGenerationAgent()
visual_asset_store = VisualAssetArtifactStore()
pipeline_config_store = PipelineConfigStore()
decomposition_store = ScriptDecompositionArtifactStore()
script_graph_compiler = ScriptGraphCompiler()
script_graph_store = ScriptGraphStore()

script_decomposition_jobs: dict[str, dict] = {}
script_decomposition_tasks: dict[str, asyncio.Task] = {}
visual_asset_generation_jobs: dict[str, dict] = {}
visual_asset_generation_tasks: dict[str, asyncio.Task] = {}


def parse_optional_llm_config(raw: str | None) -> LLMProviderConfig | None:
    text = str(raw or "").strip()
    if not text or text.lower() in {"null", "none", "undefined"}:
        return None
    return LLMProviderConfig.model_validate_json(text)


def pipeline_config_from_payload(payload: dict) -> PipelineWorkbenchConfig:
    if "defaults" in payload or "agents" in payload:
        defaults_payload = payload.get("defaults") or {}
        agents_payload = payload.get("agents") or {}
        agents: dict[str, PipelineAgentConfig] = {}
        for agent_id, agent_payload in agents_payload.items():
            agent_payload = agent_payload or {}
            agents[str(agent_id)] = PipelineAgentConfig(
                use_default_llm=bool(agent_payload.get("use_default_llm", True)),
                llm=LLMProviderConfig.model_validate(agent_payload.get("llm") or {}),
                use_default_image=bool(agent_payload.get("use_default_image", True)),
                image=image_config_from_payload(agent_payload.get("image") or {}),
            )
        return PipelineWorkbenchConfig(
            defaults=PipelineDefaultsConfig(
                llm=LLMProviderConfig.model_validate(defaults_payload.get("llm") or {}),
                image=image_config_from_payload(defaults_payload.get("image") or {}),
            ),
            agents=agents,
        )
    return PipelineWorkbenchConfig.model_validate(payload)


def public_agent_config(config: PipelineWorkbenchConfig, include_secrets: bool = False) -> dict[str, dict]:
    agent_ids = [
        "script_decomposition",
        "world_builder",
        "visual_prompt_composer",
        "visual_asset_generation",
        "npc_runtime",
    ]
    result: dict[str, dict] = {}
    for agent_id in agent_ids:
        agent = config.agents.get(agent_id, PipelineAgentConfig())
        item: dict[str, dict | bool] = {
            "use_default_llm": agent.use_default_llm,
            "llm": public_llm_config(agent.llm, include_secrets=include_secrets),
            "effective_llm": public_llm_config(resolve_pipeline_llm_config(config, agent_id), include_secrets=include_secrets),
            "use_default_image": agent.use_default_image,
            "image": public_image_config(agent.image, include_secrets=include_secrets),
        }
        if agent_id == "visual_asset_generation":
            effective_image = resolve_pipeline_image_config(config, agent_id)
            item["effective_image"] = public_image_config(
                effective_image,
                api_key=resolve_image_api_key(effective_image),
                include_secrets=include_secrets,
            )
        result[agent_id] = item
    return result


def load_pipeline_config() -> PipelineWorkbenchConfig:
    try:
        return active_pipeline_config_store().load()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def active_pipeline_config_store() -> PipelineConfigStore:
    api_routes = sys.modules.get("app.api.routes")
    if api_routes is not None:
        candidate = getattr(api_routes, "pipeline_config_store", None)
        if isinstance(candidate, PipelineConfigStore):
            return candidate
    return pipeline_config_store


def pipeline_llm_config(purpose: str) -> LLMProviderConfig:
    return resolve_pipeline_llm_config(load_pipeline_config(), purpose)


def with_pipeline_visual_config(request: VisualAssetRequest) -> VisualAssetRequest:
    config = load_pipeline_config()
    updates = {
        "prompt_model": request.prompt_model or resolve_pipeline_llm_config(config, "visual_prompt"),
    }
    if is_empty_image_provider(request.provider):
        updates["provider"] = resolve_pipeline_image_config(config, "visual_asset_generation")
    return request.model_copy(update=updates)


def is_empty_image_provider(provider: ImageGenerationProviderConfig) -> bool:
    default_provider = ImageGenerationProviderConfig()
    return (
        provider.provider == default_provider.provider
        and provider.api_base_url == default_provider.api_base_url
        and provider.model == default_provider.model
        and provider.size == default_provider.size
        and not provider.api_key
        and provider.steps == default_provider.steps
        and provider.cfg_scale == default_provider.cfg_scale
        and provider.seed is None
        and provider.text_mode is None
    )


def job_event(job: dict, status: str, title: str, detail: str) -> None:
    job["updated_at"] = now_iso()
    job.setdefault("events", []).append(
        {
            "status": status,
            "title": title,
            "detail": detail,
            "at": job["updated_at"],
        }
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def raise_if_cancelled(job: dict) -> None:
    if job.get("cancel_requested"):
        raise asyncio.CancelledError()


def new_job(cancelable: bool = False) -> tuple[str, dict[str, Any]]:
    job_id = uuid4().hex
    job: dict[str, Any] = {
        "job_id": job_id,
        "status": "queued",
        "events": [],
        "result": None,
        "error": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    if cancelable:
        job["cancel_requested"] = False
    return job_id, job


def extract_document_parts_text(uploads: list[tuple[str, bytes]]) -> str:
    parts: list[str] = []
    supported = {".txt", ".md", ".markdown", ".json", ".docx", ".pdf", ".rtf", ".html", ".htm", ".csv"}
    for index, (filename, content) in enumerate(sorted(uploads, key=lambda item: item[0]), start=1):
        suffix = Path(filename).suffix.lower()
        if suffix and suffix not in supported:
            continue
        if not content:
            continue
        text = extract_document_text(filename, content)
        parts.append(f"\n\n## Source File {index}: {filename}\n\n{text}")
    if not parts:
        raise ValueError("No readable text found in uploaded documents.")
    return "\n".join(parts).strip()


async def extract_uploaded_documents_text(files: list[UploadFile]) -> str:
    parts: list[str] = []
    supported = {".txt", ".md", ".markdown", ".json", ".docx", ".pdf", ".rtf", ".html", ".htm", ".csv"}
    for index, file in enumerate(sorted(files, key=lambda item: item.filename or ""), start=1):
        filename = file.filename or f"document_{index}"
        suffix = Path(filename).suffix.lower()
        if suffix and suffix not in supported:
            continue
        content = await file.read()
        if not content:
            continue
        text = extract_document_text(filename, content)
        parts.append(f"\n\n## Source File {index}: {filename}\n\n{text}")
    if not parts:
        raise HTTPException(status_code=400, detail="No readable text found in uploaded documents.")
    return "\n".join(parts).strip()


def cancelled_visual_asset_result(request: VisualAssetRequest) -> VisualAssetGenerationResult:
    plan = request_visual_plan_or_empty(request)
    metadata = dict(plan.metadata or {})
    metadata["generation_status"] = "cancelled"
    plan = plan.model_copy(update={"metadata": metadata})
    return VisualAssetGenerationResult(
        plan=plan,
        generated=[],
        failed=[],
        metadata={
            "status": "cancelled",
            "cancelled": True,
            "generated_count": 0,
            "failed_count": 0,
            "planned_count": len(plan.assets),
            "generation_run_id": metadata.get("generation_run_id", ""),
        },
    )


def request_visual_plan_or_empty(request: VisualAssetRequest) -> VisualAssetPlan:
    if request.plan:
        try:
            return VisualAssetPlan.model_validate(request.plan)
        except Exception:
            pass
    return VisualAssetPlan(
        plan_id="cancelled_visual_assets",
        world_id="",
        title="",
        provider=request.provider,
        assets=[],
        metadata={},
    )


def lorebook_versions(metadata: dict) -> list[dict]:
    versions = metadata.get("npc_lorebook_versions")
    return [item for item in versions if isinstance(item, dict)] if isinstance(versions, list) else []


def append_lorebook_version(metadata: dict, lorebook: NpcLorebookArtifact) -> list[dict]:
    versions = lorebook_versions(metadata)
    created_at = str(lorebook.metadata.get("created_at") or now_iso())
    version_id = f"lorebook_{uuid4().hex}"
    version = {
        "version_id": version_id,
        "artifact_id": lorebook.artifact_id,
        "world_id": lorebook.world_id,
        "title": lorebook.title,
        "created_at": created_at,
        "agent": "NpcLorebookCreationAgent",
        "created_by": lorebook.metadata.get("created_by", ""),
        "entry_count": len(lorebook.entries),
        "fallback_used": bool(lorebook.metadata.get("creation_agent_failed", False)),
        "is_active": True,
        "artifact": lorebook.model_dump(),
    }
    inactive_versions = [{**item, "is_active": False} for item in versions]
    return [version, *inactive_versions][:20]


def config_storage_payload(config: PipelineWorkbenchConfig) -> dict[str, Any]:
    return json.loads(config.model_dump_json())
