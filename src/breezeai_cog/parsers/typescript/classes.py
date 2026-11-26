"""Class / interface / enum extraction (TS/JS). Returns the Class plus its methods
(flat Functions linked by parentId) and statements."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ConstructorParam, Function, Statement
from ..statements_common import emit_enum_members, member_statement
from ..treesitter import line_span, node_text
from ..callresolve import CallResolver, noop_resolver
from .decorators import extract_decorators
from .functions import (
    NESTED_FN_VALUE_TYPES,
    build_function,
    collect_nested_functions,
    extract_params,
    _calls as body_calls,
    track_built,
)
from .statements import collect_typed_db_receivers, extract_statements

_TYPE = {
    "class_declaration": "class",
    "class": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
}

#: Interface members captured as flat Statements. A `method_signature` is captured as a
#: Function instead (it is a callable member, resolvable to implementers); these are not.
_INTERFACE_STMT_MEMBERS = {
    "property_signature",   # field:     `name: string`
    "call_signature",       # callable:  `(e: string): void`
    "construct_signature",  # newable:   `new (id: string): T`
    "index_signature",      # index:     `[key: string]: unknown`
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
    default_name: str | None = None,
) -> tuple[Class, list[Function], list[Statement]]:
    """``default_name``: the name of a class written without one (``export default class {…}``
    → ``default``); the Class is then ``isAnonymous``."""
    name_node = cnode.child_by_field_name("name")
    name = node_text(name_node, source) if name_node is not None else ""
    anonymous = not name and default_name is not None
    if anonymous:
        name = default_name
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
    is_interface = cnode.type == "interface_declaration"

    body = cnode.child_by_field_name("body")
    if body is not None:
        # Collect ORM-typed field names from the constructor so HIGH_COLLISION verbs
        # (find/create/save/…) are only tagged as db_method_call when the receiver is
        # a field typed as Repository/DataSource/EntityManager/etc. — not stateManager,
        # itemsCollection, or other non-DB names that happen to have a DB-suffix.
        typed_db_ids = collect_typed_db_receivers(body, source)
        # Function-valued fields (`handle = () => {}`) are each extracted as a Function below,
        # so their field statement would repeat it.
        field_functions = frozenset(
            (value.start_byte, value.end_byte)
            for child in body.named_children
            if child.type in ("public_field_definition", "field_definition")
            and (value := child.child_by_field_name("value")) is not None
            and value.type in NESTED_FN_VALUE_TYPES
        )
        # The class's own statements are listed first, its methods' after (as a function's
        # nested ones follow its own): a detector enriching "the statement on this line" then
        # finds the class-level call. They are collected after the methods are built, so
        # that the code inside methods and functions is left out (below).
        own_at = len(statements)
        with track_built() as built_spans:
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
                        resolve=resolve, typed_db_ids=typed_db_ids,
                    )
                    methods.extend(fns)
                    statements.extend(fn_statements)
                    if mname == "constructor":
                        ctor_params = [
                            ConstructorParam(name=p.name, type=p.type)
                            for p in extract_params(child.child_by_field_name("parameters"), source)
                            if p.type is not None
                        ]
                elif child.type in ("public_field_definition", "field_definition"):
                    # Arrow/function class field: `handleClick = () => {}` (React handler idiom).
                    # These sit outside method bodies, so build_function's recursion never reaches
                    # them; seed each as a method named by its field key.
                    value = child.child_by_field_name("value")
                    if value is not None and value.type in NESTED_FN_VALUE_TYPES:
                        fname_node = child.child_by_field_name("name")
                        fns, fn_statements = build_function(
                            value, name=node_text(fname_node, source) if fname_node is not None else "",
                            kind=value.type, decorators=extract_decorators(pending, source),
                            source=source, path=path, parent_id=cid, class_name=name,
                            seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
                            typed_db_ids=typed_db_ids,
                        )
                        methods.extend(fns)
                        statements.extend(fn_statements)
                    elif value is not None:
                        # Class-level code (`matchers = { name: (a, b) => … }`): a function with a
                        # name of its own, or bound to a name in it, is a method-level Function of
                        # the class; the rest is the class's own (Class.calls, below).
                        _nested_members(value, name, cid, source, path, seen_ids, capture, limit,
                                        resolve, typed_db_ids, methods, statements)
                elif child.type == "class_static_block":
                    # `static { register(() => …); }`: runs when the class loads — the class's code.
                    _nested_members(child, name, cid, source, path, seen_ids, capture, limit,
                                    resolve, typed_db_ids, methods, statements)
                elif child.type == "abstract_method_signature" or (
                        is_interface and child.type == "method_signature"):
                    # Interface method signature, or an abstract class's `abstract m(): T;` (no
                    # body). The same contract shape — a callable member with params + return type; emit as
                    # a Function (calls:[]) so callers of the interface method resolve to
                    # implementers via IMPLEMENTS + name match. Other member kinds (field +
                    # call/construct/index signatures) fall to the Statement branch below.
                    # Guarded to interfaces: a class body's `method_signature` is an overload
                    # sig, redundant with its method_definition — never a duplicate Function.
                    mname_node = child.child_by_field_name("name")
                    mname = node_text(mname_node, source) if mname_node is not None else ""
                    fns, fn_statements = build_function(
                        child, name=mname, kind="method",
                        decorators=extract_decorators(pending, source), source=source, path=path,
                        parent_id=cid, class_name=name, seen_ids=seen_ids, capture=capture, limit=limit,
                        resolve=resolve, typed_db_ids=typed_db_ids,
                    )
                    methods.extend(fns)
                    statements.extend(fn_statements)
                elif is_interface and capture and child.type in _INTERFACE_STMT_MEMBERS:
                    # Every interface member that isn't a callable method → flat Statement
                    # (raw nodeType + full text), like a class field. Covers the data field
                    # (property_signature) and the type-level contracts (call/construct/index
                    # signatures) — nothing is dropped. The latter three have no member name,
                    # so their Statement carries name=None but keeps the full source text.
                    statements.append(
                        member_statement(child, source, path, parent_id=cid, limit=limit,
                                         seen_ids=seen_ids)
                    )
                pending = []
        # The class's own code: outside its methods and function fields, nameless callbacks
        # included — its statements, and its calls as Class.calls.
        owned = frozenset(built_spans)
        statements[own_at:own_at] = extract_statements(
            body, source, path, parent_id=cid, capture=capture, limit=limit, seen_ids=seen_ids,
            built=field_functions, typed_db_ids=typed_db_ids, descend_all=True, barriers=owned,
            opaque=_CLASS_OPAQUE,
        )
        class_calls = body_calls(body, source, resolve, name, owned, opaque=_CLASS_OPAQUE)
    else:
        class_calls = []

    # Enum members become flat statements parented to the enum Class (their `text` — incl.
    # any `= value` — is queryable). Gated by statement capture like every other statement.
    if capture and cnode.type == "enum_declaration":
        ebody = cnode.child_by_field_name("body") or next(
            (c for c in cnode.named_children if c.type == "enum_body"), None
        )
        if ebody is not None:
            statements.extend(
                emit_enum_members(
                    ebody, source, path,
                    member_types={"enum_assignment", "property_identifier"},
                    parent_id=cid, limit=limit, seen_ids=seen_ids,
                )
            )

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
        calls=class_calls or None,
        isAnonymous=True if anonymous else None,
        startLine=start,
        endLine=end,
    )
    return cls, methods, statements


#: What a class's own code does not enter: a nested class (its code is its own), a decorator
#: (not a call the class makes), a method's or an overload's signature.
_CLASS_OPAQUE = frozenset({"class_declaration", "class", "abstract_class_declaration",
                           "decorator"})


def _nested_members(
    node: Node, class_name: str, cid: str, source: bytes, path: str, seen_ids: set[str],
    capture: bool, limit: int, resolve: CallResolver, typed_db_ids, methods: list[Function],
    statements: list[Statement],
) -> None:
    """The named functions inside class-level code (a field initializer, a static block),
    each a Function of the class; anonymous callbacks are walked through."""
    for value_node, nested_name, nested_kind in collect_nested_functions(node, source):
        fns, fn_statements = build_function(
            value_node, name=nested_name, kind=nested_kind, decorators=[], source=source,
            path=path, parent_id=cid, class_name=class_name, seen_ids=seen_ids,
            capture=capture, limit=limit, resolve=resolve, typed_db_ids=typed_db_ids,
        )
        methods.extend(fns)
        statements.extend(fn_statements)
