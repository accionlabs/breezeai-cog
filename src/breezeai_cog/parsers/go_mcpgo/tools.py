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

Resources and prompts are read the same way, each at its constructor::

    s.AddResource(mcp.NewResource("docs://readme", "README",          → mcp_resource  mcp-go  RESOURCE  docs://readme
        mcp.WithResourceDescription("The README")), readmeHandler)
    s.AddResourceTemplate(mcp.NewResourceTemplate("users://{id}", "User"), userHandler)
                                                                    → mcp_resource  …  users://{id}  params id
    s.AddPrompt(mcp.NewPrompt("review",                               → mcp_prompt  mcp-go  PROMPT  review
        mcp.WithPromptDescription("Review code"),
        mcp.WithArgument("code", mcp.ArgumentDescription("The code"), mcp.RequiredArgument()),
    ), reviewHandler)

* ``endpoint`` — the URI / URI template / prompt name, a literal; anything else → no record.
* ``name`` — a resource's name (``NewResource`` / ``NewResourceTemplate``'s second argument) when
  a literal, else the URI.
* ``description`` — ``WithResourceDescription`` / ``WithTemplateDescription`` /
  ``WithPromptDescription``.
* ``params`` — a template's simple ``{variables}`` (None with an RFC 6570 operator); ``[]`` for a
  ``NewResource``, whose URI is fixed; a prompt's ``WithArgument`` options (``RequiredArgument()``,
  ``ArgumentDescription(…)``), None when an option is not one the reader knows.
* ``handler`` — the second argument of ``AddResource`` / ``AddResourceTemplate`` / ``AddPrompt``,
  or the ``Handler`` of ``server.ServerResource{Resource: …}`` /
  ``ServerResourceTemplate{Template: …}`` / ``ServerPrompt{Prompt: …}``, followed as for a tool.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Parameter
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..go_mcp_common import go_string as _string
from ..go_mcp_common import HandlerLines, package_names
from ..go_mcp_common import walk as _walk
from ..mcp_common import (
    PROMPT,
    RESOURCE,
    TOOL,
    function_lines,
    mcp_emit,
    param,
    uri_template_params,
)
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
#: Each constructor read here → (kind, the server method that registers it, the server package's
#: registration struct, that struct's field holding it).
_CONSTRUCTORS = {
    "NewTool": (TOOL, "AddTool", "ServerTool", "Tool"),
    "NewResource": (RESOURCE, "AddResource", "ServerResource", "Resource"),
    "NewResourceTemplate": (RESOURCE, "AddResourceTemplate", "ServerResourceTemplate", "Template"),
    "NewPrompt": (PROMPT, "AddPrompt", "ServerPrompt", "Prompt"),
}
_ADDERS = {add: ctor for ctor, (_, add, _, _) in _CONSTRUCTORS.items()}
_STRUCTS = {struct: (ctor, field) for ctor, (_, _, struct, field) in _CONSTRUCTORS.items()}
#: ``NewPrompt`` / ``NewResource…`` options known to add no argument.
_PROMPT_OPTIONS = frozenset({"WithPromptDescription", "WithPromptTitle", "WithPromptIcons"})
_ARGUMENT_OPTIONS = frozenset({"ArgumentDescription", "ArgumentTitle", "RequiredArgument"})
_RESOURCE_DESCRIPTIONS = frozenset({"WithResourceDescription", "WithTemplateDescription"})


def _package_names(root: Node, source: bytes, package: str = _PACKAGE) -> set[str]:
    """Local names of an mcp-go package (``mcp`` / ``server``, or an import alias)."""
    return package_names(root, source, package)


def _unwrap(handler: Node | None, source: bytes, pkgs: set[str]) -> Node | None:
    """``mcp.NewTypedToolHandler(fn)`` → ``fn``; any other node as is."""
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
) -> tuple[str | None, list[Parameter] | None]:
    """``(description, params)`` from ``NewTool``'s options. An option that is not an ``mcp``
    call (a helper, ``opts...``), sets a raw schema or is not a known non-parameter option
    may add params: params unknown (None), never a partial list."""
    description: str | None = None
    out: list[Parameter] = []
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


