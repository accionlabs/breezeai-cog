"""Additive detector: HotChocolate roots declared only by registration (the v11/v12 shape).

A class can be a GraphQL root solely because the composition root registers it::

    builder.Services.AddGraphQLServer().AddQueryType<BookQueries>();   // Program.cs
    public class BookQueries { public Book GetBookById(int id) => …; }  // BookQueries.cs

``BookQueries.cs`` carries nothing HotChocolate, so no parser claims it and nothing in the file says
it is an API. The fact lives in another file, so this detector has two stages
(see :mod:`..additive`):

* **index stage** — while the C# index is built, ``_collect`` records every registration with the
  namespaces in scope at the registering file, and ``_resolve`` binds each one to the file that
  declares the class, using the C# binding rules in :mod:`..csharp.imports`;
* **run** — while ``BookQueries.cs`` is parsed (by whichever parser owns it), ``_emit`` adds its
  operations from that fact.

A file the HotChocolate parser owns is left to the parser, which reads the same fact; the two
never both emit for one file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import DetectContext, Detector, index_fact, register_detector
from ..csharp.imports import CSharpIndex, declared_files_by_name, in_scope_namespaces, type_files
from ..treesitter import node_text
from .mappings import owned_by_hotchocolate
from .routes import detect_hotchocolate_routes

#: Root registration calls → operation kind.
_REGISTRATIONS = {
    "AddQueryType": "query", "AddMutationType": "mutation", "AddSubscriptionType": "subscription",
}

#: The detector's name, which is also the name its fact is stored under on the C# index.
NAME = "hotchocolate-registered-roots"


@dataclass(frozen=True)
class RegisteredRoots:
    """Registered roots, resolved.

    ``files``: declaring file → {simple class name: kind} — keyed by file so two same-named
    classes in different namespaces or projects are told apart the way the compiler binds the
    registration. ``types``: simple name → kind, only when **every** class of that name in the
    repo is a registered root of that one kind — the lookup for a name seen without its
    declaration (an ``[ExtendObjectType(typeof(BookQueries))]`` target); otherwise absent.
    """

    files: dict[str, dict[str, str]] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)


def registered_roots(index: Any | None) -> RegisteredRoots:
    """The resolved fact, or an empty one (no index, e.g. single-file parsing)."""
    roots = index_fact(index, NAME)
    return roots if isinstance(roots, RegisteredRoots) else RegisteredRoots()


def _collect(
    root: Node, source: bytes, rel: str, repo_root: Path | None,
) -> list[tuple[str, str, str, tuple[str, ...]]] | None:
    """Each ``AddQueryType<T>()`` / ``AddMutationType<T>()`` / ``AddSubscriptionType<T>()`` call,
    with the namespaces in scope there: ``(file, kind, type name, scopes)``.

    Any builder chain counts — ``AddGraphQLServer()`` (ASP.NET), ``AddGraphQL()`` (other hosts
    such as Azure Functions) and the v10/v11 ``SchemaBuilder.New()`` all register roots the same
    way, and a root missed here is a root whose fields are later read as a data type's.
    Read from the tree, not the bytes, so a commented-out registration is not one. Only the
    generic form names a class; ``AddQueryType(d => d.Name("Query"))`` names a schema type.
    """
    scopes: tuple[str, ...] | None = None
    found: list[tuple[str, str, str, tuple[str, ...]]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != "generic_name":
            continue
        ident = next((c for c in node.named_children if c.type == "identifier"), None)
        kind = _REGISTRATIONS.get(node_text(ident, source)) if ident is not None else None
        access = node.parent
        if kind is None or access is None or access.type != "member_access_expression":
            continue
        if access.parent is None or access.parent.type != "invocation_expression":
            continue
        targs = next((c for c in node.named_children if c.type == "type_argument_list"), None)
        if targs is None or len(targs.named_children) != 1:
            continue
        name = node_text(targs.named_children[0], source)
        if "<" in name:  # a generic type argument is not a class we can bind
            continue
        if scopes is None:
            scopes = tuple(sorted(in_scope_namespaces(root, source)))
        found.append((rel, kind, name, scopes))
    return found or None


def _resolve(collected: list[Any], index: CSharpIndex) -> RegisteredRoots:
    """Bind each registration to the class it names. A class registered under two different
    kinds (two composition roots disagreeing) is dropped: one is wrong and nothing says which.

    A registering file is never listed in ``files`` (which is what ``_emit`` and the parser emit
    routes for): it is the composition root, which stays with its own parser and keeps its REST
    routes. A root declared beside its registration still counts for the name-only ``types``
    lookup.
    """
    registered: dict[tuple[str, str], str | None] = {}
    registering: set[str] = set()
    for rel, kind, name, scopes in sorted(r for per_file in collected for r in per_file):
        registering.add(rel)
        simple = name.rsplit(".", 1)[-1]
        for decl in type_files(name, set(scopes) | index.global_usings, index, rel):
            prev = registered.get((decl, simple), kind)
            registered[(decl, simple)] = kind if prev == kind else None
    roots = RegisteredRoots()
    for (decl, simple), kind in registered.items():
        if kind is not None and decl not in registering:
            roots.files.setdefault(decl, {})[simple] = kind
    declared = declared_files_by_name(index)
    for simple in sorted({s for _, s in registered}):
        kinds = {k for (_, s), k in registered.items() if s == simple}
        files = {d for (d, s) in registered if s == simple}
        only = next(iter(kinds)) if len(kinds) == 1 else None
        if only is not None and files == declared.get(simple):
            roots.types[simple] = only
    return roots


def _emit(dc: DetectContext) -> str | None:
    """Operations of the registered roots declared in this file, unless the HotChocolate parser
    owns the file (it emits them itself, from the same fact)."""
    if owned_by_hotchocolate(dc.source):
        return None
    roots = registered_roots(dc.ctx.resolution_index)
    own = roots.files.get(dc.path)
    if not own:  # the cheap check for every other C# file: one dictionary lookup
        return None
    seen = {s.id for s in dc.record.statements}
    routes = detect_hotchocolate_routes(
        dc.record, dc.root, dc.source, seen, dc.ctx.resolution_index,
        hc_root_types={**roots.types, **own})
    if not routes:
        return None
    dc.record.statements.extend(routes)
    return "graphql"


register_detector(Detector(
    name=NAME, language="csharp", order=30, run=_emit,
    guard=None,  # the marker is in another file; _emit's first check is a dictionary lookup
    skip_fixtures=True, frameworks=("graphql",),
    index_gate=tuple(f"{call}<".encode() for call in _REGISTRATIONS),
    collect=_collect, resolve=_resolve,
))
