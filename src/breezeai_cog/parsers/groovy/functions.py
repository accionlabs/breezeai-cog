"""Groovy method / constructor + parameter + annotation + call extraction.

Differs from Java's model in three ways the dekobon grammar dictates:
* modifiers (``public``/``private``/``static``/``abstract``/``final``/``def``) are
  **inline children** of the declaration, not wrapped in a ``modifiers`` node;
* names are a ``name`` field holding an ``identifier``, or a ``quoted_identifier`` for a
  string-named method (Spock's ``def "order is created"()``);
* a call is a ``method_invocation`` whose ``function`` field is either a bare
  ``identifier`` (``foo()``) or a ``field_access`` (``obj.foo()``).

Default visibility in Groovy is **public** (Java's is package-private).
"""

from __future__ import annotations

from collections.abc import Mapping

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..callresolve import CallResolver, method_types, noop_resolver
from ..detection.api_calls import names_http_client_type
from ..index_common import record_distinct
from ..statements_common import reset_http_client_ids, set_http_client_ids
from ..treesitter import line_span, node_text
from .statements import MEMBER_ACCESS, extract_statements

_VISIBILITY = ("public", "private", "protected")
#: Statement keywords error recovery can leave as a constructor's name (``catch (E e) { }``
#: after a method the grammar broke off).
STATEMENT_WORDS = frozenset({"catch", "finally", "for", "if", "else", "while", "do", "switch",
                             "synchronized", "try", "return"})


def has_declaration_error(node: Node) -> bool:
    """True if the declaration header itself is corrupt — an ``ERROR`` / missing node is a
    **direct** child of the declaration.

    This is the grammar's error-recovery tell: when a preceding construct (e.g. a
    parenthesised-constant enum body) mis-parses, dekobon merges the next field+method into
    one garbled ``method_declaration`` whose name/type/params are fabricated (a field ``ID``
    becomes a method ``ID(...)``). Such a node must NOT be emitted — capturing nothing is a
    known gap, but capturing a method that does not exist is high-confidence wrong data
    (§ reliability: absent beats wrong). A messy method *body* (an error nested deeper) is
    fine — the header is trustworthy — so we check only direct children, and the name: one the
    grammar could not read (``default Class<? extends T> kind()`` leaves a ``MISSING`` inside
    it) would be emitted empty."""
    if any(c.type == "ERROR" or c.is_missing for c in node.children):
        return True
    name = node.child_by_field_name("name")
    return name is not None and (name.has_error or not name.text)


_INTERPOLATIONS = ("gstring_dollar_interpolation", "gstring_brace_interpolation")


def name_text(name_node: Node | None, source: bytes) -> str:
    """A method name as Groovy reads it. A string name (``def "order is created"()``, called
    as ``this."order is created"()``) is a ``quoted_identifier``; its name is the text inside
    the quotes."""
    if name_node is None:
        return ""
    name = node_text(name_node, source)
    if name_node.type == "quoted_identifier":
        for quote in ('"""', "'''", '"', "'"):
            if len(name) >= 2 * len(quote) and name.startswith(quote) and name.endswith(quote):
                return name[len(quote):-len(quote)]
    return name


def decl_name(node: Node, source: bytes) -> str:
    """A declaration's name; a string-named method's is the text inside its quotes."""
    return name_text(node.child_by_field_name("name"), source)


def _flags(node: Node) -> tuple[str, bool, bool]:
    """(visibility, is_static, is_abstract) from a declaration's inline modifier children.
    Groovy defaults to public."""
    visibility, is_static, is_abstract = "public", False, False
    for child in node.children:
        if child.type in _VISIBILITY:
            visibility = child.type
        elif child.type == "static":
            is_static = True
        elif child.type == "abstract":
            is_abstract = True
    return visibility, is_static, is_abstract


def _annotation(node: Node, source: bytes) -> Decorator:
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source).rsplit(".", 1)[-1] if name_node is not None else ""
    args: list[str] = []
    arglist = node.child_by_field_name("arguments")
    if arglist is not None:
        for arg in arglist.named_children:
            text = node_text(arg, source)
            if arg.type == "string_literal":
                text = text.strip('"').strip("'")
            args.append(text)
    return Decorator(name=name, args=args)


