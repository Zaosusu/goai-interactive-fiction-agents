# 通用 NPC Agent 技术架构说明

本文记录当前项目的技术底座。剧情、UI 美术、玩法数值和具体世界观创作不在本文范围内。

## 1. 当前定位

当前框架已经进入“产品 MVP 底座”阶段，目标是支撑：

- 多世界观沙盒管理
- 外部项目接入前置分析
- AI 生成可运行世界
- NPC 固定 JSON 响应
- command 白名单状态变更
- CommandExecutor 中心化执行状态变更
- 世界配置保存前校验和修复
- 生成世界先修复、再 review、再自动试玩 quality gate
- session snapshot 恢复
- Corrective RAG 记忆注入
- 热插拔 JSON 世界观运行
- 多阶段 review pipeline：世界、NPC、UI 投影、模拟流程

一句话：

```text
app/agents 是产品化 Agent 模块层；
app/core 是跨 Agent 共享运行时与基础设施；
app/worlds 是世界适配层与 sandbox 兼容实现层；
data/worlds 是沙盒世界配置；
data/sessions 是运行中世界会话快照；
data/memory 是长期检索记忆；
static 是浏览器操作台。
```

当前重要架构变化：

```text
项目已经引入 app/agents/<agent_module> 作为新的 Agent module first 边界。
部分 Agent module 已经拥有自己的 schema/compiler/store。
部分 Agent module 当前仍 re-export app/core 或 app/worlds/sandbox 的旧实现。
这不是两套互斥架构，而是从早期横向实现向产品化 Agent 模块迁移中的状态。
```

项目内开发标准：

```text
.codex/skills/npc-agent-architecture/SKILL.md
AGENT_DEVELOPMENT_STANDARD.md
```

标准摘要：

- `Agent` 负责判断、解释、生成、审查、语义理解、策略选择。
- `Tool / Compiler` 负责确定性、可重复、可测试的转换、校验、执行。
- `Store` 负责读写 artifact、session、文件、图谱或生成结果。
- `Provider / Client` 负责外部 LLM、图片、embedding 等 API。
- Agent 专属 schema、prompt、compiler、validator、review、store 应优先放到 `app/agents/<agent_module>/`。
- `app/core` 只放跨 Agent 共享基础设施。

核心编排：

```text
RouterAgent
  -> WorldBuilderAgent
      -> if script_decomposition: ScriptDecompositionAgent
      -> else: generic WorldBuilder flow

RouterAgent
  -> ProjectIntakeAgent
  -> IntegrationAdapterAgent
  -> ProjectIntegrationAnalysis

RouterAgent
  -> ProjectIntegrationAnalysis
  -> WorldBuilderAgent
  -> MechanicsDesignAgent
  -> WorldValidator / SchemaRepairer
  -> WorldReviewAgent
  -> PlaytestAgent
  -> quality_gate

RouterAgent
  -> WorldBuilderAgent
  -> MechanicsDesignAgent
  -> WorldValidator / SchemaRepairer
  -> WorldReviewAgent
  -> PlaytestAgent
  -> quality_gate

RouterAgent
  -> NpcAgent
  -> AgentLLMOutput schema gate
      -> valid: skip NpcProtocolReviewAgent
      -> invalid: NpcProtocolReviewAgent
  -> StateValidatorAgent
  -> CommandExecutor
  -> NpcReviewAgent

RouterAgent
  -> UiStateProjector
  -> UiReviewAgent

RouterAgent
  -> PlaytestAgent
  -> FlowReviewAgent

ScriptDecompositionAgent
  -> ScriptDecompositionArtifactStore
  -> ScriptGraphCompiler
  -> ScriptGraphStore

VisualPromptComposerAgent
  -> VisualAssetGenerationAgent
  -> VisualAssetArtifactStore
  -> image provider
```

说明：

- ReviewAgent 当前是确定性规则实现，先保证每次运行都有结构化 review report。
- `ProjectIntakeAgent + IntegrationAdapterAgent` 是接入已有项目/设定/接口文档的前置层。
- 剧本拆解是第一个垂直编译案例，`WorldBuilderAgent` 会把 `template=script_decomposition` 或包含案件型剧本结构的输入转交给 `ScriptDecompositionAgent`。
- `CommandValidator` 负责判断 command 能不能执行，`CommandExecutor` 负责真正改状态。
- 世界状态字段由 `WorldBuilderAgent + MechanicsDesignAgent` 这条链路决定，不由 ReviewAgent 或 Validator 临时发明。
- 世界生成质量门是“先修，再测”：Validator/SchemaRepairer 修复后，再由 PlaytestAgent 自动试玩。
- 后续可以把同名 ReviewAgent 替换为 LLM 语义审查，但最终保存/落地仍以 Validator 的确定性校验为准。
- 剧本拆解、剧本图谱、视觉资产生成已经拆成显式 artifact 阶段；前端或调用方应显式触发下一阶段，避免隐藏式连跑。

当前完整创作到运行 pipeline：

```text
ScriptDecompositionAgent
  -> ScriptDecompositionResult
  -> ScriptGraphCompiler
  -> ScriptGraphDocument
  -> optional VisualPromptComposerAgent
  -> optional VisualAssetGenerationAgent
  -> WorldBuilderAgent
  -> SandboxWorldConfig
  -> attach_visual_bindings
  -> NpcLorebookCreationAgent / NpcLorebookCompiler fallback
  -> NpcLorebookArtifact
  -> SandboxWorldAdapter
  -> NpcLorebookRuntime
  -> NpcAgent / AgentRuntime
  -> PlaytestAgent.simulate_adapter()
```

上图即完整的 NPC Lorebook runtime 流程图，已内嵌于本节，不依赖外部图册。

边界说明：

- `ScriptGraphCompiler` 是 compiler，不是 Agent；只把拆解结果转成节点和边。
- `WorldBuilderAgent` 的干净输入是 `script_graph`，可选消费 `visual_plan` / `visual_result`。
- `attach_visual_bindings` 是确定性绑定工具，当前主要绑定 NPC portrait / `metadata.npc_portraits`。
- `NpcLorebookCreationAgent` 生成面向 NPC runtime 的世界书；LLM 不可用时由 `NpcLorebookCompiler` fallback。
- `PlaytestAgent` 当前不是独立游戏客户端运行时，而是基于 `SandboxWorldAdapter` 的自动试玩 / review 阶段。

