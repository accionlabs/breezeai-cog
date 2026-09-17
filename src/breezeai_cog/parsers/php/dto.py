"""PHP DTO and FQCN type resolution utilities for route statement extraction.

Supports:
- Resolving PHP type hints to fully qualified class names (FQCN) using file
  use declarations (including grouped use imports and aliases) and namespaces.
- Extracting requestDTO from Symfony action parameters (#[MapRequestPayload] or DTO convention).
- Extracting requestDTO from Laravel route handlers (FormRequest-typed closure or controller parameters).
- Extracting responseDTO from typed return signatures.
"""

from __future__ import annotations

from tree_sitter import Node

from ..treesitter import node_text

_PRIMITIVE_TYPES = frozenset(
    {
        "int",
        "integer",
        "float",
        "double",
        "string",
        "bool",
        "boolean",
        "array",
        "object",
        "callable",
        "iterable",
        "mixed",
        "void",
        "never",
        "null",
        "resource",
        "self",
        "static",
        "parent",
        "true",
        "false",
    }
)

_GENERIC_FRAMEWORK_TYPES = frozenset(
    {
        "Request",
        "ServerRequestInterface",
        "RequestInterface",
        "Response",
        "JsonResponse",
        "ResponseInterface",
        "EntityManagerInterface",
        "Security",
        "UserInterface",
        "LoggerInterface",
        "ContainerInterface",
        "Closure",
    }
)


def extract_use_map(root: Node, source: bytes) -> tuple[str, dict[str, str]]:
    """Extract (namespace, {simple_name_or_alias: fqcn}) from a PHP AST."""
    namespace = ""
    for child in root.named_children:
        if child.type == "namespace_definition":
            nm = child.child_by_field_name("name")
            if nm is not None:
                namespace = node_text(nm, source).strip("\\")
            break

    use_map: dict[str, str] = {}

    def add_fqcn(raw_target: str, alias_node: Node | None = None) -> None:
        simple_name = (
            node_text(alias_node, source)
            if alias_node is not None
            else raw_target.rsplit("\\", 1)[-1]
        )
        use_map[simple_name] = raw_target

    for child in root.named_children:
        if child.type != "namespace_use_declaration":
            continue
        group = next((node for node in child.named_children if node.type == "namespace_use_group"), None)
        group_prefix_node = next(
            (node for node in child.named_children if node.type == "namespace_name"), None
        )
        if group is not None and group_prefix_node is not None:
            group_prefix = node_text(group_prefix_node, source).strip("\\")
            body = group.child_by_field_name("body")
            for clause in (body.named_children if body is not None else group.named_children):
                if clause.type != "namespace_use_clause":
                    continue
                target_node = next(
                    (node for node in clause.named_children if node.type in ("qualified_name", "name")),
                    None,
                )
                if target_node is not None:
                    target = node_text(target_node, source).strip("\\")
                    add_fqcn(f"{group_prefix}\\{target}", clause.child_by_field_name("alias"))
            continue
        for clause in child.named_children:
            if clause.type != "namespace_use_clause":
                continue
            target_node = next(
                (
                    c
                    for c in clause.named_children
                    if c.type in ("qualified_name", "name", "namespace_name")
                ),
                None,
            )
            if target_node is None:
                continue
            raw_target = node_text(target_node, source).strip("\\")
            add_fqcn(raw_target, clause.child_by_field_name("alias"))

    return namespace, use_map


def resolve_type_to_fqcn(
    type_name: str | None,
    use_map: dict[str, str],
    namespace: str = "",
    allow_primitives: bool = False,
) -> str | None:
    """Resolve a PHP type-hint string to its FQCN."""
    if not type_name:
        return None
    cleaned = type_name.strip().lstrip(":").strip().lstrip("?")
    if not cleaned:
        return None
    if not allow_primitives and cleaned.lower() in _PRIMITIVE_TYPES:
        return None
    if cleaned.startswith("\\"):
        return cleaned.lstrip("\\")
    if cleaned in use_map:
        return use_map[cleaned]
    if "\\" in cleaned:
        first, rest = cleaned.split("\\", 1)
        if first in use_map:
            return f"{use_map[first]}\\{rest}"
        if namespace:
            return f"{namespace}\\{cleaned}"
        return cleaned
    if namespace:
        return f"{namespace}\\{cleaned}"
    return cleaned


