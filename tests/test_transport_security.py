"""传输层安全：Host 校验必须反映真实绑定地址，而不是 SDK 的默认值。

本服务不做 Host 白名单——它面向受信任的局域网且免鉴权，白名单不解决任何
实际威胁，只会误伤。但"不做"不是省略参数就能表达的：

`streamable_http_app()` 的 host 参数默认是 "127.0.0.1"，省略它时 SDK 会判定
这是本机服务，无视 transport_security=None 的意图，自动装上 localhost-only
的 Host 白名单。局域网客户端于是在 /mcp 上收到 421 Invalid Host header，
而 /health 与 /search 是自定义路由、不过该中间件，表面上一切正常——
这个故障真实发生过，排查代价很高，故在此锁死。
"""

from pathlib import Path

import pytest

from test_corpus import make_config


@pytest.fixture
def corpus(tmp_path: Path):
    raw = tmp_path / "memory"
    raw.mkdir(parents=True)
    (raw / "note.md").write_text("SecProto 机制说明。", encoding="utf-8")
    return tmp_path


@pytest.fixture
def captured(monkeypatch):
    """拦下 streamable_http_app 的实参，不真正建应用。"""
    calls: list[dict] = []

    from mcp.server.mcpserver import MCPServer

    def fake(self, **kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(MCPServer, "streamable_http_app", fake)
    return calls


def _build_app(config, captured):
    """复刻 main() 里那一段真实的组装代码。"""
    from index import IndexHolder
    from server import create_server

    holder = IndexHolder(config)
    holder.build_now()
    server = create_server(config, holder)
    server.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=None,
        host=config.host,
    )
    return captured[-1]


def test_bind_all_interfaces_passes_real_host(corpus, captured):
    """绑 0.0.0.0 时必须把 0.0.0.0 传给 SDK。

    传 "127.0.0.1"（或省略该参数）会触发 SDK 的自动保护，
    导致所有非 localhost 的 Host 在 /mcp 上收到 421。
    """
    config = make_config(corpus, MEMORY_HOST="0.0.0.0")
    kwargs = _build_app(config, captured)

    assert kwargs["host"] == "0.0.0.0", "省略 host 会让 SDK 悄悄启用 localhost-only 白名单"
    assert kwargs["host"] not in ("127.0.0.1", "localhost", "::1")


def test_transport_security_always_none(corpus, captured):
    """本服务不构造 TransportSecuritySettings。

    传入一个 allowed_hosts 为空的配置对象会拒绝所有请求（全站 400），
    与"关闭保护"恰好相反——所以这里必须是 None，不能是空配置对象。
    """
    config = make_config(corpus, MEMORY_HOST="0.0.0.0")
    kwargs = _build_app(config, captured)

    assert kwargs["transport_security"] is None


def test_localhost_bind_still_reports_localhost(corpus, captured):
    """显式绑本机时传 127.0.0.1 —— 此时 SDK 自动启用保护是正确行为。"""
    config = make_config(corpus, MEMORY_HOST="127.0.0.1")
    kwargs = _build_app(config, captured)

    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["transport_security"] is None


def test_config_has_no_allowed_hosts_knob(corpus):
    """allowed_hosts 开关已移除：写进 config.json 会被当作未知字段拒绝，而不是静默生效或忽略。"""
    from config import ConfigError

    with pytest.raises(ConfigError, match="未知字段"):
        make_config(corpus, allowed_hosts=["example.com:*"])
    config = make_config(corpus)
    assert not hasattr(config, "allowed_hosts")
    assert "dns_rebinding_protection" not in config.describe()
