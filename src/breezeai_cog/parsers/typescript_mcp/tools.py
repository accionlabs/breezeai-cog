"""Additive detector: tools of a TypeScript / JavaScript MCP server (``@modelcontextprotocol/sdk``).

::

    const server = new McpServer({ name: "x" })     # or a parameter typed McpServer
    server.tool("add", "Add two numbers",           → mcp_tool  mcp-sdk-ts  TOOL  add
                { a: z.number().describe("first"), b: z.number().optional() },
                async ({ a, b }) => …)
    server.registerTool("echo",                     → mcp_tool  mcp-sdk-ts  TOOL  echo
                { description: "Echo", inputSchema: { msg: z.string() } }, echoHandler)

* **Server receiver** — the call's object must be a name (or ``this.x``) assigned
  ``new McpServer(…)`` in this file, or a parameter typed ``McpServer``.
* ``tool(name, [description], [paramsShape], [annotations], callback)`` — every overload: the
  description is the string after the name; an object is the params shape only when its values
  are zod chains, so an annotations object (``{ readOnlyHint: true }``) is never read as params.
* ``registerTool(name, { description, inputSchema }, callback)`` — ``inputSchema`` a zod raw
  shape, ``z.object({…})``, or a JSON-schema literal (``properties`` / ``required``).
* ``handler`` — a named callback (its line when declared in this file); an inline callback has
  no name, only its ``handlerLine``.
* A non-literal tool name → no record.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import ToolParam
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..mcp_common import param, tool_emit
from ..treesitter import node_text

FRAMEWORK = "mcp-sdk-ts"
_CALLBACKS = (
    "arrow_function",
    "function_expression",
    "function",
    "identifier",
    "member_expression",
)
_MAX_DEFAULT = 200


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


def _servers(root: Node, source: bytes) -> set[str]:
    """Receivers proven to be an ``McpServer``."""
    out: set[str] = set()
    for node in _walk(root):
        if node.type == "new_expression":
            ctor = node.child_by_field_name("constructor")
            if ctor is None or node_text(ctor, source).rsplit(".", 1)[-1] != "McpServer":
                continue
            parent = node.parent
            if parent is not None and parent.type == "variable_declarator":
                name = parent.child_by_field_name("name")
                if name is not None and name.type == "identifier":
                    out.add(node_text(name, source))
            elif parent is not None and parent.type == "assignment_expression":
                left = parent.child_by_field_name("left")
                if left is not None:
                    out.add(node_text(left, source))
        elif node.type in ("required_parameter", "optional_parameter", "public_field_definition"):
            ann = node.child_by_field_name("type")
            name = node.child_by_field_name("pattern") or node.child_by_field_name("name")
            if (
                ann is not None
                and name is not None
                and node_text(ann, source).lstrip(": ").rsplit(".", 1)[-1] == "McpServer"
            ):
                text = node_text(name, source)
                out.add(text)
                if node.type == "public_field_definition":
                    out.add(f"this.{text}")
    return out


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


def _zod_param(name: str, value: Node, source: bytes, zod: set[str]) -> ToolParam | None:
    chain = _chain(value, source, zod)
    if not chain:
        return None
    base, base_args = chain[0]
    type_ = base
    if base == "enum" and base_args and base_args[0].type == "array":
        vals = [_string(v, source) for v in base_args[0].named_children]
        if all(v is not None for v in vals):
            type_ = f"enum({','.join(v for v in vals if v is not None)})"
    required, default, description = True, None, None
    for method, args in chain[1:]:
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


def _zod_shape(obj: Node, source: bytes, zod: set[str]) -> list[ToolParam] | None:
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


def _input_schema(node: Node, source: bytes, zod: set[str]) -> list[ToolParam] | None:
    shape = _zod_shape(node, source, zod)
    if shape is not None:
        return shape
    chain = _chain(node, source, zod) if node.type == "call_expression" else None
    if chain and chain[0][0] == "object" and chain[0][1]:
        return _zod_shape(chain[0][1][0], source, zod)
    if node.type == "object":  # JSON schema literal
        pairs = _pairs(node, source)
        props = pairs.get("properties")
        if props is None or props.type != "object":
            return None
        req = pairs.get("required")
        required = (
            {(_string(v, source) or "") for v in req.named_children}
            if req is not None and req.type == "array"
            else set()
        )
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
    return None


def _handler(cb: Node, source: bytes, functions: dict[str, int]) -> tuple[str | None, int | None]:
    if cb.type in ("identifier", "member_expression"):
        name = node_text(cb, source)
        return name, functions.get(name)
    return None, cb.start_point[0] + 1


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    servers = _servers(dc.root, source)
    if not servers:
        return None
    zod = _zod_names(dc)
    functions = {f.name: f.startLine for f in dc.record.functions}
    found: list[Emit] = []
    for call in _walk(dc.root):
        if call.type != "call_expression":
            continue
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            continue
        obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
        method = node_text(prop, source) if prop is not None else ""
        if (
            method not in ("tool", "registerTool")
            or obj is None
            or node_text(obj, source) not in servers
        ):
            continue
        args_node = call.child_by_field_name("arguments")
        args = [a for a in (args_node.named_children if args_node else []) if a.type != "comment"]
        if len(args) < 2 or args[-1].type not in _CALLBACKS:
            continue
        name = _string(args[0], source)
        if not name:
            continue
        description: str | None = None
        params: list[ToolParam] = []
        middle = args[1:-1]
        if method == "registerTool":
            if not middle or middle[0].type != "object":
                continue
            config = _pairs(middle[0], source)
            description = _string(config.get("description"), source)
            schema = config.get("inputSchema")
            params = (_input_schema(schema, source, zod) or []) if schema is not None else []
        else:
            if middle and middle[0].type in ("string", "template_string"):
                description = _string(middle[0], source)
                middle = middle[1:]
            shape = next(
                (s for s in (_zod_shape(m, source, zod) for m in middle) if s is not None), None
            )
            params = shape or []
        handler, handler_line = _handler(args[-1], source, functions)
        found.append(
            tool_emit(
                call,
                source,
                endpoint=name,
                framework=FRAMEWORK,
                handler=handler,
                handler_line=handler_line,
                description=description,
                params=params,
                node_type=None,
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
