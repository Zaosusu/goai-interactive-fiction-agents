from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api import shared
from app.api.dependencies import get_agent, reset_agent_session
from app.core.models import (
    AutonomousTickRequest,
    AutonomousTickResponse,
    ChatRequest,
    ChatResponse,
    MemoryQueryRequest,
    MemoryQueryResponse,
    SessionSnapshotResponse,
    WorldActionRequest,
    WorldActionResponse,
)

router = APIRouter(tags=["client"])


@router.post("/worlds/{world_id}/start", response_model=WorldActionResponse)
async def start_world(world_id: str) -> WorldActionResponse:
    try:
        world = shared.world_store.load(world_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"world not found: {world_id}") from exc
    agent = reset_agent_session(world_id)
    opening_scene = world.opening_scene or world.description or world.lore or "世界已启动，等待玩家行动。"
    return agent.world_action(WorldActionRequest(action="advance_scene", payload={"scene": opening_scene}))


@router.get("/worlds/{world_id}/session", response_model=SessionSnapshotResponse)
async def get_world_session(world_id: str) -> SessionSnapshotResponse:
    return get_agent(world_id).snapshot()


@router.post("/worlds/{world_id}/chat", response_model=ChatResponse)
async def chat_in_world(world_id: str, request: ChatRequest) -> ChatResponse:
    try:
        request = request.model_copy(update={"npc_llm": request.npc_llm or shared.pipeline_llm_config("npc")})
        return await get_agent(world_id).chat(request)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/worlds/{world_id}/action", response_model=WorldActionResponse)
async def action_in_world(world_id: str, request: WorldActionRequest) -> WorldActionResponse:
    try:
        return get_agent(world_id).world_action(request)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/worlds/{world_id}/agent/tick", response_model=AutonomousTickResponse)
async def tick_in_world(world_id: str, request: AutonomousTickRequest) -> AutonomousTickResponse:
    return get_agent(world_id).autonomous_tick(request)


@router.post("/worlds/{world_id}/memory/query", response_model=MemoryQueryResponse)
async def query_world_memory(world_id: str, request: MemoryQueryRequest) -> MemoryQueryResponse:
    return get_agent(world_id).query_memory(request)
