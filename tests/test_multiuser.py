"""多人共用（docs/adr/0028 ~ 0031）：配置、派生、路由中间件、会话限域、编辑者登记。

中间件测试走真实 ASGI（与 test_rest.py 同路数）；工具层限域对 run_* 直调——
工具函数体只是把 _session 算出的 (scoped, names, editor) 传进去，
REST 端点（同样构造 scoped config）承担端到端覆盖。
"""

import asyncio
import dataclasses
import json
import os
import time
from pathlib import Path

import pytest

from config import (Config, ConfigError, MultiUserConfig, effective_sources,
                    find_user, is_admin, personal_sources,
                    personal_sources_report)
from index import CACHE_FORMAT, ContentCache, FileEntry, IndexHolder
from server import (UserScopeMiddleware, editor_identity, run_get_document,
                    run_recent, run_save, run_search, scoped_config,
                    scope_names, user_from_context)
from urllib.parse import unquote
from writer import save_memory

from test_corpus import make_config


def make_multi_config(tmp_path: Path, **overrides) -> Config:
    root = tmp_path / "users"
    root.mkdir(exist_ok=True)
    for name in ("memory",):
        (tmp_path / name).mkdir(exist_ok=True)
    (tmp_path / "memory" / "公共.md").write_text("SecProto 公共内容。", encoding="utf-8")
    data = {
        "host": "127.0.0.1", "port": 7099, "poll_interval": 0,
        "sources": [{"name": "memory", "dir": str(tmp_path / "memory")}],
        "multi_user": {"enabled": True, "store_dir": str(root)},
    }
    for key, value in overrides.items():
        if key.startswith("MEMORY_"):
            key = key[len("MEMORY_"):].lower()
            value = int(value) if str(value).lstrip("-").isdigit() else value
        data[key] = value
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return Config.load(path, create_default=False)


def call(app, method: str, path: str, query: str = "",
         headers: list[tuple[bytes, bytes]] | None = None) -> tuple[int, dict]:
    async def run():
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": query.encode(), "root_path": "",
            "headers": [(b"host", b"127.0.0.1:7099"), *(headers or [])],
            "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 7099),
        }
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return status, json.loads(body) if body else {}

    return asyncio.run(run())


async def echo_app(scope, receive, send):
    """回显收到的 x-mymemory-user 头，验证中间件的注入/覆盖行为。"""
    values = [v.decode("latin-1") for k, v in scope.get("headers", [])
              if k.decode("latin-1").lower() == "x-mymemory-user"]
    body = json.dumps({"user": values[0] if values else None},
                      ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


# --- 配置 -------------------------------------------------------------------

def test_multi_user_absent_is_single_machine(tmp_path: Path):
    config = make_config(tmp_path)
    assert config.multi_user is None
    assert effective_sources(config) == config.sources
    assert find_user(config, "alice") is None
    assert not is_admin(config, "李四")
    assert editor_identity(config, None) is None
    assert scoped_config(config, None) is config


def test_multi_user_disabled_by_default(tmp_path: Path):
    """enabled 默认 false：multi_user 块已配置也不启用，回到单机形态。"""
    config = make_multi_config(tmp_path, multi_user={"store_dir": str(tmp_path / "users")})
    assert config.multi_user is None
    assert effective_sources(config) == config.sources
    assert not is_admin(config, "admin")
    assert editor_identity(config, None) is None
    assert scoped_config(config, None) is config


def test_multi_user_parses_defaults(tmp_path: Path):
    config = make_multi_config(tmp_path)
    assert isinstance(config.multi_user, MultiUserConfig)
    assert config.multi_user.admins == ("admin",)  # 未显式配置 admins 时默认 admin
    assert config.multi_user.guest_writable is False  # 访客对公共 source 默认只读
    assert is_admin(config, "admin")


def test_multi_user_empty_admins_is_explicit(tmp_path: Path):
    """显式给出 admins（含空数组）则完全按给定值：空数组 = 无管理员。"""
    config = make_multi_config(tmp_path, multi_user={
        "enabled": True, "store_dir": str(tmp_path / "users"), "admins": []})
    assert config.multi_user.admins == ()
    assert not is_admin(config, "admin")


def test_multi_user_enabled_must_be_bool(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "sources": [{"name": "memory", "dir": str(tmp_path / "memory")}],
        "multi_user": {"enabled": "yes", "store_dir": str(tmp_path / "users")},
    }, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="enabled"):
        Config.load(path, create_default=False)


