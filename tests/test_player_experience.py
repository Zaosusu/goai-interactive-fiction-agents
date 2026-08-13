from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.player_experience.runtime import PlayerStoryRuntime
from app.player_experience.store import PlayerSessionStore
from app.worlds.sandbox.models import SandboxNPC, SandboxWorldConfig, WorldSummary


def make_world() -> SandboxWorldConfig:
    graph = {
        "version": "creator_graph.v1",
        "world": {
            "world_id": "test_story",
            "name": "问心钟",
            "lore": "问心钟停响的修仙夜。",
            "player": {
                "name": "沈青锋",
                "location": "问心台",
                "stats": {"spirit": 1},
                "inventory": [],
            },
        },
        "characters": [
            {
                "id": "npc_ling",
                "name": "凌霜",
                "role": "守钟人",
                "portrait": "",
            }
        ],
        "nodes": [
            {
                "id": "start",
                "type": "story",
                "title": "钟停之夜",
                "character": "npc_ling",
                "content": "问心钟在你踏上石阶时突然停响。",
                "next": "choice",
            },
            {
                "id": "choice",
                "type": "choice",
                "title": "如何回应",
                "character": "npc_ling",
                "content": "凌霜按住剑柄，等你开口。",
                "choices": [
                    {
                        "id": "tell_truth",
                        "text": "说出你听见灵脉哭泣",
                        "next": "truth",
                        "effects": {
                            "set_flags": {"honest": True},
                            "increase_player": {"stats.spirit": 2},
                        },
                    },
                    {
                        "id": "hide_truth",
                        "text": "隐瞒异象",
                        "next": "truth",
                        "effects": {"set_flags": {"honest": False}},
                    },
                ],
            },
            {
                "id": "truth",
                "type": "story",
                "title": "钟下真相",
                "character": "npc_ling",
                "content": "她告诉你，钟并非沉默，而是在等一个能听懂它的人。",
                "next": "ending",
            },
            {
                "id": "ending",
                "type": "ending",
                "title": "引钟人",
                "character": "npc_ling",
                "content": "黎明前，你成为新一任引钟人。",
            },
        ],
        "post_story": {
            "enabled": True,
            "events": [
                {
                    "id": "ask_bell_name",
                    "title": "钟灵回应",
                    "description": "问心钟第一次说出了自己的名字。",
                    "keywords": ["名字"],
                    "after_messages": 1,
                    "ending_id": "ending",
                    "conditions": {"flags": {"honest": True}},
                    "effects": {
                        "set_flags": {"bell_spoke": True},
                        "grant_item": "钟灵残响",
                    },
                    "once": True,
                }
            ],
        },
    }
    return SandboxWorldConfig(
        world_id="test_story",
        name="问心钟",
        description="一段修仙文字冒险。",
        lore="问心钟停响的修仙夜。",
        opening_scene="问心钟突然停响。",
        player=graph["world"]["player"],
        npcs=[SandboxNPC(id="npc_ling", name="凌霜", role="守钟人", location="问心台")],
        metadata={"creator_graph": graph, "published_to_play": True},
    )


def test_player_runtime_traverses_branch_persists_and_unlocks_post_story(tmp_path: Path) -> None:
    runtime = PlayerStoryRuntime(PlayerSessionStore(tmp_path / "sessions"))
    world = make_world()

    started = runtime.start(world, session_id="player_one")
    assert started.node.id == "start"
    assert started.speaker and started.speaker.name == "凌霜"
    assert started.can_advance is True
    assert started.post_story_available is False

    choice = runtime.advance(world, "player_one")
    assert choice.node.id == "choice"
    assert [item.id for item in choice.choices] == ["tell_truth", "hide_truth"]
    with pytest.raises(ValueError, match="先选择"):
        runtime.advance(world, "player_one")

    truth = runtime.choose(world, "player_one", "tell_truth")
    assert truth.node.id == "truth"
    assert truth.flags["honest"] is True
    assert truth.player["stats"]["spirit"] == 3
    assert truth.history[-2].kind == "choice"

    restored = runtime.resume(world, "player_one")
    assert restored.node.id == "truth"
    ending = runtime.advance(world, "player_one")
    assert ending.node.id == "ending"
    assert ending.ended is True
    assert ending.post_story_available is True
    assert ending.post_story_characters[0].id == "npc_ling"

    session, events = runtime.record_post_story_exchange(
        world,
        "player_one",
        "这口钟真正的名字是什么？",
        "它叫无妄。",
        npc_id="npc_ling",
        npc_name="凌霜",
    )
    assert [event.id for event in events] == ["ask_bell_name"]
    assert session.flags["bell_spoke"] is True
    assert session.player["inventory"] == [{"name": "钟灵残响", "quantity": 1}]
    assert len(runtime.resume(world, "player_one").post_story_history) == 3

    _, repeated_events = runtime.record_post_story_exchange(
        world,
        "player_one",
        "再说一次它的名字。",
        "钟灵已经回应过你。",
        npc_id="npc_ling",
        npc_name="凌霜",
    )
    assert repeated_events == []


