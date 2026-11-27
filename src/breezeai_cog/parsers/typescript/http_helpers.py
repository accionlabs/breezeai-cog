"""TypeScript / JavaScript HTTP calls made through the application's own helpers are recorded where
they run — the TypeScript adapter of the shared rule (``parsers/outbound_helpers.py``;
BREEZEAI-1654).

The shared classifier records ``axios.get(url)`` as an ``api_call`` where the client is called.
Inside a helper of the application that call is made on the helper's parameter, and says nothing
of where the request goes; the helper's callers hold the URL::

    // src/utils/httpRequest.ts
    async function getRequest(url: string, headers) {            // a helper
      const response = await axios.get(url, { headers });       // plain statement
    }
    // src/services/confluence.service.ts
    const url = `https://${domain}.atlassian.net/rest/api/3/field`;
    fields = await getRequest(url, { … });     // api_call GET https://${domain}.atlassian.net/…

Contract §5, "Where an outbound record is written": each call of a helper is the ``api_call``, the
caller's URL argument in the helper's URL template; the helper's own call on its URL parameter is
a plain statement, whether or not any of its callers resolve.

**Helper** — a function of the repository (a declaration, a method, a function stored in a
``const``, a class field or an object property) whose HTTP call, as the shared classifier reads
it (``match_api`` with the file's client names), takes its URL from one of the function's own
parameters, whole or as its last part (``{url}``, ``{apiPath}{url}``, ``/api{path}``); a
function whose URL only takes a base or an id from a parameter (``${apiUrl}/orders``,
``/orders/${id}/items``) names its endpoint itself and keeps its record. A helper keeps the URL
template and the verb: the one the call names, or the parameter that carries it
(``request(method, url)``). A function whose URL slot cannot be proven (the parameter rewritten,
two parameters, a config object ``axios({ url })``) is not one.
**Relay** — a function of the same file passing its own parameter into a helper's URL position
(``get(url) { return this.request('GET', url); }``); a helper too.

**Call site** — a call the TypeScript call resolver links (``calls[].path``) to a helper's file
under the helper's name. A name two functions of one file share is not followed. It carries the
``framework`` the helper's own request would get (``axios``, ``fetch``, ``angular``: the labels
the client packages register, read in the helper's file); none when no label, or two, vouch for
it.

What this module reads is TypeScript grammar only; the engine merges helpers, follows relays,
links call sites and writes the records, and the client packages contribute their labels by
registering them (no client package is imported here). Each helper's request is kept in the
fact by position, so the helper's own record is cleared without scanning its file again, and a
call site's ``const`` is read from one walk of its function, not one walk per call.
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
from ..outbound_helpers import FileLabels, Helper, HelperPolicy, Pass, RelayCall, clear_emits, \
    enclosing_function, follow_relays, link_site, site_emit, site_verb, unresolved_twin, \
    url_slot
from ..statements_common import HTTP_VERB_ARGS, url_or_value
from ..treesitter import node_text
from .statements import collect_http_client_ids, render_url

FACT = "typescript.http-helpers"

POLICY = HelperPolicy(
    language="typescript",
    relays="same-file",
    depth=4,
    sticky_ambiguous=False,
    verbs=frozenset({"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"}),
    own_receivers=frozenset({"", "this"}),
)
_VERBS = POLICY.verbs
#: Function values a name can hold.
_FN_VALUES = frozenset({"arrow_function", "function_expression", "function"})
#: Nodes that start a scope of their own: calls inside belong to it.
_SCOPES = frozenset({"function_declaration", "generator_function_declaration",
                     "method_definition", "arrow_function", "function_expression", "function",
                     "generator_function", "class_declaration", "class"})

#: A helper's request in its file, to clear it in pass 2: (function name, the helper it made,
#: start byte, end byte, node type).
RequestAt = tuple[str, Helper, int, int, str]


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
    #: file → the requests of its helpers to clear (start byte, end byte, node type).
    requests: dict[str, tuple[tuple[int, int, str], ...]]


def _params(fn: Node, source: bytes) -> list[str] | None:
    """Parameter names; None when one is a pattern or a rest parameter (positions unsure)."""
    single = fn.child_by_field_name("parameter")  # `url => …`
    if single is not None:
        return [node_text(single, source)]
    params = fn.child_by_field_name("parameters")
    names: list[str] = []
    for p in params.named_children if params is not None else []:
        if p.type in ("required_parameter", "optional_parameter"):
            pat = p.child_by_field_name("pattern")
            if pat is None or pat.type != "identifier":
                return None
            names.append(node_text(pat, source))
        elif p.type == "identifier":  # JavaScript
            names.append(node_text(p, source))
        elif p.type in ("assignment_pattern",):
            left = p.child_by_field_name("left")
            if left is None or left.type != "identifier":
                return None
            names.append(node_text(left, source))
        elif p.type != "comment":
            return None
    return names


def _functions(root: Node, source: bytes) -> Iterator[tuple[str, Node]]:
    """(the name callers use, the function node) for each named function of the file."""
    stack = [root]
    while stack:
        n = stack.pop()
        stack.extend(n.named_children)
        kind = n.type
        if kind in ("function_declaration", "method_definition"):
            name = n.child_by_field_name("name")
            if name is not None:
                yield node_text(name, source).strip("'\""), n
        elif kind == "variable_declarator":
            name, value = n.child_by_field_name("name"), n.child_by_field_name("value")
            if name is not None and name.type == "identifier" and value is not None \
                    and value.type in _FN_VALUES:
                yield node_text(name, source), value
        elif kind in ("public_field_definition", "field_definition", "pair"):
            key = n.child_by_field_name("name") or n.child_by_field_name("property") \
                or n.child_by_field_name("key")
            value = n.child_by_field_name("value")
            if key is not None and value is not None and value.type in _FN_VALUES:
                yield node_text(key, source).strip("'\""), value


def _own_calls(fn: Node) -> Iterator[Node]:
    """The calls in ``fn``'s body, not those of a function nested in it (an inline callback
    included: a request made in ``.then(…)`` is still the helper's — only named nested functions
    and classes are scopes of their own here)."""
    body = fn.child_by_field_name("body")
    stack = [body] if body is not None else []
    while stack:
        n = stack.pop()
        if n.type == "call_expression":
            yield n
        for c in n.named_children:
            if c.type in ("function_declaration", "class_declaration", "class",
                          "method_definition"):
                continue
            stack.append(c)


def _reads(node: Node, params: list[str], source: bytes) -> list[str]:
    out: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier" and node_text(n, source) in params:
            out.append(node_text(n, source))
        stack.extend(n.named_children)
    return out


def _assigned(fn: Node, source: bytes) -> set[str]:
    """Names assigned in ``fn`` (``url = …``, ``url += …``): a parameter rewritten."""
    out: set[str] = set()
    stack = [fn]
    while stack:
        n = stack.pop()
        if n.type in ("assignment_expression", "augmented_assignment_expression"):
            left = n.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                out.add(node_text(left, source))
        stack.extend(n.named_children)
    return out


def http_request(call: Node, source: bytes, client_ids: frozenset[str]
                 ) -> tuple[str | None, Node | None, Node] | None:
    """(verb, verb argument, URL argument) when the shared classifier reads ``call`` as an HTTP
    call with a positional URL; None otherwise (a config object, no argument)."""
    fn = call.child_by_field_name("function")
    callee = node_text(fn, source) if fn is not None else ""
    verb = match_api(callee, callee.rsplit(".", 1)[-1], client_ids, "typescript")
    args = call.child_by_field_name("arguments")
    named = list(args.named_children) if args is not None else []
    if verb is None or not named or named[0].type == "object":
        return None
    if verb == "GET" and callee.rsplit(".", 1)[-1].lower().endswith("fetch"):
        # `fetch(url, init)`: the verb is init's `method` (GET without one)
        return _fetch_verb(named[1] if len(named) > 1 else None, source) + (named[0],)
    if len(named) >= 2:
        first = render_url(named[0], source)
        if first is not None and first.lower() in HTTP_VERB_ARGS:
            return first.upper(), None, named[1]
        if verb == "REQUEST":  # `request(method, url)`: the verb is an argument
            return None, named[0], named[1]
    return (verb if verb in _VERBS else None), None, named[0]


def _fetch_verb(init: Node | None, source: bytes) -> tuple[str | None, Node | None]:
    """(verb, the node that carries it) for a ``fetch`` init: GET with none; the literal
    ``method``; the node a ``method`` names (a parameter, for a helper); unknown otherwise."""
    if init is None:
        return "GET", None
    if init.type != "object":
        return None, None  # an options variable: its method is not read
    for pair in init.named_children:
        if pair.type == "shorthand_property_identifier" and node_text(pair, source) == "method":
            return None, pair
        key = pair.child_by_field_name("key") if pair.type == "pair" else None
        if key is not None and node_text(key, source).strip("'\"") == "method":
            value = pair.child_by_field_name("value")
            lit = render_url(value, source) if value is not None else None
            if lit is not None and lit.upper() in _VERBS:
                return lit.upper(), None
            return None, value if value is not None and value.type == "identifier" else None
    return "GET", None


def _scan(root: Node, source: bytes) -> tuple[list[tuple[str, Helper, Node]],
                                              list[RelayCall[str]], list[str]]:
    """This file's helpers (name, helper, the request node), relays and function names."""
    client_ids = collect_http_client_ids(root, source)
    labels = FileLabels("typescript", root, source)
    helpers: list[tuple[str, Helper, Node]] = []
    relays: list[RelayCall[str]] = []
    names: list[str] = []
    for name, fn in _functions(root, source):
        names.append(name)
        params = _params(fn, source)
        if not params:
            continue
        rewritten = _assigned(fn, source) & set(params)
        usable = [p for p in params if p not in rewritten]
        for call in _own_calls(fn):
            req = http_request(call, source, client_ids)
            args = call.child_by_field_name("arguments")
            named = list(args.named_children) if args is not None else []
            if req is not None:
                verb, verb_arg, url = req
                refs = _reads(url, usable, source)
                if len(refs) != 1:
                    continue
                template = url_or_value(url, source, render_url) or ""
                slot = url_slot(refs, template)
                if slot is None:
                    # the parameter is not the URL's end: `${apiUrl}/orders` names its endpoint
                    # itself (a base passed in), `/orders/${id}/items` too — not a helper
                    continue
                vname = node_text(verb_arg, source) \
                    if verb_arg is not None \
                    and verb_arg.type in ("identifier", "shorthand_property_identifier") \
                    and node_text(verb_arg, source) in usable else ""
                helpers.append((name, Helper(params.index(refs[0]), template, slot, verb,
                                             params.index(vname) if vname else -1,
                                             url_name=refs[0], verb_name=vname,
                                             framework=labels.label_of(call)), call))
                continue
            callee = call.child_by_field_name("function")
            text = node_text(callee, source) if callee is not None else ""
            target = text.removeprefix("this.")
            if not target.isidentifier():
                continue  # a call on another object: not followed within the file
            passes: list[Pass] = []
            for i, a in enumerate(named):
                refs = _reads(a, usable, source)
                if len(set(refs)) != 1 or len(refs) != 1:
                    continue
                shown = url_or_value(a, source, render_url) or ""
                if shown.count("{" + refs[0] + "}") == 1:
                    passes.append(Pass(i, params.index(refs[0]), refs[0], shown))
            literals = tuple((i, (render_url(a, source) or "").upper()) for i, a in enumerate(named)
                             if a.type == "string")
            if passes:
                relays.append(RelayCall(name, target, tuple(passes), literals))
    return helpers, relays, names


