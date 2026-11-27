"""Groovy HTTP calls made through the application's own helpers are recorded where they run
(Java: ``java_http_helpers.py``, Python: ``python_http_helpers.py``).

An HTTP client built with its URL makes the request where its verb is called. When the
application wraps that in its own method, the request is made on the method's parameter and
says nothing of where it goes; the method's callers hold the path::

    static Map getJson(String path) {
      String uri = BASE_URL + path
      return new OrderApiClient(uri).get()            // plain statement
    }
    OrderApi.getJson("/${COLLECTION}/${id}")        // api_call GET ${BASE_URL}/{COLLECTION}/{id}

Contract §5, "Where an outbound record is written": each call of a helper is the ``api_call``,
the caller's URL argument in the helper's URL template; the helper's own request on its URL
parameter is a plain statement.

**Helper** — a method of the repository whose request is a verb called on a new HTTP client
(``new T(url).get()``, or a local holding one, ``T client = new T(url); client.put(body)``; ``T``
named as the shared classifier names a client) and whose client takes its URL from one of the
method's parameters, whole or as its last part — written in place or through a local assigned
once (``String uri = BASE_URL + path``). The verb is the one called. A method whose URL only takes
a base or an id from a parameter names its endpoint itself and keeps its record; one whose verb
call also names a URL (``client.get(path: '/x')``) is no helper.

**Call site** — a call the Groovy call resolver links (``calls[].path``) to a helper's file under
the helper's name; a name two methods of one file share is not followed. The URL argument is
rendered as written, or through a local of the caller assigned once.

Not followed: a URL parameter rewritten in the method, two parameters in the URL, a method that
passes its own parameter on to a helper (a relay: neither it nor its callers get a record), a
client held in a field or a parameter.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..emit import statement_id
from ..schemas import FileRecord, Function
from .addresses import runtime_name
from .additive import DetectContext, Detection, Detector, FactSpec, Gate, index_fact, \
    register_detector, register_fact
from .emits import Emit
from .groovy.functions import _new_client, class_fields, client_locals, declarations
from .groovy.statements import _render_url
from .index_common import record_distinct
from .statements_common import url_or_value
from .treesitter import node_text

FACT = "groovy.http-helpers"
_VERBS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})
_URL_KEYS = frozenset({"url", "uri", "path"})
_DECLARING = frozenset({"local_variable_declaration", "formal_parameter",
                        "catch_formal_parameter", "closure_parameter", "for_in_statement"})

#: A helper: (URL parameter index, URL template, its slot, verb).
Helper = tuple[int, str, str, str]


def _methods(root: Node) -> Iterator[tuple[Node, Node | None]]:
    """Each method of the file with the class body it is in (None for a script's top-level
    method). Methods of a class declared inside a method are left out."""
    stack: list[tuple[Node, Node | None]] = [(root, None)]
    while stack:
        n, body = stack.pop()
        for c in n.named_children:
            if c.type == "method_declaration":
                yield c, body
            elif c.type == "class_body":
                stack.append((c, c))
            elif c.type not in ("block", "closure"):
                stack.append((c, body))


def _params(fn: Node, source: bytes) -> list[str]:
    ps = fn.child_by_field_name("parameters")
    out: list[str] = []
    for p in ps.named_children if ps is not None else ():
        names = declarations(p, source)
        out.append(names[0][0] if names else "")
    return out


def _scope(fn: Node, source: bytes) -> tuple[dict[str, Node | None], set[str]]:
    """The method's locals assigned once at their declaration (name → value; None when declared
    more than once, declared without a value, or another way), and every name assigned again."""
    values: dict[str, Node | None] = {}
    assigned: set[str] = set()
    body = fn.child_by_field_name("body")
    stack = [body] if body is not None else []
    while stack:
        n = stack.pop()
        stack.extend(n.named_children)
        if n.type == "assignment_expression":
            left = n.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                assigned.add(node_text(left, source))
        elif n.type in _DECLARING:
            for name, _ in declarations(n, source):
                value = _declared_value(n, name, source) if n.type == \
                    "local_variable_declaration" else None
                if name in values:
                    values[name] = None
                else:
                    values[name] = value
    return values, assigned


def _declared_value(decl: Node, name: str, source: bytes) -> Node | None:
    """The value a local declaration gives ``name``: ``def x = v`` (in a ``variable_declarator``)
    or a typed ``T x = v`` (held directly)."""
    for d in decl.named_children:
        if d.type == "variable_declarator":
            nm = d.child_by_field_name("name")
            if nm is not None and node_text(nm, source) == name:
                return d.child_by_field_name("value")
    names = decl.children_by_field_name("name")
    if len(names) == 1 and node_text(names[0], source) == name:
        return decl.child_by_field_name("value")
    return None


def _reads(node: Node, names: list[str], source: bytes) -> list[str]:
    out: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier" and node_text(n, source) in names:
            out.append(node_text(n, source))
        stack.extend(n.named_children)
    return out


def _requests(fn: Node, body: Node | None, values: dict[str, Node | None], source: bytes
              ) -> Iterator[tuple[Node, Node, str]]:
    """The method's requests on a new client: (verb call, the client's URL argument, verb).
    ``values``: the method's locals (:func:`_scope`, built once by the caller)."""
    fields = class_fields(body, source) if body is not None else {}
    clients = client_locals(fn, source, fields)
    block = fn.child_by_field_name("body")
    stack = [block] if block is not None else []
    while stack:
        n = stack.pop()
        stack.extend(n.named_children)
        if n.type != "method_invocation":
            continue
        member = n.child_by_field_name("function")
        if member is None or member.type != "field_access":
            continue
        field = member.child_by_field_name("field")
        verb = node_text(field, source).upper() if field is not None else ""
        if verb not in _VERBS:
            continue
        obj = member.child_by_field_name("object")
        ctor: Node | None = None
        if obj is not None and _new_client(obj, source):
            ctor = obj
        elif obj is not None and obj.type == "identifier" and node_text(obj, source) in clients:
            ctor = values.get(node_text(obj, source))
        if ctor is None or ctor.type != "object_creation_expression":
            continue
        args = n.child_by_field_name("arguments")
        keys = [a.child_by_field_name("key") for a in (args.named_children if args else ())
                if a.type == "named_argument"]
        if any(k is not None and node_text(k, source).strip("'\"") in _URL_KEYS for k in keys):
            continue  # the verb call names a URL too: the client's is only its base
        cargs = ctor.child_by_field_name("arguments")
        positional = [a for a in (cargs.named_children if cargs is not None else ())
                      if a.type != "named_argument"]
        if positional:
            yield n, positional[0], verb