## 1.1 Agent Module First 标准

项目内 skill：

```text
.codex/skills/npc-agent-architecture/SKILL.md
```

团队说明：

```text
AGENT_DEVELOPMENT_STANDARD.md
```

推荐模块形态：

```text
app/agents/<agent_module>/
  agent.py
  schema.py
  prompt.py
  tools.py
  compiler.py
  validator.py
  review.py
  store.py
  routes.py
```

当前 Agent module 清单：

```text
app/agents/project_intake/
  外部项目接入分析入口。当前 re-export app/worlds/sandbox/project_intake.py。

app/agents/world_builder/
  世界生成入口。当前 re-export app/worlds/sandbox/generator.py。

app/agents/npc_lorebook/
  NPC 世界书生成、确定性编译、审查与运行时激活。
  独立 artifact stage：消费可运行世界、剧本图谱和视觉资产，输出 NpcLorebookArtifact。
  世界书规则归属此模块：条目、关键词/正则关键词、激活策略、插入位置、扫描深度、连锁触发、token 预算，以及长对话总结/记忆表格条目。
  app/agents/npc_runtime/lorebook_* 仅保留兼容 re-export。

app/agents/script_decomposition/
  剧本拆解产品化模块。已包含 ScriptGraph schema/compiler/store/tools。
  Agent 主体当前仍复用 app/worlds/sandbox/script_decomposition.py。

app/agents/visual_prompt_composer/
  视觉提示词编排入口。当前 re-export app/worlds/sandbox/visual_assets.py。

app/agents/visual_asset_generation/
  视觉资产生成入口。已包含 VisualAssetArtifactStore。
  生成 Agent 当前复用 app/worlds/sandbox/visual_assets.py。

app/agents/npc_runtime/
  NPC runtime 入口。当前 re-export AgentRuntime / NpcAgent / RouterAgent / StateValidatorAgent。

app/agents/npc_review/
  NPC 输出协议与质量审查入口。当前 re-export app/core/review_agents.py。

app/agents/world_review/
  世界结构审查入口。当前 re-export app/core/review_agents.py。

app/agents/ui_projection/
  UI 状态投影与审查入口。当前 re-export app/core/review_agents.py。

app/agents/playtest_validation/
  自动试玩与流程审查入口。当前 re-export app/core/review_agents.py。

app/agents/experience_learning/
  体验反馈学习入口。当前 re-export app/worlds/sandbox/experience.py。
```

迁移边界：

- 新增大能力时，先按 `app/agents/<agent_module>` 设计。
- 如果短期复用 `app/worlds/sandbox` 旧实现，`app/agents/<agent_module>` 仍应作为外部 import 入口。
- 不要把 Agent 专属 compiler/store/review 继续堆到 `app/core`。
- 不要把确定性 compiler/tool 命名成 `*Agent`。

## 2. 启动入口

文件：

```text
app/main.py
```

方法：

```python
index()
```

职责：

- 创建 FastAPI app。
- 挂载 `app.api.routes.router`。
- 托管 `/static`。
- `GET /` 返回 `static/index.html`。

启动：

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## 3. API 层

文件：

```text
app/api/routes.py
app/api/dependencies.py
```

主要入口：

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
POST   /api/worlds/script-decomposition/import
POST   /api/worlds/script-decomposition/import/jobs
GET    /api/worlds/script-decomposition/import/jobs/{job_id}
POST   /api/worlds/script-decomposition/import/jobs/{job_id}/cancel
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

`dependencies.py` 维护：

```python
_agents: dict[str, UniversalNPCAgent]
```

关键方法：

```python
get_agent(world_id)
reset_agent(world_id)
```

每个 `world_id` 对应一个运行中的 Agent 实例。

长任务 API 约定：

```text
job_id
status: queued / running / cancelling / cancelled / done / error
events[]
result
error
cancel_requested
created_at / updated_at
```

当前长任务：

- 剧本文档导入拆解：`/api/worlds/script-decomposition/import/jobs`
- 视觉资产生成：`/api/worlds/visual-assets/generate/jobs`

## 4. Agent 门面

文件：

```text
app/agent.py
```

类：

```python
UniversalNPCAgent
```

初始化：

```python
AgentRuntime(get_world_adapter(world_id))
```

方法：

```python
chat()
world_action()
autonomous_tick()
query_memory()
snapshot()
```

它只是门面，实际编排在 `AgentRuntime`。

## 5. Runtime 核心

文件：

```text
app/core/runtime.py
```

类：

```python
AgentRuntime
```

初始化组装：

```text
WorldAdapter
LLMClient
MemoryStore
RuntimeSessionStore
CorrectiveRagPipeline
Planner
RouterAgent
NpcAgent
npc_agents: dict[str, NpcAgent]
npc_sessions: dict[str, NpcRuntimeState]
StateValidatorAgent
AgentSessionState
NpcReviewAgent
UiStateProjector
UiReviewAgent
PlaytestAgent
FlowReviewAgent
```

聊天链路：

```text
chat()
  -> if request.group_chat or target_npc_ids: group_chat()
  -> 补默认 player_goal
  -> _hydrate_relevant_memories()
  -> _resolve_runtime_npc_id()
  -> _get_npc_agent(npc_id)
  -> _get_npc_session(npc_id)
  -> adapter.record_player_message(..., npc_state)
  -> router.route_chat()
  -> npc_agent.respond(..., npc_state)
  -> AgentLLMOutput schema gate
       -> if valid: skip protocol repair
       -> if invalid/raw: NpcProtocolReviewAgent.repair_raw_output()
  -> WorldRuntimeGuardrail.output_violations()
       -> if unknown location/action suggestion: retry LLM up to 2 times
       -> if still invalid: adapter.recover_guardrail_failure()
  -> router.route_output()
  -> state_validator.apply(..., npc_state)
       -> CommandValidator.validate()
       -> CommandExecutor.execute()
  -> NpcReviewAgent.review()
  -> adapter.build_chat_response(..., npc_state)
```

