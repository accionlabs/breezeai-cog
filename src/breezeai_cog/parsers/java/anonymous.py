"""Java anonymous classes (``new T(…) { … }``) → a Class of their own, with their methods.

Each anonymous class body becomes a Class parented to the file, like a named nested type. Its
name follows ``javac``: ``Outer$1``, ``Outer$2`` … in source order within the named class that
holds it, and ``Outer$1$1`` for one nested in another, so ids are stable across runs on the same
source. Its methods become Functions under it with their own calls and statements; the method or
field that creates it keeps only its own (see ``build_method``'s ``own_anonymous``).

``metadata`` carries ``anonymous: true`` and ``instantiates`` (the type after ``new``, as
written). That type is the class's ``extends`` only when it is certainly a class (constructor
arguments are given, or this file declares it as a class) and its ``implements`` only when this
file declares it as an interface; otherwise neither is set (``new Handler<T>() { … }`` may be
either).

Calls in its methods resolve as they did when they were attributed to the enclosing method: the
enclosing named class stays the owner the resolver sees through the anonymous class (``through``),
and a method written inside a named method sees that method's variables (``scope_node``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..index_common import record_distinct
from ..treesitter import line_span, node_text
from .functions import build_method, class_fields, class_members
from .statements import anonymous_bodies, extract_statements

_TYPE_DECLS = (
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
)
_METHODS = ("method_declaration", "constructor_declaration")
_COMMENTS = ("line_comment", "block_comment")

#: ``classes.build_class``, passed in (it imports this module): builds a type declared in an
#: anonymous class body, named ``Outer$1.Inner``.
BuildType = Callable[..., tuple[list[Class], list[Function], list[Statement]]]


def declared_kinds(root: Node, source: bytes) -> dict[str, str | None]:
    """Simple name → the declaration node type of each type this file declares, ``None`` when two
    of its types share the name."""
    kinds: dict[str, str | None] = {}

    def walk(n: Node) -> None:
        for child in n.named_children:
            if child.type in _TYPE_DECLS:
                nm = child.child_by_field_name("name")
                if nm is not None:
                    record_distinct(kinds, node_text(nm, source), child.type)
            walk(child)

    walk(root)
    return kinds


def _simple(type_text: str) -> str:
    return "".join(type_text.split()).split("<", 1)[0].rsplit(".", 1)[-1]


def _heritage(
    creation: Node,
    created: str | None,
    kinds: Mapping[str, str | None],
) -> tuple[str | None, list[str]]:
    if created is None:
        return None, []
    args = creation.child_by_field_name("arguments")
    if args is not None and any(c.type not in _COMMENTS for c in args.named_children):
        return created, []  # an interface takes no constructor arguments
    kind = kinds.get(_simple(created))
    if kind == "class_declaration":
        return created, []
    if kind == "interface_declaration":
        return None, [created]
    return None, []


def _through(member: Node, stop: Node, source: bytes) -> tuple[str | None, ...]:
    """The anonymous and local classes ``member`` is declared in, innermost first, up to the named
    class body ``stop``: each anonymous class's type as written, ``None`` for any other body."""
    chain: list[str | None] = []
    n = member.parent
    while n is not None and n != stop:
        if n.type == "class_body":
            p = n.parent
            t = (
                p.child_by_field_name("type")
                if p is not None and p.type == "object_creation_expression"
                else None
            )
            chain.append(node_text(t, source) if t is not None else None)
        n = n.parent
    return tuple(chain)


