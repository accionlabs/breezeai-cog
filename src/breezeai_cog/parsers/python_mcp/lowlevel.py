"""Tools of a server built with the official Python SDK's **low-level** API
(``mcp.server.lowlevel.Server``), read for the ``python-mcp`` detector (``tools.py``).

::

    from mcp.server.lowlevel import Server        # or: from mcp.server import Server
    import mcp.types as types
    server = Server("demo")                       # or: def build(server: Server): …
                                                  # or: from .app import server
    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name="add",                → mcp_tool  python-mcp  TOOL  add
                       description="Add two numbers",                 handler call_tool
                       inputSchema={"type": "object", "properties": {"a": {"type": "number"}}}),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict): …

* **Server receiver** — the decorator's object must be proven a low-level server: a name
  assigned ``Server(…)`` in its scope or an enclosing one, a parameter annotated ``Server``, or
  a name imported from a module that assigns one at module level (fact :data:`FACT`) and bound
  by nothing else in this module (``server = wrap(server)`` is no proof) — with
  ``Server`` imported from ``mcp.server.lowlevel``, ``mcp.server.lowlevel.server`` or
  ``mcp.server``. A local class named ``Server`` is not one; a nearer binding of the name that
  is not a server hides it.
* **Tools** — each ``Tool(…)`` call (``Tool`` imported from ``mcp.types``, or ``types.Tool`` with
  ``types`` bound to ``mcp.types``; the standard library's ``types`` is not it) that is a direct
  element of a list literal returned by the ``@<server>.list_tools()`` function
  (``return [...]``, or ``return ListToolsResult(tools=[...])``). A list built by a
  comprehension, a filter, a condition or a variable → no record.
* ``endpoint`` — ``name=``, a string literal; anything else → no record. ``description`` —
  ``description=`` when a literal, else none.
* ``params`` — ``inputSchema=`` a literal dict (read as Python values, then as a JSON schema:
  ``properties`` / ``required``); anything else → None (unknown).
* ``handler`` — the function decorated ``@<server>.call_tool()`` on the same server in the same
  file (it dispatches every tool); none, or more than one → no handler.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import DetectContext, FactSpec, index_fact, register_fact
from ..emits import Emit
from ..mcp_common import schema_params, tool_emit
from ..python_mcp_common import PYTHON_MCP
from ..treesitter import node_text

#: Fact: the module-level low-level servers of each file (``{module path: server names}``).
FACT = "mcp.lowlevel-servers"
#: Modules that export the low-level ``Server`` class.
SERVER_MODULES = ("mcp.server.lowlevel", "mcp.server.lowlevel.server", "mcp.server")
#: The module of the SDK's ``Tool`` / ``ListToolsResult`` types.
TYPES_MODULE = "mcp.types"
#: ``@x.list_tools(`` — a cheap byte check before any tree walk.
LIST_TOOLS_DECORATOR = re.compile(rb"@[ \t]*[\w.]+[ \t]*\.[ \t]*list_tools[ \t]*\(")

_SCOPES = ("function_definition", "class_definition", "lambda")


def _walk(node: Node) -> list[Node]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.named_children))
    return out


def _imported_names(root: Node, source: bytes, modules: tuple[str, ...], name: str) -> set[str]:
    """What ``name`` of any of ``modules`` is called in this file: ``from m import name [as a]``
    → ``name`` / ``a``; ``import m [as a]`` → ``m.name`` / ``a.name``."""
    out: set[str] = set()
    for node in _walk(root):
        if node.type == "import_from_statement":
            module = node.child_by_field_name("module_name")
            if module is None or node_text(module, source) not in modules:
                continue
            for child in node.named_children[1:]:
                if child.type == "dotted_name" and node_text(child, source) == name:
                    out.add(name)
                elif child.type == "aliased_import":
                    orig, alias = (
                        child.child_by_field_name("name"),
                        child.child_by_field_name("alias"),
                    )
                    if orig is not None and alias is not None and node_text(orig, source) == name:
                        out.add(node_text(alias, source))
        elif node.type == "import_statement":
            for child in node.named_children:
                aliased = child.type == "aliased_import"
                target = child.child_by_field_name("name") if aliased else child
                alias = child.child_by_field_name("alias") if aliased else None
                if target is not None and node_text(target, source) in modules:
                    local = node_text(alias if alias is not None else target, source)
                    out.add(f"{local}.{name}")
    return out


def _types_names(root: Node, source: bytes, name: str) -> set[str]:
    """What ``mcp.types.<name>`` is called in this file: ``from mcp.types import name``,
    ``from mcp import name`` (the package re-exports it), ``from mcp import types [as t]`` →
    ``t.name``, ``import mcp.types [as t]``, ``import mcp`` → ``mcp.name`` / ``mcp.types.name``."""
    out = _imported_names(root, source, (TYPES_MODULE, "mcp"), name)
    out |= {f"{t}.{name}" for t in _imported_names(root, source, ("mcp",), "types")}
    if f"mcp.{name}" in out:  # `import mcp`
        out.add(f"mcp.types.{name}")
    return out


def server_class_names(root: Node, source: bytes) -> set[str]:
    """What the low-level ``Server`` class is called in this file."""
    return _imported_names(root, source, SERVER_MODULES, "Server")


def _is_call_to(node: Node | None, source: bytes, names: set[str]) -> bool:
    if node is None or node.type != "call":
        return False
    fn = node.child_by_field_name("function")
    return fn is not None and node_text(fn, source) in names


def _scope_of(node: Node) -> Node | None:
    """The function, class or lambda whose body ``node`` is in; None at module level."""
    cur = node.parent
    while cur is not None:
        if cur.type in _SCOPES:
            return cur
        cur = cur.parent
    return None


def _scope_key(scope: Node | None) -> tuple[int, int]:
    return (scope.start_byte, scope.end_byte) if scope is not None else (-1, -1)


def _pattern_names(node: Node, source: bytes) -> list[str]:
    """The names a binding target declares (``a``, ``a, b``, ``[a, *b]``)."""
    if node.type == "identifier":
        return [node_text(node, source)]
    if node.type in ("pattern_list", "tuple_pattern", "list_pattern", "list_splat_pattern"):
        return [n for c in node.named_children for n in _pattern_names(c, source)]
    return []


#: Per scope: name → True when every binding of it there is a low-level server, else False.
_Bindings = dict[tuple[int, int], dict[str, bool]]


def _bindings(
    root: Node,
    source: bytes,
    classes: set[str],
    only_imported: set[str] | None = None,
) -> _Bindings:
    """Every name each scope binds, and whether it is a low-level server there: assigned
    ``Server(…)`` or a parameter annotated ``Server``. Any other binding (an assignment of
    something else, an untyped parameter, an import, a loop or ``with`` target, a ``def`` or
    ``class``) makes the name not a server in that scope. ``only_imported`` receives the names
    the module binds by one ``from … import`` and nothing else (the ones the cross-file fact
    may prove)."""
    out: _Bindings = {}
    #: Module-level bindings per name: how many, and how many are a ``from … import``.
    counts: dict[str, list[int]] = {}

    def bind(scope: Node | None, name: str, is_server: bool, from_import: bool = False) -> None:
        names = out.setdefault(_scope_key(scope), {})
        names[name] = names.get(name, True) and is_server
        if scope is None:
            count = counts.setdefault(name, [0, 0])
            count[0] += 1
            count[1] += from_import

    for node in _walk(root):
        t = node.type
        if t == "assignment":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is None:
                continue
            server = left.type == "identifier" and _is_call_to(right, source, classes)
            for name in _pattern_names(left, source):
                bind(_scope_of(node), name, server)
        elif t in ("augmented_assignment",):
            left = node.child_by_field_name("left")
            for name in _pattern_names(left, source) if left is not None else []:
                bind(_scope_of(node), name, False)
        elif t in ("for_statement", "for_in_clause"):
            left = node.child_by_field_name("left")
            for name in _pattern_names(left, source) if left is not None else []:
                bind(_scope_of(node), name, False)
        elif t == "as_pattern":
            alias = node.child_by_field_name("alias")
            target = (
                alias.named_children[0] if alias is not None and alias.named_children else alias
            )
            for name in _pattern_names(target, source) if target is not None else []:
                bind(_scope_of(node), name, False)
        elif t in ("function_definition", "class_definition"):
            defined = node.child_by_field_name("name")
            if defined is not None:
                bind(_scope_of(node), node_text(defined, source), False)
        elif t in ("identifier", "typed_parameter", "default_parameter", "typed_default_parameter"):
            parent = node.parent
            if parent is None or parent.type not in ("parameters", "lambda_parameters"):
                continue
            owner = parent.parent
            if t == "identifier":
                name_node: Node | None = node
            else:
                name_node = node.child_by_field_name("name") or (
                    node.named_children[0] if node.named_children else None
                )
            if name_node is None or name_node.type != "identifier":
                continue
            ann = node.child_by_field_name("type")
            typed = ann is not None and node_text(ann, source).strip("\"' ") in classes
            bind(owner, node_text(name_node, source), typed)
        elif t in ("import_from_statement", "import_statement"):
            scope = _scope_of(node)
            for child in node.named_children:
                if t == "import_from_statement" and child == node.child_by_field_name(
                    "module_name"
                ):
                    continue
                from_import = t == "import_from_statement"
                if child.type == "aliased_import":
                    alias = child.child_by_field_name("alias")
                    if alias is not None:
                        bind(scope, node_text(alias, source), False, from_import)
                elif child.type == "dotted_name":
                    bind(scope, node_text(child, source).split(".")[0], False, from_import)
    if only_imported is not None:
        # The names bound at module level by one `from … import` and nothing else.
        only_imported.update(name for name, (total, froms) in counts.items() if total == froms == 1)
    return out


def module_servers(root: Node, source: bytes) -> set[str]:
    """Module-level names that are a low-level server (what another file can import)."""
    classes = server_class_names(root, source)
    if not classes:
        return set()
    names = _bindings(root, source, classes).get(_scope_key(None), {})
    return {name for name, is_server in names.items() if is_server}


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> Any:
    names = module_servers(root, source)
    return (rel, tuple(sorted(names))) if names else None


def _resolve(values: list[Any], index: object) -> dict[str, frozenset[str]] | None:
    out = {rel: frozenset(names) for rel, names in values}
    return out or None


register_fact(
    FactSpec(
        name=FACT,
        language="python",
        gate=(b"mcp.server",),
        collect=_collect,
        resolve=_resolve,
    )
)


def _import_aliases(root: Node, source: bytes) -> dict[str, str]:
    """``from m import srv as server`` → ``{"server": "srv"}`` (module level)."""
    out: dict[str, str] = {}
    for node in root.named_children:
        if node.type != "import_from_statement":
            continue
        for child in node.named_children:
            if child.type == "aliased_import":
                name, alias = child.child_by_field_name("name"), child.child_by_field_name("alias")
                if name is not None and alias is not None:
                    out[node_text(alias, source)] = node_text(name, source).rsplit(".", 1)[-1]
    return out


def _decorator_target(decorator: Node, source: bytes, method: str) -> Node | None:
    """``@x.<method>()`` → the ``x`` identifier node; None for anything else."""
    call = decorator.named_children[0] if decorator.named_children else None
    if call is None or call.type != "call":
        return None
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "attribute":
        return None
    obj, attr = fn.child_by_field_name("object"), fn.child_by_field_name("attribute")
    if obj is None or attr is None or obj.type != "identifier" or node_text(attr, source) != method:
        return None
    return obj


def _returns(fn: Node) -> list[Node]:
    """The ``return`` statements of ``fn`` itself (not of a nested function, lambda or class)."""
    body = fn.child_by_field_name("body")
    out: list[Node] = []
    stack = list(body.named_children) if body is not None else []
    while stack:
        n = stack.pop()
        if n.type in _SCOPES or n.type == "decorated_definition":
            continue
        if n.type == "return_statement":
            out.append(n)
            continue
        stack.extend(n.named_children)
    return out


def _kwarg(call: Node, name: str, source: bytes) -> Node | None:
    args = call.child_by_field_name("arguments")
    for a in args.named_children if args is not None else []:
        key = a.child_by_field_name("name") if a.type == "keyword_argument" else None
        if key is not None and node_text(key, source) == name:
            return a.child_by_field_name("value")
    return None


def _string(node: Node | None, source: bytes) -> str | None:
    """A plain string literal (implicit concatenation allowed; no f-strings)."""
    value = (
        _literal_value(node, source)
        if node is not None and node.type in ("string", "concatenated_string")
        else None
    )
    return value if isinstance(value, str) else None


def _literal_value(node: Node, source: bytes) -> Any:
    try:
        return ast.literal_eval(node_text(node, source))
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _tool_calls(fn: Node, source: bytes, tools: set[str], results: set[str]) -> list[Node]:
    """The ``Tool(…)`` calls that are direct elements of a list literal ``fn`` returns:
    ``return [Tool(…), …]`` or ``return ListToolsResult(tools=[Tool(…), …])``."""
    out: list[Node] = []
    for ret in _returns(fn):
        value = ret.named_children[0] if ret.named_children else None
        if value is not None and _is_call_to(value, source, results):
            value = _kwarg(value, "tools", source)
        if value is None or value.type != "list":
            continue
        out.extend(e for e in value.named_children if _is_call_to(e, source, tools))
    return out


def _tool_emit(call: Node, source: bytes, handler: tuple[str, int] | None) -> Emit | None:
    name = _string(_kwarg(call, "name", source), source)
    if not name:
        return None  # a computed name: the tool exists, but its name is unknown
    desc_node = _kwarg(call, "description", source)
    description = _string(desc_node, source) if desc_node is not None else None
    schema = _kwarg(call, "inputSchema", source)
    params = (
        schema_params(_literal_value(schema, source))
        if schema is not None and schema.type == "dictionary"
        else None
    )
    return tool_emit(
        call,
        source,
        endpoint=name,
        framework=PYTHON_MCP,
        handler=handler[0] if handler else None,
        handler_line=handler[1] if handler else None,
        description=description,
        params=params,
        node_type=None,
    )


def detect_lowlevel_tools(dc: DetectContext) -> list[Emit]:
    """The tools of the file's ``@<server>.list_tools()`` functions (see the module docstring)."""
    source, root = dc.source, dc.root
    if b"mcp" not in source or not LIST_TOOLS_DECORATOR.search(source):
        return []
    tools = _types_names(root, source, "Tool")
    if not tools:
        return []
    classes = server_class_names(root, source)
    results = _types_names(root, source, "ListToolsResult")
    only_imported: set[str] = set()
    bindings = _bindings(root, source, classes, only_imported)
    imported: dict[str, frozenset[str]] = index_fact(dc.ctx.resolution_index, FACT) or {}
    modules = dc.bindings or {}
    aliases = _import_aliases(root, source)

    def server_of(receiver: Node) -> tuple[Any, ...] | None:
        """The identity of the server ``receiver`` names (the scope binding it, or the module
        it is imported from), or None when it is not a low-level server."""
        name = node_text(receiver, source)
        scope = _scope_of(receiver)
        innermost = True
        while True:
            # A class body is not visible from the functions inside it.
            if scope is None or scope.type != "class_definition" or innermost:
                names = bindings.get(_scope_key(scope), {})
                if name in names:
                    if names[name]:
                        return ("local", _scope_key(scope), name)
                    if scope is not None or name not in modules or name not in only_imported:
                        return None
                    break  # bound only by an import of an in-repo module: the fact decides
            if scope is None:
                break
            innermost = False
            scope = _scope_of(scope)
        target = modules.get(name)
        if target is not None and aliases.get(name, name) in imported.get(target, frozenset()):
            return ("imported", target, aliases.get(name, name))
        return None

    listers: list[tuple[tuple[Any, ...], Node]] = []
    dispatchers: dict[tuple[Any, ...], list[Node]] = {}
    for node in _walk(root):
        if node.type != "decorated_definition":
            continue
        fn = node.child_by_field_name("definition")
        if fn is None or fn.type != "function_definition":
            continue
        for decorator in (c for c in node.named_children if c.type == "decorator"):
            for method in ("list_tools", "call_tool"):
                receiver = _decorator_target(decorator, source, method)
                server = server_of(receiver) if receiver is not None else None
                if server is None:
                    continue
                if method == "list_tools":
                    listers.append((server, fn))
                else:
                    dispatchers.setdefault(server, []).append(fn)
    found: list[Emit] = []
    for server, fn in listers:
        handlers = dispatchers.get(server, [])
        handler: tuple[str, int] | None = None
        if len(handlers) == 1:
            name_node = handlers[0].child_by_field_name("name")
            if name_node is not None:
                handler = (node_text(name_node, source), handlers[0].start_point[0] + 1)
        for call in _tool_calls(fn, source, tools, results):
            emit = _tool_emit(call, source, handler)
            if emit is not None:
                found.append(emit)
    return found
