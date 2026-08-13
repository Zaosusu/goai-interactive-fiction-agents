from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.model_config import LLMProviderConfig


class StrictModel(BaseModel):
    model_config = {"extra": "forbid"}


class StoryExpansionRequest(BaseModel):
    brief: str = Field(min_length=4, max_length=12000)
    target_node_count: int = Field(ge=1, le=100)
    source_node_id: str = Field(default="", max_length=100)
    reconnect_node_id: str = Field(default="", max_length=100)
    insertion_mode: Literal["after", "branch"] = "after"
    project: dict[str, Any] = Field(default_factory=dict)
    expansion_llm: LLMProviderConfig | None = None


class StoryExpansionNode(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    type: Literal["story", "choice"] = "story"
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=3000)
    character: str = Field(default="", max_length=100)
    background_description: str = Field(default="", max_length=2000)
    conditions: dict[str, Any] = Field(default_factory=dict)
    effects: dict[str, Any] = Field(default_factory=dict)


class StoryExpansionDraft(StrictModel):
    summary: str = Field(min_length=1, max_length=1200)
    nodes: list[StoryExpansionNode] = Field(min_length=1, max_length=100)


class StoryExpansionResponse(StrictModel):
    draft: StoryExpansionDraft
    source: str = "llm"
    model: str = ""
    raw_excerpt: str = ""
    repair_attempted: bool = False
