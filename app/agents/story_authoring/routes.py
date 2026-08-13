from __future__ import annotations

from collections.abc import Callable
from fastapi import APIRouter, HTTPException

from app.agents.story_authoring.agent import StoryAuthoringAgent, StoryAuthoringError
from app.agents.story_authoring.compiler import StoryDraftCompiler
from app.agents.story_authoring.schema import (
    StoryAuthoringRequest,
    StoryAuthoringResponse,
    StoryAuthoringRunSummary,
)
from app.agents.story_authoring.store import StoryAuthoringStore
from app.agents.story_authoring.service import StoryAuthoringService, StoryAuthoringValidationError
from app.agents.story_authoring.validator import StoryDraftValidator
from app.core.model_config import LLMProviderConfig


def create_router(
    *,
    resolve_llm_config: Callable[[str], LLMProviderConfig],
    agent: StoryAuthoringAgent | None = None,
    compiler: StoryDraftCompiler | None = None,
    validator: StoryDraftValidator | None = None,
    store: StoryAuthoringStore | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/story-authoring", tags=["story-authoring"])
    service = StoryAuthoringService(
        resolve_llm_config=resolve_llm_config,
        agent=agent,
        compiler=compiler,
        validator=validator,
        store=store,
    )

    @router.post("/generate", response_model=StoryAuthoringResponse)
    async def generate_story(request: StoryAuthoringRequest) -> StoryAuthoringResponse:
        try:
            return await service.generate(request)
        except StoryAuthoringError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except StoryAuthoringValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": exc.message, "issues": exc.issues},
            ) from exc

    @router.get("/runs", response_model=list[StoryAuthoringRunSummary])
    async def list_runs() -> list[StoryAuthoringRunSummary]:
        return service.store.list()

    @router.get("/runs/{generation_id}", response_model=StoryAuthoringResponse)
    async def get_run(generation_id: str) -> StoryAuthoringResponse:
        try:
            return service.store.load(generation_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
