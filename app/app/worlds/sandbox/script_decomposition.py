from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from inspect import isawaitable

from app.agents.script_decomposition.compiler import ScriptGraphCompiler
from app.core.text_generation import OpenAICompatibleTextGenerationClient, TextGenerationClient
from app.worlds.sandbox.models import (
    SandboxAction,
    SandboxNPC,
    SandboxTask,
    SandboxWorldConfig,
    ScriptCharacterSheet,
    ScriptCharacterInput,
    ScriptClueSheet,
    ScriptClueInput,
    ScriptDecompositionBuildResponse,
    ScriptDecompositionResult,
    ScriptDecompositionReport,
    ScriptDecompositionRequest,
    ScriptEndingSheet,
    ScriptEndingInput,
    ScriptStoryEntity,
    ScriptStoryEvidence,
    ScriptStoryGraphFacts,
    ScriptStoryRelation,
)
from app.worlds.sandbox.validator import SandboxWorldValidator


REQUIRED_SECTIONS = {
    "案件真相": "truth",
    "公共背景": "public_background",
    "角色": "characters",
    "线索": "clues",
    "地点": "locations",
}


class ScriptDecompositionAgent:
    def __init__(
        self,
        compiler: "ScriptWorldCompiler | None" = None,
        text_client: TextGenerationClient | None = None,
        progress_callback: Callable[[str, str], object] | None = None,
    ) -> None:
        self.compiler = compiler or ScriptWorldCompiler()
        self.text_client = text_client
        self.progress_callback = progress_callback
        self.last_llm_raw = ""
        self.last_llm_error = ""
        self._stream_chars = 0
        self._stream_next_emit = 400
        self._stream_preview = ""

    def decompose(self, request: ScriptDecompositionRequest) -> ScriptDecompositionResult:
        case = normalize_script_request(request)
        report = validate_script_decomposition(case)
        return script_request_to_decomposition(case, report)

    async def decompose_async(self, request: ScriptDecompositionRequest) -> ScriptDecompositionResult:
        if request.decomposition_mode.lower() not in {"llm", "agent", "model"}:
            return self.decompose(request)
        await self._emit("ScriptDecompositionAgent", "Start LLM decomposition.")
        case = await self._decompose_with_llm(request)
        await self._emit("ScriptGraphValidationAgent", "Validate graph nodes, relation endpoints, evidence, and playable structure.")
        report = validate_script_decomposition(case)
        decomposition = script_request_to_decomposition(case, report)
        review = ScriptDecompositionReviewAgent().review(request, decomposition, report, self.last_llm_error)
        calibration = ScriptRuleCalibrationAgent().calibrate(request, decomposition, report, review)
        metadata = dict(decomposition.metadata or {})
        metadata["decomposition_mode"] = "llm_agent"
        metadata["decomposition_model"] = _safe_llm_metadata(request.decomposition_llm)
        metadata["review"] = review
        metadata["rule_calibration"] = calibration
        if self.last_llm_raw:
            metadata["llm_raw_excerpt"] = self.last_llm_raw[:2000]
        if self.last_llm_error:
            metadata["llm_error"] = self.last_llm_error
        await self._emit("ScriptDecompositionReviewAgent", _review_summary(review))
        await self._emit("ScriptRuleCalibrationAgent", _calibration_summary(calibration))
        return decomposition.model_copy(update={"metadata": metadata})

    def build(self, request: ScriptDecompositionRequest) -> ScriptDecompositionBuildResponse:
        decomposition = self.decompose(request)
        return self.compiler.compile(decomposition)

    async def build_async(self, request: ScriptDecompositionRequest) -> ScriptDecompositionBuildResponse:
        decomposition = await self.decompose_async(request)
        return self.compiler.compile(decomposition)

    async def decompose_response_async(self, request: ScriptDecompositionRequest) -> ScriptDecompositionBuildResponse:
        decomposition = await self.decompose_async(request)
        report = validate_script_decomposition(decomposition_to_script_request(decomposition))
        return ScriptDecompositionBuildResponse(world=None, report=report, decomposition=decomposition)

    async def _decompose_with_llm(self, request: ScriptDecompositionRequest) -> ScriptDecompositionRequest:
        if not request.source_text.strip():
            return normalize_script_request(request)
        chunks = _source_chunks(request.source_text)
        if len(chunks) > 1:
            cases: list[ScriptDecompositionRequest] = []
            errors: list[str] = []
            await self._emit("ScriptDecompositionAgent", f"Split source into {len(chunks)} document chunks.")
            for index, (chunk_name, chunk_text) in enumerate(chunks, start=1):
                await self._emit(
                    "ScriptDecompositionAgent",
                    f"Decomposing chunk {index}/{len(chunks)}: {chunk_name} ({len(chunk_text)} chars).",
                )
                chunk_request = request.model_copy(
                    update={
                        "source_text": chunk_text,
                        "title": request.title or chunk_name,
                    }
                )
                case = await self._decompose_single_with_llm(chunk_request)
                if self.last_llm_error:
                    errors.append(f"{chunk_name}: {self.last_llm_error}")
                await self._emit(
                    "ScriptDecompositionAgent",
                    f"Chunk {index}/{len(chunks)} extracted {len(case.characters)} characters and {len(case.clues)} clues.",
                )
                cases.append(case)
            self.last_llm_error = "; ".join(errors)
            await self._emit("ScriptDecompositionAgent", "Merging chunk decompositions into one script JSON.")
            return _merge_script_requests(request, cases)
        return await self._decompose_single_with_llm(request)

    async def _decompose_single_with_llm(self, request: ScriptDecompositionRequest) -> ScriptDecompositionRequest:
        client = self.text_client or OpenAICompatibleTextGenerationClient(request.decomposition_llm, purpose="world_builder")
        system_prompt = (
            "你是 ScriptDecompositionAgent。你的任务是从剧本、小说章节、设定集或游戏资产文档中抽取结构化 JSON。"
            "只返回合法 JSON 对象，不要 Markdown。不要编造原文没有的事实；但原文出现的人物、地点、物品、线索、任务、剧情必须抽取。"
            "必须用图数据库/知识图谱的方式理解故事：先识别实体，再识别实体之间的关系、事件因果、任务依赖和证据来源。"
            "如果原文是小说章节，clues 可以是关键物品、事件、知识、伏笔、任务触发器。保留中文名称。"
        )
        user_prompt = _decomposition_user_prompt(request, force=False)
        try:
            await self._emit("ScriptDecomposition LLM", f"Sending decomposition prompt ({len(user_prompt)} chars).")
            self._reset_stream_counter()
            raw = await self._generate_text(client, system_prompt, user_prompt)
        except Exception as exc:
            self.last_llm_error = f"{type(exc).__name__}: {exc}"
            self.last_llm_raw = ""
            await self._emit("ScriptDecomposition LLM", f"Model call failed: {self.last_llm_error}")
            return normalize_script_request(request)
        self.last_llm_raw = raw
        self.last_llm_error = ""
        await self._emit("ScriptDecomposition LLM", f"Received response ({len(raw)} chars); parsing JSON.")
        payload = _extract_json_object(raw)
        if payload is None:
            self.last_llm_error = "llm_returned_non_json"
            await self._emit("ScriptDecompositionAgent", "Model response was not valid JSON; falling back to deterministic parser.")
            return normalize_script_request(request)
        case = _script_request_from_agent_json(payload, request)
        if request.source_text.strip() and (len(case.characters) < 1 or len(case.clues) < 1 or not case.core_plot):
            retry_prompt = _decomposition_user_prompt(request, force=True, previous_output=raw)
            try:
                await self._emit("ScriptDecompositionAgent", "First pass looked incomplete; retrying extraction with stricter instructions.")
                self._reset_stream_counter()
                retry_raw = await self._generate_text(client, system_prompt, retry_prompt)
            except Exception as exc:
                self.last_llm_error = f"retry_{type(exc).__name__}: {exc}"
                await self._emit("ScriptDecomposition LLM", f"Retry failed: {self.last_llm_error}")
                return case
            self.last_llm_raw = retry_raw
            retry_payload = _extract_json_object(retry_raw)
            if retry_payload is not None:
                retry_case = _script_request_from_agent_json(retry_payload, request)
                if len(retry_case.characters) >= len(case.characters) and len(retry_case.clues) >= len(case.clues):
                    case = retry_case
            else:
                self.last_llm_error = "llm_retry_returned_non_json"
                await self._emit("ScriptDecompositionAgent", "Retry response was not valid JSON.")
        return case

    async def _emit(self, title: str, detail: str) -> None:
        if not self.progress_callback:
            return
        result = self.progress_callback(title, detail)
        if isawaitable(result):
            await result

    async def _generate_text(self, client: TextGenerationClient, system_prompt: str, user_prompt: str) -> str:
        try:
            return await client.generate_text(system_prompt, user_prompt, on_token=self._on_stream_token)
        except TypeError:
            return await client.generate_text(system_prompt, user_prompt)

    def _reset_stream_counter(self) -> None:
        self._stream_chars = 0
        self._stream_next_emit = 400
        self._stream_preview = ""

    async def _on_stream_token(self, token: str) -> None:
        self._stream_chars += len(token)
        self._stream_preview = (self._stream_preview + token)[-1200:]
        if self._stream_chars >= self._stream_next_emit:
            preview = _format_stream_preview(self._stream_preview)
            detail = f"LLM 正在流式返回 script_json，已收到 {self._stream_chars} 字符。"
            if preview:
                detail += f"\n预览：{preview}"
            await self._emit("ScriptDecomposition LLM", detail)
            self._stream_next_emit += 800


