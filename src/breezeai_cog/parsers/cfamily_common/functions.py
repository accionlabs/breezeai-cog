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

import re

from dataclasses import dataclass

from collections.abc import Sequence, Set as AbstractSet

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .mappings import TRANSPARENT_SCOPES
from .statements import extract_statements
from .types import build_type_map, qualifier_macro_name

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


def function_name_node(fd: Node | None) -> Node | None:
    """The node naming a ``function_declarator``'s function: its ``declarator``, with
    parentheses around the name removed — ``int (f)(int)`` declares ``f``, a guard against a
    macro named ``f``. A function pointer's parentheses hold a pointer, not a name, and stay."""
    inner = fd.child_by_field_name("declarator") if fd is not None else None
    while inner is not None and inner.type == "parenthesized_declarator":
        named = inner.named_children
        if len(named) != 1 or named[0].type not in (
            "identifier", "field_identifier", "qualified_identifier", "parenthesized_declarator",
        ):
            break
        inner = named[0]
    return inner


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


def param_name_node(param: Node) -> Node | None:
    """A ``parameter_declaration``'s name node — past a qualifier macro
    (:func:`qualifier_macro_name`); ``None`` when unnamed."""
    return qualifier_macro_name(param) or _first_identifier(param.child_by_field_name("declarator"))


def has_error_besides_qualifier_macros(node: Node) -> bool:
    """Whether ``node`` has a parse error other than the name a qualifier macro pushed out of a
    parameter (:func:`qualifier_macro_name`)."""
    if not node.has_error:
        return False
    if node.is_missing:
        return True
    if node.type == "ERROR":
        prev = node.prev_sibling
        return prev is None or prev.type != "parameter_declaration" or (
            qualifier_macro_name(prev) is None
        )
    return any(has_error_besides_qualifier_macros(c) for c in node.children)


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
        name_node = param_name_node(p)
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
                name_node = qualifier_macro_name(p)
                name = (
                    node_text(name_node, source) if name_node is not None
                    else _declared_variable(p.child_by_field_name("declarator"), source)
                )
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
                        call_types = types
                        fn = child.child_by_field_name("function")
                        if (
                            fn is not None and fn.type == "field_expression"
                            and receiver is not None and receiver in variables
                            and receiver not in (types or {})
                        ):
                            # `p.m()` on a local of unknown type (`auto p = …`): a variable,
                            # never a class that shares its name.
                            call_types = {**(types or {}), receiver: None}
                        path = None if through_variable else resolve(name, receiver, owner, call_types)
                        calls.append(Call(name=name, path=path))
            visit(child)

    visit(body)
    return calls


def _defs_in(node: Node, source: bytes, names: set[str], spilled: bool) -> None:
    """Collect free-function and out-of-class-method names defined in this subtree —
    the same-file call-resolution set. Corrupt-header definitions are skipped, and so are
    unqualified ones in a file with spilled members (:func:`spilled_members`)."""
    for c in node.named_children:
        if c.type == "namespace_definition":
            body = c.child_by_field_name("body")
            if body is not None:
                _defs_in(body, source, names, spilled)
        elif c.type == "template_declaration" or c.type in TRANSPARENT_SCOPES:
            _defs_in(c, source, names, spilled)  # include guard, #if, extern "C" { … }
        elif c.type == "function_definition" and spilled and not owner_is_explicit(c):
            continue
        elif c.type == "function_definition":
            _add_def_name(c, source, names)


def _add_def_name(fn: Node, source: bytes, names: set[str]) -> None:
    """Add a function definition's name to the same-file set, when it can be trusted."""
    read = read_declaration(fn, source)
    fd = function_declarator_of(fn.child_by_field_name("declarator"))
    inner = function_name_node(fd)
    if read is None or inner is None:
        return
    if read.name is not None:
        names.add(read.name)
    elif inner.type == "identifier":
        names.add(node_text(inner, source))
    elif inner.type == "qualified_identifier":
        nm = inner.child_by_field_name("name")
        if nm is not None:
            names.add(node_text(nm, source))


