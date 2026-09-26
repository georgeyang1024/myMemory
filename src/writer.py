"""写入层：把一条记忆落成某个 source 目录下的一个 Markdown 文件。

这是本服务里唯一一处写磁盘的代码（index.cache 与 config.json 属于服务自身状态，
不在记忆目录里），因此写入边界必须在这里一次性守死：

1. **source 必须显式指定，且可写、当前可用**。配置只读（writable: false）、非 local 存储、
   掉盘、目录不存在一律拒绝；storage 层在落盘前还会再拦一次，且绝不重建被删除的目录。
2. **分类与文件名是标识符，不是路径**。二者都经过字符集白名单校验
   （config 模块的 is_valid_category / is_valid_filename）：白名单不含 / 与 \\，
   ".." 与首尾的 "." 另行禁止，所以 "../x"、"a/b"、".hidden" 在语法层就被拒绝，
   落盘路径只可能是 <source目录>/<分类>/<文件名>.md 或 <source目录>/<文件名>.md。
   不做 realpath、不做前缀比较——与 get-document 的集合成员判断同源。
3. **只写一级目录，分类可为空**。分类名不可嵌套；为空时直接写在 source 根目录。
4. **同路径即覆盖**（upsert）。目标文件已存在时整篇替换，不改名、不追加、不合并。
   代价是**覆盖不可撤销且没有备份**——见 ADR-0014。为了让这件事至少是**可见的**，
   覆盖时返回 created=False 与被替换内容的长度。

写入成功后由调用方（server.py）记下 agent 标记并触发索引刷新，本模块不认识索引。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from config import Config
from storage import DISK_OFFLINE, open_storage

logger = logging.getLogger(__name__)

EXTENSION = ".md"


class WriteError(ValueError):
    """入参不合法或落盘失败。消息直接返回给调用方（LLM），因此要可读且可据以纠正。"""


@dataclass(frozen=True, slots=True)
class Written:
    """一次成功的写入。(source, path) 与检索结果同形，可直接喂给 get-document。"""

    source: str
    path: str          # 相对 source 目录的 POSIX 路径，如 "技术/协议要点.md"
    absolute: Path
    char_count: int
    mtime: float                         # 落盘后的 mtime，用于判定 edited_by
    created: bool                        # True=新建，False=覆盖了已有文件
    replaced_char_count: int | None      # 覆盖时为被替换内容的长度，新建时为 None


def _normalize_stem(filename: str) -> str:
    """去掉调用方可能带上的 .md 后缀，返回文件名主干。

    调用方是 LLM，"传不传扩展名"是个纯粹的无谓分歧：两种都接受，
    对齐到同一种形态即可，不值得为此让它读一条报错再重试一轮。
    """
    stem = filename.strip()
    if stem.lower().endswith(EXTENSION):
        stem = stem[: -len(EXTENSION)]
    return stem.strip()


def save_memory(config: Config, source: str, category: str, filename: str,
                content: str) -> Written:
    """把一条记忆写到 <source目录>/[<category>/]<filename>.md，已存在即整篇覆盖。

    source 必须已配置且可写；category 可为空（写到 source 根目录）。
    category 与 filename 均为标识符而非路径，校验不通过即抛 WriteError。
    """
    name = (source or "").strip()
    if not name:
        raise WriteError("source 不能为空。可写的 source 见 list-sources。")
    target = config.source(name)
    if target is None:
        raise WriteError(f"source 不存在：{name!r}。")
    if not target.can_write:
        raise WriteError(f"source {name} 为只读，不能写入。")
    state = open_storage(target).probe()
    if not state.available:
        why = "挂载盘掉线" if state.reason == DISK_OFFLINE else "目录不存在"
        raise WriteError(f"source {name} 当前不可用（{why}），不能写入。")

    category = (category or "").strip()
    stem = _normalize_stem(filename or "")

    if category and not config.is_valid_category(category):
        raise WriteError(
            f"category 非法：{category!r}。只允许中英文、数字、空格、常用标点与 ()[]{{}}.,&#+@!'=~% ，"
            f"长度 1-64，不能以 . 或空格开头结尾、不能含 .. 或 / —— 分类只有一级，不支持嵌套目录。"
            f"不需要分类时传空字符串。"
        )

    if not stem:
        raise WriteError("filename 不能为空。传文件名即可，.md 后缀可带可不带。")
    if not config.is_valid_filename(stem):
        raise WriteError(
            f"filename 非法：{filename!r}。只允许中英文、数字、空格、常用标点与 ()[]{{}}.,&#+@!'=~% ，"
            f"长度 1-120，不能以 . 或空格开头结尾、不能含 .. / \\ : * ? \" < > |，"
            f"也不能是 CON、NUL 这类系统保留名 —— 目录层级由 category 决定。"
        )

    if not (content or "").strip():
        raise WriteError(
            "content 不能为空。请传入这条记忆的 Markdown 正文。"
            "（空内容会覆盖掉已有记忆，因此这里一律拒绝。）"
        )
    if len(content) > config.max_create_chars:
        raise WriteError(
            f"content 过长：{len(content)} 字符，上限 {config.max_create_chars}。"
            f"请拆成多条记忆，或精简内容。"
        )

    relative = f"{category}/{stem}{EXTENSION}" if category else f"{stem}{EXTENSION}"
    storage = open_storage(target)

    # 覆盖前先量一下旧内容的长度。目的不是备份（没有备份），而是让"这次覆盖掉了
    # 一篇 3000 字的记忆"这件事出现在响应里——否则调用方无从察觉自己刚刚
    # 替换了什么。读失败不阻断写入：拿不到长度只是少一条提示。
    replaced_char_count: int | None = None
    created = storage.stat(relative) is None
    if not created:
        try:
            replaced_char_count = len(storage.read_text(relative))
        except OSError as exc:
            logger.warning("覆盖前读取旧内容失败 %s/%s: %s", name, relative, exc)

    # 结尾补一个换行：Markdown 文件不以换行结尾在各类工具里都是噪音来源。
    text = content if content.endswith("\n") else content + "\n"
    try:
        stat = storage.write_text(relative, text)
    except OSError as exc:
        raise WriteError(f"写入失败：{name}/{relative}（{exc}）") from exc

    if created:
        logger.info("已创建记忆 %s/%s（%d 字符）", name, relative, len(text))
    else:
        logger.info(
            "已覆盖记忆 %s/%s（%s → %d 字符）", name, relative,
            "?" if replaced_char_count is None else replaced_char_count, len(text),
        )
    return Written(
        source=name,
        path=relative,
        absolute=target.dir.joinpath(*relative.split("/")),
        char_count=len(text),
        mtime=stat.mtime,
        created=created,
        replaced_char_count=replaced_char_count,
    )
