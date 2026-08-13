from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.worlds.sandbox.models import ScriptDecompositionBuildResponse


DATA_DIR = Path("data") / "script_decompositions"


class ScriptDecompositionArtifactStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or DATA_DIR
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, result: ScriptDecompositionBuildResponse, title_hint: str = "") -> dict[str, Any]:
        safe_name = self._safe_name(title_hint or (result.decomposition.title if result.decomposition else ""))
        if not safe_name:
            safe_name = "script_decomposition"
        paths = {
            "decomposition_path": self.root / f"{safe_name}.decomposition.json",
            "report_path": self.root / f"{safe_name}.report.json",
            "response_path": self.root / f"{safe_name}.response.json",
        }
        payload = _redact_sensitive(result.model_dump())
        artifact = {
            "artifact_id": safe_name,
            "title": title_hint or (result.decomposition.title if result.decomposition else ""),
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "decomposition_path": str(paths["decomposition_path"]),
            "report_path": str(paths["report_path"]),
            "response_path": str(paths["response_path"]),
        }
        payload["artifact"] = artifact
        paths["decomposition_path"].write_text(
            json.dumps(payload.get("decomposition") or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        paths["report_path"].write_text(
            json.dumps(payload.get("report") or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        paths["response_path"].write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return artifact

    def list(self) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.decomposition.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            artifact_id = path.name[: -len(".decomposition.json")]
            title = artifact_id
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                title = str(payload.get("title") or title)
            except Exception:
                payload = {}
            story_graph = payload.get("story_graph", {}) if isinstance(payload, dict) else {}
            entities = story_graph.get("entities", []) if isinstance(story_graph, dict) else []
            relations = story_graph.get("relations", []) if isinstance(story_graph, dict) else []
            artifacts.append(
                {
                    "artifact_id": artifact_id,
                    "title": title,
                    "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                    "decomposition_path": str(path),
                    "report_path": str(self.root / f"{artifact_id}.report.json"),
                    "response_path": str(self.root / f"{artifact_id}.response.json"),
                    "node_count": len(entities) if isinstance(entities, list) else 0,
                    "edge_count": len(relations) if isinstance(relations, list) else 0,
                    "evidence_count": _count_story_graph_evidence(story_graph),
                }
            )
        return artifacts

    def load(self, artifact_id: str) -> dict[str, Any]:
        safe_name = self._safe_name(artifact_id)
        if safe_name != artifact_id:
            raise ValueError("invalid script decomposition artifact id")
        decomposition_path = self.root / f"{safe_name}.decomposition.json"
        report_path = self.root / f"{safe_name}.report.json"
        response_path = self.root / f"{safe_name}.response.json"
        if not decomposition_path.exists():
            raise FileNotFoundError(f"script decomposition artifact not found: {artifact_id}")

        decomposition = json.loads(decomposition_path.read_text(encoding="utf-8"))
        report = _read_json_if_exists(report_path) or {}
        response = _read_json_if_exists(response_path) or {}
        artifact = response.get("artifact") if isinstance(response, dict) else None
        if not isinstance(artifact, dict):
            artifact = {
                "artifact_id": safe_name,
                "title": decomposition.get("title", safe_name) if isinstance(decomposition, dict) else safe_name,
                "decomposition_path": str(decomposition_path),
                "report_path": str(report_path),
                "response_path": str(response_path),
            }
        return {
            "artifact": artifact,
            "decomposition": decomposition,
            "report": report,
            "response": response,
        }

    def _safe_name(self, value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip()).strip(" ._")
        cleaned = re.sub(r"\s+", "_", cleaned)
        return cleaned[:80]


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"api_key", "authorization"} or lowered.endswith("_api_key"):
                redacted[key] = "[redacted]" if item else ""
            elif lowered in {"base_url", "api_base_url"}:
                redacted[key] = "[redacted]" if item else ""
            else:
                redacted[key] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _count_story_graph_evidence(story_graph: Any) -> int:
    if not isinstance(story_graph, dict):
        return 0
    total = 0
    for key in ("entities", "relations"):
        items = story_graph.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            evidence = item.get("evidence", []) if isinstance(item, dict) else []
            if isinstance(evidence, list):
                total += len(evidence)
    return total


def _read_json_if_exists(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
