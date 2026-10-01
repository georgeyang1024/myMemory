# 多人共用 · 开发细节 Spec

> 状态：**提案 · 待实施**（2026-10-01 grilling 会话产出；同日实施前审查修订）
> 决策记录见 [ADR-0028](adr/0028-multi-user-shared-deployment.md)、
> [ADR-0029](adr/0029-single-instance-scoping-and-dynamic-personal-sources.md)、
> [ADR-0030](adr/0030-multi-user-config-and-admin-full-scope.md)、
> [ADR-0031](adr/0031-editor-attribution.md)、
> [ADR-0032](adr/0032-remove-merge-tool.md)；
> 术语以 [GLOSSARY.md](GLOSSARY.md) §多人共用 为准。
> 目标版本 **0.5.0**（当前 0.4.0）。实施后同步修订 [ARCHITECTURE.md](ARCHITECTURE.md)
> 与 [REQUIREMENTS.md](REQUIREMENTS.md) §16。

---

## 1. 目标与非目标

### 目标

1. 服务部署到服务器，小队（≤10 人）多人共用一个实例。
2. 新增可选配置子项 `multi_user`（内含 `enabled` 开关、`store_dir` 与 `admins`）；
   不配置或 `enabled` 缺省（默认 false）→ 单机形态，行为与 0.4.0 完全一致（兼容锁死）。
3. `?user=<名字>` 路由个人 source：会话范围 = 全部公共 source + 该用户个人 source。
4. 管理员（`multi_user.admins` 白名单，未配置时默认 `["admin"]`）全域读写 store_dir：
   全域单会话（公共 + 全部个人 source），开通 = 改配置重启生效。
5. 文件夹即授权：服务器个人根目录下建一级子目录 = 开通该用户；热生效，无需重启。
6. 非法 user 在 HTTP 层 4xx 拒绝并提示；访客（无参数）仅公共；管理员豁免"目录存在"要求。
7. REST `/search` `/recent` 同规则限域；`/health` 默认只揭示 `store_dir` 与
   `guest_writable`，`?user=<管理员名>` 才另给 `admins` 名单与开通的用户清单。
8. 编辑者登记：recent 的 `edited_by` 整体更名 `editor`，多人下登记路由身份（用户名/管理员名/`guest`）。
9. 限域两层：scoped config 管 `source=` 显式参数，**范围名集过滤管默认跨 source 路径**
   （search / recent / get-document / _suggest）——二者缺一不可（§4.2，实施前审查补充）。

### 非目标

| 非目标 | 理由 |
|---|---|
| 鉴权 / token / 身份验证 | 本期软边界：知道用户名（含管理员名）即可冒充是已接受的已知风险（ADR-0028/0030），后续收紧再议 |
| 管理员写 `writable: false` 的公共 source | 尊重 writable 语义（ADR-0022）；需要 AI 维护的公共 source 显式配 `writable: true` |
| 管理员绕过 `allow_mcp_delete` | 全局断路器=真全局（需求 §15），管理员不豁免 |
| 公共 source 配个人根目录的父目录（特权总览） | 打破不重叠不嵌套不变量 + 双重索引，禁止 |
| 个人根目录多个 | 一个根目录，其下一级子目录即用户全集 |
| 经 MCP / REST 增删用户、改管理员名单或改配置 | 与"配置无 MCP 面"既有决策一致：开通用户 = 服务器建目录，开通管理员 = 改配置重启 |
| 自动开通（新名字首次出现即建目录） | 拼错名字会建孤儿目录；建目录是管理员的明确动作 |
| 用户级配额 / 容量限制 | 规模小，不做 |
| 审计日志 / 历史版本 | 编辑列表每文件一条、不留历史（既有语义）；只登记"最后一次经 MCP 改动者" |

---

## 2. 领域模型（引用 GLOSSARY）

```
config.json
├── sources[]                    公共 source（沿用，字段不变，多人下对全部会话可见）
└── multi_user                   可选大配置子项；enabled = true 才进入多人共用形态
    ├── enabled                  显式开关（bool，默认 false：块可预配而不启用）
    ├── store_dir            个人根目录（必填）
    │   └── <一级子目录>/        每个目录 = 一个用户 = 一个个人 source
    │                            目录名 = 用户名 = source 名（恒可写）
    ├── admins[]                 管理员名单（纯授权身份，不要求个人目录；未配置默认 ["admin"]）
    └── guest_writable           访客能否写公共 source（bool，默认 false）

?user=<用户名>   ──路由──▶  会话范围 = 公共 sources + 该用户的个人 source
?user=<管理员名> ──路由──▶  全域范围 = 公共 sources + 全部个人 sources
（缺省）         ──路由──▶  访客 = 仅公共 sources
```

