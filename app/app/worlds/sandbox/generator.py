from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.agents.npc_lorebook import NpcLorebookCreationAgent, NpcLorebookCreationError
from app.core.model_config import resolve_llm_config
from app.core.protocol_tools import WorldGenerationProtocolTool
from app.agents.experience_learning import ExperienceLearningAgent
from app.agents.playtest_validation import PlaytestAgent
from app.agents.world_builder.tools import MechanicsDesignAgent
from app.agents.world_review import WorldReviewAgent
from app.worlds.sandbox.adapter import SandboxWorldAdapter
from app.worlds.sandbox.models import (
    SandboxAction,
    SandboxNPC,
    SandboxTask,
    SandboxWorldConfig,
    ScriptDecompositionRequest,
    WorldGenerationResponse,
    WorldGenerateRequest,
    WorldTemplateSummary,
)
from app.worlds.sandbox.script_decomposition import ScriptDecompositionAgent, _script_graph_runtime_context
from app.worlds.sandbox.template_store import WorldTemplateStore
from app.worlds.sandbox.validator import SandboxWorldValidator
from app.worlds.sandbox.visual_binding import attach_visual_bindings


load_dotenv()

WORLD_GENERATION_PROTOCOL_TOOL = WorldGenerationProtocolTool()

COMPLEXITY_PRESETS: dict[str, dict[str, int]] = {
    "simple": {"min_npcs": 3, "min_tasks": 5, "min_actions": 5, "progress_target": 10},
    "medium": {"min_npcs": 5, "min_tasks": 8, "min_actions": 8, "progress_target": 30},
    "complex": {"min_npcs": 8, "min_tasks": 12, "min_actions": 14, "progress_target": 60},
    "ultra": {"min_npcs": 12, "min_tasks": 18, "min_actions": 22, "progress_target": 100},
}


def _complexity_profile(request: WorldGenerateRequest) -> dict[str, int | str]:
    key = str(request.complexity or "medium").strip().lower()
    if key not in COMPLEXITY_PRESETS:
        key = "medium"
    preset = COMPLEXITY_PRESETS[key]
    learned = ExperienceLearningAgent().profile() if request.use_learned_profile else None
    min_tasks = int(request.min_tasks or (learned.recommended_tasks if learned and learned.sample_count else preset["min_tasks"]))
    min_npcs = int(request.min_npcs or (learned.recommended_npcs if learned and learned.sample_count else preset["min_npcs"]))
    min_actions = int(request.min_actions or (learned.recommended_actions if learned and learned.sample_count else preset["min_actions"]))
    min_actions = max(min_actions, min_tasks)
    return {
        "key": key,
        "min_npcs": max(1, min_npcs),
        "min_tasks": max(1, min_tasks),
        "min_actions": max(1, min_actions),
        "progress_target": max(preset["progress_target"], min_tasks * 5),
        "learned_sample_count": learned.sample_count if learned else 0,
        "learned_confidence": learned.confidence if learned else "disabled",
    }


def list_world_templates() -> list[WorldTemplateSummary]:
    return WorldTemplateStore().list()


def generate_world_config(request: WorldGenerateRequest) -> SandboxWorldConfig:
    if _is_script_graph_request(request):
        return _build_script_graph_world(request)
    if _is_script_decomposition_request(request):
        return _build_script_decomposition_world(request)
    return _generic_theme_fallback(request)


async def generate_world_config_with_ai(request: WorldGenerateRequest) -> SandboxWorldConfig:
    return await WorldBuilderAgent().generate(request)


class WorldBuilderAgent:
    async def generate(
        self,
        request: WorldGenerateRequest,
        fallback: SandboxWorldConfig | None = None,
    ) -> SandboxWorldConfig:
        if _is_script_graph_request(request):
            fallback = _build_script_graph_world(request)
            if not request.world_builder_llm:
                return await _finalize_world_with_lorebook(fallback, request)
            candidate = await _generate_ai_candidate(request)
            repaired = _repair_world_config(candidate, request, fallback)
            repaired.metadata = {
                **(repaired.metadata or {}),
                "script_graph": request.script_graph,
                "visual_plan": _compact_visual_plan_for_world(request.visual_plan),
                "visual_result": _compact_visual_result_for_world(request.visual_result),
                "visual_asset_summary": _visual_asset_prompt_context(request.visual_plan, request.visual_result),
                "story_graph_summary": _script_graph_runtime_context(request.script_graph or {}),
                "script_graph_input_source": "workbench_script_graph",
            }
            return await _finalize_world_with_lorebook(repaired, request)
        if _is_script_decomposition_request(request):
            return await _finalize_world_with_lorebook(_build_script_decomposition_world(request), request)
        fallback = fallback or generate_world_config(request)
        candidate = await _generate_ai_candidate(request)
        repaired = _repair_world_config(candidate, request, fallback)
        return await _finalize_world_with_lorebook(repaired, request)


async def _finalize_world_with_lorebook(config: SandboxWorldConfig, request: WorldGenerateRequest) -> SandboxWorldConfig:
    final_world = _prepare_world_for_runtime(config)
    final_world = await _attach_lorebook_with_agent(final_world, request, strict=True)
    return _attach_world_quality_gate(final_world)


def _finalize_world_quality(config: SandboxWorldConfig) -> SandboxWorldConfig:
    return _attach_world_quality_gate(_prepare_world_for_runtime(config))


def _prepare_world_for_runtime(config: SandboxWorldConfig) -> SandboxWorldConfig:
    guarded = WORLD_GENERATION_PROTOCOL_TOOL.repair_world_config(config)
    final_world = SandboxWorldValidator().ensure_valid(guarded)
    final_world = attach_visual_bindings(final_world)
    mechanics_notes = MechanicsDesignAgent().design(final_world)
    if mechanics_notes:
        final_world.metadata = {**(final_world.metadata or {}), "mechanics_design_notes": mechanics_notes}
    return final_world


def _attach_world_quality_gate(final_world: SandboxWorldConfig) -> SandboxWorldConfig:
    world_review = WorldReviewAgent().review(final_world)
    playtest_review = PlaytestAgent().simulate_adapter(SandboxWorldAdapter(final_world))
    final_world.metadata = {
        **(final_world.metadata or {}),
        "world_review": world_review.model_dump(),
        "playtest_review": playtest_review.model_dump(),
        "quality_gate": {
            "validator_passed": True,
            "world_review_passed": world_review.passed,
            "playtest_passed": playtest_review.passed,
            "passed": world_review.passed and playtest_review.passed,
        },
    }
    return final_world


def _attach_npc_portraits(config: SandboxWorldConfig) -> SandboxWorldConfig:
    metadata = config.metadata if isinstance(config.metadata, dict) else {}
    assets = _character_visual_assets(metadata.get("visual_plan"), metadata.get("visual_result"))
    if not assets:
        return config
    updated_npcs: list[SandboxNPC] = []
    portrait_index: dict[str, dict[str, Any]] = {}
    for npc in config.npcs:
        asset = _match_npc_visual_asset(npc, assets)
        if not asset:
            updated_npcs.append(npc)
            continue
        portrait = {
            "asset_id": str(asset.get("id") or ""),
            "url": str(asset.get("output_path") or ""),
            "output_path": str(asset.get("output_path") or ""),
            "source_id": str(asset.get("source_id") or ""),
            "source_name": str(asset.get("source_name") or asset.get("display_name") or ""),
            "status": str(asset.get("status") or ""),
            "kind": str(asset.get("kind") or ""),
        }
        updated_npcs.append(npc.model_copy(update={"portrait": portrait}))
        portrait_index[npc.id] = portrait
    if not portrait_index:
        return config
    return config.model_copy(update={"npcs": updated_npcs, "metadata": {**metadata, "npc_portraits": portrait_index}})


