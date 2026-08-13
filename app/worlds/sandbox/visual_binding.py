from __future__ import annotations

import re
from typing import Any

from app.worlds.sandbox.models import SandboxNPC, SandboxWorldConfig


def attach_visual_bindings(config: SandboxWorldConfig) -> SandboxWorldConfig:
    return _attach_npc_portraits(config)


def _attach_npc_portraits(config: SandboxWorldConfig) -> SandboxWorldConfig:
    metadata = config.metadata if isinstance(config.metadata, dict) else {}
    assets = _character_visual_assets(metadata.get("visual_plan"), metadata.get("visual_result"))
    if not assets:
        return config
    updated_npcs: list[SandboxNPC] = []
    portrait_index: dict[str, dict[str, Any]] = {}
    changed = False
    for npc in config.npcs:
        asset = _match_npc_visual_asset(npc, assets)
        if not asset:
            updated_npcs.append(npc)
            continue
        portrait = {
            "asset_id": str(asset.get("id") or ""),
            "url": str(asset.get("output_path") or ""),
            "output_path": str(asset.get("output_path") or ""),
            "source_id": str(asset.get("source_id") or ""),
            "source_name": str(asset.get("source_name") or asset.get("display_name") or ""),
            "display_name": str(asset.get("display_name") or asset.get("source_name") or ""),
            "status": str(asset.get("status") or ""),
            "kind": str(asset.get("kind") or ""),
        }
        current = npc.portrait if isinstance(npc.portrait, dict) else {}
        if current != portrait:
            changed = True
        updated_npcs.append(npc.model_copy(update={"portrait": portrait}))
        portrait_index[npc.id] = portrait
    if not portrait_index:
        return config
    metadata_changed = metadata.get("npc_portraits") != portrait_index
    if not changed and not metadata_changed:
        return config
    return config.model_copy(update={"npcs": updated_npcs, "metadata": {**metadata, "npc_portraits": portrait_index}})


def _character_visual_assets(visual_plan: Any, visual_result: Any) -> list[dict[str, Any]]:
    generated = [asset for asset in (visual_result or {}).get("generated", []) if isinstance(asset, dict)] if isinstance(visual_result, dict) else []
    plan_assets = [asset for asset in (visual_plan or {}).get("assets", []) if isinstance(asset, dict)] if isinstance(visual_plan, dict) else []
    assets = generated or plan_assets
    return [
        asset
        for asset in assets
        if str(asset.get("kind") or "").lower() == "character"
        and str(asset.get("output_path") or "").strip()
        and str(asset.get("status") or "generated") != "failed"
    ]


def _match_npc_visual_asset(npc: SandboxNPC, assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    npc_keys = {_norm_match(npc.id), _norm_match(npc.name)}
    npc_keys = {key for key in npc_keys if key}
    for asset in assets:
        asset_keys = {
            _norm_match(str(asset.get("source_id") or "")),
            _norm_match(str(asset.get("source_name") or "")),
            _norm_match(str(asset.get("display_name") or "")),
            _norm_match(str(asset.get("id") or "")),
        }
        if npc_keys & {key for key in asset_keys if key}:
            return asset
    for asset in assets:
        label = _norm_match(" ".join(str(asset.get(key) or "") for key in ("id", "source_id", "source_name", "display_name")))
        if any(key and key in label for key in npc_keys):
            return asset
    return None


def _norm_match(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "").lower())
