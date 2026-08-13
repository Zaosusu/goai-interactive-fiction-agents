from __future__ import annotations

import json
import re
from typing import Any

from app.agents.npc_lorebook.compiler import NpcLorebookCompiler
from app.agents.npc_lorebook.review import NpcLorebookReviewAgent
from app.agents.npc_lorebook.schema import NpcLorebookArtifact, NpcLorebookEntry
from app.core.model_config import LLMProviderConfig
from app.core.text_generation import OpenAICompatibleTextGenerationClient, TextGenerationClient


class NpcLorebookCreationError(RuntimeError):
    pass


class NpcLorebookCreationAgent:
    """Creates runtime lorebook entries from a playable world artifact."""

    def __init__(self, text_client: TextGenerationClient | None = None) -> None:
        self.text_client = text_client
        self.compiler = NpcLorebookCompiler()
        self.review_agent = NpcLorebookReviewAgent()
        self.last_raw = ""
        self.last_error = ""

    async def create(
        self,
        world: Any,
        llm_config: LLMProviderConfig | None = None,
        *,
        strict: bool = False,
    ) -> NpcLorebookArtifact:
        fallback = self.compiler.compile(world)
        if llm_config is None and self.text_client is None:
            error = "missing_lorebook_llm_config"
            if strict:
                raise NpcLorebookCreationError(error)
            fallback.metadata = {
                **fallback.metadata,
                "created_by": "NpcLorebookCompiler",
                "creation_agent_failed": True,
                "creation_agent_error": error,
            }
            return fallback
        client = self.text_client or OpenAICompatibleTextGenerationClient(llm_config, purpose="world_builder")
        try:
            raw = await client.generate_text(self._system_prompt(), self._user_prompt(world, fallback))
            self.last_raw = raw
            self.last_error = ""
            artifact = self._artifact_from_raw(raw, world, fallback)
            artifact = self._sanitize_artifact(artifact)
            report = self.review_agent.review(artifact)
            if report.passed:
                artifact.metadata = {
                    **artifact.metadata,
                    "created_by": "NpcLorebookCreationAgent",
                    "fallback_available": True,
                    "review": report.model_dump(),
                }
                return artifact
            self.last_error = "review_failed"
        except Exception as exc:
            self.last_raw = getattr(self, "last_raw", "")
            self.last_error = f"{type(exc).__name__}: {exc}"

        if strict:
            raise NpcLorebookCreationError(self.last_error or "lorebook_creation_failed")

        fallback.metadata = {
            **fallback.metadata,
            "created_by": "NpcLorebookCompiler",
            "creation_agent_failed": True,
            "creation_agent_error": self.last_error,
        }
        return fallback

    def _artifact_from_raw(self, raw: str, world: Any, fallback: NpcLorebookArtifact) -> NpcLorebookArtifact:
        payload = _extract_json_object(raw)
        if payload is None:
            raise ValueError("lorebook_agent_returned_non_json")
        if "entries" in payload:
            artifact_payload = {
                "artifact_id": payload.get("artifact_id") or fallback.artifact_id,
                "world_id": payload.get("world_id") or getattr(world, "world_id", fallback.world_id),
                "title": payload.get("title") or fallback.title,
                "schema_version": payload.get("schema_version") or "npc_lorebook.v1",
                "entries": payload.get("entries") or [],
                "metadata": payload.get("metadata") or {},
            }
        else:
            artifact_payload = {
                "artifact_id": fallback.artifact_id,
                "world_id": fallback.world_id,
                "title": fallback.title,
                "schema_version": "npc_lorebook.v1",
                "entries": payload.get("lorebook_entries") or payload.get("items") or [],
                "metadata": {},
            }
        artifact_payload["entries"] = self._repair_entries(artifact_payload.get("entries") or [], fallback)
        return NpcLorebookArtifact.model_validate(artifact_payload)

    def _repair_entries(self, entries: list[Any], fallback: NpcLorebookArtifact) -> list[dict[str, Any]]:
        repaired: list[dict[str, Any]] = []
        fallback_by_id = {entry.id: entry for entry in fallback.entries}
        for index, raw in enumerate(entries, start=1):
            if not isinstance(raw, dict):
                continue
            entry_id = str(raw.get("id") or raw.get("key") or f"agent_entry_{index}")
            fallback_entry = fallback_by_id.get(entry_id)
            repaired.append(
                {
                    "id": entry_id,
                    "title": str(raw.get("title") or raw.get("name") or (fallback_entry.title if fallback_entry else entry_id)),
                    "content": str(raw.get("content") or raw.get("text") or raw.get("description") or ""),
                    "entry_type": raw.get("entry_type") if raw.get("entry_type") in _ENTRY_TYPES else (fallback_entry.entry_type if fallback_entry else "world"),
                    "keywords": _string_list(raw.get("keywords") or raw.get("keys")),
                    "regex_keywords": _string_list(raw.get("regex_keywords") or raw.get("regex_keys")),
                    "strategy": raw.get("strategy") if raw.get("strategy") in {"constant", "normal", "selective", "disabled"} else "normal",
                    "position": raw.get("position") if raw.get("position") in {"system", "developer", "user", "assistant"} else "system",
                    "priority": int(raw.get("priority") or (fallback_entry.priority if fallback_entry else 500)),
                    "scan_depth": int(raw.get("scan_depth") or 5),
                    "token_budget": int(raw.get("token_budget") or 260),
                    "chain": bool(raw.get("chain") or False),
                    "npc_ids": _string_list(raw.get("npc_ids")),
                    "locations": _string_list(raw.get("locations")),
                    "source_refs": _string_list(raw.get("source_refs")),
                    "metadata": raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
                }
            )
        return repaired or [entry.model_dump() for entry in fallback.entries]

    def _sanitize_artifact(self, artifact: NpcLorebookArtifact) -> NpcLorebookArtifact:
        entries: list[NpcLorebookEntry] = []
        for entry in artifact.entries:
            entries.append(
                entry.model_copy(
                    update={
                        "title": self.compiler._world_facing_text(entry.title),
                        "content": self.compiler._world_facing_text(entry.content),
                        "keywords": [self.compiler._world_facing_text(keyword) for keyword in entry.keywords if self.compiler._world_facing_text(keyword)],
                    }
                )
            )
        return artifact.model_copy(update={"entries": entries})

    def _system_prompt(self) -> str:
        return (
            "你是 NpcLorebookCreationAgent，负责为 NPC Runtime 创建世界书条目。"
            "你要把可运行世界和故事事实整理成运行时可按需注入的背景知识。"
            "世界书必须覆盖角色、地点、道具、线索、场景、任务边界、秘密揭露规则和可用视觉资产；不只是 NPC 列表。"
            "只输出一个 JSON 对象，不要 Markdown。"
            "不要在 title/content/keywords 中写任何开发者、数据结构、调试台、内部配置概念。"
            "条目必须是世界内角色可以理解的事实、传闻、地点、人际关系、行动边界或保密规则。"
        )

    def _user_prompt(self, world: Any, fallback: NpcLorebookArtifact) -> str:
        compact = {
            "world": _compact_world_for_lorebook(world),
            "fallback_outline": [entry.model_dump() for entry in fallback.entries[:24]],
            "required_schema": {
                "artifact_id": "string",
                "world_id": "string",
                "title": "string",
                "schema_version": "npc_lorebook.v1",
                "entries": [
                    {
                        "id": "stable_id",
                        "title": "world-facing short title",
                        "content": "NPC-facing lore, 80-260 Chinese chars",
                        "entry_type": "world|character|location|item|clue|scene|task|rule|secret|visual|other",
                        "keywords": ["trigger words"],
                        "regex_keywords": ["optional regex trigger"],
                        "strategy": "constant|normal|selective|disabled",
                        "position": "system|developer|user|assistant",
                        "priority": 100,
                        "scan_depth": 5,
                        "token_budget": 220,
                        "chain": False,
                        "npc_ids": ["optional npc ids"],
                        "locations": ["optional locations"],
                        "source_refs": ["optional source ids"],
                        "metadata": {"memory_role": "world|summary|table|runtime_note"},
                    }
                ],
                "metadata": {"created_by": "NpcLorebookCreationAgent"},
            },
        }
        return json.dumps(compact, ensure_ascii=False, indent=2)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    candidate = str(text or "").strip()
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


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[,，、\n]+", value)
    elif not isinstance(value, list):
        value = [value]
    return [str(item).strip() for item in value if str(item or "").strip()]


