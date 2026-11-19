"""MCP server tools of the official Python SDK (``mcp.server.fastmcp``):
one ``mcp_tool`` statement per tool.

Every case runs the whole pipeline (``analyze_files``), which also fails on any entry-point
contract error (``parsers/contract.py``).

The ``@<server>.tool`` reader is shared with FastMCP and covered in ``test_roles_fastmcp.py``;
these cases cover which package emits a tool.
"""

from __future__ import annotations

import pytest

from breezeai_cog.schemas import Statement


def _tools(statements: list[Statement]) -> dict[str | None, Statement]:
    tools = [s for s in statements if s.semanticType == "mcp_tool"]
    for t in tools:
        assert t.method == "TOOL" and t.nodeType
    return {t.endpoint: t for t in tools}


def _params(tool: Statement) -> list[tuple]:
    return [(p.name, p.type, p.required, p.default, p.description) for p in tool.params or []]


def test_official_sdk_fastmcp_is_python_mcp(analyze_files) -> None:
    src = "from mcp.server.fastmcp import FastMCP\n\nmcp = FastMCP('demo')\n\n\n@mcp.tool()\ndef echo(text: str) -> str:\n    return text\n"
    assert _tools(analyze_files({"srv.py": src}))["echo"].framework == "python-mcp"


def test_official_sdk_server_imported_from_another_module(analyze_files) -> None:
    files = {
        "app/server.py": "from mcp.server.fastmcp import FastMCP\n\nmcp = FastMCP('x')\n",
        "app/tools.py": "from app.server import mcp\n\n\n@mcp.tool()\ndef hello(name: str) -> str:\n    return name\n",
    }
    tools = _tools(analyze_files(files))
    assert set(tools) == {"hello"}
    # The server's file decides the SDK: the tools file imports neither package.
    assert tools["hello"].framework == "python-mcp"
    assert _params(tools["hello"]) == [("name", "str", True, None, None)]


def test_each_tool_is_emitted_once_by_the_package_of_its_server(analyze_files) -> None:
    """A tools file using a FastMCP server and an official-SDK server: each tool is one
    statement, with its own server's framework."""
    files = {
        "app/fast.py": "from fastmcp import FastMCP\n\nfast = FastMCP('a')\n",
        "app/official.py": "from mcp.server.fastmcp import FastMCP\n\nofficial = FastMCP('b')\n",
        "app/tools.py": (
            "from app.fast import fast\nfrom app.official import official\n\n\n"
            "@fast.tool()\ndef one() -> str:\n    return ''\n\n\n"
            "@official.tool()\ndef two() -> str:\n    return ''\n"
        ),
    }
    tools = [s for s in analyze_files(files) if s.semanticType == "mcp_tool"]
    assert sorted((t.endpoint, t.framework) for t in tools) == [
        ("one", "fastmcp"),
        ("two", "python-mcp"),
    ]


# ── Low-level server (`mcp.server.lowlevel.Server`) ─────────────────────────

LOWLEVEL = """
from mcp.server.lowlevel import Server
import mcp.types as types

server = Server("demo")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="add",
            description="Add two numbers",
            inputSchema={
                "type": "object",
                "properties": {"a": {"type": "number", "description": "First"}, "b": {"type": "number"}},
                "required": ["a"],
            },
        ),
        types.Tool(name="ping", inputSchema={"type": "object", "properties": {}}),
        types.Tool(name=COMPUTED, description="x", inputSchema={}),
        types.Tool(name="schema", description=DESC, inputSchema=Model.model_json_schema()),
        types.Tool(name="fstring", inputSchema={"type": "object", "properties": {"q": {"description": f"{x}"}}}),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    return []
"""


def test_lowlevel_server_tools(analyze_files) -> None:
    tools = _tools(analyze_files({"srv.py": LOWLEVEL}))
    assert set(tools) == {"add", "ping", "schema", "fstring"}  # a computed name is not guessed
    add = tools["add"]
    assert (add.framework, add.description, add.handler, add.handlerLine) == (
        "python-mcp",
        "Add two numbers",
        "call_tool",
        28,  # the `@server.call_tool()` function dispatches every tool
    )
    assert add.startLine == 11 and add.nodeType == "call"
    assert _params(add) == [
        ("a", "number", True, None, "First"),
        ("b", "number", False, None, None),
    ]
    assert tools["ping"].params == [] and tools["ping"].description is None
    # A schema that is not a literal dict (a call, an f-string inside) is unknown, never partial.
    assert tools["schema"].params is None and tools["schema"].description is None
    assert tools["fstring"].params is None


def test_lowlevel_tool_imports_and_list_tools_result(analyze_files) -> None:
    src = """
from mcp.server import Server as McpLowLevel
from mcp.types import Tool as T, ListToolsResult
from mcp import types as mt


def build(app: McpLowLevel) -> None:
    @app.list_tools()
    async def tools():
        if ready:
            return ListToolsResult(tools=[T(name="one", inputSchema={})])
        return [mt.Tool(name="two", inputSchema={})]
"""
    tools = _tools(analyze_files({"srv.py": src}))
    assert set(tools) == {"one", "two"}
    assert tools["one"].handler is None and tools["one"].handlerLine is None  # no call_tool


