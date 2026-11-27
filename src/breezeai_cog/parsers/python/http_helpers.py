"""Python HTTP calls made through the application's own helpers are recorded where they run — the
Python adapter of the shared rule (``parsers/outbound_helpers.py``; BREEZEAI-1654).

The shared classifier records ``session.get(url)`` as an ``api_call`` where the client is called.
Inside a helper of the application that call is made on the helper's parameter; its callers hold
the URL::

    async def make_generic_request_with_retry(session, url: str, method: str = "GET", **kw):
        if method.upper() == "POST":
            response = await session.post(url, **kw)        # plain statement
        else:
            response = await session.get(url, **kw)         # plain statement

    await make_generic_request_with_retry(session, url=document_url, method="POST")
                                                            # api_call POST {document_url}

Contract §5, "Where an outbound record is written": each call of a helper is the ``api_call``, the
caller's URL argument (positional or keyword) in the helper's URL template; the helper's own
request on its URL parameter is a plain statement.

**Helper** — a function or method of the repository whose HTTP call, as the shared classifier
reads it (``match_api``), takes its URL from one of the function's own parameters, whole or as its
last part (``{url}``, ``{base}{path}``); one that only takes a base or an id from a parameter
(``f"{base}/orders"``) names its endpoint itself and keeps its record. The verb: the one the call
names; a verb-first ``request(method, url)`` argument or ``method=`` keyword holding a parameter;
or — when the function makes the request with different verbs on the same URL (branching on its
verb) — its parameter named ``method`` / ``http_method`` / ``verb``, with that parameter's default
when a caller leaves it out; else unknown. ``self`` / ``cls`` do not count as positions.

**Relay** — a function of the same file passing its own parameter (or an expression holding it)
into a helper's URL position. **Call site** — a call the Python call resolver links
(``calls[].path``) to a helper's file under the helper's name; a name two functions of one file
share is not followed. Not followed: a URL parameter rewritten, two parameters in the URL, a
``*args`` call.

What this module reads is Python grammar only; the engine merges helpers, follows relays, links
call sites and writes the records. Each helper's request is kept in the fact by position, so the
helper's own record is cleared without scanning its file again, and a call site's local is read
from one walk of its function, not one walk per call.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, FactSpec, Gate, index_fact, \
    register_detector, register_fact
from ..detection.api_calls import match_api
from ..emits import Emit
from ..index_common import record_distinct
from ..outbound_helpers import Helper, HelperPolicy, Pass, RelayCall, clear_emits, \
    enclosing_function, follow_relays, link_site, site_emit, site_verb, unresolved_twin, \
    url_slot
from ..statements_common import HTTP_VERB_ARGS, url_or_value
from ..treesitter import node_text
from .statements import _render_url

FACT = "python.http-helpers"
_VERB_PARAMS = ("method", "http_method", "verb")

POLICY = HelperPolicy(
    language="python",
    relays="same-file",
    depth=4,
    sticky_ambiguous=False,
    verbs=frozenset({"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"}),
    own_receivers=frozenset({"", "self", "cls"}),
)
_VERBS = POLICY.verbs

#: A helper's request in its file, to clear it in pass 2: (function name, start byte, end byte,
#: node type).
RequestAt = tuple[str, int, int, str]


@dataclass(frozen=True, slots=True)
class FileHelpers:
    """What one file gives the fact."""

    path: str
    helpers: tuple[tuple[str, Helper], ...]
    relays: tuple[RelayCall[str], ...]
    #: Every function name of the file, in order (a name twice is not followed).
    names: tuple[str, ...]
    requests: tuple[RequestAt, ...]


@dataclass(frozen=True, slots=True)
class Fact:
    helpers: dict[tuple[str, str], Helper]
    #: file → the requests of its helpers to clear.
    requests: dict[str, tuple[RequestAt, ...]]


def _is_method(fn: Node) -> bool:
    return fn.parent is not None and fn.parent.type == "block" and fn.parent.parent is not None \
        and fn.parent.parent.type == "class_definition"


def _params(fn: Node, source: bytes) -> tuple[list[str], dict[str, str]] | None:
    """(positional parameter names, ``self`` / ``cls`` dropped for a method; their string
    defaults). None with a ``*args`` before them or an unreadable parameter."""
    params = fn.child_by_field_name("parameters")
    names: list[str] = []
    defaults: dict[str, str] = {}
    for p in params.named_children if params is not None else []:
        kind = p.type
        if kind == "identifier":
            names.append(node_text(p, source))
        elif kind in ("typed_parameter",):
            ident = next((c for c in p.named_children if c.type == "identifier"), None)
            if ident is None:
                return None  # `*args: T` / `**kw: T`
            names.append(node_text(ident, source))
        elif kind in ("default_parameter", "typed_default_parameter"):
            nm, value = p.child_by_field_name("name"), p.child_by_field_name("value")
            if nm is None:
                return None
            names.append(node_text(nm, source))
            lit = _render_url(value, source) if value is not None and value.type == "string" \
                else None
            if lit:
                defaults[node_text(nm, source)] = lit
        elif kind in ("list_splat_pattern", "dictionary_splat_pattern", "keyword_separator",
                      "positional_separator"):
            break  # what follows is keyword-only or variadic: positions end here
        elif kind != "comment":
            return None
    if names and names[0] in ("self", "cls") and _is_method(fn):
        names = names[1:]
    return names, defaults


#: Nodes a ``def`` can sit in, besides ``*_statement`` / ``*_clause`` / ``*_definition``.
_HOLDS_STATEMENTS = frozenset({"module", "block", "decorated_definition", "case_clause"})
#: A word an HTTP call of the shared classifier needs in a function (``match_api``'s Python
#: client names); a function without one holds no request.
_CLIENT_WORDS = re.compile(rb"(?i)http|requests|session|aiohttp|urllib|client|\.request\(")


def _functions(root: Node, source: bytes) -> Iterator[tuple[str, Node]]:
    """Every ``def`` of the file — statements only are walked, never an expression's subtree."""
    stack = [root]
    while stack:
        n = stack.pop()
        stack.extend(c for c in n.named_children if c.type in _HOLDS_STATEMENTS
                     or c.type.endswith(("_statement", "_clause", "_definition")))
        if n.type == "function_definition":
            name = n.child_by_field_name("name")
            if name is not None:
                yield node_text(name, source), n


