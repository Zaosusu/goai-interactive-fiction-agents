from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.core.model_config import LLMProviderConfig


CreatorOperationType = Literal[
    "set_world",
    "set_player_stat",
    "add_item",
    "remove_item",
    "add_character",
    "update_character",
    "delete_character",
    "add_node",
    "update_node",
    "delete_node",
    "add_choice",
    "update_choice",
    "delete_choice",
    "connect_nodes",
    "disconnect_nodes",
    "create_branch",
]


class StrictModel(BaseModel):
    model_config = {"extra": "forbid"}


class WorldPatch(StrictModel):
    name: str | None = None
    lore: str | None = None
    player: dict[str, Any] | None = None


class StatPatch(StrictModel):
    value: str | int | float | bool


class ItemPatch(StrictModel):
    name: str
    quantity: int = Field(default=1, ge=1)


class CharacterPatch(StrictModel):
    id: str = ""
    name: str = ""
    role: str = "NPC"
    personality: str = ""
    location: str = ""
    portrait: str = ""


class CharacterUpdate(StrictModel):
    name: str | None = None
    role: str | None = None
    personality: str | None = None
    location: str | None = None
    portrait: str | None = None


class NodePatch(StrictModel):
    id: str = ""
    type: Literal["story", "choice", "ending"] = "story"
    title: str = ""
    content: str = ""
    character: str = ""
    background: str = ""
    conditions: dict[str, Any] = Field(default_factory=dict)
    effects: dict[str, Any] = Field(default_factory=dict)
    next: str = ""
    after: str = ""
    x: float | None = None
    y: float | None = None


class NodeUpdate(StrictModel):
    type: Literal["story", "choice", "ending"] | None = None
    title: str | None = None
    content: str | None = None
    character: str | None = None
    background: str | None = None
    conditions: dict[str, Any] | None = None
    effects: dict[str, Any] | None = None
    next: str | None = None


class ChoicePatch(StrictModel):
    id: str = ""
    text: str
    next: str = ""
    conditions: dict[str, Any] = Field(default_factory=dict)
    effects: dict[str, Any] = Field(default_factory=dict)


class ChoiceUpdate(StrictModel):
    choice_id: str
    text: str | None = None
    next: str | None = None
    conditions: dict[str, Any] | None = None
    effects: dict[str, Any] | None = None


class ConnectPatch(StrictModel):
    target_id: str
    choice_id: str = ""


class DeleteChoicePatch(StrictModel):
    choice_id: str


class BranchNodePatch(StrictModel):
    id: str = ""
    type: Literal["story", "choice", "ending"] = "story"
    title: str
    content: str = ""
    character: str = ""
    background: str = ""
    conditions: dict[str, Any] = Field(default_factory=dict)
    effects: dict[str, Any] = Field(default_factory=dict)


class BranchPatch(StrictModel):
    source_node_id: str = ""
    choice_text: str
    choice_id: str = ""
    choice_conditions: dict[str, Any] = Field(default_factory=dict)
    choice_effects: dict[str, Any] = Field(default_factory=dict)
    nodes: list[BranchNodePatch] = Field(min_length=1, max_length=24)
    reconnect_node_id: str = ""


_OPERATION_DATA_MODELS: dict[str, type[BaseModel]] = {
    "set_world": WorldPatch,
    "set_player_stat": StatPatch,
    "add_item": ItemPatch,
    "remove_item": ItemPatch,
    "add_character": CharacterPatch,
    "update_character": CharacterUpdate,
    "delete_character": StrictModel,
    "add_node": NodePatch,
    "update_node": NodeUpdate,
    "delete_node": StrictModel,
    "add_choice": ChoicePatch,
    "update_choice": ChoiceUpdate,
    "delete_choice": DeleteChoicePatch,
    "connect_nodes": ConnectPatch,
    "disconnect_nodes": ConnectPatch,
    "create_branch": BranchPatch,
}


