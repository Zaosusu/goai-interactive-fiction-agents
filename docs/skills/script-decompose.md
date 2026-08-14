# Skill 契约：`script-decompose`

> 映射模块：`app/agents/script_decomposition` · 闭环位置：步骤 2 · 拆解

| 字段 | 内容 |
|---|---|
| **输入** | 原始剧本 / 题材设定（通常由 `project_intake` 完成结构化接入后传入） |
| **输出** | 结构化拆解结果 `ScriptDecompositionResult`（通常包装于 `ScriptDecompositionBuildResponse`，含 `world` / `report` / `decomposition` / `artifact`）。`ScriptGraphDocument` 是**后续 `/script-graph` 编译阶段**（`ScriptGraphCompiler.compile()`）的产物，不属于本阶段直接输出 |
| **调用条件** | Pipeline 剧本拆解阶段触发；`project_intake` 已完成项目与设定接入 |
| **依赖工具** | `script_decomposition` 模块内的 LLM 理解 / Review / compiler / store / schema |
| **失败处理** | 含一次**更严格提示**的重试；Schema 校验失败或不完整则阻断告警，不进入下游，已持久化状态保留可重跑（不直接使用 World Runtime Guardrail） |
| **安全边界** | LLM 负责理解与抽取，但以输入设定为事实来源，通过 Schema / Evidence / Review 限制无依据扩写 |
| **验证方式** | `pytest` 中 `script_decomposition` 单测；`ScriptDecompositionResult` / `ScriptDecompositionBuildResponse` Schema 校验 |
| **复用价值** | 任意叙事作品复用同一拆解契约，能力沉淀不随作品重写 |
