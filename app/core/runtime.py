from langchain_core.messages import HumanMessage, SystemMessage
import random
import re

from app.agents.npc_runtime.conversation_review import NpcConversationReview
from app.agents.npc_runtime.memory_lifecycle import NpcMemoryLifecycle
from app.agents.npc_runtime.turn_director import NpcTurnDirector, NpcTurnPlan
from app.core.agents import NpcAgent, RouterAgent, StateValidatorAgent
from app.core.llm import LLMClient
from app.core.memory import MemoryStore, create_memory_store
from app.core.models import (
    AgentLLMOutput,
    AgentSessionState,
    AutonomousTickRequest,
    AutonomousTickResponse,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    MemoryItem,
    MemoryQueryRequest,
    MemoryQueryResponse,
    NpcRuntimeState,
    SessionSnapshotResponse,
    WorldActionRequest,
    WorldActionResponse,
    WorldAdapter,
)
from app.core.planner import Planner
from app.core.providers import create_npc_llm_client
from app.core.rag import CorrectiveRagPipeline
from app.core.review_agents import FlowReviewAgent, NpcReviewAgent, PlaytestAgent, UiReviewAgent, UiStateProjector
from app.core.session_store import RuntimeSessionStore, load_agent_state, load_npc_runtime
from app.worlds.sandbox.qingmeng_triggers import maybe_handle_qingmeng_trigger


