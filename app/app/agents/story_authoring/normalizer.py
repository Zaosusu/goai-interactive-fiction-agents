from __future__ import annotations

import json
from typing import Any


_DRAFT_FIELDS = {
    "schema_version",
    "story_id",
    "title",
    "genre",
    "tone",
    "premise",
    "player_role",
    "player_goal",
    "world_lore",
    "start_scene_id",
    "player_name",
    "player_stats",
    "initial_items",
    "characters",
    "clues",
    "scenes",
    "visual_style",
}
_CHARACTER_FIELDS = {
    "id",
    "name",
    "role",
    "public_profile",
    "secret",
    "goal",
    "speaking_style",
    "initial_location",
    "knowledge_boundaries",
    "portrait_description",
}
_CLUE_FIELDS = {
    "id",
    "title",
    "description",
    "source_scene_id",
    "owner_character_id",
    "reveals",
    "required_clue_ids",
}
_SCENE_FIELDS = {
    "id",
    "kind",
    "title",
    "location",
    "duration_minutes",
    "objective",
    "opening_narration",
    "beats",
    "choices",
    "default_next_scene_id",
    "unlock_clue_ids",
    "conditions",
    "effects",
    "background_description",
}
_BEAT_FIELDS = {
    "id",
    "kind",
    "speaker_id",
    "content",
    "purpose",
    "conditions",
    "effects",
    "visual_description",
}
_CHOICE_FIELDS = {
    "id",
    "text",
    "next_scene_id",
    "consequence_summary",
    "conditions",
    "effects",
}


