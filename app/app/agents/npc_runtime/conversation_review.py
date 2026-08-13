from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.npc_runtime.turn_director import NpcTurnPlan
from app.core.models import AgentLLMOutput, ChatRequest, NpcRuntimeState


class NpcConversationIssue(BaseModel):
    code: str
    message: str
    severity: Literal["warning", "error"] = "error"


class NpcConversationReviewResult(BaseModel):
    reviewer: str = "NpcConversationReview"
    passed: bool = True
    issues: list[NpcConversationIssue] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    retry_instruction: str = ""


class NpcConversationReview:
    _developer_terms = re.compile(
        r"ScriptGraphDocument|story_graph|script_graph|故事图谱|剧本图谱|图谱|世界树|世界书|Lorebook|JSON|开发者|配置台|调试台|"
        r"当前可用地点|重新核对地点|已登记地点|我是\s*(?:AI|NPC)|只是\s*(?:AI|NPC)",
        re.IGNORECASE,
    )

    def review(
        self,
        output: AgentLLMOutput,
        request: ChatRequest,
        npc_state: NpcRuntimeState,
        plan: NpcTurnPlan | dict[str, Any],
    ) -> NpcConversationReviewResult:
        value = plan if isinstance(plan, NpcTurnPlan) else NpcTurnPlan.model_validate(plan or {})
        text = str(output.content or "").strip()
        issues: list[NpcConversationIssue] = []
        question_count = len(re.findall(r"[？?]", text))
        action_count = len(re.findall(r"（[^（）]{1,500}）|\([^()]{1,500}\)", text))

        if not text:
            issues.append(self._issue("empty_content", "NPC 可见回复为空。"))
        if self._looks_like_json(text):
            issues.append(self._issue("json_leak", "完整结构化 JSON 被放进了 NPC 可见台词。"))
        if self._developer_terms.search(text):
            issues.append(self._issue("developer_context_leak", "NPC 台词暴露了世界外或运行时概念。"))
        if npc_state.last_reply and self._similar(text, npc_state.last_reply):
            issues.append(self._issue("repeated_reply", "NPC 回复与上一轮高度重复。"))
        if question_count > value.question_budget:
            issues.append(self._issue("question_budget", f"本轮问题数 {question_count} 超过预算 {value.question_budget}。"))
        if action_count > value.action_budget:
            issues.append(self._issue("action_budget", f"本轮动作描写数 {action_count} 超过预算 {value.action_budget}。", "warning"))
        if value.scene_constraints.get("speech") == "silent" and self._spoken_text(text):
            issues.append(self._issue("silence_violation", "剧情要求 NPC 沉默，但回复出现了说出口的台词。"))

        errors = [issue for issue in issues if issue.severity == "error"]
        retry = ""
        if errors:
            retry = (
                "你上一版回复未通过角色对话复核。"
                + "；".join(issue.message for issue in errors)
                + "。请保持同一 NPC 身份，基于原玩家输入和本轮导演计划完整重说。"
                "只返回 AgentLLMOutput JSON；content 只放玩家可见台词，不要解释复核、协议或系统。"
            )
        return NpcConversationReviewResult(
            passed=not errors,
            issues=issues,
            metrics={
                "question_count": question_count,
                "question_budget": value.question_budget,
                "action_count": action_count,
                "action_budget": value.action_budget,
                "content_chars": len(text),
            },
            retry_instruction=retry,
        )

    def _looks_like_json(self, text: str) -> bool:
        candidate = text.strip()
        if not (candidate.startswith("{") and candidate.endswith("}")):
            return False
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return False
        return isinstance(parsed, dict) and any(key in parsed for key in ("content", "command", "inner_thought", "action_type"))

    def _similar(self, left: str, right: str) -> bool:
        a = self._normalize(left)
        b = self._normalize(right)
        if len(a) < 18 or len(b) < 18:
            return False
        if a in b or b in a:
            return True
        pairs_a = {a[index : index + 2] for index in range(len(a) - 1)}
        pairs_b = {b[index : index + 2] for index in range(len(b) - 1)}
        union = pairs_a | pairs_b
        return bool(union) and len(pairs_a & pairs_b) / len(union) >= 0.72

    def _spoken_text(self, text: str) -> str:
        return re.sub(r"（[^（）]{0,1000}）|\([^()]{0,1000}\)|[\s，。！？!?、：:；;…—-]", "", text)

    def _normalize(self, value: str) -> str:
        return re.sub(r"[\s，。！？!?、：:；;（）()'\"“”‘’]", "", str(value or "")).lower()

    def _issue(self, code: str, message: str, severity: Literal["warning", "error"] = "error") -> NpcConversationIssue:
        return NpcConversationIssue(code=code, message=message, severity=severity)
