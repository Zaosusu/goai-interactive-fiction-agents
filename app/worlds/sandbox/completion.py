from __future__ import annotations

from typing import Any

from app.core.models import AgentSessionState


def evaluate_task_completions(state: AgentSessionState, text: str = "") -> list[str]:
    completed: list[str] = []
    for task in state.world_state.setdefault("tasks", []):
        if task.get("status") == "done":
            continue
        completion = task.get("completion")
        if not isinstance(completion, dict) or not completion:
            continue
        if _matches_completion(state, completion, text):
            task["status"] = "done"
            completed.append(str(task.get("title") or task.get("id") or "任务"))
    if completed:
        state.quest_progress = f"任务完成：{'、'.join(completed)}。"
        state.add_memory(state.quest_progress, 0.75)
    return completed


def _matches_completion(state: AgentSessionState, completion: dict[str, Any], text: str) -> bool:
    mode = str(completion.get("mode") or "all").lower()
    checks = []

    if "items" in completion:
        checks.append(_has_required_items(state.world_state.get("player", {}), completion.get("items")))
    if "missing_items" in completion:
        checks.append(not _has_items(state.world_state.get("player", {}), _as_list(completion.get("missing_items"))))
    if "keywords" in completion:
        checks.append(_has_completion_intent(text) and _has_keywords(text, _as_list(completion.get("keywords"))))
    if "location" in completion:
        checks.append(str(state.world_state.get("player", {}).get("location") or "") == str(completion.get("location") or ""))
    if "player" in completion and isinstance(completion.get("player"), dict):
        checks.append(_matches_mapping(state.world_state.get("player", {}), completion["player"]))
    if "flags" in completion and isinstance(completion.get("flags"), dict):
        checks.append(_matches_mapping(state.world_state.get("flags", {}), completion["flags"]))
    if "relations" in completion and isinstance(completion.get("relations"), dict):
        checks.append(_matches_numeric_mapping(state.world_state.get("relations", {}), completion["relations"]))
    if "stats" in completion and isinstance(completion.get("stats"), dict):
        checks.append(_matches_numeric_mapping(state.world_state.get("player", {}), completion["stats"]))
    if "actions" in completion:
        executed = {
            str(event.get("action_id") or event.get("action") or "")
            for event in state.world_state.get("custom_events", [])
            if isinstance(event, dict)
        }
        checks.append(all(str(action) in executed for action in _as_list(completion.get("actions"))))
    if "previous_tasks" in completion:
        done_tasks = {
            str(task.get("id") or "")
            for task in state.world_state.get("tasks", [])
            if isinstance(task, dict) and task.get("status") == "done"
        }
        checks.append(all(str(task_id) in done_tasks for task_id in _as_list(completion.get("previous_tasks"))))

    if not checks:
        return False
    if mode == "any":
        return any(checks)
    return all(checks)


def _has_items(player: dict[str, Any], required: list[str]) -> bool:
    inventory = _flatten_items(player.get("inventory")) | _flatten_items(player.get("items"))
    for key, value in player.items():
        if value is True:
            inventory.add(str(key))
    return all(str(item) in inventory for item in required)


def _has_required_items(player: dict[str, Any], required: Any) -> bool:
    return _has_items(player, _as_list(required))


def _flatten_items(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    result = set()
    for item in value:
        if isinstance(item, str):
            result.add(item)
        elif isinstance(item, dict):
            for key in ("name", "label", "id"):
                if item.get(key):
                    result.add(str(item[key]))
    return result


def _has_keywords(text: str, keywords: list[str]) -> bool:
    source = text.lower()
    return all(str(keyword).lower() in source for keyword in keywords)


def _has_completion_intent(text: str) -> bool:
    source = str(text or "").lower()
    if not source.strip():
        return False

    question_markers = [
        "?",
        "？",
        "怎么",
        "如何",
        "该做什么",
        "做什么",
        "需要做",
        "要做",
        "怎么办",
        "怎么做",
        "怎么才能",
        "如何才能",
        "请问",
        "能不能",
        "可以吗",
        "要不要",
    ]
    done_markers = [
        "我已",
        "我已经",
        "已经",
        "已完成",
        "完成了",
        "做完",
        "做了",
        "办完",
        "准备好了",
        "带来了",
        "拿到了",
        "提交了",
        "交了",
        "练了",
        "练习了",
        "训练了",
        "执行了",
        "找到了",
        "到达",
        "到了",
        "done",
        "completed",
        "finished",
        "i did",
        "i have",
    ]
    has_done_marker = any(marker in source for marker in done_markers)
    if not has_done_marker:
        return False
    return not any(marker in source for marker in question_markers)


def _matches_mapping(source: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(_get_path(source, key) == value for key, value in expected.items())


def _matches_numeric_mapping(source: dict[str, Any], expected: dict[str, Any]) -> bool:
    for key, raw_rule in expected.items():
        value = _number(_get_path(source, key))
        if value is None:
            return False
        if isinstance(raw_rule, dict):
            if "min" in raw_rule and value < float(raw_rule["min"]):
                return False
            if "max" in raw_rule and value > float(raw_rule["max"]):
                return False
            if "eq" in raw_rule and value != float(raw_rule["eq"]):
                return False
            if ">=" in raw_rule and value < float(raw_rule[">="]):
                return False
            if ">" in raw_rule and value <= float(raw_rule[">"]):
                return False
            if "<=" in raw_rule and value > float(raw_rule["<="]):
                return False
            if "<" in raw_rule and value >= float(raw_rule["<"]):
                return False
            if "==" in raw_rule and value != float(raw_rule["=="]):
                return False
        elif value < float(raw_rule):
            return False
    return True


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _get_path(source: dict[str, Any], path: str) -> Any:
    current: Any = source
    for part in str(path).split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current
