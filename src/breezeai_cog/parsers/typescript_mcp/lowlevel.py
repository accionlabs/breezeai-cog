"""Tools of a TypeScript / JavaScript server built with the SDK's **low-level** ``Server``
(``@modelcontextprotocol/sdk/server/index.js``), read for the ``typescript-mcp`` detector
(``tools.py``).

::

    import { Server } from "@modelcontextprotocol/sdk/server/index.js";
    import { ListToolsRequestSchema, CallToolRequestSchema, Tool } from "@modelcontextprotocol/sdk/types.js";

    const SEARCH: Tool = { name: "search", description: "Search", inputSchema: {…} };   → mcp_tool  search
    const server = new Server({ name: "x", version: "1" }, { capabilities: { tools: {} } });
    server.setRequestHandler(ListToolsRequestSchema, async () => ({
      tools: [SEARCH, { name: "echo", inputSchema: { type: "object", properties: {…} } }],  → mcp_tool  echo
    }));
    server.setRequestHandler(CallToolRequestSchema, async (request) => …);   handler (its line)

* **Server receiver** — a name (or ``this.x``) assigned ``new Server(…)`` or a parameter typed
  ``Server``, with ``Server`` imported from ``@modelcontextprotocol/sdk/server/index.js``; or
  ``<mcp>.server``, the low-level server of a proven ``McpServer``. A ``Server`` of any other
  package (socket.io, ``http``) is not one; a nearer binding of the name hides it.
* **Schemas** — ``ListToolsRequestSchema`` / ``CallToolRequestSchema`` imported from
  ``@modelcontextprotocol/sdk/types.js``; the same name from anywhere else is not it.
* **Tools** — the object literals of the ``tools: [...]`` array of the object the list handler
  returns (an arrow function's expression body, or a ``return`` of the function itself), and a
  ``const`` of this file holding such an object, referenced by name in that array. A list built
  any other way (a variable, ``.map``, a spread, a condition) → no record.
* ``endpoint`` — ``name``, a string literal; anything else → no record. ``description`` — a
  string literal or a ``+`` of them. ``params`` — a JSON-schema ``inputSchema`` literal
  (``properties`` / ``required``); anything else → None (unknown).
* ``handler`` — the callback of ``setRequestHandler(CallToolRequestSchema, …)`` on the same
  server in the same file (it dispatches every tool): a function literal gives its line, a
  function of this file its name; none, or more than one → no handler.
"""

from __future__ import annotations

from collections.abc import Callable

from tree_sitter import Node

from ...schemas import Parameter
from ..emits import Emit
from ..mcp_common import tool_emit
from ..typescript.declarations import import_bindings
from ..treesitter import node_text

#: ``(call, name, source, typed, built) → shadowed?`` — ``tools._shadowed``.
Shadowed = Callable[
    [
        Node,
        str,
        bytes,
        Callable[[Node, bytes], bool] | None,
        Callable[[Node | None, bytes], bool] | None,
    ],
    bool,
]
#: A JSON-schema object literal → its params, or None (unknown).
SchemaReader = Callable[[Node], "list[Parameter] | None"]

SERVER_MODULES = frozenset(
    {"@modelcontextprotocol/sdk/server/index.js", "@modelcontextprotocol/sdk/server/index"}
)
TYPES_MODULES = frozenset({"@modelcontextprotocol/sdk/types.js", "@modelcontextprotocol/sdk/types"})
_FUNCTION_LITERALS = ("arrow_function", "function_expression", "function")
_FUNCTION_SCOPES = frozenset(
    {
        "function_declaration",
        "function_expression",
        "function",
        "arrow_function",
        "method_definition",
        "generator_function_declaration",
        "generator_function",
    }
)


def _walk(node: Node) -> list[Node]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.named_children))
    return out


def _sdk_names(imports: dict[str, tuple[str, str]], modules: frozenset[str], name: str) -> set[str]:
    """What the SDK's ``name`` of ``modules`` is called here: ``import { name as a }`` → ``a``,
    ``import * as ns`` → ``ns.name``."""
    out: set[str] = set()
    for local, (module, export) in imports.items():
        if module not in modules:
            continue
        if export == name:
            out.add(local)
        elif export == "*":
            out.add(f"{local}.{name}")
    return out


def _unparen(node: Node | None) -> Node | None:
    while node is not None and node.type in (
        "parenthesized_expression",
        "as_expression",
        "satisfies_expression",
    ):
        node = node.named_children[0] if node.named_children else None
    return node


