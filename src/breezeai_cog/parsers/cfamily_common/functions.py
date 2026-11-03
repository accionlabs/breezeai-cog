"""C and C++ function / method + parameter + call extraction.

The grammar shapes this parser relies on (discovered empirically):

* A **free function** is a ``function_definition`` whose ``declarator`` is a
  ``function_declarator`` whose own ``declarator`` is a plain ``identifier``.
* An **out-of-class method definition** (``Judge::decide``) is a
  ``function_definition`` whose ``function_declarator``'s ``declarator`` is a
  ``qualified_identifier`` (``scope`` = the class, ``name`` = the method).
* A **member function declaration** is a ``field_declaration`` inside a class body
  whose ``declarator`` is a ``function_declarator`` (its ``declarator`` a
  ``field_identifier``) — no body.
* Return type is the declaration's ``type`` field; the declarator may be wrapped in a
  ``pointer_declarator`` / ``reference_declarator`` (``int* f()``), unwrapped here.
* A call is a ``call_expression`` whose ``function`` is a bare ``identifier``
  (``foo()``), a ``field_expression`` (``obj.method()`` — ``field`` is the method,
  ``argument`` the receiver), or a ``qualified_identifier`` (``Foo::bar()``).
"""

from __future__ import annotations

from dataclasses import dataclass

from collections.abc import Set as AbstractSet

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .mappings import TRANSPARENT_SCOPES
from .statements import extract_statements
from .types import build_type_map

#: Declarator wrappers that sit between a declaration's ``type`` and the real
#: ``function_declarator`` / name (``int* f()``, ``int& g()``, ``int (*p)()``).
_DECL_WRAPPERS = (
    "pointer_declarator",
    "reference_declarator",
    "parenthesized_declarator",
    "init_declarator",
)


def has_declaration_error(node: Node) -> bool:
    """True if a declaration header is itself corrupt — an ``ERROR`` / missing node is a
    **direct** child.

    C++ translation units frequently parse with ``has_error`` (an unexpanded macro such
    as ``Q_OBJECT``, a preprocessor token the grammar can't place). The grammar's
    error-recovery then merges the surrounding tokens into a garbled declaration whose
    name/type is fabricated. Such a node must NOT be emitted — capturing nothing is a
    known gap, capturing a function that does not exist is high-confidence wrong data
    (reliability: absent beats wrong). A messy *body* (an error nested deeper) is fine —
    the header is trustworthy — so only direct children are checked."""
    return any(c.type == "ERROR" or c.is_missing for c in node.children)


def unwrap_declarator(node: Node | None) -> Node | None:
    """Strip pointer/reference/parenthesized/init wrappers to the inner declarator
    (a ``function_declarator``, ``identifier``, ``qualified_identifier``, or
    ``field_identifier``). Returns ``None`` if the chain dead-ends."""
    seen = 0
    while node is not None and node.type in _DECL_WRAPPERS and seen < 16:
        seen += 1
        nxt = node.child_by_field_name("declarator")
        if nxt is None:  # reference_declarator nests its target as an unnamed child
            nxt = next(
                (c for c in node.named_children if c.type not in ("type_qualifier", "ms_pointer_modifier")),
                None,
            )
        if nxt is None or nxt is node:
            break
        node = nxt
    return node


def function_declarator_of(declarator: Node | None) -> Node | None:
    """The ``function_declarator`` reached by unwrapping ``declarator``, or ``None`` if
    this declaration is not a function (a plain variable/field)."""
    inner = unwrap_declarator(declarator)
    return inner if inner is not None and inner.type == "function_declarator" else None


def is_misread_declarator(declarator: Node | None) -> bool:
    """Whether ``declarator`` is a function returning a function — not valid C or C++, so the
    grammar misread it. ``LUA_API int (lua_gettop) (lua_State *L);`` reads as a function
    ``int`` taking ``lua_gettop``, because ``LUA_API`` is a macro the grammar takes for the
    type. Its names can't be trusted."""
    fd = function_declarator_of(declarator)
    inner = fd.child_by_field_name("declarator") if fd is not None else None
    return inner is not None and inner.type == "function_declarator"


def is_function_pointer(declarator: Node | None) -> bool:
    """Whether ``declarator`` declares a pointer (or reference) *to* a function —
    ``size_t (*read)(void *, size_t)`` — which is a variable, not a function. Its
    ``function_declarator`` wraps a ``parenthesized_declarator`` holding the pointer; a function
    whose name is merely parenthesized (``int (f)(int)``) has no pointer there."""
    fd = function_declarator_of(declarator)
    inner = fd.child_by_field_name("declarator") if fd is not None else None
    while inner is not None and inner.type == "parenthesized_declarator":
        inner = next((c for c in inner.named_children if c.type != "type_qualifier"), None)
        if inner is not None and inner.type in ("pointer_declarator", "reference_declarator"):
            return True
    return False