## 3. 配置变更（`src/config.py`）

### 3.1 schema

```json
{
  "sources": [ ],
  "multi_user": {
    "enabled": true,
    "store_dir": "E:\\memories\\users",
    "admins": ["admin"],
    "guest_writable": false
  }
}
```

- `multi_user`：可选对象；`enabled = true` → 多人共用形态（`enabled`
  **默认 false**：块存在但开关关闭 = 单机形态，块内其余配置保留备启用）。
  `Config.from_data`
  新增字段解析（`_KNOWN_KEYS` 增补），`Config` dataclass 增
  `multi_user: MultiUserConfig | None`（dataclass：`store_dir: Path`、
  `admins: tuple[str, ...]`、`guest_writable: bool = False`）。
- **团队形态允许没有公共 source**（2026-10-01，ADR-0028/0025 修订）：
  `enabled: true` 时 `sources` 可缺省或为空——语料 = 存储目录成员子文件夹
  派生的个人 source；开关关闭时 `sources` 仍必须非空。首次建档的团队分支
  （`run.py` 交互：选团队使用 → 问存储目录 + 管理员账号默认 admin）写出的
  就是不含公共 source 的这份配置。
- 内部键：`enabled` 必为布尔（默认 false，非布尔报 `ConfigError`）；
  `store_dir` 必填（缺失报 `ConfigError`）；`admins` 可选字符串数组
  （**未显式配置时默认 `["admin"]`**；显式给出——含空数组——完全按给定值）；
  `guest_writable` 可选布尔（默认 false，非布尔报 `ConfigError`）；
  未知键报 `ConfigError`（沿用 `_KNOWN_KEYS` 的口径）。
- CLI（根目录 `config.py`）新增子命令：
  - `config.py multi-user set <dir>` / `multi-user unset`：启用/移除多人共用形态
    （`set` 校验目录存在，与 `source add` 同口径，并写 `enabled: true`；走既有
    `Config.from_data` 校验后落盘的路径）。
  - `config.py multi-user enable` / `multi-user disable`：翻开关（重启生效；
    disable 保留 store_dir / admins / guest_writable，只把 enabled 置 false）。
  - `config.py multi-user admin add <名字>` / `admin remove <名字>` /
    `admin list`：维护管理员名单（重启生效）。
  - `config.py multi-user show`：展示当前 multi_user 配置与生效状态。
  - `config.py multi-user user list`：枚举个人根目录下的一级子目录与派生有效性
    （§3.3 规则），排障用。
  - `config.py multi-user user add <名字>`：校验名字合法（§3.3，含**保留字**
    agent/scan/guest
    直接拒绝）且不与公共 source 重名后**创建目录**（建目录 = 开通；`mkdir` 的
    便捷封装，等价于手工建目录；早失败——运行时派生过滤只是兜底）。

  多人共用相关子命令全部统一挂在 `multi-user` 名下（`set/unset/enable/disable/
  show/admin …/user …`），CLI 顶层不再有独立的 `user` 命令。

### 3.2 校验规则（`Config.from_data`）

1. `multi_user.store_dir`：字符串、非空；补绝对路径，不解析符号链接
   （与 source dir 同口径）。
2. **不要求目录存在**（与 sources 一致，需求 §8）：目录不在 = 0 个用户，
   启动警告，服务照常。
3. `multi_user.admins`：字符串数组；**未显式配置时默认 `["admin"]`**（ADR-0030
   修订二）；每项 **strip 后入表**、去重、空串剔除并警告；
   名字**不做** `_SOURCE_NAME_PATTERN` 校验（管理员是纯授权身份，ADR-0030）。
   硬约束两条（都是防头注入的**传输层**约束，不是命名白名单——管理员名要经
   `?user=` 逐字匹配后注入请求头）：**不得含控制字符**（CR/LF/NUL 等，含则
   `ConfigError`）；**长度 ≤64**（与 user 参数上限一致）。另有编辑者保留字约束：
   不得为 `agent` / `scan` / `guest`（ADR-0031，枚举值歧义）——**大小写不敏感**
   （casefold 比较；Windows 目录名本就不区分大小写，`Guest` 目录与保留字
   `guest` 的歧义在 Windows 上真实存在，Linux 上从严统一）。
