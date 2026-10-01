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
    python config.py multi-user set <个人根目录> [--restart]     # 启用多人共用（重启生效）
    python config.py multi-user enable [--restart]               # 打开开关（重启生效）
    python config.py multi-user disable [--restart]              # 关闭开关（其余配置保留）
    python config.py multi-user unset [--restart]                # 移除多人共用
    python config.py multi-user show                             # 配置与生效状态
    python config.py multi-user admin add <名字> [--restart]     # 管理员名单（重启生效）
    python config.py multi-user admin remove <名字> [--restart]
    python config.py multi-user admin list
    python config.py multi-user user list                         # 枚举用户目录（排障）
    python config.py multi-user user add <名字>                   # 开通用户（建目录，热生效）
    python config.py config set poll_interval <秒> [--restart]
    python config.py config set max_cached_docs <篇数> [--restart]
    python config.py config edit --scoring <字段>=<值> [--scoring ...]
                                 [--reset-scoring <字段>|all] [--restart]     # 全局打分调整
    python config.py source edit <name> --scoring <字段>=<值> [--scoring ...]
                                    [--reset-scoring <字段>|all] [--restart]  # 单个 source 覆盖
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
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE / "src"
RUN_PY = HERE / "run.py"

sys.path.insert(0, str(SRC_DIR))
from config import (  # noqa: E402
    DEFAULTS,
    Config,
    ConfigError,
    bootstrap_config_data,
    bootstrap_config_data_with_source,
    personal_sources_report,
    read_config_data,
    resolve_config_path,
    validate_source_name,
    write_json_atomic,
    _is_editor_reserved,
)

# 可经 CLI 设置的配置项。其余配置请直接编辑 config.json（需求 §5）。
# 键 → (单位, 值为 0 时的含义)
SETTABLE = {
    "poll_interval": ("秒", "关闭轮询"),
    "max_cached_docs": ("篇", "不限，全文全部常驻内存"),
}
# 配置项 → (含义, 开启时的后果)：布尔开关，取值 true/false。
SETTABLE_BOOLS = {
    "allow_mcp_delete": ("AI 能否删除记忆", "delete 工具对 AI 开放"),
}
# 打分调整（ADR-0027）：全局用 config edit --scoring，单个 source 用 source edit --scoring，写法相同。
SCORING_FIELDS = tuple(DEFAULTS["scoring"])
_BOOL_WORDS = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}


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


def parse_scoring_value(field: str, raw: str) -> Any:
    """把命令行字符串转成 scoring 字段的值。范围（非负）由保存前的完整校验把关。"""
    text = raw.strip()
    if field == "strip_wikilinks":
        if text.lower() not in _BOOL_WORDS:
            raise CliError(f"{field} 必须是 true 或 false，当前值：{raw!r}")
        return _BOOL_WORDS[text.lower()]
    if field == "recency_window_days":
        try:
            return int(text)
        except ValueError as exc:
            raise CliError(f"{field} 必须是整数（天），当前值：{raw!r}") from exc
    if field == "historical_keywords":
        # 逗号分隔；各项 strip，丢空项；范围（非空、条数）由保存前的完整校验把关。
        return [item.strip() for item in text.split(",") if item.strip()]
    try:
        value = float(text)
    except ValueError as exc:
        raise CliError(f"{field} 必须是数字（分），当前值：{raw!r}") from exc
    return int(value) if value.is_integer() else value


def format_scoring(overrides: dict[str, Any]) -> str:
    def render(value: Any) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, list):
            return ",".join(str(item) for item in value)
        return str(value)
    return ", ".join(f"{k}={render(v)}" for k, v in overrides.items())


