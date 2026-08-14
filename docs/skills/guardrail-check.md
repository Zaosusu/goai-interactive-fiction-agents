# Skill 契约：`guardrail-check`

> 映射模块：`WorldRuntimeGuardrail`（`app/worlds/sandbox/guardrails.py`）· 闭环位置：运行时 · Generic NPC Runtime 对话护栏

| 字段 | 内容 |
|---|---|
| **输入** | NPC 运行时的待产出对白文本、建议动作与位置执行请求 |
| **输出** | 合规判定 + 重试指令（越界时拒绝并给出纠正反馈） |
| **调用条件** | Generic NPC Runtime 每次产出对白 / 建议动作 / 位置执行时调用（由 `app/core/runtime.py` 与 sandbox `actions` / `adapter` 触发） |
| **依赖工具** | `WorldRuntimeGuardrail`；被 Generic NPC Runtime 与 sandbox 动作执行调用。源码未显示 `world_builder`、`story_*` 等所有生成阶段都调用它，因此**不是跨全部生成阶段的全局护栏** |
| **失败处理** | 三条路径分别处理：① **对白内容**（`_repair_output_with_guardrail`，`runtime.py:197`）：检测对白文本与建议动作中的未登记地点，最多**两次重试**（`range(1,3)`），两次仍失败则**抛出 `RuntimeError`** 终止本次回复，且该异常路径**不会自动保存**新增的错误记忆（无 `_save_session`）；② **建议动作**（`adapter.py:220` `sanitize_suggested_actions`）：确定性过滤掉提及未登记地点的建议，保留至多 4 条，不足时回退默认动作，不抛异常；③ **非法位置动作**（`actions.py:109` `location_rejection`）：玩家执行未登记地点的 `move_player` 世界动作时直接**返回拒绝响应**（提示已知地点），不进入移动执行 |
| **安全边界** | 确定性检测，约束地点 / 建议动作 / 对白不超出当前世界登记范围 |
| **验证方式** | `pytest` 护栏断言用例（越界地点、未登记动作等） |
| **复用价值** | 作为 Generic NPC Runtime 统一的运行时护栏，跨 NPC 复用 |
