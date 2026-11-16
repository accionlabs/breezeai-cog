"""Additive detector: tools of a Kotlin MCP server built with the official Kotlin SDK or the
MCP Java SDK.

Official Kotlin SDK (``io.modelcontextprotocol.kotlin.sdk``)::

    server.addTool(name = "calc", description = "Calculator",       → mcp_tool  kotlin-mcp  TOOL  calc
        inputSchema = Tool.Input(properties = buildJsonObject {
            putJsonObject("a") { put("type", "number") } }, required = listOf("a"))) { request -> … }

* Only in a file importing the SDK's server package (``io.modelcontextprotocol.kotlin.sdk.server``):
  an ``addTool`` on anything else is not a tool. ``name`` / ``description`` named or the first
  two positional arguments, or the ``name =`` / ``description =`` of a ``Tool(…)`` passed first.
* ``params`` — none without an ``inputSchema`` (the SDK's empty default); from a
  ``Tool.Input(…)`` / ``ToolSchema(…)`` whose ``properties`` is a ``buildJsonObject { … }`` of
  ``putJsonObject("name") { put("type", …); put("description", …) }`` and whose ``required`` is
  a ``listOf("…")`` literal; any other schema → None (unknown).
* ``addTools(listOf(RegisteredTool(Tool(name = …, …)) { … }, …))`` — one record per
  ``RegisteredTool`` of an inline ``listOf`` / ``mutableListOf`` / ``arrayOf``, read as
  ``addTool(Tool(…))``; a list from a variable or a call → no record.

MCP Java SDK (``io.modelcontextprotocol``) called from Kotlin::

    McpServerFeatures.SyncToolSpecification(                        → mcp_tool  java-mcp  TOOL  calc
        McpSchema.Tool("calc", "Calculator", SCHEMA_JSON)) { exchange, args -> … }
    McpServerFeatures.SyncToolSpecification.builder()               → (Async… and the builders alike)
        .tool(McpSchema.Tool.builder().name("calc").build()).callHandler(::calc).build()

* As in Java (``java_mcp``): the tool is a ``Tool`` constructor or builder, inline or a
  ``val`` / ``var`` declared once in the file; a non-literal name → no record; ``params`` from a
  JSON-schema string literal (``.trimIndent()`` allowed), else None (unknown); a callable
  reference (``::calc``, ``this::calc``) names the handler, a lambda gives only its line.

Import aliases (``import io.modelcontextprotocol.kotlin.sdk.Tool as McpTool``) are followed for
the types above when the aliased class is in an MCP package; an alias of any other class
(``import com.acme.Tool as Tool``) hides the name instead.

Only registrations are read here. A Spring AI ``@McpTool`` / ``@Tool`` function belongs to
``kotlin_springai``.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Parameter
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..jvm_mcp_common import (
    JAVA_MCP,
    json_schema_params,
    kotlin_aliases,
    kotlin_call_args,
    kotlin_canon,
    kotlin_string,
    kotlin_suffix_name,
    kotlin_type_name,
    walk,
)
from ..mcp_common import param, tool_emit
from ..treesitter import node_text

#: ``framework`` of a tool registered through the official Kotlin SDK.
KOTLIN_MCP = "kotlin-mcp"

_KOTLIN_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b"}


def _unescape(text: str) -> str | None:
    """Kotlin string escapes → characters; None for a unicode escape (not read)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1 : i + 2]
        if nxt in ('"', "'", "\\", "$"):
            out.append(nxt)
        elif nxt in _KOTLIN_ESCAPES:
            out.append(_KOTLIN_ESCAPES[nxt])
        else:
            return None
        i += 2
    return "".join(out)


def _literal_text(node: Node | None, source: bytes) -> str | None:
    """A string literal's value: escapes decoded in a ``"…"`` string, none in a raw one."""
    raw = kotlin_string(node, source)
    if raw is None or node is None:
        return None
    return raw if b'"""' in source[node.start_byte : node.end_byte] else _unescape(raw)


