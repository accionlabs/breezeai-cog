"""MCP client calls (``mcp_call``, outbound): a proven MCP client calls a tool, reads a
resource or gets a prompt by a literal name / URI.

Every case runs the whole pipeline (``analyze_files``), which also fails on any contract error
(``parsers/contract.py``): an ``mcp_call`` has a method of TOOL / RESOURCE / PROMPT, a framework,
an endpoint and no handler.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from breezeai_cog.parsers.contract import validate
from breezeai_cog.schemas import FileRecord, Statement


@pytest.fixture
def analyze_records(tmp_path: Path) -> Callable[[dict[str, str]], dict[str, FileRecord]]:
    """As ``analyze_files``, returning each file's record (for its file-level framework)."""
    from breezeai_cog import iter_file_records

    def run(files: dict[str, str]) -> dict[str, FileRecord]:
        repo = tmp_path / "records"
        for rel, src in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src)
        records = {r.path: r for r in iter_file_records(repo)}
        assert not [v for r in records.values() for v in validate(r) if v.severity == "error"]
        return records

    return run


def _calls(statements: list[Statement]) -> list[tuple[str | None, str | None, str | None]]:
    calls = sorted(
        (s for s in statements if s.semanticType == "mcp_call"), key=lambda s: (s.path, s.startLine)
    )
    for c in calls:
        assert c.handler is None and c.params is None
    # The call line keeps one statement: the call's own, enriched, never a second record (Kotlin
    # records each nested call of a line on its own: there the call's statement is enriched).
    lines = Counter((s.path, s.startLine) for s in statements)
    for c in calls:
        assert "#" not in c.id and c.nodeType != "synthetic"
        if not c.path.endswith(".kt"):
            assert lines[(c.path, c.startLine)] == 1, f"{c.id}: {lines[(c.path, c.startLine)]}"
    return [(c.method, c.endpoint, c.framework) for c in calls]


PYTHON_CLIENT = """
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from pydantic import AnyUrl


async def run(params):
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("add", {"a": 1, "b": 2})
        await session.read_resource(AnyUrl("memo://insights"))
        await session.read_resource("file:///logs/app.log")
        prompt = await session.get_prompt(name="review", arguments={"code": "x"})
        await session.call_tool(tool_name, {})


async def typed(session: ClientSession) -> None:
    await session.call_tool(name="echo", arguments={})
"""


def test_python_client_session_calls(analyze_files) -> None:
    statements = analyze_files({"client.py": PYTHON_CLIENT})
    assert _calls(statements) == [
        ("TOOL", "add", "python-mcp"),
        ("RESOURCE", "memo://insights", "python-mcp"),
        ("RESOURCE", "file:///logs/app.log", "python-mcp"),
        ("PROMPT", "review", "python-mcp"),
        ("TOOL", "echo", "python-mcp"),
    ]


def test_python_fastmcp_client_calls(analyze_files) -> None:
    src = """
from fastmcp import Client


async def main():
    async with Client("http://localhost:8000/mcp") as client:
        await client.call_tool("add", {"a": 1})
        await client.get_prompt("greet")
"""
    assert _calls(analyze_files({"client.py": src})) == [
        ("TOOL", "add", "fastmcp"),
        ("PROMPT", "greet", "fastmcp"),
    ]


def test_python_client_only_file_has_no_framework(analyze_records) -> None:
    records = analyze_records({"client.py": PYTHON_CLIENT})
    assert records["client.py"].framework is None


def test_python_client_look_alikes(analyze_files) -> None:
    """aiohttp's ClientSession, agent frameworks' MCP server objects, LlamaIndex, LangChain and
    a rebound name: no mcp_call."""
    files = {
        "aio.py": """
from aiohttp import ClientSession


async def f():
    async with ClientSession() as session:
        await session.call_tool("x")
""",
        "agents.py": """
from agents.mcp import MCPServerStdio
from pydantic_ai.mcp import MCPServerStdio as PydanticServer
from llama_index.tools.mcp import BasicMCPClient
from langchain_mcp_adapters.client import MultiServerMCPClient


async def f():
    async with MCPServerStdio(params={}) as server:
        await server.call_tool("x", {})
    pyd = PydanticServer("npx")
    await pyd.call_tool("y", {})
    client = BasicMCPClient("http://x")
    await client.call_tool("z", {})
    multi = MultiServerMCPClient({})
    tools = await multi.get_tools()
""",
        "rebound.py": """
from mcp import ClientSession


async def f(read, write):
    session = ClientSession(read, write)
    session = other()
    await session.call_tool("x", {})
""",
    }
    assert not [s for s in analyze_files(files) if s.semanticType == "mcp_call"]


TS_CLIENT = """
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const client = new Client({ name: "app", version: "1.0.0" });
await client.connect(new StdioClientTransport({ command: "node", args: ["server.js"] }));
const result = await client.callTool({ name: "add", arguments: { a: 1, b: 2 } });
await client.readResource({ uri: "memo://insights" });
await client.getPrompt({ name: "review", arguments: { code: "x" } });
await client.callTool({ name: toolName, arguments: {} });
await client.callTool(request);

export async function typed(c: Client) {
  return c.callTool({ name: "echo" });
}
"""


