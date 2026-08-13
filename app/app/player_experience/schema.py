from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PlayerStartRequest(BaseModel):
    session_id: str = Field(default="", max_length=160)
    restart: bool = False


class PlayerAdvanceRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=160)


class PlayerChoiceRequest(PlayerAdvanceRequest):
    choice_id: str = Field(min_length=1, max_length=160)


class PlayerHistoryEntry(BaseModel):
    kind: Literal["narration", "dialogue", "choice", "system"] = "narration"
    node_id: str = ""
    speaker_id: str = ""
    speaker_name: str = ""
    content: str = ""
    created_at: str = ""


class PlayerStorySession(BaseModel):
    schema_version: str = "player_story_session.v1"
    session_id: str
    world_id: str
    graph_hash: str = ""
    current_node_id: str = ""
    flags: dict[str, Any] = Field(default_factory=dict)
    player: dict[str, Any] = Field(default_factory=dict)
    visited_node_ids: list[str] = Field(default_factory=list)
    history: list[PlayerHistoryEntry] = Field(default_factory=list)
    post_story_history: list[PlayerHistoryEntry] = Field(default_factory=list)
    triggered_event_ids: list[str] = Field(default_factory=list)
    ended: bool = False
    started_at: str = ""
    updated_at: str = ""


class PlayerChoiceView(BaseModel):
    id: str
    text: str
    consequence_summary: str = ""


class PlayerCharacterView(BaseModel):
    id: str = ""
    name: str = ""
    role: str = ""
    portrait: str = ""


class PlayerNodeView(BaseModel):
    id: str
    type: str = "story"
    title: str = ""
    content: str = ""
    location: str = ""
    background: str = ""
    objective: str = ""


class PlayerWorldView(BaseModel):
    world_id: str
    name: str
    description: str = ""
    player_name: str = "玩家"


class PlayerSessionResponse(BaseModel):
    world: PlayerWorldView
    session_id: str
    node: PlayerNodeView
    speaker: PlayerCharacterView | None = None
    choices: list[PlayerChoiceView] = Field(default_factory=list)
    can_advance: bool = False
    ended: bool = False
    player: dict[str, Any] = Field(default_factory=dict)
    flags: dict[str, Any] = Field(default_factory=dict)
    history: list[PlayerHistoryEntry] = Field(default_factory=list)
    post_story_available: bool = False
    post_story_characters: list[PlayerCharacterView] = Field(default_factory=list)
    post_story_history: list[PlayerHistoryEntry] = Field(default_factory=list)
    saved_at: str = ""
    recovery_notice: str = ""


class PlayableWorldSummary(BaseModel):
    world_id: str
    name: str
    description: str = ""
    node_count: int = 0
    character_count: int = 0
    has_visuals: bool = False


class PostStoryChatRequest(PlayerAdvanceRequest):
    message: str = Field(min_length=1, max_length=4000)
    target_npc_id: str = Field(default="", max_length=160)


class PostStoryEventView(BaseModel):
    id: str
    title: str
    description: str = ""


class PostStoryChatResponse(BaseModel):
    session_id: str
    reply: str
    speaker: PlayerCharacterView | None = None
    triggered_events: list[PostStoryEventView] = Field(default_factory=list)
    history: list[PlayerHistoryEntry] = Field(default_factory=list)
    player: dict[str, Any] = Field(default_factory=dict)
    flags: dict[str, Any] = Field(default_factory=dict)
    saved_at: str = ""
