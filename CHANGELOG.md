# Changelog

本文件记录各版本的可见变化。格式参考 [Keep a Changelog](https://keepachangelog.com/)，
版本号采用语义化风格的两位段（major.minor）。

追踪最新的方式：

- 详细设计与决策依据：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 与 [docs/adr/](docs/adr/)
- 需求与验收记录：[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) · [docs/TASKS.md](docs/TASKS.md)

---

## [0.4.0] · 2026-09-29

检索打分调整，详见 [ADR-0027](docs/adr/0027-search-scoring-adjustments.md) 与
[检索说明第 4 轮](docs/RETRIEVAL-NOTES.md)。

### 新增

- **`scoring` 配置**（全局一份，source 可按字段覆盖）：`score` = BM25 分 + 路径命中加分 + 时间加分
  - `recency_window_days` / `recency_bonus`（默认 30 / 10）：按文件修改时间在窗口内线性衰减加分
  - `path_match_bonus`（默认 5）：查询的每个词都出现在文档路径（含文件名）里时，整篇文档加一次
  - `strip_wikilinks`（默认开）：分词前整段去掉 `[[...]]`；原文、片段、偏移不变
- `/health` 的配置摘要报告有效打分配置
- **CLI**：`--scoring 字段=值` / `--reset-scoring 字段|all`，全局用 `config.py config edit`，
  单个 source 用 `config.py source edit <名称>`，写法相同；`source list` 显示各 source 的覆盖

### 变更

- **`search` 默认返回条数 5 → 10**：`limit` 不传时返回前 10 条，上限仍为 `max_results`（默认 20）。
- **默认开启，升级后排序会变化**。想保持旧排序：
  `"scoring": {"recency_bonus": 0, "path_match_bonus": 0, "strip_wikilinks": false}`
- **升级后首次启动全量重建一次索引**：`strip_wikilinks` 进入缓存指纹

---

## [0.3.0] · 2026-09-28

掉盘 source 的数据保全，详见 [ADR-0026](docs/adr/0026-offline-content-natural-lru-and-cold-start-rescue.md)。

### 变更

- **刷新只填全文缓存的空位**：刷新不再挤掉已缓存的全文，掉盘全文只会被真实读取自然淘汰
- **冷启动指纹不符时抢救旧缓存**：保留 `edited_by`；掉盘 source 不再从索引消失（有全文的重新分词，没有的沿用旧分词）
- **指纹去掉代码版本号**：升版本不再作废缓存；改了切块或分词逻辑而配置没变时，须递增 `CACHE_FORMAT`
- **精简工具描述**：删去 get-document、rename、merge 描述中与参数描述重复的路径说明；统一 delete 参数描述的写法

### 修复

- **没有 PID 文件的服务识别不到**（前台启动的，或启动器被杀后遗留的子进程）：`run.py --status` 改为同时探测端口 `/health`，报告"运行中（无 PID 文件）"；`--background` 发现端口上已有服务在应答就跳过启动，不再拉起第二个实例

---

## [0.2.0] · 2026-09-27

写入面扩展与删除断路器。此前的写入面只有 `save`（整篇覆盖），整理已有记忆每次都要
"读回原文 → save 整篇写回"；删除能力则从未存在过。

### 新增（MCP 工具）

- **`rename(source, old_path, new_path)`** — 同 source 内改名 / 移动一级分类；
  旧文件必须真实存在，**目标存在即拒绝**（不覆盖）；路径与 `search` 返回值同形，
  `.md` 后缀可带可不带
- **`replace(source, path, old_string, new_string)`** — 全文**完全字面**替换 old→new
  （无正则、无大小写折叠、不归一化换行），命中几处换几处并返回 `replaced_count`；
  0 命中、空 `new_string`、old==new 一律拒绝且不动文件
- **`merge(source, from_path, to_path)`** — 把一篇**已存在**的记忆并入另一篇，
  然后删除源文件。并入段以 `## 源文件相对路径` 起头、前有 `---` 分隔线，来源可追溯；
  先写目标后删源，删除失败不回滚，响应以 `source_removed: false` 如实标出
- **`delete(source, path)`** — 真删一篇记忆（无备份、不可恢复）；不做回收站 / 软删除

### 新增（配置与 CLI）

- **删除断路器 `allow_mcp_delete`**（config.json，默认 `false`，重启生效）：
  `delete` 与 `merge` 同受控制（merge 会删源）——关闭时连工具都不注册，
  tools/list 里完全不可见，LLM 看不到就不会调用；写入层另有同一开关的运行时闸兜底。
  开启 = 人工改配置并重启，无热切换
- **CLI**：`python3 config.py config set allow_mcp_delete <true|false> [--restart]`；
  非布尔值拒绝且不改配置文件

### 变更

- 工具面：常驻 7 个（`search` / `get-document` / `save` / `rename` / `replace` /
  `list-sources` / `recent`）+ 断路器控制 2 个，共 9 个
- 删除能力默认红线关闭——删除不可恢复，开闸只能人工显式配置
- 文档同步：README（MCP 章节改为一览表 + 通用口径）、ARCHITECTURE（§6 工具面、
  §8 安全模型、模块表、风险清单）、GLOSSARY（`edited_by` 定义、删除断路器词条）、
  ADR-0006 / ADR-0014 修订标注

---

## [0.1.0] · 2026-09-24

首个正式版本。此前版本号已作废。由单目录的只读检索服务改造为
"人与 AI 共同使用、按 source 划分、可配置可写入"的本机记忆库。

### 新增

- **多 source**：config.json 声明任意个记忆目录（本地盘 / 挂载盘均可）；
  统一索引、检索可按 source 过滤；所有输出（search / recent / get-document /
  `list-sources`）逐条携带 `source` 与文档身份 `(source, path)`
- **只读/可写 source**：`writable` 布尔字段（省略即 `true`）；只读 source 照常检索、
  服务永不写入；有效可写 = 配置可写 ∧ 当前可用 ∧ 存储 local
- **MCP 写入工具 `save(source, filename, content, category="")`**：
  落到 `<source>/<category>/<filename>.md`，已存在即整篇覆盖；空正文一律拒绝；
  响应带 `created`（新建/覆盖）与 `replaced_char_count`（被替换内容长度）
- **config.json + config.py CLI**：配置全部收进一个 JSON（默认 `~/.myMemory/config.json`，
  环境变量 `MEMORY_CONFIG` 覆盖），改名/删目录等操作只能人工完成，**重启生效**；
  `config.py` 提供 `source list/add/edit/remove`、`config set poll_interval` / `max_cached_docs`、
  `reindex`、`restart`；校验失败时一个字节都不改配置文件
- **最近编辑列表 `recent`（MCP 工具 + `GET /recent`）**：按 mtime 倒序每文件一条，
  `edited_by` 区分 `agent`（经 save 写入）与 `scan`（人改的或绕过服务的改动）
- **索引缓存与增量更新**（`index.cache`）：启动先用缓存立即服务、后台增量校验
  （`/health` 的 `verifying`）；轮询、save、reindex 走同一条增量刷新路径，并发合并；
  文件名、日期（YY-MM-DD）、长标识符参与分词，路径语义可检索

### 修复

- **文件名白名单**：放行 `( ) [ ] { } . , & # + @ ! ' = ~ %` 等常用半角符号——
  此前带英文括号的文件名保存失败；`\ / : * ? " < > |`、`..`、首尾 `.` 与空格、
  Windows 保留名在语法层拒绝，跨目录写入不可能发生

### 安全

- 配置修改与 source 管理完全退出 AI 可达面：CLI / 手改配置是唯一入口，MCP 不暴露
- 写入边界集中在 `writer.py`：source 显式、白名单校验、只读拒绝、可用性探测；
  工具 / REST / 挂载盘三层隔离，REST 端点全为只读

### 移除

- 按平台区分的虚拟环境机制（`.venv-windows` / `.venv-linux`），统一为 `.venv`，
  只在当前系统运行；多平台共用一个目录导致的 PID 文件互相覆盖问题随之消失
- 独立的 `status.json`（agent 标记并入索引缓存）；Git 采集与环境变量配置
