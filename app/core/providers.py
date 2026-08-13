import os

from app.core.llm import LLMClient, OpenAICompatibleLLMClient
from app.core.model_config import LLMProviderConfig


def create_npc_llm_client(config: LLMProviderConfig | None = None, provider: str | None = None) -> LLMClient:
    provider = (provider or (config.provider if config else None) or os.getenv("NPC_LLM_PROVIDER") or os.getenv("LLM_PROVIDER") or "openai_compatible").lower()

    if provider == "openai_compatible":
        return OpenAICompatibleLLMClient(config=config, purpose="npc")

    raise ValueError(f"Unknown NPC LLM provider: {provider}")


def create_world_builder_llm_client(config: LLMProviderConfig | None = None, provider: str | None = None) -> LLMClient:
    provider = (
        provider
        or (config.provider if config else None)
        or os.getenv("WORLD_BUILDER_LLM_PROVIDER")
        or os.getenv("LLM_PROVIDER")
        or "openai_compatible"
    ).lower()

    if provider == "openai_compatible":
        return OpenAICompatibleLLMClient(config=config, purpose="world_builder")

    raise ValueError(f"Unknown world builder LLM provider: {provider}")


def create_visual_prompt_llm_client(config: LLMProviderConfig | None = None, provider: str | None = None) -> LLMClient:
    provider = (
        provider
        or (config.provider if config else None)
        or os.getenv("VISUAL_PROMPT_LLM_PROVIDER")
        or os.getenv("LLM_PROVIDER")
        or "openai_compatible"
    ).lower()

    if provider == "openai_compatible":
        return OpenAICompatibleLLMClient(config=config, purpose="visual_prompt")

    raise ValueError(f"Unknown visual prompt LLM provider: {provider}")


def create_llm_client(provider: str | None = None) -> LLMClient:
    return create_npc_llm_client(provider=provider)
