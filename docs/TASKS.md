# myMemory 任务清单（当前版本 0.3.0）

> 依据：[REQUIREMENTS.md](REQUIREMENTS.md)（以下简称"需求"）
> 第一轮（T1–T12）与第二轮（R1–R12）均已完成；第三轮（写入面扩展 + 删除断路器，§15）已完成。
> 规则：逐项完成，完成一项勾选一项；每项完成后跑全量测试，保持绿色。

## 第一轮（已完成）

### 实施

- [x] **T1 配置文件 config.json**（需求 §3.1 §3.2 §4.1 §4.2）
  - `Config.load()`：`MEMORY_CONFIG` 覆盖路径，默认 `~/.myMemory/config.json`；缺失时由 run.py 交互式建档（回车默认 `~/.myMemory/memory`，source `memory`/默认记忆源），无法交互报"未指定记忆存储"（ADR-0025）
  - 解析 host/port/poll_interval/workspaces/extensions/chunk/各上限，类型与范围校验 fail-fast
  - Workspace：名称校验（普通名不以 `readonly` 开头、不含 `/`；只读名 `readonly/<名>`）、重名、目录存在、真实路径不重叠不嵌套（Windows 大小写不敏感）、`type` 缺省 `local`
  - 其余 `MEMORY_*` 环境变量不再读取
- [x] **T2 存储抽象与多 workspace 扫描**（需求 §3.4 §8）
  - `storage.py`：存储接口 + `LocalStorage`（列文件、读、写、取 mtime）；非 `local` 按只读
  - `corpus`：按 workspace 扫描，`DocMeta` / `Chunk` 带 `workspace`，指纹覆盖全部 workspace
- [x] **T3 统一索引按 (workspace, path) 组织**（需求 §3.3 §6.1 §8）
  - 已索引集合元素为 `(workspace, path)`；workspace 名参与分词
  - `search` 支持可选 workspace 过滤；`Hit` 带 `workspace`
  - 轮询删除 Git 采集
- [x] **T4 文件名 / 分类名白名单修复**（需求 §7）
  - 放行 `( ) [ ] { } . , & # + @ ! ' = ~ %`；禁止首尾 `.`/空格、`..`、系统保留名
  - 分类可为空
- [x] **T5 写入层：workspace 必填、只读拒写**（需求 §6.3）
  - `save_memory(config, workspace, category, filename, content)`；未知/只读 workspace 拒绝
  - 落盘到 `<dir>/<分类>/<文件名>.md` 或 `<dir>/<文件名>.md`，返回落盘后 mtime
- [x] **T6 status.json**（需求 §4.3）
  - 启动读一次，丢弃不存在的 workspace/文件；丢失或损坏按空记录启动并告警
  - 每次 save 成功后更新对应记录并原子写入
- [x] **T7 MCP 工具面**（需求 §6）
  - search：结果独立 `workspace` 字段、可选 `workspace` 参数
  - get-document：`workspace` 必填，建议项为 `{workspace, path}`
  - save：`workspace` 必填、`category` 可选、失败带 `writable_workspaces`
  - 新增 `myMemory-list-workspaces`、`myMemory-recent`（默认 10、最多 20、`edited_by`）
  - 静态 instructions 与工具描述更新
- [x] **T8 REST 端点**（需求 §9）
  - `/health`：删除 git、root、scan_dirs；新增 `workspaces`、`config_file`
  - `/search`：可选 `workspace`；新增 `POST /reindex`
- [x] **T9 删除 Git 状态与环境变量残留**（需求 §10）
  - 删除 `gitinfo.py`、`test_gitinfo.py`、`.env.example`；`__main__` 改用 `Config.load()`
  - `run.py`：去掉 `.env` 加载，端口从 config.json 读取，向子进程传递绝对 `MEMORY_CONFIG`
- [x] **T10 CLI memctl.py**（需求 §5；2026-09-25 已更名为 config.py）
  - `workspace list/add/edit/remove`、`config set poll_interval`、`reindex`、`restart`、`--restart`
  - 复用 `Config` 校验；校验失败不写文件；原子写入；`remove` 需确认或 `--yes`
- [x] **T11 文档同步**（需求 §12）
  - ARCHITECTURE.md、README.md、GLOSSARY.md 按现状改写；ADR-0015~0018 状态改为已实施

### 验收