def rebuild_note(fields) -> None:
    if "strip_wikilinks" in fields:
        say("注意：strip_wikilinks 改变分词结果，重启时会全量重建一次索引")


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
        if isinstance(w.get("scoring"), dict) and w["scoring"]:
            line += f"  [打分覆盖 {format_scoring(w['scoring'])}]"
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
    if (args.new_name is None and args.dir is None and args.desc is None and args.writable is None
            and not args.scoring and not args.reset_scoring):
        raise CliError("没有要修改的内容：请指定 --name、--dir、--readonly/--writable、--desc、"
                       "--scoring 或 --reset-scoring 中的至少一个")
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
    touched = edit_scoring(item, args.scoring or [], args.reset_scoring or [],
                           f"source {args.name}")
    if touched:
        changes.append("打分覆盖 → " + (format_scoring(item["scoring"]) if item.get("scoring")
                                      else "无（全部继承全局）"))
    save(path, data)
    say(f"已修改 source {args.name}：{'；'.join(changes)}")
    if args.new_name is not None and args.new_name.strip() != args.name:
        say("注意：改名会改变文档身份 (source, path)，调用方需改用新名称")
    rebuild_note(touched)
    return finish(path, args)


def edit_scoring(item: dict[str, Any], assignments: list[str], resets: list[str],
                 owner: str) -> set[str]:
    """先按 --reset-scoring 删字段，再按 --scoring 字段=值 写字段。返回动过的字段。

    item 是整个配置（全局）或一个 source。只存写了的字段，没写的继承上一层；
    清空后连 scoring 对象一起删掉。
    """
    raw = item.get("scoring")
    overrides: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    touched: set[str] = set()
    for name in resets:
        if name == "all":
            touched |= set(overrides)
            overrides.clear()
            continue
        if name not in SCORING_FIELDS:
            raise CliError(f"未知的打分字段：{name}；可用：{', '.join(SCORING_FIELDS)} 或 all")
        if name not in overrides:
            raise CliError(f"{owner} 没有设置 {name}，无需重置")
        del overrides[name]
        touched.add(name)
    for assignment in assignments:
        name, sep, value = assignment.partition("=")
        name = name.strip()
        if not sep:
            raise CliError(f"--scoring 的格式是 字段=值，如 recency_bonus=0，当前：{assignment!r}")
        if name not in SCORING_FIELDS:
            raise CliError(f"未知的打分字段：{name}；可用：{', '.join(SCORING_FIELDS)}")
        overrides[name] = parse_scoring_value(name, value)
        touched.add(name)
    if overrides:
        item["scoring"] = overrides
    else:
        item.pop("scoring", None)
    return touched


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

def config_edit(path: Path, args: argparse.Namespace) -> int:
    """全局打分调整：与 source edit 的 --scoring / --reset-scoring 写法相同。"""
    if not args.scoring and not args.reset_scoring:
        raise CliError("没有要修改的内容：请指定 --scoring 或 --reset-scoring")
    data = copy.deepcopy(load(path))
    touched = edit_scoring(data, args.scoring or [], args.reset_scoring or [], "全局配置")
    save(path, data)
    current = format_scoring(data["scoring"]) if data.get("scoring") else "无（全部取默认值）"
    say(f"已修改全局打分：{current}（未单独覆盖的 source 都按此计算）")
    rebuild_note(touched)
    return finish(path, args)


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


# --- 多人共用与用户 -----------------------------------------------------------

def _mu_raw_enabled(data: dict[str, Any]) -> bool:
    """multi_user 块存在且开关打开（enabled 默认 false）。"""
    mu = data.get("multi_user")
    return isinstance(mu, dict) and bool(mu.get("enabled", False))


def _require_multi_user(data: dict[str, Any]) -> dict[str, Any]:
    mu = data.get("multi_user")
    if not isinstance(mu, dict):
        raise CliError("尚未启用多人共用：先执行 python config.py multi-user set <个人根目录>")
    return mu


def multi_user_set(path: Path, args: argparse.Namespace) -> int:
    data = copy.deepcopy(load(path))
    mu = data.setdefault("multi_user", {})
    mu["enabled"] = True
    mu["store_dir"] = absolute_dir(args.dir)
    if getattr(args, "guest_writable", None):
        mu["guest_writable"] = True
    save(path, data)
    say(f"已启用多人共用（enabled = true）：个人根目录 {mu['store_dir']}"
        + ("；访客可写公共 source" if mu.get("guest_writable") else "；访客对公共 source 只读（默认）"))
    return finish(path, args)