async def _attach_lorebook_with_agent(config: SandboxWorldConfig, request: WorldGenerateRequest, *, strict: bool = False) -> SandboxWorldConfig:
    final_world = config.model_copy(deep=True)
    lorebook_agent = NpcLorebookCreationAgent()
    try:
        lorebook = await lorebook_agent.create(final_world, request.world_builder_llm, strict=strict)
    except NpcLorebookCreationError as exc:
        final_world.metadata = {
            **(final_world.metadata or {}),
            "npc_lorebook_generation": {
                "agent": "NpcLorebookCreationAgent",
                "created_by": "",
                "entry_count": 0,
                "fallback_used": False,
                "error": str(exc),
                "failed": True,
            },
            "quality_gate": {
                "validator_passed": True,
                "world_review_passed": False,
                "playtest_passed": False,
                "lorebook_passed": False,
                "passed": False,
            },
        }
        raise
    final_world.metadata = {
        **(final_world.metadata or {}),
        "npc_lorebook": lorebook.model_dump(),
        "npc_lorebook_generation": {
            "agent": "NpcLorebookCreationAgent",
            "created_by": lorebook.metadata.get("created_by"),
            "entry_count": len(lorebook.entries),
            "fallback_used": lorebook.metadata.get("creation_agent_failed", False),
            "error": lorebook.metadata.get("creation_agent_error", ""),
        },
    }
    return final_world


def _world_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time())}"


def _is_script_decomposition_request(request: WorldGenerateRequest) -> bool:
    if request.script_decomposition:
        return True
    template = str(request.template or "").lower()
    if template in {"script_decomposition", "script_case", "剧本杀"}:
        return True
    text = str(request.theme or "")
    return any(token in text for token in ["剧本杀", "案件真相", "公共背景", "禁止提前泄露"]) and any(
        token in text for token in ["角色", "线索"]
    )


def _is_script_graph_request(request: WorldGenerateRequest) -> bool:
    return isinstance(request.script_graph, dict) and isinstance(request.script_graph.get("nodes"), list) and isinstance(request.script_graph.get("edges"), list)


def _script_decomposition_from_request(request: WorldGenerateRequest) -> ScriptDecompositionRequest:
    if request.script_decomposition:
        data = dict(request.script_decomposition)
        data.setdefault("player_name", request.player_name)
        if request.world_name:
            data.setdefault("title", request.world_name)
        return ScriptDecompositionRequest.model_validate(data)
    return ScriptDecompositionRequest(
        title=request.world_name,
        player_name=request.player_name,
        source_text=request.theme,
    )


def _build_script_decomposition_world(request: WorldGenerateRequest) -> SandboxWorldConfig:
    world = ScriptDecompositionAgent().build(_script_decomposition_from_request(request)).world
    if request.script_graph or request.visual_plan or request.visual_result:
        metadata = dict(world.metadata or {})
        if request.script_graph:
            metadata["script_graph"] = request.script_graph
            metadata["story_graph_summary"] = _script_graph_runtime_context(request.script_graph)
        if request.visual_plan:
            metadata["visual_plan"] = _compact_visual_plan_for_world(request.visual_plan)
        if request.visual_result:
            metadata["visual_result"] = _compact_visual_result_for_world(request.visual_result)
        if request.visual_plan or request.visual_result:
            metadata["visual_asset_summary"] = _visual_asset_prompt_context(request.visual_plan, request.visual_result)
        metadata["script_graph_input_source"] = "workbench_script_graph"
        world = world.model_copy(update={"metadata": metadata})
    return world


def _build_script_graph_world(request: WorldGenerateRequest) -> SandboxWorldConfig:
    graph = request.script_graph or {}
    nodes = _graph_nodes_by_id(graph)
    characters = _graph_nodes_by_kind(graph, {"character"})
    locations = _graph_locations(graph, nodes)
    start_location = locations[0] if locations else "起点"
    title = request.world_name or str(graph.get("title") or "线索世界")
    npcs = []
    for index, node in enumerate(characters, start=1):
        npc_locations = _graph_related_labels(graph, str(node.get("id") or ""), nodes, "LOCATED_AT") or [start_location]
        npcs.append(
            SandboxNPC(
                id=_safe_id_part(str((node.get("properties") or {}).get("source_id") or node.get("id") or f"npc_{index}")) or f"npc_{index}",
                name=str(node.get("label") or f"NPC{index}"),
                role=str((node.get("properties") or {}).get("role") or node.get("kind") or "NPC"),
                personality=_graph_node_description(node) or "根据自己知道的经历、可靠传闻和当前地点线索回应玩家。",
                goals=[
                    "只依据自己知道的经历、可靠传闻和当前世界事实回答。",
                    "根据玩家已发现的信息逐步回应，不主动编造未知事实。",
                ],
                location=npc_locations[0],
                locations=npc_locations,
            )
        )
    if not npcs:
        npcs = [
            SandboxNPC(
                id="story_guide",
                name="线索引导员",
                role="引导 NPC",
                personality="帮助玩家探索人物、地点、事件和线索之间的关联。",
                goals=["引导玩家核对线索", "不编造未知事实"],
                location=start_location,
                locations=[start_location],
            )
        ]

    clue_like = _graph_nodes_by_kind(graph, {"clue", "item", "secret", "event", "timeline_event", "task", "trigger"})[:12]
    tasks = [
        SandboxTask(
            id="inspect_story_clues",
            title="梳理线索记录",
            description="查看地点、事件、人物和关键线索之间的关系。",
            completion={"stats": {"graph_inspect_count": {"min": max(1, min(3, len(clue_like) or 1))}}},
        ),
        SandboxTask(
            id="talk_to_story_characters",
            title="询问相关人物",
            description="和主要人物对话，确认他们与地点、线索和事件的关系。",
            completion={"stats": {"questioned_count": {"min": max(1, min(3, len(npcs)))}}},
        ),
        SandboxTask(
            id="submit_story_deduction",
            title="提交阶段性推理",
            description="基于已验证的线索关系提交阶段性判断。",
            completion={"player": {"graph_deduction_submitted": True}},
        ),
    ]
    actions: list[SandboxAction] = []
    for index, location in enumerate(locations[:10], start=1):
        actions.append(
            SandboxAction(
                id=f"move_{_safe_id_part(location) or 'location'}_{index}",
                label=f"前往{location}",
                description=f"移动到地点：{location}",
                effect={"scene": f"你来到{location}，开始核对这里关联的人物、事件和线索。", "set_player": {"location": location}},
            )
        )
    for index, node in enumerate(clue_like, start=1):
        label = str(node.get("label") or node.get("id") or f"线索{index}")
        location = _graph_related_label(graph, str(node.get("id") or ""), nodes, "FOUND_AT") or _graph_related_label(
            graph,
            str(node.get("id") or ""),
            nodes,
            "OCCURS_AT",
        ) or start_location
        actions.append(
            SandboxAction(
                id=f"inspect_clue_{_safe_id_part(label) or 'clue'}_{index}",
                label=f"核对{label}",
                description=f"核对线索及其关系：{label}",
                effect={
                    "scene": f"你核对了线索《{label}》：{_graph_node_description(node)}",
                    "set_player": {"location": location},
                    "increase_player": {"graph_inspect_count": 1},
                },
            )
        )
    for npc in npcs:
        actions.append(
            SandboxAction(
                id=f"question_{npc.id}",
                label=f"询问{npc.name}",
                description=f"围绕已知关系询问{npc.name}。",
                effect={
                    "scene": f"你和{npc.name}核对了相关关系。",
                    "set_player": {"location": npc.location},
                    "increase_player": {"questioned_count": 1},
                    "active_npc_id": npc.id,
                },
            )
        )
    actions.append(
        SandboxAction(
            id="submit_story_deduction",
            label="提交阶段性推理",
            description="根据已验证的线索和关系提交推理。",
            effect={
                "scene": "你根据已验证的线索和关系提交了阶段性推理。",
                "set_player": {"graph_deduction_submitted": True},
                "complete_task": "submit_story_deduction",
            },
        )
    )
    world = SandboxWorldConfig(
        world_id=_safe_id_part(str(graph.get("graph_id") or title)) or "script_graph_world",
        name=title,
        description="由故事结构生成的线索驱动世界。",
        lore="这个世界围绕人物、地点、线索、事件和任务关系展开；NPC 只知道自己经历过、听说过或在当前位置能合理知道的信息。",
        opening_scene=f"你进入《{title}》的调查现场，当前位置是{start_location}。",
        player={
            "name": request.player_name or "玩家",
            "location": start_location,
            "role": "调查者",
            "status": "正在核对线索与传闻。",
            "inventory": [],
            "graph_inspect_count": 0,
            "questioned_count": 0,
            "graph_deduction_submitted": False,
        },
        npcs=npcs,
        story_goals=["核对线索记录", "询问相关人物", "验证关系链路", "提交阶段性推理"],
        tasks=tasks,
        actions=actions,
        initial_memories=[
            "世界事实以可靠线索记录为准。",
            "NPC 不得编造未知人物、地点、线索或关系。",
            "玩家需要通过核对线索和人物关系来验证故事是否可运行。",
        ],
        metadata={
            "generated_by": "script_graph_world_builder",
            "compiled_by": "script_graph_world_compiler",
            "schema_version": "script_graph_world.v1",
            "mvp_loop": True,
            "template": "script_graph",
            "script_graph": graph,
            "visual_plan": _compact_visual_plan_for_world(request.visual_plan),
            "visual_result": _compact_visual_result_for_world(request.visual_result),
            "visual_asset_summary": _visual_asset_prompt_context(request.visual_plan, request.visual_result),
            "story_graph_summary": _script_graph_runtime_context(graph),
            "script_graph_input_source": "workbench_script_graph",
        },
    )
    return attach_visual_bindings(world)