- [x] **T12 系统性检查**：逐条对照需求 §11 验收标准与 §2–§10 条款，记录结果

### 第一轮验收记录（2026-09-24）

自动化：全量 `pytest` **290 passed**（Linux，`.venv-linux`）。
端到端：真实服务经 `run.py --background` 启动（端口取自 config.json），用官方 MCP 客户端走 Streamable HTTP 调用全部 5 个工具；
Windows 侧经 shellbridge 用 `.venv-windows` 跑 `--check` 与写入校验。

| # | 需求 §11 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | 两读写 + 一只读（含挂载盘）启动，跨库检索，结果带独立 workspace 字段 | ✅ | E2E：memory/team/readonly/org 跨库命中；Windows：`readonly/zconfig` → `Z:\config`（UNC 挂载盘，路径已匿名化）自检通过 45 文档 |
| 2 | 重叠/嵌套/不存在/重名/普通名以 readonly 开头 → 启动失败；CLI 拒绝且不改文件 | ✅ | `test_config.py`、`test_memctl.py::test_invalid_operations_leave_config_untouched`；E2E `--check` 退出码 2；Windows 大小写不同的嵌套目录同样被拒 |
| 3 | save 只读被拒、带可写列表、磁盘无写入 | ✅ | E2E `save ro`；`test_run_save_to_readonly_lists_writable_workspaces`；storage 层兜底 `test_storage_refuses_readonly_even_if_writer_is_bypassed` |
| 4 | save 缺 workspace 被拒；category 为空写到根目录 | ✅ | `test_missing_unknown_or_readonly_workspace_is_rejected`、`test_empty_category_writes_to_workspace_root`；schema `required` 含 workspace |
| 5 | §7 放行用例全部成功、拒绝用例全部被拒 | ✅ | `test_common_ascii_symbols_are_allowed`（15 例）、`test_illegal_filename_is_rejected`（21 例）；E2E 与 Windows 均保存 `评审纪要(9月) v1.2.md`；Windows 拒绝 CON/nul.md/a:b/a\b/x. |
| 6 | get-document 必须带 workspace；跨库同名 path 可区分 | ✅ | `test_get_document_requires_matching_workspace`、`test_indexed_set_is_workspace_path_pairs`；E2E 错 workspace 被拒并给出 `{workspace,path}` 建议 |
| 7 | recent 默认 10 / 最多 20 / 每文件一条；agent → 再次改动后 scan；重启后 agent 保留 | ✅ | `test_recent_*`（6 例）；E2E：save 后 agent，编辑器追加 + reindex 后 scan，CLI `… --restart` 后 agent 标记仍在 |
| 8 | CLI reindex 触发重建；服务未运行时提示 | ✅ | E2E：reindex 后编辑器新建的文件立即可检索；`test_reindex_when_service_down` |
| 9 | `--restart` 与 `restart` 经 run.py 重启，新配置生效 | ✅ | E2E：`workspace add extra --restart` 后 list-workspaces 出现 extra；`test_restart_*` |
| 10 | ~~无 config.json 自动生成~~（被 ADR-0025 取代：首次启动交互式询问记忆目录建档，非交互报"未指定记忆存储"）；MEMORY_CONFIG 生效 | ✅ | `test_ensure_config_*`、`test_add_creates_config_from_scratch`；`test_config_path_*` |
| 11 | /health 无 git 字段 | ✅ | E2E health 字段列表；`test_health_has_no_git_or_single_root` |
| 12 | 既有测试按新契约更新后全部通过 | ✅ | 290 passed；未删除或弱化断言，仅删除随 gitinfo 一并移除的 Git 相关用例 |

| 条款 | 检查项 | 结果 |
|---|---|---|
| §3.4 | 存储接口 + LocalStorage；`type` 字段保留，非 local 目前启动报"尚未支持"，`writable` 对非 local 恒为 false | ✅ |
| §4.1 | 只保留 `MEMORY_CONFIG`；其余 `MEMORY_*` 被忽略（`test_other_memory_env_vars_are_ignored`、`test_resolve_port_ignores_memory_port_env`） | ✅ |
| §4.3 | status.json 损坏/缺失按空启动；过期记录丢弃；原子写 | ✅ |
| §6.4 | list-workspaces 不含目录路径；instructions 静态不列 workspace 名 | ✅ |
| §8 | 轮询不再采 Git；`POST /reindex` 合并并发；GET /reindex 返回 405 | ✅ |
| §10 | 删除 gitinfo.py、test_gitinfo.py、.env.example、run.py 的 .env 加载 | ✅ |
| §12 | ARCHITECTURE、README、GLOSSARY 改写；ADR-0015~0018 标为已实施；0001/0006/0007/0010/0014 标注修订 | ✅ |

