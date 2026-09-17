"""Symfony route detection (#[Route(...)] attributes) off the FileRecord."""

from __future__ import annotations

import re

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement
from ..php.dto import _GENERIC_FRAMEWORK_TYPES, extract_use_map, resolve_type_to_fqcn


def _parse_route_attr(args: list[str]) -> tuple[str | None, list[str], str | None]:
    """Parse route path, methods list, and route name from attribute arguments."""
    path = None
    methods: list[str] = []
    name = None

    for arg in args:
        arg_str = arg.strip()
        if arg_str.startswith(("methods:", "methods=")):
            # Parse array of methods: methods: ['GET', 'POST'] or methods: 'GET'
            raw = arg_str.split(":", 1)[-1] if ":" in arg_str else arg_str.split("=", 1)[-1]
            found = re.findall(r"['\"]([A-Z_]+)['\"]", raw, re.IGNORECASE)
            methods.extend(m.upper() for m in found)
        elif arg_str.startswith(("name:", "name=")):
            raw = arg_str.split(":", 1)[-1] if ":" in arg_str else arg_str.split("=", 1)[-1]
            name = raw.strip("'\" ")
        elif not arg_str.startswith(
            ("requirements:", "defaults:", "options:", "schemes:", "host:", "condition:")
        ):
            if path is None:
                # Positional path argument or path: '...'
                raw = (
                    arg_str.split(":", 1)[-1]
                    if ":" in arg_str and not arg_str.startswith("/")
                    else arg_str
                )
                path = raw.strip("'\" ")

    return path, methods, name


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


def _docblock_route_specs(fn, root, source: bytes) -> list[tuple[list[str], str]]:
    """Extract method-level ``@Route(...)`` annotations from the preceding PHPDoc block."""
    line_starts = [0]
    for match in re.finditer(b"\n", source):
        line_starts.append(match.end())
    if fn.startLine < 1 or fn.startLine > len(line_starts):
        return []
    method_start = line_starts[fn.startLine - 1]
    comments = []

    def walk(node) -> None:
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
        (_split_annotation_args(match.group(1)), match.group(0))
        for match in re.finditer(r"@Route\s*\((.*?)\)", text, re.IGNORECASE | re.DOTALL)
    ]


def _function_guards(fn) -> list[str] | None:
    guards: list[str] = []
    for dec in fn.decorators:
        if dec.name == "IsGranted" and dec.args:
            guard = dec.args[0].strip("'\"")
            if guard and guard not in guards:
                guards.append(guard)
    return guards or None


def _symfony_request_dto(fn, use_map: dict[str, str], namespace: str) -> str | None:
    # 1. Parameter with #[MapRequestPayload] attribute
    for p in fn.params:
        if any(
            d.name == "MapRequestPayload" or d.name.endswith("\\MapRequestPayload")
            for d in p.decorators
        ):
            if p.type:
                return resolve_type_to_fqcn(p.type, use_map, namespace)

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
            return resolve_type_to_fqcn(p.type, use_map, namespace)

    return None


def _symfony_response_dto(fn, use_map: dict[str, str], namespace: str) -> str | None:
    if fn.returnType:
        return resolve_type_to_fqcn(fn.returnType, use_map, namespace)
    return None


def detect_symfony_routes(
    record: FileRecord,
    seen_ids: set[str],
    root: Any | None = None,
    source: bytes | None = None,
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
                cpath, _, _ = _parse_route_attr(dec.args)
                if cpath:
                    class_prefixes[cls.id] = cpath

    for fn in record.functions:
        route_specs = [
            (
                dec.args,
                dec.text or f"#[Route('{_parse_route_attr(dec.args)[0] or ''}')]",
            )
            for dec in fn.decorators
            if _is_route_decorator(dec.name)
        ]
        if root is not None and source is not None:
            route_specs.extend(_docblock_route_specs(fn, root, source))
        for route_args, route_text in route_specs:
            mpath, methods, rname = _parse_route_attr(route_args)
            cls_prefix = class_prefixes.get(fn.parentId)
            full_path = _combine_paths(cls_prefix, mpath)
            verbs = methods if methods else ["ANY"]
            guards = _function_guards(fn)
            request_dto = _symfony_request_dto(fn, use_map, namespace)
            response_dto = _symfony_response_dto(fn, use_map, namespace)

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
