# 外部游戏 / 客户端接入契约

本文面向“基于本 NPC Agent 基座二次开发”的外部游戏、独立产品前端、桌面端、Web 客户端或其他运行环境。  
它们可以是 React、Vue、Next.js、Nuxt、Flutter Web、Unity WebGL、桌面端或其他客户端。它们不属于本仓库 `static/` 演示页，也不应该把某一个具体世界观的剧情逻辑写死在客户端。

本仓库提供的是：

```text
通用 NPC Agent 基座能力
```

外部游戏 / 客户端要做的是：

```text
调用统一 API，展示世界列表、世界编辑器、运行台、NPC 对话、状态栏。
```

当前基座不是某一个世界观的后端，而是一套 Agent 能力平台。外部客户端可以只接入运行时，也可以接入创作者后台：

```text
运行时：世界列表、开始游戏、NPC 对话、世界动作、session 恢复。
创作者后台：项目接入分析、世界生成、剧本拆解、剧本图谱、视觉资产计划与生成、世界书检查、体验反馈学习。
```

当前完整创作链路：

```text
剧本 / 文档输入
  -> ScriptDecompositionAgent
  -> ScriptGraphCompiler
  -> 可选 VisualPromptComposerAgent / VisualAssetGenerationAgent
  -> WorldBuilderAgent
  -> SandboxWorldConfig
  -> attach_visual_bindings
  -> NpcLorebookCreationAgent 或 NpcLorebookCompiler fallback
  -> NpcLorebookArtifact
  -> NPC 对话 / 试玩验证
```

外部客户端可以分阶段接入，不需要一次性实现全部后台。但如果做创作者工作台，建议把剧本拆解、故事图谱、视觉资产、世界生成、世界书、NPC 对话测试、试玩验证做成显式阶段，方便检查和重跑。

## 1. 后端地址

本地开发默认：

```text
http://127.0.0.1:8000
```

API 前缀：

```text
/api
```

示例：

```text
http://127.0.0.1:8000/api/worlds
```

## 2. 跨域配置

独立前端通常运行在：

```text
http://localhost:3000
http://localhost:5173
```

后端已支持 CORS，配置项：

```env
CORS_ALLOW_ORIGINS=http://127.0.0.1:3000,http://localhost:3000,http://127.0.0.1:5173,http://localhost:5173
```

如果你的独立前端运行在别的地址，比如：

```text
http://localhost:8080
https://your-domain.com
```

就把它加入 `.env`：

```env
CORS_ALLOW_ORIGINS=http://localhost:8080,https://your-domain.com
```

## 3. 重要边界

外部游戏 / 客户端必须遵守：

- 不要直接修改 Agent 内部状态。
- 不要自己执行 `command`。
- 不要把具体剧情流程写死到前端。
- 不要把某一个世界观的 UI 流程写成全局流程。
- 所有状态变化以后端返回为准。
- `actions` 是世界接口，不等于玩家可见按钮。
- `suggested_actions` 是线索笔记，不是必须点击的操作。

正确模型：

```text
外部游戏 / 客户端负责展示和收集玩家输入
后端负责 Agent、世界状态、command 校验、任务推进、记忆
```

## 4. 推荐外部产品模块

独立游戏或产品前端建议拆成这些模块：

```text
ApiClient
WorldList
WorldEditor
WorldGenerator
GameRuntime
NpcChat
NpcSelector
PlayerStatusPanel
QuestPanel
CluePanel
MemoryDebugPanel
RawStateDebugPanel
```

其中：

- `WorldEditor` 面向创作者。
- `GameRuntime` 面向玩家。
- `MemoryDebugPanel` / `RawStateDebugPanel` 面向调试，不建议默认给普通玩家展示。

## 5. API 总览

只使用这些接口：