def multi_user_enable(path: Path, args: argparse.Namespace) -> int:
    data = copy.deepcopy(load(path))
    mu = _require_multi_user(data)
    if not mu.get("store_dir"):
        raise CliError("multi_user 缺少 store_dir：先执行 python config.py multi-user set <目录>")
    mu["enabled"] = True
    save(path, data)
    say(f"已打开多人共用开关（enabled = true）：个人根目录 {mu['store_dir']}")
    return finish(path, args)


def multi_user_disable(path: Path, args: argparse.Namespace) -> int:
    data = copy.deepcopy(load(path))
    mu = _require_multi_user(data)
    mu["enabled"] = False
    save(path, data)
    say("已关闭多人共用开关（enabled = false，单机形态；其余 multi_user 配置保留，重启生效）")
    return finish(path, args)


def multi_user_unset(path: Path, args: argparse.Namespace) -> int:
    data = copy.deepcopy(load(path))
    if not isinstance(data.get("multi_user"), dict):
        say("multi_user 未配置，无需移除")
        return 0
    del data["multi_user"]
    save(path, data)
    say("已移除多人共用形态，回到单机（个人 source 不再经 ?user= 路由）")
    return finish(path, args)


def multi_user_show(path: Path, _args: argparse.Namespace) -> int:
    data = read_config_data(path) if path.exists() else None
    if not data:
        raise CliError(f"配置文件不存在：{path}")
    config = Config.from_data(data, config_file=path)
    if config.multi_user is None:
        if isinstance(data.get("multi_user"), dict):
            say("多人共用已配置但开关关闭（multi_user.enabled = false，当前为单机形态）")
        else:
            say("多人共用未启用（单机形态）")
        return 0
    mu = config.multi_user
    say(f"多人共用已启用（重启后生效的配置；用户目录本身热生效）")
    say(f"  个人根目录：{mu.store_dir}" + ("" if mu.store_dir.is_dir() else "（当前不存在：0 个用户）"))
    say(f"  管理员：{', '.join(mu.admins) if mu.admins else '（无）'}"
        + ("" if mu.admins or (isinstance(data.get("multi_user"), dict)
                               and isinstance(data["multi_user"].get("admins"), list))
           else "（未配置 admins，默认 admin）"))
    say(f"  访客写公共 source：{'允许' if mu.guest_writable else '禁止（默认）'}")
    users, skipped = personal_sources_report(mu.store_dir, config.sources)
    say(f"  已派生用户：{', '.join(src.name for src in users) or '（无）'}")
    for item in skipped:
        reason = {"invalid_name": "名字不合法", "name_conflict": "与现有 source 重名",
                  "reserved": "编辑者保留字"}.get(item["reason"], item["reason"])
        say(f"  已跳过目录：{item['name']}（{reason}）")
    return 0


def mu_admin_add(path: Path, args: argparse.Namespace) -> int:
    data = copy.deepcopy(load(path))
    mu = _require_multi_user(data)
    name = args.name.strip()
    admins = [a for a in mu.get("admins", []) if isinstance(a, str)]
    if name in admins:
        say(f"已是管理员：{name}")
        return 0
    admins.append(name)
    mu["admins"] = admins
    save(path, data)
    say(f"已添加管理员 {name}（改配置，重启生效）")
    return finish(path, args)


def mu_admin_remove(path: Path, args: argparse.Namespace) -> int:
    data = copy.deepcopy(load(path))
    mu = _require_multi_user(data)
    name = args.name.strip()
    admins = [a for a in mu.get("admins", []) if isinstance(a, str)]
    if name not in admins:
        say(f"不在管理员名单中：{name}")
        return 0
    mu["admins"] = [a for a in admins if a != name]
    save(path, data)
    say(f"已移除管理员 {name}（改配置，重启生效）")
    return finish(path, args)


def mu_admin_list(path: Path, _args: argparse.Namespace) -> int:
    data = load(path)
    mu = data.get("multi_user")
    admins = [a for a in mu.get("admins", []) if isinstance(a, str)] if isinstance(mu, dict) else []
    say("管理员名单：" + (", ".join(admins) if admins else "（空）"))
    return 0