def _string(node: Node | None, source: bytes) -> str | None:
    """A string literal, a template literal without substitutions, or a ``+`` of them."""
    if node is None:
        return None
    if node.type == "binary_expression":
        parts: list[str] = []
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "binary_expression":
                op = n.child_by_field_name("operator")
                if op is None or node_text(op, source) != "+":
                    return None
                left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
                if left is None or right is None:
                    return None
                stack.extend((right, left))
                continue
            value = _string(n, source) if n.type != "binary_expression" else None
            if value is None:
                return None
            parts.append(value)
        return "".join(parts)
    if node.type == "string":
        frags = [c for c in node.named_children if c.type in ("string_fragment", "escape_sequence")]
        return "".join(node_text(f, source) for f in frags)
    if node.type == "template_string":
        if any(c.type == "template_substitution" for c in node.named_children):
            return None
        return node_text(node, source)[1:-1]
    return None


def _pairs(obj: Node, source: bytes) -> dict[str, Node]:
    out: dict[str, Node] = {}
    for p in obj.named_children:
        if p.type != "pair":
            continue
        key, value = p.child_by_field_name("key"), p.child_by_field_name("value")
        if key is None or value is None:
            continue
        name = _string(key, source) if key.type == "string" else node_text(key, source)
        if name:
            out[name] = value
    return out


def _const_objects(root: Node, source: bytes) -> dict[str, Node | None]:
    """``const X = {…}`` / ``const X: Tool = {…}`` of this file → the object (None when the name
    is declared more than once, or once with something else)."""
    out: dict[str, Node | None] = {}
    for node in _walk(root):
        if node.type not in ("lexical_declaration", "variable_declaration"):
            continue
        const = node.type == "lexical_declaration" and node_text(node, source).startswith("const")
        for decl in node.named_children:
            if decl.type != "variable_declarator":
                continue
            name = decl.child_by_field_name("name")
            if name is None or name.type != "identifier":
                continue
            key = node_text(name, source)
            value = _unparen(decl.child_by_field_name("value"))
            obj = value if const and value is not None and value.type == "object" else None
            out[key] = None if key in out else obj
    return out


def _returned_objects(fn: Node) -> list[Node]:
    """The objects ``fn`` returns: an arrow function's expression body, or the value of each
    ``return`` of the function itself (not of a nested function)."""
    body = fn.child_by_field_name("body")
    if body is None:
        return []
    if body.type != "statement_block":
        value = _unparen(body)
        return [value] if value is not None and value.type == "object" else []
    out: list[Node] = []
    stack = list(body.named_children)
    while stack:
        n = stack.pop()
        if n.type in _FUNCTION_SCOPES or n.type in ("class_declaration", "class"):
            continue
        if n.type == "return_statement":
            value = _unparen(n.named_children[0] if n.named_children else None)
            if value is not None and value.type == "object":
                out.append(value)
            continue
        stack.extend(n.named_children)
    return out


def _binding_scope(receiver: Node, source: bytes) -> tuple[int, int]:
    """Where the receiver's name is bound: the nearest enclosing function (by its parameters)
    or block (by a declaration) that binds it, for a plain name; the enclosing class for
    ``this.x``; else the file. Two calls on the same text in the same binding scope are on the
    same server."""
    text = node_text(receiver, source)
    node = receiver.parent
    while node is not None and node.type != "program":
        if receiver.type != "identifier":
            if node.type in ("class_declaration", "class") and text.startswith("this."):
                return (node.start_byte, node.end_byte)
        elif node.type in _FUNCTION_SCOPES:
            params = node.child_by_field_name("parameters") or node.child_by_field_name("parameter")
            if params is not None and any(
                c.type == "identifier" and node_text(c, source) == text for c in _walk(params)
            ):
                return (node.start_byte, node.end_byte)
        elif node.type == "statement_block":
            for stmt in node.named_children:
                if stmt.type not in ("lexical_declaration", "variable_declaration"):
                    continue
                for decl in stmt.named_children:
                    name = (
                        decl.child_by_field_name("name")
                        if decl.type == "variable_declarator"
                        else None
                    )
                    if name is not None and node_text(name, source) == text:
                        return (node.start_byte, node.end_byte)
        node = node.parent
    return (-1, -1)


