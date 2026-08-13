import json
import re
from pathlib import Path
from typing import Any

from app.core.models import AgentSessionState, EmotionVector, MemoryItem, NpcRuntimeState, PlanStep


class RuntimeSessionStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path("data") / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, world_id: str) -> dict[str, Any] | None:
        path = self._path(world_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, world_id: str, state: AgentSessionState, npc_sessions: dict[str, NpcRuntimeState]) -> None:
        payload = {
            "schema_version": 1,
            "world_id": world_id,
            "state": dump_agent_state(state),
            "npc_sessions": {
                npc_id: dump_npc_runtime(npc_state)
                for npc_id, npc_state in sorted(npc_sessions.items())
            },
        }
        self._path(world_id).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def delete(self, world_id: str) -> None:
        path = self._path(world_id)
        if path.exists():
            path.unlink()

    def _path(self, world_id: str) -> Path:
        safe_id = re.sub(r"[^a-zA-Z0-9_\-:.]+", "_", world_id.strip()).strip("_") or "sandbox_world"
        return self.root / f"{safe_id}.session.json"


def dump_agent_state(state: AgentSessionState) -> dict[str, Any]:
    return {
        "emotion": state.emotion.to_dict(),
        "memories": [dump_memory(item) for item in state.memories],
        "goals": list(state.goals),
        "plan": [step.model_dump() for step in state.plan],
        "quest_progress": state.quest_progress,
        "world_state": state.world_state,
    }


def load_agent_state(data: dict[str, Any]) -> AgentSessionState:
    return AgentSessionState(
        emotion=load_emotion(data.get("emotion")),
        memories=[load_memory(item) for item in data.get("memories", []) if isinstance(item, dict)],
        goals=[str(goal) for goal in data.get("goals", [])],
        plan=[PlanStep.model_validate(step) for step in data.get("plan", []) if isinstance(step, dict)],
        quest_progress=str(data.get("quest_progress") or ""),
        world_state=data.get("world_state") if isinstance(data.get("world_state"), dict) else {},
    )


def dump_npc_runtime(npc_state: NpcRuntimeState) -> dict[str, Any]:
    return {
        "npc_id": npc_state.npc_id,
        "emotion": npc_state.emotion.to_dict(),
        "memories": [dump_memory(item) for item in npc_state.memories],
        "goals": list(npc_state.goals),
        "turn_count": npc_state.turn_count,
        "last_reply": npc_state.last_reply,
        "relationship_stage": npc_state.relationship_stage,
        "memory_capsule": list(npc_state.memory_capsule),
        "working_memory": dict(npc_state.working_memory),
        "memory_summaries": list(npc_state.memory_summaries),
        "turn_plan": dict(npc_state.turn_plan),
        "conversation_review": dict(npc_state.conversation_review),
        "last_compressed_turn": npc_state.last_compressed_turn,
    }


def load_npc_runtime(data: dict[str, Any]) -> NpcRuntimeState:
    return NpcRuntimeState(
        npc_id=str(data.get("npc_id") or "default"),
        emotion=load_emotion(data.get("emotion")),
        memories=[load_memory(item) for item in data.get("memories", []) if isinstance(item, dict)],
        goals=[str(goal) for goal in data.get("goals", [])],
        turn_count=int(data.get("turn_count") or 0),
        last_reply=str(data.get("last_reply") or ""),
        relationship_stage=str(data.get("relationship_stage") or "familiar"),
        memory_capsule=[str(item) for item in data.get("memory_capsule", []) if str(item or "").strip()],
        working_memory=data.get("working_memory") if isinstance(data.get("working_memory"), dict) else {},
        memory_summaries=[str(item) for item in data.get("memory_summaries", []) if str(item or "").strip()],
        turn_plan=data.get("turn_plan") if isinstance(data.get("turn_plan"), dict) else {},
        conversation_review=data.get("conversation_review") if isinstance(data.get("conversation_review"), dict) else {},
        last_compressed_turn=int(data.get("last_compressed_turn") or 0),
    )


def dump_memory(item: MemoryItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "timestamp": item.timestamp,
        "content": item.content,
        "importance": item.importance,
    }


def load_memory(data: dict[str, Any]) -> MemoryItem:
    item = MemoryItem(
        content=str(data.get("content") or ""),
        importance=float(data.get("importance", 0.5)),
    )
    if data.get("id"):
        item.id = str(data["id"])
    if data.get("timestamp"):
        item.timestamp = str(data["timestamp"])
    return item


def load_emotion(data: Any) -> EmotionVector:
    if not isinstance(data, dict):
        return EmotionVector()
    values = {}
    for key in EmotionVector().to_dict():
        try:
            values[key] = float(data.get(key, 0.0))
        except (TypeError, ValueError):
            values[key] = 0.0
    return EmotionVector(**values)
