from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from typing import Any

from app.agents.script_decomposition.schema import ScriptGraphDocument, ScriptGraphEdge, ScriptGraphNode
from app.worlds.sandbox.models import ScriptDecompositionResult


class ScriptGraphCompiler:
    """Deterministically compiles script_json into a property graph artifact."""

    def compile(
        self,
        decomposition: ScriptDecompositionResult | dict[str, Any],
        source_artifact_id: str = "",
    ) -> ScriptGraphDocument:
        script = (
            decomposition
            if isinstance(decomposition, ScriptDecompositionResult)
            else ScriptDecompositionResult.model_validate(decomposition)
        )
        graph_id = _node_id("script", script.script_id or script.title or source_artifact_id or "script")
        builder = _GraphBuilder(graph_id=graph_id, title=script.title, source_artifact_id=source_artifact_id)
        if script.story_graph.entities or script.story_graph.relations:
            return self._compile_story_graph_facts(builder, script, graph_id)

        builder.add_node(
            graph_id,
            "script",
            script.title or source_artifact_id or "script",
            {
                "script_id": script.script_id,
                "script_type": script.script_type,
                "public_background": script.public_background,
                "core_plot": script.core_plot,
            },
        )

        truth_id = ""
        if script.truth:
            truth_id = _node_id("truth", script.truth)
            builder.add_node(truth_id, "truth", "truth", {"content": script.truth})
            builder.add_edge(graph_id, truth_id, "HAS_TRUTH")

        location_nodes = self._locations(builder, graph_id, script)
        character_nodes = self._characters(builder, graph_id, script, location_nodes)
        clue_nodes = self._clues(builder, graph_id, script, location_nodes, character_nodes, truth_id)
        self._timeline(builder, graph_id, script, location_nodes, character_nodes, clue_nodes)
        self._threads(builder, graph_id, script, character_nodes, clue_nodes)
        self._endings(builder, graph_id, script, truth_id)
        self._constraints(builder, graph_id, script)

        return builder.document()

    def _compile_story_graph_facts(
        self,
        builder: "_GraphBuilder",
        script: ScriptDecompositionResult,
        graph_id: str,
    ) -> ScriptGraphDocument:
        builder.add_node(
            graph_id,
            "script",
            script.title or script.script_id or "script",
            {
                "script_id": script.script_id,
                "script_type": script.script_type,
                "public_background": script.public_background,
                "core_plot": script.core_plot,
            },
        )
        entity_id_map: dict[str, str] = {}
        for entity in script.story_graph.entities:
            node_id = _node_id(entity.kind or "entity", entity.id or entity.name)
            entity_id_map[entity.id] = node_id
            entity_id_map[_norm(entity.id)] = node_id
            entity_id_map[entity.name] = node_id
            entity_id_map[_norm(entity.name)] = node_id
            for alias in entity.aliases:
                entity_id_map[alias] = node_id
                entity_id_map[_norm(alias)] = node_id
            builder.add_node(
                node_id,
                entity.kind or "entity",
                entity.name or entity.id,
                {
                    **entity.properties,
                    "source_id": entity.id,
                    "aliases": entity.aliases,
                    "description": entity.description,
                    "evidence": [item.model_dump() for item in entity.evidence],
                },
            )
            if node_id != graph_id:
                builder.add_edge(graph_id, node_id, f"HAS_{(entity.kind or 'ENTITY').upper()}")

        for relation in script.story_graph.relations:
            source_id = _resolve_story_node(entity_id_map, relation.source)
            target_id = _resolve_story_node(entity_id_map, relation.target)
            if not source_id or not target_id:
                continue
            builder.add_edge(
                source_id,
                target_id,
                relation.type or "RELATED_TO",
                {
                    **relation.properties,
                    "source_id": relation.id,
                    "description": relation.description,
                    "confidence": relation.confidence,
                    "evidence": [item.model_dump() for item in relation.evidence],
                },
            )
        document = builder.document()
        document.metadata.update(
            {
                "graph_source": "story_graph_facts",
                "uncertainties": list(script.story_graph.uncertainties),
                "contradictions": list(script.story_graph.contradictions),
            }
        )
        return document

    def _locations(self, builder: "_GraphBuilder", graph_id: str, script: ScriptDecompositionResult) -> dict[str, str]:
        values = list(script.locations)
        values.extend(character.location for character in script.characters if character.location)
        values.extend(clue.location for clue in script.clues if clue.location)
        location_nodes: dict[str, str] = {}
        for location in _dedupe(values):
            node_id = _node_id("location", location)
            location_nodes[_norm(location)] = node_id
            builder.add_node(node_id, "location", location)
            builder.add_edge(graph_id, node_id, "HAS_LOCATION")
        return location_nodes

    def _characters(
        self,
        builder: "_GraphBuilder",
        graph_id: str,
        script: ScriptDecompositionResult,
        location_nodes: dict[str, str],
    ) -> dict[str, str]:
        character_nodes: dict[str, str] = {}
        for character in script.characters:
            node_id = _node_id("character", character.id or character.name)
            character_nodes[_norm(character.id)] = node_id
            character_nodes[_norm(character.name)] = node_id
            builder.add_node(
                node_id,
                "character",
                character.name,
                {
                    "source_id": character.id,
                    "role": character.role,
                    "public_info": character.public_info,
                    "secret": character.secret,
                    "motive": character.motive,
                    "alibi": character.alibi,
                    "location": character.location,
                    "metadata": character.metadata,
                },
            )
            builder.add_edge(graph_id, node_id, "HAS_CHARACTER", {"role": character.role})
            location_id = location_nodes.get(_norm(character.location))
            if location_id:
                builder.add_edge(node_id, location_id, "LOCATED_AT")
        return character_nodes

    def _clues(
        self,
        builder: "_GraphBuilder",
        graph_id: str,
        script: ScriptDecompositionResult,
        location_nodes: dict[str, str],
        character_nodes: dict[str, str],
        truth_id: str,
    ) -> dict[str, str]:
        clue_nodes: dict[str, str] = {}
        for clue in script.clues:
            node_id = _node_id("clue", clue.id or clue.title)
            clue_nodes[_norm(clue.id)] = node_id
            clue_nodes[_norm(clue.title)] = node_id
            builder.add_node(
                node_id,
                "clue",
                clue.title,
                {
                    "source_id": clue.id,
                    "content": clue.content,
                    "source": clue.source,
                    "location": clue.location,
                    "owner": clue.owner,
                    "reveals": clue.reveals,
                    "trigger": clue.trigger,
                    "metadata": clue.metadata,
                },
            )
            builder.add_edge(graph_id, node_id, "HAS_CLUE")
            location_id = location_nodes.get(_norm(clue.location))
            if location_id:
                builder.add_edge(node_id, location_id, "FOUND_AT")
            owner_id = _lookup(character_nodes, clue.owner)
            if owner_id:
                builder.add_edge(node_id, owner_id, "OWNED_BY", {"owner_text": clue.owner})
            if truth_id and (clue.reveals or clue.content):
                builder.add_edge(node_id, truth_id, "REVEALS", {"text": clue.reveals or clue.content})
            for character_text, character_id in _mentioned_nodes(character_nodes, f"{clue.content} {clue.reveals} {clue.trigger}"):
                builder.add_edge(node_id, character_id, "MENTIONS_CHARACTER", {"matched_text": character_text})
        return clue_nodes

    def _timeline(
        self,
        builder: "_GraphBuilder",
        graph_id: str,
        script: ScriptDecompositionResult,
        location_nodes: dict[str, str],
        character_nodes: dict[str, str],
        clue_nodes: dict[str, str],
    ) -> None:
        previous_id = ""
        for index, event in enumerate(script.timeline, start=1):
            node_id = _node_id("event", f"{index}:{event}")
            builder.add_node(node_id, "timeline_event", event, {"order": index, "text": event})
            builder.add_edge(graph_id, node_id, "HAS_TIMELINE_EVENT", {"order": index})
            if previous_id:
                builder.add_edge(previous_id, node_id, "NEXT_EVENT")
            previous_id = node_id
            for matched, target_id in _mentioned_nodes(location_nodes, event):
                builder.add_edge(node_id, target_id, "OCCURS_AT", {"matched_text": matched})
            for matched, target_id in _mentioned_nodes(character_nodes, event):
                builder.add_edge(node_id, target_id, "INVOLVES_CHARACTER", {"matched_text": matched})
            for matched, target_id in _mentioned_nodes(clue_nodes, event):
                builder.add_edge(node_id, target_id, "INVOLVES_CLUE", {"matched_text": matched})

    def _threads(
        self,
        builder: "_GraphBuilder",
        graph_id: str,
        script: ScriptDecompositionResult,
        character_nodes: dict[str, str],
        clue_nodes: dict[str, str],
    ) -> None:
        for index, thread in enumerate(script.hidden_threads, start=1):
            node_id = _node_id("hidden_thread", f"{index}:{thread}")
            builder.add_node(node_id, "hidden_thread", thread, {"order": index, "text": thread})
            builder.add_edge(graph_id, node_id, "HAS_HIDDEN_THREAD", {"order": index})
            for matched, target_id in _mentioned_nodes(character_nodes, thread):
                builder.add_edge(node_id, target_id, "INVOLVES_CHARACTER", {"matched_text": matched})
            for matched, target_id in _mentioned_nodes(clue_nodes, thread):
                builder.add_edge(node_id, target_id, "INVOLVES_CLUE", {"matched_text": matched})

    def _endings(self, builder: "_GraphBuilder", graph_id: str, script: ScriptDecompositionResult, truth_id: str) -> None:
        for index, ending in enumerate(script.endings, start=1):
            node_id = _node_id("ending", ending.id or ending.title or str(index))
            builder.add_node(
                node_id,
                "ending",
                ending.title or f"ending {index}",
                {
                    "source_id": ending.id,
                    "condition": ending.condition,
                    "reveal": ending.reveal,
                    "metadata": ending.metadata,
                },
            )
            builder.add_edge(graph_id, node_id, "HAS_ENDING")
            if truth_id and ending.reveal:
                builder.add_edge(node_id, truth_id, "REVEALS", {"text": ending.reveal})

    def _constraints(self, builder: "_GraphBuilder", graph_id: str, script: ScriptDecompositionResult) -> None:
        for index, constraint in enumerate(script.constraints, start=1):
            node_id = _node_id("constraint", f"{index}:{constraint}")
            builder.add_node(node_id, "constraint", constraint, {"order": index, "text": constraint})
            builder.add_edge(graph_id, node_id, "HAS_CONSTRAINT", {"order": index})