```text
GET    /api/health
GET    /api/config/effective

GET    /api/worlds
GET    /api/world-templates
POST   /api/world-templates
PUT    /api/world-templates/{template_id}
DELETE /api/world-templates/{template_id}
GET    /api/experience/profile
POST   /api/experience/feedback
POST   /api/projects/analyze
POST   /api/worlds
POST   /api/worlds/generate
POST   /api/worlds/script-decomposition
POST   /api/worlds/script-decomposition/compile
GET    /api/worlds/script-decompositions
GET    /api/worlds/script-decompositions/{artifact_id}
POST   /api/worlds/script-decomposition/import
POST   /api/worlds/script-decomposition/import/jobs
GET    /api/worlds/script-decomposition/import/jobs/{job_id}
POST   /api/worlds/script-decomposition/import/jobs/{job_id}/cancel
POST   /api/worlds/script-graph/compile
GET    /api/worlds/script-graphs
GET    /api/worlds/script-graphs/{artifact_id}
POST   /api/worlds/visual-assets/plan
GET    /api/worlds/visual-assets
GET    /api/worlds/visual-assets/{artifact_id}
POST   /api/worlds/visual-assets/plans
GET    /api/worlds/visual-assets/runs
GET    /api/worlds/visual-assets/runs/{run_id}
DELETE /api/worlds/visual-assets/runs/{run_id}
POST   /api/worlds/visual-assets/generate
POST   /api/worlds/visual-assets/generate/jobs
GET    /api/worlds/visual-assets/generate/jobs/{job_id}
POST   /api/worlds/visual-assets/generate/jobs/{job_id}/cancel
POST   /api/worlds/import
GET    /api/worlds/{world_id}
PUT    /api/worlds/{world_id}
DELETE /api/worlds/{world_id}

POST   /api/worlds/{world_id}/start
GET    /api/worlds/{world_id}/session
POST   /api/worlds/{world_id}/chat
POST   /api/worlds/{world_id}/action
POST   /api/worlds/{world_id}/agent/tick
POST   /api/worlds/{world_id}/memory/query
```

不要使用旧接口：

```text
/api/chat
/api/world/action
/api/agent/tick
/api/memory/query
```

这些已经从后端移除。

## 5.1 配置查看

接口：

```text
GET /api/config/effective
```

用途：

- 创作者后台检查 world builder、visual prompt、NPC、image provider 是否已配置。
- 默认不会返回密钥明文。

响应重点字段：

```ts
type EffectiveConfig = {
  world_api: PublicLLMConfig;
  visual_prompt_api: PublicLLMConfig;
  npc_api: PublicLLMConfig;
  image_api: {
    provider: string;
    api_base_url: string;
    model: string;
    size: string;
    api_key_env: string;
    api_key: string;
    has_api_key: boolean;
    source: string;
  };
};

type PublicLLMConfig = {
  provider: string;
  base_url: string;
  model: string;
  temperature: number;
  timeout: number;
  max_retries: number;
  has_api_key: boolean;
  source: string;
};
```

除非是本地可信调试，不要使用 `include_secrets=true`。

## 6. 通用请求封装

外部前端可以这样封装：

```ts
const API_BASE = "http://127.0.0.1:8000";

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...(init.headers || {}),
    },
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(text);
  }

  return response.json() as Promise<T>;
}
```

## 7. 世界列表

接口：

```text
GET /api/worlds
```

响应：

```ts
type WorldSummary = {
  world_id: string;
  name: string;
  description: string;
  kind: "sandbox" | "builtin" | string;
};
```

用途：

- 渲染世界列表。
- `kind === "builtin"` 的世界只运行，不编辑。
- `kind === "sandbox"` 的世界可以编辑。

## 8. 世界配置

接口：

```text
GET /api/worlds/{world_id}
PUT /api/worlds/{world_id}
POST /api/worlds
```

核心类型：