class CreatorAssistantOperation(BaseModel):
    model_config = {"extra": "forbid"}

    type: CreatorOperationType
    target_id: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_operation_data(self) -> "CreatorAssistantOperation":
        model = _OPERATION_DATA_MODELS[self.type]
        normalized = model.model_validate(self.data)
        self.data = normalized.model_dump(mode="json", exclude_none=True)
        if self.type in {
            "set_player_stat",
            "update_character",
            "delete_character",
            "update_node",
            "delete_node",
            "add_choice",
            "update_choice",
            "delete_choice",
            "connect_nodes",
            "disconnect_nodes",
        } and not self.target_id:
            raise ValueError(f"{self.type} requires target_id")
        return self


class CreatorGraphIssue(StrictModel):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    node_id: str = ""


class CreatorGraphReport(StrictModel):
    valid: bool
    node_count: int = 0
    edge_count: int = 0
    branch_count: int = 0
    ending_count: int = 0
    reachable_count: int = 0
    issues: list[CreatorGraphIssue] = Field(default_factory=list)


class CreatorAssistantRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    project: dict[str, Any] = Field(default_factory=dict)
    selected_node_id: str = ""
    history: list[dict[str, str]] = Field(default_factory=list, max_length=50)
    creator_llm: LLMProviderConfig | None = None


class CreatorHistoryMessageCreate(StrictModel):
    role: Literal["user", "assistant"]
    speaker: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=20000)
    summary: list[str] = Field(default_factory=list, max_length=20)


class CreatorHistoryMessage(CreatorHistoryMessageCreate):
    message_id: str
    world_id: str
    created_at: str


class CreatorToolCall(StrictModel):
    tool: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=500)


class CreatorToolDefinition(StrictModel):
    id: str
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    destructive: bool = False
    long_running: bool = False
    available: bool = True
    stage: str = "creator"
    owner_agent: str = ""
    capability_type: Literal["agent", "validator", "compiler", "store"] = "agent"


class CreatorAssistantResponse(BaseModel):
    reply: str
    operations: list[CreatorAssistantOperation] = Field(default_factory=list, max_length=100)
    tool_calls: list[CreatorToolCall] = Field(default_factory=list, max_length=20)
    summary: list[str] = Field(default_factory=list)
    intent: Literal["chat", "clarify", "graph_edit", "workflow", "error"] = "graph_edit"
    route: str = "creator_graph"
    requires_confirmation: bool = True
    source: str = "fallback"
    raw_excerpt: str = ""


class CreatorChangePreview(CreatorAssistantResponse):
    change_id: str
    base_hash: str
    preview_project: dict[str, Any]
    report: CreatorGraphReport


class CreatorWorkflowPreview(CreatorChangePreview):
    preview_id: str
    executable: bool = True


class CreatorWorkflowRunRequest(StrictModel):
    preview_id: str = Field(min_length=1, max_length=120)
    project: dict[str, Any]


class CreatorWorkflowEvent(StrictModel):
    status: str
    tool: str = ""
    title: str
    detail: str = ""
    at: str


class CreatorWorkflowRun(StrictModel):
    run_id: str
    preview_id: str
    world_id: str
    request_summary: str = ""
    status: Literal["queued", "running", "done", "error", "cancelling", "cancelled"]
    current_tool: str = ""
    project: dict[str, Any] = Field(default_factory=dict)
    report: CreatorGraphReport | None = None
    artifacts: dict[str, Any] = Field(default_factory=dict)
    events: list[CreatorWorkflowEvent] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    cancel_requested: bool = False
    acknowledged_at: str = ""
    created_at: str
    updated_at: str


class CreatorApplyRequest(BaseModel):
    project: dict[str, Any]
    operations: list[CreatorAssistantOperation] = Field(min_length=1, max_length=100)
    expected_hash: str


class CreatorApplyResponse(BaseModel):
    project: dict[str, Any]
    report: CreatorGraphReport
    applied_count: int


class CreatorVersionCreateRequest(BaseModel):
    world_id: str = Field(min_length=1, max_length=120)
    label: str = Field(default="Manual snapshot", max_length=160)
    project: dict[str, Any]


class CreatorVersionSummary(BaseModel):
    version_id: str
    world_id: str
    label: str
    created_at: str
    node_count: int
    project_hash: str


class CreatorVersionArtifact(CreatorVersionSummary):
    project: dict[str, Any]
