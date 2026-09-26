# ADR-0017 · 配置：config.json + config.py，不开放 MCP 配置工具

**状态**：已采纳并实施 · 2026-09-24
**影响**：[ADR-0006](0006-two-tool-surface.md)（工具面）、[ADR-0007](0007-in-memory-index-with-polling.md)（轮询周期来源）
**修订**：2026-09-24（二）—— reindex 默认增量，新增 `--full` / `?full=1` 全量，见 [ADR-0019](0019-index-cache-incremental.md)。
**术语**：2026-09-24 起 workspace 更名为 source，见 [ADR-0023](0023-rename-workspace-to-source.md)；本文保留原措辞。

## 决策

- 全部配置进 `config.json`（默认 `~/.myMemory/`，最初为运行目录，2026-09-24 第二轮改为用户目录以免污染代码目录），唯一保留环境变量 `MEMORY_CONFIG` 覆盖其路径；
  其余 `MEMORY_*` 与 `.env` 作废。修改一律**重启生效**。
- workspace 增删改与刷新周期由独立 CLI `config.py` 完成，人工操作；
  重启通过调用 `run.py --restart`。
- 唯一的热操作是 `reindex`：CLI 调用服务新增的 `POST /reindex`，复用并发合并的刷新。

## 为什么不用 MCP 配置工具

最初需求是"通过 MCP 设置刷新周期、增删改 workspace"。评审发现：服务免鉴权，
任何局域网调用方（包括 AI 自身）都能借此把只读改回读写、挂载任意目录后用 get-document 读出、
或删掉团队库。补丁式规则（只读单向、只读锁定、admin 开关）层层叠加仍有绕过路径
（删只读 → 同目录新增读写），于是整体移出 MCP。刷新周期虽然风险低，也一并移到 CLI，
保持"配置只有一个入口、一种生效方式"。

## 后果

- MCP 工具面只增加两个只读工具（list-workspaces、recent），无配置写入面。
- `POST /reindex` 无写入能力，免鉴权下开放可接受。
- 人手动编辑 `config.json` 与使用 CLI 等价，均需重启。
