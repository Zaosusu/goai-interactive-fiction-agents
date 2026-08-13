from __future__ import annotations

import asyncio
import traceback

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.agents.script_decomposition import (
    ScriptGraphBuildRequest,
    ScriptGraphBuildResponse,
    build_script_world_async,
    decompose_script_async_with_progress,
)
from app.agents.world_builder import generate_world_config_with_ai
from app.api import shared
from app.api.dependencies import reset_agent
from app.worlds.sandbox.importer import import_world_from_document
from app.worlds.sandbox.models import (
    SandboxWorldConfig,
    ScriptDecompositionBuildResponse,
    ScriptDecompositionRequest,
    VisualAssetGenerationResult,
    VisualAssetPlan,
    VisualAssetRequest,
    WorldGenerateRequest,
)

router = APIRouter(tags=["pipeline"])


@router.post("/worlds/generate", response_model=SandboxWorldConfig)
async def generate_world(request: WorldGenerateRequest) -> SandboxWorldConfig:
    request = request.model_copy(update={"world_builder_llm": request.world_builder_llm or shared.pipeline_llm_config("world_builder")})
    config = await generate_world_config_with_ai(request)
    try:
        saved = shared.world_store.save(config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reset_agent(saved.world_id)
    return saved


@router.post("/worlds/script-decomposition", response_model=ScriptDecompositionBuildResponse)
async def create_script_decomposition_world(request: ScriptDecompositionRequest) -> ScriptDecompositionBuildResponse:
    try:
        request = request.model_copy(update={"decomposition_llm": request.decomposition_llm or shared.pipeline_llm_config("script_decomposition")})
        result = await decompose_script_async_with_progress(request)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": str(exc),
                "type": type(exc).__name__,
                "trace": traceback.format_exc(limit=6),
            },
        ) from exc
    result.artifact = shared.decomposition_store.save(result, request.title)
    return result


@router.post("/worlds/script-decomposition/compile", response_model=ScriptDecompositionBuildResponse)
async def compile_script_decomposition_world(request: ScriptDecompositionRequest) -> ScriptDecompositionBuildResponse:
    try:
        request = request.model_copy(update={"decomposition_llm": request.decomposition_llm or shared.pipeline_llm_config("script_decomposition")})
        result = await build_script_world_async(request)
        if result.world is None:
            raise ValueError("script decomposition compiler returned no world")
        result.world = shared.world_store.save(result.world)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": str(exc),
                "type": type(exc).__name__,
                "trace": traceback.format_exc(limit=6),
            },
        ) from exc
    reset_agent(result.world.world_id)
    return result


@router.get("/worlds/script-decompositions")
async def list_script_decomposition_artifacts() -> list[dict]:
    return shared.decomposition_store.list()