def _type_callee(call: Node, source: bytes, aliases: dict[str, str]) -> str:
    """``_callee_name`` with a called simple name resolved through the import aliases (a
    constructor call: ``Tool(…)``, ``AliasOfTool(…)``)."""
    callee = call.named_children[0] if call.named_children else None
    if callee is not None and callee.type == "simple_identifier":
        return kotlin_canon(node_text(callee, source), aliases)
    return _callee_name(call, source)


def _callee_name(call: Node, source: bytes) -> str:
    """Simple name a ``call_expression`` calls (``McpSchema.Tool(…)`` → ``Tool``)."""
    callee = call.named_children[0] if call.named_children else None
    if callee is None:
        return ""
    if callee.type == "simple_identifier":
        return node_text(callee, source)
    if callee.type == "navigation_expression":
        return kotlin_suffix_name(callee, source)
    return ""


def _named(args: list[tuple[str | None, Node]], key: str) -> Node | None:
    return next((v for k, v in args if k == key), None)


def _positional(args: list[tuple[str | None, Node]]) -> list[Node]:
    return [v for k, v in args if k is None]


# ── MCP Java SDK ─────────────────────────────────────────────────────────────


def _builder_chain(
    node: Node, source: bytes, aliases: dict[str, str]
) -> tuple[str, dict[str, list[Node]]] | None:
    """``X.builder().a(1).b(2).build()`` → ``("X", {"builder": [], "a": [1], …, "build": []})``;
    None when ``node`` is not such a chain ending in ``build()``."""
    calls: list[tuple[str, list[Node]]] = []
    cur: Node | None = node
    while cur is not None and cur.type == "call_expression":
        callee = cur.named_children[0] if cur.named_children else None
        if callee is None or callee.type != "navigation_expression":
            return None
        calls.append((kotlin_suffix_name(callee, source), _positional(kotlin_call_args(cur, source))))
        cur = callee.named_children[0]
    if cur is None or len(calls) < 2 or calls[-1][0] != "builder" or calls[0][0] != "build":
        return None
    return kotlin_type_name(cur, source, aliases), dict(reversed(calls))


def _local_variables(root: Node, source: bytes) -> dict[str, Node | None]:
    """``val`` / ``var`` declared once in the file → their initializer (None when declared
    twice)."""
    out: dict[str, Node | None] = {}
    for node in walk(root):
        if node.type != "property_declaration":
            continue
        decl = next((c for c in node.named_children if c.type == "variable_declaration"), None)
        name = (
            next((c for c in decl.named_children if c.type == "simple_identifier"), None)
            if decl is not None
            else None
        )
        if name is None:
            continue
        value: Node | None = None
        after_eq = False
        for c in node.children:
            if c.type == "=":
                after_eq = True
            elif after_eq and c.is_named:
                value = c
                break
        key = node_text(name, source)
        out[key] = None if key in out else value
    return out


def _tool_fields(
    node: Node | None, source: bytes, variables: dict[str, Node | None], aliases: dict[str, str]
) -> tuple[str, str | None, list[Parameter] | None] | None:
    """``(name, description, params)`` of an MCP ``Tool`` expression: ``McpSchema.Tool(…)``,
    ``McpSchema.Tool.builder()…build()``, or a variable assigned one of them once."""
    if node is not None and node.type == "simple_identifier":
        node = variables.get(node_text(node, source))
    if node is None or node.type != "call_expression":
        return None
    chain = _builder_chain(node, source, aliases)
    if chain is None:
        if _type_callee(node, source, aliases) != "Tool":
            return None
        args = kotlin_call_args(node, source)
        pos = _positional(args)
        name = kotlin_string(_named(args, "name") or (pos[0] if pos else None), source)
        if not name:
            return None
        if _named(args, "name") is not None:
            schema = _named(args, "inputSchema")
            desc = kotlin_string(_named(args, "description"), source)
            return (
                name,
                desc,
                json_schema_params(_literal_text(schema, source)) if schema else None,
            )
        if len(pos) == 3:  # Tool(name, description, inputSchema)
            return name, kotlin_string(pos[1], source), json_schema_params(_literal_text(pos[2], source))
        return name, None, None  # other overloads: positions differ by SDK version
    root, calls = chain
    if root != "Tool":
        return None
    name_args = calls.get("name") or []
    name = kotlin_string(name_args[0], source) if len(name_args) == 1 else None
    if not name:
        return None
    desc_args = calls.get("description") or []
    description = kotlin_string(desc_args[0], source) if len(desc_args) == 1 else None
    schema_args = calls.get("inputSchema")
    params = json_schema_params(_literal_text(schema_args[-1], source)) if schema_args else []
    return name, description, params


