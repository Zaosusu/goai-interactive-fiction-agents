from __future__ import annotations

import copy
from typing import Any

from app.core.models import AgentLLMOutput, AgentSessionState, ReviewIssue, ReviewReport, WorldActionRequest, command_to_dict
from app.core.protocol_tools import AgentLLMOutputProtocolTool
from app.worlds.sandbox.mechanics import build_mechanics, completion_stat_paths, expected_completion_paths, produced_stat_paths


def _report(reviewer: str, issues: list[ReviewIssue], notes: list[str] | None = None, metadata: dict[str, Any] | None = None) -> ReviewReport:
    return ReviewReport(
        reviewer=reviewer,
        passed=not any(issue.severity == "error" for issue in issues),
        issues=issues,
        notes=notes or [],
        metadata=metadata or {},
    )


class WorldReviewAgent:
    def review(self, world: Any) -> ReviewReport:
        issues: list[ReviewIssue] = []
        if not getattr(world, "npcs", None):
            issues.append(ReviewIssue(severity="error", area="world", path="npcs", message="世界缺少 NPC。"))
        if not getattr(world, "tasks", None):
            issues.append(ReviewIssue(severity="error", area="world", path="tasks", message="世界缺少任务。"))
        if not getattr(world, "actions", None):
            issues.append(ReviewIssue(severity="warning", area="world", path="actions", message="世界缺少后台动作，开放探索可能无法推进状态。"))

        task_ids = {task.id for task in getattr(world, "tasks", [])}
        action_task_ids = {
            str(action.effect.get("complete_task"))
            for action in getattr(world, "actions", [])
            if isinstance(action.effect, dict) and action.effect.get("complete_task")
        }
        missing_action = sorted(task_ids - action_task_ids)
        if missing_action:
            issues.append(
                ReviewIssue(
                    severity="warning",
                    area="world",
                    path="actions",
                    message=f"这些任务没有对应 complete_task 动作：{', '.join(missing_action)}",
                )
            )
        mechanics = build_mechanics(world)
        if not mechanics:
            issues.append(ReviewIssue(severity="warning", area="world", path="metadata.mechanics", message="世界缺少 mechanics 字段，无法做语义级完成条件审查。"))

        produced_paths = produced_stat_paths(world)
        for index, task in enumerate(getattr(world, "tasks", [])):
            if not task.completion:
                issues.append(ReviewIssue(severity="warning", area="world", path=f"tasks[{index}].completion", message="任务缺少 completion 条件。"))
            else:
                issues.extend(_semantic_completion_issues(task, index, mechanics, produced_paths))
        return _report("WorldReviewAgent", issues, metadata={"world_id": getattr(world, "world_id", "")})


class NpcReviewAgent:
    def review(self, output: AgentLLMOutput, state: AgentSessionState) -> ReviewReport:
        issues: list[ReviewIssue] = []
        command = command_to_dict(output.command)
        if not output.content.strip():
            issues.append(ReviewIssue(severity="error", area="npc", path="content", message="NPC 回复为空。"))
        if output.quest_progress:
            issues.append(ReviewIssue(severity="warning", area="npc", path="quest_progress", message="NPC 输出不应直接写 quest_progress，进度应由规则层判定。"))
        if command.get("name") == "none" and any(word in output.content for word in ["给你", "交给你", "获得", "拿到"]):
            issues.append(ReviewIssue(severity="warning", area="npc", path="command", message="NPC 文本疑似给予物品，但 command 为 none。"))
        return _report("NpcReviewAgent", issues, metadata={"command": command, "turn_state": state.world_state.get("turn")})


class NpcProtocolReviewAgent:
    """
    Deterministic protocol repair layer for NPC LLM output.

    This is not a gameplay writer. Its job is to guarantee that any model output
    that contains usable NPC text becomes a valid AgentLLMOutput object before it
    reaches runtime state validation.
    """

    def __init__(self, protocol_tool: AgentLLMOutputProtocolTool | None = None) -> None:
        self.protocol_tool = protocol_tool or AgentLLMOutputProtocolTool()

    def validate(self, output: Any):
        return self.protocol_tool.validate_agent_output(output)

    def repair_raw_output(self, raw: Any, fallback_actions: list[str]) -> AgentLLMOutput:
        return self.protocol_tool.repair_agent_output(raw, fallback_actions)

    def repair_data(self, data: dict[str, Any], fallback_actions: list[str], raw_text: str = "") -> AgentLLMOutput:
        return self.protocol_tool.repair_agent_output(data if data else raw_text, fallback_actions)


