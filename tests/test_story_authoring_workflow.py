from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.story_authoring.compiler import StoryDraftCompiler
from app.agents.story_authoring.agent import StoryAuthoringAgent
from app.agents.story_authoring.routes import create_router
from app.agents.story_authoring.schema import StoryAuthoringRequest, StoryDraft
from app.agents.story_authoring.service import StoryAuthoringService, StoryAuthoringValidationError
from app.agents.story_authoring.store import StoryAuthoringStore
from app.agents.story_authoring.validator import StoryDraftValidator
from app.core.model_config import LLMProviderConfig


def make_draft() -> StoryDraft:
    return StoryDraft.model_validate(
        {
            "schema_version": "story_draft.v1",
            "story_id": "wenxin_gate",
            "title": "问心山门",
            "genre": "修仙剧情冒险",
            "tone": "悬疑克制",
            "premise": "问心钟在入门夜突然停响。",
            "player_role": "能够听见灵脉声音的外门弟子。",
            "player_goal": "查清灵脉异变，并选择相信的人。",
            "world_lore": "青崖宗依靠山底灵脉修行，问心钟与封印相连。",
            "start_scene_id": "gate",
            "player_name": "林问",
            "player_stats": {"realm": "炼气一层", "qi": 0, "sect_favor": 0},
            "initial_items": ["入门木牌"],
            "characters": [
                {
                    "id": "shen_zhiwei",
                    "name": "沈知微",
                    "role": "大师姐",
                    "public_profile": "严厉守序。",
                    "secret": "知道第三声钟的真相。",
                    "goal": "维持封印。",
                    "speaking_style": "简短、克制。",
                    "initial_location": "山门",
                    "knowledge_boundaries": ["不会主动说出封印真相"],
                },
                {
                    "id": "gu_qingluo",
                    "name": "顾青萝",
                    "role": "药堂弟子",
                    "public_profile": "待人温和。",
                    "secret": "正在调查黑色灵草。",
                    "goal": "救下受伤弟子。",
                    "speaking_style": "温和但会突然反问。",
                    "initial_location": "药堂",
                    "knowledge_boundaries": ["获得玩家信任前不透露调查"],
                },
            ],
            "clues": [
                {
                    "id": "black_root",
                    "title": "黑色草根",
                    "description": "引灵草根部被灵脉气息侵蚀。",
                    "source_scene_id": "medicine",
                    "owner_character_id": "gu_qingluo",
                    "reveals": "灵脉异变已经影响药谷。",
                    "required_clue_ids": [],
                }
            ],
            "scenes": [
                {
                    "id": "gate",
                    "kind": "scene",
                    "title": "入门钟停了",
                    "location": "青崖宗山门",
                    "duration_minutes": 8,
                    "objective": "完成点名并弄清钟声异常。",
                    "opening_narration": "雨落山门，问心钟只响了两声。",
                    "beats": [
                        {
                            "id": "roll_call",
                            "kind": "dialogue",
                            "speaker_id": "shen_zhiwei",
                            "content": "新弟子，报名字。别回头看雾里。",
                            "purpose": "建立规则与危险",
                            "conditions": {},
                            "effects": {},
                        },
                        {
                            "id": "warning",
                            "kind": "dialogue",
                            "speaker_id": "shen_zhiwei",
                            "content": "第三声若响，今夜就没人能下山。",
                            "purpose": "制造悬念",
                            "conditions": {},
                            "effects": {},
                        },
                    ],
                    "choices": [
                        {
                            "id": "ask_bell",
                            "text": "追问第三声钟",
                            "next_scene_id": "medicine",
                            "consequence_summary": "大师姐开始留意玩家。",
                            "conditions": {},
                            "effects": {"set_flags": {"asked_about_bell": True}},
                        }
                    ],
                    "default_next_scene_id": "",
                    "unlock_clue_ids": [],
                    "conditions": {},
                    "effects": {},
                },
                {
                    "id": "medicine",
                    "kind": "scene",
                    "title": "药堂伤者",
                    "location": "药王谷",
                    "duration_minutes": 12,
                    "objective": "判断伤者是否只是灵气逆行。",
                    "opening_narration": "药堂门口，一个新弟子倒在雨里。",
                    "beats": [
                        {
                            "id": "first_lie",
                            "kind": "dialogue",
                            "speaker_id": "gu_qingluo",
                            "content": "只是灵气逆行，一颗养气丹就好。",
                            "purpose": "给出第一个谎言",
                            "conditions": {},
                            "effects": {},
                        },
                        {
                            "id": "truth_pressure",
                            "kind": "dialogue",
                            "speaker_id": "gu_qingluo",
                            "content": "你第一次见到经脉被咬断的人吗？那就别再问了。",
                            "purpose": "揭示危险",
                            "conditions": {},
                            "effects": {},
                        },
                    ],
                    "choices": [],
                    "default_next_scene_id": "ending",
                    "unlock_clue_ids": ["black_root"],
                    "conditions": {},
                    "effects": {},
                },
                {
                    "id": "ending",
                    "kind": "ending",
                    "title": "第三声钟",
                    "location": "药王谷",
                    "duration_minutes": 10,
                    "objective": "决定是否隐瞒黑色草根。",
                    "opening_narration": "第三声钟从后山传来，顾青萝抬头看向你。",
                    "beats": [
                        {
                            "id": "ending_line",
                            "kind": "dialogue",
                            "speaker_id": "gu_qingluo",
                            "content": "现在决定吧，你要把我交给她，还是跟我去看灵脉？",
                            "purpose": "留下下一章钩子",
                            "conditions": {},
                            "effects": {},
                        }
                    ],
                    "choices": [],
                    "default_next_scene_id": "",
                    "unlock_clue_ids": [],
                    "conditions": {},
                    "effects": {},
                },
            ],
        }
    )