已知限制（均已写入文档，非缺陷）：

- 多个 stdio 进程各自在启动时读 status.json，彼此的 agent 标记要到重启后才互见（README 客户端配置一节）。
- 挂载盘目录在日志与 /health 中显示为解析后的真实路径（如 UNC），config.json 内容本身不被改写。
- Windows venv 未装 pytest，Windows 侧只做了端到端校验，未跑单元测试。

---

## 第二轮（已完成）

测试一律在 Windows `.venv` 中执行（经 shellbridge）。

- [x] **R1 单平台运行**（需求 §5）
  - `run.py` 虚拟环境统一为 `.venv`，删除按平台分目录、跨平台检测、旧 `.venv` 提示及对应测试
  - 停服务，删除 `.venv-windows`、`.venv-linux`，重建 `.venv` 并安装 `requirements-dev.txt`，在 Windows 跑通现有测试
- [x] **R2 版本号 0.1.0**（需求 §11）
  - `server.py`、`__init__.py` 改为 0.1.0，删除注释中的 3.x 版本演变史
  - 文档与 ADR 中旧版本号措辞清理
- [x] **R3 工具短名**（需求 §6）
  - 5 个工具改名为 `search` / `get-document` / `save` / `list-workspaces` / `recent`
  - instructions、工具描述、参数描述与契约测试同步
- [x] **R4 GET /recent**（需求 §10）
  - JSON，`limit` / `workspace` 参数，与 MCP `recent` 共用逻辑；未知 workspace 400
- [x] **R5 workspace 可用性判定**（需求 §3.4 §8）
  - 盘根可达性检查（盘符与 UNC）；`disk_offline` / `dir_missing`；扫描途中出错复查盘根
  - 启动时不可用只警告不失败；名称/重名/重叠仍失败；重叠判定掉盘时按原样路径
  - `list-workspaces` / `/health` 增加 `available`、`unavailable_reason`
- [x] **R6 索引缓存**（需求 §7.1 §7.2）
  - `index.cache`：格式版本、配置指纹、每文件条目（正文、块偏移、分词结果、`agent_mtime`）、BM25 模型；pickle，原子写
  - 启动先加载缓存立即监听，后台增量校验；`/health` 的 `verifying`
  - 缓存缺失/损坏/指纹不符：告警后全量构建
- [x] **R7 增量更新与缓存清理**（需求 §7.3 §7.4）
  - 轮询、save、reindex 走增量；`reindex --full` / `POST /reindex?full=1` 全量
  - 文件删除、目录删除清除条目；掉盘保留；配置移除的 workspace 重启后清除
- [x] **R8 掉盘行为**（需求 §8）
  - 掉盘 workspace 不更新不删除；get-document 从缓存返回并标 `stale`；save 拒绝且不建目录；恢复后自动更新
- [x] **R9 删除 status.json**（需求 §12）
  - 删除 `status.py` 及测试，`agent_mtime` 并入缓存；recent 的 edited_by 改读缓存
- [x] **R12 只读改为 writable 字段**（需求 §3.1 §3.2 §6 §9）
  - CLI：`writable` 布尔字段（省略即 true）；删除 `readonly/` 前缀逻辑，名称一律不含 `/`
  - 有效可写 = 配置 writable ∧ 可用 ∧ local；search / recent / get-document / list-workspaces / `/health` 输出 `writable`
  - CLI：`add` 必须 `--readonly` / `--writable` 之一，`edit` 互斥切换，写配置总是显式 `writable`
  - 删除现有 `config.json`、`status.json`，由人重建
- [x] **R10 文档同步**
  - ARCHITECTURE、README、GLOSSARY 按现状改写；ADR-0019~0021 标为已实施

### 第二轮验收

- [x] **R11 系统性检查**：逐条对照需求 §13，并在 Windows 上用真实部门知识库验证启动耗时与掉盘场景

### 第二轮验收记录（2026-09-24）