群聊链路：

```text
group_chat()
  -> 补默认 player_goal
  -> _hydrate_relevant_memories()
  -> _resolve_group_participant_ids()
       -> target_npc_ids 优先
       -> 否则使用玩家当前位置 nearby NPC
       -> 最多 max_npc_replies，当前硬上限 6
  -> adapter.record_player_message() 只写一次公共玩家发言
  -> for each npc_id:
       -> _get_npc_agent(npc_id)
       -> _get_npc_session(npc_id)
       -> npc_agent.respond(..., npc_state)
       -> guardrail / state_validator / npc_review
       -> 写入该 NPC 私有 session
  -> 聚合 ChatResponse.messages
  -> reply 兼容旧客户端：拼接多名 NPC 的文本
  -> command 为 {"name":"group","args":{"npc_ids":[...]}}
```

世界动作链路：

```text
world_action()
  -> adapter.handle_world_action()
  -> planner.mark_result()
```

会话快照：

```text
snapshot()
  -> UiStateProjector.project()
  -> UiReviewAgent.review()
  -> PlaytestAgent.simulate_adapter()
  -> FlowReviewAgent.review()
  -> SessionSnapshotResponse
```

## 6. Multi-Agent 层

文件：

```text
app/core/agents.py
```

类：

```python
RouterAgent
NpcAgent
StateValidatorAgent
```

`RouterAgent`：

- `route_chat()`：单 NPC 对话仍路由到 `npc_agent`。
- `route_output()`：当前把输出路由到 `state_validator_agent`。

Per-NPC Runtime：

```text
AgentRuntime.npc_agents: dict[str, NpcAgent]
AgentRuntime.npc_sessions: dict[str, NpcRuntimeState]
```

- 每个 NPC id 懒加载一个 `NpcAgent` 实例。
- 每个 NPC id 拥有独立 `NpcRuntimeState`。
- `NpcRuntimeState` 包含 `emotion`、`memories`、`goals`、`turn_count`、`last_reply`。
- `adapter.build_system_prompt()` 会把当前 NPC 的私有运行状态注入 prompt。
- `output.new_memories` 在有 `npc_state` 时写入当前 NPC 私有记忆，不默认广播到世界级 RAG。
- 世界状态仍然共享，command 仍统一经过 `StateValidatorAgent` 和 `CommandExecutor`。
- `world_state["npc_sessions"]` 保存调试快照，供前端或调试面板观察。

`NpcAgent.respond()`：

```text
adapter.build_system_prompt(..., npc_state)
adapter.build_human_prompt()
llm.invoke()
```

NPC 群聊边界：

- 当前群聊是“同一轮玩家输入 -> 多个 NPC 顺序各自回复”。
- 多个 NPC 可以看到公共最近对话；私有记忆不会自动互相泄漏。
- 当前不是无限自发圆桌，也不会让 NPC 无限制互相追问。
- 如果要做二阶圆桌，需要在 `group_chat()` 后追加 debate/response pass，让后发言 NPC 明确读取前序 NPC 本轮发言。

`NpcProtocolReviewAgent`：

- 不是每次都触发；只有 `AgentLLMOutput` schema 校验失败、structured output 抛错、raw JSON 字段漂移或只返回纯文本时才进入。
- 协议判断属于 Deterministic Guardrails Layer（确定性护栏层），使用 `AgentLLMOutputProtocolTool.validate_agent_output()`，不是让 LLM 自己判断自己是否合规。
- 协议修复属于 Deterministic Guardrails Layer（确定性护栏层），使用 `AgentLLMOutputProtocolTool.repair_agent_output()`，再把结果交给 `StateValidatorAgent`。
- 审查并修复 NPC LLM 原始输出协议。
- 支持从 Markdown fenced JSON、普通 JSON、字段别名和纯文本回复中恢复 `AgentLLMOutput`。
- 统一补齐 `action_type/content/inner_thought/command/suggested_actions` 等字段。
- 如果 command 参数不合法，先降级为 `none`，再交给 `StateValidatorAgent` 做世界级校验。
- 如果连可见 NPC 文本都无法恢复，则返回带 `provider_error` 的 `wait`，由 world adapter 按当前世界状态生成非变更型 NPC 兜底回复。

`StateValidatorAgent.apply()`：

```text
CommandValidator.validate()
CommandExecutor.execute()
adapter.apply_llm_output()
```

说明：

- `CommandValidator` 是门卫，只判断 command 是否允许、参数是否合法。
- `CommandExecutor` 是执行器，只执行已经通过校验的 command。
- `adapter.apply_llm_output()` 不再负责通用 command 执行，只做世界专属后处理，例如情绪、记忆、目标更新、文本兜底和 completion 判定。

## 6.1 Deterministic Guardrails Layer（确定性护栏层）

这一层负责校验、修复、拒绝和执行。以下约束必须由确定性代码判断，不能只写在 prompt 里：

```python
AgentLLMOutputProtocolTool
WorldGenerationProtocolTool
CommandValidator
CommandExecutor
SandboxWorldValidator
MechanicsDesignAgent
WorldRuntimeGuardrail
evaluate_task_completions
```

- `AgentLLMOutputProtocolTool.validate_agent_output()`：判断 NPC 输出是否符合 `AgentLLMOutput`。
- `AgentLLMOutputProtocolTool.repair_agent_output()`：修复 NPC 输出字段漂移、纯文本、Markdown JSON。
- `WorldGenerationProtocolTool.validate_world_generation()`：判断世界生成输出是否符合 `WorldGenerationResponse`。
- `WorldGenerationProtocolTool.repair_generation_payload()`：修复世界 JSON 字段漂移和 completion 表达差异。
- `CommandValidator`：判断 command 是否允许、参数是否存在、引用的 task/npc/action 是否存在。
- `CommandExecutor`：只执行已经通过校验的 command。
- `SandboxWorldValidator`：保存/运行前修复世界 JSON，根据已有 completion/action 兜底补齐最低玩家字段、NPC、任务、action、completion、地点闭环；它不负责原创机制字段。
- `MechanicsDesignAgent`：整理 `metadata.mechanics`，并让 action 产出字段对齐 task completion。
- `WorldRuntimeGuardrail`：运行时禁止 NPC 引导玩家去未登记地点或使用不存在的地点式建议。
- `evaluate_task_completions()`：用代码判断任务完成，NPC 不允许直接写任务进度。

