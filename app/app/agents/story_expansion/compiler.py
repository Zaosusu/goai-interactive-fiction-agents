from __future__ import annotations

import copy

from app.agents.creator_assistant.compiler import CreatorGraphCompiler
from app.agents.story_expansion.schema import StoryExpansionDraft, StoryExpansionRequest


class StoryExpansionCompiler:
    """Deterministically inserts an Agent-authored node sequence into a Creator graph."""

    def __init__(self) -> None:
        self.graph_compiler = CreatorGraphCompiler()

    def apply(self, request: StoryExpansionRequest, draft: StoryExpansionDraft) -> tuple[dict, object]:
        project = self.graph_compiler.normalize(request.project)
        nodes = project["nodes"]
        source_id = request.source_node_id or (nodes[0]["id"] if nodes else "")
        source = next((node for node in nodes if node["id"] == source_id), None)
        if source is None:
            raise ValueError(f"story expansion source node not found: {source_id}")
        if source.get("type") == "ending":
            raise ValueError("story expansion cannot continue after an ending node")

        existing_ids = {str(node.get("id") or "") for node in nodes}
        generated_ids = [node.id for node in draft.nodes]
        duplicates = existing_ids.intersection(generated_ids)
        if duplicates:
            raise ValueError(f"story expansion returned existing node ids: {', '.join(sorted(duplicates))}")

        character_ids = {str(item.get("id") or "") for item in project.get("characters", [])}
        unknown_characters = sorted({node.character for node in draft.nodes if node.character and node.character not in character_ids})
        if unknown_characters:
            raise ValueError(f"story expansion returned unknown characters: {', '.join(unknown_characters)}")

        old_successor = str(request.reconnect_node_id or source.get("next") or "")
        if old_successor and old_successor not in existing_ids:
            raise ValueError(f"story expansion reconnect node not found: {old_successor}")

        created: list[dict] = []
        base_x = float(source.get("x") or 120)
        base_y = float(source.get("y") or 120)
        for index, spec in enumerate(draft.nodes):
            created.append(
                {
                    "id": spec.id,
                    "type": spec.type,
                    "title": spec.title,
                    "content": spec.content,
                    "character": spec.character,
                    "background": "",
                    "background_description": spec.background_description,
                    "conditions": copy.deepcopy(spec.conditions),
                    "effects": copy.deepcopy(spec.effects),
                    "next": draft.nodes[index + 1].id if index + 1 < len(draft.nodes) else old_successor,
                    "choices": [],
                    "x": min(1940, base_x + 300 + (index % 5) * 330),
                    "y": min(2420, base_y + (index // 5) * 210),
                    "authoring": {"kind": "story_expansion", "sequence": index + 1},
                }
            )

        nodes.extend(created)
        if request.insertion_mode == "branch":
            source.setdefault("choices", []).append(
                {
                    "id": f"choice_expand_{created[0]['id']}",
                    "text": draft.summary[:120],
                    "next": created[0]["id"],
                    "conditions": {},
                    "effects": {},
                }
            )
        else:
            source["next"] = created[0]["id"]

        normalized = self.graph_compiler.normalize(project)
        return normalized, self.graph_compiler.validate(normalized)