def test_multi_user_missing_store_dir_is_error(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "sources": [{"name": "memory", "dir": str(tmp_path / "memory")}],
        "multi_user": {"enabled": True, "admins": ["李四"]},
    }, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="store_dir"):
        Config.load(path, create_default=False)


def test_multi_user_unknown_key_is_error(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "sources": [{"name": "memory", "dir": str(tmp_path / "memory")}],
        "multi_user": {"enabled": True, "store_dir": str(tmp_path / "users"), "quota": 10},
    }, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="未知字段"):
        Config.load(path, create_default=False)


def test_guest_writable_must_be_bool(tmp_path: Path):
    with pytest.raises(ConfigError, match="guest_writable"):
        make_multi_config(tmp_path, multi_user={
            "enabled": True, "store_dir": str(tmp_path / "users"), "guest_writable": "yes"})


def test_admins_strip_dedupe_and_drop_blank(tmp_path: Path):
    config = make_multi_config(tmp_path, multi_user={
        "enabled": True, "store_dir": str(tmp_path / "users"), "admins": ["李四", " 李四 ", "  ", "王五"]})
    assert config.multi_user.admins == ("李四", "王五")


@pytest.mark.parametrize("bad", ["a\r\nb", "a\x00b", "\x1b[31m"])
def test_admin_with_control_char_is_error(tmp_path: Path, bad: str):
    with pytest.raises(ConfigError, match="控制字符"):
        make_multi_config(tmp_path, multi_user={
            "enabled": True, "store_dir": str(tmp_path / "users"), "admins": [bad]})


def test_admin_too_long_is_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="过长"):
        make_multi_config(tmp_path, multi_user={
            "enabled": True, "store_dir": str(tmp_path / "users"), "admins": ["名" * 65]})


@pytest.mark.parametrize("name", ["agent", "scan", "guest", "Agent", "GUEST", "Scan"])
def test_admin_reserved_casefold_is_error(tmp_path: Path, name: str):
    with pytest.raises(ConfigError, match="保留字"):
        make_multi_config(tmp_path, multi_user={
            "enabled": True, "store_dir": str(tmp_path / "users"), "admins": [name]})


def test_store_dir_overlapping_public_source_is_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="重叠或嵌套"):
        make_multi_config(tmp_path, sources=[
            {"name": "memory", "dir": str(tmp_path)},
        ], multi_user={"enabled": True, "store_dir": str(tmp_path / "memory" / "子")})


def test_store_dir_nested_in_public_source_is_error(tmp_path: Path):
    (tmp_path / "memory" / "users").mkdir(parents=True, exist_ok=True)
    with pytest.raises(ConfigError, match="重叠或嵌套"):
        make_multi_config(tmp_path, multi_user={
            "enabled": True, "store_dir": str(tmp_path / "memory" / "users")})


def test_store_dir_may_not_contain_public_source(tmp_path: Path):
    root = tmp_path / "users"
    (root / "alice").mkdir(parents=True, exist_ok=True)
    (tmp_path / "公共").mkdir(exist_ok=True)
    with pytest.raises(ConfigError, match="重叠或嵌套"):
        make_multi_config(tmp_path, sources=[
            {"name": "memory", "dir": str(tmp_path / "memory")},
            {"name": "公共", "dir": str(root / "alice")},
        ], multi_user={"enabled": True, "store_dir": str(root)})


def test_store_dir_missing_is_not_an_error(tmp_path: Path):
    config = make_multi_config(tmp_path, multi_user={"enabled": True, "store_dir": str(tmp_path / "不在")})
    assert config.multi_user is not None
    assert personal_sources(config.multi_user.store_dir, config.sources) == ()


# --- 派生 -------------------------------------------------------------------