def _first_identifier(node: Node | None) -> Node | None:
    """First ``identifier`` in ``node``'s subtree — the parameter name inside a
    (possibly pointer/reference/array) declarator. ``None`` for an unnamed parameter."""
    if node is None:
        return None
    if node.type == "identifier":
        return node
    for c in node.named_children:
        found = _first_identifier(c)
        if found is not None:
            return found
    return None


def extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    """Parameters from a ``parameter_list``. Name from the declarator's identifier
    (honest-null ``""`` when unnamed); type from the ``type`` field (bare base type —
    pointer/reference/const markers are dropped, not guessed at)."""
    out: list[Parameter] = []
    if params_node is None:
        return out
    for p in params_node.named_children:
        if p.type not in ("parameter_declaration", "optional_parameter_declaration"):
            continue
        tnode = p.child_by_field_name("type")
        name_node = _first_identifier(p.child_by_field_name("declarator"))
        out.append(Parameter(
            name=node_text(name_node, source) if name_node is not None else "",
            type=node_text(tnode, source) if tnode is not None else "",
        ))
    return out


def _callee(call: Node, source: bytes) -> tuple[str, str | None] | None:
    """(method_name, receiver) for a ``call_expression``. ``function`` is a bare
    ``identifier`` (receiver ``None``), a ``field_expression`` (``obj.method`` — receiver
    is the object), or a ``qualified_identifier`` (``Foo::bar`` — receiver is the scope).
    An unrecognized callee shape returns ``None`` (no fabricated call)."""
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "identifier":
        return node_text(fn, source), None
    if fn.type == "field_expression":
        field = fn.child_by_field_name("field")
        obj = fn.child_by_field_name("argument")
        if field is None:
            return None
        receiver = node_text(obj, source) if obj is not None else None
        return node_text(field, source), receiver
    if fn.type == "qualified_identifier":
        name = fn.child_by_field_name("name")
        scope = fn.child_by_field_name("scope")
        if name is None:
            return None
        return node_text(name, source), node_text(scope, source) if scope is not None else None
    return None


def _declared_variable(declarator: Node | None, source: bytes) -> str | None:
    """The name a declarator declares, through pointer / reference / array / init / function
    declarators (``(*cb)(int)`` → ``cb``)."""
    node = declarator
    for _ in range(16):
        if node is None:
            return None
        if node.type in ("identifier", "field_identifier"):
            return node_text(node, source)
        node = node.child_by_field_name("declarator") or next(
            (c for c in node.named_children
             if c.type == "identifier" or c.type.endswith("declarator")),
            None,
        )
    return None


def variable_names(body: Node | None, params: Node | None, source: bytes) -> set[str]:
    """The names a function's parameters and local variables take, of any type — including
    pointers to functions (``int (*cb)(int)``) and the parameters of lambdas in the body. A
    call to one of them is a call through a variable, never to a function elsewhere. A local
    prototype (``int helper(int);``) declares a function, not a variable, and is left out."""
    names: set[str] = set()

    def add_params(plist: Node) -> None:
        for p in plist.named_children:
            if p.type in ("parameter_declaration", "optional_parameter_declaration"):
                name = _declared_variable(p.child_by_field_name("declarator"), source)
                if name:
                    names.add(name)

    def walk(node: Node) -> None:
        for c in node.named_children:
            if c.type == "declaration":
                for d in c.children_by_field_name("declarator"):
                    if function_declarator_of(d) is not None and not is_function_pointer(d):
                        continue  # a local prototype
                    name = _declared_variable(d, source)
                    if name:
                        names.add(name)
            elif c.type == "parameter_list":
                add_params(c)
            elif c.type == "for_range_loop":
                name = _declared_variable(c.child_by_field_name("declarator"), source)
                if name:
                    names.add(name)
            walk(c)

    if params is not None:
        add_params(params)
    if body is not None:
        walk(body)
    return names