```ts
type SandboxWorldConfig = {
  world_id: string;
  name: string;
  description: string;
  lore: string;
  opening_scene: string;
  player: Record<string, any>;
  npcs: SandboxNPC[];
  story_goals: string[];
  tasks: SandboxTask[];
  actions: SandboxAction[];
  initial_memories: string[];
  metadata: Record<string, any>;
};

type SandboxNPC = {
  id: string;
  name: string;
  role: string;
  personality: string;
  goals: string[];
  location: string;
};

type SandboxTask = {
  id: string;
  title: string;
  description: string;
  status: "pending" | "running" | "done" | "failed" | "skipped" | string;
};

type SandboxAction = {
  id: string;
  label: string;
  description: string;
  effect: Record<string, any>;
};
```

前端编辑器字段建议：

```text
world_id        世界 ID
name            世界名称
description     简介
lore            世界观/规则/背景
opening_scene   开场场景
player          玩家模板
npcs            NPC 列表
story_goals     故事目标
tasks           任务列表
actions         世界动作接口
metadata        调试/生成信息
```

`metadata.mechanics` 用于声明本世界的机制字段。外部前端不要假设所有世界都有同一套字段，例如不要把 `realm_level`、`skills.vocal`、`fan_count` 写成全局固定规则。

推荐结构：

```ts
type MechanicsField = {
  label: string;
  type: "number" | "string" | "boolean" | "item" | "relation" | string;
  aliases?: string[];
  description?: string;
  min?: number;
  max?: number;
  unit?: string;
};

type SandboxWorldMetadata = {
  mechanics?: Record<string, MechanicsField>;
  script_graph?: Record<string, any>;
  visual_plan?: Record<string, any>;
  visual_result?: Record<string, any>;
  npc_portraits?: Record<string, any>;
  npc_lorebook?: Record<string, any>;
  npc_lorebook_generation?: Record<string, any>;
  validation?: Record<string, any>;
  world_review?: Record<string, any>;
  playtest_review?: Record<string, any>;
  quality_gate?: Record<string, any>;
};
```

字段来源：

```text
WorldBuilderAgent 初步决定这个世界需要哪些状态字段
MechanicsDesignAgent 整理 metadata.mechanics，并对齐 completion/action.effect
attach_visual_bindings 写入 NPC portrait / metadata.npc_portraits
NpcLorebookCreationAgent 或 NpcLorebookCompiler 写入 metadata.npc_lorebook
WorldReviewAgent 只检查一致性，不决定字段
```

保存时提交完整 `SandboxWorldConfig`，不要只提交 patch。

后端保存前会执行：

```text
SandboxWorldValidator.ensure_valid()
```

所以后端可能会自动补齐字段，并在 `metadata.validation` 写入校验结果。

注意：Validator 的补齐是最低可运行兜底，不代表它决定了玩法机制字段。字段语义仍以 `metadata.mechanics` 为准。

## 9. AI 生成世界

### 9.0 外部项目接入分析

接口：

```text
POST /api/projects/analyze
```

用途：

```text
已有游戏 / 设定文档 / 接口说明
  -> ProjectIntakeAgent 总结项目
  -> IntegrationAdapterAgent 规划接入边界
  -> 得到 recommended_world_request / adapter_plan
  -> 再决定生成 sandbox 世界或开发真实 WorldAdapter
```

请求：

```ts
type ProjectIntakeRequest = {
  project_name: string;
  description: string;
  documents: string[];
  repo_hint: string;
  api_hint: string;
  target_player: string;
};
```

响应：

```ts
type ProjectIntegrationAnalysis = {
  intake: {
    project_name: string;
    project_type: string;
    theme: string;
    source_summary: string;
    player_fields: string[];
    npcs: string[];
    locations: string[];
    candidate_mechanics: Array<{ path: string; label: string; kind: string; source: string }>;
    candidate_actions: Array<{ id: string; label: string; maps_to: string; risk: string }>;
    integration_risks: string[];
    recommended_world_request: WorldGenerateRequest;
  };
  adapter_plan: {
    adapter_type: "sandbox_json" | "world_adapter_required" | string;
    world_input_mode: string;
    action_mappings: Array<{ id: string; label: string; maps_to: string; risk: string }>;
    required_external_apis: string[];
    state_ownership: Record<string, "agent_runtime" | "external_game" | string>;
    guardrails: string[];
    risks: string[];
  };
};
```

