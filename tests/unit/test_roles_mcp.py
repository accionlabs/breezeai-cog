"""MCP server tools (BREEZEAI-1457): one ``mcp_tool`` statement per tool, per SDK.

Every case runs the whole pipeline (``analyze_files``), which also fails on any entry-point
contract error (``parsers/contract.py``). Each SDK covers a renamed tool, a tool without a
description, optional params with defaults, and a look-alike that must not be read as a tool.
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


# ── Python: FastMCP / mcp.server.fastmcp ────────────────────────────────────

FASTMCP_SERVER = '''
from typing import Annotated
from fastmcp import FastMCP, Context
from pydantic import Field

mcp = FastMCP("breeze")


@mcp.tool()
def add(a: int, b: int = 2) -> int:
    """Add two numbers.

    Returns the sum.
    """
    return a + b


@mcp.tool(name="Code_Graph_Search", description="Semantic search over the code graph")
async def search(
    query: Annotated[str, Field(description="Search text")],
    ctx: Context,
    limit: int = Field(default=10, description="Max hits"),
    labels: list[str] | None = None,
) -> str:
    """Ignored: the explicit description wins."""
    return ""


@mcp.tool
def ping():
    return "pong"
'''


def test_fastmcp_module_server(analyze_files) -> None:
    tools = _tools(analyze_files({"app/server.py": FASTMCP_SERVER}))
    assert set(tools) == {"add", "Code_Graph_Search", "ping"}

    add = tools["add"]
    assert (add.framework, add.handler, add.handlerLine) == ("fastmcp", "add", 10)
    assert add.startLine == 9 and add.nodeType == "synthetic"
    assert add.description == "Add two numbers.\n\nReturns the sum."
    assert _params(add) == [("a", "int", True, None, None), ("b", "int", False, "2", None)]

    search = tools["Code_Graph_Search"]  # renamed; explicit description; Context injected
    assert (
        search.handler == "search" and search.description == "Semantic search over the code graph"
    )
    assert _params(search) == [
        ("query", "str", True, None, "Search text"),
        ("limit", "int", False, "10", "Max hits"),
        ("labels", "list[str] | None", False, "None", None),
    ]

    ping = tools["ping"]  # bare decorator, no docstring
    assert ping.description is None and ping.params == []


def test_fastmcp_typed_register_function_with_stacked_decorators(analyze_files) -> None:
    src = '''
from fastmcp import FastMCP
from app.errors import handle_errors


def register(mcp: FastMCP):
    @mcp.tool(name="Documents", annotations={"readOnlyHint": True})
    @handle_errors("Documents API")
    async def documents(project_uuid: str, page: int = 1) -> str:
        """List documents."""
        return ""
'''
    tools = _tools(
        analyze_files(
            {
                "app/tools/documents.py": src,
                "app/errors.py": "def handle_errors(n):\n    return n\n",
            }
        )
    )
    doc = tools["Documents"]
    assert doc.handler == "documents" and doc.handlerLine == 9 and doc.startLine == 7
    assert doc.description == "List documents."
    assert _params(doc) == [
        ("project_uuid", "str", True, None, None),
        ("page", "int", False, "1", None),
    ]


def test_fastmcp_server_imported_from_another_module(analyze_files) -> None:
    files = {
        "app/server.py": "from fastmcp import FastMCP\n\nmcp = FastMCP('x')\n",
        "app/tools.py": "from app.server import mcp as server\n\n\n@server.tool\ndef hello(name: str) -> str:\n    return name\n",
    }
    tools = _tools(analyze_files(files))
    assert set(tools) == {"hello"}
    # The server's file decides the SDK, not the tools file (which never imports fastmcp).
    assert tools["hello"].framework == "fastmcp"


def test_official_sdk_fastmcp_is_mcp_python(analyze_files) -> None:
    src = "from mcp.server.fastmcp import FastMCP\n\nmcp = FastMCP('demo')\n\n\n@mcp.tool()\ndef echo(text: str) -> str:\n    return text\n"
    assert _tools(analyze_files({"srv.py": src}))["echo"].framework == "mcp-python"


def test_agent_tools_are_not_mcp_tools(analyze_files) -> None:
    src = """
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio

agent = Agent("model", toolsets=[MCPServerStdio("npx", args=[])])


@agent.tool
def roll(ctx, sides: int = 6) -> int:
    return 4
"""
    assert _tools(analyze_files({"agent.py": src})) == {}


def test_a_computed_tool_name_is_not_guessed(analyze_files) -> None:
    src = "from fastmcp import FastMCP\n\nmcp = FastMCP('x')\nNAME = 'a'\n\n\n@mcp.tool(name=NAME)\ndef a() -> str:\n    return ''\n"
    assert _tools(analyze_files({"srv.py": src})) == {}


# ── TypeScript: @modelcontextprotocol/sdk ───────────────────────────────────

TS_SERVER = """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

const server = new McpServer({ name: "demo", version: "1.0.0" });