def _handler(
    node: Node | None, source: bytes, methods: dict[str, int | None]
) -> tuple[str | None, int | None]:
    """A lambda → its line; a callable reference (``::calc``, ``this::calc``) → its name."""
    if node is None:
        return None, None
    if node.type == "lambda_literal":
        return None, node.start_point[0] + 1
    name = ""
    if node.type == "callable_reference":
        ident = [c for c in node.named_children if c.type == "simple_identifier"]
        name = node_text(ident[-1], source) if ident else ""
    elif node.type == "navigation_expression":
        suffix = node.named_children[-1] if node.named_children else None
        if suffix is not None and node_text(suffix, source).startswith("::"):
            name = kotlin_suffix_name(node, source)
    if name:
        return name, methods.get(name)
    return None, None


def _methods(dc: DetectContext) -> dict[str, int | None]:
    lines: dict[str, list[int]] = {}
    for f in dc.record.functions:
        lines.setdefault(f.name, []).append(f.startLine)
    return {name: ls[0] if len(ls) == 1 else None for name, ls in lines.items()}


def _sdk_tools(dc: DetectContext) -> list[Emit]:
    """``SyncToolSpecification`` / ``AsyncToolSpecification`` of the MCP Java SDK."""
    source = dc.source
    if b"io.modelcontextprotocol" not in source or b"ToolSpecification" not in source:
        return []
    variables = _local_variables(dc.root, source)
    methods = _methods(dc)
    aliases = kotlin_aliases(source)
    found: list[Emit] = []
    for node in walk(dc.root):
        if node.type != "call_expression":
            continue
        tool: Node | None = None
        handler: Node | None = None
        chain = _builder_chain(node, source, aliases)
        if chain is not None:
            if not chain[0].endswith("ToolSpecification"):
                continue
            tool_args = chain[1].get("tool") or []
            handler_args = chain[1].get("callHandler") or []
            tool = tool_args[0] if len(tool_args) == 1 else None
            handler = handler_args[0] if len(handler_args) == 1 else None
        elif _type_callee(node, source, aliases).endswith("ToolSpecification"):
            args = kotlin_call_args(node, source)
            pos = _positional(args)
            tool = _named(args, "tool") or (pos[0] if pos else None)
            rest = [v for v in pos if v is not tool and v.type != "null_literal"]
            handler = (
                _named(args, "callHandler") or _named(args, "call") or (rest[-1] if rest else None)
            )
            if handler is None:
                continue
        else:
            continue
        fields = _tool_fields(tool, source, variables, aliases)
        if fields is None:
            continue
        name, description, params = fields
        handler_name, handler_line = _handler(handler, source, methods)
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


# ── Official Kotlin SDK ─────────────────────────────────────


def _string_list(node: Node | None, source: bytes) -> list[str] | None:
    """``listOf("a", "b")`` / ``arrayOf(…)`` of literals; None for anything else."""
    if node is None or node.type != "call_expression":
        return None
    if _callee_name(node, source) not in ("listOf", "arrayOf", "setOf"):
        return None
    out = [kotlin_string(v, source) for v in _positional(kotlin_call_args(node, source))]
    return [v for v in out if v is not None] if all(v is not None for v in out) else None