@router.get("/worlds/script-decompositions/{artifact_id}")
async def get_script_decomposition_artifact(artifact_id: str) -> dict:
    try:
        return shared.decomposition_store.load(artifact_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/worlds/script-graph/compile", response_model=ScriptGraphBuildResponse)
async def compile_script_graph(request: ScriptGraphBuildRequest) -> ScriptGraphBuildResponse:
    try:
        decomposition = request.decomposition
        source_artifact_id = request.artifact_id
        if decomposition is None and request.artifact_id:
            loaded = shared.decomposition_store.load(request.artifact_id)
            decomposition = loaded.get("decomposition")
        if decomposition is None:
            raise ValueError("script graph compile requires decomposition or artifact_id")
        graph = shared.script_graph_compiler.compile(decomposition, source_artifact_id=source_artifact_id)
        artifact = shared.script_graph_store.save(graph, request.title or graph.title) if request.save else None
        return ScriptGraphBuildResponse(graph=graph, artifact=artifact)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/worlds/script-graphs")
async def list_script_graph_artifacts() -> list[dict]:
    return shared.script_graph_store.list()


@router.get("/worlds/script-graphs/{artifact_id}")
async def get_script_graph_artifact(artifact_id: str) -> dict:
    try:
        return shared.script_graph_store.load(artifact_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/worlds/visual-assets/plan", response_model=VisualAssetPlan)
async def plan_visual_assets(request: VisualAssetRequest) -> VisualAssetPlan:
    request = shared.with_pipeline_visual_config(request)
    request = request.model_copy(update={"plan": None, "style_guide": {}})
    plan = await shared.visual_asset_agent.plan_async(request)
    shared.visual_asset_store.save_plan(plan)
    return plan


@router.get("/worlds/visual-assets")
async def list_visual_asset_artifacts() -> list[dict]:
    return shared.visual_asset_store.list()


@router.get("/worlds/visual-assets/runs")
async def list_visual_asset_runs(world_id: str = "", title: str = "", output_root: str = "output/visual_assets") -> list[dict]:
    try:
        return shared.visual_asset_store.list_runs(world_id=world_id, title=title, output_root=output_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/worlds/visual-assets/runs/{run_id}")
async def get_visual_asset_run(run_id: str, world_id: str = "", title: str = "", output_root: str = "output/visual_assets") -> dict:
    try:
        return shared.visual_asset_store.load_run(run_id=run_id, world_id=world_id, title=title, output_root=output_root)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/worlds/visual-assets/runs/{run_id}")
async def delete_visual_asset_run(run_id: str, world_id: str = "", title: str = "", output_root: str = "output/visual_assets") -> dict:
    try:
        return shared.visual_asset_store.delete_run(run_id=run_id, world_id=world_id, title=title, output_root=output_root)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/worlds/visual-assets/{artifact_id}")
async def get_visual_asset_artifact(artifact_id: str) -> dict:
    try:
        return shared.visual_asset_store.load(artifact_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/worlds/visual-assets/{artifact_id}/remove-character-backgrounds", response_model=VisualAssetGenerationResult)
async def remove_character_backgrounds(artifact_id: str, model: str = "auto") -> VisualAssetGenerationResult:
    """Apply the production character-cutout stage to a saved visual artifact."""
    try:
        loaded = shared.visual_asset_store.load(artifact_id)
        if not isinstance(loaded.get("result"), dict):
            raise ValueError("visual asset artifact has no generated result")
        result = VisualAssetGenerationResult.model_validate(loaded["result"])
        processed = shared.visual_asset_agent.remove_character_backgrounds(result, model=model)
        shared.visual_asset_store.save_result(processed)
        return processed
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/worlds/visual-assets/plans")
async def save_visual_asset_plan(plan: VisualAssetPlan) -> dict:
    return shared.visual_asset_store.save_plan(plan)


@router.post("/worlds/visual-assets/generate/jobs")
async def start_visual_asset_generation_job(request: VisualAssetRequest) -> dict[str, str]:
    request = shared.with_pipeline_visual_config(request)
    job_id, job = shared.new_job(cancelable=True)
    shared.visual_asset_generation_jobs[job_id] = job
    task = asyncio.create_task(_run_visual_asset_generation_job(job_id, request))
    shared.visual_asset_generation_tasks[job_id] = task
    shared.job_event(job, "queued", "VisualAssetGenerationAgent", "Queued image generation job.")
    return {"job_id": job_id, "status": "queued"}


@router.get("/worlds/visual-assets/generate/jobs/{job_id}")
async def get_visual_asset_generation_job(job_id: str) -> dict:
    job = shared.visual_asset_generation_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="visual asset generation job not found")
    return job


@router.post("/worlds/visual-assets/generate/jobs/{job_id}/cancel")
async def cancel_visual_asset_generation_job(job_id: str) -> dict:
    job = shared.visual_asset_generation_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="visual asset generation job not found")
    if job.get("status") in {"done", "error", "cancelled"}:
        return job
    job["cancel_requested"] = True
    job["status"] = "cancelling"
    job["updated_at"] = shared.now_iso()
    task = shared.visual_asset_generation_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
    shared.job_event(job, "cancelling", "VisualAssetGenerationAgent", "Stop requested. Cancelling the active model call; any already-started provider request may still finish remotely.")
    return job


@router.post("/worlds/visual-assets/generate", response_model=VisualAssetGenerationResult)
async def generate_visual_assets(request: VisualAssetRequest) -> VisualAssetGenerationResult:
    request = shared.with_pipeline_visual_config(request)
    result = await shared.visual_asset_agent.generate_async(request)
    shared.visual_asset_store.save_result(result)
    return result


async def _run_visual_asset_generation_job(job_id: str, request: VisualAssetRequest) -> None:
    job = shared.visual_asset_generation_jobs[job_id]
    job["status"] = "running"
    job["updated_at"] = shared.now_iso()
    shared.job_event(job, "running", "VisualAssetGenerationAgent", "Generating images. Stop will take effect between image requests.")
    try:
        async def progress(status: str, title: str, detail: str) -> None:
            if job.get("cancel_requested") and status == "running":
                shared.job_event(job, "cancelling", title, detail)
                return
            shared.job_event(job, status, title, detail)

        result = await shared.visual_asset_agent.generate_async(
            request,
            should_cancel=lambda: bool(job.get("cancel_requested")),
            progress_callback=progress,
        )
        shared.visual_asset_store.save_result(result)
        job["result"] = result.model_dump()
        if result.metadata.get("cancelled") or job.get("cancel_requested"):
            job["status"] = "cancelled"
            shared.job_event(
                job,
                "cancelled",
                "VisualAssetGenerationAgent",
                f"Stopped after {len(result.generated)} generated image(s); {len(result.failed)} failed.",
            )
        else:
            job["status"] = "done"
            shared.job_event(
                job,
                "done",
                "VisualAssetGenerationAgent",
                f"Generated {len(result.generated)} image(s); {len(result.failed)} failed.",
            )
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        job["error"] = None
        job["result"] = shared.cancelled_visual_asset_result(request).model_dump()
        shared.job_event(job, "cancelled", "VisualAssetGenerationAgent", "Image generation job cancelled by user.")
    except Exception as exc:
        job["status"] = "error"
        job["error"] = {"type": type(exc).__name__, "message": str(exc), "trace": traceback.format_exc(limit=6)}
        shared.job_event(job, "error", "VisualAssetGenerationAgent", f"{type(exc).__name__}: {exc}")
    finally:
        job["updated_at"] = shared.now_iso()
        shared.visual_asset_generation_tasks.pop(job_id, None)


@router.post("/worlds/import", response_model=SandboxWorldConfig)
async def import_world(
    file: UploadFile = File(...),
    player_name: str = Form("主角"),
    world_name: str = Form(""),
    use_ai: bool = Form(True),
) -> SandboxWorldConfig:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    try:
        config = await import_world_from_document(file.filename or "imported_document", content, player_name, world_name, use_ai)
        saved = shared.world_store.save(config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reset_agent(saved.world_id)
    return saved


@router.post("/worlds/script-decomposition/import", response_model=ScriptDecompositionBuildResponse)
async def import_script_decomposition(
    files: list[UploadFile] = File(...),
    player_name: str = Form("主角"),
    world_name: str = Form(""),
    decomposition_mode: str = Form("llm"),
    decomposition_llm: str = Form(""),
) -> ScriptDecompositionBuildResponse:
    if not files:
        raise HTTPException(status_code=400, detail="Please upload at least one file.")
    try:
        text = await shared.extract_uploaded_documents_text(files)
        llm_config = None
        if decomposition_llm:
            try:
                llm_config = shared.parse_optional_llm_config(decomposition_llm)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"decomposition_llm is not valid JSON: {exc}") from exc
        llm_config = llm_config or shared.pipeline_llm_config("script_decomposition")
        result = await decompose_script_async_with_progress(
            ScriptDecompositionRequest(
                title=world_name,
                player_name=player_name,
                source_text=text,
                decomposition_mode=decomposition_mode,
                decomposition_llm=llm_config,
            )
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": str(exc),
                "type": type(exc).__name__,
                "trace": traceback.format_exc(limit=6),
            },
        ) from exc
    result.artifact = shared.decomposition_store.save(result, world_name)
    return result


@router.post("/worlds/script-decomposition/import/jobs")
async def start_script_decomposition_import_job(
    files: list[UploadFile] = File(...),
    player_name: str = Form("player"),
    world_name: str = Form(""),
    decomposition_mode: str = Form("llm"),
    decomposition_llm: str = Form(""),
) -> dict[str, str]:
    if not files:
        raise HTTPException(status_code=400, detail="Please upload at least one file.")
    job_id, job = shared.new_job()
    shared.script_decomposition_jobs[job_id] = job
    uploads = [(file.filename or f"document_{index}", await file.read()) for index, file in enumerate(files, start=1)]
    task = asyncio.create_task(
        _run_script_decomposition_import_job(
            job_id,
            uploads,
            player_name,
            world_name,
            decomposition_mode,
            decomposition_llm,
        )
    )
    shared.script_decomposition_tasks[job_id] = task
    shared.job_event(job, "queued", "ScriptDecompositionJob", f"Queued {len(uploads)} uploaded file(s).")
    return {"job_id": job_id, "status": "queued"}


@router.get("/worlds/script-decomposition/import/jobs/{job_id}")
async def get_script_decomposition_import_job(job_id: str) -> dict:
    job = shared.script_decomposition_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="script decomposition job not found")
    return job