def user_list(path: Path, _args: argparse.Namespace) -> int:
    data = read_config_data(path) if path.exists() else None
    if not data:
        raise CliError(f"配置文件不存在：{path}")
    config = Config.from_data(data, config_file=path)
    if config.multi_user is None:
        raise CliError("未启用多人共用：先执行 python config.py multi-user set <个人根目录>")
    users, skipped = personal_sources_report(config.multi_user.store_dir, config.sources)
    say(f"个人根目录：{config.multi_user.store_dir}")
    for src in users:
        say(f"  {src.name}  →  {src.dir}")
    for item in skipped:
        reason = {"invalid_name": "名字不合法", "name_conflict": "与现有 source 重名",
                  "reserved": "编辑者保留字"}.get(item["reason"], item["reason"])
        say(f"  （已跳过）{item['name']}  # {reason}")
    if not users and not skipped:
        say("（个人根目录下没有一级子目录）")
    return 0


def user_add(path: Path, args: argparse.Namespace) -> int:
    data = load(path)
    mu_raw = _require_multi_user(data)
    root_raw = mu_raw.get("store_dir")
    if not isinstance(root_raw, str) or not root_raw.strip():
        raise CliError("multi_user 缺少 store_dir：先执行 python config.py multi-user set <目录>")
    # 用服务的校验口径：名字过 source 名白名单（会 strip），保留字与公共重名另查。
    name = validate_source_name(args.name)
    config = Config.from_data(data, config_file=path)
    if _is_editor_reserved(name):
        raise CliError(
            f"名字 {name!r} 是编辑者保留字（不区分大小写），不能用作个人目录名")
    if any(src.name.casefold() == name.casefold() for src in config.sources):
        raise CliError(f"名字 {name!r} 与现有公共 source 重名，请换一个名字")
    root = Path(os.path.abspath(Path(root_raw.strip()).expanduser()))
    target = root / name
    if target.exists():
        raise CliError(f"目录已存在（该用户可能已开通）：{target}")
    target.mkdir(parents=True)
    say(f"已开通用户 {name}：{target}")
    say("建目录即开通，热生效（无需重启）；MCP 端点：http://<服务器>:<端口>/mcp?user=" + name)
    return 0


# --- 运行中服务 --------------------------------------------------------------

def service_address(path: Path) -> tuple[str, int]:
    data = read_config_data(path) if path.exists() else {}
    host = data.get("host", "127.0.0.1")
    port = data.get("port", 7083)
    if not isinstance(host, str) or host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return host, port if isinstance(port, int) else 7083