自动化：Windows `.venv` 中全量 `pytest` **303 passed, 1 skipped**（跳过项为 Windows 无权限创建符号链接）。
端到端：Windows 上用真实服务进程 + 官方 MCP 客户端（Streamable HTTP），用 `subst Q:` 建立再删除来制造**真实的盘根不可达**。
生产：删除旧 `config.json`，用 CLI 按新格式重建三个 workspace，重启服务并核对。

| # | 需求 §13 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | 工具全名为 5 个短名 | ✅ | E2E `list_tools` = get-document / list-workspaces / recent / save / search；`test_tool_names_carry_no_server_prefix` |
| 2 | 只用 `.venv`，无跨平台 venv 代码 | ✅ | `.venv-windows`、`.venv-linux` 已删除；`run.py` 仅 `default_venv_dir() == .venv`；`test_default_venv_is_single_dot_venv` |
| 3 | 版本 0.1.0，无 v4 / 4.0.0 | ✅ | `/health` 与 MCP 握手 0.1.0；代码与文档已清理（仅 REQUIREMENTS 保留"此前版本作废"的历史说明） |
| 4 | `GET /recent` JSON、默认 10 最多 20、workspace 过滤 | ✅ | E2E + `test_recent_endpoint_returns_json`；生产 `curl /recent?limit=3` |
| 5 | 有缓存时数秒内监听，先 `verifying: true` 后 false | ✅ | 生产：缓存加载 0.42 s，CLI restart 全程 9 s；后台校验 9.6 s 后 `verifying` 转 false。E2E 中小样本校验只需 0.02 s，首个 `/health` 已为 false（以日志"已从缓存加载"为证） |
| 6 | 部门知识库有缓存重启 ≤ 15 s，无变化不重读不重分词 | ✅ | 生产约 1460 文档 / 2.1 万块：重启 8–9 s（原 3.5–4 min）；日志"索引无变化（9.64s）"；`test_incremental_reuses_unchanged_entries` |
| 7 | 缓存损坏 / 指纹变化 → 告警并全量构建 | ✅ | E2E 写入垃圾缓存后全量构建可用；`test_bad_cache_falls_back_to_full_build`（garbage / fingerprint / format） |
| 8 | 掉盘：检索与全文可用、`stale`、`disk_offline`、不删除、恢复后自动更新 | ✅ | E2E（subst 删除后）：available=false / disk_offline、writable=false、条目保留、get-document 返回缓存全文且 stale=true、search 命中、save 被拒、其他 workspace 照常更新、全量重建不动掉盘条目；subst 恢复后自动可用且收录新文件 |
| 9 | 启动时盘不在：警告、正常启动、缓存内容可检索 | ✅ | E2E 掉盘状态下重启 1.6 s 就绪，qd 的 2 个条目仍可检索；`test_offline_workspace_survives_restart_via_cache` |
| 10 | 盘在目录被删：清空索引与缓存、`dir_missing`、启动只警告 | ✅ | E2E 删除 ro 目录后 doc_count=0、dir_missing；重启只警告，缓存中亦为 0 |
| 11 | save 到不可用 workspace 被拒，不建目录 | ✅ | `test_save_to_unavailable_workspace_is_rejected_without_recreating_dir`、`test_save_to_offline_workspace_is_rejected`；E2E 掉盘 save 被拒 |
| 12 | reindex 默认增量、`--full` / `?full=1` 全量 | ✅ | E2E `mode` = incremental / full；`test_reindex_posts_to_running_service`（CLI URL 带 `?full=1`） |
| 13 | agent 标记跨重启保留，不生成 status.json | ✅ | E2E 重启后仍为 agent；`test_run_save_marks_agent_in_index_and_cache`；E2E 目录中无 status.json |
| 14 | `writable: false`：save 被拒，所有输出 writable=false | ✅ | E2E ro 在 search / get-document 中 writable=false、save 被拒且目录无写入；`test_writable_flag_on_every_document_output` |
| 15 | 省略 writable 即可写；名称含 `/` 启动失败 | ✅ | `test_multiple_workspaces_and_writable_field`、`test_illegal_workspace_names_are_rejected`（含 `readonly/org`） |
| 16 | 可写 workspace 掉盘 / 目录不存在时 writable=false | ✅ | E2E qd 掉盘后 writable=false；`test_offline_workspace_is_stale_and_not_writable` |
| 17 | CLI add 必须 `--readonly`/`--writable`；edit 切换不改名；总写出 writable | ✅ | `test_add_requires_readonly_or_writable`、`test_edit_toggles_readonly_without_renaming`、`test_edit_rename_keeps_readonly`；生产重建的 config.json 三项均含 writable |
| 18 | 全部测试在 Windows `.venv` 中通过 | ✅ | 303 passed, 1 skipped |

