from __future__ import annotations

import os
from collections.abc import Callable
from inspect import isawaitable
from typing import Protocol

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.core.model_config import LLMProviderConfig, resolve_llm_config

load_dotenv()


class TextGenerationClient(Protocol):
    async def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        on_token: Callable[[str], object] | None = None,
    ) -> str:
        ...


class OpenAICompatibleTextGenerationClient:
    def __init__(self, config: LLMProviderConfig | None = None, purpose: str = "visual_prompt") -> None:
        resolved = resolve_llm_config(config, purpose)
        if not resolved.api_key:
            raise RuntimeError(f"Missing {purpose.upper()} LLM API key.")
        self.model = resolved.model
        self.base_url = resolved.base_url
        self.llm = ChatOpenAI(
            model=resolved.model,
            api_key=resolved.api_key,
            base_url=resolved.base_url or None,
            temperature=resolved.temperature,
            timeout=resolved.timeout,
            max_retries=resolved.max_retries,
            streaming=True,
        )

    async def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        on_token: Callable[[str], object] | None = None,
    ) -> str:
        chunks: list[str] = []
        async for chunk in self.llm.astream([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]):
            content = str(chunk.content or "")
            if not content:
                continue
            chunks.append(content)
            if on_token:
                result = on_token(content)
                if isawaitable(result):
                    await result
        return "".join(chunks)
