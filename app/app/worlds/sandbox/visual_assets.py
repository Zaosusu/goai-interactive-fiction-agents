from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from app.core.text_generation import OpenAICompatibleTextGenerationClient, TextGenerationClient
from app.core.image_generation import (
    ImageGenerationClient,
    ImageGenerationRequest,
    OpenAICompatibleImageGenerationClient,
    generate_with_retry,
)
from app.agents.script_decomposition.compiler import ScriptGraphCompiler
from app.agents.visual_asset_generation.background_removal import validate_transparent_portrait
from app.worlds.sandbox.models import (
    SandboxNPC,
    SandboxWorldConfig,
    ScriptCharacterSheet,
    ScriptDecompositionResult,
    VisualAssetGenerationResult,
    VisualAssetPlan,
    VisualAssetRequest,
    VisualAssetSpec,
)

VisualAssetProgressCallback = Callable[[str, str, str], None | Awaitable[None]]


def _safe_character_cutout(character_postprocessor, input_path: str, *, model: str) -> tuple[str, dict, str]:
    """Return a validated cutout or preserve the original image on any quality failure."""
    try:
        cutout = character_postprocessor.process(input_path, model=model)
        validation = validate_transparent_portrait(cutout.output_path)
        return cutout.output_path, {**cutout.metadata, **validation, "background_removal_status": "accepted"}, ""
    except Exception as exc:
        warning = f"自动抠图未通过主体保护，已保留原图：{type(exc).__name__}: {exc}"
        return (
            input_path,
            {
                "background_removed": False,
                "background_removal_status": "rejected",
                "background_removal_fallback": "original",
                "background_removal_error": f"{type(exc).__name__}: {exc}"[:500],
                "original_character_preserved": True,
            },
            warning,
        )


def _retry_rejected_character_asset(
    image_client: ImageGenerationClient,
    provider,
    spec: VisualAssetSpec,
    image_response,
    output_path: str,
    postprocess_metadata: dict,
    postprocess_warning: str,
    character_postprocessor,
    *,
    model: str,
    seed: int | None,
) -> tuple[object, str, dict, str]:
    if not postprocess_warning:
        return image_response, output_path, postprocess_metadata, postprocess_warning
    first_error = str(postprocess_metadata.get("background_removal_error") or postprocess_warning)
    try:
        retry_response = generate_with_retry(
            image_client,
            ImageGenerationRequest(
                prompt=_character_isolation_retry_prompt(spec.prompt),
                output_path=_character_isolation_retry_path(image_response.output_path),
                negative_prompt=_join_negative_prompt(spec.negative_prompt, _character_isolation_negative_prompt()),
                size=spec.size,
                seed=seed + 100_000 if seed is not None else None,
                metadata={"asset_id": spec.id, "kind": spec.kind, "display_name": spec.display_name, "isolation_retry": True},
            ),
            provider,
        )
        retry_output, retry_metadata, retry_warning = _safe_character_cutout(
            character_postprocessor,
            retry_response.output_path,
            model=model,
        )
        if retry_warning:
            raise ValueError(retry_warning)
        return (
            retry_response,
            retry_output,
            {
                **retry_metadata,
                "character_isolation_retry_attempted": True,
                "character_isolation_retry_succeeded": True,
                "character_isolation_retry_first_error": first_error[:500],
            },
            "",
        )
    except Exception as exc:
        retry_error = f"{type(exc).__name__}: {exc}"
        return (
            image_response,
            output_path,
            {
                **postprocess_metadata,
                "character_isolation_retry_attempted": True,
                "character_isolation_retry_succeeded": False,
                "character_isolation_retry_error": retry_error[:500],
            },
            f"{postprocess_warning}；纯色隔离背景自动重试仍未通过，继续保留第一次原图：{retry_error}",
        )


_CHARACTER_SCREEN_VARIANTS = (
    ("white", "#FFFFFF"),
    ("red", "#FF0000"),
    ("green", "#00FF00"),
)