def _player_name(request: WorldGenerateRequest) -> str:
    return request.player_name.strip() or "主角"


def _theme(request: WorldGenerateRequest, fallback: str) -> str:
    return request.theme.strip() or fallback


async def _generate_ai_candidate(request: WorldGenerateRequest) -> SandboxWorldConfig:
    config = resolve_llm_config(request.world_builder_llm, "world_builder")
    api_key = config.api_key
    if not api_key:
        raise RuntimeError("Missing WORLD_BUILDER_LLM_API_KEY or LLM_API_KEY.")

    llm = ChatOpenAI(
        model=config.model,
        api_key=api_key,
        base_url=config.base_url or None,
        temperature=config.temperature,
        timeout=config.timeout,
        max_retries=config.max_retries,
        streaming=True,
    )
    messages = [
        SystemMessage(content=_world_generator_system_prompt()),
        HumanMessage(content=_world_generator_user_prompt(request)),
        HumanMessage(content="Return exactly one JSON object. Do not use Markdown. Fill all required fields even when uncertain."),
    ]
    chunks: list[str] = []
    async for chunk in llm.astream(messages):
        content = str(chunk.content or "")
        if content:
            chunks.append(content)
    response = WORLD_GENERATION_PROTOCOL_TOOL.repair_world_generation("".join(chunks))
    world = response.world
    world.metadata = {
        **(world.metadata or {}),
        "generation_thoughts": response.thoughts.model_dump(),
        "generation_validation_notes": response.validation_notes,
    }
    return world


def _parse_generation_json(content: str) -> dict[str, Any]:
    return WORLD_GENERATION_PROTOCOL_TOOL.parse_generation_json(content)


def _normalize_generation_payload(data: dict[str, Any]) -> dict[str, Any]:
    return WORLD_GENERATION_PROTOCOL_TOOL.repair_generation_payload(data)


def _coerce_string_list(value: Any) -> list[str]:
    return WORLD_GENERATION_PROTOCOL_TOOL._coerce_string_list(value)


def _normalize_completion(value: Any) -> dict[str, Any]:
    return WORLD_GENERATION_PROTOCOL_TOOL._normalize_completion(value)


def _normalize_condition_list(conditions: Any) -> dict[str, Any]:
    return WORLD_GENERATION_PROTOCOL_TOOL._normalize_condition_list(conditions)


def _normalize_numeric_conditions(conditions: dict[str, Any]) -> dict[str, Any]:
    return WORLD_GENERATION_PROTOCOL_TOOL._normalize_numeric_conditions(conditions)


def _operator_rule(operator: Any, target: Any) -> dict[str, Any]:
    return WORLD_GENERATION_PROTOCOL_TOOL._operator_rule(operator, target)


def _derive_world_name(theme: str) -> str:
    text = _plain_theme(theme)
    return f"{text[:24] or '自定义目标'}"


def _derive_start_location(theme: str) -> str:
    text = _plain_theme(theme)
    match = re.search(r"(?:在|去|前往|来到)([\u4e00-\u9fa5A-Za-z0-9_ -]{2,12})(?:，|。|,|\.|$)", text)
    return match.group(1).strip() if match else "起点"


def _derive_target_label(theme: str) -> str:
    text = _plain_theme(theme)
    return text[:60] or "完成用户目标"


def _derive_player_role(theme: str) -> str:
    text = _plain_theme(theme)
    match = re.search(r"(?:一个|一名|名叫[^，。,.]{1,20}的)([^，。,.]{2,20})", text)
    return match.group(1).strip() if match else "主角"


def _theme_stage_blueprint(theme: str) -> dict[str, list[str]]:
    text = _plain_theme(theme)
    if any(word in text for word in ["偶像", "出道", "bej", "BEJ", "总选", "粉丝", "面试"]):
        return {
            "locations": ["面试等候区", "声乐练习室", "舞蹈练习室", "摄影棚", "小剧场后台", "粉丝见面会现场", "商务洽谈室", "总选舞台"],
            "roles": ["面试负责人", "声乐老师", "舞蹈老师", "摄影师", "同期成员", "粉丝运营", "商务经纪", "总选主持"],
            "names": ["周面试官", "许声乐老师", "林舞蹈老师", "阿岚摄影师", "沈同期", "小鱼运营", "顾经纪", "总选主持人"],
            "tasks": ["通过入团面试", "完成声乐基础训练", "完成舞蹈基础训练", "拍摄第一组宣发照", "适应小剧场后台流程", "完成小型粉丝互动", "处理一次商务合作问题", "冲刺总选排名"],
            "verbs": ["准备面试材料", "练习声乐", "练习舞蹈", "完成宣发拍摄", "熟悉后台流程", "经营粉丝互动", "处理商务沟通", "完成舞台表现"],
        }
    return {
        "locations": ["起点办公室", "训练区", "资料室", "协作现场", "公开展示区", "关键会谈室", "复盘室", "最终舞台"],
        "roles": ["引导负责人", "训练导师", "资料管理员", "协作伙伴", "观察评委", "关键联系人", "复盘顾问", "最终评审"],
        "names": ["引导员", "训练导师", "资料管理员", "协作伙伴", "观察评委", "关键联系人", "复盘顾问", "最终评审"],
        "tasks": ["确认目标", "完成基础训练", "收集关键资料", "完成协作挑战", "通过公开展示", "完成关键会谈", "完成阶段复盘", "达成最终目标"],
        "verbs": ["确认目标", "训练能力", "收集资料", "推进协作", "公开展示", "关键沟通", "复盘调整", "完成目标"],
    }


