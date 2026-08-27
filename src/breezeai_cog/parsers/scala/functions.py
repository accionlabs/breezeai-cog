"""Scala function / method + parameter + annotation + call extraction."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .statements import extract_statements

#: Local type declarations inside a function body. Their members belong to the type,
#: not to the enclosing function, and local types are not extracted as Class nodes —
#: so ``collect_nested_functions`` stops here rather than hoisting their methods.
_LOCAL_TYPE_SCOPES = (
    "class_definition",
    "object_definition",
    "trait_definition",
    "enum_definition",
)


def modifiers_node(node: Node) -> Node | None:
    return next((c for c in node.named_children if c.type == "modifiers"), None)


def _flags(node: Node) -> str:
    """Scala defaults to 'public' visibility."""
    mods = modifiers_node(node)
    if mods is not None:
        for child in mods.children:
            if child.type in ("private", "protected"):
                return child.type
    return "public"


def _annotation(node: Node, source: bytes) -> Decorator:
    name_node = (
        node.child_by_field_name("name")
        or next(
            (
                c
                for c in node.named_children
                if c.type in ("type_identifier", "scoped_type_identifier", "generic_type", "identifier")
            ),
            None,
        )
    )
    name = node_text(name_node, source).rsplit(".", 1)[-1] if name_node is not None else ""
    args: list[str] = []
    arglist = node.child_by_field_name("arguments")
    if arglist is not None:
        for arg in arglist.named_children:
            text = node_text(arg, source)
            if arg.type == "string":
                text = text.strip('"')
            args.append(text)
    return Decorator(name=name, args=args)


def extract_annotations(node: Node | None, source: bytes) -> list[Decorator]:
    if node is None:
        return []
    decorators: list[Decorator] = []
    # Check direct annotation children
    for c in node.named_children:
        if c.type == "annotation":
            decorators.append(_annotation(c, source))
        elif c.type == "modifiers":
            for mc in c.named_children:
                if mc.type == "annotation":
                    decorators.append(_annotation(mc, source))
    return decorators


def extract_params(node: Node, source: bytes) -> list[Parameter]:
    """Extract parameters from the first parameter list of a method/function."""
    out: list[Parameter] = []
    # Find the first `parameters` child list
    params_node = next((c for c in node.named_children if c.type == "parameters"), None)
    if params_node is None:
        return out

    for p in params_node.named_children:
        if p.type == "parameter":
            nnode = p.child_by_field_name("name")
            tnode = p.child_by_field_name("type")
            out.append(
                Parameter(
                    name=node_text(nnode, source) if nnode is not None else "",
                    type=node_text(tnode, source) if tnode is not None else "",
                    decorators=extract_annotations(p, source),
                )
            )
    return out


def _calls(
    body: Node | None,
    source: bytes,
    resolve: CallResolver = noop_resolver,
    barriers: frozenset[tuple[int, int]] = frozenset(),
) -> list[Call]:
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[str] = set()

    def visit(n: Node) -> None:
        # Descend through control flow and lambdas — those calls belong to the nearest
        # named enclosing function — but stop at ``barriers``: the spans of nested
        # ``def``s extracted as their own scope (see build_function).
        for child in n.named_children:
            if _span(child) in barriers:
                continue
            if child.type == "call_expression":
                fn = child.child_by_field_name("function")
                if fn is not None:
                    name: str = ""
                    receiver: str | None = None
                    if fn.type == "field_expression":
                        val = fn.child_by_field_name("value")
                        field = fn.child_by_field_name("field")
                        name = node_text(field, source) if field is not None else ""
                        receiver = node_text(val, source) if val is not None else None
                    elif fn.type == "identifier":
                        name = node_text(fn, source)
                    elif fn.type == "generic_function":
                        gfn = fn.child_by_field_name("function") or (fn.named_children[0] if fn.named_children else None)
                        if gfn is not None:
                            if gfn.type == "field_expression":
                                val = gfn.child_by_field_name("value")
                                field = gfn.child_by_field_name("field")
                                name = node_text(field, source) if field is not None else ""
                                receiver = node_text(val, source) if val is not None else None
                            elif gfn.type == "identifier":
                                name = node_text(gfn, source)
                    if name and name not in seen:
                        seen.add(name)
                        calls.append(Call(name=name, path=resolve(name, receiver)))
            visit(child)

    visit(body)
    return calls


def _span(node: Node) -> tuple[int, int]:
    return (node.start_byte, node.end_byte)


def collect_nested_functions(body: Node | None, source: bytes) -> list[Node]:
    """Nested ``def``s whose nearest named enclosing function is this one.

    Scala leans on local helper defs far more than Java does, so without this they
    were dropped entirely *and* their statements were folded into the enclosing
    function — attributing code to a scope that never declared it.

    Descends through control flow, blocks and lambdas but stops at each nested
    function (deeper names belong to its own recursion) and at a nested
    class/object/trait/enum (its members belong to that type, and local types are
    not extracted here). The returned nodes' spans double as the barrier set.
    """
    if body is None:
        return []
    out: list[Node] = []

    def visit(n: Node) -> None:
        for c in n.named_children:
            if c.type in ("function_definition", "function_declaration"):
                out.append(c)
                continue  # barrier: its body belongs to it, not to the enclosing fn
            if c.type in _LOCAL_TYPE_SCOPES:
                continue  # local class/object: not extracted here; folding unchanged
            visit(c)

    visit(body)
    return out


def defined_names(root: Node, source: bytes) -> set[str]:
    """Names of functions, methods, classes, traits, objects, enums defined in the file."""
    names: set[str] = set()
    types = {
        "function_definition",
        "function_declaration",
        "class_definition",
        "trait_definition",
        "object_definition",
        "enum_definition",
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


def type_map(root: Node, source: bytes) -> dict[str, str]:
    """Variable name → declared type, for receiver-type call resolution (Phase 2)."""
    types: dict[str, str] = {}

    def add(name_node: Node | None, type_node: Node | None, *, override: bool) -> None:
        if name_node is None or type_node is None:
            return
        name = node_text(name_node, source)
        if override or name not in types:
            types[name] = node_text(type_node, source)

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type in ("val_definition", "var_definition"):
                add(c.child_by_field_name("pattern"), c.child_by_field_name("type"), override=True)
            elif c.type == "parameter":
                add(c.child_by_field_name("name"), c.child_by_field_name("type"), override=False)
            elif c.type == "class_parameter":
                add(c.child_by_field_name("name"), c.child_by_field_name("type"), override=True)
            walk(c)

    walk(root)
    return types


def build_function(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    class_name: str | None,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    is_static: bool = False,
    fn_type: str = "method",
    resolve: CallResolver = noop_resolver,
) -> tuple[list[Function], list[Statement]]:
    """This function plus every nested ``def`` inside it, and their statements.

    Returns a list because a Scala ``def`` can declare local helper ``def``s; each is
    emitted as its own Function parented to this one, and its span is a barrier so its
    calls and statements are not double-counted here.
    """
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source) if name_node is not None else ""
    start, end = line_span(node)
    fid = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)
    visibility = _flags(node)
    ret_node = node.child_by_field_name("return_type")
    body = node.child_by_field_name("body")
    tp = node.child_by_field_name("type_parameters")
    nested = collect_nested_functions(body, source)
    barriers = frozenset(_span(f) for f in nested)

    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type=fn_type,
        visibility=visibility,
        isStatic=is_static,
        generics=node_text(tp, source) if tp is not None else None,
        params=extract_params(node, source),
        decorators=extract_annotations(node, source),
        returnType=node_text(ret_node, source) if ret_node is not None else None,
        startLine=start,
        endLine=end,
        calls=_calls(body, source, resolve, barriers) if body is not None else [],
    )

    statements = (
        extract_statements(
            body,
            source,
            path,
            parent_id=fid,
            capture=capture,
            limit=limit,
            seen_ids=seen_ids,
            descend_all=True,  # walk control flow / lambdas — their statements land here
            barriers=barriers,  # …except separately-extracted nested defs
        )
        if body is not None
        else []
    )

    functions = [fn]
    for nested_node in nested:
        sub_fns, sub_stmts = build_function(
            nested_node,
            source,
            path,
            parent_id=fid,
            class_name=None,
            seen_ids=seen_ids,
            capture=capture,
            limit=limit,
            is_static=False,
            fn_type="function",
            resolve=resolve,
        )
        functions.extend(sub_fns)
        statements.extend(sub_stmts)
    return functions, statements
