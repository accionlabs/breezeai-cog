"""Additive detector: calls of a Python MCP **client** — the official SDK's ``ClientSession``
(``python-mcp``) or the standalone ``fastmcp.Client`` (``fastmcp``).

::

    from mcp import ClientSession
    async with ClientSession(read, write) as session:      # or: session = ClientSession(…)
                                                           # or: def run(session: ClientSession)
        await session.call_tool("add", {"a": 1})           → mcp_call  python-mcp  TOOL      add
        await session.read_resource(AnyUrl("memo://x"))    → mcp_call  python-mcp  RESOURCE  memo://x
        await session.get_prompt("review", {"code": c})    → mcp_call  python-mcp  PROMPT    review

    from fastmcp import Client
    async with Client("http://localhost:8000/mcp") as client:
        await client.call_tool("add", {"a": 1})            → mcp_call  fastmcp  TOOL  add

* **Client receiver** — a name bound, in the nearest scope that binds it, only to the client
  class: assigned ``ClientSession(…)`` / ``Client(…)`` (``=`` or ``:=``), the target of
  ``with … as name``, or a parameter annotated with the class. Any other binding in that scope —
  a loop variable, a ``def`` / ``class`` of that name, an ``import … as name`` — makes it not
  one; ``global`` / ``nonlocal`` send a function's bindings of the name to the module / the
  enclosing function. The class must be imported from the SDK (``mcp``,
  ``mcp.client.session``; ``fastmcp``, ``fastmcp.client``): ``aiohttp.ClientSession`` and an
  agent framework's MCP server object (``MCPServerStdio.call_tool``) are not clients.
* ``endpoint`` — the first argument (or ``name=`` / ``uri=``), a string literal, or
  ``AnyUrl("…")`` of one for a resource; anything else → no record.
* The record is the call's own statement (the one starting on the call's line), with no
  handler.
* A client-only file gets no file-level framework.
"""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..mcp_common import call_emit
from ..python_mcp_common import FASTMCP, PYTHON_MCP
from ..treesitter import node_text
from .lowlevel import _imported_names, _string, _url, _walk

#: ``(modules exporting the class, the class, framework)``.
_CLIENTS = (
    (("mcp", "mcp.client.session"), "ClientSession", PYTHON_MCP),
    (("fastmcp", "fastmcp.client"), "Client", FASTMCP),
)
#: Client methods → (method, keyword naming what they reach).
_CALLS = {
    "call_tool": ("TOOL", "name"),
    "read_resource": ("RESOURCE", "uri"),
    "get_prompt": ("PROMPT", "name"),
}
_SCOPES = ("function_definition", "class_definition", "lambda")


def _scope_of(node: Node) -> Node | None:
    cur = node.parent
    while cur is not None:
        if cur.type in _SCOPES:
            return cur
        cur = cur.parent
    return None


def _key(scope: Node | None) -> tuple[int, int]:
    return (scope.start_byte, scope.end_byte) if scope is not None else (-1, -1)


#: Target shapes that bind each name inside them (``a, (b, *c) = …``, ``with … as (a, b)``). An
#: attribute or subscript target (``self.session = …``, ``cache[session] = …``) binds no name.
_PATTERNS = (
    "pattern_list",
    "tuple_pattern",
    "list_pattern",
    "parenthesized_expression",
    "list_splat_pattern",
    "tuple",
    "list",
    "list_splat",
)


def _names(target: Node, source: bytes) -> list[str]:
    if target.type == "identifier":
        return [node_text(target, source)]
    if target.type not in _PATTERNS:
        return []
    return [n for c in target.named_children for n in _names(c, source)]