class ScriptDecompositionReviewAgent:
    def review(
        self,
        request: ScriptDecompositionRequest,
        decomposition: ScriptDecompositionResult,
        report: ScriptDecompositionReport,
        llm_error: str = "",
    ) -> dict:
        issues: list[dict[str, str]] = []
        if llm_error:
            issues.append({"severity": "error", "area": "llm", "message": llm_error})
        if report.errors:
            issues.extend({"severity": "error", "area": "story_graph", "message": item} for item in report.errors)
        if report.unresolved_references:
            issues.append(
                {
                    "severity": "error",
                    "area": "story_graph",
                    "message": "Unresolved graph references: " + ", ".join(report.unresolved_references),
                }
            )
        if report.isolated_nodes:
            issues.append(
                {
                    "severity": "warning",
                    "area": "story_graph",
                    "message": "Isolated graph nodes need review: " + ", ".join(report.isolated_nodes[:12]),
                }
            )
        if report.ontology_warnings:
            issues.extend({"severity": "warning", "area": "ontology", "message": item} for item in report.ontology_warnings)
        if request.source_text and report.node_count == 0:
            issues.append({"severity": "error", "area": "coverage", "message": "No story graph nodes were extracted from non-empty source text."})
        return {
            "reviewer": "ScriptDecompositionReviewAgent",
            "passed": not any(item["severity"] == "error" for item in issues),
            "issues": issues,
            "notes": [
                "Review checks observable story-graph quality, relation coverage, and evidence coverage; it does not expose hidden model reasoning.",
                "A passed graph structure means the artifact is compilable, not that every narrative omission has been found.",
            ],
        }


class ScriptRuleCalibrationAgent:
    def calibrate(
        self,
        request: ScriptDecompositionRequest,
        decomposition: ScriptDecompositionResult,
        report: ScriptDecompositionReport,
        review: dict,
    ) -> dict:
        text = request.source_text or ""
        material_type = "case_script"
        if "## Source File" in text or "章节" in text or "chapter" in text.lower():
            material_type = "multi_chapter_novel"
        elif not decomposition.truth and decomposition.core_plot:
            material_type = "narrative_script_asset"
        recommended_rules = {
            "require_story_graph": True,
            "minimum_character_nodes": 1,
            "minimum_location_or_scene_nodes": 1,
            "minimum_playable_asset_nodes": 1,
            "minimum_relation_edges": 1,
            "require_resolved_relation_endpoints": True,
            "require_evidence_for_key_nodes": True,
            "playable_relation_types": [
                "LOCATED_AT",
                "OCCURS_AT",
                "FOUND_AT",
                "INVOLVES",
                "REVEALS",
                "DEPENDS_ON",
                "NEXT_EVENT",
                "CAUSES",
                "OWNS",
                "KNOWS_SECRET",
            ],
        }
        return {
            "agent": "ScriptRuleCalibrationAgent",
            "material_type": material_type,
            "recommended_rules": recommended_rules,
            "graph_validation_passed": report.passed,
            "review_passed": bool(review.get("passed")),
            "next_action": "revise_prompt_or_retry_by_chunks" if not review.get("passed") else "human_review_or_compile",
        }


class ScriptWorldCompiler:
    def compile(self, decomposition: ScriptDecompositionResult) -> ScriptDecompositionBuildResponse:
        request = decomposition_to_script_request(decomposition)
        report = validate_script_decomposition(request)
        world = decomposition_to_world(request, report, decomposition)
        return ScriptDecompositionBuildResponse(world=world, report=report, decomposition=decomposition)


def build_script_world(request: ScriptDecompositionRequest) -> ScriptDecompositionBuildResponse:
    return ScriptDecompositionAgent().build(request)


async def build_script_world_async(request: ScriptDecompositionRequest) -> ScriptDecompositionBuildResponse:
    return await ScriptDecompositionAgent().build_async(request)


async def build_script_world_async_with_progress(
    request: ScriptDecompositionRequest,
    progress_callback: Callable[[str, str], object] | None = None,
) -> ScriptDecompositionBuildResponse:
    return await ScriptDecompositionAgent(progress_callback=progress_callback).build_async(request)


async def decompose_script_async_with_progress(
    request: ScriptDecompositionRequest,
    progress_callback: Callable[[str, str], object] | None = None,
) -> ScriptDecompositionBuildResponse:
    return await ScriptDecompositionAgent(progress_callback=progress_callback).decompose_response_async(request)


def _review_summary(review: dict) -> str:
    issues = review.get("issues") or []
    return f"Review {'passed' if review.get('passed') else 'failed'} with {len(issues)} issue(s)."


def _calibration_summary(calibration: dict) -> str:
    return (
        f"Material type: {calibration.get('material_type') or 'unknown'}; "
        f"next action: {calibration.get('next_action') or 'unknown'}."
    )


def _safe_llm_metadata(config: object | None) -> dict:
    if not config:
        return {}
    data = config.model_dump() if hasattr(config, "model_dump") else dict(config)
    return {
        "provider": data.get("provider", ""),
        "model": data.get("model", ""),
        "temperature": data.get("temperature"),
        "timeout": data.get("timeout"),
        "max_retries": data.get("max_retries"),
        "has_api_key": bool(data.get("api_key") or data.get("api_key_env")),
        "has_base_url": bool(data.get("base_url") or data.get("base_url_env")),
    }


def _format_stream_preview(text: str, limit: int = 260) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return ""
    if len(normalized) <= limit:
        return normalized
    return "..." + normalized[-limit:]


def _source_chunks(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"(?:^|\n)## Source File\s+\d+:\s*(.+?)\n", text))
    if not matches:
        return [("source_text", text)]
    chunks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        name = match.group(1).strip() or f"source_{index + 1}"
        chunk = text[start:end].strip()
        if chunk:
            chunks.append((name, chunk))
    return chunks or [("source_text", text)]


def _merge_script_requests(original: ScriptDecompositionRequest, cases: list[ScriptDecompositionRequest]) -> ScriptDecompositionRequest:
    merged = ScriptDecompositionRequest(
        case_id=original.case_id,
        title=original.title or next((case.title for case in cases if case.title), ""),
        player_name=original.player_name,
        source_text=original.source_text,
        public_background="\n\n".join(dict.fromkeys(case.public_background for case in cases if case.public_background)),
        core_plot="\n\n".join(dict.fromkeys(case.core_plot for case in cases if case.core_plot)),
        truth=next((case.truth for case in cases if case.truth), ""),
        hidden_threads=list(dict.fromkeys(item for case in cases for item in case.hidden_threads if item)),
        timeline=list(dict.fromkeys(item for case in cases for item in case.timeline if item)),
        locations=list(dict.fromkeys(item for case in cases for item in case.locations if item)),
        forbidden_spoilers=list(dict.fromkeys(item for case in cases for item in case.forbidden_spoilers if item)),
        decomposition_mode=original.decomposition_mode,
        decomposition_llm=original.decomposition_llm,
    )
    merged.characters = _dedupe_characters([item for case in cases for item in case.characters])
    merged.clues = _dedupe_clues([item for case in cases for item in case.clues])
    merged.endings = [item for case in cases for item in case.endings]
    merged.story_graph = _merge_story_graphs([case.story_graph for case in cases])
    return merged


