import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.core.model_config import LLMProviderConfig


class NoneCommand(BaseModel):
    name: Literal["none"] = "none"
    args: dict[str, Any] = Field(default_factory=dict)


class SetPlayerArgs(BaseModel):
    patch: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class SetPlayerCommand(BaseModel):
    name: Literal["set_player"] = "set_player"
    args: SetPlayerArgs


class GrantItemArgs(BaseModel):
    item: str
    quantity: float | int = 1
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class GrantItemCommand(BaseModel):
    name: Literal["grant_item"] = "grant_item"
    args: GrantItemArgs


class CompleteTaskArgs(BaseModel):
    task_id: str
    status: str = "done"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompleteTaskCommand(BaseModel):
    name: Literal["complete_task"] = "complete_task"
    args: CompleteTaskArgs


class SwitchNpcArgs(BaseModel):
    npc_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SwitchNpcCommand(BaseModel):
    name: Literal["switch_npc"] = "switch_npc"
    args: SwitchNpcArgs


class SetFlagArgs(BaseModel):
    key: str
    value: Any = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class SetFlagCommand(BaseModel):
    name: Literal["set_flag"] = "set_flag"
    args: SetFlagArgs


class RunWorldActionArgs(BaseModel):
    action_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunWorldActionCommand(BaseModel):
    name: Literal["run_world_action"] = "run_world_action"
    args: RunWorldActionArgs


NpcCommand = Annotated[
    NoneCommand
    | SetPlayerCommand
    | GrantItemCommand
    | CompleteTaskCommand
    | SwitchNpcCommand
    | SetFlagCommand
    | RunWorldActionCommand,
    Field(discriminator="name"),
]


def command_to_dict(command: NpcCommand | dict[str, Any] | None) -> dict[str, Any]:
    if command is None:
        return {"name": "none", "args": {}}
    if isinstance(command, dict):
        return command or {"name": "none", "args": {}}
    return command.model_dump()


class AgentLLMOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    action_type: Literal["say", "ask", "emote", "refuse", "hint", "trade", "quest", "wait"] = "say"
    content: str = Field(description="Visible NPC reply to the player")
    inner_thought: str = Field(description="Private NPC thought, shown only in debug panel")
    reasoning: str = Field(default="", description="Short reason for the selected command")
    plan: list[str] = Field(default_factory=list, description="Short-term plan bullets")
    criticism: str = Field(default="", description="Self-check before acting")
    command: NpcCommand = Field(default_factory=NoneCommand, description="Whitelisted world command with typed args")
    emotion_delta: dict[str, float] = Field(default_factory=dict)
    new_memories: list[str] = Field(default_factory=list)
    goal_updates: list[str] = Field(default_factory=list)
    quest_progress: str = ""
    suggested_actions: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    message: str
    player_name: str = "沈青锋"
    location: str = "山门主殿"
    player_goal: str = ""
    target_npc_id: str = ""
    target_npc_ids: list[str] = Field(default_factory=list)
    group_chat: bool = False
    max_npc_replies: int = 50
    npc_llm: LLMProviderConfig | None = None


class ChatMessage(BaseModel):
    role: Literal["npc", "system"] = "npc"
    npc_id: str = ""
    speaker: str = "NPC"
    content: str
    action_type: str = "say"
    command: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    reply: str
    action_type: str
    inner_thought: str
    command: dict[str, Any] = Field(default_factory=dict)
    emotion: dict[str, float]
    memories: list[str]
    goals: list[str]
    player_goal: str
    quest_progress: str
    suggested_actions: list[str]
    player: dict[str, Any]
    active_entity: dict[str, Any] | None
    speaker: dict[str, Any] | None = None
    npcs: list[dict[str, Any]] = Field(default_factory=list)
    nearby_npcs: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(default_factory=list)
    debug_trace: dict[str, Any] = Field(default_factory=dict)


class WorldActionRequest(BaseModel):
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


class WorldActionResponse(BaseModel):
    action: str
    narration: str
    state: dict[str, Any] = Field(default_factory=dict)
    player: dict[str, Any] = Field(default_factory=dict)
    active_entity: dict[str, Any] | None = None
    speaker: dict[str, Any] | None = None
    npcs: list[dict[str, Any]] = Field(default_factory=list)
    nearby_npcs: list[dict[str, Any]] = Field(default_factory=list)
    quest_progress: str = ""
    suggested_actions: list[str] = Field(default_factory=list)


class AutonomousTickRequest(BaseModel):
    max_steps: int = 1
    objective: str = ""


class AutonomousTickResponse(BaseModel):
    objective: str
    executed: list[WorldActionResponse] = Field(default_factory=list)
    plan: list[dict[str, Any]] = Field(default_factory=list)
    stopped_reason: str = ""


class MemoryQueryRequest(BaseModel):
    query: str
    limit: int = 8


class PlanStep(BaseModel):
    id: str
    description: str
    action: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "running", "done", "failed", "skipped"] = "pending"


class RagDocument(BaseModel):
    id: str
    content: str
    importance: float
    relevance: float
    verdict: Literal["relevant", "weak", "irrelevant"] = "weak"


class RagContext(BaseModel):
    original_query: str = ""
    rewritten_query: str | None = None
    documents: list[RagDocument] = Field(default_factory=list)
    reliable: bool = False
    note: str = ""


class MemoryQueryResponse(BaseModel):
    rag: RagContext