def _param_type(param_node: Node, source: bytes) -> str | None:
    type_node = param_node.child_by_field_name("type")
    if type_node is None:
        type_node = next(
            (
                c
                for c in param_node.named_children
                if c.type
                in (
                    "primitive_type",
                    "named_type",
                    "qualified_name",
                    "optional_type",
                    "union_type",
                    "intersection_type",
                    "type_list",
                )
            ),
            None,
        )
    return node_text(type_node, source) if type_node is not None else None


def _param_has_attr(param_node: Node, source: bytes, attr_name: str) -> bool:
    for child in param_node.named_children:
        if child.type == "attribute_list":
            for attr in child.named_children:
                if attr.type == "attribute":
                    name_node = attr.child_by_field_name("name") or (
                        attr.named_children[0] if attr.named_children else None
                    )
                    if name_node is not None:
                        name = node_text(name_node, source).strip("\\").rsplit("\\", 1)[-1]
                        if name == attr_name:
                            return True
    return False


def _is_laravel_form_request(
    param_type: str,
    form_request_classes: set[str] | None = None,
) -> bool:
    clean = param_type.lstrip("?\\").strip()
    simple = clean.rsplit("\\", 1)[-1]
    if simple in _GENERIC_FRAMEWORK_TYPES:
        return False
    if form_request_classes and simple in form_request_classes:
        return True
    return simple.endswith(("Request", "FormRequest")) and simple != "Request"


def _extract_return_type_from_node(node: Node, source: bytes) -> str | None:
    rt_node = node.child_by_field_name("return_type")
    if rt_node is not None:
        return node_text(rt_node, source).lstrip(":").strip()
    return None


def extract_callable_dtos(
    callable_node: Node,
    source: bytes,
    use_map: dict[str, str],
    namespace: str,
    form_request_classes: set[str] | None = None,
) -> tuple[str | None, str | None]:
    """Extract (requestDTO, responseDTO) from a closure, arrow function, or method AST node."""
    request_dto: str | None = None
    response_dto: str | None = None

    params_node = callable_node.child_by_field_name("parameters")
    if params_node is None:
        params_node = next(
            (c for c in callable_node.named_children if c.type in ("formal_parameters", "parameters")),
            None,
        )

    if params_node is not None:
        for p in params_node.named_children:
            if p.type in ("simple_parameter", "property_promotion_parameter", "variadic_parameter"):
                ptype = _param_type(p, source)
                if not ptype:
                    continue

                # 1. MapRequestPayload attribute (Symfony)
                if _param_has_attr(p, source, "MapRequestPayload"):
                    request_dto = resolve_type_to_fqcn(ptype, use_map, namespace)
                    break

                # 2. FormRequest parameter (Laravel) or DTO convention (Symfony)
                if _is_laravel_form_request(ptype, form_request_classes):
                    request_dto = resolve_type_to_fqcn(ptype, use_map, namespace)
                    break

                clean = ptype.lstrip("?\\").strip()
                simple = clean.rsplit("\\", 1)[-1]
                if simple not in _GENERIC_FRAMEWORK_TYPES and (
                    simple.endswith(("DTO", "Dto", "Payload", "Input"))
                    or "DTO" in simple
                    or "Dto" in simple
                ):
                    request_dto = resolve_type_to_fqcn(ptype, use_map, namespace)
                    break

    rt = _extract_return_type_from_node(callable_node, source)
    if rt:
        response_dto = resolve_type_to_fqcn(rt, use_map, namespace)

    return request_dto, response_dto
