"""Function / method / arrow + parameter + decorator + call extraction (TS/JS)."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..treesitter import line_span, node_text
from .statements import extract_statements

_SKIP_SCOPES = {
    "function_declaration", "function_expression", "arrow_function",
    "method_definition", "class_declaration", "class",
}


def _type_text(annotation: Node | None, source: bytes) -> str | None:
    if annotation is None:
        return None
    return node_text(annotation, source).lstrip(":").strip() or None


def _visibility(node: Node, source: bytes) -> str:
    for child in node.named_children:
        if child.type == "accessibility_modifier":
            return node_text(child, source)
    return "public"


def decorator(node: Node, source: bytes) -> Decorator:
    inner = node.named_children[0] if node.named_children else None
    if inner is None:
        return Decorator(name=node_text(node, source).lstrip("@"), args=[])
    args: list[str] = []
    if inner.type == "call_expression":
        arglist = inner.child_by_field_name("arguments")
        if arglist is not None:
            args = [node_text(a, source) for a in arglist.named_children]
        inner = inner.child_by_field_name("function") or inner
    return Decorator(name=node_text(inner, source).rsplit(".", 1)[-1], args=args)


def extract_decorators(nodes: list[Node], source: bytes) -> list[Decorator]:
    return [decorator(n, source) for n in nodes]


def extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    out: list[Parameter] = []
    if params_node is None:
        return out
    for p in params_node.named_children:
        if p.type in ("required_parameter", "optional_parameter"):
            pat = p.child_by_field_name("pattern")
            name = node_text(pat, source) if pat is not None else ""
            decs = extract_decorators([c for c in p.named_children if c.type == "decorator"], source)
            out.append(Parameter(
                name=name, type=_type_text(p.child_by_field_name("type"), source) or "",
                decorators=decs,  # e.g. Nest @Body/@Param/@Query, Angular @Inject (spec C4.1)
            ))
        elif p.type == "rest_pattern":
            ident = next((c for c in p.named_children if c.type == "identifier"), None)
            out.append(Parameter(name="..." + (node_text(ident, source) if ident else ""), type=""))
        elif p.type == "identifier":
            out.append(Parameter(name=node_text(p, source), type=""))
    return out


def _calls(body: Node | None, source: bytes) -> list[Call]:
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[str] = set()

    def visit(node: Node) -> None:
        for child in node.named_children:
            if child.type in _SKIP_SCOPES:
                continue
            if child.type == "call_expression":
                fn = child.child_by_field_name("function")
                if fn is not None:
                    name = node_text(fn, source).rsplit(".", 1)[-1]
                    if name.isidentifier() and name not in seen:
                        seen.add(name)
                        calls.append(Call(name=name))
            visit(child)

    visit(body)
    return calls


def build_function(
    node: Node,
    *,
    name: str,
    kind: str,
    decorators: list[Decorator],
    source: bytes,
    path: str,
    parent_id: str,
    class_name: str | None,
    seen_ids: set[str],
    capture: bool,
    limit: int,
) -> tuple[Function, list[Statement]]:
    start, end = line_span(node)
    fid = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)
    body = node.child_by_field_name("body")
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type=kind,
        visibility=_visibility(node, source),
        isStatic=any(c.type == "static" for c in node.children),
        generics=_type_text(node.child_by_field_name("type_parameters"), source) or None,
        params=extract_params(node.child_by_field_name("parameters"), source),
        decorators=decorators,
        returnType=_type_text(node.child_by_field_name("return_type"), source),
        startLine=start,
        endLine=end,
        calls=_calls(body, source),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids
    )
    return fn, statements
