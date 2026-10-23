# ADR-0036 · 主动保存开关：`auto_save` 系统配置 + `?auto_save=` URL 覆盖

**状态**：已采纳并实施 · 2026-10-16（0.7.0）

## 背景

0.7.0 重写的服务总说明（instructions）加了「主动保存」一节：经验、踩坑、偏好等
可以不经询问直接 save。这是行为引导，不是能力：有的使用者想要 AI 主动沉淀，
有的只想在明确要求时才写。原先这一节写死在 instructions 里，无法关闭。

多人共用时同一个实例服务多个客户端，偏好可能不同；单靠系统配置只能一刀切。

## 决策

1. **系统开关**：顶层 `auto_save`，布尔，**默认 `false`**，与 `link_graph` 同类
   （改配置重启生效；CLI `config set auto_save <true|false>`）。
   关闭时 instructions 不含「主动保存」一节，其余内容不变。
2. **URL 覆盖**：MCP 端点支持 `?auto_save=<true|false>`，覆盖系统配置，只作用于该连接。
   - 取值大小写不敏感：`true/1/yes/on`、`false/0/no/off`；缺省或空值沿用系统配置。
   - 非法值在 `/mcp` 上直接 400（`UserScopeMiddleware`），不静默回退——与 `?user=` 一样 fail loud。
   - 可与 `?user=` 同时使用：`/mcp?user=张三&auto_save=true`。
3. **实现位置**：instructions 只在握手时下发，所以只在 `initialize`（及新协议的
   `server/discover`）的返回里改写 `instructions` 字段——用 SDK 的服务端中间件
   （`server.middleware`）读 `ctx.request` 的 query string。工具列表与工具行为不受影响。
   stdio 没有 URL，恒用系统配置。

## 考虑过的方案

- **只做系统配置**：多人共用时无法按人区分。否决。
- **按 `?user=` 存个人偏好**：要新增持久化与管理入口；URL 参数由使用者在客户端配置里自己决定，
  更简单。暂不做。
- **ASGI 层改写 HTTP 响应体**：要处理 JSON / SSE 两种响应格式，脆弱。改用 SDK 中间件改结构化结果。
- **同时隐藏/改写 save 工具说明**：工具说明本身不含主动保存引导，无需改动。

## 后果

- 默认关闭：升级后未配置的实例不再主动保存；想要的需设 `auto_save: true` 或在 URL 上加参数。
- 覆盖只在新建连接时生效：改 URL 后客户端需重连。
- 依赖 SDK 中间件接口（mcp 2.x 标注为 provisional），升级 SDK 时由契约测试
  `test_auto_save_url_param_overrides_config` 兜底。
