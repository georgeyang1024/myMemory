"""配置：config.json -> 不可变配置对象。

全部配置来自一个 JSON 文件。唯一保留的环境变量是 MEMORY_CONFIG，
用于覆盖该文件的位置；默认是 **~/.myMemory/config.json**。运行时文件（index.cache、
logs/）都放在配置文件所在的目录里，代码目录不落任何运行数据。
配置修改一律重启生效，服务运行期间不重读（见 ADR-0017）。

本模块只依赖标准库：仓库根目录的 config.py（CLI）与 run.py 也直接复用这里的校验，
避免"CLI 放行、服务启动时才报错"这种口径漂移。

所有字段在此处做类型与范围校验，越界即启动失败（fail fast），
不静默回退到默认值——静默回退会让误配以"行为诡异"而非"启动报错"的形式出现。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_ENV = "MEMORY_CONFIG"
# 运行时数据目录：配置、索引缓存、日志。不放在代码目录里，免得污染仓库。
DATA_DIR = Path.home() / ".myMemory"
CONFIG_FILENAME = "config.json"
CACHE_FILENAME = "index.cache"

STORAGE_TYPES = ("local",)

# source 名称：中英文、数字、下划线、连字符。不含 "/"——只读由 writable 字段表达，与名称无关。
_SOURCE_NAME_PATTERN = re.compile(r"^[\w\u4e00-\u9fff-]{1,64}$")

# 分类名与文件名（不含扩展名）共用的白名单：
# 中英文、数字、空格、下划线、连字符、中文与全角标点，加上常用半角符号。
# 刻意不含 \ / : * ? " < > | ——它们要么是路径分隔符，要么在 Windows 上不能出现在文件名里。
# "." 被放行（v1.2），但 ".." 与首尾的 "." 另行禁止，因此仍无法表达上跳或隐藏文件。
_NAME_CHARS = r"[\w\u4e00-\u9fff \u3000-\u303f\uff00-\uffef()\[\]{}.,&#+@!'=~%-]"
_CATEGORY_PATTERN = re.compile(rf"^{_NAME_CHARS}{{1,64}}$")
_FILENAME_PATTERN = re.compile(rf"^{_NAME_CHARS}{{1,120}}$")

# Windows 保留设备名：带不带扩展名都不能用作文件名（CON.md 同样不行）。
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

DEFAULTS: dict[str, Any] = {
    # 默认只监听本机：免鉴权 + 可写，服务默认不应暴露到网络。
    # 需要局域网访问时在 config.json 里显式设 "host": "0.0.0.0"。
    "host": "127.0.0.1",
    "port": 7083,
    "poll_interval": 600,
    "extensions": [".md", ".txt"],
    "chunk_size": 800,
    "chunk_overlap": 120,
    "max_results": 20,
    "snippet_chars": 1200,
    "max_doc_chars": 40_000,
    "max_create_chars": 100_000,
    # 常驻内存的全文最多缓存多少篇，超出按 LRU 丢弃（需要时再从磁盘读）；0 表示不限。
    # 所有文档照常进索引、照常可检索，这里只限制全文缓存。
    "max_cached_docs": 1000,
    # 领域术语词表（可选）：型号、协议名这类 jieba 切不开的标识符。
    # 分词时保持为一个词，且长字母数字串会额外发出其包含的术语，
    # 让"用系列名检索完整型号"命中。改动会作废索引缓存。
    "domain_terms": [],
    # AI 能否删除记忆（delete 工具，以及 merge 并入后删除源文件）。
    # 默认 False：删除不可备份不可恢复，关闭时这两个工具对 AI 彻底隐藏，
    # 要开启需人工改配置并重启——这是心智上的断路器，不是运行时开关。
    "allow_mcp_delete": False,
    # 多人共用形态（docs/adr/0028 ~ 0031）：store_dir 必填、admins 可选
    # （缺省时默认 ["admin"]）、guest_writable 默认 false（访客对公共 source
    # 一律只读）。enabled 是显式开关，默认 false：multi_user 块可以预先配好
    # 而不启用——只有 enabled = true 才进入多人共用形态。
    "multi_user": None,
    # BM25 之外的打分调整，全局一份，source 可按字段覆盖
    # （docs/adr/0027-search-scoring-adjustments.md）。
    "scoring": {
        "recency_window_days": 30,
        "recency_bonus": 10,
        "path_match_bonus": 5,
        "strip_wikilinks": True,
        # 历史文档降分（docs/adr/0033）：相对路径含任一关键字时扣分，默认关闭。
        "historical_penalty": 0,
        "historical_keywords": [],
    },
}

# 字段 -> (最小值, 最大值)
_INT_RANGES: dict[str, tuple[int, int]] = {
    "port": (1, 65_535),
    "poll_interval": (0, 86_400),
    "chunk_size": (100, 10_000),
    "chunk_overlap": (0, 9_999),
    "max_results": (1, 100),
    "snippet_chars": (100, 20_000),
    "max_doc_chars": (1_000, 1_000_000),
    "max_create_chars": (1_000, 1_000_000),
    "max_cached_docs": (0, 10_000_000),
}

_KNOWN_KEYS = frozenset(DEFAULTS) | {"sources"}
_SOURCE_KEYS = frozenset({"name", "dir", "type", "writable", "description", "scoring"})


class ConfigError(ValueError):
    """配置非法。启动阶段抛出，附带可读的修复提示。"""


def is_valid_name_part(name: str, pattern: re.Pattern[str]) -> bool:
    """分类名 / 文件名主干是否合法：白名单 + 首尾与 .. + 保留名。"""
    if not pattern.match(name):
        return False
    if name[0] in ". " or name[-1] in ". ":
        return False
    if ".." in name:
        return False
    if name.split(".", 1)[0].strip().upper() in _RESERVED_NAMES:
        return False
    return True


def validate_source_name(name: Any) -> str:
    """返回合法的名称，否则抛 ConfigError。"""
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("source 名称不能为空")
    name = name.strip()
    if not _SOURCE_NAME_PATTERN.match(name):
        raise ConfigError(
            f"source 名称非法：{name!r}。只允许中英文、数字、下划线与连字符，长度 1-64，"
            f"不能含 /（只读请用 \"writable\": false 表达）"
        )
    return name


def _dir_key(path: Path) -> str:
    """用于重叠判定的规范化路径：Windows 上不区分大小写。"""
    return os.path.normcase(str(path))


def _contains(outer: Path, inner: Path) -> bool:
    """outer 是否等于或包含 inner。"""
    outer_key, inner_key = _dir_key(outer), _dir_key(inner)
    if outer_key == inner_key:
        return True
    return inner_key.startswith(outer_key.rstrip(os.sep) + os.sep)


def _real_dir(directory: Path) -> Path:
    """**仅用于重叠判定**的路径：能解析真实路径时解析（含符号链接），访问不到时按原样的绝对路径。

    掉盘时解析不了真实路径，此时仍按配置里写的路径比较（需求 §3.1）。
    访问文件与判定掉盘一律用配置里写的路径（Source.dir）：解析会把 Z:\\ 换成 UNC、
    把 subst 盘换成底层目录，那就不再是"写什么用什么"，盘根判定也会失真。
    """
    try:
        if directory.exists():
            return directory.resolve()
    except OSError:
        pass
    return Path(os.path.abspath(directory))


@dataclass(frozen=True, slots=True)
class Scoring:
    """一个 source 的有效打分调整（全局值与 source 覆盖按字段合并后的结果）。

    recency_*、path_match_bonus 在查询时叠加到 BM25 分上；strip_wikilinks 在建索引时生效；
    historical_* 在查询时按路径命中扣分（docs/adr/0033）。historical_keywords 存储为
    strip + lower + 去重后的 tuple，匹配端不再转小写。
    """

    recency_window_days: int = DEFAULTS["scoring"]["recency_window_days"]
    recency_bonus: float = DEFAULTS["scoring"]["recency_bonus"]
    path_match_bonus: float = DEFAULTS["scoring"]["path_match_bonus"]
    strip_wikilinks: bool = DEFAULTS["scoring"]["strip_wikilinks"]
    historical_penalty: float = DEFAULTS["scoring"]["historical_penalty"]
    historical_keywords: tuple[str, ...] = tuple(DEFAULTS["scoring"]["historical_keywords"])


def parse_scoring(raw: Any, base: Scoring, where: str = "scoring") -> Scoring:
    """校验一个 scoring 对象，并在 base 之上按字段覆盖。raw 省略（None）即原样继承 base。

    where 用于报错定位，如 "scoring" 或 "source a 的 scoring"。
    """
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} 必须是对象")
    unknown = set(raw) - set(DEFAULTS["scoring"])
    if unknown:
        raise ConfigError(f"{where} 含未知字段：{', '.join(sorted(unknown))}")
    for key, value in raw.items():
        if key == "strip_wikilinks":
            ok = isinstance(value, bool)
            expect = "true 或 false"
        elif key == "recency_window_days":
            ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
            expect = "非负整数"
        elif key == "historical_keywords":
            ok = (isinstance(value, list) and len(value) <= 100
                  and all(isinstance(kw, str) and kw.strip() for kw in value))
            expect = "非空字符串数组（≤100 条）"
        else:
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
            expect = "非负数"
        if not ok:
            raise ConfigError(f"{where}.{key} 必须是{expect}，当前值：{value!r}")
    fields = dict(raw)
    keywords = fields.get("historical_keywords")
    if keywords is not None:
        fields["historical_keywords"] = tuple(dict.fromkeys(
            kw.strip().lower() for kw in keywords))
    return dataclasses.replace(base, **fields)


@dataclass(frozen=True, slots=True)
class Source:
    """一个有名字的记忆来源：一个 source 恰好对应一个目录。"""

    name: str
    dir: Path
    type: str = "local"
    writable: bool = True          # 配置里的可写标记（省略即 true）
    description: str = ""
    scoring: Scoring = field(default_factory=Scoring)  # 有效值，不写回配置文件

    @property
    def can_write(self) -> bool:
        """按配置能否写入：配置可写且为 local 存储。非 local 一律先按只读（需求 §3.4）。

        对外输出的 writable 还要叠加当前可用性，见 server.py。
        """
        return self.writable and self.type == "local"

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {"name": self.name, "dir": str(self.dir)}
        if self.type != "local":
            body["type"] = self.type
        body["writable"] = self.writable
        if self.description:
            body["description"] = self.description
        return body


def parse_sources(raw: Any, *, allow_empty: bool = False) -> tuple[Source, ...]:
    """校验 sources 列表：名称、重名、类型、字段、不重叠不嵌套。

    **不检查目录是否存在**：目录访问不到（掉盘或被删除）时服务照常启动，只给警告
    （需求 §8）。路径拼错由 CLI 添加时校验目录存在来拦截。
    allow_empty 仅在 multi_user 启用（enabled: true）时由调用方传入：
    团队形态可以没有公共 source，语料 = 个人根目录派生的个人 source。
    """
    if raw is not None and not isinstance(raw, list):
        raise ConfigError("sources 必须是非空数组，至少配置一个 source")
    if not raw and not allow_empty:
        raise ConfigError(
            "sources 必须是非空数组，至少配置一个 source"
            + ("（团队共用形态没有公共 source 时语料来自个人根目录的子文件夹）"
               if raw is not None else "")
        )
    if not raw:
        return ()

    sources: list[Source] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"sources[{i}] 必须是对象")
        unknown = set(item) - _SOURCE_KEYS
        if unknown:
            raise ConfigError(f"sources[{i}] 含未知字段：{', '.join(sorted(unknown))}")
        name = validate_source_name(item.get("name"))
        if name in seen:
            raise ConfigError(f"source 重名：{name}")
        seen.add(name)

        kind = item.get("type", "local")
        if kind not in STORAGE_TYPES:
            raise ConfigError(
                f"source {name} 的存储类型 {kind!r} 尚未支持，当前只支持：{', '.join(STORAGE_TYPES)}"
            )

        raw_dir = item.get("dir")
        if not isinstance(raw_dir, str) or not raw_dir.strip():
            raise ConfigError(f"source {name} 缺少 dir")
        # 写什么用什么：只补成绝对路径，不解析盘符映射与符号链接。
        directory = Path(os.path.abspath(Path(raw_dir.strip()).expanduser()))

        writable = item.get("writable", True)
        if not isinstance(writable, bool):
            raise ConfigError(f"source {name} 的 writable 必须是 true 或 false")

        description = item.get("description", "")
        if not isinstance(description, str):
            raise ConfigError(f"source {name} 的 description 必须是字符串")

        sources.append(Source(name=name, dir=directory, type=kind, writable=writable,
                                    description=description.strip()))

    real = [_real_dir(ws.dir) for ws in sources]
    for i, a in enumerate(sources):
        for j in range(i + 1, len(sources)):
            b = sources[j]
            if _contains(real[i], real[j]) or _contains(real[j], real[i]):
                raise ConfigError(
                    f"source {a.name}（{a.dir}）与 {b.name}（{b.dir}）的目录重叠或嵌套"
                )
    return tuple(sources)


# --- 多人共用（docs/adr/0028 ~ 0031） ----------------------------------------

# editor 的保留取值（ADR-0031）：不得用作个人目录名与管理员名。
# 比较大小写不敏感（casefold）——Windows 目录名不区分大小写。
EDITOR_RESERVED_NAMES = frozenset({"agent", "scan", "guest"})

_USER_MAX_LEN = 64


def _has_control_char(name: str) -> bool:
    """传输层硬约束：控制字符（CR/LF/NUL 等）会让注入的请求头被拆行/截断。"""
    return any(ord(ch) < 0x20 or ch == "\x7f" for ch in name)


def _is_editor_reserved(name: str) -> bool:
    return name.casefold() in EDITOR_RESERVED_NAMES


@dataclass(frozen=True, slots=True)
class MultiUserConfig:
    """多人共用形态的配置子项。enabled = true 才进入本形态；
    不配置或开关关闭（enabled 默认 false）= 单机形态，行为与从前一致。"""

    store_dir: Path
    admins: tuple[str, ...] = ()
    guest_writable: bool = False


def parse_multi_user(raw: Any, sources: tuple[Source, ...]) -> MultiUserConfig:
    """校验 multi_user 子项；未配置或开关关闭（None / enabled=false）返回 None，即单机形态。

    - enabled：布尔开关，**默认 false**——multi_user 块可预先写好而不启用，
      只有显式 enabled = true 才进入多人共用形态。
    - store_dir 必填：字符串、非空，补绝对路径，不解析符号链接（与 source dir 同口径）。
    - **不要求目录存在**（与 sources 一致，需求 §8）：目录不在 = 0 个用户，启动警告，服务照常。
    - admins：字符串数组，strip 后入表、去重、空串剔除并警告；不做 source 命名
      白名单校验（管理员是纯授权身份，ADR-0030）。硬约束三条都是传输层底线
      （管理员名要经 `?user=` 逐字匹配后注入请求头）：无控制字符、长度 ≤64、
      不得为编辑者保留字（casefold）。**未显式给出 admins 时默认 ["admin"]**；
      显式给出（含空数组）则完全按给定值。
    - guest_writable：布尔，默认 false（访客对公共 source 一律只读，ADR-0030 修订）。
    - 嵌套禁令扩展：store_dir 与任何公共 source 目录不得互相包含。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("multi_user 必须是对象")
    unknown = set(raw) - {"store_dir", "admins", "guest_writable", "enabled"}
    if unknown:
        raise ConfigError(f"multi_user 含未知字段：{', '.join(sorted(unknown))}")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("multi_user.enabled 必须是 true 或 false")
    if not enabled:
        logger.info("multi_user 已配置但 enabled = false，多人共用未启用（单机形态）")
        return None

    raw_root = raw.get("store_dir")
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise ConfigError("multi_user 缺少 store_dir（个人根目录，多人共用形态必填）")
    store_dir = Path(os.path.abspath(Path(raw_root.strip()).expanduser()))

    if not store_dir.is_dir():
        logger.warning(
            "multi_user.store_dir 当前不存在：%s（0 个用户；目录建好后即刻生效，无需重启）",
            store_dir,
        )

    if "admins" in raw:
        raw_admins = raw["admins"]
    else:
        # 默认管理员是 admin（未显式给出名单时）；显式给出（含空数组）则完全按给定值。
        raw_admins = ["admin"]
    if not isinstance(raw_admins, list) or not all(isinstance(a, str) for a in raw_admins):
        raise ConfigError("multi_user.admins 必须是字符串数组")
    admins: list[str] = []
    for item in raw_admins:
        name = item.strip()
        if not name:
            logger.warning("multi_user.admins 含空白名字，已剔除")
            continue
        if _has_control_char(name):
            raise ConfigError(f"multi_user.admins 含控制字符，非法：{item!r}")
        if len(name) > _USER_MAX_LEN:
            raise ConfigError(
                f"multi_user.admins 名字过长（上限 {_USER_MAX_LEN} 字符）：{name[:20]}…"
            )
        if _is_editor_reserved(name):
            raise ConfigError(
                f"multi_user.admins 不得使用编辑者保留字（不区分大小写）：{name!r}。"
                f"保留字：{', '.join(sorted(EDITOR_RESERVED_NAMES))}"
            )
        if name in admins:
            logger.warning("multi_user.admins 含重复名字：%s，已去重", name)
            continue
        admins.append(name)

    guest_writable = raw.get("guest_writable", False)
    if not isinstance(guest_writable, bool):
        raise ConfigError("multi_user.guest_writable 必须是 true 或 false")

    # 嵌套禁令：store_dir 与公共 source 目录不得互相包含（复用 parse_sources 的口径）。
    real_root = _real_dir(store_dir)
    for ws in sources:
        real_src = _real_dir(ws.dir)
        if _contains(real_root, real_src) or _contains(real_src, real_root):
            raise ConfigError(
                f"source {ws.name}（{ws.dir}）与个人根目录（{store_dir}）的目录重叠或嵌套"
            )

    return MultiUserConfig(store_dir=store_dir,
                           admins=tuple(admins), guest_writable=guest_writable)