Agent / ReviewAgent 可以提出内容、解释和建议，但最终是否合规、是否修复、是否执行，必须落到 Deterministic Guardrails Layer（确定性护栏层）。

## 7. Command 校验与执行

文件：

```text
app/core/commands.py
app/core/command_executor.py
```

类：

```python
CommandValidator
CommandValidationResult
CommandExecutor
```

入口：

```python
CommandValidator.validate(adapter, state, output)
CommandExecutor.execute(adapter, state, output)
```

作用：

- 所有 LLM command 先经过中心化校验。
- 校验通过后，由 `CommandExecutor` 中心化执行状态变更。
- adapter 不再直接硬编码通用 command 执行。
- 不允许的 command 会被降级为：

```json
{"name": "none", "args": {}}
```

当前校验：

```text
set_player       -> 必须有 args.patch object
grant_item       -> item 必须非空
complete_task    -> task_id 必须存在
switch_npc       -> npc_id 必须存在
set_flag         -> 必须有 key
run_world_action -> action_id 必须存在
```

当前执行：

```text
set_player       -> 合并 patch 到 state.world_state.player
grant_item       -> 写入 player.inventory
complete_task    -> 修改 tasks[].status
switch_npc       -> 修改 active_npc_id
set_flag         -> 写入 world_state.flags
run_world_action -> 调用 adapter.handle_world_action()
```

每个世界通过 adapter 暴露：

```python
allowed_commands()
world_action_ids()
```

## 8. 通用数据模型

文件：

```text
app/core/models.py
```

核心模型：

```python
AgentLLMOutput
ChatRequest
ChatMessage
ChatResponse
WorldActionRequest
WorldActionResponse
SessionSnapshotResponse
AgentSessionState
NpcRuntimeState
WorldAdapter
```

`AgentLLMOutput.command` 是状态变更入口：

```json
{
  "name": "set_player",
  "args": {
    "patch": {
      "trial_token": true
    }
  }
}
```

`WorldAdapter` 是世界热插拔协议，所有世界都要实现它。

`ChatRequest` 支持两种模式：

```ts
type ChatRequest = {
  message: string;
  player_name: string;
  location: string;
  player_goal: string;
  target_npc_id: string;      // 单聊目标
  target_npc_ids: string[];   // 群聊参与者
  group_chat: boolean;        // true 时进入 group_chat()
  max_npc_replies: number;    // 群聊最多回复数，运行时硬上限 6
};
```

`ChatResponse.messages` 是多 NPC 回复的主结构：

```ts
type ChatMessage = {
  role: "npc" | "system";
  npc_id: string;
  speaker: string;
  content: string;
  action_type: string;
  command: Record<string, any>;
};
```

- 单聊时 `messages` 也会包含一条 NPC 消息，兼容统一渲染。
- 群聊时 `reply` 会拼接多名 NPC 文本，用于兼容旧客户端。
- 新客户端应优先渲染 `messages[]`，再回退到 `reply`。

## 9. LLM Provider

文件：

```text
app/core/providers.py
app/core/llm.py
app/core/model_config.py
```

入口：

```python
create_llm_client()
OpenAICompatibleLLMClient.invoke()
```

当前使用：

```text
langchain_openai.ChatOpenAI
with_structured_output(AgentLLMOutput)
```

配置：

```env
LLM_PROVIDER=openai_compatible
LLM_API_KEY=...
LLM_BASE_URL=https://us.mixaicloud.com/v1
LLM_MODEL=qwen3.5-flash

WORLD_BUILDER_LLM_API_KEY=...
WORLD_BUILDER_LLM_BASE_URL=...
WORLD_BUILDER_LLM_MODEL=...
WORLD_BUILDER_LLM_TIMEOUT=900

VISUAL_PROMPT_LLM_API_KEY=...
VISUAL_PROMPT_LLM_BASE_URL=...
VISUAL_PROMPT_LLM_MODEL=...
VISUAL_PROMPT_LLM_TIMEOUT=120

NPC_LLM_API_KEY=...
NPC_LLM_BASE_URL=...
NPC_LLM_MODEL=...
NPC_LLM_TIMEOUT=90
```

MixAI 只是 OpenAI-compatible 配置，不写死在 Agent 核心。

`resolve_llm_config(purpose=...)` 支持按用途覆盖：

```text
world_builder -> WORLD_BUILDER_LLM_*
visual_prompt -> VISUAL_PROMPT_LLM_*
npc          -> NPC_LLM_*
fallback     -> LLM_*
```

## 9.0 图片生成 Provider

文件：

```text
app/core/image_generation.py
```

核心模型：

```python
ImageGenerationProviderConfig
ImageGenerationRequest
ImageGenerationResponse
OpenAICompatibleImageGenerationClient
generate_with_retry()
resolve_image_api_key()
```

默认配置：

```text
provider=stepfun
api_base_url=https://api.stepfun.com/step_plan/v1
model=step-image-edit-2
api_key_env=STEPFUN_API_KEY
api_key_file=~/.stepfun-img/secret.json
```

边界：

- 图片 provider 是跨 Agent 基础设施，所以放在 `app/core`。
- 视觉资产计划、artifact、run 管理属于 `app/agents/visual_asset_generation`。
- artifact 写入前会 redacted `api_key`、`authorization`、`*_api_key`。

## 9.1 运行态持久化

文件：

```text
app/core/session_store.py
```

类：

```python
RuntimeSessionStore
```

默认数据：

```text
data/sessions/{world_id}.session.json
```

保存内容：

```text
AgentSessionState
  -> emotion
  -> memories
  -> goals
  -> plan
  -> quest_progress
  -> world_state

npc_sessions: dict[str, NpcRuntimeState]
  -> emotion
  -> memories
  -> goals
  -> turn_count
  -> last_reply
```

恢复时机：

