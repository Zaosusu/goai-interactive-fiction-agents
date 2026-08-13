from __future__ import annotations

import json
from pathlib import Path

from app.worlds.sandbox.models import ExperienceFeedbackRequest, ExperienceLearningProfile


DATA_PATH = Path("data") / "experience_feedback.json"


class ExperienceFeedbackStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DATA_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, feedback: ExperienceFeedbackRequest) -> ExperienceFeedbackRequest:
        items = self.list()
        items.append(feedback)
        self.path.write_text(
            json.dumps([item.model_dump() for item in items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return feedback

    def list(self) -> list[ExperienceFeedbackRequest]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [ExperienceFeedbackRequest.model_validate(item) for item in data if isinstance(item, dict)]


class ExperienceLearningAgent:
    """
    Deterministic experience calibration agent.

    User playtest feedback is the source of truth. The agent summarizes what
    task/NPC/action scale felt immersive, too short, or too long, then exposes a
    profile the world generator can use as a default hint. Explicit user inputs
    still override this profile.
    """

    def __init__(self, store: ExperienceFeedbackStore | None = None) -> None:
        self.store = store or ExperienceFeedbackStore()

    def record(self, feedback: ExperienceFeedbackRequest) -> ExperienceLearningProfile:
        self.store.add(feedback)
        return self.profile()

    def profile(self) -> ExperienceLearningProfile:
        samples = self.store.list()
        if not samples:
            return ExperienceLearningProfile()

        pacing_counts: dict[str, int] = {}
        for sample in samples:
            pacing_counts[sample.pacing] = pacing_counts.get(sample.pacing, 0) + 1

        good = [
            sample
            for sample in samples
            if sample.immersion_score >= 4 and sample.pacing in {"immersive", "slightly_short", "slightly_long"}
        ]
        if not good:
            good = sorted(samples, key=lambda item: item.immersion_score, reverse=True)[: max(1, min(3, len(samples)))]

        recommended_npcs = round(sum(item.npc_count for item in good) / len(good))
        recommended_tasks = round(sum(item.task_count for item in good) / len(good))
        recommended_actions = round(sum(item.action_count for item in good) / len(good))
        confidence = "high" if len(samples) >= 8 else "medium" if len(samples) >= 3 else "low"
        dominant_pacing = max(pacing_counts.items(), key=lambda item: item[1])[0]
        summary = (
            f"基于 {len(samples)} 条体验反馈，当前更推荐约 {recommended_tasks} 个任务、"
            f"{recommended_npcs} 个 NPC、{recommended_actions} 个动作。主要体感：{dominant_pacing}。"
        )
        generation_hint = (
            "生成世界时优先保证任务之间有探索、对话、地点切换和状态变化，不要把任务做成清单打勾。"
            f"当前学习到的沉浸区间大约是 {recommended_tasks} 个任务、{recommended_npcs} 个 NPC、"
            f"{recommended_actions} 个动作。"
        )
        return ExperienceLearningProfile(
            sample_count=len(samples),
            recommended_npcs=max(1, recommended_npcs),
            recommended_tasks=max(1, recommended_tasks),
            recommended_actions=max(1, recommended_actions),
            confidence=confidence,
            summary=summary,
            pacing_counts=pacing_counts,
            generation_hint=generation_hint,
        )
