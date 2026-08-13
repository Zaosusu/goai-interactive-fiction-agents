from __future__ import annotations

import re

from app.agents.npc_lorebook.schema import NpcLorebookArtifact, NpcLorebookEntry


class NpcLorebookRuntime:
    def __init__(self, artifact: NpcLorebookArtifact, max_entries: int = 8, token_budget: int = 1200) -> None:
        self.artifact = artifact
        self.max_entries = max_entries
        self.token_budget = token_budget

    def activate(
        self,
        *,
        message: str = "",
        player_goal: str = "",
        conversation: str = "",
        recent_messages: list[str] | None = None,
        npc_id: str = "",
        location: str = "",
    ) -> list[NpcLorebookEntry]:
        recent = [str(item or "") for item in (recent_messages or []) if str(item or "").strip()]
        if not recent:
            recent = self._conversation_lines(conversation)
        current_query = "\n".join(part for part in (message, player_goal) if part)
        full_query = "\n".join(part for part in (message, player_goal, conversation) if part)

        scored = self._score_entries(
            current_query=current_query,
            full_query=full_query,
            recent_messages=recent,
            npc_id=npc_id,
            location=location,
        )
        active_text = "\n".join(entry.content for _, entry in scored if entry.chain)
        if active_text:
            scored.extend(
                self._score_entries(
                    current_query=current_query,
                    full_query=f"{full_query}\n{active_text}",
                    recent_messages=[*recent, active_text],
                    npc_id=npc_id,
                    location=location,
                    excluded_ids={entry.id for _, entry in scored},
                    chain_only=True,
                )
            )

        scored.sort(key=lambda item: (-item[1].priority, -item[0], item[1].id))
        selected: list[NpcLorebookEntry] = []
        used_budget = 0
        for _, entry in scored:
            cost = self._entry_cost(entry)
            if selected and used_budget + cost > self.token_budget:
                continue
            selected.append(entry)
            used_budget += cost
            if len(selected) >= self.max_entries:
                break
        return selected

    def format_entries(self, entries: list[NpcLorebookEntry]) -> str:
        if not entries:
            return "暂无激活条目。"
        lines = []
        for entry in entries:
            lines.append(f"- [{entry.position}] {entry.title}: {entry.content}")
        return "\n".join(lines)

    def _score_entries(
        self,
        *,
        current_query: str,
        full_query: str,
        recent_messages: list[str],
        npc_id: str,
        location: str,
        excluded_ids: set[str] | None = None,
        chain_only: bool = False,
    ) -> list[tuple[int, NpcLorebookEntry]]:
        scored: list[tuple[int, NpcLorebookEntry]] = []
        excluded_ids = excluded_ids or set()
        for entry in self.artifact.entries:
            if entry.id in excluded_ids or entry.strategy == "disabled":
                continue
            score = self._score(
                entry,
                current_query=current_query,
                full_query=full_query,
                recent_messages=recent_messages,
                npc_id=npc_id,
                location=location,
                chain_only=chain_only,
            )
            if score > 0:
                scored.append((score, entry))
        return scored

    def _score(
        self,
        entry: NpcLorebookEntry,
        *,
        current_query: str,
        full_query: str,
        recent_messages: list[str],
        npc_id: str,
        location: str,
        chain_only: bool = False,
    ) -> int:
        score = 0
        if entry.strategy == "constant":
            return 0 if chain_only else 10000 + entry.priority
        if npc_id and npc_id in entry.npc_ids:
            score += 1800
        if location and location in entry.locations:
            score += 900
        keyword_query = self._keyword_query(entry, current_query=current_query, full_query=full_query, recent_messages=recent_messages)
        score += self._keyword_score(entry, keyword_query)
        if score <= 0:
            return 0
        return score + entry.priority

    def _keyword_query(self, entry: NpcLorebookEntry, *, current_query: str, full_query: str, recent_messages: list[str]) -> str:
        if entry.strategy == "selective":
            return full_query
        if entry.strategy == "normal":
            depth = max(1, entry.scan_depth)
            return "\n".join([*recent_messages[-depth:], current_query])
        return full_query

    def _keyword_score(self, entry: NpcLorebookEntry, query: str) -> int:
        score = 0
        lowered = query.lower()
        for keyword in entry.keywords:
            key = str(keyword or "").strip()
            if key and key.lower() in lowered:
                score += 120 + min(len(key), 20)
        for pattern in entry.regex_keywords:
            try:
                if re.search(pattern, query, flags=re.IGNORECASE):
                    score += 180
            except re.error:
                continue
        return score

    def _conversation_lines(self, conversation: str) -> list[str]:
        return [line.strip() for line in str(conversation or "").splitlines() if line.strip()]

    def _entry_cost(self, entry: NpcLorebookEntry) -> int:
        return max(1, entry.token_budget)