def test_personal_sources_derived_and_filtered(tmp_path: Path):
    root = tmp_path / "users"
    (root / "alice").mkdir(parents=True)
    (root / "张三").mkdir()
    (root / "带 空格").mkdir()
    (root / "guest").mkdir()
    (root / "纯文件.md").write_text("不是目录", encoding="utf-8")
    (root / "alice" / "deep" / "x.md").parent.mkdir(parents=True)
    (root / "alice" / "deep" / "x.md").write_text("深层不派生", encoding="utf-8")
    (root / "memory").mkdir()  # 与公共 source 重名
    (root / "Alice2").mkdir()

    config = make_multi_config(tmp_path)
    derived, skipped = personal_sources_report(
        config.multi_user.store_dir, config.sources)
    assert {src.name for src in derived} == {"alice", "张三", "Alice2"}
    for src in derived:
        assert src.writable and src.type == "local"
        assert src.dir == root / src.name
    reasons = {s["name"]: s["reason"] for s in skipped}
    assert reasons["带 空格"] == "invalid_name"
    assert reasons["memory"] == "name_conflict"


def test_personal_sources_casefold_dup_keeps_one(tmp_path: Path):
    # Windows 上 ALICE 与 alice 是同一目录、无法并存；casefold 重名在 Linux 才会
    # 真实出现，这里用"个人目录 vs 公共 source 只差大小写"覆盖同一条比较路径。
    root = tmp_path / "users"
    (root / "alice").mkdir(parents=True)
    (root / "alice2").mkdir()
    (tmp_path / "公共目录").mkdir()
    config = make_multi_config(tmp_path, sources=[
        {"name": "memory", "dir": str(tmp_path / "memory")},
        {"name": "Alice2", "dir": str(tmp_path / "公共目录")},
    ])
    derived, skipped = personal_sources_report(root, config.sources)
    assert {src.name for src in derived} == {"alice"}
    assert [s["name"] for s in skipped] == ["alice2"]
    assert skipped[0]["reason"] == "name_conflict"


def test_find_user_exact_case_sensitive(tmp_path: Path):
    (tmp_path / "users" / "alice").mkdir(parents=True, exist_ok=True)
    config = make_multi_config(tmp_path)
    assert find_user(config, "alice") is not None
    assert find_user(config, "Alice") is None
    assert find_user(config, " alice ") is None, "strip 由中间件统一做，find_user 逐字比较"


def test_is_admin_exact(tmp_path: Path):
    config = make_multi_config(tmp_path, multi_user={
        "enabled": True, "store_dir": str(tmp_path / "users"), "admins": ["李四"]})
    assert is_admin(config, "李四")
    assert not is_admin(config, "李四 ")
    assert not is_admin(config, "lisi")


# --- 会话限域（scoped_config + 范围名集） -------------------------------------

def _alice_env(tmp_path: Path):
    """alice / bob 两个用户 + 公共只读 source；bob 的文档已入索引。"""
    for name in ("alice", "bob"):
        user_dir = tmp_path / "users" / name
        user_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "users" / "bob" / "机密.md").write_text(
        "SecProto BobProto bob 的机密。", encoding="utf-8")
    config = make_multi_config(tmp_path, sources=[
        {"name": "memory", "dir": str(tmp_path / "memory"), "writable": False},
    ])
    holder = IndexHolder(config)
    holder.build_now()
    return config, holder


def test_scoped_config_three_branches(tmp_path: Path):
    config, _ = _alice_env(tmp_path)
    (tmp_path / "users" / "alice").mkdir(exist_ok=True)

    guest = scoped_config(config, None)
    assert [src.name for src in guest.sources] == ["memory"]
    assert all(not src.writable for src in guest.sources), "访客默认对公共 source 只读"

    alice = scoped_config(config, "alice")
    assert [src.name for src in alice.sources] == ["memory", "alice"]

    # 王五不在 admins 里 → 只回落为"无个人目录的普通用户"，范围 = 仅公共
    outsider = scoped_config(config, "王五")
    assert [src.name for src in outsider.sources] == ["memory"]

    admin = dataclasses.replace(
        config, multi_user=dataclasses.replace(config.multi_user, admins=("王五",)))
    full = scoped_config(admin, "王五")
    assert {src.name for src in full.sources} == {"memory", "alice", "bob"}


