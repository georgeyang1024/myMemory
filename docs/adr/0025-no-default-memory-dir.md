# ADR-0025 · 首次启动不猜测记忆目录，交互式建档

**状态**：已采纳并实施 · 2026-09-26
**取代**：[REQUIREMENTS §4.1](../REQUIREMENTS.md) 的"首次启动不存在时自动生成：只含读写 source `memory` → `mcp/../memory`"。

**修订**：2026-10-01——交互建档前先问使用形态：**个人使用**（默认，逐字问记忆
目录，行为同前）或**团队使用**（多人共用，ADR-0028/0030）：逐字问团队存储目录
（子文件夹 = 团队成员，回车默认 `~/.myMemory/users`）与管理员账号（逗号分隔
多个，回车默认 admin），写出 `multi_user`（`enabled: true`）。团队形态**不建
公共 source**（需要共享记忆再 `config.py source add`），配置校验相应放宽：
`multi_user` 启用时允许 `sources` 缺省/为空，语料 = 成员个人 source。
非交互建档（`--init` 管道下）按个人使用 + 默认目录兜底，与原行为一致。

## 背景

此前的默认配置按代码位置反推记忆目录：`config.py` 位于 `mcp/src/my_memory/`，
`parents[3]` 即仓库上一级，默认 source `memory` 指向 `<仓库上一级>/memory/`。
这只在"仓库恰好放在 `xxx/myDocs/mcp`"这种布局下碰巧正确；clone 到别处
（如 `D:\projects\mcp`）时，默认记忆目录就变成 `D:\projects\memory`——
一个不存在的、与代码混在一起的位置。由于目录不可用只警告不失败
（[ADR-0020](0020-mounted-disk-offline.md)），首次启动表面一切正常，
写入却一直失败，排障体验很差。

任何"从代码位置猜数据位置"的规则都只在特定目录布局下成立，而代码可以被
放在任何地方；记忆放哪里只有操作者知道。

## 决策

- 删除 `_DEFAULT_MEMORY_DIR`（代码位置反推）与自动生成含猜测路径的默认配置。
- **终端场景**（stdin/stdout 都是 TTY）：`run.py` 启动器检测到配置文件不存在时
  交互式建档——逐字询问记忆目录，直接回车用 `~/.myMemory/memory`（自动创建目录）；
  写出的配置 = `DEFAULTS` 默认值 + 一个可写 source `memory`（描述"默认记忆源"）。
  `python -m my_memory`（HTTP 模式）走同一入口。
- **非交互场景**（`--stdio` 传输、后台子进程、管道）：无法提问，也绝不猜测——
  启动失败，报"未指定记忆存储"并指引先在终端完成建档
  （`python3 run.py` 或 `python config.py source add memory --dir <目录> --writable`）。
- **`run.py --init`（2026-09-26 追加）**：显式的检查/修复动作，处理完退出不启动服务。
  配置缺失时建档（非终端下用默认 `~/.myMemory/memory` 兜底并明确打印——建档由
  使用者的 `--init` 显式发起，默认目录是被声明过的选择，不再是猜测）；配置
  JSON/校验不通过时先备份为 `config.json.bak`（覆盖旧备份）再重建；配置完好时
  只校验报告（source 列表、目录可用性），一个字节不改。三个分支最后都走与
  正常启动共用的 `setup_environment`：虚拟环境就绪、依赖装齐（已装则秒过）。
- `config.py source add` 允许从零建档：首个 source 是 `memory` 时与 run.py 首次
  建档写出的配置同构；其余子命令在配置不存在时直接报错。
- `config.py`（CLI）与服务本体都不再隐式建档：建档只发生在 run.py 交互启动或
  显式 `source add` 时。后台模式必须由 run.py 先建档——子进程 stdin 是 DEVNULL，
  服务进程内无法提问。

## 备选与取舍

- **默认 `~/.myMemory/memory` 静默生成**：与配置同目录、安装位置无关。但记忆是
  用户数据，放哪里是用户的决定；默认值只作为交互提示里的回车选项出现，不静默生效。
- **保留代码位置推导 + 兜底**：两条规则叠加，行为随布局分支，更难解释也更难测试。
- **占位配置 + 启动提示**：生成 dir 为空的配置再靠运行时提示，多引入一种
  "配置存在但不可用"的状态；交互一次到位更简单。

## 后果

- 首次启动多一次问答，换来配置一次写对；README 安装流程已同步。
- "配置文件不存在"成为明确的错误状态，不再有"自动生成的配置指向不存在目录"
  这类静默故障。
- 用例：`test_ensure_config_*`（run.py 建档/跳过/stdio 失败）、
  `test_add_creates_config_from_scratch`（CLI 同构建档）、
  `test_missing_config_blocks_other_commands`。