def _own_calls(fn: Node) -> Iterator[Node]:
    """Calls in ``fn``'s body, not those of a ``def`` or ``class`` nested in it."""
    body = fn.child_by_field_name("body")
    stack = [body] if body is not None else []
    while stack:
        n = stack.pop()
        if n.type == "call":
            yield n
        for c in n.named_children:
            if c.type not in ("function_definition", "class_definition"):
                stack.append(c)


def _reads(node: Node, names: list[str], source: bytes) -> list[str]:
    out: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier" and node_text(n, source) in names \
                and not (n.parent is not None and n.parent.type == "keyword_argument"
                         and n.parent.child_by_field_name("name") == n):
            out.append(node_text(n, source))
        stack.extend(n.named_children)
    return out


def _rewritten(fn: Node, source: bytes) -> set[str]:
    out: set[str] = set()
    stack = [fn]
    while stack:
        n = stack.pop()
        if n.type in ("assignment", "augmented_assignment"):
            left = n.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                out.add(node_text(left, source))
        stack.extend(n.named_children)
    return out


def _arguments(call: Node, source: bytes) -> tuple[list[Node], dict[str, Node], bool, bool]:
    """(positional arguments, keyword arguments, a ``*args`` among them, a ``**kw`` among them —
    it may carry any keyword, the verb too)."""
    args = call.child_by_field_name("arguments")
    positional: list[Node] = []
    keywords: dict[str, Node] = {}
    splat = kw_splat = False
    for a in args.named_children if args is not None else []:
        if a.type == "keyword_argument":
            nm, value = a.child_by_field_name("name"), a.child_by_field_name("value")
            if nm is not None and value is not None:
                keywords[node_text(nm, source)] = value
        elif a.type == "list_splat":
            splat = True
        elif a.type == "dictionary_splat":
            kw_splat = True
        elif a.type != "comment":
            positional.append(a)
    return positional, keywords, splat, kw_splat


