"""JAX-RS wiring as a registered fact on the Java index, ``jaxrs.prefixes``: ``@ApplicationPath``
and sub-resource locators.

Two constructs put a prefix in front of a JAX-RS resource's routes from outside its class:

* ``@ApplicationPath("/api")`` on the ``Application`` subclass — the base of every resource of
  that application (scoped to the Maven/Gradle module, like the Spring context path). The same
  base may be set in the module's ``web.xml`` or properties instead (:mod:`.config`); sources
  that disagree make it unknown. A module with no base of its own, in a repo where another module
  sets one, may be served by that module's application: its base is unknown too.
* a **sub-resource locator** — a resource method with ``@Path`` and no HTTP-verb annotation,
  returning another resource class: ``@Path("/items") public ItemRes items()`` on
  ``@Path("/orders") class OrderRes`` serves ``ItemRes``'s methods under ``/orders/items``.

Each file contributes its raw facts (:func:`collect_jaxrs_facts`); :func:`resolve_jaxrs` turns
the repo's facts into per-module application paths and per-class sub-resource bases once the
repo-wide constants are known. Honest-null: a path that exists but cannot be resolved → ``None``
(the routes behind it get an empty endpoint); a class reached at two different bases is
ambiguous and keeps its own path (absent).
"""

from __future__ import annotations

import re

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..java.config import module_of
from ..treesitter import node_text
from .config import PROPERTY_KEYS, property_bases, web_xml_bases

FACT = "jaxrs.prefixes"

_HTTP_VERBS = frozenset({"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"})

#: An annotation value: ``("lit", "/x")``, a constant reference ``("ref", "Class.FIELD")``, a
#: concatenation ``("expr", "<owner>\0<source>")`` resolved once constants are known, or
#: ``("unknown", "")`` for anything else (a method call).
PathValue = tuple[str, str]

_IDENT = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")


def _split_plus(text: str) -> list[str]:
    """Split a Java string expression on ``+`` outside string literals."""
    parts, cur, quoted, i = [], [], False, 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quoted and i + 1 < len(text):
            cur.append(text[i:i + 2])
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        if ch == "+" and not quoted:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def resolve_path_text(text: str, owner: str, consts: dict[str, str]) -> str | None:
    """An annotation path argument as written (``"/x"``, ``value = "/x"``, ``P``, ``Api.BASE``,
    ``Api.BASE + "/x"``) → its literal string. A bare name is a constant of ``owner``; a dotted
    name is looked up by its last two segments (``Class.FIELD``). None when any part is not a
    string literal or a known constant — the address is then unknown, never a code name."""
    body = text.strip()
    if body.startswith("value") and "=" in body.split('"', 1)[0]:
        body = body.split("=", 1)[1].strip()
    out: list[str] = []
    for part in _split_plus(body):
        part = part.strip()
        if len(part) >= 2 and part[0] == '"' and part[-1] == '"':
            out.append(part[1:-1])
        elif _IDENT.fullmatch(part):
            key = ".".join(part.split(".")[-2:]) if "." in part else f"{owner}.{part}"
            value = consts.get(key)
            if value is None:
                return None
            out.append(value)
        else:
            return None
    return "".join(out)


@dataclass
class JaxrsFacts:
    """One file's JAX-RS wiring facts."""

    #: (module, value) per ``@ApplicationPath`` in the file.
    app_paths: list[tuple[str, PathValue]] = field(default_factory=list)
    #: class simple name → its class-level ``@Path`` value (resource classes only).
    class_paths: dict[str, PathValue] = field(default_factory=dict)
    #: (declaring class, locator path, returned class simple name) per sub-resource locator.
    locators: list[tuple[str, PathValue, str]] = field(default_factory=list)
    #: nested class simple names declared in the file (top-level names come from the FQCN
    #: index) — a locator's returned type is matched by simple name, so a name declared twice in
    #: the repo is not followed.
    nested: list[str] = field(default_factory=list)
    #: (repo root, module) of a file with a resource class — its module's configured base is
    #: read once in ``_resolve``.
    module_root: tuple[str, str] | None = None