def do_reindex(path: Path, args: argparse.Namespace | None = None) -> int:
    try:
        host, port = service_address(path)
    except ConfigError as exc:
        raise CliError(str(exc)) from exc
    full = bool(getattr(args, "full", False))
    # 多人共用下 /reindex 仅限管理员（服务端 403 拒绝）：CLI 自动携带管理员
    # 身份——--user 显式指定，否则取 admins 名单首个；admins 未配置时用
    # 默认管理员 admin；开关关闭（enabled=false）或单机形态不带 user。
    user = ""
    if path.exists():
        mu = read_config_data(path).get("multi_user")
        if isinstance(mu, dict) and mu.get("enabled", False):
            explicit = (getattr(args, "user", None) or "").strip()
            admins = [a for a in (mu or {}).get("admins", ["admin"])
                      if isinstance(a, str) and a.strip()]
            if explicit:
                user = explicit
            elif admins:
                user = admins[0]
            else:
                raise CliError("多人共用下 reindex 仅限管理员：请先 multi-user admin add，"
                               "或用 reindex --user <管理员名>")
    params = []
    if full:
        params.append("full=1")
    if user:
        params.append("user=" + urllib.parse.quote(user, safe=""))
    url = (f"http://{'[' + host + ']' if ':' in host else host}:{port}/reindex"
           + ("?" + "&".join(params) if params else ""))
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
    p.add_argument("--scoring", action="append", metavar="字段=值",
                   help="该 source 单独的打分覆盖，可重复，如 --scoring recency_bonus=0；"
                        f"字段：{', '.join(SCORING_FIELDS)}")
    p.add_argument("--reset-scoring", action="append", metavar="字段",
                   help="删除该 source 某个字段的覆盖、恢复继承全局，可重复；all 表示全部删除")
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

    p = cfg.add_parser("edit", help="修改全局打分调整（写法与 source edit 相同）")
    p.add_argument("--scoring", action="append", metavar="字段=值",
                   help=f"全局打分，可重复，如 --scoring recency_bonus=8；字段：{', '.join(SCORING_FIELDS)}")
    p.add_argument("--reset-scoring", action="append", metavar="字段",
                   help="删除全局某个字段、恢复默认值，可重复；all 表示全部删除")
    p.add_argument("--restart", action="store_true", help="改完后重启服务")
    p.set_defaults(func=config_edit)

    mu = sub.add_parser("multi-user", help="多人共用形态：开关、个人根目录与管理员名单").add_subparsers(
        dest="action", required=True)

    p = mu.add_parser("set", help="启用多人共用：指定个人根目录（目录必须已存在；重启生效）")
    p.add_argument("dir", help="个人根目录：其一级子目录每个对应一个用户")
    p.add_argument("--guest-writable", dest="guest_writable", action="store_true", default=None,
                   help="允许访客（无 ?user= 的匿名连接）写公共 source；默认禁止")
    p.add_argument("--restart", action="store_true", help="改完后重启服务")
    p.set_defaults(func=multi_user_set)

    p = mu.add_parser("enable", help="打开多人共用开关（multi_user.enabled = true；重启生效）")
    p.add_argument("--restart", action="store_true", help="改完后重启服务")
    p.set_defaults(func=multi_user_enable)

    p = mu.add_parser("disable", help="关闭多人共用开关（multi_user.enabled = false，"
                                      "回到单机形态；其余配置保留，重启生效）")
    p.add_argument("--restart", action="store_true", help="改完后重启服务")
    p.set_defaults(func=multi_user_disable)

    p = mu.add_parser("unset", help="移除多人共用形态，回到单机")
    p.add_argument("--restart", action="store_true", help="改完后重启服务")
    p.set_defaults(func=multi_user_unset)

    p = mu.add_parser("show", help="展示 multi_user 配置与生效状态")
    p.set_defaults(func=multi_user_show)

    admin = mu.add_parser("admin", help="维护管理员名单（改配置，重启生效）").add_subparsers(
        dest="admin_action", required=True)
    p = admin.add_parser("add", help="添加管理员（全域读写；无需同名个人目录）")
    p.add_argument("name", help="管理员名（任意字符串，但经 ?user= 传输：不得含控制字符、"
                                "长度 ≤64、不得为保留字 agent/scan/guest——不区分大小写）")
    p.add_argument("--restart", action="store_true", help="改完后重启服务")
    p.set_defaults(func=mu_admin_add)
    p = admin.add_parser("remove", help="移除管理员")
    p.add_argument("name")
    p.add_argument("--restart", action="store_true", help="改完后重启服务")
    p.set_defaults(func=mu_admin_remove)
    p = admin.add_parser("list", help="列出管理员")
    p.set_defaults(func=mu_admin_list)

    usr = mu.add_parser("user", help="排障与开通：个人根目录下的用户目录").add_subparsers(
        dest="user_action", required=True)
    p = usr.add_parser("list", help="枚举个人根目录下的一级子目录与派生有效性")
    p.set_defaults(func=user_list)
    p = usr.add_parser("add", help="开通用户：校验名字合法后创建个人目录（建目录 = 开通，热生效）")
    p.add_argument("name", help="用户名：中英文、数字、下划线、连字符；保留字 agent/scan/guest 不可用")
    p.set_defaults(func=user_add)

    p = sub.add_parser("reindex", help="通知运行中的服务立即重建索引（默认增量，无需重启）")
    p.add_argument("--full", action="store_true", help="全量重建：忽略缓存，所有文件重读、重分词")
    p.add_argument("--user", default=None,
                   help="多人共用下的管理员名（admins 非空时可省略，默认取名单首个）；单机形态无需指定")
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
