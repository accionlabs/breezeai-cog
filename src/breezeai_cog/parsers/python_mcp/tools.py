"""Additive detector: tools of a Python MCP server (FastMCP 2/3, ``mcp.server.fastmcp``).

::

    mcp = FastMCP("breeze")                       # or: def register(mcp: FastMCP): …
                                                  # or: from .server import mcp
    @mcp.tool(name="Code_Graph_Search")           → mcp_tool  fastmcp  TOOL  Code_Graph_Search
    @handle_errors("Code Graph API")                              handler code_graph_search
    async def code_graph_search(query: str, limit: int = 25) -> str:
        \"\"\"Semantic search over the code graph.\"\"\"   description, params

* **Server receiver** — the decorator's object must be proven a server: a name assigned
  ``FastMCP(…)`` in this file, a parameter annotated ``FastMCP``, or a name imported from a
  module that assigns one (fact :mod:`.servers`). ``@agent.tool`` (pydantic-ai) and any other
  ``.tool`` decorator are left alone.
* ``endpoint`` — ``name=`` (or the first positional string), else the function name; a
  non-literal name → no record.
* ``description`` — ``description=`` when a literal, else the docstring (FastMCP's rule);
  a non-literal ``description=`` → none.
* ``params`` — from the signature: annotation text, default, ``Annotated[T, Field(description=…)]``
  / ``Annotated[T, "…"]`` and ``= Field(…)`` descriptions; ``self`` / ``cls``, ``*args`` /
  ``**kwargs`` and the injected ``Context`` are skipped. Required = no default.
* ``framework`` — ``fastmcp`` when the file imports the standalone ``fastmcp`` package, else
  ``mcp-python`` (the official SDK's ``mcp.server.fastmcp``).
"""

from __future__ import annotations

import ast

from tree_sitter import Node

from ...schemas import ToolParam
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..mcp_common import param, tool_emit
from ..treesitter import node_text
from .servers import SDK_MARKERS, mcp_servers, server_assignments, server_class_names

_MAX_DEFAULT = 200
_SKIPPED_PARAMS = frozenset({"self", "cls"})


def _literal(node: Node | None, source: bytes) -> str | None:
    """The value of a plain string literal (implicit concatenation allowed; no f-strings)."""
    if node is None or node.type not in (
        "string",
        "concatenated_string",
        "parenthesized_expression",
    ):
        return None
    try:
        value = ast.literal_eval(node_text(node, source))
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    return value if isinstance(value, str) else None


def _kwarg(call: Node, name: str, source: bytes) -> Node | None:
    args = call.child_by_field_name("arguments")
    for a in args.named_children if args is not None else []:
        key = a.child_by_field_name("name") if a.type == "keyword_argument" else None
        if key is not None and node_text(key, source) == name:
            return a.child_by_field_name("value")
    return None


def _positional(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    return [
        a
        for a in (args.named_children if args is not None else [])
        if a.type not in ("keyword_argument", "comment", "list_splat", "dictionary_splat")
    ]


def _walk(node: Node) -> list[Node]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.named_children))
    return out


def _is_server_type(node: Node | None, source: bytes, classes: set[str]) -> bool:
    return node is not None and node_text(node, source).strip("\"' ") in classes


def _typed_server_params(root: Node, source: bytes, classes: set[str]) -> set[str]:
    out: set[str] = set()
    if not classes:
        return out
    for node in _walk(root):
        if node.type in ("typed_parameter", "typed_default_parameter") and _is_server_type(
            node.child_by_field_name("type"), source, classes
        ):
            name = node.child_by_field_name("name") or (
                node.named_children[0] if node.named_children else None
            )
            if name is not None and name.type == "identifier":
                out.add(node_text(name, source))
    return out


def _import_aliases(root: Node, source: bytes) -> dict[str, str]:
    """``from m import mcp as server`` → ``{"server": "mcp"}``."""
    out: dict[str, str] = {}
    for node in _walk(root):
        if node.type != "import_from_statement":
            continue
        for child in node.named_children:
            if child.type == "aliased_import":
                name, alias = child.child_by_field_name("name"), child.child_by_field_name("alias")
                if name is not None and alias is not None:
                    out[node_text(alias, source)] = node_text(name, source).rsplit(".", 1)[-1]
    return out


def _tool_decorator(decorator: Node, source: bytes) -> tuple[str, Node | None] | None:
    """``@x.tool`` / ``@x.tool(…)`` → ``(x, call or None)``."""
    expr = decorator.named_children[0] if decorator.named_children else None
    call = None
    if expr is not None and expr.type == "call":
        call, expr = expr, expr.child_by_field_name("function")
    if expr is None or expr.type != "attribute":
        return None
    obj, attr = expr.child_by_field_name("object"), expr.child_by_field_name("attribute")
    if obj is None or attr is None or obj.type != "identifier" or node_text(attr, source) != "tool":
        return None
    return node_text(obj, source), call


def _docstring(fn: Node, source: bytes) -> str | None:
    body = fn.child_by_field_name("body")
    first = body.named_children[0] if body is not None and body.named_children else None
    if first is not None and first.type == "expression_statement":  # grammar versions differ
        first = first.named_children[0] if first.named_children else None
    return _literal(first, source)


