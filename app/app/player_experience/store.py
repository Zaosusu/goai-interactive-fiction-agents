from __future__ import annotations

import json
import re
from pathlib import Path

from app.player_experience.schema import PlayerStorySession


class PlayerSessionStore:
    def __init__(self, root: Path | str = "data/player_sessions") -> None:
        self.root = Path(root)

    def save(self, session: PlayerStorySession) -> PlayerStorySession:
        path = self._path(session.world_id, session.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
        return session

    def load(self, world_id: str, session_id: str) -> PlayerStorySession:
        path = self._path(world_id, session_id)
        if not path.exists():
            raise FileNotFoundError(f"player session not found: {session_id}")
        return PlayerStorySession.model_validate_json(path.read_text(encoding="utf-8"))

    def exists(self, world_id: str, session_id: str) -> bool:
        return self._path(world_id, session_id).exists()

    def delete(self, world_id: str, session_id: str) -> None:
        path = self._path(world_id, session_id)
        if path.exists():
            path.unlink()

    def _path(self, world_id: str, session_id: str) -> Path:
        return self.root / _safe_id(world_id) / f"{_safe_id(session_id)}.json"


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(value or "").strip()).strip("_")
    if not normalized:
        raise ValueError("world/session id is empty")
    return normalized[:160]
