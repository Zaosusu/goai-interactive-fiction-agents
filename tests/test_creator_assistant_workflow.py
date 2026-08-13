from __future__ import annotations

from pathlib import Path
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agents.creator_assistant import CreatorAssistantAgent, CreatorGraphCompiler
from app.agents.creator_assistant.agent import _fallback_tool_calls
from app.agents.creator_assistant.layout import CreatorGraphLayoutCompiler
from app.agents.creator_assistant.routes import create_router
from app.agents.creator_assistant.schema import (
    CreatorAssistantOperation,
    CreatorAssistantRequest,
    CreatorAssistantResponse,
    CreatorHistoryMessageCreate,
    CreatorToolCall,
)
from app.agents.creator_assistant.store import CreatorHistoryStore, CreatorVersionStore, CreatorWorkflowStore
from app.agents.creator_assistant.tools import CreatorToolExecutor, CreatorToolRegistry, bind_visual_assets, compile_creator_world
from app.agents.story_expansion import StoryExpansionAgent, StoryExpansionCompiler, StoryExpansionDraft, StoryExpansionNode, StoryExpansionRequest
from app.core.model_config import LLMProviderConfig


def make_project() -> dict:
    return {
        "version": "creator_graph.v1",
        "world": {
            "world_id": "creator_workflow_test",
            "name": "Workflow test",
            "lore": "",
            "player": {"name": "Player", "location": "Opening", "stats": {}, "inventory": []},
        },
        "characters": [],
        "nodes": [
            {"id": "start", "type": "story", "title": "Start", "content": "", "next": "main", "choices": [], "x": 100, "y": 100},
            {"id": "main", "type": "story", "title": "Main", "content": "", "next": "ending", "choices": [], "x": 500, "y": 100},
            {"id": "ending", "type": "ending", "title": "Ending", "content": "", "next": "", "choices": [], "x": 900, "y": 100},
        ],
    }


def branch_operation() -> CreatorAssistantOperation:
    return CreatorAssistantOperation(
        type="create_branch",
        data={
            "source_node_id": "start",
            "choice_text": "Investigate the station",
            "nodes": [
                {"id": "branch_station", "title": "Abandoned station", "content": "Search the platform."},
                {"id": "branch_key", "title": "Old key", "content": "Find the old key."},
            ],
            "reconnect_node_id": "ending",
        },
    )


def test_creator_operation_schema_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CreatorAssistantOperation(type="add_node", data={"title": "Node", "unexpected": True})

    with pytest.raises(ValidationError):
        CreatorAssistantOperation(type="update_node", data={"title": "Missing target"})


def test_creator_graph_compiler_builds_complete_reconnecting_branch() -> None:
    compiler = CreatorGraphCompiler()

    project, report = compiler.apply(make_project(), [branch_operation()])

    assert report.valid is True
    assert report.branch_count == 1
    assert report.reachable_count == report.node_count == 5
    start = next(node for node in project["nodes"] if node["id"] == "start")
    station = next(node for node in project["nodes"] if node["id"] == "branch_station")
    key = next(node for node in project["nodes"] if node["id"] == "branch_key")
    assert start["choices"][0]["next"] == "branch_station"
    assert station["next"] == "branch_key"
    assert key["next"] == "ending"


def test_creator_preview_apply_detects_stale_project(tmp_path: Path) -> None:
    class FakeAgent:
        async def edit(self, request):
            return CreatorAssistantResponse(reply="Preview ready", operations=[branch_operation()], summary=["Add branch"], source="test")

    app = FastAPI()
    app.include_router(
        create_router(
            resolve_llm_config=lambda purpose: LLMProviderConfig(),
            agent=FakeAgent(),
            version_store=CreatorVersionStore(tmp_path / "versions"),
            workflow_store=CreatorWorkflowStore(tmp_path / "workflows"),
        ),
        prefix="/api",
    )
    client = TestClient(app)
    project = make_project()

    preview = client.post(
        "/api/creator/assistant/preview",
        json={"message": "Create a station branch", "selected_node_id": "start", "project": project},
    )
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["report"]["valid"] is True
    assert payload["report"]["branch_count"] == 1

    stale_project = make_project()
    stale_project["world"]["name"] = "Changed after preview"
    conflict = client.post(
        "/api/creator/assistant/apply",
        json={"project": stale_project, "operations": payload["operations"], "expected_hash": payload["base_hash"]},
    )
    assert conflict.status_code == 409

    applied = client.post(
        "/api/creator/assistant/apply",
        json={"project": project, "operations": payload["operations"], "expected_hash": payload["base_hash"]},
    )
    assert applied.status_code == 200
    assert applied.json()["report"]["reachable_count"] == 5


