from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api import shared
from app.api.dependencies import get_agent
from app.core.models import ChatRequest
from app.player_experience.runtime import PlayerStoryRuntime
from app.player_experience.schema import (
    PlayableWorldSummary,
    PlayerAdvanceRequest,
    PlayerChoiceRequest,
    PlayerSessionResponse,
    PlayerStartRequest,
    PostStoryChatRequest,
    PostStoryChatResponse,
)

router = APIRouter(prefix="/player", tags=["player-experience"])
runtime = PlayerStoryRuntime()


@router.get("/worlds", response_model=list[PlayableWorldSummary])
async def list_playable_worlds() -> list[PlayableWorldSummary]:
    result: list[PlayableWorldSummary] = []
    for summary in shared.world_store.list_worlds():
        try:
            world = shared.world_store.load(summary.world_id)
        except ValueError:
            continue
        if (world.metadata or {}).get("published_to_play") is not True:
            continue
        graph = (world.metadata or {}).get("creator_graph")
        if not isinstance(graph, dict) or not graph.get("nodes"):
            continue
        characters = graph.get("characters") if isinstance(graph.get("characters"), list) else []
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        result.append(
            PlayableWorldSummary(
                world_id=world.world_id,
                name=world.name,
                description=world.description or world.lore,
                node_count=len(nodes),
                character_count=len(characters),
                has_visuals=any(node.get("background") for node in nodes)
                or any(character.get("portrait") for character in characters),
            )
        )
    return result


@router.post("/worlds/{world_id}/start", response_model=PlayerSessionResponse)
async def start_player_story(world_id: str, request: PlayerStartRequest) -> PlayerSessionResponse:
    world = _world(world_id)
    try:
        return runtime.start(world, request.session_id, request.restart)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/worlds/{world_id}/sessions/{session_id}", response_model=PlayerSessionResponse)
async def resume_player_story(world_id: str, session_id: str) -> PlayerSessionResponse:
    world = _world(world_id)
    try:
        return runtime.resume(world, session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/worlds/{world_id}/advance", response_model=PlayerSessionResponse)
async def advance_player_story(world_id: str, request: PlayerAdvanceRequest) -> PlayerSessionResponse:
    world = _world(world_id)
    try:
        return runtime.advance(world, request.session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/worlds/{world_id}/choose", response_model=PlayerSessionResponse)
async def choose_player_story(world_id: str, request: PlayerChoiceRequest) -> PlayerSessionResponse:
    world = _world(world_id)
    try:
        return runtime.choose(world, request.session_id, request.choice_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/worlds/{world_id}/post-story/chat", response_model=PostStoryChatResponse)
async def post_story_chat(world_id: str, request: PostStoryChatRequest) -> PostStoryChatResponse:
    world = _world(world_id)
    try:
        context = runtime.post_story_context(world, request.session_id)
        target_npc_id = request.target_npc_id or (context.post_story_characters[0].id if context.post_story_characters else "")
        target = next((item for item in context.post_story_characters if item.id == target_npc_id), None)
        if target is None:
            raise ValueError(f"后日谈角色不可用：{target_npc_id or '未指定'}")
        world_target = next((item for item in world.npcs if item.id == target_npc_id), None)
        if world_target is None:
            raise ValueError(f"世界中不存在后日谈角色：{target_npc_id}")
        target_location = str(world_target.location or (world_target.locations[0] if world_target.locations else "")).strip()
        if not target_location:
            target_location = context.node.location or str(context.player.get("location") or "")
        ending_context = (
            "固定剧情已经结束，现在是后日谈自由对话。"
            "请基于已经发生的结局自然回应，不要声称改写或推进已结束的固定主线。"
            f"玩家现在来到{target_location or '后日谈场景'}与{target.name}交谈。"
            f"结局：{context.node.title}。玩家已获得的状态：{context.flags}"
        )
        chat = await get_agent(world_id).chat(
            ChatRequest(
                message=request.message,
                player_name=context.world.player_name,
                location=target_location,
                player_goal=ending_context,
                target_npc_id=target_npc_id,
            )
        )
        speaker_data = chat.speaker or {}
        npc_id = str(speaker_data.get("id") or target_npc_id)
        npc_name = str(speaker_data.get("name") or (target.name if target else "NPC"))
        session, events = runtime.record_post_story_exchange(
            world,
            request.session_id,
            request.message,
            chat.reply,
            npc_id=npc_id,
            npc_name=npc_name,
        )
        speaker = next((item for item in context.post_story_characters if item.id == npc_id), target)
        return PostStoryChatResponse(
            session_id=session.session_id,
            reply=chat.reply,
            speaker=speaker,
            triggered_events=events,
            history=session.post_story_history[-300:],
            player=session.player,
            flags=session.flags,
            saved_at=session.updated_at,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.delete("/worlds/{world_id}/sessions/{session_id}")
async def reset_player_story(world_id: str, session_id: str) -> dict[str, bool]:
    _world(world_id)
    runtime.reset(world_id, session_id)
    return {"deleted": True}


def _world(world_id: str):
    try:
        world = shared.world_store.load(world_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"world not found: {world_id}") from exc
    if (world.metadata or {}).get("published_to_play") is not True:
        raise HTTPException(status_code=404, detail=f"playable world not found: {world_id}")
    return world