def make_request() -> StoryAuthoringRequest:
    return StoryAuthoringRequest(
        brief="创作一段外门弟子调查宗门灵脉的三十分钟修仙剧情。",
        target_minutes=30,
        target_scene_count=3,
        target_character_count=2,
    )


def test_story_draft_validator_accepts_reachable_authored_story() -> None:
    review = StoryDraftValidator().review(make_draft(), make_request())

    assert review.valid is True
    assert review.total_minutes == 30
    assert review.reachable_scene_count == 3
    assert review.dialogue_beat_count == 5
    assert all(issue.severity != "error" for issue in review.issues)


def test_story_draft_compiler_builds_existing_creator_graph() -> None:
    project = StoryDraftCompiler().compile(make_draft())

    assert project["version"] == "creator_graph.v1"
    assert project["world"]["world_id"] == "wenxin_gate"
    assert project["nodes"][0]["id"] == "start"
    assert project["nodes"][0]["content"].startswith("雨落山门")
    assert any(node["content"].startswith("新弟子，报名字") for node in project["nodes"])
    assert any(node["type"] == "ending" for node in project["nodes"])
    assert project["story_authoring"]["clues"][0]["id"] == "black_root"


def test_story_authoring_agent_calls_text_api_and_parses_structured_draft() -> None:
    class FakeTextClient:
        model = "fake-story-model"

        def __init__(self) -> None:
            self.calls = []

        async def generate_text(self, system_prompt, user_prompt, on_token=None):
            self.calls.append((system_prompt, user_prompt))
            return json.dumps(make_draft().model_dump(mode="json"), ensure_ascii=False)

    import asyncio

    client = FakeTextClient()
    request = make_request().model_copy(update={"story_llm": LLMProviderConfig(api_key="test-key", model="unused")})
    draft, raw, model = asyncio.run(StoryAuthoringAgent(text_client=client).create(request))

    assert draft.title == "问心山门"
    assert model == "fake-story-model"
    assert "问心山门" in raw
    assert len(client.calls) == 1
    assert "具体 NPC 台词" in client.calls[0][0]