- `AgentRuntime` 初始化时先创建世界初始状态，再读取 `RuntimeSessionStore.load(world_id)`。
- 如果存在快照，用快照里的 `AgentSessionState` 和 `npc_sessions` 覆盖内存状态。
- LLM client、adapter、router、validator、review agent 不序列化，启动时重新创建。

保存时机：

- 单 NPC 对话完成后。
- 群聊完成后。
- 世界动作执行后。
- autonomous tick 完成后。

重置语义：

- `reset_agent(world_id)` 只清内存实例。
- `reset_agent_session(world_id)` 会同时删除 `data/sessions/{world_id}.session.json`。
- `/api/worlds/{world_id}/start` 使用 `reset_agent_session()`，所以点击“开始游戏/重新开始游戏”会清掉旧房间运行态。

## 10. 记忆与 CRAG

文件：

```text
app/core/memory.py
app/core/vector_memory.py
app/core/embeddings.py
app/core/rag.py
```

记忆创建入口：

```python
create_memory_store(world_id)
```

默认：

```text
JsonVectorMemoryStore -> data/memory/{world_id}.vector.json
```

CRAG 入口：

```python
CorrectiveRagPipeline.run(query, hints)
```

流程：

```text
retrieve
grade
if unreliable -> rewrite query
retrieve again
return RagContext
```

## 11. Planner 与 ToolRegistry

文件：

```text
app/core/planner.py
app/core/tools.py
```

`Planner` 用于：

```text
POST /api/worlds/{world_id}/agent/tick
```

方法：

```python
ensure_plan()
next_action()
mark_result()
```

`ToolRegistry` 是项目自己的世界动作注册表，不是 LangChain Tool。

方法：

```python
register()
run()
available_actions()
descriptions()
```

## 12. 世界注册

文件：

```text
app/worlds/registry.py
```

入口：

```python
get_world_adapter(world_id)
```

逻辑：

```text
data/worlds/{world_id}.json -> SandboxWorldAdapter
```

当前产品方向只保留热插拔 JSON 世界观。具体案例使用 `data/worlds/sandbox_1.json`（青岚修真界 MVP）。

## 13. Sandbox 世界

目录：

```text
app/worlds/sandbox/
```

### 13.1 models.py

定义：

```python
SandboxWorldConfig
SandboxNPC
SandboxTask
SandboxAction
WorldGenerateRequest
WorldGenerationResponse
```

### 13.2 store.py

类：

```python
SandboxWorldStore
```

方法：

```python
list_worlds()
load()
save()
delete()
create_default()
```

`save()` 会先调用：

```python
SandboxWorldValidator.ensure_valid()
```

### 13.3 validator.py

类：

```python
SandboxWorldValidator
WorldValidationResult
```

方法：

```python
validate()
repair()
ensure_valid()
```

作用：

- 保存前校验世界配置。
- 根据已有 completion/action 自动补齐最低可运行字段。
- 检查 NPC、任务、动作 id。
- 检查 action.effect 引用的 task / npc 是否存在。
- 在 metadata 写入 validation 结果。

### 13.4 generator.py

入口：

```python
generate_world_config_with_ai()
WorldBuilderAgent.generate()
_finalize_world_quality()
```

流程：

```text
fallback 模板
  -> AI structured generation
  -> WorldBuilderAgent 初步决定世界状态字段
  -> _repair_world_config()
  -> WorldGenerationProtocolTool.repair_world_config()
  -> SandboxWorldValidator.ensure_valid()
  -> MechanicsDesignAgent 整理 metadata.mechanics 并对齐 completion/action.effect
  -> WorldReviewAgent.review()
  -> PlaytestAgent.simulate_adapter()
  -> 写入 metadata.quality_gate
  -> SandboxWorldStore.save()
```

字段机制设计边界：

```text
WorldBuilderAgent
  -> 根据用户主题和世界类型初步决定状态字段
  -> 写入 player / tasks[].completion / actions[].effect
  -> 在 metadata.mechanics 声明字段含义、类型、别名

MechanicsDesignAgent
  -> 读取 WorldBuilderAgent 生成的世界草案
  -> 审查 completion、action.effect、metadata.mechanics 是否一致
  -> 补齐 mechanics 表
  -> 对齐 action 产出和 task completion
  -> 后续可升级为 LLM 子 Agent，输出 mechanics schema 和 overwrite patch

WorldReviewAgent
  -> 不决定字段
  -> 只检查 mechanics schema、任务 completion、action effect 是否一致
```

谁决定字段：

```text
WorldBuilderAgent 初步决定
MechanicsDesignAgent 结构化确认和修正
WorldReviewAgent 只做一致性审查
SandboxWorldValidator 只做确定性修复和最低可运行兜底
```

例如：

```text
偶像世界：
  skills.vocal
  skills.dance
  stage_presence
  fan_count

修仙世界：
  realm_level
  cultivation
  spirit_seal_integrity
  battle_power
```

生成规模和模板：

```text
WorldGenerateRequest
  -> template: 来自 WorldTemplateStore / data/world_templates.json
  -> complexity: simple / medium / complex / ultra
  -> min_npcs / min_tasks / min_actions: 用户可覆盖默认规模
  -> final_task_requires_previous: 最终任务是否必须依赖前置任务/进度门槛
  -> use_learned_profile: 是否使用 ExperienceLearningAgent 的体验学习画像
```

`WorldTemplateStore` 只提供叙事结构，不绑定题材。例如三幕式、短剧反转、探案、经营、关系线、冒险战斗、文档改编等模板都可以套到不同主题上。

体验学习：

```text
ExperienceFeedbackStore
  -> data/experience_feedback.json

ExperienceLearningAgent.profile()
  -> 根据玩家反馈统计推荐 NPC / task / action 数量
  -> 生成 generation_hint
  -> WorldBuilderAgent 生成时可作为默认规模参考
```

质量门：

```text
先修，再测。

Validator / SchemaRepairer 修复世界
  -> WorldReviewAgent 审查修复后的世界
  -> PlaytestAgent 自动试玩修复后的世界
  -> quality_gate.passed = world_review_passed && playtest_passed
```

写入 metadata：

