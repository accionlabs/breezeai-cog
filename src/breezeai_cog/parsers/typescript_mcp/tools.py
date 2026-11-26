"""Additive detector: tools of a TypeScript / JavaScript MCP server (``@modelcontextprotocol/sdk``).

::

    const server = new McpServer({ name: "x" })     # or a parameter typed McpServer
    server.tool("add", "Add two numbers",           → mcp_tool  typescript-mcp  TOOL  add
                { a: z.number().describe("first"), b: z.number().optional() },
                async ({ a, b }) => …)
    server.registerTool("echo",                     → mcp_tool  typescript-mcp  TOOL  echo
                { description: "Echo", inputSchema: { msg: z.string() } }, echoHandler)

* **Server receiver** — the call's object must be a name (or ``this.x``) assigned
  ``new McpServer(…)`` in this file, or a parameter typed ``McpServer``, and the call's
  nearest binding of the name must be that one: a name shadowed at the call by another
  parameter or local of an enclosing scope (``function f(server: Foo) { server.tool(…) }``, a
  ``var`` anywhere in the function), or a ``this.x`` of a class other than the one whose field
  is the server, is not the server (:mod:`.scopes`).
* ``tool(name, [description], [paramsShape], [annotations], callback)`` — every overload: the
  description is the string after the name; an object is the params shape only when its values
  are zod chains, so an annotations object (``{ readOnlyHint: true }``) is never read as params.
* ``registerTool(name, { description, inputSchema }, callback)`` — ``inputSchema`` a zod raw
  shape, ``z.object({…})``, or a JSON-schema literal (``properties`` / ``required``).
* ``params`` — ``[]`` only for no schema or an empty shape ``{}``; a schema that cannot be read
  whole (a variable, ``{ id: idSchema }``, a spread, ``MySchema.shape``) → ``None`` (unknown),
  never a partial list.
* ``handler`` — a named callback (its line when it is a function declared at the top of this
  file and no enclosing scope binds the name first: a parameter, a local, a nested declaration,
  a ``catch`` or loop variable gives no line, see :mod:`.scopes`); an inline callback has no
  name, only its ``handlerLine``. A wrapper call is looked into only for a function literal,
  or a function declared in this file passed as its only argument
  (``withErrors(namedHandler)``); any other wrapper (``createHandler(apiClient)``) gives no
  ``handler``, only the wrapper's line.
* A non-literal tool name → no record.

Resources and prompts of the same ``McpServer``::

    server.resource("config", "config://app", { description: "Config" }, cb)
                                                    → mcp_resource  typescript-mcp  RESOURCE  config://app  name config
    server.registerResource("user", new ResourceTemplate("users://{id}", { list: undefined }),
                            { description: "A user" }, cb)
                                                    → mcp_resource  …  users://{id}   params id
    server.prompt("review", "Review code", { code: z.string() }, cb)
                                                    → mcp_prompt  typescript-mcp  PROMPT  review
    server.registerPrompt("explain", { description: "Explain", argsSchema: { topic: z.string() } }, cb)

* ``resource(name, uriOrTemplate, [metadata], cb)`` / ``registerResource(name, uriOrTemplate,
  config, cb)`` — ``name`` is the first argument when it is a string literal (else the URI);
  ``endpoint`` is the URI, a string literal, or the first argument of
  ``new ResourceTemplate("…", …)`` with ``ResourceTemplate`` imported from
  ``@modelcontextprotocol/sdk/server/mcp.js``; anything else → no record. ``description`` from
  the metadata / config literal; ``params`` are the template's simple ``{variables}`` (None with
  an RFC 6570 operator); ``[]`` for a fixed URI string.
* ``prompt(name, [description], [argsShape], cb)`` / ``registerPrompt(name, { description,
  argsSchema }, cb)`` — ``params`` are the arguments of the zod shape: required unless
  ``.optional()`` / ``.default()``, described by ``.describe()``; no type or default (a prompt
  argument has none). A shape that cannot be read whole → None (unknown).
* ``handler`` — the callback, as for a tool.

A low-level ``Server``'s ``setRequestHandler(ListToolsRequestSchema, …)`` (and the resource and
prompt list handlers) is read by :mod:`.lowlevel` (where the rules are).
"""

from __future__ import annotations

from collections.abc import Callable

from tree_sitter import Node

