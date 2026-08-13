from __future__ import annotations

import re

from app.worlds.sandbox.models import (
    IntegrationAdapterPlan,
    ProjectActionCandidate,
    ProjectIntakeRequest,
    ProjectIntakeSummary,
    ProjectIntegrationAnalysis,
    ProjectMechanicCandidate,
    WorldGenerateRequest,
)


class ProjectIntakeAgent:
    def summarize(self, request: ProjectIntakeRequest) -> ProjectIntakeSummary:
        source = _source_text(request)
        project_type = _detect_project_type(source)
        mechanics = _mechanics_for_type(project_type, source)
        actions = _actions_for_type(project_type, source)
        locations = _extract_locations(source)
        npcs = _extract_npcs(source)
        player_fields = sorted({mechanic.path for mechanic in mechanics} | {"name", "location", "status", "inventory"})
        risks = _integration_risks(project_type, source, actions)
        theme = _theme(request, project_type)

        return ProjectIntakeSummary(
            project_name=request.project_name or _guess_project_name(source),
            project_type=project_type,
            theme=theme,
            source_summary=_summarize_source(source),
            player_fields=player_fields,
            npcs=npcs,
            locations=locations,
            candidate_mechanics=mechanics,
            candidate_actions=actions,
            integration_risks=risks,
            recommended_world_request=WorldGenerateRequest(
                template=_template_for_type(project_type),
                theme=theme,
                player_name=request.target_player or "玩家",
                world_name=request.project_name or "",
                complexity="medium",
                use_learned_profile=True,
            ),
        )


class IntegrationAdapterAgent:
    def plan(self, summary: ProjectIntakeSummary) -> IntegrationAdapterPlan:
        external_actions = [action for action in summary.candidate_actions if action.maps_to]
        external_apis = sorted({action.maps_to for action in external_actions})
        state_ownership = {field: "external_game" if _external_owned(field) else "agent_runtime" for field in summary.player_fields}
        risks = list(summary.integration_risks)
        if external_apis:
            risks.append("外部 API 动作必须由真实游戏服务端确认结果，Agent 不能伪造成败。")
        return IntegrationAdapterPlan(
            adapter_type="sandbox_json" if not external_apis else "world_adapter_required",
            world_input_mode="project_intake_summary",
            action_mappings=summary.candidate_actions,
            required_external_apis=external_apis,
            state_ownership=state_ownership,
            guardrails=[
                "NPC 只能引用 intake 中识别出的地点/NPC，新增地点必须进入世界配置。",
                "任务完成必须走 completion/evaluate_task_completions，不允许 NPC 口头完成。",
                "外部系统拥有的状态字段只能由外部 API 回写或 adapter action 写入。",
            ],
            risks=risks,
        )


def analyze_project_integration(request: ProjectIntakeRequest) -> ProjectIntegrationAnalysis:
    intake = ProjectIntakeAgent().summarize(request)
    adapter_plan = IntegrationAdapterAgent().plan(intake)
    return ProjectIntegrationAnalysis(intake=intake, adapter_plan=adapter_plan)


def _source_text(request: ProjectIntakeRequest) -> str:
    parts = [request.project_name, request.description, request.repo_hint, request.api_hint, *request.documents]
    return "\n".join(str(part or "").strip() for part in parts if str(part or "").strip())


def _detect_project_type(source: str) -> str:
    text = source.lower()
    rules = [
        ("cultivation_rpg", ["修仙", "境界", "宗门", "灵石", "妖兽", "cultivation", "realm"]),
        ("idol_training", ["偶像", "练习生", "舞台", "唱功", "舞蹈", "粉丝", "idol", "stage", "vocal"]),
        ("mystery_investigation", ["探案", "案件", "线索", "搜证", "嫌疑", "mystery", "clue"]),
        ("management_sim", ["经营", "商店", "设施", "资源", "声望", "management"]),
        ("relationship_route", ["恋爱", "好感", "信任", "关系", "relationship"]),
        ("adventure_battle", ["战斗", "冒险", "怪物", "副本", "battle", "monster"]),
    ]
    for project_type, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return project_type
    return "custom_story_world"


