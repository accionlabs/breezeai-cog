"""Go function extraction and call collection."""

from __future__ import annotations

from dataclasses import dataclass, field

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .statements import extract_statements


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


def _collect_calls(node: Node, source: bytes, resolve: CallResolver = noop_resolver) -> list[Call]:
    calls: list[Call] = []
    seen: set[str] = set()

    def walk(current: Node) -> None:
        for child in current.named_children:
            if child.type == "call_expression":
                name, receiver = _call_name(child, source)
                if name and name not in seen:
                    seen.add(name)
                    calls.append(Call(name=name, path=resolve(name, receiver)))
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
    #: Every ``func_literal`` in document order, with the start byte of its nearest enclosing
    #: function/method/literal node (``None`` at package level).
    literals: list[tuple[Node, int | None]] = field(default_factory=list)


_FUNCTION_NODES = frozenset({"function_declaration", "method_declaration", "func_literal"})


def scan_file(root: Node, source: bytes) -> FileScan:
    scan = FileScan()

    def walk(node: Node, owner: int | None) -> None:
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
                scan.literals.append((child, owner))
            walk(child, child.start_byte if kind in _FUNCTION_NODES else owner)

    walk(root, None)
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
        calls=_collect_calls(body, source, resolve) if body is not None else [],
    )
    statements = extract_statements(body, source, path, parent_id=fn.id, capture=capture, limit=limit, seen_ids=seen_ids, descend_all=True) if body is not None else []
    return fn, statements


def build_anonymous_function(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    resolve: CallResolver = noop_resolver,
) -> Function:
    """Build a record for a Go function literal without re-emitting its body."""
    start, end = line_span(node)
    return Function(
        id=disambiguate(function_id(path, "<anonymous>", start), seen_ids),
        parentId=parent_id,
        path=path,
        name="<anonymous>",
        type="anonymous",
        startLine=start,
        endLine=end,
        params=_extract_params(node.child_by_field_name("parameters"), source),
        calls=_collect_calls(node.child_by_field_name("body"), source, resolve)
        if node.child_by_field_name("body") is not None else [],
    )