def test_typescript_client_calls(analyze_files) -> None:
    assert _calls(analyze_files({"client.ts": TS_CLIENT})) == [
        ("TOOL", "add", "typescript-mcp"),
        ("RESOURCE", "memo://insights", "typescript-mcp"),
        ("PROMPT", "review", "typescript-mcp"),
        ("TOOL", "echo", "typescript-mcp"),
    ]


def test_typescript_client_only_file_has_no_framework(analyze_records) -> None:
    assert analyze_records({"client.ts": TS_CLIENT})["client.ts"].framework is None


def test_typescript_client_look_alikes(analyze_files) -> None:
    """The pg ``Client``, the AI SDK's MCP client, a ``Client`` not from the SDK: no mcp_call."""
    files = {
        "pg.ts": """
import { Client } from "pg";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
const client = new Client();
await client.callTool({ name: "x" });
""",
        "vercel.ts": """
import { experimental_createMCPClient } from "ai";
import { Client as McpClient } from "@modelcontextprotocol/sdk/client/index.js";
const client = await experimental_createMCPClient({ transport });
await client.callTool({ name: "x" });
""",
        "shadow.ts": """
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
const client = new Client({ name: "a", version: "1" });
function f(client: Other) { return client.callTool({ name: "y" }); }
""",
    }
    assert not [s for s in analyze_files(files) if s.semanticType == "mcp_call"]


MCPGO_CLIENT = """
package main

import (
	"github.com/mark3labs/mcp-go/client"
	"github.com/mark3labs/mcp-go/mcp"
)

func main() {
	c, err := client.NewStdioMCPClient("server", nil)
	req := mcp.CallToolRequest{}
	req.Params.Name = "add"
	req.Params.Arguments = map[string]any{"a": 1}
	res, err := c.CallTool(ctx, req)
	c.ReadResource(ctx, mcp.ReadResourceRequest{Params: mcp.ReadResourceParams{URI: "memo://insights"}})
	c.GetPrompt(ctx, mcp.GetPromptRequest{Params: mcp.GetPromptParams{Name: "review"}})
	var twice mcp.CallToolRequest
	twice.Params.Name = "one"
	twice.Params.Name = "two"
	c.CallTool(ctx, twice)
	shared := mcp.CallToolRequest{}
	shared.Params.Name = "shared"
	c.CallTool(ctx, shared)
	c.CallTool(ctx, shared)
}

func typed(c *client.Client) {
	c.CallTool(ctx, mcp.CallToolRequest{Params: mcp.CallToolParams{Name: "echo"}})
}
"""


def test_mcpgo_client_calls(analyze_files) -> None:
    assert _calls(analyze_files({"main.go": MCPGO_CLIENT})) == [
        ("TOOL", "add", "mcp-go"),
        ("RESOURCE", "memo://insights", "mcp-go"),
        ("PROMPT", "review", "mcp-go"),
        ("TOOL", "echo", "mcp-go"),
    ]


GO_SDK_CLIENT = """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

func main() {
	client := mcp.NewClient(&mcp.Implementation{Name: "app"}, nil)
	session, err := client.Connect(ctx, transport, nil)
	session.CallTool(ctx, &mcp.CallToolParams{Name: "greet", Arguments: map[string]any{}})
	session.ReadResource(ctx, &mcp.ReadResourceParams{URI: "embedded:info"})
	session.GetPrompt(ctx, &mcp.GetPromptParams{Name: "review"})
	session.CallTool(ctx, params)
}

func typed(cs *mcp.ClientSession) {
	cs.CallTool(ctx, &mcp.CallToolParams{Name: "echo"})
}
"""


def test_go_sdk_client_calls(analyze_files) -> None:
    assert _calls(analyze_files({"main.go": GO_SDK_CLIENT})) == [
        ("TOOL", "greet", "go-mcp"),
        ("RESOURCE", "embedded:info", "go-mcp"),
        ("PROMPT", "review", "go-mcp"),
        ("TOOL", "echo", "go-mcp"),
    ]


def test_go_client_only_file_has_no_framework(analyze_records) -> None:
    records = analyze_records({"a/main.go": GO_SDK_CLIENT, "b/main.go": MCPGO_CLIENT})
    assert records["a/main.go"].framework is None and records["b/main.go"].framework is None


def test_go_client_look_alikes(analyze_files) -> None:
    """A receiver that is not a proven client, a reassigned session, another package: none."""
    files = {
        "a.go": """
package main

import "github.com/modelcontextprotocol/go-sdk/mcp"

func f(sessions []*mcp.ClientSession, s *Other) {
	sessions[0].CallTool(ctx, &mcp.CallToolParams{Name: "x"})
	s.CallTool(ctx, &mcp.CallToolParams{Name: "y"})
	cs, _ := mcp.NewClient(nil, nil).Connect(ctx, t, nil)
	cs = other
	cs.CallTool(ctx, &mcp.CallToolParams{Name: "z"})
}
""",
        "b.go": """
package main

import (
	"github.com/mark3labs/mcp-go/client"
	"example.com/fake/mcp"
)

func f() {
	c, _ := client.NewStdioMCPClient("s", nil)
	c.CallTool(ctx, mcp.CallToolRequest{Params: mcp.CallToolParams{Name: "x"}})
}
""",
    }
    assert not [s for s in analyze_files(files) if s.semanticType == "mcp_call"]