4. **嵌套禁令扩展**：`multi_user.store_dir` 与任何公共 source 目录不得
   互相包含（复用 `parse_sources` 里 `_contains`/`_real_dir` 的检查，把
   store_dir 纳入两两比较）。违例报 `ConfigError`，措辞与现有嵌套报错一致。
5. `multi_user` 内部不参与任何命名约束（`store_dir` 不是 source）。

### 3.3 个人 source 派生规则（新函数，放 `src/config.py`）

`personal_sources(store_dir, static_names) -> tuple[Source, ...]`：

- 枚举 `store_dir` 下一级**子目录**（`os.scandir`，忽略文件与更深层级）。
- 每个目录派生 `Source(name=目录名, dir=个人根目录/目录名, type="local", writable=True, description=f"个人记忆源（经 ?user={目录名} 路由）")`，
  scoring 沿用全局值（个人 source 不单独配 scoring）。
- **过滤**（跳过并在日志与 `/health` 警告，不报错不挡启动）：
  a. 目录名不匹配 `_SOURCE_NAME_PATTERN`（含空格、点等）；
  b. 目录名与公共 source 名重名（casefold 比较，理由同 §3.2 保留字——
     Windows 目录名不区分大小写，`ALICE` 目录与公共 source `alice` 名义上
     是两个 source、实际可能指向纠缠的目录）；
  c. 目录名为保留字 `agent` / `scan` / `guest`（casefold 比较，§3.2）；
  d. 目录名与前文已派生的个人 source 重名（casefold 比较；Linux 上
     `Alice` 与 `alice` 可并存，过滤其一，scandir 枚举序稳定的平台上行为确定）。
  警告文案给出命名规则与重名原因，管理员可据此改名。
- **effective sources** = 公共 + 全部派生个人 source，供索引层使用
  （新函数 `effective_sources(config)`，每次调用当场枚举——ADR-0029 决策二）。

### 3.4 用户与管理员校验（路由层用）

`find_user(config, name) -> Source | None`：按 §3.3 规则**当场枚举**，
返回名字**逐字相等**的个人 source；无 → None。

- 大小写敏感的字符串比较（枚举出的目录名是权威拼写；Windows 文件系统不区分
  大小写，但路由层区分——`?user=Alice` 在目录名为 `alice` 时拒绝，跨平台行为一致，
  也不会经大小写歧义绕过 §3.3b 的重名过滤）。
- **strip 口径**：URL 解码后由**中间件 strip 一次**（§4.1），`find_user` 与
  `is_admin` 都对 strip 后的值**逐字比较**、各自不再 strip——两边同口径
  （`?user=李四%20` 与 `?user=alice%20` 要么都过、要么都不过）。

`is_admin(config, name) -> bool`：`name` 与 `config.multi_user.admins`
**逐字相等**（strip 已由中间件做过，配置解析时管理员名也已 strip 入表，§3.2.3）。
无目录要求、无命名白名单校验（校验在配置加载时做，§3.2.3）。

**路由合法性**（中间件用）：`name` 合法 ⟺ `is_admin(name)` **或** `find_user(name)` 命中。
合法名字集合 = **admins ∪ 个人目录名**；管理员豁免"目录存在"要求（ADR-0030）。

## 4. 路由与限域（`src/server.py` + `src/main.py`）

### 4.1 ASGI 中间件（新，`src/server.py` 或独立模块）

包住整个 uvicorn 应用（SDK `streamable_http_app` 之外再包一层纯 ASGI/Starlette 中间件）：

```
请求 → 归一路径（去掉一个尾斜杠）→ 解析 scope["query_string"] 里的 user 参数
  ├─ 路径不在校验集（404 等）→ 原样放行，不碰 user 参数
  ├─ 无 user            → 放行，**删除** x-mymemory-user 头（访客/公共 only）
  ├─ strip 后 is_admin   → 注入头后放行（管理员豁免个人目录要求，ADR-0030）
  ├─ strip 后 find_user  → 注入头后放行
  └─ 两者都未命中        → 直接返回 400 JSON，不进内层应用：
        {"error": "user 不存在：<名字>。该用户未开通（个人根目录下无此目录）
                   或不在管理员名单中，请联系管理员开通后重试"}
```

- 合法名字集合 = `is_admin(name)` ∪ `find_user(name)`（§3.4）；中间件闭包持有
  config，判定与注入之外不做任何事——**管理员/普通用户的分叉不在中间件**，
  而在工具层的 scoped_config（§4.2），中间件职责单一。