def _resource_description(options: list[Node], source: bytes, pkgs: set[str]) -> str | None:
    """``WithResourceDescription("…")`` / ``WithTemplateDescription("…")`` among the options."""
    for opt in options:
        call = _pkg_call(opt, source, pkgs)
        if call is not None and call[0] in _RESOURCE_DESCRIPTIONS and call[1]:
            return _string(call[1][0], source)
    return None


def _prompt_options(
    options: list[Node], source: bytes, pkgs: set[str]
) -> tuple[str | None, list[Parameter] | None]:
    """``(description, params)`` from ``NewPrompt``'s options: one param per
    ``WithArgument("name", …)``; an option the reader does not know may add one: params None."""
    description: str | None = None
    out: list[Parameter] = []
    unknown = False
    for opt in options:
        call = _pkg_call(opt, source, pkgs)
        if call is None or (call[0] != "WithArgument" and call[0] not in _PROMPT_OPTIONS):
            unknown = True
            continue
        name, args = call
        if name == "WithPromptDescription" and args:
            description = _string(args[0], source)
        elif name == "WithArgument":
            arg_name = _string(args[0], source) if args else None
            if not arg_name:
                unknown = True
                continue
            required, arg_desc = False, None
            for prop in args[1:]:
                pc = _pkg_call(prop, source, pkgs)
                if pc is None or pc[0] not in _ARGUMENT_OPTIONS:
                    unknown = True
                elif pc[0] == "RequiredArgument":
                    required = True
                elif pc[0] == "ArgumentDescription" and pc[1]:
                    arg_desc = _string(pc[1][0], source)
            out.append(param(arg_name, None, required=required, description=arg_desc))
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


def _server_struct(type_node: Node | None, source: bytes, server_pkgs: set[str]) -> str | None:
    """``server.ServerTool`` (``ServerResource``, …: the server package under any import name)
    → the constructor its entry is built with (``NewTool``, …); None for any other type."""
    if type_node is None or type_node.type != "qualified_type":
        return None
    pkg, name = type_node.child_by_field_name("package"), type_node.child_by_field_name("name")
    if pkg is None or name is None or node_text(pkg, source) not in server_pkgs:
        return None
    found = _STRUCTS.get(node_text(name, source))
    return found[0] if found is not None else None


def _entry_fields(body: Node, source: bytes, ctor: str) -> tuple[Node, Node] | None:
    """``{Tool: t, Handler: h}`` (``{Resource: r, …}``, …) → ``(t, h)``."""
    fields: dict[str, Node] = {}
    for el in body.named_children:
        parts = [c for c in el.named_children if c.type != "comment"]
        if el.type != "keyed_element" or len(parts) != 2:
            continue
        key, value = (p.named_children[0] if p.named_children else p for p in parts)
        fields[node_text(key, source)] = value
    entry, handler = fields.get(_CONSTRUCTORS[ctor][3]), fields.get("Handler")
    return (entry, handler) if entry is not None and handler is not None else None


