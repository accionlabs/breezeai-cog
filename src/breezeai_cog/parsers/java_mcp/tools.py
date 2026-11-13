"""Additive detector: tools of a Java MCP server built on Spring AI.

::

    @McpTool(name = "weather", description = "Current weather")     → mcp_tool  spring-ai-mcp  TOOL  weather
    public String weather(@McpToolParam(description = "City") String city) { … }

    @Tool(description = "Current weather")                          → mcp_tool  (only in an MCP server)
    public String weather(@ToolParam(description = "City", required = false) String city, ToolContext ctx)

* ``@McpTool`` / ``@McpToolParam`` (Spring AI MCP annotations) are MCP tools by definition.
* ``@Tool`` / ``@ToolParam`` are Spring AI's general function-calling annotations: a chat client
  uses them too. They count only in a module whose build file (``pom.xml`` /
  ``build.gradle[.kts]`` in the file's folder or an ancestor) declares a Spring AI MCP
  **server** dependency (fact :data:`FACT`); otherwise nothing is emitted, so a chat-client
  module next to an MCP server module stays out.
  A ``<dependencyManagement>`` entry (a parent BOM) or a Gradle ``platform(…)`` line is not a
  dependency of the module. ``@Tool`` must also be Spring AI's: imported from
  ``org.springframework.ai.tool.annotation`` or written fully qualified.
* ``endpoint`` — ``name =`` when a literal, else the method name (Spring AI's default).
* ``params`` — ``description`` / ``required`` from the parameter annotation (required by
  default); ``ToolContext`` and MCP exchange / context parameters are injected, not inputs.

MCP Java SDK (``io.modelcontextprotocol``), without Spring::

    new McpServerFeatures.SyncToolSpecification(                    → mcp_tool  mcp-java-sdk  TOOL  calc
        new McpSchema.Tool("calc", "Calculator", SCHEMA_JSON), (exchange, args) -> …)
    McpServerFeatures.SyncToolSpecification.builder()               → (Async… and the builders alike)
        .tool(McpSchema.Tool.builder().name("calc").description("…").inputSchema(…).build())
        .callHandler(this::calc).build()

* The tool is a ``Tool`` constructor or builder, inline or a local variable declared once in
  the file; a non-literal name → no record. ``description`` only from a literal (the
  three-argument constructor's second argument, or ``.description("…")``).
* ``params`` from a JSON-schema string literal (``properties`` / ``required``); a schema that is
  not a literal, or does not parse → None (unknown).
* ``handler`` — a method reference (``this::calc``, ``Calc::calc``) names it; a lambda has no
  name, only its line.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ...schemas import ToolParam
from ..additive import (
    DetectContext,
    Detection,
    Detector,
    FactSpec,
    Gate,
    index_fact,
    register_detector,
    register_fact,
)
from ..emits import Emit
from ..mcp_common import param, tool_emit
from ..treesitter import node_text

FRAMEWORK = "spring-ai-mcp"
SDK_FRAMEWORK = "mcp-java-sdk"
FACT = "spring-ai.mcp-server"
_SERVER_DEPENDENCIES = (
    b"spring-ai-starter-mcp-server",
    b"spring-ai-mcp-server",
    b"spring-ai-mcp-server-spring-boot-starter",
)
_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")
_INJECTED = frozenset(
    {
        "ToolContext",
        "McpSyncServerExchange",
        "McpAsyncServerExchange",
        "McpSyncRequestContext",
        "McpAsyncRequestContext",
        "McpTransportContext",
    }
)


#: A Maven ``<dependencyManagement>`` block (versions for children, not dependencies) or an XML
#: comment; removed before the server-starter check (BREEZEAI-1457).
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


def _collect(root: Any, source: bytes, rel: str, repo_root: Path | None) -> Any:
    folder, _, name = rel.rpartition("/")
    if name in _BUILD_FILES and _declares_server(name, source):
        return folder  # "" for the repo root
    return None


def _resolve(values: list[Any], index: object) -> frozenset[str] | None:
    """The folders of the modules that declare an MCP server starter."""
    return frozenset(values) or None


def _in_mcp_module(path: str, modules: frozenset[str] | None) -> bool:
    return bool(modules) and any(m == "" or path.startswith(f"{m}/") for m in modules or ())


register_fact(
    FactSpec(
        name=FACT,
        language="java",
        gate=(b"mcp-server",),
        collect=_collect,
        resolve=_resolve,
        sources=(".xml", ".gradle", ".kts"),
        grammar=None,
    )
)


def _string(node: Node | None, source: bytes) -> str | None:
    """A Java string literal or text block (``+`` concatenation of literals allowed)."""
    if node is None:
        return None
    if node.type == "binary_expression":
        parts = [_string(c, source) for c in node.named_children]
        return (
            "".join(p for p in parts if p is not None)
            if all(p is not None for p in parts)
            else None
        )
    if node.type != "string_literal":
        return None
    text = node_text(node, source)
    if text.startswith('"""'):
        return text[3:-3]
    return text[1:-1]  # escapes kept as written


