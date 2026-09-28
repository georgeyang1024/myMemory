# ADR-0023 · 术语更名：workspace → source

**状态**：已采纳并实施 · 2026-09-24

"workspace"（工作区）暗示一个供人工作的地方，而这里的概念是**记忆的来源**：个人库、团队库、
挂载进来的挂载进来的部门库、只读的机构文档。统一更名为 **source**（来源），覆盖代码标识符、
配置字段（`workspaces` → `sources`）、MCP 工具与参数（`list-workspaces` → `list-sources`，
参数与输出字段 `workspace` → `source`，`writable_workspaces` → `writable_sources`）、
CLI 子命令（原 `memctl.py workspace …`，现 `config.py source …`，CLI 已于 2026-09-25 由
memctl.py 更名为 config.py）以及现状文档。
索引缓存格式版本随之升为 2，旧缓存作废，首次启动全量构建一次。
ADR-0015~0022 与 TASKS 中的验收记录是历史记录，保留原措辞，读时把 workspace 理解为 source；
ADR-0015 的文件名 `0015-workspaces.md` 同样不改，以免既有链接失效。