def build_anonymous(
    body: Node,
    source: bytes,
    path: str,
    *,
    qualified: str,
    owner_class: str,
    parent_id: str,
    stop: Node,
    anchor: Node | None,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
    fields: Mapping[str, str | None] | None = None,
    kinds: Mapping[str, str | None] | None = None,
    build_type: BuildType | None = None,
) -> tuple[list[Class], list[Function], list[Statement]]:
    """One anonymous class body → (classes, methods, statements), its nested anonymous classes
    included. ``qualified`` is its name with the enclosing named classes (``Outer.Inner$1``),
    ``owner_class`` the simple name of the named class holding it (the owner its calls resolve
    with), ``stop`` that class's body, ``anchor`` the named method it is written in (None in a
    field initializer or an initializer block), ``fields`` the enclosing classes' fields."""
    creation = body.parent
    assert creation is not None
    type_node = creation.child_by_field_name("type")
    created = node_text(type_node, source) if type_node is not None else None
    extends, implements = _heritage(creation, created, kinds or {})
    cid = disambiguate(class_id(path, qualified), seen_ids)
    start, end = line_span(creation)

    own_fields = dict(fields or {})
    own_fields.update(class_fields(body, source))  # its own fields hide the enclosing ones

    classes: list[Class] = []
    methods: list[Function] = []
    statements = extract_statements(
        body,
        source,
        path,
        parent_id=cid,
        capture=capture,
        limit=limit,
        seen_ids=seen_ids,
        skip_anonymous=True,
    )
    n = 0
    for member in class_members(body):
        if member.type in _TYPE_DECLS:
            if build_type is not None:  # `new T() { class Line { … } }` → `Outer$1.Line`
                sub_classes, sub_methods, sub_statements = build_type(
                    member,
                    source,
                    path,
                    parent_id=parent_id,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve=resolve,
                    owner=qualified,
                    outer_fields=own_fields,
                    kinds=kinds,
                )
                classes.extend(sub_classes)
                methods.extend(sub_methods)
                statements.extend(sub_statements)
            continue
        if member.type in _METHODS:
            fn, fn_statements = build_method(
                member,
                source,
                path,
                parent_id=cid,
                class_name=owner_class,
                id_owner=qualified,
                seen_ids=seen_ids,
                capture=capture,
                limit=limit,
                resolve=resolve,
                fields=own_fields,
                through=_through(member, stop, source),
                scope_node=anchor,
                own_anonymous=True,
            )
            methods.append(fn)
            statements.extend(fn_statements)
        inner_anchor = anchor if anchor is not None or member.type not in _METHODS else member
        for inner in anonymous_bodies(member):
            n += 1
            sub_classes, sub_methods, sub_statements = build_anonymous(
                inner,
                source,
                path,
                qualified=f"{qualified}${n}",
                owner_class=owner_class,
                parent_id=parent_id,
                stop=stop,
                anchor=inner_anchor,
                seen_ids=seen_ids,
                capture=capture,
                limit=limit,
                resolve=resolve,
                fields=own_fields,
                kinds=kinds,
                build_type=build_type,
            )
            classes.extend(sub_classes)
            methods.extend(sub_methods)
            statements.extend(sub_statements)

    metadata: dict[str, object] = {"anonymous": True}
    if created is not None:
        metadata["instantiates"] = created
    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=qualified.rsplit(".", 1)[-1],
        type="class",
        extends=extends,
        implements=implements,
        metadata=metadata,
        startLine=start,
        endLine=end,
    )
    return [cls, *classes], methods, statements


def anonymous_classes(
    body: Node,
    source: bytes,
    path: str,
    *,
    qualified: str,
    owner_class: str,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
    fields: Mapping[str, str | None] | None = None,
    kinds: Mapping[str, str | None] | None = None,
    build_type: BuildType | None = None,
) -> tuple[list[Class], list[Function], list[Statement]]:
    """Every anonymous class in a named class's body (its member types excluded: they number
    their own), numbered ``qualified$1``, ``qualified$2`` … in source order."""
    classes: list[Class] = []
    methods: list[Function] = []
    statements: list[Statement] = []
    n = 0
    for member in class_members(body):
        if member.type in _TYPE_DECLS:
            continue
        anchor = member if member.type in _METHODS else None
        for inner in anonymous_bodies(member):
            n += 1
            sub_classes, sub_methods, sub_statements = build_anonymous(
                inner,
                source,
                path,
                qualified=f"{qualified}${n}",
                owner_class=owner_class,
                parent_id=parent_id,
                stop=body,
                anchor=anchor,
                seen_ids=seen_ids,
                capture=capture,
                limit=limit,
                resolve=resolve,
                fields=fields,
                kinds=kinds,
                build_type=build_type,
            )
            classes.extend(sub_classes)
            methods.extend(sub_methods)
            statements.extend(sub_statements)
    return classes, methods, statements