def defined_names(
    root: Node, source: bytes, spilled: bool = False, extra: Sequence[Node] = (),
    extra_whole: bool = False,
) -> set[str]:
    """Free-function / method names defined in this file (for same-file call resolution).
    ``extra`` are definitions found only by a second parse (``branches.branch_fix``); with
    ``extra_whole`` that parse read the file whole, so none of them is a spilled member."""
    names: set[str] = set()
    _defs_in(root, source, names, spilled)
    for fn in extra:
        if fn.type == "function_definition" and (
            extra_whole or not (spilled and not owner_is_explicit(fn))
        ):
            _add_def_name(fn, source, names)
    return names


#: Operators C++ allows only as class members.
_MEMBER_ONLY_OPERATORS = frozenset({"operator=", "operator->", "operator()", "operator[]"})


def _member_shaped(node: Node, source: bytes, cpp: bool) -> bool:
    """Whether a definition or declaration at file or namespace scope can only be a class
    member: named by a ``field_identifier`` (the grammar's name for a member), or — in C++ —
    an unqualified destructor, a member-only operator, or a definition with no return type (a
    constructor; a free function needs one)."""
    fd = function_declarator_of(node.child_by_field_name("declarator"))
    inner = function_name_node(fd)
    if inner is None:
        return False
    if inner.type == "field_identifier":
        return True
    if not cpp:
        return False
    return (
        inner.type == "destructor_name"
        or (inner.type == "operator_name"
            and "".join(node_text(inner, source).split()) in _MEMBER_ONLY_OPERATORS)
        or (node.type == "function_definition" and inner.type == "identifier"
            and node.child_by_field_name("type") is None)
    )


def spilled_members(root: Node, source: bytes, cpp: bool) -> bool:
    """Whether a file's class members spilled out to file scope. When the grammar can't parse
    the file as a whole, its root is an ``ERROR`` holding every declaration flat, and the
    members of a class whose head was lost in it read as free functions. One definition that
    can only be a member (:func:`_member_shaped`) proves it; then no unqualified definition in
    the file can be trusted to belong to the file."""
    if root.type != "ERROR":
        return False
    stack = [root]
    while stack:
        scope = stack.pop()
        for c in scope.named_children:
            if c.type == "namespace_definition":
                body = c.child_by_field_name("body")
                if body is not None:
                    stack.append(body)
            elif c.type in ("template_declaration", "linkage_specification") or c.type in TRANSPARENT_SCOPES:
                stack.append(c)
            elif c.type in ("function_definition", "declaration") and _member_shaped(c, source, cpp):
                return True
    return False


def owner_is_explicit(node: Node) -> bool:
    """Whether a definition or declaration names its owner (``int Judge::decide()``,
    ``int Cache::hits = 0;``) — kept even where spilled members make the file's flat
    definitions untrustworthy."""
    d = node.child_by_field_name("declarator")
    while d is not None and d.type not in _DECLARED_NAME_NODES:
        if d.type == "qualified_identifier":
            return True
        d = d.child_by_field_name("declarator") or next(
            (c for c in d.named_children if c.type == "qualified_identifier"), None
        )
    return False


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


def _sized_macro(ret: Node) -> Node | None:
    """The macro in ``API unsigned`` — a type the grammar folded from a macro and ``unsigned`` /
    ``signed`` / ``long`` / ``short``: its one name, written first. ``None`` for any other type."""
    names = [c for c in ret.children if c.type == "type_identifier"]
    if ret.type != "sized_type_specifier" or len(names) != 1 or names[0] is not ret.children[0]:
        return None
    return names[0]


