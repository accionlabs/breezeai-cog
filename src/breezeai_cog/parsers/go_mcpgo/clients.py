"""Additive detector: calls of a Go MCP **client** built with ``github.com/mark3labs/mcp-go``.

::

    c, err := client.NewStdioMCPClient("server", nil)          # any client.New…Client(…)
    req := mcp.CallToolRequest{}
    req.Params.Name = "add"
    res, err := c.CallTool(ctx, req)                          → mcp_call  mcp-go  TOOL      add
    c.ReadResource(ctx, mcp.ReadResourceRequest{Params: mcp.ReadResourceParams{URI: "memo://x"}})
                                                              → mcp_call  mcp-go  RESOURCE  memo://x
    c.GetPrompt(ctx, mcp.GetPromptRequest{Params: mcp.GetPromptParams{Name: "review"}})
                                                              → mcp_call  mcp-go  PROMPT    review

* **Client receiver** — a name whose only binding in its function is a ``client.New…Client(…)``
  call (or ``client.NewClient(…)``) of mcp-go's ``client`` package, or a parameter typed
  ``*client.Client``.
* **Request** — a ``mcp.CallToolRequest{…}`` (``ReadResourceRequest`` / ``GetPromptRequest``)
  literal whose ``Params`` literal sets ``Name`` (``URI``) to a string literal, inline or a
  variable of the function declared once; for a variable, its ``Params.Name`` may instead be set
  by one ``req.Params.Name = "…"`` (the only such write) that runs before the call on every path
  — straight in the statement list that declares the request and makes the call, ahead of the
  call, with no ``if`` / ``for`` / ``switch`` / ``select`` / func literal around it — and the
  variable must be passed to this one call only. Anything else → no record.
* The record is the call's own statement, with no handler; a client-only file gets no file-level
  framework.
"""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..go_mcp_common import (
    Binding,
    call_args,
    call_target,
    function_bindings,
    go_string,
    keyed_fields,
    literal_type,
    package_names,
    selector,
    top_function,
    walk,
)
from ..mcp_common import call_emit
from ..treesitter import node_text

FRAMEWORK = "mcp-go"
_MCP = "github.com/mark3labs/mcp-go/mcp"
_CLIENT = "github.com/mark3labs/mcp-go/client"
#: Client methods → (method, request type, params field naming what they reach).
_CALLS = {
    "CallTool": ("TOOL", "CallToolRequest", "Name"),
    "ReadResource": ("RESOURCE", "ReadResourceRequest", "URI"),
    "GetPrompt": ("PROMPT", "GetPromptRequest", "Name"),
}


def _detect(dc: DetectContext) -> Detection | None:
    source, root = dc.source, dc.root
    mcp, clients = package_names(root, source, _MCP), package_names(root, source, _CLIENT)
    if not mcp or not clients:
        return None
    cache: dict[tuple[int, int], dict[str, list[Binding]]] = {}
    idents: dict[tuple[int, int], dict[str, list[Node]]] = {}

    def bindings(fn: Node) -> dict[str, list[Binding]]:
        key = (fn.start_byte, fn.end_byte)
        if key not in cache:
            cache[key] = function_bindings(fn, source)
        return cache[key]

    def uses(fn: Node) -> dict[str, list[Node]]:
        """The function's identifiers by name, built once per function."""
        key = (fn.start_byte, fn.end_byte)
        if key not in idents:
            found: dict[str, list[Node]] = {}
            for n in walk(fn):
                if n.type == "identifier":
                    found.setdefault(node_text(n, source), []).append(n)
            idents[key] = found
        return idents[key]

    def is_client(binding: Binding) -> bool:
        value, type_node = binding
        if value is not None and value.type == "call_expression":
            target = selector(value.child_by_field_name("function"), source)
            return (
                target is not None
                and target[0] in clients
                and target[1].startswith("New")
                and target[1].endswith("Client")
            )
        if type_node is not None and type_node.type == "pointer_type" and type_node.named_children:
            return selector(type_node.named_children[0], source) in {(c, "Client") for c in clients}
        return False

    emits: list[Emit] = []
    nodes = walk(root)
    for call in nodes:
        if call.type != "call_expression":
            continue
        target = call_target(call, source)
        kind = _CALLS.get(target[1]) if target is not None else None
        fn = top_function(call)
        if kind is None or target is None or fn is None:
            continue
        found = bindings(fn).get(target[0], [])
        if len(found) != 1 or not is_client(found[0]):
            continue
        args = call_args(call)
        if len(args) != 2:
            continue
        endpoint = _request_endpoint(args[1], kind, uses(fn), bindings(fn), source, mcp)
        if endpoint:
            emits.append(
                call_emit(call, source, method=kind[0], endpoint=endpoint, framework=FRAMEWORK)
            )
    return Detection(emits) if emits else None