```json
{
  "mechanics": {
    "skills.vocal": {
      "label": "唱功",
      "type": "number",
      "aliases": ["声乐", "演唱能力"],
      "description": "玩家在舞台训练中的唱功水平。"
    }
  },
  "world_review": {},
  "playtest_review": {},
  "quality_gate": {
    "validator_passed": true,
    "world_review_passed": true,
    "playtest_passed": true,
    "passed": true
  }
}
```

含义：

- 能修好并跑通：`quality_gate.passed = true`。
- 修了还跑不通：`quality_gate.passed = false`，后续可以拒绝保存或返回修复建议。

AI 输出协议：

```python
WorldGenerationResponse
```

### 13.5 adapter.py

类：

```python
SandboxWorldAdapter
```

关键方法：

```python
create_initial_state()
build_system_prompt()
build_human_prompt()
record_player_message()
apply_llm_output()
build_chat_response()
allowed_commands()
world_action_ids()
handle_world_action()
```

世界专属 action 仍由 adapter 做落地：

```python
handle_world_action()
```

支持：

```text
none
set_player
grant_item
complete_task
switch_npc
set_flag
run_world_action
```

注意：

- 上面的 command 由 `app/core/command_executor.py` 统一执行。
- `SandboxWorldAdapter.apply_llm_output()` 不再执行通用 command。
- adapter 保留的是世界专属能力：构建 prompt、构建响应、提供 action 列表、处理世界 action、同步 completion。

### 13.6 actions.py

类：

```python
SandboxActionService
```

入口：

```python
handle()
configured_action()
_apply_effect()
```

支持 effect：

```text
set_player
set_flags
active_npc_id
scene
complete_task
```

### 13.7 completion.py

入口：

```python
evaluate_task_completions(state, text)
```

作用：

- 根据任务 `completion` 条件统一判定任务是否完成。
- 支持 `items`、`missing_items`、`keywords`、`location`、`player`、`flags`、`relations`、`stats`、`actions`。
- `mode` 默认是 `all`，也支持 `any`。

### 13.8 importer.py

入口：

```python
import_world_from_document()
extract_document_text()
```

作用：

- 从 txt/md/json/html/pdf/docx 等文档抽取文本。
- 把用户导入设定转成可运行 `SandboxWorldConfig`。
- `use_ai=false` 时走 fallback 世界生成；`use_ai=true` 时走 `WorldBuilderAgent.generate()`。

### 13.9 mechanics.py

类：

```python
MechanicsDesignAgent
```

入口：

```python
MechanicsDesignAgent.design(config)
build_mechanics(config)
expected_completion_paths(task, mechanics)
produced_stat_paths(config)
```

作用：

- 从 `metadata.mechanics`、任务 `completion`、动作 `effect` 中整理机制字段表。
- 如果任务 completion 需要某个 stats/player 字段，而完成该任务的 action 没有产出该字段，会补到 `increase_player`。
- 不负责原创“这个世界应该有什么玩法字段”，只负责把 WorldBuilderAgent 已经生成的字段结构化、对齐。

### 13.10 guardrails.py

类：

```python
WorldRuntimeGuardrail
```

作用：

- 从玩家起点、NPC 地点、action.effect.set_player.location 收集已知地点。
- `move_player` 只能移动到已知地点。
- NPC 回复如果建议未登记地点，Runtime 会触发最多 2 次 LLM 重试。
- 两次仍失败时，adapter 返回确定性的安全地点引导。

### 13.11 experience.py

类：

```python
ExperienceFeedbackStore
ExperienceLearningAgent
```

数据：

```text
data/experience_feedback.json
```

作用：

- 保存玩家试玩反馈。
- 根据沉浸评分、节奏反馈、NPC/任务/action 数量生成体验学习画像。
- 世界生成时可用该画像作为默认复杂度和规模提示。

### 13.12 template_store.py

类：

```python
WorldTemplateStore
```

数据：

```text
data/world_templates.json
```

默认模板：

```text
freeform
three_act_growth
short_drama_reversal
mystery_investigation
management_growth
relationship_route
adventure_battle
document_adaptation
```

作用：

- 管理可编辑的世界生成模板。
- 模板只描述叙事结构，不绑定题材。
- API 支持创建、更新、删除模板。

### 13.13 project_intake.py

类：

```python
ProjectIntakeAgent
IntegrationAdapterAgent
```

入口：

```python
analyze_project_integration(request)
ProjectIntakeAgent.summarize()
IntegrationAdapterAgent.plan()
```

作用：

- 接收外部项目描述、文档摘要、仓库提示、API 提示。
- 总结项目类型、主题、玩家字段、NPC、地点、候选 mechanics、候选 actions。
- 输出 `recommended_world_request`，作为后续 `WorldBuilderAgent` 的输入建议。
- 输出 `adapter_plan`，说明是否只需 sandbox JSON，还是需要实现真实 `WorldAdapter` 映射外部 API。

流程：

```text
已有游戏 / 设定文档 / 接口说明
  -> ProjectIntakeAgent
  -> ProjectIntakeSummary
  -> IntegrationAdapterAgent
  -> IntegrationAdapterPlan
  -> WorldBuilderAgent / WorldAdapter 开发
```

边界：

- 它不直接生成世界。
- 它不直接调用外部游戏 API。
- 它只把“接入项目”整理成后续 Agent 和开发者能使用的结构化上下文。

### 13.14 script_decomposition.py

类：

```python
ScriptDecompositionAgent
```

入口：

```python
build_script_world(request)
ScriptDecompositionAgent.build(request)
```

触发方式：

```text
WorldGenerateRequest.template == "script_decomposition"
WorldGenerateRequest.script_decomposition 非空
导入文档包含 公共背景 / 角色 / 线索 / 案件真相 等案件型剧本结构
POST /api/worlds/script-decomposition
```

作用：

- 剧本拆解作为第一个具体垂直编译案例，不再走泛用主题生成。
- 当前实现优先支持“案件型剧本”：按固定结构保留公共背景、案件真相、角色公开信息、角色秘密、动机、不在场证明、线索、地点、时间线、结局和禁止提前泄露规则。
- 输出 `SandboxWorldConfig`，并把完整案件结构保存在 `metadata.script_case`。
- 生成的 NPC 仍进入 per-NPC runtime，每个角色拥有独立私有记忆和持久化状态。