class SessionSnapshotResponse(BaseModel):
    world_id: str
    started: bool = False
    state: dict[str, Any] = Field(default_factory=dict)
    player: dict[str, Any] = Field(default_factory=dict)
    active_entity: dict[str, Any] | None = None
    speaker: dict[str, Any] | None = None
    npcs: list[dict[str, Any]] = Field(default_factory=list)
    nearby_npcs: list[dict[str, Any]] = Field(default_factory=list)
    quest_progress: str = ""
    goals: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    inner_thought: str = ""


class ReviewIssue(BaseModel):
    severity: Literal["info", "warning", "error"] = "info"
    area: str
    message: str
    path: str = ""


class ReviewReport(BaseModel):
    reviewer: str
    passed: bool = True
    issues: list[ReviewIssue] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass
class EmotionVector:
    trust: float = 0.0
    fear: float = 0.0
    anger: float = 0.0
    respect: float = 0.0
    joy: float = 0.0
    anticipation: float = 0.0

    def apply_delta(self, delta: dict[str, float]) -> None:
        for key in self.to_dict():
            value = getattr(self, key)
            value += float(delta.get(key, 0.0))
            setattr(self, key, max(-1.0, min(1.0, value)))

    def to_dict(self) -> dict[str, float]:
        return {
            "trust": self.trust,
            "fear": self.fear,
            "anger": self.anger,
            "respect": self.respect,
            "joy": self.joy,
            "anticipation": self.anticipation,
        }


@dataclass
class MemoryItem:
    content: str
    importance: float = 0.5
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class AgentSessionState:
    emotion: EmotionVector = field(default_factory=EmotionVector)
    memories: list[MemoryItem] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    plan: list[PlanStep] = field(default_factory=list)
    quest_progress: str = ""
    world_state: dict[str, Any] = field(default_factory=dict)
    rag_context: RagContext = field(default_factory=RagContext)
    memory_writer: Callable[[MemoryItem], None] | None = None

    def add_memory(self, content: str, importance: float = 0.5) -> None:
        item = MemoryItem(content=content, importance=importance)
        self.memories.append(item)
        self.memories = self.memories[-30:]
        if self.memory_writer:
            self.memory_writer(item)

    def relevant_memories(self, query: str, limit: int = 8) -> list[MemoryItem]:
        query_chars = set(query.lower())

        def score(item: MemoryItem) -> float:
            return len(query_chars & set(item.content.lower())) + item.importance

        return sorted(self.memories, key=score, reverse=True)[:limit]


@dataclass
class NpcRuntimeState:
    npc_id: str
    emotion: EmotionVector = field(default_factory=EmotionVector)
    memories: list[MemoryItem] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    turn_count: int = 0
    last_reply: str = ""
    relationship_stage: str = "familiar"
    memory_capsule: list[str] = field(default_factory=list)
    working_memory: dict[str, Any] = field(default_factory=dict)
    memory_summaries: list[str] = field(default_factory=list)
    turn_plan: dict[str, Any] = field(default_factory=dict)
    conversation_review: dict[str, Any] = field(default_factory=dict)
    last_compressed_turn: int = 0

    def add_memory(self, content: str, importance: float = 0.5) -> None:
        text = str(content or "").strip()
        if not text:
            return
        self.memories.append(MemoryItem(content=text, importance=importance))
        self.memories = self.memories[-80:]

    def relevant_memories(self, query: str, limit: int = 6) -> list[MemoryItem]:
        query_chars = set(str(query or "").lower())

        def score(item: MemoryItem) -> float:
            return len(query_chars & set(item.content.lower())) + item.importance

        return sorted(self.memories, key=score, reverse=True)[:limit]

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "npc_id": self.npc_id,
            "emotion": self.emotion.to_dict(),
            "goals": self.goals[-8:],
            "turn_count": self.turn_count,
            "last_reply": self.last_reply,
            "relationship_stage": self.relationship_stage,
            "memory_capsule": list(self.memory_capsule[-8:]),
            "working_memory": dict(self.working_memory),
            "memory_summaries": list(self.memory_summaries[-4:]),
            "turn_plan": dict(self.turn_plan),
            "conversation_review": dict(self.conversation_review),
            "last_compressed_turn": self.last_compressed_turn,
            "memories": [item.content for item in self.memories[-12:]],
        }


class WorldAdapter(Protocol):
    world_id: str

    def default_player_goal(self) -> str:
        ...

    def create_initial_state(self) -> AgentSessionState:
        ...

    def build_system_prompt(self, state: AgentSessionState, request: ChatRequest, npc_state: NpcRuntimeState | None = None) -> str:
        ...

    def build_human_prompt(self, request: ChatRequest) -> str:
        ...

    def record_player_message(self, state: AgentSessionState, request: ChatRequest, npc_state: NpcRuntimeState | None = None) -> None:
        ...

    def apply_llm_output(self, state: AgentSessionState, output: AgentLLMOutput, npc_state: NpcRuntimeState | None = None) -> None:
        ...

    def build_chat_response(
        self,
        state: AgentSessionState,
        output: AgentLLMOutput,
        player_goal: str,
        npc_state: NpcRuntimeState | None = None,
    ) -> ChatResponse:
        ...

    def default_actions(self, state: AgentSessionState) -> list[str]:
        ...

    def rag_hints(self, state: AgentSessionState) -> list[str]:
        ...

    def handle_world_action(
        self,
        state: AgentSessionState,
        request: WorldActionRequest,
    ) -> WorldActionResponse:
        ...

    def allowed_commands(self) -> list[str]:
        ...

    def world_action_ids(self) -> list[str]:
        ...
