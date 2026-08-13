from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


LorebookStrategy = Literal["constant", "normal", "selective", "disabled"]
LorebookEntryType = Literal["world", "character", "location", "item", "clue", "scene", "task", "rule", "secret", "visual", "summary", "table", "other"]
LorebookPosition = Literal["system", "developer", "user", "assistant"]


class NpcLorebookEntry(BaseModel):
    id: str
    title: str
    content: str
    entry_type: LorebookEntryType = "world"
    keywords: list[str] = Field(default_factory=list)
    regex_keywords: list[str] = Field(default_factory=list)
    strategy: LorebookStrategy = "normal"
    position: LorebookPosition = "system"
    priority: int = 100
    scan_depth: int = 5
    token_budget: int = 220
    chain: bool = False
    npc_ids: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NpcLorebookArtifact(BaseModel):
    artifact_id: str
    world_id: str
    title: str = ""
    schema_version: str = "npc_lorebook.v1"
    entries: list[NpcLorebookEntry] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
