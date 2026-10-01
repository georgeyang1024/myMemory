"""入口：读配置 -> 阻塞式首次构建 -> 启动轮询 -> 起传输层。

首次构建必须在开始接受请求之前完成，以避免出现
"服务已启动但索引未就绪"的窗口。

两种传输：

- **HTTP**（默认）：局域网多客户端共享一个实例，另带 /health 与 /search。
- **stdio**（`--stdio`）：由 MCP 客户端自己拉起进程，一个客户端一个实例，
  无端口、无监听、无暴露面。个人使用的首选。

用法：
    python src\\main.py            启动 HTTP 服务
    python src\\main.py --stdio    以 stdio 传输启动（供 MCP 客户端拉起）
    python src\\main.py --check    只做一次配置与索引自检，不监听端口
"""

from __future__ import annotations

import dataclasses
import logging
import sys
import time

# 代码使用了 dataclass(slots=True)（3.10 引入）。版本不足时若不拦截，
# 报出的是难以定位的 TypeError，因此在这里给出明确提示。
if sys.version_info < (3, 10):
    raise SystemExit(
        f"myMemory 需要 Python 3.10 或更高版本，当前为 "
        f"{sys.version_info.major}.{sys.version_info.minor}。"
    )

import uvicorn

from config import Config, ConfigError, effective_sources
from index import IndexHolder, build
from server import UserScopeMiddleware, create_server
from storage import open_storage

logger = logging.getLogger("mymemory")


def run_check(config: Config) -> int:
    """自检：只构建一次索引并报告结果，不监听端口。

    用于部署后确认"配置正确、语料找得到、依赖装全了"，
    不必先起服务再用另一个终端 curl。
    多人共用形态下对 effective sources（含派生个人 source）自检，
    并报告 multi_user 状态（SPEC §4.4）。
    """
    started = time.perf_counter()
    effective = dataclasses.replace(config, sources=effective_sources(config))
    snapshot = build(effective)
    elapsed = time.perf_counter() - started
    print(
        f"索引自检通过：{snapshot.doc_count} 文档 / "
        f"{snapshot.chunk_count} chunk / {elapsed:.2f}s"
    )
    if config.multi_user is not None:
        mu = config.multi_user
        print(
            f"多人共用已启用：个人根目录 {mu.store_dir}；"
            f"管理员 {', '.join(mu.admins) or '（无）'}；"
            f"访客{'可写' if mu.guest_writable else '只读'}公共 source"
        )
    if snapshot.doc_count == 0:
        print(
            f"警告：索引为空。若记忆库刚建好、还没写过记忆，这是正常的；"
            f"否则请检查 {config.config_file} 里各 source 的 dir 是否指向记忆目录。"
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check_only = "--check" in argv
    use_stdio = "--stdio" in argv

    # stderr，不是 stdout。stdio 传输下 stdout 是 JSON-RPC 的数据通道，
    # 往里写任何一个字节都会让客户端解析失败——而且失败得很难看懂：
    # 客户端只会报"协议错误"，不会告诉你是谁多打印了一行。
    # logging 默认就写 stderr，这里显式写出来是为了让后来改动的人看见这条约束。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        config = Config.load()
    except ConfigError as exc:
        logger.error("配置错误：%s", exc)
        return 2

    if use_stdio:
        # stdio 忽略整个 multi_user 子项（SPEC §4.4）：单机语义完全不变——
        # 公共 source 照自身 writable 可写、editor 记 agent、admins/guest_writable 无效果。
        config = dataclasses.replace(config, multi_user=None)

    logger.info("配置：%s", config.describe())
    for ws in config.sources:
        logger.info("source %s%s → %s", ws.name, "（只读）" if not ws.can_write else "", ws.dir)
        state = open_storage(ws).probe()
        if not state.available:
            # 不可用不导致启动失败（需求 §8）：掉盘时照常用缓存里的内容服务；
            # 目录不存在按删除处理。路径拼错也会落到这里，请看 /health 的 available。
            logger.warning(
                "source %s 当前不可用：%s（%s）",
                ws.name, "挂载盘掉线" if state.reason == "disk_offline" else "目录不存在", ws.dir,
            )

    if check_only:
        return run_check(config)

    holder = IndexHolder(config)
    logger.info("正在加载索引（有缓存则立即服务、后台增量校验；无缓存则全量构建后才接受请求）…")
    snapshot = holder.start()
    logger.info(
        "索引就绪：%d 文档 / %d chunk / %.2fs",
        snapshot.doc_count, snapshot.chunk_count, snapshot.build_seconds,
    )
    if snapshot.doc_count == 0:
        logger.warning(
            "索引为空（记忆库还没有内容，或 source 的 dir 指错了）。配置文件：%s",
            config.config_file,
        )

    holder.start_polling()

    server = create_server(config, holder)

    if use_stdio:
        # stdio 下没有 /health 与 /search——它们是 Starlette 自定义路由，
        # 只存在于 HTTP 应用里。索引状态改由每次 search 响应的 index 字段送达
        # （这正是 ADR-0006 砍掉 kb_status 时的设计）。
        #
        # 轮询在 stdio 下尤其重要：每个客户端是一个独立进程、各自持有一份索引，
        # 别的会话（或你在编辑器里）新增的记忆，本进程只能靠轮询发现。
        # 因此 stdio 场景建议把 config.json 的 poll_interval 调到 60 左右。
        logger.info("以 stdio 传输启动；轮询间隔 %d 秒", config.poll_interval)
        server.run("stdio")
        return 0

    # transport_security=None 表示不启用 DNS rebinding 保护——本服务面向
    # 受信任的局域网、且免鉴权，Host 白名单不解决任何实际威胁，只会误伤。
    #
    # host 必须显式传：省略时 SDK 取默认值 "127.0.0.1"，据此判定这是本机服务，
    # 会无视 transport_security=None 的意图，自动装上 localhost-only 的 Host 白名单，
    # 导致局域网客户端在 /mcp 上收到 421 Invalid Host header
    # （而 /health 与 /search 是自定义路由、不过该中间件，因此表面一切正常，极难排查）。
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=None,
        host=config.host,
    )

    logger.warning(
        "本服务无鉴权，且不校验 Host 头，请仅部署在受信任的局域网内。"
    )
    if config.multi_user is not None:
        logger.info(
            "多人共用已启用：身份只来自 ?user= URL 参数（知道用户名即可冒充，"
            "已接受的已知风险）；本服务无鉴权，请仅部署在受信任的局域网内。"
        )

    logger.info(
        "启动 HTTP 服务：MCP 端点 http://%s:%d/mcp ；健康检查 /health ；检索 /search?q=",
        config.host, config.port,
    )
    # uvicorn 默认空闲 5 秒即关连接；客户端连接池复用这条已关闭的连接时会报 ECONNRESET
    # （经 Docker/WSL 端口转发时客户端往往收不到关闭通知）。放宽到 75 秒。
    app = UserScopeMiddleware(app, config)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info",
                timeout_keep_alive=75)
    return 0


if __name__ == "__main__":
    sys.exit(main())