专用输入建议：

```text
标题:
公共背景:
案件真相:
地点:
角色:
  姓名:
  身份:
  公开信息:
  秘密:
  动机:
  不在场证明:
  地点:
线索:
  标题:
  内容:
  地点:
  关联角色:
  揭示:
时间线:
禁止提前泄露:
结局:
```

质量边界：

- 这条链路优先确定性解析用户给出的剧本结构，不让 LLM 自由改写真相。
- `ScriptDecompositionReport` 会报告缺少公共背景、案件真相、角色或线索等硬错误。
- 当前支持 MVP 案件型剧本：搜证、询问、推理、揭晓真相。
- 更复杂的多幕本、玩家角色本、私聊房间和主持人结算，可以继续在该 schema 上扩展。

### 13.15 visual_assets.py

类：

```python
VisualPromptComposerAgent
VisualAssetGenerationAgent
```

产品化入口：

```text
app/agents/visual_prompt_composer/
app/agents/visual_asset_generation/
```

API：

```text
POST   /api/worlds/visual-assets/plan
POST   /api/worlds/visual-assets/generate
POST   /api/worlds/visual-assets/generate/jobs
GET    /api/worlds/visual-assets/generate/jobs/{job_id}
POST   /api/worlds/visual-assets/generate/jobs/{job_id}/cancel
GET    /api/worlds/visual-assets/runs
GET    /api/worlds/visual-assets/runs/{run_id}
DELETE /api/worlds/visual-assets/runs/{run_id}
```

作用：

- 从世界、角色、地点、风格要求生成视觉资产计划。
- 将计划中的角色 / 场景资产转成图片生成 prompt。
- 调用图片 provider 生成图片。
- 保存 visual plan artifact 到 `data/visual_assets`。
- 保存图片运行结果到 `output/visual_assets`。
- 支持 job events 和 cancel，避免长时间图片生成没有进度。

边界：

- 视觉资产生成是独立阶段，不应该隐藏在世界生成里自动执行。
- plan artifact 可以人工检查、修改、重跑。
- 图片输出属于运行产物，默认不应跟踪进 Git。

### 13.16 剧本图谱编译

目录：

```text
app/agents/script_decomposition/
```

核心类：

```python
ScriptGraphCompiler
ScriptGraphStore
ScriptGraphDocument
ScriptGraphNode
ScriptGraphEdge
```

API：

```text
POST /api/worlds/script-graph/compile
GET  /api/worlds/script-graphs
GET  /api/worlds/script-graphs/{artifact_id}
```

流程：

```text
ScriptDecompositionAgent
  -> ScriptDecompositionResult / artifact
  -> ScriptGraphCompiler
  -> ScriptGraphDocument
  -> ScriptGraphStore
```

边界：

- `ScriptDecompositionAgent` 负责理解故事。
- `ScriptGraphCompiler` 只做确定性图谱编译。
- Compiler 不重新解释故事，不改写真相、人物关系、线索含义和结局。

## 14. Review / Playtest Agents

文件：

```text
app/core/review_agents.py
```

产品化入口：

```text
app/agents/world_review/
app/agents/npc_review/
app/agents/ui_projection/
app/agents/playtest_validation/
```

类：

```python
WorldReviewAgent
MechanicsDesignAgent
NpcProtocolReviewAgent
NpcReviewAgent
UiStateProjector
UiReviewAgent
PlaytestAgent
FlowReviewAgent
```

职责：

- `MechanicsDesignAgent`：整理和修正机制字段表，确保 `metadata.mechanics`、`tasks[].completion`、`actions[].effect` 对齐；它不强行规定某类世界必须有哪些字段。
- `WorldReviewAgent.review()`：审查世界是否缺 NPC、任务、动作、completion，并检查 mechanics schema、任务 completion、action effect 是否一致；它不负责决定字段。
- `NpcProtocolReviewAgent.repair_raw_output()`：审查和修复 NPC LLM 输出协议，保证进入状态层前是合法 `AgentLLMOutput`。
- `NpcReviewAgent.review()`：审查 NPC 输出是否为空、是否越权写进度、是否文本给物品但 command 缺失。
- `UiStateProjector.project()`：把世界状态投影成前端更容易展示的 UI state。
- `UiReviewAgent.review()`：审查 UI 所需字段是否能满足任务 completion。
- `PlaytestAgent.simulate_adapter()`：复制初始状态，按 action 自动试玩，判断修复后的世界能否闭环。
- `FlowReviewAgent.review()`：总结当前任务完成情况和剩余卡点。

## 15. 青岚修真界案例世界

文件：

```text
data/worlds/sandbox_1.json
```

作用：

- 作为热插拔 JSON 世界观的可跑通案例。
- 验证 NPC 对话、地点移动、物品栏、任务 completion、世界动作与状态校验的最小闭环。
- 不再保留代码型内置世界；新世界都应通过 JSON 世界观配置接入。

## 16. 前端

目录：

```text
static/
```

文件：

```text
index.html
app.js
style.css
```

主要前端方法：

```javascript
loadWorlds()
selectWorld()
generateWorld()
createWorld()
saveWorld()
startWorld()
sendChat()
moveToLocation()
lookOrFind()
queryMemory()
renderRuntime()
restoreSession()
saveSession()
```

前端会使用：

```text
localStorage: npc-agent-session:{world_id}
GET /api/worlds/{world_id}/session
```

做刷新后的状态恢复。

外部游戏 / 独立客户端的 API 请求体、响应体和接入规则单独维护在：

```text
EXTERNAL_CLIENT_INTEGRATION.md
```

新的产品前端或独立游戏 Agent 应优先阅读该文档。`static/app.js` 只是本仓库内置 demo UI，不是产品前端必须沿用的实现。

## 17. 测试

目录：

```text
tests/
```

当前主测试：

```text
tests/test_productized_foundation.py
```

覆盖：

