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


# ── Resources and prompts ─────────────────────────────────────────────────────


def _of(statements: list[Statement], semantic_type: str) -> dict[str | None, Statement]:
    found = [s for s in statements if s.semanticType == semantic_type]
    for s in found:
        assert s.name == s.endpoint
    return {s.endpoint: s for s in found}


KOTLIN_SDK_ENTRIES = """
package demo

import io.modelcontextprotocol.kotlin.sdk.server.Server
import io.modelcontextprotocol.kotlin.sdk.types.PromptArgument

fun configure(server: Server) {
    server.addResource(uri = "docs://readme", name = "README",
        description = "The README", mimeType = "text/markdown") { request -> TODO() }
    server.addResource("file:///{literal}", "Literal", "A file") { request -> TODO() }
    server.addResourceTemplate(uriTemplate = "users://{id}", name = "User") { request, vars -> TODO() }
    server.addPrompt(
        name = "review",
        description = "Review code",
        arguments = listOf(
            PromptArgument(name = "code", description = "The code", required = true),
            PromptArgument(name = "style"),
        ),
    ) { request -> TODO() }
    server.addPrompt(name = "dyn", arguments = args) { request -> TODO() }
    server.addResource(uri = computed, name = "x", description = "x") { request -> TODO() }
}
"""


def test_kotlin_sdk_resources_and_prompts_with_no_tool(analyze_files) -> None:
    statements = analyze_files({"Server.kt": KOTLIN_SDK_ENTRIES})
    resources = _of(statements, "mcp_resource")
    assert set(resources) == {"docs://readme", "file:///{literal}", "users://{id}"}
    readme = resources["docs://readme"]
    assert (
        readme.framework,
        readme.method,
        readme.description,
        readme.handlerLine,
        readme.params,
    ) == ("kotlin-mcp", "RESOURCE", "The README", 9, [])
    assert resources["file:///{literal}"].description == "A file"
    assert resources["file:///{literal}"].params == []
    assert _params(resources["users://{id}"]) == [("id", "string", True, None, None)]
    prompts = _of(statements, "mcp_prompt")
    assert set(prompts) == {"review", "dyn"}
    review = prompts["review"]
    assert (review.framework, review.description, review.handlerLine) == (
        "kotlin-mcp",
        "Review code",
        19,
    )
    assert _params(review) == [
        ("code", None, True, None, "The code"),
        ("style", None, False, None, None),
    ]
    assert prompts["dyn"].params is None


KOTLIN_JAVA_SDK_ENTRIES = """
package demo

import io.modelcontextprotocol.server.McpServerFeatures
import io.modelcontextprotocol.spec.McpSchema

val memo = McpServerFeatures.SyncResourceSpecification(
    McpSchema.Resource.builder("memo://insights", "Insights").description("Memo").build()) { ex, req -> TODO() }
val review = McpServerFeatures.SyncPromptSpecification(
    McpSchema.Prompt("review", "Review", listOf(McpSchema.PromptArgument("code", "The code", true)))) { ex, req -> TODO() }
val tool = McpServerFeatures.SyncToolSpecification(McpSchema.Tool.builder("named").build()) { ex, req -> TODO() }
"""


def test_kotlin_java_sdk_resources_and_prompts(analyze_files) -> None:
    statements = analyze_files({"Specs.kt": KOTLIN_JAVA_SDK_ENTRIES})
    memo = _of(statements, "mcp_resource")["memo://insights"]
    assert (memo.framework, memo.description, memo.params, memo.handlerLine) == (
        "java-mcp",
        "Memo",
        [],
        8,
    )
    review = _of(statements, "mcp_prompt")["review"]
    assert review.framework == "java-mcp" and review.description == "Review"
    assert _params(review) == [("code", None, True, None, "The code")]
    assert _tools(statements)["named"].params == []  # the SDK 1.0 builder names the tool


def test_kotlin_resource_look_alikes(analyze_files) -> None:
    """``addResource`` / ``addPrompt`` without the SDK's server package: no record."""
    src = """
package demo

import com.acme.Registry

fun configure(registry: Registry) {
    registry.addResource(uri = "docs://x", name = "x", description = "x") { TODO() }
    registry.addPrompt(name = "p") { TODO() }
}
"""
    statements = analyze_files({"Other.kt": src})
    assert not [s for s in statements if s.semanticType in ("mcp_resource", "mcp_prompt")]


def test_kotlin_entry_forms(analyze_files) -> None:
    """Java SDK constructors and builders in Kotlin, and the Kotlin SDK's typed entries."""
    src = """
package demo

import io.modelcontextprotocol.kotlin.sdk.server.Server
import io.modelcontextprotocol.kotlin.sdk.types.*
import io.modelcontextprotocol.server.McpServerFeatures
import io.modelcontextprotocol.spec.McpSchema

val tpl = McpServerFeatures.SyncResourceTemplateSpecification(
    McpSchema.ResourceTemplate("files://{path}", "Files", "A file", "text/plain", null)) { ex, req -> TODO() }
val res = McpServerFeatures.SyncResourceSpecification(
    McpSchema.Resource("memo://a", "A", null, "Memo A", "text/plain", null, null, null)) { ex, req -> TODO() }
val titled = McpServerFeatures.AsyncPromptSpecification(
    McpSchema.Prompt("titled", "Title", "Described", listOf(
        McpSchema.PromptArgument.builder("q").description("Query").required(true).build()))) { ex, req -> TODO() }
val bad = McpServerFeatures.SyncPromptSpecification(
    McpSchema.Prompt("bad", "d", listOf(McpSchema.PromptArgument("a", description = "x", required = true)))) { ex, req -> TODO() }
val built = McpServerFeatures.SyncPromptSpecification(
    McpSchema.Prompt.builder("built").description("B").arguments(null).build()) { ex, req -> TODO() }

fun configure(server: Server) {
    server.addResourceTemplate(ResourceTemplate("users://{id}", "User", "A user", null)) { r, v -> TODO() }
    server.addPrompt(Prompt(name = "typed", description = "T", arguments = null)) { TODO() }
    server.addPrompt("positional", "P", listOf(PromptArgument("a", "d", false))) { TODO() }
}
"""
    statements = analyze_files({"Entries.kt": src})
    resources = _of(statements, "mcp_resource")
    assert (resources["files://{path}"].description, resources["files://{path}"].framework) == (
        "A file",
        "java-mcp",
    )
    assert _params(resources["files://{path}"]) == [("path", "string", True, None, None)]
    assert resources["memo://a"].description == "Memo A" and resources["memo://a"].params == []
    assert resources["users://{id}"].framework == "kotlin-mcp"
    prompts = _of(statements, "mcp_prompt")
    assert prompts["titled"].description == "Described"
    assert _params(prompts["titled"]) == [("q", None, True, None, "Query")]
    assert prompts["bad"].params is None  # named arguments: positions differ between the SDKs
    assert prompts["built"].params == [] and prompts["built"].description == "B"
    assert (prompts["typed"].framework, prompts["typed"].description, prompts["typed"].params) == (
        "kotlin-mcp",
        "T",
        [],
    )
    assert _params(prompts["positional"]) == [("a", None, False, None, "d")]


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