def _macro_prefixed(node: Node, source: bytes) -> bool:
    """Whether a declaration's only errors come from a macro in front of its type:
    ``LUA_API int lua_gettop(…)`` reads as the type ``LUA_API`` with ``int`` in an error. That
    holds when the type is a plain name, every error sits between it and the declarator and is
    made of bare words, and the declarator — name and parameters — has no error at all.
    ``LUALIB_API unsigned int f(…)`` reads as the type ``LUALIB_API unsigned`` with ``int`` in an
    error: there the words must all be type keywords."""
    if node.type not in ("declaration", "function_definition", "field_declaration"):
        return False
    ret = node.child_by_field_name("type")
    declarator = node.child_by_field_name("declarator")
    if ret is None or declarator is None or has_error_besides_qualifier_macros(declarator):
        return False
    if ret.type != "type_identifier" and (
        _sized_macro(ret) is None
        or any(
            node_text(w, source) not in _TYPE_KEYWORDS
            for e in node.children if e.type == "ERROR" for w in e.children
        )
    ):
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
    """How a declaration or definition is read: its declared ``name`` when a macro moved it
    (else ``None``: the declarator's own) and whether its ``returnType`` can be trusted. Read
    past a macro in front of a keyword type (``LUA_API int f``), the ``macro`` and the
    ``return_type`` are both certain."""

    name: str | None = None
    return_type_known: bool = True
    macro: str | None = None
    return_type: str | None = None


#: C's type keywords. One can't be a macro's name, so a word in front of them is the macro.
_TYPE_KEYWORDS = frozenset({
    "void", "char", "short", "int", "long", "float", "double", "signed", "unsigned", "bool",
    "_Bool", "const", "volatile",
})


def _past_macro(node: Node, source: bytes) -> DeclarationRead:
    """The read of a declaration with a macro in front of its type (:func:`_macro_prefixed`).
    ``LUA_API int f`` and ``uLong ZEXPORT f`` parse the same way, so which word is the type is
    unknown — unless the words after the first are all type keywords: then the first is the
    macro and the keywords are the type (qualifiers left out, as for any return type)."""
    words = [
        node_text(w, source) for e in node.children if e.type == "ERROR" for w in e.children
    ]
    type_words = [w for w in words if w not in ("const", "volatile")]
    ret = node.child_by_field_name("type")
    if ret is None or not type_words or not all(w in _TYPE_KEYWORDS for w in words):
        return DeclarationRead(return_type_known=False)
    macro = _sized_macro(ret)
    if macro is not None:  # `API unsigned` + `int`
        keywords = [node_text(c, source) for c in ret.children if c is not macro]
        return DeclarationRead(
            return_type_known=False, macro=node_text(macro, source),
            return_type=" ".join([*keywords, *type_words]),
        )
    return DeclarationRead(
        return_type_known=False, macro=node_text(ret, source), return_type=" ".join(type_words),
    )


def _macro_after_keyword_type(node: Node, source: bytes) -> str | None:
    """The macro between a keyword return type and the name: ``int ZEXPORT unzOpen(…)``, ``void
    WINAPI f(…)``. After a type made of keywords only the declarator can follow, so a bare word
    there — one the grammar leaves in an error before a clean declarator — is a calling-convention
    or export macro. ``None`` for any other shape (``uLong ZEXPORT f`` is ``_past_macro``'s)."""
    ret = node.child_by_field_name("type")
    declarator = node.child_by_field_name("declarator")
    if (
        ret is None or declarator is None or has_error_besides_qualifier_macros(declarator)
        or not (
            ret.type == "primitive_type"
            or (ret.type == "sized_type_specifier"
                and all(c.type != "type_identifier" for c in ret.children))
        )
    ):
        return None
    errors = [c for c in node.children if c.type == "ERROR" or c.is_missing]
    if len(errors) != 1:
        return None
    err = errors[0]
    if (
        err.type != "ERROR" or err.child_count != 1 or err.children[0].type != "identifier"
        or err.start_byte < ret.end_byte or err.end_byte > declarator.start_byte
    ):
        return None
    word = node_text(err.children[0], source)
    return None if word in _TYPE_KEYWORDS else word


