"""Function / method / arrow + parameter + decorator + call extraction (TS/JS)."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .statements import extract_statements

_DEF_TYPES = {
    "function_declaration", "generator_function_declaration", "method_definition",
    "class_declaration", "abstract_class_declaration", "interface_declaration", "enum_declaration",
}


def defined_names(root: Node, source: bytes) -> set[str]:
    """Function/method/class/interface names + arrow/fn consts defined in the file."""
    names: set[str] = set()

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type in _DEF_TYPES:
                nm = c.child_by_field_name("name")
                if nm is not None:
                    names.add(node_text(nm, source))
            elif c.type in ("lexical_declaration", "variable_declaration"):
                for d in c.named_children:
                    if d.type == "variable_declarator":
                        val = d.child_by_field_name("value")
                        if val is not None and val.type in ("arrow_function", "function_expression", "function"):
                            nm = d.child_by_field_name("name")
                            if nm is not None:
                                names.add(node_text(nm, source))
            walk(c)

    walk(root)
    return names


def type_map(root: Node, source: bytes) -> dict[str, str]:
    """Variable name → declared type, for receiver-type call resolution (Phase 2):
    class fields, constructor parameter-properties, params, and typed locals."""
    types: dict[str, str] = {}

    def add(name_node: Node | None, type_node: Node | None, *, override: bool) -> None:
        if name_node is None or type_node is None:
            return
        t = _type_text(type_node, source)
        if not t:
            return
        name = node_text(name_node, source)
        if override or name not in types:
            types[name] = t

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type == "public_field_definition":
                add(c.child_by_field_name("name"), c.child_by_field_name("type"), override=True)
            elif c.type in ("required_parameter", "optional_parameter"):
                add(c.child_by_field_name("pattern"), c.child_by_field_name("type"), override=False)
            elif c.type == "variable_declarator":
                add(c.child_by_field_name("name"), c.child_by_field_name("type"), override=False)
            walk(c)

    walk(root)
    return types


def _type_text(annotation: Node | None, source: bytes) -> str | None:
    if annotation is None:
        return None
    return node_text(annotation, source).lstrip(":").strip() or None


# Type-expression → DTO name: skip generic wrappers/primitives, take the first PascalCase
# name (``Promise<OrderDto[]>`` → ``OrderDto``, ``void``/``object`` → None). Shared by the
# route parsers (NestJS keeps a private copy; LoopBack uses this) for responseDTO capture.
_DTO_ID_RE = re.compile(r"[A-Za-z_$][\w$]*")
_NON_DTO_TYPES = {
    "Promise", "Observable", "Array", "Map", "Set", "Record", "Partial", "Readonly",
    "void", "any", "unknown", "never", "null", "undefined", "string", "number",
    "boolean", "object", "bigint", "symbol", "this", "true", "false",
}


def dto_from_type(t: str | None) -> str | None:
    if not t:
        return None
    for tok in _DTO_ID_RE.findall(t):
        if tok in _NON_DTO_TYPES or not tok[0].isupper():
            continue
        return tok
    return None


def return_dto(member: Node, source: bytes) -> str | None:
    """Handler return type → responseDTO (``Promise<TimezoneDto[]>`` → ``TimezoneDto``)."""
    return dto_from_type(_type_text(member.child_by_field_name("return_type"), source))


def _visibility(node: Node, source: bytes) -> str:
    for child in node.named_children:
        if child.type == "accessibility_modifier":
            return node_text(child, source)
    return "public"


def decorator(node: Node, source: bytes) -> Decorator:
    inner = node.named_children[0] if node.named_children else None
    if inner is None:
        return Decorator(name=node_text(node, source).lstrip("@"), args=[])
    args: list[str] = []
    if inner.type == "call_expression":
        arglist = inner.child_by_field_name("arguments")
        if arglist is not None:
            args = [node_text(a, source) for a in arglist.named_children]
        inner = inner.child_by_field_name("function") or inner
    return Decorator(name=node_text(inner, source).rsplit(".", 1)[-1], args=args)


def extract_decorators(nodes: list[Node], source: bytes) -> list[Decorator]:
    return [decorator(n, source) for n in nodes]


def extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    out: list[Parameter] = []
    if params_node is None:
        return out
    for p in params_node.named_children:
        if p.type in ("required_parameter", "optional_parameter"):
            pat = p.child_by_field_name("pattern")
            name = node_text(pat, source) if pat is not None else ""
            decs = extract_decorators([c for c in p.named_children if c.type == "decorator"], source)
            out.append(Parameter(
                name=name, type=_type_text(p.child_by_field_name("type"), source) or "",
                decorators=decs,  # e.g. Nest @Body/@Param/@Query, Angular @Inject
            ))
        elif p.type == "rest_pattern":
            ident = next((c for c in p.named_children if c.type == "identifier"), None)
            out.append(Parameter(name="..." + (node_text(ident, source) if ident else ""), type=""))
        elif p.type == "identifier":
            out.append(Parameter(name=node_text(p, source), type=""))
    return out


NESTED_FN_VALUE_TYPES = ("arrow_function", "function_expression", "function")


def _span(node: Node) -> tuple[int, int]:
    """Stable identity for a node within one parse (used as a barrier key)."""
    return (node.start_byte, node.end_byte)


def _property_key_name(key: Node | None, source: bytes) -> str:
    """Object-property key → function name. Strips quotes from string keys so
    ``{ "eachMessage": () => … }`` names the function ``eachMessage``."""
    if key is None:
        return ""
    text = node_text(key, source)
    if key.type in ("string", "string_fragment") and len(text) >= 2 and text[0] in "\"'`":
        return text[1:-1]
    return text


def collect_nested_functions(body: Node | None, source: bytes) -> list[tuple[Node, str, str]]:
    """Named functions declared *inside* this body whose nearest named enclosing
    function is this one. Recognizes every way a nested function acquires a name:
    in-body ``function`` declarations, ``const f = () =>`` / function-expression
    bindings (the React event-handler pattern), object-property arrows/functions
    (``{ eachMessage: async () => … }`` — the Kafka handler / NestJS ``useFactory``
    pattern), and named function expressions passed as callbacks
    (``arr.forEach(function step() { … })``). Descends through anonymous callbacks
    and control flow — a handler defined inside ``useEffect(() => …)`` still belongs
    here — but stops at each named function, since those deeper names belong to that
    function's own recursion.

    Returns ``(value_node, name, kind)`` per nested function; the value nodes double
    as the barrier set so the enclosing function does not also fold their calls/
    statements (preserving one-call-per-nearest-named-function attribution)."""
    if body is None:
        return []
    out: list[tuple[Node, str, str]] = []

    def visit(n: Node) -> None:
        for c in n.named_children:
            if c.type == "function_declaration":
                nm = c.child_by_field_name("name")
                out.append((c, node_text(nm, source) if nm is not None else "", "function"))
                continue  # barrier: its body belongs to it, not to the enclosing fn
            if c.type in ("lexical_declaration", "variable_declaration"):
                for d in c.named_children:
                    if d.type != "variable_declarator":
                        visit(d)
                        continue
                    val = d.child_by_field_name("value")
                    if val is not None and val.type in NESTED_FN_VALUE_TYPES:
                        nm = d.child_by_field_name("name")
                        out.append((val, node_text(nm, source) if nm is not None else "", val.type))
                    elif val is not None:
                        visit(val)  # e.g. const x = arr.map(() => { const g = () => {} })
                continue
            if c.type == "pair":
                # Object-property function: name comes from the key
                # (``{ eachMessage: async () => … }``, NestJS ``{ useFactory: () => … }``).
                val = c.child_by_field_name("value")
                if val is not None and val.type in NESTED_FN_VALUE_TYPES:
                    out.append((val, _property_key_name(c.child_by_field_name("key"), source), val.type))
                    continue  # barrier: its body belongs to it
                visit(c)
                continue
            if c.type == "function_expression":
                # Named function expression used as a callback
                # (``arr.forEach(function step() { … })``). Anonymous ones fall
                # through to ``visit`` so their calls stay with the enclosing fn.
                nm = c.child_by_field_name("name")
                if nm is not None:
                    out.append((c, node_text(nm, source), "function_expression"))
                    continue  # barrier: its body belongs to it
            visit(c)  # anonymous callbacks, control flow, blocks, expressions

    visit(body)
    return out


def _calls(
    body: Node | None, source: bytes, resolve: CallResolver = noop_resolver,
    owner: str | None = None, barriers: frozenset[tuple[int, int]] = frozenset(),
) -> list[Call]:
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[str] = set()

    def visit(node: Node) -> None:
        # Descend into inline callbacks/lambdas — their calls belong to the nearest
        # named enclosing function — but stop at ``barriers``: the spans of nested
        # named functions extracted as their own scope. See build_function.
        for child in node.named_children:
            if _span(child) in barriers:
                continue
            if child.type == "call_expression":
                fn = child.child_by_field_name("function")
                if fn is not None:
                    # Normalize optional chaining so `this.svc?.m()` resolves like `this.svc.m()`
                    # (otherwise the receiver becomes `this.svc?` and never matches a type).
                    callee = node_text(fn, source).replace("?.", ".")
                    name = callee.rsplit(".", 1)[-1]
                    receiver = callee.rsplit(".", 1)[0] if "." in callee else None
                    if name.isidentifier() and name not in seen:
                        seen.add(name)
                        calls.append(Call(name=name, path=resolve(name, receiver, owner)))
            visit(child)

    visit(body)
    return calls


def build_function(
    node: Node,
    *,
    name: str,
    kind: str,
    decorators: list[Decorator],
    source: bytes,
    path: str,
    parent_id: str,
    class_name: str | None,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
) -> tuple[list[Function], list[Statement]]:
    start, end = line_span(node)
    fid = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)
    body = node.child_by_field_name("body")
    # Nested named functions (in-body `function`/`const f = () =>`) are extracted as
    # their own Function nodes, parented to this one; their spans become barriers so
    # this function does not also fold their calls/statements.
    nested = collect_nested_functions(body, source)
    barriers = frozenset(_span(v) for v, _, _ in nested)
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type=kind,
        visibility=_visibility(node, source),
        isStatic=any(c.type == "static" for c in node.children),
        generics=_type_text(node.child_by_field_name("type_parameters"), source) or None,
        params=extract_params(node.child_by_field_name("parameters"), source),
        decorators=decorators,
        returnType=_type_text(node.child_by_field_name("return_type"), source),
        startLine=start,
        endLine=end,
        calls=_calls(body, source, resolve, class_name, barriers),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids,
        descend_all=True,  # walk inline callbacks/lambdas — attribute their statements here
        barriers=barriers,  # …except separately-extracted nested named functions
    )
    functions = [fn]
    for value_node, nested_name, nested_kind in nested:
        sub_fns, sub_stmts = build_function(
            value_node, name=nested_name, kind=nested_kind, decorators=[], source=source,
            path=path, parent_id=fid, class_name=class_name, seen_ids=seen_ids,
            capture=capture, limit=limit, resolve=resolve,
        )
        functions.extend(sub_fns)
        statements.extend(sub_stmts)
    return functions, statements