def _lambda_statements(call: Node, source: bytes) -> list[Node] | None:
    """The statements of a call's trailing lambda (``f { a; b }``); None without one."""
    lam = next(
        (v for k, v in reversed(kotlin_call_args(call, source)) if v.type == "lambda_literal"), None
    )
    if lam is None:
        return None
    body = next((c for c in lam.named_children if c.type == "statements"), None)
    return [
        s
        for s in (body.named_children if body is not None else [])
        if s.type not in ("line_comment", "multiline_comment")
    ]


def _json_properties(node: Node | None, source: bytes) -> dict[str, dict[str, str]] | None:
    """``buildJsonObject { putJsonObject("a") { put("type", "number") } }`` → ``{"a": {"type":
    "number"}}`` (string ``put``s only); None when any statement is something else."""
    if (
        node is None
        or node.type != "call_expression"
        or _callee_name(node, source) != "buildJsonObject"
    ):
        return None
    statements = _lambda_statements(node, source)
    if statements is None:
        return None
    out: dict[str, dict[str, str]] = {}
    for stmt in statements:
        if stmt.type != "call_expression" or _callee_name(stmt, source) != "putJsonObject":
            return None
        pos = [v for v in _positional(kotlin_call_args(stmt, source)) if v.type != "lambda_literal"]
        name = kotlin_string(pos[0], source) if len(pos) == 1 else None
        inner = _lambda_statements(stmt, source)
        if not name or inner is None:
            return None
        spec: dict[str, str] = {}
        for put in inner:
            if put.type == "call_expression" and _callee_name(put, source) == "put":
                kv = _positional(kotlin_call_args(put, source))
                key = kotlin_string(kv[0], source) if len(kv) == 2 else None
                value = _literal_text(kv[1], source) if len(kv) == 2 else None
                if key is not None and value is not None:
                    spec[key] = value
        out[name] = spec
    return out


def _kotlin_schema_params(
    node: Node | None, source: bytes, aliases: dict[str, str]
) -> list[Parameter] | None:
    """Params of a ``Tool.Input(…)`` / ``ToolSchema(…)``; None when it cannot be read whole."""
    if node is None or node.type != "call_expression":
        return None
    if _type_callee(node, source, aliases) not in ("Input", "ToolSchema"):
        return None
    args = kotlin_call_args(node, source)
    pos = _positional(args)
    props_node = _named(args, "properties") or (pos[0] if pos else None)
    required_node = _named(args, "required") or (pos[1] if len(pos) > 1 else None)
    if props_node is None:
        return [] if required_node is None else None
    props = _json_properties(props_node, source)
    required = _string_list(required_node, source) if required_node is not None else []
    if props is None or required is None:
        return None
    return [
        param(
            name, spec.get("type"), required=name in required, description=spec.get("description")
        )
        for name, spec in props.items()
    ]


_LIST_LITERALS = ("listOf", "mutableListOf", "arrayOf")


def _kotlin_sdk_tool(
    node: Node,
    fields: list[tuple[str | None, Node]],
    pos: list[Node],
    handler: Node | None,
    source: bytes,
    methods: dict[str, int | None],
    aliases: dict[str, str],
) -> Emit | None:
    """One Kotlin SDK tool: ``fields`` are ``addTool``'s (or its ``Tool(…)``'s) arguments, ``pos``
    the positional ones (``name``, ``description``, ``inputSchema``)."""
    name = kotlin_string(_named(fields, "name") or (pos[0] if pos else None), source)
    if not name or handler is None:
        return None
    description = kotlin_string(
        _named(fields, "description") or (pos[1] if len(pos) > 1 else None), source
    )
    schema = _named(fields, "inputSchema") or (pos[2] if len(pos) > 2 else None)
    params = _kotlin_schema_params(schema, source, aliases) if schema is not None else []
    handler_name, handler_line = _handler(handler, source, methods)
    return tool_emit(
        node,
        source,
        endpoint=name,
        framework=KOTLIN_MCP,
        handler=handler_name,
        handler_line=handler_line,
        description=description,
        params=params,
        node_type=None,
    )