#: A word an HTTP call of the shared classifier needs in the file: a client name it knows
#: (``match_api``'s hints, ``fetch``), or what makes a name a client (``axios``, a config
#: object's ``method``). A file with none of them is not walked.
_CLIENT_WORDS = re.compile(rb"(?i)http|axios|fetch|superagent|\bgot\b|\bky\b|webclient|apiclient|"
                           rb"restclient|request|method")


def _has_request(root: Node, source: bytes) -> bool:
    """Whether any call of the file is an HTTP request with a positional URL — one walk. Relays
    are followed within a file, so a file without one holds no helper."""
    if not _CLIENT_WORDS.search(source):
        return False
    client_ids = collect_http_client_ids(root, source)
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "call_expression" and http_request(n, source, client_ids) is not None:
            return True
        stack.extend(n.named_children)
    return False


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> FileHelpers | None:
    if not _has_request(root, source):
        return None
    helpers, relays, names = _scan(root, source)
    if not helpers:
        return None
    return FileHelpers(rel, tuple((n, h) for n, h, _ in helpers), tuple(relays), tuple(names),
                       tuple((n, h, c.start_byte, c.end_byte, c.type) for n, h, c in helpers))


def _fold_verb(literal: str) -> str | None:
    return literal if literal in _VERBS else None


