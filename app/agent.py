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
from app.agents.npc_runtime import AgentRuntime
from app.core.session_store import RuntimeSessionStore
from app.worlds.registry import get_world_adapter


class UniversalNPCAgent:
    """
    Provider-agnostic facade for the demo API.

    The real architecture is:
    - app.core: world-agnostic runtime, state models, provider-agnostic LLM protocol
    - app.core.providers: provider factory
    - app.worlds.<world_id>: world adapter and domain rules
    """

    def __init__(self, world_id: str = "sandbox_1", session_store: RuntimeSessionStore | None = None) -> None:
        self.runtime = AgentRuntime(get_world_adapter(world_id), session_store=session_store)

    async def chat(self, request: ChatRequest) -> ChatResponse:
        return await self.runtime.chat(request)

    def world_action(self, request: WorldActionRequest) -> WorldActionResponse:
        return self.runtime.world_action(request)

    def autonomous_tick(self, request: AutonomousTickRequest) -> AutonomousTickResponse:
        return self.runtime.autonomous_tick(request)

    def query_memory(self, request: MemoryQueryRequest) -> MemoryQueryResponse:
        return self.runtime.query_memory(request)

    def snapshot(self) -> SessionSnapshotResponse:
        return self.runtime.snapshot()
