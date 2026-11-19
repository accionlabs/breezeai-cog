"""MCP server tools of the official Kotlin SDK and the MCP Java SDK in Kotlin:
one ``mcp_tool`` statement per tool.

Every case runs the whole pipeline (``analyze_files``), which also fails on any entry-point
contract error (``parsers/contract.py``).
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


KOTLIN_ADD_TOOLS_VARIABLES = """
package demo

import io.modelcontextprotocol.kotlin.sdk.RegisteredTool
import io.modelcontextprotocol.kotlin.sdk.Tool
import io.modelcontextprotocol.kotlin.sdk.server.Server

val topTools = listOf(RegisteredTool(Tool(name = "top", description = "Top")) { _ -> null })
val shadowed = listOf(RegisteredTool(Tool(name = "outer")) { _ -> null })

class Registry(private val server: Server) {
    private val classTools = arrayOf(RegisteredTool(Tool(name = "member")) { _ -> null })
    private val untouched = mutableListOf(RegisteredTool(Tool(name = "untouched")) { _ -> null })
    private val grown = mutableListOf(RegisteredTool(Tool(name = "grown")) { _ -> null })
    private var reassignable = listOf(RegisteredTool(Tool(name = "var")) { _ -> null })

    fun register() {
        server.addTools(topTools)
        server.addTools(topTools)
        server.addTools(classTools)
        server.addTools(untouched)
        grown.add(extra())
        server.addTools(grown)
        server.addTools(reassignable)
        val shadowed = listOf(RegisteredTool(Tool(name = "inner")) { _ -> null })
        server.addTools(shadowed)
        server.addTools(built())
        server.addTools(returned())
        server.addTools(withArgs(1))
        server.addTools(buildList { add(RegisteredTool(Tool(name = "builder")) { _ -> null }) })
    }

    fun fromParam(shadowed: List<RegisteredTool>) {
        server.addTools(shadowed)
    }
}

fun built() = listOf(RegisteredTool(Tool(name = "built")) { _ -> null })

fun returned(): List<RegisteredTool> {
    return listOf(RegisteredTool(Tool(name = "returned")) { _ -> null })
}

fun withArgs(n: Int) = listOf(RegisteredTool(Tool(name = "withArgs")) { _ -> null })
"""


def test_kotlin_sdk_add_tools_with_a_list_variable_or_function(analyze_files) -> None:
    statements = analyze_files({"src/main/kotlin/demo/Registry.kt": KOTLIN_ADD_TOOLS_VARIABLES})
    tools = [s for s in statements if s.semanticType == "mcp_tool"]
    # one record per element even when the list is passed twice; the innermost `val` wins;
    # a `var`, a `mutableListOf` something adds to, a call with arguments, `buildList` and a
    # parameter are not read
    assert sorted(t.endpoint for t in tools) == [
        "built",
        "inner",
        "member",
        "returned",
        "top",
        "untouched",
    ]
    top = next(t for t in tools if t.endpoint == "top")
    assert (top.framework, top.description, top.startLine) == ("kotlin-mcp", "Top", 8)


def test_kotlin_add_tools_variable_without_the_mcp_sdk_is_not_a_tool(analyze_files) -> None:
    src = KOTLIN_ADD_TOOLS_VARIABLES.replace("io.modelcontextprotocol.kotlin.sdk", "com.acme.agent")
    assert _tools(analyze_files({"src/main/kotlin/demo/Registry.kt": src})) == {}


def test_kotlin_add_tools_name_bound_nearer_than_the_val_is_not_read(analyze_files) -> None:
    """The ``val`` of the same name further out is hidden by a primary-constructor parameter, a
    ``for`` loop variable, a ``catch`` parameter, a ``when (val …)`` subject and a destructured
    local ``val (tools, cfg) = …``."""
    src = """
package demo

import io.modelcontextprotocol.kotlin.sdk.RegisteredTool
import io.modelcontextprotocol.kotlin.sdk.Tool
import io.modelcontextprotocol.kotlin.sdk.server.Server

