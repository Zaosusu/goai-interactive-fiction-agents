from __future__ import annotations

import json
import re
from typing import Any

from app.agents.npc_lorebook.schema import NpcLorebookArtifact, NpcLorebookEntry
from app.core.models import ReviewIssue, ReviewReport


class NpcLorebookReviewAgent:
    """Reviews NPC lorebook artifacts before they are injected into runtime prompts."""

    forbidden_terms = (
        "图谱",
        "世界树",
        "ScriptGraphDocument",
        "script_graph",
        "story graph",
        "story_graph",
        "WorldTree",
        "world_tree",
        "JSON",
        "json",
        "节点",
        "关系边",
        "图边",
        "开发者",
        "后台配置",
        "测试台",
    )

    def review(self, artifact: NpcLorebookArtifact | dict[str, Any]) -> ReviewReport:
        lorebook = artifact if isinstance(artifact, NpcLorebookArtifact) else NpcLorebookArtifact.model_validate(artifact)
        issues: list[ReviewIssue] = []

        if not lorebook.entries:
            issues.append(self._issue("error", "entries", "NPC 世界书没有任何可激活条目。"))

        for index, entry in enumerate(lorebook.entries):
            issues.extend(self._review_entry(entry, index))

        metadata = {
            "artifact_id": lorebook.artifact_id,
            "entry_count": len(lorebook.entries),
            "forbidden_term_count": len(self.forbidden_terms),
            "constant_entries": sum(1 for entry in lorebook.entries if entry.strategy == "constant"),
            "normal_entries": sum(1 for entry in lorebook.entries if entry.strategy == "normal"),
            "selective_entries": sum(1 for entry in lorebook.entries if entry.strategy == "selective"),
            "disabled_entries": sum(1 for entry in lorebook.entries if entry.strategy == "disabled"),
            "token_budget": sum(entry.token_budget for entry in lorebook.entries if entry.strategy != "disabled"),
        }
        if metadata["constant_entries"] > 3:
            issues.append(
                self._issue(
                    "warning",
                    "entries",
                    "常驻条目超过 3 条，容易挤占上下文；核心世界观和阶段总结之外优先使用 normal/selective。",
                )
            )
        return ReviewReport(
            reviewer="NpcLorebookReviewAgent",
            passed=not any(issue.severity == "error" for issue in issues),
            issues=issues,
            notes=["世界书条目会按策略、关键词、扫描深度、优先级和 token 预算激活。"],
            metadata=metadata,
        )

    def _review_entry(self, entry: NpcLorebookEntry, index: int) -> list[ReviewIssue]:
        issues: list[ReviewIssue] = []
        base_path = f"entries[{index}]"

        if not entry.id.strip():
            issues.append(self._issue("error", f"{base_path}.id", "世界书条目缺少 id。"))
        if not entry.title.strip():
            issues.append(self._issue("error", f"{base_path}.title", "世界书条目缺少标题。"))
        if not entry.content.strip():
            issues.append(self._issue("error", f"{base_path}.content", "世界书条目内容为空。"))
        if entry.strategy != "constant" and not entry.keywords and not entry.regex_keywords and not entry.npc_ids and not entry.locations:
            issues.append(self._issue("warning", f"{base_path}.keywords", "非常驻条目缺少关键词、正则关键词、NPC 或地点触发条件。"))
        if entry.strategy == "constant" and entry.keywords:
            issues.append(self._issue("warning", f"{base_path}.keywords", "常驻条目不需要关键词；如需按需注入，请改用 normal/selective。"))
        if entry.strategy == "normal" and entry.scan_depth > 8:
            issues.append(self._issue("warning", f"{base_path}.scan_depth", "normal 条目扫描深度过大，建议 3-5，最多不要超过 8。"))
        if entry.token_budget > 500:
            issues.append(self._issue("warning", f"{base_path}.token_budget", "单条世界书预算过高，建议拆成 200-500 字以内的条目。"))
        for pattern_index, pattern in enumerate(entry.regex_keywords):
            try:
                re.compile(pattern)
            except re.error:
                issues.append(self._issue("error", f"{base_path}.regex_keywords[{pattern_index}]", "正则关键词无法编译。"))
        if len(entry.content) > max(600, entry.token_budget * 4):
            issues.append(self._issue("warning", f"{base_path}.content", "世界书条目过长，运行时可能挤占对话上下文。"))

        fields = {
            "title": entry.title,
            "content": entry.content,
            "keywords": json.dumps(entry.keywords, ensure_ascii=False),
            "regex_keywords": json.dumps(entry.regex_keywords, ensure_ascii=False),
        }
        for field_name, value in fields.items():
            leaked = self._find_forbidden(value)
            if leaked:
                issues.append(
                    self._issue(
                        "error",
                        f"{base_path}.{field_name}",
                        "NPC 可见世界书字段包含开发者/数据结构概念。",
                    )
                )
        return issues

    def _find_forbidden(self, value: str) -> list[str]:
        text = str(value or "")
        return [term for term in self.forbidden_terms if term in text]

    def _issue(self, severity: str, path: str, message: str) -> ReviewIssue:
        return ReviewIssue(severity=severity, area="npc_lorebook", path=path, message=message)
