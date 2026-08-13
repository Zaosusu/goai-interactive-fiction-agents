from __future__ import annotations

import json
import re
from pathlib import Path

from app.worlds.sandbox.models import WorldTemplateSummary


DATA_PATH = Path("data") / "world_templates.json"

DEFAULT_TEMPLATES = [
    WorldTemplateSummary(
        id="freeform",
        name="自由生成 / 不套模板",
        description="完全按用户主题生成世界、机制、NPC、任务和闭环。",
        structure_prompt="不套固定结构，优先保留用户主题与目标。",
        sort_order=0,
    ),
    WorldTemplateSummary(
        id="three_act_growth",
        name="三幕式成长线",
        description="起点困境、训练/探索、突破、结局选择。",
        structure_prompt="按三幕式成长结构组织：起点困境 -> 中段训练/探索 -> 关键突破 -> 结局选择。",
        sort_order=10,
    ),
    WorldTemplateSummary(
        id="short_drama_reversal",
        name="短剧反转线",
        description="压迫开局、获得条件、公开反转、最终裁决。",
        structure_prompt="按短剧反转结构组织：压迫开局 -> 获得条件 -> 公开反转 -> 最终裁决。",
        sort_order=20,
    ),
    WorldTemplateSummary(
        id="script_decomposition",
        name="剧本拆解",
        description="按标准 ScriptDecompositionResult 拆分公共背景、角色秘密、线索、时间线、约束和真相。",
        structure_prompt="使用 ScriptDecompositionAgent：先拆解为可审查 Script IR，再编译为可运行世界 JSON。",
        sort_order=25,
    ),
    WorldTemplateSummary(
        id="mystery_investigation",
        name="探案解谜线",
        description="接案、搜证、询问、推理、揭晓真相。",
        structure_prompt="按探案结构组织：接案 -> 搜证 -> 询问 -> 推理 -> 揭晓真相。",
        sort_order=30,
    ),
    WorldTemplateSummary(
        id="management_growth",
        name="经营养成线",
        description="资源、设施、人员、声望、阶段目标。",
        structure_prompt="按经营养成结构组织：资源获取 -> 设施/人员配置 -> 声望或产出提升 -> 阶段目标。",
        sort_order=40,
    ),
    WorldTemplateSummary(
        id="relationship_route",
        name="关系攻略线",
        description="相识、建立信任、冲突、选择、关系结局。",
        structure_prompt="按关系线组织：相识 -> 建立信任 -> 冲突或误会 -> 选择 -> 关系结局。",
        sort_order=50,
    ),
    WorldTemplateSummary(
        id="adventure_battle",
        name="冒险战斗线",
        description="接任务、探索区域、遭遇、成长、最终挑战。",
        structure_prompt="按冒险战斗结构组织：接任务 -> 探索区域 -> 遭遇挑战 -> 成长 -> 最终挑战。",
        sort_order=60,
    ),
    WorldTemplateSummary(
        id="document_adaptation",
        name="文档改编线",
        description="适合把设定集、小说片段、PDF/Word 改成可玩世界。",
        structure_prompt="优先保留导入文档中的角色、地点、冲突和物品，再改编为可玩闭环。",
        sort_order=70,
    ),
]


class WorldTemplateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DATA_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(DEFAULT_TEMPLATES)

    def list(self, include_disabled: bool = False) -> list[WorldTemplateSummary]:
        templates = self._read()
        if not include_disabled:
            templates = [template for template in templates if template.enabled]
        return sorted(templates, key=lambda item: (item.sort_order, item.name))

    def get(self, template_id: str) -> WorldTemplateSummary | None:
        safe_id = self.normalize_id(template_id)
        return next((item for item in self._read() if item.id == safe_id), None)

    def save(self, template: WorldTemplateSummary) -> WorldTemplateSummary:
        template.id = self.normalize_id(template.id or template.name)
        templates = [item for item in self._read() if item.id != template.id]
        templates.append(template)
        self._write(templates)
        return template

    def delete(self, template_id: str) -> None:
        safe_id = self.normalize_id(template_id)
        templates = [item for item in self._read() if item.id != safe_id]
        self._write(templates)

    def normalize_id(self, value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_\-]+", "_", value.strip().lower()).strip("_") or "template"

    def _read(self) -> list[WorldTemplateSummary]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [WorldTemplateSummary.model_validate(item) for item in data]

    def _write(self, templates: list[WorldTemplateSummary]) -> None:
        self.path.write_text(
            json.dumps([item.model_dump() for item in templates], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
