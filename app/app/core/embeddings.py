import hashlib
import math
import os
from typing import Protocol

from dotenv import load_dotenv
from langchain_openai import OpenAIEmbeddings

load_dotenv()


class EmbeddingClient(Protocol):
    def embed_query(self, text: str) -> list[float]:
        ...


class OpenAICompatibleEmbeddingClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        api_key = api_key or os.getenv("EMBEDDING_API_KEY") or os.getenv("LLM_API_KEY")
        base_url = base_url or os.getenv("EMBEDDING_BASE_URL") or os.getenv("LLM_BASE_URL")
        model = model or os.getenv("EMBEDDING_MODEL") or "text-embedding-3-small"

        if not api_key:
            raise RuntimeError("Missing EMBEDDING_API_KEY or LLM_API_KEY. Set it in .env.")

        self.embeddings = OpenAIEmbeddings(
            model=model,
            api_key=api_key,
            base_url=base_url,
        )

    def embed_query(self, text: str) -> list[float]:
        return self.embeddings.embed_query(text)


class HashEmbeddingClient:
    """
    Deterministic local fallback embedding.

    This is not semantically strong, but it keeps the vector memory pipeline
    runnable when an embedding provider is unavailable.
    """

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions

    def embed_query(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = self._tokens(text)
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[idx] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def _tokens(self, text: str) -> list[str]:
        normalized = text.lower().strip()
        chunks = normalized.split()
        if chunks:
            return chunks
        return [normalized[i : i + 2] for i in range(max(1, len(normalized) - 1))]
