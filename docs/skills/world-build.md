# Skill 契约：`world-build`

> 映射模块：`app/worlds/sandbox/generator.py`（`WorldBuilderAgent`）· 闭环位置：步骤 3 · 世界

| 字段 | 内容 |
|---|---|
| **输入** | 剧本拆解结果 / 世界观种子 |
| **输出** | 返回 `SandboxWorldConfig`（世界结构 + 实体 / 地点定义）；实际持久化到 World Store 由 Pipeline / API 调用方完成 |
| **调用条件** | 剧本拆解（步骤 2）完成后触发 |
| **依赖工具** | `WorldBuilderAgent`（`app/worlds/sandbox/generator.py`）及其内部 `_prepare_world_for_runtime` / `_attach_world_quality_gate`。`WorldBuilderAgent.generate()` 经 `_finalize_world_with_lorebook()` 在**构建流程内直接调用** `SandboxWorldValidator` 校验、`WorldReviewAgent().review()` 复核与 `PlaytestAgent().simulate_adapter()` 模拟——这是世界构建自带的质量门，并非完全由外部编排方单独调用 |
| **失败处理** | 构建阶段先生成候选并经 `_repair_world_config` 按约束修复、无 `world_builder_llm` 时回退模板；最终门结果写入 `metadata.quality_gate`（`passed` 标志）。框架不循环自动重生成；`review_playable_world` / `validate_creator_graph` 是 `playtest-validate` Skill 描述的另两条独立验证路径 |
| **安全边界** | 以输入设定为事实来源，通过 Schema / Evidence / Review 限制无依据扩写 |
| **验证方式** | `pytest` 单测；World Store 结构 / 一致性校验 |
| **复用价值** | 世界可独立持久化复用，换作品时仅替换世界观种子 |
