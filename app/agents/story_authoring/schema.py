from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.agents.creator_assistant.schema import CreatorGraphReport
from app.core.model_config import LLMProviderConfig


class StrictModel(BaseModel):
    model_config = {"extra": "forbid"}


class StoryAuthoringRequest(BaseModel):
    brief: str = Field(min_length=10, max_length=20000)
    genre: str = Field(default="修仙剧情冒险", min_length=1, max_length=120)
    tone: str = Field(default="沉浸、克制、有悬念", max_length=240)
    audience: str = Field(default="喜欢角色互动与剧情选择的玩家", max_length=240)
    target_minutes: int = Field(default=30, ge=10, le=180)
    target_scene_count: int = Field(default=6, ge=3, le=16)
    target_character_count: int = Field(default=4, ge=2, le=12)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    language: str = Field(default="简体中文", max_length=40)
    story_llm: LLMProviderConfig | None = None


class StoryCharacter(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    role: str = Field(default="NPC", max_length=160)
    public_profile: str = Field(default="", max_length=1200)
    secret: str = Field(default="", max_length=1200)
    goal: str = Field(default="", max_length=800)
    speaking_style: str = Field(default="", max_length=800)
    initial_location: str = Field(default="", max_length=160)
    knowledge_boundaries: list[str] = Field(default_factory=list, max_length=20)
    portrait_description: str = Field(default="", max_length=2000)


class StoryClue(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1600)
    source_scene_id: str = Field(min_length=1, max_length=80)
    owner_character_id: str = Field(default="", max_length=80)
    reveals: str = Field(default="", max_length=1200)
    required_clue_ids: list[str] = Field(default_factory=list, max_length=12)


class StoryBeat(StrictModel):
    id: str = Field(default="", max_length=100)
    kind: Literal["narration", "dialogue", "action", "reveal"] = "dialogue"
    speaker_id: str = Field(default="", max_length=80)
    content: str = Field(min_length=1, max_length=3000)
    purpose: str = Field(default="", max_length=500)
    conditions: dict[str, Any] = Field(default_factory=dict)
    effects: dict[str, Any] = Field(default_factory=dict)
    visual_description: str = Field(default="", max_length=2000)


class StoryChoice(StrictModel):
    id: str = Field(default="", max_length=100)
    text: str = Field(min_length=1, max_length=500)
    next_scene_id: str = Field(default="", max_length=80)
    consequence_summary: str = Field(default="", max_length=800)
    conditions: dict[str, Any] = Field(default_factory=dict)
    effects: dict[str, Any] = Field(default_factory=dict)


class StoryScene(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    kind: Literal["scene", "ending"] = "scene"
    title: str = Field(min_length=1, max_length=160)
    location: str = Field(min_length=1, max_length=160)
    duration_minutes: int = Field(default=5, ge=1, le=45)
    objective: str = Field(default="", max_length=800)
    opening_narration: str = Field(min_length=1, max_length=3000)
    beats: list[StoryBeat] = Field(default_factory=list, min_length=1, max_length=30)
    choices: list[StoryChoice] = Field(default_factory=list, max_length=8)
    default_next_scene_id: str = Field(default="", max_length=80)
    unlock_clue_ids: list[str] = Field(default_factory=list, max_length=12)
    conditions: dict[str, Any] = Field(default_factory=dict)
    effects: dict[str, Any] = Field(default_factory=dict)
    background_description: str = Field(default="", max_length=2400)

    @model_validator(mode="after")
    def ending_has_no_outgoing_edge(self) -> "StoryScene":
        if self.kind == "ending" and (self.default_next_scene_id or self.choices):
            raise ValueError("ending scenes cannot have choices or a default next scene")
        return self


class StoryDraft(StrictModel):
    schema_version: str = "story_draft.v1"
    story_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=160)
    genre: str = Field(min_length=1, max_length=160)
    tone: str = Field(default="", max_length=500)
    premise: str = Field(min_length=1, max_length=2000)
    player_role: str = Field(min_length=1, max_length=1200)
    player_goal: str = Field(min_length=1, max_length=1200)
    world_lore: str = Field(min_length=1, max_length=6000)
    start_scene_id: str = Field(min_length=1, max_length=80)
    player_name: str = Field(default="玩家", max_length=80)
    player_stats: dict[str, str | int | float | bool] = Field(default_factory=dict)
    initial_items: list[str] = Field(default_factory=list, max_length=30)
    visual_style: dict[str, Any] = Field(default_factory=dict)
    characters: list[StoryCharacter] = Field(min_length=2, max_length=16)
    clues: list[StoryClue] = Field(min_length=1, max_length=40)
    scenes: list[StoryScene] = Field(min_length=3, max_length=24)


class StoryDraftIssue(StrictModel):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    reference_id: str = ""


class StoryDraftReview(StrictModel):
    valid: bool
    scene_count: int
    character_count: int
    clue_count: int
    dialogue_beat_count: int
    total_minutes: int
    reachable_scene_count: int
    ending_count: int
    issues: list[StoryDraftIssue] = Field(default_factory=list)


class StoryAuthoringResponse(StrictModel):
    generation_id: str
    created_at: str
    source: str = "llm"
    model: str = ""
    reply: str
    draft: StoryDraft
    review: StoryDraftReview
    project: dict[str, Any]
    graph_report: CreatorGraphReport
    raw_excerpt: str = ""
    artifact_path: str = ""


class StoryAuthoringRunSummary(StrictModel):
    generation_id: str
    created_at: str
    story_id: str
    title: str
    source: str
    model: str = ""
    scene_count: int
    node_count: int
