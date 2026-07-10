"""Decorator-declared route detection for TS controllers: ``@Controller('base')`` /
``@JsonController('base')`` classes whose methods carry ``@Get(':id')`` / ``@Post()`` etc.
Covers both **NestJS** (``@nestjs/common``) and **routing-controllers** — the decorator
grammar is identical, so detection keys on decorator *names*, not the import source; the
caller passes the resolved ``framework`` label. Emits ``semanticType="route"`` statements
parented to their handler method (via the shared id convention, so parentId matches the
base TypeScript parser's function id)."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import disambiguate, function_id, statement_id
from ...schemas import Statement
from ...parsers.typescript.functions import _type_text, decorator, extract_params
from ..treesitter import node_text

_METHOD_DECORATORS = {
    "Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH",
    "Delete": "DELETE", "Options": "OPTIONS", "Head": "HEAD", "All": "ALL",
}
# @nestjs/microservices message consumers → eventbus_consumer semanticType.
_MESSAGING_DECORATORS = {"EventPattern": "EVENT", "MessagePattern": "MESSAGE"}
_RESPONSE_DECORATORS = {"ApiResponse", "ApiOkResponse", "ApiCreatedResponse"}
_TYPE_PROP_RE = re.compile(r"\btype\s*:\s*\[?\s*([A-Za-z_$][\w.$]*)")
# return-type → responseDTO: skip generic wrappers and primitives, take the first
# PascalCase type name (``Promise<OrderDto[]>`` → ``OrderDto``, ``void`` → None).
_ID_RE = re.compile(r"[A-Za-z_$][\w$]*")
_NON_DTO_TYPES = {
    "Promise", "Observable", "Array", "Map", "Set", "Record", "Partial", "Readonly",
    "void", "any", "unknown", "never", "null", "undefined", "string", "number",
    "boolean", "object", "bigint", "symbol", "this", "true", "false",
}


def _dto_from_type(t: str | None) -> str | None:
    if not t:
        return None
    for tok in _ID_RE.findall(t):
        if tok in _NON_DTO_TYPES or not tok[0].isupper():
            continue
        return tok
    return None


def _return_dto(member: Node, source: bytes) -> str | None:
    """Handler return type → responseDTO (fallback when no ``@ApiResponse``)."""
    return _dto_from_type(_type_text(member.child_by_field_name("return_type"), source))
# `@Controller({ path: 'orders', host: '...' })` — pull the string `path` out of the
# object form (the string form `@Controller('orders')` is handled directly).
_PATH_PROP_RE = re.compile(r"""\bpath\s*:\s*['"`]([^'"`]*)['"`]""")


def _unquote(text: str) -> str:
    return text.strip().strip("'\"`")


def _pattern(d) -> str | None:
    """Address/topic of ``@EventPattern('x')`` / ``@MessagePattern({cmd:'y'})``."""
    if not d.args:
        return None
    raw = d.args[0].strip()
    return _unquote(raw) if raw[:1] in "'\"`" else (raw or None)


def _guards(decs: list[Node], source: bytes) -> list[str]:
    """Guard/auth names: ``@UseGuards(...)`` args (NestJS) and ``@Authorized`` (routing-
    controllers). Presence of any drives ``authRequired``."""
    out: list[str] = []
    for dec in decs:
        d = decorator(dec, source)
        if d.name == "UseGuards":
            out.extend(_unquote(a) for a in d.args)
        elif d.name == "Authorized":  # routing-controllers auth decorator
            out.append("Authorized")
    return out


def _response_dto(decs: list[Node], source: bytes) -> str | None:
    """``@ApiResponse({ type: Dto })`` / ApiOkResponse / ApiCreatedResponse → Dto."""
    for dec in decs:
        d = decorator(dec, source)
        if d.name in _RESPONSE_DECORATORS:
            for arg in d.args:
                m = _TYPE_PROP_RE.search(arg)
                if m:
                    return m.group(1)
    return None


def _request_dto(member: Node, source: bytes) -> str | None:
    """Declared type of the ``@Body``-decorated parameter → requestDTO."""
    for p in extract_params(member.child_by_field_name("parameters"), source):
        if any(d.name == "Body" for d in p.decorators):
            return p.type or None
    return None


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


_CONTROLLER_DECORATORS = {"Controller", "JsonController"}  # NestJS + routing-controllers


def _controller_base(decorators: list[Node], source: bytes) -> str | None:
    for dec in decorators:
        d = decorator(dec, source)
        if d.name in _CONTROLLER_DECORATORS:
            if not d.args:
                return ""
            arg = d.args[0].strip()
            if arg.startswith("{"):  # object form: @Controller({ path: 'x', host: ... })
                m = _PATH_PROP_RE.search(arg)
                return m.group(1) if m else ""
            return _unquote(arg)
    return None


def _version(decorators: list[Node], source: bytes) -> str | None:
    """``@Version('2')`` (URI versioning) on a method or the controller → version tag."""
    for dec in decorators:
        d = decorator(dec, source)
        if d.name == "Version" and d.args:
            return _unquote(d.args[0])
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


def detect_nest_routes(
    root: Node, source: bytes, path: str, *, seen_ids: set[str], framework: str = "nestjs"
) -> list[Statement]:
    routes: list[Statement] = []
    for cls, decs in _class_with_decorators(root):
        base = _controller_base(decs, source)  # None when the class is not a @Controller
        is_controller = base is not None
        class_name = node_text(cls.child_by_field_name("name"), source)
        body = cls.child_by_field_name("body")
        if body is None:
            continue
        ctrl_guards = _guards(decs, source) if is_controller else []
        ctrl_version = _version(decs, source) if is_controller else None
        pending: list[Node] = []
        for member in body.named_children:
            if member.type == "decorator":
                pending.append(member)
                continue
            if member.type == "comment":
                continue  # a comment between a route decorator and its handler must not drop it
            if member.type == "method_definition":
                mname = node_text(member.child_by_field_name("name"), source)
                mline = member.start_point[0] + 1
                parent = function_id(path, mname, mline, class_name=class_name)
                guards = ctrl_guards + _guards(pending, source)  # merge controller + method
                for dec in pending:
                    d = decorator(dec, source)
                    verb = _METHOD_DECORATORS.get(d.name) if is_controller else None
                    msg = _MESSAGING_DECORATORS.get(d.name)
                    if verb is None and msg is None:
                        continue
                    sl, sc = dec.start_point[0] + 1, dec.start_point[1]
                    common = dict(
                        id=disambiguate(statement_id(path, sl, sc), seen_ids),
                        parentId=parent,
                        nodeType="synthetic",
                        text=node_text(dec, source).split("\n", 1)[0],
                        framework=framework,
                        handler=mname,
                        handlerLine=mline,
                        isRegex=False,
                        authRequired=bool(guards),
                        guards=guards or None,
                        startLine=sl,
                        endLine=dec.end_point[0] + 1,
                        path=path,
                    )
                    if verb is not None:  # HTTP route
                        routes.append(Statement(
                            semanticType="route",
                            method=verb,
                            endpoint=_join(base, _unquote(d.args[0]) if d.args else ""),
                            routeKind="route",
                            version=_version(pending, source) or ctrl_version,
                            requestDTO=_request_dto(member, source),
                            responseDTO=_response_dto(pending, source) or _return_dto(member, source),
                            **common,
                        ))
                    else:  # @EventPattern / @MessagePattern microservice consumer
                        routes.append(Statement(
                            semanticType="eventbus_consumer",
                            method=msg,
                            endpoint=_pattern(d),
                            routeKind="message",
                            **common,
                        ))
            pending = []
    return routes