def _server_entries(
    n: Node, source: bytes, server_pkgs: set[str]
) -> list[tuple[str, Node, Node, Node]]:
    """``server.ServerTool{Tool: t, Handler: h}`` → ``[("NewTool", literal, t, h)]``; a
    ``[]server.ServerTool{{Tool: …, Handler: …}, …}`` literal gives one per element (and the same
    for ``ServerResource`` / ``ServerResourceTemplate`` / ``ServerPrompt``)."""
    type_node, body = n.child_by_field_name("type"), n.child_by_field_name("body")
    if type_node is None or body is None:
        return []
    ctor = _server_struct(type_node, source, server_pkgs)
    if ctor is not None:
        pair = _entry_fields(body, source, ctor)
        return [(ctor, n, *pair)] if pair is not None else []
    element = type_node.child_by_field_name("element")
    if type_node.type not in ("slice_type", "array_type", "implicit_length_array_type"):
        return []
    if element is not None and element.type == "pointer_type" and element.named_children:
        element = element.named_children[0]
    ctor = _server_struct(element, source, server_pkgs)
    if ctor is None:
        return []
    out: list[tuple[str, Node, Node, Node]] = []
    for el in body.named_children:
        inner = el.named_children[0] if el.type == "literal_element" and el.named_children else None
        if inner is not None and inner.type == "literal_value":  # element type elided
            pair = _entry_fields(inner, source, ctor)
            if pair is not None:
                out.append((ctor, inner, *pair))
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
    # A registration — `AddTool(tool, h)` or `server.ServerTool{Tool: tool, Handler: h}` (and
    # the resource / prompt forms) — of a `NewTool(…)` call inline: the call's start byte →
    # handler node.
    inline: dict[int, Node] = {}
    # … of a variable: (constructor, name) → [(start byte, handler, enclosing function spans)].
    by_var: dict[tuple[str, str], list[tuple[int, Node, list[Span]]]] = {}
    # Assignments: (scope, name) → start bytes; declarations: name → function spans.
    assigns: dict[tuple[Span, str], list[int]] = {}
    declared: dict[str, set[Span]] = {}
    assigned: dict[int, str] = {}  # NewTool call start byte → variable name

    def register(ctor: str, at: Node, entry: Node, handler: Node) -> None:
        if entry.type == "identifier":
            by_var.setdefault((ctor, node_text(entry, source)), []).append(
                (at.start_byte, handler, _chain(at))
            )
        else:
            inline[entry.start_byte] = handler

    for n in nodes:
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            field = (
                fn.child_by_field_name("field")
                if fn is not None and fn.type == "selector_expression"
                else None
            )
            args = n.child_by_field_name("arguments")
            adder = node_text(field, source) if field is not None else ""
            if adder in _ADDERS and args is not None:
                a = [c for c in args.named_children if c.type != "comment"]
                if len(a) >= 2:
                    register(_ADDERS[adder], n, a[0], a[1])
        elif n.type == "composite_literal" and server_pkgs:
            for ctor, at, entry_node, handler_node in _server_entries(n, source, server_pkgs):
                register(ctor, at, entry_node, handler_node)
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

    def handler_of(call: Node, ctor: str, var: str) -> Node | None:
        """The handler registered for the entry ``var`` holds from ``call`` on."""
        scope = _scope(call, root_span)
        regs = by_var.get((ctor, var), [])
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

    functions = HandlerLines(source, function_lines(dc.record))
    wrapper_pkgs = pkgs | server_pkgs
    found: list[Emit] = []
    for n in nodes:
        call = _pkg_call(n, source, pkgs)
        if call is None or call[0] not in _CONSTRUCTORS or not call[1]:
            continue
        ctor = call[0]
        name = _string(call[1][0], source)
        if not name:
            continue
        resource_name: str | None = None  # a resource's own literal name
        if ctor == "NewTool":
            description, params = _params(call[1][1:], source, pkgs)
        elif ctor == "NewPrompt":
            description, params = _prompt_options(call[1][1:], source, pkgs)
        else:
            # `NewResource(uri, name, opts…)`: a fixed URI, braces and all, has no variables.
            description = _resource_description(call[1][2:], source, pkgs)
            params = uri_template_params(name) if ctor == "NewResourceTemplate" else []
            resource_name = _string(call[1][1], source) if len(call[1]) > 1 else None
        var = assigned.get(n.start_byte)
        h = handler_of(n, ctor, var) if var is not None else inline.get(n.start_byte)
        h = _unwrap(h, source, wrapper_pkgs)
        handler, handler_line = None, None
        if h is not None and h.type == "identifier":
            handler = node_text(h, source)
            handler_line = functions.line(h)
        elif h is not None and h.type == "func_literal":
            handler_line = h.start_point[0] + 1
        elif h is not None and h.type == "selector_expression":
            handler = node_text(h, source)
        found.append(
            mcp_emit(
                n,
                source,
                kind=_CONSTRUCTORS[ctor][0],
                endpoint=name,
                framework=FRAMEWORK,
                handler=handler,
                handler_line=handler_line,
                description=description,
                params=params,
                name=resource_name,
            )
        )
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=FRAMEWORK)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="go-mcpgo",
        gate=Gate("go", order=130, guard=b"mark3labs/mcp-go", skip_fixtures=True),
        detect=_detect,
        frameworks=(FRAMEWORK,),
    )
)
