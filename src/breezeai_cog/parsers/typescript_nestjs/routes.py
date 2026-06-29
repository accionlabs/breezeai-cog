"""NestJS route detection: ``@Controller('base')`` classes whose methods carry
``@Get(':id')`` / ``@Post()`` etc. Emits ``semanticType="route"`` statements parented
to their handler method (via the shared id convention, so parentId matches the base
TypeScript parser's function id)."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id, statement_id
from ...schemas import Statement
from ...parsers.typescript.functions import decorator
from ..treesitter import node_text

_METHOD_DECORATORS = {
    "Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH",
    "Delete": "DELETE", "Options": "OPTIONS", "Head": "HEAD", "All": "ALL",
}


def _unquote(text: str) -> str:
    return text.strip().strip("'\"`")


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _controller_base(decorators: list[Node], source: bytes) -> str | None:
    for dec in decorators:
        d = decorator(dec, source)
        if d.name == "Controller":
            return _unquote(d.args[0]) if d.args else ""
    return None


def _class_with_decorators(root: Node):
    """Yield (class_declaration, decorator_nodes) for top-level classes."""
    pending: list[Node] = []
    for child in root.named_children:
        if child.type == "decorator":
            pending.append(child)
            continue
        decs, cls = list(pending), None
        pending = []
        if child.type == "export_statement":
            decs += [c for c in child.named_children if c.type == "decorator"]
            cls = next((c for c in child.named_children if c.type == "class_declaration"), None)
        elif child.type == "class_declaration":
            cls = child
        if cls is not None:
            yield cls, decs


def detect_nest_routes(root: Node, source: bytes, path: str, *, seen_ids: set[str]) -> list[Statement]:
    routes: list[Statement] = []
    for cls, decs in _class_with_decorators(root):
        base = _controller_base(decs, source)
        if base is None:
            continue  # not a @Controller
        class_name = node_text(cls.child_by_field_name("name"), source)
        body = cls.child_by_field_name("body")
        if body is None:
            continue
        pending: list[Node] = []
        for member in body.named_children:
            if member.type == "decorator":
                pending.append(member)
                continue
            if member.type == "method_definition":
                mname = node_text(member.child_by_field_name("name"), source)
                mline = member.start_point[0] + 1
                for dec in pending:
                    d = decorator(dec, source)
                    verb = _METHOD_DECORATORS.get(d.name)
                    if verb is None:
                        continue
                    sl, sc = dec.start_point[0] + 1, dec.start_point[1]
                    routes.append(Statement(
                        id=disambiguate(statement_id(path, sl, sc), seen_ids),
                        parentId=function_id(path, mname, mline, class_name=class_name),
                        nodeType="decorator",
                        semanticType="route",
                        text=node_text(dec, source).split("\n", 1)[0],
                        method=verb,
                        endpoint=_join(base, _unquote(d.args[0]) if d.args else ""),
                        framework="nestjs",
                        handler=mname,
                        handlerLine=mline,
                        routeKind="route",
                        startLine=sl,
                        endLine=dec.end_point[0] + 1,
                        path=path,
                    ))
            pending = []
    return routes