def _derive_personal(store_dir: Path,
                     static_names: tuple[Source, ...]) -> tuple[tuple[Source, ...], list[dict[str, str]]]:
    """枚举个人根目录的一级子目录，派生个人 source 并过滤非法名字。

    返回 (派生的 source 全集, 被跳过的 [{name, reason}])。跳过只警告不报错，
    不挡启动（与"目录不存在不阻止启动"的宽容语义一致，需求 §8）。
    过滤规则（docs/adr/0029 / SPEC §3.3）：
    a. 目录名不匹配 source 名白名单（含空格、点等）；
    b. 与公共 source 名重名（casefold——Windows 目录名不区分大小写）；
    c. 编辑者保留字 agent/scan/guest（casefold，ADR-0031）；
    d. 与已派生的个人 source 重名（casefold；Linux 上 Alice/alice 可并存，留其一）。
    """
    derived: list[Source] = []
    skipped: list[dict[str, str]] = []
    taken: set[str] = {ws.name.casefold() for ws in static_names}
    try:
        entries = list(os.scandir(store_dir))
    except OSError as exc:
        logger.warning("枚举个人根目录失败 %s：%s（按 0 个用户处理）", store_dir, exc)
        return (), []
    for entry in entries:
        name = entry.name
        if not entry.is_dir():
            continue
        if not _SOURCE_NAME_PATTERN.match(name):
            skipped.append({"name": name, "reason": "invalid_name"})
            logger.warning(
                "个人目录 %s 的名字不合法（只允许中英文、数字、下划线与连字符，长度 1-%d），已跳过",
                name, _USER_MAX_LEN,
            )
        elif _is_editor_reserved(name):
            skipped.append({"name": name, "reason": "reserved"})
            logger.warning(
                "个人目录 %s 与编辑者保留字冲突（%s），已跳过，请改名",
                name, ", ".join(sorted(EDITOR_RESERVED_NAMES)),
            )
        elif name.casefold() in taken:
            skipped.append({"name": name, "reason": "name_conflict"})
            logger.warning("个人目录 %s 与现有 source 重名，已跳过，请改名", name)
        else:
            taken.add(name.casefold())
            derived.append(Source(
                name=name,
                dir=store_dir / name,
                type="local",
                writable=True,
                description=f"个人记忆源（经 ?user={name} 路由）",
            ))
    return tuple(derived), skipped