说明：

- 这是接入前置分析，不会直接创建世界。
- 如果 `adapter_plan.adapter_type === "sandbox_json"`，通常可以直接拿 `recommended_world_request` 去生成 sandbox 世界。
- 如果是 `world_adapter_required`，说明候选动作需要映射真实游戏 API，不能只靠 JSON effect 伪造结果。

### 9.1 剧本拆解生成

接口：

```text
POST /api/worlds/script-decomposition
POST /api/worlds/script-decomposition/compile
```

用途：

```text
结构化剧本内容 / 文档文本
  -> ScriptDecompositionAgent
  -> ScriptDecompositionResult / Report / Artifact
  -> 可选 compile
  -> SandboxWorldConfig
  -> 保存为可运行世界
```

最小请求：

```ts
type ScriptDecompositionRequest = {
  case_id: string;
  title: string;
  player_name: string;
  public_background: string;
  truth: string;
  locations: string[];
  timeline: string[];
  characters: Array<{
    id: string;
    name: string;
    role: string;
    public_info: string;
    secret: string;
    motive: string;
    alibi: string;
    location: string;
  }>;
  clues: Array<{
    id: string;
    title: string;
    content: string;
    location: string;
    owner: string;
    reveals: string;
    trigger: string;
  }>;
  forbidden_spoilers: string[];
};
```

响应：

```ts
type ScriptDecompositionBuildResponse = {
  world: SandboxWorldConfig;
  report: {
    passed: boolean;
    errors: string[];
    warnings: string[];
    character_count: number;
    clue_count: number;
    location_count: number;
    has_truth: boolean;
  };
};
```

说明：

- 剧本拆解是当前第一个垂直编译案例，应优先使用此接口或 `template="script_decomposition"`。
- `/api/worlds/script-decomposition` 负责拆解并保存 artifact，不一定直接保存可运行世界。
- `/api/worlds/script-decomposition/compile` 负责把拆解结果编译为可运行世界并保存。
- 当前实现优先支持案件型剧本；该链路不会把案件真相交给通用 WorldBuilder 自由改写。
- 完整案件结构会写入 `world.metadata.script_case`。
- 前端应把 `report.errors/warnings` 展示给编剧或运营确认。
- 如果上传文档包含 `公共背景 / 角色 / 线索 / 案件真相` 等案件型剧本结构，后端导入也会优先走该专用 Agent。

### 9.1.1 剧本文档导入长任务

接口：

```text
POST /api/worlds/script-decomposition/import
POST /api/worlds/script-decomposition/import/jobs
GET  /api/worlds/script-decomposition/import/jobs/{job_id}
POST /api/worlds/script-decomposition/import/jobs/{job_id}/cancel
```

用途：

- 多文件上传。
- 后端抽取 txt/md/json/docx/pdf/rtf/html/csv 文本。
- 用 `decomposition_mode` 决定拆解模式。
- 长任务版本会返回 progress events，适合前端显示阶段进度。

长任务响应结构：

```ts
type JobState = {
  job_id: string;
  status: "queued" | "running" | "cancelling" | "cancelled" | "done" | "error" | string;
  events: Array<{ status: string; title: string; detail: string; at: string }>;
  result: any | null;
  error: { type: string; message: string; trace?: string } | null;
  created_at: string;
  updated_at: string;
};
```

前端建议：

- 上传后轮询 `GET jobs/{job_id}`。
- 显示 `events[]`，不要只给用户一个转圈。
- 用户点击停止时调用 cancel endpoint。

### 9.1.2 剧本拆解 Artifact 与故事图谱

接口：

