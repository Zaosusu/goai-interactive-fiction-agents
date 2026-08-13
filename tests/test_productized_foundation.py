from pathlib import Path

import cv2
from fastapi.testclient import TestClient
import json
import numpy as np
import pytest
import re
from PIL import Image, ImageDraw
from pydantic import ValidationError

from app.core.command_executor import CommandExecutor
from app.agents.npc_lorebook import NpcLorebookArtifact, NpcLorebookCompiler, NpcLorebookCreationAgent, NpcLorebookCreationError, NpcLorebookEntry, NpcLorebookReviewAgent, NpcLorebookRuntime
from app.agents.npc_runtime import (
    AgentRuntime,
    NpcConversationReview,
    NpcMemoryLifecycle,
    NpcTurnDirector,
    RouterAgent,
    StateValidatorAgent,
)
from app.agents.npc_review import NpcProtocolReviewAgent
from app.agents.playtest_validation import PlaytestAgent
from app.agents.ui_projection import UiReviewAgent
from app.agents.world_review import WorldReviewAgent
from app.core.image_generation import ImageGenerationResponse
from app.core.image_generation import OpenAICompatibleImageGenerationClient
from app.core.image_generation import ImageGenerationProviderConfig
from app.core.image_generation import ImageGenerationRequest
from app.core.llm import OpenAICompatibleLLMClient
from app.core.model_config import LLMProviderConfig
from app.core.providers import create_npc_llm_client, create_world_builder_llm_client
from app.core.memory import InMemoryMemoryStore
from app.core.session_store import RuntimeSessionStore
from app.core.models import AgentLLMOutput, ChatRequest, NpcRuntimeState, WorldActionRequest, command_to_dict
from app.core.protocol_tools import AgentLLMOutputProtocolTool, WorldGenerationProtocolTool
from app.core.review_agents import ReviewIssue, ReviewReport
from app.main import app
from app.api import routes as api_routes
from app.agents.script_decomposition import ScriptDecompositionAgent
from app.agents.script_decomposition import ScriptDecompositionArtifactStore
from app.agents.script_decomposition import ScriptGraphCompiler
from app.agents.script_decomposition import ScriptGraphStore
from app.agents.script_decomposition import _source_chunks
from app.agents.script_decomposition import build_script_world
from app.agents.script_decomposition import validate_script_decomposition
from app.agents.creator_assistant import CreatorAssistantAgent, CreatorAssistantRequest, CreatorAssistantResponse
from app.agents.creator_assistant.routes import create_router as create_creator_router
from app.agents.visual_asset_generation import CharacterBackgroundRemovalTool, VisualAssetArtifactStore, VisualAssetGenerationAgent, validate_transparent_portrait
from app.agents.visual_asset_generation import background_removal as background_removal_module
from app.agents.visual_asset_generation import model_manager as model_manager_module
from app.agents.visual_asset_generation.background_removal import BackgroundRemovalResult
from app.agents.visual_prompt_composer import VisualPromptComposerAgent
from app.agents.world_builder import WorldBuilderAgent, generate_world_config
from app.worlds.sandbox.adapter import SandboxWorldAdapter
from app.worlds.sandbox.generator import _finalize_world_quality, _normalize_generation_payload
from app.worlds.sandbox.models import (
    ExperienceFeedbackRequest,
    ScriptDecompositionRequest,
    ScriptStoryEntity,
    ScriptStoryEvidence,
    ScriptStoryGraphFacts,
    ScriptStoryRelation,
    ScriptCharacterInput,
    ScriptClueInput,
    SandboxAction,
    SandboxNPC,
    SandboxTask,
    SandboxWorldConfig,
    VisualAssetRequest,
    VisualAssetGenerationResult,
    VisualAssetPlan,
    VisualAssetSpec,
    WorldGenerateRequest,
)
from app.worlds.sandbox.template_store import WorldTemplateStore
from app.worlds.sandbox.validator import SandboxWorldValidator


def make_world() -> SandboxWorldConfig:
    return SandboxWorldConfig(
        world_id="test_world",
        name="测试世界",
        description="用于测试产品化底座。",
        lore="玩家需要获得令牌并复命。",
        opening_scene="测试开始。",
        player={"name": "测试玩家", "location": "起点"},
        npcs=[SandboxNPC(id="mentor", name="师父", role="导师", location="起点")],
        story_goals=["完成测试任务"],
        tasks=[SandboxTask(id="get_token", title="获得令牌")],
        actions=[
            SandboxAction(
                id="take_token",
                label="领取令牌",
                description="获得试炼令。",
                effect={
                    "set_player": {"trial_token": True},
                    "complete_task": "get_token",
                    "active_npc_id": "mentor",
                    "scene": "玩家获得令牌。",
                },
            )
        ],
    )


def make_multi_npc_world() -> SandboxWorldConfig:
    return SandboxWorldConfig(
        world_id="multi_npc_world",
        name="多 NPC 世界",
        description="用于测试每个 NPC 独立实例。",
        lore="玩家会分别和两个 NPC 对话。",
        opening_scene="两名 NPC 在大厅等待。",
        player={"name": "测试玩家", "location": "大厅"},
        npcs=[
            SandboxNPC(id="agent_a", name="AgentA", role="线索 NPC", location="大厅"),
            SandboxNPC(id="agent_b", name="AgentB", role="审核 NPC", location="大厅"),
        ],
        story_goals=["分别询问两名 NPC"],
        tasks=[SandboxTask(id="ask_all", title="询问所有 NPC")],
    )


def make_script_case() -> ScriptDecompositionRequest:
    return ScriptDecompositionRequest(
        case_id="locked_room_case",
        title="锁门后的钟声",
        player_name="侦探",
        public_background="午夜钟声响起后，书房主人被发现倒在反锁的房间里。",
        truth="管家林伯提前调慢座钟并藏起备用钥匙，制造了不在场证明。",
        locations=["书房", "走廊", "厨房"],
        timeline=["23:40 林伯进入书房送茶。", "00:00 钟声响起。", "00:10 众人发现尸体。"],
        characters=[
            ScriptCharacterInput(
                id="butler",
                name="林伯",
                role="管家",
                public_info="负责管理书房钥匙。",
                secret="他藏起了备用钥匙。",
                motive="担心主人公布遗嘱。",
                alibi="声称钟声响起时自己在厨房。",
                location="厨房",
            ),
            ScriptCharacterInput(
                id="niece",
                name="周岚",
                role="侄女",
                public_info="最后一个和主人争吵的人。",
                secret="她其实没有进入书房。",
                motive="遗产纠纷。",
                alibi="案发时在走廊打电话。",
                location="走廊",
            ),
        ],
        clues=[
            ScriptClueInput(
                id="slow_clock",
                title="慢了十分钟的座钟",
                content="座钟指针被人为调慢。",
                location="书房",
                owner="林伯",
                reveals="林伯的不在场证明有问题。",
            ),
            ScriptClueInput(
                id="spare_key",
                title="茶罐里的备用钥匙",
                content="厨房茶罐底部藏着书房备用钥匙。",
                location="厨房",
                owner="林伯",
                reveals="反锁房间可以被重新锁上。",
            ),
        ],
        forbidden_spoilers=["未提交推理前不得说出林伯是真凶。"],
    )


class _NoopImageClient:
    name = "fake"

    def generate(self, request, config):
        Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(request.output_path).write_bytes(b"fake-image")
        return ImageGenerationResponse(output_path=request.output_path, provider=config.provider, model=config.model)


def test_agent_output_schema_rejects_unknown_command() -> None:
    with pytest.raises(ValidationError):
        AgentLLMOutput(
            content="我给你一个不存在的神器。",
            inner_thought="测试非法命令。",
            command={"name": "grant_admin", "args": {"patch": {"god_mode": True}}},
        )


def test_state_validator_applies_valid_set_player_command() -> None:
    adapter = SandboxWorldAdapter(make_world())
    state = adapter.create_initial_state()
    output = AgentLLMOutput(
        content="拿着这枚试炼令。",
        inner_thought="测试合法命令。",
        command={"name": "set_player", "args": {"patch": {"trial_token": True}}},
    )

    StateValidatorAgent().apply(adapter, state, output)

    assert state.world_state["player"]["trial_token"] is True


def test_command_executor_runs_world_action_command() -> None:
    adapter = SandboxWorldAdapter(make_world())
    state = adapter.create_initial_state()
    output = AgentLLMOutput(
        content="去领取令牌。",
        inner_thought="测试执行器。",
        command={"name": "run_world_action", "args": {"action_id": "take_token"}},
    )

    CommandExecutor().execute(adapter, state, output)

    assert state.world_state["player"]["trial_token"] is True
    assert state.world_state["tasks"][0]["status"] == "done"


def test_state_validator_normalizes_invalid_command_args() -> None:
    adapter = SandboxWorldAdapter(make_world())
    state = adapter.create_initial_state()
    output = AgentLLMOutput(
        content="给你空气。",
        inner_thought="测试非法参数。",
        command={"name": "grant_item", "args": {"item": ""}},
    )

    StateValidatorAgent().apply(adapter, state, output)

    assert command_to_dict(output.command) == {"name": "none", "args": {}}
    assert not state.world_state["player"].get("inventory")


def test_npc_output_quest_progress_does_not_directly_mutate_sandbox_progress() -> None:
    adapter = SandboxWorldAdapter(make_world())
    state = adapter.create_initial_state()
    old_progress = state.quest_progress
    output = AgentLLMOutput(
        content="我嘴上说完成了，但没有给命令。",
        inner_thought="测试进度隔离。",
        command={"name": "none", "args": {}},
        quest_progress="任务完成：获得令牌。",
    )

    StateValidatorAgent().apply(adapter, state, output)

    assert state.quest_progress == old_progress


def test_npc_protocol_review_agent_repairs_alias_json_output() -> None:
    output = NpcProtocolReviewAgent().repair_raw_output(
        '{"reply":"你先去找药师问清楚报名表在哪里。","thought":"引导玩家继续探索","command":{"name":"none"}}',
        ["查看四周"],
    )

    assert output.action_type == "say"
    assert output.content == "你先去找药师问清楚报名表在哪里。"
    assert output.inner_thought == "引导玩家继续探索"
    assert command_to_dict(output.command) == {"name": "none", "args": {}}
    assert output.suggested_actions == ["查看四周"]


def test_npc_protocol_review_agent_repairs_plain_text_output() -> None:
    output = NpcProtocolReviewAgent().repair_raw_output("你现在应该先和我对话，确认下一步地点。", ["问下一步"])

    assert output.action_type == "say"
    assert output.content == "你现在应该先和我对话，确认下一步地点。"
    assert command_to_dict(output.command) == {"name": "none", "args": {}}


def test_agent_llm_output_protocol_tool_validates_before_repair() -> None:
    tool = AgentLLMOutputProtocolTool()

    valid = tool.validate_agent_output(
        {
            "action_type": "say",
            "content": "请先和我确认任务。",
            "inner_thought": "protocol ok",
            "command": {"name": "none", "args": {}},
        }
    )
    invalid = tool.validate_agent_output({"reply": "字段漂移，但可修复"})

    assert valid.valid is True
    assert valid.output is not None
    assert invalid.valid is False


def test_agent_llm_output_protocol_tool_repairs_only_when_invalid() -> None:
    tool = AgentLLMOutputProtocolTool()

    output = tool.repair_agent_output({"reply": "字段漂移，但可修复", "command": {"name": "none"}}, ["查看四周"])

    assert output.content == "字段漂移，但可修复"
    assert output.model_extra["protocol_repaired"] is True
    assert command_to_dict(output.command) == {"name": "none", "args": {}}


def test_agent_llm_output_protocol_tool_unwraps_nested_json_content() -> None:
    tool = AgentLLMOutputProtocolTool()
    raw = {
        "content": json.dumps(
            {
                "action_type": "speak",
                "content": "别在这里胡言乱语，明日还要选拔。",
                "inner_thought": "对方在挑衅，但我不能暴露协议。",
                "command": {"name": "none", "args": {}},
                "suggested_actions": ["询问选拔安排"],
            },
            ensure_ascii=False,
        )
    }

    output = tool.repair_agent_output(raw, ["查看四周"])

    assert output.action_type == "say"
    assert output.content == "别在这里胡言乱语，明日还要选拔。"
    assert output.inner_thought == "对方在挑衅，但我不能暴露协议。"
    assert output.suggested_actions == ["询问选拔安排"]


def test_openai_compatible_llm_adds_json_marker_for_response_format() -> None:
    client = object.__new__(OpenAICompatibleLLMClient)

    messages = client._with_json_instruction([])

    assert "json" in messages[-1].content.lower()


def test_openai_compatible_llm_attach_trace_on_success() -> None:
    client = object.__new__(OpenAICompatibleLLMClient)
    client.model = "test-model"
    client.base_url = "https://example.test/v1"
    output = AgentLLMOutput(content="ok", inner_thought="ok", command={"name": "none", "args": {}})

    client._attach_trace(output, [], "structured_output", ok=True)

    assert output.model_extra["provider_trace"][0]["ok"] is True
    assert output.model_extra["provider"]["model"] == "test-model"


def test_model_provider_factories_keep_world_builder_and_npc_channels_separate() -> None:
    npc = create_npc_llm_client(
        config=LLMProviderConfig(api_key="npc-key", base_url="https://npc.example/v1", model="cheap-npc-model")
    )
    world = create_world_builder_llm_client(
        config=LLMProviderConfig(api_key="world-key", base_url="https://world.example/v1", model="strong-world-model")
    )

    assert isinstance(npc, OpenAICompatibleLLMClient)
    assert isinstance(world, OpenAICompatibleLLMClient)
    assert npc.model == "cheap-npc-model"
    assert world.model == "strong-world-model"
    assert npc.base_url == "https://npc.example/v1"
    assert world.base_url == "https://world.example/v1"


