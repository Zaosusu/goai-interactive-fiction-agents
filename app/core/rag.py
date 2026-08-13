import math
from dataclasses import dataclass

from app.core.memory import MemoryStore
from app.core.models import MemoryItem, RagContext, RagDocument


@dataclass
class CorrectiveRagConfig:
    initial_k: int = 8
    retry_k: int = 8
    min_relevant: int = 1
    relevant_threshold: float = 0.12
    weak_threshold: float = 0.08


class CorrectiveRagPipeline:
    """
    Lightweight CRAG:
    1. Retrieve memories.
    2. Grade relevance with lexical overlap.
    3. If no reliable document, rewrite query with domain hints and retrieve again.
    4. Return a RagContext that the adapter can inject into prompt.
    """

    def __init__(self, memory_store: MemoryStore, config: CorrectiveRagConfig | None = None) -> None:
        self.memory_store = memory_store
        self.config = config or CorrectiveRagConfig()

    def run(self, query: str, hints: list[str] | None = None) -> RagContext:
        hints = hints or []
        first_docs = self._grade(query, self.memory_store.retrieve(query, self.config.initial_k))
        reliable = [doc for doc in first_docs if doc.verdict == "relevant"]
        if len(reliable) >= self.config.min_relevant:
            return RagContext(
                original_query=query,
                documents=first_docs,
                reliable=True,
                note="retrieved_relevant_memory",
            )

        rewritten_query = self._rewrite_query(query, hints)
        second_docs = self._grade(rewritten_query, self.memory_store.retrieve(rewritten_query, self.config.retry_k))
        reliable = [doc for doc in second_docs if doc.verdict == "relevant"]
        return RagContext(
            original_query=query,
            rewritten_query=rewritten_query,
            documents=second_docs,
            reliable=len(reliable) >= self.config.min_relevant,
            note="rewritten_query_retrieval" if reliable else "no_reliable_memory_found",
        )

    def _grade(self, query: str, memories: list[MemoryItem]) -> list[RagDocument]:
        seen = set()
        documents = []
        for memory in memories:
            normalized_content = memory.content.strip()
            if normalized_content in seen or self._is_low_quality(normalized_content):
                continue
            seen.add(normalized_content)
            score = self._relevance(query, normalized_content, memory.importance)
            documents.append(
                RagDocument(
                    id=memory.id,
                    content=memory.content,
                    importance=memory.importance,
                    relevance=score,
                    verdict=self._verdict(score),
                )
            )
        return documents

    def _relevance(self, query: str, content: str, importance: float) -> float:
        query_tokens = self._tokens(query)
        content_tokens = self._tokens(content)
        if not query_tokens or not content_tokens:
            return 0.0
        overlap = len(query_tokens & content_tokens)
        union = len(query_tokens | content_tokens)
        jaccard = overlap / union if union else 0.0
        char_overlap = len(set(query.lower()) & set(content.lower())) / max(1, len(set(query.lower())))
        return min(1.0, jaccard * 0.7 + char_overlap * 0.2 + importance * 0.1)

    def _verdict(self, score: float) -> str:
        if score >= self.config.relevant_threshold:
            return "relevant"
        if score >= self.config.weak_threshold:
            return "weak"
        return "irrelevant"

    def _rewrite_query(self, query: str, hints: list[str]) -> str:
        matched_hints = [hint for hint in hints if hint.lower() in query.lower()]
        hint_text = " ".join(matched_hints)
        return f"{query} {hint_text}".strip()

    def _tokens(self, text: str) -> set[str]:
        normalized = text.lower().strip()
        chunks = normalized.split()
        if chunks:
            return set(chunks)
        if len(normalized) <= 2:
            return {normalized} if normalized else set()
        return {normalized[i : i + 2] for i in range(len(normalized) - 1)}

    def _is_low_quality(self, text: str) -> bool:
        if not text:
            return True
        bad_chars = text.count("?") + text.count("\ufffd")
        return bad_chars / max(1, len(text)) > 0.15
