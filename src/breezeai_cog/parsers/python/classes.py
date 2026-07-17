"""Class extraction — returns the Class plus its methods (Functions whose
``parentId`` is the class id)."""

from __future__ import annotations

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
) -> tuple[Class, list[Function], list[Statement]]:
    """Return (Class, methods, statements). Methods and statements are flat — the
    caller collects them onto ``FileRecord.functions``/``.statements`` (linked by
    parentId), not nested on the Class."""
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
    ctor_params: list[ConstructorParam] = []
    body = cnode.child_by_field_name("body")
    if body is not None:
        # class-level statements (e.g. class variables), parented to the class
        statements.extend(
            extract_statements(body, source, path, parent_id=cid, capture=capture, limit=limit, seen_ids=seen_ids)
        )
        for child in body.named_children:
            defn, decs = _unwrap(child)
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
    return cls, methods, statements