def _dedupe_characters(characters: list[ScriptCharacterInput]) -> list[ScriptCharacterInput]:
    by_name: dict[str, ScriptCharacterInput] = {}
    for character in characters:
        key = character.name.strip()
        if not key:
            continue
        existing = by_name.get(key)
        if existing is None:
            by_name[key] = character
            continue
        by_name[key] = existing.model_copy(
            update={
                "public_info": existing.public_info or character.public_info,
                "secret": existing.secret or character.secret,
                "motive": existing.motive or character.motive,
                "alibi": existing.alibi or character.alibi,
                "location": existing.location or character.location,
            }
        )
    return list(by_name.values())


def _dedupe_clues(clues: list[ScriptClueInput]) -> list[ScriptClueInput]:
    by_title: dict[str, ScriptClueInput] = {}
    for clue in clues:
        key = clue.title.strip()
        if not key:
            continue
        existing = by_title.get(key)
        if existing is None:
            by_title[key] = clue
            continue
        by_title[key] = existing.model_copy(
            update={
                "content": existing.content or clue.content,
                "source": existing.source or clue.source,
                "location": existing.location or clue.location,
                "owner": existing.owner or clue.owner,
                "reveals": existing.reveals or clue.reveals,
                "trigger": existing.trigger or clue.trigger,
            }
        )
    return list(by_title.values())


def _merge_story_graphs(graphs: list[ScriptStoryGraphFacts]) -> ScriptStoryGraphFacts:
    entities: dict[str, ScriptStoryEntity] = {}
    relations: dict[str, ScriptStoryRelation] = {}
    uncertainties: list[str] = []
    contradictions: list[str] = []
    for graph in graphs:
        for entity in graph.entities:
            key = entity.id or _safe_id(entity.name, "entity")
            if key not in entities:
                entities[key] = entity
        for relation in graph.relations:
            key = relation.id or f"{relation.source}|{relation.type}|{relation.target}|{relation.description}"
            if key not in relations:
                relations[key] = relation
        uncertainties.extend(graph.uncertainties)
        contradictions.extend(graph.contradictions)
    return ScriptStoryGraphFacts(
        entities=list(entities.values()),
        relations=list(relations.values()),
        uncertainties=list(dict.fromkeys(item for item in uncertainties if item)),
        contradictions=list(dict.fromkeys(item for item in contradictions if item)),
    )


def normalize_script_request(request: ScriptDecompositionRequest) -> ScriptDecompositionRequest:
    if request.source_text and not (request.characters or request.clues or request.truth or request.core_plot):
        parsed = parse_script_text(request.source_text)
        return request.model_copy(
            update={
                "title": request.title or parsed.title,
                "core_plot": request.core_plot or parsed.core_plot,
                "hidden_threads": request.hidden_threads or parsed.hidden_threads,
                "truth": request.truth or parsed.truth,
                "public_background": request.public_background or parsed.public_background,
                "characters": request.characters or parsed.characters,
                "clues": request.clues or parsed.clues,
                "endings": request.endings or parsed.endings,
                "timeline": request.timeline or parsed.timeline,
                "locations": request.locations or parsed.locations,
                "forbidden_spoilers": request.forbidden_spoilers or parsed.forbidden_spoilers,
            }
        )
    return request


def _extract_json_object(raw: str) -> dict | None:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _decomposition_user_prompt(
    request: ScriptDecompositionRequest,
    force: bool = False,
    previous_output: str = "",
) -> str:
    source_text = request.source_text[:45000]
    truncated_note = ""
    if len(request.source_text) > len(source_text):
        truncated_note = f"\n\n注意：原始文本共 {len(request.source_text)} 字，本次只提供前 {len(source_text)} 字；如果需要完整长篇，应先分卷/分章节批处理再汇总。"
    force_rules = ""
    if force:
        force_rules = (
            "\n\n上一次输出没有抽取到足够的人物/线索/剧情。请重新阅读 SOURCE_TEXT。"
            "只要原文出现人物名、地点、物品、事件、任务或伏笔，就必须抽取。"
            "禁止回答无法识别，除非 SOURCE_TEXT 真的为空。"
            f"\n上一次输出摘录：\n{previous_output[:1500]}\n"
        )
    return f"""
SOURCE_TEXT_BEGIN
{source_text}
SOURCE_TEXT_END
{truncated_note}
{force_rules}

请从 SOURCE_TEXT 中抽取并只返回以下 JSON 对象：
{{
  "case_id": "",
  "title": "{request.title or ''}",
  "player_name": "{request.player_name or '主角'}",
  "public_background": "",
  "core_plot": "",
  "hidden_threads": [],
  "truth": "",
  "timeline": [],
  "locations": [],
  "forbidden_spoilers": [],
  "characters": [
    {{"id": "", "name": "", "role": "", "public_info": "", "secret": "", "motive": "", "alibi": "", "location": ""}}
  ],
  "clues": [
    {{"id": "", "title": "", "content": "", "source": "", "location": "", "owner": "", "reveals": "", "trigger": ""}}
  ],
  "endings": [],
  "story_graph": {{
    "entities": [
      {{"id": "", "kind": "character|location|item|event|secret|task|organization|rule|chapter", "name": "", "aliases": [], "description": "", "properties": {{}}, "evidence": [{{"source": "", "text": "", "confidence": "high|medium|low"}}]}}
    ],
    "relations": [
      {{"id": "", "source": "", "target": "", "type": "LOCATED_AT|OWNS|KNOWS_SECRET|REVEALS|CAUSES|DEPENDS_ON|INVOLVES|MENTIONS|NEXT_EVENT|CONFLICTS_WITH", "description": "", "properties": {{}}, "evidence": [{{"source": "", "text": "", "confidence": "high|medium|low"}}], "confidence": "high|medium|low"}}
    ],
    "uncertainties": [],
    "contradictions": []
  }}
}}

抽取规则：
- title 优先使用用户标题，其次使用文档/小说/剧本名。
- public_background 是世界观、时代背景、公共设定。
- core_plot 是本批章节/本单元可游玩的主线剧情；非案件型小说也必须填。
- truth 只有原文明确给出案件真相才填；普通小说可为空。
- characters 至少抽取主要人物；不要因为不是案件型剧本就留空。
- clues 抽取关键物品、线索、伏笔、任务触发器、异常事件、知识资产；普通小说也要抽。
- locations 抽取明确地点。
- story_graph 是核心输出，不是可选装饰。必须把故事拆成实体和关系，而不只是填 characters/clues/locations。
- story_graph.entities 至少覆盖主要人物、地点、关键物品/线索、关键事件、秘密/暗线、任务触发器。
- story_graph.relations 必须表达“谁在哪里、谁拥有/知道什么、哪个线索揭示什么、哪个事件导致哪个事件、任务依赖什么、章节顺序/事件顺序”。
- 每个重要实体和关系尽量给 evidence.text，引用或概括原文依据；缺乏依据的关系不要编。
- 不确定或冲突的信息写入 uncertainties / contradictions，不要硬判。
- 不要编造原文没有的事实。
- 返回 JSON，不要解释。
""".strip()