class UiReviewAgent:
    def review(self, world_state: dict[str, Any]) -> ReviewReport:
        issues: list[ReviewIssue] = []
        player = world_state.get("player", {}) if isinstance(world_state, dict) else {}
        tasks = world_state.get("tasks", []) if isinstance(world_state, dict) else []
        for task_index, task in enumerate(tasks):
            completion = task.get("completion") if isinstance(task, dict) else None
            if not isinstance(completion, dict):
                continue
            stats = completion.get("stats") if isinstance(completion.get("stats"), dict) else {}
            for path in stats:
                if _get_path(player, path) is None:
                    issues.append(ReviewIssue(severity="error", area="ui", path=f"player.{path}", message=f"任务需要 {path}，但玩家状态缺少该字段。"))
            if completion.get("items") is not None and "inventory" not in player and "items" not in player:
                issues.append(ReviewIssue(severity="warning", area="ui", path=f"tasks[{task_index}].completion.items", message="任务需要物品，但玩家状态没有 inventory/items。"))
        return _report("UiReviewAgent", issues)


class UiStateProjector:
    def project(self, world_state: dict[str, Any]) -> dict[str, Any]:
        player = world_state.get("player", {}) if isinstance(world_state, dict) else {}
        tasks = world_state.get("tasks", []) if isinstance(world_state, dict) else []
        npcs = world_state.get("npcs", []) if isinstance(world_state, dict) else []
        location = str(player.get("location") or "")
        nearby_npcs = [npc for npc in npcs if str(npc.get("location") or "") == location]
        return {
            "player": player,
            "tasks": tasks,
            "nearby_npcs": nearby_npcs,
            "completion_targets": _completion_targets(tasks),
        }


class PlaytestAgent:
    def simulate(self, world_state: dict[str, Any]) -> ReviewReport:
        return FlowReviewAgent().review(world_state)

    def simulate_adapter(self, adapter: Any, max_steps: int = 20) -> ReviewReport:
        state = adapter.create_initial_state()
        action_ids = list(adapter.world_action_ids())
        steps: list[dict[str, Any]] = []
        issues: list[ReviewIssue] = []

        if not action_ids:
            return _report(
                "PlaytestAgent",
                [
                    ReviewIssue(
                        severity="error",
                        area="playtest",
                        path="actions",
                        message="世界没有可执行 action，自动试玩无法推进。",
                    )
                ],
                metadata=_playtest_metadata(state.world_state, steps, stopped_reason="no_actions", adapter=adapter),
            )

        for index, action_id in enumerate(action_ids[: max(1, max_steps)]):
            before = copy.deepcopy(state.world_state)
            before_done = _done_task_ids(before)
            response = adapter.handle_world_action(state, WorldActionRequest(action=action_id, payload={"source": "playtest"}))
            after = copy.deepcopy(state.world_state)
            after_done = _done_task_ids(after)
            completed = sorted(after_done - before_done)
            steps.append(
                {
                    "step": index + 1,
                    "action": action_id,
                    "narration": response.narration,
                    "completed_task_ids": completed,
                    "state_changed": before != after,
                }
            )
            if _all_tasks_done(after):
                return _report(
                    "PlaytestAgent",
                    [],
                    notes=["自动试玩已完成所有任务，当前世界闭环可达。"],
                    metadata=_playtest_metadata(after, steps, stopped_reason="completed", adapter=adapter),
                )

        blocked = _pending_tasks(state.world_state)
        if blocked:
            missing = _missing_completion_sources(state.world_state)
            message = "自动试玩执行完可用 action 后仍有任务未完成。"
            if missing:
                message = f"{message} 可能缺少可产生这些 completion 字段的 action：{', '.join(missing)}。"
            issues.append(
                ReviewIssue(
                    severity="error",
                    area="playtest",
                    path="tasks",
                    message=message,
                )
            )

        stopped_reason = "blocked" if issues else "max_steps_reached"
        return _report(
            "PlaytestAgent",
            issues,
            notes=[f"未完成任务：{', '.join(_task_label(task) for task in blocked)}"] if blocked else [],
            metadata=_playtest_metadata(state.world_state, steps, stopped_reason=stopped_reason, adapter=adapter),
        )


