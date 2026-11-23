"""Tools, resources and prompts of a TypeScript / JavaScript server built with the SDK's
**low-level** ``Server`` (``@modelcontextprotocol/sdk/server/index.js``), read for the
``typescript-mcp`` detector (``tools.py``).

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
  package (socket.io, ``http``) is not one. The call's nearest binding of the name must be the
  proven one (a nearer binding, a ``var`` anywhere in the function, hides it); a ``this.x``
  is the server only in the class whose field it is.
* **Schemas** — ``ListToolsRequestSchema`` / ``CallToolRequestSchema`` imported from
  ``@modelcontextprotocol/sdk/types.js``; the same name from anywhere else is not it.
* **Tools** — the object literals of the ``tools: [...]`` array of the object the list handler
  returns (an arrow function's expression body, or a ``return`` of the function itself), and a
  module-level ``const`` holding such an object, referenced by name in that array (only when
  nothing else in the file declares or imports that name). A list built
  any other way (a variable, ``.map``, a spread, a condition) → no record. A server given more
  than one list handler (only the last set answers) → no record.
* ``endpoint`` — ``name``, a string literal; anything else → no record. ``description`` — a
  string literal or a ``+`` of them. ``params`` — a JSON-schema ``inputSchema`` literal
  (``properties`` / ``required``); anything else → None (unknown).
* ``handler`` — the callback of ``setRequestHandler(CallToolRequestSchema, …)`` on the same
  server in the same file (it dispatches every tool): a function literal gives its line, a
  function of this file its name; none, or more than one → no handler.

Resources and prompts are read the same way, from their own list handlers::

    server.setRequestHandler(ListResourcesRequestSchema, async () => ({
      resources: [{ uri: "console://logs", name: "Logs" }] }));      → mcp_resource  console://logs
    server.setRequestHandler(ListResourceTemplatesRequestSchema, async () => ({
      resourceTemplates: [{ uriTemplate: "users://{id}", name: "User" }] }));  → mcp_resource  users://{id}
    server.setRequestHandler(ReadResourceRequestSchema, readResource);        handler of both
    server.setRequestHandler(ListPromptsRequestSchema, async () => ({
      prompts: [{ name: "review", arguments: [{ name: "code", required: true }] }] }))  → mcp_prompt  review
    server.setRequestHandler(GetPromptRequestSchema, getPrompt);              handler

* ``mcp_resource`` — ``uri`` (a template: ``uriTemplate``), a string literal; anything else → no
  record. ``name`` is the entry's ``name`` when it is a string literal and no spread in the
  object could override it; else the URI. ``params`` are a template's simple ``{variables}`` (None with an RFC 6570 operator);
  ``[]`` for a fixed ``uri``.
* ``mcp_prompt`` — ``name``, a string literal; ``params`` from a literal ``arguments`` array of
  ``{ name, description, required }`` objects (``required`` false unless ``true``); no
  ``arguments`` → ``[]``; anything else → None (unknown).
"""

from __future__ import annotations

from collections.abc import Callable

from tree_sitter import Node

from ...schemas import Parameter
from ..emits import Emit
from ..mcp_common import PROMPT, RESOURCE, TOOL, guarded, mcp_emit, param, uri_template_params
from ..typescript.declarations import import_bindings
from ..treesitter import node_text
from .scopes import Binding, Scopes, function_line, proven_receivers, receiver_binding


#: A JSON-schema object literal → its params, or None (unknown).
SchemaReader = Callable[[Node], "list[Parameter] | None"]

SERVER_MODULES = frozenset(
    {"@modelcontextprotocol/sdk/server/index.js", "@modelcontextprotocol/sdk/server/index"}
)
TYPES_MODULES = frozenset({"@modelcontextprotocol/sdk/types.js", "@modelcontextprotocol/sdk/types"})
#: ``(list schema, dispatch schema, key of the returned array, kind)`` of what a server lists.
_LISTED = (
    ("ListToolsRequestSchema", "CallToolRequestSchema", "tools", TOOL),
    ("ListResourcesRequestSchema", "ReadResourceRequestSchema", "resources", RESOURCE),
    (
        "ListResourceTemplatesRequestSchema",
        "ReadResourceRequestSchema",
        "resourceTemplates",
        RESOURCE,
    ),
    ("ListPromptsRequestSchema", "GetPromptRequestSchema", "prompts", PROMPT),
)
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
    """A string literal, a template literal without substitutions, or a ``+`` of them; None
    when any part has an escape sequence."""
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
    if node.type not in ("string", "template_string"):
        return None
    if any(c.type in ("escape_sequence", "template_substitution") for c in node.named_children):
        return None  # an escape is not decoded here, so its text would be wrong
    if node.type == "string":
        return "".join(
            node_text(c, source) for c in node.named_children if c.type == "string_fragment"
        )
    return node_text(node, source)[1:-1]


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