def _script_request_from_agent_json(payload: dict, original: ScriptDecompositionRequest) -> ScriptDecompositionRequest:
    request = ScriptDecompositionRequest(
        case_id=str(payload.get("case_id") or original.case_id or ""),
        title=str(payload.get("title") or original.title or ""),
        player_name=str(payload.get("player_name") or original.player_name or "主角"),
        source_text=original.source_text,
        public_background=str(payload.get("public_background") or ""),
        core_plot=str(payload.get("core_plot") or ""),
        hidden_threads=[str(item) for item in _as_list(payload.get("hidden_threads")) if item],
        truth=str(payload.get("truth") or ""),
        timeline=[str(item) for item in _as_list(payload.get("timeline")) if item],
        locations=[str(item) for item in _as_list(payload.get("locations")) if item],
        forbidden_spoilers=[str(item) for item in _as_list(payload.get("forbidden_spoilers")) if item],
        characters=[
            ScriptCharacterInput(
                id=str(item.get("id") or ""),
                name=str(item.get("name") or ""),
                role=str(item.get("role") or "NPC"),
                public_info=str(item.get("public_info") or ""),
                secret=str(item.get("secret") or ""),
                motive=str(item.get("motive") or ""),
                alibi=str(item.get("alibi") or ""),
                location=str(item.get("location") or ""),
            )
            for item in _as_dicts(payload.get("characters"))
            if str(item.get("name") or "").strip()
        ],
        clues=[
            ScriptClueInput(
                id=str(item.get("id") or ""),
                title=str(item.get("title") or ""),
                content=str(item.get("content") or ""),
                source=str(item.get("source") or ""),
                location=str(item.get("location") or ""),
                owner=str(item.get("owner") or ""),
                reveals=str(item.get("reveals") or ""),
                trigger=str(item.get("trigger") or ""),
            )
            for item in _as_dicts(payload.get("clues"))
            if str(item.get("title") or "").strip()
        ],
        endings=[
            ScriptEndingInput(
                id=str(item.get("id") or ""),
                title=str(item.get("title") or ""),
                condition=str(item.get("condition") or ""),
                reveal=str(item.get("reveal") or ""),
            )
            for item in _as_dicts(payload.get("endings"))
            if str(item.get("title") or "").strip()
        ],
        decomposition_mode=original.decomposition_mode,
        decomposition_llm=original.decomposition_llm,
    )
    request.story_graph = _story_graph_from_agent_json(payload.get("story_graph") or {})
    return request


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _as_dicts(value) -> list[dict]:
    return [item for item in _as_list(value) if isinstance(item, dict)]


def _story_graph_from_agent_json(payload: dict) -> ScriptStoryGraphFacts:
    if not isinstance(payload, dict):
        return ScriptStoryGraphFacts()
    return ScriptStoryGraphFacts(
        entities=[
            ScriptStoryEntity(
                id=str(item.get("id") or ""),
                kind=str(item.get("kind") or "entity"),
                name=str(item.get("name") or ""),
                aliases=[str(alias) for alias in _as_list(item.get("aliases")) if alias],
                description=str(item.get("description") or ""),
                properties=item.get("properties") if isinstance(item.get("properties"), dict) else {},
                evidence=_story_evidence_list(item.get("evidence")),
            )
            for item in _as_dicts(payload.get("entities"))
            if str(item.get("id") or item.get("name") or "").strip()
        ],
        relations=[
            ScriptStoryRelation(
                id=str(item.get("id") or ""),
                source=str(item.get("source") or ""),
                target=str(item.get("target") or ""),
                type=str(item.get("type") or "RELATED_TO").upper(),
                description=str(item.get("description") or ""),
                properties=item.get("properties") if isinstance(item.get("properties"), dict) else {},
                evidence=_story_evidence_list(item.get("evidence")),
                confidence=str(item.get("confidence") or "medium"),
            )
            for item in _as_dicts(payload.get("relations"))
            if str(item.get("source") or "").strip() and str(item.get("target") or "").strip()
        ],
        uncertainties=[str(item) for item in _as_list(payload.get("uncertainties")) if item],
        contradictions=[str(item) for item in _as_list(payload.get("contradictions")) if item],
    )


def _story_evidence_list(value) -> list[ScriptStoryEvidence]:
    evidence: list[ScriptStoryEvidence] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            evidence.append(
                ScriptStoryEvidence(
                    source=str(item.get("source") or ""),
                    text=str(item.get("text") or ""),
                    confidence=str(item.get("confidence") or "medium"),
                )
            )
        elif item:
            evidence.append(ScriptStoryEvidence(text=str(item), confidence="medium"))
    return evidence


def parse_script_text(text: str) -> ScriptDecompositionRequest:
    sections = _sections(text)
    title = _first_line(sections.get("标题", "")) or _first_line(sections.get("剧本名", "")) or _document_title(text) or "未命名剧本"
    characters = _parse_characters(sections.get("角色", ""))
    clues = _parse_clues(sections.get("线索", ""))
    endings = _parse_endings(sections.get("结局", ""))
    public_background = sections.get("公共背景", "") or sections.get("世界观", "")
    truth = sections.get("案件真相", "")
    core_plot = sections.get("核心剧情", "") or sections.get("任务", "") or truth
    hidden_threads = _parse_hidden_threads(sections.get("核心剧情", ""))
    locations = _split_items(sections.get("地点", ""))
    locations.extend(_parse_locations(sections.get("场景", "")))
    locations.extend(_table_locations(text))
    constraints = _lines(sections.get("禁止提前泄露", ""))
    constraints.extend(_parse_constraints(sections.get("约束", "")))
    return ScriptDecompositionRequest(
        title=title,
        public_background=public_background,
        core_plot=core_plot,
        hidden_threads=hidden_threads,
        truth=truth,
        timeline=_lines(sections.get("时间线", "")),
        locations=list(dict.fromkeys(item for item in locations if item)),
        characters=characters,
        clues=clues,
        endings=endings,
        forbidden_spoilers=list(dict.fromkeys(item for item in constraints if item)),
    )


def script_request_to_decomposition(
    case: ScriptDecompositionRequest,
    report: ScriptDecompositionReport | None = None,
) -> ScriptDecompositionResult:
    report = report or validate_script_decomposition(case)
    locations = _case_locations(case)
    story_graph = case.story_graph
    if not story_graph.entities and not story_graph.relations:
        story_graph = _fallback_story_graph(case, locations)
    return ScriptDecompositionResult(
        script_id=case.case_id,
        script_type="case_investigation",
        title=case.title,
        player_name=case.player_name or "侦探",
        public_background=case.public_background,
        core_plot=case.core_plot,
        hidden_threads=list(case.hidden_threads),
        truth=case.truth,
        timeline=list(case.timeline),
        locations=locations,
        characters=[
            ScriptCharacterSheet(
                id=character.id,
                name=character.name,
                role=character.role,
                public_info=character.public_info,
                secret=character.secret,
                motive=character.motive,
                alibi=character.alibi,
                location=character.location,
            )
            for character in case.characters
        ],
        clues=[
            ScriptClueSheet(
                id=clue.id,
                title=clue.title,
                content=clue.content,
                source=clue.source,
                location=clue.location,
                owner=clue.owner,
                reveals=clue.reveals,
                trigger=clue.trigger,
            )
            for clue in case.clues
        ],
        endings=[
            ScriptEndingSheet(
                id=ending.id,
                title=ending.title,
                condition=ending.condition,
                reveal=ending.reveal,
            )
            for ending in case.endings
        ],
        constraints=list(case.forbidden_spoilers),
        story_graph=story_graph,
        world_mapping={
            "world_id": case.case_id or _safe_id(case.title, "script_case"),
            "template": "script_decomposition",
            "task_strategy": ["collect_clues", "question_suspects", "deduce_truth"],
            "mechanics": ["clue_count", "questioned_count", "truth_revealed"],
        },
        report=report.model_dump(),
        metadata={
            "decomposed_by": "script_decomposition_agent",
            "source_schema": "script_decomposition",
        },
    )