def _by_name(known: dict[str, Helper]) -> Callable[[RelayCall[str]], Helper | None]:
    """A relay's callee: the function of that name in the file, as known so far."""
    def lookup(relay: RelayCall[str]) -> Helper | None:
        return known.get(relay.target)
    return lookup


def _resolve(values: list[FileHelpers], index: Any) -> Fact | None:
    """(file, name) → helper. A name two functions of one file share, or a helper of two shapes,
    is dropped; relays within a file are followed (:data:`POLICY`)."""
    out: dict[tuple[str, str], Helper] = {}
    requests: dict[str, tuple[tuple[int, int, str], ...]] = {}
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
        # the helper's own request, when the function is that helper in the end
        kept = tuple((start, end, kind) for name, helper, start, end, kind in v.requests
                     if known.get(name) == helper)
        if kept:
            requests[v.path] = kept
    return Fact(out, requests) if out else None


register_fact(FactSpec(name=FACT, language="typescript", gate=(b"(",), collect=_collect,
                       resolve=_resolve))


def _request_node(root: Node, start: int, end: int, kind: str) -> Node | None:
    """The node a helper's request was found at in pass 1 (same bytes, same tree)."""
    node: Node | None = root.descendant_for_byte_range(start, end)
    while node is not None and not (node.type == kind and node.start_byte == start
                                    and node.end_byte == end):
        node = node.parent if node.start_byte >= start and node.end_byte <= end else None
    return node


