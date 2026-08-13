from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from app.agents.creator_assistant.agent import CreatorAssistantAgent
from app.agents.creator_assistant.compiler import CreatorGraphCompiler, CreatorGraphValidationError
from app.agents.creator_assistant.schema import (
    CreatorApplyRequest,
    CreatorApplyResponse,
    CreatorAssistantRequest,
    CreatorAssistantResponse,
    CreatorChangePreview,
    CreatorHistoryMessage,
    CreatorHistoryMessageCreate,
    CreatorToolDefinition,
    CreatorVersionArtifact,
    CreatorVersionCreateRequest,
    CreatorVersionSummary,
    CreatorWorkflowPreview,
    CreatorWorkflowRun,
    CreatorWorkflowRunRequest,
)
from app.agents.creator_assistant.store import CreatorHistoryStore, CreatorVersionStore, CreatorWorkflowStore
from app.agents.creator_assistant.mcp import CreatorMcpCallRequest, McpCallToolResult, McpToolsListResult
from app.agents.creator_assistant.tools import CreatorToolExecutor, CreatorToolRegistry
from app.agents.creator_assistant.workflow import CreatorWorkflowOrchestrator
from app.core.model_config import LLMProviderConfig


def create_router(
    *,
    resolve_llm_config: Callable[[str], LLMProviderConfig],
    agent: CreatorAssistantAgent | None = None,
    compiler: CreatorGraphCompiler | None = None,
    version_store: CreatorVersionStore | None = None,
    history_store: CreatorHistoryStore | None = None,
    tool_registry: CreatorToolRegistry | None = None,
    workflow_orchestrator: CreatorWorkflowOrchestrator | None = None,
    workflow_store: CreatorWorkflowStore | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/creator", tags=["creator"])
    creator_agent = agent or CreatorAssistantAgent()
    graph_compiler = compiler or CreatorGraphCompiler()
    versions = version_store or CreatorVersionStore()
    history = history_store or CreatorHistoryStore()
    registry = tool_registry or CreatorToolRegistry()
    if workflow_orchestrator is None:
        from app.api import shared
        from app.api.dependencies import reset_agent

        executor = CreatorToolExecutor(
            resolve_llm_config=resolve_llm_config,
            resolve_visual_request=shared.with_pipeline_visual_config,
            world_store=shared.world_store,
            visual_asset_agent=shared.visual_asset_agent,
            visual_asset_store=shared.visual_asset_store,
            reset_world_agent=reset_agent,
        )
        workflow_orchestrator = CreatorWorkflowOrchestrator(
            registry=registry,
            executor=executor,
            store=workflow_store,
        )
    workflows = workflow_orchestrator
    mcp_server = workflows.mcp_server

    async def run_agent(request: CreatorAssistantRequest) -> CreatorAssistantResponse:
        request = request.model_copy(update={"creator_llm": request.creator_llm or resolve_llm_config("creator_assistant")})
        return await creator_agent.edit(request)

    @router.post("/assistant/edit", response_model=CreatorAssistantResponse)
    async def edit_creator_project(request: CreatorAssistantRequest) -> CreatorAssistantResponse:
        return await run_agent(request)

    @router.get("/tools", response_model=list[CreatorToolDefinition])
    async def list_creator_tools() -> list[CreatorToolDefinition]:
        return registry.list()

    @router.get("/mcp/tools/list", response_model=McpToolsListResult)
    async def list_creator_mcp_tools() -> McpToolsListResult:
        return mcp_server.list_tools()

    @router.post("/mcp/tools/call", response_model=McpCallToolResult)
    async def call_creator_mcp_tool(request: CreatorMcpCallRequest) -> McpCallToolResult:
        async def ignore_progress(title: str, detail: str) -> None:
            return None

        return await mcp_server.call_tool(
            name=request.name,
            arguments=request.arguments,
            project=request.project,
            artifacts=request.artifacts,
            should_cancel=lambda: False,
            progress=ignore_progress,
        )

    @router.post("/workflows/preview", response_model=CreatorWorkflowPreview)
    async def preview_creator_workflow(request: CreatorAssistantRequest) -> CreatorWorkflowPreview:
        response = await run_agent(request)
        try:
            return workflows.preview(request, response)
        except CreatorGraphValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.report.model_dump(mode="json")) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/workflows/run", response_model=CreatorWorkflowRun)
    async def run_creator_workflow(request: CreatorWorkflowRunRequest) -> CreatorWorkflowRun:
        try:
            return workflows.start(request.preview_id, request.project)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/workflows/latest/{world_id}", response_model=CreatorWorkflowRun | None)
    async def get_latest_creator_workflow(world_id: str) -> CreatorWorkflowRun | None:
        try:
            return workflows.latest(world_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/workflows/{run_id}", response_model=CreatorWorkflowRun)
    async def get_creator_workflow(run_id: str) -> CreatorWorkflowRun:
        try:
            return workflows.get(run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/workflows/{run_id}/cancel", response_model=CreatorWorkflowRun)
    async def cancel_creator_workflow(run_id: str) -> CreatorWorkflowRun:
        try:
            return workflows.cancel(run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/workflows/{run_id}/acknowledge", response_model=CreatorWorkflowRun)
    async def acknowledge_creator_workflow(run_id: str) -> CreatorWorkflowRun:
        try:
            return workflows.acknowledge(run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/assistant/preview", response_model=CreatorChangePreview)
    async def preview_creator_change(request: CreatorAssistantRequest) -> CreatorChangePreview:
        response = await run_agent(request)
        if not response.operations:
            raise HTTPException(status_code=422, detail="creator assistant returned no operations")
        try:
            preview_project, report = graph_compiler.apply(request.project, response.operations)
        except CreatorGraphValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.report.model_dump(mode="json")) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return CreatorChangePreview(
            **response.model_dump(),
            change_id=f"change_{uuid4().hex}",
            base_hash=graph_compiler.hash(request.project),
            preview_project=preview_project,
            report=report,
        )

    @router.post("/assistant/apply", response_model=CreatorApplyResponse)
    async def apply_creator_change(request: CreatorApplyRequest) -> CreatorApplyResponse:
        current_hash = graph_compiler.hash(request.project)
        if current_hash != request.expected_hash:
            raise HTTPException(status_code=409, detail="creator project changed after preview; regenerate the preview")
        try:
            project, report = graph_compiler.apply(request.project, request.operations)
        except CreatorGraphValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.report.model_dump(mode="json")) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return CreatorApplyResponse(project=project, report=report, applied_count=len(request.operations))

    @router.post("/versions", response_model=CreatorVersionArtifact)
    async def create_creator_version(request: CreatorVersionCreateRequest) -> CreatorVersionArtifact:
        try:
            return versions.save(request.world_id, request.label, request.project)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/history/{world_id}", response_model=list[CreatorHistoryMessage])
    async def list_creator_history(world_id: str) -> list[CreatorHistoryMessage]:
        try:
            return history.list(world_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/history/{world_id}", response_model=CreatorHistoryMessage)
    async def append_creator_history(
        world_id: str,
        message: CreatorHistoryMessageCreate,
    ) -> CreatorHistoryMessage:
        try:
            return history.append(world_id, message)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.delete("/history/{world_id}", status_code=204, response_model=None)
    async def clear_creator_history(world_id: str) -> None:
        try:
            history.clear(world_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/versions/{world_id}", response_model=list[CreatorVersionSummary])
    async def list_creator_versions(world_id: str) -> list[CreatorVersionSummary]:
        return versions.list(world_id)

    @router.get("/versions/{world_id}/{version_id}", response_model=CreatorVersionArtifact)
    async def get_creator_version(world_id: str, version_id: str) -> CreatorVersionArtifact:
        try:
            return versions.load(world_id, version_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