def _annotations(decl: Node) -> list[Node]:
    mods = next((c for c in decl.named_children if c.type == "modifiers"), None)
    return [a for a in (mods.named_children if mods is not None else [])
            if a.type in ("annotation", "marker_annotation")]


def _ann_name(ann: Node, source: bytes) -> str:
    name = ann.child_by_field_name("name")
    return node_text(name, source).rsplit(".", 1)[-1] if name is not None else ""


def _value(ann: Node, source: bytes, owner: str) -> PathValue:
    """The annotation's single value (positional or ``value = …``)."""
    args = ann.child_by_field_name("arguments")
    kids = list(args.named_children) if args is not None else []
    if len(kids) != 1:
        return ("unknown", "")
    v = kids[0]
    if v.type == "element_value_pair":
        key = v.child_by_field_name("key")
        if key is None or node_text(key, source) != "value":
            return ("unknown", "")
        v = v.child_by_field_name("value") or v
    if v.type == "string_literal":
        frag = next((c for c in v.named_children if c.type == "string_fragment"), None)
        return ("lit", node_text(frag, source) if frag is not None else "")
    if v.type == "field_access":
        return ("ref", node_text(v, source))
    if v.type == "identifier":  # a constant of the declaring class
        return ("ref", f"{owner}.{node_text(v, source)}")
    if v.type == "binary_expression":  # Api.BASE + "/x": resolved once constants are known
        return ("expr", f"{owner}\x00{node_text(v, source)}")
    return ("unknown", "")


def resolve_value(v: PathValue, consts: dict[str, str]) -> str | None:
    """A collected annotation value → its literal string, or None when it does not resolve."""
    kind, text = v
    if kind == "lit":
        return text
    if kind == "ref":  # Class.FIELD (a qualified reference keeps its last two segments)
        return consts.get(".".join(text.split(".")[-2:]))
    if kind == "expr":
        owner, expr = text.split("\x00", 1)
        return resolve_path_text(expr, owner, consts)
    return None


def declared_paths(root: Node, source: bytes, consts: dict[str, str]) -> dict[tuple[str, int], str | None]:
    """``@Path`` of every class and method in a file → its literal value (None = present but
    unresolved), keyed by (class simple name, 1-based line where the declaration starts). Read
    from the syntax tree: the captured decorator text drops the quotes of a plain string, so
    ``@Path("users")`` and ``@Path(users)`` (a constant) look the same there."""
    out: dict[tuple[str, int], str | None] = {}

    def visit(node: Node, owner: str | None) -> None:
        for child in node.named_children:
            if child.type in ("class_declaration", "interface_declaration", "enum_declaration"):
                name_node = child.child_by_field_name("name")
                cls = node_text(name_node, source) if name_node is not None else None
                if cls is not None:
                    for ann in _annotations(child):
                        if _ann_name(ann, source) == "Path":
                            out[(cls, child.start_point[0] + 1)] = resolve_value(
                                _value(ann, source, cls), consts)
                visit(child, cls)
            elif child.type == "method_declaration" and owner is not None:
                for ann in _annotations(child):
                    if _ann_name(ann, source) == "Path":
                        out[(owner, child.start_point[0] + 1)] = resolve_value(
                            _value(ann, source, owner), consts)
            else:
                visit(child, owner)

    visit(root, None)
    return out


def _type_name(method: Node, source: bytes) -> str | None:
    t = method.child_by_field_name("type")
    if t is None:
        return None
    if t.type == "type_identifier":
        return node_text(t, source)
    if t.type == "scoped_type_identifier":
        return node_text(t, source).rsplit(".", 1)[-1]
    return None