from ...schemas import Parameter
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..mcp_common import (
    PROMPT,
    RESOURCE,
    TOOL,
    function_lines,
    guarded,
    guarded_one,
    mcp_emit,
    param,
    prompt_params,
    uri_template_params,
)
from ..typescript_http_common import inline_handler
from ..treesitter import node_text
from ..typescript.declarations import import_bindings, top_level_declarations
from .lowlevel import detect_lowlevel
from .scopes import Binding, Scopes, function_line, proven_receivers, receiver_binding

FRAMEWORK = "typescript-mcp"
_CALLBACKS = (
    "arrow_function",
    "function_expression",
    "function",
    "identifier",
    "member_expression",
)
_FUNCTION_LITERALS = ("arrow_function", "function_expression", "function")
#: Keys of the MCP tool-annotations object (``tool(name, shape, annotations, cb)``).
_ANNOTATION_KEYS = frozenset(
    {"title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
)
_MAX_DEFAULT = 200
#: Modules that export ``McpServer`` and ``ResourceTemplate``.
_MCP_MODULES = frozenset(
    {"@modelcontextprotocol/sdk/server/mcp.js", "@modelcontextprotocol/sdk/server/mcp"}
)
#: The server methods read here → the kind of record they register.
_REGISTRATIONS = {
    "tool": TOOL,
    "registerTool": TOOL,
    "resource": RESOURCE,
    "registerResource": RESOURCE,
    "prompt": PROMPT,
    "registerPrompt": PROMPT,
}


def _walk(node: Node) -> list[Node]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.named_children))
    return out


def _string(node: Node | None, source: bytes) -> str | None:
    """A string literal, or a template literal without substitutions."""
    if node is None:
        return None
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


def _mcp_typed(node: Node, source: bytes) -> bool:
    ann = node.child_by_field_name("type")
    return ann is not None and node_text(ann, source).lstrip(": ").rsplit(".", 1)[-1] == "McpServer"


def _new_mcp_server(node: Node | None, source: bytes) -> bool:
    if node is None or node.type != "new_expression":
        return False
    ctor = node.child_by_field_name("constructor")
    return ctor is not None and node_text(ctor, source).rsplit(".", 1)[-1] == "McpServer"


def _servers(root: Node, source: bytes, scopes: Scopes) -> set[Binding]:
    """The bindings proven to be an ``McpServer``: a name assigned ``new McpServer(…)`` or a
    parameter typed ``McpServer``, and a class field given either (``this.x``, for that class).
    A call is on the server only when its receiver's nearest binding is one of them, so
    ``function f(server: Foo) { server.tool(…) }`` is not."""
    return proven_receivers(root, source, scopes, _mcp_typed, _new_mcp_server)


def _zod_names(dc: DetectContext) -> set[str]:
    names = {"z"}
    for local, module in (dc.bindings or {}).items():
        if module in ("zod", "zod/v4", "zod/v3"):
            names.add(local)
    return names


def _chain(node: Node, source: bytes, zod: set[str]) -> list[tuple[str, list[Node]]] | None:
    """``z.string().describe("x").optional()`` → ``[("string", []), ("describe", [..]), ("optional", [])]``
    (innermost first), or None when ``node`` is not a zod chain."""
    calls: list[tuple[str, list[Node]]] = []
    cur: Node | None = node
    while cur is not None and cur.type == "call_expression":
        fn = cur.child_by_field_name("function")
        args = cur.child_by_field_name("arguments")
        if fn is None or fn.type != "member_expression":
            return None
        prop = fn.child_by_field_name("property")
        calls.append(
            (
                node_text(prop, source) if prop is not None else "",
                list(args.named_children) if args else [],
            )
        )
        obj = fn.child_by_field_name("object")
        if obj is not None and obj.type == "identifier":
            return list(reversed(calls)) if node_text(obj, source) in zod else None
        cur = obj
    return None


#: Wrapper factories whose argument is the parameter's own schema (``z.optional(z.string())``).
#: ``optional`` / ``nullish`` make the key optional; ``nullable`` only allows ``null`` and the
#: key stays required, as with the ``.nullable()`` method.
_ZOD_WRAPPERS = frozenset({"optional", "nullish", "nullable"})


def _zod_param(name: str, value: Node, source: bytes, zod: set[str]) -> Parameter | None:
    chain = _chain(value, source, zod)
    if not chain:
        return None
    base, base_args = chain[0]
    if base in _ZOD_WRAPPERS:
        # The wrapped schema gives the type (None when it is not a zod chain: a variable).
        inner = _zod_param(name, base_args[0], source, zod) if len(base_args) == 1 else None
        outer = _zod_param_chain(name, None, [(base, [])] + chain[1:], source)
        if inner is None:
            return outer
        return param(
            name,
            inner.type,
            required=inner.required and outer.required,
            default=outer.default if outer.default is not None else inner.default,
            description=outer.description or inner.description,
        )
    return _zod_param_chain(name, base, chain, source)


