"""Global guards as a registered fact on the Java index, ``java.web-guards``: servlet filters,
MVC interceptors, JAX-RS filters (Spring and JAX-RS read it).

These run before many handlers but are declared away from them (contract role: Guard; decision
Q9 — a global guard's name goes into ``guards`` of every binding it **certainly** covers):

* a Spring servlet **filter** — a ``@Component`` class that is a ``Filter`` /
  ``OncePerRequestFilter`` / ``GenericFilterBean`` (Spring Boot maps it to ``/*``), a
  ``@WebFilter(urlPatterns = …)`` class, or a ``FilterRegistrationBean`` with ``addUrlPatterns``;
* a Spring MVC **interceptor** — ``registry.addInterceptor(x).addPathPatterns(…)
  .excludePathPatterns(…)`` in a ``WebMvcConfigurer``;
* a JAX-RS ``@Provider`` **request filter** (``ContainerRequestFilter``) — every resource of the
  module, or with a ``@NameBinding`` annotation only the resources / methods carrying it.

Each file contributes raw facts (:func:`collect_guard_facts`); :func:`resolve_guards` reduces the
repo's facts per module. Honest-null: a pattern that is not a literal makes that guard's coverage
unknown, so it is not added anywhere. Patterns are matched against the endpoint **within** the
servlet context (before the context path is joined), as the servlet container and Spring do.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..treesitter import node_text
from .config import module_of

_FILTER_TYPES = frozenset({"Filter", "OncePerRequestFilter", "GenericFilterBean"})
_BEAN_ANNOTATIONS = frozenset({"Component", "Service", "Configuration"})

#: Coverage: a tuple of URL patterns, or None when a pattern is not a literal (unknown → skip).
Patterns = tuple[str, ...] | None


@dataclass
class GuardFacts:
    """One file's global-guard facts (``rel`` keeps the reduce order deterministic)."""

    rel: str
    module: str
    #: (filter class, patterns) — ``("/*",)`` for a ``@Component`` filter.
    filters: list[tuple[str, Patterns]] = field(default_factory=list)
    #: (filter class, patterns) from a ``FilterRegistrationBean`` (overrides the bean's ``/*``).
    registrations: list[tuple[str, Patterns]] = field(default_factory=list)
    #: (interceptor name, include patterns or () for all, exclude patterns), in source order.
    interceptors: list[tuple[str, Patterns, Patterns]] = field(default_factory=list)
    #: (JAX-RS request filter class, the annotation names on it).
    jaxrs_filters: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    #: annotation types declared with ``@NameBinding``.
    name_bindings: list[str] = field(default_factory=list)


@dataclass
class ModuleGuards:
    """Global guards of one module, resolved."""

    #: (filter class, URL patterns), sorted by name (filter order without ``@Order`` is unset).
    filters: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    #: (interceptor, include patterns or () for all, exclude patterns), registration order.
    interceptors: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = field(default_factory=list)
    #: (JAX-RS filter class, its name-binding annotations — empty = every resource).
    jaxrs_filters: list[tuple[str, frozenset[str]]] = field(default_factory=list)


def _annotations(decl: Node) -> list[Node]:
    mods = next((c for c in decl.named_children if c.type == "modifiers"), None)
    return [a for a in (mods.named_children if mods is not None else [])
            if a.type in ("annotation", "marker_annotation")]


def _ann_name(ann: Node, source: bytes) -> str:
    name = ann.child_by_field_name("name")
    return node_text(name, source).rsplit(".", 1)[-1] if name is not None else ""


def _simple(type_node: Node | None, source: bytes) -> str | None:
    """Simple name of a type node (generic arguments and package dropped)."""
    if type_node is None:
        return None
    if type_node.type == "generic_type":
        type_node = next((c for c in type_node.named_children
                          if c.type in ("type_identifier", "scoped_type_identifier")), None)
        if type_node is None:
            return None
    return node_text(type_node, source).rsplit(".", 1)[-1]


def _supertypes(decl: Node, source: bytes) -> set[str]:
    out: set[str] = set()
    sup = decl.child_by_field_name("superclass")
    for c in sup.named_children if sup is not None else []:
        if (n := _simple(c, source)) is not None:
            out.add(n)
    ifaces = decl.child_by_field_name("interfaces")
    for tl in ifaces.named_children if ifaces is not None else []:
        for c in tl.named_children:
            if (n := _simple(c, source)) is not None:
                out.add(n)
    return out


def _strings(node: Node | None, source: bytes) -> Patterns:
    """String literals of an annotation value / argument list; None if any part is not one."""
    if node is None:
        return None
    items = (list(node.named_children)
             if node.type in ("element_value_array_initializer", "argument_list") else [node])
    out: list[str] = []
    for it in items:
        if it.type != "string_literal":
            return None
        frag = next((c for c in it.named_children if c.type == "string_fragment"), None)
        out.append(node_text(frag, source) if frag is not None else "")
    return tuple(out)


