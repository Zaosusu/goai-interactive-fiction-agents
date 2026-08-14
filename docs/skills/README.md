# Skill 契约（6 项）

本目录是 GOAI 初赛方案 PPT 第 7 页（核心 Skill 契约）所引用的完整契约来源。
六项核心 Skill 是面向 **AgentTeams（原 Hiclaw）** 的正式设计契约；当前仓库为自定义实现，复赛阶段将按本契约封装为 AgentTeams Worker / Skill。

> 说明：当前框架尚未集成 AgentTeams 运行库，以下为本框架 `app/agents/*` 已有能力在设计期的契约化表述。

| Skill | 映射模块 | 闭环位置 |
|---|---|---|
| `script-decompose` | `app/agents/script_decomposition` | 步骤 2 · 拆解 |
| `world-build` | `app/agents/world_builder` | 步骤 3 · 世界 |
| `npc-runtime` | `app/agents/npc_runtime` | 步骤 7 · 发布后运行时 |
| `lorebook-activate` | `app/agents/npc_lorebook` | 步骤 4 · 剧情 / 运行时 |
| `playtest-validate` | `app/agents/playtest_validation` | 步骤 6 · 审查 |
| `guardrail-check` | `WorldRuntimeGuardrail`（`app/worlds/sandbox/guardrails.py`） | 运行时 · Generic NPC Runtime 对话护栏 |

每项契约字段：输入 / 输出 / 调用条件 / 依赖工具 / 失败处理 / 安全边界 / 验证方式 / 复用价值。

- [script-decompose.md](./script-decompose.md)
- [world-build.md](./world-build.md)
- [npc-runtime.md](./npc-runtime.md)
- [lorebook-activate.md](./lorebook-activate.md)
- [playtest-validate.md](./playtest-validate.md)
- [guardrail-check.md](./guardrail-check.md)
