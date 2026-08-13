import json
import math
from dataclasses import dataclass
from pathlib import Path

from app.core.embeddings import EmbeddingClient, HashEmbeddingClient, OpenAICompatibleEmbeddingClient
from app.core.memory import MemoryStore
from app.core.models import MemoryItem


@dataclass
class VectorMemoryRecord:
    item: MemoryItem
    embedding: list[float]


class JsonVectorMemoryStore(MemoryStore):
    def __init__(
        self,
        path: Path,
        embedding_client: EmbeddingClient | None = None,
        max_items: int = 1000,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding_client = embedding_client or self._create_embedding_client()
        self.max_items = max_items
        self.records: list[VectorMemoryRecord] = []
        self._load()

    def add(self, item: MemoryItem) -> None:
        if any(record.item.id == item.id or record.item.content == item.content for record in self.records):
            return
        self.records.append(VectorMemoryRecord(item=item, embedding=self._embed(item.content)))
        self.records = self.records[-self.max_items :]
        self._save()

    def retrieve(self, query: str, limit: int = 8) -> list[MemoryItem]:
        if not self.records:
            return []
        query_embedding = self._embed(query)
        scored = []
        for record in self.records:
            score = self._cosine(query_embedding, record.embedding) + record.item.importance * 0.08
            scored.append((score, record.item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:limit]]

    def list_recent(self, limit: int = 30) -> list[MemoryItem]:
        return [record.item for record in self.records[-limit:]]

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.records = [
            VectorMemoryRecord(
                item=MemoryItem(**record["item"]),
                embedding=record["embedding"],
            )
            for record in data
        ]

    def _save(self) -> None:
        data = [
            {
                "item": {
                    "id": record.item.id,
                    "timestamp": record.item.timestamp,
                    "content": record.item.content,
                    "importance": record.item.importance,
                },
                "embedding": record.embedding,
            }
            for record in self.records
        ]
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _create_embedding_client(self) -> EmbeddingClient:
        provider = self.path_provider()
        if provider == "hash":
            return HashEmbeddingClient()
        return OpenAICompatibleEmbeddingClient()

    def _embed(self, text: str) -> list[float]:
        try:
            return self.embedding_client.embed_query(text)
        except Exception:
            self.embedding_client = HashEmbeddingClient()
            return self.embedding_client.embed_query(text)

    def path_provider(self) -> str:
        import os

        return (os.getenv("EMBEDDING_PROVIDER") or "openai_compatible").lower()

    def _cosine(self, left: list[float], right: list[float]) -> float:
        size = min(len(left), len(right))
        if size == 0:
            return 0.0
        dot = sum(left[i] * right[i] for i in range(size))
        left_norm = math.sqrt(sum(value * value for value in left[:size]))
        right_norm = math.sqrt(sum(value * value for value in right[:size]))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return dot / (left_norm * right_norm)
