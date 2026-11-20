"""Additive detector: tools of a Go MCP server built with the official SDK
(``github.com/modelcontextprotocol/go-sdk``).

::

    server := mcp.NewServer(&mcp.Implementation{Name: "greeter"}, nil)   # or: s *mcp.Server
    type args struct {
        Name string `json:"name" jsonschema:"the person to greet"`
    }
    mcp.AddTool(server, &mcp.Tool{Name: "greet", Description: "say hi"}, SayHi)
                                                → mcp_tool  go-mcp  TOOL  greet   handler SayHi
    server.AddTool(&mcp.Tool{Name: "raw", InputSchema: &jsonschema.Schema{…}}, rawHandler)
                                                → mcp_tool  go-mcp  TOOL  raw

* The statement sits on the ``AddTool`` call that registers the tool: the generic
  ``mcp.AddTool(server, tool, handler)`` (the SDK's own function: its first argument is a
  server by its type), or ``<server>.AddTool(tool, handler)`` on a name assigned
  ``mcp.NewServer(…)`` or a parameter typed ``*mcp.Server`` (the nearest binding of the name
  decides). ``mcp`` is the SDK's package under any import name.
* The tool is a ``&mcp.Tool{…}`` literal, inline or a variable of the same function that holds
  one (``t := &mcp.Tool{…}`` passed as ``t``; ``t := mcp.Tool{…}`` passed as ``&t``), only when
  the variable is declared once and used nowhere else in the function than as an ``AddTool``
  tool argument: reassigned, aliased (``p := t``), passed to another call, dereferenced
  (``*t = …``) or a field written (``t.Name = …``) → no record.
* ``endpoint`` — ``Name``, a string literal; anything else → no record. ``description`` —
  ``Description`` when a literal.
* ``params`` — from a literal ``InputSchema: &jsonschema.Schema{Type, Properties:
  map[string]*jsonschema.Schema{"a": {Type, Description}}, Required: []string{…}}``; else, for
  the generic ``mcp.AddTool``, from the input struct the SDK infers the schema from (the type
  argument ``AddTool[In, Out]``, or the handler's third parameter), declared in this file: one
  param per field, named by its ``json`` tag, required unless ``omitempty`` / ``omitzero``,
  described by its ``jsonschema`` tag. A field the reader cannot follow (``json:"-"``,
  unexported, embedded) or a type from elsewhere → None (unknown).
* ``handler`` — a function name (its line when declared in this file), a method value's text,
  or a function literal's line.
"""

from __future__ import annotations

import bisect
import re

from tree_sitter import Node

from ...schemas import Parameter
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..go_mcp_common import HandlerLines, go_string, package_names, walk
from ..mcp_common import function_lines, param, schema_params, tool_emit
from ..treesitter import node_text

FRAMEWORK = "go-mcp"
_PACKAGE = "github.com/modelcontextprotocol/go-sdk/mcp"
_SCOPES = ("function_declaration", "method_declaration", "func_literal")
_DECLARATIONS = ("short_var_declaration", "var_spec", "assignment_statement", "range_clause")
#: Implicit blocks besides ``{…}``: an ``if`` / ``for`` / ``switch`` (its init statement, a
#: type-switch alias) and each ``case`` / ``default`` clause of a ``switch`` or ``select``.
_BLOCKS = frozenset(
    {
        "block",
        "if_statement",
        "for_statement",
        "expression_switch_statement",
        "type_switch_statement",
        "select_statement",
        "expression_case",
        "type_case",
        "default_case",
        "communication_case",
    }
)
_TAG = re.compile(r'(\w+):"((?:[^"\\]|\\.)*)"')
_INTEGERS = frozenset(
    {
        "int",
        "int8",
        "int16",
        "int32",
        "int64",
        "uint",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "uintptr",
    }
)

Span = tuple[int, int]


def _span(node: Node | None) -> Span:
    return (node.start_byte, node.end_byte) if node is not None else (-1, -1)


def _scope_of(node: Node) -> Node | None:
    """The innermost function enclosing ``node``; None at package level."""
    cur = node.parent
    while cur is not None:
        if cur.type in _SCOPES:
            return cur
        cur = cur.parent
    return None


