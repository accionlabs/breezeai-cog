"""MCP server tools (BREEZEAI-1457): one ``mcp_tool`` statement per tool, per SDK.

Every case runs the whole pipeline (``analyze_files``), which also fails on any entry-point
contract error (``parsers/contract.py``). Each SDK covers a renamed tool, a tool without a
description, optional params with defaults, and a look-alike that must not be read as a tool.
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
    reverse = tools["reverse"]  # no Name =: the SDK's derived name (snake_case)
    assert reverse.handler == "Reverse" and reverse.name == "reverse"
    assert reverse.description == "Reverses the message."
    assert _params(reverse) == [("message", "string", True, None, None)]


def test_csharp_tool_name_derived_like_the_sdk(analyze_files) -> None:
    src = """
using System.Threading.Tasks;
using ModelContextProtocol.Server;

[McpServerToolType]
public class Tools
{
    [McpServerTool] public static async Task<string> GetWeatherAsync(string city) => city;
    [McpServerTool] public static ValueTask PingAsync() => default;
    [McpServerTool] public static string ExportAsync() => "";
    [McpServerTool] public static string GetHTTPStatus2() => "";
    [McpServerTool] public static string Async() => "";
}
"""
    tools = _tools(analyze_files({"src/Tools.cs": src}))
    # `Async` dropped only for an async return type; acronyms grouped as System.Text.Json does
    assert set(tools) == {"get_weather", "ping", "export_async", "get_http_status2", "async"}
    assert tools["get_weather"].handler == "GetWeatherAsync"


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


# ── Review fixes (BREEZEAI-1457) ────────────────────────────────────────────

TS_HEADER = """
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

const server = new McpServer({ name: "demo", version: "1.0.0" });
"""


def test_typescript_unreadable_schema_is_unknown_not_empty(analyze_files) -> None:
    src = (
        TS_HEADER
        + """
server.tool("get", "Get one", { id: idSchema }, async ({ id }) => ({ content: [] }));
server.tool("spread", { ...common, q: z.string() }, async () => ({ content: [] }));
server.tool("coerce", { n: z.coerce.number() }, async () => ({ content: [] }));
server.tool("shared", "Shared", sharedShape, async () => ({ content: [] }));
server.registerTool("reg", { description: "R", inputSchema: MySchema.shape }, async () => ({ content: [] }));
server.registerTool("json", { inputSchema: { type: "object", properties: { a: { type: "string" } }, required: fields } }, async () => ({ content: [] }));
server.tool("empty", "Nothing", {}, async () => ({ content: [] }));
server.tool("none", "No schema", async () => ({ content: [] }));
server.registerTool("plain", { description: "P" }, async () => ({ content: [] }));
"""
    )
    tools = _tools(analyze_files({"src/server.ts": src}))
    for name in ("get", "spread", "coerce", "shared", "reg", "json"):
        assert tools[name].params is None, name
    assert tools["get"].description == "Get one"
    for name in ("empty", "none", "plain"):
        assert tools[name].params == [], name


def test_typescript_zod_wrappers_and_partial_objects(analyze_files) -> None:
    src = (
        TS_HEADER
        + """