server.tool("add", "Add two numbers",
  { a: z.number().describe("First"), b: z.number().optional().default(2) },
  async ({ a, b }) => ({ content: [] }));

server.tool("ping", { readOnlyHint: true }, async () => ({ content: [] }));

server.tool("lookup", { id: z.string() }, { readOnlyHint: true }, lookupHandler);

server.registerTool("echo",
  { title: "Echo", description: "Echo a message", inputSchema: { msg: z.string(), mode: z.enum(["a", "b"]) } },
  async ({ msg }) => ({ content: [] }));

async function lookupHandler({ id }) { return { content: [] }; }

const other = makeAgent();
other.tool("notAnMcpTool", async () => 1);
"""


def test_typescript_sdk_tool_overloads(analyze_files) -> None:
    tools = _tools(analyze_files({"src/server.ts": TS_SERVER}))
    assert set(tools) == {"add", "ping", "lookup", "echo"}

    add = tools["add"]
    assert add.framework == "mcp-sdk-ts" and add.description == "Add two numbers"
    assert add.handler is None and add.handlerLine == 9  # inline callback: its line
    assert _params(add) == [("a", "number", True, None, "First"), ("b", "number", False, "2", None)]

    ping = tools["ping"]  # (name, annotations, cb): the annotations are not params
    assert ping.params == [] and ping.description is None

    lookup = tools["lookup"]  # (name, shape, annotations, cb) with a named handler
    assert _params(lookup) == [("id", "string", True, None, None)]
    assert (lookup.handler, lookup.handlerLine) == ("lookupHandler", 19)

    echo = tools["echo"]
    assert echo.description == "Echo a message"
    assert _params(echo) == [
        ("msg", "string", True, None, None),
        ("mode", "enum(a,b)", True, None, None),
    ]


# ── Java: Spring AI ─────────────────────────────────────────────────────────

JAVA_TOOLS = """
package demo;

import org.springframework.ai.tool.annotation.Tool;
import org.springframework.ai.tool.annotation.ToolParam;
import org.springframework.ai.chat.model.ToolContext;

public class WeatherTools {
    @Tool(name = "current_weather", description = "Current weather for a city")
    public String weather(@ToolParam(description = "City name") String city,
                          @ToolParam(description = "Units", required = false) String units,
                          ToolContext ctx) {
        return "";
    }

    @Tool(description = "Server time")
    public String now() { return ""; }
}
"""
POM_MCP = "<project><dependencies><dependency><artifactId>spring-ai-starter-mcp-server-webmvc</artifactId></dependency></dependencies></project>"


def test_spring_ai_tools_in_an_mcp_server(analyze_files) -> None:
    tools = _tools(
        analyze_files({"pom.xml": POM_MCP, "src/main/java/demo/WeatherTools.java": JAVA_TOOLS})
    )
    assert set(tools) == {"current_weather", "now"}  # `now`: default name = method name
    w = tools["current_weather"]
    assert (w.framework, w.handler, w.description) == (
        "spring-ai-mcp",
        "weather",
        "Current weather for a city",
    )
    assert _params(w) == [
        ("city", "String", True, None, "City name"),
        ("units", "String", False, None, "Units"),
    ]


def test_spring_ai_tool_without_an_mcp_server_is_a_chat_tool(analyze_files) -> None:
    pom = "<project><dependencies><dependency><artifactId>spring-ai-starter-model-openai</artifactId></dependency></dependencies></project>"
    assert (
        _tools(analyze_files({"pom.xml": pom, "src/main/java/demo/WeatherTools.java": JAVA_TOOLS}))
        == {}
    )


def test_mcp_tool_annotation_needs_no_build_evidence(analyze_files) -> None:
    src = """
package demo;

import org.springaicommunity.mcp.annotation.McpTool;
import org.springaicommunity.mcp.annotation.McpToolParam;

public class Calc {
    @McpTool(name = "add", description = "Add")
    public int add(@McpToolParam(description = "First", required = true) int a, int b) { return a + b; }
}
"""
    tools = _tools(analyze_files({"src/main/java/demo/Calc.java": src}))
    assert _params(tools["add"]) == [
        ("a", "int", True, None, "First"),
        ("b", "int", True, None, None),
    ]


# ── C#: ModelContextProtocol ────────────────────────────────────────────────

CS_TOOLS = """
using System.ComponentModel;
using ModelContextProtocol.Server;

[McpServerToolType]
public static class EchoTools
{
    [McpServerTool(Name = "echo"), Description("Echoes the message")]
    public static string Echo([Description("The message")] string message, int times = 1,
                              string? prefix, CancellationToken ct = default) => message;

