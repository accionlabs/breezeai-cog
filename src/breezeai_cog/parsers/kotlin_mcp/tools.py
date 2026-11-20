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
  reference (``::calc``, ``this::calc``) names the handler, a lambda gives only its line. The
  handler's line: for ``::calc`` the file's one top-level ``fun calc`` (none when ``calc`` is
  imported, or a class or block around the reference declares its own ``calc``), for
  ``this::calc`` the one ``calc`` of the class or object around it (none inside a lambda or an
  object literal, whose ``this`` may be another); ``Type::calc`` and ``expr::calc`` give none.
* The server builder's shortcut ``McpServer.sync(…)….tool(tool) { … }`` / ``.toolCall(tool,
  handler)`` (also ``async``) is read the same way, only on a chain that starts at
  ``McpServer.sync(`` / ``.async(`` with ``McpServer`` imported from
  ``io.modelcontextprotocol.server`` (by name, ``.*`` or an ``as`` alias, and no other
  ``McpServer`` imported; by ``.*`` only when the repository index shows no ``McpServer`` in
  the file's own package) or written fully qualified; ``.tools(spec…)`` is not read.

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

import bisect
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
    index_types,
    mcp_server_names,
    shortcut_emit,
    walk,
)
from ..mcp_common import (
    PROMPT,
    RESOURCE,
    guarded,
    mcp_emit,
    param,
    tool_emit,
    uri_template_params,
)
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


_CLASS_SCOPES = ("class_declaration", "object_declaration", "companion_object")


def _fun_name(decl: Node, source: bytes) -> str | None:
    """The name of a ``function_declaration`` that is not an extension (``fun String.calc()``
    is never what a bare ``::calc`` or ``this::calc`` reaches)."""
    if decl.child_by_field_name("receiver") is not None:
        return None
    ident = next((c for c in decl.named_children if c.type == "simple_identifier"), None)
    return node_text(ident, source) if ident is not None else None


def _declared_funs(holder: Node, source: bytes) -> dict[str, list[Node]]:
    """Name → the functions of that name declared directly in ``holder`` (a file, a class body,
    a block), extensions included (a same-named extension makes the name ambiguous)."""
    out: dict[str, list[Node]] = {}
    for c in holder.named_children:
        if c.type != "function_declaration":
            continue
        ident = next((x for x in c.named_children if x.type == "simple_identifier"), None)
        if ident is not None:
            out.setdefault(node_text(ident, source), []).append(c)
    return out


class _HandlerSites:
    """Where a callable reference passed as a handler is declared: a bare ``::calc`` is a
    top-level function of the file (one of the name, not an extension, and ``calc`` not
    imported), unless a class or block around the reference declares a ``calc`` of its own;
    ``this::calc`` is a method of the class or object around it. Anything else gives no line."""

    def __init__(self, root: Node, source: bytes) -> None:
        self.root = root
        self.source = source
        self.imported: set[str] = set()
        #: A holder (by its span) → its functions by name, read once per holder.
        self._funs: dict[tuple[int, int], dict[str, list[Node]]] = {}
        headers = [
            h
            for top in root.named_children
            for h in (top.named_children if top.type == "import_list" else [top])
            if h.type == "import_header"
        ]
        for header in headers:
            alias = next((c for c in header.named_children if c.type == "import_alias"), None)
            path = next((c for c in header.named_children if c.type == "identifier"), None)
            if alias is not None:
                self.imported.add(node_text(alias, source).split()[-1])
            elif path is not None and not any(
                c.type == "wildcard_import" for c in header.named_children
            ):
                self.imported.add(node_text(path, source).rsplit(".", 1)[-1].strip())

    def _declared(self, holder: Node, name: str) -> list[Node]:
        key = (holder.start_byte, holder.end_byte)
        funs = self._funs.get(key)
        if funs is None:
            funs = self._funs[key] = _declared_funs(holder, self.source)
        return funs.get(name, [])

    def top_level(self, ref: Node, name: str) -> int | None:
        if name in self.imported:
            return None  # the imported function, declared elsewhere
        node = ref.parent
        while node is not None and node.type != "source_file":
            if node.type in ("class_body", "statements") and self._declared(node, name):
                return None  # a member or local function of the name is nearer
            node = node.parent
        found = self._declared(self.root, name)
        if len(found) != 1 or _fun_name(found[0], self.source) != name:
            return None
        return found[0].start_point[0] + 1

    def own_method(self, ref: Node, name: str) -> int | None:
        node = ref.parent
        while node is not None and node.type not in _CLASS_SCOPES:
            if node.type in ("lambda_literal", "anonymous_function", "object_literal"):
                return None  # `this` may be the lambda's receiver or the object literal
            node = node.parent
        if node is None:
            return None
        body = next((c for c in node.named_children if c.type == "class_body"), None)
        found = self._declared(body, name) if body is not None else []
        if len(found) != 1 or _fun_name(found[0], self.source) != name:
            return None
        return found[0].start_point[0] + 1


def _handler(
    node: Node | None, source: bytes, sites: _HandlerSites
) -> tuple[str | None, int | None]:
    """A lambda → its line; a callable reference (``::calc``, ``this::calc``) → its name, and the
    line :class:`_HandlerSites` finds for it; ``Type::calc`` / ``expr::calc`` → the name only."""
    if node is None:
        return None, None
    if node.type == "lambda_literal":
        return None, node.start_point[0] + 1
    if node.type == "callable_reference":
        ident = [c for c in node.named_children if c.type == "simple_identifier"]
        name = node_text(ident[-1], source) if ident else ""
        if not name:
            return None, None
        if len(node.named_children) != 1:
            return name, None  # `Type::calc`
        return name, sites.top_level(node, name)
    if node.type == "navigation_expression":
        suffix = node.named_children[-1] if node.named_children else None
        if suffix is None or not node_text(suffix, source).startswith("::"):
            return None, None
        name = kotlin_suffix_name(node, source)
        if not name:
            return None, None
        receiver = node.named_children[0]
        if receiver.type == "this_expression" and not receiver.named_children:
            return name, sites.own_method(node, name)
        return name, None  # `this@Outer::calc`, `expr::calc`
    return None, None


def _sdk_tools(dc: DetectContext) -> list[Emit]:
    """``SyncToolSpecification`` / ``AsyncToolSpecification`` of the MCP Java SDK."""
    source = dc.source
    if b"io.modelcontextprotocol" not in source or b"ToolSpecification" not in source:
        return []
    variables = _local_variables(dc.root, source)
    sites = _HandlerSites(dc.root, source)
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
        handler_name, handler_line = _handler(handler, source, sites)
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
            and node_text(receiver, source) in servers
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
    sites = _HandlerSites(dc.root, source)
    aliases = kotlin_aliases(source)
    servers = mcp_server_names(source, index_types(dc.ctx))
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
        if len(pos) != 2 or len(args) != 2 or not _rooted_at_server(node, source, servers, rooted):
            continue
        fields = _tool_fields(pos[0], source, variables, aliases)
        if fields is None:
            continue
        suffix = callee.named_children[-1]
        name_node = next(
            (c for c in suffix.named_children if c.type == "simple_identifier"), suffix
        )
        found.append(
            shortcut_emit(node, name_node, fields, _handler(pos[1], source, sites), source)
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
    sites: _HandlerSites,
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
    handler_name, handler_line = _handler(handler, source, sites)
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


def _declared_names(decl: Node, source: bytes) -> list[str]:
    """The names a ``property_declaration`` declares: ``val x = …`` one, a destructuring
    ``val (a, b) = …`` each of its components."""
    out: list[str] = []
    for c in decl.named_children:
        if c.type == "variable_declaration":
            vars_ = [c]
        elif c.type == "multi_variable_declaration":
            vars_ = [v for v in c.named_children if v.type == "variable_declaration"]
        else:
            continue
        for var in vars_:
            ident = next((x for x in var.named_children if x.type == "simple_identifier"), None)
            if ident is not None:
                out.append(node_text(ident, source))
    return out


def _destructures(decl: Node) -> bool:
    """True for ``val (a, b) = …``: a component is never a list literal read whole."""
    return any(c.type == "multi_variable_declaration" for c in decl.named_children)


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


def _ident_is(node: Node, name: str, source: bytes) -> bool:
    """True when ``node`` (a ``class_parameter`` / ``variable_declaration``) declares ``name``."""
    ident = next((x for x in node.named_children if x.type == "simple_identifier"), None)
    return ident is not None and node_text(ident, source) == name


def _binds(node: Node, name: str, source: bytes) -> bool:
    """True when ``node`` binds ``name`` for the code inside it other than by a ``val`` / ``var``
    declaration: a parameter of a function, constructor or lambda, a class's primary-constructor
    parameter, a ``for`` loop variable, a ``catch`` parameter or a ``when (val x = …)`` subject."""
    if node.type in (
        "function_declaration",
        "lambda_literal",
        "anonymous_function",
        "secondary_constructor",
    ):
        return _binds_param(node, name, source)
    if node.type == "class_declaration":
        ctor = next((c for c in node.named_children if c.type == "primary_constructor"), None)
        return ctor is not None and any(
            c.type == "class_parameter" and _ident_is(c, name, source) for c in ctor.named_children
        )
    if node.type == "for_statement":
        return any(
            v.type == "variable_declaration" and _ident_is(v, name, source)
            for c in node.named_children
            if c.type in ("variable_declaration", "multi_variable_declaration")
            for v in ([c] if c.type == "variable_declaration" else c.named_children)
        )
    if node.type == "catch_block":
        ident = next((c for c in node.named_children if c.type == "simple_identifier"), None)
        return ident is not None and node_text(ident, source) == name
    if node.type == "when_expression":
        subject = next((c for c in node.named_children if c.type == "when_subject"), None)
        return subject is not None and any(
            c.type == "variable_declaration" and _ident_is(c, name, source)
            for c in subject.named_children
        )
    return False


#: Nodes whose own ``val`` / ``var`` declarations are indexed together (:class:`_Names`): a
#: name used inside one is declared in it or in one around it, never in a sibling.
_OWNERS = (
    "source_file",
    "class_declaration",
    "object_declaration",
    "function_declaration",
    "secondary_constructor",
    "lambda_literal",
    "anonymous_function",
)

_Span = tuple[int, int]


def _owner(node: Node) -> _Span:
    """The span of the nearest :data:`_OWNERS` node around (or at) ``node``."""
    n: Node | None = node
    while n is not None and n.type not in _OWNERS:
        n = n.parent
    target = n if n is not None else node
    return (target.start_byte, target.end_byte)


class _Names:
    """The file's declarations and uses by name, built once (on the first ``addTools`` whose list
    is not written inline): ``val`` / ``var`` declarations by name and the :data:`_OWNERS` node
    they are declared in, functions, and identifier uses in source order. An ``addTools`` call
    then looks only at the declarations of the nodes around it, and a list only at the uses
    inside its own block, so a file of many functions each declaring the same name stays linear."""

    def __init__(self, root: Node, source: bytes) -> None:
        self.props: dict[tuple[str, _Span], list[Node]] = {}
        self.funcs: dict[str, list[Node]] = {}
        self.uses: dict[str, list[Node]] = {}
        self.use_starts: dict[str, list[int]] = {}
        #: :func:`_only_passed_to_add_tools` per declaration (its start byte), so a list passed
        #: to many ``addTools`` calls is judged once.
        self.add_tools_only: dict[int, bool] = {}
        for n in walk(root):
            if n.type == "property_declaration":
                # A destructured component is indexed too: it hides an outer list of its name.
                for name in _declared_names(n, source):
                    self.props.setdefault((name, _owner(n)), []).append(n)
            elif n.type == "function_declaration":
                ident = next((c for c in n.named_children if c.type == "simple_identifier"), None)
                if ident is not None:
                    self.funcs.setdefault(node_text(ident, source), []).append(n)
            elif n.type == "simple_identifier":
                text = node_text(n, source)
                self.uses.setdefault(text, []).append(n)
                self.use_starts.setdefault(text, []).append(n.start_byte)

    def declarations(self, name: str, use: Node) -> list[Node]:
        """Declarations of ``name`` in the :data:`_OWNERS` nodes around ``use``."""
        out: list[Node] = []
        n: Node | None = use
        while n is not None:
            if n.type in _OWNERS:
                out.extend(self.props.get((name, (n.start_byte, n.end_byte)), []))
            n = n.parent
        return out

    def uses_in(self, name: str, scope: Node) -> list[Node]:
        """Uses of ``name`` inside ``scope`` (identifier uses are leaves, kept in source order)."""
        starts = self.use_starts.get(name, [])
        lo = bisect.bisect_left(starts, scope.start_byte)
        hi = bisect.bisect_left(starts, scope.end_byte)
        return self.uses[name][lo:hi] if hi > lo else []


def _contains(outer: Node, inner: Node) -> bool:
    return outer.start_byte <= inner.start_byte and inner.end_byte <= outer.end_byte


def _list_declaration(use: Node, name: str, source: bytes, names: _Names) -> Node | None:
    """The innermost declaration of ``name`` visible at ``use``: a local declared before it in
    an enclosing block, a property of an enclosing class, or a top-level one. None when the
    innermost binding is anything else (a parameter, a primary-constructor parameter, a loop
    variable, a ``catch`` parameter, a ``when`` subject — :func:`_binds`), or nothing declares
    it.

    A class's instance property is not visible inside a nested class that is not ``inner`` (nor
    a nested ``object`` / companion): Kotlin resolves the name further out there. A property of
    the ``companion object`` of a class around ``use`` (and nearer than the declaration found)
    is what the name resolves to: then None, the companion's list is not read. Not modelled:
    extension receivers, inherited members and ``with(x) { … }`` / ``apply`` implicit receivers
    (a name read through one of them is taken for the lexical declaration)."""
    best: Node | None = None
    best_key: tuple[int, int] | None = None
    for decl in names.declarations(name, use):
        container = decl.parent
        if container is None or container.type not in ("statements", "class_body", "source_file"):
            continue
        if not _contains(container, use):
            continue
        if container.type == "class_body" and _behind_nested_class(use, container, source):
            continue
        if container.type == "statements" and decl.start_byte >= use.start_byte:
            continue  # a local is visible only after its declaration
        # The containers holding the use are nested: the smallest is the innermost; in one
        # block the latest declaration before the use.
        key = (container.end_byte - container.start_byte, -decl.start_byte)
        if best_key is None or key < best_key:
            best, best_key = decl, key
    stop = best.parent if best is not None else None
    node = use.parent
    while node is not None and node != stop:
        if _binds(node, name, source):
            return None  # a nearer binding hides it
        if node.type == "class_declaration" and _companion_declares(node, name, source):
            return None  # the companion's member is nearer than any declaration further out
        node = node.parent
    return best


def _companion_declares(cls: Node, name: str, source: bytes) -> bool:
    """True when the ``companion object`` of the class ``cls`` declares a property ``name``: the
    name resolves to it anywhere in the class (and the classes nested in it) unless the class
    itself declares one. Which list it holds is not read: the name is then left unresolved."""
    body = next(
        (c for c in cls.named_children if c.type in ("class_body", "enum_class_body")), None
    )
    for member in body.named_children if body is not None else ():
        if member.type != "companion_object":
            continue
        cbody = next((c for c in member.named_children if c.type == "class_body"), None)
        for decl in cbody.named_children if cbody is not None else ():
            if decl.type == "property_declaration" and name in _declared_names(decl, source):
                return True
    return False


def _behind_nested_class(use: Node, body: Node, source: bytes) -> bool:
    """True when ``use`` sits in a class nested in the class owning ``body`` that cannot see its
    instance members: a member class that is not ``inner``, an ``object`` or a companion (a
    local class declared in a function body does see them)."""
    owner = body.parent
    if owner is None or owner.type != "class_declaration":
        return False  # an object's members are visible to the classes nested in it
    n = use.parent
    while n is not None and n != body:
        if n.type in ("object_declaration", "companion_object"):
            return True
        if (
            n.type == "class_declaration"
            and n.parent is not None
            and n.parent.type == "class_body"
            and not _is_inner(n, source)
        ):
            return True
        n = n.parent
    return False


def _is_inner(cls: Node, source: bytes) -> bool:
    mods = next((c for c in cls.named_children if c.type == "modifiers"), None)
    return mods is not None and any(
        c.type == "class_modifier" and node_text(c, source) == "inner" for c in mods.named_children
    )


def _only_passed_to_add_tools(name: str, decl: Node, source: bytes, names: _Names) -> bool:
    """True when every use of ``name`` where ``decl`` is visible (its block; the whole file for a
    property) is the sole argument of an ``addTools`` call (a mutable list nothing adds to,
    removes from or clears). Memoized per declaration on ``names``."""
    cached = names.add_tools_only.get(decl.start_byte)
    if cached is None:
        cached = names.add_tools_only[decl.start_byte] = _uses_only_add_tools(
            name, decl, source, names
        )
    return cached


def _uses_only_add_tools(name: str, decl: Node, source: bytes, names: _Names) -> bool:
    scope = decl.parent if decl.parent is not None and decl.parent.type == "statements" else None
    for n in names.uses_in(name, scope if scope is not None else _root(decl)):
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


def _root(node: Node) -> Node:
    while node.parent is not None:
        node = node.parent
    return node


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
        if decl is None or kind is None or node_text(kind, source) != "val" or _destructures(decl):
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
    sites = _HandlerSites(dc.root, source)
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
            emits.append(_kotlin_sdk_tool(node, fields, pos, handler, source, sites, aliases))
        elif method == "addTools":
            # A list read from a `val` or a function is new: its failure keeps the other tools.
            listed = guarded(dc.path, _registered_tools, node, source, aliases, names)
            for element, tool, handler in listed:
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
                        sites,
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
        desc_args = calls.get("description") or []
        req_args = calls.get("required") or []
        desc_node: Node | None = desc_args[0] if desc_args else None
        req_node: Node | None = req_args[0] if req_args else None
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
    sites = _HandlerSites(dc.root, source)
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
            handler = (
                next(
                    (v for k, v in reversed(args) if k is None and v.type == "lambda_literal"), None
                )
                or _named(args, "readHandler")
                or _named(args, "promptProvider")
            )
            pos = [v for k, v in args if k is None and v.type != "lambda_literal"]
            first = pos[0] if len(pos) == 1 and len(args) <= 2 else None
            if (
                first is not None
                and first.type == "call_expression"
                and _type_callee(first, source, aliases) == entry
            ):  # addPrompt(Prompt(…)) { … }
                fields = _named_entry(kotlin_call_args(first, source), entry, source, aliases)
            elif (
                entry != "ResourceTemplate"
                or _named(args, "uriTemplate") is not None
                or (len(pos) >= 2)
            ):
                fields = _named_entry(args, entry, source, aliases)
            if handler is None:
                continue
        if fields is None:
            continue
        endpoint, description, params = fields
        handler_name, handler_line = _handler(handler, source, sites)
        assert entry is not None
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
    found = (
        _sdk_tools(dc)
        + guarded(dc.path, _shortcut_tools, dc)
        + _kotlin_sdk_tools(dc)
        + guarded(dc.path, _entries, dc)
    )
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
