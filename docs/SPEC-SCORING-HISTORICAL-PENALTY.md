# 检索打分：历史关键字降分 · 开发细节 Spec

> 状态：**已实施**（2026-10-01）。
> 决策记录见 [ADR-0033](adr/0033-search-historical-keyword-penalty.md)；
> 打分框架沿用 [ADR-0027](adr/0027-search-scoring-adjustments.md)，术语以 [GLOSSARY.md](GLOSSARY.md) 为准。

---

## 1. 目标与非目标

### 目标

1. `scoring` 新增 `historical_penalty` 与 `historical_keywords`：source 内相对路径
   （目录 + 文件名）含**任一**关键字（子串、不区分大小写）的文档，在 BM25 分之外
   **扣** `historical_penalty` 分——命中多个关键字只扣一次，每篇文档只算一次。
2. 全局一份默认值，source 可按字段覆盖；机制、校验、CLI 与 0027 的四字段完全同构。
3. 只影响查询时打分：命中集合、`total_matched`、每篇最多 2 个结果位的规则都不变；
   `score` 变为 `BM25 分 + 路径命中加分 + 时间加分 − 历史降分`。
4. 不依赖 mtime——mtime 失效（批量改动、版本库拉取）时由它兜底。

### 非目标

| 非目标 | 理由 |
|---|---|
| 排除命中文档 | 只返回证据（ADR-0003）：只降分不排除 |
| 默认开启 | 关键字默认表对任意语料都可能误伤（如英文笔记文件名含 `meeting`）；必须先核对误伤面再开启 |
| 进入索引缓存指纹 | 不改分词与切块，改配置不重建 |
| 关键字目录白名单 / 正则匹配 | 复杂度不成比例；靠选词 + 上线前 grep 控制误伤面 |
| frontmatter / 状态标注类信号 | ADR-0027 已否决：外部同步的文档标不了 |

## 2. 配置（`src/config.py`）

### 2.1 schema

```json
"scoring": {
  "recency_window_days": 30,
  "recency_bonus": 10,
  "path_match_bonus": 5,
  "strip_wikilinks": true,
  "historical_penalty": 0,
  "historical_keywords": []
}
```

- `DEFAULTS["scoring"]` 增补两个字段：`historical_penalty: 0`（默认关闭）、
  `historical_keywords: []`。顶层合法键由 `DEFAULTS` 推导，自动放行 `scoring`。
- `Scoring` dataclass 增两个字段：`historical_penalty: float = 0`、
  `historical_keywords: tuple[str, ...] = ()`（不可变，`asdict` 后随 `/health` 输出）。
- `parse_scoring` 校验：
  - `historical_penalty`：非负数（显式排除 bool——`isinstance(True, int)` 为真）。
  - `historical_keywords`：字符串数组，每项 strip 后非空，总数 ≤ 100；否则
    `ConfigError` 点名字段（`scoring.historical_keywords` 或 `source a 的 scoring.…`）。
  - 归一化：每项 strip + lower + 去重保序后存为 tuple（匹配不区分大小写在配置层完成一次，
    查询时不再逐篇转换）。
- source 级覆盖与继承逻辑零改动（`dataclasses.replace`）。

## 3. 打分（`src/index.py`）

### 3.1 `_document_bonus`

```python
bonus = 0.0
lowered = path.lower()
if scoring.path_match_bonus and terms and all(t in lowered for t in terms):
    bonus += scoring.path_match_bonus
window = scoring.recency_window_days
if window and scoring.recency_bonus:
    ...  # 线性衰减，不变
if scoring.historical_penalty and any(kw in lowered for kw in scoring.historical_keywords):
    bonus -= scoring.historical_penalty
return bonus
```

- `path.lower()` 提出来算一次，三项共用。
- 关键字已小写化存储，匹配端只 lower 路径。
- `NO_SCORING`（快照缺 source 配置时的全 0 值）显式带 `historical_penalty=0`。

### 3.2 不变的部分

- 快照构造点（`build` / 缓存加载 / 增量重建 / `update_availability`）签名不变，
  新字段随 `Scoring` 对象自动携带。
- 缓存指纹 `cache_fingerprint` 不变：两个查询时字段都不进指纹。
- `search` 的命中筛选 → 加分 → 排序 → 多样性约束流程不变。

## 4. CLI（根目录 `config.py`）

- `SCORING_FIELDS` 由 `DEFAULTS["scoring"]` 推导，新字段自动进入
  `--scoring` / `--reset-scoring` 的合法集与错误提示。
- `parse_scoring_value` 新增：`historical_keywords` 用**逗号分隔**，各项 strip，
  丢空项（`historical_keywords=` 写出 `[]`，等效关闭）；`historical_penalty` 走既有
  数字分支。范围（非负）由保存前的完整校验把关，校验不过一个字节都不改。
- `format_scoring` 渲染数组：`historical_keywords=meeting,已废弃`（`source list` 与
  修改回显共用）。
- `rebuild_note` 不变：改这两个字段不触发"全量重建"提示。

## 5. 测试清单

接缝不变：`build_index(root, scoring=...)`、`Config.load`、CLI `main([...])`。

- **config 层**：省略取默认；全局设 penalty、source 继承；source 覆盖 keywords 归一化
  （strip、lower、去重、保序）；非数组 / 数组含非字符串 / 数组含空串 / 超 100 条 /
  penalty 负数 / penalty 为 bool → 报错点名；`bootstrap_config_data` 写出含新字段的默认节；
  `describe` 的 source 摘要含新字段且只列与全局不同的 source。
- **打分层**：
  - 关键字在目录名 → 该文档所有 chunk 扣一次（对比全关与开 penalty 的分差 == −penalty）；
  - 命中多个关键字只扣一次；
  - 大小写不敏感（关键字 `MEETING` 命中 `meeting/…`）；
  - 不含关键字的文档 0；无 penalty 时全 0；
  - 组合：`路径命中 + 时间加分 − 降分` 同处生效，排序按最终分；
  - 命中集合与 total 在开 / 关之间不变；
  - 全部 mtime 相同（模拟失效）时，权威文档仍胜过命中关键字的历史稿；
  - source 级 penalty 只影响该 source。
- **CLI**：全局与 source 的 `historical_penalty=3`、`historical_keywords=meeting,已废弃`
  写入与回显；逗号分隔解析；重置单字段 / all；非法值退出码 2 且文件字节不变；
  `source list` 显示数组覆盖。

## 6. 上线步骤（本仓库实例）

1. 语料按关键字 grep 误伤面（本实例只在 `newBleSec` source 开启；
   `myNote` 文件名含关键字的笔记会被误伤，不开）。
2. `python config.py source edit newBleSec --scoring historical_penalty=3
   --scoring "historical_keywords=meeting,已废弃,已删除,已过期,deprecated"`
3. 重启服务；`/health` 的 `scoring.sources` 核对 `newBleSec` 的有效值。
4. 实搜核对：目标查询（专题名 / 专题名 决策 / 按日期查历史结论）排名不回退；
   跨 source 核对未开启的 source 排序不变。
