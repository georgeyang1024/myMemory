# ADR-0009 · 实现栈：官方 SDK + Streamable HTTP + 2 个 REST 端点

**状态**：已采纳 · 2026-09-02

## 决策

- 传输：**Streamable HTTP**，单端点 `POST/GET /mcp`
- 框架：官方 `mcp` Python SDK，`uvicorn` 承载 ASGI 应用
- 额外挂载两个纯 REST 端点：`GET /health`、`GET /search?q=&limit=`

## 理由

Streamable HTTP 正是团队既有 `mobileKnowledgeBase` 的形态
（`{"type":"http","url":"http://192.168.4.234:7081/mcp"}`），客户端配法完全一致，
团队认知成本为零。旧的 HTTP+SSE 双端点传输已被规范标记为 legacy。

手写 JSON-RPC 需自行实现 initialize 握手、`tools/list`、`tools/call`、会话管理，
并持续追踪规范演进——把 SDK 的维护成本转嫁给自己，无收益。

REST 端点的价值：`/health` 让部署后用一条 curl 即可确认服务与索引状态，
无需启动 MCP 客户端；`/search` 便于人工调参、验证检索质量，并为未来非 MCP
调用方留出接口。两者合计约 15 行代码。

## SDK API 实测结论（重要）

**`mcp` 2.x 已将 `FastMCP` 更名为 `MCPServer`**，v1 代码不兼容：

```python
from mcp.server.mcpserver import MCPServer          # 2.x 正确入口
# from mcp.server.fastmcp import FastMCP            # 1.x，2.x 下 ModuleNotFoundError
```

已验证可用的 API：

| API | 签名要点 |
|---|---|
| `MCPServer(name=..., version=...)` | 构造 |
| `@server.tool(name=..., title=..., description=...)` | 注册工具 |
| `@server.custom_route(path, methods)` | 挂载 REST 端点 |
| `server.streamable_http_app(streamable_http_path='/mcp', json_response=False, stateless_http=False, transport_security=None, ...)` | 返回 Starlette ASGI 应用，交给 uvicorn |

该实测结论直接导致 [ADR-0011](0011-pinned-deps-both-platforms.md) 锁死版本号。

## 后果

- 依赖共 4 个：`mcp`、`uvicorn`、`jieba`、`rank_bm25`
- 协议细节（握手、session、SSE 流、错误码）全部由 SDK 负责
- DNS rebinding 保护的默认行为需单独处理 → [ADR-0010](0010-indexed-set-membership.md) §3