def http_request(call: Node, source: bytes) -> tuple[str | None, Node | None, Node] | None:
    """(verb, the node carrying it, URL argument) when the shared classifier reads ``call`` as an
    HTTP call."""
    fn = call.child_by_field_name("function")
    callee = node_text(fn, source) if fn is not None else ""
    verb = match_api(callee, callee.rsplit(".", 1)[-1], None, "python")
    if verb is None:
        return None
    positional, keywords, splat, _ = _arguments(call, source)
    if splat:
        return None
    if len(positional) >= 2:
        first = _render_url(positional[0], source)
        if first is not None and first.lower() in HTTP_VERB_ARGS:
            return first.upper(), None, positional[1]
    if verb == "REQUEST":  # `session.request(method, url)` / `request(method=…, url=…)`
        verb_node = positional[0] if positional else keywords.get("method")
        url = positional[1] if len(positional) >= 2 else keywords.get("url")
        if url is None:
            return None
        lit = _render_url(verb_node, source) if verb_node is not None else None
        if lit is not None and lit.upper() in _VERBS:
            return lit.upper(), None, url
        return None, verb_node, url
    url = positional[0] if positional else keywords.get("url")
    return (verb if verb in _VERBS else None), None, url  # type: ignore[return-value]


def _scan(root: Node, source: bytes) -> tuple[list[tuple[str, Helper, Node]],
                                              list[RelayCall[str]], list[str]]:
    """This file's helpers (name, helper, request node), relays and function names."""
    helpers: list[tuple[str, Helper, Node]] = []
    relays: list[RelayCall[str]] = []
    functions = list(_functions(root, source))
    names = [name for name, _ in functions]
    # 1. Direct helpers: only a function whose text names an HTTP client can hold a request.
    candidates = [(name, fn) for name, fn in functions
                  if _CLIENT_WORDS.search(source, fn.start_byte, fn.end_byte)]
    helper_names: set[str] = set()
    for name, fn in candidates:
        sig = _params(fn, source)
        if not sig or not sig[0]:
            continue
        params, defaults = sig
        usable = [p for p in params if p not in _rewritten(fn, source)]
        found: list[tuple[Helper, Node]] = []
        for call in _own_calls(fn):
            req = http_request(call, source)
            if req is None or req[2] is None:
                continue
            verb, verb_node, url = req
            refs = _reads(url, usable, source)
            if len(refs) != 1:
                continue
            template = url_or_value(url, source, _render_url) or ""
            slot = url_slot(refs, template)
            if slot is None:
                continue
            vname = node_text(verb_node, source) if verb_node is not None \
                and verb_node.type == "identifier" and node_text(verb_node, source) in usable \
                else ""
            vidx = params.index(vname) if vname else -1
            found.append((Helper(params.index(refs[0]), template, slot, verb, vidx,
                                 url_name=refs[0], verb_name=vname,
                                 verb_default=defaults.get(vname), is_method=_is_method(fn)),
                          call))
        # One URL slot, several verbs (the function branches on its verb): the verb parameter.
        shapes = {(h.url_index, h.template, h.slot) for h, _ in found}
        if len(shapes) == 1:
            verbs = {(h.verb, h.verb_index) for h, _ in found}
            if len(verbs) == 1:
                helper = found[0][0]
            else:
                vname = next((p for p in _VERB_PARAMS if p in usable), "")
                first = found[0][0]
                helper = Helper(first.url_index, first.template, first.slot, None,
                                params.index(vname) if vname else -1, url_name=first.url_name,
                                verb_name=vname, verb_default=defaults.get(vname),
                                is_method=_is_method(fn))
            helpers.extend((name, helper, call) for _, call in found)
            helper_names.add(name)
    if not helper_names:
        return helpers, relays, names
    # 2. Relays within the file: a function calling one of them by name.
    pattern = re.compile(rb"\b(?:" + b"|".join(re.escape(n.encode()) for n in helper_names)
                         + rb")\s*\(")
    for name, fn in functions:
        if name in helper_names or not pattern.search(source, fn.start_byte, fn.end_byte):
            continue
        sig = _params(fn, source)
        if not sig or not sig[0]:
            continue
        params, defaults = sig
        usable = [p for p in params if p not in _rewritten(fn, source)]
        for call in _own_calls(fn):
            callee = call.child_by_field_name("function")
            target = node_text(callee, source).removeprefix("self.") if callee is not None else ""
            if not target.isidentifier():
                continue
            positional, keywords, splat, _ = _arguments(call, source)
            if splat:
                continue
            args: list[tuple[int | str, Node]] = [*enumerate(positional), *keywords.items()]
            passes: list[Pass] = []
            for key, a in args:
                refs = _reads(a, usable, source)
                shown = url_or_value(a, source, _render_url) or ""
                if len(refs) == 1 and shown.count("{" + refs[0] + "}") == 1:
                    passes.append(Pass(key, params.index(refs[0]), refs[0], shown))
            literals = tuple((key, (_render_url(a, source) or "").upper())
                             for key, a in args if a.type == "string")
            if passes:
                relays.append(RelayCall(name, target, tuple(passes), literals, _is_method(fn)))
    return helpers, relays, names


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> FileHelpers | None:
    helpers, relays, names = _scan(root, source)
    if not helpers:
        return None
    return FileHelpers(rel, tuple((n, h) for n, h, _ in helpers), tuple(relays), tuple(names),
                       tuple((n, c.start_byte, c.end_byte, c.type) for n, _, c in helpers))