server.tool("wrap", {
  a: z.optional(z.string().describe("A")),
  b: z.nullable(z.number()),
  c: z.nullish(z.boolean()),
  d: z.optional(someSchema),
}, async () => ({ content: [] }));
server.registerTool("part", { inputSchema: z.object({ x: z.string(), y: z.number() }).partial() }, async () => ({ content: [] }));
server.registerTool("deep", { inputSchema: z.object({ x: z.string() }).strict().deepPartial() }, async () => ({ content: [] }));
server.registerTool("some", { inputSchema: z.object({ x: z.string(), y: z.number() }).partial({ x: true }) }, async () => ({ content: [] }));
server.registerTool("ext", { inputSchema: z.object({ x: z.string() }).extend({ y: z.number() }) }, async () => ({ content: [] }));
server.registerTool("whole", { inputSchema: z.object({ x: z.string() }).describe("in") }, async () => ({ content: [] }));
"""
    )
    tools = _tools(analyze_files({"src/server.ts": src}))
    assert _params(tools["wrap"]) == [
        ("a", "string", False, None, "A"),
        ("b", "number", True, None, None),  # nullable: the key is still required
        ("c", "boolean", False, None, None),
        ("d", None, False, None, None),  # the wrapped schema is a variable: type unknown
    ]
    assert _params(tools["part"]) == [
        ("x", "string", False, None, None),
        ("y", "number", False, None, None),
    ]
    assert _params(tools["deep"]) == [("x", "string", False, None, None)]
    assert tools["some"].params is None and tools["ext"].params is None
    assert _params(tools["whole"]) == [("x", "string", True, None, None)]


def test_typescript_handler_factory_argument_is_not_the_handler(analyze_files) -> None:
    files = {
        "src/client.ts": "export const apiClient = { get() {} };\n",
        "src/server.ts": TS_HEADER
        + """import { apiClient } from "./client";
server.tool("search", "Search", { q: z.string() }, createHandler(apiClient));
server.tool("wrapped", "W", withRetry(async () => ({ content: [] })));
""",
    }
    tools = _tools(analyze_files(files))
    search = tools["search"]
    assert search.handler is None and search.handlerPath is None
    assert search.handlerLine == 7  # the factory call builds the handler there
    assert tools["wrapped"].handler is None and tools["wrapped"].handlerLine == 8


def test_python_annotated_metadata_and_field_defaults(analyze_files) -> None:
    src = """
from typing import Annotated, Literal
from fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP("x")


@mcp.tool
def listing(
    sort: Annotated[Literal["asc", "desc"], "Sort order"],
    order: Annotated[Literal["asc", "desc"], Field(description="Order")],
    size: Annotated[int, Field(default=5)],
    must: int = Field(default=..., description="Must"),
    also: int = Field(...),
) -> str:
    return ""
"""
    tools = _tools(analyze_files({"app/server.py": src}))
    assert _params(tools["listing"]) == [
        ("sort", 'Literal["asc", "desc"]', True, None, "Sort order"),
        ("order", 'Literal["asc", "desc"]', True, None, "Order"),
        ("size", "int", False, "5", None),
        ("must", "int", True, None, "Must"),
        ("also", "int", True, None, None),
    ]


def test_python_literal_type_strings_are_not_a_description(analyze_files) -> None:
    src = (
        "from typing import Annotated, Literal\nfrom fastmcp import FastMCP\n\nmcp = FastMCP('x')\n\n\n"
        "@mcp.tool\ndef f(sort: Annotated[Literal['asc', 'desc'], Field(ge=1)]) -> str:\n    return ''\n"
    )
    [p] = _tools(analyze_files({"app/server.py": src}))["f"].params or []
    assert p.description is None


POM_BOM_PARENT = """<project>
  <modules><module>chat</module></modules>
  <dependencyManagement><dependencies>
    <dependency><artifactId>spring-ai-starter-mcp-server-webmvc</artifactId></dependency>
  </dependencies></dependencyManagement>
  <!-- <dependency><artifactId>spring-ai-starter-mcp-server</artifactId></dependency> -->
