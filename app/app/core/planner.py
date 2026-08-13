import uuid

from app.core.models import AgentSessionState, PlanStep, WorldActionRequest


class Planner:
    def ensure_plan(
        self,
        state: AgentSessionState,
        objective: str,
        fallback_actions: list[str],
    ) -> list[PlanStep]:
        pending = [step for step in state.plan if step.status in {"pending", "running"}]
        if pending:
            return state.plan

        state.plan = [
            PlanStep(
                id=str(uuid.uuid4()),
                description=f"推进目标：{objective or state.quest_progress or '当前任务'}",
                action=self._action_from_suggestion(action),
            )
            for action in fallback_actions[:3]
        ]
        return state.plan

    def next_action(self, state: AgentSessionState) -> WorldActionRequest | None:
        for step in state.plan:
            if step.status == "pending" and step.action:
                step.status = "running"
                return WorldActionRequest(action=step.action, payload=step.payload)
        return None

    def mark_result(self, state: AgentSessionState, request: WorldActionRequest, success: bool) -> None:
        for step in state.plan:
            if step.status == "running" and step.action == request.action:
                step.status = "done" if success else "failed"
                return

    def _action_from_suggestion(self, suggestion: str) -> str | None:
        if "探索" in suggestion or "户外" in suggestion:
            return "explore"
        if "望气" in suggestion or "风险" in suggestion:
            return "inspect"
        if "挑战" in suggestion or "战斗" in suggestion:
            return "battle"
        if "复命" in suggestion:
            return "report"
        return None
