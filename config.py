#!/usr/bin/env python3
"""config.py —— myMemory 的配置管理工具（人工操作）。

只依赖 Python 标准库，不需要虚拟环境。
负责：source 增删改、刷新周期、立即重建索引、重启服务。
启动、停止、状态、日志仍由 run.py 负责。

配置修改一律**重启生效**（见 docs/adr/0017-config-file-and-cli.md）；
加 --restart 可在改完后直接调用 run.py --restart。唯一的热操作是 reindex。

用法：
    python config.py source list
    python config.py source add <name> --dir <目录> (--readonly | --writable) [--desc "..."] [--restart]
    python config.py source edit <name> [--name <新名>] [--dir <新目录>] [--readonly | --writable]
                                    [--desc "..."] [--restart]
    python config.py source remove <name> [--yes] [--restart]
    python config.py config set poll_interval <秒> [--restart]
    python config.py config set max_cached_docs <篇数> [--restart]
    python config.py reindex [--full]
    python config.py restart

配置文件位置与服务同一口径：MEMORY_CONFIG > ~/.myMemory/config.json。
配置不存在时不会静默生成——记忆目录无法猜测（见 docs/adr/0025-no-default-memory-dir.md）：
`source add` 可从零建档（首个 source 建议名为 memory）；其余子命令直接报错。
校验与服务共用 src/config.py：CLI 放行的配置，服务一定能启动。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE / "src"
RUN_PY = HERE / "run.py"

sys.path.insert(0, str(SRC_DIR))
from config import (  # noqa: E402
    Config,
    ConfigError,
    bootstrap_config_data,
    bootstrap_config_data_with_source,
    read_config_data,
    resolve_config_path,
    write_json_atomic,
)

# 可经 CLI 设置的配置项。其余配置请直接编辑 config.json（需求 §5）。
# 键 → (单位, 值为 0 时的含义)
SETTABLE = {
    "poll_interval": ("秒", "关闭轮询"),
    "max_cached_docs": ("篇", "不限，全文全部常驻内存"),
}
# 配置项 → (含义, 开启时的后果)：布尔开关，取值 true/false。
SETTABLE_BOOLS = {
    "allow_mcp_delete": ("AI 能否删除记忆", "delete 工具与 merge 删源对 AI 开放"),
}


class CliError(Exception):
    """可预期的失败：打印消息，退出码 2，不写任何文件。"""


def say(message: str) -> None:
    print(message, flush=True)


# --- 配置读写 ---------------------------------------------------------------

def load(path: Path) -> dict[str, Any]:
    """读取配置。不存在时报错——记忆目录无从猜测，先跑 run.py 建档或 source add（ADR-0025）。"""
    if not path.exists():
        raise CliError(
            f"配置文件不存在：{path}\n"
            f"请先添加一个 source（目录必须已存在）：\n"
            f"  python config.py source add memory --dir <记忆目录> --writable"
        )
    try:
        return read_config_data(path)
    except ConfigError as exc:
        raise CliError(str(exc)) from exc


def save(path: Path, data: dict[str, Any]) -> None:
    """先用服务的校验跑一遍完整配置，通过了才落盘。"""
    try:
        Config.from_data(data, config_file=path)
    except ConfigError as exc:
        raise CliError(f"校验未通过，未修改配置文件：{exc}") from exc
    write_json_atomic(path, data)


def find(data: dict[str, Any], name: str) -> dict[str, Any]:
    for item in data.get("sources") or []:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    names = ", ".join(str(w.get("name")) for w in data.get("sources") or [] if isinstance(w, dict))
    raise CliError(f"source 不存在：{name}（现有：{names or '无'}）")


def absolute_dir(raw: str) -> str:
    """校验目录存在，返回绝对路径。不解析盘符映射——Z:\\ 就写 Z:\\，不换成 UNC。"""
    directory = Path(raw).expanduser()
    if not directory.is_dir():
        raise CliError(f"目录不存在：{directory}。请在服务将要运行的系统上执行本命令")
    return os.path.abspath(directory)


def finish(path: Path, args: argparse.Namespace) -> int:
    say(f"已写入 {path}")
    if getattr(args, "restart", False):
        return do_restart(path)
    say("重启后生效：python config.py restart（或 python run.py --restart）")
    return 0


# --- source ---------------------------------------------------------------

def source_list(path: Path, _args: argparse.Namespace) -> int:
    if not path.exists():
        say(f"配置文件不存在：{path}（先运行 run.py 完成首次建档，或用 source add 添加）")
        return 0
    data = read_config_data(path)
    rows = [w for w in data.get("sources") or [] if isinstance(w, dict)]
    say(f"配置文件：{path}")
    if not rows:
        say("（没有 source）")
        return 0
    width = max(len(str(w.get("name", ""))) for w in rows)
    for w in rows:
        name = str(w.get("name", ""))
        mode = "读写" if w.get("writable", True) else "只读"
        line = f"  {name.ljust(width)}  {mode}  {w.get('dir', '')}"
        if w.get("description"):
            line += f"  # {w['description']}"
        say(line)
    return 0


def source_add(path: Path, args: argparse.Namespace) -> int:
    name = args.name.strip()
    # 创建时必须显式指定只读或可写（argparse 已保证二选一）；写配置总是写出 writable。
    item: dict[str, Any] = {"name": name, "dir": absolute_dir(args.dir), "writable": args.writable}
    if args.desc:
        item["description"] = args.desc.strip()

    # 从零建档：记忆目录无法猜测（ADR-0025），建档只发生在使用者显式 source add 时。
    # 首个 source 是 memory 时走共享建档入口，与 run.py 首次启动写出的配置同构
    # （描述默认"默认记忆源"）。
    if not path.exists():
        say(f"配置文件不存在，将创建：{path}")
        if name == "memory":
            data = bootstrap_config_data_with_source(item["dir"])
            data["sources"][0]["writable"] = args.writable
            if args.desc:
                data["sources"][0]["description"] = args.desc.strip()
        else:
            data = bootstrap_config_data()
            data["sources"] = [item]
        save(path, data)
        say(f"已建档并添加 source {name}（{'读写' if args.writable else '只读'}）→ {item['dir']}")
        return finish(path, args)

    data = copy.deepcopy(load(path))
    data.setdefault("sources", []).append(item)
    save(path, data)
    say(f"已添加 source {name}（{'读写' if args.writable else '只读'}）→ {item['dir']}")
    return finish(path, args)


def source_edit(path: Path, args: argparse.Namespace) -> int:
    if args.new_name is None and args.dir is None and args.desc is None and args.writable is None:
        raise CliError("没有要修改的内容：请指定 --name、--dir、--readonly/--writable 或 --desc 中的至少一个")
    data = copy.deepcopy(load(path))
    item = find(data, args.name)
    changes = []
    if args.new_name is not None:
        item["name"] = args.new_name.strip()
        changes.append(f"名称 {args.name} → {item['name']}")
    if args.dir is not None:
        item["dir"] = absolute_dir(args.dir)
        changes.append(f"目录 → {item['dir']}")
    if args.writable is not None:
        item["writable"] = args.writable
        changes.append("可写" if args.writable else "只读")
    else:
        item.setdefault("writable", True)
    if args.desc is not None:
        if args.desc.strip():
            item["description"] = args.desc.strip()
        else:
            item.pop("description", None)
        changes.append("描述已更新")
    save(path, data)
    say(f"已修改 source {args.name}：{'；'.join(changes)}")
    if args.new_name is not None and args.new_name.strip() != args.name:
        say("注意：改名会改变文档身份 (source, path)，调用方需改用新名称")
    return finish(path, args)


def source_remove(path: Path, args: argparse.Namespace) -> int:
    data = copy.deepcopy(load(path))
    item = find(data, args.name)
    if not args.yes:
        answer = input(f"确认从配置中删除 source {args.name}（{item.get('dir')}）？"
                       f"磁盘上的文件不会被删除。[y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            say("已取消，未修改配置")
            return 1
    data["sources"] = [w for w in data["sources"] if w is not item]
    save(path, data)
    say(f"已从配置中删除 source {args.name}；目录 {item.get('dir')} 下的文件保持不变")
    return finish(path, args)


# --- config -----------------------------------------------------------------

def config_set(path: Path, args: argparse.Namespace) -> int:
    settable = sorted(set(SETTABLE) | set(SETTABLE_BOOLS))
    if args.key not in settable:
        raise CliError(f"不支持通过 CLI 设置 {args.key}；可设置：{', '.join(settable)}。"
                       f"其余配置请直接编辑 {path}")
    data = copy.deepcopy(load(path))
    if args.key in SETTABLE_BOOLS:
        meaning, effect = SETTABLE_BOOLS[args.key]
        values = {"true": True, "1": True, "yes": True,
                  "false": False, "0": False, "no": False}
        raw = args.value.strip().lower()
        if raw not in values:
            raise CliError(f"{args.key} 必须是 true 或 false，当前值：{args.value!r}")
        data[args.key] = values[raw]
        save(path, data)
        say(f"已设置 {args.key} = {raw}（{meaning}；开启时{effect}）")
        return finish(path, args)
    try:
        value = int(args.value)
    except ValueError as exc:
        raise CliError(f"{args.key} 必须是整数（{SETTABLE[args.key][0]}），当前值：{args.value!r}") from exc
    data[args.key] = value
    save(path, data)
    say(f"已设置 {args.key} = {value}" + (f"（0 表示{SETTABLE[args.key][1]}）" if value == 0 else ""))
    return finish(path, args)


# --- 运行中服务 --------------------------------------------------------------

def service_address(path: Path) -> tuple[str, int]:
    data = read_config_data(path) if path.exists() else {}
    host = data.get("host", "127.0.0.1")
    port = data.get("port", 7083)
    if not isinstance(host, str) or host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return host, port if isinstance(port, int) else 7083


def do_reindex(path: Path, _args: argparse.Namespace | None = None) -> int:
    try:
        host, port = service_address(path)
    except ConfigError as exc:
        raise CliError(str(exc)) from exc
    full = bool(getattr(_args, "full", False))
    url = f"http://{'[' + host + ']' if ':' in host else host}:{port}/reindex" + ("?full=1" if full else "")
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        say(f"服务未运行（{url} 不可达：{exc}）。下次启动时会自动全量构建索引。")
        return 1
    state = body.get("index_refresh")
    say(f"已通知服务{'全量' if full else '增量'}重建索引" +
        ("，并入了正在进行的那一轮" if state == "merged" else "") +
        "。完成后可用 /health 的 built_at 确认。")
    return 0


def do_restart(path: Path, _args: argparse.Namespace | None = None) -> int:
    environ = dict(os.environ)
    environ["MEMORY_CONFIG"] = str(path)
    say("正在重启服务：run.py --restart")
    return subprocess.run([sys.executable, str(RUN_PY), "--restart"], env=environ).returncode


# --- 入口 -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="config.py",
        description="myMemory 配置管理：source、刷新周期、立即重建、重启。配置修改重启生效。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ws = sub.add_parser("source", help="管理 source").add_subparsers(dest="action", required=True)

    p = ws.add_parser("list", help="列出全部 source")
    p.set_defaults(func=source_list)

    p = ws.add_parser("add", help="添加 source")
    p.add_argument("name", help="名称：中英文、数字、下划线、连字符")
    p.add_argument("--dir", required=True, help="目录，必须已存在（与 edit 一致用 --dir 指定）")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--readonly", dest="writable", action="store_false",
                      help="只读：服务永不写入（必须与 --writable 二选一）")
    mode.add_argument("--writable", dest="writable", action="store_true", help="可写")
    p.add_argument("--desc", default="", help="一句话描述，供 AI 判断该往哪写")
    p.add_argument("--restart", action="store_true", help="改完后重启服务")
    p.set_defaults(func=source_add)

    p = ws.add_parser("edit", help="修改 source（--readonly / --writable 切换只读）")
    p.add_argument("name", help="现有名称")
    p.add_argument("--name", dest="new_name", default=None, help="新名称")
    p.add_argument("--dir", default=None, help="新目录，必须已存在")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--readonly", dest="writable", action="store_false", default=None,
                      help="设为只读")
    mode.add_argument("--writable", dest="writable", action="store_true", default=None,
                      help="设为可写")
    p.add_argument("--desc", default=None, help="新描述（传空字符串清除）")
    p.add_argument("--restart", action="store_true", help="改完后重启服务")
    p.set_defaults(func=source_edit)

    p = ws.add_parser("remove", help="从配置中删除 source（不删除磁盘文件）")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="跳过确认")
    p.add_argument("--restart", action="store_true", help="改完后重启服务")
    p.set_defaults(func=source_remove)

    cfg = sub.add_parser("config", help="修改配置").add_subparsers(dest="action", required=True)
    p = cfg.add_parser("set", help="设置配置项：poll_interval（秒）、max_cached_docs（篇）、"
                                   "allow_mcp_delete（true/false，AI 删除开关）")
    p.add_argument("key", choices=sorted(set(SETTABLE) | set(SETTABLE_BOOLS)))
    p.add_argument("value")
    p.add_argument("--restart", action="store_true", help="改完后重启服务")
    p.set_defaults(func=config_set)

    p = sub.add_parser("reindex", help="通知运行中的服务立即重建索引（默认增量，无需重启）")
    p.add_argument("--full", action="store_true", help="全量重建：忽略缓存，所有文件重读、重分词")
    p.set_defaults(func=do_reindex)

    p = sub.add_parser("restart", help="重启服务（调用 run.py --restart）")
    p.set_defaults(func=do_restart)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = resolve_config_path()
    try:
        return args.func(path, args)
    except CliError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
