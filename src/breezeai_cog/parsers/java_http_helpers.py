"""HTTP calls made through the application's own helpers are recorded where they run (BREEZEAI-1654).

An HTTP client call is an ``api_call`` where the client is called. When the application wraps the
client in its own method, that call is made on the method's parameter and says nothing of where
the request goes; the method's callers hold the URL::

    // ApiHandler.java
    public HttpClientRequest getClient(String url, …) {               // a relay
      return requestClient(ApiUtil.METHOD_GET, url, null, headers, h);
    }
    public HttpClientRequest requestClient(String method, String url, …) {   // a helper
      HttpClientRequest request = client.request(method, apiPath + url, …);
    }
    // FileApiHandler.java (extends ApiHandler)
    String url = "id";
    HttpClientRequest apiRequest = getClient(url, null, endHandler);  // api_call GET {apiPath}id

Contract §5, "Where an outbound record is written": each call of a helper is the ``api_call``, with
the caller's URL argument in the helper's URL template and the verb the helper fixes or the
caller passes; inside the helper (and each relay) the call on its own URL parameter is a plain
statement, whether or not any of its callers resolve.

**Helper** — a method or constructor of the repository whose HTTP client request (a Vert.x 2
``HttpClient`` request, an Apache ``new HttpGet(…)`` …, matched exactly as capture matches them)
takes its URL from one of its parameters, whole or as its last part (``apiPath + url``); a method
whose URL only takes a base or an id from a parameter (``base + "/orders"``) names its endpoint
itself and keeps its record. It keeps the
URL template (``{apiPath}{url}``, the slot ``{url}``) and the verb: fixed by the request, or the
parameter that carries it (``request(method, …)``).

**Relay** — a method passing its own parameter into a helper's URL position, called on its own
class (unqualified or ``this.``, its superclasses included), on a class name, or through
``super(…)`` / ``this(…)``. It is a helper with the same template; its verb is the helper's, the
parameter it passes into the verb position, or the constant it passes there
(``ApiUtil.METHOD_GET`` folded through the repository's constants).

A method whose URL slot cannot be proven (the parameter rewritten in the method, used twice, two
parameters in the URL) is not a helper and keeps its record; one declared with a different shape
in two files is dropped (unsure).

**Call site** — a call the Java call resolver links to a helper's file under the helper's name,
with the helper's arity: the ``calls`` entry of the enclosing Function decides, so inherited and
static calls resolve as the call graph does. Two helpers the entry could mean → nothing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..schemas import FileRecord
from .additive import DetectContext, Detection, Detector, FactSpec, Gate, index_fact, \
    register_detector, register_fact
from .constfold import resolve_all
from .emits import Emit
from .index_common import record_distinct
from .java.constants import collect_constants
from .java.imports import JavaIndex
from .java_apache_httpclient.http_calls import requests as apache_requests
from .java_vertx.forwarders import _callee, _class_of, _Names, _scan, pick
from .java_vertx.java_syntax import (arguments, imports_and_package, known_address,
                                     local_value, name_of, request_address)
from .java_vertx.legacy import client_requests, returning_client
from .treesitter import node_text

FACT = "java.http-helpers"
_VERBS = frozenset({"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH", "TRACE",
                    "CONNECT"})
#: How many relays deep a helper is followed.
_DEPTH = 6

#: A helper: (URL parameter index, URL template, its slot, fixed verb or None, verb parameter
#: index or -1, framework or None).
Helper = tuple[int, str, str, str | None, int, str | None]
#: A relay: (key, callee owner candidates, exact, ``method/arity``, (callee argument index, own
#: parameter index) pairs, (callee argument index, constant text) pairs).
Relay = tuple[str, tuple[str, ...], bool, str, tuple[tuple[int, int], ...],
              tuple[tuple[int, str], ...]]
#: What one file gives the fact: (its path, helpers, relays, (class, superclass candidates,
#: exact) for each class).
FileFacts = tuple[str, list[tuple[str, Helper]], list[Relay],
                  list[tuple[str, tuple[str, ...], bool]]]


def _signature(decl: Node, source: bytes) -> tuple[list[str], bool]:
    """(parameter names, varargs): a ``T... rest`` parameter is the last name. The positions
    before it are fixed, so a helper with varargs is still followed — called with at least as
    many arguments as it has fixed parameters (``name/k+`` in its key)."""
    params = decl.child_by_field_name("parameters")
    names: list[str] = []
    varargs = False
    for p in params.named_children if params is not None else []:
        if p.type == "formal_parameter":
            names.append(name_of(p.child_by_field_name("name"), source))
        elif p.type == "spread_parameter":
            declarator = next((c for c in p.named_children if c.type == "variable_declarator"),
                              None)
            nm = declarator.child_by_field_name("name") if declarator is not None else None
            names.append(name_of(nm, source) if nm is not None else "")
            varargs = True
    return names, varargs


def _arity(names: list[str], varargs: bool) -> str:
    return f"{len(names) - 1}+" if varargs else str(len(names))


def _members(name: str, count: int) -> Iterator[str]:
    """The keys' ``method/arity`` a call of ``name`` with ``count`` arguments may mean: the exact
    arity, then a varargs method with at most ``count`` fixed parameters."""
    yield f"{name}/{count}"
    for k in range(count, -1, -1):
        yield f"{name}/{k}+"


def _param_refs(node: Node, params: list[str], reassigned: Mapping[str, Any],
                source: bytes) -> list[str]:
    """The method's own parameters ``node`` reads (identifiers only, not rewritten in it)."""
    out: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier":
            text = node_text(n, source)
            if text in params and text not in reassigned:
                out.append(text)
        stack.extend(n.named_children)
    return out