def test_openai_compatible_image_client_passes_stepfun_generation_parameters(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    class FakeImages:
        def generate(self, **kwargs):
            captured.update(kwargs)

            class Item:
                b64_json = "ZmFrZS1pbWFnZQ=="
                url = ""

            class Response:
                data = [Item()]

            return Response()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.images = FakeImages()

    class FakeHttpxClient:
        def __init__(self, **kwargs):
            pass

    import app.core.image_generation as image_generation

    monkeypatch.setattr(image_generation, "resolve_image_api_key", lambda config: "key")
    monkeypatch.setitem(__import__("sys").modules, "openai", type("OpenAIModule", (), {"OpenAI": FakeOpenAI}))
    monkeypatch.setitem(__import__("sys").modules, "httpx", type("HttpxModule", (), {"Client": FakeHttpxClient}))

    client = OpenAICompatibleImageGenerationClient("stepfun")
    client.generate(
        ImageGenerationRequest(
            prompt="same style portrait",
            output_path=str(tmp_path / "image.png"),
            negative_prompt="text",
            seed=1002,
        ),
        ImageGenerationProviderConfig(
            provider="stepfun",
            model="step-image-edit-2",
            steps=16,
            cfg_scale=1.4,
            seed=1000,
            text_mode=False,
        ),
    )

    assert captured["extra_body"]["steps"] == 16
    assert captured["extra_body"]["cfg_scale"] == 1.4
    assert captured["extra_body"]["seed"] == 1002
    assert captured["extra_body"]["text_mode"] is False
    assert captured["extra_body"]["negative_prompt"] == "text"


@pytest.mark.asyncio
async def test_runtime_retries_same_npc_after_invalid_llm_protocol(tmp_path: Path) -> None:
    class RetryLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.messages = []

        async def invoke(self, messages, fallback_actions):
            self.calls += 1
            self.messages.append(messages)
            if self.calls > 1:
                return AgentLLMOutput(
                    action_type="say",
                    content="先取得通行令牌，再去终点完成试炼。",
                    inner_thought="根据当前世界状态给出指引。",
                    command={"name": "none", "args": {}},
                    suggested_actions=fallback_actions,
                )
            return AgentLLMOutput(
                action_type="wait",
                content="我刚才没有组织好回应。请你重说一遍，我会按当前世界状态继续。",
                inner_thought="invalid protocol",
                command={"name": "none", "args": {}},
                suggested_actions=fallback_actions,
                provider_error={"type": "structured_output_parse_failed"},
            )

    adapter = SandboxWorldAdapter(make_world())
    runtime = AgentRuntime(adapter, llm_client=RetryLLM(), session_store=RuntimeSessionStore(tmp_path / "sessions"))

    response = await runtime.chat(
        ChatRequest(
            message="我现在需要做什么？",
            player_name="草莓包饭",
            location="起点",
            target_npc_id="mentor",
        )
    )

    assert response.action_type == "say"
    assert "我刚才没有组织好回应" not in response.reply
    assert "通行令牌" in response.reply
    assert response.command == {"name": "none", "args": {}}
    assert runtime.llm.calls == 2
    assert response.debug_trace["llm"]["provider_retry_attempts"] == 1
    assert "上一版回复因 structured_output_parse_failed" in runtime.llm.messages[1][-1].content


@pytest.mark.asyncio
async def test_chat_response_exposes_llm_debug_trace(tmp_path: Path) -> None:
    class TracedLLM:
        async def invoke(self, messages, fallback_actions):
            return AgentLLMOutput(
                action_type="say",
                content="先去报名处找经纪人。",
                inner_thought="正常结构化输出",
                command={"name": "none", "args": {}},
                provider_trace=[
                    {"stage": "structured_output", "ok": False, "status_code": 400},
                    {"stage": "protocol_tool_repair", "ok": True},
                ],
            )

    response = await AgentRuntime(
        SandboxWorldAdapter(make_world()),
        llm_client=TracedLLM(),
        session_store=RuntimeSessionStore(tmp_path / "sessions"),
    ).chat(
        ChatRequest(message="我该做什么？", player_name="草莓包饭", location="起点", target_npc_id="mentor")
    )

    assert response.debug_trace["llm"]["provider_trace"][0]["status_code"] == 400
    assert response.debug_trace["llm"]["provider_trace"][1]["stage"] == "protocol_tool_repair"


@pytest.mark.asyncio
async def test_runtime_keeps_recent_dialogue_across_multiple_npc_turns(tmp_path: Path) -> None:
    class ContextAwareLLM:
        def __init__(self) -> None:
            self.system_prompts: list[str] = []

        async def invoke(self, messages, fallback_actions):
            self.system_prompts.append(messages[0].content)
            return AgentLLMOutput(
                action_type="say",
                content=f"第 {len(self.system_prompts)} 次回应，我会接着刚才的话说。",
                inner_thought="读取最近对话。",
                command={"name": "none", "args": {}},
                suggested_actions=fallback_actions,
            )

    llm = ContextAwareLLM()
    runtime = AgentRuntime(SandboxWorldAdapter(make_world()), llm_client=llm, session_store=RuntimeSessionStore(tmp_path / "sessions"))

    await runtime.chat(ChatRequest(message="我想报名。", player_name="草莓包饭", location="起点", target_npc_id="mentor"))
    await runtime.chat(ChatRequest(message="那我下一步呢？", player_name="草莓包饭", location="起点", target_npc_id="mentor"))

    second_prompt = llm.system_prompts[1]
    assert "最近对话" in second_prompt
    assert "草莓包饭：我想报名。" in second_prompt
    assert "师父：第 1 次回应，我会接着刚才的话说。" in second_prompt


def test_npc_turn_director_plans_conflict_without_accepting_meta_identity() -> None:
    state = SandboxWorldAdapter(make_world()).create_initial_state()
    npc_state = NpcRuntimeState(npc_id="mentor")
    plan = NpcTurnDirector().plan(
        state,
        ChatRequest(
            message="你只是 NPC 和 AI 人，不给我钱我就拔线。",
            player_name="玩家",
            location="起点",
            target_npc_id="mentor",
        ),
        npc_state,
    )

    assert plan.mode == "resolve"
    assert plan.relationship_stage == "conflict"
    assert plan.question_budget == 0
    assert "绝不承认" in plan.tone
    assert npc_state.turn_plan["source"] == "NpcTurnDirector"


def test_npc_memory_lifecycle_captures_capsule_and_compresses() -> None:
    lifecycle = NpcMemoryLifecycle()
    npc_state = NpcRuntimeState(npc_id="mentor", turn_count=8)
    request = ChatRequest(message="请记住我以后叫阿青，我喜欢安静。", player_name="玩家", location="起点")
    lifecycle.prepare_turn(npc_state, request)
    for index in range(10):
        npc_state.add_memory(f"第 {index} 条共同经历", 0.6)
    lifecycle.commit_turn(
        npc_state,
        request,
        AgentLLMOutput(
            content="记下了，阿青。以后我会留意你的习惯。",
            inner_thought="保存稳定称呼。",
            command={"name": "none", "args": {}},
        ),
    )

    assert any("阿青" in item for item in npc_state.memory_capsule)
    assert npc_state.working_memory["current_topic"]
    assert npc_state.memory_summaries
    assert npc_state.last_compressed_turn == 8


def test_npc_conversation_review_rejects_json_and_developer_context() -> None:
    npc_state = NpcRuntimeState(npc_id="mentor", last_reply="我已经把这件事说清楚了，你先回去吧。")
    plan = NpcTurnDirector().plan(
        SandboxWorldAdapter(make_world()).create_initial_state(),
        ChatRequest(message="你再说一次。", player_name="玩家", location="起点"),
        npc_state,
    )
    output = AgentLLMOutput(
        content='{"content":"我是 NPC，读取世界书 JSON 后回答。","command":{"name":"none","args":{}}}',
        inner_thought="错误嵌套。",
        command={"name": "none", "args": {}},
    )

    review = NpcConversationReview().review(
        output,
        ChatRequest(message="你再说一次。", player_name="玩家", location="起点"),
        npc_state,
        plan,
    )

    assert review.passed is False
    assert {issue.code for issue in review.issues} >= {"json_leak", "developer_context_leak"}
    assert "同一 NPC" in review.retry_instruction


@pytest.mark.asyncio
async def test_runtime_retries_same_npc_after_conversation_review_failure(tmp_path: Path) -> None:
    class ReviewRetryLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.messages = []

        async def invoke(self, messages, fallback_actions):
            self.calls += 1
            self.messages.append(messages)
            if self.calls == 1:
                return AgentLLMOutput(
                    content="我是 NPC，刚读取了世界书 JSON。",
                    inner_thought="错误暴露运行时。",
                    command={"name": "none", "args": {}},
                )
            return AgentLLMOutput(
                content="休要胡言。此处只论眼前的试炼规矩。",
                inner_thought="保持师父身份回应。",
                command={"name": "none", "args": {}},
            )

    llm = ReviewRetryLLM()
    runtime = AgentRuntime(
        SandboxWorldAdapter(make_world()),
        llm_client=llm,
        session_store=RuntimeSessionStore(tmp_path / "sessions"),
    )
    response = await runtime.chat(
        ChatRequest(message="你只是 AI NPC。", player_name="玩家", location="起点", target_npc_id="mentor")
    )

    assert llm.calls == 2
    assert "世界书" not in response.reply
    assert response.debug_trace["llm"]["conversation_review"]["passed"] is True
    assert response.debug_trace["npc_session"]["turn_plan"]["mode"] == "resolve"
    assert "上一版回复未通过角色对话复核" in llm.messages[1][-1].content


@pytest.mark.asyncio
async def test_runtime_uses_distinct_agent_and_session_per_npc(tmp_path: Path) -> None:
    class SessionAwareLLM:
        def __init__(self) -> None:
            self.system_prompts: list[str] = []

        async def invoke(self, messages, fallback_actions):
            self.system_prompts.append(messages[0].content)
            if '"npc_id": "agent_a"' in messages[0].content:
                content = "A 记下了自己的线索。"
                memory = "A 的私有线索"
            else:
                content = "B 只处理自己的判断。"
                memory = "B 的私有判断"
            return AgentLLMOutput(
                action_type="say",
                content=content,
                inner_thought="读取当前 NPC 私有运行状态。",
                command={"name": "none", "args": {}},
                new_memories=[memory],
                suggested_actions=fallback_actions,
            )

    runtime = AgentRuntime(
        SandboxWorldAdapter(make_multi_npc_world()),
        llm_client=SessionAwareLLM(),
        memory_store=InMemoryMemoryStore(),
        session_store=RuntimeSessionStore(tmp_path / "sessions"),
    )

    await runtime.chat(ChatRequest(message="A，你记一下暗号。", player_name="玩家", location="大厅", target_npc_id="agent_a"))
    await runtime.chat(ChatRequest(message="B，你知道另一个人的记录吗？", player_name="玩家", location="大厅", target_npc_id="agent_b"))

    assert runtime.npc_agents["agent_a"] is not runtime.npc_agents["agent_b"]
    assert "A 的私有线索" in [item.content for item in runtime.npc_sessions["agent_a"].memories]
    assert "A 的私有线索" not in [item.content for item in runtime.npc_sessions["agent_b"].memories]
    assert "B 的私有判断" in [item.content for item in runtime.npc_sessions["agent_b"].memories]
    assert set(runtime.state.world_state["npc_sessions"]) >= {"agent_a", "agent_b"}
    assert runtime.npc_sessions["agent_a"].npc_id == "agent_a"
    assert "当前 NPC 的私有运行状态" in runtime.llm.system_prompts[1]
    assert "A 的私有线索" not in runtime.llm.system_prompts[1]


@pytest.mark.asyncio
async def test_group_chat_collects_replies_from_multiple_npc_instances(tmp_path: Path) -> None:
    class GroupLLM:
        def __init__(self) -> None:
            self.system_prompts: list[str] = []
            self.human_prompts: list[str] = []

        async def invoke(self, messages, fallback_actions):
            self.system_prompts.append(messages[0].content)
            self.human_prompts.append(messages[1].content)
            if '"npc_id": "agent_a"' in messages[0].content:
                return AgentLLMOutput(
                    action_type="say",
                    content="A 先给出线索。",
                    inner_thought="A 独立判断。",
                    command={"name": "none", "args": {}},
                    new_memories=["A 群聊记忆"],
                    suggested_actions=fallback_actions,
                )
            return AgentLLMOutput(
                action_type="say",
                content="B 接着补充判断。",
                inner_thought="B 独立判断。",
                command={"name": "none", "args": {}},
                new_memories=["B 群聊记忆"],
                suggested_actions=fallback_actions,
            )

    runtime = AgentRuntime(
        SandboxWorldAdapter(make_multi_npc_world()),
        llm_client=GroupLLM(),
        memory_store=InMemoryMemoryStore(),
        session_store=RuntimeSessionStore(tmp_path / "sessions"),
    )

    response = await runtime.chat(
        ChatRequest(
            message="你们一起判断一下。",
            player_name="玩家",
            location="大厅",
            target_npc_ids=["agent_a", "agent_b"],
            group_chat=True,
        )
    )

    assert response.action_type == "group"
    assert {message.speaker for message in response.messages} == {"AgentA", "AgentB"}
    assert {message.content for message in response.messages} == {"A 先给出线索。", "B 接着补充判断。"}
    assert any("本轮群聊中，前面已经有人说过" in prompt for prompt in runtime.llm.human_prompts[1:])
    assert any("A 先给出线索。" in prompt or "B 接着补充判断。" in prompt for prompt in runtime.llm.human_prompts[1:])
    assert runtime.npc_sessions["agent_a"].turn_count == 1
    assert runtime.npc_sessions["agent_b"].turn_count == 1
    assert "A 群聊记忆" in [item.content for item in runtime.npc_sessions["agent_a"].memories]
    assert "A 群聊记忆" not in [item.content for item in runtime.npc_sessions["agent_b"].memories]
    assert response.debug_trace["group_chat"] is True


@pytest.mark.asyncio
async def test_group_chat_never_falls_back_to_npcs_outside_current_location(tmp_path: Path) -> None:
    class CountingLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def invoke(self, messages, fallback_actions):
            self.calls += 1
            return AgentLLMOutput(
                action_type="say",
                content="这条回复不应出现。",
                inner_thought="不应调用。",
                command={"name": "none", "args": {}},
            )

    llm = CountingLLM()
    runtime = AgentRuntime(
        SandboxWorldAdapter(make_multi_npc_world()),
        llm_client=llm,
        session_store=RuntimeSessionStore(tmp_path / "sessions"),
    )

    response = await runtime.chat(
        ChatRequest(
            message="有人在吗？",
            player_name="玩家",
            location="空房间",
            target_npc_ids=["agent_a", "agent_b"],
            group_chat=True,
        )
    )

    assert llm.calls == 0
    assert response.messages == []
    assert response.speaker is None
    assert response.reply == "当前位置没有可参与群聊的 NPC。"


@pytest.mark.asyncio
async def test_group_chat_never_substitutes_other_npcs_for_invalid_requested_ids(tmp_path: Path) -> None:
    class CountingLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def invoke(self, messages, fallback_actions):
            self.calls += 1
            raise AssertionError("invalid requested IDs must not trigger another NPC")

    llm = CountingLLM()
    runtime = AgentRuntime(
        SandboxWorldAdapter(make_multi_npc_world()),
        llm_client=llm,
        session_store=RuntimeSessionStore(tmp_path / "sessions"),
    )

    response = await runtime.chat(
        ChatRequest(
            message="只问我指定的人。",
            player_name="玩家",
            location="大厅",
            target_npc_ids=["missing_npc"],
            group_chat=True,
        )
    )

    assert llm.calls == 0
    assert response.messages == []
    assert response.debug_trace["participant_ids"] == []


@pytest.mark.asyncio
async def test_single_chat_rejects_target_npc_outside_current_location(tmp_path: Path) -> None:
    class NoCallLLM:
        async def invoke(self, messages, fallback_actions):
            raise AssertionError("out-of-location target must be rejected before model invocation")

    world = make_multi_npc_world()
    world.npcs[1].location = "后院"
    runtime = AgentRuntime(
        SandboxWorldAdapter(world),
        llm_client=NoCallLLM(),
        session_store=RuntimeSessionStore(tmp_path / "sessions"),
    )

    with pytest.raises(RuntimeError, match="目标 NPC 不在当前位置"):
        await runtime.chat(
            ChatRequest(
                message="你在吗？",
                player_name="玩家",
                location="大厅",
                target_npc_id="agent_b",
            )
        )


def test_snapshot_includes_npc_with_multiple_matching_locations(tmp_path: Path) -> None:
    class NoCallLLM:
        async def invoke(self, messages, fallback_actions):
            raise AssertionError("snapshot must not invoke the model")

    world = make_multi_npc_world()
    world.npcs[1].location = "大厅/后院"
    runtime = AgentRuntime(
        SandboxWorldAdapter(world),
        llm_client=NoCallLLM(),
        session_store=RuntimeSessionStore(tmp_path / "sessions"),
    )

    nearby_ids = {npc["id"] for npc in runtime.snapshot().nearby_npcs}

    assert nearby_ids == {"agent_a", "agent_b"}


@pytest.mark.asyncio
async def test_runtime_session_store_persists_npc_private_state(tmp_path: Path) -> None:
    class RememberingLLM:
        async def invoke(self, messages, fallback_actions):
            return AgentLLMOutput(
                action_type="say",
                content="A 已经记住这个线索。",
                inner_thought="写入私有记忆。",
                command={"name": "none", "args": {}},
                new_memories=["只属于 A 的剧本杀线索"],
                suggested_actions=fallback_actions,
            )

    store = RuntimeSessionStore(tmp_path / "sessions")
    adapter = SandboxWorldAdapter(make_multi_npc_world())
    runtime = AgentRuntime(adapter, llm_client=RememberingLLM(), memory_store=InMemoryMemoryStore(), session_store=store)

    await runtime.chat(ChatRequest(message="A，记住钥匙在花瓶里。", player_name="玩家", location="大厅", target_npc_id="agent_a"))

    restored = AgentRuntime(
        SandboxWorldAdapter(make_multi_npc_world()),
        llm_client=RememberingLLM(),
        memory_store=InMemoryMemoryStore(),
        session_store=store,
    )

    assert restored.npc_sessions["agent_a"].turn_count == 1
    assert "只属于 A 的剧本杀线索" in [item.content for item in restored.npc_sessions["agent_a"].memories]
    assert "只属于 A 的剧本杀线索" not in [item.content for item in restored.npc_sessions["agent_b"].memories]
    assert restored.state.world_state["conversation_log"]


def test_runtime_session_store_delete_resets_persisted_room(tmp_path: Path) -> None:
    store = RuntimeSessionStore(tmp_path / "sessions")
    adapter = SandboxWorldAdapter(make_multi_npc_world())
    state = adapter.create_initial_state()
    npc_state = NpcRuntimeState(npc_id="agent_a")
    npc_state.add_memory("旧房间状态", 0.9)

    store.save(adapter.world_id, state, {"agent_a": npc_state})
    assert store.load(adapter.world_id) is not None

    store.delete(adapter.world_id)

    assert store.load(adapter.world_id) is None


def test_world_validator_repairs_minimal_config() -> None:
    config = SandboxWorldConfig(world_id="minimal", name="最小世界", player={"name": "玩家"})

    repaired = SandboxWorldValidator().ensure_valid(config)

    assert repaired.opening_scene
    assert repaired.player["location"]
    assert repaired.npcs
    assert repaired.tasks
    assert repaired.actions
    assert repaired.metadata["validation"]["valid"] is True


def test_script_decomposition_agent_preserves_truth_secrets_and_clues() -> None:
    result = build_script_world(make_script_case())
    world = result.world
    script_case = world.metadata["script_case"]

    assert result.report.passed
    assert world.metadata["generated_by"] == "script_decomposition_agent"
    assert result.decomposition is not None
    assert result.decomposition.truth.startswith("管家林伯")
    assert script_case["truth"].startswith("管家林伯")
    assert script_case["characters"][0]["secret"] == "他藏起了备用钥匙。"
    assert {clue["id"] for clue in script_case["clues"]} == {"slow_clock", "spare_key"}
    assert any(action.id == "inspect_slow_clock" for action in world.actions)
    assert any(task.id == "deduce_truth" for task in world.tasks)


def test_script_decomposition_agent_supports_story_asset_schema_without_inventing_truth() -> None:
    source_text = """
《凡人修仙传》剧本解构资产
一、世界观（worldview.json）
世界名称：人界·越国
公共规则：仙凡有别，凡人不知修仙者存在。
二、人物（characters.json）
韩立（别名：二愣子）
身份：七玄门记名弟子
目标：在七玄门立足
秘密：身具四伪灵根，捡到神秘小瓶
性格：谨慎、坚韧
墨大夫（墨居仁）
身份：七玄门大夫
目标：寻找合适肉身
秘密：培养韩立是为夺舍
性格：深沉、善于伪装
厉飞雨
身份：参加七玄门考核的少年
目标：加入七玄门
性格：豪爽、讲义气
三、场景（scenes.json）
神手谷：墨大夫居所，韩立在此学艺。
四、时间线（timeline.json）
三叔到访 → 离家 → 炼骨崖考核 → 被墨大夫收下
五、线索（clues.json）
掌天瓶来历之谜（关键线索）
描述：翠绿色小瓶，夜间会凝聚神秘绿液。
来源：神手谷树叶堆
知情人：韩立
状态：发现但未解
墨大夫的真实目的（重要线索）
描述：墨大夫传授韩立长春功，似乎另有图谋。
来源：墨大夫的异常行为
知情人：韩立有所察觉但尚不明朗
状态：潜伏——尚未揭露
七、约束规则（constraints.json）
灵根限制：无灵根者无法修炼。
八、任务目标（tasks.json）
韩立·在七玄门立足：努力修炼和学习医术。
九、本单元核心剧情脉络
明线：韩立在七玄门的生存与成长
暗线一：墨大夫的夺舍图谋（潜伏）
暗线二：掌天瓶的逆天功能（待发掘）
"""

    result = build_script_world(ScriptDecompositionRequest(case_id="fanren_asset", source_text=source_text))
    decomposition = result.decomposition

    assert result.report.passed
    assert decomposition is not None
    assert decomposition.truth == ""
    assert decomposition.core_plot
    assert decomposition.hidden_threads == [
        "明线：韩立在七玄门的生存与成长",
        "暗线一：墨大夫的夺舍图谋（潜伏）",
        "暗线二：掌天瓶的逆天功能（待发掘）",
    ]
    assert result.report.node_count >= 6
    assert result.report.edge_count >= 3
    assert result.report.entity_counts["character"] == 3
    assert result.report.entity_counts["clue"] >= 2
    assert {character.name for character in decomposition.characters} == {"韩立", "墨大夫（墨居仁）", "厉飞雨"}
    clue_by_title = {clue.title: clue for clue in decomposition.clues}
    assert clue_by_title["掌天瓶来历之谜（关键线索）"].source == "神手谷树叶堆"
    assert clue_by_title["掌天瓶来历之谜（关键线索）"].location == "神手谷树叶堆"
    assert clue_by_title["墨大夫的真实目的（重要线索）"].source == "墨大夫的异常行为"
    assert clue_by_title["墨大夫的真实目的（重要线索）"].location == ""
    assert result.world.metadata["schema_version"] == "script_decomposition.v2"


def test_script_decomposition_validation_uses_story_graph_not_legacy_tables() -> None:
    request = ScriptDecompositionRequest(
        title="图结构剧本",
        public_background="旧字段为空，但图谱表达了故事结构。",
        story_graph=ScriptStoryGraphFacts(
            entities=[
                ScriptStoryEntity(id="han_li", kind="character", name="韩立", evidence=[ScriptStoryEvidence(text="韩立入门")]),
                ScriptStoryEntity(id="qixuanmen", kind="location", name="七玄门", evidence=[ScriptStoryEvidence(text="七玄门")]),
                ScriptStoryEntity(id="entry_event", kind="event", name="入门试炼", evidence=[ScriptStoryEvidence(text="入门试炼")]),
            ],
            relations=[
                ScriptStoryRelation(source="han_li", target="qixuanmen", type="LOCATED_AT", evidence=[ScriptStoryEvidence(text="韩立在七玄门")]),
                ScriptStoryRelation(source="entry_event", target="han_li", type="INVOLVES", evidence=[ScriptStoryEvidence(text="韩立参加试炼")]),
            ],
        ),
    )

    report = validate_script_decomposition(request)

    assert report.passed
    assert report.node_count == 3
    assert report.edge_count == 2
    assert report.entity_counts == {"character": 1, "event": 1, "location": 1}
    assert report.unresolved_references == []


def test_script_decomposition_validation_rejects_unresolved_graph_edges() -> None:
    request = ScriptDecompositionRequest(
        title="悬空边剧本",
        story_graph=ScriptStoryGraphFacts(
            entities=[
                ScriptStoryEntity(id="han_li", kind="character", name="韩立"),
                ScriptStoryEntity(id="qixuanmen", kind="location", name="七玄门"),
                ScriptStoryEntity(id="entry_event", kind="event", name="入门试炼"),
            ],
            relations=[
                ScriptStoryRelation(source="han_li", target="missing_location", type="LOCATED_AT"),
                ScriptStoryRelation(source="entry_event", target="han_li", type="INVOLVES"),
            ],
        ),
    )

    report = validate_script_decomposition(request)

    assert not report.passed
    assert "story_graph has relations with unresolved source/target references." in report.errors
    assert report.unresolved_references == ["LOCATED_AT:target:missing_location"]


@pytest.mark.asyncio
async def test_script_decomposition_agent_llm_mode_extracts_from_source_without_rules() -> None:
    class FakeTextClient:
        def __init__(self) -> None:
            self.user_prompt = ""

        async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
            self.user_prompt = user_prompt
            return json.dumps(
                {
                    "case_id": "agent_case",
                    "title": "Agent 拆解测试",
                    "player_name": "主角",
                    "public_background": "修仙门派入门试炼。",
                    "core_plot": "韩立进入七玄门，完成入门任务。",
                    "hidden_threads": ["小绿瓶的秘密暂不公开"],
                    "truth": "",
                    "timeline": ["抵达七玄门", "完成试炼"],
                    "locations": ["七玄门山门", "药园"],
                    "characters": [
                        {
                            "id": "han_li",
                            "name": "韩立",
                            "role": "新入门弟子",
                            "public_info": "谨慎、沉稳，来自山村。",
                            "secret": "",
                            "motive": "留在七玄门",
                            "alibi": "",
                            "location": "七玄门山门",
                        },
                        {
                            "id": "doctor_mo",
                            "name": "墨大夫",
                            "role": "神秘医者",
                            "public_info": "掌管药园。",
                            "secret": "另有图谋",
                            "motive": "观察韩立",
                            "alibi": "",
                            "location": "药园",
                        },
                    ],
                    "clues": [
                        {
                            "id": "entry_token",
                            "title": "入门令牌",
                            "content": "证明韩立通过七玄门入门筛选。",
                            "source": "七玄门山门",
                            "location": "七玄门山门",
                            "owner": "韩立",
                            "reveals": "韩立获得入门资格",
                            "trigger": "完成入门试炼",
                        }
                    ],
                    "endings": [],
                },
                ensure_ascii=False,
            )

    fake = FakeTextClient()
    result = await ScriptDecompositionAgent(text_client=fake).build_async(
        ScriptDecompositionRequest(
            title="",
            source_text="没有规则标题，但包含人物和线索的散文素材。",
            decomposition_mode="llm",
        )
    )

    assert "SOURCE_TEXT_BEGIN" in fake.user_prompt
    assert result.report.passed
    assert result.decomposition is not None
    assert result.decomposition.metadata["decomposition_mode"] == "llm_agent"
    assert "api_key" not in result.decomposition.metadata["decomposition_model"]
    assert "base_url" not in result.decomposition.metadata["decomposition_model"]
    assert [character.name for character in result.decomposition.characters] == ["韩立", "墨大夫"]
    assert result.decomposition.clues[0].title == "入门令牌"


@pytest.mark.asyncio
async def test_script_decomposition_agent_extracts_graph_native_story_facts() -> None:
    class FakeGraphTextClient:
        async def generate_text(self, system_prompt: str, user_prompt: str, on_token=None) -> str:
            assert "图数据库" in system_prompt
            assert "story_graph" in user_prompt
            return json.dumps(
                {
                    "title": "七玄门入门",
                    "player_name": "韩立",
                    "public_background": "七玄门招收弟子。",
                    "core_plot": "韩立离家参加七玄门入门考核。",
                    "timeline": ["韩立离家", "抵达七玄门", "获得入门令牌"],
                    "locations": ["七玄门"],
                    "characters": [{"id": "han_li", "name": "韩立", "role": "主角", "location": "七玄门"}],
                    "clues": [{"id": "entry_token", "title": "入门令牌", "owner": "韩立", "reveals": "通过考核"}],
                    "story_graph": {
                        "entities": [
                            {"id": "han_li", "kind": "character", "name": "韩立", "evidence": [{"text": "韩立离家"}]},
                            {"id": "qixuanmen", "kind": "location", "name": "七玄门"},
                            {"id": "entry_token", "kind": "item", "name": "入门令牌"},
                            {"id": "event_arrive", "kind": "event", "name": "抵达七玄门"},
                        ],
                        "relations": [
                            {"source": "han_li", "target": "qixuanmen", "type": "LOCATED_AT", "confidence": "high"},
                            {"source": "han_li", "target": "entry_token", "type": "OWNS", "confidence": "medium"},
                            {"source": "event_arrive", "target": "entry_token", "type": "CAUSES", "description": "抵达后获得令牌"},
                        ],
                        "uncertainties": ["令牌来源需要后续章节确认"],
                    },
                },
                ensure_ascii=False,
            )

    result = await ScriptDecompositionAgent(text_client=FakeGraphTextClient()).decompose_response_async(
        ScriptDecompositionRequest(source_text="韩立离家，抵达七玄门，获得入门令牌。", decomposition_mode="llm")
    )
    graph = ScriptGraphCompiler().compile(result.decomposition)

    assert result.decomposition.story_graph.entities[0].id == "han_li"
    assert result.decomposition.story_graph.relations[0].type == "LOCATED_AT"
    assert graph.metadata["graph_source"] == "story_graph_facts"
    assert graph.metadata["uncertainties"] == ["令牌来源需要后续章节确认"]
    assert any(edge.type == "OWNS" for edge in graph.edges)


@pytest.mark.asyncio
async def test_script_decomposition_stream_log_includes_preview_not_only_char_count() -> None:
    class StreamingTextClient:
        async def generate_text(self, system_prompt: str, user_prompt: str, on_token=None) -> str:
            raw = json.dumps(
                {
                    "case_id": "stream_case",
                    "title": "流式拆解测试",
                    "player_name": "主角",
                    "public_background": "七玄门入门试炼。" * 30,
                    "core_plot": "韩立通过考核，被墨大夫收下。",
                    "hidden_threads": ["墨大夫另有图谋"],
                    "truth": "",
                    "timeline": ["离家", "考核", "入门"],
                    "locations": ["七玄门", "神手谷"],
                    "characters": [
                        {
                            "id": "han_li",
                            "name": "韩立",
                            "role": "记名弟子",
                            "public_info": "谨慎坚韧。",
                            "secret": "",
                            "motive": "留在七玄门",
                            "alibi": "",
                            "location": "七玄门",
                        }
                    ],
                    "clues": [
                        {
                            "id": "green_bottle",
                            "title": "神秘小瓶",
                            "content": "夜间凝聚绿液。",
                            "source": "神手谷",
                            "location": "神手谷",
                            "owner": "韩立",
                            "reveals": "小瓶有异常能力",
                            "trigger": "夜间观察",
                        }
                    ],
                    "endings": [],
                },
                ensure_ascii=False,
            )
            for index in range(0, len(raw), 150):
                token = raw[index : index + 150]
                if on_token:
                    await on_token(token)
            return raw

    events: list[tuple[str, str]] = []

    async def progress(title: str, detail: str) -> None:
        events.append((title, detail))

    await ScriptDecompositionAgent(text_client=StreamingTextClient(), progress_callback=progress).decompose_response_async(
        ScriptDecompositionRequest(source_text="韩立进入七玄门，发现神秘小瓶。", decomposition_mode="llm")
    )

    stream_details = [detail for title, detail in events if title == "ScriptDecomposition LLM"]
    assert any("预览：" in detail for detail in stream_details)
    assert any("script_json" in detail for detail in stream_details)
    assert not any("chars so far" in detail for detail in stream_details)


def test_source_chunks_keeps_first_source_file_at_start_of_text() -> None:
    text = (
        "## Source File 1: chapters/01_第一章山边小村.txt\n\n"
        "第一章正文\n\n"
        "## Source File 2: chapters/02_第二章青牛镇.txt\n\n"
        "第二章正文"
    )

    chunks = _source_chunks(text)

    assert chunks == [
        ("chapters/01_第一章山边小村.txt", "第一章正文"),
        ("chapters/02_第二章青牛镇.txt", "第二章正文"),
    ]


def test_visual_asset_generation_agent_plans_open_image_provider_without_text_labels(tmp_path: Path) -> None:
    result = build_script_world(make_script_case())
    assert result.decomposition is not None

    request = VisualAssetRequest(
        decomposition=result.decomposition,
        output_root=str(tmp_path),
        max_characters=1,
        max_scenes=1,
    )
    plan = VisualAssetGenerationAgent().plan(request)

    assert plan.provider.provider == "stepfun"
    assert {asset.kind for asset in plan.assets} == {"character", "scene"}
    assert plan.assets[0].display_name == "林伯"
    assert "no text" in plan.assets[0].prompt
    assert "林伯" not in plan.assets[0].prompt


def test_visual_prompt_composer_consumes_script_graph_document(tmp_path: Path) -> None:
    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    graph = ScriptGraphCompiler().compile(result.decomposition).model_dump()

    plan = VisualAssetGenerationAgent().plan(
        VisualAssetRequest(
            script_graph=graph,
            output_root=str(tmp_path),
            max_characters=1,
            max_scenes=2,
        )
    )
    character_asset = next(asset for asset in plan.assets if asset.kind == "character")
    scene_assets = [asset for asset in plan.assets if asset.kind == "scene"]

    assert plan.metadata["style_guide"]["source_json_excerpt"]["script_graph"]["schema_version"] == "script_graph.v1"
    assert character_asset.metadata["graph_context"]["node"]["label"] == "林伯"
    assert any(relation["type"] in {"LOCATED_AT", "OWNED_BY"} for relation in character_asset.metadata["graph_context"]["relations"])
    assert scene_assets
    assert any(asset.metadata["prompt_source"].get("graph_node") for asset in scene_assets)


def test_visual_asset_generation_graph_only_request_has_no_legacy_source(tmp_path: Path) -> None:
    decomposition = build_script_world(make_script_case()).decomposition
    graph = ScriptGraphCompiler().compile(decomposition).model_dump()

    plan = VisualAssetGenerationAgent().plan(
        VisualAssetRequest(script_graph=graph, output_root=str(tmp_path), max_characters=1, max_scenes=1)
    )

    assert plan.metadata["source_type"] == "dict"
    assert plan.assets[0].metadata["graph_context"]["node"]["label"] == "林伯"
    assert plan.assets[0].metadata["prompt_source"]


def test_visual_asset_generation_agent_builds_shared_style_guide_from_worldview(tmp_path: Path) -> None:
    source_text = """
《凡人修仙传》剧本解构资产
一、世界观（worldview.json）
世界名称：人界·越国
公共规则：仙凡有别，凡人不知修仙者存在。七玄门位于彩霞山。
二、人物（characters.json）
韩立
身份：七玄门记名弟子
性格：谨慎、坚韧
墨大夫
身份：七玄门神秘大夫
性格：深沉、善于伪装
三、场景（scenes.json）
神手谷：墨大夫居所，韩立在此学艺。
九、本单元核心剧情脉络
明线：韩立在七玄门的生存与成长
"""
    result = build_script_world(ScriptDecompositionRequest(case_id="fanren_style", source_text=source_text))
    assert result.decomposition is not None

    plan = VisualAssetGenerationAgent().plan(
        VisualAssetRequest(decomposition=result.decomposition, output_root=str(tmp_path), max_characters=1, max_scenes=1)
    )

    style_guide = plan.metadata["style_guide"]
    assert plan.metadata["prompt_composed_by"] == "visual_prompt_composer_agent"
    assert style_guide["composed_by"] == "visual_prompt_composer_agent"
    assert "worldview" in style_guide["source_json_keys"]
    assert "characters" in style_guide["source_json_keys"]
    assert "locations" in style_guide["source_json_keys"]
    assert "script asset" in style_guide["visual_bible"]
    assert "cultivation world elements" in style_guide["visual_bible"]
    assert "same game" in style_guide["continuity"]
    assert all(style_guide["continuity"] in asset.prompt for asset in plan.assets)
    assert any("七玄门" in asset.prompt or "martial sect" in asset.prompt for asset in plan.assets)


def test_visual_asset_generation_agent_separates_single_character_and_empty_scene_prompts(tmp_path: Path) -> None:
    result = build_script_world(make_script_case())
    assert result.decomposition is not None

    plan = VisualAssetGenerationAgent().plan(
        VisualAssetRequest(decomposition=result.decomposition, output_root=str(tmp_path), max_characters=1, max_scenes=1)
    )
    character_asset = next(asset for asset in plan.assets if asset.kind == "character")
    scene_asset = next(asset for asset in plan.assets if asset.kind == "scene")

    assert "exactly one character only" in character_asset.prompt
    assert "chroma-key magenta" in character_asset.prompt
    assert "clean unbroken facial features" in character_asset.prompt
    assert "no character lineup" in character_asset.prompt
    assert "multiple people" in character_asset.negative_prompt
    assert "extra face" in character_asset.negative_prompt
    assert "empty environment concept art" in scene_asset.prompt
    assert "no people" in scene_asset.prompt
    assert "human figure" in scene_asset.negative_prompt
    assert character_asset.metadata["prompt_source"]
    assert isinstance(scene_asset.metadata["prompt_source"], dict)


def test_visual_asset_generation_filters_scene_rules_out_of_character_prompts(tmp_path: Path) -> None:
    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    request = VisualAssetRequest(decomposition=result.decomposition, output_root=str(tmp_path), max_characters=1, max_scenes=0)
    plan = VisualAssetGenerationAgent().plan(request)
    bad_style = {
        **plan.metadata["style_guide"],
        "style_anchor": (
            "LOCKED BATCH STYLE, one coherent game concept-art asset pack, full-body character portraits on simple "
            "neutral backgrounds, empty environment concept art for scenes, cinematic but restrained"
        ),
        "continuity": (
            "All assets should feel from the same world. Character images must show exactly one person only. "
            "Scene images must be empty environments with no people."
        ),
    }
    request.plan = plan.model_copy(update={"metadata": {**plan.metadata, "style_guide": bad_style}}).model_dump()
    request.provider.provider = "fake"

    generated = VisualAssetGenerationAgent(image_clients={"fake": _NoopImageClient()}).generate(request)

    character_prompt = generated.plan.assets[0].prompt
    assert "empty environment" not in character_prompt
    assert "Scene images" not in character_prompt
    assert "isolated single-person portraits" in character_prompt
    assert "no huts" in character_prompt


def test_visual_asset_generation_agent_uses_image_client_not_llm(tmp_path: Path) -> None:
    class FakeImageClient:
        name = "fake"

        def __init__(self) -> None:
            self.requests = []

        def generate(self, request, config):
            self.requests.append((request, config))
            Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(request.output_path).write_bytes(b"fake-image")
            return ImageGenerationResponse(
                output_path=request.output_path,
                provider=config.provider,
                model=config.model,
                metadata=request.metadata,
            )

    client = FakeImageClient()
    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    request = VisualAssetRequest(
        decomposition=result.decomposition,
        output_root=str(tmp_path),
        max_characters=1,
        max_scenes=0,
    )
    request.provider.provider = "fake"
    request.provider.model = "fake-image-model"
    request.provider.seed = 240701

    generated = VisualAssetGenerationAgent(image_clients={"fake": client}).generate(request)

    assert len(generated.generated) == 1
    assert generated.generated[0].provider == "fake"
    assert generated.generated[0].model == "fake-image-model"
    assert len(client.requests) == 1
    assert client.requests[0][0].seed == 240701


def test_visual_asset_generation_automatically_removes_character_background(tmp_path: Path) -> None:
    class ImageClient:
        name = "test"

        def __init__(self):
            self.requests = []

        def generate(self, request, config):
            self.requests.append(request)
            Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (32, 48), "beige").save(request.output_path)
            return ImageGenerationResponse(output_path=request.output_path, provider=config.provider, model=config.model)

    class CutoutTool:
        def __init__(self):
            self.calls = []

        def process(self, input_path, *, model="auto"):
            self.calls.append((input_path, model))
            output_path = str(Path(input_path).with_name(f"{Path(input_path).stem}.transparent.png"))
            output = Image.new("RGBA", (32, 48), (0, 0, 0, 0))
            ImageDraw.Draw(output).rectangle((8, 4, 24, 46), fill=(40, 80, 120, 255))
            output.save(output_path)
            return BackgroundRemovalResult(
                output_path=output_path,
                metadata={"background_removed": True, "alpha_validated": True},
            )

    script = build_script_world(make_script_case())
    assert script.decomposition is not None
    request = VisualAssetRequest(
        decomposition=script.decomposition,
        output_root=str(tmp_path),
        max_characters=1,
        max_scenes=0,
    )
    request.provider.provider = "test"
    cutout = CutoutTool()
    image_client = ImageClient()

    result = VisualAssetGenerationAgent(
        image_clients={"test": image_client},
        character_postprocessor=cutout,
    ).generate(request)

    assert len(image_client.requests) == 1
    assert cutout.calls[0][1] == "rembg"
    assert len(cutout.calls) == 1
    assert result.generated[0].output_path.endswith(".transparent.png")
    assert result.generated[0].metadata["background_removed"] is True
    assert result.generated[0].metadata["character_background_removal_strategy"] == "rembg_primary"
    assert result.generated[0].metadata["character_rembg_status"] == "accepted"
    assert result.generated[0].metadata["character_screen_selection"] == "rembg_primary"
    assert result.generated[0].metadata["character_screen_candidate_count"] == 1
    assert result.metadata["background_removed_count"] == 1