def extract_annotations(node: Node, source: bytes) -> list[Decorator]:
    """Annotations are direct children of the declaration in Groovy."""
    return [_annotation(c, source) for c in node.named_children if c.type == "annotation"]


def extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    out: list[Parameter] = []
    if params_node is None:
        return out
    for p in params_node.named_children:
        if p.type != "formal_parameter":
            continue
        tnode = p.child_by_field_name("type")
        nnode = p.child_by_field_name("name")
        out.append(Parameter(
            name=node_text(nnode, source) if nnode is not None else "",
            type=node_text(tnode, source) if tnode is not None else "",
            decorators=extract_annotations(p, source),
        ))
    return out


def _callee(call: Node, source: bytes) -> tuple[str, str | None, bool]:
    """(method_name, receiver, resolvable) for a ``method_invocation``. ``function`` is a bare
    ``identifier`` (receiver ``None``) or a member access: ``obj.method``, ``obj?.method`` or
    ``list*.method``. A spread call runs the method on each element, so the receiver's own type
    says nothing about it: it is not resolvable."""
    if call.type == "command_chain":  # ``println "done"`` — the call's name is its receiver field
        name = call.child_by_field_name("receiver")
        return (node_text(name, source), None, True) if name is not None else ("", None, False)
    fn = call.child_by_field_name("function")
    if fn is None:
        return "", None, False
    fields = MEMBER_ACCESS.get(fn.type)
    if fields is not None:
        obj = fn.child_by_field_name(fields[0])
        member = fn.child_by_field_name(fields[1])
        if member is not None and any(c.type in _INTERPOLATIONS for c in member.named_children):
            return "", None, False  # ``this."find$field"()`` — only known at run time
        method = name_text(member, source) if member is not None else ""
        receiver = node_text(obj, source) if obj is not None else None
        return method, receiver, fn.type != "spread_dot_expression"
    return node_text(fn, source), None, True


def _calls(
    body: Node | None, source: bytes, resolve: CallResolver = noop_resolver,
    scope: Mapping[str, str | None] | None = None, owner: str | None = None,
) -> list[Call]:
    """The calls in a method body; ``scope`` is the method's own variable types, ``owner`` the
    simple name of the class it is declared in: a bare, ``this`` or ``super`` call may mean
    one of that class's own or inherited methods."""
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[tuple[str, str | None]] = set()  # one entry per name and target file

    def visit(node: Node, inner: bool) -> None:
        # Descend into every scope, including inline closures — their calls belong to
        # the nearest named enclosing function (mirrors Java lambda handling). Inside an
        # anonymous class body (`inner`), `this` and a bare name mean that class's members.
        for child in node.named_children:
            if child.type in ("method_invocation", "command_chain"):
                name, receiver, resolvable = _callee(child, source)
                if name:
                    path = (resolve(name, receiver, None if inner else owner, scope)
                            if resolvable else None)
                    if (name, path) not in seen:
                        seen.add((name, path))
                        calls.append(Call(name=name, path=path))
            visit(child, inner or child.type == "class_body")

    visit(body, False)
    return calls


def _decl_types() -> set[str]:
    return {
        "method_declaration", "constructor_declaration", "class_declaration",
        "interface_declaration", "enum_declaration", "trait_declaration",
    }


def defined_names(root: Node, source: bytes) -> set[str]:
    """Method/constructor/class/interface/enum/trait names defined in the file."""
    names: set[str] = set()
    types = _decl_types()

    def walk(n: Node) -> None:
        for c in n.named_children:
            if has_declaration_error(c):
                continue  # corrupt header → fabricated name; don't feed same-file resolution
            if c.type in types and c.child_by_field_name("name") is not None:
                names.add(decl_name(c, source))
            walk(c)

    walk(root)
    return names


