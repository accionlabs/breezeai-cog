"""Ruby function / method extraction."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .statement import extract_statements

_VISIBILITY_MARKERS = {"public", "protected", "private"}


def defined_names(root: Node, source: bytes) -> set[str]:
    """Return names defined directly in a Ruby file/class/module scope."""
    names: set[str] = set()

    def add_name(node: Node) -> None:
        if node.type == "method":
            name = next((c for c in node.named_children if c.type == "identifier"), None)
            if name is not None:
                names.add(node_text(name, source))
        if node.type in {"class", "module"}:
            name = next((c for c in node.named_children if c.type == "constant"), None)
            if name is not None:
                names.add(node_text(name, source))

    body = next((c for c in root.named_children if c.type == "body_statement"), None)
    for node in (body.named_children if body is not None else root.named_children):
        add_name(node)
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


def _visibility(node: Node, source: bytes) -> str:
    arguments = node.parent
    call = arguments.parent if arguments is not None and arguments.type == "argument_list" else None
    if (
        arguments is not None
        and call is not None
        and call.type == "call"
        and call.child_by_field_name("receiver") is None
    ):
        method = call.child_by_field_name("method")
        call_args = call.child_by_field_name("arguments")
        if (
            call_args == arguments
            and len(arguments.named_children) == 1
            and arguments.named_children[0] == node
            and method is not None
            and node_text(method, source) in _VISIBILITY_MARKERS
        ):
            return node_text(method, source)

    visibility = "public"
    parent = node.parent
    if parent is None:
        return visibility
    for sibling in parent.named_children:
        if sibling == node:
            break
        if sibling.type == "identifier":
            marker = node_text(sibling, source)
            if marker in _VISIBILITY_MARKERS:
                visibility = marker
            continue
        if sibling.type != "call" or sibling.child_by_field_name("receiver") is not None:
            continue
        if sibling.child_by_field_name("arguments") is not None:
            continue
        method = sibling.child_by_field_name("method")
        if method is not None:
            marker = node_text(method, source)
            if marker in _VISIBILITY_MARKERS:
                visibility = marker

    name_node = next((c for c in node.named_children if c.type == "identifier"), None)
    if name_node is None:
        return visibility
    name = node_text(name_node, source)
    after = False
    for sibling in parent.named_children:
        if sibling == node:
            after = True
            continue
        if not after or sibling.type != "call" or sibling.child_by_field_name("receiver") is not None:
            continue
        args = sibling.child_by_field_name("arguments")
        method = sibling.child_by_field_name("method")
        if args is None or method is None:
            continue
        marker = node_text(method, source)
        if marker not in _VISIBILITY_MARKERS:
            continue

        names: set[str] = set()
        for argument in args.named_children:
            if argument.type == "simple_symbol":
                names.add(node_text(argument, source).lstrip(":"))
            elif argument.type == "string":
                content = next(
                    (child for child in argument.named_children if child.type == "string_content"),
                    None,
                )
                if content is not None:
                    names.add(node_text(content, source))
        if name in names:
            visibility = marker
    return visibility

def _calls_in_block(body: Node | None, source: bytes, resolve: CallResolver = noop_resolver) -> list[Call]:
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[tuple[str, str | None]] = set()

    def visit(node: Node) -> None:
        # A bare paren-less `foo` is an `identifier` in this grammar, not a call node; don't
        # guess whether it is a local-variable read or an implicit method call.
        if node.type == "call":
            parts: list[str] = []
            for child in node.named_children:
                if child.type in {"identifier", "constant", "instance_variable", "class_variable", "global_variable"}:
                    parts.append(node_text(child, source))
            if not parts:
                return
            name = parts[-1]
            receiver = parts[0] if len(parts) > 1 else None
            path = resolve(name, receiver)
            if (name, path) not in seen:  # one entry per name and target file
                seen.add((name, path))
                calls.append(Call(name=name, path=path))
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
    id_owner: str | None = None,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
    typed_db_ids: frozenset[str] | None = None,
) -> tuple[list[Function], list[Statement]]:
    name_node = next((c for c in node.named_children if c.type == "identifier"), None)
    name = node_text(name_node, source) if name_node is not None else "<anonymous>"
    start, end = line_span(node)
    # The id carries the qualified owner (`Outer.Inner`) so a nested class's methods are
    # identified unambiguously; `class_name` stays simple.
    fid = disambiguate(
        function_id(path, name, start, class_name=id_owner or class_name), seen_ids)
    params_node = node.child_by_field_name("parameters")
    body = next((c for c in node.named_children if c.type == "body_statement"), None)
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type="method",
        visibility=_visibility(node, source),
        params=extract_params(params_node, source),
        startLine=start,
        endLine=end,
        calls=_calls_in_block(body, source, resolve),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit,
        seen_ids=seen_ids, typed_db_ids=typed_db_ids,
    )
    return [fn], statements
