"""Function / method + parameter + decorator + call extraction."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..treesitter import line_span, node_text
from .statements import extract_statements


def _visibility(name: str) -> str:
    if name.startswith("__") and name.endswith("__"):
        return "public"  # dunder
    if name.startswith("__"):
        return "private"
    if name.startswith("_"):
        return "protected"
    return "public"


def _decorator_name(node: Node, source: bytes) -> tuple[str, list[str]]:
    """A ``decorator`` node -> (simple name, args). Handles @x, @a.b, @x(args)."""
    inner = node.named_children[0] if node.named_children else None
    if inner is None:
        return node_text(node, source).lstrip("@"), []
    args: list[str] = []
    if inner.type == "call":
        target = inner.child_by_field_name("function") or inner.named_children[0]
        arglist = inner.child_by_field_name("arguments")
        if arglist is not None:
            args = [node_text(a, source) for a in arglist.named_children]
        inner = target
    name = node_text(inner, source)
    return name.rsplit(".", 1)[-1], args  # simple name, no module/@


def extract_decorators(decorator_nodes: list[Node], source: bytes) -> list[Decorator]:
    return [Decorator(name=n, args=a) for n, a in (_decorator_name(d, source) for d in decorator_nodes)]


def extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    if params_node is None:
        return []
    out: list[Parameter] = []
    for child in params_node.named_children:
        type_node = child.child_by_field_name("type")
        type_str = node_text(type_node, source) if type_node is not None else ""
        if child.type == "identifier":
            out.append(Parameter(name=node_text(child, source), type=""))
        elif child.type in ("typed_parameter", "default_parameter", "typed_default_parameter"):
            ident = next((c for c in child.named_children if c.type == "identifier"), None)
            name = node_text(ident, source) if ident is not None else node_text(child, source)
            out.append(Parameter(name=name, type=type_str))
        elif child.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            ident = next((c for c in child.named_children if c.type == "identifier"), None)
            prefix = "*" if child.type == "list_splat_pattern" else "**"
            out.append(Parameter(name=prefix + (node_text(ident, source) if ident else ""), type=""))
    return out


def _extract_calls(body: Node | None, source: bytes) -> list[Call]:
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[str] = set()

    def visit(node: Node) -> None:
        for child in node.named_children:
            if child.type in ("function_definition", "class_definition"):
                continue  # nested scope's own calls
            if child.type == "call":
                fn = child.child_by_field_name("function")
                if fn is not None:
                    name = node_text(fn, source).rsplit(".", 1)[-1]
                    if name and name not in seen:
                        seen.add(name)
                        calls.append(Call(name=name))  # path resolved later (M4+)
            visit(child)

    visit(body)
    return calls


def build_function(
    fnode: Node,
    decorators: list[Decorator],
    source: bytes,
    path: str,
    *,
    parent_id: str,
    class_name: str | None,
    seen_ids: set[str],
    capture: bool = False,
    limit: int = 1000,
) -> tuple[Function, list[Statement]]:
    """Return the Function and its (flat) statements — the caller collects statements
    onto ``FileRecord.statements`` (statements are not nested on the Function)."""
    name = node_text(fnode.child_by_field_name("name"), source)
    start, end = line_span(fnode)
    fid = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)
    ret = fnode.child_by_field_name("return_type")
    body = fnode.child_by_field_name("body")
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type="method" if class_name else "function",
        visibility=_visibility(name),
        isStatic=any(d.name == "staticmethod" for d in decorators),
        params=extract_params(fnode.child_by_field_name("parameters"), source),
        decorators=[d for d in decorators if d.name not in ("staticmethod", "classmethod")],
        returnType=node_text(ret, source) if ret is not None else None,
        startLine=start,
        endLine=end,
        calls=_extract_calls(body, source),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids
    )
    return fn, statements