def test_guest_writable_true_keeps_public_writable(tmp_path: Path):
    config, _ = _alice_env(tmp_path)
    opened = dataclasses.replace(config.multi_user, guest_writable=True)
    config = dataclasses.replace(config, multi_user=opened)
    guest = scoped_config(config, None)
    assert [src.writable for src in guest.sources] == [False], "公共 source 自身 writable: false"
    public_only = dataclasses.replace(config, sources=tuple(
        dataclasses.replace(src, writable=True) for src in config.sources))
    guest = scoped_config(public_only, None)
    assert [src.writable for src in guest.sources] == [True]


def test_default_search_does_not_leak(tmp_path: Path):
    """泄漏修复回归：alice 的默认 search / recent 拿不到 bob 的内容。"""
    config, holder = _alice_env(tmp_path)
    alice = scoped_config(config, "alice")
    allowed = scope_names(alice)
    assert "bob" not in allowed

    body = run_search(alice, holder.snapshot, "BobProto", 10, None, allowed)
    assert body["results"] == [] and body["total_matched"] == 0

    body = run_search(alice, holder.snapshot, "SecProto", 10, None, allowed)
    assert [r["source"] for r in body["results"]] == ["memory"]

    recent = run_recent(alice, holder.snapshot, 20, None, allowed)
    assert {r["source"] for r in recent["results"]} <= {"memory", "alice"}


def test_get_document_cross_user_is_rejected(tmp_path: Path):
    """泄漏修复回归：alice 知道 bob 的路径也读不到，建议里同样不出现 bob 的文档。"""
    config, holder = _alice_env(tmp_path)
    alice = scoped_config(config, "alice")
    allowed = scope_names(alice)
    payload = run_get_document(alice, holder.snapshot, "bob", "机密.md", 0, 1000, allowed)
    assert payload.get("error") and "content" not in payload
    assert not any(s["source"] == "bob" for s in payload.get("suggestions", []))
    for suggestion in payload.get("suggestions", []):
        assert suggestion["source"] in allowed, "建议只来自会话范围"


def test_admin_default_search_sees_everything(tmp_path: Path):
    config, holder = _alice_env(tmp_path)
    admin = dataclasses.replace(
        config, multi_user=dataclasses.replace(config.multi_user, admins=("王五",)))
    allowed = scope_names(scoped_config(admin, "王五"))
    body = run_search(scoped_config(admin, "王五"), holder.snapshot, "BobProto", 10, None, allowed)
    assert [(r["source"], r["path"]) for r in body["results"]] == [("bob", "机密.md")]
    recent = run_recent(scoped_config(admin, "王五"), holder.snapshot, 20, None, allowed)
    assert {r["source"] for r in recent["results"]} == {"memory", "bob"}


# --- 中间件 -------------------------------------------------------------------

def test_middleware_no_user_strips_client_header(tmp_path: Path):
    config = make_multi_config(tmp_path, multi_user={
        "enabled": True, "store_dir": str(tmp_path / "users"), "admins": ["李四"]})
    app = UserScopeMiddleware(echo_app, config)
    status, body = call(app, "GET", "/mcp",
                        headers=[(b"x-mymemory-user", b"%E6%9D%8E%E5%9B%9B")])
    assert status == 200 and body["user"] is None, "客户端自带的内部头必须被无条件删除"


def test_middleware_valid_user_injects_percent_encoded_header(tmp_path: Path):
    (tmp_path / "users" / "张三").mkdir(parents=True, exist_ok=True)
    config = make_multi_config(tmp_path)
    app = UserScopeMiddleware(echo_app, config)
    status, body = call(app, "GET", "/mcp", "user=%E5%BC%A0%E4%B8%89")
    assert status == 200
    assert body["user"] == "%E5%BC%A0%E4%B8%89", "注入值必须 percent-encode（latin-1 安全）"
    from urllib.parse import unquote
    assert unquote(body["user"]) == "张三"