- **校验路径集** = `/mcp`、`/search`、`/recent`、`/health`（判定前先去掉一个尾斜杠）：
  `/mcp/`、`/search/` 等尾斜杠变体**同样校验**、未命中同样 400——**fail loud
  优于静默降级**（URL 写错尾斜杠静默变访客会让个人 source 无声消失、写入记
  guest，极难排查）。`/health` 在校验集内是因为它的 `multi_user` 块按身份分层
  （见 §4.3 REST 表）——无效 user 在这里同样 400。`/reindex` 也已入校验集，
  且多人共用下仅管理员可触发（§4.3）；其他路径（404）忽略 `user` 参数原样放行。
- `user` 参数值**URL 解码后 strip 一次**（§3.4 口径）再比较；strip 后为空
  （`?user=` 或 `?user=%20%20`）视同缺省 → 访客；超过 64 字符 → 400（与
  source 名上限一致，防头注入超长值）；重复参数（`?user=a&user=b`）取首个，
  忽略其余。
- **身份只来自 `?user=` URL 参数**：中间件对每个请求**无条件删除客户端自带的
  `x-mymemory-user` 头**，合法时以中间件自己算出的值为准重新注入——客户端
  伪造该头不可能绕过路由校验直接成为管理员/他人。
- **注入值的编码**：ASGI 头是 bytes，Starlette 按 latin-1 解码头值，中文等
  非 ASCII 用户名不能裸注入。锁定方案：**注入侧 percent-encode（UTF-8，
  ASCII 安全）、读取侧 percent-decode**——`scoped_config` / editor 计算处
  读头后先解码再用（§4.2、§6.3）。
- **user 是每请求判定，不绑定 mcp 会话**：SDK 会话（mcp-session-id）与 user
  无绑定，客户端应固定完整 URL；GET SSE 重连请求若不带 user 参数，该通道按
  访客放行（scope 只在 POST 的工具调用上判定，SSE 是服务端推送通道，无 scope
  影响）。后续收紧候选：initialize 时把 user 绑到会话、后续请求不一致即 403
  （见 [ADR-0028](adr/0028-multi-user-shared-deployment.md) 收紧路线）。
- 中间件不做目录枚举缓存（§3.4 每次当场枚举；≤10 人一层 `scandir`，代价可忽略），
  保证开通即生效。

### 4.2 工具层限域（`src/server.py` `create_server`）

- 每个工具函数增加 `ctx: Context` 参数，经 `ctx.headers.get("x-mymemory-user")`
  取当前用户（**percent-decode 后使用**，§4.1）。stdio 下该头天然不存在 →
  访客；`ctx.request_context` 缺失时同样回退访客。
- 新函数 `scoped_config(config, user) -> Config`（管理员/普通用户的唯一分叉点）：
  - `user` 为 None/空 → **访客**：`sources = 公共 sources`，且各 Source 的
    `writable` **一律置 false**（`multi_user.guest_writable: false` 时，下同）——
    访客对公共 source 的写入沿现有只读拒绝路径失败，list-sources 对访客显示
    全部只读；`guest_writable: true` 时保持原 `writable` 值；
  - `is_admin(config, user)` → **全域**：`sources = 公共 + 全部个人 source`（§3.3 派生全集）；
  - 其余 → `sources = 公共 + (find_user(config, user) 或 空)`。
- **限域是两层，缺一不可**（实施前审查发现的前提修正——原声明"run_* 零改动、
  限域自然落在 config.source() 查找层"只对**显式 `source=` 参数**成立，
  默认跨 source 路径不经过 config.source()，会从全局快照泄漏他人内容）：
  1. **scoped config**（上）：管 `source=` 显式参数与写入路径；
  2. **范围名集过滤**：管默认（跨 source）路径——
     - `snapshot.search` 增 `allowed_sources: frozenset[str] | None` 参数，
       `source is None` 时按名集过滤 chunk（src/index.py）；
     - `run_search` / `run_recent` / `run_get_document` / `_suggest` 各增
       会话范围参数（传 scoped config 算出的名集）：recent 只列范围内条目、
       get-document 的 `indexed_paths` 命中判定先过范围（范围外按现有
       "source 不存在 / 路径不在索引中"错误）、`_suggest` 只在范围内找近似文档。
     写路径 `run_save` / `run_rename` / `run_replace` 增 editor 参（§6.3）、
     `run_delete` 不变——读取类与写路径的签名演进统一记录在 §9。
