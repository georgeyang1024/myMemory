# ADR-0029 · 单实例每请求限域 + 个人 source 动态派生热发现

**状态**：提案 · 2026-10-01（开发细节见 [SPEC-MULTI-USER](../SPEC-MULTI-USER.md)）

**修订**：2026-10-01（实施前审查）——① 决策一补"限域两层"（scoped config 单独
兜不住默认跨 source 路径）；② 决策二删掉指纹扩展：它建立在"现有轮询指纹
(文件数， max mtime)"之上，而代码里**不存在**这样的指纹——轮询是每轮无条件
做一次增量 refresh（全量 stat 扫描），"每轮当场枚举 effective sources"本身
就覆盖新目录/新文件/改名三个场景；把名集塞进磁盘缓存的 `cache_fingerprint`
反而会在用户增删时作废整个缓存触发不必要的全量重建。

## 决策一：单实例每请求限域，不做每用户一个 server 实例

多人共用走**一个** `MCPServer` 实例 + 每请求按 `?user=` 构造**限域 Config 视图**：

- 一个纯 ASGI 中间件包住整个应用：解析 `?user=`，校验（见下），未知用户直接
  HTTP 400 拒绝；校验通过后把用户名**注入请求头**（`x-mymemory-user`），再交给内层 SDK 应用。
  头值 percent-encode（UTF-8）——ASGI 头是 bytes、Starlette 按 latin-1 解码，
  中文用户名裸注入会乱码；读侧 percent-decode。**该头是内部通道不是身份来源**：
  中间件对每个请求无条件删除客户端自带的同名头，身份只认 `?user=`。
- **限域两层**（实施前审查修正）：scoped config 管 `source=` 显式参数与写入，
  **范围名集**（`snapshot.search` 增 `allowed_sources` 参数；recent /
  get-document / suggest 在 run_* 层过滤）管默认跨 source 路径——否则 alice
  的默认 search / recent / get-document 会从全局快照拿到 bob 的内容与路径。
  因此"读取类 run_* 零改动"不成立：读取类与写路径一样演进签名。
- 工具函数经 SDK 的 `Context.headers` 读到该头（已对 `mcp==2.1.1` 实测确认：
  streamable_http 传输把 Starlette 原始 Request 挂进
  `ServerMessageMetadata.request_context`，`ctx.headers` 可用），据此用
  `dataclasses.replace(config, sources=…)` 构造限域视图传给既有
  `run_*` 函数。
- stdio / 无该头 → 访客（仅公共 source），与"stdio 忽略个人根目录"决策自然吻合。

**否决的替代方案**：每用户一个 `MCPServer` 实例按查询参数派发。它同样能让
"source 不存在"错误自然兜住跨人访问，但每人一份 `streamable_http_app` 各带
独立 lifespan / session manager，而用户目录是**热发现**的（随时可能出现新用户），
冷启动一个新 app 的生命周期管理很别扭；且工具注册 ×N 份纯属浪费。单实例方案
的**限域**对所有 `run_*` 函数零改动——限域全部落在 config.source() 查找这一层
（编辑者登记后来给写路径 run_* 增加了 editor 参，见
[ADR-0031](0031-editor-attribution.md)，与限域无关）。

**否决的替代方案**：中间件设 ContextVar、工具读之。SDK 以线程池跑同步工具，
contextvar 跨线程传播依赖 anyio 实现细节；头注入让用户随**请求本身**而非
环境状态传播，stdio 下天然缺席（正是想要的行为）。

## 决策二：个人 source 动态派生 + 热发现

`Config.sources` 从"启动时一次性静态解析"改为两层：

- **静态**：`sources` 列表（公共），启动时校验，重启生效——照旧。
- **动态**：个人根目录（`multi_user.store_dir`，见
  [ADR-0030](0030-multi-user-config-and-admin-full-scope.md)）下的一级子目录
  每次**用时枚举**派生（name=目录名、
  dir=root/名、writable=true），不进 config.json、不落索引缓存身份。

理由：开通 = 建目录，是服务器上的运维动作；若走"枚举一次存配置"，新用户
要重启生效，与"热生效"决策冲突。

热发现分两条线，缺一不可：

1. **路由校验线**：中间件对未知用户拒绝前**当场 `scandir`**个人根目录
   （≤10 人、一层目录，代价可忽略）——新目录建好立刻可用，不等索引。
2. **索引线**：构建与轮询刷新一律用**当场重新枚举**的 effective sources。
   这本身就覆盖了曾担心的三个场景（修订时删掉了建立在不存在的"轮询指纹"
   之上的指纹扩展方案）：空的新用户目录进 source 集（availability 出现）；
   新目录里的文件直接被扫描进索引；用户目录改名（alice → alice2）时 entries
   dict 每轮从空重建，旧 source 条目自动移除、新 source 全量进索引。
   磁盘缓存的 `cache_fingerprint`（配置级）保持不含动态 source 名集——
   用户增删不作废缓存，由增量刷新消化。

**缓存启动路径同样用 effective sources**：`_snapshot_from_cache` 的条目名
过滤、availability 探测与 `open_storages`，以及 `_rescue_previous` 的抢救
过滤，都按 effective sources 进行——否则重启后、后台校验完成前个人 source
搜不到，CACHE_FORMAT bump 升级那次全量重建还会把 personal 的 agent_mtime
抢救一起丢掉。

派生过滤规则：目录名不匹配 source 名白名单（`_SOURCE_NAME_PATTERN`）或与
公共 source 重名的跳过并在日志与 `/health` 警告，不使服务失败——与
"目录不存在不阻止启动"（需求 §8）的宽容语义一致。

个人 source 的消失沿用既有语义：盘根可访问但目录不见 = `dir_missing`
（索引与缓存清除）；个人根目录本身掉盘 = 全体个人 source `disk_offline`
（不更新不删除，恢复后自动回来）。
