#!/usr/bin/env python3
"""myMemory 独立启动器：自建虚拟环境、自动安装依赖、启动服务。

只依赖 Python 3.10+ 标准库，本身不需要任何第三方包。
只在当前系统上运行（见 docs/adr/0021-single-platform.md），虚拟环境为 mcp/.venv。

这是服务的启动入口；配置管理见 config.py。曾经有 run.sh / run.ps1 / start.sh / start.ps1
四个包装脚本，已全部删除：同一件事分散在五个文件里，四份要同步维护的平台差异，
而这些差异本来就能在 Python 里判断。

用法：
    python3 run.py                 前台启动服务（Ctrl-C 停止）
    python3 run.py --init          建档/修复配置 + 装齐依赖：缺失建档、损坏重建（留 .bak）、完好报告
    python3 run.py --background    后台启动，就绪后打印端口与 PID 再退出
    python3 run.py --status        查看后台服务状态
    python3 run.py --stop          停止后台服务
    python3 run.py --restart       重启后台服务
    python3 run.py --logs          跟踪后台日志（Ctrl-C 只退出跟踪）
    python3 run.py --check         只做配置与索引自检，不监听端口
    python3 run.py --stdio         以 stdio 传输启动（供 MCP 客户端自己拉起）
    python3 run.py --reinstall     强制重装依赖
    python3 run.py --recreate      删除并重建虚拟环境（环境坏掉时用这个）
    python3 run.py --no-venv       用当前解释器直接跑（不建 venv）
    python3 run.py --find-links ./wheels --offline    离线安装
    python3 run.py --index-url https://mirrors.aliyun.com/pypi/simple/   走镜像源
    python3 run.py --bundle ./wheels                  在联网机器上打离线依赖包

Windows 上用 `py -3 run.py ...`，其余完全相同。

配置在 ~/.myMemory/config.json；不存在时首次启动会交互询问记忆目录建档
（stdio / 非终端环境下无法提问，启动失败并提示"未指定记忆存储"，见 docs/adr/0025）。
日志在 ~/.myMemory/logs/。
唯一的环境变量 MEMORY_CONFIG 可以指定别的配置文件：
    MEMORY_CONFIG=/path/to/config.json python3 run.py
source 与刷新周期用 config.py 管理；配置修改重启生效。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MIN_PYTHON = (3, 10)

# 依赖是否可用，以"能否在目标解释器里导入"为准，不以指纹文件为准。
PROBE_IMPORTS = ("uvicorn", "mcp", "jieba", "rank_bm25")

if sys.version_info < MIN_PYTHON:
    # 这里不能用 f-string 之外的新语法，保证在老解释器上也能打印出这条提示。
    sys.exit(
        "myMemory 需要 Python %d.%d 或更高版本，当前为 %d.%d。\n"
        "代码使用了 3.10 引入的 dataclass(slots=True)。"
        % (MIN_PYTHON[0], MIN_PYTHON[1], sys.version_info[0], sys.version_info[1])
    )

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE / "src"
REQUIREMENTS = HERE / "requirements.txt"
STAMP_NAME = ".myMemory-deps"


IS_WINDOWS = os.name == "nt"

# Windows 进程创建标志。非 Windows 的 subprocess 模块不定义这些名字，
# 因此带常量兜底取值——这样在 Linux 上也能用单元测试覆盖 Windows 分支。
#
# CREATE_NO_WINDOW：控制台程序，但**不给它分配控制台窗口**。
# 注意不能用 DETACHED_PROCESS：那只是"不继承父进程的控制台"，
# 而 python.exe 是控制台程序，Windows 会另给它**新建一个控制台**——
# 屏幕上就多出一个黑窗（实测报告）。这两个标志互斥，只能二选一。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

DEFAULT_PORT = 7083
# 后台启动时等待"就绪"的上限（秒）。首次启动要建虚拟环境、装依赖、再建索引，
# 可能要几分钟；超时不等于失败，只是不再等，所以提示里让人去看日志。
READY_TIMEOUT = int(os.environ.get("MYMEMORY_READY_TIMEOUT", "180"))


def default_venv_dir() -> Path:
    """默认虚拟环境路径：mcp/.venv。

    本服务只在当前系统上运行（见 docs/adr/0021-single-platform.md），
    不再为"同一目录被两个操作系统共用"按平台分目录。
    """
    return HERE / ".venv"


def log(message: str) -> None:
    print(f"[myMemory] {message}", flush=True)


def venv_python(venv_dir: Path) -> Path:
    """虚拟环境中解释器的位置。Windows 与 POSIX 布局不同。"""
    if IS_WINDOWS:
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def deps_importable(python: Path) -> bool:
    """在目标解释器里实际导入一遍依赖。

    这是判断"环境是否可用"的唯一可靠依据。仅凭指纹文件会漏掉几类真实情况：
      - 用户手工创建过 .venv，或从别处拷贝了带指纹的目录
      - 依赖被外部操作卸载或损坏
    """
    probe = "import " + ", ".join(PROBE_IMPORTS)
    try:
        result = subprocess.run(
            [str(python), "-c", probe],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def requirements_digest() -> str:
    """依赖清单的指纹，用于判断是否需要重新安装。

    锁死版本号的 requirements.txt 一旦变动，已有 venv 就是过期的。
    没有这个判断，改了依赖却复用旧 venv 会导致难以定位的版本错配。
    """
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def ensure_venv(venv_dir: Path, *, recreate: bool = False) -> Path:
    python = venv_python(venv_dir)

    if python.exists() and not recreate:
        return python

    clear = recreate and venv_dir.exists()
    if clear:
        log(f"重建虚拟环境 {venv_dir}（--recreate）")
    elif venv_dir.exists():
        log(f"虚拟环境目录残缺，就地补建 {venv_dir}")
    else:
        log(f"创建虚拟环境 {venv_dir}")
    try:
        import venv as venv_module
    except ImportError:
        sys.exit(
            "当前 Python 缺少 venv 模块。\n"
            "Debian/Ubuntu 上通常需要先安装：sudo apt install python3-venv"
        )
    venv_module.EnvBuilder(with_pip=True, clear=clear).create(str(venv_dir))

    python = venv_python(venv_dir)
    if not python.exists():
        sys.exit(f"虚拟环境创建失败：未找到 {python}")
    return python


def install_dependencies(python: Path, args: argparse.Namespace) -> None:
    stamp = python.parent.parent / STAMP_NAME
    digest = requirements_digest()

    stamp_matches = (
        stamp.exists() and stamp.read_text(encoding="utf-8").strip() == digest
    )

    if not args.reinstall and stamp_matches and deps_importable(python):
        return

    if args.reinstall:
        log("安装依赖（--reinstall）")
    elif stamp_matches:
        # 指纹对得上但导入不了：环境是坏的，不能因为指纹匹配就跳过安装。
        log("依赖缺失或损坏，重新安装")
    elif stamp.exists():
        log("requirements.txt 已变更，重新安装依赖")
    else:
        log("安装依赖")

    pip_base = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    if args.index_url:
        pip_base += ["--index-url", args.index_url]
    if args.find_links:
        pip_base += ["--find-links", str(Path(args.find_links).resolve())]
    if args.offline:
        pip_base += ["--no-index"]
        if not args.find_links:
            sys.exit("--offline 需要同时指定 --find-links 指向本地 wheel 目录")

    # pip 自升级失败不应中断安装：离线环境下它必然失败，但不影响后续。
    subprocess.run(
        pip_base + ["--upgrade", "pip"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )

    result = subprocess.run(pip_base + ["-r", str(REQUIREMENTS)])
    if result.returncode != 0:
        hints = [f"依赖安装失败（退出码 {result.returncode}）。请看上方 pip 的输出定位原因。"]
        if python == Path(sys.executable):
            hints.append(
                "  · 若提示 externally-managed-environment（PEP 668）：这是系统 Python 的"
                "保护机制。\n"
                "    去掉 --no-venv，让 run.py 自建虚拟环境即可绕开。"
            )
        hints.append(
            "  · 若为网络问题，可改用镜像源或离线安装：\n"
            "      python3 run.py --index-url https://mirrors.aliyun.com/pypi/simple/\n"
            "      python3 run.py --bundle ./wheels        # 在联网机器上打包\n"
            "      python3 run.py --find-links ./wheels --offline"
        )
        sys.exit("\n".join(hints))

    if not deps_importable(python):
        sys.exit(
            "依赖安装看似成功，但在目标解释器中仍无法导入。\n"
            f"  解释器：{python}\n"
            f"  需要的包：{', '.join(PROBE_IMPORTS)}\n"
            "请尝试删除虚拟环境目录后重试：\n"
            f"  python3 run.py --recreate"
        )

    stamp.write_text(digest, encoding="utf-8")


def bundle_wheels(python: Path, target: Path, args: argparse.Namespace) -> int:
    """在联网机器上把全部依赖打成 wheel 包，供离线机器安装。

    必须用 `pip wheel` 而不是 `pip download`：jieba 在 PyPI 上只发布
    sdist（.tar.gz），没有 wheel。`pip download` 拿到的是 sdist，
    离线安装时需要现场构建，而构建又依赖 setuptools —— 若 setuptools
    不在离线包里就会失败。`pip wheel` 在联网端就完成构建，
    产出的目录可以纯 --no-index 安装。
    """
    target = target.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    command = [str(python), "-m", "pip", "wheel", "--disable-pip-version-check",
               "-r", str(REQUIREMENTS), "-w", str(target)]
    if args.index_url:
        command += ["--index-url", args.index_url]

    log(f"打包依赖到 {target}")
    result = subprocess.run(command)
    if result.returncode != 0:
        sys.exit(f"依赖打包失败（退出码 {result.returncode}）")

    wheels = sorted(target.glob("*.whl"))
    leftovers = sorted(target.glob("*.tar.gz"))
    log(f"完成：{len(wheels)} 个 wheel")
    if leftovers:
        log(f"警告：仍有 {len(leftovers)} 个 sdist 未能构建成 wheel，离线安装可能失败")
    print()
    print("把该目录连同本仓库拷贝到离线机器后，在 mcp/ 下执行：")
    print(f"    python3 run.py --find-links {target.name} --offline")
    return 0


def config_path() -> Path:
    """配置文件位置，与服务同一口径：MEMORY_CONFIG > ~/.myMemory/config.json。

    解析逻辑复用 src/config.py（只依赖标准库，不需要虚拟环境），
    免得启动器与服务对"配置在哪"各说各话。
    """
    sys.path.insert(0, str(SRC_DIR))
    try:
        from config import resolve_config_path
    finally:
        sys.path.pop(0)
    return resolve_config_path()


def load_config_module():
    sys.path.insert(0, str(SRC_DIR))
    try:
        import config as module
    finally:
        sys.path.pop(0)
    return module


def ask_memory_dir(cm) -> Path:
    """询问记忆目录：终端里逐字输入，回车用默认 ~/.myMemory/memory。

    目录不存在会自动创建。非终端环境（--init 显式要求的重建场景）不提问，
    直接用默认目录并打印说明——建档只发生在使用者的明确动作下，默认目录
    是被声明过的选择，不再是猜测。
    """
    default_dir = cm.DATA_DIR / "memory"
    if sys.stdin.isatty() and sys.stdout.isatty():
        print("指定记忆目录（支持本地磁盘、挂载盘、Windows 的 SMB 写法 \\\\192.168.x.x\\共享，不存在会自动创建）")
        raw = input(f"记忆目录 [{default_dir}]：").strip()
        directory = Path(raw).expanduser() if raw else default_dir
    else:
        directory = default_dir
        print(f"非终端环境，使用默认记忆目录：{directory}")
    directory = Path(os.path.abspath(directory))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_bootstrap_config(path: Path, cm, directory: Path) -> None:
    """写入首次建档配置：默认值 + 可写 source memory（描述"默认记忆源"）。"""
    data = cm.bootstrap_config_data_with_source(str(directory))
    cm.write_json_atomic(path, data)
    print(f"已写入 {path}")
    print(f"记忆目录：{directory}。以后可用 source add 添加团队、组织等更多 source。")


def ensure_config(passthrough: list[str]) -> None:
    """首次启动（配置文件不存在）时交互式建档，否则直接返回（ADR-0025）。

    - 记忆目录无法从代码位置猜测，因此在终端逐字询问；直接回车用 ~/.myMemory/memory
      （自动创建目录）。写出的 source 名固定 memory、描述"默认记忆源"、可写。
    - 交互的前提是 stdin/stdout 都是终端。stdio 传输由 MCP 客户端拉起、stdin 是管道，
      无法提问——此时启动失败，提示"未指定记忆存储"，由使用者先在终端跑一次
      run.py 或 config.py source add 建档。
    """
    path = config_path()
    if path.exists():
        return

    if "--stdio" in passthrough or not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit(
            "未指定记忆存储：配置文件不存在，且当前无法交互提问（stdio 传输或非终端环境）。\n"
            f"  请先在终端运行一次：python3 run.py（按提示输入记忆目录），\n"
            f"  或显式建档：python3 config.py source add memory --dir <记忆目录> --writable\n"
            f"  配置位置：{path}"
        )

    cm = load_config_module()
    print(f"首次启动：还没有配置文件（{path}）。")
    write_bootstrap_config(path, cm, ask_memory_dir(cm))


def setup_environment(args: argparse.Namespace) -> Path:
    """确保虚拟环境与依赖可用，返回服务解释器。正常启动与 --init 共用。

    install_dependencies 幂等（指纹匹配且依赖可导入即跳过），重复调用无害。
    """
    if not REQUIREMENTS.exists():
        sys.exit(f"未找到依赖清单：{REQUIREMENTS}")
    if not SRC_DIR.is_dir():
        sys.exit(f"未找到源码目录：{SRC_DIR}")

    if args.no_venv:
        python = Path(sys.executable)
        log(f"使用当前解释器 {python}（--no-venv）")
        if args.reinstall:
            install_dependencies(python, args)
        elif not deps_importable(python):
            # --no-venv 的语义是"环境由我自己管"，不擅自往系统解释器装包：
            # 那可能需要管理员权限，或被 PEP 668 的 externally-managed 保护拦下。
            sys.exit(
                f"当前解释器缺少依赖：{', '.join(PROBE_IMPORTS)}\n"
                f"  解释器：{python}\n"
                "可选做法：\n"
                "  1. 去掉 --no-venv，让 run.py 自建虚拟环境（推荐）\n"
                f"  2. 自行安装：{python} -m pip install -r {REQUIREMENTS}\n"
                "  3. 明确要求装到当前解释器：加上 --reinstall"
            )
    else:
        venv_arg = args.venv if args.venv is not None else str(default_venv_dir())
        venv_dir = Path(venv_arg).expanduser().resolve()
        python = ensure_venv(venv_dir, recreate=args.recreate)
        install_dependencies(python, args)
    return python


def do_init(args: argparse.Namespace) -> int:
    """--init：建档/修复配置 + 装齐依赖（ADR-0025）。

    - 配置不存在：建档（交互询问记忆目录；非终端用默认目录）。
    - 配置存在但 JSON/校验不通过：先备份为 config.json.bak（覆盖旧备份），再重建。
    - 配置完好：只校验并报告（source 列表、各目录可用性），一个字节都不改。
    - 三个分支最后都走 setup_environment：虚拟环境就绪、依赖装齐（已装则秒过）。

    显式的人工动作，处理完直接返回，不启动服务。
    """
    cm = load_config_module()
    sys.path.insert(0, str(SRC_DIR))
    try:
        from storage import DISK_OFFLINE, open_storage
    finally:
        sys.path.pop(0)

    path = config_path()
    print(f"配置文件：{path}")
    code = 0

    if not path.exists():
        print("配置文件不存在，开始建档。")
        write_bootstrap_config(path, cm, ask_memory_dir(cm))
    else:
        try:
            data = cm.read_config_data(path)
            config = cm.Config.from_data(data, config_file=path)
        except cm.ConfigError as exc:
            backup = path.with_name(path.name + ".bak")
            backup.write_bytes(path.read_bytes())
            print(f"配置文件损坏（{exc}）")
            print(f"已备份原文件到 {backup}，将重新建档。")
            write_bootstrap_config(path, cm, ask_memory_dir(cm))
        else:
            # 完好：只校验 + 报告。
            print("配置校验通过。")
            if not config.sources:
                print("警告：没有任何 source，服务起不来——先补一个：")
                print(f"  python3 config.py source add memory --dir <记忆目录> --writable")
                code = 1
            else:
                for ws in config.sources:
                    state = open_storage(ws).probe()
                    if state.available:
                        status = "可用"
                    elif state.reason == DISK_OFFLINE:
                        status = "不可用（挂载盘掉线）"
                    else:
                        status = "不可用（目录不存在）"
                    line = f"  {ws.name}（{'读写' if ws.can_write else '只读'}）→ {ws.dir} ：{status}"
                    if ws.description:
                        line += f"  # {ws.description}"
                    print(line)
                if config.source("memory") is None:
                    print("提示：配置里没有默认 source memory，可用 source add 补一个。")

    # 依赖：与正常启动同一口径，已就绪则秒过。
    python = setup_environment(args)
    log(f"依赖就绪：{python}")
    return code


# 日志与 PID 文件放在配置文件所在目录的 logs/ 下（默认 ~/.myMemory/logs），不落在代码目录。
LOG_DIR = config_path().parent / "logs"
LOG_FILE = LOG_DIR / "myMemory.log"
PID_FILE = LOG_DIR / "myMemory.pid"


def build_child_env() -> dict[str, str]:
    environ = dict(os.environ)
    # 把解析好的绝对路径钉给子进程，前台、后台与 config 始终读同一份配置。
    environ["MEMORY_CONFIG"] = str(config_path())

    existing = environ.get("PYTHONPATH")
    environ["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)

    # 让 Python 在中文 Windows 控制台上也用 UTF-8 输出。否则日志中出现
    # 记忆目录下的中文文件名时，cp936 控制台可能抛 UnicodeEncodeError。
    environ.setdefault("PYTHONUTF8", "1")
    environ.setdefault("PYTHONIOENCODING", "utf-8")
    return environ


# ============================ 后台进程管理 =================================
#
# 这一段取代了原来的 start.sh / start.ps1。平台差异在 Python 里判断，
# 不再靠两份各写一遍的脚本——那两份总会慢慢长出不一致的行为。


def resolve_port() -> int:
    """服务将监听的端口，取自 config.json；文件不存在或读不出时用默认值。

    这里只为"打印端口"和"探活时敲哪个端口"，不做校验——真正的配置解析在
    src/config.py 里，两处都校验只会让口径慢慢漂移。
    """
    import json
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        port = data.get("port", DEFAULT_PORT) if isinstance(data, dict) else DEFAULT_PORT
    except (OSError, ValueError):
        return DEFAULT_PORT
    return port if isinstance(port, int) and not isinstance(port, bool) else DEFAULT_PORT


def is_zombie(pid: int) -> bool:
    """POSIX：这个 pid 是不是一个已退出但还没被回收的僵尸进程。

    为什么必须单独判断：僵尸进程在进程表里仍然存在，`os.kill(pid, 0)` 对它
    **返回成功**。而我们后台启动的服务的父进程（run.py 自己）启动完就退出了，
    服务因此被 PID 1 收养——如果 PID 1 不回收子进程（容器里的 PID 1 常常
    只是个 `sleep infinity`，而不是 init/systemd），它就会一直挂在那里。

    不判断的后果是实测遇到的：服务其实 1 秒内就优雅退出了，
    --stop 却认为它还活着，白等满 10 秒、再对一个死进程发 SIGKILL，
    最后打印"未能优雅退出，强制结束"——一条完全误导人的警告。
    """
    status = Path(f"/proc/{pid}/status")
    if status.exists():          # Linux
        try:
            for line in status.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("State:"):
                    return line.split(":", 1)[1].strip().startswith("Z")
        except OSError:
            return False
        return False
    try:                          # macOS / BSD：没有 /proc
        result = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip().startswith("Z")


def process_alive(pid: int) -> bool:
    """进程是否还活着（僵尸不算活着）。

    ⚠️ 不能用 `os.kill(pid, 0)` —— 那只在 POSIX 上是"探测而不发信号"。
    Windows 的 os.kill 会调 TerminateProcess，**信号 0 也照样把进程杀掉**。
    因此 Windows 走 tasklist 查询。
    """
    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # 存在，只是不属于当前用户
    except OSError:
        return False
    return not is_zombie(pid)


def read_pid() -> int | None:
    """PID 文件里记的进程，若仍存活则返回它；否则清掉残留的 PID 文件。"""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        PID_FILE.unlink(missing_ok=True)
        return None
    if not process_alive(pid):
        PID_FILE.unlink(missing_ok=True)
        return None
    return pid


def fetch_health(port: int, timeout: float = 2.0) -> dict | None:
    """敲一次 /health。不通返回 None——探活期间不通是常态，不是错误。"""
    import json
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def do_status() -> int:
    port = resolve_port()
    pid = read_pid()
    if pid is None:
        log(f"未在后台运行（端口 {port}）")
        return 1
    log(f"运行中 · PID {pid} · 端口 {port}")
    health = fetch_health(port)
    if health:
        log(f"索引：{health.get('doc_count')} 文档 / {health.get('chunk_count')} chunk")
        log(f"重建中：{health.get('rebuilding')}")
        log(f"记忆根目录：{health.get('root')}")
    else:
        log("⚠️  进程在，但 /health 不通——可能仍在构建索引，或端口与 PID 文件不一致")
    return 0


def do_stop() -> int:
    pid = read_pid()
    if pid is None:
        log("未在后台运行，无需停止")
        return 0
    log(f"正在停止 PID {pid} …")
    if IS_WINDOWS:
        # Windows 上没有可用于控制台子进程的温和信号；服务本身无状态
        # （索引全在内存、写入是单次原子 write），直接终止是安全的。
        # /T 连子进程一起收，避免 pip 之类的孙进程留下。
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, check=False)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            log(f"发送 SIGTERM 失败：{exc}")

    for _ in range(50):
        if not process_alive(pid):
            break
        time.sleep(0.2)
    if process_alive(pid):
        log("⚠️  未能优雅退出，强制结束")
        if not IS_WINDOWS:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    PID_FILE.unlink(missing_ok=True)
    log("已停止")
    return 0


def do_logs() -> int:
    """跟踪日志。不调用 tail —— Windows 上没有。"""
    if not LOG_FILE.exists():
        log(f"还没有日志文件：{LOG_FILE}")
        return 1
    log(f"跟踪 {LOG_FILE}（Ctrl-C 退出跟踪，不影响服务）")
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(0, os.SEEK_END)
            # 先回放最后约 40 行，免得一开始是一片空白
            size = handle.tell()
            handle.seek(max(0, size - 8192))
            handle.readline()
            sys.stdout.write(handle.read())
            sys.stdout.flush()
            while True:
                line = handle.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.4)
    except KeyboardInterrupt:
        return 0


def spawn_background(command: list[str], environ: dict[str, str]) -> int:
    """把服务放到后台，返回它的 PID。父进程随后可以退出。

    两个平台的"脱离终端"机制不同：
      - POSIX：start_new_session=True（setsid），脱离控制终端与进程组，
        关掉终端不会把 SIGHUP 带给服务；
      - Windows：CREATE_NO_WINDOW 让它没有控制台窗口（不弹黑窗），
        CREATE_NEW_PROCESS_GROUP 让 Ctrl-C 不会传给它。
        没有控制台也意味着关闭父窗口时的 CTRL_CLOSE_EVENT 到不了它，
        因此"脱离终端"这件事同样成立。

    stdin 必须接 DEVNULL：否则后台进程读到终端会被挂起（POSIX 的 SIGTTIN）。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")

    stream = LOG_FILE.open("a", encoding="utf-8")
    kwargs: dict = {
        "stdout": stream,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "env": environ,
        "cwd": str(HERE),
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(command, **kwargs)
    stream.close()
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(process.pid), encoding="utf-8")
    return process.pid


