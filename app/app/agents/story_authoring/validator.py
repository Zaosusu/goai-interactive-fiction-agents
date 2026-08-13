from __future__ import annotations

from collections import deque

from app.agents.story_authoring.schema import (
    StoryAuthoringRequest,
    StoryDraft,
    StoryDraftIssue,
    StoryDraftReview,
)


class StoryDraftValidator:
    """Deterministic checks for story references, reachability and authored content."""

    def review(self, draft: StoryDraft, request: StoryAuthoringRequest | None = None) -> StoryDraftReview:
        issues: list[StoryDraftIssue] = []
        scene_ids = [scene.id for scene in draft.scenes]
        character_ids = [character.id for character in draft.characters]
        clue_ids = [clue.id for clue in draft.clues]
        scene_set = set(scene_ids)
        character_set = set(character_ids)
        clue_set = set(clue_ids)

        self._duplicates(scene_ids, "scene_id_duplicate", "剧情场景 ID 重复", issues)
        self._duplicates(character_ids, "character_id_duplicate", "角色 ID 重复", issues)
        self._duplicates(clue_ids, "clue_id_duplicate", "线索 ID 重复", issues)

        if draft.start_scene_id not in scene_set:
            issues.append(self._issue("error", "start_scene_missing", "开场场景不存在。", draft.start_scene_id))

        adjacency: dict[str, list[str]] = {scene_id: [] for scene_id in scene_set}
        dialogue_count = 0
        ending_count = 0
        for scene in draft.scenes:
            if scene.kind == "ending":
                ending_count += 1
            if scene.default_next_scene_id:
                if scene.default_next_scene_id not in scene_set:
                    issues.append(self._issue("error", "next_scene_missing", "默认下一场景不存在。", scene.default_next_scene_id))
                else:
                    adjacency[scene.id].append(scene.default_next_scene_id)
            for choice in scene.choices:
                if not choice.next_scene_id:
                    issues.append(self._issue("error", "choice_target_missing", "玩家选项必须指向下一场景。", scene.id))
                elif choice.next_scene_id not in scene_set:
                    issues.append(self._issue("error", "choice_scene_missing", "玩家选项指向不存在的场景。", choice.next_scene_id))
                else:
                    adjacency[scene.id].append(choice.next_scene_id)
            if scene.kind != "ending" and not adjacency[scene.id]:
                issues.append(self._issue("error", "scene_dead_end", "非结局场景没有后续场景。", scene.id))
            for beat in scene.beats:
                if beat.kind == "dialogue":
                    dialogue_count += 1
                    if not beat.speaker_id:
                        issues.append(self._issue("error", "dialogue_speaker_missing", "对话节拍缺少说话角色。", scene.id))
                    elif beat.speaker_id not in character_set:
                        issues.append(self._issue("error", "dialogue_speaker_unknown", "对话引用了不存在的角色。", beat.speaker_id))
            for clue_id in scene.unlock_clue_ids:
                if clue_id not in clue_set:
                    issues.append(self._issue("error", "scene_clue_missing", "场景解锁了不存在的线索。", clue_id))

        for clue in draft.clues:
            if clue.source_scene_id not in scene_set:
                issues.append(self._issue("error", "clue_scene_missing", "线索来源场景不存在。", clue.source_scene_id))
            if clue.owner_character_id and clue.owner_character_id not in character_set:
                issues.append(self._issue("error", "clue_owner_missing", "线索持有角色不存在。", clue.owner_character_id))
            for required_id in clue.required_clue_ids:
                if required_id not in clue_set:
                    issues.append(self._issue("error", "required_clue_missing", "线索前置条件不存在。", required_id))

        reachable = self._reachable(draft.start_scene_id, adjacency)
        for scene_id in sorted(scene_set - reachable):
            issues.append(self._issue("error", "scene_unreachable", "场景无法从开场到达。", scene_id))
        reachable_endings = [scene for scene in draft.scenes if scene.id in reachable and scene.kind == "ending"]
        if not reachable_endings:
            issues.append(self._issue("error", "ending_missing", "剧情至少需要一个可达结局。"))

        total_minutes = sum(scene.duration_minutes for scene in draft.scenes)
        if request is not None:
            tolerance = max(5, round(request.target_minutes * 0.3))
            if abs(total_minutes - request.target_minutes) > tolerance:
                issues.append(
                    self._issue(
                        "warning",
                        "duration_mismatch",
                        f"场景时长合计 {total_minutes} 分钟，与目标 {request.target_minutes} 分钟偏差较大。",
                    )
                )
            if len(draft.scenes) != request.target_scene_count:
                issues.append(
                    self._issue(
                        "warning",
                        "scene_count_mismatch",
                        f"生成了 {len(draft.scenes)} 个场景，目标为 {request.target_scene_count} 个。",
                    )
                )
            if len(draft.characters) != request.target_character_count:
                issues.append(
                    self._issue(
                        "warning",
                        "character_count_mismatch",
                        f"生成了 {len(draft.characters)} 个角色，目标为 {request.target_character_count} 个。",
                    )
                )
        if dialogue_count < max(6, len(draft.scenes) * 2):
            issues.append(self._issue("warning", "dialogue_content_thin", "具体 NPC 台词偏少，沉浸感可能不足。"))

        return StoryDraftReview(
            valid=not any(issue.severity == "error" for issue in issues),
            scene_count=len(draft.scenes),
            character_count=len(draft.characters),
            clue_count=len(draft.clues),
            dialogue_beat_count=dialogue_count,
            total_minutes=total_minutes,
            reachable_scene_count=len(reachable),
            ending_count=ending_count,
            issues=issues,
        )

    def _duplicates(self, values: list[str], code: str, message: str, issues: list[StoryDraftIssue]) -> None:
        seen: set[str] = set()
        for value in values:
            if value in seen:
                issues.append(self._issue("error", code, message, value))
            seen.add(value)

    def _reachable(self, start: str, adjacency: dict[str, list[str]]) -> set[str]:
        if start not in adjacency:
            return set()
        result: set[str] = set()
        queue: deque[str] = deque([start])
        while queue:
            scene_id = queue.popleft()
            if scene_id in result:
                continue
            result.add(scene_id)
            queue.extend(adjacency.get(scene_id, []))
        return result

    def _issue(self, severity: str, code: str, message: str, reference_id: str = "") -> StoryDraftIssue:
        return StoryDraftIssue(severity=severity, code=code, message=message, reference_id=reference_id)
