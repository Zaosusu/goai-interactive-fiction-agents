from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.agents.creator_assistant.compiler import CreatorGraphCompiler
from app.player_experience.schema import (
    PlayerCharacterView,
    PlayerChoiceView,
    PlayerHistoryEntry,
    PlayerNodeView,
    PostStoryEventView,
    PlayerSessionResponse,
    PlayerStorySession,
    PlayerWorldView,
)
from app.player_experience.store import PlayerSessionStore
from app.worlds.sandbox.models import SandboxWorldConfig


class PlayerStoryRuntime:
    """Deterministic GALGAME-style traversal over a Creator Graph."""

    def __init__(self, store: PlayerSessionStore | None = None) -> None:
        self.store = store or PlayerSessionStore()
        self.graph_compiler = CreatorGraphCompiler()

    def start(self, world: SandboxWorldConfig, session_id: str = "", restart: bool = False) -> PlayerSessionResponse:
        graph = self._graph(world)
        session_id = session_id.strip() or f"play_{uuid4().hex}"
        if not restart and self.store.exists(world.world_id, session_id):
            session, recovery_notice = self._load_compatible_session(world, graph, session_id)
            return self._response(world, graph, session, recovery_notice=recovery_notice)

        session = self._create_session(world, graph, session_id)
        return self._response(world, graph, session)

    def resume(self, world: SandboxWorldConfig, session_id: str) -> PlayerSessionResponse:
        graph = self._graph(world)
        session, recovery_notice = self._load_compatible_session(world, graph, session_id)
        return self._response(world, graph, session, recovery_notice=recovery_notice)

    def advance(self, world: SandboxWorldConfig, session_id: str) -> PlayerSessionResponse:
        graph = self._graph(world)
        session, recovery_notice = self._load_compatible_session(world, graph, session_id)
        if recovery_notice:
            return self._response(world, graph, session, recovery_notice=recovery_notice)
        current = self._node(graph, session.current_node_id)
        if session.ended:
            return self._response(world, graph, session)
        available = self._available_choices(current, session)
        if available:
            raise ValueError("当前剧情需要先选择一个选项。")
        target = str(current.get("next") or "")
        if not target:
            session.ended = current.get("type") == "ending"
            session.updated_at = _now()
            self.store.save(session)
            return self._response(world, graph, session)
        self._enter_node(graph, session, target)
        self.store.save(session)
        return self._response(world, graph, session)

    def choose(self, world: SandboxWorldConfig, session_id: str, choice_id: str) -> PlayerSessionResponse:
        graph = self._graph(world)
        session, recovery_notice = self._load_compatible_session(world, graph, session_id)
        if recovery_notice:
            return self._response(world, graph, session, recovery_notice=recovery_notice)
        if session.ended:
            raise ValueError("剧情已经结束。")
        current = self._node(graph, session.current_node_id)
        choice = next(
            (item for item in self._available_choices(current, session) if str(item.get("id") or "") == choice_id),
            None,
        )
        if choice is None:
            raise ValueError(f"当前不可选择该选项：{choice_id}")
        target = str(choice.get("next") or "")
        if not target:
            raise ValueError("该选项没有配置后续剧情。")
        player_name = str(session.player.get("name") or "玩家")
        session.history.append(
            PlayerHistoryEntry(
                kind="choice",
                node_id=current.get("id") or "",
                speaker_name=player_name,
                content=str(choice.get("text") or ""),
                created_at=_now(),
            )
        )
        self._apply_effects(session, choice.get("effects") or {})
        self._enter_node(graph, session, target)
        self.store.save(session)
        return self._response(world, graph, session)

    def reset(self, world_id: str, session_id: str) -> None:
        self.store.delete(world_id, session_id)

    def post_story_context(self, world: SandboxWorldConfig, session_id: str) -> PlayerSessionResponse:
        response = self.resume(world, session_id)
        if not response.ended:
            raise ValueError("固定剧情尚未结束，暂不能进入后日谈自由聊天。")
        if not response.post_story_available:
            raise ValueError("这个作品没有开启后日谈自由聊天。")
        return response

    def record_post_story_exchange(
        self,
        world: SandboxWorldConfig,
        session_id: str,
        message: str,
        reply: str,
        *,
        npc_id: str = "",
        npc_name: str = "",
    ) -> tuple[PlayerStorySession, list[PostStoryEventView]]:
        graph = self._graph(world)
        session, _ = self._load_compatible_session(world, graph, session_id)
        if not session.ended or not self._post_story_available(graph, session):
            raise ValueError("只有完成固定剧情后才能进行后日谈自由聊天。")

        now = _now()
        session.post_story_history.extend(
            [
                PlayerHistoryEntry(
                    kind="dialogue",
                    node_id=session.current_node_id,
                    speaker_name=str(session.player.get("name") or "玩家"),
                    content=message,
                    created_at=now,
                ),
                PlayerHistoryEntry(
                    kind="dialogue",
                    node_id=session.current_node_id,
                    speaker_id=npc_id,
                    speaker_name=npc_name or "NPC",
                    content=reply,
                    created_at=now,
                ),
            ]
        )
        triggered = self._trigger_post_story_events(graph, session, f"{message}\n{reply}")
        session.updated_at = _now()
        self.store.save(session)
        return session, triggered

    def _graph(self, world: SandboxWorldConfig) -> dict[str, Any]:
        graph = (world.metadata or {}).get("creator_graph")
        if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not graph["nodes"]:
            raise ValueError("这个世界没有 Creator Graph，暂时不能在文字冒险播放器中运行。")
        if (world.metadata or {}).get("published_to_play") is not True:
            raise ValueError("这个世界尚未发布，暂时不能在文字冒险播放器中运行。")
        normalized = self.graph_compiler.normalize(graph)
        report = self.graph_compiler.validate(normalized)
        if not report.valid:
            messages = "；".join(issue.message for issue in report.issues if issue.severity == "error")
            raise ValueError(f"Creator Graph 无法运行：{messages}")
        return normalized

    def _create_session(
        self,
        world: SandboxWorldConfig,
        graph: dict[str, Any],
        session_id: str,
    ) -> PlayerStorySession:
        start_id = "start" if self._node(graph, "start", required=False) else str(graph["nodes"][0]["id"])
        now = _now()
        session = PlayerStorySession(
            session_id=session_id,
            world_id=world.world_id,
            graph_hash=_graph_hash(graph),
            current_node_id=start_id,
            player=copy.deepcopy(graph.get("world", {}).get("player") or world.player or {}),
            started_at=now,
            updated_at=now,
        )
        self._enter_node(graph, session, start_id)
        self.store.save(session)
        return session

    def _load_compatible_session(
        self,
        world: SandboxWorldConfig,
        graph: dict[str, Any],
        session_id: str,
    ) -> tuple[PlayerStorySession, str]:
        session = self.store.load(world.world_id, session_id)
        current_hash = _graph_hash(graph)
        current_node_exists = self._node(graph, session.current_node_id, required=False) is not None
        if current_node_exists:
            if session.graph_hash != current_hash:
                session.graph_hash = current_hash
                session.updated_at = _now()
                self.store.save(session)
                return session, "剧情内容已更新，已保留你当前仍然有效的进度。"
            return session, ""

        stale_node_id = session.current_node_id
        restarted = self._create_session(world, graph, session_id)
        return restarted, f"剧情已更新，旧存档节点 {stale_node_id} 已失效，已自动从新故事开场重新开始。"

    def _enter_node(self, graph: dict[str, Any], session: PlayerStorySession, node_id: str) -> None:
        node = self._node(graph, node_id)
        if not self._conditions_match(node.get("conditions") or {}, session):
            raise ValueError(f"尚未满足进入剧情节点 {node_id} 的条件。")
        session.current_node_id = node_id
        session.visited_node_ids.append(node_id)
        self._apply_effects(session, node.get("effects") or {})
        character = self._character(graph, str(node.get("character") or ""))
        speaker_name = str(character.get("name") or "") if character else ""
        session.history.append(
            PlayerHistoryEntry(
                kind="dialogue" if character else "narration",
                node_id=node_id,
                speaker_id=str(character.get("id") or "") if character else "",
                speaker_name=speaker_name,
                content=str(node.get("content") or node.get("title") or ""),
                created_at=_now(),
            )
        )
        session.ended = node.get("type") == "ending" and not node.get("next") and not node.get("choices")
        session.updated_at = _now()

    def _apply_effects(self, session: PlayerStorySession, effects: dict[str, Any]) -> None:
        flags = effects.get("set_flags")
        if isinstance(flags, dict):
            session.flags.update(copy.deepcopy(flags))
        player_patch = effects.get("set_player")
        if isinstance(player_patch, dict):
            _deep_merge(session.player, player_patch)
        increases = effects.get("increase_player")
        if isinstance(increases, dict):
            for path, amount in increases.items():
                _increase_path(session.player, str(path), amount)
        item = effects.get("grant_item") or effects.get("add_item")
        if item:
            _grant_item(session.player, item)
        remove_item = effects.get("remove_item")
        if remove_item:
            _remove_item(session.player, remove_item)

    def _response(
        self,
        world: SandboxWorldConfig,
        graph: dict[str, Any],
        session: PlayerStorySession,
        *,
        recovery_notice: str = "",
    ) -> PlayerSessionResponse:
        node = self._node(graph, session.current_node_id)
        character = self._character(graph, str(node.get("character") or ""))
        choices = self._available_choices(node, session)
        location = str(
            (node.get("effects") or {}).get("set_player", {}).get("location")
            or session.player.get("location")
            or node.get("title")
            or ""
        )
        authoring = node.get("authoring") if isinstance(node.get("authoring"), dict) else {}
        post_story_characters = [
            PlayerCharacterView(
                id=str(item.get("id") or ""),
                name=str(item.get("name") or ""),
                role=str(item.get("role") or ""),
                portrait=str(item.get("portrait") or ""),
            )
            for item in graph.get("characters", [])
            if isinstance(item, dict) and str(item.get("id") or "")
        ]
        objective = ""
        if "【当前目标】" in str(node.get("content") or ""):
            objective = str(node.get("content") or "").split("【当前目标】", 1)[-1].strip()
        return PlayerSessionResponse(
            world=PlayerWorldView(
                world_id=world.world_id,
                name=world.name,
                description=world.description or world.lore,
                player_name=str(session.player.get("name") or "玩家"),
            ),
            session_id=session.session_id,
            node=PlayerNodeView(
                id=str(node.get("id") or ""),
                type=str(node.get("type") or "story"),
                title=str(node.get("title") or ""),
                content=str(node.get("content") or ""),
                location=location,
                background=str(node.get("background") or ""),
                objective=objective or str(authoring.get("purpose") or ""),
            ),
            speaker=PlayerCharacterView(
                id=str(character.get("id") or ""),
                name=str(character.get("name") or ""),
                role=str(character.get("role") or ""),
                portrait=str(character.get("portrait") or ""),
            )
            if character
            else None,
            choices=[
                PlayerChoiceView(
                    id=str(choice.get("id") or ""),
                    text=str(choice.get("text") or ""),
                    consequence_summary=str(choice.get("consequence_summary") or ""),
                )
                for choice in choices
            ],
            can_advance=not session.ended and not choices and bool(node.get("next")),
            ended=session.ended,
            player=copy.deepcopy(session.player),
            flags=copy.deepcopy(session.flags),
            history=session.history[-300:],
            post_story_available=self._post_story_available(graph, session),
            post_story_characters=post_story_characters if session.ended else [],
            post_story_history=session.post_story_history[-300:],
            saved_at=session.updated_at,
            recovery_notice=recovery_notice,
        )

    def _post_story_available(self, graph: dict[str, Any], session: PlayerStorySession) -> bool:
        config = graph.get("post_story") if isinstance(graph.get("post_story"), dict) else {}
        has_characters = any(str(item.get("id") or "") for item in graph.get("characters", []) if isinstance(item, dict))
        return session.ended and has_characters and config.get("enabled", True) is not False

    def _trigger_post_story_events(
        self,
        graph: dict[str, Any],
        session: PlayerStorySession,
        conversation_text: str,
    ) -> list[PostStoryEventView]:
        config = graph.get("post_story") if isinstance(graph.get("post_story"), dict) else {}
        events = config.get("events") if isinstance(config.get("events"), list) else []
        message_count = len([item for item in session.post_story_history if item.speaker_id == ""])
        normalized_text = conversation_text.casefold()
        triggered: list[PostStoryEventView] = []
        for index, event in enumerate(events, start=1):
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("id") or f"post_story_event_{index}")
            if event.get("once", True) is not False and event_id in session.triggered_event_ids:
                continue
            if str(event.get("ending_id") or "") not in {"", session.current_node_id}:
                continue
            if message_count < int(event.get("after_messages") or 0):
                continue
            keywords = event.get("keywords") if isinstance(event.get("keywords"), list) else []
            if keywords and not any(str(keyword).casefold() in normalized_text for keyword in keywords if str(keyword).strip()):
                continue
            if not self._conditions_match(event.get("conditions") or {}, session):
                continue
            self._apply_effects(session, event.get("effects") or {})
            session.triggered_event_ids.append(event_id)
            view = PostStoryEventView(
                id=event_id,
                title=str(event.get("title") or "后日谈事件"),
                description=str(event.get("description") or ""),
            )
            session.post_story_history.append(
                PlayerHistoryEntry(
                    kind="system",
                    node_id=session.current_node_id,
                    speaker_name="事件",
                    content=" · ".join(item for item in [view.title, view.description] if item),
                    created_at=_now(),
                )
            )
            triggered.append(view)
        return triggered

    def _available_choices(self, node: dict[str, Any], session: PlayerStorySession) -> list[dict[str, Any]]:
        return [
            choice
            for choice in node.get("choices", [])
            if isinstance(choice, dict) and self._conditions_match(choice.get("conditions") or {}, session)
        ]

    def _conditions_match(self, conditions: dict[str, Any], session: PlayerStorySession) -> bool:
        if not conditions:
            return True
        checks: list[bool] = []
        if isinstance(conditions.get("flags"), dict):
            checks.append(all(session.flags.get(key) == value for key, value in conditions["flags"].items()))
        if isinstance(conditions.get("player"), dict):
            checks.append(all(_get_path(session.player, key) == value for key, value in conditions["player"].items()))
        if isinstance(conditions.get("stats"), dict):
            checks.append(all(_numeric_match(_get_path(session.player, f"stats.{key}"), rule) for key, rule in conditions["stats"].items()))
        if conditions.get("visited"):
            required = conditions["visited"] if isinstance(conditions["visited"], list) else [conditions["visited"]]
            checks.append(all(str(node_id) in session.visited_node_ids for node_id in required))
        if conditions.get("items"):
            required = conditions["items"] if isinstance(conditions["items"], list) else [conditions["items"]]
            owned = {_item_name(item) for item in session.player.get("inventory", [])}
            checks.append(all(str(item) in owned for item in required))
        mode = str(conditions.get("mode") or "all").lower()
        return any(checks) if mode == "any" else all(checks) if checks else True

    def _ensure_current_node(self, graph: dict[str, Any], session: PlayerStorySession) -> None:
        self._node(graph, session.current_node_id)

    def _node(self, graph: dict[str, Any], node_id: str, required: bool = True) -> dict[str, Any] | None:
        node = next((item for item in graph.get("nodes", []) if str(item.get("id") or "") == node_id), None)
        if node is None and required:
            raise ValueError(f"剧情节点不存在：{node_id}")
        return node

    def _character(self, graph: dict[str, Any], character_id: str) -> dict[str, Any] | None:
        if not character_id:
            return None
        return next(
            (item for item in graph.get("characters", []) if str(item.get("id") or "") == character_id),
            None,
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _graph_hash(graph: dict[str, Any]) -> str:
    payload = json.dumps(graph, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _get_path(source: dict[str, Any], path: str) -> Any:
    current: Any = source
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _set_path(source: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = source
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _increase_path(source: dict[str, Any], path: str, amount: Any) -> None:
    current = _get_path(source, path)
    try:
        next_value = float(current or 0) + float(amount)
        if next_value.is_integer():
            next_value = int(next_value)
    except (TypeError, ValueError):
        return
    _set_path(source, path, next_value)


def _numeric_match(value: Any, rule: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if not isinstance(rule, dict):
        return number >= float(rule)
    if "min" in rule and number < float(rule["min"]):
        return False
    if "max" in rule and number > float(rule["max"]):
        return False
    if ">=" in rule and number < float(rule[">="]):
        return False
    if "<=" in rule and number > float(rule["<="]):
        return False
    if "eq" in rule and number != float(rule["eq"]):
        return False
    return True


def _grant_item(player: dict[str, Any], raw: Any) -> None:
    item = raw if isinstance(raw, dict) else {"name": str(raw), "quantity": 1}
    name = str(item.get("name") or item.get("id") or "").strip()
    if not name:
        return
    inventory = player.setdefault("inventory", [])
    existing = next((candidate for candidate in inventory if _item_name(candidate) == name), None)
    quantity = int(item.get("quantity") or 1)
    if isinstance(existing, dict):
        existing["quantity"] = int(existing.get("quantity") or 1) + quantity
    elif existing is None:
        inventory.append({"name": name, "quantity": quantity})


def _remove_item(player: dict[str, Any], raw: Any) -> None:
    name = str(raw.get("name") if isinstance(raw, dict) else raw)
    player["inventory"] = [item for item in player.get("inventory", []) if _item_name(item) != name]


def _item_name(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("name") or item.get("id") or "")
    return ""