def _scope_of(node: Node) -> Node | None:
    fn = node.parent
    while fn is not None and fn.type not in _SCOPES:
        fn = fn.parent
    return fn


def _relayed(node: Node, path: str, helpers: dict[tuple[str, str], Helper],
             scopes: dict[tuple[int, int], str], source: bytes) -> bool:
    """``node`` (a URL argument) reads a parameter of the function it is written in, and that
    function is itself a helper of this file: a relay passing its URL on — its callers make the
    request."""
    fn = _scope_of(node)
    name = scopes.get((fn.start_byte, fn.end_byte)) if fn is not None else None
    if fn is None or name is None or (path, name) not in helpers:
        return False
    params = _params(fn, source) or []
    return bool(_reads(node, params, source))


def _constants(fn: Node, source: bytes) -> dict[str, list[Node]]:
    """Every ``const`` declared in ``fn`` (nested functions included): name → values. One walk
    per function (§11), shared by every call site in it."""
    out: dict[str, list[Node]] = {}
    stack = [fn]
    while stack:
        n = stack.pop()
        if n.type == "lexical_declaration" and node_text(n, source).startswith("const"):
            for d in n.named_children:
                nm = d.child_by_field_name("name") if d.type == "variable_declarator" else None
                if nm is not None:
                    value = d.child_by_field_name("value")
                    if value is not None:
                        out.setdefault(node_text(nm, source), []).append(value)
        stack.extend(n.named_children)
    return out


def _local_value(node: Node, source: bytes,
                 cache: dict[tuple[int, int], dict[str, list[Node]]]) -> Node | None:
    """The value of a ``const`` of that name declared once in the enclosing function."""
    fn = _scope_of(node)
    if fn is None:
        return None
    span = (fn.start_byte, fn.end_byte)
    if span not in cache:
        cache[span] = _constants(fn, source)
    found = cache[span].get(node_text(node, source), [])
    return found[0] if len(found) == 1 else None


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
    # 1. The helper's own request on its URL parameter: a plain statement.
    own = fact.requests.get(path, ())
    if own:
        nodes = [n for start, end, kind in own
                 if (n := _request_node(dc.root, start, end, kind)) is not None]
        found.extend(clear_emits(nodes, path, dc.record))
    # 2. Each call of a helper: the api_call.
    names = {name for (_, name) in helpers}
    if any(n.encode() in source for n in names):
        scopes = {(fn.start_byte, fn.end_byte): name for name, fn in _functions(dc.root, source)}
        constants: dict[tuple[int, int], dict[str, list[Node]]] = {}
        stack = [dc.root]
        while stack:
            n = stack.pop()
            stack.extend(n.named_children)
            if n.type != "call_expression":
                continue
            fn = n.child_by_field_name("function")
            callee = node_text(fn, source) if fn is not None else ""
            name = callee.rsplit(".", 1)[-1].replace("?", "")
            if name not in names:
                continue
            owner = enclosing_function(dc.record, n.start_point[0] + 1)
            calls = owner.calls if owner is not None else (dc.record.calls or [])
            key = link_site(calls, name, _helper_key(helpers, name))
            if key is None:
                continue
            receiver = callee.rsplit(".", 1)[0] if "." in callee else ""
            if receiver not in POLICY.own_receivers and unresolved_twin(calls, name):
                continue  # another `name` here resolves nowhere (`map.get(…)`): only a bare or
                # `this.` call is surely the helper's
            helper = helpers[key]
            args = n.child_by_field_name("arguments")
            named = list(args.named_children) if args is not None else []
            # a spread (`...args`) may hold any number of arguments: positions from it on are unknown
            spread = next((i for i, a in enumerate(named) if a.type == "spread_element"),
                          len(named))
            url = helper.url_index
            if url >= spread or _relayed(named[url], path, helpers, scopes, source):
                continue  # a relay passing its own parameter on: its callers make the request
            arg = named[url]
            if arg.type == "identifier":
                value_node = _local_value(arg, source, constants)
                if value_node is not None and render_url(value_node, source) is not None:
                    arg = value_node
            value = node_text(arg, source) if arg.type == "number" \
                else url_or_value(arg, source, render_url)
            if value is None:
                continue
            vidx = helper.verb_index
            literal = render_url(named[vidx], source) if 0 <= vidx < spread else None
            found.append(site_emit(n, helper, value, site_verb(helper, literal, _VERBS), POLICY))
    return Detection(found) if found else None


register_detector(Detector(
    name="typescript-http-helpers",
    # after the axios / fetch / Angular HttpClient labels (36–38), so a helper's own call loses
    # its label too
    gate=Gate("typescript", order=39, guard=None, skip_fixtures=True),
    detect=_detect,
))
