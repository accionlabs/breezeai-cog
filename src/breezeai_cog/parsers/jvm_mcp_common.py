"""Shared by the JVM MCP packages (``java_springai``, ``java_mcp``, ``kotlin_springai``,
``kotlin_mcp``).

* **Server proof** — fact :data:`FACT`: the modules whose build file (``pom.xml`` /
  ``build.gradle[.kts]``) declares a Spring AI MCP **server** starter, registered for Java and
  Kotlin. Spring AI's ``@Tool`` is also what a chat client uses, so it counts as an MCP tool
  only in such a module (:func:`in_mcp_server_module`). A ``<dependencyManagement>`` entry (a
  parent BOM) or a Gradle ``platform(…)`` line is not a dependency of the module.
* **``@Tool`` reader** — what both Spring AI packages agree on: the framework value, Spring
  AI's ``@Tool`` class, and the parameter types Spring AI injects (:data:`INJECTED`), which
  are not tool inputs.
* **ToolSpecification reader** — what both MCP Java SDK readers agree on: the framework value
  and a tool's ``params`` from its JSON-schema text (:func:`json_schema_params`).
* **Literals** — a Java string (:func:`java_string`), and for Kotlin a string, a call's
  arguments and an import alias; ``+`` chains are read without recursion
  (:func:`concat_strings`).

Each grammar's tree walk stays in its own package.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..schemas import Parameter
from .additive import FactSpec, index_fact, register_fact
from .emits import Emit
from .mcp_common import schema_params, tool_emit
from .treesitter import node_text

#: ``framework`` of a Spring AI ``@McpTool`` / ``@Tool`` method, in Java or Kotlin.
SPRING_AI = "spring-ai-mcp"
#: ``framework`` of an MCP Java SDK tool specification, in Java or Kotlin.
JAVA_MCP = "java-mcp"
FACT = "spring-ai.mcp-server"
#: Spring AI's ``@Tool``: the annotation counts only when it is this class.
SPRING_TOOL_FQN = "org.springframework.ai.tool.annotation.Tool"
#: Packages of the MCP annotations library (``@McpResource`` / ``@McpPrompt`` / ``@McpArg``): the
#: community project and Spring AI's own copy of it.
MCP_ANNOTATION_PACKAGES = (
    "org.springaicommunity.mcp.annotation",
    "org.springframework.ai.mcp.annotation",
)
_SERVER_DEPENDENCIES = (
    b"spring-ai-starter-mcp-server",
    b"spring-ai-mcp-server",
    b"spring-ai-mcp-server-spring-boot-starter",
)
_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")
#: Parameter types Spring AI injects: not inputs of the tool.
INJECTED = frozenset(
    {
        "ToolContext",
        "McpSyncServerExchange",
        "McpAsyncServerExchange",
        "McpSyncRequestContext",
        "McpAsyncRequestContext",
        "McpTransportContext",
    }
)


# ── Server proof ─────────────────────────────────────────────────────────────

#: A Maven ``<dependencyManagement>`` block (versions for children, not dependencies) or an XML
#: comment; removed before the server-starter check.
_POM_NOT_DEPENDENCIES = re.compile(
    rb"<dependencyManagement\b.*?</dependencyManagement\s*>|<!--.*?-->", re.DOTALL
)
#: A Gradle line that imports a BOM rather than declaring a dependency.
_GRADLE_PLATFORM = re.compile(rb"\b(?:platform|enforcedPlatform|mavenBom)\b")


def _declares_server(name: str, source: bytes) -> bool:
    """True when the build file declares a Spring AI MCP server starter as a dependency."""
    if name == "pom.xml":
        source = _POM_NOT_DEPENDENCIES.sub(b"", source)
    else:
        source = b"\n".join(
            line for line in source.split(b"\n") if not _GRADLE_PLATFORM.search(line)
        )
    return any(d in source for d in _SERVER_DEPENDENCIES)


def _collect_modules(root: Any, source: bytes, rel: str, repo_root: Path | None) -> Any:
    folder, _, name = rel.rpartition("/")
    if name in _BUILD_FILES and _declares_server(name, source):
        return folder  # "" for the repo root
    return None


def _resolve_modules(values: list[Any], index: object) -> frozenset[str] | None:
    """The folders of the modules that declare an MCP server starter."""
    return frozenset(values) or None


def in_mcp_server_module(path: str, index: Any | None) -> bool:
    """True when the file at repo-relative ``path`` is in a module whose build file declares a
    Spring AI MCP server starter (fact :data:`FACT`)."""
    modules: frozenset[str] | None = index_fact(index, FACT)
    return bool(modules) and any(m == "" or path.startswith(f"{m}/") for m in modules or ())


for _language in ("java", "kotlin"):
    register_fact(
        FactSpec(
            name=FACT,
            language=_language,
            gate=(b"mcp-server",),
            collect=_collect_modules,
            resolve=_resolve_modules,
            sources=(".xml", ".gradle", ".kts"),
            grammar=None,
        )
    )


# ── Shared readers ───────────────────────────────────────────────────────────


def walk(node: Node) -> list[Node]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.named_children))
    return out


def concat_strings(
    node: Node,
    source: bytes,
    kind: str,
    leaf: Callable[[Node | None, bytes], str | None],
) -> str | None:
    """The ``+`` chain of string literals rooted at ``node`` (nodes of type ``kind``), read
    left to right with an explicit stack: a generated thousand-part concatenation cannot
    exhaust the recursion limit. None when any operand is not a literal (``leaf`` → None) or
    an operator is not ``+``."""
    parts: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == kind:
            if any(not c.is_named and node_text(c, source) != "+" for c in n.children):
                return None
            stack.extend(reversed([c for c in n.named_children if "comment" not in c.type]))
            continue
        value = leaf(n, source)
        if value is None:
            return None
        parts.append(value)
    return "".join(parts)


def json_schema_params(text: str | None) -> list[Parameter] | None:
    """Params of a JSON schema's text (``properties`` / ``required``); None when it does not
    parse (unknown). The schema itself is read by :func:`~.mcp_common.schema_params`."""
    if text is None:
        return None
    try:
        schema = json.loads(text)
    except (ValueError, RecursionError):  # a deeply nested literal must not fail the file
        return None
    return schema_params(schema)


# ── The McpServer builder shortcut ───────────────────────────────────────────

#: ``.tool(`` / ``.toolCall(`` — a cheap byte check before any tree walk.
SHORTCUT_CALL = re.compile(rb"\.\s*tool(?:Call)?\s*\(")
#: The ``McpServer`` factories a shortcut chain starts from.
SERVER_FACTORIES = frozenset({"sync", "async"})
#: The builder methods that register a ``Tool`` with its handler.
SHORTCUTS = frozenset({"tool", "toolCall"})


def shortcut_emit(
    node: Node,
    name_node: Node,
    fields: tuple[str, str | None, list[Parameter] | None],
    handler: tuple[str | None, int | None],
    source: bytes,
) -> Emit:
    """The record of a ``.tool(…)`` / ``.toolCall(…)`` shortcut: at the method's name, not at
    the start of the chain (several tools of one chain get their own lines)."""
    name, description, params = fields
    emit = tool_emit(
        node,
        source,
        endpoint=name,
        framework=JAVA_MCP,
        handler=handler[0],
        handler_line=handler[1],
        description=description,
        params=params,
        node_type=None,
    )
    emit.line = name_node.start_point[0] + 1
    emit.col = name_node.start_point[1]
    line_end = source.find(b"\n", name_node.start_byte)
    stop = node.end_byte if line_end < 0 else min(line_end, node.end_byte)
    emit.text = source[name_node.start_byte : stop].decode("utf-8", "replace").rstrip()
    return emit


# ── Java literals ────────────────────────────────────────────────────────────


def java_string(node: Node | None, source: bytes) -> str | None:
    """A Java string literal or text block (``+`` concatenation of literals allowed)."""
    if node is None:
        return None
    if node.type == "binary_expression":
        return concat_strings(node, source, "binary_expression", java_string)
    if node.type != "string_literal":
        return None
    text = node_text(node, source)
    if text.startswith('"""'):
        return text[3:-3]
    return text[1:-1]  # escapes kept as written