def _params_value(
    literal: Node | None, kind: tuple[str, str, str], source: bytes, mcp: set[str]
) -> tuple[bool, Node | None]:
    """``(is the request literal, the Params field's value node or None)``."""
    if literal_type(literal, source) not in {(m, kind[1]) for m in mcp}:
        return False, None
    fields = keyed_fields(literal, source)
    if fields is None:
        return False, None
    params = fields.get("Params")
    if params is None:
        return True, None
    inner = keyed_fields(params, source)
    if inner is None or literal_type(params, source) not in {
        (m, kind[1].replace("Request", "Params")) for m in mcp
    }:
        return False, None
    return True, inner.get(kind[2])


def _declares(ident: Node) -> bool:
    """``ident`` is a name a ``var`` / ``:=`` declaration binds."""
    parent = ident.parent
    if parent is None:
        return False
    if parent.type == "var_spec":
        return ident in parent.children_by_field_name("name")
    owner = parent.parent
    return (
        parent.type == "expression_list"
        and owner is not None
        and owner.type == "short_var_declaration"
        and owner.child_by_field_name("left") == parent
    )


#: Nodes whose statements may run zero or many times, or later: a node reached through one of them
#: is not run straight through by its block.
_BRANCHES = frozenset(
    {
        "if_statement",
        "for_statement",
        "expression_switch_statement",
        "type_switch_statement",
        "select_statement",
        "expression_case",
        "default_case",
        "type_case",
        "communication_case",
        "func_literal",
    }
)


def _home(node: Node) -> Node | None:
    """The statement list ``node`` runs straight through in, or None when an ``if`` / ``for`` /
    ``switch`` / ``select`` / func literal sits between them (an ``if``'s initializer runs once,
    unconditionally, in the enclosing list)."""
    cur = node
    while cur.parent is not None:
        parent = cur.parent
        if parent.type in ("statement_list", "block"):
            return parent
        if parent.type in _BRANCHES and not (
            parent.type == "if_statement" and parent.child_by_field_name("initializer") == cur
        ):
            return None
        cur = parent
    return None


def _request_endpoint(
    arg: Node,
    kind: tuple[str, str, str],
    uses: dict[str, list[Node]],
    bindings: dict[str, list[Binding]],
    source: bytes,
    mcp: set[str],
) -> str | None:
    if arg.type != "identifier":
        ok, value = _params_value(arg, kind, source, mcp)
        return go_string(value, source) if ok else None
    name = node_text(arg, source)
    found = bindings.get(name, [])
    if len(found) != 1:
        return None
    value_node, type_node = found[0]
    if value_node is not None:
        ok, value = _params_value(value_node, kind, source, mcp)
        if not ok:
            return None
    elif type_node is not None and selector(type_node, source) in {(m, kind[1]) for m in mcp}:
        value = None  # `var req mcp.CallToolRequest`
    else:
        return None
    # Every other use of the variable in the function: `req.Params.<field> = …` writes, and this
    # one call.
    writes: list[Node] = []
    calls = 0
    declared: Node | None = None
    for ident in uses.get(name, []):
        parent = ident.parent
        if _declares(ident):
            declared = ident
            continue  # the declaration (its only binding)
        if parent is not None and parent.type == "argument_list":
            calls += 1
            continue
        chain = parent
        if (
            chain is not None
            and chain.type == "selector_expression"
            and chain.parent is not None
            and chain.parent.type == "selector_expression"
            and node_text(chain.child_by_field_name("field"), source) == "Params"  # type: ignore[arg-type]
        ):
            field_node = chain.parent
            stmt = field_node.parent.parent if field_node.parent is not None else None
            if (
                field_node.parent is not None
                and field_node.parent.type == "expression_list"
                and stmt is not None
                and stmt.type == "assignment_statement"
                and stmt.child_by_field_name("left") == field_node.parent
            ):
                if node_text(field_node.child_by_field_name("field"), source) == kind[2]:  # type: ignore[arg-type]
                    writes.append(stmt)
                continue
        return None  # any other use: passed on, aliased, read
    if calls != 1:
        return None
    if writes:
        if value is not None or len(writes) != 1:
            return None
        # The write runs on every path to the call, before it: it sits straight in the statement
        # list that declares the request and makes the call, ahead of the call.
        write, home = writes[0], _home(writes[0])
        if (
            write.start_byte >= arg.start_byte
            or declared is None
            or home is None
            or home != _home(declared)
            or home != _home(arg)
        ):
            return None
        right = writes[0].child_by_field_name("right")
        rvals = (
            [c for c in right.named_children if c.type != "comment"] if right is not None else []
        )
        value = rvals[0] if len(rvals) == 1 else None
    return go_string(value, source)


register_detector(
    Detector(
        name="go-mcpgo-client",
        gate=Gate("go", order=135, guard=b"mark3labs/mcp-go/client"),
        detect=_detect,
        frameworks=(FRAMEWORK,),
    )
)