def _generate_character_screen_candidates(
    image_client: ImageGenerationClient,
    provider,
    spec: VisualAssetSpec,
    character_postprocessor,
    *,
    model: str,
    seed: int | None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[object, str, dict, str]:
    """Try one rembg cutout first; generate three chroma candidates only as fallback."""
    accepted: list[tuple[float, object, str, dict, str]] = []
    candidate_audit: list[dict] = []
    generated_responses: dict[str, object] = {}
    first_response = None
    first_error = ""
    normalized_model = str(model or "auto").strip().lower()
    rembg_primary = normalized_model not in {"chroma", "contour", "local"}
    rembg_model = "rembg" if normalized_model in {"", "auto"} else normalized_model
    rembg_audit: dict = {
        "status": "not_requested",
        "model": rembg_model if rembg_primary else "",
    }

    # The first generated image doubles as the white-screen fallback candidate.
    # If rembg succeeds, the other two image API calls are never made.
    first_screen_name, first_screen_hex = _CHARACTER_SCREEN_VARIANTS[0]
    try:
        first_variant_path = _character_screen_variant_path(spec.output_path, first_screen_name)
        first_response = generate_with_retry(
            image_client,
            ImageGenerationRequest(
                prompt=_character_screen_variant_prompt(spec.prompt, first_screen_name, first_screen_hex),
                output_path=first_variant_path,
                negative_prompt=_provider_prompt_budget(
                    _join_negative_prompt(spec.negative_prompt, _character_screen_negative_prompt(first_screen_name)),
                    430,
                ),
                size=spec.size,
                seed=seed,
                metadata={
                    "asset_id": spec.id,
                    "kind": spec.kind,
                    "display_name": spec.display_name,
                    "screen_variant": first_screen_name,
                    "screen_hex": first_screen_hex,
                    "screen_candidate_index": 0,
                    "rembg_primary_source": rembg_primary,
                },
            ),
            provider,
        )
        generated_responses[first_screen_name] = first_response
    except Exception as exc:
        first_error = f"{type(exc).__name__}: {exc}"[:500]
        rembg_audit = {
            **rembg_audit,
            "status": "source_generation_failed" if rembg_primary else "not_requested",
            "error": first_error,
        }

    if rembg_primary and first_response is not None:
        rembg_output, rembg_metadata, rembg_warning = _safe_character_cutout(
            character_postprocessor,
            first_response.output_path,
            model=rembg_model,
        )
        rembg_score = float(rembg_metadata.get("background_removal_quality_score") or 0.0)
        rembg_audit = {
            "status": "accepted" if not rembg_warning else "rejected",
            "model": rembg_metadata.get("background_removal_model") or rembg_model,
            "source_path": first_response.output_path,
            "output_path": rembg_output,
            "score": round(rembg_score, 6),
            "transparent_pixel_ratio": rembg_metadata.get("transparent_pixel_ratio"),
            "face_alpha_coverage": rembg_metadata.get("face_alpha_coverage"),
            "error": str(rembg_metadata.get("background_removal_error") or rembg_warning)[:500],
        }
        if not rembg_warning:
            return (
                first_response,
                rembg_output,
                {
                    **rembg_metadata,
                    "character_background_removal_strategy": "rembg_primary",
                    "character_rembg_attempted": True,
                    "character_rembg_status": "accepted",
                    "character_rembg_candidate": rembg_audit,
                    "character_screen_selection": "rembg_primary",
                    "character_screen_selected": first_screen_name,
                    "character_screen_selected_score": round(rembg_score, 6),
                    "character_screen_candidate_count": 1,
                    "character_screen_accepted_count": 1,
                    "character_screen_candidates": [],
                },
                "",
            )
        first_error = first_error or rembg_audit["error"]

    fallback_model = "chroma" if rembg_primary else normalized_model
    for index, (screen_name, screen_hex) in enumerate(_CHARACTER_SCREEN_VARIANTS):
        if should_cancel and should_cancel():
            candidate_audit.append({"screen": screen_name, "status": "cancelled"})
            break
        variant_path = _character_screen_variant_path(spec.output_path, screen_name)
        try:
            response = generated_responses.get(screen_name)
            if response is None:
                response = generate_with_retry(
                    image_client,
                    ImageGenerationRequest(
                        prompt=_character_screen_variant_prompt(spec.prompt, screen_name, screen_hex),
                        output_path=variant_path,
                        negative_prompt=_provider_prompt_budget(
                            _join_negative_prompt(spec.negative_prompt, _character_screen_negative_prompt(screen_name)),
                            430,
                        ),
                        size=spec.size,
                        # Keep the same base seed to maximize character identity;
                        # the screen contract is the only intended variation.
                        seed=seed,
                        metadata={
                            "asset_id": spec.id,
                            "kind": spec.kind,
                            "display_name": spec.display_name,
                            "screen_variant": screen_name,
                            "screen_hex": screen_hex,
                            "screen_candidate_index": index,
                        },
                    ),
                    provider,
                )
                generated_responses[screen_name] = response
            if first_response is None:
                first_response = response
            output_path, metadata, warning = _safe_character_cutout(
                character_postprocessor,
                response.output_path,
                model=fallback_model,
            )
            score = float(metadata.get("background_removal_quality_score") or 0.0)
            audit = {
                "screen": screen_name,
                "screen_hex": screen_hex,
                "source_path": response.output_path,
                "output_path": output_path,
                "status": "accepted" if not warning else "rejected",
                "score": round(score, 6),
                "background_removal_tool": metadata.get("background_removal_tool"),
                "chroma_screen_mode": metadata.get("chroma_screen_mode"),
                "chroma_residue_largest_component_ratio": metadata.get("chroma_residue_largest_component_ratio"),
                "white_residue_largest_component_ratio": metadata.get("white_residue_largest_component_ratio"),
                "transparent_pixel_ratio": metadata.get("transparent_pixel_ratio"),
                "face_alpha_coverage": metadata.get("face_alpha_coverage"),
                "error": str(metadata.get("background_removal_error") or warning)[:500],
            }
            candidate_audit.append(audit)
            if warning:
                first_error = first_error or audit["error"]
                continue
            accepted.append((score, response, output_path, metadata, screen_name))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            first_error = first_error or error
            candidate_audit.append(
                {
                    "screen": screen_name,
                    "screen_hex": screen_hex,
                    "source_path": variant_path,
                    "status": "failed",
                    "score": 0.0,
                    "error": error,
                }
            )
    if accepted:
        score, response, output_path, metadata, screen_name = max(accepted, key=lambda item: item[0])
        return (
            response,
            output_path,
            {
                **metadata,
                "character_background_removal_strategy": (
                    "rembg_then_chroma_fallback" if rembg_primary else f"{fallback_model}_screen_candidates"
                ),
                "character_rembg_attempted": rembg_primary,
                "character_rembg_status": rembg_audit.get("status"),
                "character_rembg_candidate": rembg_audit,
                "character_screen_selection": "white_red_green_best_of_three",
                "character_screen_selected": screen_name,
                "character_screen_selected_score": round(score, 6),
                "character_screen_candidate_count": len(candidate_audit),
                "character_screen_accepted_count": len(accepted),
                "character_screen_candidates": candidate_audit,
            },
            "",
        )
    if first_response is None:
        raise RuntimeError(f"all character screen candidates failed before image generation: {first_error}")
    warning = f"rembg 与白/红/绿三路色键结果均未通过质量校验，已保留第一张不透明原图：{first_error}"
    return (
        first_response,
        first_response.output_path,
        {
            "background_removed": False,
            "background_removal_status": "rejected",
            "background_removal_fallback": "original",
            "background_removal_error": first_error[:500],
            "original_character_preserved": True,
            "character_background_removal_strategy": (
                "rembg_then_chroma_fallback" if rembg_primary else f"{fallback_model}_screen_candidates"
            ),
            "character_rembg_attempted": rembg_primary,
            "character_rembg_status": rembg_audit.get("status"),
            "character_rembg_candidate": rembg_audit,
            "character_screen_selection": "white_red_green_best_of_three",
            "character_screen_candidate_count": len(candidate_audit),
            "character_screen_accepted_count": 0,
            "character_screen_candidates": candidate_audit,
        },
        warning,
    )


class VisualAssetGenerationAgent:
    def __init__(
        self,
        image_clients: dict[str, ImageGenerationClient] | None = None,
        prompt_composer: "VisualPromptComposerAgent | None" = None,
        character_postprocessor=None,
    ) -> None:
        self.image_clients = image_clients or {"stepfun": OpenAICompatibleImageGenerationClient("stepfun")}
        self.prompt_composer = prompt_composer or VisualPromptComposerAgent()
        if character_postprocessor is None:
            from app.agents.visual_asset_generation.background_removal import CharacterBackgroundRemovalTool

            character_postprocessor = CharacterBackgroundRemovalTool()
        self.character_postprocessor = character_postprocessor

    def plan(self, request: VisualAssetRequest) -> VisualAssetPlan:
        if _requires_async_prompt_composer(request):
            raise RuntimeError("Visual prompt model composition requires plan_async() or generate_async().")
        return self._plan_with_prompt_bundle(request, self.prompt_composer.compose_prompt_bundle(_resolve_source(request), request))

    async def plan_async(self, request: VisualAssetRequest) -> VisualAssetPlan:
        source = _resolve_source(request)
        bundle = await self.prompt_composer.compose_prompt_bundle_async(source, request)
        return self._plan_with_prompt_bundle(request, bundle)

    def _plan_with_prompt_bundle(self, request: VisualAssetRequest, bundle: dict) -> VisualAssetPlan:
        source = _resolve_source(request)
        world_id = _source_world_id(source)
        title = _source_title(source)
        output_root = Path(request.output_root) / _safe_id(world_id or title, "visual_assets")
        style_guide = dict(bundle.get("style_guide") or {})
        warnings: list[str] = []
        assets: list[VisualAssetSpec] = []
        prompt_specs = {str(item.get("id") or ""): item for item in bundle.get("assets", []) if isinstance(item, dict)}

        if request.include_characters:
            characters = _source_characters(source)
            if request.max_characters is not None:
                characters = characters[: request.max_characters]
            if not characters:
                warnings.append("no_characters_available_for_visual_assets")
            for index, character in enumerate(characters, start=1):
                assets.append(
                    self.prompt_composer.compose_character_spec(
                        character,
                        index,
                        output_root,
                        request,
                        style_guide,
                        prompt_specs.get(f"character:{getattr(character, 'id', '')}") or prompt_specs.get(f"character:{character.name}"),
                    )
                )

        if request.include_scenes:
            scenes = _source_scene_records(source, request.script_graph)
            if request.max_scenes is not None:
                scenes = scenes[: request.max_scenes]
            if not scenes:
                warnings.append("no_scenes_available_for_visual_assets")
            for index, scene in enumerate(scenes, start=1):
                scene_name = str(scene.get("name") or "")
                assets.append(
                    self.prompt_composer.compose_scene_spec(
                        scene,
                        index,
                        output_root,
                        request,
                        style_guide,
                        prompt_specs.get(f"scene:{scene_name}"),
                    )
                )

        return VisualAssetPlan(
            plan_id=f"{world_id or _safe_id(title, 'visual_assets')}_visual_assets",
            world_id=world_id,
            title=title,
            provider=request.provider,
            assets=assets,
            warnings=list(dict.fromkeys(warnings + [warning for asset in assets for warning in asset.warnings])),
            metadata={
                "generated_by": "visual_asset_generation_agent",
                "prompt_composed_by": self.prompt_composer.name,
                "source_type": type(source).__name__,
                "asset_count": len(assets),
                "style_guide": style_guide,
                "upstream_context": {
                    "source_json": _source_script_json(source, request.script_graph),
                    "story_graph_context": style_guide.get("graph_visual_context", ""),
                    "source_contract": "script_graph -> visual_plan -> image_generation -> character_background_removal -> artifact",
                },
                "postprocessing": {
                    "character_background_removal": request.auto_remove_character_background,
                    "background_removal_model": request.background_removal_model,
                    "alpha_validation_required": True,
                },
            },
        )

    def generate(self, request: VisualAssetRequest, should_cancel: Callable[[], bool] | None = None) -> VisualAssetGenerationResult:
        plan = _request_plan(request)
        if plan is None:
            if _requires_async_prompt_composer(request):
                raise RuntimeError("Visual prompt model composition requires generate_async().")
            plan = self.plan(request)
        return self._generate_from_plan(request, plan, should_cancel=should_cancel)

    async def generate_async(
        self,
        request: VisualAssetRequest,
        should_cancel: Callable[[], bool] | None = None,
        progress_callback: VisualAssetProgressCallback | None = None,
    ) -> VisualAssetGenerationResult:
        plan = _request_plan(request) or await self.plan_async(request)
        await _emit_progress(
            progress_callback,
            "running",
            "VisualAssetGenerationAgent",
            _plan_flow_detail(plan),
        )
        if request.prompt_model is not None:
            plan = await self.prompt_composer.finalize_plan_for_image_generation_async(plan, request, progress_callback=progress_callback)
        else:
            await _emit_progress(
                progress_callback,
                "running",
                "VisualAssetGenerationAgent",
                "No prompt_model configured; using the saved visual plan prompts with deterministic generation guardrails.",
            )
        return await self._generate_from_plan_async(request, plan, should_cancel=should_cancel, progress_callback=progress_callback)

    def _generate_from_plan(
        self,
        request: VisualAssetRequest,
        plan: VisualAssetPlan,
        should_cancel: Callable[[], bool] | None = None,
    ) -> VisualAssetGenerationResult:
        plan = _plan_with_generation_context(plan, request)
        plan = _plan_with_generation_run(plan)
        plan = _plan_with_safe_output_paths(plan.model_copy(update={"provider": request.provider}), request.output_root)
        generated: list[VisualAssetSpec] = []
        failed: list[VisualAssetSpec] = []
        cancelled = False

        image_client = self.image_clients.get(plan.provider.provider)
        if image_client is None:
            raise ValueError(f"Unknown visual asset provider: {plan.provider.provider}")
        for index, spec in enumerate(plan.assets, start=1):
            if should_cancel and should_cancel():
                cancelled = True
                break
            spec = spec.model_copy(
                update={
                    "provider": plan.provider.provider,
                    "model": plan.provider.model,
                    "size": plan.provider.size or spec.size,
                }
            )
            try:
                if _should_remove_character_background(spec, request, plan.provider.provider):
                    image_response, output_path, postprocess_metadata, postprocess_warning = _generate_character_screen_candidates(
                        image_client,
                        plan.provider,
                        spec,
                        self.character_postprocessor,
                        model=request.background_removal_model,
                        seed=_seed_for_asset(plan.provider.seed, index),
                        should_cancel=should_cancel,
                    )
                else:
                    image_response = generate_with_retry(
                        image_client,
                        ImageGenerationRequest(
                            prompt=spec.prompt,
                            output_path=spec.output_path,
                            negative_prompt=spec.negative_prompt,
                            size=spec.size,
                            seed=_seed_for_asset(plan.provider.seed, index),
                            metadata={"asset_id": spec.id, "kind": spec.kind, "display_name": spec.display_name},
                        ),
                        plan.provider,
                    )
                    postprocess_metadata = {}
                    postprocess_warning = ""
                    output_path = image_response.output_path
                generated_spec = spec.model_copy(
                    update={
                        "status": image_response.status,
                        "output_path": output_path,
                        "metadata": {**spec.metadata, **image_response.metadata, **postprocess_metadata},
                        "warnings": [*spec.warnings, *([postprocess_warning] if postprocess_warning else [])],
                    }
                )
                generated.append(generated_spec)
                if should_cancel and should_cancel():
                    cancelled = True
                    break
            except Exception as exc:
                failed.append(spec.model_copy(update={"status": "failed", "warnings": [*spec.warnings, str(exc)]}))

        metadata = {
            "generation_run_id": plan.metadata.get("generation_run_id") if isinstance(plan.metadata, dict) else "",
            "status": "cancelled" if cancelled else "done",
            "cancelled": cancelled,
            "generated_count": len(generated),
            "failed_count": len(failed),
            "planned_count": len(plan.assets),
            "background_removed_count": sum(1 for item in generated if item.metadata.get("background_removed")),
            "background_removal_rejected_count": sum(
                1 for item in generated if item.metadata.get("background_removal_status") == "rejected"
            ),
        }
        if cancelled:
            plan = plan.model_copy(update={"metadata": {**(plan.metadata or {}), "generation_status": "cancelled"}})

        return VisualAssetGenerationResult(
            plan=plan,
            generated=generated,
            failed=failed,
            metadata=metadata,
        )

    async def _generate_from_plan_async(
        self,
        request: VisualAssetRequest,
        plan: VisualAssetPlan,
        should_cancel: Callable[[], bool] | None = None,
        progress_callback: VisualAssetProgressCallback | None = None,
    ) -> VisualAssetGenerationResult:
        plan = _plan_with_generation_context(plan, request)
        plan = _plan_with_generation_run(plan)
        plan = _plan_with_safe_output_paths(plan.model_copy(update={"provider": request.provider}), request.output_root)
        generated: list[VisualAssetSpec] = []
        failed: list[VisualAssetSpec] = []
        cancelled = False

        image_client = self.image_clients.get(plan.provider.provider)
        if image_client is None:
            raise ValueError(f"Unknown visual asset provider: {plan.provider.provider}")

        await _emit_progress(
            progress_callback,
            "running",
            "VisualAssetGenerationAgent",
            _run_flow_detail(plan),
        )
        total = len(plan.assets)
        character_total = sum(1 for asset in plan.assets if asset.kind == "character")
        character_index = 0
        for index, spec in enumerate(plan.assets, start=1):
            if should_cancel and should_cancel():
                cancelled = True
                await _emit_progress(
                    progress_callback,
                    "cancelled",
                    "VisualAssetGenerationAgent",
                    f"已在资产 {index}/{total} 处理前收到停止请求，剩余资产不再生成。",
                )
                break
            spec = spec.model_copy(
                update={
                    "provider": plan.provider.provider,
                    "model": plan.provider.model,
                    "size": plan.provider.size or spec.size,
                }
            )
            await _emit_progress(
                progress_callback,
                "running",
                "ImageGenerationProvider",
                _asset_start_detail(spec, index, total, _seed_for_asset(plan.provider.seed, index)),
            )
            try:
                if _should_remove_character_background(spec, request, plan.provider.provider):
                    character_index += 1
                    await _emit_progress(
                        progress_callback,
                        "running",
                        "CharacterBackgroundRemovalTool",
                        f"正在自动抠图 {character_index}/{character_total}：{spec.display_name or spec.id}。",
                    )
                    image_response, output_path, postprocess_metadata, postprocess_warning = await asyncio.to_thread(
                        _generate_character_screen_candidates,
                        image_client,
                        plan.provider,
                        spec,
                        self.character_postprocessor,
                        model=request.background_removal_model,
                        seed=_seed_for_asset(plan.provider.seed, index),
                        should_cancel=should_cancel,
                    )
                    await _emit_progress(
                        progress_callback,
                        "running",
                        "CharacterBackgroundRemovalTool",
                        (
                            f"抠图未通过主体保护，已保留角色原图：{postprocess_warning}。"
                            if postprocess_warning
                            else f"透明 PNG 已通过质量校验：{output_path}。"
                        ),
                    )
                else:
                    image_response = generate_with_retry(
                        image_client,
                        ImageGenerationRequest(
                            prompt=spec.prompt,
                            output_path=spec.output_path,
                            negative_prompt=spec.negative_prompt,
                            size=spec.size,
                            seed=_seed_for_asset(plan.provider.seed, index),
                            metadata={"asset_id": spec.id, "kind": spec.kind, "display_name": spec.display_name},
                        ),
                        plan.provider,
                    )
                    postprocess_metadata = {}
                    postprocess_warning = ""
                    output_path = image_response.output_path
                generated_spec = spec.model_copy(
                    update={
                        "status": image_response.status,
                        "output_path": output_path,
                        "metadata": {**spec.metadata, **image_response.metadata, **postprocess_metadata},
                        "warnings": [*spec.warnings, *([postprocess_warning] if postprocess_warning else [])],
                    }
                )
                generated.append(generated_spec)
                await _emit_progress(
                    progress_callback,
                    "running",
                    "ImageGenerationProvider",
                    f"资产 {index}/{total} 已完成：{spec.kind}「{spec.display_name or spec.id}」→ {output_path}。",
                )
                if should_cancel and should_cancel():
                    cancelled = True
                    await _emit_progress(
                        progress_callback,
                        "cancelled",
                        "VisualAssetGenerationAgent",
                        f"资产 {index}/{total} 完成后收到停止请求，剩余资产不再生成。",
                    )
                    break
            except Exception as exc:
                failed.append(spec.model_copy(update={"status": "failed", "warnings": [*spec.warnings, str(exc)]}))
                await _emit_progress(
                    progress_callback,
                    "error",
                    "ImageGenerationProvider",
                    f"资产 {index}/{total} 生成失败：{spec.kind}「{spec.display_name or spec.id}」。{type(exc).__name__}: {exc}",
                )

        metadata = {
            "generation_run_id": plan.metadata.get("generation_run_id") if isinstance(plan.metadata, dict) else "",
            "status": "cancelled" if cancelled else "done",
            "cancelled": cancelled,
            "generated_count": len(generated),
            "failed_count": len(failed),
            "planned_count": len(plan.assets),
            "background_removed_count": sum(1 for item in generated if item.metadata.get("background_removed")),
            "background_removal_rejected_count": sum(
                1 for item in generated if item.metadata.get("background_removal_status") == "rejected"
            ),
        }
        if cancelled:
            plan = plan.model_copy(update={"metadata": {**(plan.metadata or {}), "generation_status": "cancelled"}})

        await _emit_progress(
            progress_callback,
            "cancelled" if cancelled else "done",
            "VisualAssetGenerationAgent",
            f"视觉资产生成完成：成功 {len(generated)}/{len(plan.assets)}，失败 {len(failed)}，运行 ID={metadata['generation_run_id']}。",
        )
        return VisualAssetGenerationResult(
            plan=plan,
            generated=generated,
            failed=failed,
            metadata=metadata,
        )

    def attach_plan_to_world(self, world: SandboxWorldConfig, plan: VisualAssetPlan) -> SandboxWorldConfig:
        metadata = dict(world.metadata or {})
        metadata["visual_assets"] = plan.model_dump()
        return world.model_copy(update={"metadata": metadata})

    def remove_character_backgrounds(
        self,
        result: VisualAssetGenerationResult,
        *,
        model: str = "auto",
    ) -> VisualAssetGenerationResult:
        """Re-run the same deterministic postprocessing stage for an existing artifact."""
        generated: list[VisualAssetSpec] = []
        errors: list[str] = []
        processed_count = 0
        for spec in result.generated:
            if spec.kind != "character":
                generated.append(spec)
                continue
            try:
                cutout = self.character_postprocessor.process(spec.output_path, model=model)
                validation = validate_transparent_portrait(cutout.output_path)
                generated.append(
                    spec.model_copy(
                        update={
                            "output_path": cutout.output_path,
                            "metadata": {**spec.metadata, **cutout.metadata, **validation},
                        }
                    )
                )
                processed_count += 1
            except Exception as exc:
                errors.append(f"{spec.display_name or spec.id}: {type(exc).__name__}: {exc}")
        if errors:
            raise RuntimeError("character background removal failed; artifact was not changed: " + "; ".join(errors))
        return result.model_copy(
            update={
                "generated": generated,
                "failed": result.failed,
                "metadata": {
                    **result.metadata,
                    "generated_count": len(generated),
                    "failed_count": len(result.failed),
                    "background_removed_count": sum(1 for item in generated if item.metadata.get("background_removed")),
                    "background_reprocessed_count": processed_count,
                },
            }
        )


def _should_remove_character_background(spec: VisualAssetSpec, request: VisualAssetRequest, provider: str) -> bool:
    # The built-in fake provider used by unit tests writes sentinel bytes instead of images.
    return spec.kind == "character" and request.auto_remove_character_background and provider != "fake"


class VisualPromptComposerAgent:
    name = "visual_prompt_composer_agent"

    def __init__(self, text_client: TextGenerationClient | None = None) -> None:
        self.text_client = text_client

    async def compose_prompt_bundle_async(
        self,
        source: SandboxWorldConfig | ScriptDecompositionResult,
        request: VisualAssetRequest,
    ) -> dict:
        if request.prompt_model is None and request.prompt_composer.lower() not in {"llm", "model"}:
            return self.compose_prompt_bundle(source, request)
        client = self.text_client or OpenAICompatibleTextGenerationClient(request.prompt_model, purpose="visual_prompt")
        script_json = _source_script_json(source, request.script_graph)
        asset_index = _asset_index_json(source, request)
        system_prompt = (
            "You are VisualPromptComposerAgent. Read the structured script graph as the source of truth and produce a "
            "coherent game visual asset batch. First derive one global visual bible from the whole graph, then write asset "
            "prompts that obey that same bible. Return only JSON. Do not invent facts not present in the JSON. If visual "
            "information is missing, write a warning. Character assets must be exactly one person in one image. Scene assets "
            "must be empty environments with no people."
        )
        graph_context = _story_graph_visual_context(script_json)
        user_prompt = json.dumps(
            {
                "script_json": script_json,
                "story_graph_context": graph_context,
                "asset_index": asset_index,
                "output_schema": {
                    "style_guide": {
                        "visual_bible": "string",
                        "style_anchor": "pure visual style phrase only; no asset-type rules",
                        "character_style_context": "style context for character portraits only; no buildings, scenes, huts, rooms, or location set dressing",
                        "scene_style_context": "style context for empty environment scenes only",
                        "graph_visual_context": "short visual summary derived from the story graph",
                        "render_style": "string",
                        "palette": "string",
                        "material_language": "string",
                        "character_camera": "string",
                        "scene_camera": "string",
                        "continuity": "string",
                        "blocked_prompt_terms": ["names and labels not to place in prompts"],
                    },
                    "assets": [
                        {
                            "id": "character:<character id or name> or scene:<scene name>",
                            "prompt": "image prompt",
                            "negative_prompt": "negative prompt",
                            "warnings": ["missing visual facts if any"],
                        }
                    ],
                },
                "rules": [
                    "Use script_json and story_graph_context as the source of truth.",
                    "Make style_guide specific enough that all assets look like one game project, not separate illustrations.",
                    "Keep style_anchor as pure style only. Do not put character rules or scene rules in style_anchor.",
                    "Put character-applicable world context only in character_style_context. It must describe clothing, body type, era, social class, material treatment, and portrait lighting; it must not mention huts, buildings, villages, rooms, roads, or environment thumbnails.",
                    "Put location/building/environment context only in scene_style_context.",
                    "Every asset prompt must reflect relevant graph relations without mixing scene instructions into character prompts.",
                    "Do not put Chinese display names or location labels into prompt text.",
                    "Keep display names only as ids in this JSON.",
                    "No text, watermark, subtitles, UI, logos in images.",
                    "For character assets: exactly one character only, no lineup, no multiple poses.",
                    "For scene assets: empty environment only, no people, no characters, no human figures.",
                ],
            },
            ensure_ascii=False,
        )
        raw = await client.generate_text(system_prompt, user_prompt)
        return _parse_prompt_bundle(raw, fallback=self.compose_prompt_bundle(source, request))

    async def finalize_plan_for_image_generation_async(
        self,
        plan: VisualAssetPlan,
        request: VisualAssetRequest,
        progress_callback: VisualAssetProgressCallback | None = None,
    ) -> VisualAssetPlan:
        if request.prompt_model is None:
            return plan
        client = self.text_client or OpenAICompatibleTextGenerationClient(request.prompt_model, purpose="visual_prompt")
        base_plan = _plan_with_generation_context(plan, request)
        await _emit_progress(
            progress_callback,
            "running",
            "VisualAssetGenerationAgent",
            _finalizer_start_detail(plan, request),
        )
        system_prompt = (
            "You are VisualAssetGenerationAgent acting as the final image prompt director. "
            "You receive the complete consumable visual-plan JSON and upstream story context. "
            "First lock one batch visual bible for the whole game asset pack, then write final image prompts that inherit it. "
            "Decide from context, not by blindly concatenating rules. Return only JSON. "
            "Character portraits and scene environments are different asset types and must not share type-specific instructions."
        )
        user_prompt = json.dumps(
            {
                "visual_plan": plan.model_dump(),
                "upstream_context": (plan.metadata or {}).get("upstream_context") or {},
                "provider": request.provider.model_dump(),
                "output_schema": {
                    "style_guide": {
                        "visual_bible": "one compact paragraph that freezes era, production style, subject design logic, atmosphere, and story-world constraints",
                        "style_anchor": "repeatable pure visual style phrase used by every asset; no asset-type rules",
                        "render_style": "same renderer/medium/detail level/lens logic for all assets",
                        "palette": "same color palette and lighting temperature for all assets",
                        "material_language": "same costume, architecture, prop, terrain, surface, weathering language from story context",
                        "character_style_context": "character portrait context only: costume language, grooming, posture, body type, social class, portrait lighting; no buildings or scene set dressing",
                        "scene_style_context": "scene/environment context only: architecture, terrain, props, atmosphere, camera; no characters",
                        "continuity": "specific cross-image consistency constraints",
                        "quality_bar": "what good images from this batch must have in common",
                    },
                    "assets": [
                        {
                            "id": "must match an existing asset id",
                            "prompt": "final prompt. Start with the shared visual bible/style/render/palette/material, then this asset's subject. Do not output only the delta.",
                            "negative_prompt": "final negative prompt for this asset",
                            "warnings": ["optional"],
                        }
                    ],
                },
                "rules": [
                    "Use the full visual_plan and upstream_context to decide what the whole batch should look like and what each image should show.",
                    "Every final prompt must explicitly include the same batch style anchor, renderer, palette, lighting, material language, and quality bar before the asset-specific subject.",
                    "The only differences between asset prompts should be subject identity, asset type, camera framing, and story-specific details.",
                    "Keep final prompts concise but complete: target 90-160 English words per asset.",
                    "Prefer concrete visual nouns and constraints over abstract adjectives.",
                    "For character assets: one isolated full-body character on a perfectly flat chroma-key magenta background, with an intact unobstructed face and no second person. Use world context only through clothing, grooming, body language, social class, era, and material wear.",
                    "For character assets: do not include huts, cottages, buildings, villages, room interiors, road scenes, carts, props, environment thumbnails, multi-panel sheets, or scene/environment instructions unless the asset itself is a prop/item asset.",
                    "For scene assets: empty environment only, no people, no NPCs, no human figures.",
                    "If manual_prompt exists in asset.metadata, treat it as user intent and integrate it, but still obey the asset type.",
                    "Do not include Chinese display names, text labels, UI, subtitles, logo, or watermark.",
                    "Keep all images visually coherent as one project, but never mix character and scene requirements in the same asset prompt.",
                ],
            },
            ensure_ascii=False,
        )
        try:
            raw = await client.generate_text(system_prompt, user_prompt)
        except Exception as exc:
            await _emit_progress(
                progress_callback,
                "error",
                "VisualAssetGenerationAgent",
                f"LLM finalizer failed; image generation will stop. {type(exc).__name__}: {exc}",
            )
            raise
        bundle = _parse_prompt_bundle(raw, fallback={})
        if not bundle:
            await _emit_progress(
                progress_callback,
                "error",
                "VisualAssetGenerationAgent",
                "LLM final prompt response could not be parsed; image generation will stop.",
            )
            raise ValueError("LLM final prompt response could not be parsed.")
        _validate_final_prompt_bundle(base_plan, bundle)
        finalized = _plan_with_final_prompt_bundle(base_plan, bundle, request)
        finalized_count = _count_finalized_assets(finalized)
        if finalized_count != len(finalized.assets):
            raise ValueError(f"LLM finalizer returned {finalized_count}/{len(finalized.assets)} usable final prompt(s).")
        await _emit_progress(
            progress_callback,
            "running",
            "VisualAssetGenerationAgent",
            f"LLM final prompts ready: finalized {finalized_count}/{len(finalized.assets)} asset(s); style_guide keys={_style_guide_keys(finalized)}.",
        )
        return finalized

    def compose_prompt_bundle(self, source: SandboxWorldConfig | ScriptDecompositionResult, request: VisualAssetRequest) -> dict:
        style_guide = self.compose_style_guide(source, request)
        script_json = _source_script_json(source, request.script_graph)
        character_graph_contexts = _story_graph_character_contexts(script_json)
        assets: list[dict] = []
        characters = _source_characters(source)
        if request.max_characters is not None:
            characters = characters[: request.max_characters]
        for character in characters:
            profile = _character_details(character)
            graph_context = _graph_context_for_character(character_graph_contexts, character)
            blocked_terms = list(style_guide.get("blocked_prompt_terms", [])) + [character.name]
            prompt = _sanitize_prompt_text(
                _compact_prompt(
                    [
                        style_guide.get("visual_bible", ""),
                        style_guide.get("render_style", ""),
                        style_guide.get("palette", ""),
                        style_guide.get("material_language", ""),
                        style_guide.get("character_camera", ""),
                        "character asset generated from the script story graph and character JSON",
                        _summarize_for_prompt(profile),
                        _summarize_graph_context(graph_context),
                        *_generation_requirements_for_asset("character"),
                        style_guide.get("continuity", ""),
                        "no text, no watermark, no logo, no extra people, no duplicate character, no character lineup, no multiple poses",
                    ]
                ),
                blocked_terms,
            )
            assets.append(
                {
                    "id": f"character:{getattr(character, 'id', '') or character.name}",
                    "prompt": prompt,
                    "negative_prompt": "multiple people, duplicate body, extra character, character lineup, turnaround sheet, three views, crowd",
                    "warnings": [] if profile or graph_context else [f"character.{character.name}.visual_description_missing"],
                }
            )
        scenes = _source_scene_records(source, request.script_graph)
        if request.max_scenes is not None:
            scenes = scenes[: request.max_scenes]
        for scene in scenes:
            scene_name = str(scene.get("name") or "")
            blocked_terms = list(style_guide.get("blocked_prompt_terms", [])) + [scene_name]
            prompt = _sanitize_prompt_text(
                _compact_prompt(
                    [
                        style_guide.get("visual_bible", ""),
                        style_guide.get("render_style", ""),
                        style_guide.get("palette", ""),
                        style_guide.get("material_language", ""),
                        style_guide.get("scene_camera", ""),
                        "scene asset generated from the script scene/location JSON",
                        _summarize_for_prompt(" ".join(str(value) for value in scene.values() if value)),
                        "empty environment concept art, wide cinematic establishing shot, no people, no characters, no human figures, no NPCs",
                        style_guide.get("continuity", ""),
                        "soft natural daylight, clear readable composition, no text, no watermark, no logo, no people, no characters, no figures",
                    ]
                ),
                blocked_terms,
            )
            assets.append(
                {
                    "id": f"scene:{scene_name}",
                    "prompt": prompt,
                    "negative_prompt": "people, person, character, human figure, NPC, crowd",
                    "warnings": [],
                }
            )
        return {"style_guide": style_guide, "assets": assets}

    def compose_style_guide(self, source: SandboxWorldConfig | ScriptDecompositionResult, request: VisualAssetRequest) -> dict:
        if request.style_guide:
            style_guide = dict(request.style_guide)
            style_guide.setdefault("composed_by", self.name)
            style_guide.setdefault("style_anchor", _default_style_anchor(style_guide))
            style_guide.setdefault("character_style_context", _derive_character_style_context(style_guide))
            style_guide.setdefault("scene_style_context", _derive_scene_style_context(style_guide))
            style_guide.setdefault("continuity", "all prompts must look like they belong to the same game world and the same asset batch")
            return style_guide

        script_json = _source_script_json(source, request.script_graph)
        blocked_prompt_terms = _prompt_blocked_terms(script_json)
        worldview = str(script_json.get("worldview") or "")
        core_plot = str(script_json.get("core_plot") or "")
        constraints = " ".join(str(item) for item in script_json.get("constraints", []))
        style_source = " ".join([worldview, core_plot, constraints])
        themes = _extract_theme_phrases_from_json(script_json)
        visual_terms = _extract_visual_terms_from_json(script_json)
        era = _extract_era(style_source)
        power_system = _extract_power_system(style_source)
        palette = _derive_palette(style_source)
        material = _derive_material_language(style_source)
        graph_visual_context = _story_graph_visual_context(script_json)
        visual_bible = _compact_prompt(
            [
                "single coherent game visual bible derived from the script asset",
                era,
                power_system,
                _sanitize_prompt_text("; ".join(themes[:6]), blocked_prompt_terms),
            ]
        )
        style_anchor = _compact_prompt(
            [
                "LOCKED BATCH STYLE",
                "pre-modern rural Chinese xianxia game concept art" if "ancient" in era or "cultivation" in power_system else "story-specific game concept art",
                "muted natural colors, grounded human proportions, weathered practical costumes",
                "same renderer, same lighting, same lens language across every image",
            ]
        )
        return {
            "visual_bible": visual_bible,
            "style_anchor": style_anchor,
            "render_style": _compact_prompt(
                [
                    "consistent production concept art",
                    "same renderer, same proportions, same lighting logic across all assets",
                    request.style_prompt,
                ]
            ),
            "palette": palette,
            "material_language": material,
            "graph_visual_context": graph_visual_context,
            "character_style_context": _compact_prompt(
                [
                    style_anchor,
                    visual_bible,
                    "rural hardship expressed through clothing, posture, grooming, and material wear, not background buildings",
                    "practical linen and cotton garments, cloth belts, worn boots, muted natural colors",
                    "single-person sprite lighting on perfectly flat chroma-key magenta background",
                ]
            ),
            "scene_style_context": _compact_prompt([style_anchor, visual_bible, graph_visual_context, material]),
            "world_terms": visual_terms[:12],
            "blocked_prompt_terms": blocked_prompt_terms,
            "graph_context_by_character": _story_graph_character_contexts(script_json),
            "character_camera": "single full-body orthographic standing sprite, exactly one intact person centered, face unobstructed",
            "scene_camera": "empty wide cinematic establishing shot, environment only",
            "continuity": "all prompts must look like they belong to the same game world and the same asset batch",
            "source_json_keys": list(script_json.keys()),
            "source_json_excerpt": script_json,
            "composed_by": self.name,
            "composer_mode": request.prompt_composer,
            "prompt_model": request.prompt_model.model_dump() if request.prompt_model else None,
        }

    def compose_character_spec(
        self,
        character: SandboxNPC | ScriptCharacterSheet,
        index: int,
        output_root: Path,
        request: VisualAssetRequest,
        style_guide: dict,
        prompt_spec: dict | None = None,
    ) -> VisualAssetSpec:
        character_id = getattr(character, "id", "") or _safe_id(character.name, f"character_{index}")
        warnings: list[str] = []
        character_profile = _character_details(character)
        graph_context = _graph_context_for_character(style_guide.get("graph_context_by_character", {}), character)
        if graph_context:
            character_profile = _compact_prompt([character_profile, _summarize_graph_context(graph_context)])
        if not character_profile:
            warnings.append(f"character.{character.name}.visual_description_missing")
        blocked_terms = list(style_guide.get("blocked_prompt_terms", [])) + [character.name]
        asset_prompt = _sanitize_prompt_text(str((prompt_spec or {}).get("prompt") or ""), blocked_terms)
        if not asset_prompt:
            asset_prompt = _compact_prompt(
                [
                    "character asset generated from the script story graph and character JSON",
                    _summarize_for_prompt(character_profile),
                ]
            )
        prompt = _asset_prompt_with_locked_style(
            style_guide,
            asset_prompt,
            [
                style_guide.get("character_camera", ""),
                *_generation_requirements_for_asset("character"),
                "no buildings, no huts, no cottages, no room interior, no village background, no environment thumbnails",
                "no props unless explicitly implied by the character JSON",
                "no text, no watermark, no logo, no extra people, no duplicate character, no character lineup, no multiple poses",
            ],
            blocked_terms,
            kind="character",
        )
        warnings.extend(str(item) for item in (prompt_spec or {}).get("warnings", []) if item)
        return VisualAssetSpec(
            id=f"character_{_safe_id(character.name, character_id)}",
            kind="character",
            display_name=character.name,
            prompt=prompt,
            output_path=str(output_root / "characters" / f"{_safe_id(character.name, character_id)}.png"),
            source_id=character_id,
            source_name=character.name,
            provider=request.provider.provider,
            model=request.provider.model,
            size=request.provider.size,
            negative_prompt=_join_negative_prompt(
                request.negative_prompt,
                _join_negative_prompt(
                    str((prompt_spec or {}).get("negative_prompt") or ""),
                    _locked_style_negative_prompt(style_guide, "character"),
                ),
            ),
            warnings=warnings,
            metadata={
                "role": getattr(character, "role", ""),
                "prompt_source": character_profile,
                "graph_context": graph_context,
                "asset_generation_contract": "isolated_character_v2",
            },
        )

    def compose_scene_spec(
        self,
        scene: dict,
        index: int,
        output_root: Path,
        request: VisualAssetRequest,
        style_guide: dict,
        prompt_spec: dict | None = None,
    ) -> VisualAssetSpec:
        scene_name = str(scene.get("name") or f"scene_{index}")
        scene_id = _safe_id(scene_name, f"scene_{index}")
        blocked_terms = list(style_guide.get("blocked_prompt_terms", [])) + [scene_name]
        asset_prompt = _sanitize_prompt_text(str((prompt_spec or {}).get("prompt") or ""), blocked_terms)
        if not asset_prompt:
            asset_prompt = _compact_prompt(
                [
                    "scene asset generated from the script scene/location JSON",
                    _summarize_for_prompt(" ".join(str(value) for value in scene.values() if value)),
                ]
            )
        prompt = _asset_prompt_with_locked_style(
            style_guide,
            asset_prompt,
            [
                style_guide.get("scene_camera", ""),
                "empty environment concept art, wide cinematic establishing shot, no people, no characters, no human figures, no NPCs",
                "soft natural daylight, clear readable composition, no text, no watermark, no logo, no people, no characters, no figures",
            ],
            blocked_terms,
            kind="scene",
        )
        return VisualAssetSpec(
            id=f"scene_{scene_id}",
            kind="scene",
            display_name=scene_name,
            prompt=prompt,
            output_path=str(output_root / "scenes" / f"{scene_id}.png"),
            source_name=scene_name,
            provider=request.provider.provider,
            model=request.provider.model,
            size=request.provider.size,
            negative_prompt=_join_negative_prompt(
                request.negative_prompt,
                _join_negative_prompt(
                    str((prompt_spec or {}).get("negative_prompt") or ""),
                    _locked_style_negative_prompt(style_guide, "scene"),
                ),
            ),
            metadata={"scene_index": index, "prompt_source": scene, "asset_generation_contract": "empty_scene_v2"},
        )


def _asset_prompt_with_locked_style(
    style_guide: dict,
    asset_prompt: str,
    asset_requirements: list[str],
    blocked_terms: list[str] | None = None,
    kind: str = "",
) -> str:
    return _sanitize_prompt_text(
        _compact_prompt(
            [
                _locked_style_prompt(style_guide, kind),
                asset_prompt,
                *asset_requirements,
                _continuity_prompt(style_guide, kind),
                "strict visual continuity across the entire generated batch",
            ]
        ),
        blocked_terms,
    )


async def _emit_progress(
    callback: VisualAssetProgressCallback | None,
    status: str,
    title: str,
    detail: str,
) -> None:
    if callback is None:
        return
    result = callback(status, title, detail)
    if inspect.isawaitable(result):
        await result


def _plan_flow_detail(plan: VisualAssetPlan) -> str:
    metadata = plan.metadata if isinstance(plan.metadata, dict) else {}
    upstream = metadata.get("upstream_context") if isinstance(metadata.get("upstream_context"), dict) else {}
    context_parts = []
    if upstream.get("source_json"):
        context_parts.append("source_json")
    if upstream.get("story_graph_context"):
        context_parts.append("story_graph_context")
    if metadata.get("style_guide"):
        context_parts.append("style_guide")
    context = "、".join(context_parts) if context_parts else "尚未检测到"
    return (
        f"已载入视觉方案 `{plan.plan_id}`（{plan.title or plan.world_id or '未命名'}）："
        f"共 {len(plan.assets)} 项资产；上游上下文：{context}。"
    )


def _finalizer_start_detail(plan: VisualAssetPlan, request: VisualAssetRequest) -> str:
    provider = request.provider
    model = request.prompt_model.model if request.prompt_model else ""
    return (
        f"正在把完整 visual_plan 交给视觉提示词 Agent：plan_id={plan.plan_id}，资产={len(plan.assets)}，"
        f"提示词模型={model or '使用已配置默认值'}，图片服务={provider.provider}，图片模型={provider.model}，尺寸={provider.size}。"
    )


def _style_guide_keys(plan: VisualAssetPlan) -> str:
    metadata = plan.metadata if isinstance(plan.metadata, dict) else {}
    style_guide = metadata.get("style_guide") if isinstance(metadata.get("style_guide"), dict) else {}
    keys = [str(key) for key in style_guide.keys() if key in {"visual_bible", "style_anchor", "character_style_context", "scene_style_context", "palette", "continuity"}]
    return ", ".join(keys) if keys else "none"


def _count_finalized_assets(plan: VisualAssetPlan) -> int:
    return sum(1 for asset in plan.assets if (asset.metadata or {}).get("finalized_by") == "visual_asset_generation_llm")


def _validate_final_prompt_bundle(plan: VisualAssetPlan, bundle: dict) -> None:
    style_guide = bundle.get("style_guide")
    assets = bundle.get("assets")
    if not isinstance(style_guide, dict):
        raise ValueError("LLM finalizer response missing style_guide.")
    if not isinstance(assets, list):
        raise ValueError("LLM finalizer response missing assets.")
    planned_ids = {asset.id for asset in plan.assets}
    returned_ids = {str(item.get("id") or "") for item in assets if isinstance(item, dict)}
    missing = sorted(planned_ids - returned_ids)
    if missing:
        raise ValueError(f"LLM finalizer response missing final prompts for {len(missing)} asset(s): {', '.join(missing[:5])}.")
    empty_prompts = [
        str(item.get("id") or "")
        for item in assets
        if isinstance(item, dict) and str(item.get("id") or "") in planned_ids and not str(item.get("prompt") or "").strip()
    ]
    if empty_prompts:
        raise ValueError(f"LLM finalizer response has empty prompt(s): {', '.join(empty_prompts[:5])}.")


def _run_flow_detail(plan: VisualAssetPlan) -> str:
    metadata = plan.metadata if isinstance(plan.metadata, dict) else {}
    run_id = str(metadata.get("generation_run_id") or "")
    return (
        f"视觉生成任务 `{run_id}` 已准备：服务={plan.provider.provider}，模型={plan.provider.model}，"
        f"默认尺寸={plan.provider.size}，steps={plan.provider.steps}，cfg_scale={plan.provider.cfg_scale}，"
        f"seed={plan.provider.seed if plan.provider.seed is not None else '随机'}，text_mode={plan.provider.text_mode}，资产={len(plan.assets)}。"
    )


def _asset_start_detail(spec: VisualAssetSpec, index: int, total: int, seed: int | None = None) -> str:
    prompt_length = len(spec.prompt or "")
    negative_length = len(spec.negative_prompt or "")
    seed_text = seed if seed is not None else "随机"
    return (
        f"正在生成资产 {index}/{total}：id={spec.id}，类型={spec.kind}，名称={spec.display_name or spec.source_name}，"
        f"尺寸={spec.size}，seed={seed_text}，正向提示词={prompt_length} 字符，负向提示词={negative_length} 字符。"
    )


def _seed_for_asset(base_seed: int | None, index: int) -> int | None:
    if base_seed is None:
        return None
    return base_seed + max(0, index - 1)


def _plan_with_final_prompt_bundle(plan: VisualAssetPlan, bundle: dict, request: VisualAssetRequest) -> VisualAssetPlan:
    style_guide = dict(plan.metadata.get("style_guide") or {}) if isinstance(plan.metadata, dict) else {}
    if isinstance(bundle.get("style_guide"), dict):
        style_guide = {**style_guide, **bundle["style_guide"]}
    style_guide.setdefault("composed_by", VisualPromptComposerAgent.name)
    style_guide.setdefault("style_anchor", _default_style_anchor(style_guide))
    style_guide.setdefault("character_style_context", _derive_character_style_context(style_guide))
    style_guide["character_style_context"] = _character_only_style_text(style_guide.get("character_style_context", ""))
    style_guide.setdefault("scene_style_context", _derive_scene_style_context(style_guide))
    style_guide.setdefault("continuity", "all prompts must look like they belong to the same game world and the same asset batch")

    prompt_specs = {str(item.get("id") or ""): item for item in bundle.get("assets", []) if isinstance(item, dict)}
    updated_assets: list[VisualAssetSpec] = []
    for asset in plan.assets:
        prompt_spec = prompt_specs.get(asset.id)
        if not prompt_spec:
            updated_assets.append(asset)
            continue
        blocked_terms = list(style_guide.get("blocked_prompt_terms", [])) + [asset.display_name, asset.source_name]
        prompt = _sanitize_prompt_text(str(prompt_spec.get("prompt") or ""), blocked_terms)
        if asset.kind == "character":
            prompt = _character_only_style_text(prompt)
        if not prompt:
            prompt = asset.prompt
        prompt = _enforce_asset_generation_contract(prompt, asset.kind)
        negative_prompt = _join_negative_prompt(
            _join_negative_prompt(request.negative_prompt, str(prompt_spec.get("negative_prompt") or "")),
            _locked_style_negative_prompt(style_guide, asset.kind),
        )
        negative_prompt = _join_negative_prompt(negative_prompt, _asset_contract_negative_prompt(asset.kind))
        warnings = [*asset.warnings, *(str(item) for item in prompt_spec.get("warnings", []) if item)]
        metadata = _asset_local_metadata(
            asset.metadata,
            extra={
                "finalized_by": "visual_asset_generation_llm",
                "asset_generation_contract": "isolated_character_v2" if asset.kind == "character" else "empty_scene_v2",
            },
        )
        updated_assets.append(asset.model_copy(update={"prompt": prompt, "negative_prompt": negative_prompt, "warnings": warnings, "metadata": metadata}))
    metadata = {**(plan.metadata or {}), "style_guide": style_guide, "final_prompt_composed_by": "visual_asset_generation_llm"}
    return plan.model_copy(update={"assets": updated_assets, "metadata": metadata})


def _locked_style_prompt(style_guide: dict, kind: str = "") -> str:
    if kind == "character":
        return _compact_prompt(
            [
                _character_only_style_text(style_guide.get("character_style_context") or _derive_character_style_context(style_guide)),
                "character standing sprite only, isolated figure on a flat chroma-key magenta background, no environment set dressing",
            ]
        )
    return _compact_prompt(
        [
            style_guide.get("scene_style_context") or _derive_scene_style_context(style_guide),
            style_guide.get("render_style", ""),
            style_guide.get("palette", ""),
        ]
    )


def _character_material_language(material_language: str) -> str:
    blocked_fragments = {
        "mud walls",
        "thatched roofs",
        "farming village textures",
        "wooden medicine shelves",
        "herb garden props",
        "weathered cliff stone",
        "mountain mist",
    }
    parts = [part.strip() for part in material_language.split(",") if part.strip()]
    kept = [part for part in parts if part not in blocked_fragments]
    return ", ".join(kept) or "plain linen cloth, cloth belts, worn boots, practical rural clothing textures"


def _derive_character_style_context(style_guide: dict) -> str:
    return _character_only_style_text(
        _compact_prompt(
            [
                style_guide.get("style_anchor") or _default_style_anchor(style_guide),
                style_guide.get("visual_bible", ""),
                style_guide.get("render_style", ""),
                style_guide.get("palette", ""),
                _character_material_language(str(style_guide.get("material_language") or "")),
                "world context is expressed through costume, body language, grooming, and material wear, not environment props",
            ]
        )
    )


def _derive_scene_style_context(style_guide: dict) -> str:
    return _compact_prompt(
        [
            style_guide.get("style_anchor") or _default_style_anchor(style_guide),
            style_guide.get("visual_bible", ""),
            style_guide.get("graph_visual_context", ""),
            style_guide.get("material_language", ""),
        ]
    )


def _continuity_prompt(style_guide: dict, kind: str) -> str:
    text = str(style_guide.get("continuity") or "")
    if kind == "character":
        text = _character_only_style_text(text)
        return _compact_prompt(
            [
                text,
                "Character assets share the same world style but remain isolated single-person portraits with no environment panels.",
            ]
        )
    return text


def _character_only_style_text(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = re.sub(r"(?i)\bscene images? must be[^.。;；]*[.。;；]?", " ", text)
    text = re.sub(r"(?i)\bscene assets? must be[^.。;；]*[.。;；]?", " ", text)
    text = re.sub(r"(?i)\bempty environment concept art for scenes?[^.。;；,]*[,.。;；]?", " ", text)
    text = re.sub(r"(?i)\b[^,.;。；]*empty environment[^,.;。；]*for scenes?[^,.;。；]*[,.。;；]?", " ", text)
    text = re.sub(r"(?i)\b[^,.;。；]*for scenes?[^,.;。；]*[,.。;；]?", " ", text)
    text = re.sub(r"(?i)\bempty environment(?:s)?[^.。;；,]*[,.。;；]?", " ", text)
    text = re.sub(r"(?i)\bcinematic establishing shots?[^.。;；,]*[,.。;；]?", " ", text)
    text = re.sub(r"(?i)\bwide environment shots?[^.。;；,]*[,.。;；]?", " ", text)
    text = re.sub(r"(?i)\bscene/location JSON\b", "character JSON", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,.;；。")


def _default_style_anchor(style_guide: dict) -> str:
    return _compact_prompt(
        [
            "LOCKED BATCH STYLE",
            "one coherent game concept-art asset pack",
            "same renderer, same proportions, same lighting, same lens language",
            str(style_guide.get("render_style") or ""),
        ]
    )


def _locked_style_negative_prompt(style_guide: dict, kind: str) -> str:
    prompt = _compact_prompt(
        [
            "inconsistent art style",
            "different renderer",
            "different character proportions",
            "unrelated world design",
            "random costume design",
            "photorealistic snapshot" if "concept art" in str(style_guide.get("render_style") or "").lower() else "",
            "modern asphalt road, cars, electric poles, neon signs" if _style_reads_as_ancient(style_guide) else "",
        ]
    )
    if kind == "character":
        return _join_negative_prompt(prompt, _character_isolation_negative_prompt())
    if kind == "scene":
        return _join_negative_prompt(prompt, "people, person, human figure, crowd")
    return prompt


def _style_reads_as_ancient(style_guide: dict) -> bool:
    text = " ".join(str(style_guide.get(key) or "") for key in ["visual_bible", "style_anchor", "graph_visual_context"])
    return any(token in text.lower() for token in ["ancient", "xianxia", "cultivation", "pre-modern", "historical"])


def _resolve_source(request: VisualAssetRequest) -> SandboxWorldConfig | ScriptDecompositionResult | dict:
    if _is_script_graph_document(request.script_graph):
        return request.script_graph or {}
    if request.decomposition is not None:
        return request.decomposition
    if request.world is not None:
        return request.world
    raise ValueError("VisualAssetRequest requires script_graph for the clean pipeline, or decomposition/world for legacy migration")


def _requires_async_prompt_composer(request: VisualAssetRequest) -> bool:
    return request.prompt_model is not None or request.prompt_composer.lower() in {"llm", "model"}


def _request_plan(request: VisualAssetRequest) -> VisualAssetPlan | None:
    if not request.plan:
        return None
    return VisualAssetPlan.model_validate(request.plan)


def _request_has_source(request: VisualAssetRequest) -> bool:
    return _is_script_graph_document(request.script_graph) or request.decomposition is not None or request.world is not None


def _plan_with_generation_context(plan: VisualAssetPlan, request: VisualAssetRequest) -> VisualAssetPlan:
    metadata = dict(plan.metadata or {})
    style_guide = dict(metadata.get("style_guide") or request.style_guide or {})
    if style_guide:
        style_guide.setdefault("composed_by", VisualPromptComposerAgent.name)
        style_guide.setdefault("style_anchor", _default_style_anchor(style_guide))
        style_guide.setdefault("character_style_context", _derive_character_style_context(style_guide))
        style_guide["character_style_context"] = _character_only_style_text(style_guide.get("character_style_context", ""))
        style_guide.setdefault("scene_style_context", _derive_scene_style_context(style_guide))
        style_guide.setdefault("continuity", "all prompts must look like they belong to the same game world and the same asset batch")
        source_excerpt = style_guide.get("source_json_excerpt") if isinstance(style_guide.get("source_json_excerpt"), dict) else {}
        if not style_guide.get("graph_visual_context") and source_excerpt:
            style_guide["graph_visual_context"] = _story_graph_visual_context(source_excerpt)
    if not style_guide:
        style_guide = VisualPromptComposerAgent().compose_style_guide(_resolve_source(request), request)

    metadata["style_guide"] = style_guide
    upstream = dict(metadata.get("upstream_context") or {})
    source_excerpt = style_guide.get("source_json_excerpt") if isinstance(style_guide.get("source_json_excerpt"), dict) else {}
    request_source_json = _source_script_json(_resolve_source(request), request.script_graph) if _request_has_source(request) else {}
    source_json = upstream.get("source_json") if isinstance(upstream.get("source_json"), dict) else {}
    if not source_json:
        source_json = source_excerpt or request_source_json
    if source_json:
        upstream["source_json"] = source_json
    story_graph_context = str(upstream.get("story_graph_context") or "").strip()
    if not story_graph_context:
        story_graph_context = str(style_guide.get("graph_visual_context") or "").strip()
    if not story_graph_context and source_json:
        story_graph_context = _story_graph_visual_context(source_json)
    upstream["story_graph_context"] = story_graph_context
    upstream["source_contract"] = "script_graph -> visual_plan -> image_generation -> character_background_removal -> artifact"
    metadata["upstream_context"] = upstream

    updated_assets: list[VisualAssetSpec] = []
    for asset in plan.assets:
        asset_metadata = _asset_local_metadata(asset.metadata)
        manual_prompt = _sanitize_prompt_text(str(asset_metadata.get("manual_prompt") or ""), [asset.display_name, asset.source_name])
        blocked_terms = list(style_guide.get("blocked_prompt_terms", [])) + [asset.display_name, asset.source_name]
        if asset_metadata.get("finalized_by") == "visual_asset_generation_llm":
            prompt = _sanitize_prompt_text(_compact_prompt([asset.prompt, manual_prompt]), blocked_terms)
            if asset.kind == "character":
                prompt = _character_only_style_text(prompt)
        else:
            asset_prompt = _compact_prompt([_asset_local_prompt(asset), manual_prompt])
            prompt = _asset_prompt_with_locked_style(
                style_guide,
                asset_prompt,
                _generation_requirements_for_asset(asset.kind),
                blocked_terms,
                kind=asset.kind,
            )
        prompt = _enforce_asset_generation_contract(prompt, asset.kind)
        negative_prompt = _join_negative_prompt(asset.negative_prompt, _locked_style_negative_prompt(style_guide, asset.kind))
        negative_prompt = _join_negative_prompt(negative_prompt, _asset_contract_negative_prompt(asset.kind))
        asset_metadata["asset_generation_contract"] = "isolated_character_v2" if asset.kind == "character" else "empty_scene_v2"
        updated_assets.append(asset.model_copy(update={"prompt": prompt, "negative_prompt": negative_prompt, "metadata": asset_metadata}))
    return plan.model_copy(update={"assets": updated_assets, "metadata": metadata})


def _asset_local_metadata(metadata: dict | None, extra: dict | None = None) -> dict:
    compact = {
        key: value
        for key, value in dict(metadata or {}).items()
        if key not in {"style_guide", "upstream_context", "source_json", "source_json_excerpt"}
    }
    if extra:
        compact.update(extra)
    return compact


def _plan_with_generation_run(plan: VisualAssetPlan) -> VisualAssetPlan:
    metadata = dict(plan.metadata or {})
    run_id = _new_generation_run_id()
    metadata["generation_run_id"] = run_id
    metadata["generation_run_created_at"] = datetime.now(timezone.utc).isoformat()
    return plan.model_copy(update={"metadata": metadata})


def _new_generation_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    return f"{stamp}_{uuid.uuid4().hex[:8]}"


def _generation_requirements_for_asset(kind: str) -> list[str]:
    if kind == "character":
        return [
            "production-ready visual novel standing sprite, exactly one character only, single person, one full body portrait, front-facing neutral pose",
            "the entire head, face, hair, hands, clothing and feet are complete and fully inside the frame; clean unbroken facial features, visible eyes, nose and mouth, nothing covering the face",
            "isolated character on a perfectly flat uniform chroma-key magenta (#FF00FF) background; no gradient, shadow, texture, horizon, scenery or background objects; do not use magenta on the character",
            "no buildings, no huts, no cottages, no room interior, no village background, no environment thumbnails",
            "no props unless explicitly implied by the character JSON",
            "no text, no watermark, no logo, no extra people, no duplicate character, no character lineup, no multiple poses",
        ]
    if kind == "scene":
        return [
            "empty environment concept art, wide cinematic establishing shot, no people, no characters, no human figures, no NPCs",
            "soft natural daylight, clear readable composition, no text, no watermark, no logo, no people, no characters, no figures",
        ]
    return ["game asset concept art, no text, no watermark, coherent with the batch style"]


def _asset_local_prompt(asset: VisualAssetSpec) -> str:
    metadata = asset.metadata or {}
    prompt_source = metadata.get("prompt_source")
    if isinstance(prompt_source, dict):
        source_text = _summarize_for_prompt(" ".join(str(value) for value in prompt_source.values() if value))
    else:
        source_text = _summarize_for_prompt(str(prompt_source or ""))
    if source_text:
        if asset.kind == "character":
            return _compact_prompt(["character asset generated from the script character JSON", _character_only_style_text(source_text)])
        if asset.kind == "scene":
            return _compact_prompt(["scene asset generated from the script scene/location JSON", source_text])

    prompt = str(asset.prompt or "")
    if "LOCKED BATCH STYLE" in prompt:
        return ""
    if asset.kind == "character":
        return _character_only_style_text(prompt)
    return prompt


def _enforce_asset_generation_contract(prompt: str, kind: str) -> str:
    return _compact_prompt([prompt, *_generation_requirements_for_asset(kind)])


def _asset_contract_negative_prompt(kind: str) -> str:
    if kind == "character":
        return _character_isolation_negative_prompt()
    if kind == "scene":
        return "people, person, portrait, character, human figure, humanoid silhouette, NPC, crowd, face, body"
    return ""


def _character_isolation_negative_prompt() -> str:
    return (
        "multiple people, second person, extra face, duplicate character, crowd, companion, character lineup, turnaround sheet, "
        "multiple poses, cropped head, cropped feet, missing face, blank face, faceless, face hole, facial occlusion, mask over face, "
        "background scenery, architecture, room, landscape, gradient background, textured background, spotlight, cast shadow, aura, smoke"
    )


def _character_isolation_retry_prompt(prompt: str) -> str:
    return _compact_prompt(
        [
            "ISOLATION RETRY: regenerate this as a clean production sprite, not an illustration or scene",
            prompt,
            *_generation_requirements_for_asset("character"),
            "absolute requirement: exactly one intact person against one flat solid magenta background with a crisp silhouette",
        ]
    )


def _character_isolation_retry_path(output_path: str) -> str:
    path = Path(output_path)
    suffix = path.suffix or ".png"
    return str(path.with_name(f"{path.stem}.isolation-retry{suffix}"))


def _character_screen_variant_prompt(prompt: str, screen_name: str, screen_hex: str) -> str:
    rewritten = re.sub(r"magenta", screen_name, prompt, flags=re.IGNORECASE).replace("#FF00FF", screen_hex)
    contract = (
        f"SCREEN CONTRACT: exactly one intact full-body person on a flat uniform {screen_name} ({screen_hex}) background; "
        f"complete face, hair, hands, clothes and feet; crisp silhouette; no gradient, shadow, scenery, text, extra people, "
        f"or {screen_name} character details"
    )
    # StepFun image endpoints cap prompts at 512 CJK characters / Latin words.
    # Reserve the final units for the non-negotiable screen contract and trim
    # repeated style/world context that visual-plan finalization may append.
    return _compact_prompt([_provider_prompt_budget(rewritten, 300), contract])


def _provider_prompt_budget(text: str, max_units: int) -> str:
    """Bound CJK characters / Latin words using the provider's counting model."""
    tokens = re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9][A-Za-z0-9_#.+/-]*|[^\s]", str(text or ""))
    return " ".join(tokens[: max(1, max_units)])


def _character_screen_negative_prompt(screen_name: str) -> str:
    return _join_negative_prompt(
        _character_isolation_negative_prompt(),
        f"{screen_name} clothes, {screen_name} hair, {screen_name} skin, {screen_name} accessories, non-uniform background",
    )


def _character_screen_variant_path(output_path: str, screen_name: str) -> str:
    path = Path(output_path)
    suffix = path.suffix or ".png"
    return str(path.with_name(f"{path.stem}.screen-{screen_name}{suffix}"))


def _plan_with_safe_output_paths(plan: VisualAssetPlan, output_root: str) -> VisualAssetPlan:
    root = Path(output_root) / _safe_id(plan.world_id or plan.title, "visual_assets")
    generation_run_id = str((plan.metadata or {}).get("generation_run_id") or "").strip()
    if generation_run_id:
        root = root / "runs" / _safe_id(generation_run_id, "run")
    updated_assets: list[VisualAssetSpec] = []
    for asset in plan.assets:
        original = Path(str(asset.output_path).replace("\\", "/"))
        category = "characters" if asset.kind == "character" else "scenes" if asset.kind == "scene" else "assets"
        if original.parent.name in {"characters", "scenes", "assets"}:
            category = original.parent.name
        filename = original.name or f"{_safe_id(asset.id or asset.display_name, 'asset')}.png"
        updated_assets.append(asset.model_copy(update={"output_path": str(root / category / filename)}))
    return plan.model_copy(update={"assets": updated_assets})


def _source_world_id(source: SandboxWorldConfig | ScriptDecompositionResult | dict) -> str:
    if isinstance(source, dict):
        return str(source.get("graph_id") or "")
    if isinstance(source, SandboxWorldConfig):
        return source.world_id
    return source.script_id or str(source.world_mapping.get("world_id", ""))


def _source_title(source: SandboxWorldConfig | ScriptDecompositionResult | dict) -> str:
    if isinstance(source, dict):
        return str(source.get("title") or source.get("graph_id") or "script_graph")
    return source.name if isinstance(source, SandboxWorldConfig) else source.title


def _source_characters(source: SandboxWorldConfig | ScriptDecompositionResult | dict) -> list[SandboxNPC | ScriptCharacterSheet]:
    if isinstance(source, dict):
        characters: list[ScriptCharacterSheet] = []
        for index, node in enumerate(_graph_nodes(source, {"character"}), start=1):
            properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
            relations = _relations_for_node(source, str(node.get("id") or ""), _graph_nodes_by_id(source))
            location = next((item["other_label"] for item in relations if item["type"] == "LOCATED_AT" and item["direction"] == "out"), "")
            characters.append(
                ScriptCharacterSheet(
                    id=str(properties.get("source_id") or node.get("id") or f"character_{index}"),
                    name=str(node.get("label") or properties.get("name") or f"character_{index}"),
                    role=str(properties.get("role") or "NPC"),
                    public_info=str(properties.get("public_info") or properties.get("description") or properties.get("text") or ""),
                    motive=str(properties.get("motive") or ""),
                    alibi=str(properties.get("alibi") or ""),
                    location=location,
                    metadata={"graph_node_id": node.get("id"), "graph_relations": relations},
                )
            )
        return characters
    return list(source.npcs if isinstance(source, SandboxWorldConfig) else source.characters)


def _source_scenes(source: SandboxWorldConfig | ScriptDecompositionResult | dict) -> list[str]:
    return [scene["name"] for scene in _source_scene_records(source)]


def _source_script_graph(source: SandboxWorldConfig | ScriptDecompositionResult | dict, script_graph: dict | None = None) -> dict:
    if _is_script_graph_document(script_graph):
        return script_graph
    if _is_script_graph_document(source):
        return source
    if isinstance(source, SandboxWorldConfig):
        metadata = source.metadata if isinstance(source.metadata, dict) else {}
        graph = metadata.get("script_graph")
        if isinstance(graph, dict) and graph.get("nodes") is not None and graph.get("edges") is not None:
            return graph
        decomposition = metadata.get("script_decomposition")
        if isinstance(decomposition, dict):
            try:
                return ScriptGraphCompiler().compile(decomposition).model_dump()
            except Exception:
                return {}
        return {}
    if isinstance(source, ScriptDecompositionResult):
        return ScriptGraphCompiler().compile(source).model_dump()
    return {}


def _source_scene_records(source: SandboxWorldConfig | ScriptDecompositionResult | dict, script_graph: dict | None = None) -> list[dict]:
    if isinstance(source, dict):
        return _dedupe_scene_records(_story_graph_scene_records({"script_graph": _source_script_graph(source, script_graph)}))
    if isinstance(source, ScriptDecompositionResult):
        script_json = _source_script_json(source, script_graph)
        records = [{"name": scene, "description": _scene_description_from_json(scene, script_json)} for scene in source.locations if scene]
        records = [*_story_graph_scene_records(script_json), *records]
        return _dedupe_scene_records(records)

    scenes: list[str] = []
    location = source.player.get("location") if isinstance(source.player, dict) else ""
    if location:
        scenes.append(str(location))
    scenes.extend(npc.location for npc in source.npcs if npc.location)
    script_case = source.metadata.get("script_case") if isinstance(source.metadata, dict) else None
    if isinstance(script_case, dict):
        scenes.extend(str(item) for item in script_case.get("locations", []) if item)
    script_json = _source_script_json(source, script_graph)
    records = [{"name": scene, "description": _scene_description_from_json(scene, script_json)} for scene in scenes if scene]
    records = [*_story_graph_scene_records(script_json), *records]
    return _dedupe_scene_records(records)


def _source_script_json(source: SandboxWorldConfig | ScriptDecompositionResult | dict, script_graph: dict | None = None) -> dict:
    graph = _source_script_graph(source, script_graph)
    if isinstance(source, dict):
        return {
            "title": str(source.get("title") or source.get("graph_id") or "script_graph"),
            "worldview": _graph_worldview(source),
            "core_plot": _graph_core_plot(source),
            "script_graph": graph,
            "characters": [character.model_dump() for character in _source_characters(source)],
            "locations": [record["name"] for record in _story_graph_scene_records({"script_graph": graph})],
            "clues": [],
            "constraints": [],
            "metadata": {"source_schema": "script_graph.v1"},
        }
    if isinstance(source, SandboxWorldConfig):
        metadata = source.metadata if isinstance(source.metadata, dict) else {}
        script_case = metadata.get("script_decomposition") or metadata.get("script_case") or {}
        if isinstance(script_case, dict):
            result = dict(script_case)
            if graph:
                result["script_graph"] = graph
            elif isinstance(metadata.get("story_graph_summary"), dict):
                result["story_graph_summary"] = metadata["story_graph_summary"]
            return result
        return {
            "title": source.name,
            "worldview": source.lore or source.description,
            "opening_scene": source.opening_scene,
            "characters": [npc.model_dump() for npc in source.npcs],
            "locations": _source_scenes(source),
            "script_graph": graph,
            "constraints": [],
        }
    return {
        "script_id": source.script_id,
        "title": source.title,
        "worldview": source.public_background,
        "core_plot": source.core_plot,
        "hidden_threads": list(source.hidden_threads),
        "characters": [character.model_dump() for character in source.characters],
        "locations": list(source.locations),
        "clues": [clue.model_dump() for clue in source.clues],
        "constraints": list(source.constraints),
        "script_graph": graph,
        "story_graph": source.story_graph.model_dump(),
        "metadata": source.metadata,
    }


def _asset_index_json(source: SandboxWorldConfig | ScriptDecompositionResult, request: VisualAssetRequest) -> list[dict]:
    assets: list[dict] = []
    if request.include_characters:
        characters = _source_characters(source)
        if request.max_characters is not None:
            characters = characters[: request.max_characters]
        for character in characters:
            script_json = _source_script_json(source, request.script_graph)
            graph_context = _graph_context_for_character(_story_graph_character_contexts(script_json), character)
            assets.append(
                {
                    "id": f"character:{getattr(character, 'id', '') or character.name}",
                    "kind": "character",
                    "display_name": character.name,
                    "source_json": character.model_dump(),
                    "graph_context": graph_context,
                    "requirements": ["exactly one character only", "one full body portrait", "no text labels"],
                }
            )
    if request.include_scenes:
        scenes = _source_scene_records(source, request.script_graph)
        if request.max_scenes is not None:
            scenes = scenes[: request.max_scenes]
        for scene in scenes:
            name = str(scene.get("name") or "")
            assets.append(
                {
                    "id": f"scene:{name}",
                    "kind": "scene",
                    "display_name": name,
                    "source_json": scene,
                    "requirements": ["empty environment only", "no people", "no characters", "no text labels"],
                }
            )
    return assets


def _story_graph_character_contexts(script_json: dict) -> dict[str, dict]:
    graph = _graph_document_from_script_json(script_json)
    nodes = _graph_nodes_by_id(graph)
    contexts: dict[str, dict] = {}
    for node in graph.get("nodes", []) if isinstance(graph.get("nodes"), list) else []:
        if not isinstance(node, dict) or str(node.get("kind") or "") != "character":
            continue
        keys = [str(node.get("id") or ""), str(node.get("label") or ""), str((node.get("properties") or {}).get("source_id") or "")]
        context = {
            "node": _compact_graph_node(node),
            "relations": _relations_for_node(graph, str(node.get("id") or ""), nodes),
        }
        for key in keys:
            norm = _norm_key(key)
            if norm:
                contexts[norm] = context
    return contexts


def _story_graph_visual_context(script_json: dict) -> str:
    graph = _graph_document_from_script_json(script_json)
    if not graph:
        return _compact_prompt(
            [
                _summarize_for_prompt(str(script_json.get("worldview") or "")),
                _summarize_for_prompt(str(script_json.get("core_plot") or "")),
            ]
        )
    nodes = _graph_nodes_by_id(graph)
    parts: list[str] = []
    for node in graph.get("nodes", []) if isinstance(graph.get("nodes"), list) else []:
        if not isinstance(node, dict):
            continue
        kind = str(node.get("kind") or "")
        if kind not in {"script", "location", "scene", "event", "timeline_event", "artifact", "clue", "organization"}:
            continue
        compact = _compact_graph_node(node)
        text = _compact_prompt([compact.get("kind", ""), compact.get("label", ""), compact.get("description", "")])
        if text:
            parts.append(_summarize_for_prompt(text))
        for relation in _relations_for_node(graph, str(node.get("id") or ""), nodes)[:3]:
            relation_text = _compact_prompt(
                [
                    str(relation.get("type") or ""),
                    str(relation.get("other_kind") or ""),
                    str(relation.get("other_label") or ""),
                    str(relation.get("description") or ""),
                ]
            )
            if relation_text:
                parts.append(_summarize_for_prompt(relation_text))
        if len(parts) >= 12:
            break
    return _compact_prompt(list(dict.fromkeys(parts))[:12])


def _is_script_graph_document(value: object) -> bool:
    return isinstance(value, dict) and isinstance(value.get("nodes"), list) and isinstance(value.get("edges"), list)


def _graph_nodes(graph: dict, kinds: set[str]) -> list[dict]:
    return [
        node
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and str(node.get("kind") or "") in kinds
    ]


def _graph_worldview(graph: dict) -> str:
    script = next(iter(_graph_nodes(graph, {"script"})), {})
    properties = script.get("properties") if isinstance(script.get("properties"), dict) else {}
    return str(properties.get("public_background") or properties.get("description") or graph.get("title") or "")


def _graph_core_plot(graph: dict) -> str:
    script = next(iter(_graph_nodes(graph, {"script"})), {})
    properties = script.get("properties") if isinstance(script.get("properties"), dict) else {}
    event_labels = [str(node.get("label") or "") for node in _graph_nodes(graph, {"event", "timeline_event"})[:8]]
    return str(properties.get("core_plot") or " ".join(item for item in event_labels if item))


def _graph_context_for_character(contexts: dict[str, dict], character: SandboxNPC | ScriptCharacterSheet) -> dict:
    for value in [getattr(character, "id", ""), getattr(character, "name", "")]:
        context = contexts.get(_norm_key(str(value or "")))
        if context:
            return context
    return {}


def _story_graph_scene_records(script_json: dict) -> list[dict]:
    graph = _graph_document_from_script_json(script_json)
    nodes = _graph_nodes_by_id(graph)
    records: list[dict] = []
    for graph_order, node in enumerate(graph.get("nodes", []) if isinstance(graph.get("nodes"), list) else []):
        if not isinstance(node, dict):
            continue
        kind = str(node.get("kind") or "")
        if kind not in {"location", "scene", "event", "timeline_event"}:
            continue
        records.append(
            {
                "name": str(node.get("label") or node.get("id") or ""),
                "kind": kind,
                "description": str((node.get("properties") or {}).get("description") or (node.get("properties") or {}).get("text") or ""),
                "graph_node": _compact_graph_node(node),
                "graph_relations": _relations_for_node(graph, str(node.get("id") or ""), nodes),
                "_graph_order": graph_order,
            }
        )
    def priority(record: dict) -> tuple[int, int]:
        graph_node = record.get("graph_node") if isinstance(record.get("graph_node"), dict) else {}
        node_id = str(graph_node.get("id") or "")
        name = str(record.get("name") or "")
        kind = str(record.get("kind") or "")
        is_dialogue_beat = "_beat_" in node_id or " · " in name
        is_opening_scene = node_id in {"start", "scene:start"}
        if is_opening_scene and not is_dialogue_beat:
            rank = 0
        elif kind == "location":
            rank = 1
        elif kind == "scene" and not is_dialogue_beat:
            rank = 2
        elif kind in {"event", "timeline_event"} and not is_dialogue_beat:
            rank = 3
        else:
            rank = 4
        return rank, int(record.get("_graph_order") or 0)

    ordered = sorted(records, key=priority)
    for record in ordered:
        record.pop("_graph_order", None)
    return ordered


def _summarize_graph_context(context: dict) -> str:
    if not context:
        return ""
    node = context.get("node") if isinstance(context.get("node"), dict) else {}
    parts = [str(node.get("kind") or ""), str(node.get("label") or ""), str(node.get("description") or "")]
    for relation in context.get("relations", [])[:6] if isinstance(context.get("relations"), list) else []:
        if not isinstance(relation, dict):
            continue
        parts.append(
            " ".join(
                str(relation.get(key) or "")
                for key in ["direction", "type", "other_label", "description"]
                if relation.get(key)
            )
        )
    return "graph relations: " + "; ".join(part for part in parts if part)


def _graph_document_from_script_json(script_json: dict) -> dict:
    graph = script_json.get("script_graph")
    if isinstance(graph, dict) and graph.get("nodes") is not None and graph.get("edges") is not None:
        return graph
    summary = script_json.get("story_graph_summary")
    if isinstance(summary, dict) and summary.get("nodes") is not None and summary.get("edges") is not None:
        return summary
    return {}


def _graph_nodes_by_id(graph: dict) -> dict[str, dict]:
    return {
        str(node.get("id") or ""): node
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and node.get("id")
    }


def _relations_for_node(graph: dict, node_id: str, nodes: dict[str, dict]) -> list[dict]:
    relations: list[dict] = []
    for edge in graph.get("edges", []) if isinstance(graph.get("edges"), list) else []:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source != node_id and target != node_id:
            continue
        other_id = target if source == node_id else source
        other = nodes.get(other_id, {})
        properties = edge.get("properties") if isinstance(edge.get("properties"), dict) else {}
        relations.append(
            {
                "direction": "out" if source == node_id else "in",
                "type": str(edge.get("type") or ""),
                "other_id": other_id,
                "other_kind": str(other.get("kind") or ""),
                "other_label": str(other.get("label") or other_id),
                "description": str(properties.get("description") or properties.get("text") or ""),
                "confidence": str(properties.get("confidence") or ""),
            }
        )
    return relations[:12]


def _compact_graph_node(node: dict) -> dict:
    properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
    return {
        "id": str(node.get("id") or ""),
        "kind": str(node.get("kind") or ""),
        "label": str(node.get("label") or ""),
        "source_id": str(properties.get("source_id") or ""),
        "description": str(properties.get("description") or properties.get("text") or ""),
    }


def _norm_key(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _parse_prompt_bundle(raw: str, fallback: dict) -> dict:
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
        return fallback
    if not isinstance(data, dict):
        return fallback
    if not isinstance(data.get("style_guide"), dict) or not isinstance(data.get("assets"), list):
        return fallback
    return data


def _character_details(character: SandboxNPC | ScriptCharacterSheet) -> str:
    if isinstance(character, SandboxNPC):
        return _strip_sensitive_text(" ".join(part for part in [character.role, character.personality] if part))
    return _strip_sensitive_text(
        " ".join(
            part
            for part in [
                character.role,
                character.public_info,
                character.motive,
            ]
            if part
        )
    )


def _extract_theme_phrases_from_json(script_json: dict) -> list[str]:
    phrases: list[str] = []
    for key in ["title", "worldview", "core_plot"]:
        for line in str(script_json.get(key) or "").splitlines():
            clean = line.strip(" -|；;，,。")
            if not clean:
                continue
            if any(token in clean for token in ["世界", "时代", "规则", "体系", "地点", "山", "谷", "村", "门", "功法", "身份"]):
                phrases.append(_summarize_for_prompt(clean))
    for item in script_json.get("constraints", []):
        phrases.append(_summarize_for_prompt(str(item)))
    return [item for item in dict.fromkeys(phrases) if item][:10]


def _extract_visual_terms_from_json(script_json: dict) -> list[str]:
    text = " ".join(
        [
            str(script_json.get("worldview") or ""),
            str(script_json.get("core_plot") or ""),
            " ".join(str(item) for item in script_json.get("locations", [])),
            " ".join(str(item) for item in script_json.get("constraints", [])),
            " ".join(
                " ".join(str(value) for value in character.values())
                for character in script_json.get("characters", [])
                if isinstance(character, dict)
            ),
        ]
    )
    terms = re.findall(r"[\u4e00-\u9fffA-Za-z0-9·（）()]{2,18}", text)
    blocked = {"编号", "说明", "状态", "重要性", "关系类型", "信任度", "冲突"}
    result = []
    for term in terms:
        if term in blocked or term.isdigit():
            continue
        if any(token in term for token in ["山", "谷", "崖", "村", "镇", "门", "功", "瓶", "药", "古代", "农", "灰色", "长袍"]):
            result.append(term)
    return list(dict.fromkeys(result))[:16]


def _extract_era(text: str) -> str:
    if "古代" in text:
        return "ancient historical setting"
    if "现代" in text:
        return "modern setting"
    if "民国" in text:
        return "Republic-era Chinese setting"
    return "setting inferred from the script world background"


def _extract_power_system(text: str) -> str:
    terms = []
    for token, phrase in [
        ("修仙", "cultivation world elements"),
        ("仙凡", "separation between mortal world and hidden immortal world"),
        ("灵根", "spiritual-root talent rules"),
        ("功法", "martial and cultivation technique system"),
        ("武林", "martial sect society"),
        ("案件", "mystery investigation structure"),
    ]:
        if token in text:
            terms.append(phrase)
    return ", ".join(terms) or "world rules inferred from the script"


def _derive_palette(text: str) -> str:
    palette = []
    if any(token in text for token in ["山", "谷", "村", "农", "药园"]):
        palette.extend(["natural mountain greens", "earthy village browns"])
    if any(token in text for token in ["灰色", "长袍", "石", "崖"]):
        palette.extend(["weathered grey cloth and stone"])
    if any(token in text for token in ["绿液", "翠绿", "瓶"]):
        palette.extend(["subtle jade green accents"])
    if any(token in text for token in ["夜", "异象"]):
        palette.extend(["soft mysterious night glow used sparingly"])
    return ", ".join(dict.fromkeys(palette)) or "palette inferred from the provided setting"


def _derive_material_language(text: str) -> str:
    materials = []
    if any(token in text for token in ["农", "村", "茅屋", "五里沟"]):
        materials.extend(["plain linen cloth", "mud walls", "thatched roofs", "farming village textures"])
    if any(token in text for token in ["七玄门", "弟子", "门派", "武技"]):
        materials.extend(["simple sect training robes", "cloth belts", "worn boots"])
    if any(token in text for token in ["药", "大夫", "谷"]):
        materials.extend(["herb garden props", "wooden medicine shelves"])
    if any(token in text for token in ["崖", "山", "彩霞山"]):
        materials.extend(["weathered cliff stone", "mountain mist"])
    return ", ".join(dict.fromkeys(materials)) or "materials inferred from the script descriptions"


def _summarize_for_prompt(text: str) -> str:
    translated = _replace_known_terms(_strip_sensitive_text(text))
    translated = re.sub(r"[|#*_]+", " ", translated)
    translated = re.sub(r"\s+", " ", translated).strip(" ;,。")
    return translated[:420]


def _replace_known_terms(text: str) -> str:
    replacements = {
        "架空古代": "fictional ancient Chinese setting",
        "古代": "ancient Chinese",
        "人界": "mortal realm",
        "修仙界": "hidden cultivation world",
        "凡人界": "mortal world",
        "仙凡有别": "strict separation between immortals and mortals",
        "茅屋": "thatched cottage",
        "药园": "herb garden",
        "大夫": "doctor",
        "灰色长袍": "grey long robe",
        "长袍": "long robe",
        "皮肤黝黑": "dark skin",
        "相貌普通": "ordinary face",
        "农家子弟": "farm boy",
        "谨慎": "cautious",
        "坚韧": "resilient",
        "憨厚": "honest",
        "老实": "simple and sincere",
        "勤奋": "hardworking",
        "豪爽": "bold and open-hearted",
        "讲义气": "loyal",
        "清癯": "thin and refined face",
        "胡须": "short beard",
        "瘦削": "slender body",
        "功法": "cultivation technique",
        "武技": "martial technique",
        "灵根": "spiritual root",
        "掌天瓶": "mysterious small jade-green bottle",
        "翠绿色": "jade green",
    }
    result = text
    for source, target in replacements.items():
        result = result.replace(source, target)
    return result


def _scene_description_from_json(scene: str, script_json: dict) -> str:
    descriptions: list[str] = []
    for key in ["core_plot", "worldview"]:
        lines = [line.strip() for line in str(script_json.get(key) or "").splitlines() if scene and scene in line]
        descriptions.extend(lines[:3])
    for clue in script_json.get("clues", []):
        if not isinstance(clue, dict):
            continue
        if scene in str(clue.get("location") or "") or scene in str(clue.get("source") or ""):
            descriptions.append(" ".join(str(clue.get(key) or "") for key in ["title", "content", "source", "location"]))
    return " ".join(descriptions)[:500]


def _prompt_blocked_terms(script_json: dict) -> list[str]:
    terms: list[str] = []
    for character in script_json.get("characters", []):
        if isinstance(character, dict):
            terms.append(str(character.get("name") or ""))
    terms.extend(str(location) for location in script_json.get("locations", []))
    for value in list(terms):
        terms.extend(part for part in re.split(r"[（）()·、/]", value) if len(part) >= 2)
    return [term for term in dict.fromkeys(term.strip() for term in terms) if term]


def _dedupe_scene_records(records: list[dict]) -> list[dict]:
    deduped: dict[str, dict] = {}
    for record in records:
        name = str(record.get("name") or "").strip()
        if not name or name in deduped:
            continue
        deduped[name] = record
    return list(deduped.values())


def _strip_sensitive_text(text: str) -> str:
    blocked = ["夺舍", "真凶", "凶手", "杀", "尸体", "备用肉身", "曲魂"]
    result = text
    for word in blocked:
        result = result.replace(word, "")
    return result


def _sanitize_prompt_text(text: str, blocked_terms: list[str] | None = None) -> str:
    result = _strip_sensitive_text(text)
    for term in blocked_terms or []:
        if term:
            result = result.replace(term, "")
    result = re.sub(r"\s+", " ", result)
    result = re.sub(r"，\s*，", "，", result)
    result = re.sub(r",\s*,", ",", result)
    return result.strip(" ,，;；")


def _compact_prompt(parts: list[str]) -> str:
    return ", ".join(part.strip(" ,") for part in parts if part and part.strip(" ,"))


def _join_negative_prompt(base: str, extra: str) -> str:
    return ", ".join(part.strip(" ,") for part in [base, extra] if part and part.strip(" ,"))


def _safe_id(value: str, fallback: str) -> str:
    if not value:
        return fallback
    ascii_value = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower()
    if ascii_value:
        return ascii_value[:64]
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"{fallback}_{digest}"