JAVA_CLIENT = """
package demo;

import io.modelcontextprotocol.client.McpClient;
import io.modelcontextprotocol.client.McpSyncClient;
import io.modelcontextprotocol.client.McpAsyncClient;
import io.modelcontextprotocol.spec.McpSchema;
import java.util.List;
import java.util.Map;

public class Calls {
    private final McpAsyncClient async;

    void run(McpClientTransport transport) {
        var client = McpClient.sync(transport).build();
        client.callTool(new McpSchema.CallToolRequest("add", Map.of("a", 1)));
        client.readResource(new McpSchema.ReadResourceRequest("memo://insights"));
        client.getPrompt(new McpSchema.GetPromptRequest("review", Map.of()));
        client.callTool(McpSchema.CallToolRequest.builder().name("built").arguments(Map.of()).build());
        client.callTool(new McpSchema.CallToolRequest(toolName, Map.of()));
        this.async.callTool(new McpSchema.CallToolRequest(mapper, "json", "{}"));
    }

    void spring(List<McpSyncClient> clients) {
        clients.get(0).callTool(new McpSchema.CallToolRequest("x", Map.of()));
    }
}
"""


def test_java_client_calls(analyze_files) -> None:
    assert _calls(analyze_files({"Calls.java": JAVA_CLIENT})) == [
        ("TOOL", "add", "java-mcp"),
        ("RESOURCE", "memo://insights", "java-mcp"),
        ("PROMPT", "review", "java-mcp"),
        ("TOOL", "built", "java-mcp"),
        ("TOOL", "json", "java-mcp"),
    ]


def test_java_client_only_file_has_no_framework(analyze_records) -> None:
    assert analyze_records({"Calls.java": JAVA_CLIENT})["Calls.java"].framework is None


def test_java_client_look_alikes(analyze_files) -> None:
    src = """
package demo;

import io.modelcontextprotocol.spec.McpSchema;

public class Other {
    void f(SomeClient client, Object McpClient) {
        client.callTool(new McpSchema.CallToolRequest("x", null));
        var c = Factory.sync(t).build();
        c.callTool(new McpSchema.CallToolRequest("y", null));
    }
}
"""
    assert not [s for s in analyze_files({"Other.java": src}) if s.semanticType == "mcp_call"]


KOTLIN_CLIENT = """
package demo

import io.modelcontextprotocol.kotlin.sdk.client.Client
import io.modelcontextprotocol.kotlin.sdk.types.*
import io.modelcontextprotocol.client.McpClient
import io.modelcontextprotocol.spec.McpSchema

class App(private val transport: Any) {
    private val mcp: Client = Client(clientInfo = Implementation(name = "app", version = "1"))

    suspend fun run() {
        mcp.callTool(name = "add", arguments = mapOf("a" to 1))
        this.mcp.callTool("positional", mapOf())
        mcp.readResource(ReadResourceRequest(ReadResourceRequestParams(uri = "memo://insights")))
        mcp.getPrompt(GetPromptRequest(GetPromptRequestParams(name = "review")))
        mcp.callTool(name = toolName, arguments = mapOf())
        val sync = McpClient.sync(transport).build()
        sync.callTool(McpSchema.CallToolRequest("java", mapOf()))
    }
}
"""


def test_kotlin_client_calls(analyze_files) -> None:
    assert _calls(analyze_files({"App.kt": KOTLIN_CLIENT})) == [
        ("TOOL", "add", "kotlin-mcp"),
        ("TOOL", "positional", "kotlin-mcp"),
        ("RESOURCE", "memo://insights", "kotlin-mcp"),
        ("PROMPT", "review", "kotlin-mcp"),
        ("TOOL", "java", "java-mcp"),
    ]


def test_kotlin_client_only_file_has_no_framework(analyze_records) -> None:
    assert analyze_records({"App.kt": KOTLIN_CLIENT})["App.kt"].framework is None


def test_kotlin_client_look_alikes(analyze_files) -> None:
    """A ``Client`` not imported from the Kotlin SDK (Ktor's ``HttpClient`` is no client either)."""
    src = """
package demo

import io.modelcontextprotocol.spec.McpSchema
import com.acme.Client

val client = Client()
suspend fun f() { client.callTool(name = "x", arguments = mapOf()) }
"""
    assert not [s for s in analyze_files({"Other.kt": src}) if s.semanticType == "mcp_call"]


CSHARP_CLIENT = """
using ModelContextProtocol.Client;

public class App
{
    private readonly IMcpClient _typed;

    public async Task RunAsync(IClientTransport transport)
    {
        var client = await McpClient.CreateAsync(transport);
        var result = await client.CallToolAsync("echo", new Dictionary<string, object?>());
        await client.ReadResourceAsync("memo://insights");
        await client.ReadResourceAsync(new Uri("file:///logs/app.log"));
        await client.GetPromptAsync(name: "review");
        await client.CallToolAsync(toolName, null);
        await _typed.CallToolAsync("typed");
    }
}
"""


def test_csharp_client_calls(analyze_files) -> None:
    assert _calls(analyze_files({"App.cs": CSHARP_CLIENT})) == [
        ("TOOL", "echo", "csharp-mcp"),
        ("RESOURCE", "memo://insights", "csharp-mcp"),
        ("RESOURCE", "file:///logs/app.log", "csharp-mcp"),
        ("PROMPT", "review", "csharp-mcp"),
        ("TOOL", "typed", "csharp-mcp"),
    ]


