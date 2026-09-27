"""对外契约：客户端 initialize / tools/list 实际收到的东西。

这里锁的不是实现，是**模型看得见的那部分**——服务说明、工具描述、
参数 schema。它们是 LLM 决定要不要调、怎么调的唯一依据，
静默退化不会让任何功能测试变红，只会让检索质量慢慢变差。

真实教训：参数的 description 曾经整个缺失（docstring 的 Args 段不会进
inputSchema），模型看到的只有 {"title": "Limit", "type": "integer"}——
1-20 的范围、越界钳制、path 必须来自 search，全都传达不到。
"""

import asyncio
from pathlib import Path

import pytest

from index import IndexHolder
from server import create_server

from test_corpus import make_config

# 常驻工具（不含受 allow_mcp_delete 开关控制的 delete / merge）。
BASE_TOOLS = ["search", "get-document", "save", "rename", "replace",
              "list-sources", "recent"]

ALL_TOOLS = [*BASE_TOOLS, "merge", "delete"]
TOOLS_WITH_PARAMS = [t for t in ALL_TOOLS if t != "list-sources"]


@pytest.fixture
def server(tmp_path: Path):
    root = tmp_path / "memory" / "技术"
    root.mkdir(parents=True)
    (root / "机制流程.md").write_text("机制与绑定。" * 50, encoding="utf-8")
    config = make_config(tmp_path, allow_mcp_delete=True)
    holder = IndexHolder(config)
    holder.build_now()
    return create_server(config, holder)


@pytest.fixture
def tools(server):
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


# --- 服务级 --------------------------------------------------------------

def test_server_identity(server):
    assert server.name == "myMemory"
    assert "记忆" in server.title


def test_instructions_describe_the_path_shape(server):
    """路径形态必须写明：一级分类 + 文件名，且分类名可当检索词。

    调用方判断"该拿什么当 category"、"能不能嵌套"、"不熟悉库内容时怎么摸索"，
    全靠这段说明。写漏了不会让任何功能测试变红，只会让它猜。
    """
    text = server.instructions
    assert "分类" in text, "必须说明路径第一段是分类目录"
    assert "嵌套" in text or "只有一级" in text, "必须说明工具写入时分类不支持嵌套"


def test_instructions_frame_it_as_a_shared_human_ai_memory(server):
    """口径必须是"人与 AI 共同的记忆系统"，不是"一个系统的语料库"。

    这决定了模型怎么对待它：双方都读都写；人随时会用编辑器直接改、
    建任意层级的目录；一级分类是对**模型写入**的约定，不是对人的限制。
    模型若把它当成自己管辖的数据库，就会去纠正人的目录结构，
    或因为路径"不合规"而拒绝读取。
    """
    text = server.instructions
    assert "共同" in text and "AI" in text, "必须说明这是人与 AI 共用的记忆"
    assert "不限" in text, "必须说明主题与范围不限，否则模型会自我设限"
    assert "编辑器" in text, "必须说明人会绕过 MCP 直接改文件"


def test_instructions_state_the_hard_constraints(server):
    """几条最容易让调用方翻车的性质必须写明。"""
    text = server.instructions
    assert "不做同义改写" in text, "不说明这点，模型搜不到就会放弃或编造"
    assert "原文片段" in text and "不是答案" in text, "必须说明返回的是证据不是答案"
    assert "覆盖" in text, "覆盖语义必须写明，否则模型会以为同名写入是安全的"
    assert "先 `search`" in text or "先 search" in text, (
        "写前必须先搜——这是覆盖语义下唯一的护栏，必须出现在 instructions 里"
    )


# --- 工具级 --------------------------------------------------------------

def test_all_five_tools_exposed(tools):
    assert set(tools) == set(ALL_TOOLS)