def _cycle(values: list[str], index: int) -> str:
    return values[index % len(values)] if values else ""


def _derive_initial_items(theme: str) -> list[str]:
    text = _plain_theme(theme)
    items = []
    for item in ["报名表", "申请表", "简历", "邀请函", "钥匙", "令牌", "证件", "照片"]:
        if item in text:
            items.append(item)
    return items


def _plain_theme(theme: str) -> str:
    try:
        data = json.loads(theme)
        if isinstance(data, dict):
            return str(data.get("excerpt") or data.get("theme") or data.get("filename") or theme)
    except (json.JSONDecodeError, TypeError):
        pass
    return str(theme or "").strip()


def _world_generator_system_prompt() -> str:
    return """
你是“可运行 NPC 沙盒世界”的结构化世界观生成 Agent。
你必须返回严格符合 WorldGenerationResponse schema 的 JSON 对象，不要 Markdown，不要解释。

固定响应格式：
{
  "thoughts": {
    "text": "一句话说明设计意图",
    "reasoning": "为什么这个世界能跑通",
    "plan": ["闭环步骤1", "闭环步骤2", "闭环步骤3"],
    "criticism": "自检：是否可运行、是否缺字段、是否剧透",
    "speak": "给用户看的生成摘要"
  },
  "world": {
    "world_id": "temporary_id",
    "name": "世界名",
    "description": "简介",
    "lore": "世界观/规则/背景",
    "opening_scene": "开场",
    "player": {},
    "npcs": [],
    "story_goals": [],
    "tasks": [],
    "actions": [],
    "initial_memories": [],
    "metadata": {}
  },
  "validation_notes": ["自检结果"]
}

生成目标：
- 不是写一段背景，而是生成一个可直接运行的 MVP 世界。
- 玩家通过和 NPC 对话获得目标、地点、人物、道具线索。
- 后台 actions 是世界状态变更接口，不应设计成给玩家直接剧透的按钮文案。
- 默认至少有清晰的五步闭环；如果用户在生成请求中自定义了 NPC/任务/action 数量，则必须按用户自定义规模生成更长闭环。
- 必须根据世界观自动设计“完成判定字段”，不要只写剧情文案：
  - 修仙/战斗可用 realm_level、cultivation、battle_power、spirit_seal 等。
  - 偶像/训练可用 skills.dance、skills.vocal、stage_confidence、fan_count 等。
  - 社交/恋爱/阵营可用 relations.<npc_id>、faction_reputation、trust_level 等。
  - 解谜/冒险可用 inventory/items、flags、keywords、location 等。
- 必须在 world.metadata.mechanics 写出本世界的可判定维度表。每项包含：
  {"id":"英文id","path":"player里的字段路径，如 skills.dance 或 confidence","label":"中文名","aliases":["任务文本里可能出现的叫法"],"kind":"stat|relation|item|flag"}
- tasks[].completion 里的 stats/player/relations/flags 必须引用 metadata.mechanics 中声明过的 path。
- actions[].effect.set_player 或 increase_player 必须产出 completion 会用到的 path。
- 每个任务都应写 completion 条件，支持 items、keywords、location、player、stats、relations、flags、actions、mode。
- 每个 action.effect 不只改 scene，也要改对应判定字段；数值增长优先用 increase_player，例如 {"increase_player":{"skills.dance":10}}。

字段要求：
- world_id 可填临时英文 id，后端会重写；必须只含小写字母、数字、下划线。
- name/description/lore/opening_scene 必须完整。
- player 必须包含 name, location, role, status，并可以包含 inventory/items 或布尔道具字段。
- npcs/tasks/actions 的最低数量以用户请求中的自定义生成规模为准；未指定时才使用默认 3/5/5。
- 每个 NPC 必须有 id, name, role, personality, goals, location，并建议填写 locations。
- NPC 可以出现在多地，但必须符合故事事实、任务路线或角色行动逻辑；locations 是该 NPC 可出现地点列表，location 是主地点/默认初始地点。
- 不要用 "地点A/地点B" 这种字符串偷懒表达多地点；多地点必须写成 locations:["地点A","地点B"]，location 只填其中一个主地点。
- 每个 task 的 id 使用英文 snake_case，status 默认 pending，completion 描述完成条件。
- 每个 action 必须有 id, label, description, effect。
- 每个 action.effect 应优先包含 scene；如果推进玩家状态，包含 set_player 或 increase_player；如果完成任务，包含 complete_task；如果切换当前 NPC，包含 active_npc_id。
- actions 要能覆盖所有任务，形成可跑完闭环。
- initial_memories 写 2-5 条用于 Agent 初始记忆。
- metadata.generated_by 必须是 "ai_world_generator"，metadata.mvp_loop 必须是 true。
""".strip()


def _world_generator_user_prompt(request: WorldGenerateRequest) -> str:
    template = WorldTemplateStore().get(request.template or "freeform")
    template_name = template.name if template else (request.template or "freeform")
    template_prompt = template.structure_prompt if template else "不套固定结构，按用户主题生成。"
    profile = _complexity_profile(request)
    graph_hint = _script_graph_prompt_context(request.script_graph)
    visual_hint = _visual_asset_prompt_context(request.visual_plan, request.visual_result)
    learned = ExperienceLearningAgent().profile() if request.use_learned_profile else None
    learned_hint = learned.generation_hint if learned and learned.sample_count else "暂无体验学习画像，按当前用户输入和复杂度预设生成。"
    final_rule = (
        "最终任务不得只靠一个 finish_goal 动作直接完成，必须同时依赖前置任务、关键字段或 progress 门槛。"
        if request.final_task_requires_previous
        else "最终任务可以由单独动作完成，但仍要保证前置任务有可玩内容。"
    )
    return f"""
故事模板：{template_name}
模板结构说明：{template_prompt}
注意：故事模板只表示叙事结构，不代表题材。不得因为模板名引入用户没有要求的修仙、商会、偶像、探案等固定题材。
复杂度预设：{profile["key"]}
用户自定义生成规模：
- NPC 至少 {profile["min_npcs"]} 个。
- 任务至少 {profile["min_tasks"]} 个。
- 后台 actions 至少 {profile["min_actions"]} 个，并覆盖所有任务。
- 核心 progress 或等价主线进度最终门槛建议不低于 {profile["progress_target"]}。
最终目标规则：{final_rule}
体验学习画像：{learned_hint}
主题关键词：{request.theme or "用户未填写，请自行设计一个清晰主题"}
玩家名：{_player_name(request)}
期望世界名：{request.world_name or "未指定"}

请生成一个完整 WorldGenerationResponse JSON。注意：world 内所有 id 用英文 snake_case，所有对玩家可见文本用中文。
""".strip() + ("\n" + graph_hint if graph_hint else "") + ("\n" + visual_hint if visual_hint else "")


def _script_graph_prompt_context(script_graph: dict[str, Any] | None) -> str:
    if not isinstance(script_graph, dict) or not script_graph.get("nodes"):
        return ""
    context = _script_graph_runtime_context(script_graph, max_nodes=30, max_edges=60)
    return "ScriptGraphDocument input; use this property graph as the source of story facts and relationships:\n" + json.dumps(
        context,
        ensure_ascii=False,
        indent=2,
    )