def test_csharp_client_only_file_has_no_framework(analyze_records) -> None:
    assert analyze_records({"App.cs": CSHARP_CLIENT})["App.cs"].framework is None


def test_csharp_client_look_alikes(analyze_files) -> None:
    src = """
using ModelContextProtocol.Client;

public class Other
{
    public async Task F(SomeClient client)
    {
        await client.CallToolAsync("x");
        var made = await Factory.CreateAsync(t);
        await made.CallToolAsync("y");
    }
}
"""
    assert not [s for s in analyze_files({"Other.cs": src}) if s.semanticType == "mcp_call"]


def test_an_enriched_call_keeps_its_statements_name(analyze_files) -> None:
    src = """
from mcp import ClientSession


async def run(session: ClientSession):
    result = await session.call_tool("x", {})
    await session.call_tool("y", {})
"""
    calls = {s.endpoint: s for s in analyze_files({"c.py": src}) if s.semanticType == "mcp_call"}
    assert calls["x"].name == "result"  # the base parser's name, not the endpoint
    assert calls["y"].name != "y"


def test_typescript_this_client_is_proven_per_class(analyze_files) -> None:
    src = """
import { Client } from "@modelcontextprotocol/sdk/client/index.js";

class A {
  private client = new Client({ name: "a", version: "1" });
  run() { return this.client.callTool({ name: "from_a" }); }
}

class B {
  constructor(private client: Other) {}
  run() { return this.client.callTool({ name: "from_b" }); }
}

class C {
  constructor(private readonly client: Client) {}
  run() { return this.client.callTool({ name: "from_c" }); }
}

class D {
  private client: Client;
  constructor() { this.client = new Client({ name: "d", version: "1" }); }
  run() { return this.client.callTool({ name: "from_d" }); }
}

class E {
  private client = new Client({ name: "e", version: "1" });
  constructor(private client: Client) {}
  run() { return this.client.callTool({ name: "from_e" }); }
}

class F {
  private client = new Client({ name: "f", version: "1" });
  run() { return function () { return this.client.callTool({ name: "from_f" }); }; }
}
"""
    assert [c[1] for c in _calls(analyze_files({"c.ts": src}))] == ["from_a", "from_c", "from_d"]


def test_typescript_loop_and_catch_variables_shadow_a_client(analyze_files) -> None:
    src = """
import { Client } from "@modelcontextprotocol/sdk/client/index.js";

export const client = new Client({ name: "a", version: "1" });
await client.callTool({ name: "kept" });

for (const client of others) { await client.callTool({ name: "loop" }); }
others.forEach((client) => client.callTool({ name: "lambda" }));
try { } catch (client) { client.callTool({ name: "caught" }); }
"""
    assert [c[1] for c in _calls(analyze_files({"c.ts": src}))] == ["kept"]


def test_java_lambda_variable_is_another_declaration(analyze_files) -> None:
    src = """
package demo;

import io.modelcontextprotocol.client.McpSyncClient;
import io.modelcontextprotocol.spec.McpSchema;

public class L {
    McpSyncClient client;
    void f(List<Other> others) {
        others.forEach(client -> client.callTool(new McpSchema.CallToolRequest("lambda", null)));
        client.callTool(new McpSchema.CallToolRequest("field", null));
    }
}
"""
    # The lambda's `client` makes the name ambiguous in the file: neither call is recorded.
    assert _calls(analyze_files({"L.java": src})) == []


def test_kotlin_loop_and_lambda_variables_are_other_declarations(analyze_files) -> None:
    src = """
package demo

import io.modelcontextprotocol.kotlin.sdk.client.Client

val mcp = Client(clientInfo = info)
suspend fun f() {
    for (mcp in others) { mcp.callTool(name = "loop", arguments = mapOf()) }
    mcp.callTool(name = "top", arguments = mapOf())
}
val client = Client(clientInfo = info)
suspend fun g() { others.forEach { client -> client.callTool(name = "lambda", arguments = mapOf()) } }
"""
    assert _calls(analyze_files({"K.kt": src})) == []


def test_csharp_foreach_and_lambda_variables_are_other_declarations(analyze_files) -> None:
    src = """
using ModelContextProtocol.Client;

public class C
{
    private IMcpClient client;
    private IMcpClient other;
    public async Task F()
    {
        foreach (var client in clients) { await client.CallToolAsync("loop"); }
        others.Select(other => other.CallToolAsync("lambda"));
        await client.CallToolAsync("field");
    }
}
"""
    assert _calls(analyze_files({"C.cs": src})) == []


# ── A call inside a multi-line statement gets a record on its own line ───────


def _by_endpoint(statements: list[Statement]) -> dict[str | None, Statement]:
    return {s.endpoint: s for s in statements if s.semanticType == "mcp_call"}


def test_python_calls_in_a_multiline_statement_are_each_on_their_own_line(analyze_files) -> None:
    src = """
import asyncio
from mcp import ClientSession


async def run(session: ClientSession, flag):
    results = await asyncio.gather(
        session.call_tool("a", {}),
        session.call_tool("b", {}),
    )
    if (flag and
            await session.call_tool("x", {})):
        pass
    single = await session.call_tool(
        "one", {})
"""
    statements = analyze_files({"c.py": src})
    calls = _by_endpoint(statements)
    assert {e: (c.startLine, c.endLine) for e, c in calls.items()} == {
        "a": (8, 8),
        "b": (9, 9),
        "x": (12, 12),
        "one": (14, 15),
    }
    # the gather and the `if` keep their own (unclassified) statements
    assert not [s for s in statements if s.startLine in (7, 11) and s.semanticType]
    assert calls["one"].name == "single"  # a call spanning lines enriches its own statement