def _parenthesized_name(node: Node, source: bytes) -> DeclarationRead | None:
    """The read of a misread declarator (:func:`is_misread_declarator`), or ``None`` when it
    can't be read. ``LUA_API int (lua_gettop) (lua_State *L);`` — the name parenthesized so a
    macro of that name can't expand — reads as a function ``int`` taking ``lua_gettop``. A
    keyword can't be a name or a macro, so when the misread "name" is a type keyword and the
    inner parentheses hold one bare word, the word in front is the macro, the keyword the return
    type and the bare word the name. ``LUALIB_API lua_Integer (luaL_len)`` is not read: it is
    also how a type and a name-building macro (``int MANGLE(f)(…)``) look."""
    fd = function_declarator_of(node.child_by_field_name("declarator"))
    inner = fd.child_by_field_name("declarator") if fd is not None else None
    word = inner.child_by_field_name("declarator") if inner is not None else None
    wrapped = inner.child_by_field_name("parameters") if inner is not None else None
    ret = node.child_by_field_name("type")
    if word is None or wrapped is None or ret is None or node_text(word, source) not in _TYPE_KEYWORDS:
        return None
    params = wrapped.named_children
    if (
        len(params) != 1 or params[0].type != "parameter_declaration"
        or params[0].child_by_field_name("declarator") is not None
        or (name := params[0].child_by_field_name("type")) is None
        or name.type != "type_identifier" or len(params[0].named_children) != 1
    ):
        return None
    if ret.type == "type_identifier":  # LUA_API int (f)
        macro, keywords = ret, []
    elif ret.type == "sized_type_specifier":  # LUA_API unsigned int (f)
        macros = [c for c in ret.children if c.type == "type_identifier"]
        if len(macros) != 1 or macros[0] is not ret.children[0]:
            return None
        macro, keywords = macros[0], [node_text(c, source) for c in ret.children[1:]]
    else:
        return None
    return DeclarationRead(
        name=node_text(name, source), return_type_known=False, macro=node_text(macro, source),
        return_type=" ".join([*keywords, node_text(word, source)]),
    )


def read_declaration(node: Node, source: bytes) -> DeclarationRead | None:
    """How to read a file-scope ``declaration`` / ``function_definition``, or ``None`` to skip
    it because its name can't be trusted. A macro the grammar can't expand is read past only in
    three shapes: a macro in front of the type (``LUA_API int f(…)``), a macro or type in front
    of the name (``LUALIB_API lua_Integer luaL_len(…)``) and a macro in front of a keyword type
    and a parenthesized name (``LUA_API int (lua_gettop)(…)``, :func:`_parenthesized_name`).
    Then the name and parameters come from clean syntax; ``returnType`` is unknown in the second
    shape and in the first unless the type is keywords: ``LUA_API int`` and ``uLong ZEXPORT``
    parse the same way, so which word is the type can't be told."""
    if is_misread_declarator(node.child_by_field_name("declarator")):
        return None if has_declaration_error(node) else _parenthesized_name(node, source)
    prefixed = False
    if has_declaration_error(node):
        if (macro := _macro_after_keyword_type(node, source)) is not None:
            return DeclarationRead(macro=macro)
        if not _macro_prefixed(node, source):
            return None
        prefixed = True
    if node.type == "function_definition" and is_macro_row(node, source):
        return None
    declarator = node.child_by_field_name("declarator")
    if is_macro_call_declarator(declarator):
        return None
    if _broken_spot(declarator) is None:
        return _past_macro(node, source) if prefixed else DeclarationRead()
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


def read_member(member: Node, source: bytes) -> DeclarationRead | None:
    """How to read a class member, or ``None`` to skip it because its header is broken. A
    macro in front of its type (``ESIMD_INLINE int row(int g) const``) is read past as at file
    scope: the name and parameters come from clean syntax, and so is a misread parenthesized
    name (``API int (f)(int a);``)."""
    if is_misread_declarator(member.child_by_field_name("declarator")):
        return None if has_declaration_error(member) else _parenthesized_name(member, source)
    if not has_declaration_error(member):
        return DeclarationRead()
    if (macro := _macro_after_keyword_type(member, source)) is not None:
        return DeclarationRead(macro=macro)
    if _macro_prefixed(member, source):
        return _past_macro(member, source)
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
    inner = function_name_node(fd)
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


