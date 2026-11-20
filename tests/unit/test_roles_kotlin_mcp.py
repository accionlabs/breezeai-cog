"""MCP server tools of the official Kotlin SDK and the MCP Java SDK in Kotlin:
one ``mcp_tool`` statement per tool.

Every case runs the whole pipeline (``analyze_files``), which also fails on any entry-point
contract error (``parsers/contract.py``).
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


KOTLIN_SDK = """
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
"""


def test_kotlin_sdk_add_tool(analyze_files) -> None:
    tools = _tools(analyze_files({"src/main/kotlin/demo/Server.kt": KOTLIN_SDK}))
    assert set(tools) == {"calc", "ping", "dyn"}  # a computed name is not guessed
    calc = tools["calc"]
    assert (calc.framework, calc.description, calc.handler, calc.handlerLine) == (
        "kotlin-mcp",
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
        "java-mcp",
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

    from breezeai_cog.parsers import jvm_mcp_common

    def too_deep(_text: str) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(jvm_mcp_common, "json", SimpleNamespace(loads=too_deep))
    tools = _tools(analyze_files({"src/main/kotlin/demo/Tools.kt": KOTLIN_JAVA_SDK}))
    assert set(tools) == {"calc", "echo"}  # the file still parses
    assert tools["calc"].params is None and tools["echo"].params is None


KOTLIN_ADD_TOOLS = """
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
"""


def test_kotlin_sdk_add_tools_with_an_inline_list(analyze_files) -> None:
    tools = _tools(analyze_files({"src/main/kotlin/demo/Server.kt": KOTLIN_ADD_TOOLS}))
    assert set(tools) == {"search", "stats"}  # a list from a variable or a call is not read
    search = tools["search"]
    assert (search.framework, search.description, search.handler, search.handlerLine) == (
        "kotlin-mcp",
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
