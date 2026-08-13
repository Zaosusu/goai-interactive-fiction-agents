from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.core.models import AgentLLMOutput
from app.worlds.sandbox.models import SandboxWorldConfig, WorldGenerationResponse


@dataclass(frozen=True)
class ProtocolToolResult:
    valid: bool
    output: AgentLLMOutput | None = None
    errors: list[str] = field(default_factory=list)


class AgentLLMOutputProtocolTool:
    """
    Deterministic code tool for the NPC output protocol.

    Agents can produce text or JSON, but this tool is the gate that decides
    whether the result is a valid AgentLLMOutput and repairs common schema drift.
    """

    allowed_action_types = {"say", "ask", "emote", "refuse", "hint", "trade", "quest", "wait"}
    action_type_aliases = {"speak", "talk", "reply", "dialogue", "dialog", "npc_response", "response"}

    def validate_agent_output(self, data: Any) -> ProtocolToolResult:
        try:
            output = data if isinstance(data, AgentLLMOutput) else AgentLLMOutput.model_validate(data)
            return ProtocolToolResult(valid=True, output=output)
        except ValidationError as exc:
            return ProtocolToolResult(valid=False, errors=[str(error.get("msg", "")) for error in exc.errors()])
        except (TypeError, ValueError) as exc:
            return ProtocolToolResult(valid=False, errors=[str(exc)])

    def repair_agent_output(self, raw: Any, fallback_actions: list[str]) -> AgentLLMOutput:
        data = self._to_mapping(raw)
        raw_text = str(raw or "").strip() if not isinstance(raw, dict) else ""
        repaired = dict(data or {})
        action_type = str(repaired.get("action_type") or repaired.get("type") or "say").lower()
        if action_type in self.action_type_aliases:
            action_type = "say"
        if action_type not in self.allowed_action_types:
            action_type = "say"

        repaired["action_type"] = action_type
        repaired["content"] = str(
            repaired.get("content")
            or repaired.get("reply")
            or repaired.get("speak")
            or repaired.get("message")
            or raw_text
            or ""
        ).strip()
        nested_content = self._extract_json_object(repaired["content"])
        if nested_content is not None:
            nested = dict(nested_content)
            nested.setdefault("action_type", repaired["action_type"])
            nested.setdefault("inner_thought", repaired.get("inner_thought", ""))
            nested.setdefault("reasoning", repaired.get("reasoning", ""))
            nested.setdefault("criticism", repaired.get("criticism", ""))
            nested.setdefault("emotion_delta", repaired.get("emotion_delta", {}))
            nested.setdefault("new_memories", repaired.get("new_memories", []))
            nested.setdefault("goal_updates", repaired.get("goal_updates", []))
            nested.setdefault("suggested_actions", repaired.get("suggested_actions", []))
            nested.setdefault("plan", repaired.get("plan", []))
            nested.setdefault("quest_progress", repaired.get("quest_progress", ""))
            nested.setdefault("command", repaired.get("command", {"name": "none", "args": {}}))
            repaired = nested
            action_type = str(repaired.get("action_type") or repaired.get("type") or "say").lower()
            if action_type in self.action_type_aliases:
                action_type = "say"
            if action_type not in self.allowed_action_types:
                action_type = "say"
            repaired["action_type"] = action_type
            repaired["content"] = str(repaired.get("content") or repaired.get("reply") or repaired.get("speak") or "").strip()
        repaired["inner_thought"] = str(repaired.get("inner_thought") or repaired.get("thought") or repaired.get("thinking") or "")
        repaired["reasoning"] = str(repaired.get("reasoning") or "")
        repaired["criticism"] = str(repaired.get("criticism") or "")
        repaired["emotion_delta"] = self._coerce_emotion_delta(repaired.get("emotion_delta", {}))
        repaired["new_memories"] = self._coerce_string_list(repaired.get("new_memories", []))
        repaired["goal_updates"] = self._coerce_string_list(repaired.get("goal_updates", []))
        repaired["suggested_actions"] = self._coerce_string_list(repaired.get("suggested_actions", [])) or fallback_actions
        repaired["plan"] = self._coerce_string_list(repaired.get("plan", []))
        repaired["quest_progress"] = str(repaired.get("quest_progress") or "")
        repaired["command"] = self._repair_command(repaired.get("command"))
        repaired["protocol_repaired"] = True

        if not repaired["content"]:
            return AgentLLMOutput(
                action_type="wait",
                content="NPC 输出为空，已交给世界适配层按当前状态恢复。",
                inner_thought="AgentLLMOutputProtocolTool could not recover visible NPC content.",
                command={"name": "none", "args": {}},
                suggested_actions=fallback_actions,
                provider_error={"type": "empty_protocol_repair"},
            )

        result = self.validate_agent_output(repaired)
        if result.valid and result.output:
            return result.output

        repaired["command"] = {"name": "none", "args": {}}
        repaired["protocol_repair_errors"] = result.errors
        fallback_result = self.validate_agent_output(repaired)
        if fallback_result.valid and fallback_result.output:
            return fallback_result.output
        raise ValueError(f"Unable to repair AgentLLMOutput protocol: {'; '.join(fallback_result.errors)}")

    def _to_mapping(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, AgentLLMOutput):
            return raw.model_dump()
        if isinstance(raw, dict):
            return raw
        text = str(raw or "").strip()
        parsed = self._extract_json_object(text)
        return parsed if parsed is not None else {"content": text}

    def _extract_json_object(self, text: str) -> dict[str, Any] | None:
        if not text:
            return None
        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = candidate.strip("`").strip()
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].strip()
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _repair_command(self, command: Any) -> dict[str, Any]:
        if not isinstance(command, dict) or not command:
            return {"name": "none", "args": {}}
        name = str(command.get("name") or command.get("type") or "none").strip() or "none"
        args = command.get("args") if isinstance(command.get("args"), dict) else {}
        data = {"name": name, "args": args}
        if name == "grant_item" and "item" not in args:
            item = command.get("item") or command.get("name")
            if item and item != "grant_item":
                data["args"] = {**args, "item": item}
        if name == "complete_task" and "task_id" not in args and command.get("task_id"):
            data["args"] = {**args, "task_id": command["task_id"]}
        if name == "switch_npc" and "npc_id" not in args and command.get("npc_id"):
            data["args"] = {**args, "npc_id": command["npc_id"]}
        return data

    def _coerce_emotion_delta(self, value: Any) -> dict[str, float]:
        allowed = {"trust", "fear", "anger", "respect", "joy", "anticipation"}
        if not isinstance(value, dict):
            return {}
        result: dict[str, float] = {}
        for key, raw in value.items():
            if key not in allowed:
                continue
            try:
                result[key] = float(raw)
            except (TypeError, ValueError):
                continue
        return result

    def _coerce_string_list(self, value: Any) -> list[str]:
        return _coerce_string_list(value)


