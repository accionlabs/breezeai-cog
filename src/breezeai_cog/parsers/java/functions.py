"""Java method / constructor + parameter + annotation + call extraction."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..callresolve import CallResolver, method_types, noop_resolver
from ..index_common import record_distinct
from ..treesitter import line_span, node_text
from .statements import extract_statements, is_anonymous_body

#: ``anonymous.KindOf`` (that module imports this one): a type as written, and the simple name
#: of the named class it is written in → its declaration's tree-sitter type, or None.
_KindOf = Callable[..., "str | None"]
_COMMENTS = ("line_comment", "block_comment")


def modifiers_node(node: Node) -> Node | None:
    return next((c for c in node.named_children if c.type == "modifiers"), None)


def _flags(modifiers: Node | None) -> tuple[str, bool]:
    visibility, is_static = "package", False
    if modifiers is not None:
        for child in modifiers.children:
            if child.type in ("public", "private", "protected"):
                visibility = child.type
            elif child.type == "static":
                is_static = True
    return visibility, is_static


def _annotation(node: Node, source: bytes) -> Decorator:
    name = node_text(node.child_by_field_name("name"), source).rsplit(".", 1)[-1]
    args: list[str] = []
    arglist = node.child_by_field_name("arguments")
    if arglist is not None:
        for arg in arglist.named_children:
            text = node_text(arg, source)
            if arg.type == "string_literal":
                text = text.strip('"')
            args.append(text)
    return Decorator(name=name, args=args)


def extract_annotations(modifiers: Node | None, source: bytes) -> list[Decorator]:
    if modifiers is None:
        return []
    return [_annotation(c, source) for c in modifiers.named_children
            if c.type in ("marker_annotation", "annotation")]


def annotation_nodes(declaration: Node | None) -> list[Node]:
    """The annotation nodes of a declaration, in the order of its ``decorators`` (the
    :func:`extract_annotations` of its modifiers): ``decorators[i]`` was read from item ``i``."""
    modifiers = modifiers_node(declaration) if declaration is not None else None
    if modifiers is None:
        return []
    return [c for c in modifiers.named_children if c.type in ("marker_annotation", "annotation")]


def extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    out: list[Parameter] = []
    if params_node is None:
        return out
    for p in params_node.named_children:
        if p.type in ("formal_parameter", "spread_parameter"):
            tnode = p.child_by_field_name("type")
            nnode = p.child_by_field_name("name")
            suffix = ""
            if p.type == "spread_parameter":
                # ``Handler... h``: the grammar names neither the type nor the name here; the
                # type is the child that is not a modifier or the declarator, written with ``...``.
                decl = next((c for c in p.named_children if c.type == "variable_declarator"), None)
                nnode = decl.child_by_field_name("name") if decl is not None else None
                tnode = next((c for c in p.named_children
                              if c.type not in ("modifiers", "variable_declarator", "line_comment",
                                                 "block_comment")), None)
                suffix = "..."
            out.append(Parameter(
                name=node_text(nnode, source) if nnode is not None else "",
                type=node_text(tnode, source) + suffix if tnode is not None else "",
                decorators=extract_annotations(modifiers_node(p), source),
            ))
    return out


def _created_class(
    creation: Node, source: bytes, owner: str | None = None, kind_of: _KindOf | None = None,
) -> str | None:
    """The class ``new X(…)`` / ``new X<T>(…)`` builds, as written without type arguments; None
    for a qualified name (``new a.b.X()``).

    An anonymous class (``new X(…) { … }``) builds X too — its constructor runs first — when X
    is certainly a class: it takes constructor arguments (an interface takes none), or
    ``kind_of`` (written in ``owner``) finds it declared as a class. An interface, or a type
    whose kind is not known (a library's), gives None: it has no constructor to call."""
    typ = creation.child_by_field_name("type")
    if typ is not None and typ.type == "generic_type":
        typ = typ.named_children[0] if typ.named_children else None
    if typ is None or typ.type != "type_identifier":
        return None
    name = node_text(typ, source)
    if any(c.type == "class_body" for c in creation.named_children):
        args = creation.child_by_field_name("arguments")
        if args is not None and any(c.type not in _COMMENTS for c in args.named_children):
            return name
        kind = kind_of(name, owner) if kind_of is not None else None
        return name if kind == "class_declaration" else None
    return name


#: Members of a class body whose code is not the class's own initialization: methods and
#: constructors are Functions, nested types are Classes.
_NOT_INITIALIZATION = frozenset({
    "method_declaration", "constructor_declaration", "compact_constructor_declaration",
    "class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
    "annotation_type_declaration",
})


def class_calls(
    body: Node | None, source: bytes, resolve: CallResolver, owner: str | None,
    fields: Mapping[str, str | None], *, own_path: str, kind_of: _KindOf | None = None,
    through: tuple[str | None, ...] | None = None,
) -> list[Call]:
    """The calls of a class's code outside any method — its field initializers, its
    ``static { … }`` and ``{ … }`` initializer blocks and its enum constants' arguments, the code
    that runs when the class loads or an object is built — in the shape of ``Function.calls``:
    one per name, in order of first appearance. Methods, constructors, nested types and
    anonymous class bodies have their own calls. Variables resolve with the class's ``fields``
    and the locals of its initializer blocks."""
    if body is None:
        return []
    locals_: list[tuple[str, str | None]] = []

    def walk(n: Node) -> None:
        for c in n.named_children:
            locals_.extend(declarations(c, source))
            walk(c)

    for member in class_members(body):
        if member.type in ("static_initializer", "block"):
            walk(member)
    scope = method_types(fields, [], locals_)

    def stop(child: Node) -> bool:
        return child.type in _NOT_INITIALIZATION

    return _calls(body, source, resolve, owner, scope, through=through, own_anonymous=True,
                  own_path=own_path, kind_of=kind_of, stop=stop)


def _calls(
    body: Node | None, source: bytes, resolve: CallResolver = noop_resolver,
    owner: str | None = None, scope: Mapping[str, str | None] | None = None,
    *, through: tuple[str | None, ...] | None = None, own_anonymous: bool = False,
    own_path: str | None = None, kind_of: _KindOf | None = None,
    stop: Callable[[Node], bool] | None = None,
) -> list[Call]:
    """The calls in a method body, one per name in order of first appearance. ``owner`` is the
    simple name of the class it is declared in: a call on ``this`` or ``super``, or a bare call
    (Java's implicit ``this``), may resolve to an inherited method. ``scope`` is the method's own
    variable types (``method_types``).

    A name's ``path`` comes from the first of its calls that resolves: in ``config.getString(k)``
    then ``JsonUtil.getString(o, k)`` the second, in-repo call gives ``getString`` its file.

    ``through``: the body is a method of an anonymous or local class (each enclosing such
    class's type, innermost first), resolved as a call written there. ``own_anonymous``: the
    calls in an anonymous class body are that class's methods' own, so they are left out.
    ``own_path``: this file; given, ``new X(…)`` of a class of another repository file is a
    call named ``X`` (its constructor's Function name) with that file's path — an anonymous
    subclass's too (``_created_class``, with ``kind_of``). ``stop``: a node it is true for is
    not walked (a method or nested type of a class body, :func:`class_calls`)."""
    if body is None:
        return []
    calls: list[Call] = []
    at: dict[str, int] = {}
    # In an anonymous or local class body `this.x` is that class's field, not the outer one's.
    inner_scope = None if scope is None else {k: v for k, v in scope.items() if not k.startswith("this.")}

    def target(name: str, receiver: str | None, through: tuple[str | None, ...] | None) -> str | None:
        inner = through is not None
        if receiver is not None and receiver.endswith(".this"):
            # `Outer.this.m()` compiles only when `Outer` is this class or an enclosing one, so
            # it is `this.m()` written in `Outer`, inside an anonymous class body or not.
            qualifier = receiver[: -len(".this")].rsplit(".", 1)[-1]
            return resolve(name, "this", qualifier) if qualifier else None
        if inner:
            path = resolve(name, receiver, None, inner_scope, field_owner=owner, through=through)
        else:
            path = resolve(name, receiver, owner, scope)
        if path is None and receiver is None and owner is not None:
            path = resolve(name, "this", owner)  # `foo()` is `this.foo()`
        return path

    def visit(node: Node, through: tuple[str | None, ...] | None) -> None:
        # Descend into every scope, including inline lambdas — their calls belong to
        # the nearest named enclosing function (see build_function). Inside an anonymous or
        # local class body (`through` is not None: each enclosing such class's type, innermost
        # first, None for a local class), `this` and a bare name may mean that class's own
        # members, so the call is not credited to the enclosing class's own methods.
        for child in node.named_children:
            if own_anonymous and is_anonymous_body(child):
                continue
            if stop is not None and stop(child):
                continue
            if child.type == "object_creation_expression" and own_path is not None:
                built = _created_class(child, source, owner, kind_of)
                # `new X(…)` calls X's constructor (a Function named X) when X resolves as Java
                # resolves a type name (an import, this package, a member type the class
                # inherits) to another file of the repository. A library class or one of this
                # file adds nothing.
                if built is not None and (built not in at or calls[at[built]].path is None):
                    path = resolve(built, None)
                    if path is None and owner is not None:
                        path = resolve(built, None, owner, as_type=True)
                    if path is not None and path != own_path:
                        if built not in at:
                            at[built] = len(calls)
                            calls.append(Call(name=built, path=path))
                        else:
                            calls[at[built]] = Call(name=built, path=path)
            if child.type == "method_invocation":
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    name = node_text(name_node, source)
                    obj = child.child_by_field_name("object")
                    receiver = node_text(obj, source) if obj is not None else None
                    if name and (name not in at or calls[at[name]].path is None):
                        path = target(name, receiver, through)
                        if name not in at:
                            at[name] = len(calls)
                            calls.append(Call(name=name, path=path))
                        elif path is not None:
                            calls[at[name]] = Call(name=name, path=path)
            nested = through
            if child.type == "class_body":
                created = node.child_by_field_name("type") \
                    if node.type == "object_creation_expression" else None
                nested = (node_text(created, source) if created is not None else None,
                          *(through or ()))
            visit(child, nested)

    visit(body, through)
    return calls


def class_method_names(root: Node, source: bytes) -> dict[str, set[str]]:
    """Class simple name → the methods a bare call in it can mean in this file: its own and its
    enclosing classes' (an inner class may call its outer class's methods). An anonymous class's
    methods belong to no named class, so a bare call in a named method never lands on them."""
    out: dict[str, set[str]] = {}

    def walk(node: Node, outer: set[str]) -> None:
        for child in node.named_children:
            if child.type in ("class_declaration", "interface_declaration", "enum_declaration",
                              "record_declaration"):
                nm = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                if nm is None or body is None:
                    continue
                own = {node_text(n, source) for m in class_members(body)
                       if m.type == "method_declaration"
                       and (n := m.child_by_field_name("name")) is not None}
                names = own | outer
                out.setdefault(node_text(nm, source), set()).update(names)
                walk(body, names)
                decls = _enum_declarations(body)
                if decls is not None:
                    walk(decls, names)
            elif child.type != "class_body":  # an anonymous class is not a named scope
                walk(child, outer)

    walk(root, set())
    return out


def _enum_declarations(body: Node) -> Node | None:
    return next((c for c in body.named_children if c.type == "enum_body_declarations"), None)


def class_members(body: Node) -> list[Node]:
    """The members of a class body. An enum's fields, constructors, methods and inner types sit
    after its constants in an ``enum_body_declarations`` node; they are members all the same."""
    members: list[Node] = []
    for child in body.named_children:
        if child.type == "enum_body_declarations":
            members.extend(child.named_children)
        else:
            members.append(child)
    return members


def defined_names(root: Node, source: bytes) -> set[str]:
    """Method/constructor/class/interface/enum/record names defined in the file."""
    names: set[str] = set()
    types = {
        "method_declaration", "constructor_declaration", "class_declaration",
        "interface_declaration", "enum_declaration", "record_declaration",
    }

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type in types:
                nm = c.child_by_field_name("name")
                if nm is not None:
                    names.add(node_text(nm, source))
            walk(c)

    walk(root)
    return names


def type_map(root: Node, source: bytes) -> dict[str, str | None]:
    """Variable name → declared type across the whole file, for a name a method does not
    declare itself (a field it inherits): kept only while every typed declaration of the name
    in the file agrees, else ``None``. Untyped ones (``var x = f()``, ``x -> …``) say nothing."""
    types: dict[str, str | None] = {}

    def walk(n: Node) -> None:
        for c in n.named_children:
            for name, typ in declarations(c, source):
                if typ is not None:
                    record_distinct(types, name, typ)
            walk(c)

    walk(root)
    return types


def class_fields(body: Node | None, source: bytes) -> dict[str, str | None]:
    """A class body's own fields → their written type (a record's components included, via
    its ``parameters`` handled by the caller)."""
    out: dict[str, str | None] = {}
    for member in class_members(body) if body is not None else ():
        if member.type in ("field_declaration", "constant_declaration"):
            out.update(declarations(member, source))
    return out


def declarations(node: Node, source: bytes) -> list[tuple[str, str | None]]:
    """The names one declaration node introduces, with their written type (None: untyped or
    not one class type, such as a multi-catch)."""
    def text(n: Node | None) -> str | None:
        return node_text(n, source) if n is not None else None

    t = node.type
    if t in ("field_declaration", "constant_declaration", "local_variable_declaration"):
        typ = text(node.child_by_field_name("type"))
        out = []
        for d in node.children_by_field_name("declarator"):
            nm = d.child_by_field_name("name")
            if nm is None:
                continue
            if typ == "var":  # `var x = new T(…)` is a T; any other initializer is not read
                value = d.child_by_field_name("value")
                created = value.child_by_field_name("type") if value is not None and \
                    value.type == "object_creation_expression" else None
                out.append((node_text(nm, source), text(created)))
            else:
                out.append((node_text(nm, source), typ))
        return out
    if t in ("formal_parameter", "enhanced_for_statement", "resource"):
        nm = node.child_by_field_name("name")
        return [(node_text(nm, source), text(node.child_by_field_name("type")))] if nm is not None else []
    if t == "catch_formal_parameter":
        nm = node.child_by_field_name("name")
        ct = next((c for c in node.named_children if c.type == "catch_type"), None)
        one = ct.named_children[0] if ct is not None and len(ct.named_children) == 1 else None
        return [(node_text(nm, source), text(one))] if nm is not None else []
    if t == "spread_parameter":  # `String... more` is an array, not a String
        vd = next((c for c in node.named_children if c.type == "variable_declarator"), None)
        spread = vd.child_by_field_name("name") if vd is not None else None
        return [(node_text(spread, source), None)] if spread is not None else []
    if t == "instanceof_expression":  # `x instanceof Order o`
        nm = node.child_by_field_name("name")
        return [(node_text(nm, source), text(node.child_by_field_name("right")))] if nm is not None else []
    if t == "lambda_expression":  # untyped lambda parameters: `x -> …`, `(x, y) -> …`
        ps = node.child_by_field_name("parameters")
        if ps is not None and ps.type == "identifier":
            return [(node_text(ps, source), None)]
        if ps is not None and ps.type == "inferred_parameters":
            return [(node_text(c, source), None) for c in ps.named_children if c.type == "identifier"]
    return []


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
    through: tuple[str | None, ...] | None = None,
    scope_node: Node | None = None,
    own_anonymous: bool = False,
    kind_of: _KindOf | None = None,
) -> tuple[Function, list[Statement]]:
    """``fields``: the enclosing class's fields (outer classes' included), for the method's
    variable types; omitted, its calls use the resolver's file-wide types. ``kind_of``: whether
    the type of an anonymous class created in the body is a class (``_created_class``).

    ``own_anonymous``: anonymous classes in the body are captured as their own Class, so their
    calls and statements are not this method's. ``through`` and ``scope_node`` are for a method
    of an anonymous class: the enclosing anonymous or local classes' types (see ``_calls``) and
    the declaration whose variables its calls see (the named method it is written in)."""
    modifiers = modifiers_node(node)
    name = node_text(node.child_by_field_name("name"), source)
    start, end = line_span(node)
    # The id carries the qualified owner (`Outer.Inner`) so a nested type's methods are
    # identified unambiguously; `class_name` stays simple for call resolution.
    fid = disambiguate(
        function_id(path, name, start, class_name=id_owner or class_name), seen_ids)
    visibility, is_static = _flags(modifiers)
    ret = node.child_by_field_name("type")
    body = node.child_by_field_name("body")
    tp = node.child_by_field_name("type_parameters")
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type="constructor" if node.type == "constructor_declaration" else "method",
        visibility=visibility,
        isStatic=is_static,
        generics=node_text(tp, source) if tp is not None else None,
        params=extract_params(node.child_by_field_name("parameters"), source),
        decorators=extract_annotations(modifiers, source),
        returnType=node_text(ret, source) if ret is not None else None,
        startLine=start,
        endLine=end,
        calls=_calls(body, source, resolve, class_name,
                     None if fields is None else method_scope(scope_node or node, source, fields),
                     through=through, own_anonymous=own_anonymous, own_path=path, kind_of=kind_of),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids,
        descend_all=True,  # walk inline lambdas — attribute their statements here
        skip_anonymous=own_anonymous,
    )
    return fn, statements