class AgentRuntime:
    def __init__(
        self,
        adapter: WorldAdapter,
        llm_client: LLMClient | None = None,
        memory_store: MemoryStore | None = None,
        session_store: RuntimeSessionStore | None = None,
        planner: Planner | None = None,
        router: RouterAgent | None = None,
        npc_agent: NpcAgent | None = None,
        state_validator: StateValidatorAgent | None = None,
    ) -> None:
        self.adapter = adapter
        self.llm = llm_client or create_npc_llm_client()
        self.memory = memory_store or create_memory_store(adapter.world_id)
        self.session_store = session_store or RuntimeSessionStore()
        self.rag = CorrectiveRagPipeline(self.memory)
        self.planner = planner or Planner()
        self.router = router or RouterAgent()
        self.npc_agent = npc_agent or NpcAgent(self.llm)
        self.npc_agents: dict[str, NpcAgent] = {}
        self.npc_sessions: dict[str, NpcRuntimeState] = {}
        self.state_validator = state_validator or StateValidatorAgent()
        self.npc_review_agent = NpcReviewAgent()
        self.turn_director = NpcTurnDirector()
        self.memory_lifecycle = NpcMemoryLifecycle()
        self.conversation_review = NpcConversationReview()
        self.ui_review_agent = UiReviewAgent()
        self.ui_state_projector = UiStateProjector()
        self.playtest_agent = PlaytestAgent()
        self.flow_review_agent = FlowReviewAgent()
        self.state: AgentSessionState = adapter.create_initial_state()
        self._restore_session()
        self._ensure_npc_runtimes()
        self._connect_memory()

    async def chat(self, request: ChatRequest) -> ChatResponse:
        if request.group_chat or request.target_npc_ids:
            return await self.group_chat(request)

        if not request.player_goal:
            request.player_goal = self.adapter.default_player_goal()

        self._hydrate_relevant_memories(request.message)
        npc_id = self._resolve_runtime_npc_id(request)
        npc_state = self._get_npc_session(npc_id)
        self.memory_lifecycle.prepare_turn(npc_state, request)
        turn_plan = self.turn_director.plan(self.state, request, npc_state)
        npc_state.turn_plan["instruction"] = self.turn_director.format_instruction(turn_plan)
        self.adapter.record_player_message(self.state, request, npc_state)
        route = self.router.route_chat(request)
        if route != "npc_agent":
            route = "npc_agent"
        output = maybe_handle_qingmeng_trigger(self.state, request, npc_id)
        if output is None:
            output = await self._get_npc_agent(npc_id, request).respond(self.adapter, self.state, request, npc_state)
        if self._needs_adapter_recovery(output):
            output = await self._retry_provider_failure(request, output, npc_state)
        else:
            output = await self._repair_output_with_guardrail(request, output, npc_state)
        output = await self._repair_output_with_conversation_review(request, output, npc_state, turn_plan)
        if self.router.route_output(output) == "state_validator_agent":
            self.state_validator.apply(self.adapter, self.state, output, npc_state)
        self.memory_lifecycle.commit_turn(npc_state, request, output)
        self.state.world_state.setdefault("reviews", {})["npc_review"] = self.npc_review_agent.review(output, self.state).model_dump()
        self._sync_npc_runtime_debug()
        response = self.adapter.build_chat_response(self.state, output, request.player_goal, npc_state)
        self._save_session()
        return response

    async def group_chat(self, request: ChatRequest) -> ChatResponse:
        if not request.player_goal:
            request.player_goal = self.adapter.default_player_goal()

        self._hydrate_relevant_memories(request.message)
        participant_ids = self._resolve_group_participant_ids(request)
        if not participant_ids:
            return await self._empty_group_response(request)

        first_state = self._get_npc_session(participant_ids[0])
        self.adapter.record_player_message(self.state, request, first_state)
        for npc_id in participant_ids[1:]:
            self._get_npc_session(npc_id).add_memory(f"{request.player_name} 对在场众人说：{request.message}", 0.55)

        messages: list[ChatMessage] = []
        inner_thoughts: list[str] = []
        debug_outputs: list[dict] = []
        last_output = None
        last_npc_state = first_state

        for npc_id in self._speaker_order(participant_ids):
            npc_state = self._get_npc_session(npc_id)
            turn_request = request.model_copy(
                update={
                    "target_npc_id": npc_id,
                    "target_npc_ids": [],
                    "group_chat": False,
                    "message": self._group_turn_message(request.message, messages),
                }
            )
            self.memory_lifecycle.prepare_turn(npc_state, turn_request)
            turn_plan = self.turn_director.plan(self.state, turn_request, npc_state)
            npc_state.turn_plan["instruction"] = self.turn_director.format_instruction(turn_plan)
            output = maybe_handle_qingmeng_trigger(self.state, turn_request, npc_id)
            if output is None:
                output = await self._get_npc_agent(npc_id, turn_request).respond(self.adapter, self.state, turn_request, npc_state)
            if self._needs_adapter_recovery(output):
                output = await self._retry_provider_failure(turn_request, output, npc_state)
            else:
                output = await self._repair_output_with_guardrail(turn_request, output, npc_state)
            output = await self._repair_output_with_conversation_review(turn_request, output, npc_state, turn_plan)
            if self.router.route_output(output) == "state_validator_agent":
                self.state_validator.apply(self.adapter, self.state, output, npc_state)
            self.memory_lifecycle.commit_turn(npc_state, turn_request, output)
            self.state.world_state.setdefault("reviews", {})[f"npc_review:{npc_id}"] = self.npc_review_agent.review(output, self.state).model_dump()

            speaker = self._npc_by_id(npc_id)
            command = getattr(output, "command", None)
            messages.append(
                ChatMessage(
                    role="npc",
                    npc_id=npc_id,
                    speaker=str((speaker or {}).get("name") or npc_id or "NPC"),
                    content=output.content,
                    action_type=output.action_type,
                    command=self._command_dict(command),
                )
            )
            self.state.add_memory(f"{messages[-1].speaker} 在群聊中说：{output.content}", 0.52)
            self._append_group_conversation(messages[-1])
            inner_thoughts.append(f"{messages[-1].speaker}: {output.inner_thought}")
            debug_outputs.append(
                {
                    "npc_id": npc_id,
                    "action_type": output.action_type,
                    "command": self._command_dict(command),
                    "llm": self._world_facing_debug(output.model_extra or {}),
                    "llm_call_count": self._llm_call_count(output),
                    "npc_session": self._world_facing_debug(npc_state.to_debug_dict()),
                    "turn_director": self._world_facing_debug(npc_state.turn_plan),
                    "conversation_review": self._world_facing_debug(npc_state.conversation_review),
                }
            )
            last_output = output
            last_npc_state = npc_state

        self._sync_npc_runtime_debug()
        base = self.adapter.build_chat_response(self.state, last_output, request.player_goal, last_npc_state)
        base.messages = messages
        base.reply = "\n".join(f"{item.speaker}：{item.content}" for item in messages)
        base.action_type = "group"
        base.inner_thought = "\n".join(inner_thoughts)
        base.command = {"name": "group", "args": {"npc_ids": participant_ids}}
        base.debug_trace = {
            "group_chat": True,
            "npc_outputs": debug_outputs,
            "llm_call_count": sum(item.get("llm_call_count", 0) for item in debug_outputs),
        }
        self._save_session()
        return base

    async def _repair_output_with_guardrail(self, request: ChatRequest, output, npc_state: NpcRuntimeState | None = None):
        guardrail = getattr(self.adapter, "guardrail", None)
        if guardrail is None:
            return output
        violations = guardrail.output_violations(output.content, output.suggested_actions)
        if not violations:
            return output

        self.state.add_memory(f"Guardrail rejected NPC output locations: {', '.join(violations[:6])}", 0.8)
        current_violations = violations
        for attempt in range(1, 3):
            feedback = guardrail.retry_instruction(current_violations, attempt=attempt)
            if npc_state is not None:
                npc_state.add_memory(
                    f"上一次回复被运行时校验拒绝：{feedback} 请重新用当前 NPC 的世界内口吻回答，不要输出系统校验说明。",
                    0.95,
                )
            retry_messages = [
                SystemMessage(content=self.adapter.build_system_prompt(self.state, request, npc_state)),
                HumanMessage(content=self.adapter.build_human_prompt(request)),
                SystemMessage(content=feedback),
            ]
            retry_agent = self._get_npc_agent(getattr(npc_state, "npc_id", "") if npc_state is not None else "", request)
            repaired = await retry_agent.respond_with_messages(retry_messages, self.adapter.default_actions(self.state))
            retry_violations = guardrail.output_violations(repaired.content, repaired.suggested_actions)
            if not retry_violations:
                repaired.model_extra["guardrail_repaired"] = True
                repaired.model_extra["guardrail_retry_attempts"] = attempt
                repaired.model_extra["guardrail_violations"] = violations
                return repaired
            current_violations = retry_violations
            self.state.add_memory(f"Guardrail retry {attempt} still invalid: {', '.join(retry_violations[:6])}", 0.85)

        self.state.add_memory(f"Guardrail failed after NPC retries: {', '.join(current_violations[:6])}", 0.9)
        if npc_state is not None:
            npc_state.add_memory(
                f"连续两次回复仍未通过地点校验：{', '.join(current_violations[:6])}。下一轮必须只使用当前世界已登记地点和在场人物。",
                0.98,
            )
        raise RuntimeError(f"NPC 回复未通过地点校验，已要求 NPC 重新生成但仍失败：{', '.join(current_violations[:6])}")

    async def _retry_provider_failure(
        self,
        request: ChatRequest,
        output: AgentLLMOutput,
        npc_state: NpcRuntimeState,
    ) -> AgentLLMOutput:
        current = output
        for attempt in range(1, 3):
            error = (current.model_extra or {}).get("provider_error") or {}
            error_type = str(error.get("type") or "provider_output_failed")
            feedback = (
                f"上一版回复因 {error_type} 未能进入对话。请保持当前 NPC 身份，基于原玩家输入重新生成完整回复。"
                "只返回合法 AgentLLMOutput JSON；不要解释错误、协议、模型或系统。"
            )
            npc_state.working_memory["last_provider_feedback"] = feedback
            retry_messages = [
                SystemMessage(content=self.adapter.build_system_prompt(self.state, request, npc_state)),
                HumanMessage(content=self.adapter.build_human_prompt(request)),
                SystemMessage(content=feedback),
            ]
            current = await self._get_npc_agent(npc_state.npc_id, request).respond_with_messages(
                retry_messages,
                self.adapter.default_actions(self.state),
            )
            if not self._needs_adapter_recovery(current):
                current.model_extra["provider_retry_attempts"] = attempt
                return await self._repair_output_with_guardrail(request, current, npc_state)
        raise RuntimeError("NPC 模型连续三次未返回可消费的结构化回复")

    async def _repair_output_with_conversation_review(
        self,
        request: ChatRequest,
        output: AgentLLMOutput,
        npc_state: NpcRuntimeState,
        turn_plan: NpcTurnPlan,
    ) -> AgentLLMOutput:
        current = output
        for attempt in range(0, 3):
            result = self.conversation_review.review(current, request, npc_state, turn_plan)
            npc_state.conversation_review = {
                **result.model_dump(),
                "attempt": attempt,
            }
            if result.passed:
                current.model_extra["conversation_review"] = npc_state.conversation_review
                current.model_extra["turn_director"] = npc_state.turn_plan
                return current
            if attempt >= 2:
                break
            npc_state.working_memory["last_review_feedback"] = result.retry_instruction
            retry_messages = [
                SystemMessage(content=self.adapter.build_system_prompt(self.state, request, npc_state)),
                HumanMessage(content=self.adapter.build_human_prompt(request)),
                SystemMessage(content=result.retry_instruction),
            ]
            retry_agent = self._get_npc_agent(npc_state.npc_id, request)
            current = await retry_agent.respond_with_messages(retry_messages, self.adapter.default_actions(self.state))
            current = await self._repair_output_with_guardrail(request, current, npc_state)

        issue_codes = [str(item.get("code") or "review_failed") for item in npc_state.conversation_review.get("issues", [])]
        raise RuntimeError(f"NPC 回复连续三次未通过对话复核：{', '.join(issue_codes)}")

    def world_action(self, request: WorldActionRequest) -> WorldActionResponse:
        response = self.adapter.handle_world_action(self.state, request)
        self.planner.mark_result(self.state, request, success=bool(response.narration))
        self._ensure_npc_runtimes()
        self._save_session()
        return response

    def autonomous_tick(self, request: AutonomousTickRequest) -> AutonomousTickResponse:
        objective = request.objective or self.adapter.default_player_goal()
        self.planner.ensure_plan(self.state, objective, self.adapter.default_actions(self.state))

        executed = []
        stopped_reason = "max_steps_reached"
        for _ in range(max(1, request.max_steps)):
            action_request = self.planner.next_action(self.state)
            if action_request is None:
                stopped_reason = "no_pending_action"
                break
            executed.append(self.world_action(action_request))

        response = AutonomousTickResponse(
            objective=objective,
            executed=executed,
            plan=[step.model_dump() for step in self.state.plan],
            stopped_reason=stopped_reason,
        )
        self._save_session()
        return response

    def query_memory(self, request: MemoryQueryRequest) -> MemoryQueryResponse:
        context = self.rag.run(request.query, self.adapter.rag_hints(self.state))
        return MemoryQueryResponse(rag=context)

    def snapshot(self) -> SessionSnapshotResponse:
        self._ensure_npc_runtimes()
        self._sync_npc_runtime_debug()
        world_state = self.state.world_state
        world_state.setdefault("reviews", {})["ui_projection"] = self.ui_state_projector.project(world_state)
        world_state.setdefault("reviews", {})["ui_review"] = self.ui_review_agent.review(world_state).model_dump()
        world_state.setdefault("reviews", {})["playtest_review"] = self.playtest_agent.simulate_adapter(self.adapter).model_dump()
        world_state.setdefault("reviews", {})["flow_review"] = self.flow_review_agent.review(world_state).model_dump()
        npcs = world_state.get("npcs", [])
        active_id = world_state.get("active_npc_id")
        speaker = None
        if active_id:
            speaker = next((npc for npc in npcs if npc.get("id") == active_id), None)
        player_location = str(world_state.get("player", {}).get("location") or "").strip()
        nearby_npcs = [
            npc
            for npc in npcs
            if player_location and _npc_matches_location(npc, player_location)
        ]
        if nearby_npcs:
            speaker = nearby_npcs[0]
        elif speaker and str(speaker.get("location") or "").strip() != player_location:
            speaker = None

        return SessionSnapshotResponse(
            world_id=self.adapter.world_id,
            started=bool(world_state),
            state=world_state,
            player=world_state.get("player", {}),
            active_entity=world_state.get("active_monster") or speaker,
            speaker=speaker,
            npcs=npcs,
            nearby_npcs=nearby_npcs,
            quest_progress=self.state.quest_progress,
            goals=self.state.goals[-8:],
            suggested_actions=self.adapter.default_actions(self.state),
        )

    def _ensure_npc_runtimes(self) -> None:
        for npc in self.state.world_state.get("npcs", []):
            npc_id = str(npc.get("id") or "").strip()
            if npc_id:
                self._get_npc_agent(npc_id)
                self._get_npc_session(npc_id)
        self._sync_npc_runtime_debug()

    def _restore_session(self) -> None:
        payload = self.session_store.load(self.adapter.world_id)
        if not payload:
            return
        state_data = payload.get("state")
        if isinstance(state_data, dict):
            self.state = load_agent_state(state_data)
        npc_data = payload.get("npc_sessions")
        if isinstance(npc_data, dict):
            self.npc_sessions = {
                str(npc_id): load_npc_runtime(data)
                for npc_id, data in npc_data.items()
                if isinstance(data, dict)
            }

    def _save_session(self) -> None:
        self._sync_npc_runtime_debug()
        self.session_store.save(self.adapter.world_id, self.state, self.npc_sessions)

    def _resolve_runtime_npc_id(self, request: ChatRequest) -> str:
        requested = str(request.target_npc_id or "").strip()
        npcs = self.state.world_state.get("npcs", [])
        location = str(request.location or self.state.world_state.get("player", {}).get("location") or "").strip()
        if requested:
            requested_npc = next((npc for npc in npcs if str(npc.get("id") or "") == requested), None)
            if requested_npc is None:
                raise RuntimeError(f"目标 NPC 不存在：{requested}")
            if not location or not _npc_matches_location(requested_npc, location):
                raise RuntimeError(f"目标 NPC 不在当前位置：{requested}")
            return requested
        active = str(self.state.world_state.get("active_npc_id") or "").strip()
        if active and any(
            str(npc.get("id") or "") == active and location and _npc_matches_location(npc, location)
            for npc in npcs
        ):
            return active
        nearby = next((npc for npc in npcs if location and _npc_matches_location(npc, location)), None)
        if nearby and nearby.get("id"):
            return str(nearby["id"])
        raise RuntimeError("当前位置没有可对话的 NPC")

    def _resolve_group_participant_ids(self, request: ChatRequest) -> list[str]:
        npcs = self.state.world_state.get("npcs", [])
        location = str(request.location or self.state.world_state.get("player", {}).get("location") or "").strip()
        nearby_ids = [
            str(npc.get("id") or "")
            for npc in npcs
            if npc.get("id") and location and _npc_matches_location(npc, location)
        ]
        requested = [str(npc_id).strip() for npc_id in request.target_npc_ids if str(npc_id).strip()]
        if request.target_npc_id and request.target_npc_id != "__nearby__":
            requested.append(str(request.target_npc_id).strip())
        selected = [npc_id for npc_id in requested if npc_id in nearby_ids] if requested else nearby_ids
        deduped = list(dict.fromkeys(selected))
        limit = max(1, min(int(request.max_npc_replies or len(deduped) or 1), 50))
        return deduped[:limit]

    def _get_npc_agent(self, npc_id: str, request: ChatRequest | None = None) -> NpcAgent:
        if request is not None and request.npc_llm is not None:
            return NpcAgent(create_npc_llm_client(config=request.npc_llm))
        key = npc_id or "default"
        if key not in self.npc_agents:
            self.npc_agents[key] = NpcAgent(self.llm)
        return self.npc_agents[key]

    def _get_npc_session(self, npc_id: str) -> NpcRuntimeState:
        key = npc_id or "default"
        if key not in self.npc_sessions:
            self.npc_sessions[key] = NpcRuntimeState(npc_id=key)
        return self.npc_sessions[key]

    def _speaker_order(self, participant_ids: list[str]) -> list[str]:
        ordered = participant_ids[:]
        random.shuffle(ordered)
        return ordered

    def _group_turn_message(self, player_message: str, messages: list[ChatMessage]) -> str:
        player_message = self._world_facing_text(player_message)
        if not messages:
            return (
                f"{player_message}\n\n"
                "这是群聊第一轮。你可以回应玩家，也可以选择沉默；如果沉默，content 只输出 \"...\"。不要提世界外的数据结构、开发工具、配置台或调试概念。"
            )
        transcript = "\n".join(f"{item.speaker}：{self._world_facing_text(item.content)}" for item in messages)
        return (
            f"{player_message}\n\n"
            "本轮群聊中，前面已经有人说过：\n"
            f"{transcript}\n\n"
            "请你基于上面的发言自然接话。你可以补充、反驳、追问，也可以选择沉默；如果沉默，content 只输出 \"...\"。不要提世界外的数据结构、开发工具、配置台或调试概念。"
        )

    def _append_group_conversation(self, message: ChatMessage) -> None:
        log = self.state.world_state.setdefault("conversation_log", [])
        if not isinstance(log, list):
            log = []
            self.state.world_state["conversation_log"] = log
        log.append(
            {
                "role": message.role,
                "speaker": message.speaker,
                "npc_id": message.npc_id,
                "content": self._world_facing_text(message.content),
            }
        )
        self.state.world_state["conversation_log"] = log[-20:]

    def _world_facing_text(self, text: str) -> str:
        sanitizer = getattr(self.adapter, "_world_facing_text", None)
        if callable(sanitizer):
            return sanitizer(text)
        return str(text or "")

    def _world_facing_debug(self, value):
        if isinstance(value, dict):
            return {key: self._world_facing_debug(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._world_facing_debug(item) for item in value]
        if isinstance(value, str):
            return self._world_facing_text(value)
        return value

    def _llm_call_count(self, output: AgentLLMOutput) -> int:
        trace = getattr(output, "provider_trace", None) or []
        return sum(1 for item in trace if str(item.get("stage") or "").endswith("_prompt"))

    def _npc_by_id(self, npc_id: str) -> dict | None:
        return next((npc for npc in self.state.world_state.get("npcs", []) if str(npc.get("id") or "") == npc_id), None)

    def _command_dict(self, command) -> dict:
        if command is None:
            return {"name": "none", "args": {}}
        if isinstance(command, dict):
            return command or {"name": "none", "args": {}}
        return command.model_dump()

    async def _empty_group_response(self, request: ChatRequest) -> ChatResponse:
        output = AgentLLMOutput(
            action_type="hint",
            content="当前位置没有可参与群聊的 NPC。",
            inner_thought="No NPC participants were available for group chat.",
            command={"name": "none", "args": {}},
            suggested_actions=self.adapter.default_actions(self.state),
        )
        response = self.adapter.build_chat_response(self.state, output, request.player_goal, None)
        response.speaker = None
        response.active_entity = None
        response.messages = []
        response.debug_trace = {
            "group_chat": True,
            "participant_ids": [],
            "llm_call_count": 0,
            "reason": "no_npc_at_current_location",
        }
        return response

    def _sync_npc_runtime_debug(self) -> None:
        self.state.world_state["npc_sessions"] = {
            npc_id: session.to_debug_dict()
            for npc_id, session in sorted(self.npc_sessions.items())
        }

    def _connect_memory(self) -> None:
        for item in self.state.memories:
            self.memory.add(item)
        self.state.memory_writer = self.memory.add
        self.state.memories = self.memory.list_recent()

    def _hydrate_relevant_memories(self, query: str) -> None:
        hints = self.adapter.rag_hints(self.state)
        self.state.rag_context = self.rag.run(query, hints)
        existing_ids = {item.id for item in self.state.memories}
        for doc in self.state.rag_context.documents:
            item = MemoryItem(
                id=doc.id,
                content=doc.content,
                importance=doc.importance,
            )
            if item.id not in existing_ids:
                self.state.memories.append(item)
        self.state.memories = self.state.memories[-30:]

    def _needs_adapter_recovery(self, output) -> bool:
        if getattr(output, "action_type", "") != "wait":
            return False
        extra = getattr(output, "model_extra", None) or {}
        return bool(extra.get("provider_error"))


def _npc_matches_location(npc: dict, location: str) -> bool:
    npc_locations = set(_npc_locations(npc))
    current_locations = {str(location or "").strip()}
    if not next(iter(current_locations), ""):
        return False
    return bool(npc_locations & current_locations)


def _npc_locations(npc: dict) -> list[str]:
    raw = npc.get("locations") if isinstance(npc, dict) else None
    if isinstance(raw, list) and raw:
        return list(dict.fromkeys(str(item).strip() for item in raw if str(item or "").strip()))
    text = str((npc or {}).get("location") or "").strip()
    if not text:
        return []
    # Compatibility for old generated worlds that encoded multiple locations in one string.
    return [part.strip() for part in re.split(r"[/／|｜,，、;；]+", text) if part.strip()]
