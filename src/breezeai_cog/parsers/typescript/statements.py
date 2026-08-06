"""Flat statement capture for TypeScript/JavaScript (gated by --capture-statements),
with shared API/DB call detection (``parsers/detection``)."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import (
    classify_statement,
    render_concat,
    resolve_endpoint,
    strip_leading_base,
    url_placeholder,
)
from ..treesitter import node_text
from .imports import _imported_names, _module_of
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "call_expression"

# TypeScript ORM/DB client type names that positively identify a DI field as a database
# client. When a class constructor declares ``private repo: Repository<User>``, ``repo``
# is added to the typed_db_ids set and HIGH_COLLISION verbs on it are trusted as ORM calls.
# This prevents ``stateManager.find()`` / ``itemsCollection.create()`` false positives.
_TS_ORM_TYPES = frozenset({
    "Repository", "DataSource", "EntityManager", "Connection", "QueryRunner",
    "MongoRepository", "TreeRepository",  # TypeORM
    "Model",  # Mongoose
})


def _field_annotation_type(node: Node, source: bytes) -> str | None:
    """Base type name from a ``: Type<…>`` / ``: Type`` annotation on a node, else None."""
    ann = next((c for c in node.named_children if c.type == "type_annotation"), None)
    if ann is None:
        return None
    inner = ann.named_children[0] if ann.named_children else None
    if inner is None:
        return None
    if inner.type == "generic_type":
        name_node = inner.child_by_field_name("name") or (
            inner.named_children[0] if inner.named_children else None
        )
        return node_text(name_node, source) if name_node is not None else None
    return node_text(inner, source) if inner.type in ("type_identifier", "identifier") else None


def _has_inject_repository_decorator(param: Node, source: bytes) -> bool:
    """True when ``param`` carries an ``@InjectRepository(…)`` decorator — the NestJS/TypeORM
    DI marker. This is a reliable signal that the parameter is a TypeORM Repository even when
    the type annotation is missing or uses a custom subclass not in ``_TS_ORM_TYPES``."""
    for child in param.named_children:
        if child.type == "decorator":
            inner = child.named_children[0] if child.named_children else None
            if inner is not None and inner.type == "call_expression":
                fn = inner.child_by_field_name("function")
                if fn is not None and node_text(fn, source) == "InjectRepository":
                    return True
    return False


def collect_typed_db_receivers(class_body: Node, source: bytes) -> frozenset[str]:
    """Return the set of field/parameter names declared with a known ORM client type.

    Scans the class body for:
    * Constructor parameters typed as an ORM type (Angular/NestJS DI:
      ``constructor(private userRepo: Repository<User>)`` → ``{"userRepo"}``)
    * Constructor parameters with an ``@InjectRepository(…)`` decorator (fallback when
      the type annotation is missing or uses a custom Repository subclass)
    * Class field declarations typed as an ORM type (``userRepo: Repository<User>``)

    The resulting set is passed to ``match_db`` as ``typed_db_ids`` so that
    HIGH_COLLISION verbs (``find``/``create``/``save``/…) are only tagged as
    ``db_method_call`` when the receiver is a known-typed ORM field, not just any
    receiver whose *name* ends in "manager"/"model"/"collection"."""
    ids: set[str] = set()
    for node in class_body.named_children:
        if node.type == "method_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None and node_text(name_node, source) == "constructor":
                params = node.child_by_field_name("parameters")
                if params is not None:
                    for p in params.named_children:
                        if p.type != "required_parameter":
                            continue
                        if _field_annotation_type(p, source) in _TS_ORM_TYPES or \
                           _has_inject_repository_decorator(p, source):
                            name = p.child_by_field_name("pattern") or next(
                                (c for c in p.named_children if c.type == "identifier"), None
                            )
                            if name is not None:
                                ids.add(node_text(name, source))
        elif node.type in ("public_field_definition", "property_declaration", "field_definition"):
            if _field_annotation_type(node, source) in _TS_ORM_TYPES:
                name = node.child_by_field_name("name")
                if name is not None:
                    ids.add(node_text(name, source))
    return frozenset(ids)


# --- Wrapped HTTP-client detection (per-file) ---------------------------------------------
# The shared api-call classifier recognises a client by a substring hint in the callee
# (``axios``/``http``/…). A WRAPPED client's name is arbitrary, so it slips past that test.
# Two forms cover almost all Vue/React service layers; both are collected into a per-file set
# of client names, which classify_statement passes to match_api:
#   1. an in-file axios instance:      const service = axios.create({...})   → ``service``
#   2. a config-object wrapper call:   request({ url: '/x', method: 'post' }) → ``request``
# Form 2 is caught by shape (a ``{url, method}`` argument), so an imported wrapper needs no
# cross-file resolution. ``axios.create`` is an unambiguous client factory, keeping form 1
# precise; form 2's two-key co-occurrence is HTTP-request-config-specific.
_URL_KEYS = frozenset({"url", "uri", "path"})
_HTTP_VERB_LITERALS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def _string_fragment(node: Node, source: bytes) -> str:
    frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
    return node_text(frag, source) if frag is not None else ""


def _is_http_config_object(obj: Node, source: bytes) -> bool:
    """True if an object literal is an HTTP request config — carries both a URL-family key
    and a ``method`` key. A ``method`` given as a string literal must be an HTTP verb (a
    non-verb literal rules it out); a non-literal ``method`` (a variable) is allowed."""
    has_url = has_method = False
    for pair in obj.named_children:
        if pair.type != "pair":
            continue
        key = pair.child_by_field_name("key")
        if key is None:
            continue
        kname = node_text(key, source).strip("'\"")
        if kname in _URL_KEYS:
            has_url = True
        elif kname == "method":
            val = pair.child_by_field_name("value")
            if (
                val is not None
                and val.type == "string"
                and _string_fragment(val, source).lower() not in _HTTP_VERB_LITERALS
            ):
                return False
            has_method = True
    return has_url and has_method


def collect_http_client_ids(root: Node, source: bytes) -> frozenset[str]:
    """Per-file set of names that are HTTP clients but carry no callee hint (see the note
    above). Byte-guarded so files with neither an axios instance nor a config-object call are
    skipped without a walk."""
    if b"axios" not in source and b"method" not in source:
        return frozenset()
    axios_names = {"axios", "Axios"}
    for node in root.named_children:
        if node.type == "import_statement" and _module_of(node, source) == "axios":
            axios_names.update(_imported_names(node, source))
    ids: set[str] = set()
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "variable_declarator":  # form 1: const X = <axios>.create(...)
            name = n.child_by_field_name("name")
            value = n.child_by_field_name("value")
            if (name is not None and name.type == "identifier"
                    and value is not None and value.type == "call_expression"):
                fn = value.child_by_field_name("function")
                if fn is not None and fn.type == "member_expression":
                    obj = fn.child_by_field_name("object")
                    prop = fn.child_by_field_name("property")
                    if (prop is not None and node_text(prop, source) == "create"
                            and obj is not None and node_text(obj, source) in axios_names):
                        ids.add(node_text(name, source))
        elif n.type == "call_expression":  # form 2: NAME({ url, method })
            fn = n.child_by_field_name("function")
            args = n.child_by_field_name("arguments")
            if fn is not None and args is not None:
                first = next(iter(args.named_children), None)
                if (
                    first is not None
                    and first.type == "object"
                    and _is_http_config_object(first, source)
                ):
                    recv = node_text(fn, source).split(".", 1)[0]
                    if recv.replace("_", "").replace("$", "").isalnum():
                        ids.add(recv)
        stack.extend(n.named_children)
    return frozenset(ids)


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type == "lexical_declaration":
        decl = next((c for c in node.named_children if c.type == "variable_declarator"), None)
        if decl is not None:
            name = decl.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                return node_text(name, source)
    elif node.type in ("type_alias_declaration", "public_field_definition", "field_definition"):
        # `type X = …` -> X ;  class field `count = 0` -> count
        name = node.child_by_field_name("name")
        if name is not None:
            return node_text(name, source)
    return None


def _render_url(node: Node, source: bytes) -> str | None:
    """Best-effort URL/path from a string, template literal, or ``+`` concatenation.
    Interpolations become ``{name}`` placeholders; a leading interpolated base/host
    segment is dropped (``\\`${base}/users/${id}\\``` -> ``/users/{id}``)."""
    if node.type == "string":
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return node_text(frag, source) if frag is not None else ""
    if node.type == "template_string":
        parts: list[str] = []
        for c in node.named_children:
            if c.type == "string_fragment":
                parts.append(node_text(c, source))
            elif c.type == "template_substitution":
                expr = c.named_children[0] if c.named_children else None
                parts.append(url_placeholder(node_text(expr, source)) if expr is not None else "{param}")
        return strip_leading_base("".join(parts))
    if node.type == "binary_expression":  # string concatenation: '/a/' + id + '/b'
        return render_concat(node, source, _render_url)
    return None


def _resolve_args(args: Node | None, source: bytes) -> tuple[str | None, str | None]:
    """(endpoint, override_method) from a call's arguments. Handles the config-object
    form (``axios({ url, method })``) — JS-specific — then falls back to the shared
    positional resolver (first-arg / verb-first)."""
    if args is None:
        return None, None
    named = list(args.named_children)
    if not named:
        return None, None
    if named[0].type == "object":  # axios({ url: '/x', method: 'get' })
        url = override = None
        for pair in named[0].named_children:
            if pair.type != "pair":
                continue
            key = pair.child_by_field_name("key")
            val = pair.child_by_field_name("value")
            kname = node_text(key, source) if key is not None else ""
            if kname in ("url", "uri", "path") and val is not None:
                url = _render_url(val, source)
            elif kname == "method" and val is not None:
                mv = _render_url(val, source)
                override = mv.lower() if mv else None
        return url, override
    return resolve_endpoint(named, source, _render_url)


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    fn = call.child_by_field_name("function")
    callee = node_text(fn, source) if fn is not None else ""
    method = callee.rsplit(".", 1)[-1]
    endpoint, override = _resolve_args(call.child_by_field_name("arguments"), source)
    if override is not None:
        method = override
    return callee, method, endpoint


def _span(node: Node) -> tuple[int, int]:
    return (node.start_byte, node.end_byte)


def _iter_in_scope(node: Node, descend_all: bool = False, barriers: frozenset[tuple[int, int]] = frozenset()):
    """Yield EMIT_TYPES statement nodes. When ``descend_all`` is False (file-root /
    class-body scope) nested scopes remain barriers — they are extracted as their own
    Function/Class. When True (a function body) we descend into inline callbacks and
    lambdas, attributing their statements to this function, EXCEPT nested named
    functions (their spans are in ``barriers``): those are extracted as their own
    scope, so descending would double-emit. This closes the "callback black hole"
    while keeping one-statement-per-nearest-named-function (see build_function)."""
    for child in node.named_children:
        if _span(child) in barriers:
            continue
        if not descend_all and child.type in NESTED_SCOPES:
            continue
        if child.type in EMIT_TYPES:
            yield child
        yield from _iter_in_scope(child, descend_all, barriers)


def extract_statements(
    body: Node | None,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    capture: bool,
    limit: int,
    seen_ids: set[str],
    descend_all: bool = False,
    barriers: frozenset[tuple[int, int]] = frozenset(),
    typed_db_ids: frozenset[str] | None = None,
) -> list[Statement]:
    if not capture or body is None:
        return []
    out: list[Statement] = []
    for node in _iter_in_scope(body, descend_all, barriers):
        out.extend(
            classify_statement(
                node, source, path, parent_id=parent_id, limit=limit, seen_ids=seen_ids,
                emit_types=EMIT_TYPES, control_flow=CONTROL_FLOW, call_type=_CALL_TYPE,
                name_of=_name_of, call_details=_call_details, language="typescript",
                typed_db_ids=typed_db_ids,
            )
        )
    return out