def test_lowlevel_list_not_written_as_literal_elements_is_not_read(analyze_files) -> None:
    src = """
from mcp.server.lowlevel import Server
from mcp.types import Tool

server = Server("demo")
TOOLS = [Tool(name="hidden", inputSchema={})]


@server.list_tools()
async def list_tools():
    def helper():
        return [Tool(name="nested", inputSchema={})]
    if flag:
        return [t for t in TOOLS if t.name != "x"]
    if other:
        return TOOLS
    return [Tool(name="a", inputSchema={})] if flag else []
"""
    assert _tools(analyze_files({"srv.py": src})) == {}


def test_lowlevel_dispatcher_must_be_on_the_same_server(analyze_files) -> None:
    src = """
from mcp.server.lowlevel import Server
from mcp.types import Tool

a = Server("a")
b = Server("b")


@a.list_tools()
async def list_a():
    return [Tool(name="from_a", inputSchema={})]


@b.call_tool()
async def call_b(name, arguments):
    return []


@b.list_tools()
async def list_b():
    return [Tool(name="from_b", inputSchema={})]


@b.call_tool()
async def call_b_again(name, arguments):
    return []
"""
    tools = _tools(analyze_files({"srv.py": src}))
    assert tools["from_a"].handler is None  # `a` has no dispatcher; `b`'s is not `a`'s
    assert tools["from_b"].handler is None  # two dispatchers on `b`: which one is unknown


def test_lowlevel_look_alikes_are_not_tools(analyze_files) -> None:
    """A local class named ``Server``, the standard library's ``types``, a name rebound in a
    nearer scope, and agent frameworks that say ``tool``: no record."""
    files = {
        "local_server.py": """
from mcp.types import Tool


class Server:
    def list_tools(self):
        return lambda f: f


server = Server()


@server.list_tools()
async def list_tools():
    return [Tool(name="fake", inputSchema={})]
""",
        "stdlib_types.py": """
import types
from mcp.server.lowlevel import Server

server = Server("x")


@server.list_tools()
async def list_tools():
    return [types.Tool(name="not_mcp_types", inputSchema={})]
""",
        "shadowed.py": """
from mcp.server.lowlevel import Server
from mcp.types import Tool

server = Server("x")


def register(server):
    @server.list_tools()
    async def list_tools():
        return [Tool(name="untyped_param", inputSchema={})]


def other():
    server = make_agent()

    @server.list_tools()
    async def list_tools():
        return [Tool(name="rebound", inputSchema={})]
""",
        "agents.py": """
from langchain_core.tools import tool, StructuredTool
from pydantic_ai import Agent
from agents import function_tool

agent = Agent("openai:gpt-4o")


@tool
def search(q: str) -> str:
    return q


@agent.tool
def lookup(ctx, q: str) -> str:
    return q


@function_tool
def weather(city: str) -> str:
    return city


calc = StructuredTool.from_function(func=search, name="calc")
""",
    }
    assert _tools(analyze_files(files)) == {}


def test_lowlevel_tools_are_python_mcp_only(analyze_files) -> None:
    """A file that also uses FastMCP: each low-level tool is one record, ``python-mcp``."""
    src = (
        "from fastmcp import FastMCP\n"
        + LOWLEVEL
        + "\nfast = FastMCP('f')\n\n\n@fast.tool()\ndef fast_one() -> str:\n    return ''\n"
    )
    tools = [s for s in analyze_files({"srv.py": src}) if s.semanticType == "mcp_tool"]
    assert sorted((t.endpoint, t.framework) for t in tools) == [
        ("add", "python-mcp"),
        ("fast_one", "fastmcp"),
        ("fstring", "python-mcp"),
        ("ping", "python-mcp"),
        ("schema", "python-mcp"),
    ]


@pytest.mark.parametrize("jobs", [1, 2])
def test_lowlevel_server_in_another_module_for_any_jobs(capture_mcp_tools, jobs) -> None:
    files = {
        "app/server.py": "from mcp.server.lowlevel import Server\n\nserver = Server('x')\n",
        "app/tools.py": (
            "from mcp import types\n"
            "from app.server import server as srv\n\n\n"
            "@srv.list_tools()\nasync def list_tools():\n"
            "    return [types.Tool(name='hello', inputSchema={'type': 'object', 'properties': {}})]\n\n\n"
            "@srv.call_tool()\nasync def dispatch(name, arguments):\n    return []\n"
        ),
        "app/other.py": (
            "from mcp import types\nfrom app.server import helper\n\n\n"
            "@helper.list_tools()\nasync def list_tools():\n"
            "    return [types.Tool(name='not_a_server', inputSchema={})]\n"
        ),
    }
    tools = capture_mcp_tools(files, jobs)
    assert set(tools) == {"hello"}
    assert (tools["hello"]["framework"], tools["hello"]["handler"]) == ("python-mcp", "dispatch")
