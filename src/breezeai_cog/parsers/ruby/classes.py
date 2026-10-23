"""Ruby class / module extraction."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ConstructorParam, Function, Statement
from ..callresolve import CallResolver
from ..treesitter import line_span, node_text
from .functions import _visibility_by_scope, build_function, extract_params
from .models import _qualified_name, model_class_identity, model_receivers
from .statements import extract_enum_members, extract_statements


def _name_of_class(node: Node, source: bytes) -> str:
    name = node.child_by_field_name("name")
    if name is not None and name.type in {"constant", "scope_resolution"}:
        return node_text(name, source)
    name = next((c for c in node.named_children if c.type in {"constant", "scope_resolution"}), None)
    if name is not None:
        return node_text(name, source)
    return "<anonymous>"


def _body_of(node: Node) -> Node | None:
    return next((c for c in node.named_children if c.type == "body_statement"), None)


def _conditional_definitions(body: Node, source: bytes) -> list[Node]:
    """Return definitions in class-body control flow and visibility wrappers."""
    found: list[Node] = []

    def visit(child: Node) -> None:
        if child.type in {"method", "singleton_method", "class", "module", "singleton_class"}:
            found.append(child)
            return
        if child.type in {"if", "unless", "elsif", "else"}:
            # Only traverse branch bodies. Descending into arbitrary calls/blocks could
            # assign dynamically evaluated definitions to the surrounding class.
            for field in ("consequence", "alternative"):
                branch = child.child_by_field_name(field)
                if branch is not None:
                    visit(branch)
            if child.type == "else":
                for nested in child.named_children:
                    visit(nested)
            return
        if child.type in {"if_modifier", "unless_modifier"}:
            definition = child.child_by_field_name("body")
            if definition is not None:
                visit(definition)
            return
        if child.type == "case":
            for branch in child.named_children:
                if branch.type in {"when", "else"}:
                    visit(branch)
            return
        if child.type == "when":
            branch_body = child.child_by_field_name("body")
            if branch_body is not None:
                visit(branch_body)
            return
        if child.type == "call":
            method = child.child_by_field_name("method")
            arguments = child.child_by_field_name("arguments")
            if (
                method is not None
                and node_text(method, source) in {"private", "protected", "public", "private_class_method"}
                and arguments is not None
            ):
                for argument in arguments.named_children:
                    if argument.type in {"method", "singleton_method"}:
                        found.append(argument)
            return
        if child.type in {"begin", "rescue", "ensure"}:
            for nested in child.named_children:
                visit(nested)
            return
        if child.type in {"then", "body_statement"}:
            for nested in child.named_children:
                visit(nested)

    for child in body.named_children:
        visit(child)
    return found


def _inline_visibility(node: Node, source: bytes) -> str | None:
    """Visibility supplied by `private def` or related inline wrappers."""
    arguments = node.parent
    wrapper = arguments.parent if arguments is not None else None
    if arguments is None or arguments.type != "argument_list" or wrapper is None or wrapper.type != "call":
        return None
    if wrapper.child_by_field_name("receiver") is not None:
        return None
    method = wrapper.child_by_field_name("method")
    marker = node_text(method, source) if method is not None else ""
    if marker in {"private", "protected", "public"}:
        return marker
    if marker == "private_class_method" and node.type == "singleton_method":
        return "private"
    return None


def _superclass_of(node: Node, source: bytes) -> str | None:
    """Return the declared superclass name for a ``class`` node, without the ``<`` token.

    Fixes C1: populates ``Class.extends`` from Ruby ``class Foo < Bar`` syntax.
    """
    superclass = node.child_by_field_name("superclass")
    if superclass is None:
        return None
    text = node_text(superclass, source).lstrip("<").strip().lstrip(":")
    return text or None


def _constructor_params(body: Node | None, source: bytes) -> list[ConstructorParam]:
    """Extract ConstructorParam list from the ``initialize`` method in a class body.

    Fixes C7: populates ``Class.constructorParams`` as required by the Target Spec.
    """
    if body is None:
        return []
    for child in body.named_children:
        if child.type != "method":
            continue
        name_node = next((c for c in child.named_children if c.type == "identifier"), None)
        if name_node is None or node_text(name_node, source) != "initialize":
            continue
        params_node = child.child_by_field_name("parameters")
        raw_params = extract_params(params_node, source)
        return [ConstructorParam(name=p.name, type=p.type) for p in raw_params]
    return []


def build_class(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve_for_scope: Callable[[Node], CallResolver],
    typed_db_ids: frozenset[str] | None = None,
    models: frozenset[str] | set[str] | None = None,
    associations: dict[str, dict[str, tuple[str, str]]] | None = None,
) -> tuple[list[Class], list[Function], list[Statement]]:
    name = _name_of_class(node, source)
    resolve = resolve_for_scope(node)
    start, end = line_span(node)
    cid = disambiguate(class_id(path, name), seen_ids)
    body = _body_of(node)
    methods: list[Function] = []
    statements: list[Statement] = []
    nested_classes: list[Class] = []
    effective_models = models if models is not None else (typed_db_ids or frozenset())
    effective_assocs = associations or {}
    qualified_name = _qualified_name(node, source) or name
    scope_name = model_class_identity(qualified_name, list(effective_models))

    if body is not None:
        method_visibilities = _visibility_by_scope(body, source)
        class_receivers = model_receivers(
            node, source, effective_models, effective_assocs, enclosing_class=scope_name, is_instance_method=False
        ) or typed_db_ids
        statements.extend(extract_statements(
            body, source, path, parent_id=cid, capture=capture, limit=limit,
            seen_ids=seen_ids, typed_db_ids=class_receivers,
        ))
        # C8: emit enum_member statements for enum-style Ruby modules
        if capture and node.type == "module":
            statements.extend(extract_enum_members(
                body, source, path, parent_id=cid, limit=limit, seen_ids=seen_ids,
            ))
        for child in _conditional_definitions(body, source):
            if child.type in {"method", "singleton_method"}:
                method_receivers = model_receivers(
                    child, source, effective_models, effective_assocs,
                    enclosing_class=scope_name, is_instance_method=(child.type == "method"),
                )
                fns, fn_stmts = build_function(
                    child,
                    source,
                    path,
                    parent_id=cid,
                    class_name=name,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve=resolve,
                    typed_db_ids=method_receivers,
                    visibility=(_inline_visibility(child, source)
                            or method_visibilities.get((child.start_byte, child.end_byte))
                            or (_visibility_by_scope(child.parent, source).get(
                                (child.start_byte, child.end_byte)
                            ) if child.parent is not None else None)),
                )
                methods.extend(fns)
                statements.extend(fn_stmts)
            elif child.type in {"class", "module"}:
                sub_classes, sub_methods, sub_stmts = build_class(
                    child,
                    source,
                    path,
                    parent_id=cid,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve_for_scope=resolve_for_scope,
                    typed_db_ids=typed_db_ids,
                    models=effective_models,
                    associations=effective_assocs,
                )
                nested_classes.extend(sub_classes)
                methods.extend(sub_methods)
                statements.extend(sub_stmts)
            elif child.type == "singleton_class":
                singleton_body = _body_of(child)
                target = next(
                    (part for part in child.named_children if part.type != "body_statement"),
                    None,
                )
                if singleton_body is None or target is None or target.type != "self":
                    continue

                singleton_visibilities = _visibility_by_scope(singleton_body, source)
                for singleton_method in singleton_body.named_children:
                    if singleton_method.type != "method":
                        continue
                    method_receivers = model_receivers(
                        singleton_method, source, effective_models, effective_assocs,
                        enclosing_class=scope_name, is_instance_method=False,
                    )
                    fns, fn_stmts = build_function(
                        singleton_method,
                        source,
                        path,
                        parent_id=cid,
                        class_name=name,
                        seen_ids=seen_ids,
                        capture=capture,
                        limit=limit,
                        resolve=resolve,
                        typed_db_ids=method_receivers,
                        visibility=singleton_visibilities.get(
                            (singleton_method.start_byte, singleton_method.end_byte)
                        ),
                        is_static=True,
                    )
                    methods.extend(fns)
                    statements.extend(fn_stmts)

    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type="class" if node.type == "class" else "module",
        visibility="public",
        startLine=start,
        endLine=end,
        extends=_superclass_of(node, source) if node.type == "class" else None,
        constructorParams=_constructor_params(body, source),
    )
    return [cls, *nested_classes], methods, statements


def iter_definitions(root: Node) -> Iterator[Node]:
    for child in root.named_children:
        if child.type in {"class", "module", "method", "singleton_method"}:
            yield child
        else:
            yield from iter_definitions(child)
