from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.worlds.sandbox.models import VisualAssetGenerationResult, VisualAssetPlan


VISUAL_ASSET_DATA_DIR = Path("data") / "visual_assets"
VISUAL_ASSET_OUTPUT_DIR = Path("output") / "visual_assets"


class VisualAssetArtifactStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or VISUAL_ASSET_DATA_DIR
        self.root.mkdir(parents=True, exist_ok=True)

    def save_plan(self, plan: VisualAssetPlan) -> dict[str, Any]:
        artifact_id = self._safe_name(plan.plan_id or plan.title or "visual_assets")
        path = self.root / f"{artifact_id}.visual_plan.json"
        payload = plan.model_dump()
        artifact = self._summary_from_plan_payload(payload, artifact_id, path)
        payload["artifact"] = artifact
        path.write_text(json.dumps(_redact_sensitive(payload), ensure_ascii=False, indent=2), encoding="utf-8")
        return artifact

    def save_result(self, result: VisualAssetGenerationResult) -> dict[str, Any]:
        artifact_id = self._safe_name(result.plan.plan_id or result.plan.title or "visual_assets")
        path = self.root / f"{artifact_id}.visual_plan.json"
        payload = result.plan.model_dump()
        payload["result"] = result.model_dump()
        artifact = self._summary_from_result_payload(payload, artifact_id, path)
        payload["artifact"] = artifact
        path.write_text(json.dumps(_redact_sensitive(payload), ensure_ascii=False, indent=2), encoding="utf-8")
        return artifact

    def list(self) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for path in self.root.glob("*.visual_plan.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            artifact_id = self._artifact_id_from_path(path)
            artifact = self._summary_from_result_payload(payload, artifact_id, path)
            artifact["updated_at"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            artifacts.append(artifact)
        return _latest_artifacts(artifacts)

    def load(self, artifact_id: str) -> dict[str, Any]:
        safe_name = self._safe_name(artifact_id)
        if safe_name != artifact_id:
            raise ValueError("invalid visual asset artifact id")
        plan_path = self.root / f"{safe_name}.visual_plan.json"
        if plan_path.exists():
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
            artifact = self._summary_from_result_payload(payload, safe_name, plan_path)
            return {"artifact": artifact, "plan": _strip_artifact(_plan_payload(payload)), "result": payload.get("result") if isinstance(payload.get("result"), dict) else None}

        raise FileNotFoundError(f"visual asset artifact not found: {artifact_id}")

    def list_runs(self, world_id: str = "", title: str = "", output_root: str = "output/visual_assets") -> list[dict[str, Any]]:
        runs_root = self._runs_root(world_id, title, output_root)
        if not runs_root.exists():
            return []
        runs: list[dict[str, Any]] = []
        for path in runs_root.iterdir():
            if not path.is_dir():
                continue
            image_paths = sorted(
                item
                for item in path.rglob("*")
                if item.is_file() and item.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            )
            stat = path.stat()
            runs.append(
                {
                    "run_id": path.name,
                    "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "asset_count": len(image_paths),
                    "path": str(path),
                    "visual_plan_artifact_id": self._matching_plan_artifact_id(world_id=world_id, title=title),
                    "assets": [str(item) for item in image_paths],
                }
            )
        return sorted(runs, key=lambda item: item.get("updated_at", ""), reverse=True)

    def load_run(self, run_id: str, world_id: str = "", title: str = "", output_root: str = "output/visual_assets") -> dict[str, Any]:
        run_path = self._run_path(run_id, world_id, title, output_root)
        if not run_path.exists():
            raise FileNotFoundError(f"visual asset run not found: {run_id}")
        images = [
            path
            for path in sorted(run_path.rglob("*"))
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ]
        stat = run_path.stat()
        plan_payload = self._matching_plan_payload(world_id=world_id, title=title)
        assets = [self._run_asset_from_path(path, plan_payload, run_path.name) for path in images]
        return {
            "run_id": run_path.name,
            "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "asset_count": len(images),
            "path": str(run_path),
            "visual_plan": _strip_artifact(plan_payload) if plan_payload else None,
            "visual_plan_artifact": self._summary_from_plan_payload(
                plan_payload,
                self._safe_name(str(plan_payload.get("plan_id") or plan_payload.get("title") or "visual_assets")),
                self.root / f"{self._safe_name(str(plan_payload.get('plan_id') or plan_payload.get('title') or 'visual_assets'))}.visual_plan.json",
            )
            if plan_payload
            else None,
            "assets": assets,
        }

    def delete_run(self, run_id: str, world_id: str = "", title: str = "", output_root: str = "output/visual_assets") -> dict[str, Any]:
        run_path = self._run_path(run_id, world_id, title, output_root)
        if not run_path.exists():
            raise FileNotFoundError(f"visual asset run not found: {run_id}")
        shutil.rmtree(run_path)
        return {"status": "deleted", "run_id": run_id, "path": str(run_path)}

    def _artifact_id_from_path(self, path: Path) -> str:
        return path.name[: -len(".visual_plan.json")]

    def _summary_from_plan_payload(self, payload: dict[str, Any], artifact_id: str, path: Path) -> dict[str, Any]:
        assets = payload.get("assets", []) if isinstance(payload, dict) else []
        return {
            "artifact_id": artifact_id,
            "kind": "visual_plan",
            "title": payload.get("title") or artifact_id,
            "plan_id": payload.get("plan_id") or "",
            "world_id": payload.get("world_id") or "",
            "asset_count": len(assets) if isinstance(assets, list) else 0,
            "generated_count": 0,
            "failed_count": 0,
            "path": str(path),
        }

    def _summary_from_result_payload(self, payload: dict[str, Any], artifact_id: str, path: Path) -> dict[str, Any]:
        artifact = self._summary_from_plan_payload(_plan_payload(payload), artifact_id, path)
        result = payload.get("result") if isinstance(payload.get("result"), dict) else None
        if result:
            artifact["generated_count"] = len(result.get("generated", [])) if isinstance(result.get("generated"), list) else 0
            artifact["failed_count"] = len(result.get("failed", [])) if isinstance(result.get("failed"), list) else 0
        return artifact

    def _run_asset_from_path(self, path: Path, plan_payload: dict[str, Any] | None, run_id: str) -> dict[str, Any]:
        planned = self._matching_asset_for_path(path, plan_payload)
        asset = dict(planned or {})
        asset.setdefault("id", path.stem)
        asset.setdefault("kind", path.parent.name[:-1] if path.parent.name in {"characters", "scenes"} else path.parent.name)
        asset.setdefault("display_name", planned.get("display_name") if planned else path.stem)
        asset["output_path"] = str(path)
        asset["status"] = "generated"
        metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
        asset["metadata"] = {**metadata, "generation_run_id": run_id}
        return asset

    def _matching_asset_for_path(self, path: Path, plan_payload: dict[str, Any] | None) -> dict[str, Any] | None:
        assets = plan_payload.get("assets", []) if isinstance(plan_payload, dict) and isinstance(plan_payload.get("assets"), list) else []
        normalized = _norm_path(str(path))
        stem = path.stem
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            if _norm_path(str(asset.get("output_path") or "")) == normalized:
                return asset
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            planned_stem = Path(str(asset.get("output_path") or asset.get("id") or "")).stem
            if planned_stem and planned_stem == stem:
                return asset
        return None

    def _safe_name(self, value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip()).strip(" ._")
        cleaned = re.sub(r"\s+", "_", cleaned)
        return cleaned[:100]

    def _runs_root(self, world_id: str, title: str, output_root: str) -> Path:
        root = Path(output_root or VISUAL_ASSET_OUTPUT_DIR)
        plan_root = root / _output_safe_id(world_id or title, "visual_assets")
        resolved_root = root.resolve()
        resolved_runs = (plan_root / "runs").resolve()
        if resolved_root not in resolved_runs.parents:
            raise ValueError("invalid visual asset output root")
        return resolved_runs

    def _run_path(self, run_id: str, world_id: str, title: str, output_root: str) -> Path:
        safe_run_id = _output_safe_id(run_id, "run")
        if safe_run_id != run_id:
            raise ValueError("invalid visual asset run id")
        runs_root = self._runs_root(world_id, title, output_root)
        run_path = (runs_root / safe_run_id).resolve()
        if runs_root.resolve() not in run_path.parents:
            raise ValueError("invalid visual asset run path")
        return run_path

    def _matching_plan_artifact_id(self, world_id: str = "", title: str = "") -> str:
        payload = self._matching_plan_payload(world_id=world_id, title=title)
        if not payload:
            return ""
        return self._safe_name(str(payload.get("plan_id") or payload.get("title") or "visual_assets"))

    def _matching_plan_payload(self, world_id: str = "", title: str = "") -> dict[str, Any] | None:
        candidates: list[tuple[float, dict[str, Any]]] = []
        for path in self.root.glob("*.visual_plan.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if world_id and str(payload.get("world_id") or "") != world_id:
                continue
            if title and str(payload.get("title") or "") != title:
                continue
            candidates.append((path.stat().st_mtime, payload))
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]


def _strip_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "artifact"}


def _plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("result"), dict) and isinstance(payload["result"].get("plan"), dict):
        return payload["result"]["plan"]
    return {key: value for key, value in payload.items() if key not in {"artifact", "result"}}


def _norm_path(value: str) -> str:
    return str(value or "").replace("\\", "/").lower().lstrip("./")


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"api_key", "authorization"} or lowered.endswith("_api_key"):
                redacted[key] = "[redacted]" if item else ""
            else:
                redacted[key] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _latest_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for artifact in sorted(artifacts, key=lambda item: item.get("updated_at", ""), reverse=True):
        key = (
            str(artifact.get("kind") or ""),
            str(artifact.get("title") or ""),
            str(artifact.get("world_id") or ""),
        )
        if key not in latest:
            latest[key] = artifact
    return sorted(latest.values(), key=lambda item: item.get("updated_at", ""), reverse=True)


def _output_safe_id(value: str, fallback: str) -> str:
    if not value:
        return fallback
    ascii_value = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower()
    if ascii_value:
        return ascii_value[:64]
    import hashlib

    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"{fallback}_{digest}"
