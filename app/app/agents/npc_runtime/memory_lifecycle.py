from __future__ import annotations

import re
from typing import Any

from app.core.models import AgentLLMOutput, ChatRequest, NpcRuntimeState


class NpcMemoryLifecycle:
    capsule_limit = 8
    summary_limit = 6
    compression_window = 8

    def prepare_turn(self, npc_state: NpcRuntimeState, request: ChatRequest) -> None:
        message = str(request.message or "").strip()
        explicit = self._explicit_memory(message)
        if explicit:
            self._append_unique(npc_state.memory_capsule, explicit, self.capsule_limit)
        npc_state.working_memory = {
            **npc_state.working_memory,
            "current_topic": self._compact(message, 160),
            "last_player_message": self._compact(message, 240),
            "player_name": str(request.player_name or "玩家"),
            "location": str(request.location or ""),
            "relationship_stage": npc_state.relationship_stage,
            "open_loop": self._open_loop_from_message(message),
        }

    def commit_turn(self, npc_state: NpcRuntimeState, request: ChatRequest, output: AgentLLMOutput) -> None:
        content = str(output.content or "").strip()
        openings = list(npc_state.working_memory.get("recent_openings", []))
        opening = self._opening(content)
        if opening:
            openings.append(opening)
        open_loop = self._open_loop_from_reply(content)
        npc_state.working_memory = {
            **npc_state.working_memory,
            "current_topic": self._compact(request.message, 160),
            "last_player_message": self._compact(request.message, 240),
            "last_npc_reply": self._compact(content, 320),
            "open_loop": open_loop,
            "relationship_stage": npc_state.relationship_stage,
            "recent_openings": openings[-4:],
            "last_action_type": str(output.action_type or "say"),
        }
        if npc_state.turn_count - npc_state.last_compressed_turn >= self.compression_window:
            summary = self._compress(npc_state)
            if summary:
                self._append_unique(npc_state.memory_summaries, summary, self.summary_limit)
            npc_state.last_compressed_turn = npc_state.turn_count

    def prompt_context(self, npc_state: NpcRuntimeState, query: str = "") -> dict[str, Any]:
        return {
            "relationship_stage": npc_state.relationship_stage,
            "memory_capsule": list(npc_state.memory_capsule[-self.capsule_limit :]),
            "working_memory": dict(npc_state.working_memory),
            "conversation_summaries": list(npc_state.memory_summaries[-3:]),
            "relevant_memories": [item.content for item in npc_state.relevant_memories(query, limit=8)],
        }

    def _explicit_memory(self, message: str) -> str:
        patterns = [
            r"(?:请)?记住(?:我)?[：,:， ]*(.{2,160})",
            r"以后叫我[：,:， ]*(.{1,60})",
            r"我的(?:名字|称呼|偏好|习惯|身份)是[：,:， ]*(.{1,120})",
        ]
        for pattern in patterns:
            match = re.search(pattern, message, flags=re.IGNORECASE)
            if match:
                value = self._compact(match.group(1), 180).rstrip("。！？!? ")
                if value:
                    return value
        return ""

    def _open_loop_from_message(self, message: str) -> str:
        if re.search(r"怎么办|下一步|怎么做|为什么|能不能|可不可以|吗[？?]?", message):
            return self._compact(message, 140)
        return ""

    def _open_loop_from_reply(self, content: str) -> str:
        questions = re.findall(r"[^。！？!?]{2,120}[？?]", content)
        return self._compact(questions[-1], 140) if questions else ""

    def _compress(self, npc_state: NpcRuntimeState) -> str:
        recent = [item.content for item in npc_state.memories[-self.compression_window * 2 :] if item.content]
        if not recent:
            return ""
        compact = "；".join(self._compact(item, 90) for item in recent[-10:])
        return f"截至第 {npc_state.turn_count} 轮的共同经历：{self._compact(compact, 720)}"

    def _opening(self, content: str) -> str:
        text = re.sub(r"^[（(][^）)]{0,240}[）)]\s*", "", content).strip()
        sentence = re.split(r"[。！？!?\n]", text, maxsplit=1)[0]
        return self._compact(sentence, 48)

    def _append_unique(self, target: list[str], value: str, limit: int) -> None:
        normalized = self._normalize(value)
        if not normalized:
            return
        target[:] = [item for item in target if self._normalize(item) != normalized]
        target.append(value)
        target[:] = target[-limit:]

    def _normalize(self, value: str) -> str:
        return re.sub(r"[\s，。！？!?、：:；;（）()'\"“”‘’]", "", str(value or "")).lower()

    def _compact(self, value: str, limit: int) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text if len(text) <= limit else f"{text[:limit]}..."