val tools = listOf(RegisteredTool(Tool(name = "a")) { _ -> null })
val loopy = listOf(RegisteredTool(Tool(name = "b")) { _ -> null })
val failed = listOf(RegisteredTool(Tool(name = "c")) { _ -> null })
val picked = listOf(RegisteredTool(Tool(name = "d")) { _ -> null })

class Reg(val tools: List<RegisteredTool>) { fun r(s: Server) = s.addTools(tools) }

fun r2(s: Server, gs: List<List<RegisteredTool>>) {
    for (loopy in gs) s.addTools(loopy)
    for ((picked, _) in gs.map { it to 1 }) s.addTools(picked)
    try { s.ping() } catch (failed: Exception) { s.addTools(failed) }
    when (val picked = gs.first()) { else -> s.addTools(picked) }
    for (g in gs) {
        val loopy = listOf(RegisteredTool(Tool(name = "in_loop")) { _ -> null })
        s.addTools(loopy)
    }
}

fun f(s: Server) { val (tools, cfg) = load(); s.addTools(tools) }
"""
    assert set(_tools(analyze_files({"src/main/kotlin/demo/Reg.kt": src}))) == {"in_loop"}


def test_kotlin_add_tools_mutable_list_is_judged_by_its_own_block(analyze_files) -> None:
    """A ``mutableListOf`` local is read when nothing in its block changes it, whatever another
    function does with a local of the same name."""
    src = """
package demo

import io.modelcontextprotocol.kotlin.sdk.RegisteredTool
import io.modelcontextprotocol.kotlin.sdk.Tool
import io.modelcontextprotocol.kotlin.sdk.server.Server

fun a(s: Server) {
    val tools = mutableListOf(RegisteredTool(Tool(name = "kept")) { _ -> null })
    s.addTools(tools)
}

fun b(s: Server) {
    val tools = mutableListOf(RegisteredTool(Tool(name = "grown")) { _ -> null })
    tools.add(extra())
    s.addTools(tools)
}
"""
    assert set(_tools(analyze_files({"src/main/kotlin/demo/Two.kt": src}))) == {"kept"}


KOTLIN_SHORTCUT = '''
package demo

import io.modelcontextprotocol.server.McpServer
import io.modelcontextprotocol.spec.McpSchema

fun build(transport: Any) {
    val calc = McpSchema.Tool("calc", "Calculator", """{"type": "object", "properties": {"a": {"type": "number"}}}""")
    McpServer.sync(transport)
        .serverInfo("demo", "1.0")
        .tool(calc) { exchange, args -> null }
        .toolCall(McpSchema.Tool.builder().name("echo").description("Echo").build(), ::echo)
        .build()
    ChatClient.create(model).prompt().tool(calc) { _, _ -> null }
}

fun echo(exchange: Any, request: Any): Any? = null
'''


def test_kotlin_mcp_server_shortcut(analyze_files) -> None:
    tools = _tools(analyze_files({"src/main/kotlin/demo/Build.kt": KOTLIN_SHORTCUT}))
    assert set(tools) == {"calc", "echo"}  # `.tool(…)` on a chain not rooted at McpServer: none
    calc = tools["calc"]
    assert (calc.framework, calc.description, calc.startLine, calc.handlerLine) == (
        "java-mcp",
        "Calculator",
        11,
        11,
    )
    assert _params(calc) == [("a", "number", False, None, None)]
    assert (tools["echo"].handler, tools["echo"].handlerLine, tools["echo"].startLine) == (
        "echo",
        17,
        12,
    )


@pytest.mark.parametrize(
    ("imports", "expected"),
    [
        ("import com.acme.McpServer\nimport io.modelcontextprotocol.spec.McpSchema", set()),
        (
            "import io.modelcontextprotocol.server.*\nimport com.acme.McpServer\n"
            "import io.modelcontextprotocol.spec.McpSchema",
            set(),
        ),
        (
            "import io.modelcontextprotocol.server.McpServer\n"
            "import com.acme.Other as McpServer\nimport io.modelcontextprotocol.spec.McpSchema",
            set(),
        ),
        ("import com.acme.*\nimport io.modelcontextprotocol.spec.McpSchema", set()),
        (
            "import io.modelcontextprotocol.server.*\nimport io.modelcontextprotocol.spec.McpSchema",
            {"calc"},
        ),
        (
            "import io.modelcontextprotocol.server.McpServer as Srv\n"
            "import io.modelcontextprotocol.spec.McpSchema",
            {"aliased"},
        ),
    ],
)
def test_kotlin_shortcut_needs_the_sdks_mcp_server(analyze_files, imports, expected) -> None:
    src = f"""
