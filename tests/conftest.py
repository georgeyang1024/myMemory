import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_PROXY_VARS = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
               "ALL_PROXY", "all_proxy")


def pytest_configure(config):
    """测试临时目录放到 ~/.myMemory/pytest-tmp：不落在代码目录，也避开 Windows
    系统临时目录（E:\\Windows\\Temp）对 pytest-of-<用户> 目录的权限问题。"""
    if config.option.basetemp is None:
        config.option.basetemp = str(Path.home() / ".myMemory" / "pytest-tmp")
    # 测试只访问本机回环地址，但代理环境变量照常被 MCP 官方客户端（httpx）拾取：
    # all_proxy=socks://… 这类取值在构建 Proxy 对象时直接报 ValueError（no_proxy
    # 救不了，Proxy 在按目标地址过滤之前就已构造）——历史上这正是 MCP 客户端
    # 一启动就失效的原因。测试进程一律摘掉代理变量，保证套件在任意机器上封闭运行。
    for name in _PROXY_VARS:
        os.environ.pop(name, None)
