from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agents.script_decomposition.schema import ScriptGraphDocument
from app.worlds.sandbox.decomposition_store import ScriptDecompositionArtifactStore


GRAPH_DATA_DIR = Path("data") / "script_graphs"


class ScriptGraphStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or GRAPH_DATA_DIR
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, graph: ScriptGraphDocument, title_hint: str = "") -> dict[str, Any]:
        safe_name = self._safe_name(title_hint or graph.title or graph.graph_id)
        if not safe_name:
            safe_name = "script_graph"
        path = self.root / f"{safe_name}.script_graph.json"
        payload = graph.model_dump()
        artifact = {
            "artifact_id": safe_name,
            "title": graph.title or title_hint,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "graph_path": str(path),
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edges),
            "source_artifact_id": graph.source_artifact_id,
        }
        payload["artifact"] = artifact
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return artifact

    def list(self) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.script_graph.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            artifact_id = path.name[: -len(".script_graph.json")]
            payload = _read_json_if_exists(path) or {}
            artifact = payload.get("artifact") if isinstance(payload, dict) else None
            if not isinstance(artifact, dict):
                artifact = {
                    "artifact_id": artifact_id,
                    "title": payload.get("title", artifact_id) if isinstance(payload, dict) else artifact_id,
                    "graph_path": str(path),
                    "node_count": len(payload.get("nodes", [])) if isinstance(payload, dict) else 0,
                    "edge_count": len(payload.get("edges", [])) if isinstance(payload, dict) else 0,
                }
            artifact["updated_at"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            artifacts.append(artifact)
        return artifacts

    def load(self, artifact_id: str) -> dict[str, Any]:
        safe_name = self._safe_name(artifact_id)
        if safe_name != artifact_id:
            raise ValueError("invalid script graph artifact id")
        path = self.root / f"{safe_name}.script_graph.json"
        if not path.exists():
            raise FileNotFoundError(f"script graph artifact not found: {artifact_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _safe_name(self, value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip()).strip(" ._")
        cleaned = re.sub(r"\s+", "_", cleaned)
        return cleaned[:80]


def _read_json_if_exists(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = ["ScriptDecompositionArtifactStore", "ScriptGraphStore"]
