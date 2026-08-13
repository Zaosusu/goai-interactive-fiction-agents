from __future__ import annotations

from dataclasses import dataclass, field

from app.worlds.sandbox.guardrails import split_location_values
from app.worlds.sandbox.models import SandboxAction, SandboxNPC, SandboxTask, SandboxWorldConfig


@dataclass
class WorldValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SandboxWorldValidator:
    def validate(self, config: SandboxWorldConfig) -> WorldValidationResult:
        errors: list[str] = []
        warnings: list[str] = []

        if not config.world_id:
            errors.append("world_id is required")
        if not config.name:
            errors.append("name is required")
        if not config.opening_scene:
            warnings.append("opening_scene is empty")
        if not isinstance(config.player, dict) or not config.player:
            errors.append("player must be a non-empty object")
        else:
            for key in ["name", "location"]:
                if not config.player.get(key):
                    warnings.append(f"player.{key} is missing")

        self._validate_npcs(config, errors, warnings)
        self._validate_tasks(config, errors, warnings)
        self._validate_actions(config, errors, warnings)
        return WorldValidationResult(valid=not errors, errors=errors, warnings=warnings)

    def repair(self, config: SandboxWorldConfig) -> SandboxWorldConfig:
        repaired = config.model_copy(deep=True)
        repaired.world_id = repaired.world_id or "sandbox_world"
        repaired.name = repaired.name or "未命名世界"
        repaired.description = repaired.description or "可运行 NPC Agent 沙盒世界。"
        repaired.lore = repaired.lore or "这是一个数据驱动的沙盒世界。"
        repaired.opening_scene = repaired.opening_scene or "世界已经开始，等待玩家行动。"

        repaired.player = {
            "name": "玩家",
            "location": "起始地点",
            "role": "主角",
            "status": "刚进入世界。",
            **(repaired.player or {}),
        }

        if not repaired.npcs:
            repaired.npcs = [
                SandboxNPC(
                    id="guide",
                    name="引导者",
                    role="新手引导 NPC",
                    personality="清楚、克制、会告诉玩家下一步目标。",
                    goals=["帮助玩家理解当前世界"],
                    location=repaired.player.get("location", "起始地点"),
                )
            ]

        if not repaired.tasks:
            repaired.tasks = [
                SandboxTask(id="start", title="理解当前目标", description="和 NPC 对话，明确要做什么。")
            ]

        if not repaired.actions:
            repaired.actions = [
                SandboxAction(
                    id="advance_scene",
                    label="推进场景",
                    description="将当前世界推进到下一幕。",
                    effect={"scene": "场景推进。", "complete_task": repaired.tasks[0].id, "active_npc_id": repaired.npcs[0].id},
                )
            ]

        task_ids = [task.id for task in repaired.tasks]
        npc_ids = [npc.id for npc in repaired.npcs]
        for index, action in enumerate(repaired.actions):
            effect = action.effect if isinstance(action.effect, dict) else {}
            effect.setdefault("scene", action.description or action.label)
            if task_ids and "complete_task" not in effect:
                effect["complete_task"] = task_ids[min(index, len(task_ids) - 1)]
            if npc_ids and "active_npc_id" not in effect:
                effect["active_npc_id"] = npc_ids[min(index, len(npc_ids) - 1)]
            action.effect = effect

        loop_notes = self._repair_playable_loop(repaired)
        condition_notes = self._repair_completion_conditions(repaired)
        mechanics_notes = self._repair_mechanics_schema(repaired)
        player_field_notes = self._repair_player_schema_from_rules(repaired)

        repaired.metadata = {
            **(repaired.metadata or {}),
            "validation_repaired": True,
            "playable_loop_repaired": bool(loop_notes),
            "playable_loop_notes": loop_notes,
            "completion_repaired": bool(condition_notes),
            "completion_notes": condition_notes,
            "mechanics_repaired": bool(mechanics_notes),
            "mechanics_notes": mechanics_notes,
            "player_field_repaired": bool(player_field_notes),
            "player_field_notes": player_field_notes,
        }
        return repaired

    def ensure_valid(self, config: SandboxWorldConfig) -> SandboxWorldConfig:
        repaired = self.repair(config)
        result = self.validate(repaired)
        repaired.metadata = {
            **(repaired.metadata or {}),
            "validation": {
                "valid": result.valid,
                "errors": result.errors,
                "warnings": result.warnings,
            },
        }
        if not result.valid:
            raise ValueError("; ".join(result.errors))
        return repaired

    def _validate_npcs(self, config: SandboxWorldConfig, errors: list[str], warnings: list[str]) -> None:
        if not config.npcs:
            errors.append("at least one npc is required")
            return
        seen = set()
        for npc in config.npcs:
            if not npc.id:
                errors.append("npc.id is required")
            if npc.id in seen:
                errors.append(f"duplicate npc id: {npc.id}")
            seen.add(npc.id)
            if not npc.name:
                warnings.append(f"npc {npc.id} has no name")

    def _validate_tasks(self, config: SandboxWorldConfig, errors: list[str], warnings: list[str]) -> None:
        if not config.tasks:
            errors.append("at least one task is required")
            return
        seen = set()
        for task in config.tasks:
            if not task.id:
                errors.append("task.id is required")
            if task.id in seen:
                errors.append(f"duplicate task id: {task.id}")
            seen.add(task.id)
            if task.status not in {"pending", "running", "done", "failed", "skipped"}:
                warnings.append(f"task {task.id} has non-standard status: {task.status}")

    def _validate_actions(self, config: SandboxWorldConfig, errors: list[str], warnings: list[str]) -> None:
        if not config.actions:
            errors.append("at least one action is required")
            return
        task_ids = {task.id for task in config.tasks}
        npc_ids = {npc.id for npc in config.npcs}
        seen = set()
        covered_tasks = set()
        for action in config.actions:
            if not action.id:
                errors.append("action.id is required")
            if action.id in seen:
                errors.append(f"duplicate action id: {action.id}")
            seen.add(action.id)
            effect = action.effect if isinstance(action.effect, dict) else {}
            if not effect.get("scene"):
                warnings.append(f"action {action.id} has no effect.scene")
            complete_task = effect.get("complete_task")
            if complete_task:
                if complete_task not in task_ids:
                    errors.append(f"action {action.id} references unknown task: {complete_task}")
                covered_tasks.add(complete_task)
            active_npc_id = effect.get("active_npc_id")
            if active_npc_id and active_npc_id not in npc_ids:
                errors.append(f"action {action.id} references unknown npc: {active_npc_id}")
        missing = task_ids - covered_tasks
        if missing:
            warnings.append(f"tasks not covered by action.complete_task: {', '.join(sorted(missing))}")

        self._validate_playable_loop(config, warnings)

    def _repair_playable_loop(self, config: SandboxWorldConfig) -> list[str]:
        notes: list[str] = []
        npc_by_id = {npc.id: npc for npc in config.npcs}

        start_location = str(config.player.get("location") or "").strip()
        if start_location and not self._npcs_at_location(config, start_location) and config.npcs:
            guide_id = self._unique_npc_id(config, "guide")
            config.npcs.insert(
                0,
                SandboxNPC(
                    id=guide_id,
                    name="引导员",
                    role="起点引导 NPC",
                    personality="清楚当前流程，会告诉玩家下一步该找谁、去哪里、带什么。",
                    goals=["防止玩家在起点卡住", "给出当前闭环的第一条线索"],
                    location=start_location,
                ),
            )
            npc_by_id[guide_id] = config.npcs[0]
            notes.append(f"added guide npc {guide_id} at start location {start_location}")

        for index, action in enumerate(config.actions):
            effect = action.effect if isinstance(action.effect, dict) else {}
            active_npc_id = effect.get("active_npc_id")
            npc = npc_by_id.get(str(active_npc_id)) if active_npc_id else None
            set_player = effect.get("set_player")
            if not isinstance(set_player, dict):
                set_player = {}

            if npc and self._npc_locations(npc) and set_player.get("location") not in self._npc_locations(npc):
                set_player["location"] = self._npc_primary_location(npc)
                notes.append(f"action {action.id} now moves player to npc {npc.id} location {self._npc_primary_location(npc)}")

            if not active_npc_id and config.npcs:
                npc = config.npcs[min(index, len(config.npcs) - 1)]
                effect["active_npc_id"] = npc.id
                set_player.setdefault("location", self._npc_primary_location(npc) or config.player.get("location") or "起始地点")
                notes.append(f"action {action.id} linked to npc {npc.id}")

            if set_player:
                effect["set_player"] = set_player
            action.effect = effect

        task_ids = {task.id for task in config.tasks}
        covered = {
            str(action.effect.get("complete_task"))
            for action in config.actions
            if isinstance(action.effect, dict) and action.effect.get("complete_task")
        }
        missing_tasks = [task for task in config.tasks if task.id in task_ids - covered]
        for task in missing_tasks:
            npc = config.npcs[0] if config.npcs else None
            effect = {
                "scene": task.description or task.title,
                "complete_task": task.id,
            }
            if npc:
                effect["active_npc_id"] = npc.id
                effect["set_player"] = {"location": self._npc_primary_location(npc) or config.player.get("location") or "起始地点"}
            config.actions.append(
                SandboxAction(
                    id=f"complete_{task.id}",
                    label=task.title,
                    description=task.description or task.title,
                    effect=effect,
                )
            )
            notes.append(f"added fallback action for uncovered task {task.id}")

        return notes

    def _validate_playable_loop(self, config: SandboxWorldConfig, warnings: list[str]) -> None:
        start_location = str(config.player.get("location") or "").strip()
        if start_location and not self._npcs_at_location(config, start_location):
            warnings.append(f"start location has no npc: {start_location}")

        npc_by_id = {npc.id: npc for npc in config.npcs}
        player_location = start_location
        for action in config.actions:
            effect = action.effect if isinstance(action.effect, dict) else {}
            player_patch = effect.get("set_player") if isinstance(effect.get("set_player"), dict) else {}
            next_location = str(player_patch.get("location") or player_location or "").strip()
            active_npc_id = str(effect.get("active_npc_id") or "")
            active_npc = npc_by_id.get(active_npc_id)
            if active_npc and self._npc_locations(active_npc) and next_location not in self._npc_locations(active_npc):
                warnings.append(
                    f"action {action.id} activates npc {active_npc_id} at {', '.join(self._npc_locations(active_npc))} but player goes to {next_location or 'nowhere'}"
                )
            if next_location and not self._npcs_at_location(config, next_location):
                warnings.append(f"action {action.id} moves player to location with no npc: {next_location}")
            player_location = next_location

    def _npcs_at_location(self, config: SandboxWorldConfig, location: str) -> list[SandboxNPC]:
        return [
            npc
            for npc in config.npcs
            if str(location or "").strip() in self._npc_locations(npc)
        ]

    def _npc_locations(self, npc: SandboxNPC) -> list[str]:
        values: list[str] = []
        for item in npc.locations or []:
            values.extend(split_location_values(item))
        if not values and npc.location:
            values = split_location_values(npc.location)
        return list(dict.fromkeys(values))

    def _npc_primary_location(self, npc: SandboxNPC) -> str:
        return self._npc_locations(npc)[0] if self._npc_locations(npc) else str(npc.location or "")

    def _unique_npc_id(self, config: SandboxWorldConfig, base: str) -> str:
        existing = {npc.id for npc in config.npcs}
        candidate = base
        index = 2
        while candidate in existing:
            candidate = f"{base}_{index}"
            index += 1
        return candidate

    def _repair_completion_conditions(self, config: SandboxWorldConfig) -> list[str]:
        notes: list[str] = []
        action_by_task = {}
        for action in config.actions:
            effect = action.effect if isinstance(action.effect, dict) else {}
            task_id = effect.get("complete_task")
            if task_id and task_id not in action_by_task:
                action_by_task[str(task_id)] = action

        for task in config.tasks:
            if task.completion:
                continue
            action = action_by_task.get(task.id)
            completion = {}

            if action:
                completion = {"actions": [action.id]}

            if completion:
                task.completion = completion
                notes.append(f"task {task.id} completion inferred as {completion}")
        return notes

    def _repair_mechanics_schema(self, config: SandboxWorldConfig) -> list[str]:
        metadata = config.metadata if isinstance(config.metadata, dict) else {}
        existing = metadata.get("mechanics")
        if isinstance(existing, list) and existing:
            return []

        mechanics = []
        seen = set()
        for task in config.tasks:
            completion = task.completion if isinstance(task.completion, dict) else {}
            stats = completion.get("stats") if isinstance(completion.get("stats"), dict) else {}
            player_rules = completion.get("player") if isinstance(completion.get("player"), dict) else {}
            for path in [*stats.keys(), *player_rules.keys()]:
                if path and path not in seen:
                    seen.add(path)
                    leaf = str(path).split(".")[-1]
                    mechanics.append({"id": str(path).replace(".", "_"), "path": str(path), "label": leaf, "aliases": [str(path), leaf], "kind": "stat"})

        for action in config.actions:
            effect = action.effect if isinstance(action.effect, dict) else {}
            for key in ["increase_player", "set_player"]:
                payload = effect.get(key) if isinstance(effect.get(key), dict) else {}
                for path in self._flatten_mapping(payload):
                    if path and path not in seen:
                        seen.add(path)
                        leaf = str(path).split(".")[-1]
                        mechanics.append({"id": str(path).replace(".", "_"), "path": str(path), "label": leaf, "aliases": [str(path), leaf], "kind": "stat"})

        if mechanics:
            config.metadata = {**metadata, "mechanics": mechanics}
            return ["metadata.mechanics inferred from completion/action paths; generation should provide semantic labels and aliases"]
        return []

    def _repair_player_schema_from_rules(self, config: SandboxWorldConfig) -> list[str]:
        notes: list[str] = []
        for task in config.tasks:
            completion = task.completion if isinstance(task.completion, dict) else {}
            stats = completion.get("stats") if isinstance(completion.get("stats"), dict) else {}
            for path in stats:
                notes.extend(self._ensure_player_path(config, path, 0, f"task {task.id} completion stats"))
            player_rules = completion.get("player") if isinstance(completion.get("player"), dict) else {}
            for path, expected in player_rules.items():
                default = self._default_for_expected(expected)
                notes.extend(self._ensure_player_path(config, path, default, f"task {task.id} completion player rule"))
            items = completion.get("items")
            missing_items = completion.get("missing_items")
            if items is not None or missing_items is not None:
                notes.extend(self._ensure_player_path(config, "inventory", [], f"task {task.id} completion items"))

        for action in config.actions:
            effect = action.effect if isinstance(action.effect, dict) else {}
            set_player = effect.get("set_player") if isinstance(effect.get("set_player"), dict) else {}
            for path, value in self._flatten_mapping(set_player).items():
                notes.extend(self._ensure_player_path(config, path, self._default_for_expected(value), f"action {action.id} set_player"))
            increase_player = effect.get("increase_player") if isinstance(effect.get("increase_player"), dict) else {}
            for path in increase_player:
                notes.extend(self._ensure_player_path(config, path, 0, f"action {action.id} increase_player"))
        return notes

    def _ensure_player_path(self, config: SandboxWorldConfig, path: str, default, source: str) -> list[str]:
        if not path:
            return []
        if self._get_path(config.player, path) is None:
            self._set_path(config.player, path, default)
            return [f"player.{path} initialized from {source}"]
        return []

    def _default_for_expected(self, expected):
        if isinstance(expected, bool):
            return False
        if isinstance(expected, (int, float)):
            return 0
        if isinstance(expected, list):
            return []
        if isinstance(expected, dict):
            return {}
        return ""

    def _flatten_mapping(self, source: dict, prefix: str = "") -> dict[str, object]:
        result = {}
        for key, value in source.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                result.update(self._flatten_mapping(value, path))
            else:
                result[path] = value
        return result

    def _get_path(self, source: dict, path: str):
        current = source
        for part in str(path).split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    def _set_path(self, target: dict, path: str, value) -> None:
        current = target
        parts = str(path).split(".")
        for part in parts[:-1]:
            next_value = current.get(part)
            if not isinstance(next_value, dict):
                next_value = {}
                current[part] = next_value
            current = next_value
        current[parts[-1]] = value