def test_typescript_calls_in_promise_all_are_each_on_their_own_line(analyze_files) -> None:
    src = """
import { Client } from "@modelcontextprotocol/sdk/client/index.js";

const client = new Client({ name: "app", version: "1" });
const [a, b] = await Promise.all([
  client.callTool({ name: "a" }),
  client.callTool({ name: "b" }),
]);
"""
    statements = analyze_files({"c.ts": src})
    calls = _by_endpoint(statements)
    assert {e: c.startLine for e, c in calls.items()} == {"a": 6, "b": 7}
    assert not [s for s in statements if s.startLine == 5 and s.semanticType]


def test_typescript_object_literal_method_this_is_not_the_class(analyze_files) -> None:
    src = """
import { Client } from "@modelcontextprotocol/sdk/client/index.js";

class A {
  private client: Client = new Client({ name: "a", version: "1" });
  handlers = { run() { return this.client.callTool({ name: "x" }); } };
  direct() { return this.client.callTool({ name: "kept" }); }
}
"""
    assert [c[1] for c in _calls(analyze_files({"c.ts": src}))] == ["kept"]


def test_python_local_rebindings_shadow_a_module_client(analyze_files) -> None:
    """A function's own binding of the name — walrus, ``def``, ``class``, ``import … as`` —
    hides the module's client; ``global`` sends a rebinding to the module."""
    src = """
from mcp import ClientSession
import other

session = ClientSession(read, write)


async def walrus():
    if (session := other.make()):
        await session.call_tool("walrus", {})


async def defined():
    def session(): ...
    await session.call_tool("def", {})


async def klass():
    class session: ...
    await session.call_tool("class", {})


async def imported():
    import other as session
    await session.call_tool("import", {})


async def from_imported():
    from other import thing as session
    await session.call_tool("from_import", {})
"""
    assert _calls(analyze_files({"c.py": src})) == []


def test_python_walrus_binds_a_client(analyze_files) -> None:
    src = """
from mcp import ClientSession


async def run(read, write):
    if (session := ClientSession(read, write)):
        await session.call_tool("walrus", {})
"""
    assert _calls(analyze_files({"c.py": src})) == [("TOOL", "walrus", "python-mcp")]


def test_python_global_rebinding_reaches_the_module_client(analyze_files) -> None:
    src = """
from mcp import ClientSession

session = ClientSession(read, write)


def reset():
    global session
    session = other()


async def run():
    await session.call_tool("x", {})
"""
    # `global session` rebinds the module's name: it is no longer only a client
    assert _calls(analyze_files({"c.py": src})) == []


def test_python_nonlocal_rebinding_reaches_the_enclosing_client(analyze_files) -> None:
    src = """
from mcp import ClientSession


async def outer(read, write):
    session = ClientSession(read, write)

    def reset():
        nonlocal session
        session = other()

    await session.call_tool("x", {})


async def kept(read, write):
    session = ClientSession(read, write)

    def inner():
        global session
        session = other()

    await session.call_tool("kept", {})
"""
    assert _calls(analyze_files({"c.py": src})) == [("TOOL", "kept", "python-mcp")]


def test_java_call_tool_request_with_meta_takes_the_first_literal(analyze_files) -> None:
    src = """
package demo;

import io.modelcontextprotocol.client.McpSyncClient;
import io.modelcontextprotocol.spec.McpSchema;
import java.util.Map;

public class M {
    McpSyncClient client;
    void f() {
        client.callTool(new McpSchema.CallToolRequest("with_meta", Map.of("a", 1), Map.of("k", "v")));
        client.callTool(new McpSchema.CallToolRequest(mapper, "json", "{}"));
        client.callTool(new McpSchema.CallToolRequest(name, Map.of(), Map.of()));
    }
}
"""
    assert _calls(analyze_files({"M.java": src})) == [
        ("TOOL", "with_meta", "java-mcp"),
        ("TOOL", "json", "java-mcp"),
    ]


def test_kotlin_call_tool_request_with_meta_takes_the_first_literal(analyze_files) -> None:
    src = """
package demo

import io.modelcontextprotocol.client.McpSyncClient
import io.modelcontextprotocol.spec.McpSchema

class M(private val client: McpSyncClient) {
    fun f() {
        client.callTool(McpSchema.CallToolRequest("with_meta", mapOf("a" to 1), mapOf("k" to "v")))
        client.callTool(McpSchema.CallToolRequest(mapper, "json", "{}"))
        client.callTool(McpSchema.CallToolRequest(name, mapOf(), mapOf()))
    }
}
"""
    assert _calls(analyze_files({"M.kt": src})) == [
        ("TOOL", "with_meta", "java-mcp"),
        ("TOOL", "json", "java-mcp"),
    ]


def test_kotlin_star_import_proves_the_sdk_client(analyze_files) -> None:
    src = """
package demo

import io.modelcontextprotocol.kotlin.sdk.client.*

val client = Client(clientInfo = info)
suspend fun f() { client.callTool(name = "star", arguments = mapOf()) }
"""
    assert _calls(analyze_files({"S.kt": src})) == [("TOOL", "star", "kotlin-mcp")]