- 工具入口把 `scoped_config(...)` 与范围名集传给既有 `run_search` /
  `run_get_document` / `run_save` / `run_rename` / `run_replace` / `run_delete` /
  `run_list_sources` / `run_recent`：
  - 范围外 source 在 `config.source(name)` 查找层落空 → 现有"source 不存在
    （现有：…）"错误，现有列表只含会话范围 → **不揭示其他用户**；
  - 默认 `search` / `recent` / `get-document` / `_suggest` 被范围名集兜住 →
    **不泄漏其他用户的内容与路径**。
- `SERVER_INSTRUCTIONS` 与工具描述增补一句：多人共用时一个会话只含公共与本人的
  个人 source，`list-sources` 返回即全集。
- `delete` 的注册开关 `allow_mcp_delete` 为全局配置，不限域（公共+个人同规则；
  merge 工具已移除，断路器语义收窄为只管 delete，ADR-0032）。

### 4.3 REST 端点（`create_server` 自定义路由）

| 端点 | 行为 |
|---|---|
| `GET /search` / `GET /recent` | 同规则：读 `user` 查询参数 → 中间件已校验（未命中 400）；命中则处理器内构造 `scoped_config` 与范围名集再调 `run_search` / `run_recent` |
| `GET /health` | 也在校验路径集内（未开通 user 同样 400）。`multi_user` 块**按身份分层**：默认（访客/普通用户/无 user）只给 `store_dir`（个人根目录）与 `guest_writable`；`?user=<管理员名>` 另给 `admins: [名字…]` 与 `users: [{name, available, doc_count}]`。现有 sources 列表照旧展示（公共） |
| `POST /reindex` | 也在校验路径集内（未开通 user 同样 400）。**多人共用下仅管理员**可触发（`?user=<管理员名>`），其余 403——重建影响整个实例；单机形态不变（免鉴权开放） |

### 4.4 stdio（`src/main.py`）

- stdio 形态忽略整个 `multi_user` 子项：`main()` 在 `--stdio` 下以
  `dataclasses.replace(config, multi_user=None)` 构造传给工具层的配置——
  工具面 = 公共 only（scoped_config 对 `multi_user is None` 原样返回、
  editor 记 None → `agent`），**admins 与 guest_writable 在 stdio 下同样无效果**，
  单机语义（公共 source 照自身 `writable` 可写）完全不变。无需新增 `--user` 参数
  （单机个人使用首选 stdio，本机自己的记忆用单机配置更合适）。
- `--check` 自检对**effective sources**（含派生个人 source）做索引构建与报告，
  并追加输出：multi_user 启用状态、个人根目录、当前用户数、admins 名单、
  `guest_writable`、被跳过目录及原因。

## 5. 索引与热发现（`src/index.py`）

1. **构建输入换为 effective sources**。`IndexHolder` 持有的仍是基座 config
   （`config_file`、缓存路径等不变），但**构建/刷新的消费点**全部改为当场
   枚举的 effective sources——具体消费点清单（实施前审查补全）：
   - `IndexHolder.__init__` 的 `open_storages`；
   - `build_now` / `start` 传给 `build` / `read_cache` / `_snapshot_from_cache`
     的 config：**传 effective config**（`dataclasses.replace(config,
     sources=effective_sources(config))`），否则 `_snapshot_from_cache` 的
     条目名过滤（`e.source in names`）、availability 探测、`open_storages`
     会把个人 source 的缓存条目整个丢掉——重启后、后台校验完成前
     `?user=alice` 搜到空；`_rescue_previous` 同理，CACHE_FORMAT bump 升级
     那次全量重建会连 personal 的 agent_mtime 一起丢；
   - `_rebuild_once` 的 `refresh`：传 effective config（新用户目录进 source 集），
     并**每轮重建 `self._storages`**（动态源在上一轮可能还没有句柄；快照与
     `update_availability` 用同一份，掉盘判定语义照旧）。
2. **不需要新指纹**（实施前审查修正）：ADR-0029 曾假设"现有轮询指纹
   (文件数， max mtime)"并据此提出扩展为 (source 名集, 文件数, max mtime)。
   **代码事实**：轮询是每轮无条件做一次增量 `refresh`（全量 stat 扫描），
   不存在这样的轮询指纹；真正存在的只有磁盘缓存的 `cache_fingerprint`
   （配置级）与快照间复用判定用的 `_content_signature`（全量键集）。
   而"每轮当场枚举 effective sources"本身就覆盖了曾担心的三个洞——
   a. 空的新用户目录（进 source 集，availability 出现）；
   b. 新目录里的文件（该目录已在 source 集内，直接扫描进索引）；
   c. 用户目录改名（alice → alice2：entries dict 每轮从空重建，
      旧 source 条目自动移除、新 source 全量进索引）。
   把名集塞进 `cache_fingerprint` 反而有害：用户增删会作废整个磁盘缓存
   触发不必要的全量重建。指纹条目不动（GLOSSARY 相应修正）。