@router.post("/worlds/script-decomposition/import/jobs/{job_id}/cancel")
async def cancel_script_decomposition_import_job(job_id: str) -> dict:
    job = shared.script_decomposition_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="script decomposition job not found")
    if job.get("status") in {"done", "error", "cancelled"}:
        return job
    job["cancel_requested"] = True
    job["status"] = "cancelling"
    shared.job_event(job, "cancelling", "ScriptDecompositionJob", "Cancellation requested by user.")
    task = shared.script_decomposition_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
    return job


async def _run_script_decomposition_import_job(
    job_id: str,
    uploads: list[tuple[str, bytes]],
    player_name: str,
    world_name: str,
    decomposition_mode: str,
    decomposition_llm: str,
) -> None:
    job = shared.script_decomposition_jobs[job_id]
    job["status"] = "running"
    shared.job_event(job, "running", "DocumentImportAgent", "Extracting text from uploaded documents.")
    try:
        shared.raise_if_cancelled(job)
        text = shared.extract_document_parts_text(uploads)
        shared.job_event(job, "running", "DocumentImportAgent", f"Extracted {len(text)} text characters.")
        shared.raise_if_cancelled(job)
        llm_config = None
        if decomposition_llm:
            llm_config = shared.parse_optional_llm_config(decomposition_llm)
            if llm_config is not None:
                shared.job_event(
                    job,
                    "running",
                    "ModelRouter",
                    f"Using model={llm_config.model or 'env'} base_url={llm_config.base_url or 'env'} timeout={llm_config.timeout or 'env'}.",
                )
        llm_config = llm_config or shared.pipeline_llm_config("script_decomposition")

        async def progress(title: str, detail: str) -> None:
            shared.raise_if_cancelled(job)
            shared.job_event(job, "running", title, detail)

        result = await decompose_script_async_with_progress(
            ScriptDecompositionRequest(
                title=world_name,
                player_name=player_name,
                source_text=text,
                decomposition_mode=decomposition_mode,
                decomposition_llm=llm_config,
            ),
            progress_callback=progress,
        )
        result.artifact = shared.decomposition_store.save(result, world_name)
        shared.job_event(
            job,
            "done",
            "ScriptDecompositionStore",
            f"Saved decomposition JSON to {result.artifact.get('decomposition_path')}.",
        )
        shared.job_event(job, "done", "ScriptDecompositionAgent", "Decomposition finished. World generation is a separate stage.")
        job["result"] = result.model_dump()
        job["status"] = "done"
        shared.job_event(job, "done", "ScriptDecompositionJob", "Job completed.")
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        job["error"] = None
        shared.job_event(job, "cancelled", "ScriptDecompositionJob", "Job cancelled by user.")
    except Exception as exc:
        job["status"] = "error"
        job["error"] = {"type": type(exc).__name__, "message": str(exc), "trace": traceback.format_exc(limit=6)}
        shared.job_event(job, "error", "ScriptDecompositionJob", f"{type(exc).__name__}: {exc}")
    finally:
        job["updated_at"] = shared.now_iso()
        shared.script_decomposition_tasks.pop(job_id, None)


visual_asset_generation_jobs = shared.visual_asset_generation_jobs
visual_asset_generation_tasks = shared.visual_asset_generation_tasks
script_decomposition_jobs = shared.script_decomposition_jobs
script_decomposition_tasks = shared.script_decomposition_tasks