def _mechanics_for_type(project_type: str, source: str) -> list[ProjectMechanicCandidate]:
    base = {
        "cultivation_rpg": [
            ("realm_level", "境界等级"),
            ("cultivation", "修为"),
            ("battle_power", "战力"),
            ("spirit_stones", "灵石"),
        ],
        "idol_training": [
            ("skills.vocal", "唱功"),
            ("skills.dance", "舞蹈"),
            ("stage_presence", "舞台表现"),
            ("fan_count", "粉丝数"),
        ],
        "mystery_investigation": [
            ("clue_count", "线索数量"),
            ("deduction_score", "推理进度"),
            ("trust_level", "证人信任"),
        ],
        "management_sim": [
            ("funds", "资金"),
            ("reputation", "声望"),
            ("production", "产出"),
        ],
        "relationship_route": [
            ("trust_level", "信任"),
            ("affection", "好感"),
            ("conflict_resolved", "冲突解决"),
        ],
        "adventure_battle": [
            ("battle_power", "战力"),
            ("health", "生命"),
            ("inventory", "物品"),
        ],
    }.get(project_type, [("progress", "主线进度"), ("trust_level", "信任"), ("inventory", "物品")])
    mechanics = [ProjectMechanicCandidate(path=path, label=label, source=project_type) for path, label in base]
    for token in re.findall(r"\b([a-zA-Z][a-zA-Z0-9_]*\.[a-zA-Z0-9_.]+)\b", source):
        if token not in {item.path for item in mechanics}:
            mechanics.append(ProjectMechanicCandidate(path=token, label=token.split(".")[-1], source="source_text"))
    return mechanics


def _actions_for_type(project_type: str, source: str) -> list[ProjectActionCandidate]:
    presets = {
        "cultivation_rpg": [("train_cultivation", "修炼"), ("challenge_monster", "挑战妖兽"), ("report_to_master", "向师父复命")],
        "idol_training": [("practice_vocal", "声乐训练"), ("practice_dance", "舞蹈训练"), ("perform_on_stage", "登台表演")],
        "mystery_investigation": [("inspect_scene", "勘察现场"), ("question_witness", "询问证人"), ("deduce_truth", "推理真相")],
        "management_sim": [("collect_resource", "收集资源"), ("upgrade_facility", "升级设施"), ("settle_account", "结算收益")],
        "relationship_route": [("talk_private", "私下交谈"), ("resolve_conflict", "解决冲突"), ("make_choice", "做出选择")],
        "adventure_battle": [("explore_area", "探索区域"), ("fight_enemy", "战斗"), ("claim_reward", "领取奖励")],
    }.get(project_type, [("talk_to_npc", "交谈"), ("inspect_location", "观察地点"), ("advance_goal", "推进目标")])
    api_prefix = _api_prefix(source)
    return [
        ProjectActionCandidate(id=action_id, label=label, maps_to=f"{api_prefix}.{action_id}" if api_prefix else "", risk="")
        for action_id, label in presets
    ]


def _extract_locations(source: str) -> list[str]:
    candidates = re.findall(r"(?:地点|位置|场景|location)[:：]\s*([^\n；;]+)", source, flags=re.IGNORECASE)
    result: list[str] = []
    for chunk in candidates:
        for item in re.split(r"[,，、/ ]+", chunk):
            item = item.strip()
            if item and item not in result:
                result.append(item)
    return result[:12]


def _extract_npcs(source: str) -> list[str]:
    candidates = re.findall(r"(?:NPC|角色|人物|npcs?)[:：]\s*([^\n；;]+)", source, flags=re.IGNORECASE)
    result: list[str] = []
    for chunk in candidates:
        for item in re.split(r"[,，、/ ]+", chunk):
            item = item.strip()
            if item and item not in result:
                result.append(item)
    return result[:12]


def _integration_risks(project_type: str, source: str, actions: list[ProjectActionCandidate]) -> list[str]:
    risks: list[str] = []
    text = source.lower()
    if project_type in {"adventure_battle", "cultivation_rpg"} or any(word in text for word in ["战斗", "battle", "经济", "交易", "背包"]):
        risks.append("战斗、经济、背包等结果应由外部游戏服务端裁决，Agent 只能请求动作。")
    if any(action.maps_to for action in actions):
        risks.append("需要实现 WorldAdapter，把候选动作映射到真实 API 或 sandbox action。")
    if not _extract_locations(source):
        risks.append("输入中缺少明确地点，WorldBuilderAgent 需要补起点和可移动地点。")
    return risks


def _template_for_type(project_type: str) -> str:
    return {
        "mystery_investigation": "mystery_investigation",
        "management_sim": "management_growth",
        "relationship_route": "relationship_route",
        "adventure_battle": "adventure_battle",
    }.get(project_type, "freeform")


def _theme(request: ProjectIntakeRequest, project_type: str) -> str:
    base = request.description.strip() or request.project_name.strip() or project_type
    return base[:1200]


def _summarize_source(source: str) -> str:
    text = re.sub(r"\s+", " ", source).strip()
    return text[:1000]


def _guess_project_name(source: str) -> str:
    first = source.strip().splitlines()[0] if source.strip() else ""
    return first[:40] or "未命名接入项目"


def _api_prefix(source: str) -> str:
    match = re.search(r"([a-zA-Z_][a-zA-Z0-9_.]*API|[a-zA-Z_][a-zA-Z0-9_.]*Service)", source)
    return match.group(1) if match else ""


def _external_owned(field: str) -> bool:
    return any(token in field for token in ["battle", "health", "funds", "inventory", "spirit_stones", "fan_count"])
