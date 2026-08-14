# Skill 契约：`playtest-validate`

> 映射模块：`app/agents/playtest_validation` + 创作闭环工具 · 闭环位置：步骤 6 · 审查

| 字段 | 内容 |
|---|---|
| **输入** | 已生成的世界 / 剧情图（待发布前） |
| **输出** | 审查报告（结构校验结果 / 可玩性报告 + 阻断项） |
| **调用条件** | 创作完成、待 `publish_to_play` 之前触发 |
| **依赖工具** | 两条相互独立路径：`validate_creator_graph` 校验 Creator Graph 结构；`review_playable_world` 调用 `WorldReviewAgent` + `PlaytestAgent` 检查 Sandbox 世界。二者不合并 |
| **失败处理** | 返回审查报告（阻断项 / 问题），由创作方**修改与重跑**；不是自动「退回直到通过」 |
| **安全边界** | 确定性模拟（WorldReviewAgent / PlaytestAgent 为确定性 Validator / Simulator），不调 LLM 评审 |
| **验证方式** | `pytest` 可玩闭环自动模拟；报告断言 |
| **复用价值** | 作为发布前质量门，每次发布可回归验证 |