def personal_sources_report(store_dir: Path,
                            static_names: tuple[Source, ...]) -> tuple[tuple[Source, ...], list[dict[str, str]]]:
    """派生全集与跳过明细（/health 与 CLI `multi-user user list` 用）。"""
    return _derive_personal(store_dir, static_names)


def personal_sources(store_dir: Path,
                     static_names: tuple[Source, ...]) -> tuple[Source, ...]:
    """个人根目录下的一级子目录派生的个人 source 全集（跳过项只记日志）。"""
    return _derive_personal(store_dir, static_names)[0]


def effective_sources(config: "Config") -> tuple[Source, ...]:
    """公共 + 全部派生个人 source：索引层（构建/刷新/缓存）的输入。

    每次调用当场枚举（ADR-0029 决策二）：新用户目录的出现/消失/改名由下一轮
    刷新自然消化，无需重启、无需指纹。
    """
    if config.multi_user is None:
        return config.sources
    return config.sources + personal_sources(
        config.multi_user.store_dir, config.sources)


def find_user(config: "Config", name: str) -> Source | None:
    """名字逐字相等的个人 source；无 → None（strip 由调用方统一做，§3.4）。"""
    if config.multi_user is None:
        return None
    for src in personal_sources(config.multi_user.store_dir, config.sources):
        if src.name == name:
            return src
    return None