def type_map(root: Node, source: bytes) -> dict[str, str | None]:
    """Variable name → declared type across the whole file, for a name a method does not
    declare itself (a field it inherits): kept only while every typed declaration of the name
    in the file agrees, else ``None``. Untyped ones (``def x = f()``, ``x -> …``) say nothing;
    a declaration with a corrupt header is skipped."""
    types: dict[str, str | None] = {}

    def walk(n: Node) -> None:
        for c in n.named_children:
            if has_declaration_error(c):
                continue  # corrupt header — do not seed receiver-type resolution
            for name, typ in declarations(c, source):
                if typ is not None:
                    record_distinct(types, name, typ)
            walk(c)

    walk(root)
    return types


#: A nested type the grammar misread as a field has a keyword as its "type".
_KEYWORD_TYPES = frozenset({"class", "interface", "enum", "trait"})


def _created(declarator: Node, source: bytes) -> str | None:
    """``def x = new T(…)``: the type it creates; None for any other initializer."""
    value = declarator.child_by_field_name("value")
    if value is None or value.type != "object_creation_expression":
        return None
    created = value.child_by_field_name("type")
    return node_text(created, source) if created is not None else None


def declarations(node: Node, source: bytes) -> list[tuple[str, str | None]]:
    """The names one declaration node introduces, with their written type (None: untyped —
    ``def x``, ``x ->``, ``for (x in xs)``)."""
    t = node.type
    typ_node = node.child_by_field_name("type")
    typ = node_text(typ_node, source) if typ_node is not None else None
    if t in ("field_declaration", "local_variable_declaration"):
        if has_declaration_error(node) or typ in _KEYWORD_TYPES:
            return []
        out: list[tuple[str, str | None]] = []
        for d in node.named_children:
            if d.type == "variable_declarator" and (nm := d.child_by_field_name("name")) is not None:
                out.append((node_text(nm, source), typ if typ is not None else _created(d, source)))
        # a typed local holds its names directly: `Audit w = null, w2 = null`
        out += [(node_text(n, source), typ) for n in node.children_by_field_name("name")]
        return out
    if t in ("formal_parameter", "catch_formal_parameter", "closure_parameter"):
        nm = node.child_by_field_name("name")
        return [(node_text(nm, source), typ)] if nm is not None else []
    if t == "for_in_statement":
        nm = node.child_by_field_name("variable")
        return [(node_text(nm, source), typ)] if nm is not None else []
    return []


def class_fields(body: Node | None, source: bytes) -> dict[str, str | None]:
    """A class body's own fields and properties → their written type (None for ``def``)."""
    out: dict[str, str | None] = {}
    for member in body.named_children if body is not None else ():
        if member.type == "field_declaration":
            out.update(declarations(member, source))
    return out


def method_scope(
    node: Node, source: bytes, fields: Mapping[str, str | None],
) -> dict[str, str | None]:
    """The variable types a method's calls resolve with (``callresolve.method_types``)."""
    params: list[tuple[str, str | None]] = []
    ps = node.child_by_field_name("parameters")
    for p in ps.named_children if ps is not None else ():
        params.extend(declarations(p, source))
    locals_: list[tuple[str, str | None]] = []

    def walk(n: Node) -> None:
        for c in n.named_children:
            locals_.extend(declarations(c, source))
            walk(c)

    body = node.child_by_field_name("body")
    if body is not None:
        walk(body)
    return method_types(fields, params, locals_)


def _new_client(value: Node | None, source: bytes) -> bool:
    """``new T(…)`` where ``T`` names an HTTP client."""
    if value is None or value.type != "object_creation_expression":
        return False
    created = value.child_by_field_name("type")
    return created is not None and names_http_client_type(node_text(created, source), "groovy")


