"""Go function extraction and call collection."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .statements import extract_statements


def _receiver_type(node: Node, source: bytes) -> str | None:
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return None
    text = node_text(receiver, source).strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    if text:
        if text.startswith("*"):
            return text[1:]
        return text
    return None


def _extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    if params_node is None:
        return []
    out: list[Parameter] = []
    for child in params_node.named_children:
        if child.type == "parameter_declaration":
            name = child.child_by_field_name("name")
            type_node = child.child_by_field_name("type")
            out.append(
                Parameter(
                    name=node_text(name, source) if name is not None else "",
                    type=node_text(type_node, source) if type_node is not None else "",
                    decorators=[],
                )
            )
    return out


def _result_type(node: Node, source: bytes) -> str | None:
    result = node.child_by_field_name("result")
    if result is None:
        return None
    types = []
    for child in result.named_children:
        if child.type == "type":
            types.append(node_text(child, source))
        elif child.type == "parameter_list":
            for p in child.named_children:
                if p.type == "parameter_declaration":
                    tnode = p.child_by_field_name("type")
                    if tnode is not None:
                        types.append(node_text(tnode, source))
    if len(types) > 1:
        return "(" + ", ".join(types) + ")"
    if not types:
        text = node_text(result, source)
        return text.strip()
    return types[0]


def _call_name(node: Node, source: bytes) -> tuple[str, str | None]:
    if node.type == "selector_expression":
        obj = node.child_by_field_name("operand")
        field = node.child_by_field_name("field")
        receiver = node_text(obj, source) if obj is not None else None
        method = node_text(field, source) if field is not None else ""
        return method, receiver
    if node.type == "identifier":
        return node_text(node, source), None
    if node.type == "call_expression":
        fn = node.child_by_field_name("function")
        if fn is not None:
            return _call_name(fn, source)
    return "", None


def _collect_calls(node: Node, source: bytes, resolve: CallResolver = noop_resolver) -> list[Call]:
    calls: list[Call] = []
    seen: set[str] = set()

    def walk(current: Node) -> None:
        for child in current.named_children:
            if child.type == "call_expression":
                name, receiver = _call_name(child, source)
                if name and name not in seen:
                    seen.add(name)
                    calls.append(Call(name=name, path=resolve(name, receiver)))
            walk(child)

    walk(node)
    return calls


def defined_names(root: Node, source: bytes) -> set[str]:
    names: set[str] = set()

    def walk(node: Node) -> None:
        for child in node.named_children:
            if child.type in {"function_declaration", "method_declaration", "type_declaration"}:
                name = child.child_by_field_name("name")
                if name is not None:
                    names.add(node_text(name, source))
            walk(child)

    walk(root)
    return names


def type_map(root: Node, source: bytes) -> dict[str, str]:
    idx: dict[str, str] = {}

    def walk(node: Node) -> None:
        for child in node.named_children:
            if child.type == "short_var_declaration":
                lhs = child.child_by_field_name("left")
                rhs = child.child_by_field_name("right")
                if lhs is not None and rhs is not None:
                    for l in lhs.named_children:
                        if l.type == "identifier":
                            idx[node_text(l, source)] = node_text(rhs, source)
            walk(child)

    walk(root)
    return idx


def build_function(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
) -> tuple[Function, list[Statement]]:
    name = node.child_by_field_name("name")
    func_name = node_text(name, source) if name is not None else ""
    start, end = line_span(node)
    body = node.child_by_field_name("body")
    receiver = _receiver_type(node, source)
    params = _extract_params(node.child_by_field_name("parameters"), source)
    result_type = _result_type(node, source)
    type_parameters = node.child_by_field_name("type_parameters")

    fn = Function(
        id=disambiguate(function_id(path, func_name, start), seen_ids),
        parentId=parent_id,
        path=path,
        name=func_name,
        type=("constructor" if not receiver and func_name.startswith("New") and result_type and result_type.lstrip("(").lstrip("*").split(",", 1)[0].strip().rsplit(".", 1)[-1] == func_name[3:] else "method" if receiver else "function"),
        startLine=start,
        endLine=end,
        receiverType=receiver,
        visibility="public" if func_name[:1].isupper() else "package",
        generics=node_text(type_parameters, source) if type_parameters is not None else None,
        params=params,
        returnType=result_type,
        calls=_collect_calls(body, source, resolve) if body is not None else [],
    )
    statements = extract_statements(body, source, path, parent_id=fn.id, capture=capture, limit=limit, seen_ids=seen_ids, descend_all=True) if body is not None else []
    return fn, statements