_ENTRY_TYPES = {"world", "character", "location", "item", "clue", "scene", "task", "rule", "secret", "visual", "summary", "table", "other"}


def _compact_world_for_lorebook(world: Any) -> dict[str, Any]:
    source = world.model_dump() if hasattr(world, "model_dump") else dict(world or {})
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    return {
        "world_id": source.get("world_id", ""),
        "name": source.get("name", ""),
        "description": _clip(source.get("description", ""), 1200),
        "lore": _clip(source.get("lore", ""), 1600),
        "opening_scene": _clip(source.get("opening_scene", ""), 1200),
        "player": _compact_mapping(source.get("player"), max_items=16, max_text=240),
        "npcs": [_compact_mapping(item, max_items=12, max_text=360) for item in _list(source.get("npcs"))[:80]],
        "story_goals": [_clip(item, 240) for item in _list(source.get("story_goals"))[:40]],
        "tasks": [_compact_mapping(item, max_items=10, max_text=360) for item in _list(source.get("tasks"))[:80]],
        "actions": [_compact_mapping(item, max_items=10, max_text=320) for item in _list(source.get("actions"))[:120]],
        "initial_memories": [_clip(item, 260) for item in _list(source.get("initial_memories"))[:40]],
        "metadata": _compact_lorebook_metadata(metadata),
    }


