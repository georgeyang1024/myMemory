# ADR-0028 · 多人共用形态：公共 source 沿用 + 个人根目录 + `?user=` 路由

**状态**：提案 · 2026-10-01（grilling 会话产出；开发细节见 [SPEC-MULTI-USER](../SPEC-MULTI-USER.md)）

**修订**：2026-10-01（实施前审查）——补记三项：① 限域两层（§ 会话范围）；②
`x-mymemory-user` 内部头只由中间件写入（无条件覆盖客户端自带值）；③ 访客对
公共 source 默认只读（`multi_user.guest_writable`，见
[ADR-0030](0030-multi-user-config-and-admin-full-scope.md)）。

## 决策

服务部署到服务器供小队（≤10 人）共用，形态为：

- **公共 source 沿用现有配置**：`config.json` 的 `sources` 列表原样保留（含 `writable` 语义），多人共用形态下对全部会话可见。团队约定公共 source 配 `writable: false`，内容由管理员在服务器上直接编辑；AI 可检索、可读。公共 source 可以为零（2026-10-01 修订，与 ADR-0025 修订一致）：`multi_user` 启用时允许 `sources` 缺省/为空，语料 = 成员个人 source；需要共享记忆再显式添加。
- **新增可选配置子项 `multi_user`**：出现即启用多人共用，内含 `store_dir`
  （个人根目录，必填）与 `admins`（管理员名单，可选，见
  [ADR-0030](0030-multi-user-config-and-admin-full-scope.md)）。其下一级子目录每个 = 一个用户的个人 source（目录名 = 用户名 = source 名，恒可写）。**不自动开通**：有对应目录才能用对应用户名。
- **路由：URL 查询参数 `?user=<名字>`**。每个用户在 MCP 客户端里配同一个端点的不同 URL（`http://<服务器>:<端口>/mcp?user=张三`）。选 query 参数而非 header / 路径段，是因为 MCP 客户端（opencode / Claude Code 等）的 HTTP 配置都能写带查询参数的 URL，自定义 header 支持参差，路径段则要为每人挂子应用。
- **会话范围**：一个会话 = 全部公共 source + 至多一个个人 source。**限域两层**
  （实施前审查修正——单靠 scoped config 不够，默认跨 source 路径会从全局快照
  泄漏他人内容）：scoped config 管 `source=` 显式参数与写入路径；范围名集
  过滤管默认（跨 source）路径——search / recent / get-document / 建议（suggest）
  都只在会话范围内取材。范围之外的个人 source 一律按"source 不存在"拒绝，
  错误信息不揭示其他用户。
- **访客模式**：不带 `?user=` 的连接只见公共 source；多人形态下默认**只读**
  （`multi_user.guest_writable: false`，ADR-0030）——匿名写团队公共记忆是
  新增风险面，默认关。
- **非法 user（拼错/未开通）**：HTTP 层直接 4xx 拒绝并提示联系管理员建目录，不静默降级。
  校验路径（`/mcp`、`/search`、`/recent`、`/health`）的尾斜杠变体同样校验——fail loud，
  不让 URL 笔误静默降级为访客。
- **REST 同规则**：`/search`、`/recent` 接受同样的 `?user=` 限域；`/health` 的 `multi_user` 块按身份分层（默认仅 `store_dir`/`guest_writable`，管理员视角另给 admins 与用户清单）；`/reindex` 入校验路径集、多人共用下仅管理员可触发（403），单机不限。
- **stdio 忽略个人根目录**：个人 source 只在 HTTP 形态下存在，stdio（单机拉起）只见公共 source，与"并存"决策一致——单机配置（无 `store_dir`）行为与从前完全一致。

## 安全模型（明确记录的已知风险）

免鉴权不变（沿用 ADR-0009 的内网信任边界），本形态的边界是：

1. **路由层硬**：会话内跨人访问（`source=别人的名字`）被拒绝，默认跨 source
   的检索/列表/读取也被范围名集兜住，AI 也看不到其他人。
2. **身份层软**：`?user=` 是身份声明不是身份证明——**知道用户名就能冒充任何人**（改一下 URL 即可）。
3. **身份只来自 `?user=`**：中间件注入的 `x-mymemory-user` 是**内部传输通道**
   （SDK 工具层拿不到 query 参数，这是实测约束），不是身份来源——中间件对每个
   请求无条件删除客户端自带的同名头，伪造该头绕不过路由校验。

"文件夹即授权"（建目录 = 开通）是普通用户的全部授权机制；管理员是另一条
更重的授权通道（配置白名单 + 重启生效），见 [ADR-0030](0030-multi-user-config-and-admin-full-scope.md)。
这与三轮 grilling
中"暂时软边界、后续再收紧"的决策一致：本期不做 token / 鉴权，但会话范围的
硬拒绝让误访问与 LLM 越权面先收敛到"只能冒充"，后续收紧只需把"冒充"堵上
（收紧候选：per-user token；或 initialize 时把 user 绑到 mcp 会话、后续请求
不一致即 403——都不需要再动 source 模型）。

规模判断：≤10 人、千篇级语料 → 单进程全局索引覆盖公共 + 全部个人 source，
查询时按会话范围过滤（见 [ADR-0029](0029-single-instance-scoping-and-dynamic-personal-sources.md)）。

## 维持的不变量

- **公共 source 目录与个人根目录（`multi_user.store_dir`）不得重叠或嵌套**（沿用现有 sources 两两不重叠不嵌套的不变量，扩展到覆盖个人根目录）。曾考虑允许"公共 source 配个人根目录的父目录"当管理员特权总览，因同一文件会被双重索引（两个 `(source, path)` 键指向同一物理文件）、破坏文档身份唯一性而**禁止**。
- 配置改完重启生效、无 MCP 配置面、删除无回收站等既有决策全部不变。