def _fold_verb(literal: str) -> str | None:
    return literal if literal in _VERBS else None


def _by_name(known: dict[str, Helper]) -> Callable[[RelayCall[str]], Helper | None]:
    """A relay's callee: the function of that name in the file, as known so far."""
    def lookup(relay: RelayCall[str]) -> Helper | None:
        return known.get(relay.target)
    return lookup


def _resolve(values: list[FileHelpers], index: Any) -> Fact | None:
    """(file, name) → helper, per file: a name two functions share, or a helper of two shapes,
    is dropped; relays within the file are followed (:data:`POLICY`)."""
    out: dict[tuple[str, str], Helper] = {}
    requests: dict[str, tuple[RequestAt, ...]] = {}
    for v in values:
        twice = {n for n in v.names if v.names.count(n) > 1}
        found: dict[str, Helper | None] = {}
        for name, helper in v.helpers:
            record_distinct(found, name, helper)
        known = {n: h for n, h in found.items() if h is not None and n not in twice}
        follow_relays(known, v.relays, lookup=_by_name(known), fold_verb=_fold_verb,
                      policy=POLICY, blocked=set(twice))
        for name, helper in known.items():
            out[(v.path, name)] = helper
        kept = tuple(r for r in v.requests if r[0] in known)
        if kept:
            requests[v.path] = kept
    return Fact(out, requests) if out else None


register_fact(FactSpec(name=FACT, language="python", gate=(b"(",), collect=_collect,
                       resolve=_resolve))


def _request_node(root: Node, start: int, end: int, kind: str) -> Node | None:
    """The node a helper's request was found at in pass 1 (same bytes, same tree)."""
    node: Node | None = root.descendant_for_byte_range(start, end)
    while node is not None and not (node.type == kind and node.start_byte == start
                                    and node.end_byte == end):
        node = node.parent if node.start_byte >= start and node.end_byte <= end else None
    return node


def _enclosing_def(node: Node) -> Node | None:
    fn = node.parent
    while fn is not None and fn.type != "function_definition":
        fn = fn.parent
    return fn


def _relayed(node: Node, path: str, helpers: dict[tuple[str, str], Helper],
             scopes: dict[tuple[int, int], str], source: bytes) -> bool:
    """``node`` reads a parameter of the function it is in, and that function is a helper."""
    fn = _enclosing_def(node)
    name = scopes.get((fn.start_byte, fn.end_byte)) if fn is not None else None
    if fn is None or name is None or (path, name) not in helpers:
        return False
    sig = _params(fn, source)
    return sig is not None and bool(sig[0]) and bool(_reads(node, sig[0], source))


def _assignments(fn: Node, source: bytes) -> dict[str, list[Node]]:
    """Every ``name = value`` in ``fn`` (its nested functions included): name → values. One
    walk per function (§11), shared by every call site in it."""
    out: dict[str, list[Node]] = {}
    stack = [fn]
    while stack:
        n = stack.pop()
        if n.type == "assignment":
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is not None and right is not None and left.type == "identifier":
                out.setdefault(node_text(left, source), []).append(right)
        stack.extend(n.named_children)
    return out


