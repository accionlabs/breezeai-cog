"""Groovy HTTP calls made through the application's own helpers are recorded where they run — the
Groovy adapter of the shared rule (``parsers/outbound_helpers.py``).

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

Not followed (:data:`POLICY`): a URL parameter rewritten in the method, two parameters in the
URL, a method that passes its own parameter on to a helper (a relay: neither it nor its callers
get a record), a client held in a field or a parameter.

What this module reads is Groovy grammar only: the engine decides helpers, links call sites and
writes the records. Each helper's request is kept in the fact by position, so the helper's own
record is cleared without scanning its file again.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, FactSpec, Gate, index_fact, \
    register_detector, register_fact
from ..addresses import runtime_name
from ..emits import Emit
from ..outbound_helpers import Helper, HelperPolicy, clear_emits, enclosing_function, \
    link_site, merge_helpers, site_emit, site_verb, unresolved_twin, url_slot
from ..statements_common import url_or_value
from ..treesitter import node_text
from .functions import _new_client, class_fields, client_locals, declarations
from .statements import _render_url

FACT = "groovy.http-helpers"
_URL_KEYS = frozenset({"url", "uri", "path"})
_DECLARING = frozenset({"local_variable_declaration", "formal_parameter",
                        "catch_formal_parameter", "closure_parameter", "for_in_statement"})

POLICY = HelperPolicy(
    language="groovy",
    relays="none",
    verbs=frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}),
    own_receivers=frozenset({"", "this", "super"}),
    reclassify=frozenset({"api_call", "db_method_call"}),
)

#: A helper's request in its file, to clear it in pass 2: (method name, start byte, end byte,
#: node type).
RequestAt = tuple[str, int, int, str]


@dataclass(frozen=True, slots=True)
class FileHelpers:
    """What one file gives the fact."""

    path: str
    helpers: tuple[tuple[str, Helper], ...]
    requests: tuple[RequestAt, ...]
    #: Method names declared more than once in the file (overloads): their requests are not
    #: cleared — a call of one may be another's.
    overloaded: frozenset[str]


@dataclass(frozen=True, slots=True)
class Fact:
    helpers: dict[tuple[str, str], Helper]
    #: file → the requests of its helpers to clear.
    requests: dict[str, tuple[RequestAt, ...]]


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


def _requests(fn: Node, fields: dict[str, str | None], values: dict[str, Node | None],
              source: bytes) -> Iterator[tuple[Node, Node, str]]:
    """The method's requests on a new client: (verb call, the client's URL argument, verb).
    ``fields``: its class's fields (read once per class); ``values``: its locals (:func:`_scope`,
    built once by the caller)."""
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
        if verb not in POLICY.verbs:
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


def _scan(root: Node, source: bytes) -> tuple[list[tuple[str, Helper, Node]], frozenset[str]]:
    """This file's helpers (method name, helper, its request's verb call) and the method names
    it declares more than once."""
    found: list[tuple[str, Helper, Node]] = []
    seen: set[str] = set()
    twice: set[str] = set()
    fields_of: dict[int, dict[str, str | None]] = {}  # a class's fields: read once per class
    for fn, body in _methods(root):
        name_node = fn.child_by_field_name("name")
        name = node_text(name_node, source) if name_node is not None else ""
        (twice if name in seen else seen).add(name)
        if name_node is None or fn.child_by_field_name("body") is None:
            continue
        params = _params(fn, source)
        if not params:
            continue
        values, assigned = _scope(fn, source)
        fields: dict[str, str | None] = {}
        if body is not None:
            if body.id not in fields_of:
                fields_of[body.id] = class_fields(body, source)
            fields = fields_of[body.id]
        for call, url, verb in _requests(fn, fields, values, source):
            expr: Node | None = url
            if url.type == "identifier" and node_text(url, source) not in params:
                local = node_text(url, source)
                expr = values.get(local) if local not in assigned else None
            if expr is None:
                continue
            refs = _reads(expr, params, source)
            if len(refs) != 1 or refs[0] in assigned or refs[0] in values:
                continue  # no parameter, two, or one rewritten / shadowed in the method
            template = url_or_value(expr, source, _render_url) or ""
            slot = url_slot(refs, template)
            if slot is None:
                continue  # a base or an id from the parameter: the method names its endpoint
            found.append((name, Helper(params.index(refs[0]), template, slot, verb), call))
    return found, frozenset(twice)


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> FileHelpers | None:
    found, twice = _scan(root, source)
    if not found:
        return None
    return FileHelpers(rel, tuple((name, helper) for name, helper, _ in found),
                       tuple((name, call.start_byte, call.end_byte, call.type)
                             for name, _, call in found),
                       twice)


def _resolve(values: list[FileHelpers], index: Any) -> Fact | None:
    """(file, method name) → helper; a name declared twice in one file with different helpers
    (or as a helper and not) is dropped: which one a call means is not known."""
    helpers = merge_helpers(((v.path, name), h) for v in values for name, h in v.helpers)
    if not helpers:
        return None
    requests = {v.path: kept for v in values
                if (kept := tuple(r for r in v.requests
                                  if (v.path, r[0]) in helpers and r[0] not in v.overloaded))}
    return Fact(helpers, requests)


register_fact(FactSpec(name=FACT, language="groovy", gate=(b"new ",), collect=_collect,
                       resolve=_resolve))


def _request_node(root: Node, start: int, end: int, kind: str) -> Node | None:
    """The node a helper's request was found at in pass 1 (same bytes, same tree)."""
    node: Node | None = root.descendant_for_byte_range(start, end)
    while node is not None and not (node.type == kind and node.start_byte == start
                                    and node.end_byte == end):
        node = node.parent if node.start_byte >= start and node.end_byte <= end else None
    return node