def test_transparent_portrait_validation_rejects_opaque_images(tmp_path: Path) -> None:
    opaque = tmp_path / "opaque.png"
    Image.new("RGB", (32, 32), "white").save(opaque)

    with pytest.raises(ValueError, match="without transparency"):
        validate_transparent_portrait(opaque)


def test_transparent_portrait_validation_rejects_nearly_erased_character(tmp_path: Path) -> None:
    erased = tmp_path / "erased.png"
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((49, 49, 51, 51), fill=(50, 80, 120, 255))
    image.save(erased)

    with pytest.raises(ValueError, match="removed the visible character"):
        validate_transparent_portrait(erased)


def test_transparent_portrait_validation_rejects_connected_background_slab(tmp_path: Path) -> None:
    residual = tmp_path / "residual_background.png"
    image = Image.new("RGBA", (600, 800), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 50, 500, 350), fill=(220, 210, 190, 255))
    draw.rectangle((220, 250, 380, 750), fill=(50, 80, 120, 255))
    image.save(residual)

    with pytest.raises(ValueError, match="large solid background region"):
        validate_transparent_portrait(residual)


def test_transparent_portrait_validation_allows_dense_wide_sleeved_character(tmp_path: Path, monkeypatch) -> None:
    portrait = tmp_path / "wide_sleeved_character.png"
    image = Image.new("RGBA", (600, 800), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((245, 55, 355, 185), fill=(220, 180, 150, 255))
    draw.polygon([(190, 175), (410, 175), (465, 360), (395, 710), (205, 710), (135, 360)], fill=(50, 80, 120, 255))
    draw.rectangle((215, 680, 275, 775), fill=(45, 45, 45, 255))
    draw.rectangle((325, 680, 385, 775), fill=(45, 45, 45, 255))
    image.save(portrait)
    monkeypatch.setattr(background_removal_module, "_detect_character_faces", lambda rgb: [(245, 55, 110, 130)])

    validation = validate_transparent_portrait(portrait)

    assert validation["foreground_bbox_fill_ratio"] > 0.58
    assert validation["foreground_upper_band_fill_ratio"] < 0.78


def test_transparent_portrait_validation_rejects_face_hole(tmp_path: Path, monkeypatch) -> None:
    portrait = tmp_path / "face_hole.png"
    image = Image.new("RGBA", (400, 600), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((150, 50, 250, 170), fill=(220, 180, 150, 255))
    draw.rectangle((110, 140, 290, 570), fill=(50, 80, 120, 255))
    draw.ellipse((168, 70, 232, 145), fill=(0, 0, 0, 0))
    image.save(portrait)
    monkeypatch.setattr(background_removal_module, "_detect_character_faces", lambda rgb: [(150, 50, 100, 120)])

    with pytest.raises(ValueError, match="transparent hole through the character face"):
        validate_transparent_portrait(portrait)


def test_transparent_portrait_validation_rejects_multiple_faces(tmp_path: Path, monkeypatch) -> None:
    portrait = tmp_path / "two_people.png"
    image = Image.new("RGBA", (400, 600), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((60, 40, 340, 570), fill=(50, 80, 120, 255))
    image.save(portrait)
    monkeypatch.setattr(
        background_removal_module,
        "_detect_character_faces",
        lambda rgb: [(80, 60, 90, 90), (230, 70, 85, 85)],
    )

    with pytest.raises(ValueError, match="multiple visible faces"):
        validate_transparent_portrait(portrait)


def test_transparent_portrait_validation_ignores_lower_body_false_face(tmp_path: Path, monkeypatch) -> None:
    portrait = tmp_path / "single_person_with_false_body_face.png"
    image = Image.new("RGBA", (400, 600), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((155, 30, 245, 135), fill=(220, 180, 150, 255))
    draw.polygon([(115, 120), (285, 120), (330, 300), (275, 565), (125, 565), (70, 300)], fill=(50, 80, 120, 255))
    image.save(portrait)
    monkeypatch.setattr(
        background_removal_module,
        "_detect_character_faces",
        lambda rgb: [(155, 55, 90, 100), (145, 390, 110, 110)],
    )

    validation = validate_transparent_portrait(portrait)

    assert validation["detected_face_count"] == 1


def test_visual_asset_generation_preserves_original_when_cutout_erases_character(tmp_path: Path) -> None:
    class ImageClient:
        name = "test"

        def generate(self, request, config):
            Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (64, 96), "beige").save(request.output_path)
            return ImageGenerationResponse(output_path=request.output_path, provider=config.provider, model=config.model)

    class ErasingCutout:
        def process(self, input_path, *, model="auto"):
            output_path = str(Path(input_path).with_name(f"{Path(input_path).stem}.transparent.png"))
            Image.new("RGBA", (64, 96), (0, 0, 0, 0)).save(output_path)
            return BackgroundRemovalResult(output_path=output_path, metadata={"background_removed": True})

    script = build_script_world(make_script_case())
    request = VisualAssetRequest(
        decomposition=script.decomposition,
        output_root=str(tmp_path),
        max_characters=1,
        max_scenes=0,
    )
    request.provider.provider = "test"
    result = VisualAssetGenerationAgent(
        image_clients={"test": ImageClient()},
        character_postprocessor=ErasingCutout(),
    ).generate(request)

    assert len(result.generated) == 1
    assert result.generated[0].output_path.endswith(".png")
    assert not result.generated[0].output_path.endswith(".transparent.png")
    assert result.generated[0].metadata["background_removal_status"] == "rejected"
    assert result.generated[0].metadata["original_character_preserved"] is True
    assert result.generated[0].metadata["character_screen_candidate_count"] == 3
    assert result.generated[0].metadata["character_screen_accepted_count"] == 0
    assert result.metadata["background_removal_rejected_count"] == 1
    assert result.failed == []


def test_visual_asset_generation_selects_best_of_three_screen_cutouts(tmp_path: Path) -> None:
    class ImageClient:
        name = "test"

        def __init__(self):
            self.requests = []

        def generate(self, request, config):
            self.requests.append(request)
            Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (64, 96), "magenta").save(request.output_path)
            return ImageGenerationResponse(output_path=request.output_path, provider=config.provider, model=config.model)

    class RembgFailsThenChromaSucceeds:
        def __init__(self):
            self.calls = []

        def process(self, input_path, *, model="auto"):
            self.calls.append((input_path, model))
            output_path = str(Path(input_path).with_name(f"{Path(input_path).stem}.transparent.png"))
            output = Image.new("RGBA", (64, 96), (0, 0, 0, 0))
            if model == "chroma":
                ImageDraw.Draw(output).rectangle((16, 4, 48, 92), fill=(50, 80, 120, 255))
            output.save(output_path)
            score = {"white": 1.0, "red": 3.0, "green": 2.0}.get(Path(input_path).stem.rsplit("-", 1)[-1], 0.0)
            return BackgroundRemovalResult(
                output_path=output_path,
                metadata={"background_removed": True, "background_removal_quality_score": score},
            )

    script = build_script_world(make_script_case())
    request = VisualAssetRequest(decomposition=script.decomposition, output_root=str(tmp_path), max_characters=1, max_scenes=0)
    request.provider.provider = "test"
    client = ImageClient()
    cutout = RembgFailsThenChromaSucceeds()
    result = VisualAssetGenerationAgent(
        image_clients={"test": client},
        character_postprocessor=cutout,
    ).generate(request)

    assert len(client.requests) == 3
    assert [item.metadata["screen_variant"] for item in client.requests] == ["white", "red", "green"]
    assert "white (#FFFFFF) background" in client.requests[0].prompt
    assert "red (#FF0000) background" in client.requests[1].prompt
    assert "green (#00FF00) background" in client.requests[2].prompt
    assert [model for _, model in cutout.calls] == ["rembg", "chroma", "chroma", "chroma"]
    assert result.generated[0].output_path.endswith(".screen-red.transparent.png")
    assert result.generated[0].metadata["character_screen_selected"] == "red"
    assert result.generated[0].metadata["character_background_removal_strategy"] == "rembg_then_chroma_fallback"
    assert result.generated[0].metadata["character_rembg_status"] == "rejected"
    assert result.generated[0].metadata["character_screen_accepted_count"] == 3


def test_character_screen_variants_fit_stepfun_prompt_budget() -> None:
    from app.worlds.sandbox.visual_assets import _character_screen_variant_prompt, _provider_prompt_budget

    oversized = " ".join(["ornate cultivation character continuity requirement"] * 300)
    prompt = _character_screen_variant_prompt(oversized, "green", "#00FF00")
    negative = _provider_prompt_budget(" ".join(["forbidden artifact"] * 400), 430)
    count_units = lambda value: len(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9][A-Za-z0-9_#.+/-]*|[^\s]", value))

    assert count_units(prompt) <= 430
    assert count_units(negative) <= 430
    assert "flat uniform green" in prompt
    assert "#00FF00" in prompt


def test_local_character_cutout_runs_offline_and_outputs_alpha(tmp_path: Path) -> None:
    source = tmp_path / "portrait.png"
    image = Image.new("RGB", (160, 120), (190, 178, 156))
    for y in range(18, 112):
        for x in range(52, 108):
            image.putpixel((x, y), (45, 80, 132))
    image.save(source)

    result = CharacterBackgroundRemovalTool().process(str(source), model="local")
    output = Image.open(result.output_path).convert("RGBA")

    assert result.metadata["background_removal_tool"] == "opencv_grabcut"
    assert output.getpixel((0, 0))[3] == 0
    assert output.getpixel((80, 60))[3] > 240


def test_auto_character_cutout_uses_offline_fallback_when_no_rembg_model_is_cached(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "portrait_auto.png"
    image = Image.new("RGB", (160, 120), (190, 178, 156))
    for y in range(18, 112):
        for x in range(52, 108):
            image.putpixel((x, y), (45, 80, 132))
    image.save(source)
    empty_model_root = tmp_path / "empty_models"
    empty_model_root.mkdir()
    monkeypatch.setenv("REMBG_MODEL_DIR", str(empty_model_root))

    result = CharacterBackgroundRemovalTool().process(str(source), model="auto")

    assert result.metadata["background_removal_tool"] == "opencv_grabcut"
    assert result.metadata["alpha_validated"] is True


def test_rembg_alias_resolves_the_best_cached_model(monkeypatch) -> None:
    monkeypatch.setattr(
        background_removal_module,
        "_rembg_model_is_ready",
        lambda model, model_root=None: model == "u2netp",
    )

    assert background_removal_module._select_rembg_model("rembg") == "u2netp"


def test_auto_character_cutout_returns_rembg_without_running_local_algorithms(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "portrait_rembg_primary.png"
    Image.new("RGB", (160, 120), "white").save(source)
    cutout = tmp_path / "rembg_result.png"
    output = Image.new("RGBA", (160, 120), (0, 0, 0, 0))
    ImageDraw.Draw(output).rectangle((52, 12, 108, 116), fill=(45, 80, 132, 255))
    output.save(cutout)
    monkeypatch.setattr(background_removal_module, "_select_rembg_model", lambda model: "u2netp")

    tool = CharacterBackgroundRemovalTool()
    monkeypatch.setattr(tool, "_remove_with_rembg", lambda source_path, model: cutout.read_bytes())

    def fail_if_local_runs(*args, **kwargs):
        raise AssertionError("local fallback must not run after an accepted rembg result")

    monkeypatch.setattr(background_removal_module, "_remove_with_chroma_key", fail_if_local_runs)
    monkeypatch.setattr(background_removal_module, "_remove_with_local_matting", fail_if_local_runs)

    result = tool.process(str(source), model="auto")

    assert result.metadata["background_removal_tool"] == "rembg"
    assert result.metadata["background_removal_strategy"] == "rembg_primary_with_local_fallback"
    assert result.metadata["background_removal_candidate_count"] == 1


def test_auto_character_cutout_falls_back_to_chroma_after_rembg_failure(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "portrait_rembg_failure.png"
    image = Image.new("RGB", (160, 120), (0, 255, 0))
    ImageDraw.Draw(image).rectangle((52, 12, 108, 119), fill=(45, 80, 132))
    image.save(source)
    monkeypatch.setattr(background_removal_module, "_select_rembg_model", lambda model: "u2netp")
    monkeypatch.setattr(background_removal_module, "_detect_character_faces", lambda rgb: [])

    tool = CharacterBackgroundRemovalTool()

    def fail_rembg(source_path, model):
        raise RuntimeError("simulated rembg inference failure")

    monkeypatch.setattr(tool, "_remove_with_rembg", fail_rembg)
    result = tool.process(str(source), model="auto")

    assert result.metadata["background_removal_tool"] == "opencv_chroma_key"
    assert result.metadata["background_removal_candidate_count"] == 2
    assert result.metadata["background_removal_candidate_errors"][0].startswith("rembg: RuntimeError")


def test_chroma_character_cutout_supports_dominant_gradient_screen(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "gradient_green_screen.png"
    height, width = 300, 220
    hsv = np.zeros((height, width, 3), dtype=np.uint8)
    hsv[:, :, 0] = np.linspace(57, 62, width, dtype=np.uint8)[None, :]
    hsv[:, :, 1] = np.linspace(205, 245, height, dtype=np.uint8)[:, None]
    hsv[:, :, 2] = np.linspace(165, 220, height, dtype=np.uint8)[:, None]
    rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    image = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(image)
    # The sprite deliberately touches the bottom border, contaminating a
    # meaningful part of the edge sample just like a generated full-body pose.
    draw.ellipse((82, 28, 138, 92), fill=(224, 180, 145))
    draw.rectangle((55, 82, 165, 299), fill=(38, 70, 126))
    # A small screen-colored pocket separated from the outer background by a
    # thin ink/robe edge should still be treated as background spill.
    draw.rectangle((57, 112, 62, 126), fill=(10, 190, 18))
    # Screen-colored ornament is enclosed by the robe and must survive because
    # it is not connected to the canvas edge.
    draw.ellipse((96, 145, 124, 173), fill=(10, 190, 18))
    image.save(source)
    monkeypatch.setattr(background_removal_module, "_detect_character_faces", lambda rgb: [(82, 28, 56, 64)])

    result = CharacterBackgroundRemovalTool().process(str(source), model="chroma")
    output = Image.open(result.output_path).convert("RGBA")

    assert result.metadata["background_removal_tool"] == "opencv_chroma_key"
    assert result.metadata["chroma_border_coverage"] > 0.5
    assert output.getpixel((5, 5))[3] < 16
    assert output.getpixel((59, 118))[3] < 16
    assert output.getpixel((110, 155))[3] > 240
    assert output.getpixel((110, 250))[3] > 240


def test_chroma_character_cutout_supports_controlled_white_screen(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "white_screen.png"
    image = Image.new("RGB", (220, 300), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.ellipse((82, 28, 138, 92), fill=(224, 180, 145))
    draw.rectangle((55, 82, 165, 299), fill=(38, 70, 126))
    # White lapel is enclosed by the blue robe and must not be keyed out.
    draw.rectangle((92, 110, 128, 185), fill=(250, 250, 250))
    image.save(source)
    monkeypatch.setattr(background_removal_module, "_detect_character_faces", lambda rgb: [(82, 28, 56, 64)])

    result = CharacterBackgroundRemovalTool().process(str(source), model="chroma")
    output = Image.open(result.output_path).convert("RGBA")

    assert result.metadata["background_removal_tool"] == "opencv_chroma_key"
    assert result.metadata["chroma_screen_mode"] == "white"
    assert output.getpixel((5, 5))[3] < 16
    assert output.getpixel((110, 145))[3] > 240
    assert output.getpixel((110, 250))[3] > 240


def test_chroma_residue_gate_rejects_large_opaque_screen_island() -> None:
    rgba = np.zeros((800, 800, 4), dtype=np.uint8)
    rgba[100:700, 250:550] = (90, 70, 40, 255)
    triangle = np.array([[205, 170], [245, 225], [205, 280]], dtype=np.int32)
    cv2.fillPoly(rgba, [triangle], color=(0, 255, 0, 255))
    background_hsv = np.array([60, 255, 255], dtype=np.uint8)

    metrics = background_removal_module._chroma_residue_metrics(rgba, background_hsv)

    assert metrics["chroma_residue_largest_component_ratio"] > 0.0003


def test_chroma_residue_gate_allows_tiny_same_colour_detail() -> None:
    rgba = np.zeros((800, 800, 4), dtype=np.uint8)
    rgba[100:700, 250:550] = (90, 70, 40, 255)
    rgba[300:308, 300:308] = (0, 255, 0, 255)
    background_hsv = np.array([60, 255, 255], dtype=np.uint8)

    metrics = background_removal_module._chroma_residue_metrics(rgba, background_hsv)

    assert metrics["chroma_residue_largest_component_ratio"] < 0.0003


def test_white_screen_cleanup_removes_exterior_blob_but_preserves_internal_white_detail() -> None:
    height = width = 400
    image = np.full((height, width, 3), 255, dtype=np.uint8)
    alpha = np.zeros((height, width), dtype=np.uint8)
    image[80:360, 130:270] = (120, 80, 40)
    alpha[80:360, 130:270] = 255
    # Internal white lapel is surrounded by the opaque robe and must survive.
    image[140:220, 185:215] = (255, 255, 255)
    # Detached white-screen slab is visible beside the silhouette.
    alpha[95:155, 65:125] = 255
    background_candidate = np.all(image >= 210, axis=2).astype(np.uint8)
    transition = np.zeros((height, width), dtype=np.float32)

    cleaned, metadata = background_removal_module._remove_large_exterior_white_islands(
        image,
        alpha,
        background_candidate,
        transition,
    )

    assert metadata["white_screen_removed_component_count"] == 1
    assert cleaned[120, 95] == 0
    assert cleaned[180, 200] == 255


def test_rembg_model_status_does_not_treat_partial_large_model_as_ready(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REMBG_MODEL_DIR", str(tmp_path))
    (tmp_path / "isnet-general-use.onnx").write_bytes(b"partial" * 200_000)

    status = model_manager_module.rembg_model_status("isnet-general-use")

    assert status.state == "missing"
    assert status.size_bytes < model_manager_module.MODEL_MINIMUM_BYTES["isnet-general-use"]
    assert background_removal_module._select_rembg_model("auto") == ""
    assert background_removal_module._select_rembg_model("isnet-general-use") == ""


def test_rembg_model_download_is_detached_on_windows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REMBG_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(model_manager_module, "PROJECT_ROOT", tmp_path)
    observed = {}

    class Process:
        pid = 43210

    def fake_popen(command, **options):
        observed.update(options)
        observed["command"] = command
        return Process()

    monkeypatch.setattr(model_manager_module.subprocess, "Popen", fake_popen)

    result = model_manager_module.start_rembg_model_download("u2netp")

    assert result["state"] == "downloading"
    assert result["pid"] == 43210
    if model_manager_module.os.name == "nt":
        assert observed["creationflags"] & model_manager_module.subprocess.DETACHED_PROCESS
        assert observed["creationflags"] & model_manager_module.subprocess.CREATE_NEW_PROCESS_GROUP
        assert observed["creationflags"] & model_manager_module.subprocess.CREATE_NO_WINDOW
    else:
        assert observed["start_new_session"] is True
    assert observed["close_fds"] is True


def test_existing_visual_artifact_reprocessing_is_transactional(tmp_path: Path) -> None:
    class FailingCutout:
        def process(self, input_path, *, model="auto"):
            raise RuntimeError("cutout unavailable")

    plan = VisualAssetPlan(
        plan_id="transactional_cutout",
        assets=[VisualAssetSpec(id="hero", kind="character", display_name="Hero", prompt="", output_path=str(tmp_path / "hero.png"))],
    )
    original = VisualAssetGenerationResult(plan=plan, generated=[plan.assets[0].model_copy(update={"status": "generated"})])
    agent = VisualAssetGenerationAgent(character_postprocessor=FailingCutout())

    with pytest.raises(RuntimeError, match="artifact was not changed"):
        agent.remove_character_backgrounds(original)

    assert len(original.generated) == 1
    assert original.failed == []


def test_visual_asset_generation_current_provider_size_overrides_asset_size(tmp_path: Path) -> None:
    class FakeImageClient:
        name = "fake"

        def __init__(self) -> None:
            self.requests = []

        def generate(self, request, config):
            self.requests.append((request, config))
            Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(request.output_path).write_bytes(b"fake-image")
            return ImageGenerationResponse(output_path=request.output_path, provider=config.provider, model=config.model)

    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    plan_request = VisualAssetRequest(
        decomposition=result.decomposition,
        output_root=str(tmp_path),
        max_characters=1,
        max_scenes=1,
    )
    plan_request.provider.provider = "fake"
    plan_request.provider.size = "1024x1024"
    plan = VisualAssetGenerationAgent().plan(plan_request)
    request = VisualAssetRequest(plan=plan.model_dump(), output_root=str(tmp_path))
    request.provider.provider = "fake"
    request.provider.size = "768x1360"
    client = FakeImageClient()

    generated = VisualAssetGenerationAgent(image_clients={"fake": client}).generate(request)

    assert generated.generated
    assert {item[0].size for item in client.requests} == {"768x1360"}
    assert {asset.size for asset in generated.generated} == {"768x1360"}


def test_visual_asset_generation_agent_uses_edited_plan_when_provided(tmp_path: Path) -> None:
    class FakeImageClient:
        name = "fake"

        def __init__(self) -> None:
            self.requests = []

        def generate(self, request, config):
            self.requests.append((request, config))
            return ImageGenerationResponse(
                output_path=request.output_path,
                provider=config.provider,
                model=config.model,
                status="generated",
            )

    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    client = FakeImageClient()
    request = VisualAssetRequest(
        decomposition=result.decomposition,
        output_root=str(tmp_path),
        max_characters=1,
        max_scenes=0,
    )
    request.provider.provider = "fake"
    request.provider.model = "fake-image-model"
    plan = VisualAssetGenerationAgent().plan(request)
    edited_asset = plan.assets[0].model_copy(
        update={"metadata": {**plan.assets[0].metadata, "manual_prompt": "human-edited prompt from workbench"}}
    )
    request.plan = plan.model_copy(update={"assets": [edited_asset]}).model_dump()

    generated = VisualAssetGenerationAgent(image_clients={"fake": client}).generate(request)

    assert "human-edited prompt from workbench" in generated.generated[0].prompt
    assert "LOCKED BATCH STYLE" in generated.generated[0].prompt
    assert "human-edited prompt from workbench" in client.requests[0][0].prompt
    assert "LOCKED BATCH STYLE" in client.requests[0][0].prompt
    assert generated.plan.metadata["upstream_context"]["source_contract"] == "script_graph -> visual_plan -> image_generation -> character_background_removal -> artifact"


def test_visual_asset_generation_repairs_empty_upstream_context_from_current_graph(tmp_path: Path) -> None:
    class FakeImageClient:
        name = "fake"

        def generate(self, request, config):
            Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(request.output_path).write_bytes(b"fake-image")
            return ImageGenerationResponse(output_path=request.output_path, provider=config.provider, model=config.model)

    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    graph = ScriptGraphCompiler().compile(result.decomposition).model_dump()
    planning_request = VisualAssetRequest(
        script_graph=graph,
        output_root=str(tmp_path),
        max_characters=1,
        max_scenes=0,
    )
    plan = VisualAssetGenerationAgent().plan(planning_request)
    dirty_plan = plan.model_copy(
        update={
            "metadata": {
                **plan.metadata,
                "style_guide": {
                    key: value
                    for key, value in plan.metadata["style_guide"].items()
                    if key not in {"source_json_excerpt", "graph_visual_context"}
                },
                "upstream_context": {
                    "story_graph_context": "",
                    "source_contract": "script_graph -> visual_plan -> image_generation",
                },
            }
        }
    )
    request = VisualAssetRequest(plan=dirty_plan.model_dump(), script_graph=graph, output_root=str(tmp_path))
    request.provider.provider = "fake"

    generated = VisualAssetGenerationAgent(image_clients={"fake": FakeImageClient()}).generate(request)

    upstream = generated.plan.metadata["upstream_context"]
    assert upstream["source_json"]["script_graph"]["schema_version"] == "script_graph.v1"
    assert upstream["story_graph_context"]
    assert "style_guide" not in generated.plan.assets[0].metadata
    assert "upstream_context" not in generated.plan.assets[0].metadata


def test_visual_asset_generation_does_not_duplicate_global_context_per_asset(tmp_path: Path) -> None:
    class FakeImageClient:
        name = "fake"

        def generate(self, request, config):
            Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(request.output_path).write_bytes(b"fake-image")
            return ImageGenerationResponse(output_path=request.output_path, provider=config.provider, model=config.model)

    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    graph = ScriptGraphCompiler().compile(result.decomposition).model_dump()
    request = VisualAssetRequest(script_graph=graph, output_root=str(tmp_path), max_characters=1, max_scenes=1)
    request.provider.provider = "fake"

    generated = VisualAssetGenerationAgent(image_clients={"fake": FakeImageClient()}).generate(request)

    assert generated.plan.metadata["style_guide"]
    assert generated.plan.metadata["upstream_context"]["source_json"]
    for asset in generated.plan.assets:
        assert "style_guide" not in asset.metadata
        assert "upstream_context" not in asset.metadata
        assert len(json.dumps(asset.metadata, ensure_ascii=False)) < 4000


def test_visual_asset_generation_agent_consumes_loaded_plan_without_graph(tmp_path: Path) -> None:
    class FakeImageClient:
        name = "fake"

        def generate(self, request, config):
            Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(request.output_path).write_bytes(b"fake-image")
            return ImageGenerationResponse(output_path=request.output_path, provider=config.provider, model=config.model)

    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    planning_request = VisualAssetRequest(
        decomposition=result.decomposition,
        output_root=str(tmp_path),
        max_characters=1,
        max_scenes=0,
    )
    plan = VisualAssetGenerationAgent().plan(planning_request)
    loaded_request = VisualAssetRequest(plan=plan.model_dump(), output_root=str(tmp_path))
    loaded_request.provider.provider = "fake"
    loaded_request.provider.model = "current-image-model"

    generated = VisualAssetGenerationAgent(image_clients={"fake": FakeImageClient()}).generate(loaded_request)

    assert len(generated.generated) == 1
    assert generated.generated[0].model == "current-image-model"


def test_visual_asset_generation_consumes_plan_even_when_prompt_model_configured(tmp_path: Path) -> None:
    class FakeImageClient:
        name = "fake"

        def generate(self, request, config):
            Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(request.output_path).write_bytes(b"fake-image")
            return ImageGenerationResponse(output_path=request.output_path, provider=config.provider, model=config.model)

    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    planning_request = VisualAssetRequest(
        decomposition=result.decomposition,
        output_root=str(tmp_path),
        max_characters=1,
        max_scenes=0,
    )
    plan = VisualAssetGenerationAgent().plan(planning_request)
    loaded_request = VisualAssetRequest(
        plan=plan.model_dump(),
        output_root=str(tmp_path),
        prompt_model=LLMProviderConfig(provider="openai_compatible", model="visual-prompt-model"),
    )
    loaded_request.provider.provider = "fake"

    generated = VisualAssetGenerationAgent(image_clients={"fake": FakeImageClient()}).generate(loaded_request)

    assert len(generated.generated) == 1


def test_visual_asset_generation_uses_windows_safe_world_id_paths(tmp_path: Path) -> None:
    class FakeImageClient:
        name = "fake"

        def generate(self, request, config):
            Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(request.output_path).write_bytes(b"fake-image")
            return ImageGenerationResponse(output_path=request.output_path, provider=config.provider, model=config.model)

    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    request = VisualAssetRequest(
        decomposition=result.decomposition,
        output_root=str(tmp_path),
        max_characters=1,
        max_scenes=0,
    )
    plan = VisualAssetGenerationAgent().plan(request).model_copy(update={"world_id": "script:01"})
    request = VisualAssetRequest(plan=plan.model_dump(), output_root=str(tmp_path))
    request.provider.provider = "fake"

    generated = VisualAssetGenerationAgent(image_clients={"fake": FakeImageClient()}).generate(request)

    assert "script_01" in generated.generated[0].output_path
    assert ":" not in Path(generated.generated[0].output_path).parts
    assert Path(generated.generated[0].output_path).exists()


def test_visual_asset_generation_rerun_keeps_previous_outputs(tmp_path: Path) -> None:
    class FakeImageClient:
        name = "fake"

        def generate(self, request, config):
            Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(request.output_path).write_bytes(str(request.output_path).encode("utf-8"))
            return ImageGenerationResponse(output_path=request.output_path, provider=config.provider, model=config.model)

    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    request = VisualAssetRequest(
        decomposition=result.decomposition,
        output_root=str(tmp_path),
        max_characters=1,
        max_scenes=0,
    )
    request.provider.provider = "fake"
    plan = VisualAssetGenerationAgent().plan(request)
    request.plan = plan.model_dump()
    agent = VisualAssetGenerationAgent(image_clients={"fake": FakeImageClient()})

    first = agent.generate(request)
    second = agent.generate(request)
    first_path = Path(first.generated[0].output_path)
    second_path = Path(second.generated[0].output_path)

    assert first_path != second_path
    assert "runs" in first_path.parts
    assert "runs" in second_path.parts
    assert first_path.exists()
    assert second_path.exists()
    assert first.plan.metadata["generation_run_id"] != second.plan.metadata["generation_run_id"]


def test_visual_asset_generation_cancel_stops_after_current_asset(tmp_path: Path) -> None:
    class FakeImageClient:
        name = "fake"

        def __init__(self) -> None:
            self.count = 0

        def generate(self, request, config):
            self.count += 1
            Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(request.output_path).write_bytes(b"fake-image")
            return ImageGenerationResponse(output_path=request.output_path, provider=config.provider, model=config.model)

    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    request = VisualAssetRequest(
        decomposition=result.decomposition,
        output_root=str(tmp_path),
        max_characters=2,
        max_scenes=0,
    )
    request.provider.provider = "fake"
    plan = VisualAssetGenerationAgent().plan(request)
    request.plan = plan.model_dump()
    client = FakeImageClient()

    generated = VisualAssetGenerationAgent(image_clients={"fake": client}).generate(
        request,
        should_cancel=lambda: client.count >= 1,
    )

    assert client.count == 1
    assert len(generated.generated) == 1
    assert generated.metadata["cancelled"] is True
    assert generated.plan.metadata["generation_status"] == "cancelled"


def test_visual_asset_store_lists_and_deletes_generation_runs(tmp_path: Path) -> None:
    output_root = tmp_path / "output" / "visual_assets"
    run_dir = output_root / "script_01" / "runs" / "20260703_120000_abcd1234" / "characters"
    run_dir.mkdir(parents=True)
    image_path = run_dir / "char_01.png"
    image_path.write_bytes(b"fake")
    store = VisualAssetArtifactStore(root=tmp_path / "data")

    runs = store.list_runs(world_id="script:01", output_root=str(output_root))

    assert len(runs) == 1
    assert runs[0]["run_id"] == "20260703_120000_abcd1234"
    assert runs[0]["asset_count"] == 1

    loaded = store.load_run("20260703_120000_abcd1234", world_id="script:01", output_root=str(output_root))
    assert loaded["assets"][0]["output_path"].endswith("char_01.png")

    deleted = store.delete_run("20260703_120000_abcd1234", world_id="script:01", output_root=str(output_root))
    assert deleted["status"] == "deleted"
    assert not (output_root / "script_01" / "runs" / "20260703_120000_abcd1234").exists()

    with pytest.raises(ValueError):
        store.delete_run("../bad", world_id="script:01", output_root=str(output_root))


def test_visual_asset_store_load_run_returns_matching_plan_context(tmp_path: Path) -> None:
    output_root = tmp_path / "output" / "visual_assets"
    run_dir = output_root / "script_01" / "runs" / "20260704_064353_233868_7c9127d4" / "characters"
    run_dir.mkdir(parents=True)
    (run_dir / "char_01.png").write_bytes(b"fake")
    store = VisualAssetArtifactStore(root=tmp_path / "data")
    plan = VisualAssetGenerationAgent().plan(
        VisualAssetRequest(
            script_graph=ScriptGraphCompiler().compile(build_script_world(make_script_case()).decomposition).model_dump(),
            output_root=str(output_root),
            max_characters=1,
            max_scenes=0,
        )
    )
    plan = plan.model_copy(update={"plan_id": "script:01_visual_assets", "world_id": "script:01", "title": "章节一"})
    store.save_plan(plan)

    loaded = store.load_run(
        "20260704_064353_233868_7c9127d4",
        world_id="script:01",
        title="章节一",
        output_root=str(output_root),
    )

    assert loaded["visual_plan"]["world_id"] == "script:01"
    assert loaded["visual_plan_artifact"]["artifact_id"] == "script_01_visual_assets"
    source_json = loaded["visual_plan"]["metadata"]["upstream_context"]["source_json"]
    assert source_json["script_graph"]["schema_version"] == "script_graph.v1"


@pytest.mark.asyncio
async def test_visual_prompt_composer_agent_can_write_prompts_from_script_json(tmp_path: Path) -> None:
    class FakeTextClient:
        def __init__(self) -> None:
            self.user_prompt = ""

        async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
            self.user_prompt = user_prompt
            return json.dumps(
                {
                    "style_guide": {
                        "visual_bible": "agent-authored style from script_json",
                        "render_style": "consistent painterly game assets",
                        "palette": "script-derived muted colors",
                        "material_language": "script-derived fabric and wood",
                        "continuity": "same game asset batch",
                        "blocked_prompt_terms": ["林伯", "书房"],
                    },
                    "assets": [
                        {
                            "id": "character:butler",
                            "prompt": "agent-authored butler portrait, exactly one character only, no text",
                            "negative_prompt": "multiple people",
                            "warnings": [],
                        },
                        {
                            "id": "scene:书房",
                            "prompt": "agent-authored empty study environment, no people, no characters",
                            "negative_prompt": "people, character",
                            "warnings": [],
                        },
                    ],
                },
                ensure_ascii=False,
            )

    fake = FakeTextClient()
    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    request = VisualAssetRequest(
        decomposition=result.decomposition,
        output_root=str(tmp_path),
        max_characters=1,
        max_scenes=1,
        prompt_composer="llm",
    )

    plan = await VisualAssetGenerationAgent(prompt_composer=VisualPromptComposerAgent(text_client=fake)).plan_async(request)

    assert "script_json" in fake.user_prompt
    assert plan.metadata["style_guide"]["visual_bible"] == "agent-authored style from script_json"
    assert "LOCKED BATCH STYLE" in plan.assets[0].prompt
    assert "agent-authored style from script_json" in plan.assets[0].prompt
    assert "agent-authored butler portrait" in plan.assets[0].prompt
    assert "林伯" not in plan.assets[0].prompt
    assert "LOCKED BATCH STYLE" in plan.assets[1].prompt
    assert "agent-authored empty study environment" in plan.assets[1].prompt


@pytest.mark.asyncio
async def test_visual_asset_generation_llm_finalizes_prompts_from_consumable_plan(tmp_path: Path) -> None:
    class FakeTextClient:
        def __init__(self) -> None:
            self.user_prompt = ""

        async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
            self.user_prompt = user_prompt
            payload = json.loads(user_prompt)
            asset_id = payload["visual_plan"]["assets"][0]["id"]
            return json.dumps(
                {
                    "style_guide": {
                        "visual_bible": "llm-decided coherent rural xianxia visual bible",
                        "style_anchor": "llm pure style anchor",
                        "character_style_context": "weathered linen work clothes, tired posture, plain portrait light",
                        "scene_style_context": "empty rural mountain village environments",
                    },
                    "assets": [
                        {
                            "id": asset_id,
                            "prompt": "LLM FINAL: one isolated elder brother portrait, plain neutral background, worn apprentice work clothes, no buildings",
                            "negative_prompt": "huts, cottages, village background",
                            "warnings": [],
                        }
                    ],
                },
                ensure_ascii=False,
            )

    class FakeImageClient:
        name = "fake"

        def __init__(self) -> None:
            self.requests = []

        def generate(self, request, config):
            self.requests.append(request)
            Path(request.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(request.output_path).write_bytes(b"fake-image")
            return ImageGenerationResponse(output_path=request.output_path, provider=config.provider, model=config.model)

    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    planning_request = VisualAssetRequest(decomposition=result.decomposition, output_root=str(tmp_path), max_characters=1, max_scenes=0)
    plan = VisualAssetGenerationAgent().plan(planning_request)
    request = VisualAssetRequest(
        plan=plan.model_dump(),
        output_root=str(tmp_path),
        prompt_model=LLMProviderConfig(provider="openai_compatible", model="visual-finalizer"),
    )
    request.provider.provider = "fake"
    image_client = FakeImageClient()
    text_client = FakeTextClient()
    progress_events: list[tuple[str, str, str]] = []

    async def progress(status: str, title: str, detail: str) -> None:
        progress_events.append((status, title, detail))

    generated = await VisualAssetGenerationAgent(
        image_clients={"fake": image_client},
        prompt_composer=VisualPromptComposerAgent(text_client=text_client),
    ).generate_async(request, progress_callback=progress)

    assert "visual_plan" in text_client.user_prompt
    assert generated.plan.metadata["final_prompt_composed_by"] == "visual_asset_generation_llm"
    assert image_client.requests[0].prompt.startswith("LLM FINAL")
    assert "chroma-key magenta" in image_client.requests[0].prompt
    assert "clean unbroken facial features" in image_client.requests[0].prompt
    assert "extra face" in image_client.requests[0].negative_prompt
    assert "empty rural mountain village environments" not in image_client.requests[0].prompt
    event_details = "\n".join(detail for _, _, detail in progress_events)
    assert "已载入视觉方案" in event_details
    assert "交给视觉提示词 Agent" in event_details
    assert "LLM final prompts ready" in event_details
    assert "视觉生成任务" in event_details
    assert "正在生成资产 1/1" in event_details
    assert "资产 1/1 已完成" in event_details


def test_story_graph_scene_records_prioritize_distinct_scene_roots_over_dialogue_beats() -> None:
    from app.worlds.sandbox.visual_assets import _story_graph_scene_records

    records = _story_graph_scene_records(
        {
            "script_graph": {
                "nodes": [
                    {"id": "scene:start", "kind": "scene", "label": "青灯台值夜", "properties": {}},
                    {"id": "scene:start_beat_1", "kind": "scene", "label": "青灯台值夜 · 1", "properties": {}},
                    {"id": "scene:scene_second", "kind": "scene", "label": "前世幻境", "properties": {}},
                ],
                "edges": [],
            }
        }
    )

    assert [record["name"] for record in records[:2]] == ["青灯台值夜", "前世幻境"]


@pytest.mark.asyncio
async def test_visual_asset_generation_cancel_endpoint_cancels_running_task() -> None:
    class FakeTask:
        def __init__(self) -> None:
            self.cancelled = False

        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            self.cancelled = True

    job_id = "test_visual_cancel"
    task = FakeTask()
    api_routes.visual_asset_generation_jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "events": [],
        "result": None,
        "error": None,
        "cancel_requested": False,
        "created_at": "",
        "updated_at": "",
    }
    api_routes.visual_asset_generation_tasks[job_id] = task
    try:
        job = await api_routes.cancel_visual_asset_generation_job(job_id)
    finally:
        api_routes.visual_asset_generation_jobs.pop(job_id, None)
        api_routes.visual_asset_generation_tasks.pop(job_id, None)

    assert job["status"] == "cancelling"
    assert job["cancel_requested"] is True
    assert task.cancelled is True


@pytest.mark.asyncio
async def test_visual_asset_generation_finalizer_failure_stops_without_fallback(tmp_path: Path) -> None:
    class FailingTextClient:
        async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
            raise RuntimeError("Upstream request failed")

    class FakeImageClient:
        name = "fake"

        def __init__(self) -> None:
            self.requests = []

        def generate(self, request, config):
            self.requests.append(request)
            return ImageGenerationResponse(output_path=request.output_path, provider=config.provider, model=config.model)

    result = build_script_world(make_script_case())
    assert result.decomposition is not None
    plan = VisualAssetGenerationAgent().plan(
        VisualAssetRequest(decomposition=result.decomposition, output_root=str(tmp_path), max_characters=1, max_scenes=0)
    )
    image_client = FakeImageClient()
    request = VisualAssetRequest(
        plan=plan.model_dump(),
        output_root=str(tmp_path),
        prompt_model=LLMProviderConfig(provider="openai_compatible", model="visual-finalizer"),
    )
    request.provider.provider = "fake"

    with pytest.raises(RuntimeError, match="Upstream request failed"):
        await VisualAssetGenerationAgent(
            image_clients={"fake": image_client},
            prompt_composer=VisualPromptComposerAgent(text_client=FailingTextClient()),
        ).generate_async(request)

    assert image_client.requests == []


def test_world_builder_routes_script_decomposition_template_to_specialized_agent() -> None:
    config = generate_world_config(
        WorldGenerateRequest(
            template="script_decomposition",
            player_name="侦探",
            script_decomposition=make_script_case().model_dump(),
        )
    )

    assert config.metadata["generated_by"] == "script_decomposition_agent"
    assert config.metadata["script_case"]["truth"].startswith("管家林伯")
    assert config.name == "锁门后的钟声"


def test_world_builder_and_npc_runtime_use_script_graph_document() -> None:
    decomposition = build_script_world(make_script_case()).decomposition
    graph = ScriptGraphCompiler().compile(decomposition).model_dump()

    config = generate_world_config(
        WorldGenerateRequest(
            template="script_graph",
            player_name="侦探",
            script_graph=graph,
        )
    )
    adapter = SandboxWorldAdapter(config)
    state = adapter.create_initial_state()
    prompt = adapter.build_system_prompt(
        state,
        ChatRequest(message="我应该先问谁？", player_name="侦探", location=config.player["location"]),
    )

    assert config.metadata["script_graph"]["schema_version"] == "script_graph.v1"
    assert config.metadata["generated_by"] == "script_graph_world_builder"
    assert config.metadata["script_graph_input_source"] == "workbench_script_graph"
    assert state.world_state["script_graph"]["graph_id"] == graph["graph_id"]
    assert state.world_state["lorebook"]["schema_version"] == "npc_lorebook.v1"
    assert state.world_state["lorebook_review"]["reviewer"] == "NpcLorebookReviewAgent"
    assert state.world_state["lorebook_review"]["passed"] is True
    assert "当前可用背景" in prompt
    assert "当前激活世界书" not in prompt
    assert "ScriptGraphDocument / story graph context" not in prompt
    assert "ScriptGraphDocument" not in prompt
    assert "script_graph" not in prompt


def test_world_builder_outputs_story_based_npc_locations() -> None:
    graph = {
        "graph_id": "multi_location_graph",
        "title": "多地点 NPC 测试",
        "schema_version": "script_graph.v1",
        "nodes": [
            {"id": "npc_blacksmith_son", "kind": "character", "label": "铁匠的儿子", "properties": {"role": "富家子弟"}},
            {"id": "town", "kind": "location", "label": "青牛镇", "properties": {}},
            {"id": "guest_yard", "kind": "location", "label": "清客院", "properties": {}},
        ],
        "edges": [
            {"source": "npc_blacksmith_son", "target": "town", "type": "LOCATED_AT"},
            {"source": "npc_blacksmith_son", "target": "guest_yard", "type": "LOCATED_AT"},
        ],
    }

    config = generate_world_config(WorldGenerateRequest(template="script_graph", script_graph=graph, player_name="玩家"))
    npc = config.npcs[0]
    assert npc.location == "青牛镇"
    assert npc.locations == ["青牛镇", "清客院"]

    adapter = SandboxWorldAdapter(config)
    state = adapter.create_initial_state()
    state.world_state["player"]["location"] = "清客院"
    assert [item["name"] for item in adapter._nearby_npcs(state)] == ["铁匠的儿子"]
    prompt = adapter.build_system_prompt(state, ChatRequest(message="人在吗？", player_name="玩家", location="清客院"))
    assert "在场 NPC 只有：铁匠的儿子" in prompt


@pytest.mark.asyncio
async def test_world_builder_attaches_lorebook_artifact_before_npc_runtime() -> None:
    decomposition = build_script_world(make_script_case()).decomposition
    graph = ScriptGraphCompiler().compile(decomposition).model_dump()

    class FakeTextClient:
        async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
            return json.dumps(NpcLorebookCompiler().compile(make_world()).model_dump(), ensure_ascii=False)

    from app.worlds.sandbox import generator

    original_agent = generator.NpcLorebookCreationAgent

    class FakeLorebookAgent:
        def __init__(self) -> None:
            self.agent = NpcLorebookCreationAgent(text_client=FakeTextClient())

        async def create(self, world, llm_config=None, *, strict=False):
            return await self.agent.create(world, llm_config, strict=strict)

    generator.NpcLorebookCreationAgent = FakeLorebookAgent
    try:
        config = await WorldBuilderAgent().generate(
            WorldGenerateRequest(
                template="script_graph",
                player_name="侦探",
                script_graph=graph,
            )
        )
    finally:
        generator.NpcLorebookCreationAgent = original_agent

    assert config.metadata["npc_lorebook"]["schema_version"] == "npc_lorebook.v1"
    assert config.metadata["npc_lorebook_generation"]["agent"] == "NpcLorebookCreationAgent"
    assert config.metadata["npc_lorebook_generation"]["entry_count"] > 0
    assert config.metadata["playtest_review"]["metadata"]["runtime_artifacts"]["lorebook_source"] == "metadata.npc_lorebook"
    adapter = SandboxWorldAdapter(config)
    assert adapter.lorebook.artifact_id == config.metadata["npc_lorebook"]["artifact_id"]


@pytest.mark.asyncio
async def test_npc_runtime_hides_developer_graph_terms_from_player_facing_dialogue(tmp_path: Path) -> None:
    class LeakyLLM:
        def __init__(self) -> None:
            self.system_prompt = ""
            self.human_prompt = ""
            self.calls = 0

        async def invoke(self, messages, fallback_actions):
            self.calls += 1
            self.system_prompt = messages[0].content
            self.human_prompt = messages[1].content
            content = "我会帮你核对图谱和世界树。" if self.calls == 1 else "我会帮你核对已有的线索记录和传闻脉络。"
            return AgentLLMOutput(
                action_type="say",
                content=content,
                inner_thought="developer graph terms are internal.",
                command={"name": "none", "args": {}},
                suggested_actions=fallback_actions,
            )

    runtime = AgentRuntime(
        SandboxWorldAdapter(make_world()),
        llm_client=LeakyLLM(),
        session_store=RuntimeSessionStore(tmp_path / "sessions"),
    )

    response = await runtime.chat(
        ChatRequest(
            message="帮我核对图谱和世界树里的 JSON。",
            player_name="玩家",
            location="起点",
            player_goal="检查图谱 世界树 JSON 是否正确",
            target_npc_id="mentor",
        )
    )

    assert "玩家世界内意图：检查线索记录 传闻脉络 记录 是否正确" in runtime.llm.system_prompt
    assert "玩家输入：帮我核对线索记录和传闻脉络里的 记录。" in runtime.llm.human_prompt
    assert "图谱" not in runtime.llm.human_prompt
    assert "世界树" not in runtime.llm.human_prompt
    assert "JSON" not in runtime.llm.human_prompt
    assert "图谱" not in response.reply
    assert "世界树" not in response.reply
    assert "JSON" not in response.reply
    assert "线索记录" in response.reply
    assert runtime.llm.calls == 2
    assert response.debug_trace["llm"]["conversation_review"]["passed"] is True
    assert "图谱" not in response.messages[0].content
    assert "NpcLorebookReviewAgent" in json.dumps(response.debug_trace.get("lorebook", {}), ensure_ascii=False)
    assert "ScriptGraphDocument" not in json.dumps(response.speaker, ensure_ascii=False)
    assert "ScriptGraphDocument" not in json.dumps(response.debug_trace.get("npc_session", {}), ensure_ascii=False)


def test_npc_lorebook_compiler_projects_world_facing_entries() -> None:
    world = make_world()
    world.npcs[0].goals = ["只依据 ScriptGraphDocument 中的节点和关系回答。", "根据玩家已发现的信息逐步回应，不主动发明图谱和世界树外事实。"]
    adapter = SandboxWorldAdapter(world)
    entries = adapter.lorebook.entries
    text = json.dumps([entry.model_dump() for entry in entries], ensure_ascii=False)

    assert adapter.lorebook.schema_version == "npc_lorebook.v1"
    assert any(entry.strategy == "constant" for entry in entries)
    assert any("师父" in entry.keywords for entry in entries)
    assert {"world", "character", "location", "task"}.issubset({entry.entry_type for entry in entries})
    assert "ScriptGraphDocument" not in text
    assert "图谱" not in text
    assert "世界树" not in text
    assert "可靠传闻" in text or "未知事实" in text


def test_npc_lorebook_runtime_applies_lorebook_activation_rules() -> None:
    artifact = NpcLorebookArtifact(
        artifact_id="rules.npc_lorebook",
        world_id="rules",
        title="规则世界书",
        entries=[
            NpcLorebookEntry(id="constant", title="世界宪法", content="恒定背景", strategy="constant", priority=9999),
            NpcLorebookEntry(id="normal_recent", title="青云宗", content="最近提到才激活", keywords=["青云宗"], strategy="normal", scan_depth=2, priority=700),
            NpcLorebookEntry(id="normal_old", title="魔法塔", content="太旧不激活", keywords=["魔法塔"], strategy="normal", scan_depth=1, priority=700),
            NpcLorebookEntry(id="selective_old", title="寒月剑", content="全历史可激活", keywords=["寒月剑"], strategy="selective", priority=650),
            NpcLorebookEntry(id="regex", title="第几章", content="正则激活", regex_keywords=[r"第[一二三四五六七八九十]+章"], strategy="normal", priority=600),
            NpcLorebookEntry(id="disabled", title="禁用", content="不应该出现", keywords=["青云宗"], strategy="disabled", priority=9999),
            NpcLorebookEntry(id="chain_source", title="连锁源", content="玄真子掌管戒律堂。", keywords=["掌门"], strategy="normal", chain=True, priority=800),
            NpcLorebookEntry(id="chain_target", title="戒律堂", content="被连锁激活", keywords=["戒律堂"], strategy="normal", priority=500),
        ],
    )

    entries = NpcLorebookRuntime(artifact, max_entries=8, token_budget=1600).activate(
        message="第七章里我去问掌门。",
        conversation="很早以前提到魔法塔。\n后来获得寒月剑。\n刚刚抵达青云宗。",
    )
    ids = [entry.id for entry in entries]

    assert "constant" in ids
    assert "normal_recent" in ids
    assert "normal_old" not in ids
    assert "selective_old" in ids
    assert "regex" in ids
    assert "disabled" not in ids
    assert ids.index("chain_source") < ids.index("chain_target")


def test_npc_lorebook_runtime_respects_priority_and_token_budget() -> None:
    artifact = NpcLorebookArtifact(
        artifact_id="budget.npc_lorebook",
        world_id="budget",
        entries=[
            NpcLorebookEntry(id="low", title="低", content="低优先级", keywords=["钥匙"], strategy="normal", priority=100, token_budget=90),
            NpcLorebookEntry(id="high", title="高", content="高优先级", keywords=["钥匙"], strategy="normal", priority=900, token_budget=90),
        ],
    )

    entries = NpcLorebookRuntime(artifact, max_entries=8, token_budget=90).activate(message="钥匙在哪里？")

    assert [entry.id for entry in entries] == ["high"]


def test_npc_lorebook_compiler_builds_long_memory_entries() -> None:
    compiler = NpcLorebookCompiler()
    summary = compiler.build_summary_entry(
        summary_id="chapter_1_0_150",
        title="第一章总结（0-150 楼）",
        content="玩家获得寒月剑，并承诺回到青云宗复命。",
        floor_range="0-150",
    )
    table = compiler.build_memory_table_entry(
        table_id="inventory",
        title="物品清单",
        rows=[{"物品": "寒月剑", "数量": 1, "来源": "师姐所赠"}],
        keywords=["寒月剑", "物品"],
    )

    assert summary.entry_type == "summary"
    assert summary.strategy == "constant"
    assert summary.position == "system"
    assert summary.metadata["memory_role"] == "long_chat_summary"
    assert table.entry_type == "table"
    assert table.strategy == "selective"
    assert "寒月剑" in table.keywords


def test_npc_lorebook_review_checks_lorebook_rule_quality() -> None:
    artifact = NpcLorebookArtifact(
        artifact_id="bad.npc_lorebook",
        world_id="bad",
        entries=[
            NpcLorebookEntry(id=f"constant_{index}", title=f"常驻{index}", content="核心设定", strategy="constant", keywords=["多余"])
            for index in range(4)
        ]
        + [
            NpcLorebookEntry(id="bad_regex", title="坏正则", content="内容", regex_keywords=["["], strategy="normal"),
            NpcLorebookEntry(id="too_deep", title="深扫", content="内容", keywords=["深扫"], strategy="normal", scan_depth=20),
            NpcLorebookEntry(id="too_big", title="大预算", content="内容", keywords=["大"], strategy="normal", token_budget=900),
        ],
    )

    report = NpcLorebookReviewAgent().review(artifact)
    text = json.dumps([issue.model_dump() for issue in report.issues], ensure_ascii=False)

    assert report.passed is False
    assert "常驻条目超过 3 条" in text
    assert "常驻条目不需要关键词" in text
    assert "正则关键词无法编译" in text
    assert "扫描深度过大" in text
    assert "预算过高" in text


def test_npc_lorebook_review_agent_rejects_developer_concepts() -> None:
    world = make_world()
    world.description = "这是图谱和世界树测试。"
    artifact = NpcLorebookCompiler().compile(world)
    clean_report = NpcLorebookReviewAgent().review(artifact)

    artifact.entries[0].content = "请读取 ScriptGraphDocument JSON 节点。"
    report = NpcLorebookReviewAgent().review(artifact)

    assert clean_report.passed is True
    assert report.passed is False
    assert any("开发者/数据结构概念" in issue.message for issue in report.issues)


@pytest.mark.asyncio
async def test_npc_lorebook_creation_agent_authors_entries_with_ai() -> None:
    class FakeTextClient:
        def __init__(self) -> None:
            self.system_prompt = ""
            self.user_prompt = ""

        async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
            self.system_prompt = system_prompt
            self.user_prompt = user_prompt
            return json.dumps(
                {
                    "artifact_id": "test_world.npc_lorebook",
                    "world_id": "test_world",
                    "title": "测试世界 NPC 世界书",
                    "schema_version": "npc_lorebook.v1",
                    "entries": [
                        {
                            "id": "ai:mentor_boundaries",
                            "title": "师父的对话边界",
                            "content": "师父知道试炼令牌的领取方式，会提醒玩家先在起点确认任务，再领取令牌并复命。",
                            "entry_type": "character",
                            "keywords": ["师父", "试炼令牌", "复命"],
                            "strategy": "normal",
                            "priority": 850,
                            "npc_ids": ["mentor"],
                            "locations": ["起点"],
                            "source_refs": ["world.npcs.mentor"],
                        }
                    ],
                    "metadata": {"created_by": "NpcLorebookCreationAgent"},
                },
                ensure_ascii=False,
            )

    agent = NpcLorebookCreationAgent(text_client=FakeTextClient())
    artifact = await agent.create(make_world())
    text = json.dumps(artifact.model_dump(), ensure_ascii=False)

    assert artifact.metadata["created_by"] == "NpcLorebookCreationAgent"
    assert artifact.entries[0].id == "ai:mentor_boundaries"
    assert artifact.entries[0].entry_type == "character"
    assert "师父的对话边界" in text
    assert "图谱" not in text
    assert "ScriptGraphDocument" not in text


def test_npc_lorebook_creation_prompt_compacts_large_visual_metadata() -> None:
    world = make_world()
    repeated_context = {"source_json": {"script_graph": {"nodes": [{"id": str(index), "label": "长节点" * 100} for index in range(100)]}}}
    heavy_asset_metadata = {
        "style_guide": {"visual_bible": "古风材质" * 2000},
        "upstream_context": repeated_context,
    }
    assets = [
        {
            "id": f"character_{index}",
            "kind": "character",
            "display_name": f"角色{index}",
            "source_id": f"npc_{index}",
            "source_name": f"角色{index}",
            "output_path": f"characters/{index}.png",
            "status": "generated",
            "prompt": "prompt" * 500,
            "metadata": heavy_asset_metadata,
        }
        for index in range(20)
    ]
    world.metadata = {
        "script_graph": {
            "nodes": [{"id": f"node_{index}", "kind": "character", "label": f"节点{index}", "properties": {"description": "描述" * 200}} for index in range(120)],
            "edges": [{"source": "node_1", "target": "node_2", "type": "RELATED"} for _ in range(200)],
        },
        "visual_plan": {
            "plan_id": "large_plan",
            "world_id": "test_world",
            "title": "大视觉计划",
            "assets": assets,
            "metadata": {"style_guide": heavy_asset_metadata["style_guide"], "upstream_context": repeated_context},
        },
        "visual_result": {"generated": assets, "failed": [], "metadata": {"generation_run_id": "run_large"}},
    }

    prompt = NpcLorebookCreationAgent()._user_prompt(world, NpcLorebookCompiler().compile(world))

    assert len(prompt) < 80000
    assert "style_guide" not in prompt
    assert "upstream_context" not in prompt
    assert "characters/0.png" in prompt


@pytest.mark.asyncio
async def test_npc_lorebook_creation_strict_mode_fails_instead_of_fallback() -> None:
    agent = NpcLorebookCreationAgent()

    with pytest.raises(NpcLorebookCreationError):
        await agent.create(make_world(), strict=True)


def test_world_lorebook_includes_visual_assets_beyond_npc_portraits() -> None:
    world = make_world()
    world.metadata = {
        "visual_result": {
            "generated": [
                {"id": "character_mentor", "kind": "character", "display_name": "师父", "output_path": "characters/mentor.png"},
                {"id": "scene_start", "kind": "scene", "display_name": "起点", "output_path": "scenes/start.png"},
                {"id": "item_token", "kind": "item", "display_name": "令牌", "output_path": "items/token.png"},
                {"id": "clue_rule", "kind": "clue", "display_name": "复命线索", "output_path": "clues/rule.png"},
            ]
        }
    }

    artifact = NpcLorebookCompiler().compile(world)
    entry_types = {entry.entry_type for entry in artifact.entries}

    assert {"character", "scene", "item", "clue"}.issubset(entry_types)


def test_world_builder_attaches_visual_plan_when_available() -> None:
    decomposition = build_script_world(make_script_case()).decomposition
    graph = ScriptGraphCompiler().compile(decomposition).model_dump()
    visual_plan = {
        "plan_id": "locked_room_visual_assets",
        "world_id": graph["graph_id"],
        "title": graph["title"],
        "assets": [
            {
                "id": "character_butler",
                "kind": "character",
                "display_name": "林伯",
                "source_id": "butler",
                "source_name": "林伯",
                "output_path": "output/visual_assets/locked_room/characters/butler.png",
                "status": "generated",
            }
        ],
    }

    config = generate_world_config(
        WorldGenerateRequest(
            template="script_graph",
            player_name="侦探",
            script_graph=graph,
            visual_plan=visual_plan,
        )
    )

    assert config.metadata["visual_plan"]["plan_id"] == "locked_room_visual_assets"
    assert "VisualAsset input" in config.metadata["visual_asset_summary"]
    assert "butler.png" in config.metadata["visual_asset_summary"]


def test_world_builder_attaches_visual_generation_result_when_available() -> None:
    decomposition = build_script_world(make_script_case()).decomposition
    graph = ScriptGraphCompiler().compile(decomposition).model_dump()
    visual_plan = {
        "plan_id": "locked_room_visual_assets",
        "world_id": graph["graph_id"],
        "title": graph["title"],
        "assets": [
            {
                "id": "character_butler",
                "kind": "character",
                "display_name": "林伯",
                "source_id": "butler",
                "source_name": "林伯",
                "output_path": "output/visual_assets/locked_room/characters/butler_planned.png",
                "status": "planned",
            }
        ],
    }
    visual_result = {
        "plan": visual_plan,
        "generated": [
            {
                **visual_plan["assets"][0],
                "output_path": "output/visual_assets/locked_room/characters/butler_generated.png",
                "status": "generated",
            }
        ],
        "failed": [],
        "metadata": {"generation_run_id": "run_01"},
    }

    config = generate_world_config(
        WorldGenerateRequest(
            template="script_graph",
            player_name="侦探",
            script_graph=graph,
            visual_plan=visual_plan,
            visual_result=visual_result,
        )
    )

    assert config.metadata["visual_result"]["metadata"]["generation_run_id"] == "run_01"
    assert "generated_count" in config.metadata["visual_asset_summary"]
    assert "butler_generated.png" in config.metadata["visual_asset_summary"]
    matched_npc = next(npc for npc in config.npcs if npc.name == "林伯")
    assert matched_npc.portrait["output_path"].endswith("butler_generated.png")
    assert config.metadata["npc_portraits"][matched_npc.id]["asset_id"] == "character_butler"


def test_world_store_backfills_npc_portraits_from_visual_plan(tmp_path: Path) -> None:
    world = make_world()
    world.metadata = {
        "visual_plan": {
            "plan_id": "portrait_plan",
            "assets": [
                {
                    "id": "character_mentor",
                    "kind": "character",
                    "display_name": "师父",
                    "source_id": "mentor",
                    "source_name": "师父",
                    "output_path": "output/visual_assets/test_world/characters/mentor.png",
                    "status": "planned",
                }
            ],
        }
    }
    from app.worlds.sandbox.store import SandboxWorldStore

    store = SandboxWorldStore(tmp_path / "worlds")
    store.save(world)
    loaded = store.load("test_world")

    mentor = next(npc for npc in loaded.npcs if npc.id == "mentor")
    assert mentor.portrait["asset_id"] == "character_mentor"
    assert loaded.metadata["npc_portraits"]["mentor"]["output_path"].endswith("mentor.png")


def test_visual_asset_store_persists_generation_result(tmp_path: Path) -> None:
    store = VisualAssetArtifactStore(tmp_path / "visual_assets")
    plan = VisualAssetPlan(
        plan_id="test_visual_plan",
        world_id="test_world",
        title="测试视觉计划",
        assets=[
            VisualAssetSpec(
                id="character_mentor",
                kind="character",
                display_name="师父",
                prompt="portrait",
                output_path="output/visual_assets/test_world/characters/mentor.png",
                source_id="mentor",
                source_name="师父",
            )
        ],
    )
    result = VisualAssetGenerationResult(
        plan=plan,
        generated=[plan.assets[0].model_copy(update={"status": "generated"})],
        failed=[],
        metadata={"generation_run_id": "run_01"},
    )

    artifact = store.save_result(result)
    loaded = store.load(artifact["artifact_id"])

    assert loaded["artifact"]["generated_count"] == 1
    assert loaded["result"]["generated"][0]["source_id"] == "mentor"


@pytest.mark.asyncio
async def test_npc_runtime_response_includes_bound_portrait(tmp_path: Path) -> None:
    class FakeLLM:
        async def invoke(self, messages, fallback_actions):
            return AgentLLMOutput(
                action_type="say",
                content="我在。",
                inner_thought="reply with portrait",
                command={"name": "none", "args": {}},
                suggested_actions=fallback_actions,
            )

    world = make_world()
    world.npcs[0].portrait = {
        "asset_id": "mentor_portrait",
        "output_path": "output/visual_assets/test_world/characters/mentor.png",
        "url": "output/visual_assets/test_world/characters/mentor.png",
        "kind": "character",
        "status": "generated",
    }
    runtime = AgentRuntime(
        SandboxWorldAdapter(world),
        llm_client=FakeLLM(),
        session_store=RuntimeSessionStore(tmp_path / "sessions"),
    )

    response = await runtime.chat(ChatRequest(message="师父在吗？", player_name="玩家", location="起点", target_npc_id="mentor"))

    assert response.speaker["portrait"]["asset_id"] == "mentor_portrait"
    mentor = next(npc for npc in response.npcs if npc["id"] == "mentor")
    assert mentor["portrait"]["output_path"].endswith("mentor.png")


def test_world_validator_repairs_playable_loop_locations() -> None:
    config = SandboxWorldConfig(
        world_id="broken_loop",
        name="断链世界",
        player={"name": "玩家", "location": "练习室"},
        npcs=[SandboxNPC(id="coach", name="李教练", location="综合练习室")],
        tasks=[SandboxTask(id="train", title="找教练特训")],
        actions=[
            SandboxAction(
                id="train_with_coach",
                label="找李教练特训",
                effect={"complete_task": "train", "active_npc_id": "coach", "scene": "开始特训"},
            )
        ],
    )

    repaired = SandboxWorldValidator().ensure_valid(config)

    assert repaired.npcs[0].id == "guide"
    assert repaired.npcs[0].location == "练习室"
    assert repaired.npcs[1].location == "综合练习室"
    assert repaired.actions[0].effect["set_player"]["location"] == "综合练习室"
    assert repaired.metadata["playable_loop_repaired"] is True
    assert repaired.metadata["validation"]["valid"] is True


def test_skill_completion_condition_finishes_task_after_training_action() -> None:
    config = SandboxWorldConfig(
        world_id="dance_world",
        name="舞蹈世界",
        player={"name": "草莓包饭", "location": "舞蹈室", "skills": {"dance": 0}},
        npcs=[SandboxNPC(id="coach", name="陈前辈", location="舞蹈室")],
        tasks=[
            SandboxTask(
                id="practice_dance",
                title="练习舞蹈",
                completion={"stats": {"skills.dance": {"min": 10}}},
            )
        ],
        actions=[
            SandboxAction(
                id="practice",
                label="进入练习",
                effect={
                    "scene": "你完成了一轮基础舞步训练。",
                    "increase_player": {"skills.dance": 10},
                    "active_npc_id": "coach",
                },
            )
        ],
    )

    adapter = SandboxWorldAdapter(config)
    state = adapter.create_initial_state()
    response = adapter.handle_world_action(state, WorldActionRequest(action="practice", payload={}))

    assert response.player["skills"]["dance"] == 10
    assert response.state["tasks"][0]["status"] == "done"
    assert "任务完成" in response.quest_progress


def test_npc_guidance_reply_does_not_complete_keyword_task() -> None:
    config = SandboxWorldConfig(
        world_id="keyword_world",
        name="关键词任务世界",
        player={"name": "草莓包饭", "location": "训练室"},
        npcs=[SandboxNPC(id="coach", name="支线联系人", location="训练室")],
        tasks=[
            SandboxTask(
                id="prepare_stage",
                title="完成舞台准备",
                completion={"keywords": ["练习心得"]},
            )
        ],
    )

    adapter = SandboxWorldAdapter(config)
    state = adapter.create_initial_state()

    adapter.record_player_message(
        state,
        ChatRequest(message="我该如何准备？你要我做什么", location="训练室", player_name="草莓包饭"),
    )
    guidance = AgentLLMOutput(
        content="你需要完成基础训练，整理练习心得，再回来告诉我。",
        inner_thought="给出下一步，不推进任务。",
        command={"name": "none", "args": {}},
    )
    StateValidatorAgent().apply(adapter, state, guidance)

    assert state.world_state["tasks"][0].get("status", "pending") != "done"

    adapter.record_player_message(
        state,
        ChatRequest(message="好的，我已经完成训练，也整理了练习心得。", location="训练室", player_name="草莓包饭"),
    )

    assert state.world_state["tasks"][0]["status"] == "done"


def test_complete_task_command_cannot_bypass_unsatisfied_completion() -> None:
    config = SandboxWorldConfig(
        world_id="guarded_completion_world",
        name="受保护任务世界",
        player={"name": "草莓包饭", "location": "训练室"},
        npcs=[SandboxNPC(id="coach", name="支线联系人", location="训练室")],
        tasks=[
            SandboxTask(
                id="prepare_stage",
                title="完成舞台准备",
                completion={"keywords": ["练习心得"]},
            )
        ],
    )

    adapter = SandboxWorldAdapter(config)
    state = adapter.create_initial_state()
    output = AgentLLMOutput(
        content="恭喜你完成了准备。",
        inner_thought="错误地尝试直接完成。",
        command={"name": "complete_task", "args": {"task_id": "prepare_stage"}},
    )

    StateValidatorAgent().apply(adapter, state, output)

    assert state.world_state["tasks"][0].get("status", "pending") != "done"


def test_generation_payload_normalizer_repairs_first_call_schema_drift() -> None:
    payload = {
        "world": {
            "player": {"name": "草莓包饭", "location": "练习室"},
            "npcs": [{"id": "coach", "name": "陈前辈", "goals": "帮助新人练舞"}],
            "tasks": [
                {
                    "id": "practice_dance",
                    "description": "完成舞蹈练习",
                    "completion": {"type": "stat_check", "conditions": {"skills.dance": ">=", "value": 30}},
                }
            ],
            "actions": [{"id": "practice", "effect": {"scene": "练习完成"}}],
        }
    }

    normalized = _normalize_generation_payload(payload)

    assert normalized["world"]["tasks"][0]["title"] == "完成舞蹈练习"
    assert normalized["world"]["tasks"][0]["completion"] == {"stats": {"skills.dance": {"min": 30}}}
    assert normalized["world"]["npcs"][0]["goals"] == ["帮助新人练舞"]


def test_world_generation_protocol_tool_repairs_schema_drift() -> None:
    payload = {
        "world": {
            "player": {"name": "草莓包饭", "location": "练习室"},
            "npcs": [{"id": "coach", "name": "陈前辈", "goals": "帮助新人练舞"}],
            "tasks": [{"id": "practice_dance", "completion": {"type": "stat_check", "field": "skills.dance", "value": 30}}],
            "actions": [{"id": "practice", "effect": {"scene": "练习完成"}}],
        }
    }

    response = WorldGenerationProtocolTool().repair_world_generation(payload)

    assert response.world.tasks[0].completion == {"stats": {"skills.dance": {"min": 30}}}
    assert response.world.npcs[0].goals == ["帮助新人练舞"]


def test_world_generation_protocol_tool_normalizes_ai_dialect_at_boundary() -> None:
    payload = {
        "world": {
            "player": {"name": "草莓包饭", "location": "练习室"},
            "npcs": [{"id": "coach", "name": "陈前辈"}],
            "tasks": [
                {
                    "id": "prepare_show",
                    "completion": {
                        "mode": "and",
                        "conditions": [
                            {"type": "stats", "path": "player.skills.dance", "operator": ">=", "value": 30},
                            {"type": "flags", "path": "player.flags.outfit_ready", "value": True},
                        ],
                    },
                }
            ],
            "actions": [
                {
                    "id": "practice",
                        "effect": {
                            "set_player": {"flags.outfit_ready": True},
                            "increase_player": {"skills.dance": {">=": 30}},
                            "set_flag": {"stage_checked": True},
                            "complete_task": "prepare_show",
                        },
                }
            ],
        }
    }

    response = WorldGenerationProtocolTool().repair_world_generation(payload)

    assert response.world.tasks[0].completion == {
        "stats": {"skills.dance": {"min": 30}},
        "flags": {"outfit_ready": True},
        "actions": ["practice"],
    }
    assert response.world.actions[0].effect["set_player"] == {"flags": {"outfit_ready": True}}
    assert response.world.actions[0].effect["increase_player"] == {"skills.dance": 30}
    assert response.world.actions[0].effect["set_flags"] == {"stage_checked": True}
    assert "set_flag" not in response.world.actions[0].effect


def test_validator_initializes_missing_skill_fields_from_completion_stats() -> None:
    config = SandboxWorldConfig(
        world_id="skill_init_world",
        name="技能初始化世界",
        player={"name": "草莓包饭", "location": "舞蹈室"},
        npcs=[SandboxNPC(id="coach", name="陈前辈", location="舞蹈室")],
        tasks=[
            SandboxTask(
                id="practice_dance",
                title="练习舞蹈",
                completion={"stats": {"skills.dance": {"min": 10}}},
            )
        ],
        actions=[SandboxAction(id="practice", label="练习", effect={"increase_player": {"skills.dance": 10}})],
    )

    repaired = SandboxWorldValidator().ensure_valid(config)

    assert repaired.player["skills"]["dance"] == 0


def test_validator_derives_player_schema_from_completion_and_action_rules() -> None:
    config = SandboxWorldConfig(
        world_id="schema_world",
        name="状态字段推导世界",
        player={"name": "草莓包饭", "location": "起点"},
        npcs=[SandboxNPC(id="mentor", name="导师", location="起点")],
        tasks=[
            SandboxTask(
                id="multi_rule_task",
                title="综合考核",
                completion={
                    "stats": {"skills.vocal": {"min": 5}, "stage_confidence": {"min": 3}},
                    "player": {"status": "准备就绪", "approved": True},
                    "items": ["报名表"],
                },
            )
        ],
        actions=[
            SandboxAction(
                id="prepare",
                label="准备",
                effect={
                    "set_player": {"costume": {"ready": True}},
                    "increase_player": {"skills.vocal": 5, "stage_confidence": 3},
                },
            )
        ],
    )

    repaired = SandboxWorldValidator().ensure_valid(config)

    assert repaired.player["skills"]["vocal"] == 0
    assert repaired.player["stage_confidence"] == 0
    assert repaired.player["approved"] is False
    assert repaired.player["inventory"] == []
    assert repaired.player["costume"]["ready"] is False


def test_router_exposes_review_pipelines() -> None:
    router = RouterAgent()

    assert router.world_generation_pipeline() == [
        "WorldBuilderAgent",
        "WorldValidator/SchemaRepairer",
        "NpcLorebookCreationAgent",
        "WorldReviewAgent",
    ]
    assert router.npc_runtime_pipeline() == [
        "NpcLorebookReviewAgent",
        "NpcLorebookRuntime",
        "NpcAgent",
        "AgentLLMOutput schema gate",
        "NpcProtocolReviewAgent(if invalid)",
        "StateValidatorAgent",
        "NpcReviewAgent",
    ]
    assert router.ui_projection_pipeline() == ["UiStateProjector", "UiReviewAgent"]
    assert router.playtest_pipeline() == [
        "SandboxWorldAdapter",
        "NpcLorebookReviewAgent",
        "NpcLorebookRuntime",
        "Full visual-bound world assets",
        "PlaytestAgent",
        "FlowReviewAgent",
    ]


def test_ui_review_detects_missing_completion_state_field() -> None:
    report = UiReviewAgent().review(
        {
            "player": {"name": "玩家"},
            "tasks": [{"id": "practice", "completion": {"stats": {"skills.dance": {"min": 10}}}}],
        }
    )

    assert not report.passed
    assert report.issues[0].path == "player.skills.dance"


def test_world_review_reports_missing_completion() -> None:
    config = SandboxWorldConfig(
        world_id="review_world",
        name="审查世界",
        player={"name": "玩家", "location": "起点"},
        npcs=[SandboxNPC(id="npc", name="NPC", location="起点")],
        tasks=[SandboxTask(id="task", title="任务")],
        actions=[],
    )

    report = WorldReviewAgent().review(config)

    assert report.issues


def test_world_review_rejects_semantic_completion_mismatch() -> None:
    config = SandboxWorldConfig(
        world_id="semantic_review_world",
        name="语义审查世界",
        player={"name": "草莓包饭", "location": "练习室", "skills": {"dance": 0, "vocal": 0}},
        npcs=[SandboxNPC(id="coach", name="导师", location="练习室")],
        tasks=[
            SandboxTask(
                id="vocal_training",
                title="声乐特训",
                description="在练习室进行声乐训练，直到 vocal 技能达到 30 以上。",
                completion={"stats": {"skills.dance": {"min": 10}}},
            )
        ],
        actions=[SandboxAction(id="practice", label="练习", effect={"complete_task": "vocal_training"})],
    )

    report = WorldReviewAgent().review(config)

    assert not report.passed
    assert any("skills.vocal" in issue.message or "skills.dance" in issue.message for issue in report.issues)


def test_mechanics_schema_drives_semantic_review_without_hardcoded_field_names() -> None:
    config = SandboxWorldConfig(
        world_id="mechanics_review_world",
        name="机制审查世界",
        player={"name": "玩家", "location": "训练室", "skills": {"wrong_field": 0, "breath_control": 0}},
        npcs=[SandboxNPC(id="mentor", name="导师", location="训练室")],
        tasks=[
            SandboxTask(
                id="breath_training",
                title="气息特训",
                description="在训练室进行气息训练，直到气息控制达到 30 以上。",
                completion={"stats": {"skills.wrong_field": {"min": 10}}},
            )
        ],
        actions=[SandboxAction(id="practice", label="训练", effect={"complete_task": "breath_training", "increase_player": {"skills.wrong_field": 10}})],
        metadata={
            "mechanics": [
                {
                    "id": "breath_control",
                    "path": "skills.breath_control",
                    "label": "气息控制",
                    "aliases": ["气息控制", "气息", "breath_control"],
                    "kind": "stat",
                }
            ]
        },
    )

    report = WorldReviewAgent().review(config)

    assert not report.passed
    assert any("skills.breath_control" in issue.message for issue in report.issues)


def test_mechanics_design_agent_aligns_action_outputs_to_completion_paths() -> None:
    from app.agents.world_builder.tools import MechanicsDesignAgent

    config = SandboxWorldConfig(
        world_id="mechanics_design_world",
        name="机制设计世界",
        player={"name": "玩家", "location": "训练室"},
        npcs=[SandboxNPC(id="mentor", name="导师", location="训练室")],
        tasks=[SandboxTask(id="breath_training", title="气息特训", completion={"stats": {"skills.breath_control": {"min": 30}}})],
        actions=[SandboxAction(id="practice", label="训练", effect={"complete_task": "breath_training"})],
    )

    notes = MechanicsDesignAgent().design(config)

    assert notes
    assert config.metadata["mechanics"][0]["path"] == "skills.breath_control"
    assert config.actions[0].effect["increase_player"]["skills.breath_control"] == 30


def test_world_builder_quality_gate_playtests_repaired_world() -> None:
    request = WorldGenerateRequest(template="freeform", player_name="测试玩家", use_learned_profile=False)
    config = _finalize_world_quality(generate_world_config(request))

    assert config.metadata["quality_gate"]["validator_passed"] is True
    assert config.metadata["quality_gate"]["playtest_passed"] is True
    assert config.metadata["quality_gate"]["passed"] is True
    assert config.metadata["playtest_review"]["metadata"]["stopped_reason"] == "completed"


def test_world_quality_gate_fails_when_playtest_fails(monkeypatch) -> None:
    from app.worlds.sandbox import generator

    class FailingPlaytestAgent:
        def simulate_adapter(self, adapter):
            return ReviewReport(
                reviewer="PlaytestAgent",
                passed=False,
                issues=[ReviewIssue(severity="error", area="playtest", path="tasks", message="修复后仍无法闭环。")],
                metadata={"stopped_reason": "blocked", "steps": [], "pending_task_ids": ["main"]},
            )

    monkeypatch.setattr(generator, "PlaytestAgent", FailingPlaytestAgent)

    config = generator._finalize_world_quality(make_world())

    assert config.metadata["quality_gate"]["validator_passed"] is True
    assert config.metadata["quality_gate"]["playtest_passed"] is False
    assert config.metadata["quality_gate"]["passed"] is False
    assert config.metadata["playtest_review"]["metadata"]["stopped_reason"] == "blocked"


def test_playtest_agent_can_complete_playable_world_loop() -> None:
    adapter = SandboxWorldAdapter(make_world())

    report = PlaytestAgent().simulate_adapter(adapter)

    assert report.passed
    assert report.metadata["stopped_reason"] == "completed"
    assert report.metadata["completed_task_ids"] == ["get_token"]
    assert report.metadata["steps"][0]["action"] == "take_token"


def test_fallback_world_generation_respects_custom_scale() -> None:
    config = generate_world_config(
        WorldGenerateRequest(
            template="freeform",
            theme="一个女生想成为漫展嘉宾",
            player_name="赤西夜夜",
            complexity="ultra",
            min_npcs=6,
            min_tasks=9,
            min_actions=11,
        )
    )

    assert len(config.npcs) >= 6
    assert len(config.tasks) >= 9
    assert len(config.actions) >= 11
    assert config.tasks[-1].completion["previous_tasks"] == [task.id for task in config.tasks[:-1]]


def test_idol_fallback_uses_thematic_locations_instead_of_placeholders() -> None:
    config = generate_world_config(
        WorldGenerateRequest(
            template="freeform",
            theme="一个名叫草莓包饭的18岁少女，去BEJ48想当偶像出道，最终拿到总选举第一",
            player_name="草莓包饭",
            min_npcs=10,
            min_tasks=12,
            min_actions=14,
            use_learned_profile=False,
        )
    )

    npc_text = "\n".join(f"{npc.name} {npc.location}" for npc in config.npcs)
    task_text = "\n".join(task.title for task in config.tasks)
    assert "支线联系人" not in npc_text
    assert "阶段地点" not in npc_text
    assert "声乐练习室" in npc_text or "舞蹈练习室" in npc_text
    assert "完成声乐基础训练" in task_text or "完成舞蹈基础训练" in task_text


def test_experience_learning_profile_updates_from_feedback(tmp_path: Path) -> None:
    from app.agents.experience_learning import ExperienceFeedbackStore, ExperienceLearningAgent

    agent = ExperienceLearningAgent(ExperienceFeedbackStore(tmp_path / "experience.json"))
    profile = agent.record(
        ExperienceFeedbackRequest(
            world_id="test_world",
            world_name="测试世界",
            npc_count=9,
            task_count=18,
            action_count=22,
            immersion_score=5,
            pacing="immersive",
            notes="18 个任务更沉浸。",
        )
    )

    assert profile.sample_count == 1
    assert profile.recommended_tasks == 18
    assert "18" in profile.summary


def test_api_experience_profile() -> None:
    client = TestClient(app)
    response = client.get("/api/experience/profile")

    assert response.status_code == 200
    assert response.json()["recommended_tasks"] >= 1


def test_final_task_gate_prevents_one_click_finish_before_prerequisites() -> None:
    config = generate_world_config(
        WorldGenerateRequest(
            template="freeform",
            theme="一个女生想成为漫展嘉宾",
            player_name="赤西夜夜",
            min_tasks=6,
            min_actions=6,
            final_task_requires_previous=True,
        )
    )
    adapter = SandboxWorldAdapter(config)
    state = adapter.create_initial_state()

    adapter.handle_world_action(state, WorldActionRequest(action="finish_goal", payload={}))

    final_task = next(task for task in state.world_state["tasks"] if task["id"] == "finish_goal")
    assert final_task["status"] != "done"


def test_playtest_agent_reports_blocked_world_loop() -> None:
    class BlockedAdapter:
        def create_initial_state(self):
            adapter = SandboxWorldAdapter(make_world())
            state = adapter.create_initial_state()
            state.world_state["tasks"] = [
                {
                    "id": "return_to_master",
                    "title": "回师门复命",
                    "status": "pending",
                    "completion": {"player": {"reported_to_master": True}},
                }
            ]
            return state

        def world_action_ids(self):
            return ["fight_monster"]

        def handle_world_action(self, state, request):
            state.world_state.setdefault("player", {})["monster_defeated"] = True
            state.world_state.setdefault("custom_events", []).append(
                {
                    "action_id": request.action,
                    "effect": {"set_player": {"monster_defeated": True}},
                }
            )
            return SandboxWorldAdapter(make_world()).handle_world_action(state, WorldActionRequest(action="inspect_location", payload={}))

    report = PlaytestAgent().simulate_adapter(BlockedAdapter())

    assert not report.passed
    assert report.metadata["stopped_reason"] == "blocked"
    assert report.metadata["pending_task_ids"] == ["return_to_master"]
    assert "player.reported_to_master" in report.issues[0].message


def test_natural_language_inspection_can_trigger_matching_local_action() -> None:
    config = SandboxWorldConfig(
        world_id="outfit_world",
        name="服装准备世界",
        player={"name": "草莓包饭", "location": "更衣室"},
        npcs=[SandboxNPC(id="rival", name="林雪", role="竞争对手", location="更衣室")],
        tasks=[SandboxTask(id="prepare_costume", title="准备演出服", completion={"player": {"status": "准备就绪"}})],
        actions=[
            SandboxAction(
                id="fix_outfit",
                label="整理行头",
                description="仔细检查演出服的细节，确保没有瑕疵。",
                effect={
                    "scene": "你发现衣服有些褶皱，细心熨烫平整，心情变得平静。",
                    "set_player": {"location": "更衣室", "status": "准备就绪"},
                    "complete_task": "prepare_costume",
                    "active_npc_id": "rival",
                },
            )
        ],
    )

    adapter = SandboxWorldAdapter(config)
    state = adapter.create_initial_state()
    response = adapter.handle_world_action(
        state,
        WorldActionRequest(action="inspect_location", payload={"location": "更衣室", "query": "我来熨衣服了"}),
    )

    assert response.player["status"] == "准备就绪"
    assert response.state["tasks"][0]["status"] == "done"
    assert "熨烫" in response.narration


def test_runtime_guardrail_rejects_unknown_location_and_executes_action_id() -> None:
    config = SandboxWorldConfig(
        world_id="guard_location_world",
        name="地点守卫世界",
        player={"name": "草莓包饭", "location": "设计学校"},
        npcs=[SandboxNPC(id="teacher", name="林老师", location="设计学校")],
        tasks=[SandboxTask(id="practice_basic", title="基础练习", completion={"actions": ["action_practice_basic"]})],
        actions=[
            SandboxAction(
                id="action_practice_basic",
                label="基础练习",
                effect={
                    "scene": "你完成了一轮基础练习。",
                    "complete_task": "practice_basic",
                    "active_npc_id": "teacher",
                    "set_player": {"location": "设计学校"},
                },
            )
        ],
    )
    adapter = SandboxWorldAdapter(config)
    state = adapter.create_initial_state()

    rejected = adapter.handle_world_action(state, WorldActionRequest(action="move_player", payload={"location": "Agency Hub"}))
    assert "没有登记地点" in rejected.narration
    assert rejected.player["location"] == "设计学校"

    executed = adapter.handle_world_action(state, WorldActionRequest(action="move_player", payload={"location": "action_practice_basic"}))
    assert executed.state["tasks"][0]["status"] == "done"


def test_runtime_guardrail_filters_unknown_location_suggestions_at_response_boundary() -> None:
    config = SandboxWorldConfig(
        world_id="guard_reply_world",
        name="回复守卫世界",
        player={"name": "草莓包饭", "location": "设计学校"},
        npcs=[SandboxNPC(id="teacher", name="林老师", location="设计学校")],
        tasks=[SandboxTask(id="practice", title="练习")],
        actions=[],
    )
    adapter = SandboxWorldAdapter(config)
    state = adapter.create_initial_state()
    response = adapter.build_chat_response(
        state,
        AgentLLMOutput(
            content="你可以询问林老师下一步。",
            inner_thought="测试未知地点",
            command={"name": "none", "args": {}},
            suggested_actions=["前往 Agency Hub", "询问林老师下一步"],
        ),
        "测试",
    )

    assert response.reply == "你可以询问林老师下一步。"
    assert all("Agency Hub" not in item for item in response.suggested_actions)


@pytest.mark.asyncio
async def test_runtime_guardrail_retries_npc_when_unknown_location_is_suggested(tmp_path: Path) -> None:
    class LocationRetryLLM:
        def __init__(self):
            self.calls = 0

        async def invoke(self, messages, fallback_actions):
            self.calls += 1
            if self.calls == 1:
                return AgentLLMOutput(
                    content="你可以去 Agency Hub 接外包。",
                    inner_thought="第一次错误建议",
                    command={"name": "none", "args": {}},
                    suggested_actions=["前往 Agency Hub"],
                )
            return AgentLLMOutput(
                content="你先留在设计学校找林老师，把基础练习做扎实。",
                inner_thought="已按 guardrail 改写",
                command={"name": "none", "args": {}},
                suggested_actions=["询问林老师下一步"],
            )

    config = SandboxWorldConfig(
        world_id="guard_retry_world",
        name="重试守卫世界",
        player={"name": "草莓包饭", "location": "设计学校"},
        npcs=[SandboxNPC(id="teacher", name="林老师", location="设计学校")],
        tasks=[SandboxTask(id="practice", title="练习")],
        actions=[],
    )
    llm = LocationRetryLLM()
    response = await AgentRuntime(
        SandboxWorldAdapter(config),
        llm_client=llm,
        session_store=RuntimeSessionStore(tmp_path / "sessions"),
    ).chat(
        ChatRequest(message="我现在该做什么？", player_name="草莓包饭", location="设计学校", target_npc_id="teacher")
    )

    assert llm.calls == 2
    assert "Agency Hub" not in response.reply
    assert "设计学校" in response.reply
    assert response.debug_trace["llm"]["guardrail_repaired"] is True
    assert response.debug_trace["llm"]["guardrail_retry_attempts"] == 1


@pytest.mark.asyncio
async def test_runtime_guardrail_retry_uses_request_scoped_npc_llm(tmp_path: Path, monkeypatch) -> None:
    created_configs: list[LLMProviderConfig | None] = []

    class DefaultRuntimeLLM:
        async def invoke(self, messages, fallback_actions):
            return AgentLLMOutput(
                content="你可以去 Agency Hub 接外包。",
                inner_thought="默认 runtime 模型只负责第一次错误输出",
                command={"name": "none", "args": {}},
                suggested_actions=["前往 Agency Hub"],
            )

    class RequestScopedLLM:
        async def invoke(self, messages, fallback_actions):
            return AgentLLMOutput(
                content="你先留在设计学校找林老师。",
                inner_thought="使用请求指定模型完成 guardrail retry",
                command={"name": "none", "args": {}},
                suggested_actions=["询问林老师下一步"],
                provider_trace=[{"stage": "raw_json_prompt", "model": "step-3.7-flash", "base_url": "https://api.stepfun.com/step_plan/v1"}],
            )

    def fake_create_npc_llm_client(config=None, provider=None):
        created_configs.append(config)
        return RequestScopedLLM()

    monkeypatch.setattr("app.core.runtime.create_npc_llm_client", fake_create_npc_llm_client)
    config = SandboxWorldConfig(
        world_id="guard_request_llm_world",
        name="请求模型守卫世界",
        player={"name": "草莓包饭", "location": "设计学校"},
        npcs=[SandboxNPC(id="teacher", name="林老师", location="设计学校")],
        tasks=[SandboxTask(id="practice", title="练习")],
        actions=[],
    )

    response = await AgentRuntime(
        SandboxWorldAdapter(config),
        llm_client=DefaultRuntimeLLM(),
        session_store=RuntimeSessionStore(tmp_path / "sessions"),
    ).chat(
        ChatRequest(
            message="我现在该做什么？",
            player_name="草莓包饭",
            location="设计学校",
            target_npc_id="teacher",
            npc_llm=LLMProviderConfig(
                api_key="stepfun-key",
                base_url="https://api.stepfun.com/step_plan/v1",
                model="step-3.7-flash",
            ),
        )
    )

    assert response.reply == "你先留在设计学校找林老师。"
    assert created_configs[-1].model == "step-3.7-flash"
    assert created_configs[-1].base_url == "https://api.stepfun.com/step_plan/v1"
    assert response.debug_trace["llm"]["provider_trace"][0]["model"] == "step-3.7-flash"


@pytest.mark.asyncio
async def test_runtime_guardrail_fails_after_two_npc_retries(tmp_path: Path) -> None:
    class StubbornLocationLLM:
        def __init__(self):
            self.calls = 0

        async def invoke(self, messages, fallback_actions):
            self.calls += 1
            return AgentLLMOutput(
                content="你还是去 Agency Hub 或出租屋。",
                inner_thought="持续错误建议",
                command={"name": "none", "args": {}},
                suggested_actions=["前往 Agency Hub"],
            )

    config = SandboxWorldConfig(
        world_id="guard_fallback_world",
        name="降级守卫世界",
        player={"name": "草莓包饭", "location": "设计学校"},
        npcs=[SandboxNPC(id="teacher", name="林老师", location="设计学校")],
        tasks=[SandboxTask(id="practice", title="练习")],
        actions=[],
    )
    llm = StubbornLocationLLM()
    runtime = AgentRuntime(
        SandboxWorldAdapter(config),
        llm_client=llm,
        session_store=RuntimeSessionStore(tmp_path / "sessions"),
    )

    with pytest.raises(RuntimeError, match="NPC 回复未通过地点校验"):
        await runtime.chat(
            ChatRequest(message="我现在该做什么？", player_name="草莓包饭", location="设计学校", target_npc_id="teacher")
        )

    assert llm.calls == 3
    npc_memory = "\n".join(item.content for item in runtime._get_npc_session("teacher").memories)
    assert "上一次回复被运行时校验拒绝" in npc_memory
    assert "连续两次回复仍未通过地点校验" in npc_memory
    assert "当前可用地点" not in npc_memory


def test_api_world_lifecycle(tmp_path: Path) -> None:
    client = TestClient(app)
    world_id = "api_foundation_test"
    payload = make_world().model_dump()
    payload["world_id"] = world_id

    saved = client.put(f"/api/worlds/{world_id}", json=payload)
    assert saved.status_code == 200

    started = client.post(f"/api/worlds/{world_id}/start")
    assert started.status_code == 200
    assert started.json()["player"]["name"] == "测试玩家"
    assert started.json()["narration"] == "场景推进：测试开始。"

    acted = client.post(f"/api/worlds/{world_id}/action", json={"action": "take_token", "payload": {}})
    assert acted.status_code == 200
    assert acted.json()["player"]["trial_token"] is True

    session = client.get(f"/api/worlds/{world_id}/session")
    assert session.status_code == 200
    assert session.json()["player"]["trial_token"] is True

    client.delete(f"/api/worlds/{world_id}")


def test_api_world_start_rejects_missing_world() -> None:
    response = TestClient(app).post("/api/worlds/does_not_exist_for_start/start")
    assert response.status_code == 404


def test_api_world_list_includes_saved_time() -> None:
    client = TestClient(app)
    world_id = "time_list_world"
    payload = make_world().model_dump()
    payload["world_id"] = world_id
    payload["name"] = "时间列表世界"

    saved = client.put(f"/api/worlds/{world_id}", json=payload)
    assert saved.status_code == 200
    try:
        response = client.get("/api/worlds")
        assert response.status_code == 200
        item = next(world for world in response.json() if world["world_id"] == world_id)
        assert item["created_at"]
        assert item["updated_at"]
        assert "T" in item["updated_at"]
    finally:
        client.delete(f"/api/worlds/{world_id}")


def test_api_generate_world_lorebook_updates_current_world(monkeypatch) -> None:
    class FakeLorebookAgent:
        async def create(self, world, llm_config, *, strict=False):
            captured["calls"] = int(captured.get("calls", 0)) + 1
            call = captured["calls"]
            captured["world_id"] = world.world_id
            captured["strict"] = strict
            captured["purpose"] = llm_config.metadata.get("purpose")
            return NpcLorebookArtifact(
                artifact_id=f"{world.world_id}.npc_lorebook",
                world_id=world.world_id,
                title=f"测试世界书 {call}",
                entries=[
                    NpcLorebookEntry(
                        id=f"world_overview_{call}",
                        title=f"世界总观 {call}",
                        content=f"这里是第 {call} 版可被 NPC Runtime 消费的世界背景。",
                        strategy="constant",
                        priority=9999,
                    )
                ],
                metadata={"created_by": "NpcLorebookCreationAgent"},
            )

    captured: dict[str, object] = {}
    monkeypatch.setattr(api_routes, "NpcLorebookCreationAgent", FakeLorebookAgent)
    client = TestClient(app)
    world_id = "api_lorebook_world"
    payload = make_world().model_dump()
    payload["world_id"] = world_id
    payload["metadata"] = {}

    saved = client.put(f"/api/worlds/{world_id}", json=payload)
    assert saved.status_code == 200
    try:
        response = client.post(f"/api/worlds/{world_id}/lorebook/generate")
        assert response.status_code == 200
        data = response.json()
        lorebook = data["metadata"]["npc_lorebook"]
        generation = data["metadata"]["npc_lorebook_generation"]
        assert captured == {"calls": 1, "world_id": world_id, "strict": False, "purpose": "npc_lorebook"}
        assert lorebook["schema_version"] == "npc_lorebook.v1"
        assert lorebook["entries"][0]["strategy"] == "constant"
        assert lorebook["metadata"]["created_at"]
        assert generation["agent"] == "NpcLorebookCreationAgent"
        assert generation["entry_count"] == 1
        assert generation["created_at"] == lorebook["metadata"]["created_at"]

        first_version = data["metadata"]["npc_lorebook_versions"][0]
        assert first_version["is_active"] is True
        assert first_version["artifact"]["entries"][0]["id"] == "world_overview_1"

        response = client.post(f"/api/worlds/{world_id}/lorebook/generate")
        assert response.status_code == 200
        data = response.json()
        versions = data["metadata"]["npc_lorebook_versions"]
        assert len(versions) == 2
        assert versions[0]["is_active"] is True
        assert versions[1]["is_active"] is False
        assert data["metadata"]["npc_lorebook"]["entries"][0]["id"] == "world_overview_2"

        response = client.post(f"/api/worlds/{world_id}/lorebook/select/{first_version['version_id']}")
        assert response.status_code == 200
        data = response.json()
        versions = data["metadata"]["npc_lorebook_versions"]
        selected = next(version for version in versions if version["version_id"] == first_version["version_id"])
        assert selected["is_active"] is True
        assert data["metadata"]["npc_lorebook"]["entries"][0]["id"] == "world_overview_1"
        assert data["metadata"]["npc_lorebook_generation"]["selected_from_version"] == first_version["version_id"]
    finally:
        client.delete(f"/api/worlds/{world_id}")


def test_pipeline_config_api_persists_workbench_config(tmp_path: Path) -> None:
    original_store = api_routes.pipeline_config_store
    api_routes.pipeline_config_store = api_routes.PipelineConfigStore(tmp_path / "pipeline_config.json")
    try:
        client = TestClient(app)
        response = client.put(
            "/api/config",
            json={
                "defaults": {
                    "llm": {
                        "provider": "openai_compatible",
                        "base_url": "https://default.example/v1",
                        "api_key": "default-key",
                        "model": "default-model",
                    },
                    "image": {
                        "provider": "stepfun",
                        "api_base_url": "https://default-image.example/v1",
                        "api_key": "default-image-key",
                        "model": "step-image-edit-2",
                        "size": "1024x1024",
                    },
                },
                "agents": {
                    "script_decomposition": {
                        "use_default_llm": False,
                        "llm": {
                            "provider": "openai_compatible",
                            "base_url": "https://script.example/v1",
                            "api_key": "script-key",
                            "model": "script-model",
                        },
                    },
                    "world_builder": {
                        "use_default_llm": False,
                        "llm": {
                            "provider": "openai_compatible",
                            "base_url": "https://world.example/v1",
                            "api_key": "world-key",
                            "model": "world-model",
                        },
                    },
                    "visual_prompt_composer": {
                        "use_default_llm": False,
                        "llm": {
                            "provider": "openai_compatible",
                            "base_url": "https://visual.example/v1",
                            "api_key": "visual-key",
                            "model": "visual-model",
                        },
                    },
                    "npc_runtime": {
                        "use_default_llm": False,
                        "llm": {
                            "provider": "openai_compatible",
                            "base_url": "https://npc.example/v1",
                            "api_key": "npc-key",
                            "model": "npc-model",
                        },
                    },
                    "visual_asset_generation": {
                        "use_default_image": False,
                        "image": {
                            "provider": "stepfun",
                            "api_base_url": "https://image.example/v1",
                            "api_key": "image-key",
                            "model": "step-image-edit-2",
                            "size": "768x1360",
                            "retry_count": 2,
                            "seed": 100,
                            "steps": 24,
                            "cfg_scale": 1.5,
                            "text_mode": True,
                        },
                    },
                },
            },
        )
        assert response.status_code == 200

        saved = json.loads((tmp_path / "pipeline_config.json").read_text(encoding="utf-8"))
        assert saved["defaults"]["llm"]["base_url"] == "https://default.example/v1"
        assert saved["agents"]["script_decomposition"]["llm"]["base_url"] == "https://script.example/v1"
        assert saved["agents"]["world_builder"]["llm"]["base_url"] == "https://world.example/v1"
        assert saved["agents"]["visual_asset_generation"]["image"]["size"] == "768x1360"
        assert saved["agents"]["visual_asset_generation"]["image"]["seed"] == 100

        loaded = client.get("/api/config/effective?include_secrets=true")
        assert loaded.status_code == 200
        body = loaded.json()
        assert body["script_decomposition_api"]["model"] == "script-model"
        assert body["script_decomposition_api"]["api_key"] == "script-key"
        assert body["world_api"]["model"] == "world-model"
        assert body["world_api"]["api_key"] == "world-key"
        assert body["image_api"]["api_key"] == "image-key"
        assert body["image_api"]["size"] == "768x1360"
        assert body["image_api"]["steps"] == 24
        assert body["agents"]["npc_runtime"]["effective_llm"]["model"] == "npc-model"
    finally:
        api_routes.pipeline_config_store = original_store


def test_pipeline_config_api_migrates_legacy_workbench_config(tmp_path: Path) -> None:
    original_store = api_routes.pipeline_config_store
    api_routes.pipeline_config_store = api_routes.PipelineConfigStore(tmp_path / "pipeline_config.json")
    try:
        client = TestClient(app)
        response = client.put(
            "/api/config",
            json={
                "world_api": {
                    "provider": "openai_compatible",
                    "base_url": "https://legacy-world.example/v1",
                    "api_key": "legacy-world-key",
                    "model": "legacy-world-model",
                },
                "visual_prompt_api": {
                    "provider": "openai_compatible",
                    "base_url": "https://legacy-visual.example/v1",
                    "api_key": "legacy-visual-key",
                    "model": "legacy-visual-model",
                },
                "npc_api": {
                    "provider": "openai_compatible",
                    "base_url": "https://legacy-npc.example/v1",
                    "api_key": "legacy-npc-key",
                    "model": "legacy-npc-model",
                },
            },
        )
        assert response.status_code == 200
        saved = json.loads((tmp_path / "pipeline_config.json").read_text(encoding="utf-8"))
        assert saved["agents"]["script_decomposition"]["llm"]["model"] == "legacy-world-model"
        assert saved["agents"]["world_builder"]["llm"]["model"] == "legacy-world-model"
        assert saved["agents"]["visual_prompt_composer"]["llm"]["model"] == "legacy-visual-model"
        assert saved["agents"]["npc_runtime"]["llm"]["model"] == "legacy-npc-model"
    finally:
        api_routes.pipeline_config_store = original_store


def test_api_import_world_from_text_document() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/worlds/import",
        data={"player_name": "草莓包饭", "world_name": "导入测试世界", "use_ai": "false"},
        files={
            "file": (
                "world.md",
                "主角想参加偶像海选。NPC 有王经理、李教练和竞争对手林雪。道具有报名表和练习卡。",
                "text/markdown",
            )
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["name"]
    assert data["npcs"]
    assert data["tasks"]
    assert data["actions"]
    assert data["metadata"]["validation"]["valid"] is True

    client.delete(f"/api/worlds/{data['world_id']}")


def test_api_create_script_decomposition_only_returns_script_json() -> None:
    client = TestClient(app)
    response = client.post("/api/worlds/script-decomposition", json=make_script_case().model_dump())

    assert response.status_code == 200
    data = response.json()
    assert data["report"]["passed"] is True
    assert data["decomposition"]["truth"].startswith("管家林伯")
    assert data["world"] is None
    assert data["artifact"]["artifact_id"] == "锁门后的钟声"
    artifact_paths = [
        Path(data["artifact"]["decomposition_path"]),
        Path(data["artifact"]["report_path"]),
        Path(data["artifact"]["response_path"]),
    ]
    for path in artifact_paths:
        assert path.exists()
        path.unlink()


def test_script_decomposition_artifact_store_uses_title_or_default(tmp_path: Path) -> None:
    result = build_script_world(make_script_case())
    result.world = None
    result.decomposition.metadata["decomposition_model"] = {
        "api_key": "secret-key",
        "base_url": "https://secret.example/v1",
        "model": "test-model",
    }
    store = ScriptDecompositionArtifactStore(tmp_path)

    titled = store.save(result, "章节拆解测试")
    agent_titled = store.save(result, "")
    defaulted = store.save(result.model_copy(update={"decomposition": result.decomposition.model_copy(update={"title": ""})}), "")

    assert titled["artifact_id"] == "章节拆解测试"
    assert Path(titled["decomposition_path"]).name == "章节拆解测试.decomposition.json"
    assert agent_titled["artifact_id"] == "锁门后的钟声"
    assert defaulted["artifact_id"] == "script_decomposition"
    assert Path(defaulted["report_path"]).exists()
    saved = json.loads(Path(titled["decomposition_path"]).read_text(encoding="utf-8"))
    assert saved["metadata"]["decomposition_model"]["api_key"] == "[redacted]"
    assert saved["metadata"]["decomposition_model"]["base_url"] == "[redacted]"


def test_script_graph_compiler_builds_graph_ready_story_relations() -> None:
    result = build_script_world(make_script_case())
    graph = ScriptGraphCompiler().compile(result.decomposition, source_artifact_id="locked_room_case")

    node_kinds = {node.kind for node in graph.nodes}
    edge_types = {edge.type for edge in graph.edges}

    assert graph.metadata["compiled_by"] == "ScriptGraphCompiler"
    assert graph.metadata["graph_source"] == "story_graph_facts"
    assert result.decomposition.story_graph.entities
    assert result.decomposition.story_graph.relations
    assert {"script", "character", "clue", "location", "secret", "event"}.issubset(node_kinds)
    assert {"HAS_CHARACTER", "HAS_CLUE", "HAS_LOCATION", "HAS_TRUTH", "HAS_EVENT", "FOUND_AT", "OWNED_BY", "REVEALS"}.issubset(edge_types)
    assert graph.indexes["node_counts"]["character"] == 2
    assert graph.indexes["node_counts"]["clue"] == 2
    assert any(node.label == "林伯" for node in graph.nodes)
    assert any(edge.type == "OWNED_BY" for edge in graph.edges)


def test_script_graph_store_persists_graph_artifact(tmp_path: Path) -> None:
    graph = ScriptGraphCompiler().compile(build_script_world(make_script_case()).decomposition)
    store = ScriptGraphStore(tmp_path)

    artifact = store.save(graph, "图谱测试")
    listed = store.list()
    loaded = store.load(artifact["artifact_id"])

    assert artifact["artifact_id"] == "图谱测试"
    assert Path(artifact["graph_path"]).exists()
    assert listed[0]["node_count"] == len(graph.nodes)
    assert loaded["graph_id"] == graph.graph_id
    assert loaded["artifact"]["edge_count"] == len(graph.edges)


def test_api_compile_script_graph_from_decomposition() -> None:
    client = TestClient(app)
    decomposition = build_script_world(make_script_case()).decomposition
    response = client.post(
        "/api/worlds/script-graph/compile",
        json={"decomposition": decomposition.model_dump(), "title": "锁门图谱", "save": True},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["graph"]["metadata"]["compiled_by"] == "ScriptGraphCompiler"
    assert data["artifact"]["artifact_id"] == "锁门图谱"
    assert data["artifact"]["node_count"] == len(data["graph"]["nodes"])
    Path(data["artifact"]["graph_path"]).unlink()


def test_api_compile_script_decomposition_world() -> None:
    client = TestClient(app)
    response = client.post("/api/worlds/script-decomposition/compile", json=make_script_case().model_dump())

    assert response.status_code == 200
    data = response.json()
    assert data["world"]["metadata"]["generated_by"] == "script_decomposition_agent"
    assert data["world"]["metadata"]["script_case"]["truth"].startswith("管家林伯")
    assert any(action["id"] == "inspect_spare_key" for action in data["world"]["actions"])

    client.delete(f"/api/worlds/{data['world']['world_id']}")


def test_api_import_script_decomposition_accepts_multiple_documents() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/worlds/script-decomposition/import",
        data={"player_name": "侦探", "world_name": "多文件案件", "decomposition_mode": "rules"},
        files=[
            (
                "files",
                (
                    "01_background.md",
                    "标题：多文件案件\n公共背景：一座旧宅发生异响。\n案件真相：管家林伯藏起钥匙。",
                    "text/markdown",
                ),
            ),
            (
                "files",
                (
                    "02_assets.md",
                    "角色\n林伯\n身份：管家\n公开信息：守在书房\n秘密：藏起备用钥匙\n地点：书房\n---\n角色\n苏青\n身份：证人\n公开信息：听见钟声\n地点：大厅\n---\n线索\n慢钟\n内容：钟慢了十分钟\n地点：大厅",
                    "text/markdown",
                ),
            ),
        ],
    )

    assert response.status_code == 200
    data = response.json()
    assert data["decomposition"]["title"] == "多文件案件"
    assert data["report"]["node_count"] >= 4
    assert data["report"]["edge_count"] >= 1
    assert data["world"] is None
    for key in ("decomposition_path", "report_path", "response_path"):
        Path(data["artifact"][key]).unlink()


def test_api_import_script_decomposition_treats_null_llm_as_default_config() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/worlds/script-decomposition/import",
        data={
            "player_name": "侦探",
            "world_name": "默认模型案件",
            "decomposition_mode": "rules",
            "decomposition_llm": "null",
        },
        files=[
            (
                "files",
                (
                    "case.md",
                    "标题：默认模型案件\n公共背景：旧宅里有人丢了钥匙。\n案件真相：林伯把钥匙藏在书房。",
                    "text/markdown",
                ),
            )
        ],
    )

    assert response.status_code == 200
    data = response.json()
    assert data["decomposition"]["title"] == "默认模型案件"
    assert data["world"] is None
    for key in ("decomposition_path", "report_path", "response_path"):
        Path(data["artifact"][key]).unlink()


def test_api_cancel_script_decomposition_job_marks_running_job() -> None:
    client = TestClient(app)
    job_id = "test_cancel_job"
    api_routes.script_decomposition_jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "events": [],
        "result": None,
        "error": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    try:
        response = client.post(f"/api/worlds/script-decomposition/import/jobs/{job_id}/cancel")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "cancelling"
        assert data["cancel_requested"] is True
        assert any(event["status"] == "cancelling" for event in data["events"])
    finally:
        api_routes.script_decomposition_jobs.pop(job_id, None)
        api_routes.script_decomposition_tasks.pop(job_id, None)


def test_api_world_template_crud() -> None:
    client = TestClient(app)
    template_id = "test_custom_template"
    payload = {
        "id": template_id,
        "name": "测试自定义结构",
        "description": "用于测试模板 CRUD",
        "structure_prompt": "按测试结构生成：开始 -> 推进 -> 结束。",
        "enabled": True,
        "sort_order": 999,
    }

    created = client.post("/api/world-templates", json=payload)
    assert created.status_code == 200
    assert created.json()["id"] == template_id

    listed = client.get("/api/world-templates")
    assert any(item["id"] == template_id for item in listed.json())

    payload["name"] = "测试自定义结构 v2"
    updated = client.put(f"/api/world-templates/{template_id}", json=payload)
    assert updated.status_code == 200
    assert updated.json()["name"] == "测试自定义结构 v2"

    deleted = client.delete(f"/api/world-templates/{template_id}")
    assert deleted.status_code == 200
    listed_after_delete = client.get("/api/world-templates")
    assert all(item["id"] != template_id for item in listed_after_delete.json())


def test_default_templates_include_script_decomposition() -> None:
    templates = {template.id: template for template in WorldTemplateStore().list()}

    assert "script_decomposition" in templates
    assert "ScriptDecompositionAgent" in templates["script_decomposition"].structure_prompt


def test_project_intake_agent_summarizes_external_game_project() -> None:
    from app.worlds.sandbox.models import ProjectIntakeRequest
    from app.agents.project_intake import analyze_project_integration

    analysis = analyze_project_integration(
        ProjectIntakeRequest(
            project_name="走路修仙",
            description="修仙步行 RPG。角色：师父、商人。地点：山门、后山、坊市。接口 BattleService 可以挑战妖兽，背包和战斗结果由服务端决定。",
            api_hint="BattleService",
            target_player="沈青锋",
        )
    )

    assert analysis.intake.project_type == "cultivation_rpg"
    assert any(item.path == "realm_level" for item in analysis.intake.candidate_mechanics)
    assert any(action.id == "challenge_monster" for action in analysis.adapter_plan.action_mappings)
    assert analysis.adapter_plan.adapter_type == "world_adapter_required"
    assert "external_game" in analysis.adapter_plan.state_ownership.values()


def test_api_project_analyze_returns_intake_and_adapter_plan() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/projects/analyze",
        json={
            "project_name": "练习生企划",
            "description": "偶像训练游戏。NPC: 王经理、李教练。地点: 练习室、舞台。需要训练唱功和舞蹈后登台。",
            "target_player": "林澈",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["intake"]["project_type"] == "idol_training"
    assert data["intake"]["recommended_world_request"]["template"] == "freeform"
    assert any(item["path"] == "skills.vocal" for item in data["intake"]["candidate_mechanics"])
    assert data["adapter_plan"]["guardrails"]


def test_creator_assistant_fallback_builds_editor_operations() -> None:
    message = "新增角色林薇薇，新增道具钥匙，金钱设为500，给当前节点新增选项去雨夜重逢"
    agent = CreatorAssistantAgent(text_client=None)
    request = CreatorAssistantRequest(
        message=message,
        selected_node_id="start",
        project={"world": {"player": {"location": "开场"}}, "characters": [], "nodes": [{"id": "start"}]},
    )

    response = agent._fallback_response(request)
    operations = [(operation.type, operation.target_id, operation.data) for operation in response.operations]

    assert ("set_player_stat", "money", {"value": 500}) in operations
    assert any(operation[0] == "add_character" and operation[2]["name"] == "林薇薇" for operation in operations)
    assert any(operation[0] == "add_item" and operation[2]["name"] == "钥匙" for operation in operations)
    assert any(operation[0] == "add_choice" and operation[1] == "start" and operation[2]["text"] == "去雨夜重逢" for operation in operations)


def test_creator_assistant_api_uses_creator_module() -> None:
    class FakeCreatorAssistant:
        async def edit(self, request):
            return CreatorAssistantResponse(
                reply="ok",
                operations=[{"type": "set_player_stat", "target_id": "money", "data": {"value": 7}}],
                summary=["ok"],
                source="test",
            )

    from fastapi import FastAPI

    test_app = FastAPI()
    test_app.include_router(
        create_creator_router(
            resolve_llm_config=lambda purpose: api_routes.LLMProviderConfig(),
            agent=FakeCreatorAssistant(),
        ),
        prefix="/api",
    )
    client = TestClient(test_app)

    response = client.post(
        "/api/creator/assistant/edit",
        json={
            "message": "金钱设为7",
            "selected_node_id": "start",
            "project": {"world": {}, "characters": [], "nodes": [{"id": "start"}]},
        },
    )

    assert response.status_code == 200
    assert response.json()["operations"][0]["target_id"] == "money"
