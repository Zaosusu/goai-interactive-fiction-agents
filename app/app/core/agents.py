from langchain_core.messages import HumanMessage, SystemMessage

from app.core.command_executor import CommandExecutor
from app.core.commands import CommandValidator
from app.core.llm import LLMClient
from app.core.models import AgentLLMOutput, AgentSessionState, ChatRequest, NpcRuntimeState, WorldAdapter, command_to_dict


class RouterAgent:
    """
    Minimal coordinator for the runtime.

    The router makes the agent split explicit:
    - world_builder_agent: generation flow, currently used by sandbox.generator
    - npc_agent: player-facing NPC response
    - state_validator_agent: validates/applies structured commands
    """

    def route_chat(self, request: ChatRequest) -> str:
        if request.message.strip():
            return "npc_agent"
        return "state_validator_agent"

    def route_output(self, output: AgentLLMOutput) -> str:
        if command_to_dict(output.command).get("name") not in {None, "", "none"}:
            return "state_validator_agent"
        return "state_validator_agent"

    def world_generation_pipeline(self) -> list[str]:
        return [
            "WorldBuilderAgent",
            "WorldValidator/SchemaRepairer",
            "NpcLorebookCreationAgent",
            "WorldReviewAgent",
        ]

    def npc_runtime_pipeline(self) -> list[str]:
        return [
            "NpcLorebookReviewAgent",
            "NpcLorebookRuntime",
            "NpcAgent",
            "AgentLLMOutput schema gate",
            "NpcProtocolReviewAgent(if invalid)",
            "StateValidatorAgent",
            "NpcReviewAgent",
        ]

    def ui_projection_pipeline(self) -> list[str]:
        return ["UiStateProjector", "UiReviewAgent"]

    def playtest_pipeline(self) -> list[str]:
        return [
            "SandboxWorldAdapter",
            "NpcLorebookReviewAgent",
            "NpcLorebookRuntime",
            "Full visual-bound world assets",
            "PlaytestAgent",
            "FlowReviewAgent",
        ]


class NpcAgent:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def respond(
        self,
        adapter: WorldAdapter,
        state: AgentSessionState,
        request: ChatRequest,
        npc_state: NpcRuntimeState | None = None,
    ) -> AgentLLMOutput:
        messages = [
            SystemMessage(content=adapter.build_system_prompt(state, request, npc_state)),
            HumanMessage(content=adapter.build_human_prompt(request)),
        ]
        return await self.llm.invoke(messages, adapter.default_actions(state))

    async def respond_with_messages(self, messages: list, fallback_actions: list[str]) -> AgentLLMOutput:
        return await self.llm.invoke(messages, fallback_actions)


class StateValidatorAgent:
    def __init__(self, validator: CommandValidator | None = None, executor: CommandExecutor | None = None) -> None:
        self.validator = validator or CommandValidator()
        self.executor = executor or CommandExecutor()

    def apply(
        self,
        adapter: WorldAdapter,
        state: AgentSessionState,
        output: AgentLLMOutput,
        npc_state: NpcRuntimeState | None = None,
    ) -> None:
        result = self.validator.validate(adapter, state, output)
        output.command = result.command
        if not result.valid:
            state.add_memory(f"Rejected invalid command: {'; '.join(result.errors)}", 0.8)
        else:
            self.executor.execute(adapter, state, output)
        adapter.apply_llm_output(state, output, npc_state)