def test_creator_version_store_round_trip(tmp_path: Path) -> None:
    compiler = CreatorGraphCompiler()
    project, _ = compiler.apply(make_project(), [branch_operation()])
    store = CreatorVersionStore(tmp_path / "versions")

    saved = store.save("creator_workflow_test", "Branch created", project)
    listed = store.list("creator_workflow_test")
    loaded = store.load("creator_workflow_test", saved.version_id)

    assert listed[0].version_id == saved.version_id
    assert loaded.project_hash == compiler.hash(project)
    assert loaded.project == compiler.normalize(project)


def test_creator_history_store_round_trip_and_project_isolation(tmp_path: Path) -> None:
    store = CreatorHistoryStore(tmp_path / "history")
    first = store.append(
        "world_a",
        CreatorHistoryMessageCreate(role="user", speaker="你", content="记住角色叫 66"),
    )
    store.append(
        "world_a",
        CreatorHistoryMessageCreate(role="assistant", speaker="Creator Agent", content="已经记住。", summary=["角色 66"]),
    )

    assert first.world_id == "world_a"
    assert [message.content for message in store.list("world_a")] == ["记住角色叫 66", "已经记住。"]
    assert store.list("world_b") == []

    store.clear("world_a")
    assert store.list("world_a") == []


def test_creator_history_api_persists_and_clears_messages(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(
        create_router(
            resolve_llm_config=lambda purpose: LLMProviderConfig(),
            history_store=CreatorHistoryStore(tmp_path / "history"),
            version_store=CreatorVersionStore(tmp_path / "versions"),
            workflow_store=CreatorWorkflowStore(tmp_path / "workflows"),
        ),
        prefix="/api",
    )
    client = TestClient(app)

    created = client.post(
        "/api/creator/history/idol_story",
        json={"role": "user", "speaker": "你", "content": "创作 66 与 77 的偶像故事", "summary": []},
    )
    assert created.status_code == 200
    assert created.json()["world_id"] == "idol_story"

    listed = client.get("/api/creator/history/idol_story")
    assert listed.status_code == 200
    assert [message["content"] for message in listed.json()] == ["创作 66 与 77 的偶像故事"]

    cleared = client.delete("/api/creator/history/idol_story")
    assert cleared.status_code == 204
    assert client.get("/api/creator/history/idol_story").json() == []


def test_creator_agent_fallback_proposes_branch() -> None:
    request_message = "\u4ece\u5f53\u524d\u8282\u70b9\u521b\u5efa\u652f\u7ebf\uff1a\u8c03\u67e5\u5e9f\u5f03\u8f66\u7ad9\u5e76\u627e\u5230\u65e7\u94a5\u5319"
    agent = CreatorAssistantAgent(text_client=None)

    response = agent._fallback_response(
        type("Request", (), {"message": request_message, "selected_node_id": "start", "project": make_project()})()
    )

    assert response.operations[0].type == "create_branch"
    assert response.operations[0].data["source_node_id"] == "start"


def test_creator_agent_fallback_normalizes_stat_command_prefix() -> None:
    agent = CreatorAssistantAgent(text_client=None)
    response = agent._fallback_response(
        type("Request", (), {"message": "\u628a\u91d1\u94b1\u8bbe\u4e3a123", "selected_node_id": "start", "project": make_project()})()
    )

    stat_operations = [operation for operation in response.operations if operation.type == "set_player_stat"]
    assert [(operation.target_id, operation.data["value"]) for operation in stat_operations] == [("money", 123)]


def test_creator_mcp_tool_discovery_and_workflow_execution(tmp_path: Path) -> None:
    class FakeAgent:
        async def edit(self, request):
            return CreatorAssistantResponse(
                reply="Use graph validation",
                tool_calls=[CreatorToolCall(tool="validate_creator_graph", reason="Check before publishing")],
                source="test",
            )

    app = FastAPI()
    app.include_router(
        create_router(
            resolve_llm_config=lambda purpose: LLMProviderConfig(),
            agent=FakeAgent(),
            version_store=CreatorVersionStore(tmp_path / "versions"),
            workflow_store=CreatorWorkflowStore(tmp_path / "workflows"),
        ),
        prefix="/api",
    )
    client = TestClient(app)

    discovered = client.get("/api/creator/mcp/tools/list")
    assert discovered.status_code == 200
    tools = discovered.json()["tools"]
    assert any(tool["name"] == "author_story" and "inputSchema" in tool for tool in tools)
    assert any(tool["name"] == "expand_story" and tool["_meta"]["ownerAgent"] == "StoryExpansionAgent" for tool in tools)
    layout_tool = next(tool for tool in tools if tool["name"] == "layout_creator_graph")
    assert layout_tool["_meta"]["ownerAgent"] == "CreatorGraphLayoutCompiler"
    assert layout_tool["annotations"]["idempotentHint"] is True
    assert any(tool["name"] == "author_story" and tool["_meta"]["ownerAgent"] == "StoryAuthoringAgent" for tool in tools)
    assert any(tool["name"] == "review_playable_world" and "PlaytestAgent" in tool["_meta"]["ownerAgent"] for tool in tools)
    assert next(tool for tool in tools if tool["name"] == "author_story")["annotations"]["readOnlyHint"] is False
    assert next(tool for tool in tools if tool["name"] == "review_playable_world")["annotations"]["readOnlyHint"] is True
    assert any(tool["name"] == "publish_to_play" and tool["annotations"]["destructiveHint"] for tool in tools)

    preview = client.post(
        "/api/creator/workflows/preview",
        json={"message": "Validate it", "project": make_project(), "selected_node_id": "start"},
    )
    assert preview.status_code == 200
    assert preview.json()["tool_calls"][0]["tool"] == "validate_creator_graph"

    started = client.post(
        "/api/creator/workflows/run",
        json={"preview_id": preview.json()["preview_id"], "project": make_project()},
    )
    assert started.status_code == 200
    run_id = started.json()["run_id"]
    result = started.json()
    for _ in range(30):
        result = client.get(f"/api/creator/workflows/{run_id}").json()
        if result["status"] in {"done", "error", "cancelled"}:
            break
        time.sleep(0.01)
    assert result["status"] == "done"
    assert result["artifacts"]["graph_report"]["valid"] is True
    recovered = client.get("/api/creator/workflows/latest/creator_workflow_test")
    assert recovered.status_code == 200
    assert recovered.json()["run_id"] == run_id
    assert recovered.json()["world_id"] == "creator_workflow_test"
    assert (tmp_path / "workflows" / f"{run_id}.json").exists()
    acknowledged = client.post(f"/api/creator/workflows/{run_id}/acknowledge")
    assert acknowledged.status_code == 200
    assert acknowledged.json()["acknowledged_at"]


def test_creator_fallback_uses_story_authoring_tool_for_complete_story() -> None:
    agent = CreatorAssistantAgent(text_client=None)
    response = agent._fallback_response(
        type(
            "Request",
            (),
            {
                "message": "创作一个30分钟修仙完整剧情，要有具体台词、选择和结局",
                "selected_node_id": "start",
                "project": make_project(),
            },
        )()
    )

    assert response.operations == []
    assert [call.tool for call in response.tool_calls] == ["author_story", "save_world"]
    normalized = CreatorToolRegistry().normalize_calls(response.tool_calls)
    assert normalized[0].arguments["brief"]
    assert response.tool_calls[0].arguments["target_minutes"] == 30


def test_creator_graph_layout_arranges_full_graph_without_changing_story() -> None:
    project = make_project()
    project["nodes"][0]["choices"] = [{"id": "branch", "text": "Branch", "next": "side", "conditions": {}, "effects": {}}]
    project["nodes"].insert(
        2,
        {"id": "side", "type": "story", "title": "Side", "content": "Keep me", "next": "ending", "choices": [], "x": 100, "y": 100},
    )
    for node in project["nodes"]:
        node["x"] = 100
        node["y"] = 100
    story_before = [(node["id"], node.get("content"), node.get("next"), node.get("choices")) for node in project["nodes"]]

    laid_out, report = CreatorGraphLayoutCompiler().layout(project, scope="all")

    positions = {(node["x"], node["y"]) for node in laid_out["nodes"]}
    assert len(positions) == len(laid_out["nodes"])
    assert report.moved_node_count >= 3
    assert report.preserved_node_count == 0
    assert story_before == [(node["id"], node.get("content"), node.get("next"), node.get("choices")) for node in laid_out["nodes"]]


def test_creator_graph_layout_current_node_preserves_other_nodes_and_anchor() -> None:
    project = make_project()
    project["nodes"][1]["x"] = 380
    project["nodes"][1]["y"] = 420
    project["nodes"][2]["x"] = 380
    project["nodes"][2]["y"] = 420

    laid_out, report = CreatorGraphLayoutCompiler().layout(project, scope="downstream", root_node_id="main")

    by_id = {node["id"]: node for node in laid_out["nodes"]}
    assert (by_id["start"]["x"], by_id["start"]["y"]) == (100, 100)
    assert (by_id["main"]["x"], by_id["main"]["y"]) == (380, 420)
    assert (by_id["ending"]["x"], by_id["ending"]["y"]) != (380, 420)
    assert report.preserved_node_count == 1


@pytest.mark.parametrize(
    "message",
    [
        "整理当前节点，把后面的节点排整齐",
        "整理当前节点，把后续节点自动排整齐，不要修改剧情内容",
        "把选中节点下游重新布局一下",
    ],
)
def test_creator_natural_language_routes_current_node_layout_without_llm(message: str) -> None:
    import asyncio

    response = asyncio.run(
        CreatorAssistantAgent(text_client=None).edit(
            CreatorAssistantRequest(message=message, project=make_project(), selected_node_id="main")
        )
    )

    assert response.source == "tool_router"
    assert response.intent == "workflow"
    assert [call.tool for call in response.tool_calls] == ["layout_creator_graph"]
    assert response.tool_calls[0].arguments == {"scope": "downstream", "root_node_id": "main"}


def test_creator_layout_tool_executes_and_returns_inspectable_artifact() -> None:
    import asyncio

    executor = CreatorToolExecutor(
        resolve_llm_config=lambda purpose: LLMProviderConfig(),
        resolve_visual_request=lambda request: request,
        world_store=object(),
        visual_asset_agent=None,
        visual_asset_store=None,
    )
    project = make_project()
    project["nodes"][1]["x"] = project["nodes"][0]["x"]
    project["nodes"][1]["y"] = project["nodes"][0]["y"]
    updated, artifacts, detail = asyncio.run(
        executor.execute(
            CreatorToolCall(tool="layout_creator_graph", arguments={"scope": "all"}),
            project,
            {},
            should_cancel=lambda: False,
            progress=lambda title, detail: asyncio.sleep(0),
        )
    )

    assert artifacts["graph_layout"]["scope"] == "all"
    assert artifacts["graph_layout"]["moved_node_count"] > 0
    assert "剧情内容和连接关系未改变" in detail
    assert updated["nodes"][0]["next"] == "main"


@pytest.mark.parametrize(
    "message",
    [
        "给我创建一个全新的故事、世界观，随便什么",
        "我要全新的剧情",
        "从零写一个修仙剧本",
        "创作一个10分钟完整修仙剧情，包含玩家选择和结局",
    ],
)
def test_creator_router_recognizes_natural_complete_story_requests(message: str) -> None:
    response = CreatorAssistantAgent(text_client=None)._fallback_response(
        type(
            "Request",
            (),
            {"message": message, "selected_node_id": "start", "project": make_project()},
        )()
    )

    assert response.operations == []
    assert [call.tool for call in response.tool_calls] == ["author_story", "save_world"]


def test_author_story_tool_removes_conflicting_generic_graph_edits() -> None:
    class ConflictingTextClient:
        async def generate_text(self, system_prompt, user_prompt, on_token=None):
            return """{
              "reply": "开始重写",
              "summary": ["调用 StoryAuthoringAgent", "将描述整理为一个新的剧情节点。"],
              "tool_calls": [{"tool": "author_story", "arguments": {"brief": "换成一部完整的全新互动剧情故事"}}],
              "operations": [{"type": "add_node", "data": {"title": "新的剧情节点", "content": "换一部", "after": "start"}}]
            }"""

    import asyncio

    response = asyncio.run(
        CreatorAssistantAgent(text_client=ConflictingTextClient()).edit(
            CreatorAssistantRequest(message="换一部吧", project=make_project(), selected_node_id="start")
        )
    )

    assert [call.tool for call in response.tool_calls] == ["author_story", "save_world"]
    assert response.operations == []
    assert response.summary == ["调用 StoryAuthoringAgent", "完整剧情生成后自动保存到世界库。"]


def test_llm_reauthor_request_always_persists_generated_story() -> None:
    class ReauthoringTextClient:
        async def generate_text(self, system_prompt, user_prompt, on_token=None):
            return """{
              "reply": "重新生成完整剧情",
              "summary": ["调用 StoryAuthoringAgent"],
              "tool_calls": [{"tool": "author_story", "arguments": {"brief": "重新梳理当前项目并创作一部完整可玩的互动剧情"}}],
              "operations": []
            }"""

    import asyncio

    response = asyncio.run(
        CreatorAssistantAgent(text_client=ReauthoringTextClient()).edit(
            CreatorAssistantRequest(message="那你重新梳理一下剧情，重新来啊弄不就行了", project=make_project())
        )
    )

    assert [call.tool for call in response.tool_calls] == ["author_story", "save_world"]


def test_creator_fallback_does_not_reauthor_when_publishing_existing_complete_story() -> None:
    agent = CreatorAssistantAgent(text_client=None)
    response = agent._fallback_response(
        type(
            "Request",
            (),
            {
                "message": "把当前这部完整剧情校验后发布到玩家端，我要马上试玩。",
                "selected_node_id": "start",
                "project": make_project(),
            },
        )()
    )

    assert "author_story" not in [call.tool for call in response.tool_calls]
    assert [call.tool for call in response.tool_calls] == ["review_playable_world", "publish_to_play"]


def test_author_story_agent_capability_preserves_current_creator_world_id() -> None:
    class FakeStoryService:
        async def generate(self, request, progress=None):
            authored = make_project()
            authored["world"]["world_id"] = "model_generated_story_id"
            authored["world"]["name"] = "Fresh authored title"
            return type(
                "StoryResponse",
                (),
                {
                    "project": authored,
                    "reply": "Story authored",
                    "model_dump": lambda self, mode=None: {"project": authored},
                },
            )()

    class DummyStore:
        pass

    import asyncio

    executor = CreatorToolExecutor(
        resolve_llm_config=lambda purpose: LLMProviderConfig(),
        resolve_visual_request=lambda request: request,
        world_store=DummyStore(),
        visual_asset_agent=None,
        visual_asset_store=DummyStore(),
        story_service=FakeStoryService(),
    )
    current = make_project()
    current["world"]["world_id"] = "creator_existing_project"
    updated, _, _ = asyncio.run(
        executor.execute(
            CreatorToolCall(tool="author_story", arguments={"brief": "创作一个足够完整的互动剧情故事"}),
            current,
            {},
            should_cancel=lambda: False,
            progress=lambda title, detail: asyncio.sleep(0),
        )
    )

    assert updated["world"]["world_id"] == "creator_existing_project"
    assert updated["world"]["name"] == "Fresh authored title"


@pytest.mark.parametrize("name", ["", "   ", "未命名互动剧情", "尚未命名", "Untitled interactive story"])
def test_creator_world_compile_rejects_empty_or_placeholder_name(name: str) -> None:
    project = make_project()
    project["world"]["name"] = name

    with pytest.raises(ValueError, match="请先填写明确名称"):
        compile_creator_world(project)


def test_creator_world_compile_preserves_explicit_trimmed_name() -> None:
    project = make_project()
    project["world"]["name"] = "  灵兽失踪谜案  "

    world = compile_creator_world(project, published=True)

    assert world.name == "灵兽失踪谜案"
    assert world.metadata["published_to_play"] is True


def test_creator_visual_request_defaults_to_plan_generate_and_bind() -> None:
    calls = _fallback_tool_calls("给当前剧情制作角色立绘和场景背景美术")

    assert [call.tool for call in calls] == [
        "plan_visual_assets",
        "generate_visual_assets",
        "bind_visual_assets",
    ]


def test_creator_visual_request_can_explicitly_stop_after_planning() -> None:
    calls = _fallback_tool_calls("只规划角色立绘和场景背景，不要生成图片")

    assert [call.tool for call in calls] == ["plan_visual_assets"]


def test_creator_can_bind_latest_visual_assets_without_regenerating_images() -> None:
    calls = _fallback_tool_calls("绑定最新视觉资产并发布到玩家端")

    assert [call.tool for call in calls] == ["bind_visual_assets", "review_playable_world", "publish_to_play"]


def test_creator_natural_request_preserves_story_and_visual_asset_counts() -> None:
    calls = _fallback_tool_calls("创作一个10分钟修仙完整剧情，2个角色、3个场景，并生成2个角色立绘和1个场景图")

    assert [call.tool for call in calls] == [
        "author_story",
        "plan_visual_assets",
        "generate_visual_assets",
        "bind_visual_assets",
        "save_world",
    ]
    assert calls[0].arguments["target_scene_count"] == 3
    assert calls[0].arguments["target_character_count"] == 2
    assert calls[1].arguments["max_characters"] == 2
    assert calls[1].arguments["max_scenes"] == 1
    assert calls[2].arguments["max_characters"] == 2
    assert calls[2].arguments["max_scenes"] == 1


def test_creator_visual_counts_accept_npc_and_image_classifiers() -> None:
    calls = _fallback_tool_calls(
        "创作完整修仙故事，2个NPC、3个场景；生成2张角色立绘和1张场景背景并发布到玩家端"
    )

    assert calls[0].arguments["target_character_count"] == 2
    assert calls[0].arguments["target_scene_count"] == 3
    assert calls[1].arguments["max_characters"] == 2
    assert calls[1].arguments["max_scenes"] == 1
    assert calls[2].arguments["max_characters"] == 2
    assert calls[2].arguments["max_scenes"] == 1


def test_creator_new_story_can_continue_through_publish_without_losing_authoring_stage() -> None:
    calls = _fallback_tool_calls("创作一个10分钟完整修仙剧情，2个角色、3个场景，生成美术并发布到玩家端")

    assert calls[0].tool == "author_story"
    assert calls[-2].tool == "review_playable_world"
    assert calls[-1].tool == "publish_to_play"


def test_bind_visual_assets_restores_generated_character_and_scene_images() -> None:
    project = make_project()
    project["characters"] = [{"id": "mentor", "name": "Mentor", "portrait": ""}]
    project["nodes"][0]["title"] = "Mountain Gate"
    result = {
        "generated": [
            {
                "kind": "character",
                "source_id": "mentor",
                "source_name": "Mentor",
                "output_path": "output/visual_assets/recovery/characters/mentor.png",
            },
            {
                "kind": "scene",
                "source_id": "start",
                "source_name": "Mountain Gate",
                "output_path": "output/visual_assets/recovery/scenes/start.png",
            },
        ],
        "failed": [],
        "metadata": {"generated_count": 2},
    }

    restored, counts = bind_visual_assets(project, result)

    assert counts == {"characters": 1, "scenes": 1}
    assert restored["characters"][0]["portrait"] == "/output/visual_assets/recovery/characters/mentor.png"
    assert restored["nodes"][0]["background"] == "/output/visual_assets/recovery/scenes/start.png"
    assert restored["pipeline_artifacts"]["visual_result"]["metadata"]["generated_count"] == 2


def test_bind_visual_assets_executor_prefers_latest_stored_result_over_stale_project_result() -> None:
    class LatestVisualStore:
        def list(self):
            return [
                {
                    "artifact_id": "creator_workflow_test_visual_assets",
                    "world_id": "creator_workflow_test",
                    "title": "Workflow test",
                }
            ]

        def load(self, artifact_id):
            assert artifact_id == "creator_workflow_test_visual_assets"
            return {
                "result": {
                    "generated": [
                        {
                            "kind": "character",
                            "source_id": "mentor",
                            "source_name": "Mentor",
                            "output_path": "output/visual_assets/latest/characters/mentor.transparent.png",
                        },
                        {
                            "kind": "scene",
                            "source_id": "start",
                            "source_name": "Mountain Gate",
                            "output_path": "output/visual_assets/latest/scenes/start.png",
                        },
                    ],
                    "failed": [],
                    "metadata": {"generation_run_id": "latest_run", "generated_count": 2},
                }
            }

    class DummyStore:
        pass

    import asyncio

    project = make_project()
    project["characters"] = [{"id": "mentor", "name": "Mentor", "portrait": ""}]
    project["nodes"][0]["title"] = "Mountain Gate"
    project["pipeline_artifacts"] = {
        "visual_result": {
            "generated": [
                {
                    "kind": "character",
                    "source_id": "mentor",
                    "output_path": "output/visual_assets/stale/characters/mentor.png",
                }
            ]
        }
    }
    executor = CreatorToolExecutor(
        resolve_llm_config=lambda purpose: LLMProviderConfig(),
        resolve_visual_request=lambda request: request,
        world_store=DummyStore(),
        visual_asset_agent=None,
        visual_asset_store=LatestVisualStore(),
    )

    updated, artifacts, _ = asyncio.run(
        executor.execute(
            CreatorToolCall(tool="bind_visual_assets", arguments={}),
            project,
            {},
            should_cancel=lambda: False,
            progress=lambda title, detail: asyncio.sleep(0),
        )
    )

    assert updated["characters"][0]["portrait"].endswith("/latest/characters/mentor.transparent.png")
    assert updated["nodes"][0]["background"].endswith("/latest/scenes/start.png")
    assert artifacts["visual_result"]["metadata"]["generation_run_id"] == "latest_run"


def test_creator_agent_uses_visible_fallback_when_llm_routing_fails() -> None:
    class ExplodingTextClient:
        async def generate_text(self, system_prompt, user_prompt, on_token=None):
            raise RuntimeError("provider unavailable")

    import asyncio

    agent = CreatorAssistantAgent(text_client=ExplodingTextClient())
    request = CreatorAssistantRequest(
        message="把当前剧情发布到玩家端",
        selected_node_id="start",
        project=make_project(),
    )
    response = asyncio.run(agent.edit(request))

    assert response.source == "fallback_error"
    assert "provider unavailable" in response.raw_excerpt
    assert response.intent == "error"
    assert response.tool_calls == []


def test_creator_agent_repairs_non_json_conversation_response() -> None:
    class RepairingConversationClient:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_text(self, system_prompt, user_prompt, on_token=None):
            self.calls += 1
            if self.calls == 1:
                return "这个故事讲述一名巡山弟子调查失踪灵兽。"
            return """{
              "intent": "chat",
              "route": "creator_conversation_agent",
              "reply": "这个故事讲述一名巡山弟子调查失踪灵兽。",
              "summary": [],
              "tool_calls": [],
              "operations": []
            }"""

    import asyncio

    client = RepairingConversationClient()
    response = asyncio.run(
        CreatorAssistantAgent(text_client=client).edit(
            CreatorAssistantRequest(
                message="请总结当前故事，不要修改任何内容。",
                selected_node_id="start",
                project=make_project(),
            )
        )
    )

    assert client.calls == 2
    assert response.intent == "chat"
    assert response.source == "llm_repair"
    assert response.requires_confirmation is False
    assert response.operations == []
    assert response.tool_calls == []


def test_creator_tool_registry_repairs_llm_numeric_overflow() -> None:
    normalized = CreatorToolRegistry().normalize_calls(
        [
            CreatorToolCall(
                tool="author_story",
                arguments={
                    "brief": "创作一部具有完整对白、选择和结局的互动故事",
                    "target_minutes": 999,
                    "target_scene_count": 71,
                    "target_character_count": 30,
                },
            )
        ]
    )

    assert normalized[0].arguments["target_minutes"] == 180
    assert normalized[0].arguments["target_scene_count"] == 16
    assert normalized[0].arguments["target_character_count"] == 12


def test_creator_agent_repairs_existing_story_expansion_misroute() -> None:
    class RepairingTextClient:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_text(self, system_prompt, user_prompt, on_token=None):
            self.calls += 1
            if self.calls == 1:
                return """{
                  "reply": "重写故事",
                  "summary": [],
                  "tool_calls": [{"tool": "author_story", "arguments": {"brief": "把当前故事扩展为一部长篇互动剧情", "target_scene_count": 71}}],
                  "operations": []
                }"""
            return """{
              "reply": "已按现有故事扩写",
              "summary": ["新增两个连续节点并接回原流程"],
              "tool_calls": [],
              "operations": [
                {"type": "add_node", "data": {"id": "extension_1", "title": "扩写一", "content": "第一段", "after": "start", "next": "extension_2"}},
                {"type": "add_node", "data": {"id": "extension_2", "title": "扩写二", "content": "第二段", "after": "extension_1"}}
              ]
            }"""

    import asyncio

    client = RepairingTextClient()
    response = asyncio.run(
        CreatorAssistantAgent(text_client=client).edit(
            CreatorAssistantRequest(
                message="在当前故事里增加2个新节点并串联起来",
                project=make_project(),
                selected_node_id="start",
            )
        )
    )

    assert client.calls == 2
    assert response.source == "llm_repair"
    assert response.tool_calls == []
    assert [operation.data.get("id") for operation in response.operations] == ["extension_1", "extension_2"]


def test_creator_agent_repairs_ambiguous_improvement_into_clarification() -> None:
    class ClarificationRepairClient:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_text(self, system_prompt, user_prompt, on_token=None):
            self.calls += 1
            if self.calls == 1:
                return """{
                  "intent": "graph_edit",
                  "route": "creator_graph",
                  "reply": "我来直接优化故事",
                  "summary": ["修改开场"],
                  "tool_calls": [],
                  "operations": [{"type": "update_node", "target_id": "start", "data": {"content": "擅自修改"}}]
                }"""
            return """{
              "intent": "clarify",
              "route": "creator_conversation_agent",
              "reply": "你更希望我先改善人物、节奏、分支还是结局？",
              "summary": [],
              "tool_calls": [],
              "operations": []
            }"""

    import asyncio

    client = ClarificationRepairClient()
    response = asyncio.run(
        CreatorAssistantAgent(text_client=client).edit(
            CreatorAssistantRequest(
                message="我觉得这个故事还不够好，帮我处理一下。",
                project=make_project(),
                selected_node_id="start",
            )
        )
    )

    assert client.calls == 2
    assert response.intent == "clarify"
    assert response.route == "creator_conversation_agent"
    assert response.source == "llm_repair"
    assert response.requires_confirmation is False
    assert response.operations == []


def test_creator_agent_does_not_merge_generic_fallback_node_into_agent_workflow() -> None:
    class ExpansionRouterClient:
        async def generate_text(self, system_prompt, user_prompt, on_token=None):
            return """{
              "intent": "workflow",
              "route": "router_agent",
              "reply": "调用扩写 Agent",
              "summary": ["扩写当前故事"],
              "tool_calls": [{"tool": "expand_story", "arguments": {"brief": "在当前故事后连续扩写五十个剧情节点", "target_node_count": 50, "source_node_id": "start"}}],
              "operations": []
            }"""

    import asyncio

    response = asyncio.run(
        CreatorAssistantAgent(text_client=ExpansionRouterClient()).edit(
            CreatorAssistantRequest(
                message="增加50个新节点，然后直接与这个故事串联起来",
                project=make_project(),
                selected_node_id="start",
            )
        )
    )

    assert response.intent == "workflow"
    assert response.source == "llm"
    assert response.operations == []
    assert response.tool_calls[0].tool == "expand_story"
    assert response.tool_calls[0].arguments["target_node_count"] == 50


def test_creator_conversation_chat_returns_non_executable_preview(tmp_path: Path) -> None:
    class ChatAgent:
        async def edit(self, request):
            return CreatorAssistantResponse(
                reply="这个故事当前有三段主流程。",
                intent="chat",
                route="creator_conversation_agent",
                requires_confirmation=False,
                source="llm",
            )

    app = FastAPI()
    app.include_router(
        create_router(
            resolve_llm_config=lambda purpose: LLMProviderConfig(),
            agent=ChatAgent(),
            version_store=CreatorVersionStore(tmp_path / "versions"),
            workflow_store=CreatorWorkflowStore(tmp_path / "workflows"),
        ),
        prefix="/api",
    )
    client = TestClient(app)
    preview = client.post(
        "/api/creator/workflows/preview",
        json={"message": "这个故事现在讲了什么？", "project": make_project(), "selected_node_id": "start"},
    )

    assert preview.status_code == 200
    assert preview.json()["intent"] == "chat"
    assert preview.json()["executable"] is False
    rejected = client.post(
        "/api/creator/workflows/run",
        json={"preview_id": preview.json()["preview_id"], "project": make_project()},
    )
    assert rejected.status_code == 409


def test_story_expansion_compiler_inserts_exact_sequence_and_reconnects() -> None:
    request = StoryExpansionRequest(
        brief="在开场之后追加调查过程",
        target_node_count=3,
        source_node_id="start",
        project=make_project(),
    )
    draft = StoryExpansionDraft(
        summary="调查隐藏线索",
        nodes=[
            StoryExpansionNode(id="expand_1", title="调查一", content="检查门锁。"),
            StoryExpansionNode(id="expand_2", title="调查二", content="发现脚印。"),
            StoryExpansionNode(id="expand_3", title="调查三", content="追到走廊。"),
        ],
    )

    project, report = StoryExpansionCompiler().apply(request, draft)
    by_id = {node["id"]: node for node in project["nodes"]}
    assert report.valid is True
    assert by_id["start"]["next"] == "expand_1"
    assert by_id["expand_1"]["next"] == "expand_2"
    assert by_id["expand_2"]["next"] == "expand_3"
    assert by_id["expand_3"]["next"] == "main"


def test_story_expansion_agent_batches_large_requests_and_normalizes_protocol_noise() -> None:
    class BatchedExpansionClient:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_text(self, system_prompt, user_prompt, on_token=None):
            self.calls += 1
            payload = __import__("json").loads(user_prompt)
            count = payload["batch_target_node_count"]
            start = payload["story_position_start"]
            nodes = []
            for index in range(start, start + count + 1):
                nodes.append(
                    {
                        "id": f"expanded_{index}",
                        "type": "choice",
                        "title": f"扩写 {index}",
                        "content": f"这是第 {index} 个连续剧情节点。",
                        "character": "unknown_character",
                        "next": "model_should_not_link_nodes",
                        "choices": [{"text": "模型噪声"}],
                    }
                )
            return __import__("json").dumps(
                {"request": "protocol noise", "summary": f"第 {self.calls} 批", "nodes": nodes},
                ensure_ascii=False,
            )

    import asyncio

    progress_events = []

    async def progress(title, detail):
        progress_events.append((title, detail))

    client = BatchedExpansionClient()
    response = asyncio.run(
        StoryExpansionAgent(text_client=client).expand(
            StoryExpansionRequest(
                brief="在当前故事中增加二十三个连续调查节点",
                target_node_count=23,
                source_node_id="start",
                project=make_project(),
            ),
            progress=progress,
        )
    )

    assert client.calls == 3
    assert len(response.draft.nodes) == 23
    assert [node.id for node in response.draft.nodes] == [f"expand_start_{index:03d}" for index in range(1, 24)]
    assert all(node.type == "story" and node.character == "" for node in response.draft.nodes)
    assert any("累计 23/23" in detail for _, detail in progress_events)


def test_expand_story_after_mode_always_reconnects_to_original_successor() -> None:
    class FakeExpansionAgent:
        async def expand(self, request, progress=None):
            assert request.source_node_id == "start"
            assert request.reconnect_node_id == "main"
            return type(
                "ExpansionResponse",
                (),
                {
                    "draft": StoryExpansionDraft(
                        summary="插入调查",
                        nodes=[StoryExpansionNode(id="inserted_story", title="调查", content="检查现场。")],
                    ),
                    "model_dump": lambda self, mode=None: {"draft": {"summary": "插入调查", "nodes": []}},
                },
            )()

    class DummyStore:
        pass

    import asyncio

    executor = CreatorToolExecutor(
        resolve_llm_config=lambda purpose: LLMProviderConfig(api_key="test"),
        resolve_visual_request=lambda request: request,
        world_store=DummyStore(),
        visual_asset_agent=None,
        visual_asset_store=DummyStore(),
        story_expansion_agent=FakeExpansionAgent(),
    )
    updated, _, _ = asyncio.run(
        executor.execute(
            CreatorToolCall(
                tool="expand_story",
                arguments={
                    "brief": "在开场后插入一段调查",
                    "target_node_count": 1,
                    "source_node_id": "start",
                    "reconnect_node_id": "ending",
                    "insertion_mode": "after",
                },
            ),
            make_project(),
            {},
            should_cancel=lambda: False,
            progress=lambda title, detail: asyncio.sleep(0),
        )
    )

    by_id = {node["id"]: node for node in updated["nodes"]}
    assert by_id["start"]["next"] == "inserted_story"
    assert by_id["inserted_story"]["next"] == "main"