def _web_filter_patterns(ann: Node, source: bytes) -> Patterns:
    args = ann.child_by_field_name("arguments")
    for kid in args.named_children if args is not None else []:
        if kid.type == "element_value_pair":
            key = kid.child_by_field_name("key")
            if key is not None and node_text(key, source) in ("urlPatterns", "value"):
                return _strings(kid.child_by_field_name("value"), source)
        else:  # @WebFilter("/api/*")
            return _strings(kid, source)
    return None  # servletNames / no pattern: coverage not from paths


def _walk(node: Node, typ: str) -> Iterable[Node]:
    if node.type == typ:
        yield node
    for c in node.named_children:
        yield from _walk(c, typ)


def _call_name(call: Node, source: bytes) -> str:
    name = call.child_by_field_name("name")
    return node_text(name, source) if name is not None else ""


def _new_type(node: Node | None, source: bytes) -> str | None:
    if node is not None and node.type == "object_creation_expression":
        return _simple(node.child_by_field_name("type"), source)
    return None


def _registrations(method: Node, source: bytes) -> list[tuple[str, Patterns]]:
    """``FilterRegistrationBean``s built in one method: the filter (``new X()`` argument,
    ``setFilter(new X())`` / ``setFilter(local)`` with ``local = new X()``, or the ``<X>`` type
    argument) and its ``addUrlPatterns`` (none → ``/*``)."""
    text = node_text(method, source)
    if "FilterRegistrationBean" not in text:
        return []
    name: str | None = None
    for gen in _walk(method, "generic_type"):
        if _simple(gen, source) == "FilterRegistrationBean":
            args = next((c for c in gen.named_children if c.type == "type_arguments"), None)
            arg = args.named_children[0] if args is not None and args.named_children else None
            name = _simple(arg, source) or name
    patterns: list[str] = []
    unknown = False
    locals_new: dict[str, str] = {}  # local variable → the class it is created as (`new X()`)
    for decl in _walk(method, "variable_declarator"):
        nm, val = decl.child_by_field_name("name"), decl.child_by_field_name("value")
        if nm is not None and (cls := _new_type(val, source)) is not None:
            locals_new[node_text(nm, source)] = cls
    for call in _walk(method, "object_creation_expression"):
        if _new_type(call, source) == "FilterRegistrationBean":
            args = call.child_by_field_name("arguments")
            first = args.named_children[0] if args is not None and args.named_children else None
            name = _new_type(first, source) or name
    for call in _walk(method, "method_invocation"):
        cname = _call_name(call, source)
        args = call.child_by_field_name("arguments")
        if cname == "setFilter" and args is not None and args.named_children:
            arg = args.named_children[0]
            name = _new_type(arg, source) or locals_new.get(node_text(arg, source)) or name
        elif cname in ("addUrlPatterns", "setUrlPatterns"):
            got = _strings(args, source)
            if got is None:
                unknown = True
            else:
                patterns += got
    if name is None:
        return []
    return [(name, None if unknown else tuple(patterns) or ("/*",))]


