"""MCP server tools of the official Python SDK (``mcp.server.fastmcp``):
one ``mcp_tool`` statement per tool.

Every case runs the whole pipeline (``analyze_files``), which also fails on any entry-point
contract error (``parsers/contract.py``).

The ``@<server>.tool`` reader is shared with FastMCP and covered in ``test_roles_fastmcp.py``;
these cases cover which package emits a tool.
"""

from __future__ import annotations

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