- 非法 command 会被拒绝。
- `CommandExecutor` 会执行合法 command。
- 合法 `set_player` command 会生效。
- 最小世界配置会被 validator 修复。
- 生成世界会写入 `quality_gate` 并自动试玩。
- `PlaytestAgent` 能识别可闭环世界和阻塞世界。
- `MechanicsDesignAgent` 会补齐 mechanics 并让 action 产出对齐 completion。
- `WorldRuntimeGuardrail` 会拦截未登记地点。
- `ExperienceLearningAgent` 会根据反馈生成体验学习画像。
- 世界模板 CRUD API 可用。
- API 世界生命周期：保存、开始、动作、session。

运行：

```powershell
python -m pytest -q
```

## 18. 数据目录

```text
data/worlds/sandbox_1.json
```

默认示例世界，进入 Git。

```text
data/worlds/*
```

用户运行时生成的世界默认忽略，避免试玩数据混入框架提交。

```text
data/memory/
```

运行时记忆，忽略。

## 19. 当前已知边界

- `CommandExecutor` 已经中心化执行通用 command，但世界专属动作仍由 adapter/action service 落地。
- `app/agents` 已经成为新的模块边界，但仍有多个模块 re-export 旧实现；这是迁移中状态，不是最终物理分层。
- `WorldBuilderAgent` 已使用 AI structured output，但仍需要 fallback 模板兜底。
- `PlaytestAgent` 当前是确定性 action 顺序试玩，还不是 LLM 玩家，也不会搜索复杂分支图。
- Sandbox effect 还缺条件判断、奖励、失败条件、复杂分支图。
- 本地 JSON memory 适合 MVP，不适合多人长期运营。
- 前端是原生 JS，适合产品 MVP，不适合复杂后台长期维护。
- 剧本图谱当前是 artifact 编译结果，还未接入真正图数据库。
- 视觉资产生成已支持 job/cancel/run artifact，但图片审美质量仍依赖 provider 和 prompt 质量。

## 20. 下一步建议

优先级：

1. 将 `app/agents` 中仍 re-export 的模块逐步迁出旧实现，形成完整 `agent.py/schema.py/compiler.py/store.py` 边界。
2. 让 `quality_gate.passed = false` 时在 API 层拒绝保存或返回修复建议。
3. 将 `PlaytestAgent` 从线性动作试玩升级为目标搜索 / 分支图试玩。
4. 将 ScriptGraph artifact 接入图数据库或可视化图谱查询。
5. 引入数据库：PostgreSQL + pgvector。
6. 前端编辑器复杂后迁移到 TypeScript 框架。

## 21. 三模块架构：Pipeline / Creator / Play

本框架在概念与产物流上由三个模块构成，对应「创作 → 编排 → 消费」的闭环。

### 21.1 Pipeline（创作 Agent 集合）

- 位置：各创作能力平铺在 `app/agents/*`，例如 `script_decomposition`（剧本拆解）、`world_builder`（世界构建）、`story_authoring` / `story_expansion`（剧情创作）、`visual_asset_generation`（视觉资产）、`world_review` / `npc_review`（审查）、`playtest_validation`（试玩验证）、`npc_runtime` / `npc_lorebook`（角色运行时与知识库）、`experience_learning`（经验沉淀）。
- `app/pipeline/` 是这些创作 Agent 的 **REST 入口**（`routes.py`）：调用 `script_decomposition` / `world_builder` / 视觉资产 Agent，产出 `SandboxWorldConfig` 及各类 artifact（剧本拆解、剧本图谱、视觉资产等）并落盘到对应 store。
- 换言之，Pipeline 是「一组可被独立调用的创作能力」；`app/pipeline/` 提供的是它的对外 HTTP 面。

### 21.2 Creator（创作工作流）

- 位置：`app/agents/creator_assistant/`。
- 它不是一个 Agent，而是一个**编排工作流**：通过 `CreatorToolRegistry`（11 个工具）把 Pipeline 里的创作 Agent 串成一条「创作剧情 → 扩写 → 审查 → 视觉资产 → 试玩 → 保存 / 发布」的创作者流程。
- 关键工具：`author_story`（调 `StoryAuthoringAgent`）、`expand_story`（调 `StoryExpansionAgent`）、`plan_visual_assets` / `generate_visual_assets` / `bind_visual_assets`（调 `VisualAssetGenerationAgent`）、`review_playable_world`（调 `WorldReviewAgent` + `PlaytestAgent`）。
- `save_world`：把 Creator Graph 编译为 `SandboxWorldConfig` 并存入世界库。
- `publish_to_play`：让该世界出现在 `/play` 的可玩世界列表。
- 对外还暴露 **MCP 形态工具边界**（`mcp.py` 的 `CreatorMcpToolServer`，`tools/list` + `tools/call`），详见 [MCP_ARCHITECTURE.md](./MCP_ARCHITECTURE.md)。

> 注意：Creator 直接 import 并调用 Pipeline 里的 Agent 类（而非经由 `app.pipeline` 模块转发），因此二者对底层 Agent 是「并行入口」关系；`app/pipeline` 与 `creator_assistant` 目前都直接驱动同一批创作 Agent。

### 21.3 Play（玩家运行时）

- 位置：`app/player_experience/`。
- `PlayerStoryRuntime` **消费整个 `SandboxWorldConfig`**（即 Pipeline/Creator 的产出物），在其上做确定性的 GALGAME 式遍历（`start` / `resume` / `advance` / `choose`），由 `PlayerSessionStore` 做会话持久化与跨进程恢复。
- 经 `app/api/routes.py` 挂载到 `/play`，对玩家暴露可玩世界。

### 21.4 闭环

```
Creator 工作流（creator_assistant）
   ├─ 调用 ──> Pipeline 创作 Agent（app/agents/* ，REST 入口 app/pipeline/）
   ├─ save_world ──> SandboxWorldConfig（世界库）
   ├─ publish_to_play ──> 进入 /play 列表
   └─ Play 运行时（player_experience）消费整个世界跑互动
玩家反馈 ──> experience_learning（经验沉淀）回流 Pipeline，驱动后续生成质量提升。
```

MCP 是 Creator 的**标准化出口**：任何标准 MCP Client 可绕过 UI 直接驱动创作能力，不锁死任何客户端。

