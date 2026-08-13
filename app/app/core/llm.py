import json
import os
from typing import Any, Protocol

from dotenv import load_dotenv
from openai import BadRequestError
from openai import APITimeoutError
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from app.core.model_config import LLMProviderConfig, resolve_llm_config
from app.core.models import AgentLLMOutput
from app.core.protocol_tools import AgentLLMOutputProtocolTool

load_dotenv()


class LLMClient(Protocol):
    async def invoke(self, messages: list[Any], fallback_actions: list[str]) -> AgentLLMOutput:
        ...


class OpenAICompatibleLLMClient:
    """
    LLM provider for any OpenAI-compatible chat API.

    The provider is selected by configuration, not by Agent code. Any compatible
    gateway can use this implementation by changing:
    - LLM_API_KEY
    - LLM_BASE_URL
    - LLM_MODEL
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float = 0.75,
        config: LLMProviderConfig | None = None,
        purpose: str = "npc",
    ) -> None:
        resolved = resolve_llm_config(config, purpose)
        api_key = api_key or resolved.api_key
        base_url = base_url or resolved.base_url
        model = model or resolved.model
        temperature = temperature if config is None else resolved.temperature

        if not api_key:
            raise RuntimeError(f"Missing {purpose.upper()} LLM API key. Set {purpose.upper()}_LLM_API_KEY or LLM_API_KEY.")

        self.model = model
        self.base_url = base_url or ""
        self.llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            timeout=resolved.timeout,
            max_retries=resolved.max_retries,
            streaming=True,
        )
        self.structured_llm = self.llm.with_structured_output(AgentLLMOutput)
        self.protocol_tool = AgentLLMOutputProtocolTool()

    async def invoke(self, messages: list[Any], fallback_actions: list[str]) -> AgentLLMOutput:
        trace: list[dict[str, Any]] = []
        structured_messages = self._with_json_instruction(messages)
        try:
            raw = await self._astream_text(structured_messages)
            trace.append(self._trace_event("raw_json_prompt", ok=True))
            parsed = self.protocol_tool.repair_agent_output(raw, fallback_actions)
            self._attach_trace(parsed, trace, "protocol_tool_repair", ok=True)
            return parsed
        except APITimeoutError as exc:
            trace.append(self._trace_event("raw_json_prompt", exc))
            return self._timeout_output(fallback_actions, trace)
        except Exception as exc:
            trace.append(self._trace_event("raw_json_prompt", exc))
            try:
                raw = await self._astream_text(
                    structured_messages
                    + [
                        HumanMessage(
                            content=(
                                "上一次输出无法解析。请根据上面的系统设定、当前 NPC、玩家输入和世界状态，完成这一轮 NPC 回复。"
                                "只输出一个 JSON 对象，不要 Markdown，不要解释协议，不要说等待用户继续提供任务。字段为 "
                                "action_type, content, inner_thought, reasoning, plan, criticism, command, "
                                "emotion_delta, new_memories, goal_updates, quest_progress, suggested_actions。"
                            )
                        )
                    ]
                )
                trace.append(self._trace_event("raw_json_repair_prompt", ok=True))
            except APITimeoutError as exc:
                trace.append(self._trace_event("raw_json_repair_prompt", exc))
                return self._timeout_output(fallback_actions, trace)
            except Exception as exc:
                trace.append(self._trace_event("raw_json_repair_prompt", exc))
                return self._protocol_failure_output(fallback_actions, trace, "provider_repair_request_failed")
            parsed = self.protocol_tool.repair_agent_output(raw, fallback_actions)
            self._attach_trace(parsed, trace, "protocol_tool_repair", ok=True)
            return parsed

    async def _astream_text(self, messages: list[Any]) -> str:
        chunks: list[str] = []
        async for chunk in self.llm.astream(messages):
            content = str(chunk.content or "")
            if content:
                chunks.append(content)
        return "".join(chunks)

    def _timeout_output(self, fallback_actions: list[str], trace: list[dict[str, Any]] | None = None) -> AgentLLMOutput:
        return AgentLLMOutput(
            action_type="wait",
            content="我这边思绪断了一下，刚才没有及时回应。你可以重发上一句话，或先换个问法继续。",
            inner_thought="LLM provider request timed out; returned controlled wait response instead of failing the API.",
            reasoning="外部模型请求超时，不能可靠推进剧情或修改状态。",
            plan=["保持当前世界状态不变", "提示玩家重试"],
            criticism="没有执行任何 command，避免超时后误改状态。",
            command={"name": "none", "args": {}},
            quest_progress="模型响应超时，世界状态未变化。",
            suggested_actions=fallback_actions,
            provider_error={
                "type": "timeout",
                "message": "LLM provider request timed out.",
            },
            provider_trace=trace or [],
        )

    def _protocol_failure_output(self, fallback_actions: list[str], trace: list[dict[str, Any]], error_type: str) -> AgentLLMOutput:
        return AgentLLMOutput(
            action_type="wait",
            content="我刚才没有组织好回应。请你重说一遍，我会按当前世界状态继续。",
            inner_thought="Model failed to return the required structured NPC JSON. Refused to treat raw text as gameplay output.",
            reasoning="非结构化输出不能进入剧情，避免自然语言绕过状态协议。",
            plan=["保持状态不变", "请求玩家重试"],
            criticism="未执行 command，未写任务进度。",
            command={"name": "none", "args": {}},
            suggested_actions=fallback_actions,
            provider_error={
                "type": error_type,
                "message": "LLM provider output could not be converted into AgentLLMOutput.",
            },
            provider_trace=trace,
        )

    def _attach_trace(self, output: AgentLLMOutput, trace: list[dict[str, Any]], stage: str, ok: bool) -> None:
        output.provider_trace = [*trace, self._trace_event(stage, ok=ok)]
        if not getattr(output, "provider", None):
            output.provider = {"model": self.model, "base_url": self.base_url}

    def _trace_event(self, stage: str, exc: Exception | None = None, ok: bool = False) -> dict[str, Any]:
        event = {
            "stage": stage,
            "ok": ok and exc is None,
            "model": self.model,
            "base_url": self.base_url,
        }
        if exc is not None:
            event.update(
                {
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:500],
                }
            )
            if isinstance(exc, BadRequestError):
                event["status_code"] = getattr(exc, "status_code", None)
        return event

    def _with_json_instruction(self, messages: list[Any]) -> list[Any]:
        marker = HumanMessage(
            content=(
                "Protocol requirement: return only one valid JSON object for AgentLLMOutput. "
                "Do not wrap it in Markdown. Do not explain this protocol. "
                "Use the current NPC voice in content. Required fields: "
                "action_type, content, inner_thought, reasoning, plan, criticism, command, "
                "emotion_delta, new_memories, goal_updates, quest_progress, suggested_actions. "
                'If no world command is needed, use {"name":"none","args":{}} for command. '
                "The word json is intentionally present for providers that require it."
            )
        )
        return [*messages, marker]

    def _parse_raw_output(self, content: str) -> AgentLLMOutput | None:
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()

        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]

        try:
            data = json.loads(text)
            action_type = str(data.get("action_type", "say")).lower()
            if action_type in {"speak", "talk", "reply", "dialogue", "dialog", "npc_response", "response"}:
                data["action_type"] = "say"
            elif action_type not in {"say", "ask", "emote", "refuse", "hint", "trade", "quest", "wait"}:
                data["action_type"] = "say"
            data["content"] = str(data.get("content") or data.get("reply") or data.get("speak") or "")
            data["inner_thought"] = str(data.get("inner_thought") or data.get("thought") or "")
            data["reasoning"] = str(data.get("reasoning") or "")
            data["criticism"] = str(data.get("criticism") or "")
            data["emotion_delta"] = self._coerce_emotion_delta(data.get("emotion_delta", {}))
            data["new_memories"] = self._coerce_string_list(data.get("new_memories", []))
            data["goal_updates"] = self._coerce_string_list(data.get("goal_updates", []))
            data["suggested_actions"] = self._coerce_string_list(data.get("suggested_actions", []))
            data["plan"] = self._coerce_string_list(data.get("plan", []))
            if not isinstance(data.get("command"), dict) or not data.get("command"):
                data["command"] = {"name": "none", "args": {}}
            else:
                data["command"].setdefault("args", {})
            return AgentLLMOutput.model_validate(data)
        except (ValidationError, json.JSONDecodeError, TypeError):
            return None

    def _coerce_emotion_delta(self, value: Any) -> dict[str, float]:
        allowed = {"trust", "fear", "anger", "respect", "joy", "anticipation"}
        if not isinstance(value, dict):
            return {}
        result: dict[str, float] = {}
        for key, raw in value.items():
            if key not in allowed:
                continue
            try:
                result[key] = float(raw)
            except (TypeError, ValueError):
                continue
        return result

    def _coerce_string_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list):
            value = [value]

        result = []
        for item in value:
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("memory") or json.dumps(item, ensure_ascii=False)
            else:
                text = str(item)
            text = text.strip()
            if text:
                result.append(text)
        return result
