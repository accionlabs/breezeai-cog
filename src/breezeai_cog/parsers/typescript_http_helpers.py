"""TypeScript / JavaScript HTTP calls made through the application's own helpers are recorded where
they run (BREEZEAI-1654; Java: ``java_http_helpers.py``).

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
template and the verb: the one the call names,
or the parameter that carries it (``request(method, url)``). A function whose URL slot cannot be
proven (the parameter rewritten, two parameters, a config object ``axios({ url })``) is not one.
**Relay** — a function of the same file passing its own parameter into a helper's URL position
(``get(url) { return this.request('GET', url); }``); a helper too.

**Call site** — a call the TypeScript call resolver links (``calls[].path``) to a helper's file
under the helper's name. A name two functions of one file share is not followed. It carries the
``framework`` the helper's own request would get (``axios``, ``fetch``, ``angular``: the client
detectors' evidence, read in the helper's file); none when no detector, or two, vouch for it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..emit import statement_id
from ..schemas import FileRecord
from .additive import DetectContext, Detection, Detector, FactSpec, Gate, index_fact, \
    register_detector, register_fact
from .detection.api_calls import match_api
from .emits import Emit
from .index_common import record_distinct
from .statements_common import HTTP_VERB_ARGS, url_or_value
from .treesitter import node_text
from .typescript.statements import collect_http_client_ids, render_url

FACT = "typescript.http-helpers"
_DEPTH = 4
_VERBS = frozenset({"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"})
#: Function values a name can hold.
_FN_VALUES = frozenset({"arrow_function", "function_expression", "function"})
#: Nodes that start a scope of their own: calls inside belong to it.
_SCOPES = frozenset({"function_declaration", "generator_function_declaration",
                     "method_definition", "arrow_function", "function_expression", "function",
                     "generator_function", "class_declaration", "class"})

#: A helper: (URL parameter index, URL template, its slot, fixed verb or None, verb parameter
#: index or -1, the client's framework label or None — what its own request is labelled with).
Helper = tuple[int, str, str, str | None, int, str | None]
#: A relay: (own name, callee name, (callee argument index, own parameter index, the argument
#: rendered with the parameter as ``{name}``, the parameter's name) entries, (callee argument index, verb literal)
#: pairs). An argument is the parameter itself (``{url}``) or an expression holding it once
#: (``'/api' + path`` → ``/api{path}``).
Relay = tuple[str, str, tuple[tuple[int, int, str, str], ...], tuple[tuple[int, str], ...]]


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


def _scan(root: Node, source: bytes) -> tuple[list[tuple[str, Helper, Node]], list[Relay],
                                              list[str]]:
    """This file's helpers (name, helper, the request node), relays and function names."""
    client_ids = collect_http_client_ids(root, source)
    labels: list[tuple[str, Callable[[Node], bool] | None]] = []
    helpers: list[tuple[str, Helper, Node]] = []
    relays: list[Relay] = []
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
                slot = "{" + refs[0] + "}"
                template = url_or_value(url, source, render_url) or ""
                if template.count(slot) != 1 or not template.endswith(slot):
                    # the parameter is not the URL's end: `${apiUrl}/orders` names its endpoint
                    # itself (a base passed in), `/orders/${id}/items` too — not a helper
                    continue
                vidx = params.index(node_text(verb_arg, source)) \
                    if verb_arg is not None \
                    and verb_arg.type in ("identifier", "shorthand_property_identifier") \
                    and node_text(verb_arg, source) in usable else -1
                helpers.append((name, (params.index(refs[0]), template, slot, verb, vidx,
                                       _framework(call, root, source, labels)), call))
                continue
            callee = call.child_by_field_name("function")
            text = node_text(callee, source) if callee is not None else ""
            target = text.removeprefix("this.")
            if not target.isidentifier():
                continue  # a call on another object: not followed within the file
            pairs: list[tuple[int, int, str, str]] = []
            for i, a in enumerate(named):
                refs = _reads(a, usable, source)
                if len(set(refs)) != 1 or len(refs) != 1:
                    continue
                shown = url_or_value(a, source, render_url) or ""
                if shown.count("{" + refs[0] + "}") == 1:
                    pairs.append((i, params.index(refs[0]), shown, refs[0]))
            mapping = tuple(pairs)
            literals = tuple((i, (render_url(a, source) or "").upper()) for i, a in enumerate(named)
                             if a.type == "string")
            if mapping:
                relays.append((name, target, mapping, literals))
    return helpers, relays, names