class _GraphBuilder:
    def __init__(self, graph_id: str, title: str = "", source_artifact_id: str = "") -> None:
        self.graph_id = graph_id
        self.title = title
        self.source_artifact_id = source_artifact_id
        self.nodes: dict[str, ScriptGraphNode] = {}
        self.edges: dict[str, ScriptGraphEdge] = {}

    def add_node(self, node_id: str, kind: str, label: str, properties: dict[str, Any] | None = None) -> None:
        if node_id in self.nodes:
            existing = self.nodes[node_id]
            merged = {**existing.properties, **(properties or {})}
            self.nodes[node_id] = existing.model_copy(update={"properties": merged})
            return
        self.nodes[node_id] = ScriptGraphNode(id=node_id, kind=kind, label=label, properties=properties or {})

    def add_edge(self, source: str, target: str, edge_type: str, properties: dict[str, Any] | None = None) -> None:
        edge_id = _edge_id(source, target, edge_type, properties or {})
        if edge_id in self.edges:
            return
        self.edges[edge_id] = ScriptGraphEdge(
            id=edge_id,
            source=source,
            target=target,
            type=edge_type,
            properties=properties or {},
        )

    def document(self) -> ScriptGraphDocument:
        nodes = list(self.nodes.values())
        edges = list(self.edges.values())
        node_counts = Counter(node.kind for node in nodes)
        edge_counts = Counter(edge.type for edge in edges)
        adjacency: dict[str, list[dict[str, str]]] = defaultdict(list)
        for edge in edges:
            adjacency[edge.source].append({"type": edge.type, "target": edge.target})
        return ScriptGraphDocument(
            graph_id=self.graph_id,
            title=self.title,
            source_artifact_id=self.source_artifact_id,
            ontology={
                "node_kinds": sorted(node_counts),
                "edge_types": sorted(edge_counts),
                "shape": "property_graph",
                "storage_targets": ["json_artifact", "neo4j", "arangodb", "nebula_graph"],
            },
            nodes=nodes,
            edges=edges,
            indexes={
                "node_counts": dict(sorted(node_counts.items())),
                "edge_counts": dict(sorted(edge_counts.items())),
                "adjacency": dict(adjacency),
            },
            metadata={
                "compiled_by": "ScriptGraphCompiler",
                "source_schema": "script_decomposition",
                "graph_ready": True,
            },
        )


def _node_id(kind: str, value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip()).strip("_.-").lower()
    if cleaned and len(cleaned) >= 2:
        return f"{kind}:{cleaned[:80]}"
    digest = hashlib.sha1(str(value or kind).encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{digest}"


def _edge_id(source: str, target: str, edge_type: str, properties: dict[str, Any]) -> str:
    payload = f"{source}|{edge_type}|{target}|{sorted(properties.items())}"
    return f"edge:{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]}"


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _lookup(index: dict[str, str], value: str) -> str:
    return index.get(_norm(value), "")


def _resolve_story_node(index: dict[str, str], value: str) -> str:
    return index.get(value) or index.get(_norm(value)) or index.get(str(value or "").strip())


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _norm(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value.strip())
    return result


def _mentioned_nodes(index: dict[str, str], text: str) -> list[tuple[str, str]]:
    normalized_text = _norm(text)
    matches: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key, node_id in index.items():
        if not key or len(key) < 2 or node_id in seen:
            continue
        if key in normalized_text:
            seen.add(node_id)
            matches.append((key, node_id))
    return matches