@pytest.mark.parametrize(
    "shadow",
    [
        "import com.acme.Client",
        "import com.acme.OtherClient as Client",
        "class Client",
        "data class Client(val x: Int = 0)",
        "object Client",
        "interface Client",
        "typealias Client = com.acme.Thing",
    ],
)
def test_kotlin_star_import_is_shadowed_by_another_client(analyze_files, shadow: str) -> None:
    """An explicit import or a declaration of ``Client`` wins over the SDK's star import."""
    src = f"""
package demo

import io.modelcontextprotocol.kotlin.sdk.client.*
{shadow if shadow.startswith("import") else ""}

{"" if shadow.startswith("import") else shadow}

val client = Client()
suspend fun f() {{ client.callTool(name = "x", arguments = mapOf()) }}
"""
    assert _calls(analyze_files({"S.kt": src})) == []


# ── mcp-go: a request's `Params.Name` write runs before the call on every path ──

_MCPGO_IMPORTS = """
package main

import (
	"github.com/mark3labs/mcp-go/client"
	"github.com/mark3labs/mcp-go/mcp"
)
"""


@pytest.mark.parametrize(
    "body",
    [
        # only on one branch: the call may send no name
        'req := mcp.CallToolRequest{}\n\tif admin { req.Params.Name = "wipe" }\n\tc.CallTool(ctx, req)',
        # after the call: the call never sends it
        'req := mcp.CallToolRequest{}\n\tc.CallTool(ctx, req)\n\treq.Params.Name = "after"',
        # in a loop, a switch case, a func literal
        'req := mcp.CallToolRequest{}\n\tfor _, n := range ns { req.Params.Name = "loop" }\n\tc.CallTool(ctx, req)',
        'req := mcp.CallToolRequest{}\n\tswitch k { case 1: req.Params.Name = "case" }\n\tc.CallTool(ctx, req)',
        'req := mcp.CallToolRequest{}\n\tset := func() { req.Params.Name = "closure" }\n\tset()\n\tc.CallTool(ctx, req)',
        # in a `switch` header: only an `if`'s initializer is followed
        'req := mcp.CallToolRequest{}\n\tswitch req.Params.Name = "hdr"; k { default: }\n\tc.CallTool(ctx, req)',
        # the call in another block than the declaration
        'req := mcp.CallToolRequest{}\n\tif admin { req.Params.Name = "inner"; c.CallTool(ctx, req) }',
        # the resource / prompt variants go the same way
        'req := mcp.ReadResourceRequest{}\n\tif admin { req.Params.URI = "memo://x" }\n\tc.ReadResource(ctx, req)',
        'req := mcp.GetPromptRequest{}\n\tc.GetPrompt(ctx, req)\n\treq.Params.Name = "late"',
        # a `goto` jumps over the write: the call may send no name
        'req := mcp.CallToolRequest{}\n\tif admin { goto send }\n\treq.Params.Name = "add"\nsend:\n\tc.CallTool(ctx, req)',
        'req := mcp.CallToolRequest{}\n\tif admin { goto send }\n\treq.Params.Name = "add"\n\tsend: c.CallTool(ctx, req)',
    ],
)
def test_mcpgo_request_name_write_must_precede_the_call_unconditionally(
    analyze_files, body: str
) -> None:
    src = (
        _MCPGO_IMPORTS
        + '\nfunc f(admin bool, k int, ns []string) {\n\tc, _ := client.NewStdioMCPClient("s", nil)\n\t'
        + body
        + "\n}\n"
    )
    assert _calls(analyze_files({"main.go": src})) == []


def test_mcpgo_request_name_write_in_the_same_list_is_kept(analyze_files) -> None:
    src = (
        _MCPGO_IMPORTS
        + """
func f(admin bool) {
	c, _ := client.NewStdioMCPClient("s", nil)
	if admin {
		req := mcp.ReadResourceRequest{}
		req.Params.URI = "memo://x"
		if _, err := c.ReadResource(ctx, req); err != nil {
			return
		}
	}
	var p mcp.GetPromptRequest
	p.Params.Name = "review"
	return c.GetPrompt(ctx, p)
}
"""
    )
    # (an `if`'s initializer keeps its own statement on the `if`'s line: not `_calls`)
    found = _by_endpoint(analyze_files({"main.go": src}))
    assert {e: (s.method, s.framework, s.startLine) for e, s in found.items()} == {
        "memo://x": ("RESOURCE", "mcp-go", 14),
        "review": ("PROMPT", "mcp-go", 20),
    }


# ── Python: an attribute / subscript target binds no name ────────────────────


def test_python_storing_the_session_keeps_it_a_client(analyze_files) -> None:
    src = """
from mcp import ClientSession


class Conn:
    async def connect(self, r, w):
        session = ClientSession(r, w)
        self.session = session
        cache[session] = 1
        self.items[session], other = session, 2
        await session.call_tool("stored_add", {})

    async def rebound(self, r, w):
        session = ClientSession(r, w)
        self.session = session
        session = other()
        await session.call_tool("rebound", {})

    async def unpacked(self, r, w):
        session = ClientSession(r, w)
        (session, self.x), [*session] = pair, rest
        await session.call_tool("unpacked", {})

    async def with_tuple(self, r, w):
        session = ClientSession(r, w)
        with open_pair() as (session, x):
            await session.call_tool("with_tuple", {})
"""
    assert _calls(analyze_files({"c.py": src})) == [("TOOL", "stored_add", "python-mcp")]