实施中发现并修正的问题：

- **workspace 目录被解析成真实路径**：`Z:\` 会被换成 UNC、subst 盘被换成底层目录，访问与掉盘判定不再针对配置里的那块盘
  （E2E 首轮 38 项中 7 项因此失败）。改为：访问与盘根判定用配置原样路径（只补成绝对路径），真实路径仅用于重叠判定；
  CLI 写配置同样不再解析。新增 `test_configured_path_is_used_as_written_not_resolved`。
- Windows 系统临时目录对 pytest 有权限问题：`pytest.ini` 把临时目录放到 `logs/pytest-tmp`。

生产部署：`memory`（可写，~140 文档）、`team`（只读，UNC 网络盘，~1100 文档）、另有一个可写 source（~240 文档），
`index.cache` ~57 MB。旧配置备份在本次会话的 scratchpad 中（`config.json.old`）。

### 追加：运行数据移出代码目录（2026-09-24）

- [x] `config.json`、`index.cache`、`logs/`（日志与 PID）默认放在 `~/.myMemory/`；`MEMORY_CONFIG` 覆盖时随配置文件所在目录
- [x] 测试临时目录改为 `~/.myMemory/pytest-tmp`（`tests/conftest.py`），`pytest.ini` 不再指向 `logs/`
- [x] 生产数据迁移：停服务 → 移动到 `~/.myMemory/` → 删除代码目录下的 `logs/` → 启动（有缓存 4 s 就绪）
- [x] Windows `.venv` 全量测试 304 passed, 1 skipped

---

## 第三轮（已完成 2026-09-27）：写入面扩展与删除断路器

写入面新增 4 个 MCP 工具 + 一个配置断路器；决策过程为多轮共识问答，记录见
[REQUIREMENTS](REQUIREMENTS.md) §15 与 [ADR-0006](adr/0006-two-tool-surface.md) /
[ADR-0014](adr/0014-write-tool-boundary.md) 修订。

- [x] **W1 rename**：同 source 内改名/移动一级分类；旧文件必须真实存在（文件系统判断，
      不认识索引），目标存在即拒绝（不覆盖）；路径与 search 返回同形、.md 可带可不带
- [x] **W2 replace**：全文完全字面的 old→new 替换（无正则/大小写折叠/换行归一化），
      命中几处换几处并返回 `replaced_count`；0 命中、空 new_string、old==new 一律拒绝且不动文件
- [x] **W3 merge**：把已存在的源并入已存在的目标后删除源文件；并入段带
      `## 源文件相对路径` 标题与 `---` 分隔线；先写目标后删源，删除失败不回滚、
      响应里 `source_removed: false` 如实标出
- [x] **W4 delete + 断路器**：真删（unlink，无备份）；config.json 全局开关
      `allow_mcp_delete`（默认 **false**，重启生效）——关闭时 `delete` 与 `merge`
      连注册都不注册（tools/list 不可见），writer 层 `_require_delete_enabled`
      用同一开关再兜一道闸
- [x] **W5 支撑设施**：storage 层 `move()` / `remove()`（与 write_text 同一套
      只读/目录缺失闸）；writer 层共用校验抽出（`_writable_target` /
      `_validate_category` / `_validate_filename` / `_validate_location`）
- [x] **W6 CLI**：`config.py config set allow_mcp_delete <true|false> [--restart]`；
      非布尔值拒绝且不改配置文件
- [x] **W7 文档同步**：README（工具面 9 个 + 开关 + CLI）、ARCHITECTURE（§6.4–6.7、
      §8.3、模块表、风险表行 15）、GLOSSARY（edited_by、删除断路器词条）、
      ADR-0006 / ADR-0014 修订标注

### 第三轮验收（2026-09-27）

