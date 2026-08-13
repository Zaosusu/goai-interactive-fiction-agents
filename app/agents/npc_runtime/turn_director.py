from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.models import AgentSessionState, ChatRequest, NpcRuntimeState


class NpcTurnPlan(BaseModel):
    mode: Literal["continue", "deepen", "comfort", "playful", "close", "resolve"] = "continue"
    current_topic: str = ""
    bridge: str = "承接玩家刚才的话"
    emotion: str = "平稳"
    energy: Literal["low", "medium", "high"] = "medium"
    tone: str = "符合当前 NPC 性格，直接回应眼前事件"
    relationship_stage: Literal["stranger", "familiar", "trusted", "conflict"] = "familiar"
    question_budget: int = Field(default=1, ge=0, le=1)
    action_budget: int = Field(default=1, ge=0, le=2)
    scene_narrative: bool = False
    story_directive: dict[str, Any] = Field(default_factory=dict)
    scene_constraints: dict[str, Any] = Field(default_factory=dict)
    avoid_phrases: list[str] = Field(default_factory=list)
    source: str = "NpcTurnDirector"


class NpcTurnDirector:
    _story_pattern = re.compile(
        r"突然|忽然|随后|次日|第二天|进入|离开|抵达|前往|打开|推开|拿起|放下|决定|"
        r"答应|拒绝|袭击|追杀|受伤|倒下|出现|发现|揭开|选拔|考核|战斗|任务",
        re.IGNORECASE,
    )
    _comfort_pattern = re.compile(r"难受|委屈|害怕|焦虑|失眠|孤独|累了|崩溃|伤心|哭", re.IGNORECASE)
    _conflict_pattern = re.compile(
        r"闭嘴|滚|废物|傻子|威胁|拔线|AI\s*人|(?:你|你们)?\s*只是\s*(?:AI|NPC)|只是\s*(?:AI|NPC)",
        re.IGNORECASE,
    )
    _memory_question_pattern = re.compile(r"还记得|你记得|之前说过|上次|以前", re.IGNORECASE)
    _silence_pattern = re.compile(r"(?:你|他|她|这个角色).{0,8}(?:保持沉默|不要说话|别说话|不许开口|闭嘴)", re.IGNORECASE)

    def plan(
        self,
        state: AgentSessionState,
        request: ChatRequest,
        npc_state: NpcRuntimeState,
    ) -> NpcTurnPlan:
        message = str(request.message or "").strip()
        stage = self._relationship_stage(npc_state, message)
        scene_narrative = bool(self._story_pattern.search(message))
        emotion = self._emotion(message)
        mode = self._mode(message, emotion, scene_narrative)
        last_reply = str(npc_state.last_reply or "").strip()
        current_topic = self._compact(message, 120)
        question_budget = 0 if scene_narrative or emotion in {"愤怒挑衅", "低落需要安慰"} else 1
        action_budget = 2 if scene_narrative else 1
        constraints = self._scene_constraints(message)
        story_directive = {
            "active": scene_narrative,
            "must_advance": scene_narrative,
            "established_change": current_topic if scene_narrative else "",
            "next_beat": "承接已发生事件，让当前 NPC 采取行动、给出后果或推进任务" if scene_narrative else "",
            "user_agency": "不得替玩家决定未声明的动作、感受或选择",
        }
        plan = NpcTurnPlan(
            mode=mode,
            current_topic=current_topic,
            bridge="先回应玩家本轮核心内容，再承接未完成话题" if last_reply else "直接回应玩家本轮核心内容",
            emotion=emotion,
            energy="high" if emotion == "愤怒挑衅" or scene_narrative else "low" if emotion == "低落需要安慰" else "medium",
            tone=self._tone(stage, emotion),
            relationship_stage=stage,
            question_budget=question_budget,
            action_budget=action_budget,
            scene_narrative=scene_narrative,
            story_directive=story_directive,
            scene_constraints=constraints,
            avoid_phrases=self._avoid_phrases(npc_state),
        )
        npc_state.relationship_stage = stage
        npc_state.turn_plan = plan.model_dump()
        state.world_state["active_turn_plan"] = plan.model_dump()
        return plan

    def format_instruction(self, plan: NpcTurnPlan | dict[str, Any]) -> str:
        value = plan if isinstance(plan, NpcTurnPlan) else NpcTurnPlan.model_validate(plan or {})
        lines = [
            "[NPC_TURN_DIRECTOR]",
            f"本轮模式：{value.mode}；玩家情绪：{value.emotion}；关系阶段：{value.relationship_stage}。",
            f"当前话题：{value.current_topic or '承接当前对话'}。",
            f"承接方式：{value.bridge}。",
            f"语气：{value.tone}。",
            f"本轮最多 {value.question_budget} 个问题、{value.action_budget} 个动作描写。",
            "不得重复最近回复的句式或把决定权原样抛回玩家。",
        ]
        if value.scene_narrative:
            lines.extend(
                [
                    "本轮属于剧情承接，必须产生至少一个可观察的新进展：行动、后果、障碍、揭示、转场或任务推进。",
                    "接受玩家已经明确声明成立的事件，不要反问事件是否真的发生。",
                    "不得替玩家决定其未声明的动作、感受或选择。",
                ]
            )
        if value.scene_constraints.get("speech") == "silent":
            lines.append("本轮 NPC 必须沉默；content 只能写动作或神态，不得出现说出口的台词。")
        if value.avoid_phrases:
            lines.append(f"避免复用最近表达：{' / '.join(value.avoid_phrases[:4])}")
        lines.append("[/NPC_TURN_DIRECTOR]")
        return "\n".join(lines)

    def _relationship_stage(self, npc_state: NpcRuntimeState, message: str) -> str:
        if self._conflict_pattern.search(message) or npc_state.emotion.anger >= 0.65:
            return "conflict"
        if npc_state.emotion.trust >= 0.55 or npc_state.emotion.respect >= 0.7:
            return "trusted"
        if npc_state.turn_count <= 0:
            return npc_state.relationship_stage if npc_state.relationship_stage in {"stranger", "familiar"} else "familiar"
        return "familiar"

    def _emotion(self, message: str) -> str:
        if self._comfort_pattern.search(message):
            return "低落需要安慰"
        if self._conflict_pattern.search(message):
            return "愤怒挑衅"
        if re.search(r"哈哈|开心|太好了|好耶|期待", message):
            return "轻快开心"
        return "平稳"

    def _mode(self, message: str, emotion: str, scene_narrative: bool) -> str:
        if emotion == "低落需要安慰":
            return "comfort"
        if emotion == "愤怒挑衅":
            return "resolve"
        if self._memory_question_pattern.search(message):
            return "deepen"
        if scene_narrative:
            return "continue"
        return "continue"

    def _tone(self, stage: str, emotion: str) -> str:
        if emotion == "低落需要安慰":
            return "克制、具体地回应情绪，不连续追问，不使用空泛安慰模板"
        if emotion == "愤怒挑衅":
            return "保持角色立场，可以反驳或驱逐，但绝不承认自己是 AI、NPC 或系统产物"
        if stage == "trusted":
            return "熟悉而自然，允许引用共同经历，但不要突然过度亲密"
        if stage == "conflict":
            return "立场鲜明、符合身份，不使用系统术语解释边界"
        return "符合身份和当前场景，直接、自然、不过度热情"

    def _scene_constraints(self, message: str) -> dict[str, Any]:
        if self._silence_pattern.search(message):
            return {"speech": "silent", "scope": "turn", "reason": "玩家或剧情要求当前角色沉默"}
        return {"speech": "normal", "scope": "turn", "reason": ""}

    def _avoid_phrases(self, npc_state: NpcRuntimeState) -> list[str]:
        values: list[str] = []
        if npc_state.last_reply:
            values.append(self._compact(npc_state.last_reply, 60))
        values.extend(str(item) for item in npc_state.working_memory.get("recent_openings", [])[-3:])
        return list(dict.fromkeys(item for item in values if item))

    def _compact(self, value: str, limit: int) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text if len(text) <= limit else f"{text[:limit]}..."
