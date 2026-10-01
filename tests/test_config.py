"""配置层：config.json 的定位、默认生成与 source 校验。

名称、重名、重叠、字段类型是启动即失败（fail fast）；目录访问不到则只警告——
掉盘或目录被删除不该让整个服务起不来（需求 §8）。
"""

import json
import os
from pathlib import Path

import pytest

from config import Config, ConfigError, bootstrap_config_data, resolve_config_path


def write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def ws(name: str, directory: Path, **extra) -> dict:
    return {"name": name, "dir": str(directory), **extra}


@pytest.fixture
def dirs(tmp_path: Path):
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    return tmp_path


# --- 定位与默认生成 ---------------------------------------------------------

def test_config_path_defaults_to_home_data_dir(tmp_path, monkeypatch):
    """默认 ~/.myMemory/config.json，与运行目录无关——代码目录不落运行数据。"""
    monkeypatch.chdir(tmp_path)
    assert resolve_config_path({}) == (Path.home() / ".myMemory" / "config.json").resolve()


def test_config_path_env_override(tmp_path):
    target = tmp_path / "elsewhere" / "my.json"
    assert resolve_config_path({"MEMORY_CONFIG": str(target)}) == target.resolve()


def test_missing_config_fails_with_source_add_hint(tmp_path, monkeypatch, capsys):
    """配置不存在时不猜测记忆目录（ADR-0025）：报错并指引 source add，不写任何文件。"""
    path = tmp_path / "config.json"
    with pytest.raises(ConfigError, match="source add"):
        Config.load(path)
    assert not path.exists(), "缺配置时应报错，而不是生成指向猜测路径的配置"


def test_bootstrap_config_data_has_no_sources():
    """空配置只有默认值，不含 sources 键（空列表过不了非空校验），留给 source add 添加。"""
    data = bootstrap_config_data()
    assert "sources" not in data
    assert data["port"] == 7083 and data["poll_interval"] == 600


def test_other_memory_env_vars_are_ignored(dirs, monkeypatch):
    monkeypatch.setenv("MEMORY_PORT", "9999")
    monkeypatch.setenv("MEMORY_POLL_INTERVAL", "1")
    config = Config.load(write(dirs / "config.json", {"sources": [ws("a", dirs / "a")]}))
    assert config.port == 7083 and config.poll_interval == 600
    assert config.host == "127.0.0.1", "默认只监听本机，不应暴露到网络"


# --- source 校验 ---------------------------------------------------------

def test_multiple_sources_and_writable_field(dirs):
    config = Config.load(write(dirs / "config.json", {"sources": [
        ws("a", dirs / "a"), ws("团队", dirs / "b", writable=True),
        ws("org", dirs / "c", writable=False),
    ]}))
    by_name = {w.name: w for w in config.sources}
    assert by_name["a"].can_write, "省略 writable 即可写"
    assert by_name["团队"].can_write
    assert not by_name["org"].can_write
    assert config.writable_sources == ["a", "团队"]


def test_readonly_is_no_longer_encoded_in_the_name(dirs):
    """readonly 不再是保留字：可以正常出现在名称里，是否只读只看 writable。"""
    config = Config.load(write(dirs / "config.json", {"sources": [
        ws("readonly-notes", dirs / "a")]}))
    assert config.sources[0].can_write


@pytest.mark.parametrize("value", ["false", 0, None, "yes"])
def test_writable_must_be_boolean(dirs, value):
    with pytest.raises(ConfigError, match="writable"):
        Config.load(write(dirs / "config.json", {"sources": [
            ws("a", dirs / "a", writable=value)]}))


@pytest.mark.parametrize("name", [
    "", "a/b", "../x", "a b", "a:b", "readonly/org", "readonly/", "x" * 65,
])
def test_illegal_source_names_are_rejected(dirs, name):
    with pytest.raises(ConfigError):
        Config.load(write(dirs / "config.json", {"sources": [ws(name, dirs / "a")]}))


def test_duplicate_names_are_rejected(dirs):
    with pytest.raises(ConfigError, match="重名"):
        Config.load(write(dirs / "config.json", {"sources": [
            ws("a", dirs / "a"), ws("a", dirs / "b")]}))


def test_missing_directory_does_not_fail_startup(dirs):
    """目录不存在（被删除或掉盘）只警告，不导致启动失败——需求 §8。"""
    config = Config.load(write(dirs / "config.json", {"sources": [ws("x", dirs / "nope")]}))
    assert config.sources[0].name == "x"


