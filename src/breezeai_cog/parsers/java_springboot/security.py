"""Spring Security URL authorization rules as route guards: the registered fact
``spring.security-rules`` on the Java index (the Spring parser reads it).

    http.authorizeRequests().antMatchers("/admin/**").hasRole("ADMIN").anyRequest().authenticated();
    http.authorizeHttpRequests(auth -> auth.requestMatchers("/public/**").permitAll() …);

Each rule is a matcher (``antMatchers`` / ``mvcMatchers`` / ``requestMatchers``, optionally with
an ``HttpMethod``, or ``anyRequest()``) and an access rule (``hasRole("ADMIN")``,
``authenticated()``, ``access("…")``, ``permitAll()`` …). Spring Security applies the **first**
rule that matches a request. So a Spring route gets, in ``guards``, the first rule that certainly
matches its address (``hasRole(ADMIN)``) — nothing when that rule is ``permitAll()`` or no rule
matches; ``authRequired`` only when the rule certainly rejects anonymous callers. Paths ignored
through ``web.ignoring()`` bypass security: no guard.

Honest-null — no guard is written for a route when an earlier rule *might* match it
(``/admin/{id}`` against ``/admin/new``), when a rule cannot be read (a regex matcher, a pattern
that is not a constant, a custom ``RequestMatcher``), or when the route's module has anything but
exactly one authorization chain (several ``SecurityFilterChain`` beans, ``securityMatcher``
scoping: which chain serves the route depends on bean order). Patterns are matched against the
path within the servlet context, as Spring Security does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..constfold import resolve_all
from ..java.constants import collect_constants, fold_arg
from ..java_security_guards import _ESCAPES, _SIGNED_IN
from ..java_spring_config import module_of
from ..treesitter import node_text

FACT = "spring.security-rules"

_AUTHORIZE = frozenset({"authorizeRequests", "authorizeHttpRequests"})
#: Matcher calls whose patterns are path patterns (``requestMatchers`` is MVC-backed in a
#: Spring MVC application, like ``mvcMatchers``).
_MATCHERS = {"antMatchers": "ant", "mvcMatchers": "mvc", "requestMatchers": "mvc"}
#: Calls that scope a chain to some requests: which chain serves a route is then unsure.
_SCOPING = frozenset({"securityMatcher", "securityMatchers", "antMatcher", "mvcMatcher",
                      "requestMatcher", "regexMatcher"})
#: Access rules that open the path to everyone (no guard).
_OPEN = frozenset({"permitAll"})
#: Access rules that certainly reject an anonymous caller.
_SIGNED_IN_RULES = frozenset({"authenticated", "fullyAuthenticated", "rememberMe", "denyAll",
                              "hasRole", "hasAnyRole", "hasAuthority", "hasAnyAuthority"})
_ACCESS = _OPEN | _SIGNED_IN_RULES | {"anonymous", "hasIpAddress", "access"}
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"})


class Rule(NamedTuple):
    """One authorization rule. ``patterns`` None = ``anyRequest()``; ``kind`` "unknown" = a rule
    that cannot be read (it may match anything). ``guard`` None = open (``permitAll``)."""

    kind: str
    methods: tuple[str, ...]
    patterns: tuple[str, ...] | None
    guard: str | None
    requires_auth: bool


_UNKNOWN = Rule("unknown", (), None, None, False)


@dataclass
class SecurityFacts:
    """One file's authorization chains, ignored paths and whether it scopes a chain."""

    rel: str
    module: str
    chains: list[list[Rule]] = field(default_factory=list)
    ignored: list[Rule] = field(default_factory=list)
    scoped: bool = False


def _name(node: Node | None, source: bytes) -> str:
    return node_text(node, source) if node is not None else ""