def wait_until_ready(pid: int, port: int) -> int:
    """等服务**就绪**，而不是等它启动。

    服务在索引构建完成前不监听端口，因此 /health 一通就等于"索引已建好、
    可以接请求了"——这才是值得报告给使用者的那个时刻。
    """
    log("等待索引构建完成（构建完成前不监听端口）…")
    for _ in range(READY_TIMEOUT):
        if not process_alive(pid):
            PID_FILE.unlink(missing_ok=True)
            log(f"⚠️  进程已退出，启动失败。日志末尾（{LOG_FILE}）：")
            print(tail_text(LOG_FILE, 20))
            return 1
        health = fetch_health(port)
        if health:
            print()
            log("✅ 后台已启动")
            log(f"   PID      : {pid}")
            log(f"   端口     : {port}")
            log(f"   MCP 端点 : http://127.0.0.1:{port}/mcp")
            log(f"   健康检查 : http://127.0.0.1:{port}/health")
            log(f"   索引     : {health.get('doc_count')} 文档 / {health.get('chunk_count')} chunk")
            log(f"   日志     : {LOG_FILE}")
            log("   停止     : python3 run.py --stop" if not IS_WINDOWS
                else "   停止     : py -3 run.py --stop")
            return 0
        time.sleep(1)

    log(f"⚠️  等待 {READY_TIMEOUT}s 仍未就绪，但进程还活着（PID {pid}）。")
    log("首次启动要建虚拟环境并安装依赖，可能就是慢。看日志：run.py --logs")
    return 1