def test_overlap_is_checked_even_when_directory_is_missing(dirs):
    """访问不到的目录按配置里的原样路径参与重叠判定。"""
    with pytest.raises(ConfigError, match="重叠或嵌套"):
        Config.load(write(dirs / "config.json", {"sources": [
            ws("a", dirs / "gone"), ws("b", dirs / "gone" / "sub")]}))


@pytest.mark.parametrize("second", ["a", "a/inner", "."])
def test_overlapping_or_nested_directories_are_rejected(dirs, second):
    (dirs / "a" / "inner").mkdir()
    with pytest.raises(ConfigError, match="重叠或嵌套"):
        Config.load(write(dirs / "config.json", {"sources": [
            ws("a", dirs / "a"), ws("b", dirs / second)]}))


def test_sibling_with_common_prefix_is_not_nested(dirs):
    """/x/a 与 /x/ab 不是嵌套——按路径段比较，不是按字符串前缀。"""
    (dirs / "ab").mkdir()
    config = Config.load(write(dirs / "config.json", {"sources": [
        ws("a", dirs / "a"), ws("ab", dirs / "ab")]}))
    assert len(config.sources) == 2


def test_symlink_to_existing_source_is_overlap(dirs):
    link = dirs / "link"
    try:
        os.symlink(dirs / "a", link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无法创建符号链接")
    with pytest.raises(ConfigError, match="重叠或嵌套"):
        Config.load(write(dirs / "config.json", {"sources": [
            ws("a", dirs / "a"), ws("b", link)]}))


def test_unknown_storage_type_is_rejected(dirs):
    with pytest.raises(ConfigError, match="尚未支持"):
        Config.load(write(dirs / "config.json", {"sources": [
            ws("a", dirs / "a", type="oss")]}))


def test_empty_sources_rejected(dirs):
    with pytest.raises(ConfigError):
        Config.load(write(dirs / "config.json", {"sources": []}))


def test_team_mode_allows_empty_sources(tmp_path: Path):
    """multi_user 启用时允许没有公共 source：语料 = 存储目录派生的个人 source。"""
    path = write(tmp_path / "config.json", {
        "multi_user": {"enabled": True, "store_dir": str(tmp_path / "users")}})
    config = Config.load(path, create_default=False)
    assert config.sources == ()
    assert config.multi_user is not None


def test_disabled_multi_user_still_requires_sources(tmp_path: Path):
    """开关关闭（单机形态）时没有公共 source 依旧不合法。"""
    path = write(tmp_path / "config.json", {
        "multi_user": {"enabled": False, "store_dir": str(tmp_path / "users")}})
    with pytest.raises(ConfigError, match="非空数组"):
        Config.load(path, create_default=False)


# --- 其余字段 ---------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("port", 0), ("port", "7083"), ("poll_interval", -1), ("poll_interval", True),
    ("chunk_size", 50), ("max_results", 101), ("max_cached_docs", -1), ("max_cached_docs", "1000"),
])
def test_out_of_range_values_fail_fast(dirs, field, value):
    with pytest.raises(ConfigError):
        Config.load(write(dirs / "config.json", {
            "sources": [ws("a", dirs / "a")], field: value}))


def test_unknown_top_level_key_is_rejected(dirs):
    """拼错的字段名不能被静默忽略——那等于配置没生效却没人知道。"""
    with pytest.raises(ConfigError, match="未知字段"):
        Config.load(write(dirs / "config.json", {
            "sources": [ws("a", dirs / "a")], "poll_intervall": 60}))


