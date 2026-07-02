"""Function / method + parameter + decorator + call extraction."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .statements import extract_statements


def defined_names(root: Node, source: bytes) -> set[str]:
    """All function/method/class names defined in the file (for same-file call resolution)."""
    names: set[str] = set()

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type in ("function_definition", "class_definition"):
                nm = c.child_by_field_name("name")
                if nm is not None:
                    names.add(node_text(nm, source))
            walk(c)

    walk(root)
    return names


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
            # default-value expr (e.g. FastAPI `Depends(get_db)`); None when no default
            value_node = child.child_by_field_name("value")
            default = node_text(value_node, source) if value_node is not None else None
            out.append(Parameter(name=name, type=type_str, default=default))
        elif child.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            ident = next((c for c in child.named_children if c.type == "identifier"), None)
            prefix = "*" if child.type == "list_splat_pattern" else "**"
            out.append(Parameter(name=prefix + (node_text(ident, source) if ident else ""), type=""))
    return out


def _extract_calls(body: Node | None, source: bytes, resolve: CallResolver = noop_resolver) -> list[Call]:
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[str] = set()

    def visit(node: Node) -> None:
        # Descend into every scope, including lambdas and nested defs — their calls
        # belong to the nearest named enclosing function (see build_function).
        for child in node.named_children:
            if child.type == "call":
                fn = child.child_by_field_name("function")
                if fn is not None:
                    callee = node_text(fn, source)
                    name = callee.rsplit(".", 1)[-1]
                    receiver = callee.rsplit(".", 1)[0] if "." in callee else None
                    if name and name not in seen:
                        seen.add(name)
                        calls.append(Call(name=name, path=resolve(name, receiver)))
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
    resolve: CallResolver = noop_resolver,
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
        generics=node_text(fnode.child_by_field_name("type_parameters"), source)
        if fnode.child_by_field_name("type_parameters") is not None else None,
        params=extract_params(fnode.child_by_field_name("parameters"), source),
        decorators=[d for d in decorators if d.name not in ("staticmethod", "classmethod")],
        returnType=node_text(ret, source) if ret is not None else None,
        startLine=start,
        endLine=end,
        calls=_extract_calls(body, source, resolve),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids,
        descend_all=True,  # walk inline lambdas/nested defs — attribute their statements here
    )
    return fn, statements