3. 缓存/掉盘语义照旧（ADR-0020）：用户目录被删 → `dir_missing` → 索引与缓存清除；
   个人根目录所在盘掉线 → 全体个人 source `disk_offline`，内容从缓存可检索；
   **个人根目录本身被删**（盘根可访问、目录不见）→ 全体个人 source `dir_missing`
   → 索引与缓存全部清除，`?user=` 全部 400。
4. 规模核对：≤10 人 × 千篇 + 公共 ≈ 万篇级，单进程内存索引与现状（部门知识库
   万篇实测）同量级，无分片必要。

## 6. 编辑者登记（`edited_by` → `editor`，ADR-0031）

"哪次改动是哪个用户做的"——一个二元判定升级为带主体登记，判定机制不变
（ADR-0018：agent_mtime 判定"写后未被动过"）：

1. **字段更名**：recent 输出 `edited_by` **整体替换**为 `editor`
   （recent 是唯一暴露点，search / get-document 本就不带）：
   - 单机形态（无 `multi_user`）：`agent` / `scan`，语义与从前完全一致；
   - 多人形态：MCP 写入记**路由身份**——普通用户记用户名、管理员记管理员名
     （登记动作者，不管写进谁的 source）；访客写入记 `guest`；
     非 MCP 改动仍 `scan`（人手改动无法归因，不推断）。
2. **保留字**：`agent` / `scan` / `guest` 不得用作个人目录名（§3.3 派生过滤
   跳过并警告）与管理员名（§3.2 配置校验拒绝）。
3. **数据流**：
   - 工具层计算 editor 身份（与 `scoped_config` 同源的头读取）：
     `multi_user` 存在时 = 用户名 / 管理员名 / `"guest"`（无 user 参数），
     否则 `None`；
   - `run_save` / `run_rename` / `run_replace` 增 `editor: str | None`
     参数——**§4.2 "读取类零改动"声明的例外**：写路径签名演进；
     `run_delete` 无标记（文件已删，条目随刷新消失）；
     写入成功后的 `request_rebuild` 理由带编辑者（"张三 创建记忆 公共/周报.md"）——
     日志里留下轻量审计线索（数据层不留历史，§1 非目标）；
   - `IndexHolder.mark_agent(source, path, mtime, editor)`：`_agent_marks`
     的值由 `float` 扩展为 `(mtime, editor)`，刷新时一并并入条目；
   - `FileEntry` 增 `agent_editor: str | None`，`edited_by` 属性更名为
     `editor`：agent_mtime 失配 → `scan`；匹配且 `agent_editor` 为 None →
     `agent`（单机写入路径传 None）；否则返回 `agent_editor`（用户名/guest）。
4. **缓存**：`CACHE_FORMAT` 3→4；条目序列化增 `agent_editor`；
   **不兼容旧缓存**——bump 后升级首次启动全量重建一次，rescue 路径沿用
   agent_mtime、editor 记 None（显示 `agent`），一次性损失（ADR-0031）。
   注意 bump 影响**所有形态**：不用多人功能的单机用户升级 0.5.0 同样付一次
   全量重建（万篇级分钟级，CHANGELOG 已标注）。
5. **文案**：`SERVER_INSTRUCTIONS` 与 `RECENT_DESCRIPTION` 中 `edited_by`
   的表述同步改为 `editor` 与新取值说明。

## 7. 错误与提示文案（锁定）

| 场景 | 响应 |
|---|---|
| `?user=` 未开通且非管理员（中间层） | HTTP 400 + §4.1 JSON 文案（不列现有用户，不暴露 store_dir 绝对路径） |
| 校验路径的尾斜杠变体（如 `/mcp/`）未开通 | 同上 400 文案（fail loud，不静默降级为访客） |
| `?user=<管理员名>` 且无同名个人目录 | 放行，全域范围（管理员豁免"目录存在"，ADR-0030） |
| 访客写公共 source（`guest_writable: false`） | 现有只读拒绝文案（"source X 为只读"）；list-sources 对访客显示全部 `writable: false` |
| 工具层 `source=范围外名字` | 现有"source 不存在：<名字>（现有：<会话内可见列表>）"——访客/普通用户/管理员各自的范围决定可见列表 |
| `/health` | 默认只揭示 `store_dir` 与 `guest_writable`；`?user=<管理员名>` 另给 `admins` 名单与开通的用户清单（身份层软：知道管理员名即可冒充，ADR-0028 已接受风险） |

