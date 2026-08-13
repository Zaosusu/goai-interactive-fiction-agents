from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.creator_assistant.compiler import CreatorGraphCompiler, CreatorGraphValidationError
from app.agents.creator_assistant.layout import CreatorGraphLayoutCompiler
from app.agents.creator_assistant.schema import CreatorToolCall, CreatorToolDefinition
from app.agents.playtest_validation import PlaytestAgent
from app.agents.story_expansion import StoryExpansionAgent, StoryExpansionCompiler, StoryExpansionRequest
from app.agents.story_authoring.schema import StoryAuthoringRequest
from app.agents.story_authoring.service import StoryAuthoringService
from app.agents.world_review import WorldReviewAgent
from app.core.model_config import LLMProviderConfig
from app.worlds.sandbox.adapter import SandboxWorldAdapter
from app.worlds.sandbox.models import (
    SandboxAction,
    SandboxNPC,
    SandboxTask,
    SandboxWorldConfig,
    VisualAssetPlan,
    VisualAssetRequest,
)


class AuthorStoryArguments(BaseModel):
    brief: str = Field(min_length=10, max_length=20000)
    genre: str = "修仙剧情冒险"
    tone: str = "沉浸、克制、有悬念"
    audience: str = "喜欢角色互动与剧情选择的玩家"
    target_minutes: int = Field(default=30, ge=10, le=180)
    target_scene_count: int = Field(default=6, ge=3, le=16)
    target_character_count: int = Field(default=4, ge=2, le=12)
    constraints: list[str] = Field(default_factory=list)


class ExpandStoryArguments(BaseModel):
    brief: str = Field(min_length=4, max_length=12000)
    target_node_count: int = Field(default=10, ge=1, le=100)
    source_node_id: str = Field(default="", max_length=100)
    reconnect_node_id: str = Field(default="", max_length=100)
    insertion_mode: str = Field(default="after", pattern="^(after|branch)$")


class VisualArguments(BaseModel):
    include_characters: bool = True
    include_scenes: bool = True
    max_characters: int | None = Field(default=None, ge=1, le=30)
    max_scenes: int | None = Field(default=None, ge=1, le=50)
    style_prompt: str = "high quality game concept art"
    auto_remove_character_background: bool = True


class EmptyArguments(BaseModel):
    pass


class LayoutGraphArguments(BaseModel):
    scope: Literal["all", "downstream"] = "all"
    root_node_id: str = Field(default="", max_length=100)


_ARGUMENT_MODELS: dict[str, type[BaseModel]] = {
    "author_story": AuthorStoryArguments,
    "expand_story": ExpandStoryArguments,
    "layout_creator_graph": LayoutGraphArguments,
    "validate_creator_graph": EmptyArguments,
    "compile_creator_graph": EmptyArguments,
    "review_playable_world": EmptyArguments,
    "save_world": EmptyArguments,
    "plan_visual_assets": VisualArguments,
    "generate_visual_assets": VisualArguments,
    "bind_visual_assets": EmptyArguments,
    "publish_to_play": EmptyArguments,
}


