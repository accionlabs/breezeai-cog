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
