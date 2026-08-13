from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.core.image_generation import ImageGenerationProviderConfig
from app.core.model_config import LLMProviderConfig


class SandboxNPC(BaseModel):
    id: str
    name: str
    role: str = "NPC"
    personality: str = ""
    goals: list[str] = Field(default_factory=list)
    location: str = ""
    locations: list[str] = Field(default_factory=list)
    portrait: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_locations(self) -> "SandboxNPC":
        values = [str(item).strip() for item in self.locations if str(item or "").strip()]
        self.locations = list(dict.fromkeys(values))
        if not self.location and self.locations:
            self.location = self.locations[0]
        return self


class SandboxTask(BaseModel):
    id: str
    title: str
    description: str = ""
    status: str = "pending"
    completion: dict[str, Any] = Field(default_factory=dict)


class SandboxAction(BaseModel):
    id: str
    label: str
    description: str = ""
    effect: dict[str, Any] = Field(default_factory=dict)


class SandboxWorldConfig(BaseModel):
    world_id: str
    name: str
    description: str = ""
    lore: str = ""
    opening_scene: str = ""
    player: dict[str, Any] = Field(default_factory=dict)
    npcs: list[SandboxNPC] = Field(default_factory=list)
    story_goals: list[str] = Field(default_factory=list)
    tasks: list[SandboxTask] = Field(default_factory=list)
    actions: list[SandboxAction] = Field(default_factory=list)
    initial_memories: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorldGenerationThoughts(BaseModel):
    text: str = ""
    reasoning: str = ""
    plan: list[str] = Field(default_factory=list)
    criticism: str = ""
    speak: str = ""


class WorldGenerationResponse(BaseModel):
    thoughts: WorldGenerationThoughts = Field(default_factory=WorldGenerationThoughts)
    world: SandboxWorldConfig
    validation_notes: list[str] = Field(default_factory=list)


class WorldSummary(BaseModel):
    world_id: str
    name: str
    description: str = ""
    kind: str = "sandbox"
    created_at: str = ""
    updated_at: str = ""


class WorldTemplateSummary(BaseModel):
    id: str
    name: str
    description: str = ""
    structure_prompt: str = ""
    enabled: bool = True
    sort_order: int = 100


class WorldGenerateRequest(BaseModel):
    template: str = "freeform"
    theme: str = ""
    player_name: str = "主角"
    world_name: str = ""
    complexity: str = "medium"
    min_npcs: int | None = Field(default=None, ge=1)
    min_tasks: int | None = Field(default=None, ge=1)
    min_actions: int | None = Field(default=None, ge=1)
    final_task_requires_previous: bool = True
    use_learned_profile: bool = True
    script_decomposition: dict[str, Any] | None = None
    script_graph: dict[str, Any] | None = None
    visual_plan: dict[str, Any] | None = None
    visual_result: dict[str, Any] | None = None
    world_builder_llm: LLMProviderConfig | None = None


class ExperienceFeedbackRequest(BaseModel):
    world_id: str = ""
    world_name: str = ""
    template: str = ""
    complexity: str = ""
    npc_count: int = Field(ge=0, le=200)
    task_count: int = Field(ge=0, le=500)
    action_count: int = Field(ge=0, le=600)
    immersion_score: int = Field(ge=1, le=5)
    pacing: str = "immersive"
    notes: str = ""


class ExperienceLearningProfile(BaseModel):
    sample_count: int = 0
    recommended_npcs: int = 5
    recommended_tasks: int = 8
    recommended_actions: int = 8
    confidence: str = "low"
    summary: str = "暂无足够体验反馈，先使用中等复杂度。"
    pacing_counts: dict[str, int] = Field(default_factory=dict)
    generation_hint: str = ""


class ProjectIntakeRequest(BaseModel):
    project_name: str = ""
    description: str = ""
    documents: list[str] = Field(default_factory=list)
    repo_hint: str = ""
    api_hint: str = ""
    target_player: str = "玩家"


class ProjectMechanicCandidate(BaseModel):
    path: str
    label: str = ""
    kind: str = "stat"
    source: str = ""


class ProjectActionCandidate(BaseModel):
    id: str
    label: str = ""
    maps_to: str = ""
    risk: str = ""


class ProjectIntakeSummary(BaseModel):
    project_name: str = ""
    project_type: str = "unknown"
    theme: str = ""
    source_summary: str = ""
    player_fields: list[str] = Field(default_factory=list)
    npcs: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    candidate_mechanics: list[ProjectMechanicCandidate] = Field(default_factory=list)
    candidate_actions: list[ProjectActionCandidate] = Field(default_factory=list)
    integration_risks: list[str] = Field(default_factory=list)
    recommended_world_request: WorldGenerateRequest = Field(default_factory=WorldGenerateRequest)


class IntegrationAdapterPlan(BaseModel):
    adapter_type: str = "sandbox_json"
    world_input_mode: str = "project_intake_summary"
    action_mappings: list[ProjectActionCandidate] = Field(default_factory=list)
    required_external_apis: list[str] = Field(default_factory=list)
    state_ownership: dict[str, str] = Field(default_factory=dict)
    guardrails: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class ProjectIntegrationAnalysis(BaseModel):
    intake: ProjectIntakeSummary
    adapter_plan: IntegrationAdapterPlan


