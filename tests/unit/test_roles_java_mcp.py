"""MCP server tools of the MCP Java SDK (``io.modelcontextprotocol``) in Java:
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
    assert (calc.framework, calc.description) == ("java-mcp", "Calculator")
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


def test_mcp_java_sdk_schema_too_deep_to_load_leaves_params_unknown(
    analyze_files, monkeypatch
) -> None:
    from types import SimpleNamespace

    from breezeai_cog.parsers import jvm_mcp_common

    def too_deep(_text: str) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(jvm_mcp_common, "json", SimpleNamespace(loads=too_deep))
    tools = _tools(analyze_files({"src/main/java/demo/Tools.java": JAVA_SDK}))
    assert set(tools) == {"calc", "echo", "dyn"}  # the file still parses
    assert tools["calc"].params is None and tools["echo"].params is None


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
        ("java", "binary_expression", "java_string"),
        ("kotlin", "additive_expression", "kotlin_string"),
    ],
)
def test_a_long_string_concatenation_is_read_without_recursion(language, kind, reader) -> None:
    import sys

    from breezeai_cog.parsers import jvm_mcp_common
    from breezeai_cog.parsers.treesitter import parse_source

    read = getattr(jvm_mcp_common, reader)
    chain = " + ".join(['"ab"'] * (sys.getrecursionlimit() + 500))
    decl = f"class A {{ String x = {chain}; }}" if language == "java" else f"val x = {chain}"
    src = decl.encode()
    node = _first(parse_source(language, src, 0).root_node, kind)
    assert read(node, src) == "ab" * (sys.getrecursionlimit() + 500)
    other = b'class A { boolean x = "a" == "b"; }' if language == "java" else b'val x = "a" - "b"'
    assert read(_first(parse_source(language, other, 0).root_node, kind), other) is None


JAVA_SHORTCUT = """
package demo;

import io.modelcontextprotocol.server.McpServer;
import io.modelcontextprotocol.spec.McpSchema;
import io.modelcontextprotocol.spec.McpSchema.Tool;

public class App {
    void start(Object transport) {
        Tool calc = new Tool("calc", "Calculator", "{\\"type\\":\\"object\\",\\"properties\\":{\\"a\\":{\\"type\\":\\"number\\"}},\\"required\\":[\\"a\\"]}");
        var server = McpServer.sync(transport)
            .serverInfo("demo", "1.0")
            .tool(calc, (exchange, args) -> null)
            .toolCall(McpSchema.Tool.builder().name("echo").description("Echo").build(), this::echo)
            .tool(new Tool(computed(), "x", "{}"), (exchange, args) -> null)
            .build();
        McpServer.async(transport).tool(new McpSchema.Tool("later", "Async", "{}"), App::later).build();
        prepareBuilder().tool(calc, (exchange, args) -> null);
        chatClient.prompt().tool(calc, (exchange, args) -> null);
    }

    Object echo(Object exchange, Object request) { return null; }
    static Object later(Object exchange, Object args) { return null; }
}
"""


def test_mcp_java_sdk_server_shortcut(analyze_files) -> None:
    """``McpServer.sync(…)….tool(tool, handler)`` / ``.toolCall(…)``, in a file with no tool
    specification: only a chain that starts at ``McpServer.sync`` / ``async`` counts."""
    tools = _tools(analyze_files({"src/main/java/demo/App.java": JAVA_SHORTCUT}))
    assert set(tools) == {"calc", "echo", "later"}
    calc = tools["calc"]
    assert (calc.framework, calc.description, calc.startLine, calc.handlerLine) == (
        "java-mcp",
        "Calculator",
        13,
        13,  # the lambda's line
    )
    assert calc.text.startswith("tool(calc")
    assert _params(calc) == [("a", "number", True, None, None)]
    echo = tools["echo"]
    assert (echo.description, echo.handler, echo.handlerLine, echo.startLine) == (
        "Echo",
        "echo",
        22,
        14,
    )
    assert (tools["later"].handler, tools["later"].handlerLine) == ("later", 23)


def test_mcp_java_sdk_shortcut_needs_the_sdk(analyze_files) -> None:
    src = JAVA_SHORTCUT.replace("io.modelcontextprotocol", "com.acme")
    assert _tools(analyze_files({"src/main/java/demo/App.java": src})) == {}


def test_a_spec_passed_to_tools_is_recorded_once(analyze_files) -> None:
    """``.tools(spec)`` is not read: the spec is recorded where it is built, once."""
    src = """
package demo;

import io.modelcontextprotocol.server.McpServer;
import io.modelcontextprotocol.server.McpServerFeatures;
import io.modelcontextprotocol.spec.McpSchema;

public class App {
    void start(Object transport) {
        var spec = new McpServerFeatures.SyncToolSpecification(
            new McpSchema.Tool("calc", "Calculator", "{}"), (exchange, args) -> null);
        McpServer.sync(transport).tools(spec).build();
        McpServer.sync(transport).tools(List.of(spec)).build();
    }
}
"""
    tools = [
        s
        for s in analyze_files({"src/main/java/demo/App.java": src})
        if s.semanticType == "mcp_tool"
    ]
    assert [t.endpoint for t in tools] == ["calc"]


# ── Resources, prompts and the SDK 1.0 Tool builder ───────────────────────────


def _of(statements: list[Statement], semantic_type: str) -> dict[str | None, Statement]:
    found = [s for s in statements if s.semanticType == semantic_type]
    for s in found:
        assert s.name == s.endpoint and s.framework == "java-mcp"
    return {s.endpoint: s for s in found}


JAVA_RESOURCES = """
package demo;

