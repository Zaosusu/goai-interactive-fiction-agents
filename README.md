# 互动叙事多智能体框架（Interactive Fiction Multi-Agent Framework）

一套面向**文字冒险 / 互动小说游戏**的开源多智能体框架。它让**多个 Agent 在共享的叙事世界里分工、协作、可被生产部署**——剧情拆解、世界观构建、角色对话、试玩验证——并通过统一的模块边界、运行时持久化与确定性护栏，把多 Agent 叙事系统从「能演示的 Demo」推进到「能部署的生产系统」。

> 框架以文字冒险游戏为核心落地方向：玩家输入一句话，在场的多位角色 Agent 各自回应、世界状态随之推进。同一个底座的可插拔 Agent 模块，亦可延伸到其它需要「多自主实体在同一环境里协作」的互动场景。

> **参赛说明**：本项目报名 **GOAI 世界人工智能开源大赛 · 新智基座（Agent Infra）赛道**，作为「面向文字冒险游戏的开源多智能体协作基础设施」提交。代码仓库：`https://github.com/Zaosusu/goai-interactive-fiction-agents`

## 核心能力

- **多 Agent 角色编排与任务拆解**：把一段设定 / 剧本拆成子任务与结构化中间产物（剧情图谱、角色卡、世界设定）。
- **上下文传递与共享状态**：角色知识库（Lorebook）把世界观设定沉淀为可被对话激活的知识切片；运行时维护跨 Agent 的共享叙事状态。
- **工具 / LLM Provider 抽象可替换**：LLM、Embedding、图像生成、记忆均通过 Provider 抽象层接入，供应商可替换。
- **运行时持久化与跨进程恢复**：游戏会话与每个角色的私有状态可跨进程重启恢复。
- **确定性护栏（Guardrail）**：约束 Agent 不编造设定外的事实（如地点、人物关系），保证剧情输出可预期。
- **Corrective RAG + 记忆**：检索增强与私有记忆接入 Agent 循环，支持纠错式检索。
- **执行证据沉淀与可观测**：会话存储（Session Store）记录完整对话 / 执行轨迹，供审查与回放。
- **经验沉淀**：玩家 / 测试反馈回流，驱动后续生成质量提升。
- **项目内 Skill 标准**：Agent 能力收敛到 `app/agents/<agent_module>` 模块边界，新能力按统一标准接入。
- **标准 MCP 架构**：以 MCP 形态暴露工具边界——`creator_assistant` 的 `CreatorMcpToolServer` 实现 `tools/list`（`list_tools()`）与 `tools/call`（`call_tool()`），工具定义 / 结果信封严格遵循 MCP 数据模型，可被标准 MCP Client 理解；当前经 `GET/POST /api/creator/mcp/tools/{list,call}` 以 REST 暴露，传输层与业务逻辑解耦，后续可平滑替换为官方 MCP 传输（stdio / SSE / Streamable HTTP）。详见 [MCP_ARCHITECTURE.md](./MCP_ARCHITECTURE.md)。

## 架构流水线（文字冒险游戏）

```text
剧本 / 设定输入
  -> 任务拆解 Agent（剧情与设定拆解）
  -> 结构化中间产物（剧情图谱 / 角色卡 / 世界书）
  -> 可选：资产生成 Agent（视觉提示词、立绘）
  -> 世界 / 环境构建 Agent
  -> 运行时（多角色协作 / 玩家对话）
  -> 试玩 / 验证 Agent
  -> 经验沉淀 Agent
```

每个阶段都是独立、可替换、可复用的 Agent 模块，通过 `app/core` 的共享基础设施（LLM、RAG、记忆、运行时、护栏、会话存储）串联。

## Agent 模块清单（app/agents）

| 模块 | 职责 |
|---|---|
| `project_intake` | 项目 / 设定 / 接口文档结构化接入 |
| `script_decomposition` / `global_script_decomposition` | 剧本与任务拆解 |
| `story_authoring` / `story_expansion` | 剧情创作与扩展 |
| `visual_prompt_composer` | 视觉提示词生成 |
| `visual_asset_generation` | 视觉资产生成 |
| `world_builder` | 世界 / 环境构建 |
| `world_review` / `npc_review` | 产出审查 |
| `npc_lorebook` | 角色知识库（可热插拔进任意故事世界） |
| `npc_runtime` | 角色运行时（私有记忆 / 私有状态） |
| `playtest_validation` | 试玩 / 验证 |
| `experience_learning` | 经验沉淀 |
| `creator_assistant` | 创作者辅助（含 MCP 形态工具边界：`CreatorMcpToolServer`） |
| `ui_projection` | 对外的 UI / 接口投影 |

（注：`npc_*` 命名来自文字冒险游戏的参考实现「角色 / NPC 协同」，模块能力本身是通用的。）

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env   # 填入 LLM / Embedding / 图像生成等 API Key
python start_backend.ps1   # 启动后端
```

详见 [TECHNICAL_ARCHITECTURE.md](./TECHNICAL_ARCHITECTURE.md)、[AGENT_DEVELOPMENT_STANDARD.md](./AGENT_DEVELOPMENT_STANDARD.md) 与 [MCP_ARCHITECTURE.md](./MCP_ARCHITECTURE.md)。

## 开源协议

Apache-2.0。详见 [LICENSE](./LICENSE)。