def _block_of(node: Node) -> Node | None:
    """The innermost block (an implicit one included) or function enclosing ``node``, where a
    name it declares is bound; None at package level."""
    cur = node.parent
    while cur is not None:
        if cur.type in _SCOPES or cur.type in _BLOCKS:
            return cur
        cur = cur.parent
    return None


def _pkg_selector(node: Node | None, source: bytes, pkgs: set[str], name: str) -> bool:
    """``node`` is ``<pkg>.<name>`` (a selector, or a qualified type)."""
    if node is None:
        return False
    if node.type == "selector_expression":
        operand, field = node.child_by_field_name("operand"), node.child_by_field_name("field")
    elif node.type == "qualified_type":
        operand, field = node.child_by_field_name("package"), node.child_by_field_name("name")
    else:
        return False
    return (
        operand is not None
        and field is not None
        and node_text(operand, source) in pkgs
        and node_text(field, source) == name
    )


def _callee(call: Node) -> Node | None:
    fn = call.child_by_field_name("function")
    if fn is not None and fn.type in ("index_expression", "generic_type"):
        fn = fn.child_by_field_name("operand") or fn.child_by_field_name("type")
    return fn


def _args(node: Node | None) -> list[Node]:
    return [a for a in (node.named_children if node is not None else []) if a.type != "comment"]


def _declares(n: Node) -> bool:
    """A ``range`` clause or ``select`` receive declares its names (``:=``) rather than
    assigning existing ones (``=``)."""
    return any(c.type == ":=" for c in n.children)


def _receive_names(n: Node) -> list[Node]:
    """The names a ``select`` receive (``case s := <-ch``) or a type-switch alias (``switch s
    := v.(type)``) binds."""
    left = n.child_by_field_name("left" if n.type == "receive_statement" else "alias")
    targets = left.named_children if left is not None and left.type == "expression_list" else [left]
    return [t for t in targets if t is not None and t.type == "identifier"]


def _targets(n: Node) -> list[tuple[Node, Node | None]]:
    """``(name, value)`` pairs a declaration or assignment binds; value None when the sides do
    not line up one to one."""
    left = n.child_by_field_name("left") or n.child_by_field_name("name")
    right = n.child_by_field_name("right") or n.child_by_field_name("value")
    lnames = [
        c
        for c in (
            left.named_children if left is not None and left.type == "expression_list" else [left]
        )
        if c is not None and c.type == "identifier"
    ]
    if n.type == "var_spec":
        lnames = [c for c in n.children_by_field_name("name") if c.type == "identifier"]
    rvals = (
        right.named_children if right is not None and right.type == "expression_list" else [right]
    )
    if n.type == "range_clause" or len(rvals) != len(lnames):
        return [(ln, None) for ln in lnames]
    return list(zip(lnames, rvals))


def _unwrap_ref(node: Node | None) -> tuple[bool, Node | None]:
    """``&x`` → ``(True, x)``; anything else → ``(False, node)``."""
    if node is not None and node.type == "unary_expression":
        op = node.children[0] if node.children else None
        if op is not None and op.type == "&":
            return True, node.child_by_field_name("operand")
    return False, node


def _keyed(body: Node | None, source: bytes) -> dict[str, Node] | None:
    """``{Key: value, …}`` → ``{"Key": value}``; None when an element is not keyed."""
    if body is None or body.type != "literal_value":
        return None
    out: dict[str, Node] = {}
    for el in _args(body):
        if el.type != "keyed_element":
            return None
        key, value = el.child_by_field_name("key"), el.child_by_field_name("value")
        if key is None or value is None:
            parts = _args(el)
            if len(parts) != 2:
                return None
            key, value = parts
        k = key.named_children[0] if key.type == "literal_element" and key.named_children else key
        v = (
            value.named_children[0]
            if value.type == "literal_element" and value.named_children
            else value
        )
        out[go_string(k, source) or node_text(k, source)] = v
    return out