def _compact_lorebook_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("generated_by", "compiled_by", "schema_version", "template", "script_graph_input_source"):
        if key in metadata:
            compact[key] = metadata[key]
    if isinstance(metadata.get("story_graph_summary"), dict):
        compact["story_graph_summary"] = _compact_graph_summary(metadata["story_graph_summary"])
    elif isinstance(metadata.get("script_graph"), dict):
        compact["story_graph_summary"] = _compact_graph_summary(metadata["script_graph"])
    if metadata.get("visual_asset_summary"):
        compact["visual_asset_summary"] = _clip(metadata.get("visual_asset_summary"), 8000)
    visual_assets = _compact_visual_assets(metadata.get("visual_plan"), metadata.get("visual_result"))
    if visual_assets:
        compact["visual_assets"] = visual_assets
    if isinstance(metadata.get("npc_portraits"), dict):
        compact["npc_portraits"] = _compact_mapping(metadata.get("npc_portraits"), max_items=80, max_text=240)
    return compact


def _compact_graph_summary(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = [item for item in graph.get("nodes", []) if isinstance(item, dict)]
    edges = [item for item in graph.get("edges", []) if isinstance(item, dict)]
    return {
        "title": graph.get("title", ""),
        "graph_id": graph.get("graph_id", ""),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": [
            {
                "id": _clip(node.get("id", ""), 120),
                "kind": _clip(node.get("kind", ""), 80),
                "label": _clip(node.get("label", ""), 160),
                "description": _clip((node.get("properties") or {}).get("description", ""), 260)
                if isinstance(node.get("properties"), dict)
                else "",
            }
            for node in nodes[:80]
        ],
        "edges": [
            {
                "source": _clip(edge.get("source", ""), 120),
                "target": _clip(edge.get("target", ""), 120),
                "type": _clip(edge.get("type") or edge.get("label") or "", 120),
            }
            for edge in edges[:120]
        ],
    }


def _compact_visual_assets(visual_plan: Any, visual_result: Any) -> dict[str, Any]:
    plan = visual_plan if isinstance(visual_plan, dict) else {}
    result = visual_result if isinstance(visual_result, dict) else {}
    generated = [asset for asset in result.get("generated", []) if isinstance(asset, dict)]
    failed = [asset for asset in result.get("failed", []) if isinstance(asset, dict)]
    plan_assets = [asset for asset in plan.get("assets", []) if isinstance(asset, dict)]
    assets = generated or plan_assets
    if not assets:
        return {}
    return {
        "plan_id": plan.get("plan_id", ""),
        "world_id": plan.get("world_id", ""),
        "title": plan.get("title", ""),
        "asset_count": len(assets),
        "generated_count": len(generated),
        "failed_count": len(failed),
        "assets": [
            {
                "id": _clip(asset.get("id", ""), 120),
                "kind": _clip(asset.get("kind", ""), 80),
                "display_name": _clip(asset.get("display_name", ""), 160),
                "source_id": _clip(asset.get("source_id", ""), 120),
                "source_name": _clip(asset.get("source_name", ""), 160),
                "output_path": _clip(asset.get("output_path", ""), 260),
                "status": _clip(asset.get("status", ""), 80),
            }
            for asset in assets[:80]
        ],
    }


def _compact_mapping(value: Any, *, max_items: int, max_text: int) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        return {}
    compact: dict[str, Any] = {}
    for key, item in list(value.items())[:max_items]:
        if key in {"metadata", "style_guide", "upstream_context", "source_json", "source_json_excerpt", "visual_plan", "visual_result"}:
            continue
        if isinstance(item, dict):
            compact[str(key)] = _compact_mapping(item, max_items=max_items, max_text=max_text)
        elif isinstance(item, list):
            compact[str(key)] = [_clip(element, max_text) if not isinstance(element, dict) else _compact_mapping(element, max_items=max_items, max_text=max_text) for element in item[:max_items]]
        else:
            compact[str(key)] = _clip(item, max_text)
    return compact


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"