def decomposition_to_script_request(decomposition: ScriptDecompositionResult) -> ScriptDecompositionRequest:
    return ScriptDecompositionRequest(
        case_id=decomposition.script_id,
        title=decomposition.title,
        player_name=decomposition.player_name or "侦探",
        public_background=decomposition.public_background,
        core_plot=decomposition.core_plot,
        hidden_threads=list(decomposition.hidden_threads),
        truth=decomposition.truth,
        timeline=list(decomposition.timeline),
        locations=list(decomposition.locations),
        forbidden_spoilers=list(decomposition.constraints),
        characters=[
            ScriptCharacterInput(
                id=character.id,
                name=character.name,
                role=character.role,
                public_info=character.public_info,
                secret=character.secret,
                motive=character.motive,
                alibi=character.alibi,
                location=character.location,
            )
            for character in decomposition.characters
        ],
        clues=[
            ScriptClueInput(
                id=clue.id,
                title=clue.title,
                content=clue.content,
                source=clue.source,
                location=clue.location,
                owner=clue.owner,
                reveals=clue.reveals,
                trigger=clue.trigger,
            )
            for clue in decomposition.clues
        ],
        endings=[
            ScriptEndingInput(
                id=ending.id,
                title=ending.title,
                condition=ending.condition,
                reveal=ending.reveal,
            )
            for ending in decomposition.endings
        ],
        story_graph=decomposition.story_graph,
    )


def _fallback_story_graph(case: ScriptDecompositionRequest, locations: list[str]) -> ScriptStoryGraphFacts:
    entities: list[ScriptStoryEntity] = []
    relations: list[ScriptStoryRelation] = []

    script_id = case.case_id or _safe_id(case.title, "script")
    entities.append(
        ScriptStoryEntity(
            id=script_id,
            kind="script",
            name=case.title or script_id,
            description=case.core_plot or case.public_background,
            properties={"public_background": case.public_background, "core_plot": case.core_plot},
        )
    )
    if case.truth:
        truth_id = f"{script_id}:truth"
        entities.append(ScriptStoryEntity(id=truth_id, kind="secret", name="truth", description=case.truth))
        relations.append(ScriptStoryRelation(source=script_id, target=truth_id, type="HAS_TRUTH", confidence="high"))

    location_ids: dict[str, str] = {}
    for location in locations:
        location_id = _safe_id(location, f"{script_id}:location_{len(location_ids) + 1}")
        location_ids[location] = location_id
        entities.append(ScriptStoryEntity(id=location_id, kind="location", name=location))
        relations.append(ScriptStoryRelation(source=script_id, target=location_id, type="HAS_LOCATION", confidence="high"))

    character_ids: dict[str, str] = {}
    for index, character in enumerate(case.characters, start=1):
        character_id = character.id or _safe_id(character.name, f"character_{index}")
        character_ids[character.name] = character_id
        entities.append(
            ScriptStoryEntity(
                id=character_id,
                kind="character",
                name=character.name,
                description=character.public_info,
                properties={
                    "role": character.role,
                    "secret": character.secret,
                    "motive": character.motive,
                    "alibi": character.alibi,
                    "location": character.location,
                },
            )
        )
        relations.append(ScriptStoryRelation(source=script_id, target=character_id, type="HAS_CHARACTER", confidence="high"))
        if character.location and character.location in location_ids:
            relations.append(
                ScriptStoryRelation(source=character_id, target=location_ids[character.location], type="LOCATED_AT", confidence="medium")
            )

    for index, clue in enumerate(case.clues, start=1):
        clue_id = clue.id or _safe_id(clue.title, f"clue_{index}")
        entities.append(
            ScriptStoryEntity(
                id=clue_id,
                kind="clue",
                name=clue.title,
                description=clue.content,
                properties={
                    "source": clue.source,
                    "location": clue.location,
                    "owner": clue.owner,
                    "reveals": clue.reveals,
                    "trigger": clue.trigger,
                },
            )
        )
        relations.append(ScriptStoryRelation(source=script_id, target=clue_id, type="HAS_CLUE", confidence="high"))
        if clue.location and clue.location in location_ids:
            relations.append(ScriptStoryRelation(source=clue_id, target=location_ids[clue.location], type="FOUND_AT", confidence="medium"))
        owner_id = character_ids.get(clue.owner)
        if owner_id:
            relations.append(ScriptStoryRelation(source=clue_id, target=owner_id, type="OWNED_BY", confidence="medium"))
        if clue.reveals and case.truth:
            relations.append(ScriptStoryRelation(source=clue_id, target=f"{script_id}:truth", type="REVEALS", description=clue.reveals))

    previous_event_id = ""
    for index, event in enumerate(case.timeline, start=1):
        event_id = f"{script_id}:event_{index}"
        entities.append(ScriptStoryEntity(id=event_id, kind="event", name=event, properties={"order": index}))
        relations.append(ScriptStoryRelation(source=script_id, target=event_id, type="HAS_EVENT", confidence="high"))
        if previous_event_id:
            relations.append(ScriptStoryRelation(source=previous_event_id, target=event_id, type="NEXT_EVENT", confidence="high"))
        previous_event_id = event_id

    return ScriptStoryGraphFacts(entities=entities, relations=relations)


