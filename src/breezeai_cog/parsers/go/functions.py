"""Go function extraction and call collection."""

from __future__ import annotations

from dataclasses import dataclass, field

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .statements import bound_name, extract_statements


def _receiver_info(node: Node, source: bytes) -> tuple[str | None, str | None]:
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return None, None
    text = node_text(receiver, source).strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    parts = text.split()
    raw_type = parts[-1] if parts else ""
    receiver_kind = "pointer" if raw_type.startswith("*") else "value"
    type_text = raw_type.lstrip("*").rsplit(".", 1)[-1].split("[", 1)[0]
    return type_text or None, receiver_kind


def _extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    """One Parameter per name: `a, b int` declares two params of type `int`; a variadic
    `rest ...string` keeps the `...` on its type, as written. Unnamed params (`func(int)`)
    get an empty name."""
    if params_node is None:
        return []
    out: list[Parameter] = []
    for child in params_node.named_children:
        if child.type not in {"parameter_declaration", "variadic_parameter_declaration"}:
            continue
        type_node = child.child_by_field_name("type")
        type_text = node_text(type_node, source) if type_node is not None else ""
        if child.type == "variadic_parameter_declaration":
            type_text = "..." + type_text
        names = child.children_by_field_name("name") or [None]
        for name in names:
            out.append(
                Parameter(
                    name=node_text(name, source) if name is not None else "",
                    type=type_text,
                    decorators=[],
                )
            )
    return out


def _result_type(node: Node, source: bytes) -> str | None:
    """Return type as written, without result names: `*User`, `(int, error)` — named results
    `(n int, err error)` also give `(int, error)`."""
    result = node.child_by_field_name("result")
    if result is None:
        return None
    if result.type != "parameter_list":
        return node_text(result, source)
    return "(" + ", ".join(p.type for p in _extract_params(result, source)) + ")"


def _call_name(node: Node, source: bytes) -> tuple[str, str | None]:
    if node.type == "selector_expression":
        obj = node.child_by_field_name("operand")
        field = node.child_by_field_name("field")
        receiver = node_text(obj, source) if obj is not None else None
        method = node_text(field, source) if field is not None else ""
        return method, receiver
    if node.type == "identifier":
        return node_text(node, source), None
    if node.type == "call_expression":
        fn = node.child_by_field_name("function")
        if fn is not None:
            return _call_name(fn, source)
    return "", None


def _collect_calls(
    node: Node, source: bytes, resolve: CallResolver = noop_resolver,
    barriers: frozenset[tuple[int, int]] = frozenset(),
) -> list[Call]:
    """Calls in ``node``, inline function literals included; a literal stored in a variable
    (its span in ``barriers``) is a Function of its own and keeps its calls."""
    calls: list[Call] = []
    seen: set[tuple[str, str | None]] = set()  # one entry per name and target file

    def walk(current: Node) -> None:
        for child in current.named_children:
            if (child.start_byte, child.end_byte) in barriers:
                continue
            if child.type == "call_expression":
                name, receiver = _call_name(child, source)
                path = resolve(name, receiver) if name else None
                if name and (name, path) not in seen:
                    seen.add((name, path))
                    calls.append(Call(name=name, path=path))
            walk(child)

    walk(node)
    return calls


@dataclass
class FileScan:
    """What :func:`scan_file` collects in one walk of the tree."""

    #: Function, method and type names declared in the file (for call resolution).
    names: set[str] = field(default_factory=set)
    #: ``x := expr`` → ``{"x": "expr"}`` (for call resolution).
    types: dict[str, str] = field(default_factory=dict)
    #: Every ``func_literal`` stored in a variable, with that variable's name, in document order.
    bound: list[tuple[Node, str]] = field(default_factory=list)
    #: ``x.Name`` selectors and ``pkg.Type`` qualified types: (``x`` / ``pkg``, ``Name``). Those
    #: whose qualifier is an import alias are the names the file uses from that package.
    qualified: set[tuple[str, str]] = field(default_factory=set)