class ScriptCharacterInput(BaseModel):
    id: str = ""
    name: str
    role: str = "嫌疑人"
    public_info: str = ""
    secret: str = ""
    motive: str = ""
    alibi: str = ""
    location: str = ""


class ScriptClueInput(BaseModel):
    id: str = ""
    title: str
    content: str = ""
    source: str = ""
    location: str = ""
    owner: str = ""
    reveals: str = ""
    trigger: str = ""


class ScriptEndingInput(BaseModel):
    id: str = ""
    title: str
    condition: str = ""
    reveal: str = ""


class ScriptCharacterSheet(BaseModel):
    id: str = ""
    name: str
    role: str = "NPC"
    public_info: str = ""
    secret: str = ""
    motive: str = ""
    alibi: str = ""
    location: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScriptClueSheet(BaseModel):
    id: str = ""
    title: str
    content: str = ""
    source: str = ""
    location: str = ""
    owner: str = ""
    reveals: str = ""
    trigger: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScriptEndingSheet(BaseModel):
    id: str = ""
    title: str
    condition: str = ""
    reveal: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScriptStoryEvidence(BaseModel):
    source: str = ""
    text: str = ""
    confidence: str = "medium"


class ScriptStoryEntity(BaseModel):
    id: str
    kind: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)
    evidence: list[ScriptStoryEvidence] = Field(default_factory=list)


class ScriptStoryRelation(BaseModel):
    id: str = ""
    source: str
    target: str
    type: str
    description: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)
    evidence: list[ScriptStoryEvidence] = Field(default_factory=list)
    confidence: str = "medium"


class ScriptStoryGraphFacts(BaseModel):
    entities: list[ScriptStoryEntity] = Field(default_factory=list)
    relations: list[ScriptStoryRelation] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)


class ScriptDecompositionResult(BaseModel):
    script_id: str = ""
    script_type: str = "case_investigation"
    title: str = ""
    player_name: str = "主角"
    public_background: str = ""
    core_plot: str = ""
    hidden_threads: list[str] = Field(default_factory=list)
    truth: str = ""
    timeline: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    characters: list[ScriptCharacterSheet] = Field(default_factory=list)
    clues: list[ScriptClueSheet] = Field(default_factory=list)
    endings: list[ScriptEndingSheet] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    story_graph: ScriptStoryGraphFacts = Field(default_factory=ScriptStoryGraphFacts)
    world_mapping: dict[str, Any] = Field(default_factory=dict)
    report: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScriptDecompositionRequest(BaseModel):
    case_id: str = ""
    title: str = ""
    player_name: str = "侦探"
    source_text: str = ""
    characters: list[ScriptCharacterInput] = Field(default_factory=list)
    clues: list[ScriptClueInput] = Field(default_factory=list)
    endings: list[ScriptEndingInput] = Field(default_factory=list)
    core_plot: str = ""
    hidden_threads: list[str] = Field(default_factory=list)
    truth: str = ""
    public_background: str = ""
    timeline: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    forbidden_spoilers: list[str] = Field(default_factory=list)
    story_graph: ScriptStoryGraphFacts = Field(default_factory=ScriptStoryGraphFacts)
    decomposition_mode: str = "rules"
    decomposition_llm: LLMProviderConfig | None = None


class ScriptDecompositionReport(BaseModel):
    passed: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    ontology_warnings: list[str] = Field(default_factory=list)
    unresolved_references: list[str] = Field(default_factory=list)
    isolated_nodes: list[str] = Field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0
    evidence_count: int = 0
    entity_counts: dict[str, int] = Field(default_factory=dict)
    relation_counts: dict[str, int] = Field(default_factory=dict)


class ScriptDecompositionBuildResponse(BaseModel):
    world: SandboxWorldConfig | None = None
    report: ScriptDecompositionReport
    decomposition: ScriptDecompositionResult | None = None
    artifact: dict[str, Any] | None = None


class VisualAssetRequest(BaseModel):
    world: SandboxWorldConfig | None = None
    decomposition: ScriptDecompositionResult | None = None
    script_graph: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    output_root: str = "output/visual_assets"
    provider: ImageGenerationProviderConfig = Field(default_factory=ImageGenerationProviderConfig)
    prompt_model: LLMProviderConfig | None = None
    prompt_composer: str = "agent"
    include_characters: bool = True
    include_scenes: bool = True
    auto_remove_character_background: bool = True
    background_removal_model: str = "auto"
    max_characters: int | None = None
    max_scenes: int | None = None
    style_prompt: str = "high quality game concept art"
    style_guide: dict[str, Any] = Field(default_factory=dict)
    negative_prompt: str = "text, watermark, logo, caption, subtitle, UI, blurry, low quality"


class VisualAssetSpec(BaseModel):
    id: str
    kind: str
    display_name: str
    prompt: str
    output_path: str
    source_id: str = ""
    source_name: str = ""
    provider: str = "stepfun"
    model: str = ""
    size: str = ""
    negative_prompt: str = ""
    status: str = "planned"
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VisualAssetPlan(BaseModel):
    plan_id: str
    world_id: str = ""
    title: str = ""
    provider: ImageGenerationProviderConfig = Field(default_factory=ImageGenerationProviderConfig)
    assets: list[VisualAssetSpec] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VisualAssetGenerationResult(BaseModel):
    plan: VisualAssetPlan
    generated: list[VisualAssetSpec] = Field(default_factory=list)
    failed: list[VisualAssetSpec] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
