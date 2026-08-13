from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from app.agents.creator_assistant.compiler import CreatorGraphCompiler
from app.agents.creator_assistant.schema import (
    CreatorAssistantOperation,
    CreatorAssistantRequest,
    CreatorAssistantResponse,
    CreatorToolCall,
)
from app.core.text_generation import OpenAICompatibleTextGenerationClient, TextGenerationClient


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.agents.creator_assistant.tools import CreatorToolRegistry


class CreatorAssistantAgent:
    """Analyzes a Creator graph and proposes strictly validated edit operations."""

    def __init__(self, text_client: TextGenerationClient | None = None, tool_registry: "CreatorToolRegistry | None" = None) -> None:
        if tool_registry is None:
            from app.agents.creator_assistant.tools import CreatorToolRegistry

            tool_registry = CreatorToolRegistry()
        self.text_client = text_client
        self.tool_registry = tool_registry
        self.compiler = CreatorGraphCompiler()
        self.last_raw = ""
        self.last_error = ""

    async def edit(self, request: CreatorAssistantRequest) -> CreatorAssistantResponse:
        self.last_raw = ""
        self.last_error = ""
        deterministic = _deterministic_layout_response(request)
        if deterministic is not None:
            return deterministic
        fallback = self._fallback_response(request)
        try:
            client = self.text_client or OpenAICompatibleTextGenerationClient(request.creator_llm, purpose="creator_assistant")
            raw = await client.generate_text(self._system_prompt(), self._user_prompt(request))
            self.last_raw = raw
            try:
                response = self._parse_and_normalize_response(raw, fallback, source="llm")
            except (ValueError, ValidationError) as exc:
                repair_reason = f"Output failed Creator Conversation protocol validation: {type(exc).__name__}: {exc}"
                logger.warning("CreatorAssistantAgent repairing invalid protocol output: %s", repair_reason)
                raw = await client.generate_text(
                    self._system_prompt(),
                    self._repair_user_prompt(request, raw, repair_reason),
                )
                self.last_raw = raw
                response = self._parse_and_normalize_response(raw, fallback, source="llm_repair")
            if _misroutes_existing_story_expansion(request.message, response.tool_calls):
                repair_reason = (
                    "This request extends the current graph. author_story replaces the whole project and is forbidden. "
                    "Route to expand_story with the exact requested node count and selected source node."
                )
                raw = await client.generate_text(
                    self._system_prompt(),
                    self._repair_user_prompt(request, raw, repair_reason),
                )
                self.last_raw = raw
                response = self._parse_and_normalize_response(raw, fallback, source="llm_repair")
                if _misroutes_existing_story_expansion(request.message, response.tool_calls):
                    raise ValueError("creator_assistant_repair_still_replaced_existing_story")
            if _requires_clarification(request.message) and (response.operations or response.tool_calls):
                repair_reason = (
                    "The creator expressed only general dissatisfaction and did not identify what should change. "
                    "Do not infer edits. Return intent=clarify with one concise question and empty operations/tool_calls."
                )
                raw = await client.generate_text(
                    self._system_prompt(),
                    self._repair_user_prompt(request, raw, repair_reason),
                )
                self.last_raw = raw
                response = self._parse_and_normalize_response(raw, fallback, source="llm_repair")
                if response.intent != "clarify" or response.operations or response.tool_calls:
                    raise ValueError("creator_assistant_repair_still_modified_ambiguous_request")
            if not response.operations and not response.tool_calls:
                if response.intent not in {"chat", "clarify"}:
                    raise ValueError("creator_assistant_returned_no_operations_or_tool_calls")
                return response.model_copy(update={"requires_confirmation": False})
            fallback_operations = [] if response.tool_calls else fallback.operations
            merged_operations = _merge_operations(response.operations, fallback_operations, request.message)
            if len(merged_operations) > len(response.operations):
                response = response.model_copy(
                    update={
                        "operations": merged_operations,
                        "summary": _unique_strings([*response.summary, *fallback.summary]),
                        "source": "llm+fallback",
                    }
                )
            merged_tool_calls = _merge_tool_calls(response.tool_calls, fallback.tool_calls)
            if len(merged_tool_calls) > len(response.tool_calls):
                response = response.model_copy(
                    update={
                        "tool_calls": merged_tool_calls,
                        "summary": _unique_strings([*response.summary, *fallback.summary]),
                        "source": "llm+fallback",
                    }
                )
            response = _sanitize_response_for_tools(response)
            response = _ensure_workflow_dependencies(response)
            final_intent = "workflow" if response.tool_calls else "graph_edit"
            return response.model_copy(
                update={
                    "intent": final_intent,
                    "route": _route_for_intent(final_intent),
                    "requires_confirmation": True,
                }
            )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("CreatorAssistantAgent degraded to fallback: %s", self.last_error)
            return CreatorAssistantResponse(
                reply="Creator Agent 没有生成可安全执行的方案。本次没有修改项目，请查看错误原因后重试。",
                operations=[],
                tool_calls=[],
                summary=["没有执行本地猜测性修改，也没有调用下游工具。"],
                intent="error",
                route="creator_conversation_agent",
                requires_confirmation=False,
                source="fallback_error",
                raw_excerpt=self.last_error,
            )

    def _parse_and_normalize_response(
        self,
        raw: str,
        fallback: CreatorAssistantResponse,
        *,
        source: str,
    ) -> CreatorAssistantResponse:
        response = self._parse_response(raw, fallback, source=source)
        return response.model_copy(update={"tool_calls": self.tool_registry.normalize_calls(response.tool_calls)})

    def _parse_response(
        self,
        raw: str,
        fallback: CreatorAssistantResponse,
        *,
        source: str,
    ) -> CreatorAssistantResponse:
        payload = _extract_json_object(raw)
        if payload is None:
            raise ValueError("creator_assistant_returned_non_json")
        operations = payload.get("operations") or []
        tool_calls = payload.get("tool_calls") or []
        intent = _normalize_intent(payload.get("intent"), operations, tool_calls)
        route = str(payload.get("route") or _route_for_intent(intent)).strip()
        return CreatorAssistantResponse.model_validate(
            {
                "reply": payload.get("reply") or fallback.reply,
                "operations": operations,
                "tool_calls": tool_calls,
                "summary": payload.get("summary") or [],
                "intent": intent,
                "route": route,
                "requires_confirmation": bool(operations or tool_calls),
                "source": source,
                "raw_excerpt": raw[:1200],
            }
        )

    def _repair_user_prompt(self, request: CreatorAssistantRequest, raw: str, reason: str) -> str:
        return self._user_prompt(request) + "\n\n" + json.dumps(
            {
                "previous_response": raw[:8000],
                "validation_error": reason,
                "instruction": "Return one corrected JSON object only. Do not explain the correction.",
            },
            ensure_ascii=False,
            indent=2,
        )

    def _fallback_response(self, request: CreatorAssistantRequest) -> CreatorAssistantResponse:
        message = request.message.strip()
        operations: list[CreatorAssistantOperation] = []
        tool_calls = _fallback_tool_calls(message, selected_node_id=request.selected_node_id)
        summary: list[str] = []
        selected_node_id = request.selected_node_id or _first_node_id(request.project)

        branch_description = _branch_description(message)
        if branch_description and selected_node_id:
            source = _node_by_id(request.project, selected_node_id)
            reconnect = str(source.get("next") or "") if source else ""
            title = _short_title(branch_description)
            operations.append(
                CreatorAssistantOperation(
                    type="create_branch",
                    data={
                        "source_node_id": selected_node_id,
                        "choice_text": f"进入支线：{title}",
                        "nodes": [
                            {
                                "type": "story",
                                "title": title,
                                "content": branch_description,
                                "character": str(source.get("character") or "") if source else "",
                            }
                        ],
                        "reconnect_node_id": reconnect,
                    },
                )
            )
            summary.append(f"从节点 {selected_node_id} 创建支线「{title}」。")

        world_name = _match_after(message, ["世界名", "世界名称", "项目名"])
        if world_name:
            operations.append(CreatorAssistantOperation(type="set_world", data={"name": world_name}))
            summary.append(f"世界名称改为「{world_name}」。")

        lore = _match_after(message, ["世界观", "设定", "背景"])
        if lore:
            operations.append(CreatorAssistantOperation(type="set_world", data={"lore": lore}))
            summary.append("更新世界设定。")

        for stat_name, value in _parse_stats(message).items():
            operations.append(CreatorAssistantOperation(type="set_player_stat", target_id=stat_name, data={"value": value}))
            summary.append(f"玩家属性 {stat_name} = {value}。")

        for item_name in _parse_items(message):
            operations.append(CreatorAssistantOperation(type="add_item", data={"name": item_name, "quantity": 1}))
            summary.append(f"新增道具「{item_name}」。")

        character_name = _parse_character_name(message)
        if character_name:
            operations.append(
                CreatorAssistantOperation(
                    type="add_character",
                    data={"name": character_name, "role": "NPC", "location": _player_location(request.project)},
                )
            )
            summary.append(f"新增角色「{character_name}」。")

        if not branch_description:
            node_title = _match_command_after(message, ["新增节点", "添加节点", "新增剧情"])
            if node_title:
                operations.append(
                    CreatorAssistantOperation(
                        type="add_node",
                        data={"type": "story", "title": node_title, "content": node_title, "after": selected_node_id},
                    )
                )
                summary.append(f"新增剧情节点「{node_title}」。")

            choice_text = _match_command_after(message, ["新增选项", "添加选项"])
            if selected_node_id and choice_text:
                operations.append(CreatorAssistantOperation(type="add_choice", target_id=selected_node_id, data={"text": choice_text}))
                summary.append(f"给节点 {selected_node_id} 新增选项「{choice_text}」。")

        if selected_node_id and any(token in message for token in ["当前节点", "这个节点", "选中节点"]):
            node_data: dict[str, Any] = {}
            title = _match_after(message, ["标题"])
            content = _match_after(message, ["正文", "内容", "文本"])
            if title:
                node_data["title"] = title
            if content:
                node_data["content"] = content
            if node_data:
                operations.append(CreatorAssistantOperation(type="update_node", target_id=selected_node_id, data=node_data))
                summary.append("更新当前节点。")

        if not operations and not tool_calls:
            operations.append(
                CreatorAssistantOperation(
                    type="add_node",
                    data={"type": "story", "title": "新的剧情节点", "content": message, "after": selected_node_id},
                )
            )
            summary.append("将描述整理为一个新的剧情节点。")

        if any(call.tool == "author_story" for call in tool_calls):
            reply = "已识别为全新完整剧情：将调用 StoryAuthoringAgent 生成可玩故事，并在完成后保存为当前项目草稿。"
            if not summary:
                summary = ["生成完整剧情并保存草稿，不混入旧剧情节点修改。"]
        elif tool_calls:
            reply = "已按你的目标选择对应 Agent 与 Pipeline 工具，请确认执行顺序。"
        else:
            reply = "我已经把你的要求整理为可预览、可确认的 Creator 图修改。"

        return CreatorAssistantResponse(
            reply=reply,
            operations=operations,
            tool_calls=tool_calls,
            summary=summary,
            intent="workflow" if tool_calls else "graph_edit",
            route="router_agent" if tool_calls else "creator_graph",
            requires_confirmation=True,
            source="fallback",
        )

    def _system_prompt(self) -> str:
        return """You are CreatorAssistantAgent inside an interactive-story graph editor and AI-native content platform.
Analyze the entire supplied graph before proposing edits. Diagnose broken branches, unreachable nodes,
dangling links, weak endings, missing choices, inconsistent characters, conditions, effects and pacing.
Return JSON only. Never return the full project. You may return graph edit operations, Pipeline tool calls, or both.

Output:
{
  "intent": "chat | clarify | graph_edit | workflow",
  "route": "creator_conversation_agent | creator_graph | router_agent",
  "reply": "short response for the creator",
  "summary": ["human-readable change summary"],
  "tool_calls": [
    {"tool":"author_story","arguments":{"brief":"...","genre":"修仙剧情冒险","target_minutes":30,"target_scene_count":6,"target_character_count":4,"constraints":[]},"reason":"..."},
    {"tool":"expand_story","arguments":{"brief":"...","target_node_count":50,"source_node_id":"selected_node_id","insertion_mode":"after"},"reason":"..."},
    {"tool":"layout_creator_graph","arguments":{"scope":"downstream","root_node_id":"selected_node_id"},"reason":"..."},
    {"tool":"plan_visual_assets","arguments":{"include_characters":true,"include_scenes":true},"reason":"..."}
  ],
  "operations": [
    {"type":"set_world","data":{"name":"...","lore":"..."}},
    {"type":"set_player_stat","target_id":"money","data":{"value":100}},
    {"type":"add_item","data":{"name":"key","quantity":1}},
    {"type":"add_character","data":{"id":"npc_id","name":"...","role":"NPC","personality":"...","location":"..."}},
    {"type":"update_character","target_id":"npc_id","data":{"personality":"..."}},
    {"type":"delete_character","target_id":"npc_id","data":{}},
    {"type":"add_node","data":{"id":"node_id","type":"story","title":"...","content":"...","after":"source_id","next":"target_id"}},
    {"type":"update_node","target_id":"node_id","data":{"title":"...","content":"...","conditions":{},"effects":{},"next":"..."}},
    {"type":"delete_node","target_id":"node_id","data":{}},
    {"type":"add_choice","target_id":"node_id","data":{"id":"choice_id","text":"...","next":"...","conditions":{},"effects":{}}},
    {"type":"update_choice","target_id":"node_id","data":{"choice_id":"choice_id","text":"...","next":"..."}},
    {"type":"delete_choice","target_id":"node_id","data":{"choice_id":"choice_id"}},
    {"type":"connect_nodes","target_id":"source_id","data":{"target_id":"target_id","choice_id":"optional_choice_id"}},
    {"type":"disconnect_nodes","target_id":"source_id","data":{"target_id":"old_target_id","choice_id":"optional_choice_id"}},
    {"type":"create_branch","data":{"source_node_id":"source_id","choice_text":"...","nodes":[{"id":"branch_1","type":"story","title":"...","content":"..."}],"reconnect_node_id":"optional_target"}}
  ]
}

Rules:
- You are the Creator Conversation Agent. First infer intent from the user's message and recent history.
- For ordinary conversation, questions about the project, explanations, or brainstorming with no requested mutation: intent=chat, route=creator_conversation_agent, and return empty operations/tool_calls.
- If a required creative choice is genuinely missing and different choices materially change the result: intent=clarify, ask one concise question, and return empty operations/tool_calls.
- A vague request such as "这个故事还不够好，帮我处理一下" is always clarify. Do not diagnose and edit until the creator chooses what to improve (for example pacing, characters, branches, dialogue, or endings).
- For deterministic edits to the current project: intent=graph_edit and route=creator_graph.
- For creation, generation, validation, visual assets, saving, publishing, or other Agent capabilities: intent=workflow and route=router_agent.
- For arranging, tidying, aligning, or auto-layout of graph nodes, call layout_creator_graph. Use scope=downstream with selected_node_id for "当前/选中节点", otherwise scope=all.
- Never claim an action ran during preview. Explain what will happen and wait for confirmation.
- For a requested side branch, prefer one create_branch operation. Include every branch node in story order.
- Use only existing ids from the graph unless creating a new entity.
- Never leave dangling next references.
- New non-ending branches should reconnect to an existing node unless the creator explicitly requests a new ending.
- Conditions and effects must be JSON objects.
- Operations must satisfy the exact schemas above; do not add fields.
- If asked to debug/fix the story, use graph_analysis issues and repair the most important structural problems.
- Use graph operations for small edits to the current project.
- Use author_story for a new complete playable story, not dozens of add_node operations.
- Never use author_story when the creator asks to extend, continue, insert into, or connect new content to the current story.
- For a request to add or expand many nodes in the current story, route to expand_story. Do not return dozens of add_node operations and do not use author_story.
- Use explicit ordered tool calls for production stages. Never imply that an unlisted downstream stage will run.
- author_story replaces the Creator project and must be followed by save_world, unless the workflow already ends with publish_to_play.
- Visual generation order is plan_visual_assets, generate_visual_assets, bind_visual_assets, then save_world or publish_to_play when requested.
- Publishing requires publish_to_play. Saving alone does not navigate the browser.

Available Pipeline tools (their JSON schemas are authoritative):
""" + json.dumps(self.tool_registry.prompt_catalog(), ensure_ascii=False, indent=2)

    def _user_prompt(self, request: CreatorAssistantRequest) -> str:
        normalized = self.compiler.normalize(request.project)
        report = self.compiler.validate(normalized)
        return json.dumps(
            {
                "creator_request": request.message,
                "selected_node_id": request.selected_node_id,
                "recent_history": request.history[-8:],
                "graph_analysis": report.model_dump(mode="json"),
                "project": _project_context(normalized, request.selected_node_id),
            },
            ensure_ascii=False,
            indent=2,
        )


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _misroutes_existing_story_expansion(message: str, calls: list[CreatorToolCall]) -> bool:
    if not any(call.tool == "author_story" for call in calls):
        return False
    normalized = re.sub(r"\s+", "", message).lower()
    expansion_markers = ["节点", "支线", "扩写", "续写", "延长", "串联", "接到", "接入", "当前故事", "这个故事"]
    replacement_markers = ["全新", "重新创作", "重写整个", "替换整个", "新项目"]
    return any(marker in normalized for marker in expansion_markers) and not any(
        marker in normalized for marker in replacement_markers
    )


