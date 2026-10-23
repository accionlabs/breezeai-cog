"""Ruby function / method extraction."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .models import extract_param_types
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


def _symbol_names(parent: Node, source: bytes, call_name: str) -> set[str]:
    """Return method names declared private via ``private :name`` or ``private :a, :b``.

    Fixes C17: handles the ``private :method_name`` form (calling ``private`` with a symbol
    argument naming a specific method) in addition to the existing section-marker form.
    """
    names: set[str] = set()
    for sibling in parent.named_children:
        if sibling.type != "call":
            continue
        if sibling.child_by_field_name("receiver") is not None:
            continue
        method = sibling.child_by_field_name("method")
        if method is None or node_text(method, source) != call_name:
            continue
        arguments = sibling.child_by_field_name("arguments")
        if arguments is None:
            continue
        for arg in arguments.named_children:
            if arg.type in {"simple_symbol", "symbol", "string"}:
                sym = node_text(arg, source).lstrip(":").strip()
                if arg.type == "string" and len(sym) >= 2 and sym[0] in "'\"":
                    sym = sym[1:-1]
                if sym:
                    names.add(sym)
    return names


def _visibility_by_scope(parent: Node, source: bytes) -> dict[tuple[int, int], str]:
    """Compute method visibility for one scope in a single pass."""
    private_by_symbol = _symbol_names(parent, source, "private")
    private_class_by_symbol = _symbol_names(parent, source, "private_class_method")
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
            result[(sibling.start_byte, sibling.end_byte)] = (
                "private" if (
                    (sibling.type == "singleton_method" and method_name in private_class_by_symbol)
                    or (sibling.type == "method" and method_name in private_by_symbol)
                ) else current_visibility
            )
            continue
        if sibling.type == "identifier":
            marker = node_text(sibling, source)
            if marker in _VISIBILITY_MARKERS:
                visibility = marker
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
    return result


def _visibility(node: Node, source: bytes) -> str:
    """Determine visibility when no scope-level visibility map was provided."""
    parent = node.parent
    if parent is None:
        return "public"
    return _visibility_by_scope(parent, source).get((node.start_byte, node.end_byte), "public")


def _extract_return_type(node: Node, source: bytes) -> str | None:
    """Extract the return type from Sorbet sig or YARD @return comment preceding a method.

    Fixes C3: captures ``Function.returnType`` from:
    - Sorbet: ``sig { returns(SomeType) }``
    - YARD: ``# @return [SomeType]``
    """
    parent = node.parent
    if parent is None:
        return None
    children = parent.named_children
    if node not in children:
        return None
    idx = children.index(node)
    for sib in reversed(children[:idx]):
        if sib.type == "comment":
            text = node_text(sib, source)
            m = re.search(r"@return\s+\[([A-Za-z0-9_:<>, ]+)\]", text)
            if m:
                return m.group(1).strip()
        elif sib.type == "call":
            m_node = sib.child_by_field_name("method")
            if m_node is not None and node_text(m_node, source) == "sig":
                # Look for .returns(T) inside the sig block
                def find_returns(n: Node) -> str | None:
                    if n.type == "call":
                        meth = n.child_by_field_name("method")
                        if meth is not None and node_text(meth, source) == "returns":
                            args = n.child_by_field_name("arguments")
                            if args is not None and args.named_children:
                                return node_text(args.named_children[0], source)
                    for c in n.named_children:
                        result = find_returns(c)
                        if result is not None:
                            return result
                    return None
                result = find_returns(sib)
                if result is not None:
                    return result
        else:
            break
    return None


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
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
    typed_db_ids: frozenset[str] | None = None,
    visibility: str | None = None,
    is_static: bool = False,
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
    fid = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)
    params_node = node.child_by_field_name("parameters")
    param_types = extract_param_types(node, source)
    body = next((c for c in node.named_children if c.type == "body_statement"), None)
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type="method",
        visibility=visibility if visibility is not None else _visibility(node, source),
        isStatic=True if is_singleton or is_static else None,  # C4: mark class methods as static
        returnType=_extract_return_type(node, source),  # C3: capture return type
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