def _schema_dict(node: Node | None, source: bytes) -> dict[str, object] | None:
    """A ``&jsonschema.Schema{…}`` / ``jsonschema.Schema{…}`` / elided ``{…}`` literal as JSON
    schema values (``type``, ``description``, ``properties``, ``required``); None when one of
    those is not a literal."""
    _, node = _unwrap_ref(node)
    if node is None:
        return None
    if node.type == "composite_literal":
        type_node = node.child_by_field_name("type")
        if type_node is None or not node_text(type_node, source).endswith("Schema"):
            return None
        node = node.child_by_field_name("body")
    fields = _keyed(node, source)
    if fields is None:
        return None
    out: dict[str, object] = {}
    for key, json_key in (("Type", "type"), ("Description", "description")):
        if key in fields:
            value = go_string(fields[key], source)
            if value is None:
                return None
            out[json_key] = value
    if "Properties" in fields:
        props_node = fields["Properties"]
        if props_node.type != "composite_literal":
            return None
        props = _keyed(props_node.child_by_field_name("body"), source)
        if props is None:
            return None
        properties: dict[str, object] = {}
        for name, spec in props.items():
            inner = _schema_dict(spec, source)
            if inner is None:
                return None
            properties[name] = inner
        out["properties"] = properties
    if "Required" in fields:
        req = fields["Required"]
        items = _args(req.child_by_field_name("body")) if req.type == "composite_literal" else None
        if items is None:
            return None
        names = [go_string(i.named_children[0] if i.named_children else i, source) for i in items]
        if any(n is None for n in names):
            return None
        out["required"] = names
    return out


def _json_type(type_node: Node, source: bytes) -> str | None:
    """The JSON-schema type the SDK infers for a Go field type; None when not a builtin."""
    t = type_node
    while t.type == "pointer_type" and t.named_children:
        t = t.named_children[0]
    text = node_text(t, source)
    if t.type in ("slice_type", "array_type"):
        return None if text.endswith("byte") else "array"
    if t.type == "map_type":
        return "object"
    if text == "string":
        return "string"
    if text == "bool":
        return "boolean"
    if text in _INTEGERS:
        return "integer"
    if text in ("float32", "float64"):
        return "number"
    return None


def _struct_params(struct: Node, source: bytes) -> list[Parameter] | None:
    """Params of the input struct the SDK infers the schema from; None for a field it cannot
    follow."""
    fields = next((c for c in struct.named_children if c.type == "field_declaration_list"), None)
    out: list[Parameter] = []
    for decl in _args(fields):
        if decl.type != "field_declaration":
            return None
        names = decl.children_by_field_name("name")
        type_node = decl.child_by_field_name("type")
        if not names or type_node is None:
            return None  # an embedded struct: its fields are promoted from elsewhere
        tag_node = decl.child_by_field_name("tag")
        tags = dict(_TAG.findall(go_string(tag_node, source) or "")) if tag_node else {}
        json_name, _, options = tags.get("json", "").partition(",")
        if json_name == "-":
            return None
        description = tags.get("jsonschema")
        if description is not None and re.match(r"^\w+=", description):
            description = None  # an old `key=value` form, not a description
        required = not ({"omitempty", "omitzero"} & set(options.split(",")))
        for name_node in names:
            name = node_text(name_node, source)
            if not name[:1].isupper():
                return None  # unexported: not in the schema, and the reader stops here
            out.append(
                param(
                    json_name or name,
                    _json_type(type_node, source),
                    required=required,
                    description=description,
                )
            )
    return out