def _requires_clarification(message: str) -> bool:
    normalized = re.sub(r"[\s，。！？、,.!?;；:：]", "", message).lower()
    vague_markers = ["不够好", "不太好", "不好", "不太行", "有问题", "处理一下", "优化一下", "完善一下", "改进一下"]
    concrete_markers = [
        "节点", "支线", "结局", "角色", "人物", "台词", "对白", "节奏", "开场", "场景", "选择", "线索",
        "美术", "立绘", "背景", "发布", "保存", "生成", "增加", "新增", "删除", "修改", "改成", "名字",
        "世界观", "抠图", "试玩", "修复", "扩写", "续写", "串联",
    ]
    return any(marker in normalized for marker in vague_markers) and not any(
        marker in normalized for marker in concrete_markers
    )


def _normalize_intent(value: Any, operations: list[Any], tool_calls: list[Any]) -> str:
    normalized = str(value or "").strip().lower()
    aliases = {
        "conversation": "chat",
        "question": "chat",
        "ask": "clarify",
        "edit": "graph_edit",
        "tool": "workflow",
        "tools": "workflow",
        "router": "workflow",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in {"chat", "clarify", "graph_edit", "workflow"}:
        return normalized
    if tool_calls:
        return "workflow"
    if operations:
        return "graph_edit"
    return "chat"


def _route_for_intent(intent: str) -> str:
    if intent == "workflow":
        return "router_agent"
    if intent == "graph_edit":
        return "creator_graph"
    return "creator_conversation_agent"


def _merge_operations(
    primary: list[CreatorAssistantOperation],
    fallback: list[CreatorAssistantOperation],
    message: str,
) -> list[CreatorAssistantOperation]:
    merged = list(primary)
    seen = {_operation_key(item) for item in merged}
    primary_has_branch = any(item.type == "create_branch" for item in primary)
    request_needs_branch = bool(_branch_description(message))
    for operation in fallback:
        if _is_generic_fallback(operation) and primary:
            continue
        if operation.type == "create_branch" and (primary_has_branch or not request_needs_branch):
            continue
        key = _operation_key(operation)
        if key in seen:
            continue
        merged.append(operation)
        seen.add(key)
    return merged


def _merge_tool_calls(primary: list[CreatorToolCall], fallback: list[CreatorToolCall]) -> list[CreatorToolCall]:
    merged = list(primary)
    seen = {item.tool for item in merged}
    for call in fallback:
        if call.tool not in seen:
            merged.append(call)
            seen.add(call.tool)
    return merged


def _fallback_tool_calls(message: str, selected_node_id: str = "") -> list[CreatorToolCall]:
    calls: list[CreatorToolCall] = []
    layout_scope = _requested_layout_scope(message)
    if layout_scope:
        scope = "downstream" if layout_scope == "downstream" and selected_node_id else "all"
        calls.append(
            CreatorToolCall(
                tool="layout_creator_graph",
                arguments={"scope": scope, "root_node_id": selected_node_id if scope == "downstream" else ""},
                reason="按剧情连接关系重新计算节点坐标，消除重叠并保持剧情内容不变。",
            )
        )
    complete_story = _is_complete_story_request(message)
    reuse_latest_visuals = any(
        token in message
        for token in ["绑定最新视觉资产", "使用最新视觉资产", "应用最新视觉资产", "载入最新视觉资产"]
    )
    visual_request = not reuse_latest_visuals and any(
        token in message for token in ["视觉资产", "立绘", "背景图", "场景图", "图片素材", "美术素材", "美术", "出图"]
    )
    plan_only = visual_request and any(token in message for token in ["只规划", "只要方案", "仅规划", "不要生成", "暂不生成", "不出图"])
    image_generation = visual_request and not plan_only
    publish = any(token in message.lower() for token in ["发布", "玩家端", "试玩", "play"])
    save = any(token in message for token in ["保存", "存到世界"])

    if complete_story:
        minutes = _first_int(message, default=30, minimum=10, maximum=180)
        scene_count = _requested_count(message, ["场景", "幕"], default=max(3, min(16, round(minutes / 5))), minimum=3, maximum=16)
        character_count = _requested_count(message, ["角色", "人物", "NPC"], default=4, minimum=2, maximum=12)
        story_brief = message.strip()
        if len(story_brief) < 10:
            story_brief = f"请创作一个全新的完整互动剧情。用户要求：{story_brief}"
        calls.append(
            CreatorToolCall(
                tool="author_story",
                arguments={
                    "brief": story_brief,
                    "genre": "修仙剧情冒险" if "修仙" in message else "互动剧情冒险",
                    "target_minutes": minutes,
                    "target_scene_count": scene_count,
                    "target_character_count": character_count,
                    "constraints": [],
                },
                reason="这是一个完整可玩剧情需求，应调用 Story Authoring Agent 生成具体台词、选择、线索和结局。",
            )
        )
    if visual_request:
        requested_visual_characters = _requested_count(
            message,
            ["角色立绘", "人物立绘", "立绘"],
            default=_requested_count(message, ["角色", "人物", "NPC"], default=None, minimum=1, maximum=30),
            minimum=1,
            maximum=30,
        )
        requested_visual_scenes = _requested_count(
            message,
            ["场景背景", "场景图", "背景图", "场景美术", "背景美术"],
            default=_requested_count(message, ["场景", "背景"], default=None, minimum=1, maximum=50),
            minimum=1,
            maximum=50,
        )
        visual_arguments = {
            "include_characters": True,
            "include_scenes": True,
            "style_prompt": message,
            "max_characters": requested_visual_characters,
            "max_scenes": requested_visual_scenes,
        }
        visual_arguments = {key: value for key, value in visual_arguments.items() if value is not None}
        calls.append(
            CreatorToolCall(
                tool="plan_visual_assets",
                arguments=visual_arguments,
                reason="先生成可检查的角色立绘与场景背景资产计划。" if image_generation else "按要求只生成视觉资产方案，不调用图片 API。",
            )
        )
    if image_generation:
        generation_arguments = {
            key: value
            for key, value in visual_arguments.items()
            if key in {"include_characters", "include_scenes", "max_characters", "max_scenes"}
        }
        calls.extend(
            [
                CreatorToolCall(tool="generate_visual_assets", arguments=generation_arguments, reason="按已确认的视觉计划调用图片 API。"),
                CreatorToolCall(tool="bind_visual_assets", arguments={}, reason="把生成结果写回角色 portrait 与节点 background。"),
            ]
        )
    if reuse_latest_visuals:
        calls.append(
            CreatorToolCall(
                tool="bind_visual_assets",
                arguments={},
                reason="从资产仓库读取当前世界的最新生成结果，并绑定到角色 portrait 与节点 background。",
            )
        )
    if (save or complete_story) and not publish:
        calls.append(CreatorToolCall(tool="save_world", arguments={}, reason="把当前 Creator Graph 保存到世界库。"))
    if publish:
        calls.append(CreatorToolCall(tool="review_playable_world", arguments={}, reason="发布前调用 WorldReviewAgent 与 PlaytestAgent 验证可玩闭环。"))
        calls.append(CreatorToolCall(tool="publish_to_play", arguments={}, reason="校验并保存，使世界可由 Play 消费。"))
    return calls


def _deterministic_layout_response(request: CreatorAssistantRequest) -> CreatorAssistantResponse | None:
    requested_scope = _requested_layout_scope(request.message)
    if not requested_scope:
        return None
    selected_node_id = request.selected_node_id or _first_node_id(request.project)
    scope = "downstream" if requested_scope == "downstream" and selected_node_id else "all"
    arguments = {"scope": scope, "root_node_id": selected_node_id if scope == "downstream" else ""}
    target = f"当前节点 {selected_node_id} 及其下游" if scope == "downstream" else "全部剧情节点"
    return CreatorAssistantResponse(
        reply=f"我会整理{target}的画布位置，自动分层、错开分支并消除重叠；剧情正文和连接关系不会改变。",
        tool_calls=[CreatorToolCall(tool="layout_creator_graph", arguments=arguments, reason="确定性整理剧情图画布。")],
        summary=[f"整理{target}。", "仅修改节点坐标，可通过 Creator 撤销。"],
        intent="workflow",
        route="router_agent",
        requires_confirmation=True,
        source="tool_router",
    )


def _requested_layout_scope(message: str) -> str:
    normalized = re.sub(r"[\s，。！？、,.!?;；:：]", "", message).lower()
    layout_markers = ["自动布局", "重新布局", "整理节点", "排列节点", "排布节点", "节点排整齐", "节点对齐", "整理画布", "整理剧情图"]
    flexible_layout_phrase = re.search(
        r"(?:整理|排列|排布|对齐).{0,8}(?:节点|画布|剧情图)|(?:节点|画布|剧情图).{0,8}(?:排整齐|布局|整理|对齐)",
        normalized,
    )
    if not any(marker in normalized for marker in layout_markers) and flexible_layout_phrase is None:
        return ""
    current_markers = ["当前节点", "这个节点", "选中节点", "当前分支", "这条分支", "下游"]
    all_markers = ["全部", "所有", "整个", "全图", "整张"]
    if any(marker in normalized for marker in current_markers) and not any(marker in normalized for marker in all_markers):
        return "downstream"
    return "all"


def _is_complete_story_request(message: str) -> bool:
    """Recognize creator language that asks to replace the project with a full story.

    This intentionally excludes node/branch/choice edits. Those remain graph operations.
    """

    normalized = re.sub(r"\s+", "", message)
    replacement_intent = any(
        token in normalized
        for token in [
            "全新",
            "新的剧情",
            "新的故事",
            "新剧情",
            "新故事",
            "新剧本",
            "从零",
            "重新创作",
            "重新来",
            "重写",
        ]
    )
    if any(token in normalized for token in ["节点", "支线", "选项", "当前场景", "这个场景", "当前节点"]):
        return False
    story_scope = any(token in normalized for token in ["剧情", "故事", "剧本", "可玩流程", "互动冒险", "世界观"])
    if not story_scope:
        return False
    creation_intent = any(
        token in normalized
        for token in ["生成", "创作", "创建", "设计", "写一个", "做一个", "新开", "来一个"]
    )
    if not replacement_intent and not creation_intent and any(
        token in normalized for token in ["发布", "玩家端", "试玩", "审查", "校验"]
    ):
        return False
    complete_marker = any(
        token in normalized
        for token in ["完整剧情", "完整故事", "完整剧本", "30分钟", "半小时", "可玩流程"]
    ) or ("完整" in normalized and story_scope) or bool(re.search(r"\d{1,3}分钟", normalized))
    return replacement_intent or (creation_intent and complete_marker)


def _sanitize_response_for_tools(response: CreatorAssistantResponse) -> CreatorAssistantResponse:
    """Remove graph edits that conflict with project-replacing Agent capabilities."""

    if not any(call.tool == "author_story" for call in response.tool_calls):
        return response
    summary = [
        item
        for item in response.summary
        if "新的剧情节点" not in item and "新增剧情节点" not in item and "整理为一个" not in item
    ]
    return response.model_copy(update={"operations": [], "summary": _unique_strings(summary)})


def _ensure_workflow_dependencies(response: CreatorAssistantResponse) -> CreatorAssistantResponse:
    """Make Agent-backed mutations durable even when the LLM omits the store stage."""

    tools = [call.tool for call in response.tool_calls]
    if "author_story" not in tools or any(tool in tools for tool in {"save_world", "publish_to_play"}):
        return response
    calls = [
        *response.tool_calls,
        CreatorToolCall(
            tool="save_world",
            arguments={},
            reason="完整剧情生成后必须保存到世界库，确保 Creator 刷新后结果仍然存在。",
        ),
    ]
    summary = _unique_strings([*response.summary, "完整剧情生成后自动保存到世界库。"])
    return response.model_copy(update={"tool_calls": calls, "summary": summary})


def _first_int(message: str, *, default: int, minimum: int, maximum: int) -> int:
    match = re.search(r"(\d{1,3})\s*分钟", message)
    value = int(match.group(1)) if match else default
    return max(minimum, min(maximum, value))


def _requested_count(
    message: str,
    units: list[str],
    *,
    default: int | None,
    minimum: int,
    maximum: int,
) -> int | None:
    unit_pattern = "|".join(re.escape(unit) for unit in sorted(units, key=len, reverse=True))
    match = re.search(
        rf"([0-9]{{1,2}}|[一二两三四五六七八九十]{{1,3}})\s*(?:个|张|幅|套)?\s*(?:{unit_pattern})",
        message,
        re.IGNORECASE,
    )
    if not match:
        return default
    raw = match.group(1)
    value = int(raw) if raw.isdigit() else _chinese_count(raw)
    return max(minimum, min(maximum, value))


def _chinese_count(value: str) -> int:
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        return (digits.get(left, 1) * 10) + digits.get(right, 0)
    return digits.get(value, 1)


def _operation_key(operation: CreatorAssistantOperation) -> tuple[str, str, str]:
    data = operation.data or {}
    if operation.type == "create_branch":
        identity = f"{data.get('source_node_id')}:{data.get('choice_text')}"
    else:
        identity = str(data.get("name") or data.get("title") or data.get("text") or data.get("choice_id") or data.get("value") or "")
    return operation.type, operation.target_id, identity


def _is_generic_fallback(operation: CreatorAssistantOperation) -> bool:
    return operation.type == "add_node" and operation.data.get("title") == "新的剧情节点"


def _project_context(project: dict[str, Any], selected_node_id: str) -> dict[str, Any]:
    nodes = project.get("nodes", [])
    return {
        "version": project.get("version"),
        "world": project.get("world", {}),
        "characters": project.get("characters", []),
        "nodes": [
            {
                "id": node.get("id"),
                "type": node.get("type"),
                "title": node.get("title"),
                "character": node.get("character"),
                "content": str(node.get("content") or "")[:600],
                "conditions": node.get("conditions", {}),
                "effects": node.get("effects", {}),
                "next": node.get("next", ""),
                "choices": node.get("choices", []),
                "selected": node.get("id") == selected_node_id,
            }
            for node in nodes[:200]
        ],
        "truncated_node_count": max(0, len(nodes) - 200),
    }


def _branch_description(message: str) -> str:
    match = re.search(r"(?:创建|新增|添加|设计|生成)?(?:一条|一个)?支线(?:剧情)?\s*[：:]?\s*(.+)", message)
    return _clean_value(match.group(1)) if match else ""


def _match_after(message: str, labels: list[str]) -> str:
    for label in labels:
        match = re.search(rf"{re.escape(label)}(?:改成|设为|设置为|是|为|:|：)\s*[「\"“]?(.+?)[」\"”]?$", message)
        if match:
            return _clean_value(match.group(1))
    return ""


def _match_command_after(message: str, labels: list[str]) -> str:
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*[「\"“]?([^，。；;,\n]+)[」\"”]?", message)
        if match:
            return _clean_value(match.group(1))
    return ""


def _parse_stats(message: str) -> dict[str, int | float]:
    aliases = {"金钱": "money", "钱": "money", "金币": "money", "生命": "hp", "血量": "hp", "体力": "stamina", "好感": "favor"}
    stats: dict[str, int | float] = {}
    for raw_name, raw_value in re.findall(r"([\w\u4e00-\u9fa5.]+?)(?:属性)?(?:改成|设置为|设为|=|为)\s*(-?\d+(?:\.\d+)?)", message):
        raw_name = re.sub(r"^(?:把|将|玩家的|玩家)", "", raw_name)
        if not raw_name:
            continue
        stats[aliases.get(raw_name, raw_name)] = float(raw_value) if "." in raw_value else int(raw_value)
    return stats


def _parse_items(message: str) -> list[str]:
    items: list[str] = []
    for pattern in [
        r"(?:新增|添加|给玩家加)(?:一个|一件)?(?:道具|物品)\s*[「\"“]?([^，。；;,\n]{1,24})[」\"”]?",
        r"道具(?:叫|名为|:|：)\s*[「\"“]?(.+?)[」\"”]?$",
    ]:
        for match in re.finditer(pattern, message):
            value = _clean_value(match.group(1))
            if value and value not in items:
                items.append(value)
    return items


def _parse_character_name(message: str) -> str:
    match = re.search(r"(?:新增|添加)(?:一个)?(?:角色|NPC|人物)\s*[「\"“]?([\w\u4e00-\u9fa5 -]{2,24})[」\"”]?", message, flags=re.IGNORECASE)
    return _clean_value(match.group(1)) if match else ""


def _node_by_id(project: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    return next((node for node in project.get("nodes", []) if node.get("id") == node_id), None)


def _first_node_id(project: dict[str, Any]) -> str:
    return str(project.get("nodes", [{}])[0].get("id") or "") if project.get("nodes") else ""


def _player_location(project: dict[str, Any]) -> str:
    return str(project.get("world", {}).get("player", {}).get("location") or "开场")


def _short_title(description: str) -> str:
    text = re.split(r"[，。；;,.!?！？]", description, maxsplit=1)[0].strip()
    return text[:32] or "新的支线"


def _clean_value(value: str) -> str:
    return value.strip().strip("。；;,.，").strip()


def _unique_strings(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))
