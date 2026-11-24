"""Java anonymous classes (``new T(…) { … }``) → a Class of their own, with their methods.

Each anonymous class body becomes a Class parented to the file, like a named nested type, with
``isAnonymous: true``. Its name:

* stored in a field (``T onError = new T() { … };``, the whole initializer): the field's name,
  ``Outer.onError``. The Class spans the field declaration and carries its annotations, and the
  declaration is not also a statement (a declaration that stores anything else still is);
* an enum constant's body (``RED("r") { … }``, a static field of the enum): the constant's name,
  ``Color.RED``, extending the enum. The Class spans the constant; the constant stays an
  ``enum_member`` statement of the enum;
* anywhere else (an argument, a ``return``, a local variable, part of a larger expression):
  ``Outer$1``, ``Outer$2`` … in source order within the named class that holds it, and
  ``Outer$1$1`` for one nested in another. A field-named class takes no number.

So ids are stable across runs on the same source. Its methods become Functions under it with
their own calls and statements; the method or field that creates it keeps only its own (see
``build_method``'s ``own_anonymous``).

The type after ``new`` is the class's ``extends`` when it is certainly a class and its
``implements`` when it is certainly an interface (:func:`type_kinds`); otherwise neither is set:
``new Handler<T>() { … }`` with ``Handler`` from a library may be either.

Calls in its methods resolve as they did when they were attributed to the enclosing method: the
enclosing named class stays the owner the resolver sees through the anonymous class (``through``),
and a method written inside a named method sees that method's variables (``scope_node``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Call, Class, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..index_common import record_distinct
from ..treesitter import line_span, node_text
from .functions import (
    build_method,
    class_fields,
    class_members,
    extract_annotations,
    modifiers_node,
    statement_calls,
)
from .heritage import file_imports
from .statements import anonymous_bodies, extract_statements, stored_field

_TYPE_DECLS = (
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
)
_METHODS = ("method_declaration", "constructor_declaration")
_COMMENTS = ("line_comment", "block_comment")
_CLASS, _INTERFACE = "class_declaration", "interface_declaration"

#: JDK types an anonymous class often instantiates, by FQCN → what they are declared as. A type
#: of a library is not known, so its anonymous class gets neither ``extends`` nor ``implements``.
_JDK_KINDS: dict[str, str] = {
    **dict.fromkeys(
        (
            "java.lang.Runnable",
            "java.lang.Iterable",
            "java.lang.Comparable",
            "java.lang.AutoCloseable",
            "java.lang.reflect.InvocationHandler",
            "java.io.FileFilter",
            "java.io.FilenameFilter",
            "java.util.Comparator",
            "java.util.Iterator",
            "java.util.concurrent.Callable",
            "java.util.concurrent.ThreadFactory",
            "java.util.function.Function",
            "java.util.function.BiFunction",
            "java.util.function.Consumer",
            "java.util.function.BiConsumer",
            "java.util.function.Supplier",
            "java.util.function.Predicate",
        ),
        _INTERFACE,
    ),
    **dict.fromkeys(
        (
            "java.lang.Object",
            "java.lang.Thread",
            "java.util.TimerTask",
            "java.util.ArrayList",
            "java.util.HashMap",
            "java.util.HashSet",
            "java.util.LinkedHashMap",
            "java.util.TreeMap",
        ),
        _CLASS,
    ),
}

#: A type as written in this file (and, optionally, the simple name of the named class it is
#: written in) → its declaration's tree-sitter type, or None when not known.
KindOf = Callable[..., str | None]

#: ``(simple type name, owner class simple name)`` → ``(kind, file)`` of the member type the owner
#: inherits from an in-repo ancestor (``index_common.inherited_member_type``), else None.
InheritedType = Callable[[str, str | None], tuple[str, str] | None]

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


def _package(root: Node, source: bytes) -> str:
    for node in root.named_children:
        if node.type == "package_declaration":
            nm = next(
                (c for c in node.named_children if c.type in ("scoped_identifier", "identifier")),
                None,
            )
            return node_text(nm, source) if nm is not None else ""
    return ""


def type_kinds(
    root: Node, source: bytes, repo: Mapping[str, str | None] | None = None,
    inherited: InheritedType | None = None,
) -> KindOf:
    """What a type written in this file is declared as, found the way Java finds the type: a type
    this file declares, a single-type import, the file's own package, then the on-demand imports
    and ``java.lang`` — each looked up among the repository's types (``repo``: FQCN → kind, from
    the Java index) and :data:`_JDK_KINDS`. A single-type import of a library type, a name two
    on-demand candidates may mean, or a type known nowhere → None.

    Given the ``owner`` it is written in, a simple name none of those finds is looked up last
    among the member types the owner inherits (``inherited``): ``new Handler() { … }`` in a
    subclass of the in-repo class that declares ``Handler``."""
    local = declared_kinds(root, source)
    imports, wildcards = file_imports(root, source)
    package = _package(root, source)
    repo = repo or {}

    def known(fqcn: str) -> tuple[bool, str | None]:
        if fqcn in repo:
            return True, repo[fqcn]  # None: two repository types share the FQCN
        if fqcn in _JDK_KINDS:
            return True, _JDK_KINDS[fqcn]
        return False, None

    def kind_of(written: str, owner: str | None = None) -> str | None:
        kind = _kind_of(written)
        if kind is None and owner is not None and inherited is not None:
            name = "".join(written.split()).split("<", 1)[0]
            if "." not in name and name not in local and name not in imports:
                hit = inherited(name, owner)
                kind = hit[0] if hit is not None else None
        return kind

    def _kind_of(written: str) -> str | None:
        name = "".join(written.split()).split("<", 1)[0]
        first, dot, rest = name.partition(".")
        suffix = dot + rest
        if first in local:  # this file's type, or one nested in it (`Outer.Inner`)
            return local[first] if not rest else local.get(name.rsplit(".", 1)[-1])
        if first in imports:
            return known(imports[first] + suffix)[1]
        found, kind = known((f"{package}.{first}" if package else first) + suffix)
        if found:
            return kind
        if rest:  # written fully qualified: `java.util.TimerTask`
            found, kind = known(name)
            if found:
                return kind
        hits = [
            k
            for f, k in (known(f"{p}.{first}{suffix}") for p in (*wildcards, "java.lang"))
            if f
        ]
        return hits[0] if len(hits) == 1 else None

    return kind_of


def _heritage(
    creation: Node, created: str | None, kind_of: KindOf | None, owner: str | None = None,
) -> tuple[str | None, list[str]]:
    if created is None:
        return None, []
    args = creation.child_by_field_name("arguments")
    if args is not None and any(c.type not in _COMMENTS for c in args.named_children):
        return created, []  # an interface takes no constructor arguments
    kind = kind_of(created, owner) if kind_of is not None else None
    if kind == _CLASS:
        return created, []
    if kind == _INTERFACE:
        return None, [created]
    return None, []


def owner_class_of(qualified: str) -> str:
    """The simple name of the class holding a member named ``qualified`` (``a.Color.RED`` →
    ``Color``)."""
    return qualified.rsplit(".", 2)[-2] if qualified.count(".") else qualified


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


def _names(member: Node, source: bytes, qualified: str, n: int) -> tuple[list[tuple[Node, str]], int]:
    """Each outermost anonymous class body in ``member`` with its name: its field's
    (``qualified.onError``) when it is a field's whole initializer, else the next number
    (``qualified$n``). Returns the pairs and the last number used."""
    out: list[tuple[Node, str]] = []
    for inner in anonymous_bodies(member):
        decl = stored_field(inner)
        if inner.parent is not None and inner.parent.type == "enum_constant":
            decl = inner.parent  # `RED { … }`: the constant is the field
        nm = decl.child_by_field_name("name") if decl is not None else None
        if nm is not None:
            out.append((inner, f"{qualified}.{node_text(nm, source)}"))
        else:
            n += 1
            out.append((inner, f"{qualified}${n}"))
    return out, n


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
    kind_of: KindOf | None = None,
    build_type: BuildType | None = None,
) -> tuple[list[Class], list[Function], list[Statement]]:
    """One anonymous class body → (classes, methods, statements), its nested anonymous classes
    included. ``qualified`` is its name with the enclosing named classes (``Outer.Inner$1``,
    ``Outer.onError``), ``owner_class`` the simple name of the named class holding it (the owner
    its calls resolve with), ``stop`` that class's body, ``anchor`` the named method it is
    written in (None in a field initializer or an initializer block), ``fields`` the enclosing
    classes' fields."""
    creation = body.parent
    assert creation is not None
    extends: str | None
    implements: list[str]
    if creation.type == "enum_constant":  # an enum constant's body subclasses the enum
        extends, implements = owner_class_of(qualified), []
    else:
        type_node = creation.child_by_field_name("type")
        created = node_text(type_node, source) if type_node is not None else None
        extends, implements = _heritage(creation, created, kind_of, owner_class)
    cid = disambiguate(class_id(path, qualified), seen_ids)

    # Stored in a field: the Class is that declaration (its span and annotations); a field
    # declaring several names spans only its own declarator.
    decl = stored_field(body)
    span, decorators = creation, []
    if decl is not None and decl.parent is not None:
        field_decl = decl.parent
        single = len(field_decl.children_by_field_name("declarator")) == 1
        span = field_decl if single else decl
        decorators = extract_annotations(modifiers_node(field_decl), source)
    start, end = line_span(span)

    own_fields = dict(fields or {})
    own_fields.update(class_fields(body, source))  # its own fields hide the enclosing ones

    classes: list[Class] = []
    methods: list[Function] = []

    def calls_for(stmt: Node) -> list[Call]:  # `new HashMap<>() {{ put(…); }}`, a field of it
        return statement_calls(stmt, source, resolve, owner_class, own_fields, own_path=path,
                               kind_of=kind_of, through=_through(stmt, stop, source))

    statements = extract_statements(
        body,
        source,
        path,
        parent_id=cid,
        capture=capture,
        limit=limit,
        seen_ids=seen_ids,
        skip_anonymous=True,
        calls_for=calls_for,
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
                    kind_of=kind_of,
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
                kind_of=kind_of,
            )
            methods.append(fn)
            statements.extend(fn_statements)
        inner_anchor = anchor if anchor is not None or member.type not in _METHODS else member
        named, n = _names(member, source, qualified, n)
        for inner, inner_name in named:
            sub_classes, sub_methods, sub_statements = build_anonymous(
                inner,
                source,
                path,
                qualified=inner_name,
                owner_class=owner_class,
                parent_id=parent_id,
                stop=stop,
                anchor=inner_anchor,
                seen_ids=seen_ids,
                capture=capture,
                limit=limit,
                resolve=resolve,
                fields=own_fields,
                kind_of=kind_of,
                build_type=build_type,
            )
            classes.extend(sub_classes)
            methods.extend(sub_methods)
            statements.extend(sub_statements)

    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=qualified.rsplit(".", 1)[-1],
        type="class",
        isAnonymous=True,
        extends=extends,
        implements=implements,
        decorators=decorators,
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
    kind_of: KindOf | None = None,
    build_type: BuildType | None = None,
) -> tuple[list[Class], list[Function], list[Statement]]:
    """Every anonymous class in a named class's body (its member types excluded: they name
    their own): ``qualified.field`` when stored in a field, else ``qualified$1``,
    ``qualified$2`` … in source order."""
    classes: list[Class] = []
    methods: list[Function] = []
    statements: list[Statement] = []
    n = 0
    for member in class_members(body):
        if member.type in _TYPE_DECLS:
            continue
        anchor = member if member.type in _METHODS else None
        named, n = _names(member, source, qualified, n)
        for inner, inner_name in named:
            sub_classes, sub_methods, sub_statements = build_anonymous(
                inner,
                source,
                path,
                qualified=inner_name,
                owner_class=owner_class,
                parent_id=parent_id,
                stop=body,
                anchor=anchor,
                seen_ids=seen_ids,
                capture=capture,
                limit=limit,
                resolve=resolve,
                fields=fields,
                kind_of=kind_of,
                build_type=build_type,
            )
            classes.extend(sub_classes)
            methods.extend(sub_methods)
            statements.extend(sub_statements)
    return classes, methods, statements
