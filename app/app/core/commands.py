from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.models import AgentLLMOutput, NoneCommand, NpcCommand, command_to_dict, AgentSessionState, WorldAdapter


DEFAULT_ALLOWED_COMMANDS = {"none"}


@dataclass(frozen=True)
class CommandValidationResult:
    valid: bool
    command: NpcCommand
    errors: list[str] = field(default_factory=list)


class CommandValidator:
    def validate(
        self,
        adapter: WorldAdapter,
        state: AgentSessionState,
        output: AgentLLMOutput,
    ) -> CommandValidationResult:
        command = output.command or NoneCommand()
        data = command_to_dict(command)
        name = str(data.get("name") or "none").strip() or "none"
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        allowed = self._allowed_commands(adapter)
        errors: list[str] = []

        if name not in allowed:
            errors.append(f"Command is not allowed in this world: {name}")
            return self._invalid(errors)

        normalized = command
        if name == "none":
            return CommandValidationResult(valid=True, command=normalized)

        if name == "set_player":
            patch = args.get("patch")
            if not isinstance(patch, dict) or not patch:
                errors.append("set_player requires non-empty args.patch object")
            return self._result(normalized, errors)

        if name == "grant_item":
            item = str(args.get("item") or args.get("name") or "").strip()
            if not item:
                errors.append("grant_item requires args.item")
            return self._result(normalized, errors)

        if name == "complete_task":
            task_id = str(args.get("task_id") or "").strip()
            task_ids = {str(task.get("id")) for task in state.world_state.get("tasks", [])}
            if not task_id:
                errors.append("complete_task requires args.task_id")
            elif task_ids and task_id not in task_ids:
                errors.append(f"Unknown task_id: {task_id}")
            return self._result(normalized, errors)

        if name == "switch_npc":
            npc_id = str(args.get("npc_id") or "").strip()
            npc_ids = {str(npc.get("id")) for npc in state.world_state.get("npcs", [])}
            if not npc_id:
                errors.append("switch_npc requires args.npc_id")
            elif npc_ids and npc_id not in npc_ids:
                errors.append(f"Unknown npc_id: {npc_id}")
            return self._result(normalized, errors)

        if name == "set_flag":
            if not str(args.get("key") or "").strip():
                errors.append("set_flag requires args.key")
            return self._result(normalized, errors)

        if name == "run_world_action":
            action_id = str(args.get("action_id") or "").strip()
            action_ids = set(self._world_action_ids(adapter))
            if not action_id:
                errors.append("run_world_action requires args.action_id")
            elif action_ids and action_id not in action_ids:
                errors.append(f"Unknown action_id: {action_id}")
            return self._result(normalized, errors)

        return CommandValidationResult(valid=True, command=normalized)

    def _allowed_commands(self, adapter: WorldAdapter) -> set[str]:
        method = getattr(adapter, "allowed_commands", None)
        if callable(method):
            return set(method())
        return DEFAULT_ALLOWED_COMMANDS

    def _world_action_ids(self, adapter: WorldAdapter) -> list[str]:
        method = getattr(adapter, "world_action_ids", None)
        if callable(method):
            return list(method())
        return []

    def _result(self, command: NpcCommand, errors: list[str]) -> CommandValidationResult:
        if errors:
            return self._invalid(errors)
        return CommandValidationResult(valid=True, command=command)

    def _invalid(self, errors: list[str]) -> CommandValidationResult:
        return CommandValidationResult(valid=False, command=NoneCommand(), errors=errors)
