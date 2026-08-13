from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
from time import monotonic
from typing import Any
from uuid import uuid4

from app.agents.creator_assistant.compiler import CreatorGraphCompiler
from app.agents.creator_assistant.schema import (
    CreatorAssistantRequest,
    CreatorAssistantResponse,
    CreatorWorkflowEvent,
    CreatorWorkflowPreview,
    CreatorWorkflowRun,
)
from app.agents.creator_assistant.mcp import CreatorMcpToolServer
from app.agents.creator_assistant.store import CreatorWorkflowStore
from app.agents.creator_assistant.tools import CreatorToolExecutor, CreatorToolRegistry


class CreatorWorkflowOrchestrator:
    def __init__(
        self,
        *,
        registry: CreatorToolRegistry,
        executor: CreatorToolExecutor,
        mcp_server: CreatorMcpToolServer | None = None,
        store: CreatorWorkflowStore | None = None,
        heartbeat_seconds: float = 15.0,
    ) -> None:
        self.registry = registry
        self.executor = executor
        self.mcp_server = mcp_server or CreatorMcpToolServer(registry=registry, executor=executor)
        self.compiler = CreatorGraphCompiler()
        self.store = store or CreatorWorkflowStore()
        self.heartbeat_seconds = max(0.01, heartbeat_seconds)
        self.previews: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def preview(self, request: CreatorAssistantRequest, response: CreatorAssistantResponse) -> CreatorWorkflowPreview:
        calls = self.registry.normalize_calls(response.tool_calls)
        project = self.compiler.normalize(request.project)
        report = self.compiler.validate(project)
        if response.operations:
            project, report = self.compiler.apply(project, response.operations)
        executable = bool(response.operations or calls)
        if not executable and response.intent not in {"chat", "clarify", "error"}:
            raise ValueError("creator assistant returned no operations or tool calls")
        preview_id = f"preview_{uuid4().hex}"
        base_hash = self.compiler.hash(request.project)
        payload = {
            "request": request.model_dump(mode="json"),
            "response": response.model_copy(update={"tool_calls": calls}).model_dump(mode="json"),
            "base_hash": base_hash,
            "preview_project": project,
            "report": report.model_dump(mode="json"),
        }
        self.previews[preview_id] = payload
        return CreatorWorkflowPreview(
            **payload["response"],
            preview_id=preview_id,
            change_id=preview_id,
            base_hash=base_hash,
            preview_project=project,
            report=report,
            executable=executable,
        )

    def start(self, preview_id: str, project: dict[str, Any]) -> CreatorWorkflowRun:
        preview = self.previews.get(preview_id)
        if preview is None:
            raise FileNotFoundError(f"creator workflow preview not found: {preview_id}")
        response = CreatorAssistantResponse.model_validate(preview["response"])
        if not response.operations and not response.tool_calls:
            raise RuntimeError("creator conversation response has no executable workflow")
        if self.compiler.hash(project) != preview["base_hash"]:
            raise RuntimeError("creator project changed after preview; regenerate the workflow preview")
        run_id = f"run_{uuid4().hex}"
        now = _now()
        run = {
            "run_id": run_id,
            "preview_id": preview_id,
            "world_id": str(project.get("world", {}).get("world_id") or ""),
            "request_summary": str(preview.get("request", {}).get("message") or "")[:500],
            "status": "queued",
            "current_tool": "",
            "project": copy.deepcopy(project),
            "report": self.compiler.validate(project).model_dump(mode="json"),
            "artifacts": {},
            "events": [],
            "error": None,
            "cancel_requested": False,
            "acknowledged_at": "",
            "created_at": now,
            "updated_at": now,
        }
        self.runs[run_id] = run
        self._event(run, "queued", "", "工作流已进入队列", "等待 Creator 工具执行器启动。")
        self.tasks[run_id] = asyncio.create_task(self._execute(run_id))
        return CreatorWorkflowRun.model_validate(run)

    def get(self, run_id: str) -> CreatorWorkflowRun:
        run = self.runs.get(run_id)
        if run is None:
            run = self.store.load(run_id).model_dump(mode="json")
            self.runs[run_id] = run
        self._fail_orphaned_run(run)
        return CreatorWorkflowRun.model_validate(run)

    def latest(self, world_id: str) -> CreatorWorkflowRun | None:
        run = self.store.latest(world_id)
        if run is None:
            return None
        payload = self.runs.get(run.run_id) or run.model_dump(mode="json")
        self.runs[run.run_id] = payload
        self._fail_orphaned_run(payload)
        return CreatorWorkflowRun.model_validate(payload)

    def cancel(self, run_id: str) -> CreatorWorkflowRun:
        run = self.runs.get(run_id)
        if run is None:
            raise FileNotFoundError(f"creator workflow run not found: {run_id}")
        if run["status"] in {"done", "error", "cancelled"}:
            return CreatorWorkflowRun.model_validate(run)
        run["cancel_requested"] = True
        run["status"] = "cancelling"
        self._event(run, "cancelling", run.get("current_tool") or "", "正在停止工作流", "当前外部 API 请求可能需要等待服务端返回。")
        task = self.tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        return CreatorWorkflowRun.model_validate(run)

    def acknowledge(self, run_id: str) -> CreatorWorkflowRun:
        run = self.get(run_id).model_dump(mode="json")
        if run["status"] == "done" and not run.get("acknowledged_at"):
            now = _now()
            run["acknowledged_at"] = now
            run["updated_at"] = now
            self.runs[run_id] = run
            self.store.save(run)
        return CreatorWorkflowRun.model_validate(run)

    async def _execute(self, run_id: str) -> None:
        run = self.runs[run_id]
        preview = self.previews[run["preview_id"]]
        response = CreatorAssistantResponse.model_validate(preview["response"])
        try:
            run["status"] = "running"
            self._event(run, "running", "creator_operations", "应用 Creator 图操作", f"共 {len(response.operations)} 项。")
            if response.operations:
                project, report = self.compiler.apply(run["project"], response.operations)
                run["project"] = project
                run["report"] = report.model_dump(mode="json")
            for call in response.tool_calls:
                if run["cancel_requested"]:
                    raise InterruptedError("creator workflow cancelled")
                run["current_tool"] = call.tool
                definition = next(item for item in self.registry.list() if item.id == call.tool)
                self._event(run, "running", call.tool, f"正在执行：{definition.name}", call.reason or definition.description)

                async def progress(title: str, detail: str) -> None:
                    self._event(run, "running", call.tool, title, detail)

                heartbeat = asyncio.create_task(self._heartbeat(run, call.tool, definition.name))
                try:
                    result = await self.mcp_server.call_tool(
                        name=call.tool,
                        arguments=call.arguments,
                        project=run["project"],
                        artifacts=run["artifacts"],
                        should_cancel=lambda: bool(run["cancel_requested"]),
                        progress=progress,
                    )
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                if run["cancel_requested"]:
                    raise InterruptedError("creator workflow cancelled")
                if result.isError:
                    error = result.structuredContent.get("error") or {}
                    message = str(error.get("message") or "")
                    if not message and result.content:
                        message = result.content[0].text
                    raise RuntimeError(message or "MCP tool call failed")
                project = result.structuredContent["project"]
                artifact_delta = result.structuredContent.get("artifacts") or {}
                detail = str(result.structuredContent.get("detail") or "")
                run["project"] = project
                run["artifacts"].update(artifact_delta)
                run["report"] = self.compiler.validate(project).model_dump(mode="json")
                self._event(run, "done", call.tool, f"已完成：{definition.name}", detail)
            run["current_tool"] = ""
            run["status"] = "done"
            self._event(run, "done", "", "Creator 工作流已完成", _completion_detail(run["artifacts"], len(response.tool_calls)))
        except (InterruptedError, asyncio.CancelledError):
            run["status"] = "cancelled"
            run["current_tool"] = ""
            self._event(run, "cancelled", "", "Creator 工作流已停止", "已完成的阶段结果仍保留在本次运行记录中。")
        except Exception as exc:
            run["status"] = "error"
            run["error"] = {"type": type(exc).__name__, "message": str(exc)}
            self._event(run, "error", run.get("current_tool") or "", "Creator 工作流失败", f"{type(exc).__name__}: {exc}")
        finally:
            run["updated_at"] = _now()
            self.store.save(run)
            self.tasks.pop(run_id, None)

    def _event(self, run: dict[str, Any], status: str, tool: str, title: str, detail: str) -> None:
        now = _now()
        run["updated_at"] = now
        run["events"].append(
            CreatorWorkflowEvent(status=status, tool=tool, title=title, detail=detail, at=now).model_dump(mode="json")
        )
        self.store.save(run)

    async def _heartbeat(self, run: dict[str, Any], tool: str, tool_name: str) -> None:
        started_at = monotonic()
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if run.get("status") != "running" or run.get("current_tool") != tool:
                return
            elapsed = int(monotonic() - started_at)
            self._event(
                run,
                "running",
                tool,
                f"{tool_name}仍在执行",
                f"已等待 {elapsed} 秒，外部模型或 API 仍在处理；无需重复点击。",
            )

    def _fail_orphaned_run(self, run: dict[str, Any]) -> None:
        if run.get("status") not in {"queued", "running", "cancelling"}:
            return
        task = self.tasks.get(str(run.get("run_id") or ""))
        if task is not None and not task.done():
            return
        run["status"] = "error"
        run["current_tool"] = ""
        run["error"] = {
            "type": "WorkflowInterrupted",
            "message": "服务重启或页面会话中断了这次工作流，请从 Creator 重新执行。",
        }
        self._event(
            run,
            "error",
            "",
            "Creator 工作流已中断",
            "服务重启前未完成；已保留此前阶段产物，请重新生成执行预览。",
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _completion_detail(artifacts: dict[str, Any], tool_count: int) -> str:
    details: list[str] = []
    if artifacts.get("story_authoring"):
        details.append("完整剧情已生成并回写画布")
    visual_result = artifacts.get("visual_result")
    visual_plan = artifacts.get("visual_plan")
    if isinstance(visual_result, dict):
        generated = visual_result.get("generated") if isinstance(visual_result.get("generated"), list) else []
        failed = visual_result.get("failed") if isinstance(visual_result.get("failed"), list) else []
        details.append(f"已生成 {len(generated)} 张图片，失败 {len(failed)} 张")
    elif isinstance(visual_plan, dict):
        assets = visual_plan.get("assets") if isinstance(visual_plan.get("assets"), list) else []
        details.append(f"仅完成 {len(assets)} 项视觉方案，尚未生成图片")
    bindings = artifacts.get("visual_bindings")
    if isinstance(bindings, dict):
        details.append(f"已绑定 {int(bindings.get('characters') or 0)} 张角色立绘、{int(bindings.get('scenes') or 0)} 张场景背景")
    saved = artifacts.get("saved_world")
    if isinstance(saved, dict):
        details.append(f"《{saved.get('name') or saved.get('world_id')}》已保存为草稿")
    published = artifacts.get("published_world")
    if isinstance(published, dict):
        details.append(f"《{published.get('name') or published.get('world_id')}》已发布到玩家端")
    return "；".join(details) + "。" if details else f"已执行 {tool_count} 个工具并回写 Creator。"
