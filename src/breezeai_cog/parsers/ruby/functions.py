"""Ruby function / method extraction."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .models import AnnotationCache
from .statements import extract_statements

_VISIBILITY_MARKERS = {"public", "protected", "private"}


def defined_names(root: Node, source: bytes) -> set[str]:
    """Return names defined directly in a Ruby file/class/module scope."""
    names: set[str] = set()

    def add_name(node: Node) -> None:
        if node.type == "method":
            name = next((c for c in node.named_children if c.type == "identifier"), None)
            if name is not None:
                names.add(node_text(name, source))
        if node.type in {"class", "module"}:
            name = next((c for c in node.named_children if c.type == "constant"), None)
            if name is not None:
                names.add(node_text(name, source))

    body = next((c for c in root.named_children if c.type == "body_statement"), None)
    for node in (body.named_children if body is not None else root.named_children):
        add_name(node)
    return names


def _extract_name(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    if node.type in {"identifier", "constant", "instance_variable"}:
        return node_text(node, source)
    if node.type == "call":
        return node_text(node, source)
    return node_text(node, source)


def extract_params(
    params_node: Node | None,
    source: bytes,
    param_types: dict[str, str] | None = None,
) -> list[Parameter]:
    if params_node is None:
        return []
    types = param_types or {}
    out: list[Parameter] = []
    for child in params_node.named_children:
        if child.type == "identifier":
            p_name = node_text(child, source)
            out.append(Parameter(name=p_name, type=types.get(p_name, "")))
        elif child.type == "optional_parameter":
            name_node = child.child_by_field_name("name")
            default_node = child.child_by_field_name("value")
            p_name = node_text(name_node, source) if name_node is not None else ""
            default = node_text(default_node, source) if default_node is not None else None
            out.append(Parameter(
                name=p_name,
                type=types.get(p_name, ""),
                default=default,
            ))
        elif child.type == "keyword_parameter":
            name_node = child.child_by_field_name("name")
            value_node = child.child_by_field_name("value")
            p_name = node_text(name_node, source) if name_node is not None else ""
            p_type = types.get(p_name, "")
            default = None
            if value_node is not None:
                default = node_text(value_node, source)
            out.append(Parameter(
                name=p_name,
                type=p_type,
                default=default,
            ))
        elif child.type in {"splat_parameter", "hash_splat_parameter", "block_parameter"}:
            name_node = child.child_by_field_name("name")
            p_name = node_text(name_node, source) if name_node is not None else node_text(child, source)
            prefix = "*" if child.type == "splat_parameter" else ("**" if child.type == "hash_splat_parameter" else "&")
            if not p_name.startswith(prefix):
                p_name = prefix + p_name
            out.append(Parameter(name=p_name, type=types.get(p_name, "")))
    return out


def _named_visibility_calls(parent: Node, source: bytes) -> dict[str, tuple[str, bool]]:
    result: dict[str, tuple[str, bool]] = {}
    for sibling in parent.named_children:
        if sibling.type != "call" or sibling.child_by_field_name("receiver") is not None:
            continue
        method = sibling.child_by_field_name("method")
        marker = node_text(method, source) if method is not None else ""
        if marker not in {*_VISIBILITY_MARKERS, "private_class_method"}:
            continue
        arguments = sibling.child_by_field_name("arguments")
        if arguments is None:
            continue
        for arg in arguments.named_children:
            if arg.type not in {"simple_symbol", "symbol", "string"}:
                continue
            name = node_text(arg, source).lstrip(":").strip()
            if arg.type == "string" and len(name) >= 2 and name[0] in "'\"":
                name = name[1:-1]
            if name:
                result[name] = (
                    "private" if marker == "private_class_method" else marker,
                    marker == "private_class_method",
                )
    return result


def _visibility_by_scope(parent: Node, source: bytes) -> dict[tuple[int, int], str]:
    """Compute method visibility for one scope in a single pass."""
    named_visibility = _named_visibility_calls(parent, source)
    visibility = "private" if parent.type == "program" else "public"
    singleton_visibility = "public"
    result: dict[tuple[int, int], str] = {}

    for sibling in parent.named_children:
        if sibling.type in {"method", "singleton_method"}:
            name_node = sibling.child_by_field_name("name")
            if name_node is None:
                name_node = next((child for child in sibling.named_children if child.type == "identifier"), None)
            method_name = node_text(name_node, source) if name_node is not None else ""
            current_visibility = singleton_visibility if sibling.type == "singleton_method" else visibility
            explicit = named_visibility.get(method_name)
            if explicit is not None and (
                explicit[1] == (sibling.type == "singleton_method")
            ):
                current_visibility = explicit[0]
            result[(sibling.start_byte, sibling.end_byte)] = current_visibility
            continue
        if sibling.type == "identifier":
            marker = node_text(sibling, source)
            if marker in _VISIBILITY_MARKERS:
                visibility = marker
            elif (
                marker == "module_function"
                and parent.type == "body_statement"
                and parent.parent is not None
                and parent.parent.type == "module"
            ):
                visibility = "private"
            continue
        if sibling.type != "call" or sibling.child_by_field_name("receiver") is not None:
            continue
        if sibling.child_by_field_name("arguments") is not None:
            continue
        method = sibling.child_by_field_name("method")
        if method is not None:
            marker = node_text(method, source)
            if marker in _VISIBILITY_MARKERS:
                # Bare visibility calls affect instance methods in class/module scopes.
                # At file scope they affect Object's instance methods as well.
                visibility = marker
    if parent.type == "body_statement" and parent.parent is not None and parent.parent.type == "module":
        module_functions = _module_function_methods(parent, source)
        for sibling in parent.named_children:
            key = (sibling.start_byte, sibling.end_byte)
            if sibling.type not in {"method", "singleton_method"} or key not in module_functions:
                continue
            name_node = sibling.child_by_field_name("name")
            name = node_text(name_node, source) if name_node is not None else ""
            explicit = named_visibility.get(name)
            if explicit is not None and not explicit[1]:
                result[key] = explicit[0]
            else:
                result[key] = "private"
    return result


def _visibility(node: Node, source: bytes) -> str:
    """Determine visibility when no scope-level visibility map was provided."""
    parent = node.parent
    if parent is None:
        return "public"
    return _visibility_by_scope(parent, source).get((node.start_byte, node.end_byte), "public")


def _extract_return_type(
    node: Node, source: bytes, annotation_cache: AnnotationCache | None = None
) -> str | None:
    """Extract the return type from Sorbet sig or YARD @return comment preceding a method.

    Fixes C3: captures ``Function.returnType`` from:
    - Sorbet: ``sig { returns(SomeType) }``
    - YARD: ``# @return [SomeType]``
    """
    cache = annotation_cache or AnnotationCache()
    return cache.annotations(node, source).return_type


def _module_function_methods(parent: Node, source: bytes) -> set[tuple[int, int]]:
    names: set[str] = set()
    all_following = False
    methods: set[tuple[int, int]] = set()
    for sibling in parent.named_children:
        if sibling.type == "identifier" and node_text(sibling, source) == "module_function":
            all_following = True
            continue
        if sibling.type in {"method", "singleton_method"}:
            method_node = sibling.child_by_field_name("name")
            method_name = node_text(method_node, source) if method_node is not None else ""
            if all_following or method_name in names:
                methods.add((sibling.start_byte, sibling.end_byte))
            continue
        if sibling.type != "call" or sibling.child_by_field_name("receiver") is not None:
            continue
        method = sibling.child_by_field_name("method")
        if method is None or node_text(method, source) != "module_function":
            continue
        arguments = sibling.child_by_field_name("arguments")
        if arguments is None or not arguments.named_children:
            all_following = True
            continue
        for argument in arguments.named_children:
            if argument.type in {"simple_symbol", "symbol", "string"}:
                names.add(node_text(argument, source).lstrip(":").strip().strip("'\""))
    for sibling in parent.named_children:
        if sibling.type in {"method", "singleton_method"}:
            method_node = sibling.child_by_field_name("name")
            method_name = node_text(method_node, source) if method_node is not None else ""
            if method_name in names:
                methods.add((sibling.start_byte, sibling.end_byte))
    return methods


# Bare identifiers are ambiguous between method calls and local-variable
# reads without semantic analysis, so deliberately skip them to avoid
# fabricating CALLS edges. However, explicit ``self.foo`` calls within a
# class body are unambiguously self-calls and are safe to capture (C10 fix).
def _calls_in_block(body: Node | None, source: bytes, resolve: CallResolver = noop_resolver) -> list[Call]:
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[tuple[str, str | None]] = set()

    def visit(node: Node) -> None:
        if node.type == "call":
            method_node = node.child_by_field_name("method")
            receiver_node = node.child_by_field_name("receiver")
            if method_node is not None:
                name = node_text(method_node, source)
                # Preserve the full receiver expression. A chained receiver such as
                # ``x.y`` must not collapse to ``None`` and look like a local ``y()`` call.
                receiver = node_text(receiver_node, source) if receiver_node is not None else None
                key = (name, receiver)
                if key not in seen:
                    seen.add(key)
                    calls.append(Call(name=name, path=resolve(name, receiver)))
        for child in node.named_children:
            visit(child)

    visit(body)
    return calls


def build_function(
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
    typed_db_ids: frozenset[str] | None = None,
    visibility: str | None = None,
    is_static: bool = False,
    annotation_cache: AnnotationCache | None = None,
) -> tuple[list[Function], list[Statement]]:
    is_singleton = node.type == "singleton_method"
    # singleton_method has the pattern: def self.foo(...) — name is the second identifier
    name_node = node.child_by_field_name("name")
    if name_node is None and is_singleton:
        # Grammar: singleton_method → "def" object "." name parameters body "end"
        # The object is typically `self`; skip it to find the actual method name
        idents = [c for c in node.named_children if c.type == "identifier"]
        name_node = idents[-1] if idents else None
    elif name_node is None:
        name_node = next((c for c in node.named_children if c.type == "identifier"), None)
    name = node_text(name_node, source) if name_node is not None else "<anonymous>"
    start, end = line_span(node)
    fid = disambiguate(function_id(path, name, start, class_name=id_owner or class_name), seen_ids)
    params_node = node.child_by_field_name("parameters")
    annotation_cache = annotation_cache or AnnotationCache()
    annotations = annotation_cache.annotations(node, source)
    param_types = annotations.param_types
    body = next((c for c in node.named_children if c.type == "body_statement"), None)
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type="method",
        visibility=visibility if visibility is not None else _visibility(node, source),
        isStatic=True if is_singleton or is_static else None,  # C4: mark class methods as static
        returnType=annotations.return_type,  # C3: capture return type
        params=extract_params(params_node, source, param_types=param_types),
        startLine=start,
        endLine=end,
        calls=_calls_in_block(body, source, resolve),
    )
    scoped_typed_db_ids = set(typed_db_ids) if typed_db_ids else set()
    for p_name, p_type in param_types.items():
        if p_type in scoped_typed_db_ids or p_type.rsplit("::", 1)[-1] in scoped_typed_db_ids:
            scoped_typed_db_ids.add(p_name)
    fn_typed_ids = frozenset(scoped_typed_db_ids) if scoped_typed_db_ids else None
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit,
        seen_ids=seen_ids, typed_db_ids=fn_typed_ids,
    )
    return [fn], statements
