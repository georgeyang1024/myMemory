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
        ws("company", dirs / "c", writable=False),
    ]}))
    by_name = {w.name: w for w in config.sources}
    assert by_name["a"].can_write, "省略 writable 即可写"
    assert by_name["团队"].can_write
    assert not by_name["company"].can_write
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
    "", "a/b", "../x", "a b", "a:b", "readonly/company", "readonly/", "x" * 65,
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