```text
GET  /api/worlds/script-decompositions
GET  /api/worlds/script-decompositions/{artifact_id}
POST /api/worlds/script-graph/compile
GET  /api/worlds/script-graphs
GET  /api/worlds/script-graphs/{artifact_id}
```

用途：

```text
ScriptDecompositionAgent
  -> decomposition artifact
  -> ScriptGraphCompiler
  -> ScriptGraphDocument
  -> graph artifact
```

关键边界：

- ScriptDecompositionAgent 负责理解故事。
- ScriptGraphCompiler 是确定性 compiler，只把拆解结果转为 nodes / edges。
- Compiler 不重新解释故事，不改写真相、人物关系、线索含义。

故事图谱响应核心：

```ts
type ScriptGraphDocument = {
  graph_id: string;
  title: string;
  source_artifact_id: string;
  schema_version: "script_graph.v1" | string;
  ontology: Record<string, any>;
  nodes: Array<{ id: string; kind: string; label: string; properties: Record<string, any> }>;
  edges: Array<{ id: string; source: string; target: string; type: string; properties: Record<string, any> }>;
  indexes: Record<string, any>;
  metadata: Record<string, any>;
};
```

### 9.1 世界模板

接口：

```text
GET    /api/world-templates
POST   /api/world-templates
PUT    /api/world-templates/{template_id}
DELETE /api/world-templates/{template_id}
```

类型：

```ts
type WorldTemplateSummary = {
  id: string;
  name: string;
  description: string;
  structure_prompt: string;
  enabled: boolean;
  sort_order: number;
};
```

说明：

- 模板只描述叙事结构，不绑定题材。
- 前端应允许用户选择 `freeform` 或自定义模板。
- 默认模板来自 `data/world_templates.json`。

### 9.2 生成请求

接口：

```text
POST /api/worlds/generate
```

请求：

```ts
type WorldGenerateRequest = {
  template: "freeform" | "three_act_growth" | "short_drama_reversal" | "script_decomposition" | "mystery_investigation" | "management_growth" | "relationship_route" | "adventure_battle" | "document_adaptation" | string;
  theme: string;
  player_name: string;
  world_name: string;
  complexity: "simple" | "medium" | "complex" | "ultra" | string;
  min_npcs?: number | null;
  min_tasks?: number | null;
  min_actions?: number | null;
  final_task_requires_previous: boolean;
  use_learned_profile: boolean;
  script_decomposition?: Record<string, any> | null;
};
```

示例：

```json
{
  "template": "freeform",
  "theme": "现代娱乐圈练习生逆袭",
  "player_name": "林澈",
  "world_name": "",
  "complexity": "medium",
  "min_npcs": null,
  "min_tasks": null,
  "min_actions": null,
  "final_task_requires_previous": true,
  "use_learned_profile": true
}
```

响应：

```ts
SandboxWorldConfig
```

说明：

- 普通模板走通用 `WorldBuilderAgent`。
- `template="script_decomposition"` 或 `script_decomposition` 非空时，会转交 `ScriptDecompositionAgent`。

前端流程：

```text
用户填写生成参数
  -> POST /api/worlds/generate
  -> 刷新 GET /api/worlds
  -> 进入生成出的 world_id
  -> 展示世界配置和“开始游戏”
```

### 9.3 体验反馈学习

接口：

```text
GET  /api/experience/profile
POST /api/experience/feedback
```

反馈请求：

```ts
type ExperienceFeedbackRequest = {
  world_id: string;
  world_name: string;
  template: string;
  complexity: string;
  npc_count: number;
  task_count: number;
  action_count: number;
  immersion_score: 1 | 2 | 3 | 4 | 5;
  pacing: "too_short" | "slightly_short" | "immersive" | "slightly_long" | "too_long" | string;
  notes: string;
};
```

画像响应：