def _split_annotated(type_text: str) -> str:
    """``Annotated[str, Field(...)]`` → ``str``."""
    inner = type_text.strip()
    if not inner.startswith("Annotated[") or not inner.endswith("]"):
        return inner
    inner = inner[len("Annotated[") : -1]
    depth = 0
    for i, ch in enumerate(inner):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return inner[:i].strip()
    return inner.strip()


def _field_call(node: Node | None, source: bytes) -> Node | None:
    if node is None or node.type != "call":
        return None
    fn = node.child_by_field_name("function")
    callee = node_text(fn, source) if fn is not None else ""
    return node if callee == "Field" or callee.endswith(".Field") else None


def _annotated_description(type_node: Node, source: bytes) -> str | None:
    if not node_text(type_node, source).lstrip().startswith("Annotated["):
        return None
    for n in _walk(type_node):
        field = _field_call(n, source)
        if field is not None:
            return _literal(_kwarg(field, "description", source), source)
    strings = [n for n in _walk(type_node) if n.type == "string"]
    return _literal(strings[0], source) if strings else None


def _short(node: Node, source: bytes) -> str:
    text = " ".join(node_text(node, source).split())
    return text if len(text) <= _MAX_DEFAULT else text[: _MAX_DEFAULT - 1] + "…"


def _params(fn: Node, source: bytes) -> list[ToolParam]:
    params = fn.child_by_field_name("parameters")
    out: list[ToolParam] = []
    for p in params.named_children if params is not None else []:
        name: Node | None
        type_node: Node | None
        value: Node | None
        if p.type == "identifier":
            name, type_node, value = p, None, None
        elif p.type in ("typed_parameter", "typed_default_parameter", "default_parameter"):
            name = p.child_by_field_name("name") or (
                p.named_children[0] if p.named_children else None
            )
            type_node, value = p.child_by_field_name("type"), p.child_by_field_name("value")
        else:  # *args / **kwargs / separators
            continue
        if name is None or name.type != "identifier":
            continue
        pname = node_text(name, source)
        type_text = node_text(type_node, source) if type_node is not None else None
        base = _split_annotated(type_text) if type_text else None
        if pname in _SKIPPED_PARAMS or (base and base.split(".")[-1].split("[")[0] == "Context"):
            continue
        description = _annotated_description(type_node, source) if type_node is not None else None
        default: str | None = None
        required = value is None
        field = _field_call(value, source)
        if field is not None:
            description = _literal(_kwarg(field, "description", source), source) or description
            given = _kwarg(field, "default", source)
            pos = _positional(field)
            if given is None and pos and node_text(pos[0], source) != "...":
                given = pos[0]
            required = given is None and _kwarg(field, "default_factory", source) is None
            default = _short(given, source) if given is not None else None
        elif value is not None:
            default = _short(value, source)
        out.append(param(pname, base, required=required, default=default, description=description))
    return out


def _detect(dc: DetectContext) -> Detection | None:
    source, record = dc.source, dc.record
    if not any(m in source for m in SDK_MARKERS) and b".tool" not in source:
        return None
    classes = server_class_names(dc.root, source)
    servers = server_assignments(
        dc.root, source, classes, module_only=False
    ) | _typed_server_params(dc.root, source, classes)
    imported = mcp_servers(dc.ctx.resolution_index)
    bindings = dc.bindings or {}
    aliases = _import_aliases(dc.root, source)

    def is_server(name: str) -> bool:
        if name in servers:
            return True
        target = bindings.get(name)
        return target is not None and aliases.get(name, name) in imported.get(target, frozenset())

    framework = (
        "fastmcp" if b"from fastmcp" in source or b"import fastmcp" in source else "mcp-python"
    )
    functions = {(f.name, f.startLine): f for f in record.functions}
    found: list[Emit] = []
    for node in _walk(dc.root):
        if node.type != "decorated_definition":
            continue
        fn = node.child_by_field_name("definition")
        if fn is None or fn.type != "function_definition":
            continue
        for decorator in (c for c in node.named_children if c.type == "decorator"):
            hit = _tool_decorator(decorator, source)
            if hit is None or not is_server(hit[0]):
                continue
            call = hit[1]
            fn_name = node_text(fn.child_by_field_name("name"), source)  # type: ignore[arg-type]
            endpoint: str | None = fn_name
            description: str | None = _docstring(fn, source)
            if call is not None:
                name_node = _kwarg(call, "name", source)
                pos = _positional(call)
                if name_node is None and pos and pos[0].type in ("string", "concatenated_string"):
                    name_node = pos[0]
                if name_node is not None:
                    endpoint = _literal(name_node, source)
                desc_node = _kwarg(call, "description", source)
                if desc_node is not None:
                    description = _literal(desc_node, source)
            if not endpoint:
                continue  # a computed name: the tool exists, but its name is unknown
            line = fn.start_point[0] + 1
            owner = functions.get((fn_name, line))
            found.append(
                tool_emit(
                    decorator,
                    source,
                    endpoint=endpoint,
                    framework=framework,
                    handler=fn_name,
                    handler_line=line,
                    description=description,
                    params=_params(fn, source),
                    parent=owner.id if owner is not None else None,
                )
            )
            break
    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label=framework)


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="python-mcp",
        gate=Gate("python", order=20, skip_fixtures=True),
        detect=_detect,
        frameworks=("fastmcp", "mcp-python"),
    )
)