def _normalize_graph_kind(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_") or "unknown"


def _normalize_relation_type(value: str) -> str:
    return re.sub(r"[^A-Z0-9_]+", "_", str(value or "").strip().upper()).strip("_")


def _normalize_graph_ref(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _story_graph_entity_refs(entities: list[ScriptStoryEntity]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for entity in entities:
        entity_id = str(entity.id or "").strip()
        if not entity_id:
            continue
        for value in [entity.id, entity.name, *entity.aliases]:
            key = _normalize_graph_ref(value)
            if key:
                refs[key] = entity_id
    return refs


def validate_script_decomposition(case: ScriptDecompositionRequest) -> ScriptDecompositionReport:
    errors: list[str] = []
    warnings: list[str] = []
    ontology_warnings: list[str] = []
    unresolved_references: list[str] = []
    graph = case.story_graph
    if not graph.entities and not graph.relations:
        graph = _fallback_story_graph(case, _case_locations(case))

    entities = graph.entities
    relations = graph.relations
    entity_counts = Counter(_normalize_graph_kind(entity.kind) for entity in entities)
    relation_counts = Counter(_normalize_relation_type(relation.type) for relation in relations)
    evidence_count = sum(len(entity.evidence) for entity in entities) + sum(len(relation.evidence) for relation in relations)

    if not entities:
        errors.append("story_graph.entities is empty; ScriptDecompositionAgent must output graph nodes.")
    if not relations:
        errors.append("story_graph.relations is empty; ScriptDecompositionAgent must output graph edges.")

    if entity_counts.get("character", 0) < 1:
        errors.append("story_graph needs at least one character node.")
    if not any(kind in entity_counts for kind in {"location", "scene"}):
        errors.append("story_graph needs at least one location or scene node.")
    if not any(kind in entity_counts for kind in {"event", "timeline_event", "task", "item", "clue", "secret", "rule", "trigger"}):
        errors.append("story_graph needs at least one playable story asset node: event/task/item/clue/secret/rule/trigger.")

    allowed_relation_types = {
        "LOCATED_AT",
        "OCCURS_AT",
        "FOUND_AT",
        "INVOLVES",
        "REVEALS",
        "DEPENDS_ON",
        "NEXT_EVENT",
        "CAUSES",
        "OWNS",
        "OWNED_BY",
        "KNOWS_SECRET",
        "HAS_SECRET",
        "HAS_CHARACTER",
        "HAS_CLUE",
        "HAS_EVENT",
        "HAS_LOCATION",
        "MENTIONS",
        "PART_OF",
    }
    location_relation_types = {"LOCATED_AT", "OCCURS_AT", "FOUND_AT", "HAS_LOCATION"}
    playable_relation_types = {
        "INVOLVES",
        "REVEALS",
        "DEPENDS_ON",
        "NEXT_EVENT",
        "CAUSES",
        "OWNS",
        "OWNED_BY",
        "KNOWS_SECRET",
        "HAS_SECRET",
        "HAS_CHARACTER",
        "HAS_CLUE",
        "HAS_EVENT",
    }
    if relations and not any(kind in relation_counts for kind in location_relation_types):
        warnings.append("story_graph has no location/scene relation such as LOCATED_AT, OCCURS_AT, FOUND_AT, or HAS_LOCATION.")
    if relations and not any(kind in relation_counts for kind in playable_relation_types):
        warnings.append("story_graph has no playable progression relation such as INVOLVES, REVEALS, DEPENDS_ON, NEXT_EVENT, CAUSES, OWNS, or KNOWS_SECRET.")

    entity_refs = _story_graph_entity_refs(entities)
    incident_counts: Counter[str] = Counter()
    for relation in relations:
        relation_type = _normalize_relation_type(relation.type)
        if relation_type and relation_type not in allowed_relation_types:
            ontology_warnings.append(f"Unknown story_graph relation type: {relation.type}")
        source_ref = str(relation.source or "").strip()
        target_ref = str(relation.target or "").strip()
        source_id = entity_refs.get(_normalize_graph_ref(source_ref))
        target_id = entity_refs.get(_normalize_graph_ref(target_ref))
        if not source_id:
            unresolved_references.append(f"{relation.id or relation_type}:source:{source_ref}")
        else:
            incident_counts[source_id] += 1
        if not target_id:
            unresolved_references.append(f"{relation.id or relation_type}:target:{target_ref}")
        else:
            incident_counts[target_id] += 1
        if not relation.evidence and relation_type in playable_relation_types:
            ontology_warnings.append(f"Relation {relation.id or source_ref + '->' + target_ref} lacks source evidence.")

    isolated_nodes = [
        entity.id
        for entity in entities
        if entity.id and incident_counts.get(entity.id, 0) == 0 and _normalize_graph_kind(entity.kind) not in {"script", "world", "chapter"}
    ]
    if isolated_nodes:
        warnings.append("story_graph contains isolated nodes that are not connected by relations.")
    for entity in entities:
        kind = _normalize_graph_kind(entity.kind)
        if kind in {"character", "event", "timeline_event", "task", "item", "clue", "secret"} and not entity.evidence:
            ontology_warnings.append(f"Entity {entity.id or entity.name} lacks source evidence.")

    if unresolved_references:
        errors.append("story_graph has relations with unresolved source/target references.")

    return ScriptDecompositionReport(
        passed=not errors,
        errors=list(dict.fromkeys(errors)),
        warnings=list(dict.fromkeys(warnings)),
        ontology_warnings=list(dict.fromkeys(ontology_warnings)),
        unresolved_references=list(dict.fromkeys(unresolved_references)),
        isolated_nodes=list(dict.fromkeys(isolated_nodes)),
        node_count=len(entities),
        edge_count=len(relations),
        evidence_count=evidence_count,
        entity_counts=dict(sorted(entity_counts.items())),
        relation_counts=dict(sorted(relation_counts.items())),
    )


def decomposition_to_world(
    case: ScriptDecompositionRequest,
    report: ScriptDecompositionReport | None = None,
    decomposition: ScriptDecompositionResult | None = None,
) -> SandboxWorldConfig:
    report = report or validate_script_decomposition(case)
    source_decomposition = decomposition or script_request_to_decomposition(case, report)
    script_graph = ScriptGraphCompiler().compile(source_decomposition).model_dump()
    graph_context = _script_graph_runtime_context(script_graph)
    locations = _case_locations(case)
    start_location = locations[0] if locations else "案发大厅"
    npcs = [
        SandboxNPC(
            id=character.id or _safe_id(character.name, "npc"),
            name=character.name,
            role=character.role or "嫌疑人",
            personality=_character_personality(character),
            goals=[
                "只说自己当前愿意公开的信息",
                "根据玩家追问逐步透露线索",
                "不要主动泄露案件真相",
            ],
            location=character.location or start_location,
        )
        for character in case.characters
    ]
    tasks = [
        SandboxTask(
            id="collect_clues",
            title="收集关键线索",
            description="通过搜证和询问 NPC 收集所有关键线索。",
            completion={"stats": {"clue_count": {"min": max(1, len(case.clues))}}},
        ),
        SandboxTask(
            id="question_suspects",
            title="询问主要角色",
            description="至少和主要角色完成一轮对话，确认动机、时间线和矛盾点。",
            completion={"stats": {"questioned_count": {"min": max(1, len(case.characters))}}},
        ),
        SandboxTask(
            id="deduce_truth",
            title="推理并指认真相",
            description="在掌握关键线索后提交推理，才能揭晓案件真相。",
            completion={"player": {"truth_revealed": True}},
        ),
    ]
    actions = _case_actions(case, locations)
    world = SandboxWorldConfig(
        world_id=case.case_id or _safe_id(case.title, "script_case"),
        name=case.title or "案件型剧本",
        description="剧本拆解生成世界：搜证、询问、推理、揭晓真相。",
        lore=_case_lore(case),
        opening_scene=f"{case.public_background}\n\n你抵达{start_location}，需要先搜证，再逐个询问相关人物。",
        player={
            "name": case.player_name or "侦探",
            "location": start_location,
            "role": "调查者",
            "status": "尚未开始搜证。",
            "inventory": [],
            "clue_count": 0,
            "questioned_count": 0,
            "truth_revealed": False,
        },
        npcs=npcs,
        story_goals=["阅读公共背景", "搜集线索", "询问角色", "整理时间线", "提交推理并揭晓真相"],
        tasks=tasks,
        actions=actions,
        initial_memories=[
            "这是案件型剧本世界。NPC 不得主动泄露案件真相。",
            "玩家需要通过搜证、询问和推理逐步推进。",
            "角色私密信息只能在合适追问或线索触发后逐步透露。",
        ],
        metadata={
            "generated_by": "script_decomposition_agent",
            "decomposed_by": "script_decomposition_agent",
            "compiled_by": "script_world_compiler",
            "schema_version": "script_decomposition.v2",
            "mvp_loop": True,
            "template": "script_decomposition",
            "mechanics": [
                {"id": "clue_count", "path": "clue_count", "label": "线索数量", "aliases": ["线索", "搜证"], "kind": "stat"},
                {"id": "questioned_count", "path": "questioned_count", "label": "询问进度", "aliases": ["询问", "盘问"], "kind": "stat"},
                {"id": "truth_revealed", "path": "truth_revealed", "label": "真相揭晓", "aliases": ["真相", "指认"], "kind": "flag"},
            ],
            "script_case": _case_metadata(case, report),
            "script_decomposition": source_decomposition.model_dump(),
            "script_graph": script_graph,
            "story_graph_summary": graph_context,
        },
    )
    return SandboxWorldValidator().ensure_valid(world)


def _script_graph_runtime_context(script_graph: dict, max_nodes: int = 40, max_edges: int = 80) -> dict:
    nodes = [node for node in script_graph.get("nodes", []) if isinstance(node, dict)]
    edges = [edge for edge in script_graph.get("edges", []) if isinstance(edge, dict)]
    node_by_id = {str(node.get("id") or ""): node for node in nodes if node.get("id")}
    compact_nodes = []
    for node in nodes[:max_nodes]:
        properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        compact_nodes.append(
            {
                "id": str(node.get("id") or ""),
                "kind": str(node.get("kind") or ""),
                "label": str(node.get("label") or ""),
                "description": str(properties.get("description") or properties.get("text") or "")[:240],
            }
        )
    compact_edges = []
    for edge in edges[:max_edges]:
        properties = edge.get("properties") if isinstance(edge.get("properties"), dict) else {}
        source = node_by_id.get(str(edge.get("source") or ""), {})
        target = node_by_id.get(str(edge.get("target") or ""), {})
        compact_edges.append(
            {
                "source": str(edge.get("source") or ""),
                "source_label": str(source.get("label") or edge.get("source") or ""),
                "type": str(edge.get("type") or ""),
                "target": str(edge.get("target") or ""),
                "target_label": str(target.get("label") or edge.get("target") or ""),
                "description": str(properties.get("description") or properties.get("text") or "")[:240],
                "confidence": str(properties.get("confidence") or ""),
            }
        )
    return {
        "graph_id": script_graph.get("graph_id", ""),
        "title": script_graph.get("title", ""),
        "schema_version": script_graph.get("schema_version", ""),
        "ontology": script_graph.get("ontology", {}),
        "indexes": script_graph.get("indexes", {}),
        "nodes": compact_nodes,
        "edges": compact_edges,
        "metadata": {
            "graph_source": (script_graph.get("metadata") or {}).get("graph_source", ""),
            "uncertainties": (script_graph.get("metadata") or {}).get("uncertainties", []),
            "contradictions": (script_graph.get("metadata") or {}).get("contradictions", []),
        },
    }


def _case_actions(case: ScriptDecompositionRequest, locations: list[str]) -> list[SandboxAction]:
    actions: list[SandboxAction] = []
    for index, clue in enumerate(case.clues, start=1):
        action_id = clue.id or _safe_id(clue.title, f"clue_{index}")
        location = clue.location or (locations[0] if locations else "案发大厅")
        source_note = clue.source or location
        item = clue.title
        actions.append(
            SandboxAction(
                id=f"inspect_{action_id}",
                label=f"搜证：{item}",
                description=clue.trigger or f"围绕{source_note}调查并获得线索「{item}」。",
                effect={
                    "scene": f"你围绕{source_note}发现线索「{item}」。{clue.content}",
                    "set_player": {"location": location},
                    "increase_player": {"clue_count": 1},
                    "grant_item": item,
                },
            )
        )
    for index, character in enumerate(case.characters, start=1):
        npc_id = character.id or _safe_id(character.name, f"npc_{index}")
        actions.append(
            SandboxAction(
                id=f"question_{npc_id}",
                label=f"询问：{character.name}",
                description=f"围绕时间线、动机和已获线索询问{character.name}。",
                effect={
                    "scene": f"你和{character.name}完成了一轮关键询问。",
                    "set_player": {"location": character.location or (locations[0] if locations else "案发大厅")},
                    "increase_player": {"questioned_count": 1},
                    "active_npc_id": npc_id,
                },
            )
        )
    actions.append(
        SandboxAction(
            id="submit_deduction",
            label="提交推理",
            description="在收集足够线索后提交最终推理并揭晓真相。",
            effect={
                "scene": "你整理所有证词与线索，开始提交最终推理。真相只在此阶段揭晓。",
                "set_player": {"truth_revealed": True},
                "complete_task": "deduce_truth",
            },
        )
    )
    return actions


def _case_metadata(case: ScriptDecompositionRequest, report: ScriptDecompositionReport) -> dict:
    return {
        "case_id": case.case_id,
        "title": case.title,
        "truth": case.truth,
        "core_plot": case.core_plot,
        "hidden_threads": case.hidden_threads,
        "public_background": case.public_background,
        "timeline": case.timeline,
        "locations": _case_locations(case),
        "characters": [character.model_dump() for character in case.characters],
        "clues": [clue.model_dump() for clue in case.clues],
        "endings": [ending.model_dump() for ending in case.endings],
        "forbidden_spoilers": case.forbidden_spoilers,
        "report": report.model_dump(),
    }


def _case_lore(case: ScriptDecompositionRequest) -> str:
    spoiler_rules = "\n".join(f"- {item}" for item in case.forbidden_spoilers) or "- 隐藏主线、角色秘密和关键伏笔只能在合适阶段逐步揭晓。"
    core_plot = case.core_plot or case.truth
    hidden_threads = "\n".join(f"- {item}" for item in case.hidden_threads) or "- 暂未显式提供。"
    return (
        f"【公共背景】\n{case.public_background}\n\n"
        f"【核心剧情】\n{core_plot}\n\n"
        f"【隐藏暗线】\n{hidden_threads}\n\n"
        f"【调查规则】玩家需要先搜证、询问角色、整理时间线，再提交推理。\n\n"
        f"【防剧透规则】\n{spoiler_rules}\n\n"
        "【主持人规则】NPC 可以承认公开信息、回应已发现线索，但不得无条件公开自己的秘密、隐藏暗线或关键真相。"
    )


def _character_personality(character: ScriptCharacterInput) -> str:
    parts = [character.public_info]
    if character.motive:
        parts.append(f"潜在动机：{character.motive}")
    if character.alibi:
        parts.append(f"公开不在场证明：{character.alibi}")
    if character.secret:
        parts.append("有私密信息，只有在玩家掌握相关线索或追问矛盾点时才逐步透露。")
    return " ".join(part for part in parts if part) or "谨慎、会根据玩家问题逐步回应。"


def _case_locations(case: ScriptDecompositionRequest) -> list[str]:
    values = [*case.locations]
    values.extend(character.location for character in case.characters if character.location)
    values.extend(clue.location for clue in case.clues if clue.location)
    deduped = [item for item in dict.fromkeys(value.strip() for value in values if value and value.strip())]
    return deduped or ["案发大厅"]


def _sections(text: str) -> dict[str, str]:
    current = ""
    sections: dict[str, list[str]] = {}
    aliases = {
        "标题": "标题",
        "剧本名": "剧本名",
        "世界观": "公共背景",
        "公共背景": "公共背景",
        "案件真相": "案件真相",
        "真相": "案件真相",
        "角色": "角色",
        "人物": "角色",
        "线索": "线索",
        "地点": "地点",
        "场景": "场景",
        "时间线": "时间线",
        "人物关系": "关系",
        "关系": "关系",
        "约束规则": "约束",
        "约束": "约束",
        "任务目标": "任务",
        "任务": "任务",
        "本单元核心剧情脉络": "核心剧情",
        "核心剧情脉络": "核心剧情",
        "本单元总结": "核心剧情",
        "结局": "结局",
        "禁止提前泄露": "禁止提前泄露",
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = _heading(line, aliases)
        if heading:
            current = heading
            sections.setdefault(current, [])
            rest = re.sub(r"^#{1,6}\s*", "", line)
            rest = re.sub(r"^[【\[]?[^】\]：:]+[】\]]?[：:]\s*", "", rest)
            if rest and rest != line:
                sections[current].append(rest)
            continue
        if current and line:
            sections.setdefault(current, []).append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _heading(line: str, aliases: dict[str, str]) -> str:
    cleaned = re.sub(r"^#{1,6}\s*", "", line).strip()
    cleaned = cleaned.strip("【】[] ")
    cleaned = re.sub(r"^(?:[一二三四五六七八九十]+|[0-9]+)[、.．]\s*", "", cleaned)
    cleaned = re.sub(r"[（(][a-zA-Z0-9_.-]+[）)]", "", cleaned).strip()
    key = re.split(r"[：:]", cleaned, maxsplit=1)[0].strip()
    return aliases.get(key, "")


def _parse_characters(text: str) -> list[ScriptCharacterInput]:
    asset_characters = _parse_asset_characters(text)
    if asset_characters:
        return asset_characters
    blocks = _blocks(text)
    characters = []
    for index, block in enumerate(blocks, start=1):
        fields = _field_map(block)
        name = fields.get("姓名") or fields.get("角色") or fields.get("name") or _first_line(block)
        if not name:
            continue
        characters.append(
            ScriptCharacterInput(
                id=fields.get("id") or _safe_id(name, f"npc_{index}"),
                name=name,
                role=fields.get("身份") or fields.get("role") or "嫌疑人",
                public_info=fields.get("公开信息") or fields.get("公开") or "",
                secret=fields.get("秘密") or fields.get("私密信息") or "",
                motive=fields.get("动机") or "",
                alibi=fields.get("不在场证明") or fields.get("时间线") or "",
                location=fields.get("地点") or "",
            )
        )
    return characters


def _parse_clues(text: str) -> list[ScriptClueInput]:
    asset_clues = _parse_asset_clues(text)
    if asset_clues:
        return asset_clues
    blocks = _blocks(text)
    clues = []
    for index, block in enumerate(blocks, start=1):
        fields = _field_map(block)
        title = fields.get("标题") or fields.get("线索") or fields.get("name") or _first_line(block)
        if not title:
            continue
        clues.append(
            ScriptClueInput(
                id=fields.get("id") or _safe_id(title, f"clue_{index}"),
                title=title,
                content=fields.get("内容") or fields.get("描述") or "",
                source=fields.get("来源") or "",
                location=fields.get("地点") or "",
                owner=fields.get("关联角色") or fields.get("owner") or "",
                reveals=fields.get("揭示") or fields.get("指向") or "",
                trigger=fields.get("触发") or "",
            )
        )
    return clues


def _parse_endings(text: str) -> list[ScriptEndingInput]:
    endings = []
    for index, block in enumerate(_blocks(text), start=1):
        fields = _field_map(block)
        title = fields.get("标题") or fields.get("结局") or _first_line(block)
        if title:
            endings.append(
                ScriptEndingInput(
                    id=fields.get("id") or _safe_id(title, f"ending_{index}"),
                    title=title,
                    condition=fields.get("条件") or "",
                    reveal=fields.get("揭示") or fields.get("内容") or "",
                )
            )
    return endings


def _blocks(text: str) -> list[str]:
    raw_blocks = re.split(r"\n\s*(?:---+|\*\*\*+)\s*\n", text.strip())
    if len(raw_blocks) == 1:
        raw_blocks = re.split(r"\n(?=(?:姓名|角色|标题|线索|id|name)[：:])", text.strip())
    return [block.strip() for block in raw_blocks if block.strip()]


def _parse_asset_characters(text: str) -> list[ScriptCharacterInput]:
    blocks = _entity_blocks(text, required_field="身份")
    characters: list[ScriptCharacterInput] = []
    for index, block in enumerate(blocks, start=1):
        lines = _lines(block)
        if not lines:
            continue
        raw_name = lines[0]
        fields = _field_map("\n".join(lines[1:]))
        public_bits = [
            _field_value(fields, "目标"),
            _field_value(fields, "性格"),
            _field_value(fields, "外貌"),
            _field_value(fields, "首次出场"),
        ]
        characters.append(
            ScriptCharacterInput(
                id=_safe_id(_clean_entity_title(raw_name), f"npc_{index}"),
                name=_clean_entity_title(raw_name),
                role=_field_value(fields, "身份") or "NPC",
                public_info="；".join(bit for bit in public_bits if bit),
                secret=_field_value(fields, "秘密"),
                motive=_field_value(fields, "目标"),
                alibi=_field_value(fields, "不在场证明", "时间线"),
                location=_field_value(fields, "地点"),
            )
        )
    return [character for character in characters if character.name]


def _parse_asset_clues(text: str) -> list[ScriptClueInput]:
    blocks = _entity_blocks(text, required_field="描述")
    clues: list[ScriptClueInput] = []
    for index, block in enumerate(blocks, start=1):
        lines = _lines(block)
        if not lines:
            continue
        raw_title = lines[0]
        fields = _field_map("\n".join(lines[1:]))
        clues.append(
            ScriptClueInput(
                id=_safe_id(_clean_entity_title(raw_title), f"clue_{index}"),
                title=_clean_entity_title(raw_title),
                content=_field_value(fields, "描述", "内容"),
                source=_field_value(fields, "来源"),
                location=_location_from_source(_field_value(fields, "来源", "地点")),
                owner=_normalize_owner(_field_value(fields, "知情人", "关联角色", "owner")),
                reveals=_field_value(fields, "状态", "揭示", "指向"),
                trigger=_field_value(fields, "触发"),
            )
        )
    return [clue for clue in clues if clue.title]


def _entity_blocks(text: str, required_field: str) -> list[str]:
    lines = _lines(text)
    blocks: list[list[str]] = []
    current: list[str] = []
    for index, line in enumerate(lines):
        if _is_subheading(line) or _is_table_row(line):
            continue
        if _looks_like_entity_heading(lines, index, required_field):
            if current:
                blocks.append(current)
            current = [line]
            continue
        if _is_field_line(line):
            if current:
                current.append(line)
            continue
        if current:
            current.append(line)
    if current:
        blocks.append(current)
    return ["\n".join(block).strip() for block in blocks if len(block) > 1]


def _looks_like_entity_heading(lines: list[str], index: int, required_field: str) -> bool:
    line = lines[index].strip()
    if not line or _is_field_line(line) or _is_subheading(line) or _is_table_row(line):
        return False
    if len(line) > 40:
        return False
    window = lines[index + 1 : index + 6]
    return any(item.startswith(f"{required_field}：") or item.startswith(f"{required_field}:") for item in window)


def _is_field_line(line: str) -> bool:
    line = line.strip()
    if re.match(r"^[^：:]{1,12}[（(][^）)]*[：:]", line):
        return False
    return bool(re.match(r"^[^：:]{1,12}[：:]", line))


def _is_subheading(line: str) -> bool:
    return bool(re.match(r"^(?:[0-9]+(?:\.[0-9]+)+|[一二三四五六七八九十]+[、.．])", line.strip()))


def _is_table_row(line: str) -> bool:
    return "|" in line


def _clean_entity_title(value: str) -> str:
    text = value.strip().lstrip("-* ").strip()
    text = re.sub(r"[（(]别名[：:].*?[）)]", "", text).strip()
    return text


def _field_value(fields: dict[str, str], *names: str) -> str:
    for name in names:
        if fields.get(name):
            return fields[name]
    return ""


def _parse_hidden_threads(text: str) -> list[str]:
    threads: list[str] = []
    capture = False
    for line in _lines(text):
        if "暗线" in line or "明线" in line:
            capture = True
        if capture and ("暗线" in line or "明线" in line):
            threads.append(line)
    return threads


def _parse_locations(text: str) -> list[str]:
    locations: list[str] = []
    for line in _lines(text):
        if _is_subheading(line) or _is_table_row(line):
            continue
        if "：" in line or ":" in line:
            name = re.split(r"[：:]", line, maxsplit=1)[0].strip()
            if name and len(name) <= 30:
                locations.append(name)
    return list(dict.fromkeys(locations))


def _table_locations(text: str) -> list[str]:
    locations: list[str] = []
    in_scene_table = False
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 3:
            continue
        if cells[:3] == ["编号", "场景名称", "位置"]:
            in_scene_table = True
            continue
        if cells[0] in {"地名", "编号", "人物"}:
            in_scene_table = False
            continue
        if cells[1] in {"村庄", "小镇", "山脉", "险要山崖", "山谷"}:
            locations.append(cells[0])
        elif in_scene_table and cells[0].isdigit() and cells[1]:
            locations.append(cells[1])
    return list(dict.fromkeys(locations))


def _parse_constraints(text: str) -> list[str]:
    constraints: list[str] = []
    for line in _lines(text):
        if _is_subheading(line) or _is_table_row(line):
            continue
        constraints.append(line)
    return constraints


def _normalize_owner(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    return re.split(r"[，,；;、\s]|有所|尚|已|未|但", text, maxsplit=1)[0].strip()


def _location_from_source(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    known_place_markers = ("谷", "崖", "屋", "镇", "山", "门", "场", "处", "堆", "房", "室", "园")
    if any(marker in text for marker in known_place_markers) and not any(word in text for word in ["行为", "感知", "异象"]):
        return text
    return ""


def _document_title(text: str) -> str:
    first = _first_line(text)
    return first if first and len(first) <= 80 else ""


def _field_map(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current = ""
    for raw_line in block.splitlines():
        line = raw_line.strip().lstrip("-* ").strip()
        match = re.match(r"([^：:]{1,20})[：:]\s*(.*)$", line)
        if match:
            current = match.group(1).strip()
            fields[current] = match.group(2).strip()
        elif current and line:
            fields[current] = f"{fields[current]}\n{line}".strip()
    return fields


def _lines(text: str) -> list[str]:
    return [line.strip().lstrip("-* ").strip() for line in text.splitlines() if line.strip()]


def _split_items(text: str) -> list[str]:
    items: list[str] = []
    for line in _lines(text):
        for item in re.split(r"[,，、/|]", line):
            item = item.strip()
            if item:
                items.append(item)
    return list(dict.fromkeys(items))


def _first_line(text: str) -> str:
    return next((line.strip().lstrip("-* ").strip() for line in text.splitlines() if line.strip()), "")


def _safe_id(value: str, fallback: str) -> str:
    text = value.strip().lower()
    replacements = {
        "侦探": "detective",
        "嫌疑人": "suspect",
        "线索": "clue",
        "真相": "truth",
        "钥匙": "key",
        "大厅": "hall",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"[^a-z0-9_]+", "_", text).strip("_")
    if not text or text in {"clue", "truth", "suspect", "npc"}:
        return fallback
    return text
