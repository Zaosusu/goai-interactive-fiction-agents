from app.agent import UniversalNPCAgent
from app.core.session_store import RuntimeSessionStore

_agents: dict[str, UniversalNPCAgent] = {}


def get_agent(world_id: str = "sandbox_1") -> UniversalNPCAgent:
    if world_id not in _agents:
        _agents[world_id] = UniversalNPCAgent(world_id)
    return _agents[world_id]


def reset_agent(world_id: str) -> UniversalNPCAgent:
    _agents.pop(world_id, None)
    return get_agent(world_id)


def reset_agent_session(world_id: str) -> UniversalNPCAgent:
    _agents.pop(world_id, None)
    RuntimeSessionStore().delete(world_id)
    return get_agent(world_id)