    /// <summary>
    /// Reverses the message.
    /// </summary>
    [McpServerTool]
    public static string Reverse(IMcpServer server, string message) => message;
}
"""


def test_csharp_sdk_tools(analyze_files) -> None:
    tools = _tools(analyze_files({"src/EchoTools.cs": CS_TOOLS}))
    echo = tools["echo"]
    assert (echo.framework, echo.handler, echo.description) == (
        "mcp-csharp",
        "Echo",
        "Echoes the message",
    )
    assert _params(echo) == [
        ("message", "string", True, None, "The message"),
        ("times", "int", False, "1", None),
        ("prefix", "string?", True, None, None),  # nullable alone is still required
    ]
    reverse = tools[None]  # no Name =: the SDK-derived name is not guessed
    assert reverse.handler == "Reverse" and reverse.name == "Reverse"
    assert reverse.description == "Reverses the message."
    assert _params(reverse) == [("message", "string", True, None, None)]


# ── Go: mark3labs/mcp-go ────────────────────────────────────────────────────

GO_SERVER = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    "github.com/mark3labs/mcp-go/server"
)

func main() {
    s := server.NewMCPServer("demo", "1.0.0")
    tool := mcp.NewTool("hello",
        mcp.WithDescription("Say hello"),
        mcp.WithString("name", mcp.Required(), mcp.Description("Who to greet")),
        mcp.WithNumber("times", mcp.DefaultNumber(1)),
    )
    s.AddTool(tool, helloHandler)
    s.AddTool(mcp.NewTool("ping"), func(ctx context.Context, r mcp.CallToolRequest) (*mcp.CallToolResult, error) {
        return nil, nil
    })
}

func helloHandler(ctx context.Context, r mcp.CallToolRequest) (*mcp.CallToolResult, error) {
    return nil, nil
}
"""


def test_go_mcp_tools(analyze_files) -> None:
    tools = _tools(analyze_files({"main.go": GO_SERVER}))
    hello = tools["hello"]
    assert (hello.framework, hello.description, hello.handler) == (
        "mcp-go",
        "Say hello",
        "helloHandler",
    )
    assert hello.handlerLine == 22
    assert _params(hello) == [
        ("name", "string", True, None, "Who to greet"),
        ("times", "number", False, "1", None),
    ]
    ping = tools["ping"]
    assert ping.description is None and ping.params == [] and ping.handlerLine == 17


def test_a_local_class_named_fastmcp_is_not_a_server(analyze_files) -> None:
    """breezeai-agents registers a copy of the Breeze tools on an in-process shim class named
    ``FastMCP``; nothing serves them over MCP, so they are not MCP tools."""
    files = {
        "app/shim.py": "class FastMCP:\n    def tool(self, **kw):\n        return lambda f: f\n",
        "app/tools.py": (
            "from app.shim import FastMCP\n\n\ndef register(mcp: FastMCP):\n"
            "    @mcp.tool(name='Code_Graph_Search')\n    async def search(query: str) -> str:\n        return ''\n"
        ),
        "app/direct.py": "from app import shim\n\nregistry = shim.FastMCP()\n",
    }
    assert _tools(analyze_files(files)) == {}


# ── Review regressions ──────────────────────────────────────────────────────


def test_go_reused_tool_variable_keeps_each_handler(analyze_files) -> None:
    src = """
package main

import "github.com/mark3labs/mcp-go/mcp"

var shared = mcp.NewTool("shared")

func regA(s *Server) {
    tool := mcp.NewTool("a")
    s.AddTool(tool, handleA)
}

func regB(s *Server) {
    tool := mcp.NewTool("b")
    s.AddTool(tool, handleB)
    tool = mcp.NewTool("c")
    s.AddTool(tool, handleC)
    s.AddTool(shared, handleShared)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert {k: t.handler for k, t in tools.items()} == {
        "a": "handleA",
        "b": "handleB",
        "c": "handleC",
        "shared": "handleShared",
    }


def test_typescript_class_field_server_and_wrapped_callback(analyze_files) -> None:
    src = """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

class Srv {
  private server = new McpServer({ name: "x", version: "1" });
  init() {
    this.server.tool("search", "Search", { q: z.string() }, withErrors(async ({ q }) => ({ content: [] })));
    this.server.tool("named", withErrors(namedHandler));
  }
}
async function namedHandler() { return { content: [] }; }
"""
    tools = _tools(analyze_files({"src/srv.ts": src}))
    assert set(tools) == {"search", "named"}
    assert tools["search"].description == "Search" and tools["search"].handlerLine == 8
    assert tools["named"].handler == "namedHandler"


def test_spring_ai_tool_only_in_the_mcp_server_module(analyze_files) -> None:
    files = {
        "pom.xml": "<project><modules><module>mcp-server</module></modules></project>",
        "mcp-server/pom.xml": POM_MCP,
        "mcp-server/src/main/java/demo/WeatherTools.java": JAVA_TOOLS,
        "chat-app/src/main/java/demo/WeatherTools.java": JAVA_TOOLS,
    }
    statements = analyze_files(files)
    paths = {s.path for s in statements if s.semanticType == "mcp_tool"}
    assert paths == {"mcp-server/src/main/java/demo/WeatherTools.java"}


def test_python_attribute_named_tools_is_not_scanned(analyze_files) -> None:
    src = "from fastmcp import FastMCP\n\nmcp = FastMCP('x')\nprint(mcp.tools)\n"
    assert _tools(analyze_files({"app/server.py": src})) == {}
