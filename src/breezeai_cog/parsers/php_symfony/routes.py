"""Symfony route detection (#[Route(...)] attributes) off the FileRecord."""

from __future__ import annotations

import re

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement


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


def detect_symfony_routes(
    record: FileRecord,
    seen_ids: set[str],
) -> list[Statement]:
    """Detect Symfony #[Route(...)] attributes on classes and methods (off the record)."""
    routes: list[Statement] = []

    # Map class id -> class prefix
    class_prefixes: dict[str, str] = {}
    class_map = {c.id: c for c in record.classes}
    for cls in record.classes:
        for dec in cls.decorators:
            if dec.name == "Route" or dec.name.endswith("\\Route"):
                cpath, _, _ = _parse_route_attr(dec.args)
                if cpath:
                    class_prefixes[cls.id] = cpath

    for fn in record.functions:
        for dec in fn.decorators:
            if dec.name == "Route" or dec.name.endswith("\\Route"):
                mpath, methods, rname = _parse_route_attr(dec.args)
                cls_prefix = class_prefixes.get(fn.parentId)
                full_path = _combine_paths(cls_prefix, mpath)
                verbs = methods if methods else ["GET"]

                cls_name = class_map[fn.parentId].name if fn.parentId in class_map else ""
                handler = f"{cls_name}@{fn.name}" if cls_name else fn.name

                for verb in verbs:
                    sid = disambiguate(statement_id(record.path, fn.startLine, 0), seen_ids)
                    routes.append(
                        Statement(
                            id=sid,
                            parentId=fn.id,
                            nodeType="attribute",
                            semanticType="route",
                            method=verb,
                            endpoint=full_path,
                            handler=handler,
                            name=rname,
                            text=f"#[Route('{full_path}')]",
                            startLine=fn.startLine,
                            endLine=fn.endLine,
                            path=record.path,
                            framework="symfony",
                        )
                    )

    return routes
