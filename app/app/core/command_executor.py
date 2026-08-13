from __future__ import annotations

import json
from typing import Any

from app.core.models import AgentLLMOutput, AgentSessionState, WorldActionRequest, WorldAdapter, command_to_dict
from app.worlds.sandbox.completion import _matches_completion


class CommandExecutor:
    def execute(
        self,
        adapter: WorldAdapter,
        state: AgentSessionState,
        output: AgentLLMOutput,
    ) -> None:
        command = command_to_dict(output.command)
        name = str(command.get("name") or "none")
        args = command.get("args") if isinstance(command.get("args"), dict) else {}

        if name == "none":
            return
        if name == "set_player":
            self._set_player(state, args)
            return
        if name == "grant_item":
            self._grant_item(state, args)
            return
        if name == "complete_task":
            self._complete_task(state, args)
            return
        if name == "switch_npc":
            self._switch_npc(state, args)
            return
        if name == "set_flag":
            self._set_flag(state, args)
            return
        if name == "run_world_action":
            self._run_world_action(adapter, state, args)
            return

    def _set_player(self, state: AgentSessionState, args: dict[str, Any]) -> None:
        patch = args.get("patch")
        if not isinstance(patch, dict):
            return
        state.world_state.setdefault("player", {}).update(patch)
        state.add_memory(f"系统执行命令 set_player：{json.dumps(patch, ensure_ascii=False)}", 0.75)

    def _grant_item(self, state: AgentSessionState, args: dict[str, Any]) -> None:
        item = str(args.get("item") or args.get("name") or "").strip()
        if not item:
            return
        quantity = args.get("quantity", 1)
        player = state.world_state.setdefault("player", {})
        inventory = player.setdefault("inventory", [])
        if not isinstance(inventory, list):
            inventory = []
            player["inventory"] = inventory
        existing = next(
            (
                entry
                for entry in inventory
                if (entry == item) or (isinstance(entry, dict) and str(entry.get("name") or entry.get("id") or entry.get("label") or "") == item)
            ),
            None,
        )
        if existing is None:
            inventory.append({"name": item, "quantity": quantity})
        elif isinstance(existing, dict):
            try:
                existing["quantity"] = float(existing.get("quantity", 1)) + float(quantity or 1)
            except (TypeError, ValueError):
                existing["quantity"] = quantity
        reason = str(args.get("reason") or "NPC给予")
        state.add_memory(f"玩家获得道具：{item}{f'（{reason}）' if reason else ''}", 0.75)

    def _complete_task(self, state: AgentSessionState, args: dict[str, Any]) -> None:
        task_id = str(args.get("task_id") or "")
        status = str(args.get("status") or "done")
        for task in state.world_state.setdefault("tasks", []):
            if task.get("id") == task_id:
                completion = task.get("completion") if isinstance(task.get("completion"), dict) else {}
                if status == "done" and completion and not _matches_completion(state, completion, ""):
                    state.add_memory(f"系统拒绝 complete_task：{task_id} 尚未满足 completion 条件", 0.75)
                    break
                task["status"] = status
                state.add_memory(f"系统执行命令 complete_task：{task_id}", 0.75)
                break

    def _switch_npc(self, state: AgentSessionState, args: dict[str, Any]) -> None:
        npc_id = str(args.get("npc_id") or "")
        if any(npc.get("id") == npc_id for npc in state.world_state.get("npcs", [])):
            state.world_state["active_npc_id"] = npc_id

    def _set_flag(self, state: AgentSessionState, args: dict[str, Any]) -> None:
        key = str(args.get("key") or "")
        if key:
            state.world_state.setdefault("flags", {})[key] = args.get("value", True)

    def _run_world_action(
        self,
        adapter: WorldAdapter,
        state: AgentSessionState,
        args: dict[str, Any],
    ) -> None:
        action_id = str(args.get("action_id") or "")
        if not action_id:
            return
        payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        response = adapter.handle_world_action(
            state,
            WorldActionRequest(action=action_id, payload={"source": "llm_command", **payload}),
        )
        if response.quest_progress:
            state.quest_progress = response.quest_progress