def _args(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    return list(args.named_children) if args is not None else []


def _chain(outer: Node, source: bytes) -> tuple[Node | None, list[Node]]:
    """``a.b(…).c(…)`` (the outermost call) → (root receiver ``a``, calls in order b, c)."""
    calls: list[Node] = []
    node: Node | None = outer
    while node is not None and node.type == "method_invocation":
        calls.append(node)
        node = node.child_by_field_name("object")
    return node, calls[::-1]


def _outermost(call: Node) -> Node:
    """The last call of the chain ``call`` is part of (``call.x().y()`` → the ``y`` call)."""
    node = call
    while (parent := node.parent) is not None and parent.type == "method_invocation" \
            and parent.child_by_field_name("object") == node:
        node = parent
    return node


def _strings(args: list[Node], source: bytes, consts: dict[str, str]) -> tuple[str, ...] | None:
    out: list[str] = []
    for a in args:
        value = fold_arg(a, source, consts)
        if value is None:
            return None
        out.append(value)
    return tuple(out)


def _matcher(call: Node, kind: str, source: bytes, consts: dict[str, str]
             ) -> tuple[str, tuple[str, ...], tuple[str, ...] | None] | None:
    """``antMatchers(HttpMethod.POST, "/a", "/b")`` → (kind, methods, patterns); None when an
    argument is not a constant pattern."""
    args = _args(call)
    methods: tuple[str, ...] = ()
    if args and args[0].type in ("field_access", "identifier") \
            and _name(args[0], source).rsplit(".", 1)[-1] in _HTTP_METHODS:
        methods = (_name(args[0], source).rsplit(".", 1)[-1],)
        args = args[1:]
    if not args:  # antMatchers(HttpMethod.GET): every path of that method
        return kind, methods, ("/**",)
    patterns = _strings(args, source, consts)
    return (kind, methods, patterns) if patterns is not None else None


def _access(call: Node, source: bytes, consts: dict[str, str]) -> tuple[str | None, bool] | None:
    """An access call → (guard text or None for open, certainly signed-in); None = unreadable."""
    name = _name(call.child_by_field_name("name"), source)
    if name in _OPEN:
        return None, False
    values = _strings(_args(call), source, consts)
    if values is None:
        return None
    if name == "access":
        expr = " ".join(values)
        signed_in = bool(_SIGNED_IN.search(expr)) and not _ESCAPES.search(expr)
        return f"access({expr})", signed_in
    return f"{name}({','.join(values)})", name in _SIGNED_IN_RULES


def _rules(calls: list[Node], source: bytes, consts: dict[str, str], *, ignoring: bool = False
           ) -> list[Rule]:
    """Pair each matcher call with the access call that follows it, in order. Anything the
    chain does that is not understood becomes an unknown rule (it may match any request)."""
    out: list[Rule] = []
    pending: tuple[str, tuple[str, ...], tuple[str, ...] | None] | None = None
    for call in calls:
        name = _name(call.child_by_field_name("name"), source)
        if name in ("and", "build"):  # the authorization block ends here
            break
        if name in _MATCHERS:
            pending = _matcher(call, _MATCHERS[name], source, consts) or ("unknown", (), None)
            if ignoring:
                out.append(Rule(pending[0], pending[1], pending[2], None, False)
                           if pending[0] != "unknown" else _UNKNOWN)
                pending = None
        elif name == "anyRequest":
            pending = ("any", (), None)
        elif name in _ACCESS and pending is not None:
            access = _access(call, source, consts)
            if pending[0] == "unknown" or access is None:
                out.append(_UNKNOWN)
            else:
                out.append(Rule(pending[0], pending[1], pending[2], access[0], access[1]))
            pending = None
        else:  # not(), a custom matcher, dispatcherTypeMatchers …: unsure
            out.append(_UNKNOWN)
            pending = None
    return out


def collect_security_facts(root: Node, source: bytes, rel: str) -> SecurityFacts | None:
    """One Java file's authorization chains (in source order); None when it has none."""
    module = module_of(rel)
    if module is None:
        return None
    consts = resolve_all(collect_constants(root, source))
    facts = SecurityFacts(rel=rel, module=module)
    stack = [root]
    calls: list[Node] = []
    while stack:
        n = stack.pop()
        if n.type == "method_invocation":
            calls.append(n)
        stack.extend(n.named_children)
    calls.sort(key=lambda c: c.start_byte)
    for call in calls:
        name = _name(call.child_by_field_name("name"), source)
        if name in _SCOPING:
            facts.scoped = True
        if name == "ignoring":
            chain = _chain(_outermost(call), source)[1]
            facts.ignored += _rules(chain[chain.index(call) + 1:], source, consts, ignoring=True)
        if name not in _AUTHORIZE:
            continue
        args = _args(call)
        if not args:  # authorizeRequests().antMatchers(…)… — the rest of this chain
            chain = _chain(_outermost(call), source)[1]
            facts.chains.append(_rules(chain[chain.index(call) + 1:], source, consts))
        elif len(args) == 1 and args[0].type == "lambda_expression":
            body = args[0].child_by_field_name("body")
            exprs = [s.named_children[0] for s in body.named_children
                     if s.type == "expression_statement" and s.named_children] \
                if body is not None and body.type == "block" else [body]
            rules: list[Rule] = []
            for expr in exprs:
                if expr is None or expr.type != "method_invocation":
                    rules.append(_UNKNOWN)
                    continue
                rules += _rules(_chain(expr, source)[1], source, consts)
            facts.chains.append(rules)
        else:  # a method reference, Customizer.withDefaults(): rules not readable
            facts.chains.append([_UNKNOWN])
    return facts if facts.chains or facts.ignored else None


def resolve_security(facts: list[SecurityFacts]) -> dict[str, list[Rule] | None]:
    """Per module: its ignored paths then its one chain's rules; None (unsure) when the module
    has several chains, none, or a scoped chain. Independent of the order of ``facts``."""
    by_module: dict[str, list[SecurityFacts]] = {}
    for f in sorted(facts, key=lambda g: g.rel):
        by_module.setdefault(f.module, []).append(f)
    out: dict[str, list[Rule] | None] = {}
    for module, files in by_module.items():
        chains = [c for f in files for c in f.chains]
        if len(chains) != 1 or any(f.scoped for f in files):
            out[module] = None
            continue
        out[module] = [r for f in files for r in f.ignored] + chains[0]
    return out


# ── matching ────────────────────────────────────────────────────────────────────────────────

_YES, _NO, _MAYBE = "yes", "no", "maybe"


def _segment(pattern: str, seg: str) -> str:
    """One pattern segment against one path segment; a path variable (``{id}``) may be any
    value, so only a wildcard segment certainly matches it."""
    if seg.startswith("{") and seg.endswith("}"):
        if pattern == "*" or re.fullmatch(r"\{[^}]*\}", pattern):
            return _YES
        return _MAYBE
    rx = "".join("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch)
                 for ch in re.sub(r"\{[^}]*\}", "*", pattern))
    return _YES if re.fullmatch(rx, seg) else _NO


def _match_segments(pat: list[str], path: list[str]) -> str:
    if not pat:
        return _YES if not path else _NO
    if pat[0] == "**":
        results = {_match_segments(pat[1:], path[i:]) for i in range(len(path) + 1)}
        return _YES if _YES in results else _MAYBE if _MAYBE in results else _NO
    if not path:
        return _NO
    first = _segment(pat[0], path[0])
    if first == _NO:
        return _NO
    rest = _match_segments(pat[1:], path[1:])
    return _NO if rest == _NO else _YES if first == rest == _YES else _MAYBE


def path_match(kind: str, pattern: str, path: str) -> str:
    """Whether an Ant / MVC pattern matches a route address: yes, no, or maybe (a path variable
    could take the value a literal names). An MVC pattern also matches ``path`` + ``/`` and
    ``path`` + ``.ext``: where only those could differ, the answer is maybe."""
    result = _match_segments([s for s in pattern.split("/") if s],
                             [s for s in path.split("/") if s])
    if result == _NO and kind == "mvc" and (path + "/").startswith(pattern.rstrip("/") + "."):
        return _MAYBE
    return result


def route_rule(rules: list[Rule], method: str | None, endpoint: str | None) -> Rule | None:
    """The rule Spring Security certainly applies to a route (the first that matches), or None
    when none matches or the first possible match is not certain."""
    for rule in rules:
        if rule.kind == "unknown":
            return None
        if rule.methods:
            if method is None:
                return None
            if method not in rule.methods:
                continue
        if rule.patterns is None:  # anyRequest()
            return rule
        if endpoint is None:
            if any(p in ("/**", "**") for p in rule.patterns):
                return rule
            return None
        results = {path_match(rule.kind, p, endpoint) for p in rule.patterns}
        if _YES in results:
            return rule
        if _MAYBE in results:
            return None
    return None


def security_rules(index: Any | None) -> dict[str, list[Rule] | None]:
    """Module root → its rules, or None when unsure (``spring.security-rules``)."""
    return index_fact(index, FACT) or {}


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> SecurityFacts | None:
    return collect_security_facts(root, source, rel)


def _resolve(values: list[SecurityFacts], index: Any) -> dict[str, list[Rule] | None] | None:
    return resolve_security(values) or None


register_fact(FactSpec(
    name=FACT, language="java", gate=(b"authorizeRequests", b"authorizeHttpRequests"),
    collect=_collect, resolve=_resolve,
))
