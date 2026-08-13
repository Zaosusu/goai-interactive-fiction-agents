from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.agents.creator_assistant.compiler import CreatorGraphCompiler
from app.agents.creator_assistant.schema import (
    CreatorHistoryMessage,
    CreatorHistoryMessageCreate,
    CreatorVersionArtifact,
    CreatorVersionSummary,
    CreatorWorkflowRun,
)


class CreatorVersionStore:
    def __init__(self, root: Path | str = "data/creator_versions") -> None:
        self.root = Path(root)
        self.compiler = CreatorGraphCompiler()

    def save(self, world_id: str, label: str, project: dict) -> CreatorVersionArtifact:
        safe_world_id = _safe_path_id(world_id)
        normalized = self.compiler.normalize(project)
        report = self.compiler.validate(normalized)
        if not report.valid:
            raise ValueError("cannot snapshot an invalid creator graph")
        created_at = datetime.now(timezone.utc).isoformat()
        version_id = f"version_{uuid4().hex}"
        artifact = CreatorVersionArtifact(
            version_id=version_id,
            world_id=world_id,
            label=label.strip() or "Manual snapshot",
            created_at=created_at,
            node_count=len(normalized.get("nodes", [])),
            project_hash=self.compiler.hash(normalized),
            project=normalized,
        )
        directory = self.root / safe_world_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{version_id}.json").write_text(
            json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return artifact

    def list(self, world_id: str) -> list[CreatorVersionSummary]:
        directory = self.root / _safe_path_id(world_id)
        if not directory.exists():
            return []
        versions: list[CreatorVersionSummary] = []
        for path in directory.glob("version_*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                versions.append(CreatorVersionSummary.model_validate(payload))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return sorted(versions, key=lambda item: item.created_at, reverse=True)

    def load(self, world_id: str, version_id: str) -> CreatorVersionArtifact:
        path = self.root / _safe_path_id(world_id) / f"{_safe_path_id(version_id)}.json"
        if not path.exists():
            raise FileNotFoundError(f"creator version not found: {version_id}")
        return CreatorVersionArtifact.model_validate_json(path.read_text(encoding="utf-8"))


class CreatorHistoryStore:
    """Durable, project-scoped conversation history for Creator Assistant."""

    def __init__(self, root: Path | str = "data/creator_history", max_messages: int = 500) -> None:
        self.root = Path(root)
        self.max_messages = max_messages

    def list(self, world_id: str) -> list[CreatorHistoryMessage]:
        path = self._path(world_id)
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return []
        messages = payload.get("messages", []) if isinstance(payload, dict) else []
        result: list[CreatorHistoryMessage] = []
        for item in messages if isinstance(messages, list) else []:
            try:
                result.append(CreatorHistoryMessage.model_validate(item))
            except ValueError:
                continue
        return result[-self.max_messages :]

    def append(self, world_id: str, message: CreatorHistoryMessageCreate) -> CreatorHistoryMessage:
        safe_world_id = _safe_path_id(world_id)
        record = CreatorHistoryMessage(
            **message.model_dump(mode="json"),
            message_id=f"message_{uuid4().hex}",
            world_id=world_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        messages = [*self.list(world_id), record][-self.max_messages :]
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{safe_world_id}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {"world_id": world_id, "messages": [item.model_dump(mode="json") for item in messages]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
        return record

    def clear(self, world_id: str) -> None:
        self._path(world_id).unlink(missing_ok=True)

    def _path(self, world_id: str) -> Path:
        return self.root / f"{_safe_path_id(world_id)}.json"


class CreatorWorkflowStore:
    """Durable Creator workflow runs, scoped by the Creator project id."""

    def __init__(self, root: Path | str = "data/creator_workflows") -> None:
        self.root = Path(root)

    def save(self, run: dict) -> CreatorWorkflowRun:
        artifact = CreatorWorkflowRun.model_validate(deepcopy(run))
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(artifact.run_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
        return artifact

    def load(self, run_id: str) -> CreatorWorkflowRun:
        path = self._path(run_id)
        if not path.exists():
            raise FileNotFoundError(f"creator workflow run not found: {run_id}")
        return CreatorWorkflowRun.model_validate_json(path.read_text(encoding="utf-8"))

    def latest(self, world_id: str) -> CreatorWorkflowRun | None:
        target = str(world_id or "").strip()
        if not target or not self.root.exists():
            return None
        latest_run: CreatorWorkflowRun | None = None
        for path in self.root.glob("run_*.json"):
            try:
                run = CreatorWorkflowRun.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if run.world_id != target:
                continue
            if latest_run is None or run.updated_at > latest_run.updated_at:
                latest_run = run
        return latest_run

    def _path(self, run_id: str) -> Path:
        return self.root / f"{_safe_path_id(run_id)}.json"


def _safe_path_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fa5]+", "_", str(value or "").strip())
    normalized = normalized.strip("_")
    if not normalized:
        raise ValueError("world/version id is empty")
    return normalized[:160]
