from __future__ import annotations

import re
from typing import Any


def split_location_values(value: Any) -> list[str]:
    return [part.strip() for part in re.split(r"[/／|｜,，、;；]+", str(value or "")) if part.strip()]


class WorldRuntimeGuardrail:
    """
    Deterministic runtime guardrails for sandbox worlds.

    LLMs may mention attractive but nonexistent places. The runtime must not
    accept those as real locations unless they are declared by world config.
    """

    def __init__(self, config: Any) -> None:
        self.config = config

    def known_locations(self) -> list[str]:
        locations: list[str] = []
        for player_location in split_location_values(getattr(self.config, "player", {}).get("location")):
            if player_location and player_location not in locations:
                locations.append(player_location)
        for npc in getattr(self.config, "npcs", []) or []:
            for location in self._npc_locations(npc):
                if location and location not in locations:
                    locations.append(location)
        for action in getattr(self.config, "actions", []) or []:
            effect = action.effect if isinstance(action.effect, dict) else {}
            set_player = effect.get("set_player") if isinstance(effect.get("set_player"), dict) else {}
            for location in split_location_values(set_player.get("location")):
                if location and location not in locations:
                    locations.append(location)
        return locations

    def _npc_locations(self, npc: Any) -> list[str]:
        raw = getattr(npc, "locations", None)
        if isinstance(raw, list) and raw:
            values: list[str] = []
            for item in raw:
                values.extend(split_location_values(item))
            return list(dict.fromkeys(values))
        return split_location_values(getattr(npc, "location", ""))

    def action_ids(self) -> set[str]:
        return {str(action.id) for action in getattr(self.config, "actions", []) or []}

    def normalize_location(self, value: str) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        for location in self.known_locations():
            if text == location:
                return location
            if text.lower() == location.lower():
                return location
        return None

    def location_rejection(self, value: str) -> str:
        locations = "、".join(self.known_locations()[:8]) or "暂无"
        return f"当前世界没有登记地点：{value}。请从已知地点中选择：{locations}。"

    def sanitize_suggested_actions(self, suggestions: list[str], fallback: list[str]) -> list[str]:
        known_locations = self.known_locations()
        action_ids = self.action_ids()
        kept: list[str] = []
        for suggestion in suggestions:
            text = str(suggestion or "").strip()
            if not text:
                continue
            if text in action_ids:
                kept.append(f"执行：{text}")
                continue
            mentioned_locations = [location for location in known_locations if location and location in text]
            looks_like_place = any(token in text for token in ["去", "前往", "到", "Hub", "hub", "区", "屋", "室", "学校", "school"])
            if looks_like_place and not mentioned_locations:
                continue
            kept.append(text)
            if len(kept) >= 4:
                break
        if kept:
            return kept
        return fallback[:4]

    def unknown_locations_in_text(self, text: str) -> list[str]:
        source = str(text or "")
        known = set(self.known_locations())
        candidates = set(re.findall(r"[A-Za-z][A-Za-z0-9_ -]*(?:Hub|hub|school|School|区|室|屋)", source))
        return [item.strip() for item in candidates if item.strip() and item.strip() not in known]

    def unknown_locations_in_suggestions(self, suggestions: list[str]) -> list[str]:
        known_locations = self.known_locations()
        unknown: list[str] = []
        for suggestion in suggestions:
            text = str(suggestion or "").strip()
            if not text:
                continue
            mentioned_locations = [location for location in known_locations if location and location in text]
            looks_like_place = any(token in text for token in ["去", "前往", "到", "Hub", "hub", "区", "屋", "室", "学校", "school"])
            if looks_like_place and not mentioned_locations:
                unknown.append(text)
        return unknown

    def output_violations(self, content: str, suggestions: list[str]) -> list[str]:
        unknown = self.unknown_locations_in_text(content)
        unknown.extend(self.unknown_locations_in_suggestions(suggestions))
        return list(dict.fromkeys(item for item in unknown if item))

    def retry_instruction(self, violations: list[str], attempt: int = 1) -> str:
        locations = "、".join(self.known_locations()[:12]) or "暂无"
        bad = "、".join(violations[:6])
        strict = ""
        if attempt >= 2:
            strict = (
                "\n这是第二次修正。你必须完全删除所有未登记地点名称，"
                "不要使用英文地名、泛称商业区、学校、Hub、工作室等配置外地点。"
            )
        return (
            "Deterministic Guardrails Layer 检测到你刚才建议了当前世界未登记的地点或地点式行动。"
            f"违规内容：{bad}。\n"
            f"当前世界允许使用的地点只有：{locations}。\n"
            "请重新回答玩家，要求：不要提及违规地点；不要编造新地点；如果要建议提升能力，"
            "必须改成前往上述已知地点、寻找已知 NPC、或描述已配置动作能支持的训练/推进方式。"
            f"{strict}"
        )

    def safe_location_guidance(self) -> str:
        return ""

    def sanitize_reply_locations(self, text: str) -> str:
        source = str(text or "")
        unknown = self.unknown_locations_in_text(source)
        if not unknown:
            return source
        return self.safe_location_guidance()
