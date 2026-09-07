"""Ruby function / method extraction."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .statement import extract_statements


def defined_names(root: Node, source: bytes) -> set[str]:
    """Return names defined in this file for same-file call resolution."""
    names: set[str] = set()

    def visit(node: Node) -> None:
        if node.type == "method":
            name = next((c for c in node.named_children if c.type == "identifier"), None)
            if name is not None:
                names.add(node_text(name, source))
        if node.type in {"class", "module"}:
            name = next((c for c in node.named_children if c.type == "constant"), None)
            if name is not None:
                names.add(node_text(name, source))
        for child in node.named_children:
            visit(child)

    visit(root)
    return names


def _extract_name(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    if node.type in {"identifier", "constant", "instance_variable"}:
        return node_text(node, source)
    if node.type == "call":
        return node_text(node, source)
    return node_text(node, source)


def extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    if params_node is None:
        return []
    out: list[Parameter] = []
    for child in params_node.named_children:
        if child.type == "identifier":
            out.append(Parameter(name=node_text(child, source), type=""))
        elif child.type == "optional_parameter":
            name = child.child_by_field_name("name")
            default = child.child_by_field_name("value")
            out.append(Parameter(
                name=node_text(name, source) if name is not None else "",
                type="",
                default=node_text(default, source) if default is not None else None,
            ))
    return out


def _calls_in_block(body: Node | None, source: bytes, resolve: CallResolver = noop_resolver) -> list[Call]:
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[tuple[str, str | None]] = set()

    def visit(node: Node) -> None:
        if node.type == "call":
            parts: list[str] = []
            for child in node.named_children:
                if child.type in {"identifier", "constant", "instance_variable", "class_variable", "global_variable"}:
                    parts.append(node_text(child, source))
            if not parts:
                return
            name = parts[-1]
            receiver = parts[0] if len(parts) > 1 else None
            key = (name, receiver)
            if key not in seen:
                seen.add(key)
                calls.append(Call(name=name, path=resolve(name, receiver)))
        for child in node.named_children:
            visit(child)

    visit(body)
    return calls


def build_function(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    class_name: str | None,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
) -> tuple[list[Function], list[Statement]]:
    name_node = next((c for c in node.named_children if c.type == "identifier"), None)
    name = node_text(name_node, source) if name_node is not None else "<anonymous>"
    start, end = line_span(node)
    fid = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)
    params_node = node.child_by_field_name("parameters")
    body = next((c for c in node.named_children if c.type == "body_statement"), None)
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type="method",
        visibility="public",
        params=extract_params(params_node, source),
        startLine=start,
        endLine=end,
        calls=_calls_in_block(body, source, resolve),
    )
    statements = extract_statements(body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids)
    return [fn], statements
