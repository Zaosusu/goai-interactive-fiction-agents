from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field


class ImageGenerationProviderConfig(BaseModel):
    provider: str = "stepfun"
    api_base_url: str = "https://api.stepfun.com/step_plan/v1"
    model: str = "step-image-edit-2"
    size: str = "1024x1024"
    steps: int | None = 8
    cfg_scale: float | None = 1.0
    seed: int | None = None
    text_mode: bool | None = None
    response_format: str = "b64_json"
    api_key: str = ""
    api_key_env: str = "STEPFUN_API_KEY"
    api_key_file: str = "~/.stepfun-img/secret.json"
    retry_count: int = 3
    retryable_error_fragments: list[str] = Field(
        default_factory=lambda: [
            "Selected model is at capacity",
            "at capacity",
            "rate limit",
            "temporarily unavailable",
        ]
    )
    extra_body: dict[str, Any] = Field(default_factory=dict)


class ImageGenerationRequest(BaseModel):
    prompt: str
    output_path: str
    negative_prompt: str = ""
    size: str = ""
    seed: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ImageGenerationResponse(BaseModel):
    output_path: str
    provider: str
    model: str
    status: str = "generated"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ImageGenerationClient(Protocol):
    name: str

    def generate(
        self,
        request: ImageGenerationRequest,
        config: ImageGenerationProviderConfig,
    ) -> ImageGenerationResponse:
        ...


class OpenAICompatibleImageGenerationClient:
    def __init__(self, name: str = "openai_compatible") -> None:
        self.name = name

    def generate(
        self,
        request: ImageGenerationRequest,
        config: ImageGenerationProviderConfig,
    ) -> ImageGenerationResponse:
        try:
            from openai import OpenAI
            import httpx
        except ImportError as exc:
            raise RuntimeError("openai package is required for image generation providers") from exc

        api_key = resolve_image_api_key(config)
        if not api_key:
            raise RuntimeError(f"Missing API key for image provider: {config.provider}")

        disable_proxy_env()
        client = OpenAI(
            api_key=api_key,
            base_url=config.api_base_url,
            http_client=httpx.Client(trust_env=False),
        )
        extra_body = dict(config.extra_body or {})
        if config.steps is not None:
            extra_body["steps"] = config.steps
        if config.cfg_scale is not None:
            extra_body["cfg_scale"] = config.cfg_scale
        seed = request.seed if request.seed is not None else config.seed
        if seed is not None:
            extra_body["seed"] = seed
        if config.text_mode is not None:
            extra_body["text_mode"] = config.text_mode
        if request.negative_prompt:
            extra_body["negative_prompt"] = request.negative_prompt

        response = client.images.generate(
            model=config.model,
            prompt=request.prompt,
            size=request.size or config.size,
            n=1,
            response_format=config.response_format,
            extra_body=extra_body,
        )
        if not response.data:
            raise RuntimeError("image provider returned no image data")

        item = response.data[0]
        out_path = Path(request.output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if config.response_format == "b64_json" and item.b64_json:
            out_path.write_bytes(base64.b64decode(item.b64_json))
        elif item.url:
            urllib.request.urlretrieve(item.url, out_path)
        elif item.b64_json:
            out_path.write_bytes(base64.b64decode(item.b64_json))
        else:
            raise RuntimeError("image provider returned no downloadable URL or b64_json")

        return ImageGenerationResponse(
            output_path=str(out_path),
            provider=config.provider,
            model=config.model,
            metadata=dict(request.metadata or {}),
        )


def generate_with_retry(
    client: ImageGenerationClient,
    request: ImageGenerationRequest,
    config: ImageGenerationProviderConfig,
) -> ImageGenerationResponse:
    attempts = max(1, config.retry_count)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return client.generate(request, config)
        except Exception as exc:
            last_error = exc
            message = str(exc)
            retryable = any(fragment.lower() in message.lower() for fragment in config.retryable_error_fragments)
            if attempt >= attempts or not retryable:
                break
            time.sleep(min(2 * attempt, 8))
    assert last_error is not None
    raise last_error


def resolve_image_api_key(config: ImageGenerationProviderConfig) -> str:
    if config.api_key:
        return config.api_key
    if config.api_key_env:
        value = os.environ.get(config.api_key_env, "")
        if value:
            return value
    path = Path(config.api_key_file).expanduser() if config.api_key_file else None
    if path and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return str(data.get("apiKey") or data.get("api_key") or "")
        except Exception:
            return ""
    return ""


def disable_proxy_env() -> None:
    for key in ["ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"]:
        os.environ[key] = ""
    os.environ["NO_PROXY"] = "*"
