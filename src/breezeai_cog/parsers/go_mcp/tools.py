"""Additive detector: tools of a Go MCP server built with ``github.com/mark3labs/mcp-go``.

::

    tool := mcp.NewTool("hello",                                  → mcp_tool  mcp-go  TOOL  hello
        mcp.WithDescription("Say hello"),
        mcp.WithString("name", mcp.Required(), mcp.Description("Who to greet")),
        mcp.WithNumber("times", mcp.DefaultNumber(1)),
    )
    s.AddTool(tool, helloHandler)                                   handler helloHandler

* The statement sits on ``mcp.NewTool(…)`` (the ``mcp`` package of mcp-go, under any import
  name); a non-literal name → no record.
* ``params`` — ``WithString`` / ``WithNumber`` / ``WithBoolean`` / ``WithArray`` / ``WithObject``
  / ``WithInteger``, with ``Required()``, ``Description(…)`` and ``Default*(…)``.
* ``handler`` — the second argument of the ``AddTool`` call, or the ``Handler`` of the
  ``server.ServerTool{Tool: …, Handler: …}`` literal (also as an element of a
  ``[]server.ServerTool{…}``), that registers the tool (inline, or
  through the variable it was assigned to: in the same function before the variable is
  reassigned; a package-level variable only from a function that does not shadow it, and only
  when exactly one such registration exists); a function literal gives only its line.
  ``mcp.NewTypedToolHandler(fn)`` (or ``server.``) is looked through to ``fn``.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import ToolParam
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..mcp_common import param, tool_emit
from ..treesitter import node_text

FRAMEWORK = "mcp-go"
_PACKAGE = "github.com/mark3labs/mcp-go/mcp"
_SERVER_PACKAGE = "github.com/mark3labs/mcp-go/server"
#: Wrappers that only adapt a typed handler's signature: the handler is their argument.
_HANDLER_WRAPPERS = frozenset({"NewTypedToolHandler"})
_PARAM_TYPES = {
    "WithString": "string",
    "WithNumber": "number",
    "WithBoolean": "boolean",
    "WithArray": "array",
    "WithObject": "object",
    "WithInteger": "integer",
}
#: ``NewTool`` options known to add no parameter: description, title, annotations and the
#: output schema. Any other option (``WithInputSchema``, ``WithRawInputSchema``, an option this
#: list does not know) may add one, so the params are unknown.
_NON_PARAM_OPTIONS = frozenset(
    {
        "WithDescription",
        "WithTitle",
        "WithTitleAnnotation",
        "WithToolAnnotation",
        "WithReadOnlyHintAnnotation",
        "WithDestructiveHintAnnotation",
        "WithIdempotentHintAnnotation",
        "WithOpenWorldHintAnnotation",
        "WithOutputSchema",
        "WithRawOutputSchema",
    }
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
    if node is None:
        return None
    if node.type == "interpreted_string_literal":
        return node_text(node, source)[1:-1]
    if node.type == "raw_string_literal":
        return node_text(node, source)[1:-1]
    return None


def _package_names(root: Node, source: bytes, package: str = _PACKAGE) -> set[str]:
    """Local names of an mcp-go package (``mcp`` / ``server``, or an import alias)."""
    out: set[str] = set()
    for spec in _walk(root):
        if spec.type != "import_spec":
            continue
        path = _string(spec.child_by_field_name("path"), source)
        if path != package:
            continue
        alias = spec.child_by_field_name("name")
        out.add(node_text(alias, source) if alias is not None else package.rsplit("/", 1)[-1])
    return out


def _unwrap(handler: Node | None, source: bytes, pkgs: set[str]) -> Node | None:
    """``mcp.NewTypedToolHandler(fn)`` → ``fn`` (BREEZEAI-1457); any other node as is."""
    call = _pkg_call(handler, source, pkgs) if handler is not None else None
    if call is not None and call[0] in _HANDLER_WRAPPERS and len(call[1]) == 1:
        return call[1][0]
    return handler


def _pkg_call(node: Node, source: bytes, pkgs: set[str]) -> tuple[str, list[Node]] | None:
    """``mcp.X(args…)`` (or ``mcp.X[T](args…)``) → ``("X", args)``."""
    if node.type != "call_expression":
        return None
    fn, args = node.child_by_field_name("function"), node.child_by_field_name("arguments")
    if fn is not None and fn.type in ("index_expression", "generic_type"):
        # A generic option, ``mcp.WithOutputSchema[Out]()``.
        fn = fn.child_by_field_name("operand") or fn.child_by_field_name("type")
    if fn is None or fn.type != "selector_expression":
        return None
    operand, field = fn.child_by_field_name("operand"), fn.child_by_field_name("field")
    if operand is None or field is None or node_text(operand, source) not in pkgs:
        return None
    return node_text(field, source), [
        a for a in (args.named_children if args else []) if a.type != "comment"
    ]


def _params(
    options: list[Node], source: bytes, pkgs: set[str]
) -> tuple[str | None, list[ToolParam] | None]:
    """``(description, params)`` from ``NewTool``'s options. An option that is not an ``mcp``
    call (a helper, ``opts...``), sets a raw schema or is not a known non-parameter option
    may add params: params unknown (None), never a partial list."""
    description: str | None = None
    out: list[ToolParam] = []
    unknown = False
    for opt in options:
        call = _pkg_call(opt, source, pkgs)
        if call is None:
            unknown = True
            continue
        name, args = call
        if name not in _PARAM_TYPES and name not in _NON_PARAM_OPTIONS:
            unknown = True
        if name == "WithDescription" and args:
            description = _string(args[0], source)
        elif name in _PARAM_TYPES and args:
            pname = _string(args[0], source)
            if not pname:
                continue
            required, pdesc, default = False, None, None
            for prop in args[1:]:
                pc = _pkg_call(prop, source, pkgs)
                if pc is None:
                    continue
                if pc[0] == "Required":
                    required = True
                elif pc[0] == "Description" and pc[1]:
                    pdesc = _string(pc[1][0], source)
                elif pc[0].startswith("Default") and pc[1]:
                    text = " ".join(node_text(pc[1][0], source).split())
                    default = text if len(text) <= _MAX_DEFAULT else text[: _MAX_DEFAULT - 1] + "…"
            out.append(
                param(
                    pname, _PARAM_TYPES[name], required=required, default=default, description=pdesc
                )
            )
    return description, None if unknown else out


_SCOPES = ("function_declaration", "method_declaration", "func_literal")
_DECLARATIONS = ("short_var_declaration", "var_spec", "assignment_statement", "range_clause")

Span = tuple[int, int]


def _chain(node: Node) -> list[Span]:
    """The byte spans of the functions enclosing ``node``, innermost first (none at top
    level)."""
    out: list[Span] = []
    cur = node.parent
    while cur is not None:
        if cur.type in _SCOPES:
            out.append((cur.start_byte, cur.end_byte))
        cur = cur.parent
    return out


def _scope(node: Node, root_span: Span) -> Span:
    """The byte span of the innermost function enclosing ``node`` (the file at top level)."""
    chain = _chain(node)
    return chain[0] if chain else root_span


def _targets(n: Node) -> list[tuple[Node, Node | None]]:
    """``(name, value)`` pairs a declaration or assignment binds (value None when the sides do
    not line up one to one, as in ``a, b := f()`` or a ``range`` clause)."""
    left = n.child_by_field_name("left") or n.child_by_field_name("name")
    right = n.child_by_field_name("right") or n.child_by_field_name("value")
    lnames = [
        c
        for c in (
            left.named_children if left is not None and left.type == "expression_list" else [left]
        )
        if c is not None and c.type == "identifier"
    ]
    if n.type == "var_spec":  # `var a, b = …`: every name child is a binding
        lnames = [c for c in n.children_by_field_name("name") if c.type == "identifier"]
    rvals = (
        right.named_children if right is not None and right.type == "expression_list" else [right]
    )
    if n.type == "range_clause" or len(rvals) != len(lnames):
        return [(ln, None) for ln in lnames]
    return list(zip(lnames, rvals))


def _is_server_tool(type_node: Node | None, source: bytes, server_pkgs: set[str]) -> bool:
    """True for the type ``server.ServerTool`` (the server package under any import name)."""
    if type_node is None or type_node.type != "qualified_type":
        return False
    pkg, name = type_node.child_by_field_name("package"), type_node.child_by_field_name("name")
    return (
        pkg is not None
        and name is not None
        and node_text(pkg, source) in server_pkgs
        and node_text(name, source) == "ServerTool"
    )


def _tool_fields(body: Node, source: bytes) -> tuple[Node, Node] | None:
    """``{Tool: t, Handler: h}`` → ``(t, h)``."""
    fields: dict[str, Node] = {}
    for el in body.named_children:
        parts = [c for c in el.named_children if c.type != "comment"]
        if el.type != "keyed_element" or len(parts) != 2:
            continue
        key, value = (p.named_children[0] if p.named_children else p for p in parts)
        fields[node_text(key, source)] = value
    tool, handler = fields.get("Tool"), fields.get("Handler")
    return (tool, handler) if tool is not None and handler is not None else None


def _server_tools(n: Node, source: bytes, server_pkgs: set[str]) -> list[tuple[Node, Node, Node]]:
    """``server.ServerTool{Tool: t, Handler: h}`` → ``[(literal, t, h)]``; a
    ``[]server.ServerTool{{Tool: …, Handler: …}, …}`` literal gives one per element."""
    type_node, body = n.child_by_field_name("type"), n.child_by_field_name("body")
    if type_node is None or body is None:
        return []
    if _is_server_tool(type_node, source, server_pkgs):
        pair = _tool_fields(body, source)
        return [(n, *pair)] if pair is not None else []
    element = type_node.child_by_field_name("element")
    if type_node.type not in ("slice_type", "array_type", "implicit_length_array_type"):
        return []
    if element is not None and element.type == "pointer_type" and element.named_children:
        element = element.named_children[0]
    if not _is_server_tool(element, source, server_pkgs):
        return []
    out: list[tuple[Node, Node, Node]] = []
    for el in body.named_children:
        inner = el.named_children[0] if el.type == "literal_element" and el.named_children else None
        if inner is not None and inner.type == "literal_value":  # element type elided
            pair = _tool_fields(inner, source)
            if pair is not None:
                out.append((inner, *pair))
    return out


def _one_handler(regs: list[tuple[int, Node]], source: bytes) -> Node | None:
    """The handler of a tool registered by ``regs``; None when they disagree."""
    if not regs or len({node_text(h, source) for _, h in regs}) != 1:
        return None
    return regs[0][1]


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    pkgs = _package_names(dc.root, source)
    if not pkgs:
        return None
    server_pkgs = _package_names(dc.root, source, _SERVER_PACKAGE)
    root_span = (dc.root.start_byte, dc.root.end_byte)
    nodes = _walk(dc.root)
    # A registration — `AddTool(tool, h)` or `server.ServerTool{Tool: tool, Handler: h}` —
    # of a `NewTool(…)` call inline: the call's start byte → handler node.
    inline: dict[int, Node] = {}
    # … of a variable: name → [(start byte, handler, enclosing function spans)].
    by_var: dict[str, list[tuple[int, Node, list[Span]]]] = {}
    # Assignments: (scope, name) → start bytes; declarations: name → function spans.
    assigns: dict[tuple[Span, str], list[int]] = {}
    declared: dict[str, set[Span]] = {}
    assigned: dict[int, str] = {}  # NewTool call start byte → variable name

    def register(at: Node, tool: Node, handler: Node) -> None:
        if tool.type == "identifier":
            by_var.setdefault(node_text(tool, source), []).append(
                (at.start_byte, handler, _chain(at))
            )
        else:
            inline[tool.start_byte] = handler

    for n in nodes:
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            field = (
                fn.child_by_field_name("field")
                if fn is not None and fn.type == "selector_expression"
                else None
            )
            args = n.child_by_field_name("arguments")
            if field is not None and node_text(field, source) == "AddTool" and args is not None:
                a = [c for c in args.named_children if c.type != "comment"]
                if len(a) >= 2:
                    register(n, a[0], a[1])
        elif n.type == "composite_literal" and server_pkgs:
            for at, tool_node, handler_node in _server_tools(n, source, server_pkgs):
                register(at, tool_node, handler_node)
        elif n.type == "parameter_declaration":
            chain = _chain(n)
            for c in n.children_by_field_name("name"):
                if chain:
                    declared.setdefault(node_text(c, source), set()).add(chain[0])
        elif n.type in _DECLARATIONS:
            scope = _scope(n, root_span)
            for ln, rv in _targets(n):
                vname = node_text(ln, source)
                assigns.setdefault((scope, vname), []).append(n.start_byte)
                if n.type != "assignment_statement" and scope != root_span:
                    declared.setdefault(vname, set()).add(scope)
                if rv is not None and rv.type == "call_expression":
                    assigned[rv.start_byte] = vname

    def handler_of(call: Node, var: str) -> Node | None:
        """The handler registered for the tool ``var`` holds from ``call`` on."""
        scope = _scope(call, root_span)
        regs = by_var.get(var, [])
        if scope != root_span:
            # The registrations in the same function after the assignment and before the
            # next assignment to the variable, which registers another tool.
            nxt = min(
                (a for a in assigns.get((scope, var), []) if a > call.start_byte), default=None
            )
            window = [
                (at, h)
                for at, h, chain in regs
                if chain
                and chain[0] == scope
                and at > call.start_byte
                and (nxt is None or at < nxt)
            ]
            return _one_handler(window, source)
        # A package-level variable is registered inside a function: not one that declares a
        # variable of that name (which shadows it), and only when nothing reassigns it and
        # exactly one registration remains.
        shadowing = declared.get(var, set())
        writes = sum(
            len(at)
            for (sc, name), at in assigns.items()
            if name == var and (sc == root_span or sc not in shadowing)
        )
        candidates = [(at, h) for at, h, chain in regs if not any(sc in shadowing for sc in chain)]
        return candidates[0][1] if writes == 1 and len(candidates) == 1 else None

    functions = {f.name: f.startLine for f in dc.record.functions}
    wrapper_pkgs = pkgs | server_pkgs
    found: list[Emit] = []
    for n in nodes:
        call = _pkg_call(n, source, pkgs)
        if call is None or call[0] != "NewTool" or not call[1]:
            continue
        name = _string(call[1][0], source)
        if not name:
            continue
        description, params = _params(call[1][1:], source, pkgs)
        var = assigned.get(n.start_byte)
        h = handler_of(n, var) if var is not None else inline.get(n.start_byte)
        h = _unwrap(h, source, wrapper_pkgs)
        handler, handler_line = None, None
        if h is not None and h.type == "identifier":
            handler = node_text(h, source)
            handler_line = functions.get(handler)
        elif h is not None and h.type == "func_literal":
            handler_line = h.start_point[0] + 1
        elif h is not None and h.type == "selector_expression":
            handler = node_text(h, source)
        found.append(
            tool_emit(
                n,
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
        name="go-mcp",
        gate=Gate("go", order=130, guard=b"mark3labs/mcp-go", skip_fixtures=True),
        detect=_detect,
        frameworks=(FRAMEWORK,),
    )
)