import io.modelcontextprotocol.server.McpServerFeatures;
import io.modelcontextprotocol.spec.McpSchema;
import java.util.List;

public class Resources {
    McpServerFeatures.SyncResourceSpecification memo() {
        return new McpServerFeatures.SyncResourceSpecification(
            new McpSchema.Resource("memo://insights", "Insights", null, "The memo", "text/plain", null, null, null),
            (exchange, req) -> null);
    }

    McpServerFeatures.AsyncResourceTemplateSpecification user() {
        var tpl = McpSchema.ResourceTemplate.builder("users://{id}", "User").description("A user").build();
        return new McpServerFeatures.AsyncResourceTemplateSpecification(tpl, this::readUser);
    }

    McpServerFeatures.SyncResourceSpecification built() {
        return new McpServerFeatures.SyncResourceSpecification(
            McpSchema.Resource.builder().uri("file:///{literal}").name("x").build(), this::readUser);
    }

    McpServerFeatures.SyncResourceSpecification computed(String uri) {
        return new McpServerFeatures.SyncResourceSpecification(new McpSchema.Resource(uri, "x", null, null, null, null, null, null), this::readUser);
    }

    Object readUser(Object exchange, Object req) { return null; }
}
"""


def test_java_resources_with_no_tool(analyze_files) -> None:
    resources = _of(analyze_files({"Resources.java": JAVA_RESOURCES}), "mcp_resource")
    assert set(resources) == {"memo://insights", "users://{id}", "file:///{literal}"}
    memo = resources["memo://insights"]
    assert (memo.method, memo.description, memo.handler, memo.handlerLine, memo.params) == (
        "RESOURCE", "The memo", None, 12, [])
    user = resources["users://{id}"]
    assert (user.description, user.handler, user.handlerLine) == ("A user", "readUser", 29)
    assert _params(user) == [("id", "string", True, None, None)]
    assert resources["file:///{literal}"].params == []  # a fixed URI has no variables


JAVA_PROMPTS = """
package demo;

import io.modelcontextprotocol.server.McpServerFeatures;
import io.modelcontextprotocol.spec.McpSchema;
import java.util.List;

public class Prompts {
    McpServerFeatures.SyncPromptSpecification review() {
        return new McpServerFeatures.SyncPromptSpecification(
            new McpSchema.Prompt("review", "Review code", List.of(
                new McpSchema.PromptArgument("code", "The code", true),
                McpSchema.PromptArgument.builder("style").build())),
            this::render);
    }

    McpServerFeatures.AsyncPromptSpecification greet() {
        return new McpServerFeatures.AsyncPromptSpecification(
            McpSchema.Prompt.builder("greet").description("Say hi").build(), (ex, req) -> null);
    }

    McpServerFeatures.SyncPromptSpecification dyn() {
        return new McpServerFeatures.SyncPromptSpecification(new McpSchema.Prompt("dyn", "d", ARGS), this::render);
    }

    Object render(Object exchange, Object req) { return null; }
}
"""


def test_java_prompts_with_no_tool(analyze_files) -> None:
    prompts = _of(analyze_files({"Prompts.java": JAVA_PROMPTS}), "mcp_prompt")
    assert set(prompts) == {"review", "greet", "dyn"}
    review = prompts["review"]
    assert (review.method, review.description, review.handler, review.handlerLine) == (
        "PROMPT", "Review code", "render", 26)
    assert _params(review) == [
        ("code", None, True, None, "The code"),
        ("style", None, False, None, None),
    ]
    assert prompts["greet"].params == [] and prompts["greet"].description == "Say hi"
    assert prompts["dyn"].params is None


def test_java_resource_look_alikes(analyze_files) -> None:
    """A ``Resource`` / ``Prompt`` outside an MCP specification, or no MCP import: no record."""
    src = """
package demo;

import org.springframework.core.io.Resource;

public class Other {
    Object r = new ResourceSpecification(new Resource("x://y", "n"), h);
    Object p = new SyncPromptSpecification(new Prompt("p", "d", null), h);
}
"""
    statements = analyze_files({"Other.java": src})
    assert not [s for s in statements if s.semanticType in ("mcp_resource", "mcp_prompt")]


def test_java_sdk_1_tool_builder_names_the_tool(analyze_files) -> None:
    src = '''
package demo;

import io.modelcontextprotocol.server.McpServerFeatures;
import io.modelcontextprotocol.spec.McpSchema;

public class Tools {
    Object a = new McpServerFeatures.SyncToolSpecification(
        McpSchema.Tool.builder("plain").description("Plain").build(), this::run);
    Object b = new McpServerFeatures.SyncToolSpecification(
        McpSchema.Tool.builder("mapped", schemaMap).build(), this::run);
    Object c = new McpServerFeatures.SyncToolSpecification(
        McpSchema.Tool.builder("json", mapper, "{\\"type\\":\\"object\\",\\"properties\\":{\\"q\\":{\\"type\\":\\"string\\"}}}").build(), this::run);

    Object run(Object exchange, Object req) { return null; }
}
'''
    tools = _tools(analyze_files({"Tools.java": src}))
    assert set(tools) == {"plain", "mapped", "json"}
    assert tools["plain"].params == [] and tools["plain"].description == "Plain"
    assert tools["mapped"].params is None
    assert _params(tools["json"]) == [("q", "string", False, None, None)]