def _const_objects(
    root: Node, source: bytes, imports: dict[str, tuple[str, str]]
) -> dict[str, Node | None]:
    """Module-level ``const X = {…}`` / ``const X: Tool = {…}`` (also ``export const``) → the
    object. None when the name is declared more than once anywhere in the file (a local or a
    parameter could shadow it where it is used), is also an import, or is declared with
    something else."""
    out: dict[str, Node | None] = {name: None for name in imports}
    for node in _walk(root):
        if node.type in ("required_parameter", "optional_parameter"):
            pattern = node.child_by_field_name("pattern")
            if pattern is not None and pattern.type == "identifier":
                out[node_text(pattern, source)] = None
            continue
        if node.type not in ("lexical_declaration", "variable_declaration"):
            continue
        parent = node.parent
        if parent is not None and parent.type == "export_statement":
            parent = parent.parent
        top = parent is not None and parent.type == "program"
        const = node.type == "lexical_declaration" and node_text(node, source).startswith("const")
        for decl in node.named_children:
            if decl.type != "variable_declarator":
                continue
            name = decl.child_by_field_name("name")
            if name is None or name.type != "identifier":
                continue
            key = node_text(name, source)
            value = _unparen(decl.child_by_field_name("value"))
            obj = value if top and const and value is not None and value.type == "object" else None
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


def detect_lowlevel(
    path: str,
    dc_root: Node,
    source: bytes,
    mcp_servers: set[Binding],
    functions: dict[str, int],
    scopes: Scopes,
    input_schema: SchemaReader,
) -> list[Emit]:
    """The tools, resources and prompts of the file's ``setRequestHandler(List…RequestSchema,
    …)`` calls (see the module docstring). ``mcp_servers`` are the bindings proven to be an
    ``McpServer``, ``scopes`` the file's scopes and ``input_schema`` the detector's JSON-schema
    literal reader. Each list handler is read guarded (``path`` names the file in the log), so a
    resource or prompt list that fails to read never drops the file's tools, nor the reverse."""
    if b"setRequestHandler" not in source or not any(
        listed[0].encode() in source for listed in _LISTED
    ):
        return []
    imports = import_bindings(dc_root, source)
    classes = _sdk_names(imports, SERVER_MODULES, "Server")
    # schema local name → (index in _LISTED) for list schemas; → dispatch schema name.
    list_schemas: dict[str, int] = {}
    dispatch_schemas: dict[str, str] = {}
    for i, (list_name, dispatch_name, _, _) in enumerate(_LISTED):
        for local in _sdk_names(imports, TYPES_MODULES, list_name):
            list_schemas[local] = i
        for local in _sdk_names(imports, TYPES_MODULES, dispatch_name):
            dispatch_schemas[local] = dispatch_name
    if not list_schemas:
        return []

    def typed(param: Node, src: bytes) -> bool:
        ann = param.child_by_field_name("type")
        return ann is not None and node_text(ann, src).lstrip(": ").strip() in classes

    def built(value: Node | None, src: bytes) -> bool:
        if value is None or value.type != "new_expression":
            return False
        ctor = value.child_by_field_name("constructor")
        return ctor is not None and node_text(ctor, src) in classes

    servers = proven_receivers(dc_root, source, scopes, typed, built) if classes else set()
    if not servers and not mcp_servers:
        return []

    listers: list[tuple[int, Binding, Node]] = []
    dispatchers: dict[tuple[str, Binding], list[Node]] = {}
    #: Per (list schema, server): how many list handlers it is given (each replaces the one
    #: before at runtime).
    list_handlers: dict[tuple[int, Binding], int] = {}
    for call in _walk(dc_root):
        if call.type != "call_expression":
            continue
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            continue
        obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
        if obj is None or prop is None or node_text(prop, source) != "setRequestHandler":
            continue
        key = _server(obj, source, scopes, servers, mcp_servers)
        if key is None:
            continue
        args_node = call.child_by_field_name("arguments")
        args = [a for a in (args_node.named_children if args_node else []) if a.type != "comment"]
        if len(args) != 2:
            continue
        schema = node_text(args[0], source)
        if schema in list_schemas:
            listed = list_schemas[schema]
            list_handlers[(listed, key)] = list_handlers.get((listed, key), 0) + 1
            if args[1].type in _FUNCTION_LITERALS:
                listers.append((listed, key, args[1]))
        elif schema in dispatch_schemas:
            dispatchers.setdefault((dispatch_schemas[schema], key), []).append(args[1])
    consts = _const_objects(dc_root, source, imports)
    found: list[Emit] = []
    seen: set[int] = set()
    for listed, key, fn in listers:
        if list_handlers[(listed, key)] > 1:
            continue  # only the last one set answers at runtime: which one is unknown
        _, dispatch_name, _, _ = _LISTED[listed]
        read = guarded(
            path,
            _listed_entries,
            listed,
            fn,
            dispatchers.get((dispatch_name, key), []),
            source,
            consts,
            functions,
            scopes,
            input_schema,
            seen,
        )
        for start, emit in read:
            seen.add(start)
            found.append(emit)
    return found