# ── TypeScript: a `var` reaches the whole function ───────────────────────────


def test_typescript_nested_var_hides_a_module_client(analyze_files) -> None:
    src = """
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
const client = new Client({ name: "x", version: "1" });
function f(x: boolean) {
  if (x) { var client = otherThing(); }
  client.callTool({ name: "nested_var" });
}
function g(xs: any[]) {
  for (var client of xs) { }
  client.callTool({ name: "loop_var" });
}
function h(x: boolean) {
  var client = new Client({ name: "y", version: "1" });
  if (x) { var client = otherThing(); }
  client.callTool({ name: "redeclared" });
}
function k() {
  const inner = () => { var client = otherThing(); };
  client.callTool({ name: "kept" });
}
"""
    assert _calls(analyze_files({"c.ts": src})) == [("TOOL", "kept", "typescript-mcp")]


# ── TypeScript: any write to the name, not only a plain `x = …`, rebinds it ──


@pytest.mark.parametrize(
    "rebind",
    [
        "client = other;",
        "({ client } = deps);",
        "({ c: client } = deps);",
        "[client] = arr;",
        "[, ...client] = arr;",
        "client &&= other;",
        "client ||= other;",
        "client ??= other;",
        "for (client of others) {}",
        "for (client in others) {}",
    ],
)
@pytest.mark.parametrize("kind", ["const", "let"])
def test_typescript_destructuring_logical_and_loop_writes_rebind_a_client(
    analyze_files, rebind: str, kind: str
) -> None:
    src = f"""
import {{ Client }} from "@modelcontextprotocol/sdk/client/index.js";
{kind} client = new Client({{ name: "x", version: "1" }});
{rebind}
await client.callTool({{ name: "wipe" }});
"""
    assert _calls(analyze_files({"c.ts": src})) == []


def test_typescript_destructuring_another_name_keeps_a_client(analyze_files) -> None:
    src = """
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
const client = new Client({ name: "x", version: "1" });
({ client: other } = deps);
[this.client] = arr;
count ??= 0;
for (item of items) {}
await client.callTool({ name: "kept" });
"""
    assert _calls(analyze_files({"c.ts": src})) == [("TOOL", "kept", "typescript-mcp")]


# ── Java / C#: every declaration kind of a name counts, never a client unless typed one ──


@pytest.mark.parametrize(
    "other",
    [
        "try (Other client = open()) { }",
        "try (var client = open()) { }",
        "if (o instanceof Other client) { }",
        "if (o instanceof Pt(Other client, int y)) { }",
        "switch (o) { case Other client -> { } default -> { } }",
        "for (Other client : others) { }",
        "try { } catch (Exception client) { }",
        "others.forEach((Other client) -> { });",
        "others.forEach(client -> { });",
    ],
)
def test_java_other_declaration_kinds_are_not_clients(analyze_files, other: str) -> None:
    src = f"""
package demo;

import io.modelcontextprotocol.client.McpSyncClient;
import io.modelcontextprotocol.spec.McpSchema;

public class D {{
    McpSyncClient client;
    void a(Object o) {{ {other} }}
    void b() {{ client.callTool(new McpSchema.CallToolRequest("wipe", null)); }}
}}
"""
    assert _calls(analyze_files({"D.java": src})) == []


def test_java_typed_resource_and_pattern_keep_a_client(analyze_files) -> None:
    src = """
package demo;

import io.modelcontextprotocol.client.McpClient;
import io.modelcontextprotocol.client.McpSyncClient;
import io.modelcontextprotocol.spec.McpSchema;

public class D {
    McpSyncClient client;
    void a(Object o) {
        try (McpSyncClient client = make()) { }
        try (var client = McpClient.sync(transport).build()) { }
        if (o instanceof McpSyncClient client) { }
    }
    void b() { client.callTool(new McpSchema.CallToolRequest("kept", null)); }
}
"""
    assert _calls(analyze_files({"D.java": src})) == [("TOOL", "kept", "java-mcp")]


@pytest.mark.parametrize(
    "other",
    [
        "if (reg.TryGet(k, out var client)) { }",
        "reg.TryGet(k, out Other client);",
        "if (o is Other client) { }",
        "if (o is Other { X: 1 } client) { }",
        "switch (o) { case Other client: break; }",
        "var r = o switch { Other client => 1, _ => 2 };",
        "var (client, x) = pair;",
        "(var client, int y) = pair;",
        "foreach (var (client, z) in pairs) { }",
        "try { } catch (Exception client) { }",
        "if (o is var (client, y)) { }",
        "if (o is Pt(Other client, var y)) { }",
        "var q = from client in xs select client;",
        "var q = from a in xs join client in ys on a equals client select a;",
        "var q = from a in xs join Other client in ys on a equals client select a;",
        "var q = from a in xs let client = a select a;",
        "var q = from a in xs group a by a into client select client;",
        "var q = from a in xs join b in ys on a equals b into client select a;",
        "Func<int, int> f = (int client) => 1;",
    ],
)
def test_csharp_other_declaration_kinds_are_not_clients(analyze_files, other: str) -> None:
    src = f"""
using ModelContextProtocol.Client;

public class C
{{
    private IMcpClient client;
    public void A(object o) {{ {other} }}
    public async Task B() {{ await client.CallToolAsync("wipe"); }}
}}
"""
    assert _calls(analyze_files({"C.cs": src})) == []


