import json
import re

from app.agents.npc_lorebook import NpcLorebookCompiler, NpcLorebookReviewAgent, NpcLorebookRuntime
from app.core.models import (
    AgentLLMOutput,
    AgentSessionState,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    EmotionVector,
    NpcRuntimeState,
    WorldActionRequest,
    WorldActionResponse,
    command_to_dict,
)
from app.worlds.sandbox.actions import SandboxActionService
from app.worlds.sandbox.completion import evaluate_task_completions
from app.worlds.sandbox.guardrails import WorldRuntimeGuardrail, split_location_values
from app.worlds.sandbox.models import SandboxWorldConfig
from app.worlds.sandbox.qingmeng_triggers import qingmeng_guide_prompt
from app.worlds.sandbox.validator import SandboxWorldValidator


class SandboxWorldAdapter:
    def __init__(self, config: SandboxWorldConfig) -> None:
        self.config = SandboxWorldValidator().ensure_valid(config)
        self.world_id = self.config.world_id
        self.action_service = SandboxActionService(self.config)
        self.guardrail = WorldRuntimeGuardrail(self.config)
        self.lorebook = self._load_lorebook_artifact()
        self.lorebook_review = NpcLorebookReviewAgent().review(self.lorebook)
        self.lorebook_runtime = NpcLorebookRuntime(self.lorebook)

    def default_player_goal(self) -> str:
        return self.config.story_goals[0] if self.config.story_goals else "探索当前世界并推动剧情。"

    def create_initial_state(self) -> AgentSessionState:
        state = AgentSessionState(
            emotion=EmotionVector(anticipation=0.15),
            goals=list(self.config.story_goals),
            quest_progress=self.config.opening_scene or "世界已启动，等待玩家行动。",
            world_state={
                "world_id": self.config.world_id,
                "world_name": self.config.name,
                "scene": self.config.opening_scene,
                "turn": 0,
                "player": dict(self.config.player),
                "npcs": [npc.model_dump() for npc in self.config.npcs],
                "tasks": [task.model_dump() for task in self.config.tasks],
                "flags": {},
                "relations": {},
                "lorebook": self.lorebook.model_dump(),
                "lorebook_review": self.lorebook_review.model_dump(),
                "script_graph": self._story_graph_context(),
                "active_npc_id": self.config.npcs[0].id if self.config.npcs else None,
            },
        )
        for memory in self.config.initial_memories:
            state.add_memory(memory, 0.55)
        return state

    def build_system_prompt(self, state: AgentSessionState, request: ChatRequest, npc_state: NpcRuntimeState | None = None) -> str:
        speaker = self._resolve_speaker(state, request.target_npc_id)
        if npc_state is None and speaker:
            npc_state = NpcRuntimeState(npc_id=str(speaker.get("id") or ""))
        qingmeng_prompt = qingmeng_guide_prompt(str(speaker.get("id") or "")) if self.config.world_id == "qingmeng_agent_case" and speaker else ""
        nearby_npcs = self._nearby_npcs(state)
        location_presence = self._format_location_presence(state, request, nearby_npcs, speaker)
        return f"""
你是一个通用 NPC Agent，当前被热插拔进一个数据驱动的世界观沙盒。
世界 ID：{self.config.world_id}
世界名称：{self.config.name}
简介：{self.config.description}

世界观设定：
{self.config.lore or "未填写。请只基于当前配置和玩家输入推进。"}

当前场景：{state.world_state.get("scene") or self.config.opening_scene or "未设定"}

玩家状态：
{json.dumps(self._world_facing_player(state.world_state.get("player", {})), ensure_ascii=False, indent=2)}

NPC 设定：
{json.dumps(self._world_facing_npcs(state.world_state.get("npcs", [])), ensure_ascii=False, indent=2)}

已知可前往地点：
{self._format_list(self._known_locations(state) or [state.world_state.get("player", {}).get("location") or "当前位置"])}

当前你要扮演的 NPC：
{json.dumps(self._world_facing_npc(speaker) or {}, ensure_ascii=False, indent=2)}

当前地点在场情况：
{location_presence}

当前 NPC 的私有运行状态：
{self._format_npc_runtime_state(npc_state, request.message)}

本轮角色扮演导演计划：
{self._format_turn_director(npc_state)}

当前 NPC 的长期连续性记忆：
{self._format_npc_continuity(npc_state, request.message)}

剧情目标：
{self._format_list([self._world_facing_text(goal) for goal in state.goals])}

任务列表：
{json.dumps(self._world_facing_collection(state.world_state.get("tasks", [])), ensure_ascii=False, indent=2)}

当前可用背景：
{self._format_lorebook_context(state, request, speaker, npc_state)}

可执行世界动作：
{json.dumps(self._world_facing_collection([action.model_dump() for action in self.config.actions]), ensure_ascii=False, indent=2)}

世界标记：
{json.dumps(state.world_state.get("flags", {}), ensure_ascii=False, indent=2)}

CRAG 记忆上下文：
{self._format_rag_context(state)}

最近对话：
{self._format_conversation_log(state)}

清梦引 NPC Agent 约束：
{qingmeng_prompt or "无。"}

玩家世界内意图：{self._world_facing_player_goal(request.player_goal or self.default_player_goal())}

输出要求：
- 必须返回结构化对象。
- content 是“当前你要扮演的 NPC”对玩家可见的话，不要串成其他 NPC 的口吻。
- 必须尊重“当前地点在场情况”；如果当前地点只有你一个 NPC 在场，不要用“你们这些人/各位/大家都在”等多人群聊口吻，也不要假装其他 NPC 也在现场。
- content 只能包含当前世界内 NPC 可以知道和说出口的信息；不要提世界外的数据结构、开发工具、配置台或调试概念。
- inner_thought 是当前 NPC 的内部判断，给调试面板看。
- reasoning 是你选择当前命令的简短原因。
- plan 是 1-3 条短计划。
- criticism 是输出前自检：是否越权、是否剧透、是否遗漏状态变更。
- command 是明确世界命令，格式为 {{"name":"命令名","args":{{...}}}}。
- command.name 只能是：
  - "none"：只说话，不改世界状态。
  - "set_player"：更新玩家状态/物品，args.patch 是要合入 player 的对象。
  - "grant_item"：给玩家道具，args.item 是道具名，args.quantity 可选，args.reason 可选。
  - "complete_task"：完成任务，args.task_id 是任务 id。
  - "switch_npc"：切换当前 NPC，args.npc_id 是 NPC id。
  - "set_flag"：设置世界标记，args.key / args.value。
  - "run_world_action"：执行已配置动作，args.action_id 必须来自后台 actions 的 id。
- 如果 NPC 明确给玩家道具、报名表、资格、令牌、灵印，必须使用 command.grant_item；不要只写在 content 里。
- 如果道具也是任务完成条件，例如 completion.items=["报名表"]，必须让 player.inventory 包含该道具。
- 如果玩家问“怎么完成任务 / 下一步 / 卡住了 / 怎么算完成”，必须基于任务 completion 回答：
  - 说明判定条件，例如道具、等级/熟练度、关键词、好感度、地点或状态。
  - 不要只继续角色闲聊，必须给玩家明确可执行出口。
  - 只能建议 NPC 设定或动作效果中已经存在的地点；不要编造“练习室、小型见面会”等未配置地点。
  - 如果建议提升某个能力，必须说明去哪个已知地点、找哪个已知 NPC、做哪类已配置行动。
  - 这类询问不是完成动作，command 必须用 "none"，不要说“你已经完成了”。
- 只有玩家明确表达已经执行了某个动作，并且当前状态满足 completion，才允许使用 complete_task 或 run_world_action 推进；否则先指导玩家做什么、去哪里、找谁、说出什么关键词或提升哪个字段。
- 不要假装拥有配置之外的硬编码世界规则；如果设定不足，就围绕当前沙盒配置提问或推进。
- suggested_actions 显示为“线索笔记”，给 2-4 条玩家已经获得的信息、可追问的问题、地点人物线索；不要泄露后台动作列表。
- 不要直接写 quest_progress；任务进度由后端根据 command 和 completion 统一判定。
""".strip()

    def build_human_prompt(self, request: ChatRequest) -> str:
        return f"""
地点：{request.location}
玩家名：{request.player_name}
玩家世界内意图：{self._world_facing_player_goal(request.player_goal)}
玩家输入：{self._world_facing_text(request.message)}
""".strip()

    def record_player_message(self, state: AgentSessionState, request: ChatRequest, npc_state: NpcRuntimeState | None = None) -> None:
        if request.location:
            state.world_state.setdefault("player", {})["location"] = request.location
        speaker = self._resolve_speaker(state, request.target_npc_id)
        speaker_name = speaker.get("name", "NPC") if speaker else "NPC"
        safe_message = self._world_facing_text(request.message)
        state.add_memory(f"{request.player_name} 在 {request.location} 对 {speaker_name} 说：{safe_message}", 0.5)
        if npc_state is not None:
            npc_state.add_memory(f"{request.player_name} 对我说：{safe_message}", 0.65)
        self._append_conversation(state, "player", request.player_name, safe_message, speaker.get("id") if speaker else request.target_npc_id)
        evaluate_task_completions(state, request.message)

    def apply_llm_output(self, state: AgentSessionState, output: AgentLLMOutput, npc_state: NpcRuntimeState | None = None) -> None:
        self._normalize_visible_output(output)
        output.content = self._world_facing_text(output.content)
        state.emotion.apply_delta(output.emotion_delta)
        if npc_state is not None:
            npc_state.emotion.apply_delta(output.emotion_delta)
            npc_state.turn_count += 1
            npc_state.last_reply = output.content
        for memory in output.new_memories:
            memory = self._world_facing_text(memory)
            if npc_state is not None:
                npc_state.add_memory(memory, 0.7)
            else:
                state.add_memory(memory, 0.6)
        for goal in output.goal_updates:
            goal = self._world_facing_text(goal)
            if goal and goal not in state.goals:
                state.goals.append(goal)
            if npc_state is not None and goal and goal not in npc_state.goals:
                npc_state.goals.append(goal)
        self._sync_granted_items_from_text(state, output.content)
        speaker = self._npc_by_id(state, npc_state.npc_id) if npc_state is not None else self._active_npc(state)
        speaker_name = speaker.get("name", "NPC") if speaker else "NPC"
        self._append_conversation(state, "npc", speaker_name, output.content, speaker.get("id") if speaker else "")
        if npc_state is not None:
            npc_state.add_memory(f"我回应玩家：{output.content}", 0.6)
        evaluate_task_completions(state)

    def build_chat_response(
        self,
        state: AgentSessionState,
        output: AgentLLMOutput,
        player_goal: str,
        npc_state: NpcRuntimeState | None = None,
    ) -> ChatResponse:
        self._normalize_visible_output(output)
        fallback_actions = self.default_actions(state)
        suggested_actions = self.guardrail.sanitize_suggested_actions(output.suggested_actions[:4], fallback_actions)
        speaker = self._npc_by_id(state, npc_state.npc_id) if npc_state is not None else self._active_npc(state)
        speaker_name = speaker.get("name", "NPC") if speaker else "NPC"
        speaker_id = str(speaker.get("id") or "") if speaker else ""
        command = command_to_dict(output.command)
        return ChatResponse(
            reply=output.content,
            action_type=output.action_type,
            inner_thought=output.inner_thought,
            command=command,
            emotion=state.emotion.to_dict(),
            memories=[self._world_facing_text(item.content) for item in state.memories[-8:] if self._is_displayable_memory(item.content)],
            goals=[self._world_facing_text(goal) for goal in state.goals[-8:]],
            player_goal=self._world_facing_player_goal(player_goal),
            quest_progress=self._world_facing_text(state.quest_progress),
            suggested_actions=[self._world_facing_text(action) for action in suggested_actions],
            player=self._world_facing_player(state.world_state.get("player", {})),
            active_entity=self._world_facing_npc(self._active_npc(state)),
            speaker=self._world_facing_npc(speaker),
            npcs=self._world_facing_npcs(state.world_state.get("npcs", [])),
            nearby_npcs=self._world_facing_npcs(self._nearby_npcs(state)),
            messages=[
                ChatMessage(
                    role="npc",
                    npc_id=speaker_id,
                    speaker=speaker_name,
                    content=output.content,
                    action_type=output.action_type,
                    command=command,
                )
            ],
            debug_trace={
                "action_type": output.action_type,
                "command": command,
                "llm": output.model_extra or {},
                "llm_call_count": _llm_call_count(output),
                "npc_session": self._world_facing_debug_dict(npc_state.to_debug_dict()) if npc_state is not None else {},
                "lorebook": {
                    "artifact_id": self.lorebook.artifact_id,
                    "entry_count": len(self.lorebook.entries),
                    "active_entries": state.world_state.get("active_lorebook_entries", []),
                    "review": self.lorebook_review.model_dump(),
                },
            },
        )

    def default_actions(self, state: AgentSessionState) -> list[str]:
        return self.action_service.default_actions(state)

    def allowed_commands(self) -> list[str]:
        return ["none", "set_player", "grant_item", "complete_task", "switch_npc", "set_flag", "run_world_action"]

    def _normalize_visible_output(self, output: AgentLLMOutput) -> None:
        text = str(output.content or "").strip()
        if not (text.startswith("{") and text.endswith("}")):
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict) or not data.get("content"):
            return
        output.content = str(data.get("content") or "").strip()
        output.inner_thought = str(data.get("inner_thought") or output.inner_thought or "")
        output.reasoning = str(data.get("reasoning") or output.reasoning or "")
        output.criticism = str(data.get("criticism") or output.criticism or "")
        if isinstance(data.get("plan"), list):
            output.plan = [str(item) for item in data["plan"]]
        if isinstance(data.get("suggested_actions"), list):
            output.suggested_actions = [str(item) for item in data["suggested_actions"]]

    def world_action_ids(self) -> list[str]:
        return [action.id for action in self.config.actions]

    def _load_lorebook_artifact(self):
        raw = self.config.metadata.get("npc_lorebook") if isinstance(self.config.metadata, dict) else None
        if isinstance(raw, dict):
            try:
                from app.agents.npc_lorebook import NpcLorebookArtifact

                return NpcLorebookArtifact.model_validate(raw)
            except Exception:
                pass
        return NpcLorebookCompiler().compile(self.config)

    def _grant_item(self, state: AgentSessionState, item: str, quantity: object = 1, reason: str = "") -> None:
        player = state.world_state.setdefault("player", {})
        inventory = player.setdefault("inventory", [])
        if not isinstance(inventory, list):
            inventory = []
            player["inventory"] = inventory
        existing = next(
            (
                entry
                for entry in inventory
                if (entry == item) or (isinstance(entry, dict) and str(entry.get("name") or entry.get("id") or entry.get("label") or "") == item)
            ),
            None,
        )
        if existing is None:
            inventory.append({"name": item, "quantity": quantity})
        elif isinstance(existing, dict):
            try:
                existing["quantity"] = float(existing.get("quantity", 1)) + float(quantity or 1)
            except (TypeError, ValueError):
                existing["quantity"] = quantity
        state.add_memory(f"玩家获得道具：{item}{f'（{reason}）' if reason else ''}", 0.75)

    def _sync_granted_items_from_text(self, state: AgentSessionState, text: str) -> None:
        if not text:
            return
        positive_words = ("给你", "交给你", "递给你", "发给你", "授予你", "获得", "拿到", "领取", "已给", "已经给")
        if not any(word in text for word in positive_words):
            return
        candidates = self._known_completion_items(state)
        for item in candidates:
            if item and item in text and not self._player_has_item(state.world_state.get("player", {}), item):
                self._grant_item(state, item, reason="根据NPC对话自动同步")

    def _known_completion_items(self, state: AgentSessionState) -> list[str]:
        items: list[str] = []
        for task in state.world_state.get("tasks", []):
            completion = task.get("completion") if isinstance(task, dict) else None
            if not isinstance(completion, dict):
                continue
            raw_items = completion.get("items")
            if raw_items is None:
                continue
            if not isinstance(raw_items, list):
                raw_items = [raw_items]
            items.extend(str(item) for item in raw_items if item)
        return list(dict.fromkeys(items))

    def _player_has_item(self, player: dict, item: str) -> bool:
        values = []
        for key in ("inventory", "items"):
            raw = player.get(key)
            if isinstance(raw, list):
                values.extend(raw)
        for entry in values:
            if entry == item:
                return True
            if isinstance(entry, dict) and str(entry.get("name") or entry.get("id") or entry.get("label") or "") == item:
                return True
        return any(value is True and str(key) == item for key, value in player.items())

    def rag_hints(self, state: AgentSessionState) -> list[str]:
        hints = [self.config.name, *self.config.story_goals]
        hints.extend(npc.name for npc in self.config.npcs)
        hints.extend(task.title for task in self.config.tasks)
        return [hint for hint in hints if hint]

    def handle_world_action(
        self,
        state: AgentSessionState,
        request: WorldActionRequest,
    ) -> WorldActionResponse:
        return self.action_service.handle(state, request)

    def _resolve_speaker(self, state: AgentSessionState, target_npc_id: str = "") -> dict | None:
        npcs = state.world_state.get("npcs", [])
        if target_npc_id:
            found = next((npc for npc in npcs if npc.get("id") == target_npc_id), None)
            if found:
                state.world_state["active_npc_id"] = found.get("id")
                return found
        nearby = self._nearby_npcs(state)
        if nearby:
            state.world_state["active_npc_id"] = nearby[0].get("id")
            return nearby[0]
        state.world_state.pop("active_npc_id", None)
        return None

    def _append_conversation(self, state: AgentSessionState, role: str, speaker: str, content: str, npc_id: str = "") -> None:
        text = str(content or "").strip()
        if not text:
            return
        log = state.world_state.setdefault("conversation_log", [])
        if not isinstance(log, list):
            log = []
            state.world_state["conversation_log"] = log
        log.append(
            {
                "role": role,
                "speaker": speaker or role,
                "npc_id": npc_id or "",
                "content": text,
            }
        )
        state.world_state["conversation_log"] = log[-20:]

    def _format_conversation_log(self, state: AgentSessionState) -> str:
        log = state.world_state.get("conversation_log", [])
        if not isinstance(log, list) or not log:
            return "暂无。"
        lines = []
        for item in log[-10:]:
            if not isinstance(item, dict):
                continue
            speaker = str(item.get("speaker") or item.get("role") or "未知")
            content = self._world_facing_text(str(item.get("content") or "").strip())
            if content:
                lines.append(f"- {speaker}：{content}")
        return "\n".join(lines) or "暂无。"

    def _format_lorebook_context(
        self,
        state: AgentSessionState,
        request: ChatRequest,
        speaker: dict | None = None,
        npc_state: NpcRuntimeState | None = None,
    ) -> str:
        conversation = self._format_conversation_log(state)
        npc_id = str((speaker or {}).get("id") or (npc_state.npc_id if npc_state is not None else "") or request.target_npc_id or "")
        entries = self.lorebook_runtime.activate(
            message=self._world_facing_text(request.message),
            player_goal=self._world_facing_player_goal(request.player_goal),
            conversation=conversation,
            npc_id=npc_id,
            location=str(request.location or state.world_state.get("player", {}).get("location") or ""),
        )
        state.world_state["active_lorebook_entries"] = [entry.model_dump() for entry in entries]
        return self.lorebook_runtime.format_entries(entries)

    def _world_facing_player(self, player: dict) -> dict:
        if not isinstance(player, dict):
            return {}
        safe = {}
        for key, value in player.items():
            if isinstance(value, str):
                safe[key] = self._world_facing_text(value)
            elif isinstance(value, list):
                safe[key] = [self._world_facing_text(item) if isinstance(item, str) else item for item in value]
            else:
                safe[key] = value
        return safe

    def _world_facing_npc(self, npc: dict | None) -> dict | None:
        if not isinstance(npc, dict):
            return npc
        safe = dict(npc)
        for key in ("personality", "role"):
            if isinstance(safe.get(key), str):
                safe[key] = self._world_facing_text(safe[key])
        if isinstance(safe.get("goals"), list):
            safe["goals"] = [self._world_facing_text(goal) for goal in safe["goals"] if self._world_facing_text(goal)]
        return safe

    def _world_facing_npcs(self, npcs) -> list[dict]:
        return [safe for npc in npcs or [] if (safe := self._world_facing_npc(npc))]

    def _world_facing_collection(self, value):
        if isinstance(value, dict):
            return {key: self._world_facing_collection(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._world_facing_collection(item) for item in value]
        if isinstance(value, str):
            return self._world_facing_text(value)
        return value

    def _world_facing_debug_dict(self, value):
        if isinstance(value, dict):
            return {key: self._world_facing_debug_dict(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._world_facing_debug_dict(item) for item in value]
        if isinstance(value, str):
            return self._world_facing_text(value)
        return value

    def _world_facing_player_goal(self, text: str) -> str:
        cleaned = self._world_facing_text(text)
        return cleaned or "继续围绕当前事件打听线索、核对传闻，并推动当前任务。"

    def _world_facing_text(self, text: str) -> str:
        cleaned = str(text or "")
        replacements = {
            "故事图谱": "线索记录",
            "剧本图谱": "线索记录",
            "图谱": "线索记录",
            "ScriptGraphDocument": "线索记录",
            "script_graph": "线索记录",
            "story graph": "线索记录",
            "story_graph": "线索记录",
            "WorldTree": "传闻脉络",
            "world_tree": "传闻脉络",
            "世界树": "传闻脉络",
            "JSON": "记录",
            "json": "记录",
            "节点": "线索",
            "关系边": "关系",
            "图边": "关系",
            "开发者": "外人",
            "测试台": "记录册",
            "后台配置": "既有规矩",
        }
        for source, target in replacements.items():
            cleaned = cleaned.replace(source, target)
        return cleaned.strip()

    def _format_npc_runtime_state(self, npc_state: NpcRuntimeState | None, query: str = "") -> str:
        if npc_state is None:
            return "暂无独立 NPC 运行态。"
        payload = self._world_facing_debug_dict(npc_state.to_debug_dict())
        payload["relevant_memories"] = [item.content for item in npc_state.relevant_memories(query)]
        payload["relevant_memories"] = [self._world_facing_text(item) for item in payload["relevant_memories"]]
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _format_turn_director(self, npc_state: NpcRuntimeState | None) -> str:
        if npc_state is None or not npc_state.turn_plan:
            return "本轮尚未生成导演计划。"
        instruction = str(npc_state.turn_plan.get("instruction") or "").strip()
        return instruction or json.dumps(self._world_facing_debug_dict(npc_state.turn_plan), ensure_ascii=False, indent=2)

    def _format_npc_continuity(self, npc_state: NpcRuntimeState | None, query: str = "") -> str:
        if npc_state is None:
            return "暂无。"
        payload = {
            "relationship_stage": npc_state.relationship_stage,
            "memory_capsule": [self._world_facing_text(item) for item in npc_state.memory_capsule[-8:]],
            "working_memory": self._world_facing_debug_dict(npc_state.working_memory),
            "conversation_summaries": [self._world_facing_text(item) for item in npc_state.memory_summaries[-3:]],
            "relevant_memories": [self._world_facing_text(item.content) for item in npc_state.relevant_memories(query, limit=8)],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _story_graph_context(self) -> dict:
        metadata = self.config.metadata if isinstance(self.config.metadata, dict) else {}
        summary = metadata.get("story_graph_summary")
        if isinstance(summary, dict):
            return summary
        graph = metadata.get("script_graph")
        if isinstance(graph, dict):
            return {
                "graph_id": graph.get("graph_id", ""),
                "title": graph.get("title", ""),
                "schema_version": graph.get("schema_version", ""),
                "ontology": graph.get("ontology", {}),
                "indexes": graph.get("indexes", {}),
                "metadata": graph.get("metadata", {}),
                "nodes": graph.get("nodes", [])[:40] if isinstance(graph.get("nodes"), list) else [],
                "edges": graph.get("edges", [])[:80] if isinstance(graph.get("edges"), list) else [],
            }
        return {}

    def _format_story_graph_context(self, state: AgentSessionState) -> str:
        graph = state.world_state.get("script_graph") or self._story_graph_context()
        if not isinstance(graph, dict) or not graph:
            return "No ScriptGraphDocument is attached to this world."
        return json.dumps(graph, ensure_ascii=False, indent=2)

    def _active_npc(self, state: AgentSessionState) -> dict | None:
        active_id = state.world_state.get("active_npc_id")
        npcs = state.world_state.get("npcs", [])
        if active_id:
            return next((npc for npc in npcs if npc.get("id") == active_id), None)
        return npcs[0] if npcs else None

    def _npc_by_id(self, state: AgentSessionState, npc_id: str) -> dict | None:
        return next((npc for npc in state.world_state.get("npcs", []) if str(npc.get("id") or "") == npc_id), None)

    def _nearby_npcs(self, state: AgentSessionState) -> list[dict]:
        location = str(state.world_state.get("player", {}).get("location") or "").strip()
        if not location:
            return []
        return [
            npc
            for npc in state.world_state.get("npcs", [])
            if self._npc_matches_location(npc, location)
        ]

    def _format_location_presence(
        self,
        state: AgentSessionState,
        request: ChatRequest,
        nearby_npcs: list[dict],
        speaker: dict | None,
    ) -> str:
        location = str(request.location or state.world_state.get("player", {}).get("location") or "当前位置").strip()
        names = [str(npc.get("name") or npc.get("id") or "NPC") for npc in nearby_npcs]
        speaker_name = str((speaker or {}).get("name") or (speaker or {}).get("id") or "当前 NPC")
        if not names:
            return f"当前位置：{location}。这里没有配置为在场的 NPC；当前只按 {speaker_name} 的视角回应。"
        if len(names) == 1:
            return f"当前位置：{location}。在场 NPC 只有：{names[0]}。请按单人对话处理。"
        return f"当前位置：{location}。在场 NPC：{'、'.join(names)}。只有这些 NPC 可以被视为现场人物。"

    def _npc_matches_location(self, npc: dict, location: str) -> bool:
        npc_locations = set(_npc_locations(npc))
        current_locations = {str(location or "").strip()}
        if not next(iter(current_locations), ""):
            return False
        return bool(npc_locations & current_locations)

    def _next_pending_task(self, state: AgentSessionState) -> dict | None:
        for task in state.world_state.get("tasks", []):
            if isinstance(task, dict) and task.get("status", "pending") not in {"done", "skipped"}:
                return task
        return None

    def _task_hint(self, task: dict | None, state: AgentSessionState | None = None) -> str:
        if not task:
            return ""
        completion = task.get("completion") if isinstance(task.get("completion"), dict) else {}
        hints: list[str] = []
        if completion.get("items"):
            hints.append(f"留意能否找到{'、'.join(str(item) for item in self._as_list(completion.get('items')))}。")
        if completion.get("location"):
            hints.append(f"可以去{completion.get('location')}看看。")
        if completion.get("keywords"):
            hints.append(f"可以追问：{'、'.join(str(item) for item in self._as_list(completion.get('keywords')))}。")
        if state is not None:
            nearby = self._nearby_npcs(state)
            if nearby:
                names = "、".join(str(npc.get("name") or npc.get("id") or "NPC") for npc in nearby)
                hints.append(f"先从{names}口中探一探，看看能否获得新的线索。")
        return "".join(hints) or "继续搜证、盘问人物，等线索彼此能对上时再推进指认。"

    def _known_locations(self, state: AgentSessionState) -> list[str]:
        current = str(state.world_state.get("player", {}).get("location") or "").strip()
        locations = []
        for npc in state.world_state.get("npcs", []):
            for place in _npc_locations(npc):
                if place and place != current and place not in locations:
                    locations.append(place)
        for action in self.config.actions:
            effect = action.effect if isinstance(action.effect, dict) else {}
            set_player = effect.get("set_player") if isinstance(effect.get("set_player"), dict) else {}
            location = str(set_player.get("location") or "").strip()
            if location and location != current and location not in locations:
                locations.append(location)
        return locations

    def _as_list(self, value) -> list:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    def _format_rag_context(self, state: AgentSessionState) -> str:
        rag = state.rag_context
        if not rag.documents:
            return "没有检索到可靠历史记忆。"
        lines = [
            f"状态：{rag.note}",
            f"原始查询：{rag.original_query}",
            f"可靠：{'是' if rag.reliable else '否'}",
        ]
        if rag.rewritten_query:
            lines.append(f"改写查询：{rag.rewritten_query}")
        for doc in rag.documents[:6]:
            lines.append(f"- [{doc.verdict} {doc.relevance:.2f}] {doc.content}")
        return "\n".join(lines)

    def _format_list(self, values: list[str]) -> str:
        return "\n".join(f"- {value}" for value in values) if values else "- 未设定"

    def _is_displayable_memory(self, content: str) -> bool:
        bad_chars = content.count("?") + content.count("\ufffd")
        return bad_chars / max(1, len(content)) <= 0.15


def _llm_call_count(output: AgentLLMOutput) -> int:
    trace = getattr(output, "provider_trace", None) or []
    return sum(1 for item in trace if str(item.get("stage") or "").endswith("_prompt"))


def _npc_locations(npc: dict) -> list[str]:
    raw = npc.get("locations") if isinstance(npc, dict) else None
    if isinstance(raw, list) and raw:
        values: list[str] = []
        for item in raw:
            values.extend(split_location_values(item))
        return list(dict.fromkeys(values))
    # Compatibility for old generated worlds that encoded multiple locations in one string.
    return split_location_values((npc or {}).get("location"))