def test_story_authoring_agent_normalizes_visual_fields_and_object_speaker_id() -> None:
    payload = make_draft().model_dump(mode="json")
    payload["visual_style"] = "东方修仙水墨动画风格"
    payload["characters"][0]["portrait_description"] = "白衣剑修，冷峻，单人全身立绘"
    payload["characters"][0]["unexpected_model_note"] = "harmless extra"
    payload["scenes"][0]["background_description"] = "雨夜山门空景，不出现人物"
    payload["scenes"][0]["beats"][0]["portrait"] = "沈知微按剑回首"
    payload["scenes"][0]["beats"][0]["speaker_id"] = {"id": "shen_zhiwei", "name": "沈知微"}

    class DriftedTextClient:
        model = "drifted-model"

        async def generate_text(self, system_prompt, user_prompt, on_token=None):
            return json.dumps(payload, ensure_ascii=False)

    import asyncio

    draft, _, _ = asyncio.run(StoryAuthoringAgent(text_client=DriftedTextClient()).create(make_request()))

    assert draft.visual_style == {"description": "东方修仙水墨动画风格"}
    assert draft.characters[0].portrait_description == "白衣剑修，冷峻，单人全身立绘"
    assert draft.scenes[0].background_description == "雨夜山门空景，不出现人物"
    assert draft.scenes[0].beats[0].speaker_id == "shen_zhiwei"
    assert draft.scenes[0].beats[0].visual_description == "沈知微按剑回首"

    project = StoryDraftCompiler().compile(draft)
    assert project["characters"][0]["portrait_description"].startswith("白衣剑修")
    assert project["nodes"][0]["background_description"].startswith("雨夜山门")


def test_story_authoring_agent_normalizes_name_based_references_to_ids() -> None:
    payload = make_draft().model_dump(mode="json")
    payload["start_scene_id"] = payload["scenes"][0]["title"]
    payload["clues"][0]["source_scene_id"] = payload["scenes"][0]["title"]
    payload["clues"][0]["owner_character_id"] = payload["characters"][0]["name"]
    payload["scenes"][0]["beats"][0]["speaker_id"] = payload["characters"][0]["name"]
    payload["scenes"][0]["choices"][0]["next_scene_id"] = payload["scenes"][1]["title"]
    payload["scenes"][0]["unlock_clue_ids"] = [payload["clues"][0]["title"]]

    class NameReferenceTextClient:
        model = "name-reference-model"

        async def generate_text(self, system_prompt, user_prompt, on_token=None):
            return json.dumps(payload, ensure_ascii=False)

    import asyncio

    draft, _, _ = asyncio.run(StoryAuthoringAgent(text_client=NameReferenceTextClient()).create(make_request()))

    assert draft.start_scene_id == payload["scenes"][0]["id"]
    assert draft.clues[0].source_scene_id == payload["scenes"][0]["id"]
    assert draft.clues[0].owner_character_id == payload["characters"][0]["id"]
    assert draft.scenes[0].beats[0].speaker_id == payload["characters"][0]["id"]
    assert draft.scenes[0].choices[0].next_scene_id == payload["scenes"][1]["id"]
    assert draft.scenes[0].unlock_clue_ids == [payload["clues"][0]["id"]]


def test_story_authoring_agent_repairs_empty_scene_beats_from_existing_narration() -> None:
    payload = make_draft().model_dump(mode="json")
    payload["scenes"][-1]["beats"] = []

    class EmptyEndingBeatTextClient:
        model = "empty-ending-beat-model"

        async def generate_text(self, system_prompt, user_prompt, on_token=None):
            return json.dumps(payload, ensure_ascii=False)

    import asyncio

    draft, _, _ = asyncio.run(StoryAuthoringAgent(text_client=EmptyEndingBeatTextClient()).create(make_request()))

    assert len(draft.scenes[-1].beats) == 1
    assert draft.scenes[-1].beats[0].kind == "narration"
    assert draft.scenes[-1].beats[0].content == payload["scenes"][-1]["opening_narration"]


def test_story_authoring_agent_removes_outgoing_edges_from_ending_scene() -> None:
    payload = make_draft().model_dump(mode="json")
    payload["scenes"][-1]["default_next_scene_id"] = payload["scenes"][0]["id"]
    payload["scenes"][-1]["choices"] = [
        {"id": "invalid_exit", "text": "离开结局", "next_scene_id": payload["scenes"][0]["id"]}
    ]

    class EndingExitTextClient:
        model = "ending-exit-model"

        async def generate_text(self, system_prompt, user_prompt, on_token=None):
            return json.dumps(payload, ensure_ascii=False)

    import asyncio

    draft, _, _ = asyncio.run(StoryAuthoringAgent(text_client=EndingExitTextClient()).create(make_request()))

    assert draft.scenes[-1].default_next_scene_id == ""
    assert draft.scenes[-1].choices == []