## 8. 测试计划（`tests/`）

新增 `tests/test_multiuser.py`，沿用现有 fixture 风格（临时目录 + `Config.from_data`）：

1. **配置**：`multi_user` 解析（缺省 None / 对象 / `enabled` 缺省 false → 单机 /
   `enabled` 非布尔报错 / `enabled: true` 但缺 `store_dir` 报错 /
   未知键报错）；`admins` 解析（**缺省 `["admin"]`** / 显式空数组 = 无管理员 /
   非数组报错 / strip 去重、空串剔除并警告 /
   含控制字符或长度 >64 报 `ConfigError` / 含保留字 agent/scan/guest 报
   `ConfigError`——**含 `Guest` 等大小写变体同样报错**）；`guest_writable` 解析
   （缺省 false / true / 非布尔报错）；store_dir 与
   公共 source 嵌套（双向）报 `ConfigError`；目录不存在不报错。
2. **派生**：一级子目录派生与过滤（非法名跳过、与公共重名跳过、保留字跳过、
   个人彼此 casefold 重名跳过其一）；`find_user` 大小写敏感逐字匹配；文件与
   深层目录不派生。
3. **中间件**：无 user 放行；`?user=` 空串/全空白放行（访客）；命中注入头；
   未命中 400 且文案含用户名与"联系管理员"；`?user=<管理员名>`（无同名目录）
   放行；超长（>64）400；percent-encoded 中文名解码后匹配
   （`?user=%E5%BC%A0%E4%B8%89` ≡ 张三）；`?user=a&user=b` 取首个；
   `?user=alice%20`（尾空白）strip 后命中；`/mcp/`（尾斜杠变体）同样校验、
   未开通 400（fail loud）；**客户端自带 `x-mymemory-user` 头被中间件无条件
   覆盖/删除**（自带 `李四` 而不带 user 参数 → 仍为访客；带非法 user → 400）；
   `/health` 带未知 user 不被拒（忽略）。
4. **工具限域**（经 ASGI 测试客户端，同 `test_rest.py` 路数）：
   - alice 会话 `list-sources` = 公共 + `alice`；guest = 仅公共；
   - alice 会话 `save(source="bob", …)` → "source 不存在"，文案不含 `bob` 的任何痕迹；
   - **默认 `search` / `recent` 只覆盖会话范围（bob 的文档搜不到）——泄漏修复的
     回归锁**；alice `get-document(source="bob", path=<bob 已索引文档>)` →
     "source 不存在"，`_suggest` 结果不含 bob 的路径；
   - alice 写 `alice` 成功且落盘正确；写公共（`writable: false`）被拒；
   - `delete` 开关行为不随 user 变化。
5. **管理员全域**（同路数）：
   - `is_admin` 逐字匹配（大小写敏感）；
   - `?user=<管理员名>` 无同名个人目录 → 放行；
   - 管理员 `list-sources` = 公共 + 全部个人；默认 `search` / `recent` 跨全部
     （能搜到 bob 的文档且来源正确）；
   - 管理员 `save` / `rename` / `replace` 到他人个人 source 成功且落盘正确；
   - 管理员写 `writable: false` 公共 source 被拒（不突破）；
   - `allow_mcp_delete: false` 时管理员会话同样看不到 delete（断路器不豁免；
     merge 工具已移除，ADR-0032）。
6. **访客写公共**：`guest_writable` 缺省（false）→ 访客写公共（即使
   `writable: true`）被拒、list-sources 显示只读；`guest_writable: true` →
   访客写公共成功（editor 记 `guest`）；访客写任何个人 source 仍不可能
   （不在其范围内）。
7. **REST**：`/search` / `/recent` 限域与 400（含管理员全域与默认范围泄漏回归）；
   `/health` 的 `multi_user` 块字段（store_dir、admins、guest_writable、
   users、skipped）。
8. **热发现**：服务运行中建新用户目录 → `?user=新名字` 立即可用（不等轮询）；
   新目录放文件 → 下一轮增量刷新（当场枚举 effective sources）后可检索；
   **用户目录改名**（alice → alice2，一增一减）→ 下一轮刷新旧 source 条目
   清除、新 source 可检索；
   删用户目录 → `?user=` 400、索引条目清除；
   个人根目录本身被删 → 全体个人 source 条目与缓存清除、`?user=` 全部 400；
   **带缓存重启** → 个人 source 条目从缓存恢复（`_snapshot_from_cache` 用
   effective sources），不出现"重启后个人记忆暂时搜不到"的窗口。