def read_return_type(node: Node, source: bytes, read: DeclarationRead) -> str | None:
    """The ``returnType`` of a declaration as ``read``: past a macro only when it is certain."""
    if read.return_type is not None:
        return read.return_type
    return return_type(node, source) if read.return_type_known else None


#: Attribute syntax the grammar parses: ``[[nodiscard]]``, ``__attribute__((noinline))``,
#: ``__declspec(dllexport)``.
ATTRIBUTE_TYPES = ("attribute_declaration", "attribute_specifier", "ms_declspec_modifier")


def attribute_decorators(node: Node, source: bytes) -> list[Decorator]:
    """One ``Decorator`` per attribute in an attribute node: ``[[nodiscard, deprecated("x")]]``
    → ``nodiscard``, ``deprecated("x")``; ``__attribute__((format(printf, 2, 3)))`` →
    ``format`` with its arguments; ``[[gnu::always_inline]]`` keeps its namespace."""
    out = []
    if node.type == "attribute_declaration":
        for attr in (c for c in node.named_children if c.type == "attribute"):
            name = attr.child_by_field_name("name")
            prefix = attr.child_by_field_name("prefix")
            args = next((c for c in attr.named_children if c.type == "argument_list"), None)
            if name is not None:
                full = node_text(name, source)
                if prefix is not None:
                    full = f"{node_text(prefix, source)}::{full}"
                out.append(Decorator(name=full, args=_args(args, source)))
        return out
    # `__attribute__((a, b(x)))` holds an argument list; `__declspec(a)` holds the words itself.
    holder = next((c for c in node.named_children if c.type == "argument_list"), node)
    for item in holder.named_children:
        if item.type == "identifier":
            out.append(Decorator(name=node_text(item, source)))
        elif item.type == "call_expression":
            fn = item.child_by_field_name("function")
            if fn is not None:
                out.append(Decorator(
                    name=node_text(fn, source),
                    args=_args(item.child_by_field_name("arguments"), source),
                ))
    return out


def _args(arg_list: Node | None, source: bytes) -> list[str]:
    return [node_text(a, source) for a in arg_list.named_children] if arg_list is not None else []


_WRAPPER_BEFORE = re.compile(rb"([A-Za-z_]\w*)[ \t]*\(\s*\Z")
_STRING_LITERAL = re.compile(rb'"(?:[^"\\\n]|\\.)*"')


def wrapper_decorator(node: Node, source: bytes) -> Decorator | None:
    """The macro wrapped around a declaration, ``DEPRECATED(LLAMA_API int f(int a), "use
    g");``, with its string arguments. The wrapper was blanked before parsing
    (``classes.parse_cfamily``), so ``NAME(`` sits right before the declaration in the original
    source, where no node covers it, and the strings between the declarator and the ``;``."""
    m = _WRAPPER_BEFORE.search(source, max(node.start_byte - 256, 0), node.start_byte)
    declarator = node.child_by_field_name("declarator")
    if m is None or declarator is None:
        return None
    args = _STRING_LITERAL.findall(source, declarator.end_byte, node.end_byte)
    return Decorator(name=m.group(1).decode(), args=[a.decode("utf-8", "replace") for a in args])