def _detect(dc: DetectContext) -> Detection | None:
    source, root = dc.source, dc.root
    if not any(m.encode() in source for m in _CALLS):
        return None
    classes: dict[str, str] = {}
    for modules, class_name, sdk in _CLIENTS:
        for local in _imported_names(root, source, modules, class_name):
            classes[local] = sdk
    if not classes:
        return None
    # (scope, name, framework of the binding: "" for one that is not a client), in file order
    pending: list[tuple[Node | None, str, str]] = []
    # scope → names a `global` / `nonlocal` statement there sends to another scope
    redirected: dict[tuple[int, int], dict[str, str]] = {}

    def bind(node: Node, scope: Node | None, value: Node | None) -> None:
        framework = ""
        if value is not None and value.type == "call":
            fn = value.child_by_field_name("function")
            framework = classes.get(node_text(fn, source), "") if fn is not None else ""
        names = [node_text(node, source)] if node.type == "identifier" else []
        if node.type != "identifier":
            framework = ""
            names = _names(node, source)
        for name in names:
            pending.append((scope, name, framework))

    calls: list[Node] = []
    for node in _walk(root):
        t = node.type
        if t == "assignment":
            left = node.child_by_field_name("left")
            if left is not None:
                bind(left, _scope_of(node), node.child_by_field_name("right"))
        elif t == "named_expression":  # (session := ClientSession(…))
            target = node.child_by_field_name("name")
            if target is not None:
                bind(target, _scope_of(node), node.child_by_field_name("value"))
        elif t == "as_pattern":
            target = next((c for c in node.named_children if c.type == "as_pattern_target"), None)
            inner = (
                target.named_children[0] if target is not None and target.named_children else None
            )
            if inner is not None:
                bind(inner, _scope_of(node), node.named_children[0])
        elif t in ("for_statement", "for_in_clause", "augmented_assignment"):
            left = node.child_by_field_name("left")
            if left is not None:
                bind(left, _scope_of(node), None)
        elif t in ("function_definition", "class_definition"):
            # `def session(): …` binds the name in the enclosing scope
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                bind(name_node, _scope_of(node), None)
        elif t in ("import_statement", "import_from_statement"):
            # `import x as session` / `from x import session`: never a client
            for item in node.children_by_field_name("name"):
                bound = item.child_by_field_name("alias") if item.type == "aliased_import" else item
                if bound is not None and bound.type == "dotted_name" and bound.named_children:
                    bound = bound.named_children[0]  # `import a.b` binds `a`
                if bound is not None and bound.type == "identifier":
                    bind(bound, _scope_of(node), None)
        elif t in ("global_statement", "nonlocal_statement"):
            statement = "global" if t == "global_statement" else "nonlocal"
            names = redirected.setdefault(_key(_scope_of(node)), {})
            for ident in node.named_children:
                if ident.type == "identifier":
                    names[node_text(ident, source)] = statement
        elif t in ("identifier", "typed_parameter", "default_parameter", "typed_default_parameter"):
            parent = node.parent
            if parent is None or parent.type not in ("parameters", "lambda_parameters"):
                continue
            param: Node | None = (
                node
                if t == "identifier"
                else (
                    node.child_by_field_name("name")
                    or (node.named_children[0] if node.named_children else None)
                )
            )
            if param is None or param.type != "identifier":
                continue
            ann = node.child_by_field_name("type")
            framework = classes.get(node_text(ann, source).strip("\"' "), "") if ann else ""
            pending.append((parent.parent, node_text(param, source), framework))
        elif t == "call":
            calls.append(node)

    def target_scope(scope: Node | None, name: str) -> tuple[int, int]:
        """The scope a binding of ``name`` in ``scope`` lands in: the module for a ``global``
        name, the nearest enclosing function for a ``nonlocal`` one."""
        redirect = redirected.get(_key(scope), {}).get(name)
        if redirect == "global":
            return _key(None)
        if redirect == "nonlocal" and scope is not None:
            outer = _scope_of(scope)
            while outer is not None and outer.type == "class_definition":
                outer = _scope_of(outer)
            return _key(outer)
        return _key(scope)

    # scope → name → the frameworks of its bindings there ("" for a binding that is not a client)
    bindings: dict[tuple[int, int], dict[str, set[str]]] = {}
    for scope, name, framework in pending:
        bindings.setdefault(target_scope(scope, name), {}).setdefault(name, set()).add(framework)

    def client_of(receiver: Node) -> str | None:
        name = node_text(receiver, source)
        scope = _scope_of(receiver)
        innermost = True
        while True:
            if scope is None or scope.type != "class_definition" or innermost:
                found = bindings.get(_key(scope), {}).get(name)
                if found is not None:
                    return next(iter(found)) or None if len(found) == 1 else None
            if scope is None:
                return None
            innermost = False
            scope = _scope_of(scope)

    emits: list[Emit] = []
    for call in calls:
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "attribute":
            continue
        obj, attr = fn.child_by_field_name("object"), fn.child_by_field_name("attribute")
        if obj is None or attr is None or obj.type != "identifier":
            continue
        kind = _CALLS.get(node_text(attr, source))
        if kind is None:
            continue
        client = client_of(obj)
        if client is None:
            continue
        endpoint = _endpoint(call, kind, source)
        if endpoint:
            emits.append(
                call_emit(call, source, method=kind[0], endpoint=endpoint, framework=client)
            )
    return Detection(emits) if emits else None


def _endpoint(call: Node, kind: tuple[str, str], source: bytes) -> str | None:
    args = call.child_by_field_name("arguments")
    items = [a for a in (args.named_children if args is not None else []) if a.type != "comment"]
    value: Node | None = None
    for a in items:
        if a.type == "keyword_argument":
            key = a.child_by_field_name("name")
            if key is not None and node_text(key, source) == kind[1]:
                value = a.child_by_field_name("value")
    if value is None and items and items[0].type not in ("keyword_argument", "list_splat"):
        value = items[0]
    if value is None:
        return None
    return _url(value, source) if kind[0] == "RESOURCE" else _string(value, source)


register_detector(
    Detector(
        name="python-mcp-client",
        gate=Gate("python", order=65, guard=b"mcp"),
        detect=_detect,
        frameworks=(PYTHON_MCP, FASTMCP),
    )
)