def test_delete_tools_hidden_until_enabled(tmp_path: Path):
    """allow_mcp_delete 默认关闭：delete 与 merge 必须整体不存在于 tools/list。

    隐藏比"列出但拒绝"更可靠——LLM 看不到就不会调用，也不会试图绕道
    （比如用整篇替换把内容替换成空）。开启开关的唯一入口是人工改配置。
    """
    root = tmp_path / "memory" / "技术"
    root.mkdir(parents=True)
    (root / "机制流程.md").write_text("机制与绑定。" * 50, encoding="utf-8")
    config = make_config(tmp_path)  # 不传 allow_mcp_delete：默认关
    holder = IndexHolder(config)
    holder.build_now()
    server = create_server(config, holder)
    tools = {tool.name for tool in asyncio.run(server.list_tools())}
    assert set(BASE_TOOLS) == tools, "删除关闭时工具面必须收敛到基础集合"
    assert "delete" not in tools and "merge" not in tools


def test_tool_names_carry_no_server_prefix(tools):
    """客户端已按服务名做命名空间（mcp__myMemory__save），工具名再带 myMemory- 就重复了。"""
    assert not [name for name in tools if name.lower().startswith("mymemory")]


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_tool_has_title_and_description(tools, name):
    tool = tools[name]
    assert tool.title
    assert tool.description and len(tool.description) > 80, "描述太短，不足以让模型判断何时该调"


# 本项目由一个知识库服务改造而来（见 docs/adr/0000-lineage.md）。
# 这些词出现在对外文案里，说明有一处改名漏了——真实发生过：
# 描述正文改完了，工具 title 还挂着"检索方案知识库"，
# 而 title 是调用方在工具列表里最先看到的那一行。
# 另加已被取代的口径：这套记忆是"人与 AI 共同的"，不是"个人的 / 我的"。
STALE_WORDS = ("知识库", "语料",
               "个人记忆", "我的记忆", "使用者的记忆")


def test_no_stale_domain_wording_in_the_tool_surface(server, tools):
    """服务标题、instructions、工具 title 与描述里都不该留前身项目的词。"""
    surfaces = {"server.title": server.title, "instructions": server.instructions}
    for name, tool in tools.items():
        surfaces[f"{name}.title"] = tool.title
        surfaces[f"{name}.description"] = tool.description

    found = {
        where: [w for w in STALE_WORDS if w in text]
        for where, text in surfaces.items()
        if any(w in text for w in STALE_WORDS)
    }
    assert not found, f"对外文案里残留前身项目的措辞：{found}"


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_tool_description_stays_short(tools, name):
    """文案要短。

    长描述每次 tools/list 都占调用方的上下文，而真正决定行为的只有几句。
    上限是刻意留了余量的软约束：超了说明又在往里堆细节，那些细节该进
    docs/，不该进工具描述。
    """
    length = len(tools[name].description)
    assert length <= 1000, f"{name} 的描述 {length} 字符，太长了"


@pytest.mark.parametrize("name", TOOLS_WITH_PARAMS)
def test_every_parameter_is_described(tools, name):
    """每个参数都必须有非空 description——这正是曾经缺失的那一环。"""
    props = tools[name].input_schema["properties"]
    assert props, "工具没有参数？"
    undocumented = [k for k, v in props.items() if not v.get("description", "").strip()]
    assert not undocumented, f"{name} 的参数缺少 description：{undocumented}"


def test_search_documents_the_limit_range(tools):
    desc = tools["search"].input_schema["properties"]["limit"]["description"]
    assert "1-20" in desc or "1–20" in desc


def test_get_document_tells_where_path_comes_from(tools):
    desc = tools["get-document"].input_schema["properties"]["path"]["description"]
    assert "search" in desc, "必须说明 path 只能来自 search，否则模型会自己拼路径"


def test_save_takes_exactly_the_agreed_params(tools):
    """source / category / filename / content，一个不多一个不少。

    多出一个 path 之类的参数就意味着调用方能自己拼路径，写入边界会从
    "分类名 + 文件名是标识符"退化成"要做路径校验"。
    """
    schema = tools["save"].input_schema
    assert set(schema["properties"]) == {"source", "category", "filename", "content"}
    assert set(schema["required"]) == {"source", "filename", "content"}, \
        "source 必填、无默认值；category 可为空"


def test_get_document_requires_source(tools):
    schema = tools["get-document"].input_schema
    assert {"source", "path"} <= set(schema["required"])