def decorators_of(node: Node, source: bytes, read: DeclarationRead) -> list[Decorator]:
    """The attributes and export / inline macros written on a function, prototype or member,
    in source order: attributes the grammar parses (also one after the parameter list,
    ``void die() __attribute__((noreturn))``), and a macro only where it is certainly one — in
    front of a keyword type (``read.macro``) or after ``unsigned`` / ``long``, where a type
    name can't stand (``unsigned long ZEXPORT crc32``)."""
    found: list[tuple[int, list[Decorator]]] = []
    if node.type == "declaration" and (wrapper := wrapper_decorator(node, source)) is not None:
        found.append((node.start_byte - 1, [wrapper]))
    fd = function_declarator_of(node.child_by_field_name("declarator"))
    for holder in (node, fd):
        for c in holder.children if holder is not None else ():
            if c.type in ATTRIBUTE_TYPES:
                found.append((c.start_byte, attribute_decorators(c, source)))
    ret = node.child_by_field_name("type")
    if read.macro is not None and ret is not None:
        found.append((ret.start_byte, [Decorator(name=read.macro)]))
    elif ret is not None and ret.type == "sized_type_specifier":
        found.extend(
            (c.start_byte, [Decorator(name=node_text(c, source))])
            for c in ret.children if c.type == "type_identifier"
        )
    pointer = node.child_by_field_name("declarator")
    while pointer is not None and pointer.type == "pointer_declarator":
        # `const char * ZEXPORT zlibVersion(void)`: no `T * A f(…)` is valid, so the word the
        # grammar leaves in an error between the `*` and the name is a macro.
        err = next((c for c in pointer.children if c.type == "ERROR"), None)
        inner = pointer.child_by_field_name("declarator")
        if (
            err is not None and inner is not None and inner.type == "function_declarator"
            and err.child_count == 1 and err.children[0].type == "identifier"
            and err.end_byte <= inner.start_byte
        ):
            found.append((err.start_byte, [Decorator(name=node_text(err.children[0], source))]))
        pointer = inner
    if (trailing := trailing_decorator(node, source)) is not None:
        found.append((node.end_byte, [trailing]))
    return [d for _, ds in sorted(found, key=lambda f: f[0]) for d in ds]


_TRAILING_TEXT = re.compile(rb"\s*([A-Za-z_]\w*)\s*(?:\((.*)\))?\s*", re.S)


def _split_args(text: bytes) -> list[str]:
    """Top-level comma-separated arguments, trimmed: ``1, f(a, b)`` → ``1``, ``f(a, b)``."""
    out, depth, start = [], 0, 0
    for i, c in enumerate(text):
        if c in b"([{":
            depth += 1
        elif c in b")]}":
            depth -= 1
        elif c == 0x2C and depth == 0:
            out.append(text[start:i])
            start = i + 1
    out.append(text[start:])
    return [a.strip().decode("utf-8", "replace") for a in out if a.strip()]


def trailing_decorator(node: Node, source: bytes) -> Decorator | None:
    """The attribute macro after a declaration's parameter list, ``void errorf(const char *fmt,
    ...) CURL_PRINTF(1, 2);``, with its arguments. It was blanked before parsing
    (``classes.parse_cfamily``), so it is the text between the declarator and the ``;`` that no
    node covers."""
    if node.type not in ("declaration", "field_declaration") or not node.children:
        return None
    declarator = node.child_by_field_name("declarator")
    last = node.children[-1]
    if declarator is None or last.type != ";" or function_declarator_of(declarator) is None:
        return None
    if any(declarator.end_byte <= c.start_byte < last.start_byte for c in node.children):
        return None
    m = _TRAILING_TEXT.fullmatch(source, declarator.end_byte, last.start_byte)
    if m is None:
        return None
    args = _split_args(m.group(2)) if m.group(2) is not None else []
    return Decorator(name=m.group(1).decode(), args=args)


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
    inner = function_name_node(fd)
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
        decorators=decorators_of(node, source, read),
        returnType=read_return_type(node, source, read),
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
    says whether the return type is known and carries a name a macro moved (see
    :func:`read_member`)."""
    fd = function_declarator_of(field_decl.child_by_field_name("declarator"))
    name_node = function_name_node(fd)  # `(f)(int)` names `f`
    name = read.name or (node_text(name_node, source) if name_node is not None else "")
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
        decorators=decorators_of(field_decl, source, read),
        returnType=read_return_type(field_decl, source, read),
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