def _zod_param_chain(
    name: str, base: str | None, chain: list[tuple[str, list[Node]]], source: bytes
) -> Parameter:
    """The param of a zod chain whose base type is ``base`` (None when unknown); ``chain[0]``
    is the base call, the rest are its methods."""
    base_args = chain[0][1]
    type_ = base
    if base == "enum" and base_args and base_args[0].type == "array":
        vals = [_string(v, source) for v in base_args[0].named_children]
        if all(v is not None for v in vals):
            type_ = f"enum({','.join(v for v in vals if v is not None)})"
    required, default, description = True, None, None
    for method, args in chain[1 if base is not None else 0 :]:
        if method in ("optional", "nullish"):
            required = False
        elif method == "default":
            required = False
            if args:
                text = " ".join(node_text(args[0], source).split())
                default = text if len(text) <= _MAX_DEFAULT else text[: _MAX_DEFAULT - 1] + "…"
        elif method == "describe" and args:
            description = _string(args[0], source)
    return param(name, type_, required=required, default=default, description=description)


def _zod_shape(obj: Node, source: bytes, zod: set[str]) -> list[Parameter] | None:
    """An object literal whose every value is a zod chain → its params; else None."""
    if obj.type != "object":
        return None
    pairs = _pairs(obj, source)
    if len(pairs) != len([c for c in obj.named_children if c.type != "comment"]):
        return None  # spreads / shorthand / methods: not a literal shape
    out = []
    for name, value in pairs.items():
        p = _zod_param(name, value, source, zod)
        if p is None:
            return None
        out.append(p)
    return out


def _input_schema(node: Node, source: bytes, zod: set[str]) -> list[Parameter] | None:
    """Params of an ``inputSchema`` value, or None when it cannot be read whole."""
    shape = _zod_shape(node, source, zod)
    if shape is not None:
        return shape
    chain = _chain(node, source, zod) if node.type == "call_expression" else None
    if chain and chain[0][0] == "object" and chain[0][1]:
        return _object_schema(chain, source, zod)
    if node.type == "object":
        return _json_schema(node, source)
    return None


def _json_schema(node: Node, source: bytes) -> list[Parameter] | None:
    """Params of a JSON-schema object literal (``properties`` / ``required``), or None when it
    cannot be read whole."""
    pairs = _pairs(node, source)
    props = pairs.get("properties")
    if props is None or props.type != "object" or not _literal_object(props, source):
        return None
    req = pairs.get("required")
    required: set[str] = set()
    if req is not None:
        if req.type != "array":
            return None  # `required: fields` — which ones is unknown
        names = [_string(v, source) for v in req.named_children]
        if any(n is None for n in names):
            return None
        required = {n for n in names if n is not None}
    out = []
    for name, spec in _pairs(props, source).items():
        fields = _pairs(spec, source) if spec.type == "object" else {}
        out.append(
            param(
                name,
                _string(fields.get("type"), source),
                required=name in required,
                description=_string(fields.get("description"), source),
            )
        )
    return out


#: ``z.object({…})`` methods that change no key and no key's optionality.
_OBJECT_NEUTRAL = frozenset({"describe", "strict", "strip", "passthrough"})


def _object_schema(
    chain: list[tuple[str, list[Node]]], source: bytes, zod: set[str]
) -> list[Parameter] | None:
    """Params of ``z.object({…})`` and its methods. ``.partial()`` / ``.deepPartial()`` make
    every key optional; a method that adds, removes or picks keys (``.extend``, ``.merge``,
    ``.pick``, ``.omit``, ``.partial({a: true})``, …) leaves the params unknown."""
    shape = _zod_shape(chain[0][1][0], source, zod)
    if shape is None:
        return None
    all_optional = False
    for method, args in chain[1:]:
        if method in ("partial", "deepPartial") and not args:
            all_optional = True
        elif method not in _OBJECT_NEUTRAL:
            return None
    if not all_optional:
        return shape
    return [
        param(p.name, p.type, required=False, default=p.default, description=p.description)
        for p in shape
    ]