def client_locals(node: Node, source: bytes, fields: Mapping[str, str | None]) -> frozenset[str]:
    """A method's locals assigned a new HTTP client and nothing else —
    ``OrderApiClient client = new OrderApiClient(uri)``, ``def c = new RESTClient(base)``. A call on one
    is that client's call, as ``new OrderApiClient(uri).put(v)`` is: ``client.put(v)``. A name
    also declared another way in the method (a parameter, another local, a closure or loop
    variable), assigned again, or a field of the class is left out: a call on it may mean something else.
    Fields and parameters typed as a client are not followed: their type may be the
    repository's own wrapper, whose methods take what is no URL."""
    clients: set[str] = set()
    others: set[str] = set(fields)
    ps = node.child_by_field_name("parameters")
    for p in ps.named_children if ps is not None else ():
        others.update(name for name, _ in declarations(p, source))

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type == "local_variable_declaration" and not has_declaration_error(c):
                pairs = [(d.child_by_field_name("name"), d.child_by_field_name("value"))
                         for d in c.named_children if d.type == "variable_declarator"]
                names = c.children_by_field_name("name")
                if names:  # a typed local holds its name and value directly
                    pairs.append((names[0], c.child_by_field_name("value") if len(names) == 1 else None))
                for nm, value in pairs:
                    if nm is not None:
                        (clients if _new_client(value, source) else others).add(node_text(nm, source))
            elif c.type == "assignment_expression":  # `client = other`: no longer that client
                left = c.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    others.add(node_text(left, source))
            else:
                others.update(name for name, _ in declarations(c, source))
            walk(c)

    body = node.child_by_field_name("body")
    if body is not None:
        walk(body)
    return frozenset(clients - others)


#: Class-body members whose code runs when the class loads or an object is built.
_INITIALIZATION = frozenset({"field_declaration", "static_initializer", "block", "enum_constant"})


def class_calls(
    body: Node | None, source: bytes, resolve: CallResolver, owner: str | None,
    fields: Mapping[str, str | None],
) -> list[Call]:
    """The calls of a class's code outside any method — its field initializers (closures
    stored in a field included), its ``static { … }`` and ``{ … }`` initializer blocks and its
    enum constants' arguments — in the shape of ``Function.calls``: one per name and target
    file, in order of first appearance. Methods, constructors and nested types have their own.
    Variables resolve with the class's ``fields`` and the names declared inside those members."""
    if body is None:
        return []
    members = [m for m in body.named_children
               if m.type in _INITIALIZATION and not has_declaration_error(m)]
    locals_: list[tuple[str, str | None]] = []

    def walk(n: Node) -> None:
        for c in n.named_children:
            locals_.extend(declarations(c, source))
            walk(c)

    for member in members:
        walk(member)
    scope = method_types(fields, [], locals_)
    calls: list[Call] = []
    seen: set[tuple[str, str | None]] = set()
    for member in members:
        for call in _calls(member, source, resolve, scope, owner):
            if (call.name, call.path) not in seen:
                seen.add((call.name, call.path))
                calls.append(call)
    return calls


def build_method(
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
    fields: Mapping[str, str | None] | None = None,
    constructor: bool = False,
) -> tuple[Function, list[Statement]]:
    """``fields``: the enclosing class's fields (outer classes' included), for the method's
    variable types; a script method has none of its own. ``constructor``: the node is one of its
    class's constructors (the caller checks the name; the grammar's ``constructor_declaration``
    alone does not tell)."""
    name = decl_name(node, source)
    start, end = line_span(node)
    # The id carries the qualified owner (`Outer.Inner`) so a nested type's methods are
    # identified unambiguously; `class_name` stays simple for call resolution.
    fid = disambiguate(
        function_id(path, name, start, class_name=id_owner or class_name), seen_ids)
    visibility, is_static, _ = _flags(node)
    ret = node.child_by_field_name("return_type")
    body = node.child_by_field_name("body")
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type="constructor" if constructor else "method",
        visibility=visibility,
        isStatic=is_static,
        params=extract_params(node.child_by_field_name("parameters"), source),
        decorators=extract_annotations(node, source),
        returnType=node_text(ret, source) if ret is not None else None,
        startLine=start,
        endLine=end,
        calls=_calls(body, source, resolve, method_scope(node, source, fields or {}), class_name),
    )
    token = set_http_client_ids(client_locals(node, source, fields or {}) if capture else frozenset())
    try:
        statements = extract_statements(
            body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids,
            descend_all=True,  # walk inline closures — attribute their statements here
        )
    finally:
        reset_http_client_ids(token)
    return fn, statements


def build_function(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
) -> tuple[Function, list[Statement]]:
    """Top-level (script) method — Groovy files may declare methods outside any class."""
    return build_method(
        node, source, path, parent_id=parent_id, class_name=None,
        seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
    )
