"""Python HTTP calls made through the application's own helpers are recorded where they run
(BREEZEAI-1654; Java: ``java_http_helpers.py``, TypeScript: ``typescript_http_helpers.py``).

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
"""

from __future__ import annotations

import re
from collections.abc import Iterator
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
from .python.statements import _render_url
from .statements_common import HTTP_VERB_ARGS, url_or_value
from .treesitter import node_text

FACT = "python.http-helpers"
_DEPTH = 4
_VERBS = frozenset({"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"})
_VERB_PARAMS = ("method", "http_method", "verb")

#: A helper: (URL parameter index, its name, URL template, its slot, fixed verb or None, verb
#: parameter index or -1, verb parameter name or "", the verb parameter's default or None, a
#: method — ``self`` / ``cls`` dropped from its positions).
Helper = tuple[int, str, str, str, str | None, int, str, str | None, bool]


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


def _arguments(call: Node, source: bytes) -> tuple[list[Node], dict[str, Node], bool]:
    """(positional arguments, keyword arguments, a ``*args`` / ``**kw`` among the positional)."""
    args = call.child_by_field_name("arguments")
    positional: list[Node] = []
    keywords: dict[str, Node] = {}
    splat = False
    for a in args.named_children if args is not None else []:
        if a.type == "keyword_argument":
            nm, value = a.child_by_field_name("name"), a.child_by_field_name("value")
            if nm is not None and value is not None:
                keywords[node_text(nm, source)] = value
        elif a.type == "list_splat":
            splat = True
        elif a.type == "dictionary_splat":
            continue
        elif a.type != "comment":
            positional.append(a)
    return positional, keywords, splat


def http_request(call: Node, source: bytes) -> tuple[str | None, Node | None, Node] | None:
    """(verb, the node carrying it, URL argument) when the shared classifier reads ``call`` as an
    HTTP call."""
    fn = call.child_by_field_name("function")
    callee = node_text(fn, source) if fn is not None else ""
    verb = match_api(callee, callee.rsplit(".", 1)[-1], None, "python")
    if verb is None:
        return None
    positional, keywords, splat = _arguments(call, source)
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
    return (verb if verb in _VERBS else None), None, url if url is not None else None


def _scan(root: Node, source: bytes) -> tuple[list[tuple[str, Helper, Node]],
                                              list[tuple[str, str, tuple, tuple]], list[str]]:
    """This file's helpers (name, helper, request node), relays and function names."""
    helpers: list[tuple[str, Helper, Node]] = []
    relays: list[tuple[str, str, tuple, tuple]] = []
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
            if req is not None and req[2] is not None:
                verb, verb_node, url = req
                refs = _reads(url, usable, source)
                if len(refs) != 1:
                    continue
                slot = "{" + refs[0] + "}"
                template = url_or_value(url, source, _render_url) or ""
                if template.count(slot) != 1 or not template.endswith(slot):
                    continue
                vname = node_text(verb_node, source) if verb_node is not None \
                    and verb_node.type == "identifier" and node_text(verb_node, source) in usable \
                    else ""
                vidx = params.index(vname) if vname else -1
                found.append(((params.index(refs[0]), refs[0], template, slot, verb, vidx, vname,
                               defaults.get(vname), _is_method(fn)), call))
        # One URL slot, several verbs (the function branches on its verb): the verb parameter.
        shapes = {(h[0], h[2], h[3]) for h, _ in found}
        if len(shapes) == 1:
            verbs = {(h[4], h[5]) for h, _ in found}
            if len(verbs) == 1:
                helper = found[0][0]
            else:
                vname = next((p for p in _VERB_PARAMS if p in usable), "")
                url_idx, url_name, template, slot = found[0][0][:4]
                helper = (url_idx, url_name, template, slot, None,
                          params.index(vname) if vname else -1, vname, defaults.get(vname),
                          _is_method(fn))
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
            positional, keywords, splat = _arguments(call, source)
            if splat:
                continue
            pairs = []
            for key, a in [*enumerate(positional), *keywords.items()]:
                refs = _reads(a, usable, source)
                shown = url_or_value(a, source, _render_url) or ""
                if len(refs) == 1 and shown.count("{" + refs[0] + "}") == 1:
                    pairs.append((key, params.index(refs[0]), refs[0], shown))
            literals = tuple((key, (_render_url(a, source) or "").upper())
                             for key, a in [*enumerate(positional), *keywords.items()]
                             if a.type == "string")
            if pairs:
                relays.append((name, target, tuple(pairs), literals, _is_method(fn)))
    return helpers, relays, names


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None):
    helpers, relays, names = _scan(root, source)
    if not helpers:
        return None
    return rel, [(n, h) for n, h, _ in helpers], relays, names