def method_nodes(root: Node, source: bytes) -> dict[tuple[str, int], Node]:
    """``(name, start line)`` → the method's declaration, the key the Java parser's
    ``Function`` records carry (``name``, ``startLine``)."""
    out: dict[tuple[str, int], Node] = {}
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in ("method_declaration", "constructor_declaration"):
            name = n.child_by_field_name("name")
            if name is not None:
                out[(node_text(name, source), n.start_point[0] + 1)] = n
        stack.extend(n.named_children)
    return out


#: Wrappers that are not part of the payload: async results and HTTP results (contract:
#: ``requestDTO`` / ``responseDTO`` are the declared payload type with these removed).
_NON_PAYLOAD_WRAPPERS = frozenset({
    "Mono", "CompletableFuture", "CompletionStage", "Callable", "DeferredResult", "Uni",
    "ResponseEntity", "HttpEntity", "Optional",
})
#: Declared types that carry no payload: no body, or a response that is not a declared type
#: (a raw ``ResponseEntity``, JAX-RS ``Response``, a Spring MVC view).
_NO_PAYLOAD = frozenset({
    "void", "Void", "?", "ResponseEntity", "HttpEntity", "Response", "ModelAndView",
})
_TYPE_ANNOTATION = re.compile(r"@[\w.]+(\([^()]*\))?\s*")


def payload_type(declared: str | None) -> str | None:
    """A handler's declared request / response type → the payload type as written, with async
    and HTTP-result wrappers removed (``ResponseEntity<List<UserDTO>>`` → ``List<UserDTO>``,
    ``Mono<OrderDto>`` → ``OrderDto``). Lists, arrays, pages, maps and the application's own
    generics stay as declared. None when no payload is declared (``void``,
    ``ResponseEntity<Void>``, JAX-RS ``Response``)."""
    if not declared:
        return None
    t = " ".join(_TYPE_ANNOTATION.sub("", declared).split())
    while "<" in t and t.endswith(">"):
        outer = t[: t.index("<")].strip().rsplit(".", 1)[-1]
        if outer not in _NON_PAYLOAD_WRAPPERS:
            break
        t = t[t.index("<") + 1: -1].strip()
    return None if not t or t.rsplit(".", 1)[-1] in _NO_PAYLOAD else t
