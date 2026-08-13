# Agent 开发标准

本文是项目内 Agent 开发标准文档（参赛仓库版），即本仓库的权威依据。以后新增 Agent、重构 pipeline、放置 tool/compiler/store/review 时，优先按本文判断。

## 1. 一句话标准

```text
Agent 模块是第一架构边界。
```

优先把能力放在：

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

不要默认新增一堆横向大目录：

```text
app/tools/
app/stores/
app/validators/
```

除非它们确实是两个以上 Agent 都要复用的基础设施。

## 2. 当前代码状态

当前项目正在从早期的 `app/core + app/worlds/sandbox` 结构，演进到 `app/agents/<agent_module>` 产品化模块结构。

所以现在有两种形态同时存在：

```text
app/agents/
  新的产品化 Agent 模块入口。
  部分模块已经拥有自己的 schema/compiler/store。
  部分模块暂时只是 re-export 旧实现，作为迁移边界。

app/core/
  跨 Agent 共享运行时、模型、LLM provider、RAG、command、protocol、session。

app/worlds/sandbox/
  当前 sandbox JSON 世界的主要实现层。
  仍承载 world builder、script decomposition、visual assets、experience 等旧实现。
```

重要结论：

```text
新增大能力时，概念边界按 app/agents/<agent_module> 设计。
短期可以复用 app/worlds/sandbox 的旧实现。
不要把新的 Agent 专属复杂逻辑继续堆进 app/core。
```

## 3. Agent / Tool / Compiler / Store 怎么区分

### Agent

Agent 负责判断、解释、生成、审查、语义理解、策略选择。

典型例子：

```text
WorldBuilderAgent
ScriptDecompositionAgent
NpcAgent
WorldReviewAgent
VisualPromptComposerAgent
ExperienceLearningAgent
```

### Tool / Compiler

Tool 或 Compiler 必须是确定性、可重复、可测试的转换或执行。

典型例子：

```text
ScriptGraphCompiler
CommandExecutor
CommandValidator
WorldGenerationProtocolTool
AgentLLMOutputProtocolTool
SandboxWorldValidator
```

不要把确定性代码命名成 `*Agent`。

### Store

Store 负责读写文件、数据库、session、artifact、图谱或生成结果。

典型例子：

```text
RuntimeSessionStore
ScriptGraphStore
ScriptDecompositionArtifactStore
VisualAssetArtifactStore
SandboxWorldStore
WorldTemplateStore
```

### Provider / Client

Provider 或 Client 负责对接外部模型、图片、embedding、向量库或其他 API。

典型例子：

```text
OpenAICompatibleLLMClient
OpenAICompatibleImageGenerationClient
EmbeddingClient
```

## 4. app/core 的边界

`app/core` 只能放跨 Agent 共享基础设施。

适合放入 `app/core`：

```text
模型配置解析
LLM provider client
Image provider client
Embedding provider client
运行时 session primitive
Command protocol
RAG primitive
通用协议修复 primitive
WorldAdapter 抽象
```

不适合放入 `app/core`：

```text
ScriptGraphCompiler
ScriptDecompositionValidator
VisualStyleGuideCompiler
WorldMechanicsCompiler
PlaytestScenarioRules
某个垂直 Agent 的专属 prompt/schema/store
```

这些应该放进拥有该能力的 Agent module。

## 5. 当前 Agent 模块清单

```text
app/agents/project_intake/
  外部项目接入分析入口。
  当前 re-export app/worlds/sandbox/project_intake.py。

app/agents/world_builder/
  世界生成入口。
  当前 re-export app/worlds/sandbox/generator.py。

app/agents/npc_lorebook/
  NPC 世界书 Agent 模块。
  拥有 agent.py / compiler.py / review.py / runtime.py / schema.py。
  消费 WorldBuilderAgent 或剧本/视觉流水线产出的世界资产，生成 NpcLorebookArtifact。
  拥有世界书规则和长记忆沉淀规则，包括条目、关键词/正则关键词、激活策略、插入位置、扫描深度、连锁触发、token 预算、阶段总结和结构化记忆表。
  不继承 WorldBuilderAgent，不把世界书逻辑隐藏成 NPC runtime 内部实现。

app/agents/script_decomposition/
  剧本拆解产品化模块。
  已包含 graph schema/compiler/store/tools。
  Agent 主体当前仍复用 app/worlds/sandbox/script_decomposition.py。

app/agents/visual_prompt_composer/
  视觉提示词编排入口。
  当前 re-export app/worlds/sandbox/visual_assets.py。

app/agents/visual_asset_generation/
  视觉资产生成入口。
  已包含 VisualAssetArtifactStore。
  生成 Agent 当前复用 app/worlds/sandbox/visual_assets.py。

app/agents/npc_runtime/
  NPC runtime 入口。
  当前 re-export AgentRuntime / NpcAgent / RouterAgent / StateValidatorAgent。

app/agents/npc_review/
  NPC 输出协议与质量审查入口。
  当前 re-export app/core/review_agents.py。

app/agents/world_review/
  世界结构审查入口。
  当前 re-export app/core/review_agents.py。

app/agents/ui_projection/
  UI 状态投影与审查入口。
  当前 re-export app/core/review_agents.py。

app/agents/playtest_validation/
  自动试玩与流程审查入口。
  当前 re-export app/core/review_agents.py。

app/agents/experience_learning/
  体验反馈学习入口。
  当前 re-export app/worlds/sandbox/experience.py。
```

