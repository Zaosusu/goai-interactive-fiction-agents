from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.agents.story_authoring.schema import StoryAuthoringResponse, StoryAuthoringRunSummary


class StoryAuthoringStore:
    def __init__(self, root: Path | str = "data/story_authoring_runs") -> None:
        self.root = Path(root)

    def artifact_path(self, generation_id: str) -> Path:
        return self.root / f"{_safe_id(generation_id)}.json"

    def save(self, response: StoryAuthoringResponse) -> StoryAuthoringResponse:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.artifact_path(response.generation_id)
        saved = response.model_copy(update={"artifact_path": str(path)})
        path.write_text(json.dumps(saved.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
        return saved

    def save_failure(
        self,
        *,
        stage: str,
        message: str,
        issues: list[dict],
        draft: dict,
        raw_excerpt: str = "",
    ) -> Path:
        """Persist inspectable failed artifacts without provider credentials."""

        self.root.mkdir(parents=True, exist_ok=True)
        failure_id = f"failure_{uuid4().hex}"
        path = self.artifact_path(failure_id)
        path.write_text(
            json.dumps(
                {
                    "failure_id": failure_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "stage": stage,
                    "message": message,
                    "issues": issues,
                    "draft": draft,
                    "raw_excerpt": raw_excerpt,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    def load(self, generation_id: str) -> StoryAuthoringResponse:
        path = self.artifact_path(generation_id)
        if not path.exists():
            raise FileNotFoundError(f"story authoring run not found: {generation_id}")
        return StoryAuthoringResponse.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[StoryAuthoringRunSummary]:
        if not self.root.exists():
            return []
        summaries: list[StoryAuthoringRunSummary] = []
        for path in self.root.glob("generation_*.json"):
            try:
                response = StoryAuthoringResponse.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            summaries.append(
                StoryAuthoringRunSummary(
                    generation_id=response.generation_id,
                    created_at=response.created_at,
                    story_id=response.draft.story_id,
                    title=response.draft.title,
                    source=response.source,
                    model=response.model,
                    scene_count=response.review.scene_count,
                    node_count=response.graph_report.node_count,
                )
            )
        return sorted(summaries, key=lambda item: item.created_at, reverse=True)


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(value or "").strip()).strip("_")
    if not normalized:
        raise ValueError("generation id is empty")
    return normalized[:160]