class FlowReviewAgent:
    def review(self, world_state: dict[str, Any]) -> ReviewReport:
        issues: list[ReviewIssue] = []
        tasks = world_state.get("tasks", []) if isinstance(world_state, dict) else []
        if tasks and all(task.get("status") == "done" for task in tasks if isinstance(task, dict)):
            notes = ["所有任务已完成，当前流程闭环可达。"]
        else:
            pending = [task.get("title") or task.get("id") for task in tasks if isinstance(task, dict) and task.get("status", "pending") != "done"]
            notes = [f"待完成任务：{', '.join(str(item) for item in pending[:5])}"] if pending else ["暂无任务状态。"]
        if not world_state.get("player"):
            issues.append(ReviewIssue(severity="error", area="flow", path="player", message="缺少玩家状态，无法模拟流程。"))
        return _report("FlowReviewAgent", issues, notes=notes)


def _get_path(source: dict[str, Any], path: str) -> Any:
    current: Any = source
    for part in str(path).split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _completion_targets(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    targets: dict[str, Any] = {}
    for task in tasks:
        completion = task.get("completion") if isinstance(task, dict) else None
        if not isinstance(completion, dict):
            continue
        if isinstance(completion.get("stats"), dict):
            targets.setdefault("stats", {}).update(completion["stats"])
        if completion.get("items") is not None:
            targets.setdefault("items", []).extend(completion["items"] if isinstance(completion["items"], list) else [completion["items"]])
        if isinstance(completion.get("player"), dict):
            targets.setdefault("player", {}).update(completion["player"])
    return targets


def _semantic_completion_issues(task: Any, index: int, mechanics: list[dict[str, Any]], produced_paths: set[str]) -> list[ReviewIssue]:
    completion = getattr(task, "completion", {}) if isinstance(getattr(task, "completion", {}), dict) else {}
    paths = completion_stat_paths(completion)
    expected_paths = expected_completion_paths(task, mechanics)
    issues: list[ReviewIssue] = []
    missing = expected_paths - paths
    if missing:
        issues.append(
            ReviewIssue(
                severity="error",
                area="world",
                path=f"tasks[{index}].completion",
                message=f"任务文本命中了 mechanics {', '.join(sorted(missing))}，但 completion 未引用这些字段。",
            )
        )
    unproducible = paths - produced_paths
    if unproducible:
        issues.append(
            ReviewIssue(
                severity="error",
                area="world",
                path=f"tasks[{index}].completion",
                message=f"completion 引用了没有 action 产出的字段：{', '.join(sorted(unproducible))}。",
            )
        )
    return issues


def _done_task_ids(world_state: dict[str, Any]) -> set[str]:
    return {
        str(task.get("id"))
        for task in world_state.get("tasks", [])
        if isinstance(task, dict) and task.get("id") and task.get("status") == "done"
    }


def _all_tasks_done(world_state: dict[str, Any]) -> bool:
    tasks = [task for task in world_state.get("tasks", []) if isinstance(task, dict)]
    return bool(tasks) and all(task.get("status") == "done" for task in tasks)


def _pending_tasks(world_state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        task
        for task in world_state.get("tasks", [])
        if isinstance(task, dict) and task.get("status", "pending") not in {"done", "skipped"}
    ]


def _task_label(task: dict[str, Any]) -> str:
    return str(task.get("title") or task.get("id") or "未命名任务")


def _playtest_metadata(world_state: dict[str, Any], steps: list[dict[str, Any]], stopped_reason: str, adapter: Any | None = None) -> dict[str, Any]:
    metadata = {
        "stopped_reason": stopped_reason,
        "steps": steps,
        "completed_task_ids": sorted(_done_task_ids(world_state)),
        "pending_task_ids": [str(task.get("id")) for task in _pending_tasks(world_state) if task.get("id")],
    }
    if adapter is not None:
        metadata["runtime_artifacts"] = _adapter_runtime_artifacts(adapter)
    return metadata


def _adapter_runtime_artifacts(adapter: Any) -> dict[str, Any]:
    config = getattr(adapter, "config", None)
    config_metadata = getattr(config, "metadata", {}) if config is not None else {}
    lorebook = getattr(adapter, "lorebook", None)
    lorebook_entries = getattr(lorebook, "entries", []) or []
    visual_result = config_metadata.get("visual_result") if isinstance(config_metadata, dict) else None
    generated = visual_result.get("generated") if isinstance(visual_result, dict) and isinstance(visual_result.get("generated"), list) else []
    visual_counts: dict[str, int] = {}
    for asset in generated:
        if not isinstance(asset, dict):
            continue
        kind = str(asset.get("kind") or "unknown")
        visual_counts[kind] = visual_counts.get(kind, 0) + 1
    return {
        "world_id": getattr(adapter, "world_id", ""),
        "lorebook_artifact_id": getattr(lorebook, "artifact_id", ""),
        "lorebook_entry_count": len(lorebook_entries),
        "lorebook_entry_types": sorted({str(getattr(entry, "entry_type", "world")) for entry in lorebook_entries}),
        "lorebook_source": "metadata.npc_lorebook" if isinstance(config_metadata, dict) and isinstance(config_metadata.get("npc_lorebook"), dict) else "compiler_compatibility",
        "visual_asset_counts": visual_counts,
        "npc_portrait_count": len(config_metadata.get("npc_portraits", {})) if isinstance(config_metadata, dict) and isinstance(config_metadata.get("npc_portraits"), dict) else 0,
    }


def _missing_completion_sources(world_state: dict[str, Any]) -> list[str]:
    produced = _produced_paths(world_state)
    required: set[str] = set()
    for task in _pending_tasks(world_state):
        completion = task.get("completion") if isinstance(task, dict) else None
        if not isinstance(completion, dict):
            continue
        required.update(_required_paths(completion))
    return sorted(required - produced)


def _required_paths(completion: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    if "items" in completion:
        paths.add("player.inventory")
    if "location" in completion:
        paths.add("player.location")
    if isinstance(completion.get("player"), dict):
        paths.update(f"player.{key}" for key in completion["player"])
    if isinstance(completion.get("flags"), dict):
        paths.update(f"flags.{key}" for key in completion["flags"])
    if isinstance(completion.get("relations"), dict):
        paths.update(f"relations.{key}" for key in completion["relations"])
    if isinstance(completion.get("stats"), dict):
        paths.update(f"player.{key}" for key in completion["stats"])
    if "actions" in completion:
        paths.add("custom_events.action_id")
    if "keywords" in completion:
        paths.add("narration.keywords")
    return paths


def _produced_paths(world_state: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for event in world_state.get("custom_events", []):
        if not isinstance(event, dict):
            continue
        paths.add("custom_events.action_id")
        effect = event.get("effect")
        if not isinstance(effect, dict):
            continue
        if isinstance(effect.get("set_player"), dict):
            paths.update(f"player.{key}" for key in effect["set_player"])
            if "inventory" in effect["set_player"] or "items" in effect["set_player"]:
                paths.add("player.inventory")
        if isinstance(effect.get("increase_player"), dict):
            paths.update(f"player.{key}" for key in effect["increase_player"])
        if isinstance(effect.get("set_flags"), dict):
            paths.update(f"flags.{key}" for key in effect["set_flags"])
        if effect.get("active_npc_id"):
            paths.add("active_npc_id")
        if effect.get("complete_task"):
            paths.add("tasks.status")
        if effect.get("scene"):
            paths.add("narration.keywords")
    player = world_state.get("player", {}) if isinstance(world_state.get("player"), dict) else {}
    paths.update(f"player.{key}" for key in player)
    if "inventory" in player or "items" in player:
        paths.add("player.inventory")
    return paths