def detect_lowlevel_tools(
    dc_root: Node,
    source: bytes,
    mcp_servers: set[str],
    functions: dict[str, int],
    shadowed: Shadowed,
    input_schema: SchemaReader,
) -> list[Emit]:
    """The tools of the file's ``setRequestHandler(ListToolsRequestSchema, …)`` calls (see the
    module docstring). ``mcp_servers`` are the proven ``McpServer`` receivers, ``shadowed`` the
    detector's scope check and ``input_schema`` its JSON-schema literal reader."""
    if b"ListToolsRequestSchema" not in source or b"setRequestHandler" not in source:
        return []
    imports = import_bindings(dc_root, source)
    classes = _sdk_names(imports, SERVER_MODULES, "Server")
    list_schema = _sdk_names(imports, TYPES_MODULES, "ListToolsRequestSchema")
    call_schema = _sdk_names(imports, TYPES_MODULES, "CallToolRequestSchema")
    if not list_schema:
        return []

    def typed(param: Node, src: bytes) -> bool:
        ann = param.child_by_field_name("type")
        return ann is not None and node_text(ann, src).lstrip(": ").strip() in classes

    def built(value: Node | None, src: bytes) -> bool:
        if value is None or value.type != "new_expression":
            return False
        ctor = value.child_by_field_name("constructor")
        return ctor is not None and node_text(ctor, src) in classes

    servers: set[str] = {f"{m}.server" for m in mcp_servers}
    for node in _walk(dc_root):
        if node.type == "new_expression" and built(node, source):
            parent = node.parent
            if parent is not None and parent.type == "variable_declarator":
                name = parent.child_by_field_name("name")
                if name is not None and name.type == "identifier":
                    servers.add(node_text(name, source))
            elif parent is not None and parent.type == "assignment_expression":
                left = parent.child_by_field_name("left")
                if left is not None:
                    servers.add(node_text(left, source))
            elif parent is not None and parent.type in (
                "public_field_definition",
                "field_definition",
            ):
                name = parent.child_by_field_name("name") or parent.child_by_field_name("property")
                if name is not None:
                    servers.update((node_text(name, source), f"this.{node_text(name, source)}"))
        elif node.type in ("required_parameter", "optional_parameter", "public_field_definition"):
            name = node.child_by_field_name("pattern") or node.child_by_field_name("name")
            if name is not None and typed(node, source):
                text = node_text(name, source)
                servers.add(text)
                if node.type == "public_field_definition":
                    servers.add(f"this.{text}")
    if not servers:
        return []

    listers: list[tuple[tuple[str, tuple[int, int]], Node]] = []
    dispatchers: dict[tuple[str, tuple[int, int]], list[Node]] = {}
    for call in _walk(dc_root):
        if call.type != "call_expression":
            continue
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            continue
        obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
        if obj is None or prop is None or node_text(prop, source) != "setRequestHandler":
            continue
        receiver = node_text(obj, source)
        if receiver not in servers:
            continue
        if obj.type == "identifier" and shadowed(call, receiver, source, typed, built):
            continue
        if obj.type == "member_expression" and receiver.endswith(".server"):
            inner = obj.child_by_field_name("object")
            if (
                inner is not None
                and inner.type == "identifier"
                and shadowed(call, node_text(inner, source), source, None, None)
            ):
                continue
        args_node = call.child_by_field_name("arguments")
        args = [a for a in (args_node.named_children if args_node else []) if a.type != "comment"]
        if len(args) != 2:
            continue
        schema = node_text(args[0], source)
        key = (receiver, _binding_scope(obj, source))
        if schema in list_schema and args[1].type in _FUNCTION_LITERALS:
            listers.append((key, args[1]))
        elif schema in call_schema:
            dispatchers.setdefault(key, []).append(args[1])
    consts = _const_objects(dc_root, source)
    found: list[Emit] = []
    seen: set[int] = set()
    for key, fn in listers:
        handler: str | None = None
        handler_line: int | None = None
        cbs = dispatchers.get(key, [])
        if len(cbs) == 1:
            cb = cbs[0]
            if cb.type in _FUNCTION_LITERALS:
                handler_line = cb.start_point[0] + 1
            elif cb.type == "identifier":
                handler = node_text(cb, source)
                handler_line = functions.get(handler)
        for result in _returned_objects(fn):
            tools = _unparen(_pairs(result, source).get("tools"))
            if tools is None or tools.type != "array":
                continue
            for element in tools.named_children:
                obj_node = _unparen(element)
                if obj_node is not None and obj_node.type == "identifier":
                    obj_node = consts.get(node_text(obj_node, source))
                if obj_node is None or obj_node.type != "object" or obj_node.start_byte in seen:
                    continue
                emit = _tool(obj_node, source, handler, handler_line, input_schema)
                if emit is not None:
                    seen.add(obj_node.start_byte)
                    found.append(emit)
    return found


def _tool(
    obj: Node,
    source: bytes,
    handler: str | None,
    handler_line: int | None,
    input_schema: SchemaReader,
) -> Emit | None:
    fields = _pairs(obj, source)
    name_node = fields.get("name")
    name = (
        _string(name_node, source)
        if name_node is not None and name_node.type in ("string", "template_string")
        else None
    )
    if not name:
        return None  # a computed name: the tool exists, but its name is unknown
    schema = _unparen(fields.get("inputSchema"))
    params: list[Parameter] | None = (
        input_schema(schema) if schema is not None and schema.type == "object" else None
    )
    return tool_emit(
        obj,
        source,
        endpoint=name,
        framework="typescript-mcp",
        handler=handler,
        handler_line=handler_line,
        description=_string(fields.get("description"), source),
        params=params,
        node_type=None,
    )
