import json
import os
from pathlib import Path
from typing import Protocol

from app.core.models import MemoryItem


class MemoryStore(Protocol):
    def add(self, item: MemoryItem) -> None:
        ...

    def retrieve(self, query: str, limit: int = 8) -> list[MemoryItem]:
        ...

    def list_recent(self, limit: int = 30) -> list[MemoryItem]:
        ...


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self.items: list[MemoryItem] = []

    def add(self, item: MemoryItem) -> None:
        if any(existing.id == item.id for existing in self.items):
            return
        self.items.append(item)
        self.items = self.items[-500:]

    def retrieve(self, query: str, limit: int = 8) -> list[MemoryItem]:
        query_chars = set(query.lower())

        def score(item: MemoryItem) -> float:
            return len(query_chars & set(item.content.lower())) + item.importance

        return sorted(self.items, key=score, reverse=True)[:limit]

    def list_recent(self, limit: int = 30) -> list[MemoryItem]:
        return self.items[-limit:]


class JsonFileMemoryStore(InMemoryMemoryStore):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__()
        self._load()

    def add(self, item: MemoryItem) -> None:
        before = len(self.items)
        super().add(item)
        if len(self.items) != before:
            self._save()

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.items = [MemoryItem(**item) for item in data]

    def _save(self) -> None:
        data = [
            {
                "id": item.id,
                "timestamp": item.timestamp,
                "content": item.content,
                "importance": item.importance,
            }
            for item in self.items
        ]
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def create_memory_store(world_id: str, root: Path | None = None) -> MemoryStore:
    root = root or Path("data") / "memory"
    memory_provider = (os.getenv("MEMORY_PROVIDER") or "json_vector").lower()
    if memory_provider == "json_vector":
        from app.core.vector_memory import JsonVectorMemoryStore

        return JsonVectorMemoryStore(root / f"{world_id}.vector.json")
    return JsonFileMemoryStore(root / f"{world_id}.json")