def _literal_object(obj: Node, source: bytes) -> bool:
    """True when every member of the object literal is a ``key: value`` pair (no spread)."""
    return len(_pairs(obj, source)) == len([c for c in obj.named_children if c.type != "comment"])


def _annotations(node: Node, source: bytes) -> bool:
    """True for a tool-annotations object literal (``{ readOnlyHint: true }``)."""
    if node.type != "object" or not _literal_object(node, source):
        return False
    keys = set(_pairs(node, source))
    return bool(keys) and keys <= _ANNOTATION_KEYS


def _tool_params(middle: list[Node], source: bytes, zod: set[str]) -> list[Parameter] | None:
    """Params of ``tool(name, [description], [shape], [annotations], cb)`` from the arguments
    between the description and the callback: ``[]`` with no shape, the shape's params when it
    reads whole, else None."""
    if not middle or (len(middle) == 1 and _annotations(middle[0], source)):
        return []
    return _zod_shape(middle[0], source, zod)


def _unwrap(cb: Node, source: bytes, functions: dict[str, int], scopes: Scopes) -> Node:
    """The node naming the handler: the callback itself, or the function inside a wrapper call
    (``withErrors(async (a) => …)``). A wrapper's argument is taken only when it is a function
    literal, or a function declared in this file passed alone (``withErrors(namedHandler)``);
    otherwise (``createHandler(apiClient)``) the wrapper call is returned: the handler is built
    there and has no name."""
    for _ in range(4):
        if cb.type != "call_expression":
            return cb
        args_node = cb.child_by_field_name("arguments")
        args = [a for a in (args_node.named_children if args_node else []) if a.type != "comment"]
        literal = next((a for a in args if a.type in _FUNCTION_LITERALS), None)
        if literal is not None:
            return literal
        if len(args) != 1:
            return cb
        (arg,) = args
        if arg.type == "identifier" and function_line(arg, source, functions, scopes) is not None:
            return arg
        if arg.type != "call_expression":
            return cb
        cb = arg
    return cb


def _handler(
    cb: Node, source: bytes, functions: dict[str, int], scopes: Scopes
) -> tuple[str | None, int | None]:
    if cb.type == "identifier":
        return node_text(cb, source), function_line(cb, source, functions, scopes)
    if cb.type == "member_expression":
        name = node_text(cb, source)
        return name, functions.get(name)
    return inline_handler(cb, source)


def _template_names(root: Node, source: bytes) -> set[str]:
    """What the SDK's ``ResourceTemplate`` is called in this file (imported from the SDK's
    ``server/mcp.js``; ``import * as m`` → ``m.ResourceTemplate``)."""
    out: set[str] = set()
    for local, (module, export) in import_bindings(root, source).items():
        if module not in _MCP_MODULES:
            continue
        if export == "ResourceTemplate":
            out.add(local)
        elif export == "*":
            out.add(f"{local}.ResourceTemplate")
    return out


def _resource_uri(
    node: Node, source: bytes, templates: Callable[[], set[str]]
) -> tuple[str | None, bool]:
    """``(URI, is a template)``: a URI string literal, or the literal template of ``new
    ResourceTemplate("…", …)``."""
    if node.type in ("string", "template_string"):
        return _string(node, source), False
    if node.type != "new_expression":
        return None, False
    ctor = node.child_by_field_name("constructor")
    if ctor is None or node_text(ctor, source) not in templates():
        return None, False
    args_node = node.child_by_field_name("arguments")
    args = [a for a in (args_node.named_children if args_node else []) if a.type != "comment"]
    return (_string(args[0], source) if args else None), True


def _prompt_params(shape: Node | None, source: bytes, zod: set[str]) -> list[Parameter] | None:
    """The arguments of a prompt's zod shape: no type or default, as the SDK lists them."""
    if shape is None:
        return []
    return prompt_params(_zod_shape(shape, source, zod))