def test_invalid_json_is_reported(dirs):
    (dirs / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON"):
        Config.load(dirs / "config.json")


def test_configured_path_is_used_as_written_not_resolved(dirs):
    """写什么用什么：盘符映射、subst、符号链接都不解析。

    解析会把 Z:\\ 换成 UNC、把 subst 盘换成底层目录——访问与掉盘判定就不再针对配置里的那块盘。
    （重叠判定仍按真实路径，见 test_symlink_to_existing_source_is_overlap。）
    """
    link = dirs / "link"
    try:
        os.symlink(dirs / "a", link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无法创建符号链接")
    config = Config.load(write(dirs / "config.json", {"sources": [ws("x", link)]}))
    assert config.sources[0].dir == Path(os.path.abspath(link))


def test_max_cached_docs_defaults_to_1000(dirs):
    config = Config.load(write(dirs / "config.json", {"sources": [ws("a", dirs / "a")]}))
    assert config.max_cached_docs == 1000
    assert Config.load(write(dirs / "config.json", {"sources": [ws("a", dirs / "a")],
                                                    "max_cached_docs": 0})).max_cached_docs == 0


# --- allow_mcp_delete：AI 删除断路器 ----------------------------------------

def test_allow_mcp_delete_defaults_to_false(dirs):
    """默认必须红线关闭：删除不可恢复，开闸只能人工显式配置。"""
    config = Config.load(write(dirs / "config.json", {"sources": [ws("a", dirs / "a")]}))
    assert config.allow_mcp_delete is False


def test_allow_mcp_delete_reads_boolean_value(dirs, monkeypatch):
    data = {"sources": [ws("a", dirs / "a")], "allow_mcp_delete": True}
    assert Config.load(write(dirs / "config.json", data)).allow_mcp_delete is True
    data["allow_mcp_delete"] = False
    assert Config.load(write(dirs / "config.json", data)).allow_mcp_delete is False


@pytest.mark.parametrize("value", ["true", 1, "false", None])
def test_allow_mcp_delete_must_be_boolean(dirs, value):
    with pytest.raises(ConfigError, match="allow_mcp_delete"):
        Config.load(write(dirs / "config.json", {
            "sources": [ws("a", dirs / "a")], "allow_mcp_delete": value}))


def test_describe_reports_allow_mcp_delete(dirs):
    config = Config.load(write(dirs / "config.json", {"sources": [ws("a", dirs / "a")]}))
    assert config.describe()["allow_mcp_delete"] is False


# --- scoring：打分调整（全局 + source 按字段覆盖） ---------------------------

def test_scoring_defaults_when_omitted(dirs):
    from config import Scoring
    config = Config.load(write(dirs / "config.json", {"sources": [ws("a", dirs / "a")]}))
    expected = Scoring(recency_window_days=30, recency_bonus=10, path_match_bonus=5,
                       strip_wikilinks=True, historical_penalty=0, historical_keywords=())
    assert config.scoring == expected
    assert config.sources[0].scoring == expected


def test_source_scoring_overrides_single_field(dirs):
    config = Config.load(write(dirs / "config.json", {
        "scoring": {"recency_window_days": 90, "recency_bonus": 10},
        "sources": [ws("a", dirs / "a", scoring={"recency_window_days": 30}),
                    ws("b", dirs / "b")],
    }))
    a, b = config.sources
    assert (a.scoring.recency_window_days, a.scoring.recency_bonus) == (30, 10)
    assert (b.scoring.recency_window_days, b.scoring.recency_bonus) == (90, 10)


def test_source_scoring_inherits_unset_fields_from_global(dirs):
    config = Config.load(write(dirs / "config.json", {
        "scoring": {"path_match_bonus": 3, "strip_wikilinks": False},
        "sources": [ws("a", dirs / "a", scoring={"recency_bonus": 0})],
    }))
    scoring = config.sources[0].scoring
    assert (scoring.path_match_bonus, scoring.strip_wikilinks, scoring.recency_bonus) == (3, False, 0)


def test_float_bonus_is_accepted(dirs):
    config = Config.load(write(dirs / "config.json", {
        "scoring": {"path_match_bonus": 2.5}, "sources": [ws("a", dirs / "a")]}))
    assert config.scoring.path_match_bonus == 2.5


# --- historical_penalty / historical_keywords（ADR-0033） ----------------------

def test_historical_scoring_defaults_off(dirs):
    config = Config.load(write(dirs / "config.json", {"sources": [ws("a", dirs / "a")]}))
    assert config.scoring.historical_penalty == 0
    assert config.scoring.historical_keywords == (), "默认关闭：不降任何分"


def test_historical_keywords_are_normalized(dirs):
    """strip、转小写、去重保序——匹配不区分大小写在配置层完成一次。"""
    config = Config.load(write(dirs / "config.json", {
        "scoring": {"historical_keywords": [" Meeting ", "已废弃", "meeting", "DEPRECATED"]},
        "sources": [ws("a", dirs / "a")]}))
    assert config.scoring.historical_keywords == ("meeting", "已废弃", "deprecated")


def test_source_overrides_historical_scoring_per_field(dirs):
    config = Config.load(write(dirs / "config.json", {
        "scoring": {"historical_penalty": 3, "historical_keywords": ["meeting"]},
        "sources": [ws("a", dirs / "a", scoring={"historical_penalty": 5}),
                    ws("b", dirs / "b", scoring={"historical_keywords": []}),
                    ws("c", dirs / "c")],
    }))
    a, b, c = config.sources
    assert (a.scoring.historical_penalty, a.scoring.historical_keywords) == (5, ("meeting",))
    assert (b.scoring.historical_penalty, b.scoring.historical_keywords) == (3, ())
    assert (c.scoring.historical_penalty, c.scoring.historical_keywords) == (3, ("meeting",))


@pytest.mark.parametrize("scoring,field", [
    ({"historical_penalty": -1}, "historical_penalty"),
    ({"historical_penalty": True}, "historical_penalty"),
    ({"historical_penalty": "3"}, "historical_penalty"),
    ({"historical_keywords": "meeting"}, "historical_keywords"),
    ({"historical_keywords": ["meeting", ""]}, "historical_keywords"),
    ({"historical_keywords": ["meeting", 3]}, "historical_keywords"),
])
def test_invalid_historical_scoring_is_rejected_naming_the_field(dirs, scoring, field):
    with pytest.raises(ConfigError, match=field):
        Config.load(write(dirs / "config.json", {
            "scoring": scoring, "sources": [ws("a", dirs / "a")]}))


def test_too_many_historical_keywords_is_rejected(dirs):
    with pytest.raises(ConfigError, match="historical_keywords"):
        Config.load(write(dirs / "config.json", {
            "scoring": {"historical_keywords": [f"kw{i}" for i in range(101)]},
            "sources": [ws("a", dirs / "a")]}))


@pytest.mark.parametrize("where", ["global", "source"])
@pytest.mark.parametrize("scoring,field", [
    ({"boost_foo": 1}, "boost_foo"),
    ({"path_match_bonus": -1}, "path_match_bonus"),
    ({"recency_bonus": -0.5}, "recency_bonus"),
    ({"recency_window_days": -1}, "recency_window_days"),
    ({"recency_window_days": 1.5}, "recency_window_days"),
    ({"recency_window_days": True}, "recency_window_days"),
    ({"recency_bonus": "10"}, "recency_bonus"),
    ({"path_match_bonus": False}, "path_match_bonus"),
    ({"strip_wikilinks": "true"}, "strip_wikilinks"),
    ({"strip_wikilinks": 1}, "strip_wikilinks"),
])
def test_invalid_scoring_is_rejected_naming_the_field(dirs, where, scoring, field):
    data = {"sources": [ws("a", dirs / "a")]}
    if where == "global":
        data["scoring"] = scoring
    else:
        data["sources"][0]["scoring"] = scoring
    with pytest.raises(ConfigError, match=field):
        Config.load(write(dirs / "config.json", data))


@pytest.mark.parametrize("where", ["global", "source"])
def test_scoring_must_be_an_object(dirs, where):
    data = {"sources": [ws("a", dirs / "a")]}
    if where == "global":
        data["scoring"] = [1, 2]
    else:
        data["sources"][0]["scoring"] = "off"
    with pytest.raises(ConfigError, match="scoring"):
        Config.load(write(dirs / "config.json", data))


def test_bootstrap_config_writes_scoring_defaults():
    data = bootstrap_config_data()
    assert data["scoring"] == {"recency_window_days": 30, "recency_bonus": 10,
                               "path_match_bonus": 5, "strip_wikilinks": True,
                               "historical_penalty": 0, "historical_keywords": []}


def test_describe_reports_effective_scoring(dirs):
    config = Config.load(write(dirs / "config.json", {
        "scoring": {"recency_bonus": 8},
        "sources": [ws("a", dirs / "a"), ws("b", dirs / "b", scoring={"recency_bonus": 0})],
    }))
    scoring = config.describe()["scoring"]
    assert scoring["default"]["recency_bonus"] == 8
    assert scoring["sources"] == {"b": {"recency_window_days": 30, "recency_bonus": 0,
                                        "path_match_bonus": 5, "strip_wikilinks": True,
                                        "historical_penalty": 0, "historical_keywords": ()}}