```ts
type ExperienceLearningProfile = {
  sample_count: number;
  recommended_npcs: number;
  recommended_tasks: number;
  recommended_actions: number;
  confidence: "low" | "medium" | "high" | string;
  summary: string;
  pacing_counts: Record<string, number>;
  generation_hint: string;
};
```

用途：

- 玩家试玩后提交反馈。
- 后端据此学习更合适的 NPC、任务、动作数量。
- `WorldGenerateRequest.use_learned_profile=true` 时，生成器会使用该画像作为默认规模参考。

## 9.4 视觉资产计划与图片生成

接口：

```text
POST   /api/worlds/visual-assets/plan
GET    /api/worlds/visual-assets
GET    /api/worlds/visual-assets/{artifact_id}
POST   /api/worlds/visual-assets/plans
POST   /api/worlds/visual-assets/generate
POST   /api/worlds/visual-assets/generate/jobs
GET    /api/worlds/visual-assets/generate/jobs/{job_id}
POST   /api/worlds/visual-assets/generate/jobs/{job_id}/cancel
GET    /api/worlds/visual-assets/runs
GET    /api/worlds/visual-assets/runs/{run_id}
DELETE /api/worlds/visual-assets/runs/{run_id}
```

推荐流程：

```text
世界 / 剧本图谱 / 风格要求
  -> VisualPromptComposerAgent / VisualAssetGenerationAgent plan
  -> 人工检查 visual plan
  -> generate job
  -> 轮询 job events
  -> 查看 output/visual_assets 下的 run
```

边界：

- 视觉资产是独立创作阶段，不要在世界生成后自动静默生成。
- `plan` 是可编辑 artifact。
- `generate/jobs` 适合真实前端，因为图片生成耗时较长。
- 图片输出在 `output/visual_assets`，属于运行产物，不应作为前端必须写死的资产路径。

前端最小展示：

- plan 列表。
- plan 详情。
- 开始生成按钮。
- job event 时间线。
- run 图片预览。
- 删除 run 按钮。

## 9.5 世界书与 NPC Runtime 消费

世界生成完成后，后端会把可运行世界、故事图谱和视觉资产 metadata 汇总为 NPC runtime 可用的世界书：

```text
SandboxWorldConfig
  -> attach_visual_bindings
  -> NpcLorebookCreationAgent / NpcLorebookCompiler fallback
  -> metadata.npc_lorebook
  -> SandboxWorldAdapter
  -> NpcLorebookRuntime
  -> NpcAgent
```

外部客户端注意：

- 世界书不是前端手写剧情分支，而是后端 artifact。
- 前端可以展示 `metadata.npc_lorebook` 供创作者检查，但不要直接依赖内部条目顺序驱动 UI。
- NPC 对话时后端会按当前 NPC、地点、玩家输入激活世界书切片。
- 如果要做创作者后台，建议提供“世界书预览 / JSON 查看 / 审查结果”区域。
- `metadata.npc_lorebook_generation.fallback_used=true` 表示 AI 世界书生成失败或未配置，当前世界书来自确定性 compiler fallback。

## 10. 开始游戏

接口：

```text
POST /api/worlds/{world_id}/start
```

响应：

```ts
type WorldActionResponse = {
  action: string;
  narration: string;
  state: Record<string, any>;
  player: Record<string, any>;
  active_entity: Record<string, any> | null;
  speaker: Record<string, any> | null;
  npcs: Record<string, any>[];
  quest_progress: string;
  suggested_actions: string[];
};
```

前端渲染：

- `narration`：系统消息。
- `player`：玩家状态栏。
- `speaker`：当前 NPC。
- `npcs`：NPC 选择器。
- `state.tasks`：任务面板。
- `quest_progress`：当前进度。
- `suggested_actions`：线索笔记。

## 11. NPC 对话

接口：

```text
POST /api/worlds/{world_id}/chat
```

请求：

```ts
type ChatRequest = {
  message: string;
  player_name: string;
  location: string;
  player_goal: string;
  target_npc_id: string;
  target_npc_ids?: string[];
  group_chat?: boolean;
  max_npc_replies?: number;
};
```