def _annotations(node: Node, source: bytes) -> dict[str, Node]:
    """Annotations in ``node``'s modifiers, by simple name."""
    out: dict[str, Node] = {}
    mods = next((c for c in node.named_children if c.type == "modifiers"), None)
    for a in mods.named_children if mods is not None else []:
        if a.type in ("annotation", "marker_annotation"):
            name = a.child_by_field_name("name")
            if name is not None:
                out[node_text(name, source).rsplit(".", 1)[-1]] = a
    return out


def _element(annotation: Node, key: str, source: bytes) -> Node | None:
    args = annotation.child_by_field_name("arguments")
    for a in args.named_children if args is not None else []:
        if a.type == "element_value_pair":
            k = a.child_by_field_name("key")
            if k is not None and node_text(k, source) == key:
                return a.child_by_field_name("value")
        elif key == "value" and a.type != "comment":
            return a  # a lone value: @ToolParam("…")
    return None


def _params(method: Node, source: bytes, param_annotation: str) -> list[ToolParam]:
    params = method.child_by_field_name("parameters")
    out: list[ToolParam] = []
    for p in params.named_children if params is not None else []:
        if p.type != "formal_parameter":
            continue
        type_node, name_node = p.child_by_field_name("type"), p.child_by_field_name("name")
        if type_node is None or name_node is None:
            continue
        type_text = node_text(type_node, source)
        if type_text.split("<")[0].rsplit(".", 1)[-1] in _INJECTED:
            continue
        ann = _annotations(p, source).get(param_annotation)
        description = required = None
        if ann is not None:
            description = _string(
                _element(ann, "description", source) or _element(ann, "value", source), source
            )
            req = _element(ann, "required", source)
            required = node_text(req, source) != "false" if req is not None else None
        out.append(
            param(
                node_text(name_node, source),
                type_text,
                required=True if required is None else required,
                description=description,
            )
        )
    return out


def _walk(node: Node) -> list[Node]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.named_children))
    return out


#: ``@Tool`` is Spring AI's only when imported from its package (BREEZEAI-1457).
_SPRING_TOOL_IMPORT = re.compile(
    rb"import\s+org\.springframework\.ai\.tool\.annotation\.(?:Tool|\*)\s*;"
)
_SPRING_TOOL_FQN = "org.springframework.ai.tool.annotation.Tool"


def _spring_tool(ann: Node, source: bytes, imported: bool) -> bool:
    name = ann.child_by_field_name("name")
    text = node_text(name, source) if name is not None else ""
    return text == _SPRING_TOOL_FQN or (imported and text == "Tool")


