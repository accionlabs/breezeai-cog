"""C and C++ class / struct / union / enum extraction → Class + flat members + statements.

A ``class_specifier`` / ``struct_specifier`` has a ``name`` (type_identifier), an
optional unnamed ``base_class_clause`` child (its ``type_identifier`` bases → the
heritage), and a ``body`` (field_declaration_list). Inside the body, a
``field_declaration`` whose declarator is a ``function_declarator`` is a member-function
declaration (emitted as a flat method ``Function``); one whose declarator is a plain
``field_identifier`` is a member variable (not a function — skipped).

A struct is a class with ``type="struct"``. Members default to ``public`` in a struct
and ``private`` in a class, updated by ``access_specifier`` labels in the body."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ClassType, ConstructorParam, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..statements_common import emit_enum_members, member_statement
from ..treesitter import line_span, node_text, parse_source
from .functions import (
    build_member_function,
    extract_params,
    function_declarator_of,
    is_function_pointer,
    is_macro_call_declarator,
    read_member,
)
from .mappings import MACRO_TYPES
from .statements import macro_statement

#: Aggregate specifiers that become a ``Class`` node, mapped to their ``type``. A union is
#: a distinct kind (overlapping storage, one member active at a time) — it is not a struct,
#: so it carries its own type rather than being flattened to ``struct``.
_AGGREGATE_TYPE: dict[str, ClassType] = {
    "class_specifier": "class",
    "struct_specifier": "struct",
    "union_specifier": "union",
}
_CLASS_TYPES = tuple(_AGGREGATE_TYPE)
#: Type-definition specifiers that can be *nested* inside a class body.
_NESTED_TYPE_SPECS = (*_CLASS_TYPES, "enum_specifier")


def _nested_type_spec(member: Node) -> Node | None:
    """The specifier of a nested type defined in a class body, or ``None`` for a real member.
    A nested ``union Chunk { … };`` / ``enum Kind { … };`` parses as a ``field_declaration``
    whose ``type`` is the specifier (not as a bare specifier), so both forms are unwrapped
    here. A member variable (``Chunk* root;``, or C's ``struct node *next;``, whose type is a
    specifier without a body) returns ``None``."""
    if member.type in _NESTED_TYPE_SPECS:
        return member
    if member.type == "field_declaration":
        tnode = member.child_by_field_name("type")
        if tnode is not None and tnode.type in _NESTED_TYPE_SPECS and _defines(tnode):
            return tnode
    return None


def _defines(spec: Node) -> bool:
    """Whether a struct/class/union/enum specifier has a body (defines the type) rather than
    naming it (``struct node``)."""
    return spec.child_by_field_name("body") is not None or any(
        c.type == "enumerator_list" for c in spec.named_children
    )
#: Class-body members that may be a function: a method with a return type is a
#: ``field_declaration``; a constructor/destructor (no return type) is a ``declaration``;
#: an inline definition (or ``= default``) is a ``function_definition``.
_MEMBER_FN_TYPES = ("field_declaration", "function_definition", "declaration")

#: ``class WORD Name [final] :`` or ``{`` — a word between the class keyword and the class
#: name. Only a candidate: the tree decides (:func:`_class_macro_words`).
_CLASS_HEAD_WITH_WORD = re.compile(
    rb"\b(?:class|struct|union)\s+([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*(?:final\s*)?(?::(?!:)|\{)"
)
_SPECIFIERS = ("class_specifier", "struct_specifier", "union_specifier")

#: A macro's byte span and the class name's byte span after it.
_MacroWord = tuple[tuple[int, int], tuple[int, int]]


def _class_macro_words(root: Node, source: bytes) -> list[_MacroWord]:
    """Macros written between ``class`` and the class name: ``class OPENVINO_API GatherMatmul
    : public Op { … }``. In C++ only an attribute can sit there, and ``alignas`` / ``[[…]]``
    parse; a bare word is an export macro the grammar can't expand. It takes the macro for the
    class name and reads the class as a function or a broken declaration, so the class is lost.
    A word counts only when the grammar did misread it that way: the name of a head with no
    body, whose declaration is a function definition or has an error — a clean ``struct Point
    p{1, 2};`` is a variable."""
    words = []
    for m in _CLASS_HEAD_WITH_WORD.finditer(source):
        word = root.descendant_for_byte_range(m.start(1), m.end(1))
        head = word.parent if word is not None else None
        if (
            word is None or head is None or word.type != "type_identifier"
            or word.start_byte != m.start(1) or word.end_byte != m.end(1)
            or head.type not in _SPECIFIERS or head.child_by_field_name("body") is not None
        ):
            continue
        owner = head.parent
        if owner is not None and (owner.type == "function_definition" or owner.has_error):
            words.append(((m.start(1), m.end(1)), (m.start(2), m.end(2))))
    return words


def _reads_as_class(root: Node, name_span: tuple[int, int]) -> bool:
    """Whether the name at ``name_span`` heads a class with a body, outside any error — the
    class parsed whole once its macro was blanked."""
    name = root.descendant_for_byte_range(*name_span)
    head = name.parent if name is not None else None
    if (
        name is None or head is None or name.type != "type_identifier"
        or (name.start_byte, name.end_byte) != name_span
        or head.type not in _SPECIFIERS or head.child_by_field_name("body") is None
    ):
        return False
    node: Node | None = head
    while node is not None:
        if node.type == "ERROR":
            return False
        node = node.parent
    return True


def parse_cfamily(grammar: str, source: bytes, timeout_micros: int) -> Node:
    """Parse a C-family file. When a macro between ``class`` and the class name hid a class
    (:func:`_class_macro_words`), the file is parsed once more with that word blanked to
    spaces: the class then reads as written without the macro, at the same byte offsets and
    lines, so names and text are still read from the original ``source``. A class that still
    doesn't parse whole without its macro (another macro breaks its body) keeps the macro, and
    stays as uncaptured as before."""
    root = parse_source(grammar, source, timeout_micros).root_node
    words = _class_macro_words(root, source)
    for _ in range(2):  # blank all; if some class still breaks, blank only the others once
        if not words:
            return root
        blanked = bytearray(source)
        for (start, end), _name in words:
            blanked[start:end] = b" " * (end - start)
        reparsed = parse_source(grammar, bytes(blanked), timeout_micros).root_node
        whole = [w for w in words if _reads_as_class(reparsed, w[1])]
        if whole == words:
            return reparsed
        words = whole
    return root


def typedef_type(node: Node, source: bytes) -> tuple[Node, str, str | None] | None:
    """``typedef struct [tag] { … } name;`` (also ``union`` / ``enum``) → the specifier, the
    typedef name and the tag (``None`` when anonymous). ``None`` when the typedef doesn't
    define the type (``typedef struct node node_t;``, no body) or doesn't give it exactly one
    plain name (``typedef struct {…} *list_ptr;``)."""
    if node.type != "type_definition":
        return None
    spec = node.child_by_field_name("type")
    if spec is None or spec.type not in _NESTED_TYPE_SPECS:
        return None
    if not _defines(spec):
        return None
    names = [d for d in node.children_by_field_name("declarator") if d.type == "type_identifier"]
    if len(names) != 1:
        return None
    tag = spec.child_by_field_name("name")
    return spec, node_text(names[0], source), node_text(tag, source) if tag is not None else None


#: The two C++ type-alias declarations. ``using namespace …`` / ``using Ns::name`` are a
#: ``using_declaration`` instead — an import, deliberately not in this set.
TYPE_ALIAS_TYPES = ("type_definition", "alias_declaration")


def _base_name(base: Node, source: bytes) -> str | None:
    """The base-class name from a ``base_class_clause`` entry — a ``type_identifier``,
    the head of a ``template_type`` (``Base<T>`` → ``Base``), or a ``qualified_identifier``."""
    if base.type == "type_identifier":
        return node_text(base, source)
    if base.type == "template_type":
        ti = base.child_by_field_name("name") or next(
            (c for c in base.named_children if c.type == "type_identifier"), None
        )
        return node_text(ti, source) if ti is not None else None
    if base.type == "qualified_identifier":
        return node_text(base, source)
    return None


def _heritage(node: Node, source: bytes) -> tuple[str | None, list[str]]:
    """(extends, implements) from the ``base_class_clause``. C++ has multiple
    inheritance — the first base is ``extends``, the rest are ``implements``."""
    clause = next((c for c in node.named_children if c.type == "base_class_clause"), None)
    if clause is None:
        return None, []
    bases: list[str] = []
    for c in clause.named_children:
        name = _base_name(c, source)
        if name is not None:
            bases.append(name)
    if not bases:
        return None, []
    return bases[0], bases[1:]


#: Nodes that *are* a declared name. ``type_identifier`` covers a ``typedef``'s new type
#: name (``typedef int Handle`` → ``Handle``), which is never how a variable/field name is
#: spelled — a field's name is an ``identifier``/``field_identifier``, and its type rides
#: the separate ``type`` field — so accepting it here is unambiguous.
_NAME_NODES = ("field_identifier", "identifier", "type_identifier")


def _declarator_name(node: Node | None, source: bytes) -> str | None:
    """Innermost declared name, unwrapping pointer/reference/array/init/function
    declarators (``* kName`` → ``kName``, ``(*Callback)(int)`` → ``Callback``)."""
    if node is None:
        return None
    if node.type in _NAME_NODES:
        return node_text(node, source)
    inner = node.child_by_field_name("declarator") or next(
        (c for c in node.named_children
         if c.type in _NAME_NODES or c.type.endswith("declarator")),
        None,
    )
    return _declarator_name(inner, source) if inner is not None else None


def type_alias_statement(
    node: Node, source: bytes, path: str, *, parent_id: str, limit: int, seen_ids: set[str]
) -> Statement:
    """One flat Statement for a C++ type alias — ``typedef X Y;`` (``type_definition``, the
    new name on its declarator) or ``using Y = X;`` (``alias_declaration``, a ``name``
    field). The aliased type stays on ``text``, which is what makes it queryable.

    Only these two node types are aliases: ``using namespace std;`` and ``using std::swap;``
    are a ``using_declaration`` (an import, not a declaration) and never reach here."""
    declarator = node.child_by_field_name("declarator")
    name = (
        _declarator_name(node.child_by_field_name("name"), source)
        if node.type == "alias_declaration"
        # `typedef voidpf (*open_func) OF((voidpf opaque));` would be named after the macro.
        else None if is_macro_call_declarator(declarator)
        else _declarator_name(declarator, source)
    )
    return member_statement(
        node, source, path, parent_id=parent_id, limit=limit, seen_ids=seen_ids, name=name
    )


def build_class(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
    owner: str | None = None,
    name: str | None = None,
    alias: str | None = None,
) -> tuple[list[Class], list[Function], list[Statement]]:
    """Return (classes, methods, statements) — all flat, linked by parentId. The class
    list is this class plus any nested (member) classes/structs parented to it. ``name``
    names the class instead of its tag (a ``typedef``'s name, see :func:`typedef_type`);
    ``alias`` is the tag when it differs."""
    if name is None:
        name_node = node.child_by_field_name("name")
        if name_node is None:  # anonymous struct/class — no name to emit; skip (honest gap)
            return [], [], []
        name = node_text(name_node, source)
    if node.child_by_field_name("body") is None:  # forward declaration (`class Foo;`) — not a
        return [], [], []                          # definition; emitting it fabricates a hollow node
    start, end = line_span(node)
    qualified = f"{owner}.{name}" if owner else name
    cid = disambiguate(class_id(path, qualified), seen_ids)
    extends, implements = _heritage(node, source)
    is_public_default = node.type in ("struct_specifier", "union_specifier")  # both default public

    methods: list[Function] = []
    statements: list[Statement] = []
    nested_classes: list[Class] = []
    ctor_params: list[ConstructorParam] = []

    body = node.child_by_field_name("body")
    if body is not None:
        # A class body holds only declarations (no executable statements), so member
        # functions are extracted directly — nothing goes through extract_statements here.
        visibility = "public" if is_public_default else "private"
        for member in body.named_children:
            member = _unwrap_template(member)
            read = read_member(member)
            if read is None:
                continue  # corrupt declaration header — skip rather than emit fabricated data
            if member.type == "access_specifier":
                visibility = node_text(member, source)
                continue
            if member.type in MACRO_TYPES:
                # A `#define` written inside the class body.
                if capture:
                    statements.append(
                        macro_statement(
                            member, source, path, parent_id=cid, limit=limit, seen_ids=seen_ids,
                        )
                    )
                continue
            if member.type in TYPE_ALIAS_TYPES:
                # A member typedef/alias (`typedef int MemberHandle;`) declares a name on
                # the class and is otherwise captured nowhere.
                if capture:
                    statements.append(
                        type_alias_statement(
                            member, source, path, parent_id=cid, limit=limit, seen_ids=seen_ids,
                        )
                    )
                continue
            spec = _nested_type_spec(member)
            if spec is not None:  # a nested type definition (class/struct/union/enum)
                if spec.type == "enum_specifier":
                    enum_cls, enum_stmts = build_enum(
                        spec, source, path, parent_id=cid, seen_ids=seen_ids,
                        capture=capture, limit=limit,
                    )
                    if enum_cls is not None:
                        nested_classes.append(enum_cls)
                        statements.extend(enum_stmts)
                else:
                    sub_classes, sub_methods, sub_statements = build_class(
                        spec, source, path,
                        parent_id=parent_id, seen_ids=seen_ids, capture=capture, limit=limit,
                        resolve=resolve, owner=qualified,
                    )
                    nested_classes.extend(sub_classes)
                    methods.extend(sub_methods)
                    statements.extend(sub_statements)
            elif member.type in _MEMBER_FN_TYPES:
                declarator = member.child_by_field_name("declarator")
                if function_declarator_of(declarator) is None or is_function_pointer(declarator):
                    # A member variable / constant, not a function — including a pointer to a
                    # function (`size_t (*read)(void *, size_t);`, C's method table). C++ class
                    # fields are otherwise captured nowhere, so emit each as a flat statement
                    # (its `text` — incl. any `= value` initializer — is queryable).
                    if capture and member.type == "field_declaration":
                        statements.append(
                            member_statement(
                                member, source, path, parent_id=cid, limit=limit, seen_ids=seen_ids,
                                name=_declarator_name(member.child_by_field_name("declarator"), source),
                            )
                        )
                    continue
                fn, fn_statements = build_member_function(
                    member, source, path,
                    parent_id=cid, class_name=name, id_owner=qualified, visibility=visibility,
                    seen_ids=seen_ids,
                    capture=capture, limit=limit, resolve=resolve, read=read,
                )
                methods.append(fn)
                statements.extend(fn_statements)
                if fn.type == "constructor" and not ctor_params:
                    fd = function_declarator_of(member.child_by_field_name("declarator"))
                    ctor_params = [
                        ConstructorParam(name=p.name, type=p.type)
                        for p in extract_params(
                            fd.child_by_field_name("parameters") if fd is not None else None, source
                        )
                    ]

    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type=_AGGREGATE_TYPE.get(node.type, "class"),
        extends=extends,
        implements=implements,
        constructorParams=ctor_params,
        startLine=start,
        endLine=end,
        alias=alias,
    )
    return [cls, *nested_classes], methods, statements


def build_enum(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    name: str | None = None,
    alias: str | None = None,
) -> tuple[Class | None, list[Statement]]:
    """An ``enum_specifier`` → a ``Class`` of type ``enum`` plus one flat Statement per
    ``enumerator`` member (parented to the enum, gated by statement capture; the member's
    ``= value`` stays inside its ``text``). Anonymous enums and forward declarations (``enum
    Color;`` — no ``enumerator_list``) emit nothing (honest gap, no hollow node). ``enum
    class`` / ``enum struct`` are scoped enums, captured the same way (scoped-ness not stored).
    ``name`` names the enum instead of its tag (a ``typedef``'s name); ``alias`` is the tag
    when it differs."""
    if name is None:
        name_node = node.child_by_field_name("name")
        if name_node is None:  # anonymous enum — no name to emit
            return None, []
        name = node_text(name_node, source)
    body = next((c for c in node.named_children if c.type == "enumerator_list"), None)
    if body is None:
        return None, []  # forward declaration, not a definition
    start, end = line_span(node)
    cid = disambiguate(class_id(path, name), seen_ids)
    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type="enum",
        startLine=start,
        endLine=end,
        alias=alias,
    )
    statements = (
        emit_enum_members(
            body, source, path,
            member_types={"enumerator"}, parent_id=cid, limit=limit, seen_ids=seen_ids,
        )
        if capture
        else []
    )
    return cls, statements


def _unwrap_template(node: Node) -> Node:
    """A ``template_declaration`` wraps its class/struct/function/alias — return the inner
    declaration so it is classified normally (else return the node unchanged). The alias
    case is the C++11 alias template, ``template<class T> using Ptr = T*;``."""
    if node.type != "template_declaration":
        return node
    inner = next(
        (c for c in node.named_children
         if c.type in (*_CLASS_TYPES, "function_definition", "field_declaration",
                       *TYPE_ALIAS_TYPES)),
        None,
    )
    return inner if inner is not None else node