def test_search_and_recent_source_is_optional(tools):
    for name in ("search", "recent"):
        schema = tools[name].input_schema
        assert "source" in schema["properties"]
        assert "source" not in schema.get("required", [])


def test_list_sources_takes_no_params(tools):
    assert not tools["list-sources"].input_schema.get("properties")


def test_recent_documents_its_limit_and_fields(tools):
    tool = tools["recent"]
    assert "1-20" in tool.input_schema["properties"]["limit"]["description"]
    assert "默认 10" in tool.input_schema["properties"]["limit"]["description"]
    for word in ("edited_by", "agent", "scan"):
        assert word in tool.description


def test_instructions_explain_sources_statically(server):
    """instructions 是静态文案：讲规则，不列具体有哪些 source。"""
    text = server.instructions
    assert "source" in text and "writable" in text
    assert "readonly/" not in text, "只读已改为 writable 字段，不再用名称前缀"
    assert "list-sources" in text
    assert "memory" not in text.replace("myMemory", ""), "不应把具体 source 名写进 instructions"


def test_save_warns_about_nesting_and_overwrite(tools):
    """两条最容易踩的约束必须出现在参数描述里，而不只在工具描述里。

    覆盖这条尤其重要：它不可撤销、没有备份，而参数描述是模型填 filename
    时唯一会读到的文字。
    """
    props = tools["save"].input_schema["properties"]
    assert "嵌套" in props["category"]["description"], "不写明就会收到 技术/协议 这种入参"
    assert "覆盖" in props["filename"]["description"], "不写明就会以为同名写入是安全的"
    assert "整篇替换" in props["content"]["description"], "不写明就会以为 content 是追加"


def test_save_says_the_refresh_is_async(tools):
    """异步刷新必须写在工具描述里。

    不说明的话，模型写完立刻 search 搜不到，会判定写入失败并重试，
    于是产生一串 "xxx"、"xxx-1"、"xxx-2" 的重复记忆。
    """
    desc = tools["save"].description
    assert "异步" in desc and "搜不到" in desc


def test_save_says_category_can_be_created(tools):
    """分类不存在会自动创建——不写明，模型会因为"库里没有这个分类"而不敢写。"""
    props = tools["save"].input_schema["properties"]
    assert "自动创建" in props["category"]["description"]
    assert "自动创建" in tools["save"].description


def test_save_states_the_overwrite_semantics(tools):
    """覆盖语义必须写明——但只要一句，不要一整段警告。

    早先这里堆了大段"不可撤销、没有备份、必须先 search"的告诫，占描述近一半篇幅。
    删掉是刻意的：真正要传达的只有"同名直接覆盖"这一条事实，
    剩下的判断交给调用方。护栏仍在 instructions 的规矩 2 里。
    """
    desc = tools["save"].description
    assert "同名" in desc and "覆盖" in desc
    assert "不可撤销" not in desc, "大段警告已按要求删除，不要写回来"
    assert len(desc) < 700, f"save 描述 {len(desc)} 字符，警告段可能又被加回来了"


def test_numeric_params_are_not_constrained(tools):
    """描述里写了范围，但 schema 上不能有 minimum/maximum。

    越界入参由 _clamp 钳制而非报错，是有意的设计：调用方是 LLM，
    越界是常见且无害的失误，钳制比让它读一条校验错误再重试省一轮往返。
    加上 ge/le 会把这个设计变成校验失败。
    """
    for name, params in (
        ("search", ["limit"]),
        ("get-document", ["offset", "limit"]),
        ("recent", ["limit"]),
    ):
        props = tools[name].input_schema["properties"]
        for param in params:
            schema = props[param]
            assert "minimum" not in schema and "maximum" not in schema, (
                f"{name}.{param} 带上了范围约束，越界将变成校验错误而非钳制"
            )


def test_no_workspace_wording_left_in_the_tool_surface(server, tools):
    """术语已统一为 source（ADR-0023）：文案、工具名、参数名里都不该再出现 workspace。"""
    surfaces = [server.instructions] + [
        text for tool in tools.values()
        for text in (tool.name, tool.title, tool.description, *tool.input_schema.get("properties", {}))
    ]
    assert not [s for s in surfaces if "workspace" in s.lower()]
