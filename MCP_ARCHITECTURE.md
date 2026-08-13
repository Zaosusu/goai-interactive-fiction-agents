# MCP 架构（MCP-shaped 的工具边界）

本文说明本项目如何以 **Model Context Protocol（MCP）形态** 对外暴露能力。核心实现位于 `app/agents/creator_assistant/mcp.py`，并通过 `app/agents/creator_assistant/routes.py` 暴露为 REST 端点。

## 1. 它是什么

`CreatorMcpToolServer` 是一个 **MCP-shaped 的工具边界**：

- 工具的定义、调用与结果信封，复用 MCP 的命名与数据结构约定（`McpToolDefinition` / `McpToolAnnotations` / `McpTextContent` / `McpCallToolResult`）；
- 传输（transport）层刻意放在该类之外，便于后续接入官方 MCP 传输（stdio / SSE / Streamable HTTP）。

换句话说：本项目没有把工具能力锁死在私有接口里，而是用 MCP 的数据模型来描述「有哪些工具、怎么调用、返回什么」；但当前实现尚未挂载官方 MCP transport（无 JSON-RPC 消息层、无 `initialize` / capabilities 协商、无标准 MCP 生命周期），且 `tools/call` 需额外携带 `project` 与 `artifacts` 上下文，因此标准 MCP Client 暂时不能直接连接。

## 2. 工具清单（tools/list）

`CreatorMcpToolServer.list_tools()` 返回 `McpToolsListResult`，其中每个工具是 `McpToolDefinition`：

| 字段 | 含义 |
|---|---|
| `name` | 工具唯一标识（对应 `CreatorToolRegistry` 中的 tool id） |
| `title` | 人类可读名称 |
| `description` | 工具说明 |
| `inputSchema` | JSON Schema 描述的输入 |
| `annotations` | MCP 工具注解：`readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint` |
| `_meta` | 扩展元信息：`stage` / `longRunning` / `available` / `ownerAgent` / `capabilityType` |

`annotations` 直接复用 MCP 规范的标准字段，便于 MCP Client 据此判断工具的副作用与可重入性。

## 3. 工具调用（tools/call）

`CreatorMcpToolServer.call_tool(...)` 对应 MCP 的 `tools/call`，返回 `McpCallToolResult`：

- `content`：`McpTextContent` 文本块列表（MCP 标准内容块）；
- `structuredContent`：结构化结果（更新后的 `project`、产生的 `artifacts` delta、执行 `detail`）；
- `isError`：是否出错。

调用请求 `CreatorMcpCallRequest` 携带 `name`、`arguments`，以及当前执行上下文 `project` / `artifacts`（由 Creator 工作流宿主注入）。

## 4. REST 暴露

当前以 REST 形式提供 MCP 形态接口（传输层在类之外，后续可平滑替换为官方 MCP 传输）：

```text
GET  /api/creator/mcp/tools/list   -> McpToolsListResult   # 对应 MCP tools/list
POST /api/creator/mcp/tools/call   -> McpCallToolResult    # 对应 MCP tools/call
```

## 5. 关键实现文件

| 文件 | 职责 |
|---|---|
| `app/agents/creator_assistant/mcp.py` | MCP 形态的工具定义、结果信封与 `CreatorMcpToolServer`（`list_tools` / `call_tool`） |
| `app/agents/creator_assistant/tools.py` | `CreatorToolRegistry`（工具注册与校验）+ `CreatorToolExecutor`（执行） |
| `app/agents/creator_assistant/routes.py` | `/api/creator/mcp/*` 端点，把 MCP 形态工具边界暴露为 REST |

## 6. 设计意图

- **不重复造协议**：直接采用 MCP 的工具数据模型，降低与外部 MCP Client / 编排器的对接成本。
- **传输可替换**：业务逻辑（`list_tools` / `call_tool`）与传输解耦，今天走 REST，明天可挂官方 MCP Server 而不改工具实现。
- **能力即工具**：Creator 工作流里的每个阶段（布局、校验、编译、绑定视觉资产、世界试玩等）都是可被 `tools/list` 发现、`tools/call` 调用的标准工具。