</project>"""


def test_spring_ai_bom_entry_is_not_an_mcp_server(analyze_files) -> None:
    files = {
        "pom.xml": POM_BOM_PARENT,
        "chat/pom.xml": "<project><dependencies><dependency><artifactId>spring-ai-starter-model-openai</artifactId></dependency></dependencies></project>",
        "chat/src/main/java/demo/WeatherTools.java": JAVA_TOOLS,
    }
    assert _tools(analyze_files(files)) == {}


def test_spring_ai_gradle_platform_is_not_an_mcp_server(analyze_files) -> None:
    gradle = 'dependencies {\n  implementation platform("org.springframework.ai:spring-ai-mcp-server-bom:1.0.0")\n}\n'
    files = {"build.gradle": gradle, "src/main/java/demo/WeatherTools.java": JAVA_TOOLS}
    assert _tools(analyze_files(files)) == {}


def test_spring_ai_tool_needs_the_spring_import(analyze_files) -> None:
    other = JAVA_TOOLS.replace(
        "import org.springframework.ai.tool.annotation.Tool;", "import com.acme.Tool;"
    )
    files = {"pom.xml": POM_MCP, "src/main/java/demo/WeatherTools.java": other}
    assert _tools(analyze_files(files)) == {}
    fqn = (
        "package demo;\nimport org.springframework.ai.chat.model.ToolContext;\n"
        'public class T {\n  @org.springframework.ai.tool.annotation.Tool(description = "D")\n'
        '  public String now() { return ""; }\n}\n'
    )
    files = {"pom.xml": POM_MCP, "src/main/java/demo/T.java": fqn}
    assert set(_tools(analyze_files(files))) == {"now"}


JAVA_SDK = '''
package demo;

import io.modelcontextprotocol.server.McpServerFeatures;
import io.modelcontextprotocol.spec.McpSchema;

public class Tools {
    private static final String SCHEMA = "{}";

    McpServerFeatures.SyncToolSpecification calc() {
        return new McpServerFeatures.SyncToolSpecification(
            new McpSchema.Tool("calc", "Calculator", """
                {"type": "object", "properties": {"a": {"type": "number", "description": "First"},
                 "b": {"type": "number"}}, "required": ["a"]}
                """),
            (exchange, args) -> null);
    }

    McpServerFeatures.AsyncToolSpecification echo() {
        var tool = McpSchema.Tool.builder().name("echo").description("Echo")
            .inputSchema(mapper, "{\\"type\\":\\"object\\",\\"properties\\":{\\"msg\\":{\\"type\\":\\"string\\"}}}").build();
        return McpServerFeatures.AsyncToolSpecification.builder().tool(tool).callHandler(this::doEcho).build();
    }

    McpServerFeatures.SyncToolSpecification dyn() {
        return new McpServerFeatures.SyncToolSpecification(new McpSchema.Tool("dyn", "Dynamic", SCHEMA), Tools::run);
    }

    McpServerFeatures.SyncToolSpecification computed(String n) {
        return new McpServerFeatures.SyncToolSpecification(new McpSchema.Tool(n, "x", SCHEMA), Tools::run);
    }

    Object doEcho(Object exchange, Object req) { return null; }
    static Object run(Object exchange, Object args) { return null; }
}
'''


def test_mcp_java_sdk_tool_specifications(analyze_files) -> None:
    tools = _tools(analyze_files({"src/main/java/demo/Tools.java": JAVA_SDK}))
    assert set(tools) == {"calc", "echo", "dyn"}  # a computed name is not guessed
    calc = tools["calc"]
    assert (calc.framework, calc.description) == ("mcp-java-sdk", "Calculator")
    assert calc.handler is None and calc.handlerLine == 16  # the lambda's line
    assert _params(calc) == [
        ("a", "number", True, None, "First"),
        ("b", "number", False, None, None),
    ]
    echo = tools["echo"]
    assert (echo.description, echo.handler, echo.handlerLine) == ("Echo", "doEcho", 33)
    assert _params(echo) == [("msg", "string", False, None, None)]
    dyn = tools["dyn"]
    assert dyn.params is None and (dyn.handler, dyn.handlerLine) == ("run", 34)


def test_mcp_java_sdk_needs_the_sdk(analyze_files) -> None:
    src = JAVA_SDK.replace("io.modelcontextprotocol", "com.acme")
    assert _tools(analyze_files({"src/main/java/demo/Tools.java": src})) == {}


def test_go_typed_tool_handler_is_looked_through(analyze_files) -> None:
    src = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    "github.com/mark3labs/mcp-go/server"
)

func main() {
    s := server.NewMCPServer("demo", "1.0.0")
    s.AddTool(mcp.NewTool("typed"), mcp.NewTypedToolHandler(typedHandler))
    s.AddTool(mcp.NewTool("other"), server.NewTypedToolHandler(typedHandler))
}

func typedHandler(ctx context.Context, r mcp.CallToolRequest, args Args) (*mcp.CallToolResult, error) {
    return nil, nil
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    for name in ("typed", "other"):
        assert (tools[name].handler, tools[name].handlerLine) == ("typedHandler", 15), name


def test_go_options_that_cannot_be_read_leave_params_unknown(analyze_files) -> None:
    src = """
