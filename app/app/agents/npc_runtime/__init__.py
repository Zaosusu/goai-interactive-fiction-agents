from app.agents.npc_lorebook import (
    NpcLorebookArtifact,
    NpcLorebookCompiler,
    NpcLorebookCreationAgent,
    NpcLorebookCreationError,
    NpcLorebookEntry,
    NpcLorebookReviewAgent,
    NpcLorebookRuntime,
)
from app.agents.npc_runtime.conversation_review import NpcConversationIssue, NpcConversationReview, NpcConversationReviewResult
from app.agents.npc_runtime.memory_lifecycle import NpcMemoryLifecycle
from app.agents.npc_runtime.turn_director import NpcTurnDirector, NpcTurnPlan


def __getattr__(name: str):
    if name in {"AgentRuntime", "NpcAgent", "RouterAgent", "StateValidatorAgent"}:
        from app.agents.npc_runtime.agent import AgentRuntime, NpcAgent, RouterAgent, StateValidatorAgent

        return {
            "AgentRuntime": AgentRuntime,
            "NpcAgent": NpcAgent,
            "RouterAgent": RouterAgent,
            "StateValidatorAgent": StateValidatorAgent,
        }[name]
    raise AttributeError(name)

__all__ = [
    "AgentRuntime",
    "NpcAgent",
    "RouterAgent",
    "StateValidatorAgent",
    "NpcLorebookArtifact",
    "NpcLorebookCompiler",
    "NpcLorebookCreationAgent",
    "NpcLorebookCreationError",
    "NpcLorebookEntry",
    "NpcLorebookReviewAgent",
    "NpcLorebookRuntime",
    "NpcTurnPlan",
    "NpcTurnDirector",
    "NpcMemoryLifecycle",
    "NpcConversationIssue",
    "NpcConversationReviewResult",
    "NpcConversationReview",
]