def _annotated_tools(dc: DetectContext, functions: dict[tuple[str, int], Any]) -> list[Emit]:
    """Spring AI ``@McpTool`` / ``@Tool`` methods."""
    source = dc.source
    mcp_annotations = b"McpTool" in source
    plain = b"springframework.ai" in source and _in_mcp_module(
        dc.record.path, index_fact(dc.ctx.resolution_index, FACT)
    )
    if not mcp_annotations and not plain:
        return []
    imported = bool(_SPRING_TOOL_IMPORT.search(source))
    found: list[Emit] = []
    for method in _walk(dc.root):
        if method.type != "method_declaration":
            continue
        anns = _annotations(method, source)
        if "McpTool" in anns:
            ann, param_ann = anns["McpTool"], "McpToolParam"
        elif plain and "Tool" in anns and _spring_tool(anns["Tool"], source, imported):
            ann, param_ann = anns["Tool"], "ToolParam"
        else:
            continue
        name_node = method.child_by_field_name("name")
        if name_node is None:
            continue
        method_name = node_text(name_node, source)
        named = _element(ann, "name", source)
        endpoint = _string(named, source) if named is not None else method_name
        if not endpoint:
            continue
        line = method.start_point[0] + 1
        owner = functions.get((method_name, line))
        found.append(
            tool_emit(
                ann,
                source,
                endpoint=endpoint,
                framework=FRAMEWORK,
                handler=method_name,
                handler_line=line,
                description=_string(_element(ann, "description", source), source),
                params=_params(method, source, param_ann),
                parent=owner.id if owner is not None else None,
            )
        )
    return found


# ── MCP Java SDK (BREEZEAI-1457) ────────────────────────────────────────────

_JAVA_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "s": " "}


def _unescape(text: str) -> str | None:
    """Java string escapes → characters; None for an octal / unicode escape (not read)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1 : i + 2]
        if nxt in ('"', "'", "\\"):
            out.append(nxt)
        elif nxt in _JAVA_ESCAPES:
            out.append(_JAVA_ESCAPES[nxt])
        elif nxt == "\n":  # text block line continuation
            pass
        else:
            return None
        i += 2
    return "".join(out)


def _args(node: Node) -> list[Node]:
    args = node.child_by_field_name("arguments")
    return [a for a in (args.named_children if args is not None else []) if a.type != "comment"]


def _type_name(node: Node, source: bytes) -> str:
    """Simple name of an ``object_creation_expression``'s type (generics dropped)."""
    t = node.child_by_field_name("type")
    return node_text(t, source).split("<")[0].rsplit(".", 1)[-1] if t is not None else ""


def _builder_chain(node: Node, source: bytes) -> tuple[str, list[tuple[str, list[Node]]]] | None:
    """``X.builder().a(1).b(2).build()`` → ``("X", [("builder", []), ("a", [1]), …])``
    (innermost first); None when ``node`` is not such a chain."""
    calls: list[tuple[str, list[Node]]] = []
    cur: Node | None = node
    while cur is not None and cur.type == "method_invocation":
        name = cur.child_by_field_name("name")
        calls.append((node_text(name, source) if name is not None else "", _args(cur)))
        cur = cur.child_by_field_name("object")
    if cur is None or not calls or calls[-1][0] != "builder":
        return None
    root = node_text(cur, source).split("<")[0].rsplit(".", 1)[-1]
    return root, list(reversed(calls))


def _schema_params(node: Node | None, source: bytes) -> list[ToolParam] | None:
    """Params of a JSON-schema string literal; None when it is not one (unknown)."""
    raw = _string(node, source)
    return _json_schema_params(_unescape(raw) if raw is not None else None)


def _json_schema_params(text: str | None) -> list[ToolParam] | None:
    """Params of a JSON schema's text (``properties`` / ``required``); None when it does not
    parse (unknown). Shared with the Kotlin detector (``kotlin_mcp``)."""
    if text is None:
        return None
    try:
        schema = json.loads(text)
    except (ValueError, RecursionError):  # a deeply nested literal must not fail the file
        return None
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(props, dict) or not isinstance(required, list):
        return None
    out = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        type_ = spec.get("type")
        desc = spec.get("description")
        out.append(
            param(
                str(name),
                type_ if isinstance(type_, str) else None,
                required=name in required,
                description=desc if isinstance(desc, str) else None,
            )
        )
    return out