def is_admin(config: "Config", name: str) -> bool:
    """名字与管理员名单逐字相等（strip 由调用方统一做，§3.4）。

    无目录要求、无命名白名单校验——校验在配置加载时做（parse_multi_user）。
    """
    if config.multi_user is None:
        return False
    return name in config.multi_user.admins


def resolve_config_path(environ: dict[str, str] | None = None) -> Path:
    """配置文件位置：MEMORY_CONFIG > ~/.myMemory/config.json。"""
    environ = os.environ if environ is None else environ
    raw = (environ.get(CONFIG_ENV) or "").strip()
    path = Path(raw).expanduser() if raw else DATA_DIR / CONFIG_FILENAME
    return path.resolve()


def bootstrap_config_data() -> dict[str, Any]:
    """仅含默认值的空配置：不含任何 source。

    记忆目录属于用户数据的位置，代码无从推断（ADR-0025）；
    source 一律由 `config.py source add` 显式添加——它会校验目录存在并补上本函数的默认值。
    """
    data: dict[str, Any] = {k: DEFAULTS[k] for k in ("host", "port", "poll_interval")}
    for key in ("extensions", "chunk_size", "chunk_overlap", "max_results",
                "snippet_chars", "max_doc_chars", "max_create_chars", "max_cached_docs"):
        data[key] = DEFAULTS[key]
    data["scoring"] = dict(DEFAULTS["scoring"])
    return data