class CreatorToolRegistry:
    def __init__(self) -> None:
        self._definitions = [
            _definition("author_story", "创作完整剧情", "调用 StoryAuthoringAgent 完成生成、Schema 审查、自动修稿和复验，再编译为 Creator Graph。", AuthorStoryArguments, long_running=True, stage="story_authoring", owner_agent="StoryAuthoringAgent", capability_type="agent"),
            _definition("expand_story", "扩写当前剧情", "调用 StoryExpansionAgent 在保留现有 Creator Graph 的前提下生成指定数量的新节点，再由确定性编译器串联并接回原流程。", ExpandStoryArguments, long_running=True, stage="story_expansion", owner_agent="StoryExpansionAgent", capability_type="agent"),
            _definition("layout_creator_graph", "整理剧情节点", "确定性整理 Creator Graph 的画布坐标。可整理全部节点，或从选中节点开始整理下游；不会修改剧情正文、连接、条件或效果。", LayoutGraphArguments, stage="creator", owner_agent="CreatorGraphLayoutCompiler", capability_type="compiler"),
            _definition("validate_creator_graph", "校验剧情图", "检查节点、连线、分支、结局和可达性，不修改内容。", EmptyArguments, stage="creator", owner_agent="CreatorGraphValidator", capability_type="validator"),
            _definition("compile_creator_graph", "编译剧情图", "规范化当前 Creator Graph，生成可保存、可运行的标准结构。", EmptyArguments, stage="creator", owner_agent="CreatorGraphCompiler", capability_type="compiler"),
            _definition("review_playable_world", "试玩审查", "调用 WorldReviewAgent 与 PlaytestAgent 审查世界结构并自动模拟可玩闭环。", EmptyArguments, stage="playtest", owner_agent="WorldReviewAgent + PlaytestAgent", capability_type="agent"),
            _definition("plan_visual_assets", "规划视觉资产", "调用 VisualAssetGenerationAgent 从角色与场景生成可检查的资产计划，不生成图片。", VisualArguments, long_running=True, stage="visual_assets", owner_agent="VisualAssetGenerationAgent", capability_type="agent"),
            _definition("generate_visual_assets", "生成视觉资产", "调用 VisualAssetGenerationAgent 执行角色立绘与场景背景生成；角色图会自动本地抠图并校验透明 PNG。", VisualArguments, long_running=True, stage="visual_assets", owner_agent="VisualAssetGenerationAgent", capability_type="agent"),
            _definition("bind_visual_assets", "绑定视觉资产", "把已生成图片确定性绑定到 Creator 角色 portrait 和节点 background。", EmptyArguments, stage="visual_assets", owner_agent="VisualAssetBindingCompiler", capability_type="compiler"),
            _definition("save_world", "保存世界", "把 Creator Graph 编译为 SandboxWorldConfig 并保存到世界库。", EmptyArguments, destructive=True, stage="world_store", owner_agent="WorldStore", capability_type="store"),
            _definition("publish_to_play", "发布到玩家端", "校验并保存当前世界，使其出现在 /play 的可玩世界列表。", EmptyArguments, destructive=True, stage="player", owner_agent="PlayerWorldPublisher", capability_type="store"),
        ]
        self._by_id = {item.id: item for item in self._definitions}

    def list(self) -> list[CreatorToolDefinition]:
        return list(self._definitions)

    def prompt_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "description": item.description,
                "input_schema": item.input_schema,
                "long_running": item.long_running,
            }
            for item in self._definitions
            if item.available
        ]

    def validate_call(self, call: CreatorToolCall) -> CreatorToolCall:
        if call.tool not in self._by_id:
            raise ValueError(f"unknown creator tool: {call.tool}")
        definition = self._by_id[call.tool]
        if not definition.available:
            raise ValueError(f"creator tool is unavailable: {call.tool}")
        model = _ARGUMENT_MODELS[call.tool]
        arguments = model.model_validate(_repair_bounded_arguments(call.tool, call.arguments)).model_dump(
            mode="json", exclude_none=True
        )
        return call.model_copy(update={"arguments": arguments})

    def normalize_calls(self, calls: list[CreatorToolCall]) -> list[CreatorToolCall]:
        result: list[CreatorToolCall] = []
        seen: set[str] = set()
        for call in calls:
            validated = self.validate_call(call)
            key = f"{validated.tool}:{validated.arguments}"
            if key not in seen:
                result.append(validated)
                seen.add(key)
        return result