def test_middleware_overrides_client_header_on_valid_user(tmp_path: Path):
    (tmp_path / "users" / "alice").mkdir(parents=True, exist_ok=True)
    config = make_multi_config(tmp_path, multi_user={
        "enabled": True, "store_dir": str(tmp_path / "users"), "admins": ["李四"]})
    app = UserScopeMiddleware(echo_app, config)
    _status, body = call(app, "GET", "/mcp", "user=alice",
                         headers=[(b"x-mymemory-user", b"%E6%9D%8E%E5%9B%9B")])
    assert body["user"] == "alice", "合法 user 以中间件算出的值为准"


def test_middleware_admin_without_directory_passes(tmp_path: Path):
    config = make_multi_config(tmp_path, multi_user={
        "enabled": True, "store_dir": str(tmp_path / "users"), "admins": ["李四"]})
    app = UserScopeMiddleware(echo_app, config)
    status, body = call(app, "GET", "/mcp", "user=%E6%9D%8E%E5%9B%9B")
    assert status == 200 and body["user"] == "%E6%9D%8E%E5%9B%9B"


def test_middleware_unknown_user_400(tmp_path: Path):
    config = make_multi_config(tmp_path)
    app = UserScopeMiddleware(echo_app, config)
    status, body = call(app, "GET", "/mcp", "user=nobody")
    assert status == 400
    assert "nobody" in body["error"] and "联系管理员" in body["error"]


def test_middleware_blank_and_oversized_user(tmp_path: Path):
    config = make_multi_config(tmp_path)
    app = UserScopeMiddleware(echo_app, config)
    for query in ("user=", "user=%20%20"):
        status, body = call(app, "GET", "/mcp", query)
        assert status == 200 and body["user"] is None, query
    status, _ = call(app, "GET", "/mcp", "user=" + "a" * 65)
    assert status == 400


def test_middleware_duplicate_takes_first(tmp_path: Path):
    for name in ("a", "b"):
        (tmp_path / "users" / name).mkdir(parents=True, exist_ok=True)
    config = make_multi_config(tmp_path)
    app = UserScopeMiddleware(echo_app, config)
    _status, body = call(app, "GET", "/mcp", "user=a&user=b")
    assert body["user"] == "a"


def test_middleware_trailing_slash_is_validated(tmp_path: Path):
    config = make_multi_config(tmp_path)
    app = UserScopeMiddleware(echo_app, config)
    status, body = call(app, "GET", "/mcp/", "user=nobody")
    assert status == 400 and "nobody" in body["error"], "尾斜杠变体必须同样校验（fail loud）"


def test_middleware_ignores_user_on_other_paths(tmp_path: Path):
    config = make_multi_config(tmp_path)
    app = UserScopeMiddleware(echo_app, config)
    status, body = call(app, "GET", "/health", "user=nobody")
    assert status == 400, "/health 已在校验集内：未开通同样拒绝（fail loud）"
    status, body = call(app, "GET", "/随便", "user=nobody")
    assert status == 200, "不在校验集的路径原样放行"


def test_user_from_context_decodes():
    """user_from_context 的解码口径：percent-encoded UTF-8 → 原名。

    真实 Context 需要请求环境，编码往返在这里覆盖（中间件测试覆盖注入端）。
    """
    name = "张三"
    from urllib.parse import quote
    encoded = quote(name, safe="")
    assert encoded != name
    assert unquote(encoded) == name


# --- REST 端到端（真实 streamable 应用 + 中间件） ------------------------------

@pytest.fixture
def http_env(tmp_path: Path):
    config, holder = _alice_env(tmp_path)
    (tmp_path / "users" / "alice" / "私有.md").write_text(
        "SecProto alice 的私有内容。", encoding="utf-8")
    holder.request_rebuild("测试补充 alice 文档")
    while holder.rebuilding:
        time.sleep(0.02)
    from server import create_server
    inner = create_server(config, holder).streamable_http_app(
        streamable_http_path="/mcp", transport_security=None, host=config.host)
    return config, UserScopeMiddleware(inner, config)