def _calls(
    body: Node | None,
    source: bytes,
    resolve: CallResolver = noop_resolver,
    owner: str | None = None,
    types: dict[str, str | None] | None = None,
    variables: AbstractSet[str] = frozenset(),
) -> list[Call]:
    """Calls in ``body``. ``owner`` is the simple/qualified name of the class the body
    belongs to (``None`` for a free function) — it lets a bare implicit-``this`` call
    ``Foo()`` resolve to ``owner::Foo``; ``types`` maps this body's locals/params to their
    class type so an ``obj.method()`` receiver can be typed. A bare call to one of
    ``variables`` (:func:`variable_names`) calls through that variable and is never resolved."""
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[str] = set()

    def visit(node: Node) -> None:
        # Descend into every scope, including inline lambdas — their calls belong to the
        # nearest named enclosing function.
        for child in node.named_children:
            if child.type == "call_expression":
                res = _callee(child, source)
                if res is not None:
                    name, receiver = res
                    if name and name not in seen:
                        seen.add(name)
                        through_variable = receiver is None and name in variables
                        path = None if through_variable else resolve(name, receiver, owner, types)
                        calls.append(Call(name=name, path=path))
            visit(child)

    visit(body)
    return calls


def _defs_in(node: Node, source: bytes, names: set[str]) -> None:
    """Collect free-function and out-of-class-method names defined in this subtree —
    the same-file call-resolution set. Corrupt-header definitions are skipped."""
    for c in node.named_children:
        if c.type == "namespace_definition":
            body = c.child_by_field_name("body")
            if body is not None:
                _defs_in(body, source, names)
        elif c.type == "template_declaration" or c.type in TRANSPARENT_SCOPES:
            _defs_in(c, source, names)  # include guard, #if, extern "C" { … }
        elif c.type == "function_definition" and (read := read_declaration(c, source)):
            fd = function_declarator_of(c.child_by_field_name("declarator"))
            if fd is None:
                continue
            inner = fd.child_by_field_name("declarator")
            if inner is None:
                continue
            if read.name is not None:
                names.add(read.name)
            elif inner.type == "identifier":
                names.add(node_text(inner, source))
            elif inner.type == "qualified_identifier":
                nm = inner.child_by_field_name("name")
                if nm is not None:
                    names.add(node_text(nm, source))


def defined_names(root: Node, source: bytes) -> set[str]:
    """Free-function / method names defined in this file (for same-file call resolution)."""
    names: set[str] = set()
    _defs_in(root, source, names)
    return names


#: Node types a declarator's chain ends at: the declared name.
_DECLARED_NAME_NODES = ("field_identifier", "identifier", "type_identifier")


def _broken_spot(node: Node | None) -> tuple[Node, Node | None, Node] | None:
    """The first error on the way from a declarator to its declared name that makes the name
    untrustworthy, as ``(declarator node, its inner declarator, the error)``. Where the error
    sits decides it:

    * **after** the name it follows, the "name" was a type or a macro and the real name is in
      the error: ``LUAI_FUNC lua_Unsigned luaH_getn(…)`` and ``voidpf ZLIB_INTERNAL zcalloc(…)``
      read as functions named ``lua_Unsigned`` and ``ZLIB_INTERNAL``. The exception is an
      initialized variable, where it is a macro between name and ``=`` (``oid ALIGN8 = {…}``);
    * **before** the name, it is a macro in front of it (``char * ZEXPORT zlibVersion()``), and
      the name is right, except inside parentheses: ``int f OF((int a, int b));`` reads as a
      parenthesized declarator holding an error and the name ``b``.

    A token the grammar inserts as missing (``API T f(void)`` reads as ``T::f`` in C++), or an
    error inside an initializer or a parameter list, leaves the name intact."""
    while node is not None and node.type not in _DECLARED_NAME_NODES:
        inner = node.child_by_field_name("declarator") or next(
            (c for c in node.named_children
             if c.type in _DECLARED_NAME_NODES or c.type.endswith("declarator")),
            None,
        )
        if node.type == "template_function":  # `name<args>`: the name is its `name` field
            inner = node.child_by_field_name("name")
        for err in (c for c in node.children if c.type == "ERROR"):
            if inner is None or node.type == "parenthesized_declarator":
                return node, inner, err
            if err.start_byte >= inner.end_byte and node.type != "init_declarator":
                return node, inner, err
        node = inner
    return None


def _renamed_by_macro(declarator: Node | None, source: bytes) -> str | None:
    """The real name of a function whose type sits where its name should be:
    ``LUALIB_API lua_Integer luaL_len(…)`` reads as a function ``lua_Integer`` with ``luaL_len``
    in an error. In C the word right before a function's ``(`` is its name, so when the error is
    exactly one bare word between the misread name and the parameter list, that word is the
    name. Any other broken shape gives ``None``."""
    spot = _broken_spot(declarator)
    if spot is None:
        return None
    node, inner, err = spot
    params = node.child_by_field_name("parameters")
    errors = [c for c in node.children if c.type == "ERROR"]
    if (
        node.type != "function_declarator" or inner is None or inner.type != "identifier"
        or params is None or errors != [err]
        or err.child_count != 1 or err.children[0].type != "identifier"
        or err.start_byte < inner.end_byte or err.end_byte > params.start_byte
    ):
        return None
    return node_text(err.children[0], source)


