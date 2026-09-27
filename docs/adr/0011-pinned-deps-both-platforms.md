# ADR-0011 · 交付形态：锁版本依赖、双平台脚本、纳入 git

**状态**：已采纳 · 2026-09-02
**修订**：2026-09-24 —— 双平台部分已被 [ADR-0021](0021-single-platform.md) 取代；锁定精确版本不变。

> **读之前先看 [ADR-0000](0000-lineage.md)**：本 ADR 的论证材料来自本项目的前身（一个团队技术知识库）的实测。**决策本身至今有效**；例子已做匿名化处理。

## 决策

1. **依赖管理**：`requirements.txt` + 标准 `venv`，**锁死精确版本号**
2. **启动脚本**：`run.sh`（Linux/macOS）与 `run.ps1`（Windows）**都提供**
   —— *2026-09-17 修订：这两个脚本连同后来加的 `start.sh` / `start.ps1` 已全部删除，
   平台差异改为在 `run.py` 内部判断。本条的主张（双平台都要能一条命令跑起来）未变，
   变的只是实现方式：从"每个平台一个脚本"变成"一个跨平台的 Python 入口"。*
3. **版本控制**：源码全部提交进 git（前身项目当时托管在团队内部的代码仓库上）

## 理由

**为何 requirements.txt 而非 uv / PEP 723**：本机未安装 `uv`，而 Python 3.12.3
自带 `venv`。四个依赖全是纯 Python 轮子（`jieba` 无编译依赖），任何平台可装。
选 uv 只为省几秒安装时间，却增加一个前置安装步骤。

**为何锁死版本**：实测已经踩到——`mcp` 2.x 将 `FastMCP` 更名为 `MCPServer`，
v1 代码直接 `ModuleNotFoundError`。浮动版本号意味着某天 `pip install` 会静默
装上不匹配的 SDK 并让服务失效。

**为何双平台**：交付边界为"只给源码、自行运行"（[ADR-0008](0008-machine-agnostic-delivery.md)），
运行机器未定——可能是 Linux 服务器，也可能是 Windows 本机。多写一份脚本成本极低。

## 目录布局

```
mcp/
├── README.md              部署与配置说明（含 DNS rebinding 开关的显著标注）
├── requirements.txt       锁版本的 4 个依赖
├── .env.example           全部环境变量及默认值
├── run.py                 唯一入口（跨平台；曾经的 run.sh / run.ps1 已删除）
├── docs/
│   ├── ARCHITECTURE.md
│   ├── IMPLEMENTATION-PLAN.md
│   ├── GLOSSARY.md
│   └── adr/0001…0011
├── src/my_memory/
│   ├── __init__.py
│   ├── __main__.py        入口：读配置 → 首次构建 → 启轮询 → 起 uvicorn
│   ├── config.py          环境变量 → 不可变配置对象
│   ├── corpus.py          扫描 → 过滤 → 读取 → 固定窗口切块
│   ├── index.py           jieba → BM25 → 查询 → mtime 轮询 → 原子替换
│   └── server.py          2 个 MCP 工具 + /health + /search
└── tests/
    ├── test_corpus.py
    ├── test_index.py
    └── test_server.py
```

## 后果

- `.gitignore` 需补充 `mcp/.venv/`（现有 `.gitignore` 已含 `*.pyc`）
- 启动脚本需处理路径含空格与中文的情况（实测语料下文件名普遍含空格，
  如 `技术说明 部署方案V3.md`），所有脚本必须引号包裹或使用数组传参