def _registered_tools(
    node: Node, source: bytes, aliases: dict[str, str]
) -> list[tuple[Node, Node, Node | None]]:
    """``(element, Tool(…) call, handler)`` of each ``RegisteredTool(Tool(…), handler)`` in an
    ``addTools(listOf(…))`` whose list is written inline; nothing for any other list."""
    args = kotlin_call_args(node, source)
    pos = _positional(args)
    lst = _named(args, "toolsToAdd") or (pos[0] if len(pos) == 1 else None)
    callee = lst.named_children[0] if lst is not None and lst.named_children else None
    if (
        lst is None
        or lst.type != "call_expression"
        or callee is None
        or callee.type != "simple_identifier"
        or node_text(callee, source) not in _LIST_LITERALS
    ):
        return []
    out: list[tuple[Node, Node, Node | None]] = []
    for element in _positional(kotlin_call_args(lst, source)):
        if (
            element.type != "call_expression"
            or _type_callee(element, source, aliases) != "RegisteredTool"
        ):
            continue
        el_args = kotlin_call_args(element, source)
        el_pos = _positional(el_args)  # a trailing lambda is the last one
        tool = _named(el_args, "tool") or (el_pos[0] if el_pos else None)
        handler = _named(el_args, "handler") or (el_pos[1] if len(el_pos) > 1 else None)
        if (
            tool is not None
            and tool.type == "call_expression"
            and _type_callee(tool, source, aliases) == "Tool"
        ):
            out.append((element, tool, handler))
    return out


def _kotlin_sdk_tools(dc: DetectContext) -> list[Emit]:
    """``addTool(name, description, inputSchema) { … }`` / ``addTools(listOf(RegisteredTool(…)))``
    of the official Kotlin SDK."""
    source = dc.source
    if b"io.modelcontextprotocol.kotlin.sdk.server" not in source or b"addTool" not in source:
        return []
    methods = _methods(dc)
    aliases = kotlin_aliases(source)
    found: list[Emit] = []
    for node in walk(dc.root):
        if node.type != "call_expression":
            continue
        method = _callee_name(node, source)
        emits: list[Emit | None] = []
        if method == "addTool":
            args = kotlin_call_args(node, source)
            pos = [v for v in _positional(args) if v.type != "lambda_literal"]
            lam = next((v for k, v in args if k is None and v.type == "lambda_literal"), None)
            handler = _named(args, "handler") or lam
            fields = args
            first = _named(args, "tool") or (pos[0] if pos else None)
            if (
                first is not None
                and first.type == "call_expression"
                and _type_callee(first, source, aliases) == "Tool"
            ):
                fields, pos = kotlin_call_args(first, source), []  # addTool(Tool(name = …, …)) { … }
            emits.append(_kotlin_sdk_tool(node, fields, pos, handler, source, methods, aliases))
        elif method == "addTools":
            for element, tool, handler in _registered_tools(node, source, aliases):
                emits.append(
                    _kotlin_sdk_tool(
                        element, kotlin_call_args(tool, source), [], handler, source, methods, aliases
                    )
                )
        found.extend(e for e in emits if e is not None)
    return found


def _detect(dc: DetectContext) -> Detection | None:
    found = _sdk_tools(dc) + _kotlin_sdk_tools(dc)
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=found[0].fields.get("framework") or KOTLIN_MCP)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="kotlin-mcp",
        gate=Gate("kotlin", order=40, guard=b"Tool", skip_fixtures=True),
        detect=_detect,
        frameworks=(JAVA_MCP, KOTLIN_MCP),
    )
)
