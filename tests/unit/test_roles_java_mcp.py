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


@pytest.mark.parametrize(
    ("imports", "expected"),
    [
        # another McpServer, in a file that uses the MCP SDK's schema types
        ("import com.acme.McpServer;\nimport io.modelcontextprotocol.spec.McpSchema;", set()),
        # the SDK's package by `*`, but another McpServer imported by name wins
        (
            "import io.modelcontextprotocol.server.*;\nimport com.acme.McpServer;\n"
            "import io.modelcontextprotocol.spec.McpSchema;",
            set(),
        ),
        # no import of McpServer at all (same-package class, or another wildcard)
        ("import com.acme.*;\nimport io.modelcontextprotocol.spec.McpSchema;", set()),
        (
            "import io.modelcontextprotocol.server.*;\nimport io.modelcontextprotocol.spec.McpSchema;",
            {"calc"},
        ),
    ],
)
def test_mcp_java_sdk_shortcut_needs_the_sdks_mcp_server(analyze_files, imports, expected) -> None:
    src = f"""
package demo;

{imports}

public class App {{
    void start(Object transport) {{
        McpServer.sync(transport).tool(new McpSchema.Tool("calc", "C", "{{}}"), (e, a) -> null).build();
        io.modelcontextprotocol.server.McpServer.async(transport)
            .tool(new McpSchema.Tool("qualified", "Q", "{{}}"), (e, a) -> null).build();
    }}
}}
"""
    tools = _tools(analyze_files({"src/main/java/demo/App.java": src}))
    assert set(tools) == expected | {"qualified"}  # the fully qualified name is always the SDK's


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


@pytest.mark.parametrize(
    "declaration",
    [
        "class McpServer { static Object sync(Object t) { return null; } }",
        "public class App2 { interface McpServer {} }",
    ],
)
def test_mcp_java_sdk_shortcut_with_a_wildcard_import_and_an_own_mcp_server_class(
    analyze_files, declaration
) -> None:
    """With the SDK's package imported by ``*``, a ``McpServer`` the file declares itself (top
    level or nested) is what the bare name means, not the SDK's."""
    src = f"""
package demo;

import io.modelcontextprotocol.server.*;
import io.modelcontextprotocol.spec.McpSchema;

public class App {{
    void start(Object transport) {{
        McpServer.sync(transport).tool(new McpSchema.Tool("calc", "C", "{{}}"), (e, a) -> null).build();
        io.modelcontextprotocol.server.McpServer.async(transport)
            .tool(new McpSchema.Tool("qualified", "Q", "{{}}"), (e, a) -> null).build();
    }}
}}

{declaration}
"""
    assert set(_tools(analyze_files({"src/main/java/demo/App.java": src}))) == {"qualified"}


@pytest.mark.parametrize(
    ("other_file", "imports", "expected"),
    [
        # a same-package McpServer in another file wins over the SDK's `*`
        (
            ("src/main/java/demo/McpServer.java", "package demo;\npublic class McpServer {}\n"),
            "import io.modelcontextprotocol.server.*;",
            set(),
        ),
        # imported by name, the SDK's class wins over the same-package one
        (
            ("src/main/java/demo/McpServer.java", "package demo;\npublic class McpServer {}\n"),
            "import io.modelcontextprotocol.server.McpServer;",
            {"calc"},
        ),
        # a McpServer of another package is not visible: the `*` import is the SDK's
        (
            ("src/main/java/other/McpServer.java", "package other;\npublic class McpServer {}\n"),
            "import io.modelcontextprotocol.server.*;",
            {"calc"},
        ),
        # a Kotlin class or typealias of the same package is a type of the package too
        (
            ("src/main/kotlin/demo/McpServer.kt", "package demo\n\nclass McpServer\n"),
            "import io.modelcontextprotocol.server.*;",
            set(),
        ),
        (
            ("src/main/kotlin/demo/Aliases.kt", "package demo\n\ntypealias McpServer = Any\n"),
            "import io.modelcontextprotocol.server.*;",
            set(),
        ),
        (
            ("src/main/kotlin/other/McpServer.kt", "package other\n\nclass McpServer\n"),
            "import io.modelcontextprotocol.server.*;",
            {"calc"},
        ),
    ],
)
def test_mcp_java_sdk_wildcard_mcp_server_loses_to_a_same_package_class(
    analyze_files, other_file, imports, expected
) -> None:
    """In Java a type of the file's own package hides one imported by ``*``: with only
    ``io.modelcontextprotocol.server.*``, a ``McpServer`` declared in another file of the same
    package (Java or Kotlin, a ``typealias`` included) is what the bare name means."""
    src = f"""package demo;

{imports}
import io.modelcontextprotocol.spec.McpSchema;

public class App {{
    void start(Object transport) {{
        McpServer.sync(transport).tool(new McpSchema.Tool("calc", "C", "{{}}"), (e, a) -> null).build();
    }}
}}
"""
    path, other = other_file
    tools = _tools(analyze_files({"src/main/java/demo/App.java": src, path: other}))
    assert set(tools) == expected