def _repair_bounded_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Repair common LLM numeric overflows before strict schema validation."""

    repaired = copy.deepcopy(arguments or {})
    bounds = {
        "author_story": {
            "target_minutes": (10, 180),
            "target_scene_count": (3, 16),
            "target_character_count": (2, 12),
        },
        "expand_story": {"target_node_count": (1, 100)},
        "plan_visual_assets": {"max_characters": (1, 30), "max_scenes": (1, 50)},
        "generate_visual_assets": {"max_characters": (1, 30), "max_scenes": (1, 50)},
    }
    for field, (minimum, maximum) in bounds.get(tool, {}).items():
        value = repaired.get(field)
        if value is None or isinstance(value, bool):
            continue
        try:
            repaired[field] = max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            continue
    return repaired


ProgressCallback = Callable[[str, str], Awaitable[None]]
RESERVED_PROJECT_NAMES = {"未命名互动剧情", "尚未命名", "Untitled interactive story"}


def _require_explicit_project_name(project: dict[str, Any]) -> str:
    world = project.get("world") if isinstance(project, dict) else None
    name = str(world.get("name") or "").strip() if isinstance(world, dict) else ""
    if not name or name in RESERVED_PROJECT_NAMES:
        raise ValueError("作品名称不能为空或使用占位名称，请先填写明确名称。")
    return name


class CreatorToolExecutor:
    """Executes deterministic and Agent-backed Creator tools in an explicit workflow."""

    def __init__(
        self,
        *,
        resolve_llm_config: Callable[[str], LLMProviderConfig],
        resolve_visual_request: Callable[[VisualAssetRequest], VisualAssetRequest],
        world_store: Any,
        visual_asset_agent: Any,
        visual_asset_store: Any,
        reset_world_agent: Callable[[str], Any] | None = None,
        story_service: StoryAuthoringService | None = None,
        story_expansion_agent: StoryExpansionAgent | None = None,
    ) -> None:
        self.resolve_visual_request = resolve_visual_request
        self.world_store = world_store
        self.visual_asset_agent = visual_asset_agent
        self.visual_asset_store = visual_asset_store
        self.reset_world_agent = reset_world_agent
        self.story_service = story_service or StoryAuthoringService(resolve_llm_config=resolve_llm_config)
        self.resolve_llm_config = resolve_llm_config
        self.story_expansion_agent = story_expansion_agent or StoryExpansionAgent()
        self.story_expansion_compiler = StoryExpansionCompiler()
        self.compiler = CreatorGraphCompiler()
        self.layout_compiler = CreatorGraphLayoutCompiler()

    async def execute(
        self,
        call: CreatorToolCall,
        project: dict[str, Any],
        artifacts: dict[str, Any],
        *,
        should_cancel: Callable[[], bool],
        progress: ProgressCallback,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        if should_cancel():
            raise InterruptedError("creator workflow cancelled")
        if call.tool == "author_story":
            response = await self.story_service.generate(
                StoryAuthoringRequest(**call.arguments),
                progress=progress,
            )
            authored_project = response.project
            current_world_id = str(project.get("world", {}).get("world_id") or "").strip()
            if current_world_id:
                authored_project = copy.deepcopy(authored_project)
                authored_project["world"]["world_id"] = current_world_id
            return authored_project, {"story_authoring": response.model_dump(mode="json")}, response.reply
        if call.tool == "expand_story":
            arguments = ExpandStoryArguments.model_validate(call.arguments)
            source_node_id = arguments.source_node_id or _default_expansion_source(project)
            source = next((node for node in project.get("nodes", []) if node.get("id") == source_node_id), None)
            original_successor = str(source.get("next") or "") if source else ""
            reconnect_node_id = (
                original_successor
                if arguments.insertion_mode == "after"
                else arguments.reconnect_node_id or original_successor
            )
            expansion_payload = arguments.model_dump(mode="json")
            expansion_payload.update(
                {
                    "source_node_id": source_node_id,
                    "reconnect_node_id": reconnect_node_id,
                    "project": project,
                    "expansion_llm": self.resolve_llm_config("story_expansion"),
                }
            )
            expansion_request = StoryExpansionRequest.model_validate(expansion_payload)
            await progress(
                "StoryExpansionAgent · 生成扩写内容",
                f"正在为当前故事生成 {arguments.target_node_count} 个连续节点。",
            )
            response = await self.story_expansion_agent.expand(expansion_request, progress=progress)
            await progress(
                "StoryExpansionCompiler · 串联节点",
                "正在保留原剧情并把新节点接入选定位置。",
            )
            expanded, report = self.story_expansion_compiler.apply(expansion_request, response.draft)
            if not report.valid:
                raise CreatorGraphValidationError(report)
            return (
                expanded,
                {"story_expansion": response.model_dump(mode="json")},
                f"StoryExpansionAgent 已生成并串联 {len(response.draft.nodes)} 个新节点。",
            )
        if call.tool == "layout_creator_graph":
            arguments = LayoutGraphArguments.model_validate(call.arguments)
            laid_out, layout_report = self.layout_compiler.layout(
                self.compiler.normalize(project),
                scope=arguments.scope,
                root_node_id=arguments.root_node_id,
            )
            graph_report = self.compiler.validate(laid_out)
            if not graph_report.valid:
                raise CreatorGraphValidationError(graph_report)
            scope_label = "全部节点" if arguments.scope == "all" else f"节点 {arguments.root_node_id} 及其下游"
            detail = f"已整理{scope_label}，移动 {layout_report.moved_node_count} 个节点；剧情内容和连接关系未改变。"
            return laid_out, {"graph_layout": layout_report.as_dict()}, detail
        if call.tool == "validate_creator_graph":
            report = self.compiler.validate(project)
            if not report.valid:
                raise CreatorGraphValidationError(report)
            return project, {"graph_report": report.model_dump(mode="json")}, "Creator Graph 校验通过。"
        if call.tool == "compile_creator_graph":
            normalized = self.compiler.normalize(project)
            report = self.compiler.validate(normalized)
            if not report.valid:
                raise CreatorGraphValidationError(report)
            return normalized, {"graph_report": report.model_dump(mode="json")}, "Creator Graph 已完成规范化编译。"
        if call.tool == "review_playable_world":
            world = compile_creator_world(project, artifacts)
            world_review = WorldReviewAgent().review(world)
            playtest_review = PlaytestAgent().simulate_adapter(SandboxWorldAdapter(world))
            review = {
                "passed": world_review.passed and playtest_review.passed,
                "world_review": world_review.model_dump(mode="json"),
                "playtest_review": playtest_review.model_dump(mode="json"),
            }
            if not review["passed"]:
                messages = [
                    issue.message
                    for report in [world_review, playtest_review]
                    for issue in report.issues
                    if issue.severity == "error"
                ]
                raise ValueError("试玩审查未通过：" + "；".join(messages[:8]))
            steps = len(playtest_review.metadata.get("steps") or [])
            return project, {"playtest_review": review}, f"WorldReviewAgent 与 PlaytestAgent 审查通过，自动试玩 {steps} 步。"
        if call.tool == "plan_visual_assets":
            request = self.resolve_visual_request(_visual_request(project, call.arguments))
            plan = await self.visual_asset_agent.plan_async(request)
            self.visual_asset_store.save_plan(plan)
            return project, {"visual_plan": _redact_sensitive(plan.model_dump(mode="json"))}, f"已规划 {len(plan.assets)} 项视觉资产。"
        if call.tool == "generate_visual_assets":
            plan_payload = artifacts.get("visual_plan") or project.get("pipeline_artifacts", {}).get("visual_plan")
            if not isinstance(plan_payload, dict):
                raise ValueError("generate_visual_assets requires an earlier plan_visual_assets stage")
            request = self.resolve_visual_request(
                _visual_request(project, call.arguments).model_copy(update={"plan": plan_payload})
            )

            async def generation_progress(status: str, title: str, detail: str) -> None:
                await progress(title, detail)

            result = await self.visual_asset_agent.generate_async(
                request,
                should_cancel=should_cancel,
                progress_callback=generation_progress,
            )
            self.visual_asset_store.save_result(result)
            return project, {"visual_result": _redact_sensitive(result.model_dump(mode="json"))}, f"已生成 {len(result.generated)} 项视觉资产，失败 {len(result.failed)} 项。"
        if call.tool == "bind_visual_assets":
            result = artifacts.get("visual_result")
            if not isinstance(result, dict):
                result = self._latest_stored_visual_result(project)
            if not isinstance(result, dict):
                result = project.get("pipeline_artifacts", {}).get("visual_result")
            if not isinstance(result, dict):
                raise ValueError("bind_visual_assets requires generated visual assets")
            bound, counts = bind_visual_assets(project, result)
            return (
                bound,
                {"visual_bindings": counts, "visual_result": _redact_sensitive(copy.deepcopy(result))},
                f"已绑定 {counts['characters']} 张角色立绘和 {counts['scenes']} 张场景背景。",
            )
        if call.tool in {"save_world", "publish_to_play"}:
            report = self.compiler.validate(project)
            if not report.valid:
                raise CreatorGraphValidationError(report)
            published = call.tool == "publish_to_play"
            world = compile_creator_world(project, artifacts, published=published)
            saved = self.world_store.save(world)
            if self.reset_world_agent is not None:
                self.reset_world_agent(saved.world_id)
            key = "published_world" if published else "saved_world"
            detail = f"《{saved.name}》已发布到玩家端。" if published else f"《{saved.name}》已保存。"
            return project, {key: saved.model_dump(mode="json")}, detail
        raise ValueError(f"unsupported creator tool: {call.tool}")

    def _latest_stored_visual_result(self, project: dict[str, Any]) -> dict[str, Any] | None:
        if self.visual_asset_store is None:
            return None
        world = project.get("world") if isinstance(project.get("world"), dict) else {}
        world_id = str(world.get("world_id") or "").strip()
        title = str(world.get("name") or "").strip()
        try:
            artifacts = self.visual_asset_store.list()
        except Exception:
            return None
        exact_world = [item for item in artifacts if world_id and str(item.get("world_id") or "") == world_id]
        matching_title = [item for item in artifacts if title and str(item.get("title") or "") == title]
        for artifact in [*exact_world, *matching_title]:
            artifact_id = str(artifact.get("artifact_id") or "")
            if not artifact_id:
                continue
            try:
                payload = self.visual_asset_store.load(artifact_id)
            except Exception:
                continue
            result = payload.get("result") if isinstance(payload, dict) else None
            if isinstance(result, dict) and isinstance(result.get("generated"), list) and result["generated"]:
                return result
        return None


def compile_creator_world(project: dict[str, Any], artifacts: dict[str, Any] | None = None, *, published: bool = False) -> SandboxWorldConfig:
    project_name = _require_explicit_project_name(project)
    compiler = CreatorGraphCompiler()
    graph = compiler.normalize(project)
    report = compiler.validate(graph)
    if not report.valid:
        raise CreatorGraphValidationError(report)
    world_data = graph["world"]
    player = copy.deepcopy(world_data.get("player") or {})
    nodes = graph.get("nodes") or []
    start = next((item for item in nodes if item.get("id") == "start"), nodes[0] if nodes else {})
    npcs = [
        SandboxNPC(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or item.get("id") or "NPC"),
            role=str(item.get("role") or "NPC"),
            personality=str(item.get("personality") or ""),
            location=str(item.get("location") or player.get("location") or ""),
            portrait={"image": str(item.get("portrait") or "")} if item.get("portrait") else {},
        )
        for item in graph.get("characters", [])
        if item.get("id")
    ]
    actions: list[SandboxAction] = []
    tasks: list[SandboxTask] = []
    for node in nodes:
        node_id = str(node.get("id") or "")
        if node.get("next"):
            actions.append(SandboxAction(id=f"go_{node_id}_next", label=f"继续：{node.get('title') or node_id}", description=str(node.get("content") or ""), effect={"scene": node.get("content") or node.get("title"), "active_npc_id": node.get("character") or ""}))
        for index, choice in enumerate(node.get("choices") or [], start=1):
            actions.append(SandboxAction(id=f"choice_{node_id}_{index}", label=str(choice.get("text") or f"选择 {index}"), description=f"节点 {node_id} 的选项", effect=copy.deepcopy(choice.get("effects") or {})))
        if node.get("type") == "ending":
            tasks.append(SandboxTask(id=f"ending_{node_id}", title=str(node.get("title") or node_id), description=str(node.get("content") or ""), completion=copy.deepcopy(node.get("conditions") or {})))
    metadata = {
        "creator_graph": _redact_sensitive(graph),
        "creator_graph_report": report.model_dump(mode="json"),
        "creator_pipeline_artifacts": _redact_sensitive(copy.deepcopy(artifacts or {})),
        "published_to_play": published,
    }
    return SandboxWorldConfig(
        world_id=str(world_data.get("world_id") or "creator_world"),
        name=project_name,
        description="由 Creator Agent 工具工作流生成。",
        lore=str(world_data.get("lore") or ""),
        opening_scene=str(start.get("content") or "故事开始。"),
        player=player,
        npcs=npcs,
        story_goals=[task.title for task in tasks] or ["推进互动剧情"],
        tasks=tasks,
        actions=actions,
        metadata=metadata,
    )


def creator_graph_to_script_graph(project: dict[str, Any]) -> dict[str, Any]:
    graph = CreatorGraphCompiler().normalize(project)
    world = graph["world"]
    nodes: list[dict[str, Any]] = [
        {
            "id": f"script:{world.get('world_id')}",
            "kind": "script",
            "label": world.get("name") or "Creator Story",
            "properties": {"public_background": world.get("lore") or "", "core_plot": world.get("lore") or ""},
        }
    ]
    edges: list[dict[str, Any]] = []
    for character in graph.get("characters", []):
        cid = str(character.get("id") or "")
        nodes.append({
            "id": f"character:{cid}",
            "kind": "character",
            "label": character.get("name") or cid,
            "properties": {
                "source_id": cid,
                "name": character.get("name") or cid,
                "role": character.get("role") or "NPC",
                "description": character.get("portrait_description") or character.get("personality") or "",
                "public_info": character.get("personality") or "",
            },
        })
    for node in graph.get("nodes", []):
        nid = str(node.get("id") or "")
        scene_id = f"scene:{nid}"
        nodes.append({
            "id": scene_id,
            "kind": "scene",
            "label": node.get("title") or nid,
            "properties": {
                "source_id": nid,
                "description": node.get("background_description") or node.get("content") or "",
                "text": node.get("content") or "",
            },
        })
        if node.get("character"):
            edges.append({"id": f"speaks:{node.get('character')}:{nid}", "source": f"character:{node.get('character')}", "target": scene_id, "type": "SPEAKS_IN", "properties": {}})
        targets = [node.get("next"), *[choice.get("next") for choice in node.get("choices") or []]]
        for index, target in enumerate(item for item in targets if item):
            edges.append({"id": f"next:{nid}:{index}", "source": scene_id, "target": f"scene:{target}", "type": "NEXT", "properties": {}})
    return {"schema_version": "script_graph.v1", "graph_id": str(world.get("world_id") or "creator_world"), "title": str(world.get("name") or "Creator Story"), "nodes": nodes, "edges": edges}


def bind_visual_assets(project: dict[str, Any], result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    updated = CreatorGraphCompiler().normalize(project)
    generated = result.get("generated") if isinstance(result.get("generated"), list) else []
    character_count = 0
    scene_count = 0
    for asset in generated:
        if not isinstance(asset, dict) or not asset.get("output_path"):
            continue
        kind = str(asset.get("kind") or "").lower()
        source_id = str(asset.get("source_id") or "")
        source_name = str(asset.get("source_name") or asset.get("display_name") or "")
        url = _asset_url(str(asset.get("output_path") or ""))
        if kind == "character":
            character = next((item for item in updated["characters"] if str(item.get("id") or "") == source_id or str(item.get("name") or "") == source_name), None)
            if character:
                character["portrait"] = url
                character_count += 1
        elif kind in {"scene", "location", "background"}:
            node = next((item for item in updated["nodes"] if str(item.get("id") or "") == source_id or str(item.get("title") or "") == source_name), None)
            if node:
                node["background"] = url
                scene_count += 1
    pipeline_artifacts = dict(updated.get("pipeline_artifacts") or {})
    pipeline_artifacts["visual_result"] = _redact_sensitive(copy.deepcopy(result))
    updated["pipeline_artifacts"] = pipeline_artifacts
    return updated, {"characters": character_count, "scenes": scene_count}


def _visual_request(project: dict[str, Any], arguments: dict[str, Any]) -> VisualAssetRequest:
    values = VisualArguments.model_validate(arguments)
    return VisualAssetRequest(
        script_graph=creator_graph_to_script_graph(project),
        include_characters=values.include_characters,
        include_scenes=values.include_scenes,
        auto_remove_character_background=values.auto_remove_character_background,
        max_characters=values.max_characters,
        max_scenes=values.max_scenes,
        style_prompt=values.style_prompt,
    )


def _default_expansion_source(project: dict[str, Any]) -> str:
    nodes = project.get("nodes") if isinstance(project.get("nodes"), list) else []
    start = next((node for node in nodes if str(node.get("id") or "") == "start"), None)
    if start is not None:
        return "start"
    return str(nodes[0].get("id") or "") if nodes else ""


def _definition(
    tool_id: str,
    name: str,
    description: str,
    model: type[BaseModel],
    *,
    destructive: bool = False,
    long_running: bool = False,
    stage: str,
    owner_agent: str,
    capability_type: str,
) -> CreatorToolDefinition:
    return CreatorToolDefinition(
        id=tool_id,
        name=name,
        description=description,
        input_schema=model.model_json_schema(),
        destructive=destructive,
        long_running=long_running,
        stage=stage,
        owner_agent=owner_agent,
        capability_type=capability_type,
    )


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("[redacted]" if item else "")
            if str(key).lower() in {"api_key", "authorization"} or str(key).lower().endswith("_api_key")
            else _redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _asset_url(path: str) -> str:
    normalized = path.replace("\\", "/")
    marker = "/output/"
    if normalized.startswith("output/"):
        return f"/{normalized}"
    if marker in normalized:
        return f"/output/{normalized.split(marker, 1)[1]}"
    if normalized.startswith("/output/"):
        return normalized
    return Path(normalized).as_posix()
