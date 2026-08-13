from __future__ import annotations

import json
import re
from collections.abc import Callable
from inspect import isawaitable

from pydantic import ValidationError

from app.agents.story_expansion.schema import StoryExpansionDraft, StoryExpansionNode, StoryExpansionRequest, StoryExpansionResponse
from app.core.text_generation import OpenAICompatibleTextGenerationClient, TextGenerationClient


MAX_BATCH_NODES = 10


class StoryExpansionAgent:
    """Authors an exact-size continuation for an existing Creator graph."""

    def __init__(self, text_client: TextGenerationClient | None = None) -> None:
        self.text_client = text_client

    async def expand(
        self,
        request: StoryExpansionRequest,
        progress: Callable[[str, str], object] | None = None,
    ) -> StoryExpansionResponse:
        client = self.text_client or OpenAICompatibleTextGenerationClient(
            request.expansion_llm,
            purpose="story_expansion",
        )
        existing_ids = {str(node.get("id") or "") for node in request.project.get("nodes", [])}
        character_ids = {str(item.get("id") or "") for item in request.project.get("characters", [])}
        generated_nodes: list[StoryExpansionNode] = []
        summaries: list[str] = []
        raw_parts: list[str] = []
        repair_attempted = False

        batch_total = (request.target_node_count + MAX_BATCH_NODES - 1) // MAX_BATCH_NODES
        for batch_index, offset in enumerate(range(0, request.target_node_count, MAX_BATCH_NODES), start=1):
            batch_count = min(MAX_BATCH_NODES, request.target_node_count - offset)
            await _emit_progress(
                progress,
                "StoryExpansionAgent · 分批生成",
                f"正在生成第 {batch_index}/{batch_total} 批，本批 {batch_count} 个节点；已完成 {len(generated_nodes)}/{request.target_node_count}。",
            )
            raw = await client.generate_text(
                self._system_prompt(),
                self._user_prompt(request, batch_index, batch_total, batch_count, generated_nodes),
            )
            raw_parts.append(raw)
            draft, error = _parse_and_validate_batch(
                raw,
                expected_count=batch_count,
                blocked_ids=existing_ids.union(node.id for node in generated_nodes),
                character_ids=character_ids,
                id_prefix=_expansion_id_prefix(request.source_node_id),
                sequence_start=offset + 1,
            )
            if draft is None:
                repair_attempted = True
                raw = await client.generate_text(
                    self._repair_system_prompt(),
                    self._repair_prompt(
                        request,
                        raw,
                        error,
                        batch_index,
                        batch_total,
                        batch_count,
                        generated_nodes,
                    ),
                )
                raw_parts.append(raw)
                draft, error = _parse_and_validate_batch(
                    raw,
                    expected_count=batch_count,
                    blocked_ids=existing_ids.union(node.id for node in generated_nodes),
                    character_ids=character_ids,
                    id_prefix=_expansion_id_prefix(request.source_node_id),
                    sequence_start=offset + 1,
                )
            if draft is None:
                raise ValueError(
                    f"StoryExpansionAgent batch {batch_index}/{batch_total} failed validation after repair: {error}"
                )
            generated_nodes.extend(draft.nodes)
            summaries.append(draft.summary)
            await _emit_progress(
                progress,
                "StoryExpansionAgent · 分批校验完成",
                f"第 {batch_index}/{batch_total} 批已通过 Schema 校验；累计 {len(generated_nodes)}/{request.target_node_count} 个节点。",
            )

        if len(generated_nodes) != request.target_node_count:
            raise ValueError(
                f"StoryExpansionAgent expected {request.target_node_count} nodes after batching, got {len(generated_nodes)}"
            )
        summary = "；".join(dict.fromkeys(item for item in summaries if item))[:1200] or request.brief[:1200]
        draft = StoryExpansionDraft(summary=summary, nodes=generated_nodes)
        model = str(getattr(client, "model", "") or (request.expansion_llm.model if request.expansion_llm else ""))
        return StoryExpansionResponse(
            draft=draft,
            model=model,
            raw_excerpt="\n".join(raw_parts)[:1200],
            repair_attempted=repair_attempted,
        )

    def _system_prompt(self) -> str:
        return """You are StoryExpansionAgent inside an interactive narrative platform.
Generate only the requested batch of NEW linear story nodes. Return JSON only.
The nodes array must contain exactly batch_target_node_count nodes in chronological order.
Each node needs concrete narration or NPC dialogue, not a task-list summary. Reuse only supplied character ids.
Every node type must be story. Do not include next, choices, links, or any existing node.
Do not create ids; the deterministic compiler assigns globally unique ids after validation.
Output: {"summary":"player-facing route label","nodes":[{"title":"...","content":"...","character":"existing id or empty","background_description":"...","conditions":{},"effects":{}}]}"""

    def _user_prompt(
        self,
        request: StoryExpansionRequest,
        batch_index: int,
        batch_total: int,
        batch_count: int,
        generated_nodes: list[StoryExpansionNode],
    ) -> str:
        project = request.project or {}
        return json.dumps(
            {
                "request": request.brief,
                "overall_target_node_count": request.target_node_count,
                "batch_index": batch_index,
                "batch_total": batch_total,
                "batch_target_node_count": batch_count,
                "story_position_start": len(generated_nodes) + 1,
                "source_node_id": request.source_node_id,
                "reconnect_node_id": request.reconnect_node_id,
                "insertion_mode": request.insertion_mode,
                "world": project.get("world", {}),
                "characters": project.get("characters", []),
                "existing_ids": [str(node.get("id") or "") for node in project.get("nodes", [])[:200]],
                "reserved_new_ids": [node.id for node in generated_nodes],
                "previous_generated_nodes": [
                    {"id": node.id, "title": node.title, "content": node.content, "character": node.character}
                    for node in generated_nodes[-4:]
                ],
                "existing_nodes": [
                    {
                        "id": node.get("id"),
                        "type": node.get("type"),
                        "title": node.get("title"),
                        "content": str(node.get("content") or "")[:500],
                        "character": node.get("character"),
                    }
                    for node in project.get("nodes", [])[:200]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    def _repair_system_prompt(self) -> str:
        return """Repair a StoryExpansionAgent JSON response. Return only one corrected JSON object.
Preserve useful creative content, but make the nodes array exactly the requested length, use unique new ids,
and use only character ids that exist in the supplied project."""

    def _repair_prompt(
        self,
        request: StoryExpansionRequest,
        raw: str,
        error: str,
        batch_index: int,
        batch_total: int,
        batch_count: int,
        generated_nodes: list[StoryExpansionNode],
    ) -> str:
        return self._user_prompt(request, batch_index, batch_total, batch_count, generated_nodes) + "\n\n" + json.dumps(
            {"validation_error": error, "previous_response": raw[:16000]},
            ensure_ascii=False,
            indent=2,
        )


async def _emit_progress(progress: Callable[[str, str], object] | None, title: str, detail: str) -> None:
    if progress is None:
        return
    result = progress(title, detail)
    if isawaitable(result):
        await result


def _parse_and_validate_batch(
    raw: str,
    *,
    expected_count: int,
    blocked_ids: set[str],
    character_ids: set[str],
    id_prefix: str,
    sequence_start: int,
) -> tuple[StoryExpansionDraft | None, str]:
    payload = _extract_json_object(raw)
    if payload is None:
        return None, "response is not a JSON object"
    candidate = payload.get("draft") if isinstance(payload.get("draft"), dict) else payload
    raw_nodes = candidate.get("nodes") if isinstance(candidate, dict) else None
    if not isinstance(raw_nodes, list):
        return None, "response does not contain a nodes array"
    normalized_nodes: list[StoryExpansionNode] = []
    seen: set[str] = set()
    try:
        for item in raw_nodes:
            if not isinstance(item, dict):
                continue
            node_id = _unique_node_id(
                f"{id_prefix}_{sequence_start + len(normalized_nodes):03d}",
                blocked_ids.union(seen),
            )
            character = str(item.get("character") or "").strip()
            if character and character not in character_ids:
                character = ""
            node = StoryExpansionNode.model_validate(
                {
                    "id": node_id,
                    "type": "story",
                    "title": str(item.get("title") or "").strip(),
                    "content": str(item.get("content") or "").strip(),
                    "character": character,
                    "background_description": str(item.get("background_description") or "").strip(),
                    "conditions": item.get("conditions") if isinstance(item.get("conditions"), dict) else {},
                    "effects": item.get("effects") if isinstance(item.get("effects"), dict) else {},
                }
            )
            normalized_nodes.append(node)
            seen.add(node_id)
    except ValidationError as exc:
        return None, str(exc)
    if len(normalized_nodes) < expected_count:
        return None, f"expected {expected_count} usable new nodes, got {len(normalized_nodes)}"
    normalized_nodes = normalized_nodes[:expected_count]
    summary = str(candidate.get("summary") or "剧情扩写").strip()[:1200] or "剧情扩写"
    draft = StoryExpansionDraft(summary=summary, nodes=normalized_nodes)
    return draft, ""


def _expansion_id_prefix(source_node_id: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", str(source_node_id or "story")).strip("_").lower()
    return f"expand_{normalized or 'story'}"


def _unique_node_id(base: str, blocked_ids: set[str]) -> str:
    if base not in blocked_ids:
        return base
    suffix = 2
    while f"{base}_{suffix}" in blocked_ids:
        suffix += 1
    return f"{base}_{suffix}"


def _extract_json_object(raw: str) -> dict | None:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            return None