def _requests(root: Node, calls: list[Node], source: bytes
              ) -> list[tuple[Node, str | None, Node | None, Node | None, str]]:
    """This file's HTTP client requests: (node, verb, verb argument, URI argument, framework),
    matched as capture matches them, in one pass over the file."""
    found: list[tuple[Node, str | None, Node | None, Node | None, str]] = []
    if b"HttpClient" in source:
        returning = returning_client(root, source)
        found.extend((call, verb, verb_arg, uri, "vertx")
                     for call, verb, verb_arg, uri in client_requests(root, calls, source,
                                                                       returning, {}))
    if b"org.apache.h" in source:
        singles, wildcards, _ = imports_and_package(root, source)
        imports = [*singles, *(f"{w}.*" for w in wildcards)]
        found.extend((node, verb, None, uri, "") for node, verb, uri in
                     apache_requests(root, source, imports))
    return found


def _scan_file(root: Node, source: bytes, rel: str
               ) -> tuple[FileFacts, list[tuple[str, tuple[int, int]]]]:
    """This file's helpers, relays and classes, and each helper's (key, request span): the
    requests that only define one."""
    names = _Names(root, source)
    scanned = _scan(root, source, names)
    requests = _requests(root, [c for cls in scanned for d in cls.decls for c in d.calls],
                         source) if b"HttpClient" in source or b"org.apache.h" in source else []
    helpers: list[tuple[str, Helper]] = []
    relays: list[Relay] = []
    classes: list[tuple[str, tuple[str, ...], bool]] = []
    spans: list[tuple[str, tuple[int, int]]] = []
    for cls in scanned:
        if cls.superclass:
            found, exact = names.candidates(cls.superclass)
            classes.append((cls.name, found, exact))
        else:
            classes.append((cls.name, (), True))
        for d in cls.decls:
            params, varargs = _signature(d.node, source)
            if not params or d.node.child_by_field_name("body") is None:
                continue
            ctor = d.node.type == "constructor_declaration"
            mname = "<init>" if ctor else name_of(d.node.child_by_field_name("name"), source)
            key = f"{cls.name}.{mname}/{_arity(params, varargs)}"
            fixed = len(params) - 1 if varargs else len(params)
            lo, hi = d.node.start_byte, d.node.end_byte
            for node, verb, verb_arg, uri, framework in requests:
                if uri is None or not lo <= node.start_byte < hi:
                    continue
                refs = _param_refs(uri, params, d.assigned, source)
                if len(refs) != 1 or params.index(refs[0]) >= fixed:
                    continue  # no parameter (an ordinary call), two, or the varargs: no slot
                slot = "{" + refs[0] + "}"
                template = request_address(uri, source, {}) or ""
                if template.count(slot) != 1 or not template.endswith(slot):
                    continue  # a base or an id from the parameter: the method names its endpoint
                vidx = -1
                if verb is None and verb_arg is not None and verb_arg.type == "identifier" \
                        and name_of(verb_arg, source) in params \
                        and name_of(verb_arg, source) not in d.assigned:
                    vidx = params.index(name_of(verb_arg, source))
                helpers.append((key, (params.index(refs[0]), template, slot, verb, vidx,
                                      framework or None)))
                spans.append((key, (node.start_byte, node.end_byte)))
            for call in d.calls:
                args = arguments(call)
                mapping = tuple((i, params.index(name_of(a, source))) for i, a in enumerate(args)
                                if a.type == "identifier" and name_of(a, source) in params[:fixed]
                                and name_of(a, source) not in d.assigned)
                if not mapping:
                    continue
                callee = _callee(call, cls.name, cls.superclass, names, source)
                if callee is None:
                    continue
                constants = tuple((i, node_text(a, source)) for i, a in enumerate(args)
                                  if a.type in ("string_literal", "field_access", "identifier")
                                  and not any(i == j for j, _ in mapping))
                relays.append((key, callee[0], callee[1], callee[2], mapping, constants))
    return (rel, helpers, relays, classes), spans


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> FileFacts | None:
    facts, _ = _scan_file(root, source, rel)
    return facts if facts[1] or facts[2] or facts[3] else None


