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
  ``addTool(Tool(…))``; also such a list in the innermost ``val`` visible at the call
  (``mutableListOf`` only when the name has no other use), or returned by a same-file
  parameterless function whose body is that list alone. Anything else → no record; a list
  passed twice gives one record per element.

MCP Java SDK (``io.modelcontextprotocol``) called from Kotlin::

    McpServerFeatures.SyncToolSpecification(                        → mcp_tool  java-mcp  TOOL  calc
        McpSchema.Tool("calc", "Calculator", SCHEMA_JSON)) { exchange, args -> … }
    McpServerFeatures.SyncToolSpecification.builder()               → (Async… and the builders alike)
        .tool(McpSchema.Tool.builder().name("calc").build()).callHandler(::calc).build()

* As in Java (``java_mcp``): the tool is a ``Tool`` constructor or builder, inline or a
  ``val`` / ``var`` declared once in the file; a non-literal name → no record; ``params`` from a
  JSON-schema string literal (``.trimIndent()`` allowed), else None (unknown); a callable
  reference (``::calc``, ``this::calc``) names the handler, a lambda gives only its line.
* The server builder's shortcut ``McpServer.sync(…)….tool(tool) { … }`` / ``.toolCall(tool,
  handler)`` (also ``async``) is read the same way, only on a chain that starts at
  ``McpServer.sync(`` / ``.async(``; ``.tools(spec…)`` is not read.

Resources and prompts, of either SDK::

    server.addResource(uri = "docs://readme", name = "README",     → mcp_resource  kotlin-mcp  RESOURCE  docs://readme
        description = "The README", mimeType = "text/markdown") { request -> … }
    server.addResourceTemplate(uriTemplate = "users://{id}", name = "User") { request, vars -> … }
                                                                    → mcp_resource  …  users://{id}  params id
    server.addPrompt(name = "review", description = "Review code",  → mcp_prompt  kotlin-mcp  PROMPT  review
        arguments = listOf(PromptArgument(name = "code", description = "The code", required = true))) { … }
    McpServerFeatures.SyncPromptSpecification(McpSchema.Prompt("review", "Review", listOf(…))) { … }
                                                                    → mcp_prompt  java-mcp  PROMPT  review

* Official Kotlin SDK — ``addResource(uri, name, description, mimeType)``,
  ``addResourceTemplate(uriTemplate, name, description, mimeType)`` (or a ``ResourceTemplate(…)``
  passed first), ``addPrompt(name, description, arguments)`` (or a ``Prompt(…)`` passed first),
  named or positional, with a trailing lambda (or ``readHandler =`` / ``promptProvider =``) as the
  handler, only in a file importing the SDK's server package.
* Java SDK — the ``Sync…`` / ``Async…`` ``ResourceSpecification`` /
  ``ResourceTemplateSpecification`` / ``PromptSpecification``, read as in Java (``java_mcp``).
* ``endpoint`` — the URI / URI template / prompt name, a literal; else no record. ``params`` —
  a template's simple ``{variables}`` (None with an RFC 6570 operator); ``[]`` for a fixed
  resource; a prompt's ``listOf(PromptArgument(…))``, ``[]`` without, None when not a literal.

Import aliases (``import io.modelcontextprotocol.kotlin.sdk.Tool as McpTool``) are followed for
the types above when the aliased class is in an MCP package; an alias of any other class
(``import com.acme.Tool as Tool``) hides the name instead.

Only registrations are read here. A Spring AI ``@McpTool`` / ``@Tool`` function belongs to
``kotlin_springai``.
"""

from __future__ import annotations

from collections.abc import Callable

from tree_sitter import Node

from ...schemas import Parameter
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..jvm_mcp_common import (
    JAVA_MCP,
    SERVER_FACTORIES,
    SHORTCUT_CALL,
    SHORTCUTS,
    json_schema_params,
    kotlin_aliases,
    kotlin_call_args,
    kotlin_canon,
    kotlin_string,
    kotlin_suffix_name,
    kotlin_type_name,
    shortcut_emit,
    walk,
)
from ..mcp_common import PROMPT, RESOURCE, mcp_emit, param, tool_emit, uri_template_params
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
        calls.append(
            (kotlin_suffix_name(callee, source), _positional(kotlin_call_args(cur, source)))
        )
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
            return (
                name,
                kotlin_string(pos[1], source),
                json_schema_params(_literal_text(pos[2], source)),
            )
        return name, None, None  # other overloads: positions differ by SDK version
    root, calls = chain
    if root != "Tool":
        return None
    name_args = calls.get("name") or []
    # `Tool.builder("name"[, schema])` (SDK 1.0 and later) names the tool up front.
    builder_args = calls.get("builder") or []
    if name_args:
        name = kotlin_string(name_args[0], source) if len(name_args) == 1 else None
    else:
        name = kotlin_string(builder_args[0], source) if 1 <= len(builder_args) <= 3 else None
    if not name:
        return None
    desc_args = calls.get("description") or []
    description = kotlin_string(desc_args[0], source) if len(desc_args) == 1 else None
    schema_args = calls.get("inputSchema")
    params: list[Parameter] | None = []
    if schema_args:
        params = json_schema_params(_literal_text(schema_args[-1], source))
    elif len(builder_args) == 3:
        params = json_schema_params(_literal_text(builder_args[2], source))
    elif len(builder_args) == 2:
        params = None  # a schema map built in code: unknown
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


def _rooted_at_server(
    node: Node, source: bytes, aliases: dict[str, str], cache: dict[tuple[int, int], bool]
) -> bool:
    """True when the call chain ``node`` belongs to starts with ``McpServer.sync(…)`` /
    ``McpServer.async(…)``. ``cache`` holds the answer for every call of a chain already
    walked, so a chain of many ``.tool(…)`` calls is walked once."""
    seen: list[tuple[int, int]] = []
    cur: Node | None = node
    result = False
    while cur is not None and cur.type == "call_expression":
        key = (cur.start_byte, cur.end_byte)
        if key in cache:
            result = cache[key]
            break
        seen.append(key)
        callee = cur.named_children[0] if cur.named_children else None
        if callee is None or callee.type != "navigation_expression":
            break
        receiver = callee.named_children[0] if callee.named_children else None
        if receiver is not None and receiver.type == "call_expression":
            cur = receiver
            continue
        result = (
            receiver is not None
            and kotlin_suffix_name(callee, source) in SERVER_FACTORIES
            and (
                kotlin_type_name(receiver, source, aliases) == "McpServer"
                if receiver.type == "simple_identifier"
                else node_text(receiver, source) == "io.modelcontextprotocol.server.McpServer"
            )
        )
        break
    for key in seen:
        cache[key] = result
    return result


def _shortcut_tools(dc: DetectContext) -> list[Emit]:
    """``McpServer.sync(…)….tool(tool) { … }`` / ``.toolCall(tool, handler)`` of the MCP Java
    SDK; ``.tools(spec…)`` is not read (each spec is recorded where it is built)."""
    source = dc.source
    if (
        b"io.modelcontextprotocol" not in source
        or b"McpServer" not in source
        or not SHORTCUT_CALL.search(source)
    ):
        return []
    variables = _local_variables(dc.root, source)
    methods = _methods(dc)
    aliases = kotlin_aliases(source)
    found: list[Emit] = []
    rooted: dict[tuple[int, int], bool] = {}
    for node in walk(dc.root):
        if node.type != "call_expression":
            continue
        callee = node.named_children[0] if node.named_children else None
        if callee is None or callee.type != "navigation_expression":
            continue
        if kotlin_suffix_name(callee, source) not in SHORTCUTS:
            continue
        args = kotlin_call_args(node, source)
        pos = _positional(args)
        if len(pos) != 2 or len(args) != 2 or not _rooted_at_server(node, source, aliases, rooted):
            continue
        fields = _tool_fields(pos[0], source, variables, aliases)
        if fields is None:
            continue
        suffix = callee.named_children[-1]
        name_node = next(
            (c for c in suffix.named_children if c.type == "simple_identifier"), suffix
        )
        found.append(
            shortcut_emit(node, name_node, fields, _handler(pos[1], source, methods), source)
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


def _value_of(decl: Node) -> Node | None:
    """The initializer of a ``property_declaration`` (``val x = <value>``)."""
    after_eq = False
    for c in decl.children:
        if c.type == "=":
            after_eq = True
        elif after_eq and c.is_named:
            return c
    return None


def _declared_name(decl: Node, source: bytes) -> str | None:
    var = next((c for c in decl.named_children if c.type == "variable_declaration"), None)
    ident = (
        next((c for c in var.named_children if c.type == "simple_identifier"), None)
        if var is not None
        else None
    )
    return node_text(ident, source) if ident is not None else None


def _binds_param(node: Node, name: str, source: bytes) -> bool:
    """True when the function or lambda ``node`` has a parameter called ``name``."""
    for c in node.named_children:
        if c.type in ("function_value_parameters", "lambda_parameters"):
            for p in walk(c):
                if p.type in ("parameter", "variable_declaration"):
                    ident = next(
                        (x for x in p.named_children if x.type == "simple_identifier"), None
                    )
                    if ident is not None and node_text(ident, source) == name:
                        return True
    return False


class _Names:
    """The file's declarations and uses by name, built once (on the first ``addTools`` whose list
    is not written inline): ``val`` / ``var`` declarations, functions and identifier uses."""

    def __init__(self, root: Node, source: bytes) -> None:
        self.props: dict[str, list[Node]] = {}
        self.funcs: dict[str, list[Node]] = {}
        self.uses: dict[str, list[Node]] = {}
        for n in walk(root):
            if n.type == "property_declaration":
                name = _declared_name(n, source)
                if name is not None:
                    self.props.setdefault(name, []).append(n)
            elif n.type == "function_declaration":
                ident = next((c for c in n.named_children if c.type == "simple_identifier"), None)
                if ident is not None:
                    self.funcs.setdefault(node_text(ident, source), []).append(n)
            elif n.type == "simple_identifier":
                self.uses.setdefault(node_text(n, source), []).append(n)


def _contains(outer: Node, inner: Node) -> bool:
    return outer.start_byte <= inner.start_byte and inner.end_byte <= outer.end_byte


def _list_declaration(use: Node, name: str, source: bytes, names: _Names) -> Node | None:
    """The innermost declaration of ``name`` visible at ``use``: a local declared before it in
    an enclosing block, a property of an enclosing class, or a top-level one. None when the
    innermost binding is a parameter, or nothing declares it."""
    visible: list[Node] = []
    for decl in names.props.get(name, []):
        container = decl.parent
        if container is None or container.type not in ("statements", "class_body", "source_file"):
            continue
        if not _contains(container, use):
            continue
        if container.type == "statements" and decl.start_byte >= use.start_byte:
            continue  # a local is visible only after its declaration
        visible.append(decl)
    # The containers holding the use are nested: the smallest is the innermost; in one block
    # the latest declaration before the use.
    best = min(
        visible,
        key=lambda d: (d.parent.end_byte - d.parent.start_byte, -d.start_byte),  # type: ignore[union-attr]
        default=None,
    )
    node = use.parent
    while node is not None and (best is None or node != best.parent):
        if node.type in ("function_declaration", "lambda_literal", "anonymous_function") and (
            _binds_param(node, name, source)
        ):
            return None  # a parameter of an enclosing function hides it
        node = node.parent
    return best


def _only_passed_to_add_tools(name: str, decl: Node, source: bytes, names: _Names) -> bool:
    """True when every use of ``name`` in the file is the sole argument of an ``addTools`` call
    (a mutable list nothing adds to, removes from or clears)."""
    for n in names.uses.get(name, []):
        if _contains(decl, n):
            continue  # the declaration itself
        arg = n.parent
        args = arg.parent if arg is not None and arg.type == "value_argument" else None
        call = args.parent.parent if args is not None and args.parent is not None else None
        if (
            args is None
            or call is None
            or call.type != "call_expression"
            or _callee_name(call, source) != "addTools"
            or len(args.named_children) != 1
            or arg is None
            or len(arg.named_children) != 1
        ):
            return False
    return True


def _function_list(call: Node, source: bytes, names: _Names) -> Node | None:
    """``build()`` → the list a same-file ``fun build() = listOf(…)`` / ``fun build() { return
    listOf(…) }`` returns (a single expression or ``return``); None for anything else."""
    callee = call.named_children[0] if call.named_children else None
    if callee is None or callee.type != "simple_identifier" or kotlin_call_args(call, source):
        return None
    decls = names.funcs.get(node_text(callee, source), [])
    if len(decls) != 1:
        return None
    params = next(
        (c for c in decls[0].named_children if c.type == "function_value_parameters"), None
    )
    body = next((c for c in decls[0].named_children if c.type == "function_body"), None)
    if body is None or (params is not None and params.named_children):
        return None
    inner = [c for c in body.named_children if "comment" not in c.type]
    if len(inner) == 1 and inner[0].type == "statements":
        stmts = [c for c in inner[0].named_children if "comment" not in c.type]
        if len(stmts) != 1 or stmts[0].type != "jump_expression":
            return None
        if not node_text(stmts[0], source).startswith("return"):
            return None
        inner = [c for c in stmts[0].named_children if "comment" not in c.type]
    return inner[0] if len(inner) == 1 else None


def _tools_list(lst: Node | None, source: bytes, names: Callable[[], _Names]) -> Node | None:
    """The ``listOf(…)`` / ``arrayOf(…)`` (or an unchanged ``mutableListOf(…)``) an ``addTools``
    argument stands for: written inline, a ``val`` of it, or a same-file function returning it."""
    if lst is not None and lst.type == "simple_identifier":
        name = node_text(lst, source)
        decl = _list_declaration(lst, name, source, names())
        kind = (
            next((c for c in decl.named_children if c.type == "binding_pattern_kind"), None)
            if decl is not None
            else None
        )
        if decl is None or kind is None or node_text(kind, source) != "val":
            return None
        value = _value_of(decl)
        if value is None or value.type != "call_expression":
            return None
        if _callee_name(value, source) == "mutableListOf" and not _only_passed_to_add_tools(
            name, decl, source, names()
        ):
            return None
        return value
    if lst is not None and lst.type == "call_expression":
        callee = lst.named_children[0] if lst.named_children else None
        if (
            callee is not None
            and callee.type == "simple_identifier"
            and node_text(callee, source) not in _LIST_LITERALS
        ):
            built = _function_list(lst, source, names())
            if built is None or built.type != "call_expression":
                return None
            callee = built.named_children[0] if built.named_children else None
            if (
                callee is None
                or callee.type != "simple_identifier"
                or node_text(callee, source) not in ("listOf", "arrayOf")
            ):
                return None
            return built
    return lst


def _registered_tools(
    node: Node, source: bytes, aliases: dict[str, str], names: Callable[[], _Names]
) -> list[tuple[Node, Node, Node | None]]:
    """``(element, Tool(…) call, handler)`` of each ``RegisteredTool(Tool(…), handler)`` in an
    ``addTools(…)`` whose list is written inline, a ``val`` holding one, or a same-file function
    returning one (:func:`_tools_list`); nothing for any other list."""
    args = kotlin_call_args(node, source)
    pos = _positional(args)
    lst = _tools_list(
        _named(args, "toolsToAdd") or (pos[0] if len(pos) == 1 else None), source, names
    )
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
    registered: set[int] = set()
    index: list[_Names] = []

    def names() -> _Names:
        if not index:
            index.append(_Names(dc.root, source))
        return index[0]

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
                fields, pos = (
                    kotlin_call_args(first, source),
                    [],
                )  # addTool(Tool(name = …, …)) { … }
            emits.append(_kotlin_sdk_tool(node, fields, pos, handler, source, methods, aliases))
        elif method == "addTools":
            for element, tool, handler in _registered_tools(node, source, aliases, names):
                if element.start_byte in registered:
                    continue  # one list passed to `addTools` twice: one record per element
                registered.add(element.start_byte)
                emits.append(
                    _kotlin_sdk_tool(
                        element,
                        kotlin_call_args(tool, source),
                        [],
                        handler,
                        source,
                        methods,
                        aliases,
                    )
                )
        found.extend(e for e in emits if e is not None)
    return found


# ── Resources and prompts ────────────────────────────────────────────────────

#: Java SDK ``Sync…`` / ``Async…Specification`` → the record type it registers.
_SPECIFICATIONS = {
    f"{mode}{entry}Specification": entry
    for mode in ("Sync", "Async")
    for entry in ("Resource", "ResourceTemplate", "Prompt")
}
#: Each entry type → (its kind, the argument / builder method naming it).
_ENTRIES = {
    "Resource": (RESOURCE, "uri"),
    "ResourceTemplate": (RESOURCE, "uriTemplate"),
    "Prompt": (PROMPT, "name"),
}
_KOTLIN_ADDERS = {
    "addResource": "Resource",
    "addResourceTemplate": "ResourceTemplate",
    "addPrompt": "Prompt",
}


def _prompt_argument(node: Node, source: bytes, aliases: dict[str, str]) -> Parameter | None:
    """``PromptArgument(name = …, description = …, required = …)`` (named, or positional in
    the official Kotlin SDK's order ``name, description, required``), or the Java SDK's
    ``PromptArgument.builder("name")…build()``; None when not read whole."""
    if node.type != "call_expression":
        return None
    chain = _builder_chain(node, source, aliases)
    if chain is not None:
        root, calls = chain
        builder_args = calls.get("builder") or []
        if root != "PromptArgument" or len(builder_args) != 1:
            return None
        if set(calls) - {"builder", "title", "description", "required", "build"}:
            return None
        name_node: Node | None = builder_args[0]
        desc_node = (calls.get("description") or [None])[0]
        req_node = (calls.get("required") or [None])[0]
    elif _type_callee(node, source, aliases) == "PromptArgument":
        args = kotlin_call_args(node, source)
        pos = _positional(args)
        if any(k is None for k, _ in args) and any(k is not None for k, _ in args):
            return None  # mixed: positions differ between the two SDKs
        if pos and len(pos) != 3:
            return None  # Java SDK (name, description, required); 4 = with title (ambiguous)
        name_node = _named(args, "name") or (pos[0] if pos else None)
        desc_node = _named(args, "description") or (pos[1] if pos else None)
        req_node = _named(args, "required") or (pos[2] if pos else None)
    else:
        return None
    name = kotlin_string(name_node, source)
    description = kotlin_string(desc_node, source)
    required = node_text(req_node, source) if req_node is not None else "null"
    if not name or required not in ("true", "false", "null"):
        return None
    if desc_node is not None and desc_node.type != "null_literal" and description is None:
        return None
    return param(name, None, required=required == "true", description=description)


def _prompt_arguments(
    node: Node | None, source: bytes, aliases: dict[str, str]
) -> list[Parameter] | None:
    """``listOf(PromptArgument(…), …)`` (``List.of`` / ``arrayOf``); ``[]`` for none or
    ``null``; None for anything else."""
    if node is None or node.type == "null_literal":
        return []
    if node.type != "call_expression" or _callee_name(node, source) not in (
        "listOf",
        "of",
        "arrayOf",
        "asList",
    ):
        return None
    out: list[Parameter] = []
    for arg in _positional(kotlin_call_args(node, source)):
        p = _prompt_argument(arg, source, aliases)
        if p is None:
            return None
        out.append(p)
    return out


def _entry_result(
    entry: str,
    endpoint: str | None,
    description: str | None,
    arguments: Node | None,
    source: bytes,
    aliases: dict[str, str],
) -> tuple[str, str | None, list[Parameter] | None] | None:
    if not endpoint:
        return None
    if entry == "Prompt":
        return endpoint, description, _prompt_arguments(arguments, source, aliases)
    # A fixed resource's URI is literal, braces and all: it has no variables.
    return endpoint, description, uri_template_params(endpoint) if entry != "Resource" else []


def _named_entry(
    args: list[tuple[str | None, Node]], entry: str, source: bytes, aliases: dict[str, str]
) -> tuple[str, str | None, list[Parameter] | None] | None:
    """The official Kotlin SDK's order: ``Resource(uri, name, description, mimeType)``,
    ``ResourceTemplate(uriTemplate, name, description, mimeType)``, ``Prompt(name, description,
    arguments)`` — named arguments, or positional when none is named."""
    key = _ENTRIES[entry][1]
    pos = [v for k, v in args if k is None and v.type != "lambda_literal"]
    named = any(k is not None for k, _ in args)
    endpoint = kotlin_string(_named(args, key) or (pos[0] if pos and not named else None), source)
    desc_index = 1 if entry == "Prompt" else 2
    desc_node = _named(args, "description") or (
        pos[desc_index] if len(pos) > desc_index and not named else None
    )
    arguments = _named(args, "arguments") or (pos[2] if len(pos) > 2 and not named else None)
    return _entry_result(
        entry, endpoint, kotlin_string(desc_node, source), arguments, source, aliases
    )


def _java_entry_fields(
    node: Node | None,
    source: bytes,
    variables: dict[str, Node | None],
    entry: str,
    aliases: dict[str, str],
) -> tuple[str, str | None, list[Parameter] | None] | None:
    """A Java SDK ``Resource`` / ``ResourceTemplate`` / ``Prompt`` built in Kotlin: a
    constructor (positions as in Java), a builder, or a ``val`` assigned one once."""
    if node is not None and node.type == "simple_identifier":
        node = variables.get(node_text(node, source))
    if node is None or node.type != "call_expression":
        return None
    key = _ENTRIES[entry][1]
    chain = _builder_chain(node, source, aliases)
    description: str | None = None
    arguments: Node | None = None
    if chain is not None:
        root, calls = chain
        if root != entry:
            return None
        builder_args = calls.get("builder") or []
        key_args = calls.get(key) or builder_args[:1]
        endpoint = kotlin_string(key_args[0], source) if len(key_args) == 1 else None
        desc_args = calls.get("description") or []
        description = kotlin_string(desc_args[0], source) if len(desc_args) == 1 else None
        if entry == "Prompt" and "arguments" in calls:
            if len(calls["arguments"]) != 1:
                return None
            arguments = calls["arguments"][0]
        return _entry_result(entry, endpoint, description, arguments, source, aliases)
    if _type_callee(node, source, aliases) != entry:
        return None
    args = kotlin_call_args(node, source)
    if any(k is not None for k, _ in args):
        return None  # Java records take no named arguments
    pos = _positional(args)
    endpoint = kotlin_string(pos[0], source) if pos else None
    if entry == "Prompt":
        if len(pos) == 3:
            description, arguments = kotlin_string(pos[1], source), pos[2]
        elif len(pos) in (4, 5, 6):
            description, arguments = kotlin_string(pos[2], source), pos[3]
        else:
            return None
    elif entry == "ResourceTemplate" and len(pos) == 5:
        description = kotlin_string(pos[2], source)
    elif len(pos) >= 7:
        description = kotlin_string(pos[3], source)
    return _entry_result(entry, endpoint, description, arguments, source, aliases)


def _entries(dc: DetectContext) -> list[Emit]:
    """Resources and prompts: the Java SDK's specifications, and the official Kotlin SDK's
    ``addResource`` / ``addResourceTemplate`` / ``addPrompt``."""
    source = dc.source
    java = b"Specification" in source
    kotlin_sdk = b"io.modelcontextprotocol.kotlin.sdk.server" in source and (
        b"addResource" in source or b"addPrompt" in source
    )
    if not java and not kotlin_sdk:
        return []
    variables = _local_variables(dc.root, source) if java else {}
    methods = _methods(dc)
    aliases = kotlin_aliases(source)
    found: list[Emit] = []
    for node in walk(dc.root):
        if node.type != "call_expression":
            continue
        fields: tuple[str, str | None, list[Parameter] | None] | None = None
        handler: Node | None = None
        framework = JAVA_MCP
        entry = _SPECIFICATIONS.get(_type_callee(node, source, aliases)) if java else None
        if entry is not None:
            args = kotlin_call_args(node, source)
            pos = _positional(args)
            first = pos[0] if pos else None
            rest = [v for v in pos[1:] if v.type != "null_literal"]
            handler = rest[-1] if rest else None
            fields = _java_entry_fields(first, source, variables, entry, aliases)
        elif kotlin_sdk and _callee_name(node, source) in _KOTLIN_ADDERS:
            entry = _KOTLIN_ADDERS[_callee_name(node, source)]
            framework = KOTLIN_MCP
            args = kotlin_call_args(node, source)
            handler = next(
                (v for k, v in reversed(args) if k is None and v.type == "lambda_literal"), None
            ) or _named(args, "readHandler") or _named(args, "promptProvider")
            pos = [v for k, v in args if k is None and v.type != "lambda_literal"]
            first = pos[0] if len(pos) == 1 and len(args) <= 2 else None
            if (
                first is not None
                and first.type == "call_expression"
                and _type_callee(first, source, aliases) == entry
            ):  # addPrompt(Prompt(…)) { … }
                fields = _named_entry(kotlin_call_args(first, source), entry, source, aliases)
            elif entry != "ResourceTemplate" or _named(args, "uriTemplate") is not None or (
                len(pos) >= 2
            ):
                fields = _named_entry(args, entry, source, aliases)
            if handler is None:
                continue
        if fields is None:
            continue
        endpoint, description, params = fields
        handler_name, handler_line = _handler(handler, source, methods)
        found.append(
            mcp_emit(
                node,
                source,
                kind=_ENTRIES[entry][0],
                endpoint=endpoint,
                framework=framework,
                handler=handler_name,
                handler_line=handler_line,
                description=description,
                params=params,
                node_type=None,
            )
        )
    return found


def _detect(dc: DetectContext) -> Detection | None:
    found = _sdk_tools(dc) + _shortcut_tools(dc) + _kotlin_sdk_tools(dc) + _entries(dc)
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=found[0].fields.get("framework") or KOTLIN_MCP)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="kotlin-mcp",
        gate=Gate("kotlin", order=40, guard=b"io.modelcontextprotocol", skip_fixtures=True),
        detect=_detect,
        frameworks=(JAVA_MCP, KOTLIN_MCP),
    )
)
