"""PHP function / method / constructor extraction."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .attributes import extract_attributes
from .statements import extract_statements

_CALL_TYPES = (
    "member_call_expression",
    "scoped_call_expression",
    "function_call_expression",
    "nullsafe_member_call_expression",
)


def _flags(node: Node, source: bytes) -> tuple[str | None, bool | None]:
    visibility: str | None = None
    is_static: bool | None = None
    for child in node.children:
        if child.type == "visibility_modifier":
            visibility = node_text(child, source).strip()
        elif child.type == "static_modifier":
            is_static = True
    return visibility, is_static


def extract_params(params_node: Node | None, source: bytes) -> list[Parameter]:
    """Extract Parameter models from a formal_parameters node."""
    out: list[Parameter] = []
    if params_node is None:
        return out
    for p in params_node.named_children:
        if p.type in ("simple_parameter", "property_promotion_parameter", "variadic_parameter"):
            # Parameter name
            name_node = p.child_by_field_name("name")
            if name_node is None:
                # Find variable_name or name
                name_node = next(
                    (c for c in p.named_children if c.type in ("variable_name", "name")), None
                )
            param_name = node_text(name_node, source).lstrip("$") if name_node is not None else ""

            # Parameter type
            type_node = p.child_by_field_name("type")
            if type_node is None:
                # Check for named type / primitive type / optional_type / union_type
                type_node = next(
                    (
                        c
                        for c in p.named_children
                        if c.type
                        in (
                            "primitive_type",
                            "named_type",
                            "optional_type",
                            "union_type",
                            "intersection_type",
                            "type_list",
                        )
                    ),
                    None,
                )
            param_type = node_text(type_node, source) if type_node is not None else ""

            # Default value
            default_node = p.child_by_field_name("default_value")
            param_default = node_text(default_node, source) if default_node is not None else None

            decorators = extract_attributes(p, source)
            out.append(
                Parameter(
                    name=param_name,
                    type=param_type,
                    decorators=decorators,
                    default=param_default,
                )
            )
    return out


def _extract_return_type(node: Node, source: bytes) -> str | None:
    rt_node = node.child_by_field_name("return_type")
    if rt_node is not None:
        return node_text(rt_node, source).lstrip(":").strip()
    return None


def _calls(
    body: Node | None,
    source: bytes,
    class_name: str | None = None,
    resolve: CallResolver = noop_resolver,
) -> list[Call]:
    if body is None:
        return []
    calls: list[Call] = []
    seen: set[str] = set()

    def visit(node: Node) -> None:
        for child in node.named_children:
            if child.type in _CALL_TYPES:
                name_node = child.child_by_field_name("name") or child.child_by_field_name(
                    "function"
                )
                if name_node is not None:
                    name = node_text(name_node, source)
                    # Extract receiver / object
                    obj_node = child.child_by_field_name("object") or child.child_by_field_name(
                        "scope"
                    )
                    receiver = (
                        node_text(obj_node, source).lstrip("$") if obj_node is not None else None
                    )
                    if name and name not in seen:
                        seen.add(name)
                        calls.append(Call(name=name, path=resolve(name, receiver, class_name)))
            visit(child)

    visit(body)
    return calls


def build_function(
    node: Node,
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
    """Build a Function model for a function_definition or method_declaration."""
    start, end = line_span(node)
    vis, is_stat = _flags(node, source)

    params_node = node.child_by_field_name("parameters")
    params = extract_params(params_node, source)
    return_type = _extract_return_type(node, source)

    fn_type = "constructor" if name == "__construct" else kind
    fn_id = disambiguate(function_id(path, name, start, class_name=class_name), seen_ids)

    body = node.child_by_field_name("body")
    calls = _calls(body, source, class_name=class_name, resolve=resolve)

    # All attributes on the function/method
    all_decs = list(decorators) + extract_attributes(node, source)

    fn = Function(
        id=fn_id,
        parentId=parent_id,
        name=name,
        type=fn_type,
        startLine=start,
        endLine=end,
        path=path,
        visibility=vis,
        isStatic=is_stat,
        params=params,
        decorators=all_decs,
        returnType=return_type,
        calls=calls,
    )

    fn_statements: list[Statement] = []
    if capture and body is not None:
        fn_statements = extract_statements(
            body,
            source,
            path,
            parent_id=fn_id,
            capture=capture,
            limit=limit,
            seen_ids=seen_ids,
            descend_all=True,
        )

    return [fn], fn_statements


def defined_names(root: Node, source: bytes) -> set[str]:
    """Names of functions, methods, and classes defined in this file."""
    names: set[str] = set()
    types = {
        "function_definition",
        "method_declaration",
        "class_declaration",
        "interface_declaration",
        "trait_declaration",
        "enum_declaration",
    }
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in types:
            nm = n.child_by_field_name("name")
            if nm is not None:
                names.add(node_text(nm, source))
        stack.extend(n.named_children)
    return names


def type_map(root: Node, source: bytes) -> dict[str, str]:
    """Map of declared variable/parameter names to simple type names."""
    types: dict[str, str] = {}
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in ("simple_parameter", "property_promotion_parameter"):
            nm = n.child_by_field_name("name")
            tn = n.child_by_field_name("type")
            if nm is not None and tn is not None:
                vname = node_text(nm, source).lstrip("$")
                tname = node_text(tn, source)
                types[vname] = tname
        elif n.type == "property_declaration":
            tn = n.child_by_field_name("type")
            if tn is not None:
                tname = node_text(tn, source)
                for pe in n.named_children:
                    if pe.type == "property_element":
                        vn = pe.child_by_field_name("name")
                        if vn is not None:
                            vname = node_text(vn, source).lstrip("$")
                            types[vname] = tname
        stack.extend(n.named_children)
    return types