def _fold(text: str, consts: Mapping[str, str]) -> str | None:
    """A verb constant as written in a relay: a string literal or a repository constant."""
    if len(text) >= 2 and text[0] == text[-1] == '"':
        value: str | None = text[1:-1]
    else:
        value = consts.get(text)
    return value.upper() if value and value.upper() in _VERBS else None


def _resolve(values: list[FileFacts], index: Any) -> dict[str, Any] | None:
    """(helpers by key, (file, ``method``, arity) → key, key → file). A key declared twice
    differently is dropped; relays are followed up to :data:`_DEPTH` deep, through the callee's
    superclasses when it does not declare the method itself."""
    merged: dict[str, Helper | None] = {}
    files: dict[str, str | None] = {}
    relays: list[tuple[str, Relay]] = []
    supers_written: dict[str, tuple[tuple[str, ...], bool]] = {}
    for rel, helpers, rels, classes in values:
        for key, helper in helpers:
            record_distinct(merged, key, helper)
            record_distinct(files, key, rel)
        for relay in rels:
            relays.append((rel, relay))
        for cls, found, exact in classes:
            supers_written[cls] = (found, exact)
    types: dict[str, Any] = getattr(index, "fqcn", None) or {}
    consts: Mapping[str, str] = getattr(index, "consts", None) or {}
    known_classes = set(supers_written)
    supers = {cls: pick(found, exact, known_classes, types) if found else None
              for cls, (found, exact) in supers_written.items()}
    known = {k: v for k, v in merged.items() if v is not None}

    def lookup(owner: str | None, member: str) -> Helper | None:
        name, _, count = member.partition("/")
        seen: set[str] = set()
        while owner is not None and owner not in seen:
            seen.add(owner)
            for m in _members(name, int(count)):
                hit = known.get(f"{owner}.{m}")
                if hit is not None:
                    return hit
            if name == "<init>":
                return None
            owner = supers.get(owner)
        return None

    for _ in range(_DEPTH):
        derived: dict[str, Helper | None] = {}
        derived_files: dict[str, str | None] = {}
        members = {k.rsplit(".", 1)[-1].partition("/")[0] for k in known}
        for rel, (key, owners, exact, member, mapping, constants) in relays:
            if key in merged or member.partition("/")[0] not in members:
                continue
            owner = pick(owners, exact, known_classes, types)
            helper = lookup(owner, member)
            own = dict(mapping)
            if helper is None or helper[0] not in own:
                continue
            url, template, slot, verb, vidx, framework = helper
            new_vidx = -1
            if vidx >= 0:
                if vidx in own:
                    new_vidx = own[vidx]
                else:
                    verb = _fold(dict(constants).get(vidx, ""), consts)
            record_distinct(derived, key, (own[url], template, slot, verb, new_vidx, framework))
            record_distinct(derived_files, key, rel)
        new = {k: v for k, v in derived.items() if v is not None and k not in known}
        if not new:
            break
        known.update(new)
        merged.update(derived)
        for k in new:
            files[k] = derived_files.get(k)
    sites: dict[tuple[str, str], str | None] = {}
    for key in known:
        rel = files.get(key)
        if rel is None:
            continue
        member = key.rsplit(".", 1)[-1]
        name, _, arity = member.partition("/")
        if name == "<init>":
            name = _class_of(key).rsplit(".", 1)[-1]
        record_distinct(sites, (rel, f"{name}/{arity}"), key)
    return {"helpers": known, "sites": sites, "files": files} if known else None


register_fact(FactSpec(name=FACT, language="java", gate=(b"",), collect=_collect,
                       resolve=_resolve))


