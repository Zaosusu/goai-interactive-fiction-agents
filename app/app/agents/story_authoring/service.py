from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from inspect import isawaitable
from typing import Any
from uuid import uuid4

from app.agents.creator_assistant.compiler import CreatorGraphCompiler
from app.agents.story_authoring.agent import StoryAuthoringAgent
from app.agents.story_authoring.compiler import StoryDraftCompiler
from app.agents.story_authoring.schema import StoryAuthoringRequest, StoryAuthoringResponse
from app.agents.story_authoring.store import StoryAuthoringStore
from app.agents.story_authoring.validator import StoryDraftValidator
from app.core.model_config import LLMProviderConfig


class StoryAuthoringValidationError(ValueError):
    def __init__(self, message: str, issues: list[dict]) -> None:
        self.message = message
        self.issues = issues
        super().__init__(_validation_message(message, issues))


class StoryAuthoringService:
    """Runs the reusable Story Authoring stage outside of its HTTP adapter."""

    def __init__(
        self,
        *,
        resolve_llm_config: Callable[[str], LLMProviderConfig],
        agent: StoryAuthoringAgent | None = None,
        compiler: StoryDraftCompiler | None = None,
        validator: StoryDraftValidator | None = None,
        store: StoryAuthoringStore | None = None,
    ) -> None:
        self.resolve_llm_config = resolve_llm_config
        self.agent = agent or StoryAuthoringAgent()
        self.compiler = compiler or StoryDraftCompiler()
        self.validator = validator or StoryDraftValidator()
        self.store = store or StoryAuthoringStore()
        self.graph_compiler = CreatorGraphCompiler()

    async def generate(
        self,
        request: StoryAuthoringRequest,
        *,
        progress: Callable[[str, str], Any] | None = None,
    ) -> StoryAuthoringResponse:
        resolved_request = request.model_copy(
            update={"story_llm": request.story_llm or self.resolve_llm_config("story_authoring")}
        )
        await _emit(progress, "StoryAuthoringAgent · 生成初稿", "正在生成符合 story_draft.v1 Schema 的完整互动剧情。")
        draft, raw, model = await self.agent.create(resolved_request)
        await _emit(progress, "StoryDraftValidator · 确定性审查", "正在检查引用、分支可达性、死路、结局和具体台词。")
        review = self.validator.review(draft, resolved_request)
        if not review.valid:
            issues = [issue.model_dump(mode="json") for issue in review.issues]
            repair = getattr(self.agent, "repair", None)
            repair_attempted = False
            if callable(repair):
                repair_attempted = True
                await _emit(
                    progress,
                    "StoryDraftRepairAgent · 自动修稿",
                    _validation_message("初稿未通过审查，正在按问题清单自动修复", issues),
                )
                draft, repair_raw, repair_model = await repair(resolved_request, draft, issues)
                raw = repair_raw
                model = repair_model or model
                await _emit(progress, "StoryDraftValidator · 修复后复验", "正在重新检查修复稿的结构与可玩流程。")
                review = self.validator.review(draft, resolved_request)
                issues = [issue.model_dump(mode="json") for issue in review.issues]
            if not review.valid:
                failure_message = (
                    "剧情草案在自动修复后仍未通过确定性校验。"
                    if repair_attempted
                    else "剧情草案未通过确定性校验。"
                )
                self.store.save_failure(
                    stage="story_draft_review",
                    message=failure_message,
                    issues=issues,
                    draft=draft.model_dump(mode="json"),
                    raw_excerpt=raw[:1200],
                )
                raise StoryAuthoringValidationError(
                    failure_message,
                    issues,
                )
        await _emit(progress, "StoryDraftValidator · 审查通过", f"{review.scene_count} 个场景均可校验，开始编译 Creator Graph。")
        project = self.compiler.compile(draft)
        graph_report = self.graph_compiler.validate(project)
        if not graph_report.valid:
            issues = [issue.model_dump(mode="json") for issue in graph_report.issues]
            self.store.save_failure(
                stage="creator_graph_compile",
                message="剧情草案无法编译为有效 Creator 图。",
                issues=issues,
                draft=draft.model_dump(mode="json"),
                raw_excerpt=raw[:1200],
            )
            raise StoryAuthoringValidationError(
                "剧情草案无法编译为有效 Creator 图。",
                issues,
            )
        response = StoryAuthoringResponse(
            generation_id=f"generation_{uuid4().hex}",
            created_at=datetime.now(timezone.utc).isoformat(),
            source="llm",
            model=model,
            reply=f"已生成《{draft.title}》：{review.scene_count} 个场景、{review.dialogue_beat_count} 条具体 NPC 台词，可导入 Creator 继续编辑。",
            draft=draft,
            review=review,
            project=project,
            graph_report=graph_report,
            raw_excerpt=raw[:1200],
        )
        return self.store.save(response)


async def _emit(progress: Callable[[str, str], Any] | None, title: str, detail: str) -> None:
    if progress is None:
        return
    result = progress(title, detail)
    if isawaitable(result):
        await result


def _validation_message(message: str, issues: list[dict]) -> str:
    errors = [item for item in issues if str(item.get("severity") or "") == "error"]
    selected = errors or issues
    details: list[str] = []
    for issue in selected[:8]:
        code = str(issue.get("code") or "validation_error")
        text = str(issue.get("message") or code)
        reference = str(issue.get("reference_id") or "")
        details.append(f"[{code}] {text}" + (f"（{reference}）" if reference else ""))
    suffix = "；".join(details)
    if len(selected) > len(details):
        suffix += f"；另有 {len(selected) - len(details)} 项"
    return f"{message} {suffix}".strip()