def tail_text(path: Path, lines: int) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(读不到日志文件)"
    return "\n".join(content.splitlines()[-lines:])


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="myMemory 启动器：自建虚拟环境、自动安装依赖、启动服务。",
        epilog="未识别的参数会原样传给服务，例如 --check、--stdio。",
    )
    parser.add_argument("--background", action="store_true",
                        help="后台启动，就绪后打印端口与 PID 再退出")
    parser.add_argument("--init", action="store_true",
                        help="建档/修复配置并装齐依赖：缺失建档、损坏备份为 .bak 后重建、完好只报告（处理完退出，不启动服务）")
    parser.add_argument("--stop", action="store_true", help="停止后台服务")
    parser.add_argument("--restart", action="store_true", help="重启后台服务")
    parser.add_argument("--status", action="store_true", help="查看后台服务状态")
    parser.add_argument("--logs", action="store_true",
                        help="跟踪后台日志（Ctrl-C 只退出跟踪，不影响服务）")
    parser.add_argument("--venv", default=None,
                        help="虚拟环境路径（默认 mcp/.venv）")
    parser.add_argument("--no-venv", action="store_true",
                        help="不建虚拟环境，用当前解释器运行（依赖需已安装）")
    parser.add_argument("--reinstall", action="store_true",
                        help="强制重新安装依赖")
    parser.add_argument("--recreate", action="store_true",
                        help="删除并重建虚拟环境，然后重装依赖")
    parser.add_argument("--index-url", default=os.environ.get("PIP_INDEX_URL"),
                        help="pip 索引地址，用于走内网或镜像源")
    parser.add_argument("--find-links", default=None,
                        help="本地 wheel 目录，配合 --offline 做离线安装")
    parser.add_argument("--offline", action="store_true",
                        help="离线安装，禁止访问网络（需配合 --find-links）")
    parser.add_argument("--bundle", metavar="DIR", default=None,
                        help="在联网机器上把依赖打成 wheel 包到 DIR，供离线机器使用")
    args, passthrough = parser.parse_known_args()

    # --init 是显式的人工动作：建档/修复 + 装齐依赖，然后直接退出。
    if args.init:
        return do_init(args)

    # 这几个动作只操作已有进程与 PID 文件，不依赖配置内容，先处理掉——
    # 否则配置缺失时（包括还没建档的新机器），连 --status / --stop 都会被建档检查拦下。
    if args.logs:
        return do_logs()
    if args.status:
        return do_status()
    if args.stop and not args.restart:
        return do_stop()

    # 首次启动建档要在动虚拟环境之前：目录是使用者当场输入的，
    # 建档失败（非交互）时也不该白装一遍依赖。
    ensure_config(passthrough)

    if args.restart:
        do_stop()
        args.background = True

    if args.venv is None:
        args.venv = str(default_venv_dir())

    if not REQUIREMENTS.exists():
        sys.exit(f"未找到依赖清单：{REQUIREMENTS}")
    if not SRC_DIR.is_dir():
        sys.exit(f"未找到源码目录：{SRC_DIR}")

    if args.bundle:
        if args.no_venv:
            return bundle_wheels(Path(sys.executable), Path(args.bundle), args)
        venv_dir = Path(args.venv).expanduser().resolve()
        python = ensure_venv(venv_dir, recreate=args.recreate)
        install_dependencies(python, args)
        return bundle_wheels(python, Path(args.bundle), args)

    if args.reinstall or args.recreate:
        args.reinstall = True

    python = setup_environment(args)

    environ = build_child_env()
    # 直接执行 src/main.py 而不是 -m：脚本目录会排在 sys.path[0]，
    # 保证 src/config.py 永远先于工作目录下同名的 CLI config.py。
    command = [str(python), str(SRC_DIR / "main.py"), *passthrough]

    if not python.exists():
        sys.exit(f"无法执行 {python}：文件不存在。请尝试 python3 run.py --recreate")

    if args.background:
        if "--stdio" in passthrough:
            sys.exit(
                "--background 与 --stdio 不能同时用。\n"
                "  stdio 传输的服务由 MCP 客户端自己拉起、通过标准输入输出通信，"
                "放到后台就没有对端了。"
            )
        if "--check" in passthrough:
            sys.exit("--background 与 --check 不能同时用：自检本来就不监听端口。")

        port = resolve_port()
        existing = read_pid()
        if existing is not None:
            log(f"已经在后台运行了 · PID {existing} · 端口 {port}")
            log("要重启用：run.py --restart")
            return 0
        log(f"正在后台启动…（日志：{LOG_FILE}）")
        pid = spawn_background(command, environ)
        return wait_until_ready(pid, port)

    if os.name == "posix":
        # 用 exec 直接替换掉启动器进程，而不是派生子进程。
        # 派生的写法有个真实的坑：kill 掉启动器时，服务子进程会被 init 收养
        # 继续占着端口，看起来"杀不掉"。exec 之后进程只有一个，
        # 信号也直达服务本身。
        try:
            os.execve(str(python), command, environ)
        except OSError as exc:
            sys.exit(f"无法执行 {python}：{exc}")

    # Windows 没有 exec 语义（os.execve 会派生新进程并让父进程立即退出，
    # 导致控制台失去对服务的控制），因此仍用子进程并等待。
    try:
        return subprocess.run(command, env=environ).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
