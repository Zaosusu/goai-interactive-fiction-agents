from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core.models import AgentSessionState, WorldActionRequest, WorldActionResponse

ToolHandler = Callable[[AgentSessionState, dict[str, Any]], WorldActionResponse]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, name: str, description: str, handler: ToolHandler) -> None:
        self._tools[name] = ToolSpec(name=name, description=description, handler=handler)

    def run(self, state: AgentSessionState, request: WorldActionRequest) -> WorldActionResponse:
        tool = self._tools.get(request.action)
        if tool is None:
            return WorldActionResponse(
                action=request.action,
                narration=f"当前世界不认识动作：{request.action}",
                state=state.world_state,
                quest_progress=state.quest_progress,
                suggested_actions=self.available_actions(),
            )
        return tool.handler(state, request.payload)

    def available_actions(self) -> list[str]:
        return list(self._tools.keys())

    def descriptions(self) -> list[str]:
        return [f"{tool.name}: {tool.description}" for tool in self._tools.values()]