def defining_requests(root: Node, source: bytes, path: str, index: Any | None
                      ) -> frozenset[tuple[int, int]]:
    """This file's requests that only define one: a helper's request on its own URL parameter.
    Each call of the helper makes the request and is the ``api_call``; these are plain
    statements. (A relay's call passing the parameter on is skipped by the call-site
    detector.)"""
    fact = index_fact(index, FACT)
    if not fact:
        return frozenset()
    helpers = fact["helpers"]
    _, spans = _scan_file(root, source, path)
    return frozenset(span for key, span in spans if key in helpers)


def _own_parameter(node: Node, source: bytes) -> bool:
    """``node`` is a parameter of the method or constructor it is written in."""
    if node.type != "identifier":
        return False
    decl = node.parent
    while decl is not None and decl.type not in ("method_declaration",
                                                 "constructor_declaration"):
        if decl.type in ("lambda_expression", "class_body"):
            return False  # a lambda's or an inner class's own name
        decl = decl.parent
    params = _signature(decl, source)[0] if decl is not None else []
    return node_text(node, source) in params


def _enclosing_function(record: FileRecord, line: int):
    best = None
    for fn in record.functions:
        start, end = fn.startLine or 0, fn.endLine or 0
        if start <= line <= end and (best is None or end - start < (best.endLine or 0)
                                     - (best.startLine or 0)):
            best = fn
    return best


def _detect(dc: DetectContext) -> Detection | None:
    index = dc.ctx.resolution_index
    fact = index_fact(index, FACT)
    if not fact:
        return None
    helpers: dict[str, Helper] = fact["helpers"]
    sites: dict[tuple[str, str], str | None] = fact["sites"]
    names = {member.partition("/")[0] for _, member in sites}
    source = dc.source
    if not any(n.encode() in source for n in names):
        return None
    consts: dict[str, str] | None = None
    found: list[Emit] = []
    stack = [dc.root]
    while stack:
        n = stack.pop()
        stack.extend(n.named_children)
        if n.type not in ("method_invocation", "object_creation_expression"):
            continue
        if n.type == "method_invocation":
            name = name_of(n.child_by_field_name("name"), source)
        else:
            typ = n.child_by_field_name("type")
            name = name_of(typ, source).split("<", 1)[0].rsplit(".", 1)[-1] if typ else ""
        if name not in names:
            continue
        args = arguments(n)
        fn = _enclosing_function(dc.record, n.start_point[0] + 1)
        if fn is None:
            continue
        keys: set[str] = set()
        for c in fn.calls:
            if c.name == name and c.path:
                hit = next((sites[(c.path, m)] for m in _members(name, len(args))
                            if (c.path, m) in sites), None)
                if hit is not None:
                    keys.add(hit)
        if len(keys) != 1:
            continue  # not a helper call, or two helpers it may be: unsure
        obj = n.child_by_field_name("object") if n.type == "method_invocation" else None
        if obj is not None and node_text(obj, source) != "this" and any(
                c.name == name and not c.path for c in fn.calls):
            continue  # another `name` here resolves nowhere (`map.get(…)`): only an unqualified
            # or `this.` call is surely the helper's
        helper = helpers.get(next(iter(keys)))
        if helper is None or helper[0] >= len(args):
            continue
        url, template, slot, verb, vidx, framework = helper
        if _own_parameter(args[url], source):
            continue  # a relay passing its own URL parameter on: its callers make the request
        if consts is None:
            cross = index.consts if isinstance(index, JavaIndex) else {}
            consts = resolve_all(collect_constants(dc.root, source), base=cross)
        arg = args[url]
        if arg.type == "identifier":
            # `String url = "id"; getClient(url)`: a local given one constant value
            init = local_value(arg, node_text(arg, source), source)
            if init is not None and known_address(init, source, consts) is not None:
                arg = init
        value = request_address(arg, source, consts)
        if value is None:
            continue
        if vidx >= 0 and vidx < len(args):
            folded = known_address(args[vidx], source, consts)
            verb = folded.upper() if folded and folded.upper() in _VERBS else None
        fields: dict[str, Any] = {"semanticType": "api_call", "method": verb,
                                  "endpoint": template.replace(slot, value)}
        if framework:
            fields["framework"] = framework
        found.append(Emit(n, fields, enrich="unclassified",
                          reclassify=frozenset({"api_call", "db_method_call"})))
    return Detection(found) if found else None


register_detector(Detector(
    name="java-http-helpers",
    gate=Gate("java", order=85, guard=None, skip_fixtures=True),
    detect=_detect,
))
