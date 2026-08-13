from __future__ import annotations

import copy
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal


LayoutScope = Literal["all", "downstream"]


@dataclass(frozen=True)
class CreatorGraphLayoutReport:
    scope: LayoutScope
    root_node_id: str
    moved_node_count: int
    preserved_node_count: int
    layer_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "root_node_id": self.root_node_id,
            "moved_node_count": self.moved_node_count,
            "preserved_node_count": self.preserved_node_count,
            "layer_count": self.layer_count,
        }


class CreatorGraphLayoutCompiler:
    """Deterministically lays out Creator graph nodes without changing story data."""

    canvas_width = 2200
    node_width = 210
    node_height = 132
    horizontal_gap = 78
    vertical_gap = 58
    band_gap = 96
    margin_x = 72
    margin_y = 72

    def layout(
        self,
        project: dict[str, Any],
        *,
        scope: LayoutScope = "all",
        root_node_id: str = "",
    ) -> tuple[dict[str, Any], CreatorGraphLayoutReport]:
        result = copy.deepcopy(project)
        nodes = result.get("nodes") if isinstance(result.get("nodes"), list) else []
        if not nodes:
            raise ValueError("Creator Graph 没有可整理的节点。")

        by_id = {str(node.get("id") or ""): node for node in nodes if str(node.get("id") or "")}
        if not by_id:
            raise ValueError("Creator Graph 的节点缺少有效 ID。")

        if scope == "downstream":
            if not root_node_id:
                raise ValueError("整理当前节点需要 root_node_id。")
            if root_node_id not in by_id:
                raise ValueError(f"找不到要整理的节点：{root_node_id}")
        elif scope != "all":
            raise ValueError(f"不支持的布局范围：{scope}")

        adjacency = _adjacency(nodes, set(by_id))
        affected = _descendants(root_node_id, adjacency) if scope == "downstream" else set(by_id)
        ordered_ids = [str(node.get("id") or "") for node in nodes if str(node.get("id") or "") in affected]
        layers = _assign_layers(ordered_ids, adjacency, root_node_id if scope == "downstream" else "")
        layer_groups: dict[int, list[str]] = {}
        for node_id in ordered_ids:
            layer_groups.setdefault(layers[node_id], []).append(node_id)

        columns_per_band = max(1, (self.canvas_width - self.margin_x * 2) // (self.node_width + self.horizontal_gap))
        layer_count = max(layers.values(), default=0) + 1
        max_band = max((layer // columns_per_band for layer in layers.values()), default=0)
        band_tops: dict[int, float] = {}
        next_top = float(self.margin_y)
        for band in range(max_band + 1):
            layer_sizes = [
                len(layer_groups.get(layer, []))
                for layer in range(band * columns_per_band, min(layer_count, (band + 1) * columns_per_band))
            ]
            band_tops[band] = next_top
            slots = max(layer_sizes or [1])
            next_top += slots * (self.node_height + self.vertical_gap) + self.band_gap

        if scope == "downstream":
            root = by_id[root_node_id]
            anchor_x = max(20.0, float(root.get("x") or self.margin_x))
            anchor_y = max(20.0, float(root.get("y") or self.margin_y))
        else:
            anchor_x = float(self.margin_x)
            anchor_y = float(self.margin_y)

        fixed_boxes = [
            _box(float(node.get("x") or 0), float(node.get("y") or 0), self.node_width, self.node_height)
            for node_id, node in by_id.items()
            if node_id not in affected
        ]
        placed_boxes = list(fixed_boxes)
        moved = 0
        for layer in range(layer_count):
            group = layer_groups.get(layer, [])
            if not group:
                continue
            band = layer // columns_per_band
            column = layer % columns_per_band
            if band % 2:
                column = columns_per_band - 1 - column
            x = anchor_x + column * (self.node_width + self.horizontal_gap)
            layer_top = anchor_y + (band_tops[band] - self.margin_y)
            for slot, node_id in enumerate(group):
                node = by_id[node_id]
                if scope == "downstream" and node_id == root_node_id:
                    placed_boxes.append(_box(anchor_x, anchor_y, self.node_width, self.node_height))
                    continue
                y = layer_top + slot * (self.node_height + self.vertical_gap)
                x, y = _avoid_collisions(x, y, placed_boxes, self.node_width, self.node_height, self.vertical_gap)
                old_x = float(node.get("x") or 0)
                old_y = float(node.get("y") or 0)
                node["x"] = round(x)
                node["y"] = round(y)
                if old_x != node["x"] or old_y != node["y"]:
                    moved += 1
                placed_boxes.append(_box(x, y, self.node_width, self.node_height))

        report = CreatorGraphLayoutReport(
            scope=scope,
            root_node_id=root_node_id,
            moved_node_count=moved,
            preserved_node_count=len(by_id) - len(affected),
            layer_count=layer_count,
        )
        return result, report


def _adjacency(nodes: list[dict[str, Any]], valid_ids: set[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {node_id: [] for node_id in valid_ids}
    for node in nodes:
        node_id = str(node.get("id") or "")
        if node_id not in result:
            continue
        targets = [str(node.get("next") or "")]
        targets.extend(str(choice.get("next") or "") for choice in node.get("choices", []) if isinstance(choice, dict))
        for target in targets:
            if target in valid_ids and target not in result[node_id]:
                result[node_id].append(target)
    return result


def _descendants(root_node_id: str, adjacency: dict[str, list[str]]) -> set[str]:
    result: set[str] = set()
    queue: deque[str] = deque([root_node_id])
    while queue:
        node_id = queue.popleft()
        if node_id in result:
            continue
        result.add(node_id)
        queue.extend(adjacency.get(node_id, []))
    return result


def _assign_layers(ordered_ids: list[str], adjacency: dict[str, list[str]], preferred_root: str) -> dict[str, int]:
    allowed = set(ordered_ids)
    indegree = {node_id: 0 for node_id in ordered_ids}
    for source in ordered_ids:
        for target in adjacency.get(source, []):
            if target in allowed:
                indegree[target] += 1
    roots = [node_id for node_id in ordered_ids if indegree[node_id] == 0]
    if preferred_root:
        roots = [preferred_root, *[node_id for node_id in roots if node_id != preferred_root]]
    elif "start" in allowed:
        roots = ["start", *[node_id for node_id in roots if node_id != "start"]]

    layers: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque((node_id, 0) for node_id in roots)
    while len(layers) < len(ordered_ids):
        if not queue:
            orphan = next(node_id for node_id in ordered_ids if node_id not in layers)
            queue.append((orphan, max(layers.values(), default=-1) + 1))
        node_id, layer = queue.popleft()
        if node_id in layers:
            continue
        layers[node_id] = layer
        queue.extend((target, layer + 1) for target in adjacency.get(node_id, []) if target in allowed and target not in layers)
    return layers


def _box(x: float, y: float, width: float, height: float) -> tuple[float, float, float, float]:
    return x, y, x + width, y + height


def _overlaps(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> bool:
    return not (left[2] <= right[0] or right[2] <= left[0] or left[3] <= right[1] or right[3] <= left[1])


def _avoid_collisions(
    x: float,
    y: float,
    occupied: list[tuple[float, float, float, float]],
    width: float,
    height: float,
    vertical_gap: float,
) -> tuple[float, float]:
    candidate_y = max(20.0, y)
    while any(_overlaps(_box(x, candidate_y, width, height), box) for box in occupied):
        candidate_y += height + vertical_gap
    return max(20.0, x), candidate_y