package main

import "github.com/mark3labs/mcp-go/mcp"

func reg(s *Server) {
    s.AddTool(mcp.NewTool("helper", mcp.WithString("q"), commonOpts()), h)
    s.AddTool(mcp.NewTool("raw", mcp.WithRawInputSchema(schema)), h)
    s.AddTool(mcp.NewTool("known", mcp.WithString("q"), mcp.WithReadOnlyHintAnnotation(true)), h)
    s.AddTool(mcp.NewTool("titled", mcp.WithTitleAnnotation("T"), mcp.WithOutputSchema[Out]()), h)
    s.AddTool(mcp.NewTool("newer", mcp.WithString("q"), mcp.WithStringEnum("mode", "a", "b")), h)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert tools["helper"].params is None and tools["raw"].params is None
    assert _params(tools["known"]) == [("q", "string", False, None, None)]
    # An option the detector does not know may add a parameter: unknown, not a partial list.
    assert tools["newer"].params is None
    assert tools["titled"].params == []


def test_go_package_tool_var_is_not_paired_through_a_shadowing_local(analyze_files) -> None:
    src = """
package main

import "github.com/mark3labs/mcp-go/mcp"

var tool = mcp.NewTool("pkg_level")

func other(s *Server) {
    tool := mcp.NewTool("other")
    s.AddTool(tool, otherHandler)
}

func pkg(s *Server) {
    s.AddTool(tool, pkgHandler)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert tools["other"].handler == "otherHandler"
    assert tools["pkg_level"].handler == "pkgHandler"


def test_go_package_tool_var_registered_twice_has_no_handler(analyze_files) -> None:
    src = """
package main

import "github.com/mark3labs/mcp-go/mcp"

var tool = mcp.NewTool("pkg_level")

func a(s *Server) { s.AddTool(tool, aHandler) }

func b(s *Server) { s.AddTool(tool, bHandler) }
"""
    assert _tools(analyze_files({"main.go": src}))["pkg_level"].handler is None


def test_go_reassigned_tool_var_stops_at_the_next_assignment(analyze_files) -> None:
    src = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    "github.com/mark3labs/mcp-go/server"
)

func reg(s *server.MCPServer) {
    t := mcp.NewTool("first")
    s.AddTools(server.ServerTool{Tool: t, Handler: firstHandler})
    t = mcp.NewTool("second")
    s.AddTool(t, secondHandler)
    u := mcp.NewTool("unregistered")
    u = mcp.NewTool("third")
    s.AddTool(u, thirdHandler)
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert {k: t.handler for k, t in tools.items()} == {
        "first": "firstHandler",
        "second": "secondHandler",
        "unregistered": None,
        "third": "thirdHandler",
    }


def test_go_server_tool_literal_gives_the_handler(analyze_files) -> None:
    src = """
package main

import (
    "github.com/mark3labs/mcp-go/mcp"
    srv "github.com/mark3labs/mcp-go/server"
)

func reg(s *srv.MCPServer) {
    tools := []srv.ServerTool{
        {Tool: mcp.NewTool("listed"), Handler: listedHandler},
    }
    s.AddTools(
        srv.ServerTool{Tool: mcp.NewTool("inline"), Handler: inlineHandler},
        &srv.ServerTool{Tool: mcp.NewTool("pointer"), Handler: pointerHandler},
    )
    s.AddTools(tools...)
}

func inlineHandler(ctx context.Context, r mcp.CallToolRequest) (*mcp.CallToolResult, error) {
    return nil, nil
}
"""
    tools = _tools(analyze_files({"main.go": src}))
    assert (tools["inline"].handler, tools["inline"].handlerLine) == ("inlineHandler", 20)
    assert tools["pointer"].handler == "pointerHandler"
    assert tools["listed"].handler == "listedHandler"  # element of a []ServerTool literal


def test_fastmcp_nullable_context_disabled_tools_and_excluded_args(analyze_files) -> None:
    src = """
from typing import Optional, Union
import mcp.server.fastmcp
from fastmcp import FastMCP, Context

mcp = FastMCP("demo")


@mcp.tool()
def a(q: str, ctx: Context | None = None) -> str:
    return q


@mcp.tool()
def b(q: str, ctx: Optional[Context] = None, c2: Union[Context, None] = None) -> str:
    return q


@mcp.tool()
def c(q: str, ctx: mcp.server.fastmcp.Context, maybe: str | None = None) -> str:
    return q


@mcp.tool(enabled=False)
def hidden(q: str) -> str:
    return q


@mcp.tool(exclude_args=["user_id"])
def scoped(q: str, user_id: str = "") -> str:
    return q


@mcp.tool(exclude_args=EXCLUDED)
def computed(q: str, user_id: str = "") -> str:
    return q
"""
    tools = _tools(analyze_files({"srv.py": src}))
    assert set(tools) == {"a", "b", "c", "scoped", "computed"}
    for name in ("a", "b"):
        assert _params(tools[name]) == [("q", "str", True, None, None)], name
    assert _params(tools["c"]) == [
        ("q", "str", True, None, None),
        ("maybe", "str | None", False, "None", None),
    ]
    assert _params(tools["scoped"]) == [("q", "str", True, None, None)]
    assert tools["computed"].params is None


def test_mcp_java_sdk_schema_too_deep_to_load_leaves_params_unknown(
    analyze_files, monkeypatch
) -> None:
    from types import SimpleNamespace

    from breezeai_cog.parsers.java_mcp import tools as java_tools

    def too_deep(_text: str) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(java_tools, "json", SimpleNamespace(loads=too_deep))
    tools = _tools(analyze_files({"src/main/java/demo/Tools.java": JAVA_SDK}))
    assert set(tools) == {"calc", "echo", "dyn"}  # the file still parses
    assert tools["calc"].params is None and tools["echo"].params is None


# ── Kotlin: Spring AI, MCP Java SDK, official Kotlin SDK ────────────────────

KOTLIN_TOOLS = """
package demo

import org.springframework.ai.tool.annotation.Tool
import org.springframework.ai.tool.annotation.ToolParam
import org.springframework.ai.chat.model.ToolContext

class WeatherTools {
    @Tool(name = "current_weather", description = "Current weather for a city")
    fun weather(
        @ToolParam(description = "City name") city: String,
        @ToolParam(description = "Units", required = false) units: String,
        days: Int = 3,
        lang: String?,
        @ToolParam(description = "Region", required = true) region: String? = null,
        ctx: ToolContext,
    ): String = ""

    @Tool
    fun now(): String = ""
}
"""
GRADLE_KTS_MCP = """dependencies {
    implementation("org.springframework.ai:spring-ai-starter-mcp-server-webflux")
}
"""


def test_kotlin_spring_ai_tools_with_a_gradle_kts_starter(analyze_files) -> None:
    files = {"build.gradle.kts": GRADLE_KTS_MCP, "src/main/kotlin/demo/WeatherTools.kt": KOTLIN_TOOLS}
    tools = _tools(analyze_files(files))
    assert set(tools) == {"current_weather", "now"}  # `now`: default name = function name
    w = tools["current_weather"]
    assert (w.framework, w.handler, w.handlerLine, w.description) == (
        "spring-ai-mcp",
        "weather",
        9,
        "Current weather for a city",
    )
    assert w.parentId and w.parentId.endswith("#WeatherTools#weather@9")
    assert _params(w) == [
        ("city", "String", True, None, "City name"),
        ("units", "String", False, None, "Units"),
        # Spring AI reads `required` from the annotation only: a default or a nullable type
        # does not make a parameter optional.
        ("days", "Int", True, "3", None),
        ("lang", "String?", True, None, None),
        ("region", "String?", True, "null", "Region"),
    ]
    assert tools["now"].description is None and _params(tools["now"]) == []


def test_kotlin_long_default_is_cut(analyze_files) -> None:
    src = KOTLIN_TOOLS.replace("days: Int = 3,", 'days: String = "' + "x" * 300 + '",')
    files = {"build.gradle.kts": GRADLE_KTS_MCP, "src/main/kotlin/demo/WeatherTools.kt": src}
    days = next(p for p in _tools(analyze_files(files))["current_weather"].params if p.name == "days")
    assert len(days.default) == 200 and days.default.endswith("…")


def test_java_spring_ai_tools_with_a_gradle_kts_starter(analyze_files) -> None:
    files = {"build.gradle.kts": GRADLE_KTS_MCP, "src/main/java/demo/WeatherTools.java": JAVA_TOOLS}
    assert set(_tools(analyze_files(files))) == {"current_weather", "now"}


def test_kotlin_spring_ai_tool_without_an_mcp_server_is_a_chat_tool(analyze_files) -> None:
    gradle = 'dependencies {\n    implementation("org.springframework.ai:spring-ai-starter-model-openai")\n}\n'
    files = {"build.gradle.kts": gradle, "src/main/kotlin/demo/WeatherTools.kt": KOTLIN_TOOLS}
    assert _tools(analyze_files(files)) == {}
    other = KOTLIN_TOOLS.replace(
        "import org.springframework.ai.tool.annotation.Tool\n", "import com.acme.Tool\n"
    )
    files = {"build.gradle.kts": GRADLE_KTS_MCP, "src/main/kotlin/demo/WeatherTools.kt": other}
    assert _tools(analyze_files(files)) == {}  # not Spring AI's @Tool


def test_kotlin_mcp_tool_annotation_needs_no_build_evidence(analyze_files) -> None:
    src = """
package demo

import org.springaicommunity.mcp.annotation.McpTool
import org.springaicommunity.mcp.annotation.McpToolParam

class Calc {
    @McpTool(name = "add", description = "Add")
    fun add(@McpToolParam(description = "First") a: Int, @McpToolParam(required = false) b: Int): Int = a + b

    @McpTool
    suspend fun reset() {}
}
"""
    tools = _tools(analyze_files({"src/main/kotlin/demo/Calc.kt": src}))
    assert set(tools) == {"add", "reset"}
    assert tools["add"].framework == "spring-ai-mcp"
    assert _params(tools["add"]) == [
        ("a", "Int", True, None, "First"),
        ("b", "Int", False, None, None),
    ]
    assert tools["reset"].description is None


KOTLIN_SDK = '''
package demo

import io.modelcontextprotocol.kotlin.sdk.CallToolResult
import io.modelcontextprotocol.kotlin.sdk.Tool
import io.modelcontextprotocol.kotlin.sdk.server.Server

fun Server.registerTools() {
    addTool(
        name = "calc",
        description = "Calculator",
        inputSchema = Tool.Input(
            properties = buildJsonObject {
                putJsonObject("a") { put("type", "number"); put("description", "First") }
                putJsonObject("b") { put("type", "number") }
            },
            required = listOf("a"),
        ),
    ) { request -> CallToolResult(content = listOf()) }
    addTool("ping", "Ping") { _ -> CallToolResult(content = listOf()) }
    addTool(name = "dyn", description = "Dynamic", inputSchema = schemaFor("dyn")) { _ -> null }
    addTool(name = computedName(), description = "x") { _ -> null }
}
'''


def test_kotlin_sdk_add_tool(analyze_files) -> None:
    tools = _tools(analyze_files({"src/main/kotlin/demo/Server.kt": KOTLIN_SDK}))
    assert set(tools) == {"calc", "ping", "dyn"}  # a computed name is not guessed
    calc = tools["calc"]
    assert (calc.framework, calc.description, calc.handler, calc.handlerLine) == (
        "mcp-kotlin",
        "Calculator",
        None,
        19,  # the trailing lambda's line
    )
    assert _params(calc) == [
        ("a", "number", True, None, "First"),
        ("b", "number", False, None, None),
    ]
    assert tools["ping"].description == "Ping" and tools["ping"].params == []  # no schema
    assert tools["dyn"].params is None  # a schema that cannot be read


def test_kotlin_add_tool_without_the_mcp_sdk_is_not_a_tool(analyze_files) -> None:
    src = KOTLIN_SDK.replace("io.modelcontextprotocol.kotlin.sdk", "com.acme.agent")
    assert _tools(analyze_files({"src/main/kotlin/demo/Server.kt": src})) == {}


KOTLIN_JAVA_SDK = '''
package demo

import io.modelcontextprotocol.server.McpServerFeatures
import io.modelcontextprotocol.spec.McpSchema

class Tools {
    fun calc() = McpServerFeatures.SyncToolSpecification(
        McpSchema.Tool("calc", "Calculator", """
            {"type": "object", "properties": {"a": {"type": "number"}}, "required": ["a"]}
        """.trimIndent())
    ) { exchange, args -> null }

    fun echo(): McpServerFeatures.AsyncToolSpecification {
        val tool = McpSchema.Tool.builder().name("echo").description("Echo")
            .inputSchema(mapper, "{\\"type\\":\\"object\\",\\"properties\\":{\\"msg\\":{\\"type\\":\\"string\\"}}}").build()
        return McpServerFeatures.AsyncToolSpecification.builder().tool(tool).callHandler(this::doEcho).build()
    }

    fun doEcho(exchange: Any, req: Any): Any? = null
}
'''


def test_kotlin_mcp_java_sdk_tool_specifications(analyze_files) -> None:
    tools = _tools(analyze_files({"src/main/kotlin/demo/Tools.kt": KOTLIN_JAVA_SDK}))
    assert set(tools) == {"calc", "echo"}
    calc = tools["calc"]
    assert (calc.framework, calc.description, calc.handler, calc.handlerLine) == (
        "mcp-java-sdk",
        "Calculator",
        None,
        12,  # the trailing lambda's line
    )
    assert _params(calc) == [("a", "number", True, None, None)]
    echo = tools["echo"]
    assert (echo.description, echo.handler, echo.handlerLine) == ("Echo", "doEcho", 20)
    assert _params(echo) == [("msg", "string", False, None, None)]


def test_kotlin_schema_too_deep_to_load_leaves_params_unknown(analyze_files, monkeypatch) -> None:
    from types import SimpleNamespace

    from breezeai_cog.parsers.java_mcp import tools as java_tools

    def too_deep(_text: str) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(java_tools, "json", SimpleNamespace(loads=too_deep))
    tools = _tools(analyze_files({"src/main/kotlin/demo/Tools.kt": KOTLIN_JAVA_SDK}))
    assert set(tools) == {"calc", "echo"}  # the file still parses
    assert tools["calc"].params is None and tools["echo"].params is None


def test_kotlin_aliased_spring_ai_tool(analyze_files) -> None:
    aliased = KOTLIN_TOOLS.replace(
        "import org.springframework.ai.tool.annotation.Tool\n",
        "import org.springframework.ai.tool.annotation.Tool as AiTool\n",
    ).replace("@Tool(", "@AiTool(").replace("@Tool\n", "@AiTool\n")
    files = {"build.gradle.kts": GRADLE_KTS_MCP, "src/main/kotlin/demo/WeatherTools.kt": aliased}
    tools = _tools(analyze_files(files))
    assert set(tools) == {"current_weather", "now"}
    assert tools["current_weather"].framework == "spring-ai-mcp"
    assert _params(tools["current_weather"])[0] == ("city", "String", True, None, "City name")


def test_kotlin_aliased_tool_needs_the_starter_and_the_spring_class(analyze_files) -> None:
    aliased = KOTLIN_TOOLS.replace(
        "import org.springframework.ai.tool.annotation.Tool\n",
        "import org.springframework.ai.tool.annotation.Tool as AiTool\n",
    ).replace("@Tool(", "@AiTool(").replace("@Tool\n", "@AiTool\n")
    assert _tools(analyze_files({"src/main/kotlin/demo/WeatherTools.kt": aliased})) == {}
    unrelated = KOTLIN_TOOLS.replace(
        "import org.springframework.ai.tool.annotation.Tool\n", "import com.acme.Tool as AiTool\n"
    ).replace("@Tool(", "@AiTool(").replace("@Tool\n", "@AiTool\n")
    files = {"build.gradle.kts": GRADLE_KTS_MCP, "src/main/kotlin/demo/WeatherTools.kt": unrelated}
    assert _tools(analyze_files(files)) == {}


KOTLIN_ADD_TOOLS = '''
package demo

import io.modelcontextprotocol.kotlin.sdk.RegisteredTool
import io.modelcontextprotocol.kotlin.sdk.Tool as McpTool
import io.modelcontextprotocol.kotlin.sdk.server.Server

fun register(server: Server) {
    server.addTools(
        listOf(
            RegisteredTool(
                McpTool(
                    name = "search",
                    description = "Search",
                    inputSchema = McpTool.Input(
                        properties = buildJsonObject { putJsonObject("q") { put("type", "string") } },
                        required = listOf("q"),
                    ),
                ),
            ) { request -> null },
            RegisteredTool(tool = McpTool(name = "stats", description = null), handler = ::stats),
            others(),
        )
    )
    server.addTools(myTools)
    server.addTools(buildTools())
}

fun stats(request: Any): Any? = null
'''


def test_kotlin_sdk_add_tools_with_an_inline_list(analyze_files) -> None:
    tools = _tools(analyze_files({"src/main/kotlin/demo/Server.kt": KOTLIN_ADD_TOOLS}))
    assert set(tools) == {"search", "stats"}  # a list from a variable or a call is not read
    search = tools["search"]
    assert (search.framework, search.description, search.handler, search.handlerLine) == (
        "mcp-kotlin",
        "Search",
        None,
        20,  # the trailing lambda's line
    )
    assert _params(search) == [("q", "string", True, None, None)]
    stats = tools["stats"]
    assert (stats.description, stats.handler, stats.handlerLine, stats.params) == (
        None,
        "stats",
        29,
        [],
    )


def test_kotlin_add_tools_without_the_mcp_sdk_is_not_a_tool(analyze_files) -> None:
    src = KOTLIN_ADD_TOOLS.replace("io.modelcontextprotocol.kotlin.sdk", "com.acme.agent")
    assert _tools(analyze_files({"src/main/kotlin/demo/Server.kt": src})) == {}


def _first(node, kind: str):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == kind:
            return n
        stack.extend(n.named_children)
    return None


@pytest.mark.parametrize(
    ("language", "kind", "reader"),
    [
        ("java", "binary_expression", "breezeai_cog.parsers.java_mcp.tools"),
        ("kotlin", "additive_expression", "breezeai_cog.parsers.kotlin_mcp.tools"),
    ],
)
def test_a_long_string_concatenation_is_read_without_recursion(language, kind, reader) -> None:
    import importlib
    import sys

    from breezeai_cog.parsers.treesitter import parse_source

    module = importlib.import_module(reader)
    chain = " + ".join(['"ab"'] * (sys.getrecursionlimit() + 500))
    decl = f'class A {{ String x = {chain}; }}' if language == "java" else f"val x = {chain}"
    src = decl.encode()
    node = _first(parse_source(language, src, 0).root_node, kind)
    assert module._string(node, src) == "ab" * (sys.getrecursionlimit() + 500)
    other = (b'class A { boolean x = "a" == "b"; }' if language == "java" else b'val x = "a" - "b"')
    assert module._string(_first(parse_source(language, other, 0).root_node, kind), other) is None