自动化：Windows `.venv` 全量 `pytest` **402 passed, 1 skipped**（新增 rename 11 例、
replace 9 例、merge 9 例、delete 11 例、config/cli/contract 同步用例）。
端到端：真实服务经 `run.py --restart` 重启后，用官方 MCP 客户端（Streamable HTTP）
实测 `list_tools` 返回 9 个工具（`allow_mcp_delete: true`），delete 参数 schema 正确；
契约测试覆盖开关关闭态：`BASE_TOOLS` 之外 delete/merge 必须整体缺席。

生产部署：生产 config.json 已用 `config.py config set allow_mcp_delete true` 开启
（用户显式选择），服务已重启（PID 33848、端口 7083、索引 415 文档 / 4094 chunk）——
此为唯一与默认配置不同的生产项，收回删除能力只需 `"allow_mcp_delete": false` + 重启。

---

## 第四轮（已实施 2026-10-01）：多人共用部署

决策见 [REQUIREMENTS](REQUIREMENTS.md) §16 与 [SPEC-MULTI-USER](SPEC-MULTI-USER.md)、
[ADR-0028](adr/0028-multi-user-shared-deployment.md) ~ [ADR-0032](adr/0032-remove-merge-tool.md)。
实施前审查补了四项关键修正（REQUIREMENTS §16 行 26~31）：**默认跨 source 路径泄漏**、
**注入头编码与伪造**、**轮询指纹前提错误（删除指纹扩展）**、**访客默认只读（guest_writable）**。

- [x] **M1 配置**：multi_user 子项（store_dir / admins / guest_writable，含
      保留字 casefold 拒绝、嵌套禁令扩展）+ personal_sources / effective_sources /
      ind_user / is_admin
- [x] **M2 路由中间件**：UserScopeMiddleware（校验路径集含尾斜杠变体、URL 解码 +
      strip 统一、percent-encode 注入 x-mymemory-user、每请求无条件删除客户端自带头、
      未命中 400 提示联系管理员）
- [x] **M3 会话限域**：scoped_config 三分支（访客 guest_writable / 用户 / 管理员全域）
      + 范围名集过滤（snapshot.search 增 llowed_sources；run_search / run_recent /
      run_get_document / _suggest 只在会话范围取材——泄漏修复）
- [x] **M4 索引**：build / refresh / _snapshot_from_cache / _rescue_previous /
      storages 全部改用当场枚举的 effective sources；无新指纹
- [x] **M5 编辑者登记**：edited_by 更名 editor（recent 唯一暴露点）；写路径
      run_save/rename/replace 增 editor 参；mark_agent 记 (mtime, editor)；
      FileEntry.agent_editor；CACHE_FORMAT 3→4（升级首启全量重建一次，所有形态）
- [x] **M6 merge 移除**：	ool_merge / un_merge / MERGE_DESCRIPTION /
      writer.merge_memory / Merged 连删；llow_mcp_delete 语义收窄为只管 delete
- [x] **M7 CLI**：multi-user set/unset/show（set 支持 --guest-writable）、
      multi-user admin add/remove/list、multi-user user list/add
      （多人相关子命令统一挂 multi-user 名下，2026-10-01）
- [x] **M8 main.py**：HTTP 形态挂中间件；stdio eplace(config, multi_user=None)；
      --check 覆盖 effective sources 并报告 multi_user 状态
- [x] **M9 文档**：GLOSSARY（指纹条目修正、访客、multi_user）、REQUIREMENTS §16
      （决策表 31 行）、ARCHITECTURE、README 部署章节、CHANGELOG
- [x] **M10 首次建档选形态**（2026-10-01 追加，REQUIREMENTS §16 行 32）：
      run.py 交互先问个人使用/团队使用；团队分支问存储目录（默认
      ~/.myMemory/users）与 admins（默认 admin）写 multi_user（enabled: true），
      不建公共 source；multi_user 启用时 sources 允许为空（开关关闭仍须非空）；
      multi-user enable/disable 子命令与 enabled 开关（默认 false）、admins 默认 admin

### 第四轮验收（2026-10-01）

Windows .venv 全量 pytest **545 passed, 1 skipped**（新增 tests/test_multiuser.py
54 例：配置/派生/中间件/限域与泄漏回归/REST/热发现/编辑者登记/--check）。
CLI 冒烟：multi-user set/show、multi-user user list/add、admin add（保留字 casefold 拒绝）实测通过。
提交前 Linux 复验全量 pytest **584 passed**（2026-10-01）。
