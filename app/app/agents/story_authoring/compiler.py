from __future__ import annotations

import copy
import re
from typing import Any

from app.agents.creator_assistant.compiler import CreatorGraphCompiler
from app.agents.story_authoring.schema import StoryDraft, StoryScene


class StoryDraftCompiler:
    """Deterministically compiles an authored draft into the existing Creator graph."""

    def __init__(self) -> None:
        self.graph_compiler = CreatorGraphCompiler()

    def compile(self, draft: StoryDraft) -> dict[str, Any]:
        scene_entry_ids = {
            scene.id: "start" if scene.id == draft.start_scene_id else f"scene_{_safe_id(scene.id)}"
            for scene in draft.scenes
        }
        nodes: list[dict[str, Any]] = []
        scene_last_nodes: dict[str, dict[str, Any]] = {}

        for scene_index, scene in enumerate(draft.scenes):
            scene_nodes = self._compile_scene(scene, scene_entry_ids[scene.id], scene_index)
            nodes.extend(scene_nodes)
            scene_last_nodes[scene.id] = scene_nodes[-1]

        for scene in draft.scenes:
            last = scene_last_nodes[scene.id]
            if scene.kind == "ending":
                last["type"] = "ending"
                last["next"] = ""
                last["choices"] = []
                continue
            if scene.default_next_scene_id:
                last["next"] = scene_entry_ids[scene.default_next_scene_id]
            last["choices"] = [
                {
                    "id": choice.id or f"choice_{_safe_id(scene.id)}_{index}",
                    "text": choice.text,
                    "next": scene_entry_ids[choice.next_scene_id],
                    "conditions": copy.deepcopy(choice.conditions),
                    "effects": _merge_effects(
                        choice.effects,
                        {
                            "set_flags": {
                                f"choice_{_safe_id(scene.id)}_{_safe_id(choice.id or str(index))}": True,
                            }
                        },
                    ),
                    "consequence_summary": choice.consequence_summary,
                }
                for index, choice in enumerate(scene.choices, start=1)
            ]

        characters = []
        for character in draft.characters:
            hidden = f"隐藏秘密（不得无条件主动透露）：{character.secret}" if character.secret else ""
            boundaries = "；".join(character.knowledge_boundaries)
            personality = "\n".join(
                item
                for item in [
                    character.public_profile,
                    f"说话风格：{character.speaking_style}" if character.speaking_style else "",
                    f"当前目标：{character.goal}" if character.goal else "",
                    hidden,
                    f"知识边界：{boundaries}" if boundaries else "",
                ]
                if item
            )
            characters.append(
                {
                    "id": character.id,
                    "name": character.name,
                    "role": character.role,
                    "personality": personality,
                    "location": character.initial_location,
                    "portrait": "",
                    "portrait_description": character.portrait_description,
                    "authoring": character.model_dump(mode="json"),
                }
            )

        start_scene = next(scene for scene in draft.scenes if scene.id == draft.start_scene_id)
        project = {
            "version": "creator_graph.v1",
            "world": {
                "world_id": draft.story_id,
                "name": draft.title,
                "lore": "\n\n".join([draft.premise, draft.world_lore, f"玩家目标：{draft.player_goal}"]),
                "player": {
                    "name": draft.player_name,
                    "role": draft.player_role,
                    "location": start_scene.location,
                    "stats": copy.deepcopy(draft.player_stats),
                    "inventory": [{"name": item, "quantity": 1} for item in draft.initial_items],
                },
            },
            "characters": characters,
            "nodes": nodes,
            "post_story": {
                "enabled": True,
                "events": [],
            },
            "story_authoring": {
                "schema_version": draft.schema_version,
                "premise": draft.premise,
                "player_goal": draft.player_goal,
                "clues": [clue.model_dump(mode="json") for clue in draft.clues],
                "scenes": [scene.model_dump(mode="json") for scene in draft.scenes],
            },
        }
        return self.graph_compiler.normalize(project)

    def _compile_scene(self, scene: StoryScene, entry_id: str, scene_index: int) -> list[dict[str, Any]]:
        x = 120 + (scene_index % 4) * 470
        y = 120 + (scene_index // 4) * 720
        opening_effects = _merge_effects(
            scene.effects,
            {
                "scene": scene.opening_narration,
                "set_player": {"location": scene.location},
                "set_flags": {f"visited_scene_{_safe_id(scene.id)}": True},
            },
        )
        nodes = [
            {
                "id": entry_id,
                "type": "story",
                "title": scene.title,
                "content": _opening_content(scene),
                "character": "",
                "background": "",
                "background_description": scene.background_description,
                "conditions": copy.deepcopy(scene.conditions),
                "effects": opening_effects,
                "next": "",
                "choices": [],
                "x": x,
                "y": y,
                "authoring": {"scene_id": scene.id, "kind": "opening", "duration_minutes": scene.duration_minutes},
            }
        ]
        for beat_index, beat in enumerate(scene.beats, start=1):
            beat_id = beat.id or f"beat_{beat_index}"
            node = {
                "id": f"{_safe_id(scene.id)}_{_safe_id(beat_id)}",
                "type": "story",
                "title": f"{scene.title} · {beat_index}",
                "content": beat.content,
                "character": beat.speaker_id if beat.kind == "dialogue" else "",
                "background": "",
                "background_description": beat.visual_description or scene.background_description,
                "conditions": copy.deepcopy(beat.conditions),
                "effects": _merge_effects(
                    beat.effects,
                    {"active_npc_id": beat.speaker_id} if beat.speaker_id else {},
                ),
                "next": "",
                "choices": [],
                "x": x + min(330, beat_index * 105),
                "y": y + beat_index * 92,
                "authoring": {"scene_id": scene.id, "beat_id": beat_id, "kind": beat.kind, "purpose": beat.purpose},
            }
            nodes[-1]["next"] = node["id"]
            nodes.append(node)

        if scene.unlock_clue_ids:
            nodes[-1]["effects"] = _merge_effects(
                nodes[-1]["effects"],
                {"set_flags": {f"clue_{_safe_id(clue_id)}": True for clue_id in scene.unlock_clue_ids}},
            )
        return nodes


def _opening_content(scene: StoryScene) -> str:
    objective = f"\n\n【当前目标】{scene.objective}" if scene.objective else ""
    return f"{scene.opening_narration}{objective}"


def _merge_effects(*effects: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for effect in effects:
        for key, value in copy.deepcopy(effect or {}).items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = {**result[key], **value}
            else:
                result[key] = value
    return result


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fa5]+", "_", str(value or "node").strip())
    return normalized.strip("_")[:80] or "node"