def scan_file(root: Node, source: bytes) -> FileScan:
    scan = FileScan()

    def walk(node: Node) -> None:
        for child in node.named_children:
            kind = child.type
            if kind in {"function_declaration", "method_declaration", "type_spec", "type_alias"}:
                name = child.child_by_field_name("name")
                if name is not None:
                    scan.names.add(node_text(name, source))
            elif kind == "short_var_declaration":
                lhs = child.child_by_field_name("left")
                rhs = child.child_by_field_name("right")
                if lhs is not None and rhs is not None:
                    for target in lhs.named_children:
                        if target.type == "identifier":
                            scan.types[node_text(target, source)] = node_text(rhs, source)
            elif kind == "func_literal":
                bound = bound_name(child, source)
                if bound is not None:
                    scan.bound.append((child, bound))
            elif kind == "selector_expression":
                operand, fld = child.child_by_field_name("operand"), child.child_by_field_name("field")
                if operand is not None and fld is not None and operand.type == "identifier":
                    scan.qualified.add((node_text(operand, source), node_text(fld, source)))
            elif kind == "qualified_type":
                pkg, name = child.child_by_field_name("package"), child.child_by_field_name("name")
                if pkg is not None and name is not None:
                    scan.qualified.add((node_text(pkg, source), node_text(name, source)))
            walk(child)

    walk(root)
    return scan


def _constructor_target_name(func_name: str, result_type: str | None) -> str | None:
    if not func_name.startswith("New") or not result_type:
        return None
    stripped = result_type.strip()
    candidate = stripped
    if candidate.startswith("(") and ")" in candidate:
        inner = candidate[1:candidate.rfind(")")]
        for part in inner.split(","):
            t = part.strip()
            if not t:
                continue
            candidate = t
            break
    if candidate.startswith("*"):
        candidate = candidate[1:]
    candidate = candidate.rsplit(".", 1)[-1]
    if candidate == "error":
        return None
    return candidate or None


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
    barriers: frozenset[tuple[int, int]] = frozenset(),
) -> tuple[Function, list[Statement]]:
    name = node.child_by_field_name("name")
    func_name = node_text(name, source) if name is not None else ""
    start, end = line_span(node)
    body = node.child_by_field_name("body")
    receiver, receiver_kind = _receiver_info(node, source)
    params = _extract_params(node.child_by_field_name("parameters"), source)
    result_type = _result_type(node, source)
    type_parameters = node.child_by_field_name("type_parameters")
    constructor_target = _constructor_target_name(func_name, result_type)

    fn = Function(
        id=disambiguate(function_id(path, func_name, start), seen_ids),
        parentId=parent_id,
        path=path,
        name=func_name,
        type=("constructor" if not receiver and constructor_target and (func_name == "New" or func_name.startswith("New")) else "method" if receiver else "function"),
        startLine=start,
        endLine=end,
        receiverType=receiver,
        metadata={"receiverKind": receiver_kind} if receiver_kind else None,
        visibility="public" if func_name[:1].isupper() else "package",
        generics=node_text(type_parameters, source) if type_parameters is not None else None,
        params=params,
        returnType=result_type,
        calls=_collect_calls(body, source, resolve, barriers) if body is not None else [],
    )
    statements = extract_statements(
        body, source, path, parent_id=fn.id, capture=capture, limit=limit, seen_ids=seen_ids,
        descend_all=True, barriers=barriers,
    ) if body is not None else []
    return fn, statements


def build_literal_function(
    node: Node,
    name: str,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    package_level: bool,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    barriers: frozenset[tuple[int, int]],
    resolve: CallResolver = noop_resolver,
) -> tuple[Function, list[Statement]]:
    """A function literal stored in a variable (``var notify = func(msg string) {…}``): a
    ``Function`` named after the variable, of type ``function_expression``. Literals nested in
    it that are stored in variables of their own (``barriers``) keep their own code."""
    start, end = line_span(node)
    body = node.child_by_field_name("body")
    fn = Function(
        id=disambiguate(function_id(path, name, start), seen_ids),
        parentId=parent_id,
        path=path,
        name=name,
        type="function_expression",
        startLine=start,
        endLine=end,
        visibility=("public" if name[:1].isupper() else "package") if package_level else None,
        params=_extract_params(node.child_by_field_name("parameters"), source),
        returnType=_result_type(node, source),
        calls=_collect_calls(body, source, resolve, barriers) if body is not None else [],
    )
    statements = extract_statements(
        body, source, path, parent_id=fn.id, capture=capture, limit=limit, seen_ids=seen_ids,
        descend_all=True, barriers=barriers,
    ) if body is not None else []
    return fn, statements