响应：

```ts
type ChatMessage = {
  role: "npc" | "system";
  npc_id: string;
  speaker: string;
  content: string;
  action_type: string;
  command: Record<string, any>;
};

type ChatResponse = {
  reply: string;
  action_type: "say" | "ask" | "emote" | "refuse" | "hint" | "trade" | "quest" | "wait" | "group" | string;
  inner_thought: string;
  command: Record<string, any>;
  emotion: Record<string, number>;
  memories: string[];
  goals: string[];
  player_goal: string;
  quest_progress: string;
  suggested_actions: string[];
  player: Record<string, any>;
  active_entity: Record<string, any> | null;
  speaker: Record<string, any> | null;
  npcs: Record<string, any>[];
  nearby_npcs: Record<string, any>[];
  messages: ChatMessage[];
};
```

前端渲染：

- 玩家消息气泡：使用本地输入。
- NPC 消息气泡：优先渲染 `messages[]`，没有 `messages` 时回退到 `reply`。
- NPC 名称：`speaker.name`，没有则显示 `NPC`。
- 玩家状态栏：`player`。
- 任务进度：`quest_progress`。
- 线索笔记：`suggested_actions`。
- 调试面板：`inner_thought`、`command`、`memories`、`emotion`。

单 NPC 对话：

```json
{
  "message": "我现在该做什么？",
  "player_name": "林澈",
  "location": "问道殿",
  "player_goal": "完成试炼",
  "target_npc_id": "sect_leader"
}
```

在场 NPC 群聊：

```json
{
  "message": "你们一起判断一下下一步。",
  "player_name": "林澈",
  "location": "问道殿",
  "player_goal": "完成试炼",
  "target_npc_ids": ["sect_leader", "advisor"],
  "group_chat": true,
  "max_npc_replies": 3
}
```

群聊规则：

- `target_npc_ids` 优先指定参与者。
- 没有指定时，后端会使用玩家当前位置的 nearby NPC。
- 每个 NPC 由自己的 `NpcAgent` 实例和 `NpcRuntimeState` 生成回复。
- `messages[]` 按 NPC 回复顺序返回。
- `reply` 是兼容旧客户端的拼接文本。
- 当前群聊是“一轮玩家输入，多名 NPC 顺序回复”；不是无限自发多轮圆桌。

运行态持久化：

- 后端会自动保存当前 `world_id` 的运行态到 `data/sessions/{world_id}.session.json`。
- 保存内容包括玩家/世界状态、公共对话日志、每个 NPC 的私有 `NpcRuntimeState`。
- 服务重启后，再访问同一个 `world_id` 会恢复上次运行态。
- 前端不需要保存 NPC 私有记忆，只渲染 API 返回状态。
- `POST /api/worlds/{world_id}/start` 表示重新开始，会清除该 `world_id` 的旧运行态。
- 当前实现是“每个 world_id 一个运行房间”。如果同一个剧本要支持多个玩家房间，需要后续把 `session_id` 加入 API 路径或请求体。

注意：

```text
前端不能执行 command。
后端已经执行或拒绝 command。
前端只渲染返回状态。
```

## 12. 世界动作

接口：

```text
POST /api/worlds/{world_id}/action
```

运行时护栏：

- `move_player` 只能移动到世界配置中已登记地点。
- 已登记地点来自玩家起点、NPC location、以及 action.effect.set_player.location。
- 如果前端提交未知地点，后端会返回 narration 说明可用地点，不会直接创建新地点。
- NPC 回复中如果建议了未登记地点，Runtime 会尝试让模型重答；仍失败时会返回确定性的安全地点引导。

请求：

```ts
type WorldActionRequest = {
  action: string;
  payload: Record<string, any>;
};
```

常见用途：

```text
switch_npc
advance_scene
complete_task
set_flag
update_relation
配置中的自定义 action id
```