def collect_jaxrs_facts(root: Node, source: bytes, rel: str) -> JaxrsFacts | None:
    """One Java file's ``@ApplicationPath`` values, class-level ``@Path`` values and
    sub-resource locators. None when the file has no JAX-RS annotations."""
    if b"ws.rs" not in source:
        return None
    facts = JaxrsFacts()
    module = module_of(rel)

    def visit(node: Node, depth: int = 0) -> None:
        for decl in node.named_children:
            if decl.type != "class_declaration":
                continue
            name_node = decl.child_by_field_name("name")
            if name_node is None:
                continue
            cls = node_text(name_node, source)
            if depth:
                facts.nested.append(cls)
            for ann in _annotations(decl):
                aname = _ann_name(ann, source)
                if aname == "ApplicationPath" and module is not None:
                    facts.app_paths.append((module, _value(ann, source, cls)))
                elif aname == "Path":
                    facts.class_paths[cls] = _value(ann, source, cls)
            body = decl.child_by_field_name("body")
            for m in body.named_children if body is not None else []:
                if m.type != "method_declaration":
                    continue
                anns = {_ann_name(a, source): a for a in _annotations(m)}
                returned = _type_name(m, source)
                if "Path" in anns and not (anns.keys() & _HTTP_VERBS) and returned is not None:
                    facts.locators.append((cls, _value(anns["Path"], source, cls), returned))
            if body is not None:
                visit(body, depth + 1)  # nested resource classes

    visit(root)
    if not (facts.app_paths or facts.class_paths or facts.locators or facts.nested):
        return None
    return facts


def _join(*parts: str) -> str:
    segs = [p.strip("/") for p in parts if p and p.strip("/")]
    return "/" + "/".join(segs) if segs else "/"


_UNKNOWN = object()


def resolve_jaxrs(
    facts: Iterable[JaxrsFacts], consts: dict[str, str], top_level: dict[str, int]
) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """→ (module → application path, class simple name → sub-resource base). ``top_level``
    counts the top-level type declarations per simple name across the repo.

    Application path: None when a module's ``@ApplicationPath`` does not resolve or two differ.
    Sub-resource base: the locator's class base + the locator path, composed through nested
    locators. None when a path on the way does not resolve; absent when the class has its own
    ``@Path`` (a root resource), its name is declared more than once in the repo, it is
    reached at two different bases (ambiguous), or not reached from a resource at all."""

    def value(v: PathValue) -> str | None:
        return resolve_value(v, consts)

    app: dict[str, str | None] = {}
    class_paths: dict[str, PathValue] = {}  # any root resource of that simple name (is it one?)
    # returned class → (owner, locator path, the owner file's own @Path values): a locator is
    # declared in its owner, so the owner's @Path is the one in that file — never another
    # package's class of the same simple name.
    locators: dict[str, list[tuple[str, PathValue, dict[str, PathValue]]]] = {}
    declared = dict(top_level)  # class simple name → how many declarations in the repo
    for f in facts:
        for cls in f.nested:
            declared[cls] = declared.get(cls, 0) + 1
        for module, v in f.app_paths:
            resolved = value(v)
            if module in app and app[module] != resolved:
                resolved = None
            app[module] = resolved
        class_paths.update(f.class_paths)
        for owner, path, returned in f.locators:
            locators.setdefault(returned, []).append((owner, path, f.class_paths))

    def base_of(cls: str, seen: frozenset[str], own_file: dict[str, PathValue]) -> object:
        """A locator owner's base: its own @Path in its file, else its locator base; None if not
        a resource, _UNKNOWN if unresolved, or a sub-resource whose name is declared twice."""
        if cls in own_file:
            own = value(own_file[cls])
            return own if own is not None else _UNKNOWN
        if cls in locators and declared.get(cls) != 1:
            return _UNKNOWN  # reached by a locator, but which class of that name is unknown
        return sub_base(cls, seen)

    def sub_base(cls: str, seen: frozenset[str]) -> object:
        if cls in seen or declared.get(cls) != 1:
            return None
        candidates: set[object] = set()
        for owner, path, owner_file in locators.get(cls, []):
            parent = base_of(owner, seen | {cls}, owner_file)
            if parent is None:  # the locator's class is not a resource
                continue
            local = value(path)
            candidates.add(_UNKNOWN if parent is _UNKNOWN or local is None
                           else _join(str(parent), local))
        if len(candidates) != 1:
            return None  # not reached, or reached at two bases → keeps its own path
        return candidates.pop()

    bases: dict[str, str | None] = {}
    for cls in locators:
        if cls in class_paths:
            continue  # a root resource keeps its own @Path
        b = sub_base(cls, frozenset())
        if b is _UNKNOWN:
            bases[cls] = None
        elif isinstance(b, str):
            bases[cls] = b
    return app, bases


