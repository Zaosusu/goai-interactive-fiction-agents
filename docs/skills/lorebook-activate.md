# Skill 契约：`lorebook-activate`

> 映射模块：`app/agents/npc_lorebook`（`NpcLorebookRuntime.activate()`）· 闭环位置：步骤 4 · 剧情 / 运行时

| 字段 | 内容 |
|---|---|
| **输入** | 当前世界的 Lorebook + 触发上下文（玩家输入 / 目标 / 对话 / 当前 location / npc_id） |
| **输出** | 激活的知识切片（按关键词与触发条件选择），注入 Agent 上下文 |
| **调用条件** | 对话 / 叙事触发时激活 |
| **依赖工具** | `npc_lorebook` 模块（`runtime` / `compiler` / `review`）。`CorrectiveRagPipeline` 是针对 `MemoryStore` 的**独立**检索机制（评分 + 查询改写二次检索），二者可并列注入上下文，但**不是依赖关系** |
| **失败处理** | 无匹配条目则降级为空上下文，不阻塞主流程 |
| **安全边界** | 仅注入当前世界已选择的 Lorebook 条目，不跨世界泄漏 |
| **验证方式** | `pytest` 条目选择与注入单测 |
| **复用价值** | 知识库热插拔进任意世界，设定沉淀可复用 |
