"""C++ function / method + parameter + call extraction.

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

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .statements import extract_statements

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


def _calls(
    body: Node | None,
    source: bytes,
    resolve: CallResolver = noop_resolver,
    owner: str | None = None,
) -> list[Call]:
    """Calls in ``body``. ``owner`` is the simple/qualified name of the class the body
    belongs to (``None`` for a free function) — it lets a bare implicit-``this`` call
    ``Foo()`` resolve to ``owner::Foo`` in the repo index."""
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
                        calls.append(Call(name=name, path=resolve(name, receiver, owner)))
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
        elif c.type == "template_declaration":
            _defs_in(c, source, names)
        elif c.type == "function_definition" and not has_declaration_error(c):
            fd = function_declarator_of(c.child_by_field_name("declarator"))
            if fd is None:
                continue
            inner = fd.child_by_field_name("declarator")
            if inner is None:
                continue
            if inner.type == "identifier":
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


def _types_in(node: Node, source: bytes, types: dict[str, str]) -> None:
    """Local ``variable → declared type`` map for receiver-type call resolution. Only
    declarations whose type is a named ``type_identifier`` contribute — a ``primitive_type``
    or ``auto`` resolves to nothing cross-file, so it is left out (honest-null)."""
    for c in node.named_children:
        if c.type in ("declaration", "parameter_declaration", "field_declaration"):
            tnode = c.child_by_field_name("type")
            if tnode is not None and tnode.type == "type_identifier":
                name_node = _first_identifier(c.child_by_field_name("declarator"))
                if name_node is not None:
                    types.setdefault(node_text(name_node, source), node_text(tnode, source))
        _types_in(c, source, types)


def type_map(root: Node, source: bytes) -> dict[str, str]:
    types: dict[str, str] = {}
    _types_in(root, source, types)
    return types


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
    kind: str = "function",
) -> tuple[Function, list[Statement]]:
    """A ``function_definition`` → a ``Function`` (+ its flat statements). ``kind`` is
    ``"function"`` for a free function or ``"method"`` for an out-of-class ``Class::method``
    definition; ``class_name`` qualifies the id (and, for a method, is the declaring class)."""
    fd = function_declarator_of(node.child_by_field_name("declarator"))
    inner = fd.child_by_field_name("declarator") if fd is not None else None
    if inner is not None and inner.type == "qualified_identifier":
        nm = inner.child_by_field_name("name")
        name = node_text(nm, source) if nm is not None else ""
    else:
        name = node_text(inner, source) if inner is not None else ""

    start, end = line_span(node)
    fid = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)
    ret = node.child_by_field_name("type")
    body = node.child_by_field_name("body")
    params = fd.child_by_field_name("parameters") if fd is not None else None
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type=kind,
        params=extract_params(params, source),
        returnType=node_text(ret, source) if ret is not None else None,
        startLine=start,
        endLine=end,
        calls=_calls(body, source, resolve, class_name),
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
) -> tuple[Function, list[Statement]]:
    """An in-class member-function **declaration** (``field_declaration`` with a
    ``function_declarator``, no body) → a flat method ``Function``. An inline
    **definition** carries a ``body`` and its calls/statements are captured too."""
    fd = function_declarator_of(field_decl.child_by_field_name("declarator"))
    name_node = fd.child_by_field_name("declarator") if fd is not None else None
    name = node_text(name_node, source) if name_node is not None else ""
    start, end = line_span(field_decl)
    fid = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)
    ret = field_decl.child_by_field_name("type")
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
        params=extract_params(params, source),
        returnType=node_text(ret, source) if ret is not None else None,
        startLine=start,
        endLine=end,
        calls=_calls(body, source, resolve, class_name),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids,
        descend_all=True,
    )
    return fn, statements
