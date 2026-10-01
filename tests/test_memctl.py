"""config.py：人工管理 source 与刷新周期。

校验失败时必须**一个字节都不改**：配置文件写坏了，服务下次就起不来。
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]


def load_cli_module():
    spec = importlib.util.spec_from_file_location("config_cli", MCP_DIR / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config_cli = load_cli_module()


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    for name in ("memory", "team", "org", "other"):
        (tmp_path / name).mkdir()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "poll_interval": 600,
        "sources": [{"name": "memory", "dir": str(tmp_path / "memory"), "writable": True}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("MEMORY_CONFIG", str(config))
    return tmp_path, config


def data(config: Path) -> dict:
    return json.loads(config.read_text(encoding="utf-8"))


def names(config: Path) -> list[str]:
    return [w["name"] for w in data(config)["sources"]]


def test_add_source(env, capsys):
    root, config = env
    assert config_cli.main(["source", "add", "team", "--dir", str(root / "team"), "--writable",
                        "--desc", "团队"]) == 0
    item = data(config)["sources"][1]
    assert item == {"name": "team", "dir": os.path.abspath(root / "team"), "writable": True,
                    "description": "团队"}
    assert "重启后生效" in capsys.readouterr().out


def test_add_creates_config_from_scratch(tmp_path, monkeypatch, capsys):
    """配置不存在时 source add 从零建档；memory 走共享入口、描述默认"默认记忆源"（ADR-0025）。"""
    root = tmp_path / "mem"
    root.mkdir()
    config = tmp_path / "config.json"
    monkeypatch.setenv("MEMORY_CONFIG", str(config))
    assert config_cli.main(["source", "add", "memory", "--dir", str(root), "--writable"]) == 0
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["port"] == 7083 and saved["poll_interval"] == 600, "默认值随建档写入"
    assert saved["sources"] == [
        {"name": "memory", "dir": os.path.abspath(root), "writable": True,
         "description": "默认记忆源"}]
    assert "将创建" in capsys.readouterr().out

    # 其它名字也允许从零建档，但不带默认描述
    other = tmp_path / "mem2"
    other.mkdir()
    config2 = tmp_path / "config2.json"
    monkeypatch.setenv("MEMORY_CONFIG", str(config2))
    assert config_cli.main(["source", "add", "team", "--dir", str(other), "--readonly"]) == 0
    saved2 = json.loads(config2.read_text(encoding="utf-8"))
    assert saved2["sources"] == [{"name": "team", "dir": os.path.abspath(other),
                                  "writable": False}]


def test_missing_config_blocks_other_commands(tmp_path, monkeypatch, capsys):
    """除 source add / list 外，配置不存在时其余子命令直接报错，不隐式创建。"""
    config = tmp_path / "config.json"
    monkeypatch.setenv("MEMORY_CONFIG", str(config))
    assert config_cli.main(["source", "list"]) == 0, "list 只提示，不算错误"
    capsys.readouterr()
    for argv in (["source", "edit", "x", "--desc", "y"],
                 ["config", "set", "poll_interval", "60"]):
        assert config_cli.main(argv) == 2
        assert "配置文件不存在" in capsys.readouterr().err
    assert not config.exists(), "不得隐式创建配置文件"


def test_add_readonly_sets_writable_false(env):
    root, config = env
    assert config_cli.main(["source", "add", "org", "--dir", str(root / "org"), "--readonly"]) == 0
    assert names(config) == ["memory", "org"], "只读不再改名"
    assert data(config)["sources"][1]["writable"] is False


def test_add_requires_readonly_or_writable(env):
    root, config = env
    before = config.read_bytes()
    with pytest.raises(SystemExit):
        config_cli.main(["source", "add", "team", "--dir", str(root / "team")])  # 没指定只读/可写
    with pytest.raises(SystemExit):
        config_cli.main(["source", "add", "team", str(root / "team"), "--writable"])  # 目录必须写 --dir
    with pytest.raises(SystemExit):
        config_cli.main(["source", "add", "team", "--writable"])  # 缺 --dir
    with pytest.raises(SystemExit):
        config_cli.main(["source", "add", "team", "--dir", str(root / "team"), "--readonly", "--writable"])
    assert config.read_bytes() == before


@pytest.mark.parametrize("argv", [
    ["source", "add", "bad/name", "--dir", "{root}/team", "--writable"],
    ["source", "add", "readonly/team", "--dir", "{root}/team", "--readonly"],   # 旧的前缀写法
    ["source", "add", "memory", "--dir", "{root}/team", "--writable"],          # 重名
    ["source", "add", "team", "--dir", "{root}/nope", "--writable"],            # 目录不存在
    ["source", "add", "team", "--dir", "{root}/memory", "--writable"],          # 重叠
    ["source", "add", "team", "--dir", "{root}/memory/sub", "--writable"],      # 嵌套（目录存在）
    ["source", "edit", "nope", "--desc", "x"],
    ["source", "edit", "memory", "--name", "a/b"],
    ["source", "remove", "nope", "--yes"],
    ["config", "set", "poll_interval", "-1"],
    ["config", "set", "poll_interval", "abc"],
])
def test_invalid_operations_leave_config_untouched(env, argv, capsys):
    root, config = env
    (root / "memory" / "sub").mkdir()
    before = config.read_bytes()
    argv = [a.replace("{root}", str(root)) for a in argv]
    assert config_cli.main(argv) == 2
    assert config.read_bytes() == before, "校验失败不得修改配置文件"
    assert "错误" in capsys.readouterr().err


def test_edit_toggles_readonly_without_renaming(env):
    root, config = env
    config_cli.main(["source", "add", "team", "--dir", str(root / "team"), "--writable"])
    assert config_cli.main(["source", "edit", "team", "--readonly"]) == 0
    assert names(config) == ["memory", "team"]
    assert data(config)["sources"][1]["writable"] is False
    assert config_cli.main(["source", "edit", "team", "--writable"]) == 0
    assert data(config)["sources"][1]["writable"] is True
    with pytest.raises(SystemExit):
        config_cli.main(["source", "edit", "team", "--readonly", "--writable"])


def test_edit_rename_keeps_readonly(env):
    root, config = env
    config_cli.main(["source", "add", "team", "--dir", str(root / "team"), "--readonly"])
    assert config_cli.main(["source", "edit", "team", "--name", "team2"]) == 0
    assert data(config)["sources"][1] == {"name": "team2", "dir": os.path.abspath(root / "team"),
                                             "writable": False}


def test_source_offline_elsewhere_does_not_block_editing(env):
    """另一个 source 的目录不在（如掉盘）时，仍可修改配置——只校验本次涉及的目录。"""
    root, config = env
    cfg = data(config)
    cfg["sources"].append({"name": "gone", "dir": str(root / "gone"), "writable": False})
    config.write_text(json.dumps(cfg), encoding="utf-8")
    assert config_cli.main(["source", "add", "team", "--dir", str(root / "team"), "--writable"]) == 0


def test_edit_dir_and_desc(env):
    root, config = env
    assert config_cli.main(["source", "edit", "memory", "--dir", str(root / "other"),
                        "--desc", "新描述"]) == 0
    item = data(config)["sources"][0]
    assert item["dir"] == os.path.abspath(root / "other") and item["description"] == "新描述"
    assert config_cli.main(["source", "edit", "memory", "--desc", ""]) == 0
    assert "description" not in data(config)["sources"][0]


def test_edit_without_changes_is_rejected(env):
    assert config_cli.main(["source", "edit", "memory"]) == 2


def test_remove_only_touches_config(env):
    root, config = env
    config_cli.main(["source", "add", "team", "--dir", str(root / "team"), "--writable"])
    (root / "team" / "保留.md").write_text("文件不删", encoding="utf-8")
    assert config_cli.main(["source", "remove", "team", "--yes"]) == 0
    assert names(config) == ["memory"]
    assert (root / "team" / "保留.md").exists(), "remove 只删配置，不删磁盘文件"


def test_remove_asks_for_confirmation(env, monkeypatch):
    root, config = env
    config_cli.main(["source", "add", "team", "--dir", str(root / "team"), "--writable"])
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert config_cli.main(["source", "remove", "team"]) == 1
    assert names(config) == ["memory", "team"]
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert config_cli.main(["source", "remove", "team"]) == 0
    assert names(config) == ["memory"]


def test_cannot_remove_last_source(env):
    _, config = env
    before = config.read_bytes()
    assert config_cli.main(["source", "remove", "memory", "--yes"]) == 2
    assert config.read_bytes() == before


def test_set_poll_interval(env):
    _, config = env
    assert config_cli.main(["config", "set", "poll_interval", "60"]) == 0
    assert data(config)["poll_interval"] == 60


def test_set_allow_mcp_delete(env):
    _, config = env
    assert data(config).get("allow_mcp_delete", False) is False, "默认必须关"
    assert config_cli.main(["config", "set", "allow_mcp_delete", "true"]) == 0
    assert data(config)["allow_mcp_delete"] is True
    assert config_cli.main(["config", "set", "allow_mcp_delete", "false"]) == 0
    assert data(config)["allow_mcp_delete"] is False


@pytest.mark.parametrize("value", ["yes-please", "off", "2"])
def test_set_allow_mcp_delete_rejects_non_boolean(env, value):
    _, config = env
    before = config.read_bytes()
    assert config_cli.main(["config", "set", "allow_mcp_delete", value]) == 2
    assert config.read_bytes() == before, "校验失败不得改配置文件"


def test_only_poll_interval_is_settable(env):
    with pytest.raises(SystemExit):
        config_cli.main(["config", "set", "port", "8080"])


def test_list(env, capsys):
    root, _ = env
    config_cli.main(["source", "add", "org", "--dir", str(root / "org"), "--readonly", "--desc", "制度"])
    capsys.readouterr()
    assert config_cli.main(["source", "list"]) == 0
    out = capsys.readouterr().out
    assert "memory" in out and "读写" in out
    assert "org" in out and "只读" in out and "制度" in out


def test_restart_flag_calls_run_py(env, monkeypatch):
    root, config = env
    calls = []
    monkeypatch.setattr(config_cli.subprocess, "run",
                        lambda cmd, env: calls.append((cmd, env)) or type("R", (), {"returncode": 0})())
    assert config_cli.main(["config", "set", "poll_interval", "60", "--restart"]) == 0
    cmd, environ = calls[0]
    assert cmd == [sys.executable, str(MCP_DIR / "run.py"), "--restart"]
    assert environ["MEMORY_CONFIG"] == str(config.resolve())


def test_restart_command(env, monkeypatch):
    calls = []
    monkeypatch.setattr(config_cli.subprocess, "run",
                        lambda cmd, env: calls.append(cmd) or type("R", (), {"returncode": 0})())
    assert config_cli.main(["restart"]) == 0
    assert calls[0][-1] == "--restart"


def test_reindex_when_service_down(env, capsys):
    _, config = env
    cfg = data(config)
    cfg["port"] = 1  # 没有服务监听
    config.write_text(json.dumps(cfg), encoding="utf-8")
    assert config_cli.main(["reindex"]) == 1
    assert "服务未运行" in capsys.readouterr().out


def test_reindex_posts_to_running_service(env, monkeypatch, capsys):
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"index_refresh": "started"}'

    def fake_urlopen(request, timeout):
        seen["url"], seen["method"] = request.full_url, request.get_method()
        return FakeResponse()

    monkeypatch.setattr(config_cli.urllib.request, "urlopen", fake_urlopen)
    assert config_cli.main(["reindex"]) == 0
    assert seen == {"url": "http://127.0.0.1:7083/reindex", "method": "POST"}
    assert "增量" in capsys.readouterr().out

    assert config_cli.main(["reindex", "--full"]) == 0
    assert seen["url"] == "http://127.0.0.1:7083/reindex?full=1"
    assert "全量" in capsys.readouterr().out


def test_reindex_multi_user_carries_admin(env, monkeypatch, capsys):
    """多人共用下 reindex 需要管理员身份：CLI 默认带 admins 首个，或 --user 显式指定。"""
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"index_refresh": "started"}'

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr(config_cli.urllib.request, "urlopen", fake_urlopen)
    root, config = env
    cfg = data(config)
    cfg["multi_user"] = {"enabled": True, "store_dir": str(root / "users"),
                         "admins": ["李四", "王五"]}
    config.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    assert config_cli.main(["reindex"]) == 0
    assert seen["url"].endswith("/reindex?user=%E6%9D%8E%E5%9B%9B")  # 默认取 admins 首个

    assert config_cli.main(["reindex", "--user", "王五"]) == 0
    assert seen["url"].endswith("/reindex?user=%E7%8E%8B%E4%BA%94")

    assert config_cli.main(["reindex", "--full"]) == 0
    assert seen["url"].endswith("/reindex?full=1&user=%E6%9D%8E%E5%9B%9B")


def test_reindex_multi_user_defaults_to_admin(env, capsys, monkeypatch):
    """多人共用下未配置 admins：默认管理员 admin 兜底；开关关闭则回到单机、不带 user。"""
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"index_refresh": "started"}'

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr(config_cli.urllib.request, "urlopen", fake_urlopen)
    root, config = env
    cfg = data(config)
    cfg["multi_user"] = {"enabled": True, "store_dir": str(root / "users")}
    config.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    assert config_cli.main(["reindex"]) == 0
    assert seen["url"].endswith("/reindex?user=admin")  # 未配置 admins，默认 admin

    cfg["multi_user"] = {"enabled": False, "store_dir": str(root / "users")}
    config.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    seen.clear()
    assert config_cli.main(["reindex"]) == 0
    assert "?" not in seen["url"].split("/reindex")[-1] and seen["url"].endswith("/reindex")


def test_reindex_multi_user_empty_admins_is_error(env, capsys):
    """显式给出空 admins（无管理员，未配置默认 admin 兜底不适用）：reindex 必须显式 --user。"""
    root, config = env
    cfg = data(config)
    cfg["multi_user"] = {"enabled": True, "store_dir": str(root / "users"), "admins": []}
    config.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    assert config_cli.main(["reindex"]) == 2
    assert "管理员" in capsys.readouterr().err


def test_set_max_cached_docs(env, capsys):
    _, config = env
    assert config_cli.main(["config", "set", "max_cached_docs", "500"]) == 0
    assert data(config)["max_cached_docs"] == 500
    assert config_cli.main(["config", "set", "max_cached_docs", "0"]) == 0
    assert "不限" in capsys.readouterr().out
    before = config.read_bytes()
    assert config_cli.main(["config", "set", "max_cached_docs", "-1"]) == 2
    assert config.read_bytes() == before


# --- scoring：打分调整（ADR-0027） ------------------------------------------------

def test_edit_global_scoring(env, capsys):
    _, config = env
    assert config_cli.main(["config", "edit", "--scoring", "recency_bonus=8"]) == 0
    assert data(config)["scoring"] == {"recency_bonus": 8}, "只写改的字段，其余继续取默认值"
    assert config_cli.main(["config", "edit", "--scoring", "path_match_bonus=2.5",
                            "--scoring", "recency_window_days=45"]) == 0
    assert data(config)["scoring"] == {"recency_bonus": 8, "path_match_bonus": 2.5,
                                       "recency_window_days": 45}
    assert "全局打分" in capsys.readouterr().out


def test_reset_global_scoring(env):
    _, config = env
    config_cli.main(["config", "edit", "--scoring", "recency_bonus=8", "--scoring", "path_match_bonus=3"])
    assert config_cli.main(["config", "edit", "--reset-scoring", "recency_bonus"]) == 0
    assert data(config)["scoring"] == {"path_match_bonus": 3}
    assert config_cli.main(["config", "edit", "--reset-scoring", "all"]) == 0
    assert "scoring" not in data(config), "清空后不留空对象，全部取默认值"


def test_edit_global_strip_wikilinks_warns_about_rebuild(env, capsys):
    _, config = env
    assert config_cli.main(["config", "edit", "--scoring", "strip_wikilinks=false"]) == 0
    assert data(config)["scoring"] == {"strip_wikilinks": False}
    assert "重建" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["config", "edit"],
    ["config", "edit", "--scoring", "recency_window_days=1.5"],
    ["config", "edit", "--scoring", "recency_window_days=-1"],
    ["config", "edit", "--scoring", "recency_bonus=abc"],
    ["config", "edit", "--scoring", "path_match_bonus=-2"],
    ["config", "edit", "--scoring", "strip_wikilinks=off"],
    ["config", "edit", "--scoring", "boost_foo=1"],
    ["config", "edit", "--reset-scoring", "recency_bonus"],   # 全局没有设置这一项
])
def test_invalid_global_scoring_leaves_config_untouched(env, argv, capsys):
    _, config = env
    before = config.read_bytes()
    assert config_cli.main(argv) == 2
    assert config.read_bytes() == before
    assert "错误" in capsys.readouterr().err


def test_scoring_is_not_settable_via_config_set(env):
    with pytest.raises(SystemExit):
        config_cli.main(["config", "set", "scoring.recency_bonus", "1"])


def test_edit_source_scoring_override(env, capsys):
    root, config = env
    assert config_cli.main(["source", "edit", "memory", "--scoring", "recency_bonus=0"]) == 0
    item = data(config)["sources"][0]
    assert item["scoring"] == {"recency_bonus": 0}
    assert item["dir"] and item["writable"] is True, "其他字段不动"
    assert config_cli.main(["source", "edit", "memory", "--scoring", "path_match_bonus=8",
                            "--scoring", "strip_wikilinks=false"]) == 0
    assert data(config)["sources"][0]["scoring"] == {
        "recency_bonus": 0, "path_match_bonus": 8, "strip_wikilinks": False}
    assert "打分" in capsys.readouterr().out


def test_reset_source_scoring_field_and_all(env):
    _, config = env
    config_cli.main(["source", "edit", "memory", "--scoring", "recency_bonus=0",
                     "--scoring", "path_match_bonus=8"])
    assert config_cli.main(["source", "edit", "memory", "--reset-scoring", "recency_bonus"]) == 0
    assert data(config)["sources"][0]["scoring"] == {"path_match_bonus": 8}
    assert config_cli.main(["source", "edit", "memory", "--reset-scoring", "all"]) == 0
    assert "scoring" not in data(config)["sources"][0], "覆盖清空后不留空对象"


def test_resetting_last_override_removes_scoring_object(env):
    _, config = env
    config_cli.main(["source", "edit", "memory", "--scoring", "recency_bonus=0"])
    assert config_cli.main(["source", "edit", "memory", "--reset-scoring", "recency_bonus"]) == 0
    assert "scoring" not in data(config)["sources"][0]


@pytest.mark.parametrize("argv", [
    ["source", "edit", "memory", "--scoring", "boost_foo=1"],
    ["source", "edit", "memory", "--scoring", "recency_bonus"],
    ["source", "edit", "memory", "--scoring", "recency_bonus=-1"],
    ["source", "edit", "memory", "--scoring", "recency_window_days=abc"],
    ["source", "edit", "memory", "--scoring", "strip_wikilinks=maybe"],
    ["source", "edit", "memory", "--reset-scoring", "boost_foo"],
    ["source", "edit", "memory", "--reset-scoring", "recency_bonus"],   # 没有这项覆盖
])
def test_invalid_source_scoring_leaves_config_untouched(env, argv, capsys):
    _, config = env
    before = config.read_bytes()
    assert config_cli.main(argv) == 2
    assert config.read_bytes() == before
    assert "错误" in capsys.readouterr().err


def test_list_shows_source_scoring_overrides(env, capsys):
    config_cli.main(["source", "edit", "memory", "--scoring", "recency_bonus=0"])
    capsys.readouterr()
    assert config_cli.main(["source", "list"]) == 0
    assert "recency_bonus=0" in capsys.readouterr().out


# --- historical_penalty / historical_keywords（ADR-0033） ----------------------

def test_edit_global_historical_scoring(env):
    _, config = env
    assert config_cli.main(["config", "edit", "--scoring", "historical_penalty=3"]) == 0
    assert data(config)["scoring"] == {"historical_penalty": 3}
    assert config_cli.main(["config", "edit", "--scoring",
                            "historical_keywords=meeting,已废弃,已过期"]) == 0
    assert data(config)["scoring"] == {
        "historical_penalty": 3, "historical_keywords": ["meeting", "已废弃", "已过期"]}


def test_historical_keywords_comma_separated_drops_empty_items(env):
    _, config = env
    assert config_cli.main(["config", "edit", "--scoring",
                            "historical_keywords= meeting , 已废弃 ,,deprecated,"]) == 0
    assert data(config)["scoring"] == {
        "historical_keywords": ["meeting", "已废弃", "deprecated"]}
    assert config_cli.main(["config", "edit", "--scoring", "historical_keywords="]) == 0
    assert data(config)["scoring"] == {"historical_keywords": []}


def test_edit_source_historical_scoring_and_reset(env, capsys):
    _, config = env
    assert config_cli.main(["source", "edit", "memory", "--scoring", "historical_penalty=3",
                            "--scoring", "historical_keywords=meeting"]) == 0
    assert data(config)["sources"][0]["scoring"] == {
        "historical_penalty": 3, "historical_keywords": ["meeting"]}
    assert config_cli.main(["source", "edit", "memory",
                            "--reset-scoring", "historical_keywords"]) == 0
    assert data(config)["sources"][0]["scoring"] == {"historical_penalty": 3}
    assert config_cli.main(["source", "edit", "memory", "--reset-scoring", "all"]) == 0
    assert "scoring" not in data(config)["sources"][0]


@pytest.mark.parametrize("argv", [
    ["config", "edit", "--scoring", "historical_penalty=-3"],
    ["config", "edit", "--scoring", "historical_penalty=old"],
    ["source", "edit", "memory", "--reset-scoring", "historical_keywords"],  # 没有这项覆盖
])
def test_invalid_historical_scoring_leaves_config_untouched(env, argv, capsys):
    _, config = env
    before = config.read_bytes()
    assert config_cli.main(argv) == 2
    assert config.read_bytes() == before
    assert "错误" in capsys.readouterr().err


def test_list_shows_historical_keyword_override(env, capsys):
    config_cli.main(["source", "edit", "memory", "--scoring", "historical_penalty=3",
                     "--scoring", "historical_keywords=meeting,已废弃"])
    capsys.readouterr()
    assert config_cli.main(["source", "list"]) == 0
    output = capsys.readouterr().out
    assert "historical_penalty=3" in output
    assert "historical_keywords=meeting,已废弃" in output