def _visual_asset_prompt_context(visual_plan: dict[str, Any] | None, visual_result: dict[str, Any] | None = None) -> str:
    if not isinstance(visual_plan, dict) and isinstance(visual_result, dict):
        plan = visual_result.get("plan")
        if isinstance(plan, dict):
            visual_plan = plan
    if not isinstance(visual_plan, dict):
        return ""
    generated = [asset for asset in (visual_result or {}).get("generated", []) if isinstance(asset, dict)] if isinstance(visual_result, dict) else []
    failed = [asset for asset in (visual_result or {}).get("failed", []) if isinstance(asset, dict)] if isinstance(visual_result, dict) else []
    assets = generated or [asset for asset in visual_plan.get("assets", []) if isinstance(asset, dict)]
    if not assets:
        return ""
    compact_assets = []
    for asset in assets[:40]:
        compact_assets.append(
            {
                "id": asset.get("id", ""),
                "kind": asset.get("kind", ""),
                "display_name": asset.get("display_name", ""),
                "source_id": asset.get("source_id", ""),
                "source_name": asset.get("source_name", ""),
                "output_path": asset.get("output_path", ""),
                "status": asset.get("status", ""),
                "generated": asset in generated or asset.get("status") == "generated",
            }
        )
    context = {
        "plan_id": visual_plan.get("plan_id", ""),
        "world_id": visual_plan.get("world_id", ""),
        "title": visual_plan.get("title", ""),
        "asset_count": len(assets),
        "generated_count": len(generated),
        "failed_count": len(failed),
        "assets": compact_assets,
    }
    return "VisualAsset input; use these generated or planned image assets as visual references attached to the story graph:\n" + json.dumps(
        context,
        ensure_ascii=False,
        indent=2,
    )


def _visual_plan_prompt_context(visual_plan: dict[str, Any] | None) -> str:
    return _visual_asset_prompt_context(visual_plan)