package demo

{imports}

fun build(transport: Any) {{
    McpServer.sync(transport).tool(McpSchema.Tool("calc", "C", "{{}}")) {{ _, _ -> null }}.build()
    Srv.sync(transport).tool(McpSchema.Tool("aliased", "A", "{{}}")) {{ _, _ -> null }}.build()
    io.modelcontextprotocol.server.McpServer.async(transport)
        .tool(McpSchema.Tool("qualified", "Q", "{{}}")) {{ _, _ -> null }}.build()
}}
"""
    tools = _tools(analyze_files({"src/main/kotlin/demo/Build.kt": src}))
    assert set(tools) == expected | {"qualified"}  # the fully qualified name is always the SDK's


def test_kotlin_add_tools_nested_class_does_not_read_the_outer_instance_list(analyze_files) -> None:
    """An outer class's instance ``val tools`` is visible in an ``inner`` class and a local class,
    not in a nested (non-``inner``) class, an ``object`` or a companion: there the name resolves
    further out (the top-level list), or to nothing."""
    src = """
package demo

import io.modelcontextprotocol.kotlin.sdk.RegisteredTool
import io.modelcontextprotocol.kotlin.sdk.Tool
import io.modelcontextprotocol.kotlin.sdk.server.Server

val shared = listOf(RegisteredTool(Tool(name = "top")) { _ -> null })

class Outer {
    val tools = listOf(RegisteredTool(Tool(name = "outer")) { _ -> null })
    val shared = listOf(RegisteredTool(Tool(name = "outer_shared")) { _ -> null })
    val forLocal = listOf(RegisteredTool(Tool(name = "local")) { _ -> null })

    inner class In { fun r(s: Server) = s.addTools(tools) }
    class Nested { fun r(s: Server) { s.addTools(tools); s.addTools(shared) } }
    object Obj { fun r(s: Server) = s.addTools(tools) }
    companion object { fun r(s: Server) = s.addTools(tools) }

    fun local(s: Server) {
        class L { fun r() = s.addTools(forLocal) }
    }
}
"""
    tools = [
        s
        for s in analyze_files({"src/main/kotlin/demo/Outer.kt": src})
        if s.semanticType == "mcp_tool"
    ]
    assert sorted(t.endpoint for t in tools) == ["local", "outer", "top"]


def test_kotlin_add_tools_name_a_companion_declares_is_not_the_top_level_list(
    analyze_files,
) -> None:
    """In a class whose ``companion object`` declares ``tools``, the name resolves to the
    companion's property (also from a nested class), not to a top-level ``tools``: its list is
    not read, so no record. Inside the companion its own list is read, and a class with no such
    companion still reads the top-level list."""
    header = """
package demo

import io.modelcontextprotocol.kotlin.sdk.RegisteredTool
import io.modelcontextprotocol.kotlin.sdk.Tool
import io.modelcontextprotocol.kotlin.sdk.server.Server

val tools = listOf(RegisteredTool(Tool(name = "outer")) { _ -> null })
"""
    companion = """
class A {
    companion object {
        val tools = listOf(RegisteredTool(Tool(name = "comp")) { _ -> null })
        fun g(s: Server) { s.addTools(tools) }
    }
    fun f(s: Server) { s.addTools(tools) }
    class N { fun h(s: Server) { s.addTools(tools) } }
}
"""
    plain = """
class B {
    fun f(s: Server) { s.addTools(tools) }
}
"""

    def endpoints(body: str) -> list[str | None]:
        stmts = analyze_files({"src/main/kotlin/demo/A.kt": header + body})
        return sorted(s.endpoint for s in stmts if s.semanticType == "mcp_tool")

    assert endpoints(companion) == ["comp"]
    assert endpoints(plain) == ["outer"]