def _listed_entries(
    listed: int,
    fn: Node,
    dispatch: list[Node],
    source: bytes,
    consts: dict[str, Node | None],
    functions: dict[str, int],
    scopes: Scopes,
    input_schema: SchemaReader,
    seen: set[int],
) -> list[tuple[int, Emit]]:
    """(start byte, record) of each entry one list handler ``fn`` returns, for the ``_LISTED``
    kind ``listed``, linked to its server's one dispatch callback (``dispatch``). ``seen`` holds
    the entries already recorded (it is only read: the caller adds these once they are all read)."""
    _, _, array_key, kind = _LISTED[listed]
    handler: str | None = None
    handler_line: int | None = None
    if len(dispatch) == 1:
        cb = dispatch[0]
        if cb.type in _FUNCTION_LITERALS:
            handler_line = cb.start_point[0] + 1
        elif cb.type == "identifier":
            handler = node_text(cb, source)
            handler_line = function_line(cb, source, functions, scopes)
    read: list[tuple[int, Emit]] = []
    taken: set[int] = set()
    for result in _returned_objects(fn):
        entries = _unparen(_pairs(result, source).get(array_key))
        if entries is None or entries.type != "array":
            continue
        for element in entries.named_children:
            obj_node = _unparen(element)
            if obj_node is not None and obj_node.type == "identifier":
                obj_node = consts.get(node_text(obj_node, source))
            if obj_node is None or obj_node.type != "object":
                continue
            if obj_node.start_byte in seen or obj_node.start_byte in taken:
                continue
            emit = _entry(obj_node, source, kind, array_key, (handler, handler_line), input_schema)
            if emit is not None:
                taken.add(obj_node.start_byte)
                read.append((obj_node.start_byte, emit))
    return read


def _server(
    obj: Node,
    source: bytes,
    scopes: Scopes,
    servers: set[Binding],
    mcp_servers: set[Binding],
) -> Binding | None:
    """The binding of a ``setRequestHandler`` receiver when it is a proven ``Server``, or the
    ``.server`` of a proven ``McpServer``; else None."""
    key = receiver_binding(obj, source, scopes)
    if key is None:
        return None
    if key in servers:
        return key
    inner = obj.child_by_field_name("object") if obj.type == "member_expression" else None
    prop = obj.child_by_field_name("property") if inner is not None else None
    if inner is None or prop is None or node_text(prop, source) != "server":
        return None
    return key if receiver_binding(inner, source, scopes) in mcp_servers else None


def _literal_string(node: Node | None, source: bytes) -> str | None:
    """A string literal (or a template literal without substitutions)."""
    return (
        _string(node, source)
        if node is not None and node.type in ("string", "template_string")
        else None
    )


def _prompt_arguments(node: Node | None, source: bytes) -> list[Parameter] | None:
    """A literal ``arguments: [{ name, description, required }]`` → params; ``[]`` without it;
    None when it is not an array of such object literals."""
    node = _unparen(node)
    if node is None:
        return []
    if node.type != "array":
        return None
    out: list[Parameter] = []
    for element in node.named_children:
        if element.type == "comment":
            continue
        obj = _unparen(element)
        if obj is None or obj.type != "object":
            return None
        fields = _pairs(obj, source)
        name = _literal_string(fields.get("name"), source)
        desc_node = fields.get("description")
        description = _string(desc_node, source) if desc_node is not None else None
        req = fields.get("required")
        req_text = node_text(req, source) if req is not None else "false"
        if (
            not name
            or req_text not in ("true", "false")
            or (desc_node is not None and description is None)
        ):
            return None
        out.append(param(name, None, required=req_text == "true", description=description))
    return out


def _entry(
    obj: Node,
    source: bytes,
    kind: tuple[str, str],
    array_key: str,
    handler: tuple[str | None, int | None],
    input_schema: SchemaReader,
) -> Emit | None:
    """One tool / resource / prompt object of a list handler's returned array."""
    fields = _pairs(obj, source)
    params: list[Parameter] | None
    declared: str | None = None
    if kind == RESOURCE:
        if not any(c.type == "spread_element" for c in obj.named_children):
            declared = _literal_string(fields.get("name"), source) or None
        template = array_key == "resourceTemplates"
        name = _literal_string(fields.get("uriTemplate" if template else "uri"), source)
        # A fixed resource's URI is literal, braces and all: it has no variables.
        params = (uri_template_params(name) if name else None) if template else []
    else:
        name = _literal_string(fields.get("name"), source)
        if kind == TOOL:
            schema = _unparen(fields.get("inputSchema"))
            params = (
                input_schema(schema) if schema is not None and schema.type == "object" else None
            )
        else:
            params = _prompt_arguments(fields.get("arguments"), source)
    if not name:
        return None  # a computed name: the entry exists, but its name is unknown
    return mcp_emit(
        obj,
        source,
        kind=kind,
        endpoint=name,
        framework="typescript-mcp",
        handler=handler[0],
        handler_line=handler[1],
        description=_string(fields.get("description"), source),
        params=params,
        name=declared,
    )