前端原则：

- 普通玩家界面不要直接展示全部后台 actions。
- 创作者/调试模式可以展示 actions。
- 玩家运行台推荐用自然输入：对话、地点、找人、观察。

## 13. Session 状态恢复

接口：

```text
GET /api/worlds/{world_id}/session
```

响应：

```ts
type SessionSnapshotResponse = {
  world_id: string;
  started: boolean;
  state: Record<string, any>;
  player: Record<string, any>;
  active_entity: Record<string, any> | null;
  speaker: Record<string, any> | null;
  npcs: Record<string, any>[];
  quest_progress: string;
  goals: string[];
  suggested_actions: string[];
  inner_thought: string;
};
```

用途：

- 独立前端刷新页面后恢复运行态。
- 多标签页同步时获取后端真实状态。
- 和前端自己的本地聊天记录存档结合使用。

建议：

```text
聊天气泡可由前端本地持久化。
世界状态以后端 session 为准。
```

## 14. 记忆查询

接口：

```text
POST /api/worlds/{world_id}/memory/query
```

请求：

```ts
type MemoryQueryRequest = {
  query: string;
  limit: number;
};
```

响应：

```ts
type MemoryQueryResponse = {
  rag: {
    original_query: string;
    rewritten_query: string | null;
    documents: {
      id: string;
      content: string;
      importance: number;
      relevance: number;
      verdict: "relevant" | "weak" | "irrelevant";
    }[];
    reliable: boolean;
    note: string;
  };
};
```

用途：

- 调试。
- 创作者检查记忆。
- 不建议默认给普通玩家展示。

## 15. 自动推进

接口：

```text
POST /api/worlds/{world_id}/agent/tick
```

请求：

```ts
type AutonomousTickRequest = {
  max_steps: number;
  objective: string;
};
```

响应：

```ts
type AutonomousTickResponse = {
  objective: string;
  executed: WorldActionResponse[];
  plan: Record<string, any>[];
  stopped_reason: string;
};
```

用途：

- 调试。
- 自动跑世界动作。
- 不建议作为普通玩家主交互。

## 16. 玩家状态栏字段

从响应里的 `player` 读取。

渲染优先级：

```text
1. 优先读取当前世界配置里的 metadata.mechanics。
2. 用 mechanics 的 label/type/description 渲染玩家状态。
3. 对 mechanics 没声明但 player 里存在的字段，放到“其他状态 / 调试状态”。
4. 不要把某一个世界的字段写成所有世界通用字段。
```

常见兜底字段：

```text
name
location
role
identity
status
realm
battle_power
cultivation
spirit_stones
inventory
items
trial_token
spirit_seal
trial_complete
*_obtained
*_owned
```

UI 建议：

- 基础信息：名字、地点、身份、状态。
- 数值：根据 `metadata.mechanics` 展示，例如境界、战力、修为、唱功、舞台表现、粉丝数。
- 物品：`inventory/items` 和布尔型关键道具。
- 条件：关键布尔字段 true/false。

## 17. 错误处理

常见状态：

```text
400 配置错误、内置世界不可编辑、内置世界不可删除
404 世界不存在
500 LLM 或运行时异常
```

前端处理：

- 保存失败：保留表单，不清空用户输入。
- 生成失败：保留生成参数。
- 聊天失败：保留玩家输入或允许重发。
- `action_type === "wait"`：按普通 NPC 回复显示，表示模型超时或暂未响应。

## 18. 独立前端开工顺序

建议顺序：

1. 建立 API client。
2. 做世界列表。
3. 做世界生成页。
4. 做世界编辑页。
5. 做运行台：聊天、NPC 选择、玩家状态、任务进度。
6. 做 session 恢复。
7. 做调试面板：command、inner_thought、RAG、raw state。

不要改后端核心逻辑来适配某个 UI。
不要把具体世界观流程写进独立前端。