# ── Kotlin literals, arguments and import aliases ────────────────────────────

#: Packages whose classes an import alias may stand for.
_MCP_PACKAGES = (
    "org.springframework.ai.",
    "org.springaicommunity.mcp.",
    "io.modelcontextprotocol.",
)
_IMPORT_ALIAS = re.compile(rb"^[ \t]*import[ \t]+([\w.]+)[ \t]+as[ \t]+(\w+)", re.MULTILINE)


def kotlin_aliases(source: bytes) -> dict[str, str]:
    """``import a.b.C as D`` → ``{"D": "a.b.C"}``."""
    return {m.group(2).decode(): m.group(1).decode() for m in _IMPORT_ALIAS.finditer(source)}


def kotlin_canon(name: str, aliases: dict[str, str]) -> str:
    """The class a simple type name stands for: an alias of an MCP / Spring AI class → that
    class's simple name; an alias of any other class → ``""`` (never a match)."""
    fqcn = aliases.get(name)
    if fqcn is None:
        return name
    return fqcn.rsplit(".", 1)[-1] if fqcn.startswith(_MCP_PACKAGES) else ""


def kotlin_type_name(node: Node, source: bytes, aliases: dict[str, str]) -> str:
    """Simple class name of a type reference (``a.b.C`` → ``C``; an alias resolved)."""
    text = node_text(node, source).split("<")[0]
    return text.rsplit(".", 1)[-1] if "." in text else kotlin_canon(text, aliases)


