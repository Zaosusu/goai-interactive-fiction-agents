import copy
from typing import Any

from app.core.models import AgentSessionState, WorldActionRequest, WorldActionResponse
from app.core.tools import ToolRegistry
from app.worlds.sandbox.completion import _matches_completion, evaluate_task_completions
from app.worlds.sandbox.guardrails import WorldRuntimeGuardrail
from app.worlds.sandbox.models import SandboxWorldConfig


class SandboxActionService:
    def __init__(self, config: SandboxWorldConfig) -> None:
        self.config = config
        self.guardrail = WorldRuntimeGuardrail(config)
        self.tools = ToolRegistry()
        self.tools.register("advance_scene", "推进当前剧情场景", self.advance_scene)
        self.tools.register("complete_task", "完成或更新一个任务", self.complete_task)
        self.tools.register("set_flag", "设置一个世界状态标记", self.set_flag)
        self.tools.register("update_relation", "更新 NPC 与玩家的关系", self.update_relation)
        self.tools.register("switch_npc", "切换当前对话 NPC", self.switch_npc)
        self.tools.register("move_player", "移动玩家到指定地点", self.move_player)
        self.tools.register("inspect_location", "观察当前地点并列出可追踪线索", self.inspect_location)
        for action in config.actions:
            if action.id not in self.tools.available_actions():
                self.tools.register(
                    action.id,
                    action.description or action.label,
                    lambda state, payload, action_id=action.id: self.configured_action(state, action_id, payload),
                )

    def handle(self, state: AgentSessionState, request: WorldActionRequest) -> WorldActionResponse:
        response = self.tools.run(state, request)
        if not response.state:
            response.state = copy.deepcopy(state.world_state)
        if not response.player:
            response.player = copy.deepcopy(state.world_state.get("player", {}))
        if not response.nearby_npcs:
            response.nearby_npcs = self._nearby_npcs(state)
        if not response.active_entity:
            response.active_entity = response.nearby_npcs[0] if response.nearby_npcs else None
        if not response.speaker:
            response.speaker = response.nearby_npcs[0] if response.nearby_npcs else None
        passive_actions = {"move_player", "switch_npc", "inspect_location"}
        if not response.quest_progress and request.action not in passive_actions:
            response.quest_progress = state.quest_progress
        if not response.suggested_actions:
            response.suggested_actions = self.default_actions(state)
        return response

    def advance_scene(self, state: AgentSessionState, payload: dict[str, Any]) -> WorldActionResponse:
        scene = payload.get("scene") or payload.get("description") or "剧情向下一幕推进。"
        state.world_state["scene"] = scene
        state.world_state["turn"] = int(state.world_state.get("turn", 0)) + 1
        state.quest_progress = f"场景推进：{scene}"
        state.add_memory(f"世界事件：{scene}", 0.65)
        return self._response("advance_scene", state.quest_progress, state)

    def complete_task(self, state: AgentSessionState, payload: dict[str, Any]) -> WorldActionResponse:
        task_id = payload.get("task_id") or payload.get("id")
        title = payload.get("title") or "当前任务"
        tasks = state.world_state.setdefault("tasks", [])
        changed = False
        for task in tasks:
            if task_id and task.get("id") == task_id:
                task["status"] = payload.get("status", "done")
                changed = True
                title = task.get("title", title)
        if not changed:
            tasks.append({"id": task_id or f"task_{len(tasks) + 1}", "title": title, "status": "done"})
        state.quest_progress = f"任务更新：{title} 已完成。"
        state.add_memory(state.quest_progress, 0.75)
        return self._response("complete_task", state.quest_progress, state)

    def set_flag(self, state: AgentSessionState, payload: dict[str, Any]) -> WorldActionResponse:
        key = str(payload.get("key") or "flag")
        value = payload.get("value", True)
        state.world_state.setdefault("flags", {})[key] = value
        state.quest_progress = f"世界标记已更新：{key}={value}"
        state.add_memory(state.quest_progress, 0.55)
        return self._response("set_flag", state.quest_progress, state)

    def update_relation(self, state: AgentSessionState, payload: dict[str, Any]) -> WorldActionResponse:
        npc_id = str(payload.get("npc_id") or "unknown")
        delta = float(payload.get("delta", 0.0))
        relations = state.world_state.setdefault("relations", {})
        relations[npc_id] = max(-1.0, min(1.0, float(relations.get(npc_id, 0.0)) + delta))
        state.quest_progress = f"关系更新：{npc_id} -> {relations[npc_id]:.2f}"
        state.add_memory(state.quest_progress, 0.55)
        return self._response("update_relation", state.quest_progress, state)

    def switch_npc(self, state: AgentSessionState, payload: dict[str, Any]) -> WorldActionResponse:
        npc_id = str(payload.get("npc_id") or payload.get("id") or "")
        npcs = state.world_state.get("npcs", [])
        npc = next((item for item in npcs if item.get("id") == npc_id), None)
        if npc is None:
            return self._response("switch_npc", f"找不到 NPC：{npc_id}", state)
        state.world_state["active_npc_id"] = npc_id
        narration = f"当前对话对象切换为：{npc.get('name', npc_id)}"
        return self._response("switch_npc", narration, state, update_progress=False)

    def move_player(self, state: AgentSessionState, payload: dict[str, Any]) -> WorldActionResponse:
        location = str(payload.get("location") or "").strip()
        if not location:
            return self._response("move_player", "请输入要前往的地点。", state)
        if location in self.guardrail.action_ids():
            return self.configured_action(state, location, {"source": "location_input_as_action"})
        normalized_location = self.guardrail.normalize_location(location)
        if not normalized_location:
            return self._response("move_player", self.guardrail.location_rejection(location), state, update_progress=False)
        location = normalized_location
        state.world_state.setdefault("player", {})["location"] = location
        nearby_npcs = self._nearby_npcs(state)
        if nearby_npcs:
            state.world_state["active_npc_id"] = str(nearby_npcs[0].get("id") or "")
            npc_names = "、".join(str(npc.get("name") or npc.get("id") or "NPC") for npc in nearby_npcs)
            narration = f"你前往：{location}。当前位置可对话 NPC：{npc_names}"
        else:
            state.world_state.pop("active_npc_id", None)
            narration = f"你前往：{location}。当前位置暂无已知 NPC。"
        completed = evaluate_task_completions(state, narration)
        if completed:
            narration = f"{narration}\n任务完成：{'、'.join(completed)}。"
        state.add_memory(narration, 0.45)
        return self._response("move_player", narration, state, update_progress=False)

    def inspect_location(self, state: AgentSessionState, payload: dict[str, Any]) -> WorldActionResponse:
        location = str(payload.get("location") or state.world_state.get("player", {}).get("location") or "").strip()
        query = str(payload.get("query") or "").strip()
        matched_action = self._match_configured_action(query, state)
        if matched_action:
            return self.configured_action(state, matched_action.id, {"source": "natural_language", "query": query})

        nearby_npcs = self._nearby_npcs(state)
        pending_tasks = [
            task
            for task in state.world_state.get("tasks", [])
            if task.get("status", "pending") not in {"done", "skipped"}
        ]
        npc_locations = []
        for npc in state.world_state.get("npcs", []):
            name = npc.get("name") or npc.get("id") or "NPC"
            npc_location = npc.get("location") or "未知地点"
            npc_locations.append(f"{name}：{npc_location}")

        if nearby_npcs:
            npc_names = "、".join(str(npc.get("name") or npc.get("id") or "NPC") for npc in nearby_npcs)
            narration = f"你观察了{location or '当前位置'}。这里可对话 NPC：{npc_names}。"
        else:
            first_task = pending_tasks[0] if pending_tasks else None
            task_text = "这里暂时没有人回应你。"
            next_action_hint = self._next_action_hint(state, first_task)
            locations_text = "；".join(npc_locations[:6]) if npc_locations else "暂无已知 NPC 地点。"
            narration = f"你观察了{location or '当前位置'}，{task_text}{next_action_hint} 已知人物位置：{locations_text}"
        completed = evaluate_task_completions(state, narration)
        if completed:
            narration = f"{narration}\n任务完成：{'、'.join(completed)}。"
        state.add_memory(narration, 0.45)
        return self._response("inspect_location", narration, state, update_progress=False)

    def _next_action_hint(self, state: AgentSessionState, task: dict | None) -> str:
        if not task:
            return ""
        task_id = str(task.get("id") or "")
        npcs = {str(npc.get("id") or ""): npc for npc in state.world_state.get("npcs", []) if isinstance(npc, dict)}
        for action in self.config.actions:
            effect = action.effect if isinstance(action.effect, dict) else {}
            if str(effect.get("complete_task") or "") != task_id:
                continue
            npc = npcs.get(str(effect.get("active_npc_id") or ""))
            set_player = effect.get("set_player") if isinstance(effect.get("set_player"), dict) else {}
            location = set_player.get("location") or (npc.get("location") if npc else "")
            npc_name = npc.get("name") if npc else ""
            parts = []
            if location:
                parts.append(f"建议前往：{location}。")
            if npc_name:
                parts.append(f"可找：{npc_name}。")
            if action.label:
                parts.append(f"可尝试：{action.label}。")
            return "".join(parts)
        return ""

    def _match_configured_action(self, query: str, state: AgentSessionState) -> Any | None:
        if not query:
            return None
        query_terms = self._terms(query)
        if not query_terms:
            return None
        player_location = str(state.world_state.get("player", {}).get("location") or "").strip()
        best = None
        best_score = 0
        for action in self.config.actions:
            effect = action.effect if isinstance(action.effect, dict) else {}
            target_location = str((effect.get("set_player") or {}).get("location") or player_location).strip()
            if target_location and player_location and target_location != player_location:
                continue
            text = " ".join(
                str(part or "")
                for part in [
                    action.label,
                    action.description,
                    effect.get("scene"),
                    effect.get("complete_task"),
                ]
            )
            action_terms = self._terms(text)
            score = len(query_terms & action_terms)
            if score > best_score:
                best = action
                best_score = score
        return best if best_score >= 1 else None

    def _terms(self, text: str) -> set[str]:
        source = str(text or "").lower()
        terms = set()
        for token in ["熨", "熨烫", "衣服", "服装", "演出服", "整理", "行头", "练习", "训练", "舞蹈", "报名表", "报到", "登台", "表演"]:
            if token in source:
                terms.add(token)
        return terms

    def configured_action(
        self,
        state: AgentSessionState,
        action_id: str,
        payload: dict[str, Any],
    ) -> WorldActionResponse:
        action = next((item for item in self.config.actions if item.id == action_id), None)
        label = action.label if action else action_id
        effect = action.effect if action else {}
        self._apply_effect(state, effect)
        state.world_state.setdefault("custom_events", []).append(
            {"action_id": action_id, "action": label, "payload": payload, "effect": effect}
        )
        scene = effect.get("scene") if isinstance(effect, dict) else None
        state.quest_progress = scene or f"执行动作：{label}"
        completed = evaluate_task_completions(state, f"{label}\n{scene or ''}")
        explicit_task = effect.get("complete_task") if isinstance(effect, dict) else None
        if explicit_task and not completed:
            task = next((item for item in state.world_state.get("tasks", []) if item.get("id") == explicit_task), None)
            if task and task.get("status") == "done":
                completed = [str(task.get("title") or task.get("id") or "任务")]
        if completed:
            state.quest_progress = f"{state.quest_progress}\n任务完成：{'、'.join(completed)}。"
        state.add_memory(f"玩家执行了世界动作：{label}", 0.6)
        return self._response(action_id, state.quest_progress, state)

    def default_actions(self, state: AgentSessionState) -> list[str]:
        player_location = str(state.world_state.get("player", {}).get("location") or "").strip()
        nearby_npcs = self._nearby_npcs(state)
        pending_tasks = [
            task
            for task in state.world_state.get("tasks", [])
            if task.get("status", "pending") not in {"done", "skipped"}
        ]
        clues: list[str] = []
        if nearby_npcs:
            names = "、".join(str(npc.get("name") or npc.get("id") or "NPC") for npc in nearby_npcs)
            clues.append(f"当前地点可交谈：{names}")
        elif player_location:
            clues.append(f"{player_location} 暂无已知 NPC，可以观察四周或换个地点。")
        if pending_tasks:
            clues.append("继续搜证、盘问人物，等证词和物证能相互印证时再推进指认。")
        if not clues:
            clues.append("暂无新线索。可以询问当前 NPC：我接下来该做什么？")
        return clues[:4]

    def _apply_effect(self, state: AgentSessionState, effect: dict[str, Any]) -> None:
        if not isinstance(effect, dict):
            return

        player_patch = effect.get("set_player")
        if isinstance(player_patch, dict):
            state.world_state.setdefault("player", {}).update(player_patch)

        player_increase = effect.get("increase_player")
        if isinstance(player_increase, dict):
            player = state.world_state.setdefault("player", {})
            for path, delta in player_increase.items():
                self._increase_path(player, str(path), delta)

        flags = effect.get("set_flags")
        if isinstance(flags, dict):
            state.world_state.setdefault("flags", {}).update(flags)

        active_npc_id = effect.get("active_npc_id")
        if active_npc_id:
            state.world_state["active_npc_id"] = str(active_npc_id)

        scene = effect.get("scene")
        if scene:
            state.world_state["scene"] = str(scene)
            state.world_state["turn"] = int(state.world_state.get("turn", 0)) + 1

        complete_task = effect.get("complete_task")
        if complete_task:
            for task in state.world_state.setdefault("tasks", []):
                if task.get("id") == complete_task:
                    completion = task.get("completion") if isinstance(task.get("completion"), dict) else {}
                    if not completion or _matches_completion(state, completion, str(scene or "")):
                        task["status"] = "done"

    def _active_npc(self, state: AgentSessionState) -> dict[str, Any] | None:
        active_id = state.world_state.get("active_npc_id")
        npcs = state.world_state.get("npcs", [])
        if active_id:
            return next((npc for npc in npcs if npc.get("id") == active_id), None)
        return npcs[0] if npcs else None

    def _response(
        self,
        action: str,
        narration: str,
        state: AgentSessionState,
        update_progress: bool = True,
    ) -> WorldActionResponse:
        nearby_npcs = self._nearby_npcs(state)
        speaker = nearby_npcs[0] if nearby_npcs else None
        return WorldActionResponse(
            action=action,
            narration=narration,
            state=copy.deepcopy(state.world_state),
            player=copy.deepcopy(state.world_state.get("player", {})),
            active_entity=copy.deepcopy(speaker),
            speaker=copy.deepcopy(speaker),
            npcs=copy.deepcopy(state.world_state.get("npcs", [])),
            nearby_npcs=copy.deepcopy(nearby_npcs),
            quest_progress=state.quest_progress if update_progress else "",
            suggested_actions=self.default_actions(state),
        )

    def _nearby_npcs(self, state: AgentSessionState) -> list[dict[str, Any]]:
        location = str(state.world_state.get("player", {}).get("location") or "").strip()
        if not location:
            return []
        return [
            npc
            for npc in state.world_state.get("npcs", [])
            if str(npc.get("location") or "").strip() == location
        ]

    def _increase_path(self, target: dict[str, Any], path: str, delta: Any) -> None:
        parts = [part for part in path.split(".") if part]
        if not parts:
            return
        current = target
        for part in parts[:-1]:
            next_value = current.get(part)
            if not isinstance(next_value, dict):
                next_value = {}
                current[part] = next_value
            current = next_value
        leaf = parts[-1]
        try:
            if isinstance(delta, dict):
                target = delta.get("min")
                for key in [">=", ">", "eq", "=="]:
                    if target is None and key in delta:
                        target = delta[key]
                if target is not None:
                    current[leaf] = max(float(current.get(leaf, 0) or 0), float(target))
                return
            current[leaf] = float(current.get(leaf, 0)) + float(delta)
        except (TypeError, ValueError):
            current[leaf] = delta