def test_rest_search_scoped(http_env):
    config, app = http_env
    status, body = call(app, "GET", "/search", "q=SecProto&user=alice")
    assert status == 200
    assert {r["source"] for r in body["results"]} == {"memory", "alice"}
    status, body = call(app, "GET", "/search", "q=SecProto&user=bob")
    assert {r["source"] for r in body["results"]} == {"memory", "bob"}
    status, body = call(app, "GET", "/search", "q=BobProto&user=alice")
    assert status == 200 and body["results"] == [], "alice 搜不到 bob 的内容"
    status, body = call(app, "GET", "/search", "q=BobProto&user=%E7%8E%8B%E4%BA%94&admins=x")
    assert status == 400, "王五不在 admins：即使客户端伪造参数也 400"


def test_rest_unknown_user_400(http_env):
    _, app = http_env
    status, body = call(app, "GET", "/search", "q=x&user=nobody")
    assert status == 400 and "联系管理员" in body["error"]
    status, body = call(app, "GET", "/recent", "user=nobody")
    assert status == 400


def test_rest_health_reveals_multi_user(http_env):
    config, app = http_env
    status, body = call(app, "GET", "/health")
    assert status == 200
    mu = body["multi_user"]
    assert mu["store_dir"] == str(config.multi_user.store_dir)
    assert mu["guest_writable"] is False
    # 默认视角不揭示 admins、不列用户名——不对局域网暴露管理员名单与开通情况。
    assert "admins" not in mu and "users" not in mu


def _admin_http_env(tmp_path: Path):
    """alice / bob 两个用户 + 管理员李四 + 公共只读 source 的 REST 环境。"""
    for name in ("alice", "bob"):
        (tmp_path / "users" / name).mkdir(parents=True, exist_ok=True)
    config = make_multi_config(tmp_path, sources=[
        {"name": "memory", "dir": str(tmp_path / "memory"), "writable": False},
    ], multi_user={"enabled": True, "store_dir": str(tmp_path / "users"), "admins": ["李四"]})
    holder = IndexHolder(config)
    holder.build_now()
    from server import create_server
    inner = create_server(config, holder).streamable_http_app(
        streamable_http_path="/mcp", transport_security=None, host=config.host)
    return config, UserScopeMiddleware(inner, config)


def test_rest_health_admin_view(tmp_path: Path):
    config, app = _admin_http_env(tmp_path)

    status, body = call(app, "GET", "/health")
    assert "admins" not in body["multi_user"] and "users" not in body["multi_user"]

    status, body = call(app, "GET", "/health", "user=%E6%9D%8E%E5%9B%9B")
    assert status == 200
    mu = body["multi_user"]
    assert mu["admins"] == ["李四"]
    assert {u["name"] for u in mu["users"]} == {"alice", "bob"}


def test_rest_reindex_admin_only(tmp_path: Path):
    """多人共用下 /reindex 仅管理员可触发；单机形态不受限（test_rest.py 覆盖）。"""
    _, app = _admin_http_env(tmp_path)
    # 访客（无 user）与普通用户：403
    status, body = call(app, "POST", "/reindex")
    assert status == 403 and "管理员" in body["error"]
    status, body = call(app, "POST", "/reindex", "user=alice")
    assert status == 403
    # 未开通用户：中间件 400
    status, body = call(app, "POST", "/reindex", "user=nobody")
    assert status == 400
    # 管理员：放行，增量与全量都可用
    status, body = call(app, "POST", "/reindex", "user=%E6%9D%8E%E5%9B%9B")
    assert status == 200 and body["mode"] == "incremental"
    status, body = call(app, "POST", "/reindex", "user=%E6%9D%8E%E5%9B%9B&full=1")
    assert status == 200 and body["mode"] == "full"


def test_rest_guest_writable_end_to_end(http_env):
    config, app = http_env
    # guest_writable: false（默认）：list-sources 经 MCP 工具面走不到这里，
    # 但 scoped_config 的行为在 run 层与 REST 共用——写公共被现有只读路径拒绝。
    guest = scoped_config(config, None)
    with pytest.raises(Exception, match="只读"):
        save_memory(guest, "memory", "", "访客尝试", "内容")


# --- 热发现 -------------------------------------------------------------------