def kotlin_string(node: Node | None, source: bytes) -> str | None:
    """A Kotlin string literal without templates (``+`` of literals, ``.trimIndent()``
    allowed); escapes kept as written."""
    if node is None:
        return None
    if node.type == "additive_expression":
        return concat_strings(node, source, "additive_expression", kotlin_string)
    if node.type == "call_expression":  # """…""".trimIndent()
        callee = node.named_children[0] if node.named_children else None
        if callee is not None and callee.type == "navigation_expression":
            if kotlin_suffix_name(callee, source) == "trimIndent" and not kotlin_call_args(
                node, source
            ):
                return kotlin_string(callee.named_children[0], source)
        return None
    if node.type != "string_literal":
        return None
    if any(c.type != "string_content" for c in node.named_children):
        return None  # a "$template"
    text = node_text(node, source)
    if text.startswith('"""'):
        return text[3:-3]
    return text[1:-1]


def kotlin_suffix_name(nav: Node, source: bytes) -> str:
    """The member a ``navigation_expression`` selects (``a.b.c`` → ``c``)."""
    suffix = nav.named_children[-1] if nav.named_children else None
    if suffix is None or suffix.type != "navigation_suffix":
        return ""
    name = next((c for c in suffix.named_children if c.type == "simple_identifier"), None)
    return node_text(name, source) if name is not None else ""


def kotlin_call_args(call: Node, source: bytes) -> list[tuple[str | None, Node]]:
    """``(name or None, value)`` of a call's arguments, a trailing lambda last (unnamed)."""
    suffix = next((c for c in call.named_children if c.type == "call_suffix"), None)
    out: list[tuple[str | None, Node]] = []
    for part in suffix.named_children if suffix is not None else []:
        if part.type == "value_arguments":
            for arg in part.named_children:
                if arg.type != "value_argument" or not arg.named_children:
                    continue
                named = any(c.type == "=" for c in arg.children)
                key = node_text(arg.named_children[0], source) if named else None
                out.append((key, arg.named_children[-1]))
        elif part.type == "annotated_lambda":
            lam = next((c for c in part.named_children if c.type == "lambda_literal"), None)
            if lam is not None:
                out.append((None, lam))
    return out