def _macro_prefixed(node: Node) -> bool:
    """Whether a declaration's only errors come from a macro in front of its type:
    ``LUA_API int lua_gettop(…)`` reads as the type ``LUA_API`` with ``int`` in an error. That
    holds when the type is a plain name, every error sits between it and the declarator and is
    made of bare words, and the declarator — name and parameters — has no error at all."""
    if node.type not in ("declaration", "function_definition", "field_declaration"):
        return False
    ret = node.child_by_field_name("type")
    declarator = node.child_by_field_name("declarator")
    if ret is None or ret.type != "type_identifier" or declarator is None or declarator.has_error:
        return False
    errors = [c for c in node.children if c.type == "ERROR" or c.is_missing]
    return bool(errors) and all(
        c.type == "ERROR" and c.child_count > 0
        and all(w.type == "identifier" for w in c.children)
        and ret.end_byte <= c.start_byte and c.end_byte <= declarator.start_byte
        for c in errors
    )


@dataclass(frozen=True)
class DeclarationRead:
    """How a file-scope declaration or definition is read: its declared ``name`` when a macro
    moved it (else ``None``: the declarator's own), and whether its ``returnType`` can be
    trusted."""

    name: str | None = None
    return_type_known: bool = True


def read_declaration(node: Node, source: bytes) -> DeclarationRead | None:
    """How to read a file-scope ``declaration`` / ``function_definition``, or ``None`` to skip
    it because its name can't be trusted. A macro the grammar can't expand is read past only in
    two shapes: a macro in front of the type (``LUA_API int f(…)``) and a macro or type in front
    of the name (``LUALIB_API lua_Integer luaL_len(…)``). Then the name and parameters come from
    clean syntax, and ``returnType`` is unknown: ``LUA_API int`` and ``uLong ZEXPORT`` parse the
    same way, so which word is the type can't be told."""
    prefixed = False
    if has_declaration_error(node):
        if not _macro_prefixed(node):
            return None
        prefixed = True
    if node.type == "function_definition" and is_macro_row(node, source):
        return None
    declarator = node.child_by_field_name("declarator")
    if is_macro_call_declarator(declarator):
        return None
    if _broken_spot(declarator) is None:
        return DeclarationRead(return_type_known=not prefixed)
    name = _renamed_by_macro(declarator, source)
    return DeclarationRead(name=name, return_type_known=False) if name is not None else None


def is_macro_call_declarator(declarator: Node | None) -> bool:
    """Whether a "function" is a macro call around the real one: zlib's ``local int gz_init
    OF((gz_statep));`` reads as a function named ``OF`` whose one parameter has no type — the
    grammar inserts a missing one, which a real parameter list never needs."""
    fd = function_declarator_of(declarator)
    params = fd.child_by_field_name("parameters") if fd is not None else None
    return params is not None and any(
        p.type == "parameter_declaration"
        and (t := p.child_by_field_name("type")) is not None and t.is_missing
        for p in params.named_children
    )


def read_member(member: Node) -> DeclarationRead | None:
    """How to read a class member, or ``None`` to skip it because its header is broken. A
    macro in front of its type (``ESIMD_INLINE int row(int g) const``) is read past as at file
    scope: the name and parameters come from clean syntax, ``returnType`` is unknown."""
    if not has_declaration_error(member):
        return DeclarationRead()
    if _macro_prefixed(member):
        return DeclarationRead(return_type_known=False)
    return None


def is_macro_row(node: Node, source: bytes) -> bool:
    """Whether a ``function_definition`` is a row of macro invocations misread as one function.
    ``GEN(a)`` ``GEN(b)`` … followed by a real function reads as a function named ``GEN`` whose
    type is ``GEN(a)``: the macro names itself. The real function's own name is lost in it."""
    ret = node.child_by_field_name("type")
    if ret is None or ret.type != "macro_type_specifier":
        return False
    macro = ret.child_by_field_name("name")
    fd = function_declarator_of(node.child_by_field_name("declarator"))
    inner = fd.child_by_field_name("declarator") if fd is not None else None
    return (
        macro is not None and inner is not None
        and node_text(macro, source) == node_text(inner, source)
    )


