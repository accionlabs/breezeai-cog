"""Symfony route detection (#[Route(...)] attributes) off the FileRecord."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Iterator

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement
from ..php.dto import _GENERIC_FRAMEWORK_TYPES, extract_use_map, resolve_type_to_fqcn
from ..treesitter import node_text

if TYPE_CHECKING:
    from ...schemas import Decorator


def _string_literal(node: Any, source: bytes) -> str | None:
    if node is None:
        return None
    if node.type == "string":
        return node_text(node, source).strip("'\"")
    if node.type == "encapsed_string":
        return node_text(node, source).strip("'\"")
    return None


def _string_literals(node: Any, source: bytes) -> Iterator[str]:
    literal = _string_literal(node, source)
    if literal is not None:
        yield literal
        return
    for child in node.named_children if node is not None else []:
        yield from _string_literals(child, source)


def _parse_route_attr_nodes(
    args: Any, source: bytes | None = None
) -> tuple[str | None, list[str], str | None]:
    """Parse a Route attribute from its argument AST node or string list, preserving literal values verbatim."""
    path = None
    methods: list[str] = []
    name = None

    if args is None:
        return path, methods, name

    if not isinstance(args, list) and source is not None:
        for arg in args.named_children:
            key_node = arg.child_by_field_name("name")
            value_node = arg.child_by_field_name("value")
            if key_node is not None:
                key = node_text(key_node, source)
                if value_node is None and arg.named_children:
                    value_node = arg.named_children[-1]
                if key == "methods" and value_node is not None:
                    methods.extend(literal.upper() for literal in _string_literals(value_node, source))
                elif key == "name" and value_node is not None:
                    name = _string_literal(value_node, source)
                elif key == "path" and value_node is not None:
                    path = _string_literal(value_node, source)
                continue
            value = arg.named_children[0] if arg.named_children else None
            if path is None and value is not None:
                path = _string_literal(value, source)
        return path, methods, name

    str_list = args if isinstance(args, list) else []
    for arg_str in str_list:
        arg_str = arg_str.strip()
        if not arg_str:
            continue
        if ":" in arg_str or "=" in arg_str:
            sep = ":" if ":" in arg_str else "="
            k, v = arg_str.split(sep, 1)
            k = k.strip()
            v = v.strip()
            if k == "methods":
                found = re.findall(r"['\"]([A-Z_]+)['\"]", v, re.IGNORECASE)
                methods.extend(m.upper() for m in found)
            elif k == "name":
                name = v.strip("'\" ")
            elif k == "path":
                path = v.strip("'\" ")
        else:
            if path is None:
                path = arg_str.strip("'\" ")

    return path, methods, name


def _parse_route_decorator(
    dec: Decorator, root: Any, source: bytes | None
) -> tuple[str | None, list[str], str | None]:
    """Parse a Route decorator using its source-matching attribute AST node."""
    if root is not None and source is not None and dec.text:
        found: list[Any] = []

        def walk(node: Any) -> None:
            if node.type == "attribute":
                name_node = node.child_by_field_name("name") or (
                    node.named_children[0] if node.named_children else None
                )
                if (
                    name_node is not None
                    and node_text(name_node, source).rsplit("\\", 1)[-1] == "Route"
                    and dec.text is not None
                    and node_text(node, source) in dec.text
                ):
                    found.append(node)
            for child in node.named_children:
                walk(child)

        walk(root)
        if found:
            args_node = found[0].child_by_field_name("parameters")
            if args_node is None:
                args_node = next(
                    (child for child in found[0].named_children if child.type == "arguments"),
                    None,
                )
            return _parse_route_attr_nodes(args_node, source)
    return _parse_route_attr_nodes(dec.args)


def _combine_paths(prefix: str | None, path: str | None) -> str:
    pfx = (prefix or "").rstrip("/")
    sub = (path or "").lstrip("/")
    if not pfx and not sub:
        return "/"
    if not pfx:
        return f"/{sub}" if not sub.startswith("/") else sub
    return f"{pfx}/{sub}"


def _is_route_decorator(name: str) -> bool:
    return name == "Route"


def _split_annotation_args(raw: str) -> list[str]:
    """Split annotation arguments without splitting commas in arrays or strings."""
    args: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(raw):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            args.append(raw[start:index].strip())
            start = index + 1
    tail = raw[start:].strip()
    if tail:
        args.append(tail)
    return args


def _docblock_route_specs(
    fn: Any, root: Any, source: bytes
) -> list[tuple[tuple[str | None, list[str], str | None], str]]:
    """Extract method-level ``@Route(...)`` annotations from the preceding PHPDoc block."""
    line_starts = [0]
    for match in re.finditer(b"\n", source):
        line_starts.append(match.end())
    if fn.startLine < 1 or fn.startLine > len(line_starts):
        return []
    method_start = line_starts[fn.startLine - 1]
    comments: list[Any] = []

    def walk(node: Any) -> None:
        if node.type == "comment" and node.end_byte <= method_start:
            text = node.text.decode("utf-8", "replace")
            if text.lstrip().startswith("/**") and re.search(
                r"@Route\s*\(", text, re.IGNORECASE
            ):
                comments.append(node)
        for child in node.named_children:
            walk(child)

    walk(root)
    preceding = [
        node for node in comments
        if source[node.end_byte:method_start].decode("utf-8", "replace").strip() == ""
    ]
    if not preceding:
        return []
    doc = max(preceding, key=lambda node: node.end_byte)
    text = doc.text.decode("utf-8", "replace")
    return [
        (_parse_route_attr_nodes(_split_annotation_args(match.group(1))), match.group(0))
        for match in re.finditer(r"@Route\s*\((.*?)\)", text, re.IGNORECASE | re.DOTALL)
    ]


def _function_guards(fn: Any) -> list[str] | None:
    guards: list[str] = []
    for dec in fn.decorators:
        if dec.name == "IsGranted" and dec.args:
            guard = dec.args[0].strip("'\"")
            if guard and guard not in guards:
                guards.append(guard)
    return guards or None


def _symfony_request_dto(
    fn: Any, use_map: dict[str, str], namespace: str, fqcn_index: dict[str, str | None] | None
) -> str | None:
    # 1. Parameter with #[MapRequestPayload] attribute
    for p in fn.params:
        if any(
            d.name == "MapRequestPayload" or d.name.endswith("\\MapRequestPayload")
            for d in p.decorators
        ):
            if p.type:
                return resolve_type_to_fqcn(p.type, use_map, namespace, fqcn_index=fqcn_index)

    # 2. FormRequest-equivalent convention: custom DTO/Request type
    for p in fn.params:
        if not p.type:
            continue
        clean = p.type.lstrip("?\\").strip()
        simple = clean.rsplit("\\", 1)[-1]
        if simple in _GENERIC_FRAMEWORK_TYPES:
            continue
        if (
            simple.endswith(("Request", "DTO", "Dto", "Payload", "Input"))
            or "DTO" in simple
            or "Dto" in simple
        ):
            return resolve_type_to_fqcn(p.type, use_map, namespace, fqcn_index=fqcn_index)

    return None


def _symfony_response_dto(
    fn: Any, use_map: dict[str, str], namespace: str, fqcn_index: dict[str, str | None] | None
) -> str | None:
    if fn.returnType:
        resolved = resolve_type_to_fqcn(fn.returnType, use_map, namespace, fqcn_index=fqcn_index)
        if resolved:
            simple = resolved.rsplit("\\", 1)[-1]
            if simple not in _GENERIC_FRAMEWORK_TYPES:
                return resolved
    return None


def detect_symfony_routes(
    record: FileRecord,
    seen_ids: set[str],
    root: Any | None = None,
    source: bytes | None = None,
    fqcn_index: dict[str, str | None] | None = None,
) -> list[Statement]:
    """Detect Symfony #[Route(...)] attributes on classes and methods (off the record)."""
    routes: list[Statement] = []

    if root is not None and source is not None:
        namespace, use_map = extract_use_map(root, source)
    else:
        use_map = {imp.rsplit("\\", 1)[-1]: imp for imp in record.externalImports}
        namespace = ""
        if record.classes:
            first_cls = record.classes[0].name
            if "\\" in first_cls:
                namespace = first_cls.rsplit("\\", 1)[0]

    # Map class id -> class prefix
    class_prefixes: dict[str, str] = {}
    class_map = {c.id: c for c in record.classes}
    for cls in record.classes:
        for dec in cls.decorators:
            if _is_route_decorator(dec.name):
                cpath, _, _ = _parse_route_decorator(dec, root, source)
                if cpath:
                    class_prefixes[cls.id] = cpath

    for fn in record.functions:
        route_specs: list[tuple[tuple[str | None, list[str], str | None], str]] = [
            (
                _parse_route_decorator(dec, root, source),
                dec.text or "#[Route]",
            )
            for dec in fn.decorators
            if _is_route_decorator(dec.name)
        ]
        if root is not None and source is not None:
            route_specs.extend(_docblock_route_specs(fn, root, source))
        for (mpath, methods, rname), route_text in route_specs:
            cls_prefix = class_prefixes.get(fn.parentId)
            full_path = _combine_paths(cls_prefix, mpath)
            verbs = methods if methods else ["ANY"]
            guards = _function_guards(fn)
            request_dto = _symfony_request_dto(fn, use_map, namespace, fqcn_index)
            response_dto = _symfony_response_dto(fn, use_map, namespace, fqcn_index)

            cls_name = class_map[fn.parentId].name if fn.parentId in class_map else ""
            handler = f"{cls_name}@{fn.name}" if cls_name else fn.name

            for verb in verbs:
                sid = disambiguate(statement_id(record.path, fn.startLine, 0), seen_ids)
                routes.append(
                    Statement(
                        id=sid,
                        parentId=fn.id,
                        nodeType="synthetic",
                        semanticType="route",
                        routeKind="route",
                        method=verb,
                        endpoint=full_path,
                        handler=handler,
                        name=rname,
                        text=route_text or f"#[Route('{full_path}')]",
                        startLine=fn.startLine,
                        endLine=fn.endLine,
                        path=record.path,
                        framework="symfony",
                        guards=guards,
                        requestDTO=request_dto,
                        responseDTO=response_dto,
                    )
                )
        # ``Route`` attributes are fully represented by the emitted route statements.
        # Preserve non-routing PHP attributes on the function.
        fn.decorators = [dec for dec in fn.decorators if not _is_route_decorator(dec.name)]

    return routes