def _resolve(values: list[tuple[str, list, list, list[str]]], index: Any
             ) -> dict[tuple[str, str], Helper] | None:
    out: dict[tuple[str, str], Helper] = {}
    for rel, helpers, relays, names in values:
        twice = {n for n in names if names.count(n) > 1}
        found: dict[str, Helper | None] = {}
        for name, helper in helpers:
            record_distinct(found, name, helper)
        known = {n: h for n, h in found.items() if h is not None and n not in twice}
        for _ in range(_DEPTH):
            new: dict[str, Helper | None] = {}
            for name, target, pairs, literals, is_method in relays:
                helper = known.get(target)
                if name in known or name in twice or helper is None:
                    continue
                url_idx, url_name, template, slot, verb, vidx, vname, vdefault, _ = helper
                own = {key: (p, pname, shown) for key, p, pname, shown in pairs}
                hit = own.get(url_idx) or own.get(url_name)
                if hit is None:
                    continue
                param, pname, shown = hit
                new_vidx, new_vname, new_default = -1, "", None
                if vidx >= 0:
                    vhit = own.get(vidx) or own.get(vname)
                    lits = dict(literals)
                    lit = lits.get(vidx) or lits.get(vname)
                    if vhit is not None and vhit[2] == "{" + vhit[1] + "}":
                        new_vidx, new_vname = vhit[0], vhit[1]  # passed on as is
                    elif lit is not None:
                        verb = lit if lit in _VERBS else None  # a literal verb
                    elif vhit is None:
                        verb = vdefault.upper() if vdefault else None  # left out: its default
                    else:
                        verb = None  # an expression: unknown
                record_distinct(new, name, (param, pname, template.replace(slot, shown),
                                            "{" + pname + "}", verb, new_vidx, new_vname,
                                            new_default, is_method))
            added = {n: h for n, h in new.items() if h is not None}
            if not added:
                break
            known.update(added)
        for name, helper in known.items():
            out[(rel, name)] = helper
    return out or None


register_fact(FactSpec(name=FACT, language="python", gate=(b"(",), collect=_collect,
                       resolve=_resolve))


def _enclosing_function(record: FileRecord, line: int):
    best = None
    for fn in record.functions:
        if fn.startLine <= line <= fn.endLine and (
                best is None or fn.endLine - fn.startLine < best.endLine - best.startLine):
            best = fn
    return best


def _statement_node(call: Node, path: str, ids: dict[str, Any]) -> Node | None:
    node: Node | None = call
    while node is not None:
        stmt = ids.get(statement_id(path, node.start_point[0] + 1, node.start_point[1]))
        if stmt is not None and stmt.nodeType == node.type:
            return node
        node = node.parent
    return None


def _relayed(node: Node, path: str, helpers: dict[tuple[str, str], Helper],
             scopes: dict[tuple[int, int], str], source: bytes) -> bool:
    """``node`` reads a parameter of the function it is in, and that function is a helper."""
    fn = node.parent
    while fn is not None and fn.type != "function_definition":
        fn = fn.parent
    name = scopes.get((fn.start_byte, fn.end_byte)) if fn is not None else None
    if name is None or (path, name) not in helpers:
        return False
    sig = _params(fn, source)
    return bool(sig) and bool(_reads(node, sig[0], source))


