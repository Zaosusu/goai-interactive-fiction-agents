from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.worlds.sandbox.models import ScriptDecompositionResult


class ScriptGraphNode(BaseModel):
    id: str
    kind: str
    label: str
    properties: dict[str, Any] = Field(default_factory=dict)


class ScriptGraphEdge(BaseModel):
    id: str
    source: str
    target: str
    type: str
    properties: dict[str, Any] = Field(default_factory=dict)


class ScriptGraphDocument(BaseModel):
    graph_id: str
    title: str = ""
    source_artifact_id: str = ""
    schema_version: str = "script_graph.v1"
    ontology: dict[str, Any] = Field(default_factory=dict)
    nodes: list[ScriptGraphNode] = Field(default_factory=list)
    edges: list[ScriptGraphEdge] = Field(default_factory=list)
    indexes: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScriptGraphBuildRequest(BaseModel):
    decomposition: ScriptDecompositionResult | None = None
    artifact_id: str = ""
    title: str = ""
    save: bool = True


class ScriptGraphBuildResponse(BaseModel):
    graph: ScriptGraphDocument
    artifact: dict[str, Any] | None = None

