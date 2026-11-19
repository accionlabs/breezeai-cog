"""Additive detector: tools of a Java MCP server built with the MCP Java SDK
(``io.modelcontextprotocol``).

::

    new McpServerFeatures.SyncToolSpecification(                    → mcp_tool  java-mcp  TOOL  calc
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

The server builder's shortcut registers a ``Tool`` directly::

    McpServer.sync(transport)                                       → mcp_tool  java-mcp  TOOL  calc
        .tool(new McpSchema.Tool("calc", "Calculator", SCHEMA_JSON), (exchange, args) -> …)
        .toolCall(McpSchema.Tool.builder().name("echo").build(), this::echo)   (also McpServer.async)

* Only on a chain that starts at ``McpServer.sync(`` / ``McpServer.async(``, with ``McpServer``
  imported from ``io.modelcontextprotocol.server`` (by name or ``.*``, and no other
  ``McpServer`` imported; by ``.*`` only when the repository index shows no ``McpServer`` in
  the file's own package) or written fully qualified; the record sits on the ``.tool`` /
  ``.toolCall`` name. The tool and handler are read as above.
* ``.tools(spec…)`` is not read: each specification is recorded where it is built.

Only tool specifications and the shortcut are read here. A Spring AI ``@McpTool`` / ``@Tool`` method belongs to
``java_springai``.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Parameter
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..jvm_mcp_common import (
    JAVA_MCP,
    SERVER_FACTORIES,
    SHORTCUT_CALL,
    SHORTCUTS,
    java_string,
    json_schema_params,
    index_types,
    mcp_server_names,
    shortcut_emit,
    walk,
)
from ..mcp_common import guarded, tool_emit
from ..treesitter import node_text

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


def _schema_params(node: Node | None, source: bytes) -> list[Parameter] | None:
    """Params of a JSON-schema string literal; None when it is not one (unknown)."""
    raw = java_string(node, source)
    return json_schema_params(_unescape(raw) if raw is not None else None)


def _tool_fields(
    node: Node | None, source: bytes, variables: dict[str, Node | None]
) -> tuple[str, str | None, list[Parameter] | None] | None:
    """``(name, description, params)`` of an MCP ``Tool`` expression: ``new McpSchema.Tool(…)``,
    ``McpSchema.Tool.builder()…build()``, or a local variable assigned one of them once."""
    if node is not None and node.type == "identifier":
        node = variables.get(node_text(node, source))
    if node is None:
        return None
    if node.type == "object_creation_expression" and _type_name(node, source) == "Tool":
        args = _args(node)
        name = java_string(args[0], source) if args else None
        if not name:
            return None
        if len(args) == 3:  # Tool(name, description, inputSchema)
            return name, java_string(args[1], source), _schema_params(args[2], source)
        return name, None, None  # other overloads: positions differ by SDK version
    chain = _builder_chain(node, source) if node.type == "method_invocation" else None
    if chain is None or chain[0] != "Tool" or chain[1][-1][0] != "build":
        return None
    calls = {method: args for method, args in chain[1]}
    name_args = calls.get("name") or []
    name = java_string(name_args[0], source) if len(name_args) == 1 else None
    if not name:
        return None
    desc_args = calls.get("description") or []
    description = java_string(desc_args[0], source) if len(desc_args) == 1 else None
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
    for node in walk(root):
        if node.type != "variable_declarator":
            continue
        name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
        if name is None:
            continue
        key = node_text(name, source)
        out[key] = None if key in out else value
    return out


def _sdk_tools(dc: DetectContext) -> list[Emit]:
    """``SyncToolSpecification`` / ``AsyncToolSpecification`` of the MCP Java SDK."""
    source = dc.source
    if b"ToolSpecification" not in source:
        return []
    variables = _local_variables(dc.root, source)
    lines: dict[str, list[int]] = {}
    for f in dc.record.functions:
        lines.setdefault(f.name, []).append(f.startLine)
    methods = {name: ls[0] if len(ls) == 1 else None for name, ls in lines.items()}
    found: list[Emit] = []
    for node in walk(dc.root):
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
                framework=JAVA_MCP,
                handler=handler_name,
                handler_line=handler_line,
                description=description,
                params=params,
                node_type=None,
            )
        )
    return found


def _rooted_at_server(
    node: Node, source: bytes, servers: frozenset[str], cache: dict[tuple[int, int], bool]
) -> bool:
    """True when the call chain ``node`` belongs to starts with ``McpServer.sync(…)`` /
    ``McpServer.async(…)``, ``McpServer`` being one of ``servers`` (what the SDK's class is
    called in the file). ``cache`` holds the answer for every call of a chain already walked,
    so a chain of many ``.tool(…)`` calls is walked once."""
    seen: list[tuple[int, int]] = []
    cur: Node | None = node
    result = False
    while cur is not None and cur.type == "method_invocation":
        key = (cur.start_byte, cur.end_byte)
        if key in cache:
            result = cache[key]
            break
        seen.append(key)
        obj = cur.child_by_field_name("object")
        if obj is None or obj.type != "method_invocation":
            name = cur.child_by_field_name("name")
            result = (
                obj is not None
                and name is not None
                and node_text(name, source) in SERVER_FACTORIES
                and node_text(obj, source) in servers
            )
            break
        cur = obj
    for key in seen:
        cache[key] = result
    return result


def _shortcut_tools(dc: DetectContext) -> list[Emit]:
    """``McpServer.sync(…)….tool(tool, handler)`` / ``.toolCall(tool, handler)`` of the MCP Java
    SDK; ``.tools(spec…)`` is not read (each spec is recorded where it is built)."""
    source = dc.source
    if b"McpServer" not in source or not SHORTCUT_CALL.search(source):
        return []
    servers = mcp_server_names(source, index_types(dc.ctx))
    variables = _local_variables(dc.root, source)
    lines: dict[str, list[int]] = {}
    for f in dc.record.functions:
        lines.setdefault(f.name, []).append(f.startLine)
    methods = {name: ls[0] if len(ls) == 1 else None for name, ls in lines.items()}
    found: list[Emit] = []
    rooted: dict[tuple[int, int], bool] = {}
    for node in walk(dc.root):
        if node.type != "method_invocation":
            continue
        name_node = node.child_by_field_name("name")
        if name_node is None or node_text(name_node, source) not in SHORTCUTS:
            continue
        args = _args(node)
        if len(args) != 2 or not _rooted_at_server(node, source, servers, rooted):
            continue
        fields = _tool_fields(args[0], source, variables)
        if fields is None:
            continue
        found.append(
            shortcut_emit(node, name_node, fields, _sdk_handler(args[1], source, methods), source)
        )
    return found


def _detect(dc: DetectContext) -> Detection | None:
    if b"io.modelcontextprotocol" not in dc.source:
        return None
    found = _sdk_tools(dc) + guarded(dc.path, _shortcut_tools, dc)
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=JAVA_MCP)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="java-mcp",
        gate=Gate("java", order=40, guard=b"io.modelcontextprotocol", skip_fixtures=True),
        detect=_detect,
        frameworks=(JAVA_MCP,),
    )
)