def _compact_visual_plan_for_world(visual_plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(visual_plan, dict):
        return None
    assets = [_compact_visual_asset_for_world(asset) for asset in visual_plan.get("assets", []) if isinstance(asset, dict)]
    return {
        "plan_id": visual_plan.get("plan_id", ""),
        "world_id": visual_plan.get("world_id", ""),
        "title": visual_plan.get("title", ""),
        "provider": _compact_provider(visual_plan.get("provider")),
        "assets": assets,
        "warnings": [str(item) for item in visual_plan.get("warnings", [])[:20]] if isinstance(visual_plan.get("warnings"), list) else [],
        "metadata": {
            key: value
            for key, value in (visual_plan.get("metadata") or {}).items()
            if key in {"generation_run_id", "generation_run_created_at", "source_type", "asset_count"}
        }
        if isinstance(visual_plan.get("metadata"), dict)
        else {},
    }


def _compact_visual_result_for_world(visual_result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(visual_result, dict):
        return None
    return {
        "plan": _compact_visual_plan_for_world(visual_result.get("plan")),
        "generated": [_compact_visual_asset_for_world(asset) for asset in visual_result.get("generated", []) if isinstance(asset, dict)],
        "failed": [_compact_visual_asset_for_world(asset) for asset in visual_result.get("failed", []) if isinstance(asset, dict)],
        "metadata": {
            key: value
            for key, value in (visual_result.get("metadata") or {}).items()
            if key in {"generation_run_id", "generation_run_path", "status", "cancelled", "generated_count", "failed_count", "planned_count"}
        }
        if isinstance(visual_result.get("metadata"), dict)
        else {},
    }


def _compact_visual_asset_for_world(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": asset.get("id", ""),
        "kind": asset.get("kind", ""),
        "display_name": asset.get("display_name", ""),
        "source_id": asset.get("source_id", ""),
        "source_name": asset.get("source_name", ""),
        "output_path": asset.get("output_path", ""),
        "provider": asset.get("provider", ""),
        "model": asset.get("model", ""),
        "size": asset.get("size", ""),
        "status": asset.get("status", ""),
        "warnings": [str(item) for item in asset.get("warnings", [])[:10]] if isinstance(asset.get("warnings"), list) else [],
        "metadata": {
            key: value
            for key, value in (asset.get("metadata") or {}).items()
            if key in {"asset_id", "kind", "display_name", "generation_run_id", "finalized_by", "manual_prompt"}
        }
        if isinstance(asset.get("metadata"), dict)
        else {},
    }


def _compact_provider(provider: Any) -> dict[str, Any]:
    if hasattr(provider, "model_dump"):
        provider = provider.model_dump()
    if not isinstance(provider, dict):
        return {}
    return {key: provider.get(key) for key in ("provider", "model", "size") if provider.get(key)}


def _character_visual_assets(visual_plan: Any, visual_result: Any) -> list[dict[str, Any]]:
    generated = [asset for asset in (visual_result or {}).get("generated", []) if isinstance(asset, dict)] if isinstance(visual_result, dict) else []
    plan_assets = [asset for asset in (visual_plan or {}).get("assets", []) if isinstance(asset, dict)] if isinstance(visual_plan, dict) else []
    assets = generated or plan_assets
    return [
        asset
        for asset in assets
        if str(asset.get("kind") or "").lower() == "character"
        and str(asset.get("output_path") or "").strip()
        and str(asset.get("status") or "generated") != "failed"
    ]


def _match_npc_visual_asset(npc: SandboxNPC, assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    npc_keys = {_norm_match(npc.id), _norm_match(npc.name)}
    npc_keys = {key for key in npc_keys if key}
    for asset in assets:
        asset_keys = {
            _norm_match(str(asset.get("source_id") or "")),
            _norm_match(str(asset.get("source_name") or "")),
            _norm_match(str(asset.get("display_name") or "")),
            _norm_match(str(asset.get("id") or "")),
        }
        if npc_keys & {key for key in asset_keys if key}:
            return asset
    for asset in assets:
        label = _norm_match(" ".join(str(asset.get(key) or "") for key in ("id", "source_id", "source_name", "display_name")))
        if any(key and key in label for key in npc_keys):
            return asset
    return None


def _norm_match(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "").lower())


def _graph_nodes_by_id(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(node.get("id") or ""): node
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and node.get("id")
    }


def _graph_nodes_by_kind(graph: dict[str, Any], kinds: set[str]) -> list[dict[str, Any]]:
    return [
        node
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and str(node.get("kind") or "") in kinds
    ]


def _graph_locations(graph: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> list[str]:
    locations = [str(node.get("label") or "") for node in _graph_nodes_by_kind(graph, {"location", "scene"}) if node.get("label")]
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict) or str(edge.get("type") or "") not in {"LOCATED_AT", "FOUND_AT", "OCCURS_AT"}:
            continue
        target = nodes.get(str(edge.get("target") or ""), {})
        if target and str(target.get("kind") or "") in {"location", "scene"}:
            locations.append(str(target.get("label") or ""))
    return list(dict.fromkeys(item for item in locations if item))


def _graph_related_label(graph: dict[str, Any], node_id: str, nodes: dict[str, dict[str, Any]], relation_type: str) -> str:
    labels = _graph_related_labels(graph, node_id, nodes, relation_type)
    return labels[0] if labels else ""


def _graph_related_labels(graph: dict[str, Any], node_id: str, nodes: dict[str, dict[str, Any]], relation_type: str) -> list[str]:
    labels: list[str] = []
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict) or str(edge.get("type") or "") != relation_type:
            continue
        if str(edge.get("source") or "") != node_id:
            continue
        target = nodes.get(str(edge.get("target") or ""), {})
        if target:
            label = str(target.get("label") or "").strip()
            if label:
                labels.append(label)
    return list(dict.fromkeys(labels))


def _graph_node_description(node: dict[str, Any]) -> str:
    properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
    return str(properties.get("description") or properties.get("text") or properties.get("public_info") or "")


def _repair_world_config(
    candidate: SandboxWorldConfig,
    request: WorldGenerateRequest,
    fallback: SandboxWorldConfig,
) -> SandboxWorldConfig:
    config = candidate.model_copy(deep=True)
    prefix = _safe_id_part(request.template or "ai_world")
    config.world_id = _world_id(prefix)
    config.name = config.name.strip() or fallback.name
    config.description = config.description.strip() or fallback.description
    config.lore = config.lore.strip() or fallback.lore
    config.opening_scene = config.opening_scene.strip() or fallback.opening_scene
    config.player = _repair_player(config.player, request, fallback.player)
    profile = _complexity_profile(request)
    config.npcs = _repair_npcs(config.npcs, fallback.npcs, int(profile["min_npcs"]), request)
    config.tasks = _repair_tasks(config.tasks, fallback.tasks, int(profile["min_tasks"]), request)
    config.actions = _repair_actions(config.actions, config.tasks, config.npcs, fallback.actions, int(profile["min_actions"]), request)
    _apply_final_task_gate(config, request)
    if not config.story_goals:
        config.story_goals = fallback.story_goals
    if not config.initial_memories:
        config.initial_memories = [
            f"玩家刚进入世界：{config.name}。",
            f"开场地点：{config.player.get('location', '起始地点')}。",
        ]
    config.metadata = {
        **(config.metadata or {}),
        "generated_by": "ai_world_generator",
        "template": request.template,
        "theme": request.theme,
        "complexity": profile,
        "final_task_requires_previous": request.final_task_requires_previous,
        "mvp_loop": True,
        "schema_repaired": True,
    }
    return config


def _repair_player(player: dict[str, Any], request: WorldGenerateRequest, fallback: dict[str, Any]) -> dict[str, Any]:
    data = {**fallback, **(player or {})}
    data["name"] = _player_name(request)
    data["location"] = str(data.get("location") or fallback.get("location") or "起始地点")
    data["role"] = str(data.get("role") or fallback.get("role") or "主角")
    data["status"] = str(data.get("status") or fallback.get("status") or "刚进入事件")
    return data


def _repair_npcs(npcs: list[SandboxNPC], fallback: list[SandboxNPC], min_count: int, request: WorldGenerateRequest) -> list[SandboxNPC]:
    repaired = []
    seen = set()
    for index, npc in enumerate(npcs or []):
        data = npc.model_dump()
        data["id"] = _unique_id(data.get("id") or data.get("name") or f"npc_{index + 1}", seen)
        data["name"] = data.get("name") or f"NPC {index + 1}"
        data["role"] = data.get("role") or "NPC"
        data["personality"] = data.get("personality") or "立场明确，会根据自身目标回应玩家。"
        data["goals"] = data.get("goals") or ["推动当前剧情闭环"]
        data["location"] = data.get("location") or "起始地点"
        data["locations"] = _normalize_npc_locations(data)
        repaired.append(SandboxNPC.model_validate(data))
    for npc in fallback:
        if len(repaired) >= min_count:
            break
        data = npc.model_dump()
        data["id"] = _unique_id(data["id"], seen)
        data["locations"] = _normalize_npc_locations(data)
        repaired.append(SandboxNPC.model_validate(data))
    while len(repaired) < min_count:
        index = len(repaired) + 1
        blueprint = _theme_stage_blueprint(request.theme)
        stage_index = index - 1
        repaired.append(
            SandboxNPC(
                id=_unique_id(f"support_npc_{index}", seen),
                name=f"{_cycle(blueprint['names'], stage_index)}{index}",
                role=_cycle(blueprint["roles"], stage_index) or "阶段推进 NPC",
                personality="提供补充线索、条件或阶段反馈，不直接替玩家完成目标。",
                goals=["补足世界互动密度", "让玩家获得新的地点、条件或人物信息"],
                location=_cycle(blueprint["locations"], stage_index) or "阶段推进现场",
                locations=[_cycle(blueprint["locations"], stage_index) or "阶段推进现场"],
            )
        )
    return repaired


def _normalize_npc_locations(data: dict[str, Any]) -> list[str]:
    raw_locations = data.get("locations")
    values = raw_locations if isinstance(raw_locations, list) else []
    locations = [str(item).strip() for item in values if str(item or "").strip()]
    primary = str(data.get("location") or "").strip()
    if primary and primary not in locations:
        locations.insert(0, primary)
    if not primary and locations:
        data["location"] = locations[0]
    return list(dict.fromkeys(locations))


def _repair_tasks(tasks: list[SandboxTask], fallback: list[SandboxTask], min_count: int, request: WorldGenerateRequest) -> list[SandboxTask]:
    repaired = []
    seen = set()
    for index, task in enumerate(tasks or []):
        data = task.model_dump()
        data["id"] = _unique_id(data.get("id") or data.get("title") or f"task_{index + 1}", seen)
        data["title"] = data.get("title") or f"任务 {index + 1}"
        data["description"] = data.get("description") or data["title"]
        data["status"] = data.get("status") or "pending"
        repaired.append(SandboxTask.model_validate(data))
    for task in fallback:
        if len(repaired) >= min_count:
            break
        data = task.model_dump()
        data["id"] = _unique_id(data["id"], seen)
        repaired.append(SandboxTask.model_validate(data))
    target_label = _derive_target_label(request.theme)
    profile = _complexity_profile(request)
    progress_actions = max(1, min_count - 2)
    progress_step = max(5, (int(profile["progress_target"]) + progress_actions - 1) // progress_actions)
    while len(repaired) < min_count:
        index = len(repaired) + 1
        stage_number = max(1, len(repaired) - 1)
        repaired.append(
            SandboxTask(
                id=_unique_id(f"stage_{index}_progress", seen),
                title=f"阶段{index}：推进主线准备",
                description=f"围绕“{target_label}”完成第 {index} 个调查、训练、沟通或资源准备节点。",
                completion={"stats": {"progress": {"min": min(int(profile["progress_target"]), stage_number * progress_step)}}},
            )
        )
    return repaired


def _repair_actions(
    actions: list[SandboxAction],
    tasks: list[SandboxTask],
    npcs: list[SandboxNPC],
    fallback: list[SandboxAction],
    min_count: int,
    request: WorldGenerateRequest,
) -> list[SandboxAction]:
    repaired = []
    seen = set()
    npc_ids = [npc.id for npc in npcs]
    task_ids = [task.id for task in tasks]
    for index, action in enumerate(actions or []):
        data = action.model_dump()
        data["id"] = _unique_id(data.get("id") or data.get("label") or f"action_{index + 1}", seen)
        data["label"] = data.get("label") or f"推进：{tasks[min(index, len(tasks) - 1)].title if tasks else index + 1}"
        data["description"] = data.get("description") or "根据玩家已获得的线索推进世界状态。"
        effect = data.get("effect") if isinstance(data.get("effect"), dict) else {}
        if "scene" not in effect:
            effect["scene"] = data["description"]
        if "complete_task" not in effect and index < len(task_ids):
            effect["complete_task"] = task_ids[index]
        if "active_npc_id" not in effect and npc_ids:
            effect["active_npc_id"] = npc_ids[min(index, len(npc_ids) - 1)]
        data["effect"] = effect
        repaired.append(SandboxAction.model_validate(data))
    for action in fallback:
        if len(repaired) >= max(min_count, len(tasks)):
            break
        data = action.model_dump()
        data["id"] = _unique_id(data["id"], seen)
        repaired.append(SandboxAction.model_validate(data))
    while len(repaired) < max(min_count, len(tasks)):
        index = len(repaired)
        task = tasks[min(index, len(tasks) - 1)] if tasks else None
        npc = npcs[min(index, len(npcs) - 1)] if npcs else None
        progress_gain = max(5, (int(_complexity_profile(request)["progress_target"]) + max(1, len(tasks) - 2) - 1) // max(1, len(tasks) - 2))
        effect: dict[str, Any] = {
            "scene": f"你完成了一个阶段性推进：{task.title if task else '继续推进目标'}。",
            "increase_player": {"progress": progress_gain},
        }
        if task:
            effect["complete_task"] = task.id
        if npc:
            effect["active_npc_id"] = npc.id
            effect["set_player"] = {"location": npc.location or "阶段推进现场"}
        repaired.append(
            SandboxAction(
                id=_unique_id(f"advance_stage_{index + 1}", seen),
                label=f"推进阶段 {index + 1}",
                description=f"完成与当前主题相关的第 {index + 1} 个阶段行动。",
                effect=effect,
            )
        )
    return repaired


def _apply_final_task_gate(config: SandboxWorldConfig, request: WorldGenerateRequest) -> None:
    if not request.final_task_requires_previous or not config.tasks:
        return
    profile = _complexity_profile(request)
    final_task = config.tasks[-1]
    final_completion = final_task.completion if isinstance(final_task.completion, dict) else {}
    final_completion = {**final_completion}
    stats = final_completion.get("stats") if isinstance(final_completion.get("stats"), dict) else {}
    stats = {**stats, "progress": {"min": int(profile["progress_target"])}}
    final_completion["stats"] = stats
    if len(config.tasks) > 1:
        final_completion["previous_tasks"] = [task.id for task in config.tasks[:-1]]
    final_task.completion = final_completion


def _safe_id_part(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower()).strip("_")
    return text or "ai_world"


def _unique_id(value: str, seen: set[str]) -> str:
    base = _safe_id_part(str(value))
    candidate = base
    index = 2
    while candidate in seen:
        candidate = f"{base}_{index}"
        index += 1
    seen.add(candidate)
    return candidate


def _three_act_xianxia(request: WorldGenerateRequest) -> SandboxWorldConfig:
    player_name = _player_name(request)
    theme = _theme(request, "退婚逆袭")
    name = request.world_name.strip() or f"{theme}：三幕式修仙世界"
    return SandboxWorldConfig(
        world_id=_world_id("three_act_xianxia"),
        name=name,
        description="一键生成的三幕式修仙 MVP：退婚受辱、出门历练、强者归来、最终抉择。",
        lore=(
            "这是一个独立修仙沙盒，不依赖任何外部游戏项目。\n"
            "世界规则：玩家通过世界动作推进剧情状态，通过 Agent 对话与当前 NPC 互动。\n"
            "三幕式结构：第一幕让主角遭遇公开挫败；第二幕出门历练并获得机缘；第三幕强者归来，面对旧人求和并做出结局选择。\n"
            "胜利条件：完成五步 MVP 闭环，并在最终选择中得到明确结局。"
        ),
        opening_scene=f"{player_name}站在青石宗外院。柳家来人当众退婚，旁人窃笑，师父云衡真人沉默地看着你，等你作出第一步反应。",
        player={
            "name": player_name,
            "location": "青石宗外院",
            "role": "被轻视的外院弟子",
            "status": "贫弱、受辱、尚未觉醒机缘",
            "realm": "炼气一层",
            "reputation": -10,
        },
        npcs=[
            SandboxNPC(id="mentor", name="云衡真人", role="师父", personality="严厉但护短，不替玩家做选择，只推动玩家成长。", goals=["让玩家走完试炼", "在关键处给出克制提醒"], location="青石宗外院"),
            SandboxNPC(id="ex_fiancee", name="柳如霜", role="退婚者", personality="骄傲、现实，后期会因玩家成长而动摇。", goals=["切断婚约", "在玩家强大后重新评估关系"], location="柳家席位"),
            SandboxNPC(id="rival", name="赵玄", role="嘲讽者", personality="势利、爱抢风头，专门制造压迫感。", goals=["羞辱玩家", "阻止玩家翻身"], location="青石宗外院"),
        ],
        story_goals=[
            "走完退婚逆袭三幕式 MVP",
            "与不同 NPC 对话，验证 Agent 会按 NPC 身份回复",
            "在最终选择中决定是否与柳如霜和解",
        ],
        tasks=[
            SandboxTask(id="act1_breakup", title="第一幕：经历退婚受辱", description="公开退婚发生，玩家声望跌入谷底。"),
            SandboxTask(id="act2_leave", title="第二幕：离宗历练", description="玩家离开宗门，进入云雾山寻找机缘。"),
            SandboxTask(id="act2_power", title="第二幕：获得机缘", description="玩家获得古玉灵印，实力与自信提升。"),
            SandboxTask(id="act3_return", title="第三幕：强者归来", description="玩家回到宗门，让旧人重新面对自己。"),
            SandboxTask(id="act3_choice", title="终幕：作出情感抉择", description="选择和解或拒绝，形成完整结局。"),
        ],
        actions=[
            SandboxAction(id="accept_breakup_scene", label="经历退婚", description="世界动作：立即进入第一幕，不等待 Agent。", effect={"set_player": {"status": "被退婚后强压怒意", "reputation": -30}, "active_npc_id": "mentor", "complete_task": "act1_breakup", "scene": "柳如霜收回婚书，赵玄当众讥笑。云衡真人只问你：是沉下去，还是走出去？"}),
            SandboxAction(id="leave_home", label="出门历练", description="进入第二幕，玩家离开宗门。", effect={"set_player": {"location": "云雾山", "status": "独自历练", "reputation": -20}, "active_npc_id": "mentor", "complete_task": "act2_leave", "scene": "你离开青石宗，云衡真人递来一枚旧玉牌，只说：活着回来。"}),
            SandboxAction(id="gain_power", label="获得古玉灵印", description="完成机缘节点，玩家变强。", effect={"set_player": {"status": "获得古玉灵印", "realm": "炼气五层", "reputation": 20}, "active_npc_id": "mentor", "complete_task": "act2_power", "scene": "云雾山深处，古玉灵印认主。你终于有了回去说话的底气。"}),
            SandboxAction(id="return_strong", label="强者归来", description="进入第三幕，让 NPC 态度发生变化。", effect={"set_player": {"location": "青石宗演武场", "status": "强者归来", "reputation": 80}, "active_npc_id": "ex_fiancee", "complete_task": "act3_return", "scene": "你回到演武场，赵玄沉默，柳如霜第一次认真看向你。"}),
            SandboxAction(id="make_choice_reconcile", label="选择和解", description="最终选择之一：给旧关系一次重新开始的机会。", effect={"set_player": {"status": "选择和解，故事进入温和结局"}, "active_npc_id": "ex_fiancee", "complete_task": "act3_choice", "scene": "你没有忘记屈辱，但选择把决定权拿回自己手里。柳如霜低头道歉，故事进入和解结局。"}),
            SandboxAction(id="make_choice_refuse", label="选择不再一起", description="最终选择之二：拒绝复合，独自继续登高。", effect={"set_player": {"status": "拒绝复合，故事进入独行结局"}, "active_npc_id": "ex_fiancee", "complete_task": "act3_choice", "scene": "你平静拒绝柳如霜。不是报复，而是你已经走向更大的天地。故事进入独行结局。"}),
        ],
        initial_memories=["玩家刚进入三幕式退婚逆袭世界。"],
        metadata={"generated_by": "agent_world_generator", "template": "three_act_xianxia", "mvp_loop": True},
    )


def _short_drama_reversal(request: WorldGenerateRequest) -> SandboxWorldConfig:
    return _generic_theme_fallback(request)


def _generic_theme_fallback(request: WorldGenerateRequest) -> SandboxWorldConfig:
    player_name = _player_name(request)
    theme = _theme(request, "用户自定义目标")
    world_name = request.world_name.strip() or _derive_world_name(theme)
    start_location = _derive_start_location(theme)
    target_label = _derive_target_label(theme)
    blueprint = _theme_stage_blueprint(theme)
    if start_location == "起点":
        start_location = blueprint["locations"][0]
    profile = _complexity_profile(request)
    min_npcs = int(profile["min_npcs"])
    min_tasks = int(profile["min_tasks"])
    min_actions = int(profile["min_actions"])
    progress_target = int(profile["progress_target"])
    progress_actions = max(1, min_tasks - 2)
    progress_step = max(5, (progress_target + progress_actions - 1) // progress_actions)
    npcs = [
        SandboxNPC(
            id="guide",
            name="引导员",
            role="起点引导 NPC",
            personality="只根据用户主题给出下一步线索，不擅自改写题材。",
            goals=["帮助玩家理解当前目标", "指出下一步要找的人或地点"],
            location=start_location,
        ),
        SandboxNPC(
            id="key_contact",
            name=_cycle(blueprint["names"], 1),
            role=_cycle(blueprint["roles"], 1),
            personality="务实，会确认玩家是否满足进入下一步的条件。",
            goals=[f"帮助或考验玩家完成：{target_label}"],
            location=_cycle(blueprint["locations"], 1),
        ),
    ]
    for index in range(3, min_npcs + 1):
        stage_index = index - 1
        npcs.append(
            SandboxNPC(
                id=f"support_npc_{index}",
                name=f"{_cycle(blueprint['names'], stage_index)}{index}",
                role=_cycle(blueprint["roles"], stage_index),
                personality="会提供一个具体条件、地点或资源线索，但不会替玩家直接完成目标。",
                goals=[f"推进：{_cycle(blueprint['tasks'], stage_index)}"],
                location=_cycle(blueprint["locations"], stage_index),
            )
        )
    tasks = [
        SandboxTask(
            id="clarify_goal",
            title="确认目标",
            description=f"向引导员确认当前目标：{target_label}",
            completion={"actions": ["clarify_goal"]},
        ),
        SandboxTask(
            id="meet_key_contact",
            title=f"找到{_cycle(blueprint['roles'], 1)}",
            description=f"前往{_cycle(blueprint['locations'], 1)}，与{_cycle(blueprint['names'], 1)}确认下一步条件。",
            completion={"location": _cycle(blueprint["locations"], 1)},
        ),
    ]
    while len(tasks) < max(1, min_tasks - 1):
        index = len(tasks) + 1
        stage_number = max(1, len(tasks) - 1)
        tasks.append(
            SandboxTask(
                id=f"stage_{index}_progress",
                title=f"阶段{index}：{_cycle(blueprint['tasks'], stage_number)}",
                description=f"前往{_cycle(blueprint['locations'], stage_number)}，通过{_cycle(blueprint['verbs'], stage_number)}提升主线进度。",
                completion={"stats": {"progress": {"min": min(progress_target, stage_number * progress_step)}}},
            )
        )
    final_completion: dict[str, Any] = {"stats": {"progress": {"min": progress_target}}}
    if request.final_task_requires_previous:
        final_completion["previous_tasks"] = [task.id for task in tasks]
    tasks.append(
        SandboxTask(
            id="finish_goal",
            title="完成目标",
            description=f"完成用户主题中的目标：{target_label}",
            completion=final_completion,
        )
    )
    actions = [
        SandboxAction(
            id="clarify_goal",
            label="确认目标",
            description="让引导员根据用户主题说明下一步。",
            effect={
                "scene": f"引导员确认：你的目标是{target_label}。下一步去{_cycle(blueprint['locations'], 1)}找{_cycle(blueprint['names'], 1)}。",
                "complete_task": "clarify_goal",
                "active_npc_id": "guide",
            },
        ),
        SandboxAction(
            id="go_to_key_contact",
            label=f"前往{_cycle(blueprint['locations'], 1)}",
            description=f"移动到{_cycle(blueprint['names'], 1)}所在的位置。",
            effect={
                "scene": f"你来到{_cycle(blueprint['locations'], 1)}，{_cycle(blueprint['names'], 1)}正在等待你说明来意。",
                "active_npc_id": "key_contact",
                "set_player": {"location": _cycle(blueprint["locations"], 1)},
            },
        ),
    ]
    for index, task in enumerate(tasks[2:-1], start=3):
        npc = npcs[min(index - 1, len(npcs) - 1)]
        actions.append(
            SandboxAction(
                id=f"advance_{task.id}",
                label=f"推进{task.title}",
                description=f"完成与用户主题相关的第 {index} 个阶段行动。",
                effect={
                    "scene": f"你在{npc.location}完成了“{task.title}”，当前能力和准备度都有提升。",
                    "active_npc_id": npc.id,
                    "set_player": {"location": npc.location, "status": f"{task.title}完成"},
                    "increase_player": {"progress": progress_step},
                    "complete_task": task.id,
                },
            )
        )
    actions.append(
        SandboxAction(
            id="finish_goal",
            label="完成目标",
            description="在满足前置条件和准备度后完成用户主题目标。",
            effect={
                "scene": f"你在完成前置条件后，正式达成目标：{target_label}。",
                "complete_task": "finish_goal",
                "active_npc_id": "key_contact",
                "set_player": {"status": "目标完成"},
                "increase_player": {"progress": progress_step},
            },
        )
    )
    while len(actions) < max(min_actions, len(tasks)):
        index = len(actions) + 1
        npc = npcs[min(index - 1, len(npcs) - 1)]
        actions.insert(
            -1,
            SandboxAction(
                id=f"optional_stage_{index}",
                label=f"补充阶段行动 {index}",
                description="增加一个可探索的中间行动，避免世界过短。",
                effect={
                    "scene": f"你在{npc.location}完成了一次额外准备，为最终目标积累条件。",
                    "active_npc_id": npc.id,
                    "set_player": {"location": npc.location, "status": "持续推进中"},
                    "increase_player": {"progress": progress_step},
                },
            ),
        )
    return SandboxWorldConfig(
        world_id=_world_id(_safe_id_part(request.template or "custom_world")),
        name=world_name,
        description=f"根据用户主题生成的通用可玩世界：{theme}",
        lore=(
            f"用户原始主题：{theme}\n"
            "这是 AI 生成失败后的保真降级世界，只保留用户输入中的角色、目标和关键词，"
            "不注入任何固定题材模板。建议重新生成或在后台设定中继续细化 NPC、任务和判定字段。"
        ),
        opening_scene=f"{player_name}来到{start_location}，准备开始目标：{target_label}。",
        player={
            "name": player_name,
            "location": start_location,
            "role": _derive_player_role(theme),
            "status": f"准备开始：{target_label}",
            "inventory": _derive_initial_items(theme),
            "progress": 0,
        },
        npcs=npcs,
        story_goals=[
            target_label,
            "根据用户主题补齐关键人物、地点、道具和判定字段",
            "完成一个可验证的 MVP 闭环",
        ],
        tasks=tasks,
        actions=actions,
        initial_memories=[
            f"用户原始主题：{theme}",
            "这是 AI 生成失败后的主题保真降级世界，不应引入用户没有提到的题材。",
        ],
        metadata={
            "generated_by": "theme_preserving_fallback",
            "template": request.template,
            "theme": request.theme,
            "complexity": profile,
            "final_task_requires_previous": request.final_task_requires_previous,
            "mvp_loop": True,
        },
    )
