from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import deque
from typing import Any

from app.agents.creator_assistant.schema import (
    CreatorAssistantOperation,
    CreatorGraphIssue,
    CreatorGraphReport,
)


class CreatorGraphValidationError(ValueError):
    def __init__(self, report: CreatorGraphReport) -> None:
        self.report = report
        message = "; ".join(issue.message for issue in report.issues if issue.severity == "error")
        super().__init__(message or "creator graph validation failed")


class CreatorGraphCompiler:
    """Deterministically validates and applies Creator edit operations."""

    def normalize(self, project: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(project or {})
        result.setdefault("version", "creator_graph.v1")
        world = result.setdefault("world", {})
        world.setdefault("world_id", "creator_world")
        world.setdefault("name", "Untitled interactive story")
        world.setdefault("lore", "")
        player = world.setdefault("player", {})
        player.setdefault("name", "Player")
        player.setdefault("location", "Opening")
        player.setdefault("stats", {})
        player.setdefault("inventory", [])
        result.setdefault("characters", [])
        result.setdefault("nodes", [])
        result.setdefault("post_story", {"enabled": True, "events": []})

        used_choice_ids: set[str] = set()
        for node in result["nodes"]:
            node.setdefault("id", "")
            node.setdefault("type", "story")
            node.setdefault("title", node.get("id") or "Story node")
            node.setdefault("content", "")
            node.setdefault("character", "")
            node.setdefault("background", "")
            node.setdefault("conditions", {})
            node.setdefault("effects", {})
            node.setdefault("next", "")
            node.setdefault("x", 80)
            node.setdefault("y", 80)
            choices = node.setdefault("choices", [])
            for index, choice in enumerate(choices, start=1):
                choice_id = str(choice.get("id") or f"choice_{_safe_id(node['id'])}_{index}")
                choice_id = _unique_id(choice_id, used_choice_ids)
                used_choice_ids.add(choice_id)
                choice["id"] = choice_id
                choice.setdefault("text", f"Choice {index}")
                choice.setdefault("next", "")
                choice.setdefault("conditions", {})
                choice.setdefault("effects", {})
        return result

    def apply(
        self,
        project: dict[str, Any],
        operations: list[CreatorAssistantOperation],
        *,
        validate_before: bool = True,
    ) -> tuple[dict[str, Any], CreatorGraphReport]:
        result = self.normalize(project)
        before_report = self.validate(result)
        if validate_before and any(issue.severity == "error" for issue in before_report.issues):
            raise CreatorGraphValidationError(before_report)

        for operation in operations:
            self._apply_operation(result, operation)

        report = self.validate(result)
        if not report.valid:
            raise CreatorGraphValidationError(report)
        return result, report

    def validate(self, project: dict[str, Any]) -> CreatorGraphReport:
        normalized = self.normalize(project)
        issues: list[CreatorGraphIssue] = []
        nodes = normalized.get("nodes", [])
        characters = normalized.get("characters", [])
        node_ids: set[str] = set()
        character_ids: set[str] = set()
        edge_count = 0
        branch_count = 0
        ending_count = 0

        for character in characters:
            character_id = str(character.get("id") or "")
            if not character_id:
                issues.append(_issue("error", "character_id_missing", "Character id is required."))
            elif character_id in character_ids:
                issues.append(_issue("error", "character_id_duplicate", f"Duplicate character id: {character_id}"))
            character_ids.add(character_id)

        for node in nodes:
            node_id = str(node.get("id") or "")
            if not node_id:
                issues.append(_issue("error", "node_id_missing", "Node id is required."))
                continue
            if node_id in node_ids:
                issues.append(_issue("error", "node_id_duplicate", f"Duplicate node id: {node_id}", node_id))
            node_ids.add(node_id)
            if node.get("type") not in {"story", "choice", "ending"}:
                issues.append(_issue("error", "node_type_invalid", f"Invalid node type: {node.get('type')}", node_id))
            if node.get("type") == "ending":
                ending_count += 1
            character_id = str(node.get("character") or "")
            if character_id and character_id not in character_ids:
                issues.append(_issue("warning", "character_reference_missing", f"Unknown character: {character_id}", node_id))

        adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
        for node in nodes:
            node_id = str(node.get("id") or "")
            if not node_id:
                continue
            outgoing = 0
            next_id = str(node.get("next") or "")
            if next_id:
                edge_count += 1
                outgoing += 1
                if next_id not in node_ids:
                    issues.append(_issue("error", "next_node_missing", f"Node points to missing target: {next_id}", node_id))
                else:
                    adjacency[node_id].append(next_id)
            seen_choice_ids: set[str] = set()
            for choice in node.get("choices", []):
                choice_id = str(choice.get("id") or "")
                if not choice_id:
                    issues.append(_issue("error", "choice_id_missing", "Choice id is required.", node_id))
                elif choice_id in seen_choice_ids:
                    issues.append(_issue("error", "choice_id_duplicate", f"Duplicate choice id: {choice_id}", node_id))
                seen_choice_ids.add(choice_id)
                if not str(choice.get("text") or "").strip():
                    issues.append(_issue("error", "choice_text_missing", f"Choice {choice_id} has no text.", node_id))
                target_id = str(choice.get("next") or "")
                if target_id:
                    edge_count += 1
                    outgoing += 1
                    if target_id not in node_ids:
                        issues.append(_issue("error", "choice_target_missing", f"Choice points to missing target: {target_id}", node_id))
                    else:
                        adjacency[node_id].append(target_id)
            if outgoing > 1:
                branch_count += 1
            if node.get("type") != "ending" and outgoing == 0:
                issues.append(_issue("warning", "dead_end", "Non-ending node has no outgoing connection.", node_id))

        reachable: set[str] = set()
        if nodes:
            start_id = "start" if "start" in node_ids else str(nodes[0].get("id") or "")
            queue: deque[str] = deque([start_id] if start_id else [])
            while queue:
                node_id = queue.popleft()
                if node_id in reachable:
                    continue
                reachable.add(node_id)
                queue.extend(adjacency.get(node_id, []))
            for node_id in sorted(node_ids - reachable):
                issues.append(_issue("warning", "node_unreachable", "Node is unreachable from the start node.", node_id))
        else:
            issues.append(_issue("error", "graph_empty", "Creator graph must contain at least one node."))

        if ending_count == 0:
            issues.append(_issue("warning", "ending_missing", "Story graph has no ending node."))

        valid = not any(issue.severity == "error" for issue in issues)
        return CreatorGraphReport(
            valid=valid,
            node_count=len(nodes),
            edge_count=edge_count,
            branch_count=branch_count,
            ending_count=ending_count,
            reachable_count=len(reachable),
            issues=issues,
        )

    def hash(self, project: dict[str, Any]) -> str:
        normalized = self.normalize(project)
        payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _apply_operation(self, project: dict[str, Any], operation: CreatorAssistantOperation) -> None:
        data = operation.data
        operation_type = operation.type
        nodes = project["nodes"]
        characters = project["characters"]

        if operation_type == "set_world":
            project["world"].update(data)
            return
        if operation_type == "set_player_stat":
            project["world"]["player"].setdefault("stats", {})[operation.target_id] = data["value"]
            return
        if operation_type in {"add_item", "remove_item"}:
            inventory = project["world"]["player"].setdefault("inventory", [])
            if operation_type == "add_item":
                existing = next((item for item in inventory if _item_name(item) == data["name"]), None)
                if existing and isinstance(existing, dict):
                    existing["quantity"] = int(existing.get("quantity") or 1) + int(data["quantity"])
                elif not existing:
                    inventory.append({"name": data["name"], "quantity": data["quantity"]})
            else:
                project["world"]["player"]["inventory"] = [item for item in inventory if _item_name(item) != data["name"]]
            return
        if operation_type == "add_character":
            used = {str(item.get("id") or "") for item in characters}
            character_id = _unique_id(data.get("id") or f"npc_{_safe_id(data.get('name') or 'character')}", used)
            characters.append({**data, "id": character_id})
            return
        if operation_type == "update_character":
            character = _required_by_id(characters, operation.target_id, "character")
            character.update(data)
            return
        if operation_type == "delete_character":
            _required_by_id(characters, operation.target_id, "character")
            project["characters"] = [item for item in characters if item.get("id") != operation.target_id]
            for node in nodes:
                if node.get("character") == operation.target_id:
                    node["character"] = ""
            return
        if operation_type == "add_node":
            self._add_node(project, data)
            return
        if operation_type == "update_node":
            node = _required_by_id(nodes, operation.target_id, "node")
            node.update(data)
            return
        if operation_type == "delete_node":
            _required_by_id(nodes, operation.target_id, "node")
            project["nodes"] = [node for node in nodes if node.get("id") != operation.target_id]
            for node in project["nodes"]:
                if node.get("next") == operation.target_id:
                    node["next"] = ""
                node["choices"] = [choice for choice in node.get("choices", []) if choice.get("next") != operation.target_id]
            return
        if operation_type == "add_choice":
            node = _required_by_id(nodes, operation.target_id, "node")
            used = {str(choice.get("id") or "") for choice in node.get("choices", [])}
            choice_id = _unique_id(data.get("id") or f"choice_{_safe_id(operation.target_id)}", used)
            node.setdefault("choices", []).append({**data, "id": choice_id})
            return
        if operation_type in {"update_choice", "delete_choice"}:
            node = _required_by_id(nodes, operation.target_id, "node")
            choice_id = data["choice_id"]
            choice = _required_by_id(node.get("choices", []), choice_id, "choice")
            if operation_type == "update_choice":
                choice.update({key: value for key, value in data.items() if key != "choice_id"})
            else:
                node["choices"] = [item for item in node.get("choices", []) if item.get("id") != choice_id]
            return
        if operation_type in {"connect_nodes", "disconnect_nodes"}:
            source = _required_by_id(nodes, operation.target_id, "source node")
            target_id = data["target_id"]
            if operation_type == "connect_nodes":
                _required_by_id(nodes, target_id, "target node")
            choice_id = data.get("choice_id") or ""
            if choice_id:
                choice = _required_by_id(source.get("choices", []), choice_id, "choice")
                choice["next"] = target_id if operation_type == "connect_nodes" else ""
            else:
                source["next"] = target_id if operation_type == "connect_nodes" else ""
            return
        if operation_type == "create_branch":
            self._create_branch(project, data)
            return
        raise ValueError(f"unsupported creator operation: {operation_type}")

    def _add_node(self, project: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        nodes = project["nodes"]
        used = {str(node.get("id") or "") for node in nodes}
        node_id = _unique_id(data.get("id") or f"{data.get('type') or 'story'}_{_safe_id(data.get('title') or 'node')}", used)
        after = str(data.get("after") or "")
        after_node = _required_by_id(nodes, after, "after node") if after else None
        node = {
            "id": node_id,
            "type": data.get("type") or "story",
            "title": data.get("title") or node_id,
            "content": data.get("content") or "",
            "character": data.get("character") or (after_node.get("character") if after_node else ""),
            "background": data.get("background") or "",
            "conditions": data.get("conditions") or {},
            "effects": data.get("effects") or {},
            "next": data.get("next") or "",
            "choices": [],
            "x": data.get("x") if data.get("x") is not None else min(1940, float(after_node.get("x") or 120) + 300 if after_node else 180),
            "y": data.get("y") if data.get("y") is not None else min(2420, float(after_node.get("y") or 120) + 120 if after_node else 180),
        }
        nodes.append(node)
        if after_node and not after_node.get("next") and after_node.get("type") != "ending":
            after_node["next"] = node_id
        return node

    def _create_branch(self, project: dict[str, Any], data: dict[str, Any]) -> None:
        nodes = project["nodes"]
        source_id = data.get("source_node_id") or ""
        source = _required_by_id(nodes, source_id, "branch source node")
        reconnect_id = data.get("reconnect_node_id") or ""
        if reconnect_id:
            _required_by_id(nodes, reconnect_id, "branch reconnect node")

        created: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        for index, spec in enumerate(data["nodes"], start=1):
            node = self._add_node(
                project,
                {
                    **spec,
                    "after": "",
                    "x": min(1940, float(source.get("x") or 120) + index * 300),
                    "y": min(2420, float(source.get("y") or 120) + 260),
                },
            )
            if previous is not None:
                previous["next"] = node["id"]
            previous = node
            created.append(node)

        if previous is not None and reconnect_id and previous.get("type") != "ending":
            previous["next"] = reconnect_id

        used_choices = {str(choice.get("id") or "") for choice in source.get("choices", [])}
        choice_id = _unique_id(data.get("choice_id") or f"choice_branch_{_safe_id(data['choice_text'])}", used_choices)
        source.setdefault("choices", []).append(
            {
                "id": choice_id,
                "text": data["choice_text"],
                "next": created[0]["id"],
                "conditions": data.get("choice_conditions") or {},
                "effects": data.get("choice_effects") or {},
            }
        )


def _issue(severity: str, code: str, message: str, node_id: str = "") -> CreatorGraphIssue:
    return CreatorGraphIssue(severity=severity, code=code, message=message, node_id=node_id)


def _required_by_id(items: list[dict[str, Any]], item_id: str, label: str) -> dict[str, Any]:
    item = next((candidate for candidate in items if str(candidate.get("id") or "") == item_id), None)
    if item is None:
        raise ValueError(f"{label} not found: {item_id}")
    return item


def _safe_id(value: Any) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", str(value or "node").strip())
    return normalized.strip("_")[:60] or "node"


def _unique_id(base: str, used: set[str]) -> str:
    candidate = _safe_id(base)
    if candidate not in used:
        return candidate
    index = 2
    while f"{candidate}_{index}" in used:
        index += 1
    return f"{candidate}_{index}"


def _item_name(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("name") or item.get("id") or "")
    return ""