def _detect(dc: DetectContext) -> Detection | None:
    source, root = dc.source, dc.root
    pkgs = package_names(root, source, _PACKAGE)
    if not pkgs:
        return None
    nodes = walk(root)
    functions = HandlerLines(source, function_lines(dc.record))
    # Per scope (function span, or the file): name → True when every binding there is a server.
    servers: dict[Span, dict[str, bool]] = {}
    # Per function: name → the (target, value) pairs that declare or assign it there.
    values: dict[Span, dict[str, list[tuple[Node, Node | None]]]] = {}
    # Every identifier of the file by name, in source order (start byte, node).
    uses: dict[str, list[tuple[int, Node]]] = {}
    structs: dict[str, Node | None] = {}
    declared_funcs: dict[str, Node | None] = {}

    def bind(scope: Node | None, name: str, is_server: bool) -> None:
        names = servers.setdefault(_span(scope), {})
        names[name] = names.get(name, True) and is_server

    for n in nodes:
        if n.type in _DECLARATIONS:
            scope = _scope_of(n)
            for ln, rv in _targets(n):
                name = node_text(ln, source)
                call = rv if rv is not None and rv.type == "call_expression" else None
                built = call is not None and _pkg_selector(_callee(call), source, pkgs, "NewServer")
                block = _block_of(n)
                bind(block, name, built)
                assigns = n.type == "assignment_statement" or (
                    n.type == "range_clause" and not _declares(n)
                )
                if assigns and not built:
                    # `x = other`: whichever enclosing declaration `x` is, it is not only a
                    # server any more.
                    while block is not None:
                        block = _block_of(block)
                        bind(block, name, False)
                values.setdefault(_span(scope), {}).setdefault(name, []).append((ln, rv))
        elif n.type in ("receive_statement", "type_switch_statement"):
            # What a receive or a type-switch alias holds is never a proven server: the alias
            # is bound in the switch, a `:=` receive in its `case`, a `=` one assigns the
            # enclosing declaration.
            for ln in _receive_names(n):
                name = node_text(ln, source)
                block = n if n.type == "type_switch_statement" else _block_of(n)
                bind(block, name, False)
                if n.type == "receive_statement" and not _declares(n):
                    while block is not None:
                        block = _block_of(block)
                        bind(block, name, False)
        elif n.type == "identifier":
            uses.setdefault(node_text(n, source), []).append((n.start_byte, n))
        elif n.type == "parameter_declaration":
            type_node = n.child_by_field_name("type")
            typed = (
                type_node is not None
                and type_node.type == "pointer_type"
                and bool(type_node.named_children)
                and _pkg_selector(type_node.named_children[0], source, pkgs, "Server")
            )
            owner = n.parent.parent if n.parent is not None else None
            for name_node in n.children_by_field_name("name"):
                bind(owner, node_text(name_node, source), typed)
        elif n.type == "type_spec":
            spec_name, spec_type = n.child_by_field_name("name"), n.child_by_field_name("type")
            if spec_name is not None:
                key = node_text(spec_name, source)
                is_struct = spec_type is not None and spec_type.type == "struct_type"
                structs[key] = None if key in structs or not is_struct else spec_type
        elif n.type == "function_declaration":
            fn_name = n.child_by_field_name("name")
            if fn_name is not None:
                key = node_text(fn_name, source)
                declared_funcs[key] = None if key in declared_funcs else n

    def is_server(receiver: Node) -> bool:
        """The nearest binding of the receiver's name (block, function, package) is a server."""
        name = node_text(receiver, source)
        scope = _block_of(receiver)
        while True:
            names = servers.get(_span(scope), {})
            if name in names:
                return names[name]
            if scope is None:
                return False
            scope = _block_of(scope)

    def registration(call: Node) -> tuple[Node, Node, bool] | None:
        """``(tool argument, handler, generic)`` of an ``AddTool`` call of the SDK on a proven
        server; None for any other call."""
        fn = _callee(call)
        args = _args(call.child_by_field_name("arguments"))
        if _pkg_selector(fn, source, pkgs, "AddTool"):
            return (args[1], args[2], True) if len(args) == 3 else None
        field = (
            fn.child_by_field_name("field")
            if fn is not None and fn.type == "selector_expression"
            else None
        )
        if fn is None or field is None or len(args) != 2 or node_text(field, source) != "AddTool":
            return None
        receiver = fn.child_by_field_name("operand")
        if receiver is None or receiver.type != "identifier" or not is_server(receiver):
            return None
        return args[0], args[1], False

    registrations = [
        (n, reg) for n in nodes if n.type == "call_expression" for reg in [registration(n)] if reg
    ]
    # The identifiers passed as an ``AddTool`` tool (``t`` / ``&t``): the only use a tool
    # variable may have besides its declaration.
    tool_args = {
        target.start_byte
        for _, (arg, _, _) in registrations
        for target in [_unwrap_ref(arg)[1]]
        if target is not None and target.type == "identifier"
    }

    def only_registered(name: str, decl: Node, scope: Node) -> bool:
        """True when every other use of ``name`` in the function ``scope`` (closures inside it
        included) is an ``AddTool`` tool argument: nothing reassigns, aliases, passes on,
        dereferences or changes a field of it."""
        found = uses.get(name, [])
        lo = bisect.bisect_left(found, scope.start_byte, key=lambda u: u[0])
        hi = bisect.bisect_left(found, scope.end_byte, key=lambda u: u[0])
        for start, ident in found[lo:hi]:
            if start == decl.start_byte or start in tool_args:
                continue
            parent = ident.parent
            if (
                parent is not None
                and parent.type == "literal_element"
                and (
                    parent.parent is not None
                    and parent.parent.type == "keyed_element"
                    and parent.parent.named_children[0] == parent
                )
            ):
                continue  # a key of a composite literal (`{Name: …}`), not the variable
            return False
        return True

    def tool_literal(arg: Node, call: Node) -> Node | None:
        """The ``mcp.Tool{…}`` literal body the argument stands for."""
        ref, target = _unwrap_ref(arg)
        if target is not None and target.type == "identifier":
            scope = _scope_of(call)
            if scope is None:
                return None
            name = node_text(target, source)
            assigned = values.get(_span(scope), {}).get(name, [])
            if len(assigned) != 1 or not only_registered(name, assigned[0][0], scope):
                return None
            value_ref, target = _unwrap_ref(assigned[0][1])
            if value_ref == ref:  # `t := &mcp.Tool{}` is passed as `t`, `t := mcp.Tool{}` as `&t`
                return None
        elif not ref:
            return None
        if target is None or target.type != "composite_literal":
            return None
        if not _pkg_selector(target.child_by_field_name("type"), source, pkgs, "Tool"):
            return None
        return target.child_by_field_name("body")

    def input_struct(call: Node, handler: Node | None) -> Node | None:
        """The struct type of the generic ``AddTool``'s input, declared in this file."""
        type_args = call.child_by_field_name("type_arguments")
        in_type: Node | None = None
        if type_args is not None and type_args.named_children:
            elem = type_args.named_children[0]
            in_type = elem.named_children[0] if elem.named_children else elem
        elif handler is not None:
            fn = (
                declared_funcs.get(node_text(handler, source))
                if handler.type == "identifier"
                else handler
                if handler.type == "func_literal"
                else None
            )
            params = fn.child_by_field_name("parameters") if fn is not None else None
            decls = [p for p in _args(params) if p.type == "parameter_declaration"]
            names = [len(p.children_by_field_name("name")) or 1 for p in decls]
            if sum(names) == 3 and names[-1] == 1:
                in_type = decls[-1].child_by_field_name("type")
        if in_type is None:
            return None
        if in_type.type == "struct_type":
            return in_type
        if in_type.type == "type_identifier":
            return structs.get(node_text(in_type, source))
        return None

    found: list[Emit] = []
    for n, (tool_arg, handler, generic) in registrations:
        fields = _keyed(tool_literal(tool_arg, n), source)
        if fields is None:
            continue
        tool_name = go_string(fields.get("Name"), source)
        if not tool_name:
            continue  # a computed name: the tool exists, but its name is unknown
        params: list[Parameter] | None = None
        if "InputSchema" in fields:
            params = schema_params(_schema_dict(fields["InputSchema"], source))
        elif generic:
            struct = input_struct(n, handler)
            params = _struct_params(struct, source) if struct is not None else None
        handler_name: str | None = None
        handler_line: int | None = None
        if handler.type == "identifier":
            handler_name = node_text(handler, source)
            handler_line = functions.line(handler)
        elif handler.type == "func_literal":
            handler_line = handler.start_point[0] + 1
        elif handler.type == "selector_expression":
            handler_name = node_text(handler, source)
        found.append(
            tool_emit(
                n,
                source,
                endpoint=tool_name,
                framework=FRAMEWORK,
                handler=handler_name,
                handler_line=handler_line,
                description=go_string(fields.get("Description"), source),
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
        gate=Gate("go", order=140, guard=b"modelcontextprotocol/go-sdk", skip_fixtures=True),
        detect=_detect,
        frameworks=(FRAMEWORK,),
    )
)
