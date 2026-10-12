"""JAX-RS wiring facts for the Java repo index: ``@ApplicationPath`` and sub-resource locators.

Two constructs put a prefix in front of a JAX-RS resource's routes from outside its class:

* ``@ApplicationPath("/api")`` on the ``Application`` subclass — the base of every resource of
  that application (scoped to the Maven/Gradle module, like the Spring context path).
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

from collections.abc import Iterable
from dataclasses import dataclass, field

from tree_sitter import Node

from ..treesitter import node_text
from .config import module_of

_HTTP_VERBS = frozenset({"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"})

#: An annotation value: ``("lit", "/x")``, a constant reference ``("ref", "Class.FIELD")``, or
#: ``("unknown", "")`` for anything else (a concatenation, a method call).
PathValue = tuple[str, str]


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
    return ("unknown", "")


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
        kind, text = v
        if kind == "lit":
            return text
        if kind == "ref":
            return consts.get(text)
        return None

    app: dict[str, str | None] = {}
    class_paths: dict[str, PathValue] = {}
    locators: dict[str, list[tuple[str, PathValue]]] = {}
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
            locators.setdefault(returned, []).append((owner, path))

    def base_of(cls: str, seen: frozenset[str]) -> object:
        """A class's base: its own @Path, else its locator base; None if not a resource,
        _UNKNOWN if unresolved or ambiguous-unknown."""
        if cls in class_paths:
            own = value(class_paths[cls])
            return own if own is not None else _UNKNOWN
        return sub_base(cls, seen)

    def sub_base(cls: str, seen: frozenset[str]) -> object:
        if cls in seen or declared.get(cls) != 1:
            return None
        candidates: set[object] = set()
        for owner, path in locators.get(cls, []):
            parent = base_of(owner, seen | {cls})
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