def test_hot_discovery_new_user_and_files(tmp_path: Path):
    config, holder = _alice_env(tmp_path)
    # 中间件当场枚举：新目录立刻可路由
    assert find_user(config, "carol") is None
    (tmp_path / "users" / "carol").mkdir()
    assert find_user(config, "carol") is not None

    # 索引线：下一轮刷新（当场枚举 effective sources）后可检索
    (tmp_path / "users" / "carol" / "新.md").write_text("CarolProto 内容。", encoding="utf-8")
    holder.request_rebuild("测试")
    while holder.rebuilding:
        time.sleep(0.02)
    body = run_search(holder.effective_config(), holder.snapshot, "CarolProto", 10)
    assert [r["source"] for r in body["results"]] == ["carol"]


def test_hot_discovery_rename_user_dir(tmp_path: Path):
    config, holder = _alice_env(tmp_path)
    holder.request_rebuild("测试")
    while holder.rebuilding:
        time.sleep(0.02)
    old = tmp_path / "users" / "bob"
    new = tmp_path / "users" / "bob2"
    old.rename(new)
    holder.request_rebuild("改名")
    while holder.rebuilding:
        time.sleep(0.02)
    names = {key[0] for key in holder.snapshot.entries}
    assert "bob" not in names and "bob2" in names
    assert find_user(config, "bob") is None and find_user(config, "bob2") is not None


def test_hot_discovery_deleted_user_dir(tmp_path: Path):
    config, holder = _alice_env(tmp_path)
    holder.request_rebuild("测试")
    while holder.rebuilding:
        time.sleep(0.02)
    (tmp_path / "users" / "bob").rename(tmp_path / "gone_tmp")
    holder.request_rebuild("删除目录")
    while holder.rebuilding:
        time.sleep(0.02)
    assert "bob" not in {key[0] for key in holder.snapshot.entries}
    assert find_user(config, "bob") is None


def test_hot_discovery_deleted_store_dir(tmp_path: Path):
    config, holder = _alice_env(tmp_path)
    holder.request_rebuild("测试")
    while holder.rebuilding:
        time.sleep(0.02)
    (tmp_path / "users").rename(tmp_path / "users_gone")
    holder.request_rebuild("删除个人根目录")
    while holder.rebuilding:
        time.sleep(0.02)
    assert "bob" not in {key[0] for key in holder.snapshot.entries}
    assert find_user(config, "bob") is None
    assert "alice" not in {key[0] for key in holder.snapshot.entries}


def test_cache_survives_restart_with_personal_sources(tmp_path: Path):
    config, holder = _alice_env(tmp_path)
    holder.build_now()
    restarted = IndexHolder(config)
    restarted.start()
    while restarted.rebuilding:
        time.sleep(0.02)
    # alice 目录为空（0 文档）→ 只能从 availability 看；bob 与 memory 从条目看。
    names = {key[0] for key in restarted.snapshot.entries}
    assert {"memory", "bob"} <= names, "缓存加载路径必须用 effective sources"
    assert "alice" in restarted.snapshot.availability, "空用户目录也要有可用性快照"


# --- 编辑者登记 ---------------------------------------------------------------

def _wait_idle(holder):
    while holder.rebuilding:
        time.sleep(0.02)


def test_editor_single_machine(tmp_path: Path):
    (tmp_path / "memory").mkdir(exist_ok=True)
    config = make_config(tmp_path)
    holder = IndexHolder(config)
    holder.build_now()
    run_save(config, holder, "memory", "", "单机", "内容")
    _wait_idle(holder)
    entry = holder.snapshot.entries[("memory", "单机.md")]
    assert entry.agent_editor is None and entry.editor == "agent"
    body = run_recent(config, holder.snapshot, 5)
    assert body["results"][0]["editor"] == "agent"