def test_story_authoring_api_generates_valid_preview_and_persists_artifact(tmp_path: Path) -> None:
    class FakeStoryAuthoringAgent:
        async def create(self, request):
            assert request.story_llm is not None
            return make_draft(), '{"title":"问心山门"}', "test-model"

    app = FastAPI()
    app.include_router(
        create_router(
            resolve_llm_config=lambda purpose: LLMProviderConfig(model="test-model", api_key="test-key"),
            agent=FakeStoryAuthoringAgent(),
            store=StoryAuthoringStore(tmp_path / "runs"),
        ),
        prefix="/api",
    )
    client = TestClient(app)

    response = client.post("/api/story-authoring/generate", json=make_request().model_dump(mode="json"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "llm"
    assert payload["review"]["valid"] is True
    assert payload["graph_report"]["valid"] is True
    assert payload["project"]["world"]["name"] == "问心山门"
    assert Path(payload["artifact_path"]).exists()

    listed = client.get("/api/story-authoring/runs")
    assert listed.status_code == 200
    assert listed.json()[0]["generation_id"] == payload["generation_id"]

    loaded = client.get(f"/api/story-authoring/runs/{payload['generation_id']}")
    assert loaded.status_code == 200
    assert loaded.json()["draft"]["title"] == "问心山门"


def test_story_authoring_api_rejects_broken_scene_reference(tmp_path: Path) -> None:
    broken = make_draft().model_copy(deep=True)
    broken.scenes[0].choices[0].next_scene_id = "missing_scene"

    class BrokenStoryAuthoringAgent:
        async def create(self, request):
            return broken, "{}", "test-model"

    app = FastAPI()
    app.include_router(
        create_router(
            resolve_llm_config=lambda purpose: LLMProviderConfig(model="test-model", api_key="test-key"),
            agent=BrokenStoryAuthoringAgent(),
            store=StoryAuthoringStore(tmp_path / "runs"),
        ),
        prefix="/api",
    )
    response = TestClient(app).post("/api/story-authoring/generate", json=make_request().model_dump(mode="json"))

    assert response.status_code == 422
    assert any(issue["code"] == "choice_scene_missing" for issue in response.json()["detail"]["issues"])


def test_story_authoring_service_repairs_review_failure_and_reports_progress(tmp_path: Path) -> None:
    broken = make_draft().model_copy(deep=True)
    broken.scenes[0].choices[0].next_scene_id = "missing_scene"

    class RepairingStoryAuthoringAgent:
        def __init__(self) -> None:
            self.repair_issues = []

        async def create(self, request):
            return broken, '{"draft":"broken"}', "test-model"

        async def repair(self, request, draft, issues):
            self.repair_issues = issues
            return make_draft(), '{"draft":"repaired"}', "test-model"

    import asyncio

    events = []
    agent = RepairingStoryAuthoringAgent()
    service = StoryAuthoringService(
        resolve_llm_config=lambda purpose: LLMProviderConfig(model="test-model", api_key="test-key"),
        agent=agent,
        store=StoryAuthoringStore(tmp_path / "runs"),
    )

    response = asyncio.run(service.generate(make_request(), progress=lambda title, detail: events.append((title, detail))))

    assert response.review.valid is True
    assert response.project["world"]["name"] == "问心山门"
    assert any(issue["code"] == "choice_scene_missing" for issue in agent.repair_issues)
    assert any("StoryDraftRepairAgent" in title for title, _ in events)
    assert any("修复后复验" in title for title, _ in events)


def test_story_authoring_service_returns_specific_issues_after_failed_repair(tmp_path: Path) -> None:
    broken = make_draft().model_copy(deep=True)
    broken.scenes[0].choices[0].next_scene_id = "missing_scene"

    class UnsuccessfulRepairAgent:
        async def create(self, request):
            return broken, "{}", "test-model"

        async def repair(self, request, draft, issues):
            return broken, "{}", "test-model"

    import asyncio

    service = StoryAuthoringService(
        resolve_llm_config=lambda purpose: LLMProviderConfig(model="test-model", api_key="test-key"),
        agent=UnsuccessfulRepairAgent(),
        store=StoryAuthoringStore(tmp_path / "runs"),
    )

    try:
        asyncio.run(service.generate(make_request()))
    except StoryAuthoringValidationError as exc:
        assert "choice_scene_missing" in str(exc)
        assert "missing_scene" in str(exc)
        assert any(issue["code"] == "choice_scene_missing" for issue in exc.issues)
    else:
        raise AssertionError("expected StoryAuthoringValidationError")

    failures = list((tmp_path / "runs").glob("failure_*.json"))
    assert len(failures) == 1
    failure = json.loads(failures[0].read_text(encoding="utf-8"))
    assert failure["stage"] == "story_draft_review"
    assert any(issue["code"] == "choice_scene_missing" for issue in failure["issues"])


def test_main_app_exposes_story_authoring_without_replacing_creator_routes() -> None:
    from app.main import app

    paths = TestClient(app).get("/openapi.json").json()["paths"]

    assert "/api/story-authoring/generate" in paths
    assert "/api/creator/assistant/preview" in paths
    creator_page = TestClient(app).get("/creator")
    assert creator_page.status_code == 200
    assert 'id="story-authoring-form"' not in creator_page.text
    assert creator_page.text.count('id="creator-agent-form"') == 1
    assert "一个入口，完成全部创作" in creator_page.text
    assert 'id="playtest-panel"' in creator_page.text
    assert 'id="playtest-toast"' in creator_page.text
    assert 'id="current-project-name"' in creator_page.text
    assert 'id="current-project-id"' in creator_page.text
    assert 'id="save-as-new-world"' in creator_page.text
    assert 'id="recover-visual-assets"' in creator_page.text
    assert 'creator.js?v=20260807-24' in creator_page.text
    assert 'creator.css?v=20260807-15' in creator_page.text
    assert 'id="creator-history-empty"' in creator_page.text
    assert 'id="creator-tool-log"' in creator_page.text


def test_creator_chatbox_keeps_history_preview_and_composer_scrollable() -> None:
    from app.main import app

    creator_page = TestClient(app).get("/creator").text
    creator_css = Path("static/creator.css").read_text(encoding="utf-8")

    history_position = creator_page.index('id="creator-agent-log"')
    preview_position = creator_page.index('id="creator-change-preview"')
    composer_position = creator_page.index('id="creator-agent-form"')
    assert history_position < preview_position < composer_position
    assert 'class="creator-command-scroll"' in creator_page
    assert 'id="toggle-creator-dock"' not in creator_page
    assert ".creator-command-scroll::-webkit-scrollbar" in creator_css
    assert "overflow-y: scroll" in creator_css
    assert ".creator-command-dock .creator-agent-log::-webkit-scrollbar" in creator_css
    assert ".creator-command-dock .change-preview::-webkit-scrollbar" in creator_css
    assert "max-height: min(240px, 30vh)" in creator_css
    assert "scrollbar-gutter: stable" in creator_css


def test_creator_tool_catalog_displays_mcp_tool_ids() -> None:
    creator_js = Path("static/creator.js").read_text(encoding="utf-8")
    creator_css = Path("static/creator.css").read_text(encoding="utf-8")

    assert '<code>${escapeHtml(tool.name || tool.id || "")}</code>' in creator_js
    assert ".creator-tool-list code" in creator_css


def test_creator_canvas_supports_horizontal_navigation() -> None:
    creator_js = Path("static/creator.js").read_text(encoding="utf-8")
    creator_css = Path("static/creator.css").read_text(encoding="utf-8")

    assert '$("#canvas-space").addEventListener("mousedown"' in creator_js
    assert "if (event.shiftKey)" in creator_js
    assert "panel.scrollLeft += deltaX" in creator_js
    assert "event.ctrlKey && !event.metaKey" in creator_js
    assert "const INITIAL_CANVAS_PAN_MARGIN = 3200" in creator_js
    assert "focusCanvasViewport(state.selectedNodeId)" in creator_js
    assert "worldX + state.canvasPanMargin" in creator_js
    assert "function ensureCanvasPanRoom(panel)" in creator_js
    assert ".canvas-panel::-webkit-scrollbar" in creator_css
    assert "height: 14px" in creator_css


def test_pipeline_exposes_world_aware_creator_navigation() -> None:
    from app.main import app

    pipeline_page = TestClient(app).get("/pipeline")
    pipeline_js = Path("static/pipeline.js").read_text(encoding="utf-8")

    assert pipeline_page.status_code == 200
    assert 'id="open-creator"' in pipeline_page.text
    assert 'href="/creator"' in pipeline_page.text
    assert "`/creator?world=${encodeURIComponent(worldId)}`" in pipeline_js