def test_player_runtime_rejects_post_story_before_ending_and_world_without_graph(tmp_path: Path) -> None:
    runtime = PlayerStoryRuntime(PlayerSessionStore(tmp_path / "sessions"))
    world = make_world()
    runtime.start(world, session_id="early")
    with pytest.raises(ValueError, match="尚未结束"):
        runtime.post_story_context(world, "early")

    graphless = SandboxWorldConfig(world_id="empty", name="空世界")
    with pytest.raises(ValueError, match="没有 Creator Graph"):
        runtime.start(graphless)


def test_player_runtime_rejects_unpublished_creator_graph(tmp_path: Path) -> None:
    runtime = PlayerStoryRuntime(PlayerSessionStore(tmp_path / "sessions"))
    world = make_world()
    world.metadata["published_to_play"] = False

    with pytest.raises(ValueError, match="尚未发布"):
        runtime.start(world)


def test_player_runtime_restarts_stale_session_when_updated_story_removed_current_node(tmp_path: Path) -> None:
    runtime = PlayerStoryRuntime(PlayerSessionStore(tmp_path / "sessions"))
    old_world = make_world()
    runtime.start(old_world, session_id="stale_reader")
    runtime.advance(old_world, "stale_reader")
    runtime.choose(old_world, "stale_reader", "tell_truth")

    updated_world = make_world()
    updated_graph = updated_world.metadata["creator_graph"]
    updated_graph["nodes"] = [node for node in updated_graph["nodes"] if node["id"] != "truth"]
    updated_graph["nodes"][1]["choices"] = [
        {"id": "continue", "text": "继续", "next": "ending", "conditions": {}, "effects": {}}
    ]

    resumed = runtime.resume(updated_world, "stale_reader")

    assert resumed.node.id == "start"
    assert "已自动从新故事开场重新开始" in resumed.recovery_notice
    stored = runtime.store.load(updated_world.world_id, "stale_reader")
    assert stored.current_node_id == "start"
    assert stored.visited_node_ids == ["start"]


def test_player_runtime_keeps_session_when_updated_story_still_has_current_node(tmp_path: Path) -> None:
    runtime = PlayerStoryRuntime(PlayerSessionStore(tmp_path / "sessions"))
    old_world = make_world()
    runtime.start(old_world, session_id="compatible_reader")
    runtime.advance(old_world, "compatible_reader")

    updated_world = make_world()
    updated_world.metadata["creator_graph"]["world"]["lore"] = "更新后的世界设定。"
    resumed = runtime.resume(updated_world, "compatible_reader")

    assert resumed.node.id == "choice"
    assert "已保留" in resumed.recovery_notice
    assert runtime.store.load(updated_world.world_id, "compatible_reader").graph_hash


class _FakeWorldStore:
    def __init__(self, world: SandboxWorldConfig) -> None:
        self.world = world

    def load(self, world_id: str) -> SandboxWorldConfig:
        if world_id != self.world.world_id:
            raise ValueError("not found")
        return self.world

    def list_worlds(self) -> list[WorldSummary]:
        return [WorldSummary(world_id=self.world.world_id, name=self.world.name)]