def test_mcp_java_sdk_method_reference_handler_line_is_the_named_class_method(
    analyze_files,
) -> None:
    """A method reference takes the line of the one method of its name in the class it names:
    ``this::calc`` the class around it, ``Other::calc`` the class ``Other`` of this file. An
    overloaded name, ``expr::calc`` (a variable), ``super::calc`` or a class of another file
    give the name only. Shortcut and tool specification alike."""
    src = """package demo;

import io.modelcontextprotocol.server.McpServer;
import io.modelcontextprotocol.server.McpServerFeatures;
import io.modelcontextprotocol.spec.McpSchema;

public class App {
    Object calc(Object e, Object a) { return null; }
    Object over(Object e, Object a) { return null; }
    Object over(Object e) { return null; }

    void start(Object transport, Other expr) {
        McpServer.sync(transport)
            .tool(new McpSchema.Tool("own", "O", "{}"), this::calc)
            .tool(new McpSchema.Tool("other", "O", "{}"), Other::calc)
            .tool(new McpSchema.Tool("overloaded", "O", "{}"), this::over)
            .tool(new McpSchema.Tool("expr", "O", "{}"), expr::calc)
            .tool(new McpSchema.Tool("elsewhere", "O", "{}"), Remote::calc)
            .build();
    }

    McpServerFeatures.SyncToolSpecification spec() {
        return new McpServerFeatures.SyncToolSpecification(
            new McpSchema.Tool("spec_other", "S", "{}"), Other::calc);
    }

    McpServerFeatures.SyncToolSpecification spec2(Other expr) {
        return new McpServerFeatures.SyncToolSpecification(
            new McpSchema.Tool("spec_expr", "S", "{}"), expr::calc);
    }
}

class Other {
    static Object calc(Object e, Object a) { return null; }
}
"""
    tools = _tools(analyze_files({"src/main/java/demo/App.java": src}))
    got = {name: (t.handler, t.handlerLine) for name, t in tools.items()}
    assert got == {
        "own": ("calc", 8),
        "other": ("calc", 34),
        "overloaded": ("over", None),
        "expr": ("calc", None),
        "elsewhere": ("calc", None),
        "spec_other": ("calc", 34),
        "spec_expr": ("calc", None),
    }

    # The file's only `calc` is not the method of another class, or of a variable's class.
    single = """package demo;

import io.modelcontextprotocol.server.McpServer;
import io.modelcontextprotocol.spec.McpSchema;

public class App {
    Object calc(Object e, Object a) { return null; }

    void start(Object transport, Object expr) {
        McpServer.sync(transport)
            .tool(new McpSchema.Tool("remote", "R", "{}"), Remote::calc)
            .tool(new McpSchema.Tool("expr", "E", "{}"), expr::calc)
            .tool(new McpSchema.Tool("named", "N", "{}"), App::calc)
            .build();
    }
}
"""
    tools = _tools(analyze_files({"src/main/java/demo/App.java": single}))
    got = {name: (t.handler, t.handlerLine) for name, t in tools.items()}
    assert got == {"remote": ("calc", None), "expr": ("calc", None), "named": ("calc", 7)}


