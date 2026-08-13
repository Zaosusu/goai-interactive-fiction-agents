from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.agents.story_authoring.schema import StoryAuthoringRequest, StoryDraft
from app.agents.story_authoring.normalizer import normalize_story_payload
from app.core.text_generation import OpenAICompatibleTextGenerationClient, TextGenerationClient


class StoryAuthoringError(RuntimeError):
    pass


class StoryAuthoringAgent:
    """Creates a complete, structured story draft through a configured text model."""

    def __init__(self, text_client: TextGenerationClient | None = None) -> None:
        self.text_client = text_client
        self.last_raw = ""

    async def create(self, request: StoryAuthoringRequest) -> tuple[StoryDraft, str, str]:
        try:
            client = self.text_client or OpenAICompatibleTextGenerationClient(request.story_llm, purpose="story_authoring")
            raw = await client.generate_text(self._system_prompt(), self._user_prompt(request))
            self.last_raw = raw
            payload = _extract_json_object(raw)
            if payload is None:
                raise StoryAuthoringError("剧情创作模型没有返回可解析的 JSON。")
            if isinstance(payload.get("draft"), dict):
                payload = payload["draft"]
            draft = StoryDraft.model_validate(normalize_story_payload(payload))
            configured_model = request.story_llm.model if request.story_llm else ""
            model = str(getattr(client, "model", "") or configured_model)
            return draft, raw, model
        except StoryAuthoringError:
            raise
        except ValidationError as exc:
            errors = "; ".join(
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in exc.errors()[:12]
            )
            raise StoryAuthoringError(f"剧情草案结构不符合要求：{errors}") from exc
        except Exception as exc:
            raise StoryAuthoringError(f"剧情创作 API 调用失败：{type(exc).__name__}: {exc}") from exc

    async def repair(
        self,
        request: StoryAuthoringRequest,
        draft: StoryDraft,
        issues: list[dict],
    ) -> tuple[StoryDraft, str, str]:
        """Repair a schema-valid draft from explicit deterministic review findings."""

        try:
            client = self.text_client or OpenAICompatibleTextGenerationClient(
                request.story_llm,
                purpose="story_authoring",
            )
            raw = await client.generate_text(
                self._repair_system_prompt(),
                self._repair_user_prompt(request, draft, issues),
            )
            self.last_raw = raw
            payload = _extract_json_object(raw)
            if payload is None:
                raise StoryAuthoringError("剧情修复模型没有返回可解析的 JSON。")
            if isinstance(payload.get("draft"), dict):
                payload = payload["draft"]
            repaired = StoryDraft.model_validate(normalize_story_payload(payload))
            configured_model = request.story_llm.model if request.story_llm else ""
            model = str(getattr(client, "model", "") or configured_model)
            return repaired, raw, model
        except StoryAuthoringError:
            raise
        except ValidationError as exc:
            errors = "; ".join(
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in exc.errors()[:12]
            )
            raise StoryAuthoringError(f"修复后的剧情草案仍不符合 Schema：{errors}") from exc
        except Exception as exc:
            raise StoryAuthoringError(f"剧情修复 API 调用失败：{type(exc).__name__}: {exc}") from exc

    def _system_prompt(self) -> str:
        return """你是互动剧情开发平台内部的 StoryAuthoringAgent。
你的任务不是写小说梗概，而是生成可编译、可编辑、可试玩的结构化互动剧情草案。
只返回一个 JSON 对象，不要 Markdown，不要解释，不要代码围栏。

必须满足：
1. 每个场景都有具体开场叙事、具体 NPC 台词、玩家目标和后续连接。
2. 台词必须是角色真正会说出口的话，不能只写“二人交谈”“玩家获得任务”等摘要。
3. 任务只是后台结构；玩家可见体验必须由情境、对话、行动、选择和反馈构成。
4. 所有 id 唯一、稳定、使用英文小写或拼音下划线；所有引用必须存在。
5. 至少一个可达结局；非结局场景必须通过 default_next_scene_id 或 choices 连接后续。
6. NPC 的 secret 与 knowledge_boundaries 要限制其自由对话，避免提前剧透。
7. choices 必须带来可理解的后果，effects 使用 JSON 对象表达 set_flags、increase_player、set_player 等状态变化。
8. 每个场景至少 2 条 dialogue beat；旁白使用 narration，行动反馈使用 action，线索揭示使用 reveal。
9. 目标时长是实际玩家体验估算，不要用大量空洞文字凑时长。

严格输出以下结构：
{
  "schema_version": "story_draft.v1",
  "story_id": "story_id",
  "title": "标题",
  "genre": "类型",
  "tone": "基调",
  "premise": "核心情境与冲突",
  "player_role": "玩家身份、能力与处境",
  "player_goal": "玩家阶段目标",
  "world_lore": "运行时需要知道的世界规则",
  "start_scene_id": "scene_id",
  "player_name": "玩家",
  "player_stats": {"realm": "炼气一层", "qi": 0},
  "initial_items": ["物品"],
  "characters": [{
    "id": "npc_id", "name": "姓名", "role": "身份", "public_profile": "公开形象",
    "secret": "隐藏秘密", "goal": "角色目标", "speaking_style": "具体说话风格",
    "initial_location": "地点", "knowledge_boundaries": ["不知道什么", "何时才可透露什么"],
    "portrait_description": "供美术资产生成使用的单人全身立绘描述"
  }],
  "clues": [{
    "id": "clue_id", "title": "线索名", "description": "玩家看到的内容",
    "source_scene_id": "scene_id", "owner_character_id": "npc_id", "reveals": "揭示内容",
    "required_clue_ids": []
  }],
  "scenes": [{
    "id": "scene_id", "kind": "scene", "title": "场景标题", "location": "地点",
    "duration_minutes": 5, "objective": "玩家当前目标", "opening_narration": "具体开场叙事",
    "background_description": "供美术资产生成使用的空景背景描述，不出现人物和文字",
    "beats": [{
      "id": "beat_id", "kind": "dialogue", "speaker_id": "npc_id", "content": "完整台词",
      "purpose": "该节拍作用", "conditions": {}, "effects": {},
      "visual_description": "可选的镜头或人物状态描述"
    }],
    "choices": [{
      "id": "choice_id", "text": "玩家可见选项", "next_scene_id": "scene_id",
      "consequence_summary": "选择后果", "conditions": {}, "effects": {"set_flags": {"flag": true}}
    }],
    "default_next_scene_id": "scene_id", "unlock_clue_ids": [], "conditions": {}, "effects": {}
  }]
}

结局场景的 kind 必须为 ending，且 choices 为空、default_next_scene_id 为空。"""

    def _user_prompt(self, request: StoryAuthoringRequest) -> str:
        return json.dumps(
            {
                "创作需求": request.brief,
                "类型": request.genre,
                "基调": request.tone,
                "目标玩家": request.audience,
                "目标时长_分钟": request.target_minutes,
                "目标场景数": request.target_scene_count,
                "目标角色数": request.target_character_count,
                "硬性约束": request.constraints,
                "输出语言": request.language,
                "质量要求": {
                    "每场景至少具体NPC台词数": 2,
                    "必须包含": ["情境", "冲突", "台词", "玩家选择", "状态反馈", "可达结局"],
                    "禁止": ["只给任务清单", "只有剧情摘要", "无后果选择", "不存在的引用"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )

    def _repair_system_prompt(self) -> str:
        return """你是互动剧情平台内部的 StoryDraftRepairAgent。
你会收到一个已经通过 Pydantic Schema、但没有通过确定性剧情审查的 story_draft.v1 草案，以及逐条审查问题。
只返回修复后的完整 JSON 对象，不要 Markdown、解释或代码围栏。
必须逐条修复 severity=error 的问题，同时尽量保留原有标题、人物、台词、场景内容和用户要求。
所有引用必须使用实际存在的 id；所有场景从 start_scene_id 可达；非结局场景必须有后续；至少一个结局可达；结局不得有出边。
不要只返回修改片段，必须返回包含 characters、clues、scenes 的完整 story_draft.v1。"""

    def _repair_user_prompt(
        self,
        request: StoryAuthoringRequest,
        draft: StoryDraft,
        issues: list[dict],
    ) -> str:
        return json.dumps(
            {
                "任务": "依据确定性审查报告修复剧情草案，并返回完整草案",
                "原始创作要求": request.brief,
                "目标时长_分钟": request.target_minutes,
                "目标场景数": request.target_scene_count,
                "目标角色数": request.target_character_count,
                "硬性约束": request.constraints,
                "确定性审查问题": issues,
                "待修复完整草案": draft.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )


def _extract_json_object(raw: str) -> dict | None:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        return None
