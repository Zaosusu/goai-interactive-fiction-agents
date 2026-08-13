from __future__ import annotations

import re
from typing import Any


class MechanicsDesignAgent:
    """
    Normalizes the abstract rule layer for a generated world.

    It does not decide that a world needs "dance", "vocal", "realm", etc.
    The world draft must already contain completion/action paths or
    metadata.mechanics. This agent only makes those declarations explicit and
    aligns actions with declared completion fields.
    """

    def design(self, config: Any) -> list[str]:
        notes: list[str] = []
        notes.extend(self._ensure_mechanics(config))
        notes.extend(self._ensure_actions_produce_completion_fields(config))
        return notes

    def _ensure_mechanics(self, config: Any) -> list[str]:
        metadata = getattr(config, "metadata", {}) if isinstance(getattr(config, "metadata", {}), dict) else {}
        if isinstance(metadata.get("mechanics"), list) and metadata["mechanics"]:
            return []
        mechanics = build_mechanics(config)
        if not mechanics:
            return []
        config.metadata = {**metadata, "mechanics": mechanics}
        return ["metadata.mechanics synthesized from declared completion/action paths"]

    def _ensure_actions_produce_completion_fields(self, config: Any) -> list[str]:
        notes: list[str] = []
        action_by_task = {}
        for action in getattr(config, "actions", []):
            effect = getattr(action, "effect", {}) if isinstance(getattr(action, "effect", {}), dict) else {}
            task_id = effect.get("complete_task")
            if task_id and task_id not in action_by_task:
                action_by_task[str(task_id)] = action

        for task in getattr(config, "tasks", []):
            completion = getattr(task, "completion", {}) if isinstance(getattr(task, "completion", {}), dict) else {}
            required_paths = completion_stat_paths(completion)
            if not required_paths:
                continue
            action = action_by_task.get(str(getattr(task, "id", "")))
            if action is None:
                continue
            effect = getattr(action, "effect", {}) if isinstance(getattr(action, "effect", {}), dict) else {}
            increase = effect.get("increase_player") if isinstance(effect.get("increase_player"), dict) else {}
            set_player = effect.get("set_player") if isinstance(effect.get("set_player"), dict) else {}
            produced = set(_flatten_paths(increase)) | set(_flatten_paths(set_player))
            missing = required_paths - produced
            if not missing:
                continue
            for path in sorted(missing):
                increase[path] = _target_for_path(completion, path)
            effect["increase_player"] = increase
            action.effect = effect
            notes.append(f"action {action.id} now produces completion fields for task {task.id}: {', '.join(sorted(missing))}")
        return notes


def build_mechanics(config: Any) -> list[dict[str, Any]]:
    mechanics: dict[str, dict[str, Any]] = {}
    metadata = getattr(config, "metadata", {}) if isinstance(getattr(config, "metadata", {}), dict) else {}

    for item in _as_list(metadata.get("mechanics")):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("id") or "").strip()
        if not path:
            continue
        mechanics[path] = _mechanic(path, item.get("label"), item.get("aliases"))

    for task in getattr(config, "tasks", []):
        completion = getattr(task, "completion", {}) if isinstance(getattr(task, "completion", {}), dict) else {}
        for path in _completion_stat_paths(completion):
            mechanics.setdefault(path, _mechanic(path))

    for action in getattr(config, "actions", []):
        effect = getattr(action, "effect", {}) if isinstance(getattr(action, "effect", {}), dict) else {}
        for path in _effect_stat_paths(effect):
            mechanics.setdefault(path, _mechanic(path))

    return list(mechanics.values())


def expected_completion_paths(task: Any, mechanics: list[dict[str, Any]]) -> set[str]:
    text = f"{getattr(task, 'title', '')} {getattr(task, 'description', '')}".lower()
    expected = set()

    for mechanic in mechanics:
        path = str(mechanic.get("path") or "")
        aliases = [str(item).lower() for item in mechanic.get("aliases", []) if item]
        if path and any(_has_rule_phrase(text, alias) for alias in aliases):
            expected.add(path)

    # Generic field mention: "vocal 技能达到 30", "confidence >= 80", "realm_level 至少 3".
    for match in re.finditer(r"\b([a-zA-Z][a-zA-Z0-9_.]*)\b\s*(?:技能|熟练度|等级|level|score|>=|>|达到|至少|大于等于)", text):
        expected.add(_canonical_path(match.group(1), mechanics))

    return expected


def completion_stat_paths(completion: dict[str, Any]) -> set[str]:
    return set(_completion_stat_paths(completion))


def produced_stat_paths(config: Any) -> set[str]:
    paths = set()
    for action in getattr(config, "actions", []):
        effect = getattr(action, "effect", {}) if isinstance(getattr(action, "effect", {}), dict) else {}
        paths.update(_effect_stat_paths(effect))
    return paths


def _completion_stat_paths(completion: dict[str, Any]) -> list[str]:
    stats = completion.get("stats") if isinstance(completion.get("stats"), dict) else {}
    player = completion.get("player") if isinstance(completion.get("player"), dict) else {}
    return [str(path) for path in [*stats.keys(), *player.keys()] if path]


def _effect_stat_paths(effect: dict[str, Any]) -> list[str]:
    paths = []
    for key in ("increase_player", "set_player"):
        value = effect.get(key)
        if isinstance(value, dict):
            paths.extend(_flatten_paths(value))
    return paths


def _flatten_paths(source: dict[str, Any], prefix: str = "") -> list[str]:
    paths = []
    for key, value in source.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            paths.extend(_flatten_paths(value, path))
        else:
            paths.append(path)
    return paths


def _mechanic(path: str, label: Any = None, aliases: Any = None) -> dict[str, Any]:
    leaf = path.split(".")[-1]
    tokens = {path, leaf, leaf.replace("_", " "), leaf.replace("_", "")}
    tokens.update(str(item) for item in _as_list(aliases) if item)
    if label:
        tokens.add(str(label))
    return {
        "id": path.replace(".", "_"),
        "path": path,
        "label": str(label or leaf),
        "aliases": sorted(tokens),
        "kind": "stat",
    }


def _canonical_path(token: str, mechanics: list[dict[str, Any]]) -> str:
    token = token.lower()
    for mechanic in mechanics:
        path = str(mechanic.get("path") or "")
        aliases = [str(item).lower() for item in mechanic.get("aliases", [])]
        if token in aliases or token == path.lower() or token == path.split(".")[-1].lower():
            return path
    return token if "." in token else f"skills.{token}"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _target_for_path(completion: dict[str, Any], path: str) -> Any:
    for section in ("stats", "player"):
        rules = completion.get(section) if isinstance(completion.get(section), dict) else {}
        rule = rules.get(path)
        if isinstance(rule, dict):
            for key in ("min", "eq", "value"):
                if key in rule:
                    return rule[key]
        if rule is not None:
            return rule
    return 1


def _has_rule_phrase(text: str, alias: str) -> bool:
    if not alias:
        return False
    alias = re.escape(alias)
    patterns = [
        rf"{alias}\s*(?:>=|>|=|达到|达|至少|大于等于|不低于)",
        rf"(?:>=|>|=|达到|达|至少|大于等于|不低于)\s*\d*\s*{alias}",
        rf"{alias}\s*(?:技能|熟练度|等级|分数|score|level)",
    ]
    return any(re.search(pattern, text) for pattern in patterns)