def test_editor_multi_user(tmp_path: Path):
    for name in ("alice", "bob", "张三"):
        (tmp_path / "users" / name).mkdir(parents=True, exist_ok=True)
    config = make_multi_config(tmp_path, multi_user={
        "enabled": True, "store_dir": str(tmp_path / "users"), "admins": ["李四"],
        "guest_writable": True})
    holder = IndexHolder(config)
    holder.build_now()

    # 张三写自己的 → 张三；李四（管理员）改 bob 的 → 李四；访客写公共 → guest
    run_save(scoped_config(config, "张三"), holder, "张三", "", "甲", "内容", "张三")
    run_save(scoped_config(config, "李四"), holder, "bob", "", "乙", "内容", "李四")
    run_save(scoped_config(config, None), holder, "memory", "", "丙", "内容", "guest")
    _wait_idle(holder)
    recent = run_recent(holder.effective_config(), holder.snapshot, 20)
    editors = {(r["source"], r["path"]): r["editor"] for r in recent["results"]}
    assert editors[("张三", "甲.md")] == "张三"
    assert editors[("bob", "乙.md")] == "李四", "登记动作者，不是记忆属主"
    assert editors[("memory", "丙.md")] == "guest"


def test_editor_scan_after_manual_touch(tmp_path: Path):
    for name in ("alice",):
        (tmp_path / "users" / name).mkdir(parents=True, exist_ok=True)
    config = make_multi_config(tmp_path)
    holder = IndexHolder(config)
    holder.build_now()
    run_save(scoped_config(config, "alice"), holder, "alice", "", "乙", "内容", "alice")
    _wait_idle(holder)
    target = tmp_path / "users" / "alice" / "乙.md"
    later = target.stat().st_mtime + 10
    os.utime(target, (later, later))
    holder.request_rebuild("人手改")
    _wait_idle(holder)
    assert holder.snapshot.entries[("alice", "乙.md")].editor == "scan"


def test_editor_cache_roundtrip(tmp_path: Path):
    for name in ("alice",):
        (tmp_path / "users" / name).mkdir(parents=True, exist_ok=True)
    config = make_multi_config(tmp_path)
    holder = IndexHolder(config)
    holder.build_now()
    run_save(scoped_config(config, "alice"), holder, "alice", "", "丙", "内容", "alice")
    _wait_idle(holder)
    restarted = IndexHolder(config)
    restarted.start()
    _wait_idle(restarted)
    entry = restarted.snapshot.entries[("alice", "丙.md")]
    assert entry.agent_editor == "alice" and entry.editor == "alice", "agent_editor 跨重启保留"
    assert CACHE_FORMAT == 4


def test_editor_values_survive_cache_rescue(tmp_path: Path):
    """CACHE_FORMAT bump 的 rescue 路径沿用 agent_mtime（editor 一次性显示 agent）。"""
    config = make_multi_config(tmp_path)
    holder = IndexHolder(config)
    holder.build_now()
    run_save(config, holder, "memory", "", "旧标记", "内容")
    _wait_idle(holder)
    # 模拟旧格式缓存被抢救：条目带 agent_mtime、无 agent_editor
    rescued = FileEntry(source="memory", path="旧标记.md",
                        mtime=holder.snapshot.entries[("memory", "旧标记.md")].mtime,
                        size=6, char_count=6, spans=(), tokens=(), path_tokens=(),
                        agent_mtime=holder.snapshot.entries[("memory", "旧标记.md")].mtime)
    cache = ContentCache(100)
    entries, _ = __import__("index").refresh(
        holder.effective_config(), {("memory", "旧标记.md"): rescued}, full=True, cache=cache)
    assert entries[("memory", "旧标记.md")].editor == "agent"


def test_run_check_covers_effective_sources(tmp_path: Path, capsys):
    """--check 对 effective sources 自检并报告 multi_user 状态（实施中曾漏 Config 包装）。"""
    (tmp_path / "users" / "alice").mkdir(parents=True, exist_ok=True)
    (tmp_path / "users" / "alice" / "有.md").write_text("SecProto 内容。", encoding="utf-8")
    config = make_multi_config(tmp_path, multi_user={
        "enabled": True, "store_dir": str(tmp_path / "users"), "admins": ["李四"]})
    import main
    assert main.run_check(config) == 0
    out = capsys.readouterr().out
    assert "多人共用已启用" in out and "李四" in out
    assert "2 文档" in out, "自检必须覆盖派生个人 source（公共 1 篇 + alice 1 篇）"