def _field_types(cls_body: Node | None, source: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in cls_body.named_children if cls_body is not None else []:
        if f.type != "field_declaration":
            continue
        t = _simple(f.child_by_field_name("type"), source)
        for d in f.named_children:
            if d.type == "variable_declarator" and t is not None:
                nm = d.child_by_field_name("name")
                if nm is not None:
                    out[node_text(nm, source)] = t
    return out


def _interceptors(method: Node, source: bytes, fields: dict[str, str]
                  ) -> list[tuple[str, Patterns, Patterns]]:
    """``registry.addInterceptor(x).addPathPatterns(…).excludePathPatterns(…)`` chains, in
    source order. The interceptor's name is its class (``new X()``, or the declared type of a
    field), else the expression text."""
    out: list[tuple[str, Patterns, Patterns]] = []
    for stmt in _walk(method, "expression_statement"):
        call = next((c for c in stmt.named_children if c.type == "method_invocation"), None)
        include: Patterns = ()
        exclude: Patterns = ()
        name: str | None = None
        node = call
        while node is not None and node.type == "method_invocation":
            cname = _call_name(node, source)
            args = node.child_by_field_name("arguments")
            if cname == "addPathPatterns":
                got = _strings(args, source)
                include = None if got is None or include is None else include + got
            elif cname == "excludePathPatterns":
                got = _strings(args, source)
                exclude = None if got is None or exclude is None else exclude + got
            elif cname == "addInterceptor" and args is not None and args.named_children:
                arg = args.named_children[0]
                text = node_text(arg, source)
                name = _new_type(arg, source) or fields.get(text) or text
            node = node.child_by_field_name("object")
        if name is not None:
            out.append((name, include, exclude))
    return out


def collect_guard_facts(root: Node, source: bytes, rel: str) -> GuardFacts | None:
    """One Java file's global-guard facts; None when it has none."""
    if not any(m in source for m in (b"Filter", b"Interceptor", b"NameBinding")):
        return None
    module = module_of(rel)
    if module is None:
        return None
    facts = GuardFacts(rel=rel, module=module)
    for decl in _walk(root, "annotation_type_declaration"):
        if any(_ann_name(a, source) == "NameBinding" for a in _annotations(decl)):
            nm = decl.child_by_field_name("name")
            if nm is not None:
                facts.name_bindings.append(node_text(nm, source))
    for decl in _walk(root, "class_declaration"):
        nm = decl.child_by_field_name("name")
        if nm is None:
            continue
        cls = node_text(nm, source)
        anns = {_ann_name(a, source): a for a in _annotations(decl)}
        supers = _supertypes(decl, source)
        if supers & _FILTER_TYPES:
            if "WebFilter" in anns:
                facts.filters.append((cls, _web_filter_patterns(anns["WebFilter"], source)))
            elif anns.keys() & _BEAN_ANNOTATIONS:
                facts.filters.append((cls, ("/*",)))
        if "ContainerRequestFilter" in supers and "Provider" in anns:
            facts.jaxrs_filters.append((cls, tuple(anns)))
        body = decl.child_by_field_name("body")
        fields = _field_types(body, source)
        for m in body.named_children if body is not None else []:
            if m.type != "method_declaration":
                continue
            mname = m.child_by_field_name("name")
            if mname is not None and node_text(mname, source) == "addInterceptors":
                facts.interceptors += _interceptors(m, source, fields)
            facts.registrations += _registrations(m, source)
    has_any = (facts.filters or facts.registrations or facts.interceptors
               or facts.jaxrs_filters or facts.name_bindings)
    return facts if has_any else None


def resolve_guards(facts: list[GuardFacts]) -> dict[str, ModuleGuards]:
    """Per module: its servlet filters (a registration overrides a bean's ``/*``; differing
    registrations of one filter → unknown → dropped), interceptors (those with a non-literal
    pattern dropped) and JAX-RS filters (name bindings matched repo-wide). The result does not
    depend on the order of ``facts``."""
    bindings = {b for f in facts for b in f.name_bindings}
    out: dict[str, ModuleGuards] = {}
    beans: dict[str, dict[str, Patterns]] = {}
    regs: dict[str, dict[str, Patterns]] = {}
    for f in sorted(facts, key=lambda g: g.rel):
        mg = out.setdefault(f.module, ModuleGuards())
        for name, pats in f.filters:
            beans.setdefault(f.module, {})[name] = pats
        for name, pats in f.registrations:
            seen = regs.setdefault(f.module, {})
            seen[name] = pats if name not in seen or seen[name] == pats else None
        for name, inc, exc in f.interceptors:
            if inc is not None and exc is not None:
                mg.interceptors.append((name, inc, exc))
        for name, anns in f.jaxrs_filters:
            mg.jaxrs_filters.append((name, frozenset(a for a in anns if a in bindings)))
    for module in set(beans) | set(regs):
        merged = {**beans.get(module, {}), **regs.get(module, {})}
        out.setdefault(module, ModuleGuards()).filters = sorted(
            (n, p) for n, p in merged.items() if p is not None
        )
    return out


def servlet_match(pattern: str, path: str) -> bool:
    """Servlet URL-pattern match: ``/*`` all, ``/api/*`` the prefix, ``*.do`` the extension,
    else exact."""
    if pattern == "/*" or pattern == "/":
        return True
    if pattern.endswith("/*"):
        base = pattern[:-2]
        return path == base or path.startswith(base + "/")
    if pattern.startswith("*."):
        return path.endswith(pattern[1:])
    return path == pattern


def _ant_regex(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("/**", i):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "{":
            j = pattern.find("}", i)
            out.append("[^/]+")
            i = j + 1 if j > 0 else len(pattern)
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + "$")


def ant_match(pattern: str, path: str) -> bool:
    """Spring Ant-style path match (``**`` any segments, ``*`` / ``{var}`` one segment)."""
    return bool(_ant_regex(pattern).match(path))


def spring_global_guards(mg: ModuleGuards, endpoint: str | None) -> list[str]:
    """Filters then interceptors that certainly run before a Spring route at ``endpoint`` (the
    path within the servlet context). An unknown endpoint only gets guards that cover all."""
    out: list[str] = []
    for name, pats in mg.filters:
        if any(p in ("/*", "/") for p in pats) or (
                endpoint is not None and any(servlet_match(p, endpoint) for p in pats)):
            out.append(name)
    for name, inc, exc in mg.interceptors:
        if endpoint is None:
            if not inc and not exc:
                out.append(name)
            continue
        if inc and not any(ant_match(p, endpoint) for p in inc):
            continue
        if any(ant_match(p, endpoint) for p in exc):
            continue
        out.append(name)
    return out


FACT = "java.web-guards"


def module_guards(index: Any | None) -> dict[str, ModuleGuards]:
    """Module root → its global guards (``java.web-guards``; Spring and JAX-RS read it)."""
    return index_fact(index, FACT) or {}


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> GuardFacts | None:
    return collect_guard_facts(root, source, rel)


def _resolve(values: list[GuardFacts], index: Any) -> dict[str, ModuleGuards] | None:
    return resolve_guards(values) or None


register_fact(FactSpec(
    name=FACT, language="java", gate=(b"Filter", b"Interceptor", b"NameBinding"),
    collect=_collect, resolve=_resolve,
))
