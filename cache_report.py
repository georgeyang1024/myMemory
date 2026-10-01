#!/usr/bin/env python3
"""cache_report.py —— 查看 myMemory 全文缓存里当前存了哪些文档（人工操作）。

只依赖 Python 标准库。读取与 config.json 同目录的 index.cache
（ADR-0019），解析其中持久化的全文缓存（LRU，容量 max_cached_docs），
按来源 / 目录 / 单篇统计，无需停止服务。

用法：
    python cache_report.py                 # 概览：按 source 与目录汇总
    python cache_report.py --list          # 逐篇列出（按最近使用从新到旧）
    python cache_report.py --source team   # 只看某个 source
    python cache_report.py --list --top 50 # 只列最近使用的 50 篇
    python cache_report.py --config <路径>  # 手动指定 config.json
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import pickle
import sys
import types
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE / "src"
sys.path.insert(0, str(SRC_DIR))
from config import read_config_data, resolve_config_path  # noqa: E402


def _stub_modules() -> None:
    """用空壳顶替分词/检索依赖，让 pickle 能定位到 src/index.py 的真实类。

    只解析 contents 列表（纯 tuple/str），不需要 jieba、rank_bm25 真正可用。
    """
    for name in ("jieba", "rank_bm25"):
        if name not in sys.modules or type(sys.modules[name]) is not types.ModuleType:
            sys.modules[name] = types.ModuleType(name)
    if not hasattr(sys.modules["rank_bm25"], "BM25Okapi"):
        sys.modules["rank_bm25"].BM25Okapi = type("BM25Okapi", (), {})


def load_cache_path(config_file: Path) -> Path:
    data = read_config_data(config_file)
    return config_file.parent / "index.cache"


def load_contents(path: Path) -> list[tuple]:
    if not path.exists():
        raise SystemExit(f"找不到索引缓存：{path}\n服务尚未建立过索引，先跑一次 run.py 启动。")
    _stub_modules()
    importlib.import_module("index")
    with open(path, "rb") as f:
        payload = pickle.load(f)
    return payload.get("contents") or []


def human(n: int) -> str:
    return f"{n:,}"


def main() -> int:
    parser = argparse.ArgumentParser(description="查看 myMemory 全文缓存（index.cache）当前缓存了哪些文档")
    parser.add_argument("--config", type=Path, default=None,
                        help="config.json 路径（默认 MEMORY_CONFIG > ~/.myMemory/config.json）")
    parser.add_argument("--list", action="store_true", help="逐篇列出缓存文档")
    parser.add_argument("--top", type=int, default=0, metavar="N",
                        help="与 --list 连用：只列最近使用的 N 篇")
    parser.add_argument("--source", default=None, metavar="NAME", help="只统计/列出这个 source")
    args = parser.parse_args()

    config_file = resolve_config_path(args.config or os.environ.get("MEMORY_CONFIG"))
    data = read_config_data(config_file)
    capacity = data.get("max_cached_docs")
    sources = {item["name"] for item in data.get("sources") or []}
    if args.source and args.source not in sources:
        raise SystemExit(f"source 不存在：{args.source}（现有：{', '.join(sorted(sources))}）")

    contents = load_contents(load_cache_path(config_file))
    if args.source:
        contents = [it for it in contents if it[0][0] == args.source]

    # dump 顺序是最近使用从旧到新，倒过来即从新到旧。
    contents = list(reversed(contents))

    print(f"缓存文件：{config_file.parent / 'index.cache'}")
    print(f"容量上限：{capacity if capacity else '不限'} 篇    当前缓存：{len(contents)} 篇")
    print()

    by_source = Counter(it[0][0] for it in contents)
    chars_by_source = Counter()
    for it in contents:
        chars_by_source[it[0][0]] += len(it[2])
    print("== 按 source ==")
    for src, n in by_source.most_common():
        print(f"  {src:<12} {n:>5} 篇   全文共 {human(chars_by_source[src]):>12} 字符")
    print()

    by_cat: Counter = Counter()
    chars_by_cat: Counter = Counter()
    for it in contents:
        src, path = it[0]
        cat = path.split("/", 1)[0] if "/" in path else "(根目录)"
        by_cat[cat] += 1
        chars_by_cat[cat] += len(it[2])
    if args.source:
        print(f"== {args.source} 内按目录 ==")
        for cat, n in by_cat.most_common():
            print(f"  {cat:<28} {n:>5} 篇   {human(chars_by_cat[cat]):>12} 字符")
        print()

    if args.list:
        shown = contents if args.top <= 0 else contents[:args.top]
        print(f"== 逐篇（最近使用优先{('，前 ' + str(args.top) + ' 篇') if args.top else ''}）==")
        for i, it in enumerate(shown, 1):
            src, path = it[0]
            text = it[2]
            print(f"  {i:>4}. [{src}] {path}   {human(len(text))} 字符")
        if args.top and args.top < len(contents):
            print(f"  ... 其余 {len(contents) - args.top} 篇未列出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
