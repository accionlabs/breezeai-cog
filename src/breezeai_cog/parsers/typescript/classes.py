"""Class / interface / enum extraction (TS/JS). Returns the Class plus its methods
(flat Functions linked by parentId) and statements."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ConstructorParam, Function, Statement
from ..treesitter import line_span, node_text
from ..callresolve import CallResolver, noop_resolver
from .functions import build_function, collect_nested_functions, extract_decorators, extract_params
from .statements import extract_statements

_TYPE = {
    "class_declaration": "class",
    "class": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
}


def _heritage(cnode: Node, source: bytes) -> tuple[str | None, list[str]]:
    extends: str | None = None
    implements: list[str] = []
    heritage = next((c for c in cnode.named_children if c.type == "class_heritage"), None)
    if heritage is not None:
        for clause in heritage.named_children:
            if clause.type == "extends_clause":
                value = clause.child_by_field_name("value") or next(
                    (c for c in clause.named_children if c.is_named), None
                )
                if value is not None:
                    extends = node_text(value, source)
            elif clause.type == "implements_clause":
                implements = [node_text(c, source) for c in clause.named_children]
    # interface extends_type_clause
    for clause in cnode.named_children:
        if clause.type == "extends_type_clause" and extends is None:
            names = [node_text(c, source) for c in clause.named_children]
            if names:
                extends = names[0]
                implements.extend(names[1:])
    return extends, implements


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
    name_node = cnode.child_by_field_name("name")
    name = node_text(name_node, source) if name_node is not None else ""
    start, end = line_span(cnode)
    cid = disambiguate(class_id(path, name), seen_ids)
    extends, implements = _heritage(cnode, source)

    # TS has no class-level access modifier — classes are public (module-scoped via export).
    is_abstract = cnode.type == "abstract_class_declaration" or any(
        c.type == "abstract" for c in cnode.children
    )
    tp = cnode.child_by_field_name("type_parameters")
    generics = node_text(tp, source) if tp is not None else None

    methods: list[Function] = []
    statements: list[Statement] = []
    ctor_params: list[ConstructorParam] = []

    body = cnode.child_by_field_name("body")
    if body is not None:
        statements.extend(
            extract_statements(body, source, path, parent_id=cid, capture=capture, limit=limit, seen_ids=seen_ids)
        )
        pending: list[Node] = []
        for child in body.named_children:
            if child.type == "decorator":
                pending.append(child)
                continue
            if child.type == "comment":
                continue  # a comment between a decorator and its method must not drop decorators
            if child.type == "method_definition":
                mname_node = child.child_by_field_name("name")
                mname = node_text(mname_node, source) if mname_node is not None else ""
                fns, fn_statements = build_function(
                    child, name=mname, kind="constructor" if mname == "constructor" else "method",
                    decorators=extract_decorators(pending, source), source=source, path=path,
                    parent_id=cid, class_name=name, seen_ids=seen_ids, capture=capture, limit=limit,
                    resolve=resolve,
                )
                methods.extend(fns)
                statements.extend(fn_statements)
                if mname == "constructor":
                    ctor_params = [
                        ConstructorParam(name=p.name, type=p.type)
                        for p in extract_params(child.child_by_field_name("parameters"), source)
                    ]
            pending = []

    # Named functions living inside class-decorator arguments (NestJS `@Module({ …
    # useFactory: () => … })`, TypeORM `forRootAsync`, etc.). These sit outside every
    # method body, so build_function's per-body recursion never reaches them; collect
    # them here and parent them to the class.
    for dec_node in decorator_nodes:
        for value_node, nested_name, nested_kind in collect_nested_functions(dec_node, source):
            fns, fn_statements = build_function(
                value_node, name=nested_name, kind=nested_kind, decorators=[], source=source,
                path=path, parent_id=cid, class_name=name, seen_ids=seen_ids,
                capture=capture, limit=limit, resolve=resolve,
            )
            methods.extend(fns)
            statements.extend(fn_statements)

    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type=_TYPE.get(cnode.type, "class"),
        visibility="public",
        isAbstract=is_abstract,
        generics=generics,
        extends=extends,
        implements=implements,
        constructorParams=ctor_params,
        decorators=extract_decorators(decorator_nodes, source),
        startLine=start,
        endLine=end,
    )
    return cls, methods, statements