def test_player_api_routes_cover_start_resume_choice_and_ending(tmp_path: Path, monkeypatch) -> None:
    from app.api import shared
    from app.player_experience import routes as player_routes

    world = make_world()
    monkeypatch.setattr(shared, "world_store", _FakeWorldStore(world))
    monkeypatch.setattr(player_routes, "runtime", PlayerStoryRuntime(PlayerSessionStore(tmp_path / "sessions")))
    client = TestClient(app)

    listed = client.get("/api/player/worlds")
    assert listed.status_code == 200
    assert listed.json()[0]["world_id"] == "test_story"

    started = client.post(
        "/api/player/worlds/test_story/start",
        json={"session_id": "api_player", "restart": False},
    )
    assert started.status_code == 200
    assert started.json()["node"]["id"] == "start"

    choice = client.post("/api/player/worlds/test_story/advance", json={"session_id": "api_player"})
    assert choice.status_code == 200
    assert len(choice.json()["choices"]) == 2

    blocked = client.post("/api/player/worlds/test_story/advance", json={"session_id": "api_player"})
    assert blocked.status_code == 409

    chosen = client.post(
        "/api/player/worlds/test_story/choose",
        json={"session_id": "api_player", "choice_id": "tell_truth"},
    )
    assert chosen.status_code == 200
    assert chosen.json()["flags"]["honest"] is True

    ending = client.post("/api/player/worlds/test_story/advance", json={"session_id": "api_player"})
    assert ending.status_code == 200
    assert ending.json()["ended"] is True

    restored = client.get("/api/player/worlds/test_story/sessions/api_player")
    assert restored.status_code == 200
    assert restored.json()["node"]["id"] == "ending"
    assert restored.json()["post_story_available"] is True

    deleted = client.delete("/api/player/worlds/test_story/sessions/api_player")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}


def test_player_library_hides_unpublished_creator_drafts(monkeypatch) -> None:
    from app.api import shared

    draft = make_world()
    draft.metadata["published_to_play"] = False
    monkeypatch.setattr(shared, "world_store", _FakeWorldStore(draft))

    listed = TestClient(app).get("/api/player/worlds")

    assert listed.status_code == 200
    assert listed.json() == []


def test_player_api_rejects_direct_access_to_unpublished_creator_drafts(monkeypatch) -> None:
    from app.api import shared

    draft = make_world()
    draft.metadata["published_to_play"] = False
    monkeypatch.setattr(shared, "world_store", _FakeWorldStore(draft))
    client = TestClient(app)

    requests = [
        ("POST", "/api/player/worlds/test_story/start", {"session_id": "draft", "restart": False}),
        ("GET", "/api/player/worlds/test_story/sessions/draft", None),
        ("POST", "/api/player/worlds/test_story/advance", {"session_id": "draft"}),
        ("POST", "/api/player/worlds/test_story/choose", {"session_id": "draft", "choice_id": "tell_truth"}),
        (
            "POST",
            "/api/player/worlds/test_story/post-story/chat",
            {"session_id": "draft", "target_npc_id": "npc_ling", "message": "测试"},
        ),
        ("DELETE", "/api/player/worlds/test_story/sessions/draft", None),
    ]

    for method, path, payload in requests:
        response = client.request(method, path, json=payload)
        assert response.status_code == 404, (method, path, response.text)


def test_player_page_route_is_registered() -> None:
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    assert "/api/player/worlds/{world_id}/post-story/chat" in paths
    page = TestClient(app).get("/play")
    assert 'id="library-button"' in page.text
    assert 'id="world-picker-close"' in page.text
    assert 'id="library-shortcut"' in page.text
    assert 'href="/play"' in page.text
    assert 'id="ending-library-button"' in page.text


def test_post_story_chat_visits_selected_npc_location(tmp_path: Path, monkeypatch) -> None:
    from app.api import shared
    from app.player_experience import routes as player_routes

    world = make_world()
    world.npcs[0].location = "问心钟楼"
    player_runtime = PlayerStoryRuntime(PlayerSessionStore(tmp_path / "sessions"))
    player_runtime.start(world, session_id="after_story")
    player_runtime.advance(world, "after_story")
    player_runtime.choose(world, "after_story", "tell_truth")
    player_runtime.advance(world, "after_story")

    class RecordingAgent:
        def __init__(self) -> None:
            self.requests = []

        async def chat(self, request):
            self.requests.append(request)
            return SimpleNamespace(reply="钟声会替我们记住这一夜。", speaker={"id": "npc_ling", "name": "凌霜"})

    agent = RecordingAgent()
    monkeypatch.setattr(shared, "world_store", _FakeWorldStore(world))
    monkeypatch.setattr(player_routes, "runtime", player_runtime)
    monkeypatch.setattr(player_routes, "get_agent", lambda _world_id: agent)

    response = TestClient(app).post(
        "/api/player/worlds/test_story/post-story/chat",
        json={"session_id": "after_story", "target_npc_id": "npc_ling", "message": "钟声回来了吗？"},
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "钟声会替我们记住这一夜。"
    assert agent.requests[0].location == "问心钟楼"
    assert "固定剧情已经结束" in agent.requests[0].player_goal
