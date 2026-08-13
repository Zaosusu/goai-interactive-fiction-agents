import json
import re
from datetime import datetime, timezone
from pathlib import Path

from app.worlds.sandbox.models import SandboxWorldConfig, WorldSummary
from app.worlds.sandbox.validator import SandboxWorldValidator
from app.worlds.sandbox.visual_binding import attach_visual_bindings

DATA_DIR = Path("data") / "worlds"


class SandboxWorldStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or DATA_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self.validator = SandboxWorldValidator()

    def list_worlds(self) -> list[WorldSummary]:
        worlds = []
        for path in sorted(self.root.glob("*.json")):
            try:
                config = self.load(path.stem)
            except (json.JSONDecodeError, ValueError):
                continue
            worlds.append(
                WorldSummary(
                    world_id=config.world_id,
                    name=config.name,
                    description=config.description,
                    kind="sandbox",
                    created_at=self._file_time_iso(path),
                    updated_at=self._file_time_iso(path),
                )
            )
        return worlds

    def exists(self, world_id: str) -> bool:
        return self._path(world_id).exists()

    def load(self, world_id: str) -> SandboxWorldConfig:
        path = self._path(world_id)
        if not path.exists():
            raise ValueError(f"Sandbox world not found: {world_id}")
        config = self.validator.ensure_valid(SandboxWorldConfig.model_validate_json(path.read_text(encoding="utf-8")))
        return attach_visual_bindings(config)

    def save(self, config: SandboxWorldConfig) -> SandboxWorldConfig:
        config = self.validator.ensure_valid(config)
        config = attach_visual_bindings(config)
        config.world_id = self.normalize_world_id(config.world_id or config.name)
        path = self._path(config.world_id)
        path.write_text(
            json.dumps(config.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return config

    def delete(self, world_id: str) -> None:
        path = self._path(world_id)
        if path.exists():
            path.unlink()

    def create_default(self) -> SandboxWorldConfig:
        index = len(list(self.root.glob("*.json"))) + 1
        world_id = f"sandbox_{index}"
        template_path = self.root / "sandbox_1.json"
        if template_path.exists():
            config = SandboxWorldConfig.model_validate_json(template_path.read_text(encoding="utf-8"))
            config.world_id = world_id
            config.name = f"{config.name} {index}"
        else:
            config = SandboxWorldConfig(
                world_id=world_id,
                name=f"青岚修真界 MVP {index}",
                description="独立修仙世界最小闭环：接取试炼、入谷、取得灵印、回宗复命。",
            )
        return self.save(config)

    def normalize_world_id(self, value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_\-:.]+", "_", value.strip()).strip("_")
        return cleaned or "sandbox_world"

    def _path(self, world_id: str) -> Path:
        safe_id = self.normalize_world_id(world_id)
        return self.root / f"{safe_id}.json"

    def _file_time_iso(self, path: Path) -> str:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
