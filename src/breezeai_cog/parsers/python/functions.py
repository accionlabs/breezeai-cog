"""Function / method + parameter + decorator + call extraction."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, function_id
from ...schemas import Call, Class, Decorator, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .receivers import local_types, maybe_typed
from .statements import extract_statements


def defined_names(root: Node, source: bytes) -> set[str]:
    """All function/method/class names defined in the file (for same-file call resolution)."""
    names: set[str] = set()

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type in ("function_definition", "class_definition"):
                nm = c.child_by_field_name("name")
                if nm is not None:
                    names.add(node_text(nm, source))
            walk(c)

    walk(root)
    return names


def _visibility(name: str) -> str:
    if name.startswith("__") and name.endswith("__"):
        return "public"  # dunder
    if name.startswith("__"):
        return "private"
    if name.startswith("_"):
        return "protected"
    return "public"


def _decorator_name(node: Node, source: bytes) -> tuple[str, list[str]]:
    """A ``decorator`` node -> (simple name, args). Handles @x, @a.b, @x(args)."""
    inner = node.named_children[0] if node.named_children else None
    if inner is None:
        return node_text(node, source).lstrip("@"), []
    args: list[str] = []
    if inner.type == "call":
        target = inner.child_by_field_name("function") or inner.named_children[0]
        arglist = inner.child_by_field_name("arguments")
        if arglist is not None:
            args = [node_text(a, source) for a in arglist.named_children]
        inner = target
    name = node_text(inner, source)
    return name.rsplit(".", 1)[-1], args  # simple name, no module/@


def extract_decorators(decorator_nodes: list[Node], source: bytes) -> list[Decorator]:
    return [Decorator(name=n, args=a) for n, a in (_decorator_name(d, source) for d in decorator_nodes)]


def extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    if params_node is None:
        return []
    out: list[Parameter] = []
    for child in params_node.named_children:
        type_node = child.child_by_field_name("type")
        type_str = node_text(type_node, source) if type_node is not None else ""
        if child.type == "identifier":
            out.append(Parameter(name=node_text(child, source), type=""))
        elif child.type in ("typed_parameter", "default_parameter", "typed_default_parameter"):
            ident = next((c for c in child.named_children if c.type == "identifier"), None)
            name = node_text(ident, source) if ident is not None else node_text(child, source)
            # default-value expr (e.g. FastAPI `Depends(get_db)`); None when no default
            value_node = child.child_by_field_name("value")
            default = node_text(value_node, source) if value_node is not None else None
            out.append(Parameter(name=name, type=type_str, default=default))
        elif child.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            ident = next((c for c in child.named_children if c.type == "identifier"), None)
            prefix = "*" if child.type == "list_splat_pattern" else "**"
            out.append(Parameter(name=prefix + (node_text(ident, source) if ident else ""), type=""))
    return out


def _span(node: Node) -> tuple[int, int]:
    """Stable identity for a node within one parse (used as a barrier key)."""
    return (node.start_byte, node.end_byte)


def collect_nested_definitions(body: Node | None, source: bytes) -> list[tuple[Node, list[Node]]]:
    """Nested ``def``s and ``class``es whose nearest named enclosing function is this one
    (closures, decorator factories, in-method helpers, function-local classes). Descends
    through lambdas and control flow but stops at each nested definition (what is inside it
    belongs to its own extraction). Returns (definition_node, decorator_nodes); the
    definition nodes' spans double as the barrier set so the enclosing function does not
    also fold their calls/statements."""
    if body is None:
        return []
    out: list[tuple[Node, list[Node]]] = []

    def visit(n: Node) -> None:
        for c in n.named_children:
            inner, decs = c, []
            if c.type == "decorated_definition":
                decs = [d for d in c.named_children if d.type == "decorator"]
                inner = next(
                    (d for d in c.named_children if d.type in ("function_definition", "class_definition")), c
                )
            if inner.type in ("function_definition", "class_definition"):
                out.append((inner, decs))
                continue  # barrier: its body belongs to it, not to the enclosing fn
            visit(c)  # lambdas, control flow, blocks, expressions

    visit(body)
    return out


def _extract_calls(
    body: Node | None, source: bytes, resolve: CallResolver = noop_resolver,
    barriers: frozenset[tuple[int, int]] = frozenset(), owner: str | None = None,
    params: Node | None = None, typed_locals: bool = True,
) -> list[Call]:
    """``owner``: the class the function is a method of (``self.m()`` / ``super().m()``).
    ``params``: the function's parameters, for the classes its local names hold (read once, at
    the first call on a plain name)."""
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[tuple[str, str | None]] = set()  # one entry per name and target file
    scope: dict[str, tuple[str, ...] | None] | None = None
    candidates: frozenset[str] | None = None

    def visit(node: Node) -> None:
        # Descend into lambdas — their calls belong to the nearest named enclosing
        # function — but stop at ``barriers``: the spans of nested ``def``s extracted
        # as their own scope (see build_function).
        for child in node.named_children:
            if _span(child) in barriers:
                continue
            if child.type == "call":
                fn = child.child_by_field_name("function")
                if fn is not None:
                    callee = node_text(fn, source)
                    name = callee.rsplit(".", 1)[-1]
                    receiver = callee.rsplit(".", 1)[0] if "." in callee else None
                    if typed_locals and receiver is not None and receiver.isidentifier() \
                            and receiver not in ("self", "cls"):
                        nonlocal scope, candidates
                        if candidates is None:  # a cheap check first, the full walk if needed
                            candidates = maybe_typed(params, body, source)
                        if scope is None and receiver in candidates:
                            scope = local_types(params, body, source)
                        path = resolve(name, receiver, owner,
                                       py_scope=scope if receiver in candidates else None) \
                            if name else None
                    else:
                        path = resolve(name, receiver, owner) if name else None
                    if name and (name, path) not in seen:
                        seen.add((name, path))
                        calls.append(Call(name=name, path=path))
            visit(child)

    visit(body)
    return calls


_DEFINITIONS = frozenset({"function_definition", "class_definition", "decorated_definition"})


def scope_calls(body: Node | None, source: bytes, resolve: CallResolver = noop_resolver
                ) -> list[Call]:
    """The calls of a module's or class body's own code: everything outside its ``def`` and
    ``class`` definitions (their bodies are their own; decorators are no call the scope makes —
    they are the definition's ``decorators``) — control-flow blocks (``if __name__ ==
    "__main__":``, ``with DAG(…):``) and lambdas included. A module-level name may be rebound in
    any function (``global``), so no local's class is read here."""
    if body is None:
        return []
    spans: set[tuple[int, int]] = set()
    stack = [body]
    while stack:
        n = stack.pop()
        for c in n.named_children:
            if c.type in _DEFINITIONS:
                spans.add(_span(c))
            else:
                stack.append(c)
    return _extract_calls(body, source, resolve, frozenset(spans), None, None, typed_locals=False)