def _scan(root: Node, source: bytes) -> list[tuple[str, Helper, Node]]:
    """This file's helpers: (method name, helper, its request's verb call)."""
    found: list[tuple[str, Helper, Node]] = []
    for fn, body in _methods(root):
        name_node = fn.child_by_field_name("name")
        if name_node is None or fn.child_by_field_name("body") is None:
            continue
        params = _params(fn, source)
        if not params:
            continue
        values, assigned = _scope(fn, source)
        for call, url, verb in _requests(fn, body, values, source):
            expr: Node | None = url
            if url.type == "identifier" and node_text(url, source) not in params:
                local = node_text(url, source)
                expr = values.get(local) if local not in assigned else None
            if expr is None:
                continue
            refs = _reads(expr, params, source)
            if len(refs) != 1 or refs[0] in assigned or refs[0] in values:
                continue  # no parameter, two, or one rewritten / shadowed in the method
            slot = "{" + refs[0] + "}"
            template = url_or_value(expr, source, _render_url) or ""
            if template.count(slot) != 1 or not template.endswith(slot):
                continue  # a base or an id from the parameter: the method names its endpoint
            found.append((node_text(name_node, source),
                          (params.index(refs[0]), template, slot, verb), call))
    return found


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None
             ) -> tuple[str, list[tuple[str, Helper]]] | None:
    helpers = [(name, helper) for name, helper, _ in _scan(root, source)]
    return (rel, helpers) if helpers else None


def _resolve(values: list[tuple[str, list[tuple[str, Helper]]]], index: Any
             ) -> dict[tuple[str, str], Helper] | None:
    """(file, method name) → helper; a name declared twice in one file with different helpers
    (or as a helper and not) is dropped: which one a call means is not known."""
    merged: dict[tuple[str, str], Helper | None] = {}
    for rel, helpers in values:
        for name, helper in helpers:
            record_distinct(merged, (rel, name), helper)
    out = {k: v for k, v in merged.items() if v is not None}
    return out or None


register_fact(FactSpec(name=FACT, language="groovy", gate=(b"new ",), collect=_collect,
                       resolve=_resolve))


def _enclosing_function(record: FileRecord, line: int) -> Function | None:
    best = None
    for fn in record.functions:
        start, end = fn.startLine or 0, fn.endLine or 0
        if start <= line <= end and (best is None or end - start < (best.endLine or 0)
                                     - (best.startLine or 0)):
            best = fn
    return best