## 6. 新增 Agent 的推荐步骤

1. 先创建模块：

```text
app/agents/<agent_module>/
  __init__.py
  agent.py
```

2. 如果有结构化输入输出，增加：

```text
schema.py
```

3. 如果有确定性转换，增加：

```text
compiler.py
tools.py
validator.py
```

4. 如果有持久化 artifact，增加：

```text
store.py
```

5. 如果有 Agent 专属审查，增加：

```text
review.py
```

6. 如果是长任务，必须提供：

```text
job id
status
events/logs
result
error
cancel endpoint or cancel flag
```

7. 如果暴露 API，可以先接入 `app/api/routes.py`；当路由变多时，再迁到 Agent module 自己的 `routes.py` 并在 API 层挂载。

## 7. Pipeline 设计标准

每个前端阶段都应该对应一个 Agent module 或明确 artifact 阶段：

```text
项目接入分析
世界生成
剧本输入 / 剧本拆解
拆解 JSON
故事图谱
视觉提示词
图片生成
视觉绑定 / 资产索引
世界书生成
NPC runtime
自动试玩
体验反馈学习
```

每个阶段至少要暴露：

```text
输入 artifact
输出 artifact
使用的 Agent / API
日志或 events
校验 / review
人工编辑点
重跑入口
长任务停止入口
```

不要隐藏式连跑后续阶段。比如：

```text
剧本拆解完成后，不应该静默生成世界。
视觉规划完成后，不应该静默生成全部图片。
世界生成完成后，可以产出世界书 artifact，但前端仍应把世界书作为可检查阶段展示。
```

后续阶段应该由用户或前端显式触发。

当前项目全流程总览：

```text
剧本 / 文档输入
  -> ScriptDecompositionAgent
  -> ScriptGraphCompiler
  -> 可选视觉资产阶段
  -> WorldBuilderAgent
  -> attach_visual_bindings
  -> NpcLorebookCreationAgent / NpcLorebookCompiler fallback
  -> NpcLorebookRuntime
  -> NpcAgent 对话 / PlaytestAgent 验证
```

注意：

- `ScriptGraphCompiler` 和 `attach_visual_bindings` 都不是 Agent。
- `NpcLorebookCreationAgent` 是世界书 artifact 的 owner，不应把世界书逻辑继续塞进 `npc_runtime`。
- `PlaytestAgent` 当前是 adapter review / 自动试玩，不是独立游戏客户端。

## 8. 剧本拆解方向

剧本拆解模块的原则：

```text
ScriptDecompositionAgent 理解故事。
script_json / decomposition artifact 保留故事关系。
ScriptGraphCompiler 只做确定性图谱编译。
Graph compiler 不重新解释故事。
```

也就是说：

- 语义理解发生在 Agent。
- 图谱节点/边生成发生在 Compiler。
- Compiler 不能擅自改写真相、人物关系、动机和结局。

## 9. 安全与可观测性要求

所有 Agent / tool / store 修改都要检查：

- 新确定性代码没有命名为 `*Agent`。
- Agent 专属逻辑有明确 module owner。
- 没有把多个不相关阶段揉进一个函数。
- API key、Authorization、密钥不会写入 artifact 或日志。
- 长任务有 progress events；能取消时提供 cancel。
- artifact 可以被查看、复用、重跑。
- 测试覆盖变更的模块边界或 artifact contract。

## 10. 对后续 Agent 的提醒

如果你是另一个 coding agent，修改本项目架构前请先读：

```text
AGENT_DEVELOPMENT_STANDARD.md        （参赛仓库内即权威标准文档）
TECHNICAL_ARCHITECTURE.md
EXTERNAL_CLIENT_INTEGRATION.md
```

本项目的关键难点不是“让 AI 输出内容”，而是：

```text
编排清楚
边界清楚
约束清楚
artifact 可检查
状态变更可追踪
```