def build_function(
    fnode: Node,
    decorators: list[Decorator],
    source: bytes,
    path: str,
    *,
    parent_id: str,
    class_name: str | None,
    id_owner: str | None = None,
    seen_ids: set[str],
    capture: bool = False,
    limit: int,
    resolve: CallResolver = noop_resolver,
    scope: str | None = None,
) -> tuple[list[Class], list[Function], list[Statement]]:
    """Return the classes, Function(s) and (flat) statements defined by this function — the
    caller collects them onto ``FileRecord.classes``/``.functions``/``.statements`` (linked by
    parentId, not nested). The functions are this one plus any nested ``def``s; the classes are
    those defined in its body. Each is extracted as its own record parented to the file (a
    class or function never parents to a function); the nesting is read from line ranges.

    ``scope`` is the qualified name of what encloses this function (``Outer`` for a method,
    ``f`` for a closure in ``f``). It does not change this function's id; it qualifies the id
    of a class defined in the body — ``path#f.Local``, ``path#Outer.m.Local`` — the way a
    nested class is ``path#Outer.Inner``, so two local classes sharing a name never collide."""
    name = node_text(fnode.child_by_field_name("name"), source)
    start, end = line_span(fnode)
    # The id carries the qualified owner (`Outer.Inner`) so a nested type's methods are
    # identified unambiguously; `class_name` stays simple for call resolution.
    fid = disambiguate(
        function_id(path, name, start, class_name=id_owner or class_name), seen_ids)
    ret = fnode.child_by_field_name("return_type")
    body = fnode.child_by_field_name("body")
    nested = collect_nested_definitions(body, source)
    qualified = f"{scope}.{name}" if scope else name
    barriers = frozenset(_span(f) for f, _ in nested)
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type="method" if class_name else "function",
        visibility=_visibility(name),
        isStatic=any(d.name == "staticmethod" for d in decorators),
        generics=node_text(fnode.child_by_field_name("type_parameters"), source)
        if fnode.child_by_field_name("type_parameters") is not None else None,
        params=extract_params(fnode.child_by_field_name("parameters"), source),
        decorators=[d for d in decorators if d.name not in ("staticmethod", "classmethod")],
        returnType=node_text(ret, source) if ret is not None else None,
        startLine=start,
        endLine=end,
        calls=_extract_calls(body, source, resolve, barriers, class_name,
                             fnode.child_by_field_name("parameters")),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids,
        descend_all=True,  # walk inline lambdas — attribute their statements here
        barriers=barriers,  # …except separately-extracted nested defs and classes
    )
    functions = [fn]
    classes: list[Class] = []
    for nested_node, nested_decs in nested:
        if nested_node.type == "class_definition":
            from .classes import build_class  # classes.py imports this module

            sub_classes, sub_fns, sub_stmts = build_class(
                nested_node, nested_decs, source, path,
                parent_id=file_id(path), seen_ids=seen_ids, capture=capture, limit=limit,
                resolve=resolve, owner=qualified,
            )
        else:
            sub_classes, sub_fns, sub_stmts = build_function(
                nested_node, extract_decorators(nested_decs, source), source, path,
                parent_id=file_id(path), class_name=None, seen_ids=seen_ids,
                capture=capture, limit=limit, resolve=resolve, scope=qualified,
            )
        classes.extend(sub_classes)
        functions.extend(sub_fns)
        statements.extend(sub_stmts)
    return classes, functions, statements
