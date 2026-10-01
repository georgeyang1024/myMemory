"""REST 端点：/health、/search、POST /reindex，走真实的 ASGI 应用。

不依赖 httpx：直接按 ASGI 协议发一个请求，拿回状态码与 JSON。
"""

import asyncio
import json
import time
from pathlib import Path

import pytest

from index import IndexHolder
from server import create_server

from test_corpus import make_config


def call(app, method: str, path: str, query: str = "") -> tuple[int, dict]:
    async def run():
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": query.encode(), "root_path": "",
            "headers": [(b"host", b"127.0.0.1:7099")],
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
        return status, json.loads(body)

    return asyncio.run(run())


@pytest.fixture
def setup(tmp_path: Path):
    for name in ("memory", "org"):
        (tmp_path / name).mkdir()
    (tmp_path / "memory" / "机制.md").write_text("SecProto 机制流程。", encoding="utf-8")
    (tmp_path / "org" / "制度.md").write_text("SecProto 合规制度。", encoding="utf-8")
    config = make_config(tmp_path, sources=[
        {"name": "memory", "dir": str(tmp_path / "memory")},
        {"name": "org", "dir": str(tmp_path / "org"), "writable": False},
    ])
    holder = IndexHolder(config)
    holder.build_now()
    app = create_server(config, holder).streamable_http_app(
        streamable_http_path="/mcp", transport_security=None, host=config.host)
    return config, holder, app


def test_health_lists_sources_and_config_file(setup):
    config, _, app = setup
    status, body = call(app, "GET", "/health")
    assert status == 200
    assert body["config_file"] == str(config.config_file)
    assert [w["name"] for w in body["sources"]] == ["memory", "org"]
    assert "git" not in body


def test_search_endpoint_accepts_source(setup):
    _, _, app = setup
    status, body = call(app, "GET", "/search", "q=SecProto&source=org")
    assert status == 200
    assert [r["source"] for r in body["results"]] == ["org"]

    status, body = call(app, "GET", "/search", "q=SecProto")
    assert {r["source"] for r in body["results"]} == {"memory", "org"}


def test_search_endpoint_unknown_source_is_400(setup):
    _, _, app = setup
    status, body = call(app, "GET", "/search", "q=SecProto&source=nope")
    assert status == 400 and body["error"]


def test_reindex_rebuilds_and_picks_up_external_edits(setup):
    config, holder, app = setup
    first = holder.snapshot
    (config.source("memory").dir / "编辑器里新增.md").write_text("新内容", encoding="utf-8")

    status, body = call(app, "POST", "/reindex")
    assert status == 200 and body["index_refresh"] in {"started", "merged"}
    assert body["mode"] == "incremental"

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and (holder.snapshot is first or holder.rebuilding):
        time.sleep(0.05)
    assert holder.snapshot.doc_count == first.doc_count + 1


def test_reindex_is_post_only(setup):
    _, _, app = setup
    async def run():
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "GET", "scheme": "http", "path": "/reindex", "raw_path": b"/reindex",
                 "query_string": b"", "root_path": "", "headers": [(b"host", b"x")],
                 "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 7099)}
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        return next(m["status"] for m in messages if m["type"] == "http.response.start")

    assert asyncio.run(run()) == 405


def test_reindex_full_flag(setup):
    _, holder, app = setup
    status, body = call(app, "POST", "/reindex", "full=1")
    assert status == 200 and body["mode"] == "full"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and holder.rebuilding:
        time.sleep(0.05)


def test_recent_endpoint_returns_json(setup):
    _, _, app = setup
    status, body = call(app, "GET", "/recent")
    assert status == 200 and body["returned"] == 2
    assert {r["source"] for r in body["results"]} == {"memory", "org"}
    assert {r["source"]: r["writable"] for r in body["results"]} == {"memory": True, "org": False}
    assert set(body["results"][0]) == {"source", "path", "writable", "updated_at", "size", "editor"}

    status, body = call(app, "GET", "/recent", "limit=1&source=org")
    assert status == 200 and [r["path"] for r in body["results"]] == ["制度.md"]

    status, body = call(app, "GET", "/recent", "source=nope")
    assert status == 400 and body["error"]


def test_health_reports_verifying_and_availability(setup):
    _, _, app = setup
    status, body = call(app, "GET", "/health")
    assert body["verifying"] is False
    assert all(w["available"] for w in body["sources"])