def _tool_fields(
    node: Node | None, source: bytes, variables: dict[str, Node | None]
) -> tuple[str, str | None, list[ToolParam] | None] | None:
    """``(name, description, params)`` of an MCP ``Tool`` expression: ``new McpSchema.Tool(…)``,
    ``McpSchema.Tool.builder()…build()``, or a local variable assigned one of them once."""
    if node is not None and node.type == "identifier":
        node = variables.get(node_text(node, source))
    if node is None:
        return None
    if node.type == "object_creation_expression" and _type_name(node, source) == "Tool":
        args = _args(node)
        name = _string(args[0], source) if args else None
        if not name:
            return None
        if len(args) == 3:  # Tool(name, description, inputSchema)
            return name, _string(args[1], source), _schema_params(args[2], source)
        return name, None, None  # other overloads: positions differ by SDK version
    chain = _builder_chain(node, source) if node.type == "method_invocation" else None
    if chain is None or chain[0] != "Tool" or chain[1][-1][0] != "build":
        return None
    calls = {method: args for method, args in chain[1]}
    name_args = calls.get("name") or []
    name = _string(name_args[0], source) if len(name_args) == 1 else None
    if not name:
        return None
    desc_args = calls.get("description") or []
    description = _string(desc_args[0], source) if len(desc_args) == 1 else None
    schema_args = calls.get("inputSchema")
    params = _schema_params(schema_args[-1], source) if schema_args else []
    return name, description, params


def _sdk_handler(
    node: Node | None, source: bytes, methods: dict[str, int | None]
) -> tuple[str | None, int | None]:
    if node is None:
        return None, None
    if node.type == "lambda_expression":
        return None, node.start_point[0] + 1
    if node.type == "method_reference":
        parts = [c for c in node.named_children if c.type != "comment"]
        name = node_text(parts[-1], source) if len(parts) >= 2 else ""
        if name and name != "new":
            return name, methods.get(name)
    return None, None


def _local_variables(root: Node, source: bytes) -> dict[str, Node | None]:
    """Variables declared once in the file → their initializer (None when declared twice)."""
    out: dict[str, Node | None] = {}
    for node in _walk(root):
        if node.type != "variable_declarator":
            continue
        name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
        if name is None:
            continue
        key = node_text(name, source)
        out[key] = None if key in out else value
    return out


def _sdk_tools(dc: DetectContext, functions: dict[tuple[str, int], Any]) -> list[Emit]:
    """``SyncToolSpecification`` / ``AsyncToolSpecification`` of the MCP Java SDK."""
    source = dc.source
    if b"io.modelcontextprotocol" not in source or b"ToolSpecification" not in source:
        return []
    variables = _local_variables(dc.root, source)
    lines: dict[str, list[int]] = {}
    for f in dc.record.functions:
        lines.setdefault(f.name, []).append(f.startLine)
    methods = {name: ls[0] if len(ls) == 1 else None for name, ls in lines.items()}
    found: list[Emit] = []
    for node in _walk(dc.root):
        tool: Node | None = None
        handler: Node | None = None
        if node.type == "object_creation_expression" and _type_name(node, source).endswith(
            "ToolSpecification"
        ):
            args = _args(node)
            if len(args) < 2:
                continue
            tool = args[0]
            handler = next((a for a in reversed(args[1:]) if a.type != "null_literal"), None)
        elif node.type == "method_invocation":
            chain = _builder_chain(node, source)
            if (
                chain is None
                or not chain[0].endswith("ToolSpecification")
                or chain[1][-1][0] != "build"
            ):
                continue
            calls = {method: args for method, args in chain[1]}
            tool_args, handler_args = calls.get("tool") or [], calls.get("callHandler") or []
            tool = tool_args[0] if len(tool_args) == 1 else None
            handler = handler_args[0] if len(handler_args) == 1 else None
        else:
            continue
        fields = _tool_fields(tool, source, variables)
        if fields is None:
            continue
        name, description, params = fields
        handler_name, handler_line = _sdk_handler(handler, source, methods)
        found.append(
            tool_emit(
                node,
                source,
                endpoint=name,
                framework=SDK_FRAMEWORK,
                handler=handler_name,
                handler_line=handler_line,
                description=description,
                params=params,
                node_type=None,
            )
        )
    return found


def _detect(dc: DetectContext) -> Detection | None:
    functions = {(f.name, f.startLine): f for f in dc.record.functions}
    found = _annotated_tools(dc, functions) + _sdk_tools(dc, functions)
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=found[0].fields.get("framework") or FRAMEWORK)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="java-mcp",
        gate=Gate("java", order=30, guard=b"Tool", skip_fixtures=True),
        detect=_detect,
        frameworks=(FRAMEWORK, SDK_FRAMEWORK),
    )
)