def _local_value(node: Node, source: bytes,
                 cache: dict[tuple[int, int], dict[str, list[Node]]]) -> Node | None:
    """The value of a name assigned once in the enclosing function."""
    fn = _enclosing_def(node)
    if fn is None:
        return None
    span = (fn.start_byte, fn.end_byte)
    if span not in cache:
        cache[span] = _assignments(fn, source)
    found = cache[span].get(node_text(node, source), [])
    return found[0] if len(found) == 1 else None


_PATTERN: tuple[object, set[str], re.Pattern[bytes]] | None = None


def _names_pattern(helpers: dict[tuple[str, str], Helper]) -> tuple[set[str], re.Pattern[bytes]]:
    """The helpers' names, and one pattern finding a call of any of them — built once per index
    (the fact is read-only), not per file."""
    global _PATTERN
    if _PATTERN is None or _PATTERN[0] is not helpers:  # the fact itself, held: never a reused id
        names = {name for (_, name) in helpers}
        _PATTERN = (helpers, names, re.compile(
            rb"\b(?:" + b"|".join(re.escape(n.encode()) for n in sorted(names)) + rb")\s*\("))
    return _PATTERN[1], _PATTERN[2]


def _helper_key(helpers: dict[tuple[str, str], Helper], name: str
                ) -> Callable[[str], tuple[str, str] | None]:
    """``calls[].path`` → the helper ``name`` is in that file, or None."""
    def key_of(path: str) -> tuple[str, str] | None:
        return (path, name) if (path, name) in helpers else None
    return key_of


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
    names, pattern = _names_pattern(helpers)
    if pattern.search(source):
        scopes = {(fn.start_byte, fn.end_byte): name for name, fn in _functions(dc.root, source)}
        assignments: dict[tuple[int, int], dict[str, list[Node]]] = {}
        stack = [dc.root]
        while stack:
            n = stack.pop()
            stack.extend(n.named_children)
            if n.type != "call":
                continue
            fn = n.child_by_field_name("function")
            name = node_text(fn, source).rsplit(".", 1)[-1] if fn is not None else ""
            if fn is None or name not in names:
                continue
            owner = enclosing_function(dc.record, n.start_point[0] + 1)
            calls = owner.calls if owner is not None else (dc.record.calls or [])
            key = link_site(calls, name, _helper_key(helpers, name))
            if key is None:
                continue
            receiver = node_text(fn, source).rsplit(".", 1)[0] if fn.type == "attribute" else ""
            if receiver not in POLICY.own_receivers and unresolved_twin(calls, name):
                continue  # another `name` here resolves nowhere (`config.get(…)`): only a bare or
                # own-class call is surely the helper's
            helper = helpers[key]
            positional, keywords, splat, kw_splat = _arguments(n, source)
            if splat or (helper.is_method and receiver[:1].isupper() and receiver.isidentifier()):
                continue  # `Client.get(c, url)`: the instance passed first shifts the positions
            arg = positional[helper.url_index] if helper.url_index < len(positional) \
                else keywords.get(helper.url_name)
            if arg is None or _relayed(arg, path, helpers, scopes, source):
                continue
            if arg.type == "identifier":
                value_node = _local_value(arg, source, assignments)
                if value_node is not None and _render_url(value_node, source) is not None:
                    arg = value_node
            value = url_or_value(arg, source, _render_url)
            if value is None:
                continue
            literal: str | None = None
            if helper.verb_index >= 0:
                vnode = positional[helper.verb_index] \
                    if helper.verb_index < len(positional) else keywords.get(helper.verb_name)
                # not passed: the parameter's default — unless a `**kw` may pass it
                literal = _render_url(vnode, source) if vnode is not None \
                    else (None if kw_splat else helper.verb_default)
            found.append(site_emit(n, helper, value, site_verb(helper, literal, _VERBS), POLICY))
    return Detection(found) if found else None


register_detector(Detector(
    name="python-http-helpers",
    gate=Gate("python", order=95, guard=None, skip_fixtures=True),
    detect=_detect,
))