def _helper_key(helpers: dict[tuple[str, str], Helper], name: str
                ) -> Callable[[str], tuple[str, str] | None]:
    """``calls[].path`` → the helper ``name`` is in that file, or None."""
    def key_of(path: str) -> tuple[str, str] | None:
        return (path, name) if (path, name) in helpers else None
    return key_of


def _enclosing_method(node: Node) -> Node | None:
    cur = node.parent
    while cur is not None and cur.type != "method_declaration":
        if cur.type == "class_body":
            return None
        cur = cur.parent
    return cur


def _detect(dc: DetectContext) -> Detection | None:
    fact: Fact | None = index_fact(dc.ctx.resolution_index, FACT)
    if fact is None:
        return None
    helpers = fact.helpers
    source, path = dc.source, dc.path
    found: list[Emit] = []
    own = fact.requests.get(path, ())
    if own:
        nodes = [n for _, start, end, kind in own
                 if (n := _request_node(dc.root, start, end, kind)) is not None]
        found.extend(clear_emits(nodes, path, dc.record))
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
            owner = enclosing_function(dc.record, n.start_point[0] + 1)
            if owner is None:
                continue
            key = link_site(owner.calls, name, _helper_key(helpers, name))
            if key is None:
                continue
            if receiver not in POLICY.own_receivers and unresolved_twin(owner.calls, name):
                continue  # another `name` here resolves nowhere (`map.get(…)`): only a bare,
                # `this.` or `super.` call is surely the helper's
            helper = helpers[key]
            args = n.child_by_field_name("arguments")
            positional = [a for a in (args.named_children if args is not None else ())
                          if a.type != "named_argument"]
            if helper.url_index >= len(positional):
                continue
            arg = positional[helper.url_index]
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
            found.append(site_emit(n, helper, rendered, site_verb(helper, None, POLICY.verbs),
                                   POLICY))
    return Detection(found) if found else None


register_detector(Detector(
    name="groovy-http-helpers",
    gate=Gate("groovy", order=95, guard=None, skip_fixtures=True),
    detect=_detect,
))