# 首次建档时的默认 source：目录必须由使用者给出（交互输入或 source add），
# 这里只定名称、描述与可写性（ADR-0025）。
DEFAULT_SOURCE_NAME = "memory"
DEFAULT_SOURCE_DESCRIPTION = "默认记忆源"


def bootstrap_config_data_with_source(dir_str: str) -> dict[str, Any]:
    """首次建档：默认值 + 一个可写 source memory（描述"默认记忆源"）指向 dir_str。

    run.py 首次启动交互建档与 `config.py source add memory` 共用这一个入口，
    保证两边写出的配置一致。
    """
    data = bootstrap_config_data()
    data["sources"] = [
        {"name": DEFAULT_SOURCE_NAME, "dir": dir_str, "writable": True,
         "description": DEFAULT_SOURCE_DESCRIPTION}
    ]
    return data


def read_config_data(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}：{exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 {path} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件 {path} 顶层必须是对象")
    return data


def write_json_atomic(path: Path, data: Any) -> None:
    """先写临时文件再替换，崩溃时不会留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _int_field(data: dict[str, Any], key: str) -> int:
    value = data.get(key, DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是整数，当前值：{value!r}")
    low, high = _INT_RANGES[key]
    if not (low <= value <= high):
        raise ConfigError(f"{key} 必须在 [{low}, {high}] 范围内，当前值：{value}")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    """服务的全部配置。构造后不可变。"""

    config_file: Path
    sources: tuple[Source, ...]
    extensions: tuple[str, ...]

    host: str
    port: int

    poll_interval: int
    chunk_size: int
    chunk_overlap: int

    max_results: int
    default_results: int
    snippet_chars: int
    max_doc_chars: int
    max_create_chars: int
    max_cached_docs: int = field(default=1000)
    max_query_chars: int = field(default=500)
    domain_terms: tuple[str, ...] = field(default=())
    allow_mcp_delete: bool = field(default=False)
    multi_user: MultiUserConfig | None = field(default=None)
    scoring: Scoring = field(default_factory=Scoring)

    @classmethod
    def load(cls, path: Path | None = None, *, create_default: bool = True) -> "Config":
        """读取配置文件；不存在时报错并给出修复指引（ADR-0025：不猜测记忆目录）。"""
        path = resolve_config_path() if path is None else Path(path).resolve()
        if not path.exists():
            if not create_default:
                raise ConfigError(f"配置文件不存在：{path}")
            raise ConfigError(
                f"配置文件不存在：{path}\n"
                f"记忆目录无法代为猜测，请先显式建档：\n"
                f"  python config.py source add memory --dir <记忆目录> --writable"
            )
        return cls.from_data(read_config_data(path), config_file=path)

    @classmethod
    def from_data(cls, data: dict[str, Any], *, config_file: Path) -> "Config":
        unknown = set(data) - _KNOWN_KEYS
        if unknown:
            raise ConfigError(f"配置含未知字段：{', '.join(sorted(unknown))}")

        # 先看 multi_user 开关再校验 sources：团队形态（enabled: true）允许
        # 没有公共 source——语料 = 个人根目录派生的个人 source。
        raw_multi_user = data.get("multi_user")
        team_mode = isinstance(raw_multi_user, dict) and raw_multi_user.get("enabled", False) is True

        sources = parse_sources(data.get("sources"), allow_empty=team_mode)

        raw_ext = data.get("extensions", DEFAULTS["extensions"])
        if not isinstance(raw_ext, list) or not raw_ext or not all(
            isinstance(e, str) and e.strip() for e in raw_ext
        ):
            raise ConfigError("extensions 必须是非空字符串数组，如 [\".md\", \".txt\"]")
        extensions = tuple(
            e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}"
            for e in raw_ext
        )

        raw_terms = data.get("domain_terms", DEFAULTS["domain_terms"])
        if not isinstance(raw_terms, list) or not all(
            isinstance(t, str) and t.strip() for t in raw_terms
        ):
            raise ConfigError(
                "domain_terms 必须是非空字符串数组（可为空），如 [\"GATT\", \"AES-GCM\"]"
            )
        if len(raw_terms) > 1000:
            raise ConfigError(f"domain_terms 过多：{len(raw_terms)} 条，上限 1000")
        domain_terms = tuple(dict.fromkeys(t.strip() for t in raw_terms))

        host = data.get("host", DEFAULTS["host"])
        if not isinstance(host, str) or not host.strip():
            raise ConfigError("host 必须是非空字符串")

        allow_mcp_delete = data.get("allow_mcp_delete", DEFAULTS["allow_mcp_delete"])
        if not isinstance(allow_mcp_delete, bool):
            raise ConfigError("allow_mcp_delete 必须是 true 或 false")

        multi_user = parse_multi_user(raw_multi_user, sources)

        # 全局值在默认值之上覆盖，source 再在全局值之上按字段覆盖。
        scoring = parse_scoring(data.get("scoring"), Scoring())
        sources = tuple(
            dataclasses.replace(src, scoring=parse_scoring(
                item.get("scoring"), scoring, where=f"source {src.name} 的 scoring"))
            for src, item in zip(sources, data.get("sources") or [])
        )

        chunk_size = _int_field(data, "chunk_size")
        chunk_overlap = _int_field(data, "chunk_overlap")
        if chunk_overlap >= chunk_size:
            raise ConfigError(
                f"chunk_overlap({chunk_overlap}) 必须小于 chunk_size({chunk_size})，否则切块无法前进"
            )

        return cls(
            config_file=Path(config_file),
            sources=sources,
            extensions=extensions,
            host=host.strip(),
            port=_int_field(data, "port"),
            poll_interval=_int_field(data, "poll_interval"),
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            max_results=_int_field(data, "max_results"),
            default_results=10,
            snippet_chars=_int_field(data, "snippet_chars"),
            max_doc_chars=_int_field(data, "max_doc_chars"),
            max_create_chars=_int_field(data, "max_create_chars"),
            max_cached_docs=_int_field(data, "max_cached_docs"),
            domain_terms=domain_terms,
            allow_mcp_delete=allow_mcp_delete,
            multi_user=multi_user,
            scoring=scoring,
        )

    @staticmethod
    def is_valid_category(name: str) -> bool:
        """分类目录名是否合法（非空时）。空分类由调用方单独处理。"""
        return is_valid_name_part(name, _CATEGORY_PATTERN)

    @staticmethod
    def is_valid_filename(stem: str) -> bool:
        """文件名主干（不含 .md）是否合法。"""
        return is_valid_name_part(stem, _FILENAME_PATTERN)

    @property
    def cache_file(self) -> Path:
        """索引缓存与 config.json 同目录（ADR-0019）。"""
        return self.config_file.parent / CACHE_FILENAME

    @property
    def chunk_step(self) -> int:
        return self.chunk_size - self.chunk_overlap

    def source(self, name: str) -> Source | None:
        for ws in self.sources:
            if ws.name == name:
                return ws
        return None

    @property
    def writable_sources(self) -> list[str]:
        """按配置可写的 source（不含当前可用性，那由运行时判断）。"""
        return [ws.name for ws in self.sources if ws.can_write]

    def describe(self) -> dict[str, object]:
        """用于启动日志与 /health 的可序列化摘要。不含 source 目录之外的敏感信息。"""
        return {
            "config_file": str(self.config_file),
            "extensions": list(self.extensions),
            "host": self.host,
            "port": self.port,
            "poll_interval": self.poll_interval,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "max_cached_docs": self.max_cached_docs,
            "domain_terms": len(self.domain_terms),
            "allow_mcp_delete": self.allow_mcp_delete,
            # 全局有效值，以及与之不同的 source 有效值
            "scoring": {
                "default": dataclasses.asdict(self.scoring),
                "sources": {src.name: dataclasses.asdict(src.scoring) for src in self.sources
                            if src.scoring != self.scoring},
            },
        }