def _local_value(node: Node, source: bytes) -> Node | None:
    """The value of a name assigned once in the enclosing function."""
    name = node_text(node, source)
    fn = node.parent
    while fn is not None and fn.type != "function_definition":
        fn = fn.parent
    found: list[Node] = []
    stack = [fn] if fn is not None else []
    while stack:
        n = stack.pop()
        if n.type == "assignment":
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is not None and right is not None and left.type == "identifier" \
                    and node_text(left, source) == name:
                found.append(right)
        stack.extend(n.named_children)
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


def _detect(dc: DetectContext) -> Detection | None:
    helpers: dict[tuple[str, str], Helper] = index_fact(dc.ctx.resolution_index, FACT) or {}
    if not helpers:
        return None
    source, path = dc.source, dc.path
    found: list[Emit] = []
    own = {name for (rel, name) in helpers if rel == path}
    if own:
        ids = {s.id: s for s in dc.record.statements if s.semanticType == "api_call"}
        for name, helper, call in _scan(dc.root, source)[0]:
            if name in own and (path, name) in helpers:
                node = _statement_node(call, path, ids)
                if node is not None:
                    found.append(Emit(node, {"semanticType": None, "method": None,
                                             "endpoint": None, "framework": None},
                                      enrich="same"))
    names, pattern = _names_pattern(helpers)
    if pattern.search(source):
        scopes = {(fn.start_byte, fn.end_byte): name for name, fn in _functions(dc.root, source)}
        stack = [dc.root]
        while stack:
            n = stack.pop()
            stack.extend(n.named_children)
            if n.type != "call":
                continue
            fn = n.child_by_field_name("function")
            name = node_text(fn, source).rsplit(".", 1)[-1] if fn is not None else ""
            if name not in names:
                continue
            owner = _enclosing_function(dc.record, n.start_point[0] + 1)
            calls = owner.calls if owner is not None else (dc.record.calls or [])
            keys = {(c.path, name) for c in calls if c.name == name and c.path
                    and (c.path, name) in helpers}
            if len(keys) != 1:
                continue
            receiver = node_text(fn, source).rsplit(".", 1)[0] if fn.type == "attribute" else ""
            if receiver not in ("", "self", "cls") and any(
                    c.name == name and not c.path for c in calls):
                continue  # another `name` here resolves nowhere (`config.get(…)`): only a bare or
                # own-class call is surely the helper's
            url_idx, url_name, template, slot, verb, vidx, vname, vdefault, is_method = \
                helpers[next(iter(keys))]
            positional, keywords, splat = _arguments(n, source)
            if splat or (is_method and receiver[:1].isupper() and receiver.isidentifier()):
                continue  # `Client.get(c, url)`: the instance passed first shifts the positions
            arg = positional[url_idx] if url_idx < len(positional) else keywords.get(url_name)
            if arg is None or _relayed(arg, path, helpers, scopes, source):
                continue
            if arg.type == "identifier":
                value_node = _local_value(arg, source)
                if value_node is not None and _render_url(value_node, source) is not None:
                    arg = value_node
            value = url_or_value(arg, source, _render_url)
            if value is None:
                continue
            if vidx >= 0:
                vnode = positional[vidx] if vidx < len(positional) else keywords.get(vname)
                lit = _render_url(vnode, source) if vnode is not None else vdefault
                verb = lit.upper() if lit and lit.upper() in _VERBS else None
            found.append(Emit(n, {"semanticType": "api_call", "method": verb,
                                  "endpoint": template.replace(slot, value)},
                              enrich="unclassified"))
    return Detection(found) if found else None


register_detector(Detector(
    name="python-http-helpers",
    gate=Gate("python", order=95, guard=None, skip_fixtures=True),
    detect=_detect,
))
