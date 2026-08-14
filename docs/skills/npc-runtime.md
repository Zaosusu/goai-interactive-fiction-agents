# Skill 契约：`npc-runtime`

> 映射模块：`app/agents/npc_runtime` + 通用 NPC 运行时（`app/core/runtime.py`）· 闭环位置：步骤 7 · NPC 运行时

| 字段 | 内容 |
|---|---|
| **输入** | NPC 配置 + 玩家输入 + 该 NPC 的私有记忆 |
| **输出** | NPC 对白 / 状态变更，由 **Generic NPC Runtime** 写入 `RuntimeSessionStore`（注意：Creator Graph Player 使用独立的 `PlayerSessionStore`，二者不是同一存储，也不共同进入本 Skill） |
| **调用条件** | Generic NPC Runtime 运行时会话激活，消费普通 Pipeline / Sandbox 世界 |
| **依赖工具** | `npc_runtime` 模块（`turn_director` / `memory_lifecycle` / `lorebook_runtime`）；`WorldRuntimeGuardrail`（运行时确定性护栏）；`npc_lorebook`（知识注入）；`CorrectiveRagPipeline`（记忆检索增强） |
| **失败处理** | 护栏拒绝则按反馈重试，**最多两次**；仍失败则返回受控错误并保留已持久化的 `RuntimeSessionStore` 状态 |
| **安全边界** | 私有记忆隔离，不串角色；`WorldRuntimeGuardrail` 约束对白、建议动作与位置执行不越界 |
| **验证方式** | `pytest` 对话一致性回归；`playtest_validation` 作为**独立**可玩闭环模拟（不等于 NPC 对话运行验证本身） |
| **复用价值** | 同一 NPC 配置可跨作品复用，私有记忆随作品隔离 |
