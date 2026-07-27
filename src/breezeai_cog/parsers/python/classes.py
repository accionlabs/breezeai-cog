"""Class extraction — returns the Class plus its methods (Functions whose
``parentId`` is the class id)."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ConstructorParam, Function, Statement
from ..treesitter import line_span, node_text
from ..callresolve import CallResolver, noop_resolver
from .functions import _visibility, build_function, extract_decorators
from .statements import extract_statements


def _unwrap(node: Node) -> tuple[Node, list[Node]]:
    """Return (definition, decorator_nodes) for a (maybe) decorated_definition."""
    if node.type == "decorated_definition":
        decs = [c for c in node.named_children if c.type == "decorator"]
        inner = next(c for c in node.named_children if c.type in ("class_definition", "function_definition"))
        return inner, decs
    return node, []


_DEFINITION_TYPES = ("function_definition", "class_definition")


def iter_definitions(body: Node) -> Iterator[tuple[Node, list[Node]]]:
    """Yield ``(definition, decorator_nodes)`` for every ``function_definition`` /
    ``class_definition`` reachable from ``body`` in source order, descending
    recursively through any intervening block statement (``with`` / ``if`` / ``try``
    / ``for`` / ``while`` / ``match`` and their async & clause variants) but **not**
    into a definition's own body — that definition's builder recurses its own
    nesting. Without this, a ``def`` nested in a module- or class-level
    ``with DAG(...):`` (Airflow) or ``if/for`` block is never seeded, since the
    walkers otherwise look only at *direct* children. Descending generically
    (rather than allow-listing block types) also covers ``while`` / ``match`` /
    ``async`` blocks that no single codebase exercises."""
    for child in body.named_children:
        defn, decs = _unwrap(child)
        if defn.type in _DEFINITION_TYPES:
            yield defn, decs  # barrier: the builder recurses nesting inside this def
        else:
            yield from iter_definitions(child)


def build_class(
    cnode: Node,
    decorator_nodes: list[Node],
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
) -> tuple[list[Class], list[Function], list[Statement]]:
    """Return ([this class, *nested classes], methods, statements). Classes,
    methods and statements are flat — the caller collects them onto
    ``FileRecord.classes``/``.functions``/``.statements`` (linked by parentId),
    not nested on the Class. Nested classes/methods reachable through in-body
    blocks (``if``/``with``/``try`` …) are included via ``iter_definitions``."""
    name = node_text(cnode.child_by_field_name("name"), source)
    start, end = line_span(cnode)
    cid = disambiguate(class_id(path, name), seen_ids)

    supers = cnode.child_by_field_name("superclasses")
    bases = [node_text(b, source) for b in supers.named_children] if supers is not None else []
    # Python has no access modifiers; use the leading-underscore convention (as for functions).
    # Abstract = inherits ABC/ABCMeta (the common marker). PEP 695 `class C[T]` → generics.
    is_abstract = any("ABC" in b for b in bases)
    tp = cnode.child_by_field_name("type_parameters")
    generics = node_text(tp, source) if tp is not None else None

    methods: list[Function] = []
    statements: list[Statement] = []
    nested_classes: list[Class] = []
    ctor_params: list[ConstructorParam] = []
    body = cnode.child_by_field_name("body")
    if body is not None:
        # class-level statements (e.g. class variables), parented to the class
        statements.extend(
            extract_statements(body, source, path, parent_id=cid, capture=capture, limit=limit, seen_ids=seen_ids)
        )
        for defn, decs in iter_definitions(body):
            if defn.type == "function_definition":
                fns, fn_statements = build_function(
                    defn, extract_decorators(decs, source), source, path,
                    parent_id=cid, class_name=name, seen_ids=seen_ids,
                    capture=capture, limit=limit, resolve=resolve,
                )
                methods.extend(fns)
                statements.extend(fn_statements)
                if fns[0].name == "__init__":
                    ctor_params = [
                        ConstructorParam(name=p.name, type=p.type)
                        for p in fns[0].params if p.name not in ("self", "cls")
                    ]
            else:  # class_definition — nested class, extracted parented to this one
                sub_classes, sub_methods, sub_statements = build_class(
                    defn, decs, source, path,
                    parent_id=cid, seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
                )
                nested_classes.extend(sub_classes)
                methods.extend(sub_methods)
                statements.extend(sub_statements)

    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type="class",
        visibility=_visibility(name),
        isAbstract=is_abstract,
        generics=generics,
        extends=bases[0] if bases else None,
        implements=bases[1:],
        constructorParams=ctor_params,
        decorators=extract_decorators(decorator_nodes, source),
        startLine=start,
        endLine=end,
    )
    return [cls, *nested_classes], methods, statements