def normalize_story_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Repair harmless model drift before strict StoryDraft validation.

    The output remains strict and deterministic. Unknown prose fields are ignored,
    while common visual-intent aliases are retained for the asset pipeline.
    """

    source = dict(payload or {})
    result = _pick(source, _DRAFT_FIELDS)
    result["schema_version"] = _text(source.get("schema_version")) or "story_draft.v1"
    for key in ["story_id", "start_scene_id"]:
        if key in source:
            result[key] = _identifier(source.get(key))
    for key in ["title", "genre", "tone", "premise", "player_role", "player_goal", "world_lore", "player_name"]:
        if key in source:
            result[key] = _text(source.get(key))
    result["player_stats"] = source.get("player_stats") if isinstance(source.get("player_stats"), dict) else {}
    result["initial_items"] = _text_list(source.get("initial_items"))
    result["visual_style"] = _visual_style(source)
    result["characters"] = [
        _normalize_character(item, index)
        for index, item in enumerate(_dict_list(source.get("characters")), start=1)
    ]
    result["clues"] = [
        _normalize_clue(item, index)
        for index, item in enumerate(_dict_list(source.get("clues")), start=1)
    ]
    result["scenes"] = [
        _normalize_scene(item, index)
        for index, item in enumerate(_dict_list(source.get("scenes")), start=1)
    ]
    _normalize_references(result)
    return result


def _normalize_references(result: dict[str, Any]) -> None:
    character_refs = _reference_index(result.get("characters"), "name")
    scene_refs = _reference_index(result.get("scenes"), "title")
    clue_refs = _reference_index(result.get("clues"), "title")

    result["start_scene_id"] = _resolve_reference(result.get("start_scene_id"), scene_refs)
    for clue in result.get("clues", []):
        clue["source_scene_id"] = _resolve_reference(clue.get("source_scene_id"), scene_refs)
        clue["owner_character_id"] = _resolve_reference(clue.get("owner_character_id"), character_refs, optional=True)
        clue["required_clue_ids"] = [
            _resolve_reference(item, clue_refs)
            for item in clue.get("required_clue_ids", [])
            if _resolve_reference(item, clue_refs)
        ]
    for scene in result.get("scenes", []):
        scene["default_next_scene_id"] = _resolve_reference(scene.get("default_next_scene_id"), scene_refs)
        scene["unlock_clue_ids"] = [
            _resolve_reference(item, clue_refs)
            for item in scene.get("unlock_clue_ids", [])
            if _resolve_reference(item, clue_refs)
        ]
        for beat in scene.get("beats", []):
            beat["speaker_id"] = _resolve_reference(beat.get("speaker_id"), character_refs, optional=beat.get("kind") != "dialogue")
        for choice in scene.get("choices", []):
            choice["next_scene_id"] = _resolve_reference(choice.get("next_scene_id"), scene_refs)


def _reference_index(items: Any, label_key: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        item_id = _identifier(item.get("id"))
        if not item_id:
            continue
        for candidate in [item_id, item.get(label_key), item.get("name"), item.get("title")]:
            key = _reference_key(candidate)
            if key:
                result[key] = item_id
    return result


def _resolve_reference(value: Any, index: dict[str, str], *, optional: bool = False) -> str:
    reference = _identifier(value)
    if not reference:
        return ""
    key = _reference_key(reference)
    if key in index:
        return index[key]
    if optional and key in {"无", "none", "null", "n/a", "旁白", "narrator", "玩家", "player"}:
        return ""
    return reference


def _reference_key(value: Any) -> str:
    return "".join(_text(value).strip().lower().split())


def _normalize_character(source: dict[str, Any], index: int) -> dict[str, Any]:
    result = _pick(source, _CHARACTER_FIELDS)
    result["id"] = _identifier(source.get("id")) or f"character_{index}"
    for key in ["name", "role", "public_profile", "secret", "goal", "speaking_style", "initial_location"]:
        if key in source:
            result[key] = _text(source.get(key))
    result["knowledge_boundaries"] = _text_list(source.get("knowledge_boundaries"))
    result["portrait_description"] = _text(
        source.get("portrait_description")
        or source.get("portrait")
        or source.get("appearance_description")
        or source.get("appearance")
    )
    return result


def _normalize_clue(source: dict[str, Any], index: int) -> dict[str, Any]:
    result = _pick(source, _CLUE_FIELDS)
    result["id"] = _identifier(source.get("id")) or f"clue_{index}"
    for key in ["source_scene_id", "owner_character_id"]:
        result[key] = _identifier(source.get(key))
    for key in ["title", "description", "reveals"]:
        if key in source:
            result[key] = _text(source.get(key))
    result["required_clue_ids"] = [_identifier(item) for item in _list(source.get("required_clue_ids")) if _identifier(item)]
    return result


def _normalize_scene(source: dict[str, Any], index: int) -> dict[str, Any]:
    result = _pick(source, _SCENE_FIELDS)
    result["id"] = _identifier(source.get("id")) or f"scene_{index}"
    for key in ["kind", "title", "location", "objective", "opening_narration"]:
        if key in source:
            result[key] = _text(source.get(key))
    result["default_next_scene_id"] = _identifier(source.get("default_next_scene_id"))
    result["unlock_clue_ids"] = [_identifier(item) for item in _list(source.get("unlock_clue_ids")) if _identifier(item)]
    result["conditions"] = source.get("conditions") if isinstance(source.get("conditions"), dict) else {}
    result["effects"] = source.get("effects") if isinstance(source.get("effects"), dict) else {}
    result["background_description"] = _text(
        source.get("background_description")
        or source.get("scene_description")
        or source.get("background_prompt")
        or source.get("visual_description")
    )
    result["beats"] = [
        _normalize_beat(item, beat_index)
        for beat_index, item in enumerate(_dict_list(source.get("beats")), start=1)
    ]
    if not result["beats"] and result.get("opening_narration"):
        result["beats"] = [
            {
                "id": "beat_1",
                "kind": "narration",
                "speaker_id": "",
                "content": result["opening_narration"],
                "purpose": "保留场景已有叙述",
                "conditions": {},
                "effects": {},
                "visual_description": result.get("background_description") or "",
            }
        ]
    result["choices"] = [
        _normalize_choice(item, choice_index)
        for choice_index, item in enumerate(_dict_list(source.get("choices")), start=1)
    ]
    if result.get("kind") == "ending":
        result["default_next_scene_id"] = ""
        result["choices"] = []
    return result


def _normalize_beat(source: dict[str, Any], index: int) -> dict[str, Any]:
    result = _pick(source, _BEAT_FIELDS)
    result["id"] = _identifier(source.get("id")) or f"beat_{index}"
    result["speaker_id"] = _identifier(source.get("speaker_id") or source.get("speaker"))
    for key in ["kind", "content", "purpose"]:
        if key in source:
            result[key] = _text(source.get(key))
    result["conditions"] = source.get("conditions") if isinstance(source.get("conditions"), dict) else {}
    result["effects"] = source.get("effects") if isinstance(source.get("effects"), dict) else {}
    result["visual_description"] = _text(
        source.get("visual_description")
        or source.get("portrait")
        or source.get("shot_description")
    )
    return result


def _normalize_choice(source: dict[str, Any], index: int) -> dict[str, Any]:
    result = _pick(source, _CHOICE_FIELDS)
    result["id"] = _identifier(source.get("id")) or f"choice_{index}"
    result["next_scene_id"] = _identifier(source.get("next_scene_id") or source.get("next"))
    for key in ["text", "consequence_summary"]:
        if key in source:
            result[key] = _text(source.get(key))
    result["conditions"] = source.get("conditions") if isinstance(source.get("conditions"), dict) else {}
    result["effects"] = source.get("effects") if isinstance(source.get("effects"), dict) else {}
    return result


def _visual_style(source: dict[str, Any]) -> dict[str, Any]:
    value = source.get("visual_style") or source.get("art_direction") or source.get("style_guide")
    if isinstance(value, dict):
        return value
    text = _text(value)
    return {"description": text} if text else {}


def _identifier(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        for key in ["id", "speaker_id", "character_id", "scene_id", "value", "name"]:
            candidate = _identifier(value.get(key))
            if candidate:
                return candidate
        return ""
    if isinstance(value, list) and value:
        return _identifier(value[0])
    return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for key in ["description", "content", "text", "name", "value", "prompt"]:
            candidate = _text(value.get(key))
            if candidate:
                return candidate
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "；".join(item for item in (_text(item) for item in value) if item)
    return str(value).strip()


def _text_list(value: Any) -> list[str]:
    return [text for text in (_text(item) for item in _list(value)) if text]


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in _list(value) if isinstance(item, dict)]


def _pick(source: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    return {key: value for key, value in source.items() if key in fields}
