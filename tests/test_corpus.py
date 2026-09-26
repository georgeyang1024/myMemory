"""corpus 层单测：切块边界、扫描过滤、指纹变更检测。"""

import json
import os
from pathlib import Path

import pytest

import corpus
from config import Config, ConfigError


def make_config(root: Path, **overrides) -> Config:
    """在 root 下写一份 config.json 并加载它。

    默认只有一个读写 source `memory` → root/memory。
    overrides 用 config.json 的字段名；为少改旧用例，也接受 MEMORY_XXX 形式。
    """
    data = {
        "host": "127.0.0.1",
        "port": 7099,
        "poll_interval": 0,
        "sources": [{"name": "memory", "dir": str(root / "memory")}],
    }
    for key, value in overrides.items():
        if key.startswith("MEMORY_"):
            key = key[len("MEMORY_"):].lower()
            value = int(value) if str(value).lstrip("-").isdigit() else value
        data[key] = value
    path = root / "config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return Config.load(path, create_default=False)


@pytest.fixture
def sample_root(tmp_path: Path) -> Path:
    root = tmp_path / "memory"
    (root / "sub").mkdir(parents=True)
    (root / "a.md").write_text("x" * 1000, encoding="utf-8")
    (root / "sub" / "b.txt").write_text("hello", encoding="utf-8")
    (root / "sub" / "空 格 文件.md").write_text("有空格的文件名", encoding="utf-8")
    (root / "ignored.csv").write_text("a,b,c", encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "junk.md").write_text("工具产物", encoding="utf-8")
    return tmp_path


# --- split_text 边界 -------------------------------------------------------

def test_split_empty_text():
    assert corpus.split_text("", size=800, step=680) == []


def test_split_blank_only_text():
    assert corpus.split_text("   \n\n  ", size=800, step=680) == []


def test_split_shorter_than_window():
    pieces = corpus.split_text("abc", size=800, step=680)
    assert pieces == [(0, 3, "abc")]


def test_split_exactly_one_window():
    text = "x" * 800
    pieces = corpus.split_text(text, size=800, step=680)
    assert len(pieces) == 1
    assert pieces[0][:2] == (0, 800)


def test_split_covers_whole_text_with_overlap():
    text = "".join(str(i % 10) for i in range(2500))
    pieces = corpus.split_text(text, size=800, step=680)
    assert pieces[0][0] == 0
    assert pieces[-1][1] == len(text), "最后一块必须覆盖到原文结尾"
    for start, end, piece in pieces:
        assert text[start:end] == piece, "偏移量必须能还原原文片段"
    starts = [p[0] for p in pieces]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)


def test_split_no_redundant_tail_chunk():
    """尾部不应产生一个完全被前一块覆盖的冗余小块。"""
    text = "y" * 1400
    pieces = corpus.split_text(text, size=800, step=680)
    assert len(pieces) == 2
    assert pieces[-1][1] == 1400


# --- 扫描过滤（经 storage + index.build）-----------------------------------------

def _keys(config):
    import index
    return {key for key in index.build(config).documents}


def test_scan_filters_extensions_and_hidden(sample_root: Path):
    paths = {path for _, path in _keys(make_config(sample_root))}
    assert paths == {"a.md", "sub/b.txt", "sub/空 格 文件.md"}
    assert not any("/." in p for p in paths), "隐藏目录下的文件必须被排除"


def test_scan_case_insensitive_extension(sample_root: Path):
    (sample_root / "memory" / "UPPER.MD").write_text("大写扩展名", encoding="utf-8")
    assert ("memory", "UPPER.MD") in _keys(make_config(sample_root))


def test_contents_and_chunk_offsets_map_back_to_source(sample_root: Path):
    import index
    snapshot = index.build(make_config(sample_root))
    assert snapshot.documents[("memory", "a.md")].char_count == 1000
    assert snapshot.contents[("memory", "sub/b.txt")] == "hello"
    assert {c.key for c in snapshot.chunks} == set(snapshot.contents)
    for chunk in snapshot.chunks:
        piece = snapshot.contents[chunk.key][chunk.char_start : chunk.char_end]
        assert piece.strip() and len(piece) == chunk.char_end - chunk.char_start


def test_config_rejects_overlap_ge_chunk_size(sample_root: Path):
    with pytest.raises(ConfigError):
        make_config(sample_root, MEMORY_CHUNK_SIZE="200", MEMORY_CHUNK_OVERLAP="200")


# --- 多 source ----------------------------------------------------------

def test_scan_covers_every_source_with_relative_paths(sample_root: Path):
    """同名 path 可以出现在不同 source 中，身份是 (source, path)。"""
    team = sample_root / "team"
    team.mkdir()
    (team / "a.md").write_text("团队的 a", encoding="utf-8")
    config = make_config(sample_root, sources=[
        {"name": "memory", "dir": str(sample_root / "memory")},
        {"name": "team", "dir": str(team), "writable": False},
    ])
    keys = _keys(config)
    assert ("memory", "a.md") in keys and ("team", "a.md") in keys
    assert all(not path.startswith(("memory/", "team/")) for _, path in keys), "path 不拼接 source 名"