class WorldGenerationProtocolTool:
    """
    Deterministic code tool for AI-generated world JSON.

    It validates WorldGenerationResponse and repairs common schema drift before
    the world validator/schema repairer handles runtime completeness.
    """

    def validate_world_generation(self, data: Any) -> ProtocolToolResult:
        try:
            output = data if isinstance(data, WorldGenerationResponse) else WorldGenerationResponse.model_validate(data)
            return ProtocolToolResult(valid=True, output=output)
        except ValidationError as exc:
            return ProtocolToolResult(valid=False, errors=[str(error.get("msg", "")) for error in exc.errors()])
        except (TypeError, ValueError) as exc:
            return ProtocolToolResult(valid=False, errors=[str(exc)])

    def parse_generation_json(self, content: str) -> dict[str, Any]:
        text = str(content or "").strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        return json.loads(text)

    def repair_generation_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        world = data.setdefault("world", {})
        world.setdefault("world_id", "temporary_id")
        world.setdefault("name", "未命名世界")
        world.setdefault("description", "")
        world.setdefault("lore", "")
        world.setdefault("opening_scene", "")
        world.setdefault("player", {})
        world.setdefault("npcs", [])
        world.setdefault("story_goals", [])
        world.setdefault("tasks", [])
        world.setdefault("actions", [])
        world.setdefault("initial_memories", [])
        world.setdefault("metadata", {})

        for index, task in enumerate(world.get("tasks") or []):
            if not isinstance(task, dict):
                continue
            task.setdefault("id", f"task_{index + 1}")
            task.setdefault("title", task.get("name") or task.get("label") or task.get("description") or task["id"])
            task.setdefault("description", task.get("title") or task["id"])
            task.setdefault("status", "pending")
            task["completion"] = self._normalize_completion(task.get("completion"))

        for index, npc in enumerate(world.get("npcs") or []):
            if not isinstance(npc, dict):
                continue
            npc.setdefault("id", f"npc_{index + 1}")
            npc.setdefault("name", npc.get("id") or f"NPC {index + 1}")
            npc.setdefault("role", "NPC")
            npc.setdefault("personality", "")
            npc["goals"] = self._coerce_string_list(npc.get("goals"))
            npc.setdefault("location", world.get("player", {}).get("location") or "起始地点")

        for index, action in enumerate(world.get("actions") or []):
            if not isinstance(action, dict):
                continue
            action.setdefault("id", f"action_{index + 1}")
            action.setdefault("label", action.get("name") or action.get("description") or action["id"])
            action.setdefault("description", action.get("label") or action["id"])
            effect = action.get("effect") if isinstance(action.get("effect"), dict) else {}
            effect.setdefault("scene", action.get("description") or action.get("label") or action["id"])
            action["effect"] = self._normalize_effect(effect)

        self._align_task_completions_with_actions(world)

        data.setdefault("thoughts", {})
        data.setdefault("validation_notes", [])
        return data

    def _align_task_completions_with_actions(self, world: dict[str, Any]) -> None:
        tasks_by_id = {
            str(task.get("id")): task
            for task in world.get("tasks") or []
            if isinstance(task, dict) and task.get("id")
        }
        for action in world.get("actions") or []:
            if not isinstance(action, dict):
                continue
            effect = action.get("effect") if isinstance(action.get("effect"), dict) else {}
            task_id = str(effect.get("complete_task") or "")
            action_id = str(action.get("id") or "")
            task = tasks_by_id.get(task_id)
            if not task or not action_id:
                continue
            completion = task.get("completion") if isinstance(task.get("completion"), dict) else {}
            actions = completion.get("actions")
            if actions is None:
                completion["actions"] = [action_id]
            else:
                existing = [str(item) for item in _coerce_string_list(actions)]
                if action_id not in existing:
                    existing.append(action_id)
                completion["actions"] = existing
            task["completion"] = completion

    def repair_world_generation(self, raw: Any) -> WorldGenerationResponse:
        data = self.parse_generation_json(raw) if isinstance(raw, str) else dict(raw or {})
        repaired = self.repair_generation_payload(data)
        result = self.validate_world_generation(repaired)
        if result.valid and isinstance(result.output, WorldGenerationResponse):
            return result.output
        raise ValueError(f"Unable to repair WorldGenerationResponse protocol: {'; '.join(result.errors)}")

    def repair_world_config(self, world: SandboxWorldConfig) -> SandboxWorldConfig:
        payload = {
            "thoughts": {},
            "world": world.model_dump(),
            "validation_notes": [],
        }
        return self.repair_world_generation(payload).world

    def _normalize_completion(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        if isinstance(value.get("conditions"), list):
            normalized_conditions = self._normalize_condition_list(value.get("conditions"))
            if normalized_conditions:
                if str(value.get("mode") or "").lower() == "any":
                    normalized_conditions["mode"] = "any"
                return normalized_conditions
        if any(key in value for key in ["items", "keywords", "location", "player", "stats", "relations", "flags", "actions"]):
            result = dict(value)
            if isinstance(result.get("stats"), dict):
                result["stats"] = self._normalize_numeric_conditions(result["stats"])
            if isinstance(result.get("relations"), dict):
                result["relations"] = self._normalize_numeric_conditions(result["relations"])
            if isinstance(result.get("items"), dict):
                result["items"] = self._normalize_item_requirements(result["items"])
            return result

        completion_type = str(value.get("type") or value.get("mode") or "").lower()
        root_conditions = value.get("conditions") if isinstance(value.get("conditions"), dict) else {}
        if completion_type == "stat_check":
            if value.get("field") and value.get("value") is not None:
                return {"stats": {str(value["field"]): self._operator_rule(value.get("operator"), value.get("value"))}}
            conditions = root_conditions or value.get("conditions")
            if isinstance(root_conditions.get("player_stats"), dict):
                return {"stats": self._normalize_numeric_conditions(root_conditions["player_stats"])}
            if isinstance(conditions, dict):
                path = conditions.get("path")
                operator = conditions.get("operator") or conditions.get(path)
                target = conditions.get("target") or conditions.get("value")
                if not path:
                    for key, raw in conditions.items():
                        if key not in {"operator", "target", "value"}:
                            path = key
                            if isinstance(raw, dict):
                                operator = raw.get("operator")
                                target = raw.get("target") or raw.get("value")
                            elif isinstance(raw, str) and raw in {">=", ">", "=", "==", "<=", "<"}:
                                operator = raw
                            else:
                                target = raw
                            break
                return {"stats": {str(path): self._operator_rule(operator, target)}} if path and target is not None else {}
        if completion_type == "relation_check":
            if isinstance(root_conditions.get("relations"), dict):
                return {"relations": self._normalize_numeric_conditions(root_conditions["relations"])}
            target_npc = value.get("target_npc") or value.get("npc_id")
            min_value = value.get("min_value") or value.get("target") or value.get("value")
            return {"relations": {str(target_npc): {"min": min_value}}} if target_npc and min_value is not None else {}
        if completion_type in {"item_check", "inventory_check"}:
            if "inventory_contains" in root_conditions:
                return {"items": self._coerce_string_list(root_conditions.get("inventory_contains"))}
            item = value.get("item_id") or value.get("item") or value.get("name")
            return {"items": [str(item)]} if item else {}
        if completion_type == "action_trigger":
            action = value.get("required_action") or value.get("action_id")
            return {"actions": [str(action)]} if action else {}
        if completion_type in {"flag_check", "flag_set"}:
            flag = value.get("flag_name") or value.get("flag")
            return {"flags": {str(flag): value.get("value", True)}} if flag else {}
        if completion_type == "multi_condition":
            result = self._normalize_condition_list(value.get("conditions"))
            if result:
                return result
            result: dict[str, Any] = {}
            if isinstance(root_conditions.get("player_stats"), dict):
                result["stats"] = self._normalize_numeric_conditions(root_conditions["player_stats"])
            if isinstance(root_conditions.get("flags"), dict):
                result["flags"] = root_conditions["flags"]
            if "inventory_contains" in root_conditions:
                result["items"] = self._coerce_string_list(root_conditions.get("inventory_contains"))
            if result:
                return result
            return {"keywords": self._coerce_string_list(value.get("requirements"))}
        if completion_type in {"combined", "all", "any"}:
            result = self._normalize_condition_list(value.get("conditions"))
            if result:
                if completion_type == "any":
                    result["mode"] = "any"
                return result
        if value.get("field") and value.get("value") is not None:
            return {"stats": {str(value["field"]): self._operator_rule(value.get("operator"), value.get("value"))}}
        return value

    def _normalize_condition_list(self, conditions: Any) -> dict[str, Any]:
        if not isinstance(conditions, list):
            return {}
        result: dict[str, Any] = {}
        for condition in conditions:
            normalized = self._normalize_condition(condition)
            for key, raw_value in normalized.items():
                if key == "mode":
                    continue
                if isinstance(raw_value, dict):
                    result.setdefault(key, {}).update(raw_value)
                elif isinstance(raw_value, list):
                    result.setdefault(key, []).extend(raw_value)
                else:
                    result[key] = raw_value
        return result

    def _normalize_condition(self, condition: Any) -> dict[str, Any]:
        if not isinstance(condition, dict):
            return {}
        condition_type = str(condition.get("type") or "").lower()
        path = str(condition.get("path") or condition.get("field") or "")
        operator = condition.get("operator")
        target = condition.get("target") if condition.get("target") is not None else condition.get("value")
        if path.startswith("player."):
            path = path[len("player.") :]
        if condition_type in {"stat", "stats", "player"} and path:
            return {"stats": {path: self._operator_rule(operator, target)}}
        if condition_type in {"relation", "relations"} and path:
            if path.startswith("relations."):
                path = path[len("relations.") :]
            return {"relations": {path: self._operator_rule(operator, target)}}
        if condition_type in {"flag", "flags"} and path:
            if path.startswith("flags."):
                path = path[len("flags.") :]
            if path.startswith("flags."):
                path = path[len("flags.") :]
            return {"flags": {path.removeprefix("flags."): target if target is not None else True}}
        if condition_type in {"item", "items", "inventory"}:
            if path.startswith("inventory."):
                path = path[len("inventory.") :]
            if path:
                return {"items": self._normalize_item_requirements({path: {"count": target or 1}})}
            return {"items": self._coerce_string_list(target)}
        if condition_type in {"keyword", "keywords"}:
            return {"keywords": self._coerce_string_list(target or condition.get("keywords"))}
        return self._normalize_completion(condition)

    def _normalize_numeric_conditions(self, conditions: dict[str, Any]) -> dict[str, Any]:
        result = {}
        for path, raw in conditions.items():
            if isinstance(raw, dict):
                operator = raw.get("operator")
                target = raw.get("target") if raw.get("target") is not None else raw.get("value")
                for key in ["min", "max", "eq", ">=", ">", "<=", "<", "=="]:
                    if target is None and key in raw:
                        operator = key
                        target = raw[key]
                result[str(path)] = self._operator_rule(operator, target)
            else:
                result[str(path)] = self._operator_rule(">=", raw)
        return result

    def _normalize_item_requirements(self, items: dict[str, Any]) -> list[str]:
        result: list[str] = []
        for key, raw in items.items():
            if isinstance(raw, dict):
                count = int(raw.get("count") or raw.get("quantity") or 1)
            else:
                try:
                    count = int(raw)
                except (TypeError, ValueError):
                    count = 1
            result.extend([str(key)] * max(1, count))
        return result

    def _normalize_effect(self, effect: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(effect)
        if isinstance(normalized.get("set_player"), dict):
            normalized["set_player"] = self._expand_dotted_mapping(normalized["set_player"])
        if isinstance(normalized.get("increase_player"), dict):
            normalized["increase_player"] = {
                str(path): self._numeric_delta_or_target(value)
                for path, value in normalized["increase_player"].items()
            }
        if isinstance(normalized.get("set_flag"), dict):
            normalized.setdefault("set_flags", {}).update(normalized.pop("set_flag"))
        relation_patch = normalized.get("increase_relations")
        if isinstance(relation_patch, dict):
            normalized["increase_relations"] = {
                str(path): self._numeric_delta_or_target(value)
                for path, value in relation_patch.items()
            }
        relation_patch = normalized.get("decrease_relations")
        if isinstance(relation_patch, dict):
            normalized["increase_relations"] = {
                **(normalized.get("increase_relations") if isinstance(normalized.get("increase_relations"), dict) else {}),
                **{str(path): -float(value) for path, value in relation_patch.items() if _is_number(value)},
            }
            normalized.pop("decrease_relations")
        if isinstance(normalized.get("add_items"), dict):
            inventory_items = []
            for item, quantity in normalized.pop("add_items").items():
                inventory_items.append({"name": str(item), "quantity": quantity})
            set_player = normalized.setdefault("set_player", {})
            inventory = set_player.setdefault("inventory", [])
            if isinstance(inventory, list):
                inventory.extend(inventory_items)
        elif isinstance(normalized.get("add_items"), list):
            set_player = normalized.setdefault("set_player", {})
            inventory = set_player.setdefault("inventory", [])
            if isinstance(inventory, list):
                inventory.extend(normalized.pop("add_items"))
        return normalized

    def _expand_dotted_mapping(self, source: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for path, value in source.items():
            current = result
            parts = [part for part in str(path).split(".") if part]
            if not parts:
                continue
            for part in parts[:-1]:
                next_value = current.get(part)
                if not isinstance(next_value, dict):
                    next_value = {}
                    current[part] = next_value
                current = next_value
            current[parts[-1]] = value
        return result

    def _numeric_delta_or_target(self, value: Any) -> Any:
        if isinstance(value, dict):
            for key in ["min", ">=", ">", "eq", "=="]:
                if key in value:
                    return value[key]
        return value

    def _operator_rule(self, operator: Any, target: Any) -> dict[str, Any]:
        op = str(operator or ">=").strip()
        if op in {">=", ">"}:
            return {"min": target}
        if op in {"<=", "<"}:
            return {"max": target}
        if op in {"=", "==", "eq"}:
            return {"eq": target}
        return {"min": target}

    def _coerce_string_list(self, value: Any) -> list[str]:
        return _coerce_string_list(value)


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    elif not isinstance(value, list):
        value = [value]
    result = []
    for item in value:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = item.get("text") or item.get("content") or item.get("memory") or json.dumps(item, ensure_ascii=False)
        else:
            text = str(item)
        text = text.strip()
        if text:
            result.append(text)
    return result


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False