9. **stdio**：带 `multi_user` 配置下工具面 = 公共 only（admins 与
   guest_writable 同样无效果；公共 source 照自身 `writable` 可写）。
10. **兼容锁**：无 `multi_user` 的现有配置 → 全部现有测试原样通过（契约测试不动）。
11. **编辑者**：单机形态 recent 输出字段为 `editor`（`edited_by` 键不存在），
    值 = agent / scan（不变）；多人形态张三 save → `editor: 张三`；
    李四（管理员）改 bob 的 source → `editor: 李四`；访客写公共（
    `guest_writable: true`）→ `editor: guest`；
    save 后人手改文件 → `editor: scan`；个人目录名 / 管理员名为保留字
    （agent/scan/guest，含大小写变体）被拒；缓存 bump 后旧缓存全量重建一次、
    agent_mtime 被 rescue 沿用。

## 9. 实施顺序与触点清单

| # | 触点 | 改动 |
|---|---|---|
| 1 | `src/config.py` | `multi_user` 子项解析 + 校验（store_dir、admins 含保留字 casefold 拒绝、guest_writable、嵌套禁令）+ `personal_sources` / `effective_sources` / `find_user` / `is_admin` |
| 2 | `src/index.py` | effective config 接入（build / refresh / `_snapshot_from_cache` / `_rescue_previous` / storages 每轮重建）；`snapshot.search` 增 `allowed_sources` 参数；`CACHE_FORMAT` 3→4；`FileEntry.agent_editor`、`edited_by` 属性→`editor`；`mark_agent` 增 editor 参、`_agent_marks` 值扩展 |
| 3 | `src/server.py` | 中间件（校验路径集含尾斜杠变体、strip、percent-encode 注入、无条件覆盖自带头）、`scoped_config`（访客含 guest_writable /用户/全域三分支）、范围名集过滤（run_search / run_recent / run_get_document / _suggest）、工具 `ctx` 参数、recent 输出 `edited_by`→`editor`、写路径 run_* 增 editor 参、REST、`/health` 的 `multi_user` 块、`SERVER_INSTRUCTIONS` / `RECENT_DESCRIPTION` 文案 |
| 4 | `src/main.py` | 挂中间件（仅 HTTP 形态）；stdio 下 `replace(config, multi_user=None)`；`--check` 用 effective sources 并输出 multi_user 状态 |
| 5 | `config.py`（CLI） | `multi-user set/unset/show/user list/user add`、`multi-user admin add/remove/list` |
| 6 | `tests/test_multiuser.py` | §8 |
| 7 | 既有测试 | recent 断言 `edited_by` → `editor`（test_server 等）；契约测试基线同步 |
| 8 | merge 移除（ADR-0032） | 删 `tool_merge` / `run_merge` / `MERGE_DESCRIPTION` / `writer.merge_memory` 及其测试；开启态契约基线 9→8 |
| 9 | 文档 | GLOSSARY（指纹条目随 §5 修正）、ARCHITECTURE、REQUIREMENTS §16、README 部署章节、CHANGELOG |

依赖不变（`mcp==2.1.1` 的 `Context.headers` 已实测可用）；版本 0.5.0。

## 10. 部署与管理流程（写进 README）

```
服务器（管理员本人，终端操作）            用户（每人各自）
1. config.py source add 公共 --dir <目录> --readonly
2. config.py multi-user set <个人根目录>
3. config.py multi-user user add 张三      # 或手工建目录；建目录=开通
4. config.py multi-user admin add 李四   # 可选：全域读写；改配置，重启生效
5. python run.py --background     # 启动/重启服务
                                          MCP 客户端配置（普通用户）：
                                          {"type": "http",
                                           "url": "http://<服务器>:<端口>/mcp?user=张三"}
                                          MCP 客户端配置（管理员）：
                                          {"type": "http",
                                           "url": "http://<服务器>:<端口>/mcp?user=李四"}
```

- 误拼用户名 → 400 提示，不会静默丢个人记忆。
- 新用户开通：服务器建目录即可，服务不重启。
- 新管理员开通：`multi-user admin add` + 重启服务。
- 收回用户：删目录（其索引与缓存随之清除；注意先备份其内容——无回收站）。
- 收回管理员：`multi-user admin remove` + 重启服务。
- 访客写公共：默认禁止（`guest_writable: false`）；确需匿名写入才显式打开。
- 管理员维护公共记忆：公共 source 配 `writable: true` 后 AI 可写；
  或保持只读、在服务器上直接编辑文件。