def _framework(call: Node, root: Node, source: bytes,
               labels: list[tuple[str, Callable[[Node], bool] | None]]) -> str | None:
    """The label the client detectors give this request (``axios``, ``fetch``, ``angular``):
    the same evidence, read once per file (``labels``). None when none, or more than one,
    vouches for it."""
    if not labels:
        from .typescript_angular.http_client import angular_evidence
        from .typescript_axios.calls import axios_evidence
        from .typescript_fetch.calls import fetch_evidence
        labels.extend((("axios", axios_evidence(root, source)),
                       ("fetch", fetch_evidence(root, source)),
                       ("angular", angular_evidence(root, source)
                        if b"@angular/common/http" in source else None)))
    hits = [name for name, evidenced in labels if evidenced is not None and evidenced(call)]
    return hits[0] if len(hits) == 1 else None


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


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None
             ) -> tuple[str, list[tuple[str, Helper]], list[Relay], list[str]] | None:
    if not _has_request(root, source):
        return None
    helpers, relays, names = _scan(root, source)
    if not helpers:
        return None
    return rel, [(n, h) for n, h, _ in helpers], relays, names


def _resolve(values: list[tuple[str, list[tuple[str, Helper]], list[Relay], list[str]]],
             index: Any) -> dict[tuple[str, str], Helper] | None:
    """(file, name) → helper. A name two functions of one file share, or a helper of two shapes,
    is dropped; relays within a file are followed up to :data:`_DEPTH` deep."""
    out: dict[tuple[str, str], Helper] = {}
    for rel, helpers, relays, names in values:
        twice = {n for n in names if names.count(n) > 1}
        found: dict[str, Helper | None] = {}
        for name, helper in helpers:
            record_distinct(found, name, helper)
        known = {n: h for n, h in found.items() if h is not None and n not in twice}
        for _ in range(_DEPTH):
            new: dict[str, Helper | None] = {}
            for name, target, mapping, literals in relays:
                helper = known.get(target)
                own = {i: (p, shown, pname) for i, p, shown, pname in mapping}
                if name in known or name in twice or helper is None or helper[0] not in own:
                    continue
                url, template, slot, verb, vidx, framework = helper
                param, shown, pname = own[url]
                new_vidx = -1
                if vidx >= 0:
                    if vidx in own and own[vidx][1] == "{" + own[vidx][2] + "}":
                        new_vidx = own[vidx][0]  # the verb parameter passed on as is
                    else:
                        lit = dict(literals).get(vidx)
                        verb = lit if lit in _VERBS else None
                new_slot = "{" + pname + "}"
                record_distinct(new, name, (param, template.replace(slot, shown), new_slot, verb,
                                            new_vidx, framework))
            added = {n: h for n, h in new.items() if h is not None}
            if not added:
                break
            known.update(added)
        for name, helper in known.items():
            out[(rel, name)] = helper
    return out or None


register_fact(FactSpec(name=FACT, language="typescript", gate=(b"(",), collect=_collect,
                       resolve=_resolve))


def _enclosing_function(record: FileRecord, line: int):
    best = None
    for fn in record.functions:
        if fn.startLine <= line <= fn.endLine and (
                best is None or fn.endLine - fn.startLine < best.endLine - best.startLine):
            best = fn
    return best


def _relayed(node: Node, path: str, helpers: dict[tuple[str, str], Helper],
             scopes: dict[tuple[int, int], str], source: bytes) -> bool:
    """``node`` (a URL argument) reads a parameter of the function it is written in, and that
    function is itself a helper of this file: a relay passing its URL on — its callers make the
    request."""
    fn = node.parent
    while fn is not None and fn.type not in _SCOPES:
        fn = fn.parent
    name = scopes.get((fn.start_byte, fn.end_byte)) if fn is not None else None
    if name is None or (path, name) not in helpers:
        return False
    params = _params(fn, source) or []
    return bool(_reads(node, params, source))