def test_mcp_server_types_fact_reads_no_other_languages_source() -> None:
    """Each JVM language collects the ``McpServer`` fact from its own files (Kotlin also from
    ``.kts`` scripts) and reads the other's value from the related index the pipeline hands it,
    so a mixed Java/Kotlin repo never reads its ``.java`` or ``.kt`` sources twice."""
    from breezeai_cog.parsers.additive import fact_sources
    from breezeai_cog.parsers.java.parser import JavaParser
    from breezeai_cog.parsers.kotlin.parser import KotlinParser

    sources = fact_sources()
    assert ".kt" not in sources.get("java", ())
    assert ".java" not in sources.get("kotlin", ())
    assert "kotlin" in JavaParser.related_languages
    assert "java" in KotlinParser.related_languages


_WILDCARD_JAVA = """package demo;

import io.modelcontextprotocol.server.*;
import io.modelcontextprotocol.spec.McpSchema;

public class App {
    void start(Object transport) {
        McpServer.sync(transport).tool(new McpSchema.Tool("calc", "C", "{}"), (e, a) -> null).build();
    }
}
"""
_WILDCARD_KOTLIN = """package demo

import io.modelcontextprotocol.server.*
import io.modelcontextprotocol.spec.McpSchema

fun build(transport: Any) {
    McpServer.sync(transport).tool(McpSchema.Tool("calc", "C", "{}")) { _, _ -> null }.build()
}
"""


@pytest.mark.parametrize(
    ("failing", "files"),
    [
        (
            "kotlin",
            {
                "src/main/java/demo/App.java": _WILDCARD_JAVA,
                "src/main/kotlin/demo/Util.kt": "package demo\n\nfun util() = 1\n",
            },
        ),
        (
            "java",
            {
                "src/main/kotlin/demo/Build.kt": _WILDCARD_KOTLIN,
                "src/main/java/demo/Util.java": "package demo;\npublic class Util {}\n",
            },
        ),
        ("java", {"src/main/java/demo/App.java": _WILDCARD_JAVA}),
        ("kotlin", {"src/main/kotlin/demo/Build.kt": _WILDCARD_KOTLIN}),
    ],
)
def test_wildcard_mcp_server_is_not_proven_when_a_jvm_index_failed(
    analyze_files, monkeypatch, failing, files
) -> None:
    """With only ``io.modelcontextprotocol.server.*`` imported, bare ``McpServer`` is the SDK's
    only when no type of the file's package has that name. When the index of a JVM language
    with files failed (its own, or the other one's), its ``McpServer`` types are unknown, so
    the name is not proven — no record. With the index built, the tool is recorded."""
    from breezeai_cog.parsers.java.parser import JavaParser
    from breezeai_cog.parsers.kotlin.parser import KotlinParser

    assert set(_tools(analyze_files(files))) == {"calc"}

    def fail(*args, **kwargs):
        raise RuntimeError("index failed")

    parser = {"java": JavaParser, "kotlin": KotlinParser}[failing]
    monkeypatch.setattr(parser, "build_index", fail)
    assert _tools(analyze_files(files)) == {}


def test_wildcard_mcp_server_is_proven_when_the_other_jvm_language_has_no_files(
    analyze_files,
) -> None:
    """A Java-only repo has no Kotlin index at all (not a failed one): nothing of Kotlin can
    declare ``McpServer``, so the wildcard-imported name is the SDK's."""
    assert set(_tools(analyze_files({"src/main/java/demo/App.java": _WILDCARD_JAVA}))) == {"calc"}