def test_csharp_typed_out_and_pattern_keep_a_client(analyze_files) -> None:
    src = """
using ModelContextProtocol.Client;

public class C
{
    private IMcpClient client;
    public void A(object o)
    {
        reg.TryGet(k, out IMcpClient client);
        if (o is McpClient client) { }
    }
    public async Task B() { await client.CallToolAsync("kept"); }
}
"""
    assert _calls(analyze_files({"C.cs": src})) == [("TOOL", "kept", "csharp-mcp")]


@pytest.mark.parametrize(
    "other",
    [
        "val (client, x) = pair",
        "try { } catch (client: Exception) { }",
        "when (val client = x) { else -> { } }",
        "for ((client, y) in pairs) { }",
        "xs.forEach { (client, z) -> }",
        "xs.forEach(fun(client: Other) { })",
    ],
)
def test_kotlin_other_declaration_kinds_are_not_clients(analyze_files, other: str) -> None:
    src = f"""
package demo

import io.modelcontextprotocol.kotlin.sdk.client.Client

val client = Client(clientInfo = info)
fun a() {{ {other} }}
suspend fun b() {{ client.callTool(name = "wipe", arguments = mapOf()) }}
"""
    assert _calls(analyze_files({"K.kt": src})) == []


@pytest.mark.parametrize(
    "other",
    [
        "case session:\n            pass",
        "case [session, *rest]:\n            pass",
        "case [*session]:\n            pass",
        "case Foo(a=session):\n            pass",
        "case {'k': session}:\n            pass",
        "case {**session}:\n            pass",
        "case Bar() as session:\n            pass",
    ],
)
def test_python_match_capture_rebinds_a_client(analyze_files, other: str) -> None:
    src = f"""
from mcp import ClientSession


async def run(r, w, x):
    session = ClientSession(r, w)
    match x:
        {other}
    await session.call_tool("wipe", {{}})
"""
    assert _calls(analyze_files({"c.py": src})) == []


def test_python_except_alias_and_match_value_pattern(analyze_files) -> None:
    src = """
from mcp import ClientSession


async def caught(r, w):
    session = ClientSession(r, w)
    try:
        pass
    except Exception as session:
        pass
    await session.call_tool("caught", {})


async def value_pattern(r, w, x):
    session = ClientSession(r, w)
    match x:
        case mod.session:
            pass
        case Foo(session=1):
            pass
    await session.call_tool("kept", {})
"""
    assert _calls(analyze_files({"c.py": src})) == [("TOOL", "kept", "python-mcp")]


# ── Java / C# / Kotlin: a name declared more than once, every time a client ──


def test_java_client_parameter_in_two_methods(analyze_files) -> None:
    src = """
package demo;

import io.modelcontextprotocol.client.McpSyncClient;
import io.modelcontextprotocol.spec.McpSchema;

public class Two {
    void a(McpSyncClient client) { client.callTool(new McpSchema.CallToolRequest("a", null)); }
    void b(McpSyncClient client) { client.callTool(new McpSchema.CallToolRequest("b", null)); }
    void c(Other other) { other.callTool(new McpSchema.CallToolRequest("c", null)); }
    void d(McpSyncClient other) { other.callTool(new McpSchema.CallToolRequest("d", null)); }
}
"""
    assert _calls(analyze_files({"Two.java": src})) == [
        ("TOOL", "a", "java-mcp"),
        ("TOOL", "b", "java-mcp"),
    ]


def test_csharp_client_parameter_in_two_methods(analyze_files) -> None:
    src = """
using ModelContextProtocol.Client;

public class Two
{
    public async Task A(IMcpClient client) { await client.CallToolAsync("a"); }
    public async Task B(McpClient client) { await client.CallToolAsync("b"); }
    public async Task C(Other other) { await other.CallToolAsync("c"); }
    public async Task D(IMcpClient other) { await other.CallToolAsync("d"); }
}
"""
    assert _calls(analyze_files({"Two.cs": src})) == [
        ("TOOL", "a", "csharp-mcp"),
        ("TOOL", "b", "csharp-mcp"),
    ]


def test_kotlin_client_parameter_in_two_functions(analyze_files) -> None:
    src = """
package demo

import io.modelcontextprotocol.kotlin.sdk.client.Client
import io.modelcontextprotocol.client.McpSyncClient
import io.modelcontextprotocol.spec.McpSchema

suspend fun a(client: Client) { client.callTool(name = "a", arguments = mapOf()) }
suspend fun b(client: Client) { client.callTool(name = "b", arguments = mapOf()) }
suspend fun c(other: Other) { other.callTool(name = "c", arguments = mapOf()) }
suspend fun d(other: Client) { other.callTool(name = "d", arguments = mapOf()) }
suspend fun e(mixed: Client) { mixed.callTool(name = "e", arguments = mapOf()) }
fun g(mixed: McpSyncClient) { mixed.callTool(McpSchema.CallToolRequest("g", mapOf())) }
"""
    # `mixed` is a client of two frameworks: which one a call reaches is not known
    assert _calls(analyze_files({"K.kt": src})) == [
        ("TOOL", "a", "kotlin-mcp"),
        ("TOOL", "b", "kotlin-mcp"),
    ]