@dataclass(frozen=True)
class JaxrsPrefixes:
    #: module root → its ``@ApplicationPath`` (None = present but unresolvable, or two differ).
    app_paths: dict[str, str | None]
    #: sub-resource class simple name → the base its locator serves it under (None = a path on
    #: the way does not resolve).
    sub_bases: dict[str, str | None]


def jaxrs_prefixes(index: Any | None) -> JaxrsPrefixes:
    found: JaxrsPrefixes | None = index_fact(index, FACT)
    return found if found is not None else JaxrsPrefixes({}, {})


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> JaxrsFacts | None:
    facts = collect_jaxrs_facts(root, source, rel)
    module = module_of(rel)
    if facts is not None and repo_root is not None and module is not None \
            and (facts.class_paths or facts.app_paths):
        facts.module_root = (str(repo_root), module)
    return facts


def module_bases(app: dict[str, str | None], modules: set[tuple[str, str]]) -> dict[str, str | None]:
    """Module → its application base, from every source (:mod:`.config`): ``@ApplicationPath``
    (``app``), ``web.xml`` and properties. Two sources that disagree, or a Quarkus key together
    with ``@ApplicationPath`` (Quarkus joins them), → None. A module with no source, while another
    module has one, → None; with no source anywhere → absent (no prefix)."""
    out: dict[str, str | None] = {}
    for repo, module in sorted(modules):
        values: list[str | None] = []
        joined = False
        if module in app:
            values.append(app[module])
        module_dir = Path(repo) / module if module else Path(repo)
        values.extend(web_xml_bases(module_dir))
        for key, value in property_bases(Path(repo), module):
            values.append(value)
            joined = joined or PROPERTY_KEYS[key]
        if not values:
            continue
        distinct = set(values)
        out[module] = None if len(distinct) > 1 or None in distinct or (
            joined and module in app) else values[0]
    for module in app:
        out.setdefault(module, app[module])
    if out:
        for _, module in modules:
            out.setdefault(module, None)  # another module sets a base: this one may be under it
    return out


def _resolve(values: list[JaxrsFacts], index: Any) -> JaxrsPrefixes | None:
    # Top-level type declarations per simple name, from the FQCN index: an FQCN declared in two
    # files maps to None there (counted twice — only "exactly one" matters).
    top_level: dict[str, int] = {}
    for fqcn, rel in (getattr(index, "fqcn", None) or {}).items():
        simple = fqcn.rsplit(".", 1)[-1]
        top_level[simple] = top_level.get(simple, 0) + (1 if rel is not None else 2)
    app_paths, sub_bases = resolve_jaxrs(values, getattr(index, "consts", None) or {}, top_level)
    app_paths = module_bases(app_paths, {f.module_root for f in values if f.module_root})
    if not app_paths and not sub_bases:
        return None
    return JaxrsPrefixes(app_paths, sub_bases)


register_fact(FactSpec(name=FACT, language="java", gate=(b"ws.rs",), collect=_collect, resolve=_resolve))