def _enclosing_method(node: Node) -> Node | None:
    cur = node.parent
    while cur is not None and cur.type != "method_declaration":
        if cur.type == "class_body":
            return None
        cur = cur.parent
    return cur


def _statement_node(call: Node, path: str, ids: dict[str, Any]) -> Node | None:
    node: Node | None = call
    while node is not None:
        stmt = ids.get(statement_id(path, node.start_point[0] + 1, node.start_point[1]))
        if stmt is not None and stmt.nodeType == node.type:
            return node
        node = node.parent
    return None


def _overloaded(root: Node, source: bytes) -> set[str]:
    """Method names declared more than once in the file (overloads): a call of one may be
    another's."""
    seen: set[str] = set()
    twice: set[str] = set()
    for fn, _ in _methods(root):
        nm = fn.child_by_field_name("name")
        name = node_text(nm, source) if nm is not None else ""
        (twice if name in seen else seen).add(name)
    return twice


def _detect(dc: DetectContext) -> Detection | None:
    helpers: dict[tuple[str, str], Helper] = index_fact(dc.ctx.resolution_index, FACT) or {}
    if not helpers:
        return None
    source, path = dc.source, dc.path
    found: list[Emit] = []
    if any(rel == path for rel, _ in helpers):
        ids = {s.id: s for s in dc.record.statements if s.semanticType == "api_call"}
        twice = _overloaded(dc.root, source)
        for name, _, call in _scan(dc.root, source):
            if (path, name) in helpers and name not in twice:
                node = _statement_node(call, path, ids)
                if node is not None:
                    found.append(Emit(node, {"semanticType": None, "method": None,
                                             "endpoint": None, "framework": None},
                                      enrich="same"))
    names = {name for _, name in helpers}
    if any(name.encode() in source for name in names):
        # each method's parameters and locals, built once on first use (§11: one walk per method)
        scopes: dict[tuple[int, int], tuple[list[str], dict[str, Node | None], set[str]]] = {}
        stack = [dc.root]
        while stack:
            n = stack.pop()
            stack.extend(n.named_children)
            if n.type != "method_invocation":
                continue
            fn = n.child_by_field_name("function")
            if fn is None:
                continue
            if fn.type == "identifier":
                name, receiver = node_text(fn, source), ""
            elif fn.type == "field_access" and (member := fn.child_by_field_name("field")):
                name = node_text(member, source)
                obj = fn.child_by_field_name("object")
                receiver = node_text(obj, source) if obj is not None else ""
            else:
                continue
            if name not in names:
                continue
            owner = _enclosing_function(dc.record, n.start_point[0] + 1)
            if owner is None:
                continue
            keys = {(c.path, name) for c in owner.calls
                    if c.name == name and c.path and (c.path, name) in helpers}
            if len(keys) != 1:
                continue
            if receiver not in ("", "this", "super") and any(
                    c.name == name and not c.path for c in owner.calls):
                continue  # another `name` here resolves nowhere (`map.get(…)`): only a bare,
                # `this.` or `super.` call is surely the helper's
            url_idx, template, slot, verb = helpers[next(iter(keys))]
            args = n.child_by_field_name("arguments")
            positional = [a for a in (args.named_children if args is not None else ())
                          if a.type != "named_argument"]
            if url_idx >= len(positional):
                continue
            arg = positional[url_idx]
            method = _enclosing_method(n)
            if method is not None and arg.type == "identifier":
                name_text = node_text(arg, source)
                span = (method.start_byte, method.end_byte)
                if span not in scopes:
                    scopes[span] = (_params(method, source), *_scope(method, source))
                params, values, assigned = scopes[span]
                if name_text in params:
                    continue  # a relay passing its own parameter on: not followed
                value = values.get(name_text) if name_text not in assigned else None
                if value is not None and _render_url(value, source) is not None:
                    arg = value
            rendered = _render_url(arg, source)
            if rendered is None:
                # a value cog cannot read, named after its source (contract §6):
                # `uri` → `{uri}`, `uri.toString()` → `{uri}`; nameless → no record
                name_part = runtime_name(node_text(arg, source))
                if name_part == "value":
                    continue
                rendered = "{" + name_part + "}"
            found.append(Emit(n, {"semanticType": "api_call", "method": verb,
                                  "endpoint": template.replace(slot, rendered)},
                              enrich="unclassified",
                              reclassify=frozenset({"api_call", "db_method_call"})))
    return Detection(found) if found else None


register_detector(Detector(
    name="groovy-http-helpers",
    gate=Gate("groovy", order=95, guard=None, skip_fixtures=True),
    detect=_detect,
))