def _registration(
    method: str,
    args: list[Node],
    source: bytes,
    zod: set[str],
    templates: Callable[[], set[str]],
) -> tuple[str, str | None, list[Parameter] | None, str | None] | None:
    """``(endpoint, description, params, declared name)`` of one ``tool`` / ``resource`` /
    ``prompt`` call (or its ``register…`` form); None when it is not one or its name / URI is
    not a literal. The declared name is a resource's literal first argument (None otherwise: a
    tool's or prompt's name is its endpoint)."""
    middle = args[1:-1]
    description: str | None = None
    params: list[Parameter] | None = []
    if _REGISTRATIONS[method] == RESOURCE:
        if not middle:
            return None
        uri, template = _resource_uri(middle[0], source, templates)
        meta = middle[1:]
        if not uri or len(meta) > 1 or (method == "registerResource" and not meta):
            return None
        if meta and meta[0].type == "object":
            description = _string(_pairs(meta[0], source).get("description"), source)
        # A fixed resource's URI is literal, braces and all: it has no variables.
        params = uri_template_params(uri) if template else []
        return uri, description, params, _string(args[0], source) or None
    name = _string(args[0], source)
    if not name:
        return None
    if method in ("registerTool", "registerPrompt"):
        if not middle or middle[0].type != "object":
            return None
        config = _pairs(middle[0], source)
        description = _string(config.get("description"), source)
        schema = config.get("inputSchema" if method == "registerTool" else "argsSchema")
        if schema is not None:
            params = (
                _input_schema(schema, source, zod)
                if method == "registerTool"
                else _prompt_params(schema, source, zod)
            )
        elif not _literal_object(middle[0], source):
            params = None  # `{ ...base }` may carry the schema
        return name, description, params, None
    if middle and middle[0].type in ("string", "template_string"):
        description = _string(middle[0], source)
        middle = middle[1:]
    if method == "prompt":
        if len(middle) > 1:
            return None
        return name, description, _prompt_params(middle[0] if middle else None, source, zod), None
    return name, description, _tool_params(middle, source, zod), None


_TOP_LEVEL_FUNCTIONS = (
    "function_declaration",
    "generator_function_declaration",
    "arrow_function",
    "function_expression",
    "function",
    "generator_function",
)


def _top_level_function_lines(dc: DetectContext) -> dict[str, int]:
    """Name → line of the functions declared at the top of the file (``function f``, ``export
    function f``, ``const f = () => …``), for a handler passed by name. A function nested in
    another one is recorded with the file as its parent too, so the record alone cannot tell it
    from a top-level one; the tree can. A name the file imports is left out: the handler is the
    imported one, declared elsewhere."""
    top = top_level_declarations(dc.root, dc.source)
    imported = import_bindings(dc.root, dc.source)
    return {
        name: line
        for name, line in function_lines(dc.record).items()
        if name in top and top[name][1].type in _TOP_LEVEL_FUNCTIONS and name not in imported
    }


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    scopes = Scopes(dc.root, source)
    servers = _servers(dc.root, source, scopes)
    functions = _top_level_function_lines(dc)
    found: list[Emit] = guarded(
        dc.path,
        detect_lowlevel,
        dc.path,
        dc.root,
        source,
        servers,
        functions,
        scopes,
        lambda n: _json_schema(n, source),
    )
    if not servers and not found:
        return None
    zod = _zod_names(dc)
    template_names: list[set[str]] = []

    def templates() -> set[str]:
        if not template_names:
            template_names.append(_template_names(dc.root, source))
        return template_names[0]

    for call in _walk(dc.root):
        if call.type != "call_expression":
            continue
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            continue
        obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
        method = node_text(prop, source) if prop is not None else ""
        if (
            method not in _REGISTRATIONS
            or obj is None
            or receiver_binding(obj, source, scopes) not in servers
        ):
            continue
        args_node = call.child_by_field_name("arguments")
        args = [a for a in (args_node.named_children if args_node else []) if a.type != "comment"]
        if len(args) < 2 or args[-1].type not in (*_CALLBACKS, "call_expression"):
            continue
        callback = _unwrap(args[-1], source, functions, scopes)
        read = (
            _registration(method, args, source, zod, templates)
            if _REGISTRATIONS[method] == TOOL
            # A failing resource / prompt reader never drops the file's tools.
            else guarded_one(dc.path, _registration, method, args, source, zod, templates)
        )
        if read is None:
            continue
        endpoint, description, params, declared = read
        handler, handler_line = _handler(callback, source, functions, scopes)
        found.append(
            mcp_emit(
                call,
                source,
                kind=_REGISTRATIONS[method],
                endpoint=endpoint,
                framework=FRAMEWORK,
                handler=handler,
                handler_line=handler_line,
                description=description,
                params=params,
                name=declared,
            )
        )
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=FRAMEWORK)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="typescript-mcp",
        gate=Gate("typescript", order=90, guard=b"@modelcontextprotocol/sdk", skip_fixtures=True),
        detect=_detect,
        frameworks=(FRAMEWORK,),
    )
)