def return_type(node: Node, source: bytes) -> str | None:
    """A definition's or declaration's return type as written. In ``unsigned long ZEXPORT f()``
    the grammar folds a macro into the keyword type (``unsigned`` / ``long`` / ``short`` never
    combine with a type name in C), so only the keywords are the type: ``unsigned long``."""
    ret = node.child_by_field_name("type")
    if ret is None:
        return None
    if ret.type == "sized_type_specifier" and any(c.type == "type_identifier" for c in ret.children):
        return " ".join(node_text(c, source) for c in ret.children if c.type != "type_identifier")
    return node_text(ret, source)


def is_declared_static(node: Node, source: bytes) -> bool:
    """Whether a function definition or member declaration is written ``static``: on a free
    function it means internal linkage, on a class member a static member."""
    return any(
        c.type == "storage_class_specifier" and node_text(c, source) == "static"
        for c in node.children
    )


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
    class_name: str | None = None,
    id_owner: str | None = None,
    kind: str = "function",
    read: DeclarationRead = DeclarationRead(),
) -> tuple[Function, list[Statement]]:
    """A ``function_definition`` → a ``Function`` (+ its flat statements). ``kind`` is
    ``"function"`` for a free function or ``"method"`` for an out-of-class ``Class::method``
    definition; ``class_name`` qualifies the id (and, for a method, is the declaring class).
    ``read`` carries a name a macro moved and whether the return type is known."""
    fd = function_declarator_of(node.child_by_field_name("declarator"))
    inner = fd.child_by_field_name("declarator") if fd is not None else None
    if inner is not None and inner.type == "qualified_identifier":
        nm = inner.child_by_field_name("name")
        name = node_text(nm, source) if nm is not None else ""
    else:
        name = node_text(inner, source) if inner is not None else ""
    if read.name is not None:
        name = read.name

    start, end = line_span(node)
    # The id carries the qualified owner (`Outer.Inner`) so a nested type's methods are
    # identified unambiguously; `class_name` stays simple for call resolution.
    fid = disambiguate(
        function_id(path, name, start, class_name=id_owner or class_name), seen_ids)
    body = node.child_by_field_name("body")
    params = fd.child_by_field_name("parameters") if fd is not None else None
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type=kind,
        # `static` on a free function: visible to its own file only (internal linkage). An
        # out-of-class `Class::method` definition can't repeat `static`: left unknown.
        isStatic=is_declared_static(node, source) or None if kind == "function" else None,
        params=extract_params(params, source),
        returnType=return_type(node, source) if read.return_type_known else None,
        startLine=start,
        endLine=end,
        calls=_calls(
            body, source, resolve, class_name, build_type_map(body, params, source),
            variable_names(body, params, source),
        ),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids,
        descend_all=True,  # walk inline lambdas — attribute their statements here
    )
    return fn, statements


def build_member_function(
    field_decl: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    class_name: str,
    visibility: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
    id_owner: str | None = None,
    read: DeclarationRead = DeclarationRead(),
) -> tuple[Function, list[Statement]]:
    """An in-class member-function **declaration** (``field_declaration`` with a
    ``function_declarator``, no body) → a flat method ``Function``. An inline
    **definition** carries a ``body`` and its calls/statements are captured too. ``read``
    says whether the return type is known (see :func:`read_member`)."""
    fd = function_declarator_of(field_decl.child_by_field_name("declarator"))
    name_node = fd.child_by_field_name("declarator") if fd is not None else None
    while name_node is not None and name_node.type == "parenthesized_declarator":
        name_node = name_node.named_children[0] if name_node.named_children else None  # (f)(int)
    name = node_text(name_node, source) if name_node is not None else ""
    start, end = line_span(field_decl)
    # The id carries the qualified owner (`Outer.Inner`) so a nested type's methods are
    # identified unambiguously; `class_name` stays simple for call resolution.
    fid = disambiguate(
        function_id(path, name, start, class_name=id_owner or class_name), seen_ids)
    body = field_decl.child_by_field_name("body")  # present only for an inline definition
    params = fd.child_by_field_name("parameters") if fd is not None else None
    kind = "constructor" if name == class_name else "method"
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type=kind,
        visibility=visibility,
        # A static member: `static` is written on its declaration in the class.
        isStatic=is_declared_static(field_decl, source) or None,
        params=extract_params(params, source),
        returnType=return_type(field_decl, source) if read.return_type_known else None,
        startLine=start,
        endLine=end,
        calls=_calls(
            body, source, resolve, class_name, build_type_map(body, params, source),
            variable_names(body, params, source),
        ),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids,
        descend_all=True,
    )
    return fn, statements