def _local_value(node: Node, source: bytes) -> Node | None:
    """The value of a ``const`` of that name declared once in the enclosing function."""
    name = node_text(node, source)
    fn = node.parent
    while fn is not None and fn.type not in _SCOPES:
        fn = fn.parent
    found: list[Node] = []
    stack = [fn] if fn is not None else []
    while stack:
        n = stack.pop()
        if n.type == "lexical_declaration" and node_text(n, source).startswith("const"):
            for d in n.named_children:
                nm = d.child_by_field_name("name") if d.type == "variable_declarator" else None
                if nm is not None and node_text(nm, source) == name:
                    value = d.child_by_field_name("value")
                    if value is not None:
                        found.append(value)
        stack.extend(n.named_children)
    return found[0] if len(found) == 1 else None


def _statement_node(call: Node, path: str, ids: dict[str, Any]) -> Node | None:
    """The captured statement holding ``call``: walk up to the node a statement id names."""
    node: Node | None = call
    while node is not None:
        stmt = ids.get(statement_id(path, node.start_point[0] + 1, node.start_point[1]))
        if stmt is not None and stmt.nodeType == node.type:
            return node
        node = node.parent
    return None


def _detect(dc: DetectContext) -> Detection | None:
    helpers: dict[tuple[str, str], Helper] = index_fact(dc.ctx.resolution_index, FACT) or {}
    if not helpers:
        return None
    source, path = dc.source, dc.path
    found: list[Emit] = []
    # 1. The helper's own request on its URL parameter: a plain statement.
    own = {name for (rel, name) in helpers if rel == path}
    if own:
        ids = {s.id: s for s in dc.record.statements if s.semanticType == "api_call"}
        for name, helper, call in _scan(dc.root, source)[0]:
            if name in own and helpers.get((path, name)) == helper:
                node = _statement_node(call, path, ids)
                if node is not None:
                    found.append(Emit(node, {"semanticType": None, "method": None,
                                             "endpoint": None, "framework": None},
                                      enrich="same"))
    # 2. Each call of a helper: the api_call.
    names = {name for (_, name) in helpers}
    if any(n.encode() in source for n in names):
        scopes = {(fn.start_byte, fn.end_byte): name for name, fn in _functions(dc.root, source)}
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
            owner = _enclosing_function(dc.record, n.start_point[0] + 1)
            calls = owner.calls if owner is not None else (dc.record.calls or [])
            keys = {(c.path, name) for c in calls if c.name == name and c.path
                    and (c.path, name) in helpers}
            if len(keys) != 1:
                continue
            receiver = callee.rsplit(".", 1)[0] if "." in callee else ""
            if receiver not in ("", "this") and any(c.name == name and not c.path for c in calls):
                continue  # another `name` here resolves nowhere (`map.get(…)`): only a bare or
                # `this.` call is surely the helper's
            url, template, slot, verb, vidx, framework = helpers[next(iter(keys))]
            args = n.child_by_field_name("arguments")
            named = list(args.named_children) if args is not None else []
            if url >= len(named) or _relayed(named[url], path, helpers, scopes, source):
                continue  # a relay passing its own parameter on: its callers make the request
            arg = named[url]
            if arg.type == "identifier":
                value_node = _local_value(arg, source)
                if value_node is not None and render_url(value_node, source) is not None:
                    arg = value_node
            value = node_text(arg, source) if arg.type == "number" \
                else url_or_value(arg, source, render_url)
            if value is None:
                continue
            if vidx >= 0:
                lit = render_url(named[vidx], source) if vidx < len(named) else None
                verb = lit.upper() if lit and lit.upper() in _VERBS else None
            fields = {"semanticType": "api_call", "method": verb,
                      "endpoint": template.replace(slot, value)}
            if framework:  # the helper's client: its label moves with the request
                fields["framework"] = framework
            found.append(Emit(n, fields, enrich="unclassified"))
    return Detection(found) if found else None


register_detector(Detector(
    name="typescript-http-helpers",
    # after the axios / fetch / Angular HttpClient labels (36–38), so a helper's own call loses
    # its label too
    gate=Gate("typescript", order=39, guard=None, skip_fixtures=True),
    detect=_detect,
))
