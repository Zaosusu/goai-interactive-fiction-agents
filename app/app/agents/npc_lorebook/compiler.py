from __future__ import annotations

import re
from typing import Any

from app.agents.npc_lorebook.schema import NpcLorebookArtifact, NpcLorebookEntry


class NpcLorebookCompiler:
    """Deterministically projects world data into runtime lorebook entries."""

    def build_summary_entry(
        self,
        *,
        summary_id: str,
        title: str,
        content: str,
        floor_range: str = "",
        priority: int = 9999,
        token_budget: int = 500,
    ) -> NpcLorebookEntry:
        return NpcLorebookEntry(
            id=f"summary:{self._slug(summary_id or title)}",
            title=title or "阶段剧情总结",
            content=self._world_facing_text(content),
            entry_type="summary",
            strategy="constant",
            position="system",
            priority=priority,
            token_budget=token_budget,
            metadata={
                "memory_role": "long_chat_summary",
                "floor_range": floor_range,
                "maintenance": "hide_summarized_messages_after_verification",
            },
        )

    def build_memory_table_entry(
        self,
        *,
        table_id: str,
        title: str,
        rows: list[dict[str, Any]],
        keywords: list[str] | None = None,
        priority: int = 820,
    ) -> NpcLorebookEntry:
        headers = list(rows[0].keys()) if rows else []
        lines = [" | ".join(headers)] if headers else []
        for row in rows[:80]:
            lines.append(" | ".join(self._world_facing_text(str(row.get(header, ""))) for header in headers))
        return NpcLorebookEntry(
            id=f"table:{self._slug(table_id or title)}",
            title=title or "结构化记忆表",
            content="\n".join(lines),
            entry_type="table",
            keywords=self._keywords(keywords or [title, *headers]),
            strategy="selective",
            position="system",
            priority=priority,
            scan_depth=5,
            token_budget=420,
            metadata={"memory_role": "structured_memory_table"},
        )

    def compile(self, world: Any) -> NpcLorebookArtifact:
        world_id = str(getattr(world, "world_id", "") or "world")
        name = str(getattr(world, "name", "") or world_id)
        entries: list[NpcLorebookEntry] = []

        overview = self._compact(
            [
                str(getattr(world, "description", "") or ""),
                str(getattr(world, "lore", "") or ""),
                str(getattr(world, "opening_scene", "") or ""),
            ]
        )
        if overview:
            entries.append(
                NpcLorebookEntry(
                    id="world_overview",
                    title="世界总观",
                    content=self._world_facing_text(overview),
                    entry_type="world",
                    keywords=[name, world_id],
                    strategy="constant",
                    position="system",
                    priority=9999,
                    token_budget=360,
                    metadata={"lorebook_rule": "global_constitution"},
                )
            )

        for npc in getattr(world, "npcs", []) or []:
            npc_id = str(getattr(npc, "id", "") or "")
            npc_name = str(getattr(npc, "name", "") or npc_id)
            locations = self._npc_locations(npc)
            location = "、".join(locations)
            role = str(getattr(npc, "role", "") or "NPC")
            personality = str(getattr(npc, "personality", "") or "")
            goals = [self._world_facing_text(str(goal)) for goal in getattr(npc, "goals", []) or []]
            content = self._compact(
                [
                    f"{npc_name}是{role}。" if npc_name else "",
                    personality,
                    f"常在{location}。" if location else "",
                    "行动准则：" + "；".join(goal for goal in goals if goal) if goals else "",
                ]
            )
            if content:
                entries.append(
                    NpcLorebookEntry(
                        id=f"npc:{npc_id or self._slug(npc_name)}",
                        title=npc_name or npc_id,
                        content=content,
                        entry_type="character",
                        keywords=self._keywords([npc_name, npc_id, role, location]),
                        strategy="normal",
                        position="system",
                        priority=700,
                        scan_depth=5,
                        npc_ids=[npc_id] if npc_id else [],
                        locations=locations,
                        source_refs=[f"world.npcs.{npc_id}"] if npc_id else [],
                    )
                )

        for location in self._world_locations(world):
            entries.append(
                NpcLorebookEntry(
                    id=f"location:{self._slug(location)}",
                    title=location,
                    content=f"{location}是当前世界中的已知地点。NPC只能建议玩家前往已知地点，不要编造未配置地点。",
                    entry_type="location",
                    keywords=[location],
                    strategy="normal",
                    position="system",
                    priority=520,
                    scan_depth=5,
                    locations=[location],
                    source_refs=[f"world.location.{location}"],
                )
            )

        for task in getattr(world, "tasks", []) or []:
            task_id = str(getattr(task, "id", "") or "")
            title = str(getattr(task, "title", "") or task_id)
            description = str(getattr(task, "description", "") or "")
            completion = getattr(task, "completion", {}) or {}
            completion_text = self._completion_text(completion if isinstance(completion, dict) else {})
            content = self._compact([title, description, completion_text])
            if content:
                entries.append(
                    NpcLorebookEntry(
                        id=f"task:{task_id or self._slug(title)}",
                        title=title,
                        content=f"当前可推进事项：{content}",
                        entry_type="task",
                        keywords=self._keywords([title, task_id, description, completion_text]),
                        strategy="selective",
                        position="system",
                        priority=460,
                        scan_depth=5,
                        source_refs=[f"world.tasks.{task_id}"] if task_id else [],
                    )
                )

        metadata = getattr(world, "metadata", {}) if isinstance(getattr(world, "metadata", {}), dict) else {}
        for asset_entry in self._visual_asset_entries(metadata):
            entries.append(asset_entry)

        for node_entry in self._graph_entries(metadata):
            entries.append(node_entry)

        return NpcLorebookArtifact(
            artifact_id=f"{world_id}.npc_lorebook",
            world_id=world_id,
            title=f"{name} NPC 世界书",
            entries=entries,
            metadata={"compiled_by": "NpcLorebookCompiler", "entry_count": len(entries)},
        )

    def _graph_entries(self, metadata: dict[str, Any]) -> list[NpcLorebookEntry]:
        graph = metadata.get("script_graph")
        if not isinstance(graph, dict):
            graph = metadata.get("story_graph_summary")
        if not isinstance(graph, dict):
            return []
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
        labels_by_id = {str(node.get("id") or ""): str(node.get("label") or node.get("id") or "") for node in nodes if isinstance(node, dict)}
        related: dict[str, list[str]] = {}
        for edge in edges[:120]:
            if not isinstance(edge, dict):
                continue
            source = labels_by_id.get(str(edge.get("source") or ""), str(edge.get("source") or ""))
            target = labels_by_id.get(str(edge.get("target") or ""), str(edge.get("target") or ""))
            relation = str(edge.get("type") or edge.get("label") or "相关")
            if source and target:
                related.setdefault(source, []).append(f"{source}与{target}存在{relation}关联。")
                related.setdefault(target, []).append(f"{target}与{source}存在{relation}关联。")
        entries = []
        for node in nodes[:80]:
            if not isinstance(node, dict):
                continue
            label = str(node.get("label") or node.get("id") or "").strip()
            if not label:
                continue
            kind = str(node.get("kind") or "线索")
            props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
            prop_text = self._compact(str(value) for value in props.values() if value and not isinstance(value, (dict, list)))
            relation_text = self._compact(related.get(label, [])[:4])
            content = self._world_facing_text(self._compact([f"{label}是{kind}。", prop_text, relation_text]))
            entries.append(
                NpcLorebookEntry(
                    id=f"graph:{self._slug(str(node.get('id') or label))}",
                    title=label,
                    content=content,
                    entry_type=_entry_type_for_graph_kind(kind),
                    keywords=self._keywords([label, kind, prop_text]),
                    strategy="normal",
                    position="system",
                    priority=420,
                    scan_depth=5,
                    token_budget=180,
                    source_refs=[str(node.get("id") or label)],
                )
            )
        return entries

    def _visual_asset_entries(self, metadata: dict[str, Any]) -> list[NpcLorebookEntry]:
        visual_result = metadata.get("visual_result")
        generated = visual_result.get("generated") if isinstance(visual_result, dict) and isinstance(visual_result.get("generated"), list) else []
        entries: list[NpcLorebookEntry] = []
        for asset in generated:
            if not isinstance(asset, dict):
                continue
            kind = str(asset.get("kind") or "").strip().lower()
            name = str(asset.get("display_name") or asset.get("source_name") or asset.get("id") or "").strip()
            if not name:
                continue
            entry_type = _entry_type_for_visual_kind(kind)
            content = self._world_facing_text(
                self._compact(
                    [
                        f"{name} has a generated visual asset.",
                        f"kind: {kind}" if kind else "",
                        f"asset path: {asset.get('output_path')}" if asset.get("output_path") else "",
                    ]
                )
            )
            entries.append(
                NpcLorebookEntry(
                    id=f"visual:{self._slug(str(asset.get('id') or name))}",
                    title=name,
                    content=content,
                    entry_type=entry_type,
                    keywords=self._keywords([name, str(asset.get("source_id") or ""), str(asset.get("source_name") or ""), kind]),
                    strategy="normal",
                    position="system",
                    priority=430 if entry_type == "character" else 360,
                    scan_depth=5,
                    locations=[name] if entry_type in {"location", "scene"} else [],
                    source_refs=[str(asset.get("id") or name)],
                    metadata={
                        "asset_id": str(asset.get("id") or ""),
                        "kind": kind,
                        "output_path": str(asset.get("output_path") or ""),
                    },
                )
            )
        return entries

    def _world_locations(self, world: Any) -> list[str]:
        values: list[str] = []
        player = getattr(world, "player", {}) if isinstance(getattr(world, "player", {}), dict) else {}
        if player.get("location"):
            values.append(str(player["location"]))
        for npc in getattr(world, "npcs", []) or []:
            values.extend(self._npc_locations(npc))
        for action in getattr(world, "actions", []) or []:
            effect = getattr(action, "effect", {}) if isinstance(getattr(action, "effect", {}), dict) else {}
            set_player = effect.get("set_player") if isinstance(effect.get("set_player"), dict) else {}
            location = str(set_player.get("location") or "")
            if location:
                values.append(location)
        return list(dict.fromkeys(values))

    def _npc_locations(self, npc: Any) -> list[str]:
        raw = getattr(npc, "locations", None)
        if isinstance(raw, list) and raw:
            return list(dict.fromkeys(str(item).strip() for item in raw if str(item or "").strip()))
        location = str(getattr(npc, "location", "") or "").strip()
        return [location] if location else []

    def _completion_text(self, completion: dict[str, Any]) -> str:
        parts = []
        if completion.get("items"):
            parts.append("需要留意道具：" + "、".join(map(str, self._as_list(completion.get("items")))))
        if completion.get("location"):
            parts.append(f"相关地点：{completion.get('location')}")
        if completion.get("keywords"):
            parts.append("可追问关键词：" + "、".join(map(str, self._as_list(completion.get("keywords")))))
        return "；".join(parts)

    def _world_facing_text(self, text: str) -> str:
        replacements = {
            "只依据 ScriptGraphDocument 中的节点和关系回答。": "只依据自己知道的经历、可靠传闻和当前世界事实回答。",
            "根据玩家已发现的信息逐步回应，不主动发明图谱外事实。": "根据玩家已发现的信息逐步回应，不主动编造未知事实。",
            "ScriptGraphDocument": "可靠记录",
            "script_graph": "可靠记录",
            "story_graph": "可靠记录",
            "WorldTree": "传闻脉络",
            "world_tree": "传闻脉络",
            "故事图谱": "线索记录",
            "剧本图谱": "线索记录",
            "图谱": "线索记录",
            "世界树": "传闻脉络",
            "JSON": "记录",
            "json": "记录",
            "节点": "线索",
            "关系边": "关系",
            "图边": "关系",
            "开发者": "外人",
            "测试台": "记录册",
            "后台配置": "既有规矩",
        }
        cleaned = str(text or "")
        for source, target in replacements.items():
            cleaned = cleaned.replace(source, target)
        return cleaned.strip()

    def _keywords(self, values: list[str]) -> list[str]:
        words: list[str] = []
        for value in values:
            text = self._world_facing_text(str(value or ""))
            if not text:
                continue
            words.append(text)
            words.extend(re.findall(r"[\u4e00-\u9fff]{2,8}", text))
            words.extend(re.findall(r"[A-Za-z][A-Za-z0-9_:-]{1,32}", text))
        return list(dict.fromkeys(word.strip() for word in words if word and len(word.strip()) >= 2))[:18]

    def _compact(self, values) -> str:
        return " ".join(str(value).strip() for value in values if str(value or "").strip())

    def _slug(self, value: str) -> str:
        slug = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", value.strip())
        return slug.strip("_")[:80] or "entry"

    def _as_list(self, value) -> list:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


def _entry_type_for_graph_kind(kind: str) -> str:
    normalized = str(kind or "").lower()
    if normalized in {"character", "npc", "person"}:
        return "character"
    if normalized in {"location", "place", "scene"}:
        return "location"
    if normalized in {"item", "prop", "object"}:
        return "item"
    if normalized in {"clue", "evidence"}:
        return "clue"
    if normalized in {"secret", "truth"}:
        return "secret"
    if normalized in {"task", "quest", "event", "timeline_event"}:
        return "task"
    return "other"


def _entry_type_for_visual_kind(kind: str) -> str:
    normalized = str(kind or "").lower()
    if normalized in {"character", "npc", "portrait"}:
        return "character"
    if normalized in {"location", "scene", "environment", "background"}:
        return "scene"
    if normalized in {"item", "prop", "object"}:
        return "item"
    if normalized in {"clue", "evidence"}:
        return "clue"
    return "visual"
