from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.creator_assistant.schema import CreatorToolCall
from app.agents.creator_assistant.tools import CreatorToolExecutor, CreatorToolRegistry, ProgressCallback


class McpToolAnnotations(BaseModel):
    title: str = ""
    readOnlyHint: bool = False
    destructiveHint: bool = False
    idempotentHint: bool = False
    openWorldHint: bool = False


class McpToolDefinition(BaseModel):
    name: str
    title: str = ""
    description: str
    inputSchema: dict[str, Any]
    annotations: McpToolAnnotations = Field(default_factory=McpToolAnnotations)
    meta: dict[str, Any] = Field(default_factory=dict, alias="_meta")

    model_config = {"populate_by_name": True}


class McpToolsListResult(BaseModel):
    tools: list[McpToolDefinition]


class McpTextContent(BaseModel):
    type: Literal["text"] = "text"
    text: str


class McpCallToolResult(BaseModel):
    content: list[McpTextContent] = Field(default_factory=list)
    structuredContent: dict[str, Any] = Field(default_factory=dict)
    isError: bool = False


class CreatorMcpCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    project: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)


class CreatorMcpToolServer:
    """MCP-shaped tool boundary used by Creator function calling and workflows.

    Transport concerns are intentionally outside this class. The Creator workflow host
    injects the current project/artifacts as execution context, while tool definitions,
    calls, and results follow MCP naming and result-envelope conventions.
    """

    def __init__(self, *, registry: CreatorToolRegistry, executor: CreatorToolExecutor) -> None:
        self.registry = registry
        self.executor = executor

    def list_tools(self) -> McpToolsListResult:
        return McpToolsListResult(
            tools=[
                McpToolDefinition(
                    name=item.id,
                    title=item.name,
                    description=item.description,
                    inputSchema=item.input_schema,
                    annotations=McpToolAnnotations(
                        title=item.name,
                        readOnlyHint=item.id in {"validate_creator_graph", "review_playable_world"},
                        destructiveHint=item.destructive,
                        idempotentHint=item.id in {"layout_creator_graph", "validate_creator_graph", "compile_creator_graph", "bind_visual_assets"},
                        openWorldHint=item.long_running,
                    ),
                    meta={
                        "stage": item.stage,
                        "longRunning": item.long_running,
                        "available": item.available,
                        "ownerAgent": item.owner_agent,
                        "capabilityType": item.capability_type,
                    },
                )
                for item in self.registry.list()
            ]
        )

    async def call_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        project: dict[str, Any],
        artifacts: dict[str, Any],
        should_cancel: Callable[[], bool],
        progress: ProgressCallback,
    ) -> McpCallToolResult:
        try:
            call = self.registry.validate_call(CreatorToolCall(tool=name, arguments=arguments))
            updated_project, artifact_delta, detail = await self.executor.execute(
                call,
                project,
                artifacts,
                should_cancel=should_cancel,
                progress=progress,
            )
            return McpCallToolResult(
                content=[McpTextContent(text=detail)],
                structuredContent={
                    "project": updated_project,
                    "artifacts": artifact_delta,
                    "detail": detail,
                },
            )
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            issues = getattr(exc, "issues", None)
            if isinstance(issues, list):
                error["issues"] = issues
            return McpCallToolResult(
                content=[McpTextContent(text=f"{type(exc).__name__}: {exc}")],
                structuredContent={"error": error},
                isError=True,
            )
